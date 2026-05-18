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
* ``/`` and ``/static`` — the bundled web frontend (static files).

The heavy web dependencies (``fastapi``, ``uvicorn``) are isolated in the
``se3[server]`` optional-dependency extra. Nothing in the core ``se3`` CLI
imports this module, so a core-only install never loads FastAPI. The
``se3-server`` console script (see :func:`se3.server.main`) is the only entry
point and checks for the extra before importing this module.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from se3.daemon import protocol

from .state import ServerState
from .ws import ConnectionManager, handle_daemon_connection

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


# -- request models --------------------------------------------------------


class NewFlowRequest(BaseModel):
    """Body of ``POST /api/flows`` — publish a new task to a machine."""

    machine_id: str
    task: str
    task_type: str = "feature"
    project_root: str = ""


class RespondRequest(BaseModel):
    """Body of ``POST /api/flows/{id}/respond`` — answer a pending call."""

    response: Any
    call_id: str = ""


def create_app() -> FastAPI:
    """Build and return the SE3 central-server FastAPI application."""
    app = FastAPI(title="SE3 Central Server", version=protocol.PROTOCOL_VERSION)
    state = ServerState()
    manager = ConnectionManager()
    # Expose for tests / introspection.
    app.state.server_state = state
    app.state.connection_manager = manager

    # -- daemon WebSocket endpoint -----------------------------------------

    @app.websocket("/ws")
    async def daemon_ws(websocket: WebSocket) -> None:
        await handle_daemon_connection(websocket, manager, state)

    # -- REST API ----------------------------------------------------------

    @app.get("/api/health")
    async def health() -> dict:
        return {"status": "ok", "protocol_version": protocol.PROTOCOL_VERSION}

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
        if not manager.is_connected(machine_id):
            raise HTTPException(
                status_code=404,
                detail=f"machine '{machine_id}' is not connected",
            )
        message = protocol.make_spawn_flow(
            task, project_root=req.project_root, task_type=req.task_type
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

    # -- frontend (static files) -------------------------------------------

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    return app


def run(host: str = "127.0.0.1", port: int = 8080, *, log_level: str = "info") -> None:
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
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="Bind port (default: 8080)")
    parser.add_argument(
        "--log-level", default="info", help="uvicorn log level (default: info)"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    run(args.host, args.port, log_level=args.log_level)
