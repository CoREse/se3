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
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from se3 import __version__
from se3.daemon import protocol

from . import crypto
from .bootstrap import DEFAULT_DB_PATH
from .auth.base import OwnerIdentity, ProviderChain
from .auth.local import PROVIDER_LOCAL, LocalAuthProvider
from .auth.ratelimit import LoginRateLimited, LoginRateLimiter, RateLimitConfig
from .auth.registry import (
    PROVIDER_OIDC,
    PROVIDER_PROXY_HEADER,
    build_provider_chain,
    make_require_owner,
)
from .auth.session import CookieConfig, SessionStore, read_cookie
from .identity import IdentityService
from .persistence import IdentityAlreadyBound, Store
from .state import ServerState
from .ws import (
    ConnectionManager,
    HistoryRequestRegistry,
    IndexRefreshRegistry,
    InterjectionEventTracker,
    IssueCommandRegistry,
    UiHub,
    _PullAbandoned,
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

#: Seconds an issue write endpoint (create / edit / close / reopen) waits for
#: the daemon to acknowledge the ``MSG_ISSUE_COMMAND`` before giving up.
#: Issue operations are lightweight YAML I/O so a short timeout suffices.
ISSUE_COMMAND_TIMEOUT = 10.0

#: When the daemon ack does not arrive within :data:`ISSUE_COMMAND_TIMEOUT`,
#: the server does NOT immediately report failure — a heavy daemon-side snapshot
#: can delay the ack past the window even though the issue already landed on
#: disk. Instead it spends up to this many seconds reconciling against the
#: in-memory issue mirror (refreshed by STATUS_UPDATE) to confirm the
#: operation's post-condition, and only reports failure if the window elapses
#: with the change still absent. This bounds the wait so the request never
#: blocks indefinitely.
ISSUE_RECONCILE_TIMEOUT = 15.0

#: Interval between issue-mirror polls inside the reconcile window. Each poll is
#: a cheap in-memory lookup, so a sub-second cadence picks up the next
#: STATUS_UPDATE promptly without busy-spinning.
ISSUE_RECONCILE_POLL_INTERVAL = 0.5

#: The identity-binding discriminator + external id of the single break-glass
#: admin subject. Break-glass is deliberately a *single* admin subject (not a
#: per-user impersonation channel), so every consumed break-glass token resolves
#: to this same stable internal owner; distinguishing real owners is the local
#: auth provider's job. See the multi-tenant design's break-glass section.
BREAKGLASS_PROVIDER = "breakglass"
BREAKGLASS_EXTERNAL_ID = "admin"


# -- request models --------------------------------------------------------


class NewFlowRequest(BaseModel):
    """Body of ``POST /api/flows`` — publish a new task to a machine.

    When *from_issue_id* is supplied the flow is sourced from an existing
    issue: the issue's owner-scoped record is the authoritative source of the
    target machine / project, the request's *task* content is ignored (the
    issue description becomes the task), and the daemon drives the issue
    lifecycle via the ``se3 run --from-issue`` CLI path.  *task* is optional in
    that case, so it defaults to an empty string.
    """

    machine_id: str = ""
    task: str = ""
    task_type: str = "feature"
    project_root: str = ""
    discover: bool = False
    from_issue_id: str = ""


class RespondRequest(BaseModel):
    """Body of ``POST /api/flows/{id}/respond`` — answer a pending call."""

    response: Any
    call_id: str = ""


class InterjectRequest(BaseModel):
    """Body of ``POST /api/flows/{id}/interject`` — inject a mid-flow instruction."""

    text: str


class LoginRequest(BaseModel):
    """Body of ``POST /api/auth/login`` — local username + password login."""

    username: str
    password: str


class BreakglassRequest(BaseModel):
    """Body of ``POST /api/auth/breakglass`` — one-time admin escape hatch."""

    token: str


class CreateDaemonKeyRequest(BaseModel):
    """Body of ``POST /api/daemon-keys`` — mint a new daemon key for the owner."""

    label: str = ""


class CreateUserRequest(BaseModel):
    """Body of ``POST /api/users`` — admin creates / invites a local user."""

    username: str
    password: str
    display_name: str = ""
    is_admin: bool = False


class SetPasswordRequest(BaseModel):
    """Body of ``POST /api/users/{owner_id}/password`` — admin resets a password."""

    password: str


class SetAdminRequest(BaseModel):
    """Body of ``POST /api/users/{owner_id}/admin`` — admin toggles the admin flag."""

    is_admin: bool


class CreateIssueRequest(BaseModel):
    """Body of ``POST /api/issues`` — create a new issue on a daemon."""

    machine_id: str
    project_root: str
    description: str
    title: str = ""
    priority: str = ""
    type: str = ""
    scope: str = ""
    tags: list = []


class EditIssueRequest(BaseModel):
    """Body of ``PATCH /api/issues/{id}`` — edit an existing issue."""

    machine_id: str = ""
    project_root: str = ""
    title: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[str] = None
    type: Optional[str] = None
    scope: Optional[str] = None
    tags: Optional[list] = None


class CloseIssueRequest(BaseModel):
    """Body of ``POST /api/issues/{id}/close`` — close an issue."""

    machine_id: str = ""
    project_root: str = ""
    reason: str = ""


class ReopenIssueRequest(BaseModel):
    """Body of ``POST /api/issues/{id}/reopen`` — reopen a closed issue."""

    machine_id: str = ""
    project_root: str = ""


def _scope_for(identity: OwnerIdentity) -> Optional[str]:
    """Map an authenticated identity to the owner-scoping value for queries.

    A regular owner is scoped to its own ``owner_id`` — it can see and control
    only its own daemons. An admin (the break-glass subject, or any owner with
    the admin flag) is given the unscoped/operator view (``None``), so an
    operator console can observe every machine. This is the single place the
    "what may this identity see" policy is decided.
    """
    return None if identity.is_admin else identity.owner_id


def _ensure_breakglass_admin(store: Store) -> str:
    """Resolve (or lazily create) the single stable break-glass admin owner.

    Break-glass is one admin subject by construction, so every token consumption
    resolves to the same internal ``owner_id`` (bound via the reserved
    ``(breakglass, admin)`` identity). The owner is created on first use with the
    admin flag set.
    """
    owner_id = store.resolve_owner_by_identity(BREAKGLASS_PROVIDER, BREAKGLASS_EXTERNAL_ID)
    if owner_id is None:
        owner_id = store.create_owner("break-glass admin", is_admin=True)
        store.link_identity(owner_id, BREAKGLASS_PROVIDER, BREAKGLASS_EXTERNAL_ID)
    return owner_id


def create_app(
    *,
    store: Optional[Store] = None,
    db_path: Optional[str] = None,
    auth_config: Optional[dict] = None,
    session_store: Optional[SessionStore] = None,
    rate_limiter: Optional[LoginRateLimiter] = None,
) -> FastAPI:
    """Build and return the SE3 central-server FastAPI application.

    The app is multi-tenant from the ground up (no more identity-unaware "bare"
    mode): a pluggable auth provider chain resolves every ``/api/*`` and
    ``/ws/ui`` request to an :class:`OwnerIdentity`, and all machine / flow /
    history views are filtered by that owner. Assembly is **fail-closed** — if
    no usable auth provider is configured, :class:`AuthNotConfigured` is raised
    here and the server refuses to start rather than serving anonymously.

    *store* / *db_path* select the persistence backend (defaults to an in-memory
    sqlite store). *auth_config* is the ``server.auth`` sub-mapping driving
    provider selection (``None`` ⇒ the built-in local provider). *session_store*
    / *rate_limiter* are injectable for tests.
    """
    app = FastAPI(title="SE3 Central Server", version=protocol.PROTOCOL_VERSION)
    state = ServerState()
    manager = ConnectionManager()
    ui_hub = UiHub()
    history_registry = HistoryRequestRegistry()
    index_refresh_registry = IndexRefreshRegistry()
    issue_command_registry = IssueCommandRegistry()
    interjection_tracker = InterjectionEventTracker()

    # -- auth / identity wiring (fail-closed) ------------------------------
    if store is None:
        store = Store(db_path or ":memory:")
    # NB: explicit ``is None`` checks — ``SessionStore`` defines ``__len__`` so
    # a fresh (empty) one is falsy; ``session_store or SessionStore()`` would
    # silently discard a caller-injected empty store.
    sessions = session_store if session_store is not None else SessionStore()
    rate = rate_limiter if rate_limiter is not None else LoginRateLimiter()
    identity = IdentityService(store)
    # Raises AuthNotConfigured when nothing can authenticate — the server then
    # refuses to start instead of falling back to the old open control plane.
    auth_chain: ProviderChain = build_provider_chain(
        auth_config, store=store, sessions=sessions, rate_limiter=rate
    )
    require_owner = make_require_owner(auth_chain)
    # The local provider (when present) owns the username+password login
    # ceremony; resolution of an established session is provider-agnostic.
    local_provider: Optional[LocalAuthProvider] = next(
        (p for p in auth_chain.providers if isinstance(p, LocalAuthProvider)), None
    )

    # Expose for tests / introspection.
    app.state.server_state = state
    app.state.connection_manager = manager
    app.state.ui_hub = ui_hub
    app.state.history_registry = history_registry
    app.state.index_refresh_registry = index_refresh_registry
    app.state.interjection_tracker = interjection_tracker
    app.state.store = store
    app.state.identity = identity
    app.state.sessions = sessions
    app.state.rate_limiter = rate
    app.state.auth_chain = auth_chain

    def _set_session_cookie(response: Response, session_id: str) -> None:
        cfg = sessions.cookie_config
        response.set_cookie(
            key=cfg.name,
            value=session_id,
            max_age=cfg.max_age,
            httponly=cfg.http_only,
            samesite=cfg.same_site,
            secure=cfg.secure,
            path=cfg.path,
        )

    # -- daemon WebSocket endpoint -----------------------------------------

    @app.websocket("/ws")
    async def daemon_ws(websocket: WebSocket) -> None:
        # The daemon channel authenticates via the HELLO key (key -> owner_id),
        # NOT via the human session cookie. Passing the identity service makes
        # a missing / invalid key fail-closed (WELCOME accepted=false + close).
        await handle_daemon_connection(
            websocket,
            manager,
            state,
            ui_hub,
            history_registry,
            index_refresh_registry,
            interjection_tracker,
            identity=identity,
            issue_registry=issue_command_registry,
        )

    # -- web-frontend WebSocket endpoint -----------------------------------

    @app.websocket("/ws/ui")
    async def ui_ws(websocket: WebSocket) -> None:
        # Resolve the connecting human before accepting any data. An
        # unauthenticated socket is fail-closed (accepted then immediately
        # closed). An authenticated owner is scoped to its own machines; an
        # admin gets the unscoped operator view.
        who = auth_chain.resolve_owner(websocket)
        if who is None:
            await handle_ui_connection(
                websocket, ui_hub, state, owner=None, require_owner=True
            )
            return
        await handle_ui_connection(
            websocket, ui_hub, state, owner=_scope_for(who), require_owner=False
        )

    # -- auth API ----------------------------------------------------------
    # login / logout / me / breakglass. These are the only unauthenticated
    # entry points (besides health/version); every other /api/* route below
    # requires a resolved owner via Depends(require_owner).

    @app.post("/api/auth/login")
    async def login(req: LoginRequest, response: Response) -> dict:
        if local_provider is None:
            raise HTTPException(
                status_code=503, detail="local password login is not enabled"
            )
        try:
            # argon2 verification is CPU-bound — run it off the event loop.
            result = await asyncio.to_thread(
                local_provider.login, req.username, req.password
            )
        except LoginRateLimited as exc:
            raise HTTPException(
                status_code=429,
                detail="too many failed login attempts; try again later",
                headers={"Retry-After": str(int(exc.retry_after) + 1)},
            )
        if result is None:
            # Uniform message for unknown-user vs bad-password (no enumeration).
            raise HTTPException(status_code=401, detail="invalid credentials")
        session_id, who = result
        _set_session_cookie(response, session_id)
        return {
            "owner_id": who.owner_id,
            "display_name": who.display_name,
            "is_admin": who.is_admin,
            "provider": who.provider,
        }

    @app.post("/api/auth/logout")
    async def logout(request: Request, response: Response) -> dict:
        # Idempotent: destroy the referenced session (if any) and clear the
        # cookie. Never requires an already-valid session.
        session_id = read_cookie(request, sessions.cookie_config.name)
        sessions.destroy(session_id)
        response.delete_cookie(
            sessions.cookie_config.name, path=sessions.cookie_config.path
        )
        return {"status": "logged_out"}

    @app.get("/api/auth/me")
    async def me(identity_: OwnerIdentity = Depends(require_owner)) -> dict:
        return {
            "owner_id": identity_.owner_id,
            "display_name": identity_.display_name,
            "is_admin": identity_.is_admin,
            "provider": identity_.provider,
        }

    @app.post("/api/auth/breakglass")
    async def breakglass(req: BreakglassRequest, response: Response) -> dict:
        # The break-glass token is a credential: hash it for the constant-time
        # one-shot consume, and never log the plaintext.
        token = req.token.strip()
        if not token:
            raise HTTPException(status_code=422, detail="'token' must not be empty")
        consumed = await asyncio.to_thread(
            store.consume_breakglass, crypto.token_hash(token)
        )
        if not consumed:
            raise HTTPException(
                status_code=401, detail="invalid or expired break-glass token"
            )
        owner_id = _ensure_breakglass_admin(store)
        session_id, _session = sessions.create(owner_id)
        _set_session_cookie(response, session_id)
        logger.info("break-glass token consumed; admin session minted")
        return {"owner_id": owner_id, "is_admin": True, "provider": BREAKGLASS_PROVIDER}

    # -- REST API ----------------------------------------------------------

    @app.get("/api/health")
    async def health() -> dict:
        return {"status": "ok", "protocol_version": protocol.PROTOCOL_VERSION}

    @app.get("/api/version")
    async def version() -> dict:
        return {"version": __version__}

    @app.get("/api/machines")
    async def list_machines(
        identity_: OwnerIdentity = Depends(require_owner),
    ) -> dict:
        machines = await state.get_machines(owner=_scope_for(identity_))
        return {"machines": machines, "count": len(machines)}

    @app.get("/api/machines/{machine_id}/flows")
    async def machine_flows(
        machine_id: str, identity_: OwnerIdentity = Depends(require_owner)
    ) -> dict:
        flows = await state.get_machine_flows(
            machine_id, owner=_scope_for(identity_)
        )
        # 404 covers both "unknown" and "owned by another owner" — no
        # cross-owner existence leak.
        if flows is None:
            raise HTTPException(status_code=404, detail=f"machine '{machine_id}' not found")
        return {"machine_id": machine_id, "flows": flows, "count": len(flows)}

    @app.get("/api/flows/{flow_id}")
    async def flow_detail(
        flow_id: str, identity_: OwnerIdentity = Depends(require_owner)
    ) -> dict:
        result = await state.get_flow(flow_id, owner=_scope_for(identity_))
        if result is None:
            raise HTTPException(status_code=404, detail=f"flow '{flow_id}' not found")
        machine_id, flow = result
        return {"machine_id": machine_id, "flow": flow}

    async def _publish_flow_from_issue(
        req: NewFlowRequest,
        identity_: OwnerIdentity,
        from_issue_id: str,
    ) -> JSONResponse:
        """Dispatch a SPAWN_FLOW sourced from an existing issue.

        The issue's owner-scoped record is the authoritative source of the
        target machine / project — the request's *task* content is ignored and
        ``from_issue_id`` is threaded through to the daemon, which runs
        ``se3 run --from-issue <id>`` and owns the full issue lifecycle
        (in-progress on start, resolved/open on exit).  Only ``open`` issues
        can be launched from the UI; the daemon still performs the final
        in-progress race check.
        """
        scope = _scope_for(identity_)
        # The owner-scoped lookup resolves the issue's authoritative
        # machine / project.  A request-supplied machine_id / project_root
        # narrows the search, so an inconsistent target reads as not-found.
        result = await state.get_issue_by_id(
            from_issue_id,
            owner=scope,
            machine_id=req.machine_id.strip() or None,
            project_root=req.project_root.strip() or None,
        )
        if result is None:
            # Covers unknown, cross-owner, and target-inconsistent issues.
            raise HTTPException(
                status_code=404, detail=f"issue '{from_issue_id}' not found"
            )
        machine_id, project_root, issue = result
        status = str(issue.get("status") or "").strip().lower()
        if status != "open":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"issue '{from_issue_id}' is not open (status {status!r}); "
                    "only open issues can start a flow"
                ),
            )
        if not manager.is_connected(machine_id):
            raise HTTPException(
                status_code=404,
                detail=f"machine '{machine_id}' owning issue '{from_issue_id}' "
                "is not connected",
            )
        message = protocol.make_spawn_flow(
            "",
            project_root=project_root,
            task_type=req.task_type,
            discover=req.discover,
            from_issue_id=from_issue_id,
        )
        ok = await manager.send_to(machine_id, message)
        if not ok:
            raise HTTPException(
                status_code=503,
                detail=f"failed to deliver SPAWN_FLOW to '{machine_id}'",
            )
        return JSONResponse(
            status_code=202,
            content={
                "status": "dispatched",
                "machine_id": machine_id,
                "from_issue_id": from_issue_id,
            },
        )

    @app.post("/api/flows")
    async def publish_flow(
        req: NewFlowRequest, identity_: OwnerIdentity = Depends(require_owner)
    ) -> JSONResponse:
        from_issue_id = req.from_issue_id.strip()
        if from_issue_id:
            return await _publish_flow_from_issue(req, identity_, from_issue_id)
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
        # Ownership gate: an owner may only dispatch to its OWN daemon. A
        # machine that is unknown OR belongs to another owner reads as absent
        # (404) — this is what closes the former remote-arbitrary-command-exec
        # / cross-owner-dispatch hole.
        owned = await state.get_machine(machine_id, owner=_scope_for(identity_))
        if owned is None:
            raise HTTPException(
                status_code=404, detail=f"machine '{machine_id}' not found"
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
    async def respond_flow(
        flow_id: str,
        req: RespondRequest,
        identity_: OwnerIdentity = Depends(require_owner),
    ) -> dict:
        # Ownership gate: a flow on another owner's machine reads as absent.
        result = await state.get_flow(flow_id, owner=_scope_for(identity_))
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
    async def interject_flow(
        flow_id: str,
        req: InterjectRequest,
        identity_: OwnerIdentity = Depends(require_owner),
    ) -> dict:
        """Deliver a mid-flow user interjection to a running flow.

        Unlike ``/respond`` (which answers an *existing* pending call), this
        endpoint pushes a fresh instruction into a flow that has no pending
        call: the owning daemon turns it into an ``interjection``-kind call
        file that ``se3 run`` drains at the next step boundary.
        """
        text = req.text.strip()
        if not text:
            raise HTTPException(status_code=422, detail="'text' must not be empty")
        # Ownership gate: a flow on another owner's machine reads as absent.
        result = await state.get_flow(flow_id, owner=_scope_for(identity_))
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


    @app.post("/api/flows/{flow_id}/resume")
    async def resume_flow(
        flow_id: str,
        identity_: OwnerIdentity = Depends(require_owner),
    ) -> JSONResponse:
        """Resume a paused or failed flow.

        The flow must be in a directly-resumable status (FAILED or PAUSED),
        must not be archived/history-only, and must belong to the requesting
        owner. The owning daemon receives a ``MSG_SPAWN_FLOW`` carrying the
        ``resume_flow_id`` field; the daemon validates the local
        ``engine.json`` and spawns ``se3 run --resume --flow-id <id>``.
        """
        scope = _scope_for(identity_)
        result = await state.is_flow_resumable(flow_id, owner=scope)
        if result is None:
            # Covers: unknown flow, cross-owner, non-resumable status, or
            # archived/history-only (not in the live flow set at all).
            raise HTTPException(
                status_code=404,
                detail=f"flow '{flow_id}' not found or not resumable",
            )
        machine_id, flow = result
        if not manager.is_connected(machine_id):
            raise HTTPException(
                status_code=404,
                detail=f"machine '{machine_id}' owning flow '{flow_id}' is not connected",
            )
        message = protocol.make_spawn_flow(
            "",  # task_description is unused for resume
            project_root=flow.get("project_root", ""),
            resume_flow_id=flow_id,
        )
        ok = await manager.send_to(machine_id, message)
        if not ok:
            raise HTTPException(
                status_code=503,
                detail=f"failed to deliver resume SPAWN_FLOW to '{machine_id}'",
            )
        return JSONResponse(
            status_code=202,
            content={
                "status": "resume_dispatched",
                "machine_id": machine_id,
                "flow_id": flow_id,
            },
        )

    # -- issue management API -----------------------------------------------
    # Issues are an in-memory mirror of each daemon's on-disk YAML files,
    # refreshed on every STATUS_UPDATE.  Write operations (create/edit/close/
    # reopen) are dispatched as MSG_ISSUE_COMMAND to the owning daemon which
    # applies them via IssueManager; the next STATUS_UPDATE reflects the change.

    @app.get("/api/issues")
    async def list_issues(
        machine_id: str = "",
        project_root: str = "",
        include_closed: bool = False,
        source: str = "",
        type: str = "",
        identity_: OwnerIdentity = Depends(require_owner),
    ) -> dict:
        """List issues across all machines (or filtered by machine/root).

        Defaults to open issues only; pass ``include_closed=True`` to include
        resolved/closed/won't-fix.
        """
        issues = await state.get_issues(
            owner=_scope_for(identity_),
            machine_id=machine_id or None,
            project_root=project_root or None,
            include_closed=include_closed,
            source=source or None,
            type_filter=type or None,
        )
        return {"issues": issues, "count": len(issues)}

    @app.get("/api/issues/{issue_id}")
    async def get_issue(
        issue_id: str,
        machine_id: str = "",
        project_root: str = "",
        identity_: OwnerIdentity = Depends(require_owner),
    ) -> dict:
        """Get a single issue by ID."""
        result = await state.get_issue_by_id(
            issue_id,
            owner=_scope_for(identity_),
            machine_id=machine_id or None,
            project_root=project_root or None,
        )
        if result is None:
            raise HTTPException(
                status_code=404, detail=f"issue '{issue_id}' not found"
            )
        mid, root, issue = result
        return {"machine_id": mid, "project_root": root, "issue": issue}

    async def _reconcile_issue_command(
        *,
        operation: str,
        machine_id: str,
        project_root: str,
        owner: Optional[str],
        baseline_ids: Optional[set] = None,
        target_issue_id: Optional[str] = None,
        expected_fields: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Poll the in-memory issue mirror to confirm the operation landed.

        Used as the stop-the-bleeding fallback when the daemon ack does not
        arrive in time: the issue may already be on disk, with the ack merely
        delayed behind a heavy daemon-side snapshot.  Polls ``state``'s issue
        mirror (refreshed by STATUS_UPDATE) for up to
        :data:`ISSUE_RECONCILE_TIMEOUT` seconds, checking the operation's
        expected post-condition.  Returns the affected ``issue_id`` once the
        change is observed, or ``None`` if the window elapses with the change
        still absent (a genuine failure / lost command).

        * ``create`` — a new human-sourced issue under *project_root* whose id
          is absent from *baseline_ids* (the pre-send id set).
        * ``edit`` — *target_issue_id* now reflects every *expected_fields*
          value.
        * ``close`` — *target_issue_id* is no longer open / in-progress.
        * ``reopen`` — *target_issue_id* is open again.
        """

        def _field_matches(actual: Any, expected: Any) -> bool:
            if isinstance(expected, (list, tuple)):
                actual_list = actual if isinstance(actual, (list, tuple)) else []
                return list(actual_list) == list(expected)
            return str(actual or "") == str(expected or "")

        async def _check_once() -> Optional[str]:
            if operation == "create":
                issues = await state.get_issues(
                    owner=owner,
                    machine_id=machine_id,
                    project_root=project_root,
                    include_closed=True,
                )
                base = baseline_ids or set()
                for iss in issues:
                    iid = str(iss.get("id") or "")
                    if (
                        iid
                        and iid not in base
                        and str(iss.get("source") or "") == "human"
                    ):
                        return iid
                return None
            # edit / close / reopen all key off a known target id.
            if not target_issue_id:
                return None
            found = await state.get_issue_by_id(
                target_issue_id,
                owner=owner,
                machine_id=machine_id,
                project_root=project_root,
            )
            if found is None:
                return None
            _, _, iss = found
            status = str(iss.get("status") or "open")
            if operation == "close":
                return target_issue_id if status not in ("open", "in-progress") else None
            if operation == "reopen":
                return target_issue_id if status == "open" else None
            if operation == "edit":
                for key, val in (expected_fields or {}).items():
                    if not _field_matches(iss.get(key), val):
                        return None
                return target_issue_id
            return None

        loop = asyncio.get_event_loop()
        deadline = loop.time() + ISSUE_RECONCILE_TIMEOUT
        while True:
            iid = await _check_once()
            if iid is not None:
                return iid
            if loop.time() >= deadline:
                return None
            await asyncio.sleep(ISSUE_RECONCILE_POLL_INTERVAL)

    async def _send_issue_command(
        machine_id: str,
        message: protocol.Message,
        request_id: str,
        *,
        operation: str,
        project_root: str,
        owner: Optional[str] = None,
        baseline_ids: Optional[set] = None,
        target_issue_id: Optional[str] = None,
        expected_fields: Optional[Dict[str, Any]] = None,
    ) -> dict:
        """Send an issue command and wait for the daemon's acknowledgment.

        Registers a future in *issue_command_registry*, dispatches the message,
        and awaits the result with :data:`ISSUE_COMMAND_TIMEOUT`.  Returns the
        daemon's result payload dict on success.

        On ack timeout it does NOT immediately fail: it reconciles against the
        issue mirror (see :func:`_reconcile_issue_command`) for up to
        :data:`ISSUE_RECONCILE_TIMEOUT` seconds, since the issue may already be
        on disk with only the ack delayed.  If the expected post-condition is
        observed it returns a synthesised success result (``ok=True`` plus the
        affected ``issue_id`` and ``reconciled=True``); only if the reconcile
        window elapses with the change still absent does it raise the 504.
        Delivery failure still raises 503 immediately.
        """
        fut = issue_command_registry.register(request_id)
        sent = await manager.send_to(machine_id, message)
        if not sent:
            issue_command_registry.discard(request_id, fut)
            raise HTTPException(
                status_code=503,
                detail=f"failed to deliver ISSUE_COMMAND to '{machine_id}'",
            )
        try:
            result = await asyncio.wait_for(fut, timeout=ISSUE_COMMAND_TIMEOUT)
        except asyncio.TimeoutError:
            issue_command_registry.discard(request_id, fut)
            reconciled_id = await _reconcile_issue_command(
                operation=operation,
                machine_id=machine_id,
                project_root=project_root,
                owner=owner,
                baseline_ids=baseline_ids,
                target_issue_id=target_issue_id,
                expected_fields=expected_fields,
            )
            if reconciled_id is not None:
                logger.info(
                    "issue command '%s' ack timed out from '%s' but reconciled "
                    "against the issue mirror (issue_id=%s); reporting success",
                    operation,
                    machine_id,
                    reconciled_id,
                )
                return {
                    "ok": True,
                    "issue_id": reconciled_id,
                    "reconciled": True,
                }
            raise HTTPException(
                status_code=504,
                detail=f"timed out waiting for issue command result from '{machine_id}'",
            )
        return result

    @app.post("/api/issues")
    async def create_issue(
        req: CreateIssueRequest,
        identity_: OwnerIdentity = Depends(require_owner),
    ) -> JSONResponse:
        """Create a new issue on a daemon."""
        description = req.description.strip()
        if not description:
            raise HTTPException(
                status_code=422, detail="'description' must not be empty"
            )
        machine_id = req.machine_id.strip()
        if not machine_id:
            raise HTTPException(
                status_code=422, detail="'machine_id' must not be empty"
            )
        project_root = req.project_root.strip()
        if not project_root:
            raise HTTPException(
                status_code=422, detail="'project_root' must not be empty"
            )
        if not os.path.isabs(project_root):
            raise HTTPException(
                status_code=422,
                detail=f"'project_root' must be an absolute path, got {project_root!r}",
            )
        # Ownership gate
        owned = await state.get_machine(machine_id, owner=_scope_for(identity_))
        if owned is None:
            raise HTTPException(
                status_code=404, detail=f"machine '{machine_id}' not found"
            )
        if not manager.is_connected(machine_id):
            raise HTTPException(
                status_code=503,
                detail=f"machine '{machine_id}' is not connected",
            )
        # Reconcile baseline: snapshot the project's current issue ids BEFORE
        # dispatching, so the timeout fallback can detect a newly-landed issue
        # by set difference (the daemon assigns the new id, so the server can
        # only recognise it as "an id that wasn't here before").
        owner = _scope_for(identity_)
        baseline = await state.get_issues(
            owner=owner,
            machine_id=machine_id,
            project_root=project_root,
            include_closed=True,
        )
        baseline_ids = {str(i.get("id") or "") for i in baseline}
        request_id = uuid.uuid4().hex
        message = protocol.make_issue_command(
            "create",
            project_root=project_root,
            description=description,
            title=req.title,
            priority=req.priority,
            type=req.type,
            scope=req.scope if req.scope else None,
            tags=req.tags if req.tags else None,
            request_id=request_id,
        )
        result = await _send_issue_command(
            machine_id,
            message,
            request_id,
            operation="create",
            project_root=project_root,
            owner=owner,
            baseline_ids=baseline_ids,
        )
        if not result.get("ok"):
            raise HTTPException(
                status_code=422,
                detail=result.get("error") or "issue creation failed on daemon",
            )
        return JSONResponse(
            status_code=201,
            content={
                "status": "created",
                "machine_id": machine_id,
                "issue_id": result.get("issue_id", ""),
            },
        )

    @app.patch("/api/issues/{issue_id}")
    async def edit_issue(
        issue_id: str,
        req: EditIssueRequest,
        identity_: OwnerIdentity = Depends(require_owner),
    ) -> dict:
        """Edit an existing issue (title, description, priority, type, tags).

        If ``machine_id`` and ``project_root`` are not supplied, the server
        resolves them from the issue mirror.
        """
        machine_id = req.machine_id.strip()
        project_root = req.project_root.strip()
        if not machine_id or not project_root:
            result = await state.get_issue_by_id(
                issue_id,
                owner=_scope_for(identity_),
                machine_id=machine_id or None,
                project_root=project_root or None,
            )
            if result is None:
                raise HTTPException(
                    status_code=404, detail=f"issue '{issue_id}' not found"
                )
            machine_id, project_root, _ = result
        # Ownership gate
        owned = await state.get_machine(machine_id, owner=_scope_for(identity_))
        if owned is None:
            raise HTTPException(
                status_code=404, detail=f"machine '{machine_id}' not found"
            )
        if not manager.is_connected(machine_id):
            raise HTTPException(
                status_code=503,
                detail=f"machine '{machine_id}' is not connected",
            )
        kwargs: Dict[str, Any] = {}
        if req.title is not None:
            kwargs["title"] = req.title
        if req.description is not None:
            kwargs["description"] = req.description
        if req.priority is not None:
            kwargs["priority"] = req.priority
        if req.type is not None:
            kwargs["type"] = req.type
        if req.scope is not None:
            kwargs["scope"] = req.scope
        if req.tags is not None:
            kwargs["tags"] = req.tags
        if not kwargs:
            raise HTTPException(
                status_code=422, detail="no fields to update"
            )
        request_id = uuid.uuid4().hex
        message = protocol.make_issue_command(
            "edit",
            project_root=project_root,
            issue_id=issue_id,
            request_id=request_id,
            **kwargs,
        )
        result = await _send_issue_command(
            machine_id,
            message,
            request_id,
            operation="edit",
            project_root=project_root,
            owner=_scope_for(identity_),
            target_issue_id=issue_id,
            expected_fields=kwargs,
        )
        if not result.get("ok"):
            raise HTTPException(
                status_code=422,
                detail=result.get("error") or "issue edit failed on daemon",
            )
        return {"status": "updated", "machine_id": machine_id, "issue_id": issue_id}

    @app.post("/api/issues/{issue_id}/close")
    async def close_issue(
        issue_id: str,
        req: CloseIssueRequest,
        identity_: OwnerIdentity = Depends(require_owner),
    ) -> dict:
        """Close an issue."""
        machine_id = req.machine_id.strip()
        project_root = req.project_root.strip()
        if not machine_id or not project_root:
            result = await state.get_issue_by_id(
                issue_id,
                owner=_scope_for(identity_),
                machine_id=machine_id or None,
                project_root=project_root or None,
            )
            if result is None:
                raise HTTPException(
                    status_code=404, detail=f"issue '{issue_id}' not found"
                )
            machine_id, project_root, _ = result
        owned = await state.get_machine(machine_id, owner=_scope_for(identity_))
        if owned is None:
            raise HTTPException(
                status_code=404, detail=f"machine '{machine_id}' not found"
            )
        if not manager.is_connected(machine_id):
            raise HTTPException(
                status_code=503,
                detail=f"machine '{machine_id}' is not connected",
            )
        request_id = uuid.uuid4().hex
        message = protocol.make_issue_command(
            "close",
            project_root=project_root,
            issue_id=issue_id,
            reason=req.reason,
            request_id=request_id,
        )
        result = await _send_issue_command(
            machine_id,
            message,
            request_id,
            operation="close",
            project_root=project_root,
            owner=_scope_for(identity_),
            target_issue_id=issue_id,
        )
        if not result.get("ok"):
            raise HTTPException(
                status_code=422,
                detail=result.get("error") or "issue close failed on daemon",
            )
        return {"status": "closed", "machine_id": machine_id, "issue_id": issue_id}

    @app.post("/api/issues/{issue_id}/reopen")
    async def reopen_issue(
        issue_id: str,
        req: ReopenIssueRequest,
        identity_: OwnerIdentity = Depends(require_owner),
    ) -> dict:
        """Reopen a closed issue."""
        machine_id = req.machine_id.strip()
        project_root = req.project_root.strip()
        if not machine_id or not project_root:
            result = await state.get_issue_by_id(
                issue_id,
                owner=_scope_for(identity_),
                machine_id=machine_id or None,
                project_root=project_root or None,
            )
            if result is None:
                raise HTTPException(
                    status_code=404, detail=f"issue '{issue_id}' not found"
                )
            machine_id, project_root, _ = result
        owned = await state.get_machine(machine_id, owner=_scope_for(identity_))
        if owned is None:
            raise HTTPException(
                status_code=404, detail=f"machine '{machine_id}' not found"
            )
        if not manager.is_connected(machine_id):
            raise HTTPException(
                status_code=503,
                detail=f"machine '{machine_id}' is not connected",
            )
        request_id = uuid.uuid4().hex
        message = protocol.make_issue_command(
            "reopen",
            project_root=project_root,
            issue_id=issue_id,
            request_id=request_id,
        )
        result = await _send_issue_command(
            machine_id,
            message,
            request_id,
            operation="reopen",
            project_root=project_root,
            owner=_scope_for(identity_),
            target_issue_id=issue_id,
        )
        if not result.get("ok"):
            raise HTTPException(
                status_code=422,
                detail=result.get("error") or "issue reopen failed on daemon",
            )
        return {"status": "reopened", "machine_id": machine_id, "issue_id": issue_id}

    # -- daemon-key self-management ----------------------------------------
    # An owner mints / lists / revokes its OWN daemon keys (the credential a
    # daemon presents in its HELLO). The plaintext key is shown exactly once,
    # at creation; the list view returns metadata only. Every route is scoped
    # to ``identity_.owner_id`` — a key is a *personal* credential, so even an
    # admin manages only its own keys here (no cross-owner key administration).

    @app.post("/api/daemon-keys")
    async def create_daemon_key(
        req: CreateDaemonKeyRequest,
        identity_: OwnerIdentity = Depends(require_owner),
    ) -> JSONResponse:
        label = req.label.strip() or None
        # High-entropy token: only its hash is persisted, the plaintext is
        # returned to the caller once and never stored / logged.
        plaintext, key_hash = crypto.generate_token("dk")
        key_id = await asyncio.to_thread(
            store.issue_daemon_key, identity_.owner_id, key_hash, label
        )
        return JSONResponse(
            status_code=201,
            content={
                "key_id": key_id,
                "key": plaintext,
                "label": label,
                "owner_id": identity_.owner_id,
            },
        )

    @app.get("/api/daemon-keys")
    async def list_daemon_keys(
        identity_: OwnerIdentity = Depends(require_owner),
    ) -> dict:
        keys = await asyncio.to_thread(store.list_daemon_keys, identity_.owner_id)
        # Metadata only — never the plaintext (gone after creation) nor the hash.
        return {
            "keys": [
                {
                    "key_id": k.key_id,
                    "label": k.label,
                    "created_at": k.created_at,
                    "revoked_at": k.revoked_at,
                    "revoked": k.revoked,
                }
                for k in keys
            ],
            "count": len(keys),
        }

    @app.delete("/api/daemon-keys/{key_id}")
    async def revoke_daemon_key(
        key_id: str, identity_: OwnerIdentity = Depends(require_owner)
    ) -> dict:
        # Ownership gate: list the caller's own keys and require membership.
        # A key_id belonging to another owner (or unknown) reads as absent
        # (404) — no cross-owner existence leak, and no cross-owner revoke.
        owned = await asyncio.to_thread(store.list_daemon_keys, identity_.owner_id)
        if not any(k.key_id == key_id for k in owned):
            raise HTTPException(
                status_code=404, detail=f"daemon key '{key_id}' not found"
            )
        await asyncio.to_thread(store.revoke_daemon_key, key_id)
        return {"status": "revoked", "key_id": key_id}

    # -- admin user-management guards --------------------------------------
    # Every user-management route enforces admin independently (no reliance on
    # the frontend hiding the entry), then layers the break-glass / self /
    # last-admin / local-only protections on top. These local helpers keep the
    # guard logic in one place so no route can forget a check.

    def _require_admin(identity_: OwnerIdentity, action: str = "manage users") -> None:
        """Reject a non-admin caller with 403 (independent per-route check)."""
        if not identity_.is_admin:
            raise HTTPException(
                status_code=403,
                detail=f"admin privileges required to {action}",
            )

    def _breakglass_owner_id() -> Optional[str]:
        """Resolve the break-glass admin owner_id, or ``None`` if not yet created.

        This is a pure *lookup* — unlike :func:`_ensure_breakglass_admin` it never
        creates the owner. Break-glass is a real owner but a reserved escape-hatch
        subject: it is filtered out of the manageable user list and refused for
        every delete / demote / password-reset operation.
        """
        return store.resolve_owner_by_identity(
            BREAKGLASS_PROVIDER, BREAKGLASS_EXTERNAL_ID
        )

    def _owner_provider_set(owner_id: str) -> set:
        """Return the set of auth providers bound to ``owner_id``."""
        return {provider for provider, _external in store.list_identities(owner_id)}

    # The last-real-admin invariant is no longer counted here on the event loop
    # and then mutated separately (that read-then-write was racy: two concurrent
    # demote/delete requests could each observe count > 1 and both commit). It is
    # now enforced atomically inside ``Store.delete_owner_guarded`` /
    # ``Store.set_admin_guarded`` — the count check and the write share one held
    # write lock — with the break-glass subject excluded as non-admin headroom.

    # -- admin user provisioning -------------------------------------------
    # An admin creates / invites a local user: a new owner + ("local",
    # username) binding + password hash, in one atomic insert. v1 deliberately
    # exposes NO public self-registration endpoint (its email-verification /
    # anti-abuse / password-recovery debt is out of scope — see the design's
    # non-goals); the only way to add a user is an admin calling here.

    @app.post("/api/users")
    async def create_user(
        req: CreateUserRequest, identity_: OwnerIdentity = Depends(require_owner)
    ) -> JSONResponse:
        # Only an admin (a local admin owner, or the break-glass admin subject)
        # may provision users.
        _require_admin(identity_, "create users")
        username = req.username.strip()
        if not username:
            raise HTTPException(status_code=422, detail="'username' must not be empty")
        if not req.password:
            raise HTTPException(status_code=422, detail="'password' must not be empty")
        display_name = req.display_name.strip() or username
        # argon2 hashing is CPU-bound — keep it off the event loop.
        password_hash = await asyncio.to_thread(crypto.hash_password, req.password)
        try:
            new_owner_id = await asyncio.to_thread(
                store.create_local_user,
                PROVIDER_LOCAL,
                username,
                password_hash,
                display_name=display_name,
                is_admin=req.is_admin,
            )
        except IdentityAlreadyBound:
            raise HTTPException(
                status_code=409, detail=f"username {username!r} already exists"
            )
        logger.info(
            "admin %s created user %r (owner %s, admin=%s)",
            identity_.owner_id,
            username,
            new_owner_id,
            req.is_admin,
        )
        return JSONResponse(
            status_code=201,
            content={
                "owner_id": new_owner_id,
                "username": username,
                "display_name": display_name,
                "is_admin": req.is_admin,
            },
        )

    @app.get("/api/users")
    async def list_users(
        identity_: OwnerIdentity = Depends(require_owner),
    ) -> dict:
        """List manageable owners (admin only).

        The break-glass escape-hatch owner is filtered out server-side — it is a
        reserved subject, not a manageable account. Only a whitelist of
        non-sensitive fields is serialized; password / key hashes never appear.
        """
        _require_admin(identity_, "list users")
        bg = _breakglass_owner_id()
        users = []
        for owner in store.list_owners():
            if owner.owner_id == bg:
                continue
            identities = store.list_identities(owner.owner_id)
            providers = {provider for provider, _external in identities}
            # The first binding's provider is the account's origin; ``can_reset_
            # password`` is true only for owners carrying a local credential.
            provider = identities[0][0] if identities else None
            users.append(
                {
                    "owner_id": owner.owner_id,
                    "display_name": owner.display_name,
                    "is_admin": owner.is_admin,
                    "created_at": owner.created_at,
                    "provider": provider,
                    "can_reset_password": PROVIDER_LOCAL in providers,
                }
            )
        return {"users": users, "count": len(users)}

    @app.delete("/api/users/{owner_id}")
    async def delete_user(
        owner_id: str, identity_: OwnerIdentity = Depends(require_owner)
    ) -> dict:
        """Delete a user (admin only), cascading its bindings / creds / keys.

        Refuses to delete the caller themselves, the break-glass subject (hidden
        as 404), or the last remaining real admin (409) — none of these may be
        removed via the regular UI without locking out management.
        """
        _require_admin(identity_, "delete users")
        owner = store.get_owner(owner_id)
        if owner is None:
            raise HTTPException(status_code=404, detail=f"user '{owner_id}' not found")
        if owner_id == identity_.owner_id:
            raise HTTPException(
                status_code=403, detail="cannot delete your own account"
            )
        if owner_id == _breakglass_owner_id():
            # Hide the reserved subject's existence rather than confirm it.
            raise HTTPException(status_code=404, detail=f"user '{owner_id}' not found")
        # The last-real-admin guard is enforced atomically inside the store: the
        # admin-count check and the DELETE commit happen under one held write
        # lock, so two concurrent deletions of two distinct real admins cannot
        # each observe a stale count > 1 and both commit (leaving zero admins).
        result = await asyncio.to_thread(
            store.delete_owner_guarded,
            owner_id,
            breakglass_owner_id=_breakglass_owner_id(),
        )
        if result == "not_found":
            raise HTTPException(status_code=404, detail=f"user '{owner_id}' not found")
        if result == "last_admin":
            raise HTTPException(
                status_code=409, detail="cannot delete the last remaining admin"
            )
        logger.info("admin %s deleted user %s", identity_.owner_id, owner_id)
        return {"status": "deleted", "owner_id": owner_id}

    @app.post("/api/users/{owner_id}/password")
    async def reset_user_password(
        owner_id: str,
        req: SetPasswordRequest,
        identity_: OwnerIdentity = Depends(require_owner),
    ) -> dict:
        """Reset a *local* user's password (admin only).

        Only owners carrying a local credential may have their password reset;
        OIDC / proxy-header owners have no local credential, so resetting one is
        meaningless (409). The plaintext is hashed off the event loop and never
        logged.
        """
        _require_admin(identity_, "reset passwords")
        owner = store.get_owner(owner_id)
        if owner is None:
            raise HTTPException(status_code=404, detail=f"user '{owner_id}' not found")
        if owner_id == _breakglass_owner_id():
            raise HTTPException(status_code=404, detail=f"user '{owner_id}' not found")
        if PROVIDER_LOCAL not in _owner_provider_set(owner_id):
            raise HTTPException(
                status_code=409,
                detail="password reset is only available for local users",
            )
        if not req.password:
            raise HTTPException(status_code=422, detail="'password' must not be empty")
        # argon2 hashing is CPU-bound — keep it off the event loop. The plaintext
        # never reaches the log; only the owner_id and outcome are recorded.
        password_hash = await asyncio.to_thread(crypto.hash_password, req.password)
        await asyncio.to_thread(store.set_password, owner_id, password_hash)
        logger.info("admin %s reset the password for user %s", identity_.owner_id, owner_id)
        return {"status": "password_reset", "owner_id": owner_id}

    @app.post("/api/users/{owner_id}/admin")
    async def set_user_admin(
        owner_id: str,
        req: SetAdminRequest,
        identity_: OwnerIdentity = Depends(require_owner),
    ) -> dict:
        """Toggle a user's admin flag (admin only).

        Promotion is unrestricted (besides the break-glass refusal). Demotion is
        guarded: the caller cannot demote themselves (403), nor demote the last
        remaining real admin (409), which would lock management out.
        """
        _require_admin(identity_, "change admin status")
        owner = store.get_owner(owner_id)
        if owner is None:
            raise HTTPException(status_code=404, detail=f"user '{owner_id}' not found")
        if owner_id == _breakglass_owner_id():
            raise HTTPException(status_code=404, detail=f"user '{owner_id}' not found")
        # Self-demotion is rejected up front (an unconditional rule). The
        # last-real-admin demotion guard is enforced atomically inside the store
        # (count check + UPDATE under one held write lock), so two concurrent
        # demotions of two distinct real admins cannot both pass a stale count.
        if not req.is_admin and owner_id == identity_.owner_id:
            raise HTTPException(
                status_code=403,
                detail="cannot revoke your own admin privileges",
            )
        result = await asyncio.to_thread(
            store.set_admin_guarded,
            owner_id,
            req.is_admin,
            breakglass_owner_id=_breakglass_owner_id(),
        )
        if result == "not_found":
            raise HTTPException(status_code=404, detail=f"user '{owner_id}' not found")
        if result == "last_admin":
            raise HTTPException(
                status_code=409, detail="cannot demote the last remaining admin"
            )
        logger.info(
            "admin %s set admin=%s for user %s",
            identity_.owner_id,
            req.is_admin,
            owner_id,
        )
        return {"owner_id": owner_id, "is_admin": req.is_admin}

    # -- history API -------------------------------------------------------
    # The server is a pure in-memory relay: ``/api/history`` serves the
    # aggregated index daemons have pushed, and ``/api/history/{flow_id}``
    # serves cached records, pulling them on demand from the owning daemon
    # on a cache miss. Nothing here is persisted to disk.

    @app.get("/api/history")
    async def list_history(
        identity_: OwnerIdentity = Depends(require_owner),
    ) -> dict:
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
        index = await state.get_history_index(owner=_scope_for(identity_))
        return {"sessions": index, "count": len(index)}

    @app.get("/api/history/{flow_id}")
    async def history_detail(
        flow_id: str,
        identity_: OwnerIdentity = Depends(require_owner),
        after: Optional[str] = None,
    ) -> dict:
        # Ownership gate first: a flow whose owning machine belongs to another
        # owner (or is unknown) reads as absent — even if its records happen to
        # be cached server-side — so one owner can never pull another's history.
        scope = _scope_for(identity_)
        owner_machine = await state.find_machine_for_history_flow(flow_id, owner=scope)
        if owner_machine is None:
            raise HTTPException(
                status_code=404,
                detail=f"no history for flow '{flow_id}'",
            )
        target_owner = await state.get_machine_owner(owner_machine)
        if target_owner is None or (scope is not None and target_owner != scope):
            raise HTTPException(
                status_code=404,
                detail=f"no history for flow '{flow_id}'",
            )
        # Cache hit: serve a full or incremental snapshot atomically. ``after``
        # is the opaque progress token the client echoes on a WS reconnect; the
        # snapshot returns only the tail after it when it still pins the current
        # bundle (``delivery: "delta"``), and the full records on every fallback
        # (``delivery: "full"``). Binding ``expected_machine_id`` to the owning
        # machine makes a bundle that has since moved daemons read as a miss, so
        # we re-pull the authoritative records below rather than serve a stale
        # snapshot.
        snapshot = await state.get_history_snapshot(
            flow_id,
            after=after,
            expected_machine_id=owner_machine,
            expected_owner=target_owner,
        )
        if snapshot is not None:
            return {"flow_id": flow_id, "cached": True, **snapshot}
        # Cache miss (no bundle, or the bundle's machine no longer matches the
        # owning daemon): pull on demand from the daemon owning this flow. Any
        # ``after`` token is ignored — the freshly pulled records are always
        # returned as a full snapshot with a new progress token.
        owner_connection = await manager.get_connection(owner_machine)
        if owner_connection is None:
            raise HTTPException(
                status_code=404,
                detail=f"no connected daemon owns history for flow '{flow_id}'",
            )
        # Concurrent cache-miss requests for the same flow/machine (e.g. the
        # running-flow view and the history-detail view reconnecting at once)
        # share ONE in-flight daemon pull: only the leader sends the
        # ``MSG_HISTORY_REQUEST``, the followers park on the same reply. This
        # prevents a second daemon reply from arriving after both waiters were
        # already resolved by the first — which, finding no waiter, would
        # replace the cache generation and broadcast ``mode: full`` to every UI
        # consumer, clearing the progress tokens REST just handed back.
        #
        # The loop lets a follower whose leader failed *before* dispatching a
        # daemon request (``_PullAbandoned``) retry as a fresh leader instead of
        # waiting out ``HISTORY_PULL_TIMEOUT`` behind a pull that will never be
        # answered.
        while True:
            fut, is_leader = history_registry.begin_pull(flow_id, owner_machine)
            pull_dispatched = False
            try:
                if is_leader:
                    # The leader's daemon send is INSIDE this try so the
                    # ``finally`` also covers a cancellation that fires while
                    # ``send_text`` is blocked: without it, a client
                    # disconnecting mid-send would leave the leader's waiter
                    # parked and the key marked in-flight forever, turning every
                    # later request into a follower that sends no new
                    # ``MSG_HISTORY_REQUEST`` and merely times out.
                    sent = await request_history(
                        manager,
                        state,
                        flow_id,
                        machine_id=owner_machine,
                        connection=owner_connection,
                    )
                    if not sent:
                        raise HTTPException(
                            status_code=404,
                            detail=(
                                "no connected daemon owns history for flow "
                                f"'{flow_id}'"
                            ),
                        )
                    pull_dispatched = True
                await asyncio.wait_for(fut, timeout=HISTORY_PULL_TIMEOUT)
                break
            except asyncio.TimeoutError:
                raise HTTPException(
                    status_code=504,
                    detail=f"timed out pulling history for flow '{flow_id}'",
                )
            except _PullAbandoned:
                # Our leader failed before dispatching a daemon request and
                # released us (the in-flight marker is already cleared). Loop
                # back to try to become the new leader ourselves rather than
                # parking behind an abandoned pull until the timeout.
                continue
            finally:
                if is_leader and not pull_dispatched:
                    # The leader failed or was cancelled BEFORE a successful
                    # daemon dispatch (its send returned ``False`` or it was
                    # cancelled before / while sending). Release every follower
                    # parked behind it and clear the in-flight marker so the
                    # next request leads a fresh pull immediately — otherwise
                    # ``discard`` would leave the marker set (followers remain)
                    # and strand them until ``HISTORY_PULL_TIMEOUT`` waiting on
                    # a ``MSG_HISTORY_REQUEST`` that was never sent.
                    history_registry.fail_pull(
                        flow_id, owner_machine, exclude=fut
                    )
                else:
                    # Drop our waiter on every other exit path — timeout,
                    # cancellation after a successful dispatch, a follower
                    # leaving, or success. On success ``resolve`` has already
                    # popped this waiter and cleared the in-flight marker, so
                    # the call is a no-op; otherwise it removes the now-dead
                    # waiter and, when it was the last one for the key, clears
                    # the marker. Because the leader keeps the pull genuinely in
                    # flight here, followers correctly stay parked on the
                    # already-dispatched request.
                    history_registry.discard(flow_id, fut, owner_machine)
        # Re-read the just-populated cache as a full snapshot so the response
        # carries ``delivery: "full"`` plus a fresh ``progress`` token the
        # client can use for its next reconnect. ``get_history_snapshot`` with
        # ``expected_machine_id`` / ``expected_owner`` is the authoritative
        # validation: it only returns records when the bundle still belongs to
        # the owning machine and owner, so a same-machine, same-owner daemon
        # reconnect during the pull window (which resolves the waiter with
        # authoritative records from the new connection, but swaps the socket
        # object) is correctly served the full snapshot rather than discarded.
        # The 409 is reserved for the case the bundle's machine/owner actually
        # changed (validation fails), so we never return records from a
        # different machine that reused the same flow id.
        full = await state.get_history_snapshot(
            flow_id,
            after=None,
            expected_machine_id=owner_machine,
            expected_owner=target_owner,
        )
        if full is not None:
            return {"flow_id": flow_id, "cached": False, **full}
        raise HTTPException(
            status_code=409,
            detail=f"history ownership changed while pulling flow '{flow_id}'",
        )

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


def _create_app_kwargs_from_server_config(server_cfg: Any) -> dict:
    """Translate a :class:`se3.config.ServerConfig` into ``create_app`` kwargs.

    The structured ``server.auth`` dataclasses are mapped onto the surfaces
    ``create_app`` consumes:

    * ``auth_config`` — the dict-shaped ``{"providers": [...]}`` that
      :func:`build_provider_chain` reads. Each configured provider name is
      expanded into a full entry mapping carrying that provider's options
      (OIDC issuer/client, proxy-header name) so an operator can enable/switch
      providers purely through configuration.
    * ``session_store`` — a :class:`SessionStore` whose cookie attributes
      (name / Secure / HttpOnly / SameSite / max-age) come from
      ``server.auth.session``.
    * ``rate_limiter`` — a :class:`LoginRateLimiter` whose lockout / window
      thresholds come from ``server.auth.local``.

    This is what makes ``server.auth.*`` and ``server.db_path`` from
    ``se3.yaml`` / the global config actually take effect on the running
    server instead of being silently ignored.
    """
    auth = server_cfg.auth

    provider_entries: list = []
    for name in auth.providers:
        if name == PROVIDER_PROXY_HEADER:
            provider_entries.append(
                {
                    "type": PROVIDER_PROXY_HEADER,
                    "enabled": auth.proxy_header.enabled,
                    "trust_proxy": auth.proxy_header.trust_proxy,
                    "header": auth.proxy_header.header,
                }
            )
        elif name == PROVIDER_OIDC:
            provider_entries.append(
                {
                    "type": PROVIDER_OIDC,
                    "enabled": auth.oidc.enabled,
                    "issuer": auth.oidc.issuer,
                    "client_id": auth.oidc.client_id,
                    "client_secret": auth.oidc.client_secret,
                    "redirect_url": auth.oidc.redirect_url,
                    "scopes": list(auth.oidc.scopes),
                }
            )
        else:
            provider_entries.append(name)

    cookie = CookieConfig(
        name=auth.session.cookie_name,
        http_only=auth.session.cookie_httponly,
        same_site=auth.session.cookie_samesite,
        secure=auth.session.cookie_secure,
        max_age=auth.session.max_age_seconds,
    )
    session_store = SessionStore(
        ttl_seconds=auth.session.max_age_seconds, cookie_config=cookie
    )
    rate_limiter = LoginRateLimiter(
        RateLimitConfig(
            max_failures=auth.local.max_failed_attempts,
            lockout_seconds=float(auth.local.lockout_seconds),
            window_seconds=float(auth.local.ratelimit_window_seconds),
        )
    )
    return {
        "db_path": str(server_cfg.db_path),
        "auth_config": {"providers": provider_entries},
        "session_store": session_store,
        "rate_limiter": rate_limiter,
    }


def run(
    host: str = "127.0.0.1",
    port: int = protocol.DEFAULT_SERVER_PORT,
    *,
    db_path: Optional[str] = None,
    auth_config: Optional[dict] = None,
    session_store: Optional[SessionStore] = None,
    rate_limiter: Optional[LoginRateLimiter] = None,
    log_level: str = "info",
) -> None:
    """Start the SE3 central server with uvicorn (blocking).

    *db_path* selects the sqlite store backing owners / identities / daemon
    keys / break-glass tokens. The CLI passes the persistent default so a token
    minted via ``se3-server bootstrap-token`` is consumable by the live server;
    ``None`` falls back to an in-memory store (used by tests). *auth_config* /
    *session_store* / *rate_limiter* carry the resolved ``server.auth.*``
    configuration through to :func:`create_app`.
    """
    import uvicorn

    app = create_app(
        db_path=db_path,
        auth_config=auth_config,
        session_store=session_store,
        rate_limiter=rate_limiter,
    )
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=log_level,
        ws_max_size=protocol.MAX_WS_MESSAGE_BYTES,
    )


def main(argv: Optional[list] = None) -> None:
    """``se3-server`` console-script entry point.

    Parses ``--host`` / ``--port`` / ``--db-path``, loads the ``server:``
    configuration (``se3.yaml`` + global ``~/.se3/config.yaml``), and runs the
    server with the resolved auth providers / cookie / lockout / db-path
    settings. Kept dependency-light (argparse + the core config loader) so the
    friendly missing-extra check in :func:`se3.server.main` stays the first
    thing a user without the extra sees.
    """
    import argparse

    from se3.config import load_server_config

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
        "--db-path",
        default=None,
        help=(
            "Path to the sqlite store (overrides server.db_path config; "
            f"default: {DEFAULT_DB_PATH})"
        ),
    )
    parser.add_argument(
        "--log-level", default="info", help="uvicorn log level (default: info)"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)

    server_cfg = load_server_config()
    kwargs = _create_app_kwargs_from_server_config(server_cfg)
    # An explicit --db-path wins over the configured server.db_path.
    if args.db_path:
        kwargs["db_path"] = args.db_path
    run(args.host, args.port, log_level=args.log_level, **kwargs)
