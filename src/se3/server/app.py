"""The SE3 central-server FastAPI application.

This module assembles the FastAPI app that aggregates state from many SE3
daemons and exposes it to the web frontend:

* ``WS  /ws`` — the daemon connection endpoint (see :mod:`se3.server.ws`);
* ``GET /api/health`` — liveness probe;
* ``GET /api/machines`` — all connected machines;
* ``GET /api/machines/{id}/flows`` — flows on one machine;
* ``GET /api/flows/{id}`` — one flow's detail;
* ``POST /api/flows`` — publish a new task (routed to a daemon as SPAWN_FLOW);
* ``POST /api/flows/{id}/respond`` — answer a flow's pending interjection/call;
* ``POST /api/flows/{id}/interject`` — inject a mid-flow instruction into a flow;
* ``GET /api/history`` — the aggregated history-session index;
* ``GET /api/history/{id}`` — one flow's history records (pulled on demand);
* ``/`` and ``/static`` — the bundled web frontend (static files).

The heavy web dependencies (``fastapi``, ``uvicorn``) are isolated in the
``se3[server]`` optional-dependency extra. Nothing in the core ``se3`` CLI
imports this module, so a core-only install never loads FastAPI. The
``se3-server`` console script (see :func:`se3.server.main`) is the only entry
point and checks for the extra before importing this module.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from se3 import __version__
from se3.daemon import protocol

from .state import ServerState
from .ws import (
    ConnectionManager,
    HistoryRequestRegistry,
    IndexRefreshRegistry,
    UiHub,
    broadcast_index_refresh,
    handle_daemon_connection,
    handle_ui_connection,
    request_history,
)

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

#: Seconds a ``GET /api/history/{flow_id}`` cache-miss waits for the owning
#: daemon to answer the on-demand ``MSG_HISTORY_REQUEST`` before giving up.
#: Sized to leave headroom for a cold first pull of a large session's jsonl
#: history (the daemon offloads the read to a thread, but the disk read itself
#: still takes real time on a multi-MB session); a shorter window risks 504s
#: even when the daemon is healthy and still reading.
HISTORY_PULL_TIMEOUT = 30.0

#: Seconds ``GET /api/history`` waits for connected daemons to answer the
#: broadcast ``MSG_HISTORY_INDEX_REQUEST`` (a forced index re-push) before it
#: gives up and returns whatever index is currently cached. Kept short so the
#: history list refreshes promptly on entry without blocking the response when
#: a daemon is slow or unreachable.
HISTORY_INDEX_REFRESH_TIMEOUT = 2.0


# -- request models --------------------------------------------------------


class NewFlowRequest(BaseModel):
    """Body of ``POST /api/flows`` — publish a new task to a machine."""

    machine_id: str
    task: str
    task_type: str = "feature"
    project_root: str = ""
    discover: bool = False


class RespondRequest(BaseModel):
    """Body of ``POST /api/flows/{id}/respond`` — answer a pending call."""

    response: Any
    call_id: str = ""


class InterjectRequest(BaseModel):
    """Body of ``POST /api/flows/{id}/interject`` — inject a mid-flow instruction."""

    text: str


def create_app() -> FastAPI:
    """Build and return the SE3 central-server FastAPI application."""
    app = FastAPI(title="SE3 Central Server", version=protocol.PROTOCOL_VERSION)
    state = ServerState()
    manager = ConnectionManager()
    ui_hub = UiHub()
    history_registry = HistoryRequestRegistry()
    index_refresh_registry = IndexRefreshRegistry()
    # Expose for tests / introspection.
    app.state.server_state = state
    app.state.connection_manager = manager
    app.state.ui_hub = ui_hub
    app.state.history_registry = history_registry
    app.state.index_refresh_registry = index_refresh_registry

    # -- daemon WebSocket endpoint -----------------------------------------

    @app.websocket("/ws")
    async def daemon_ws(websocket: WebSocket) -> None:
        await handle_daemon_connection(
            websocket, manager, state, ui_hub, history_registry, index_refresh_registry
        )

    # -- web-frontend WebSocket endpoint -----------------------------------

    @app.websocket("/ws/ui")
    async def ui_ws(websocket: WebSocket) -> None:
        await handle_ui_connection(websocket, ui_hub, state)

    # -- REST API ----------------------------------------------------------

    @app.get("/api/health")
    async def health() -> dict:
        return {"status": "ok", "protocol_version": protocol.PROTOCOL_VERSION}

    @app.get("/api/version")
    async def version() -> dict:
        return {"version": __version__}

    @app.get("/api/machines")
    async def list_machines() -> dict:
        machines = await state.get_machines()
        return {"machines": machines, "count": len(machines)}

    @app.get("/api/machines/{machine_id}/flows")
    async def machine_flows(machine_id: str) -> dict:
        flows = await state.get_machine_flows(machine_id)
        if flows is None:
            raise HTTPException(status_code=404, detail=f"machine '{machine_id}' not found")
        return {"machine_id": machine_id, "flows": flows, "count": len(flows)}

    @app.get("/api/flows/{flow_id}")
    async def flow_detail(flow_id: str) -> dict:
        result = await state.get_flow(flow_id)
        if result is None:
            raise HTTPException(status_code=404, detail=f"flow '{flow_id}' not found")
        machine_id, flow = result
        return {"machine_id": machine_id, "flow": flow}

    @app.post("/api/flows")
    async def publish_flow(req: NewFlowRequest) -> JSONResponse:
        task = req.task.strip()
        if not task:
            raise HTTPException(status_code=422, detail="'task' must not be empty")
        machine_id = req.machine_id.strip()
        if not machine_id:
            raise HTTPException(status_code=422, detail="'machine_id' must not be empty")
        project_root = req.project_root.strip()
        if not project_root:
            raise HTTPException(
                status_code=422, detail="'project_root' must not be empty"
            )
        # Only enforce absolute-path shape — the target need not be a known
        # machine.project_roots entry. The owning daemon auto-runs `se3 init`
        # on first use, so a freshly typed brand-new directory is valid input.
        if not os.path.isabs(project_root):
            raise HTTPException(
                status_code=422,
                detail=f"'project_root' must be an absolute path, got {project_root!r}",
            )
        if not manager.is_connected(machine_id):
            raise HTTPException(
                status_code=404,
                detail=f"machine '{machine_id}' is not connected",
            )
        message = protocol.make_spawn_flow(
            task,
            project_root=project_root,
            task_type=req.task_type,
            discover=req.discover,
        )
        ok = await manager.send_to(machine_id, message)
        if not ok:
            raise HTTPException(
                status_code=503,
                detail=f"failed to deliver SPAWN_FLOW to '{machine_id}'",
            )
        return JSONResponse(
            status_code=202,
            content={"status": "dispatched", "machine_id": machine_id, "task": task},
        )

    @app.post("/api/flows/{flow_id}/respond")
    async def respond_flow(flow_id: str, req: RespondRequest) -> dict:
        result = await state.get_flow(flow_id)
        if result is None:
            raise HTTPException(status_code=404, detail=f"flow '{flow_id}' not found")
        machine_id, flow = result
        if not manager.is_connected(machine_id):
            raise HTTPException(
                status_code=404,
                detail=f"machine '{machine_id}' owning flow '{flow_id}' is not connected",
            )
        call_id = req.call_id.strip()
        if not call_id:
            # Default to the flow's first pending call when none is named.
            pending = flow.get("pending_calls") or []
            if pending:
                call_id = str(pending[0].get("call_id") or "")
        if not call_id:
            raise HTTPException(
                status_code=422,
                detail="no 'call_id' supplied and the flow has no pending call",
            )
        message = protocol.make_respond_call(
            call_id, req.response, project_root=flow.get("project_root", "")
        )
        ok = await manager.send_to(machine_id, message)
        if not ok:
            raise HTTPException(
                status_code=503,
                detail=f"failed to deliver RESPOND_CALL to '{machine_id}'",
            )
        return {"status": "dispatched", "machine_id": machine_id, "call_id": call_id}

    @app.post("/api/flows/{flow_id}/interject")
    async def interject_flow(flow_id: str, req: InterjectRequest) -> dict:
        """Deliver a mid-flow user interjection to a running flow.

        Unlike ``/respond`` (which answers an *existing* pending call), this
        endpoint pushes a fresh instruction into a flow that has no pending
        call: the owning daemon turns it into an ``interjection``-kind call
        file that ``se3 run`` drains at the next step boundary.
        """
        text = req.text.strip()
        if not text:
            raise HTTPException(status_code=422, detail="'text' must not be empty")
        result = await state.get_flow(flow_id)
        if result is None:
            raise HTTPException(status_code=404, detail=f"flow '{flow_id}' not found")
        machine_id, flow = result
        if not manager.is_connected(machine_id):
            raise HTTPException(
                status_code=503,
                detail=f"machine '{machine_id}' owning flow '{flow_id}' is not connected",
            )
        message = protocol.make_interject_flow(
            flow_id, text, project_root=flow.get("project_root", "")
        )
        ok = await manager.send_to(machine_id, message)
        if not ok:
            raise HTTPException(
                status_code=503,
                detail=f"failed to deliver INTERJECT_FLOW to '{machine_id}'",
            )
        return {"status": "dispatched", "machine_id": machine_id, "flow_id": flow_id}

    # -- history API -------------------------------------------------------
    # The server is a pure in-memory relay: ``/api/history`` serves the
    # aggregated index daemons have pushed, and ``/api/history/{flow_id}``
    # serves cached records, pulling them on demand from the owning daemon
    # on a cache miss. Nothing here is persisted to disk.

    @app.get("/api/history")
    async def list_history() -> dict:
        # Entering the history view must always reflect the latest sessions, not
        # whatever index a daemon last happened to push. Actively ask every
        # connected daemon to rebuild and re-push its index, then briefly wait
        # for those re-pushes to land before aggregating. With no connected
        # daemon — or when a daemon is slow and the wait times out — we degrade
        # gracefully to the currently cached index and still return 200.
        waiters = await broadcast_index_refresh(manager, index_refresh_registry)
        if waiters:
            try:
                await asyncio.wait(
                    list(waiters.values()),
                    timeout=HISTORY_INDEX_REFRESH_TIMEOUT,
                )
            finally:
                # Drop every parked waiter regardless of whether it resolved,
                # so a late re-push never leaves a dangling future behind.
                for machine_id, fut in waiters.items():
                    index_refresh_registry.discard(machine_id, fut)
        sessions = await state.get_history_index()
        return {"sessions": sessions, "count": len(sessions)}

    @app.get("/api/history/{flow_id}")
    async def history_detail(flow_id: str) -> dict:
        cached = await state.get_history(flow_id)
        if cached is not None:
            return {"flow_id": flow_id, "cached": True, **cached}
        # Cache miss: pull on demand from the daemon owning this flow.
        fut = history_registry.register(flow_id)
        sent = await request_history(manager, state, flow_id)
        if not sent:
            history_registry.discard(flow_id, fut)
            raise HTTPException(
                status_code=404,
                detail=f"no connected daemon owns history for flow '{flow_id}'",
            )
        try:
            data = await asyncio.wait_for(fut, timeout=HISTORY_PULL_TIMEOUT)
        except asyncio.TimeoutError:
            history_registry.discard(flow_id, fut)
            raise HTTPException(
                status_code=504,
                detail=f"timed out pulling history for flow '{flow_id}'",
            )
        return {"flow_id": flow_id, "cached": False, **(data or {})}

    # -- frontend (static files) -------------------------------------------
    # Mounted last so the API routes and WebSocket endpoints above take
    # precedence. ``html=True`` serves ``index.html`` for ``/`` and lets the
    # bundled ``style.css`` / ``app.js`` load from the same origin, so the
    # frontend's WebSocket connects back without a cross-origin step.

    if STATIC_DIR.is_dir():
        app.mount(
            "/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static"
        )

    return app


def run(
    host: str = "127.0.0.1",
    port: int = protocol.DEFAULT_SERVER_PORT,
    *,
    log_level: str = "info",
) -> None:
    """Start the SE3 central server with uvicorn (blocking)."""
    import uvicorn

    uvicorn.run(create_app(), host=host, port=port, log_level=log_level)


def main(argv: Optional[list] = None) -> None:
    """``se3-server`` console-script entry point.

    Parses ``--host`` / ``--port`` and runs the server. Kept dependency-light
    (argparse only) so the friendly missing-extra check in
    :func:`se3.server.main` stays the first thing a user without the extra
    sees.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="se3-server", description="SE3 central control-plane server"
    )
    parser.add_argument(
        "--version",
        "-v",
        action="version",
        version=f"se3-server version {__version__}",
        help="Show version information",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument(
        "--port",
        type=int,
        default=protocol.DEFAULT_SERVER_PORT,
        help=f"Bind port (default: {protocol.DEFAULT_SERVER_PORT})",
    )
    parser.add_argument(
        "--log-level", default="info", help="uvicorn log level (default: info)"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    run(args.host, args.port, log_level=args.log_level)
