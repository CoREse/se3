"""The SE3 central-server FastAPI application.

This module assembles the FastAPI app that aggregates state from many SE3
daemons and exposes it to the web frontend:

* ``WS  /ws`` — the daemon connection endpoint (see :mod:`tianluo.server.ws`);
* ``GET /api/health`` — liveness probe;
* ``GET /api/machines`` — all connected machines;
* ``GET /api/machines/{id}/flows`` — flows on one machine;
* ``GET|POST|DELETE /api/machines/{id}/projects`` — that machine's persistent
  project registry (list from the mirror; add / remove via a daemon command);
* ``GET /api/flows/{id}`` — one flow's detail;
* ``POST /api/flows`` — publish a new task (routed to a daemon as SPAWN_FLOW);
* ``POST /api/flows/{id}/respond`` — answer a flow's pending interjection/call;
* ``POST /api/flows/{id}/interject`` — inject a mid-flow instruction into a flow;
* ``POST /api/uploads`` — relay a pasted/dropped attachment to the owning daemon,
  which stores it under the project's uploads directory;
* ``GET /api/uploads/file`` — read one stored attachment back out of that
  directory (the inline-thumbnail path), via the same daemon socket;
* ``GET /api/history`` — the aggregated history-session index;
* ``GET /api/history/{id}`` — one flow's history records (pulled on demand),
  shaped for the collapsed render: tool-call detail bodies and a step event's
  StepState ``inputs`` snapshot are held back;
* ``GET /api/history/{id}/detail`` — one held-back body, fetched when the user
  expands its chip: a tool call's detail (``source=raw`` / ``progress``) or a
  step event's original record (``source=step``);
* ``/`` and ``/static`` — the bundled web frontend (static files).

The heavy web dependencies (``fastapi``, ``uvicorn``) are isolated in the
``tianluo[server]`` optional-dependency extra. Nothing in the core ``luo`` CLI
imports this module, so a core-only install never loads FastAPI. The
``se3-server`` console script (see :func:`tianluo.server.main`) is the only entry
point and checks for the extra before importing this module.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import uuid
import weakref
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, MutableMapping, Optional, Tuple

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from tianluo import __version__
from tianluo.daemon import protocol
from tianluo.daemon.history import _clip as _clip_desc
from tianluo.daemon.wire_metrics import WireMetrics

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
from .history_summary import (
    DETAIL_SOURCE_PROGRESS,
    DETAIL_SOURCE_RAW,
    DETAIL_SOURCE_STEP,
    locate_record_detail,
    summarize_history_records,
)
from .identity import IdentityService
from .persistence import (
    MESSAGE_HISTORY_CHANNELS,
    MESSAGE_HISTORY_MAX_ENTRIES,
    IdentityAlreadyBound,
    Store,
)
from .state import ServerState, records_reach_bytes
from .ws import (
    HEARTBEAT_TIMEOUT,
    PING_INTERVAL,
    ConnectionManager,
    DetailRequestRegistry,
    HistoryRequestRegistry,
    HistoryWindowRegistry,
    IndexRefreshRegistry,
    InterjectionEventTracker,
    IssueCommandRegistry,
    PresenceDebouncer,
    ProjectCommandRegistry,
    UiHub,
    UploadRequestRegistry,
    _PullAbandoned,
    dump_json_chunked,
    broadcast_index_refresh,
    handle_daemon_connection,
    handle_ui_connection,
    request_detail,
    request_history,
    request_history_window,
)

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

#: Directory holding the frontend's per-language dictionaries (``<code>.json``).
#: It is the WebUI language registry's single source of truth: the manifest
#: endpoint below is derived from its contents, so adding a UI language is a pure
#: data change (drop a new locale JSON) with no frontend code edit.
UI_LOCALES_DIR = STATIC_DIR / "i18n"


def _discover_ui_languages() -> list:
    """List the UI languages the bundled frontend can serve.

    Each entry carries the language code and its endonym (the language's own name
    for itself, read from that dictionary's own ``lang.<code>`` key) so the
    switcher can label an option even before its dictionary is fetched. An
    unreadable / malformed locale file is skipped rather than failing the request
    — a broken translation drop must never take the console down.
    """
    languages = []
    if not UI_LOCALES_DIR.is_dir():
        return languages
    for path in sorted(UI_LOCALES_DIR.glob("*.json")):
        code = path.stem
        label = code
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                own = data.get(f"lang.{code}")
                if isinstance(own, str) and own:
                    label = own
        except (OSError, ValueError):
            logger.warning("Skipping unreadable UI locale file: %s", path)
            continue
        languages.append({"code": code, "label": label})
    return languages

#: Seconds a ``GET /api/history/{flow_id}`` cache-miss waits for the owning
#: daemon to answer the on-demand ``MSG_HISTORY_REQUEST`` before giving up.
#: Sized to leave headroom for a cold first pull of a large session's jsonl
#: history (the daemon offloads the read to a thread, but the disk read itself
#: still takes real time on a multi-MB session); a shorter window risks 504s
#: even when the daemon is healthy and still reading.
HISTORY_PULL_TIMEOUT = 30.0

#: Hard ceiling on how many step blocks one windowed history request may ask
#: for. The WebUI asks for 10; the cap exists so a hand-crafted
#: ``?window=100000`` cannot turn the windowed route back into the whole-flow
#: read it was introduced to replace.
HISTORY_WINDOW_MAX_BLOCKS = 200

#: How often the on-demand detail endpoint re-reads the bundle while following a
#: daemon pull's later frames. A pull larger than the daemon's per-frame byte
#: budget arrives as a ``full`` head plus ``append`` tails and the pull waiter is
#: resolved by the head alone, so a detail living in a later tail must not be
#: reported absent while it is still on the wire. The follow ends on the bundle's
#: own verdict (the record arrived, or the daemon provably read past its line —
#: see ``ServerState.get_history_record_detail``), never on an elapsed-silence
#: guess; this interval only decides how promptly that verdict is noticed, and is
#: short enough to be imperceptible while costing a handful of cheap metadata
#: reads rather than a spin.
HISTORY_PULL_TAIL_POLL = 0.25

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

#: Seconds a project-registry write endpoint (add / remove a project root) waits
#: for the daemon to acknowledge the ``MSG_PROJECT_COMMAND``. The daemon-side
#: work is one ``stat`` plus a small-file rewrite, so a short window suffices.
#: Unlike the issue leg there is deliberately NO reconcile fallback on timeout:
#: the registry mirror only refreshes on the next STATUS_UPDATE, and reporting
#: a guessed success for a registry write the daemon may never have applied is
#: worse than a visible 504 the operator can simply retry.
PROJECT_COMMAND_TIMEOUT = 10.0

#: Maps the daemon's stable ``error_code`` (see
#: :func:`~tianluo.daemon.protocol.make_project_result`) onto an HTTP status. The
#: code — not the status — is what the frontend localizes, so this table only
#: has to make the response *semantically* honest to a non-browser client.
PROJECT_ERROR_STATUS: Dict[str, int] = {
    "not_found": 404,
    "not_registered": 404,
    "not_a_directory": 422,
    "invalid_path": 422,
    "live_flow": 409,
    # The request was well-formed and the entry does exist — the daemon's own
    # registry file could not be rewritten. 500 (not 4xx) so a non-browser
    # client reads it as "retry", matching the operator-facing copy.
    "registry_error": 500,
}

#: Status for a daemon failure whose ``error_code`` this server revision does
#: not know (a newer daemon, or a bare ``ok=false`` with no code). 400 says
#: "the request did not succeed" without claiming a specific cause.
PROJECT_ERROR_STATUS_DEFAULT = 400

#: Seconds ``POST /api/uploads`` waits for the daemon to acknowledge the
#: ``MSG_UPLOAD_COMMAND``. The daemon-side work is one hash plus a write of at
#: most :data:`~tianluo.daemon.protocol.MAX_UPLOAD_BYTES`, offloaded to a thread —
#: but this sits in the operator's typing path behind an unresolved placeholder
#: token, so the window is kept to the same short 10s as the other command legs:
#: a visible failure the operator can re-paste beats an input box that hangs.
UPLOAD_COMMAND_TIMEOUT = 10.0

#: Error codes this server mints itself (the daemon never sends them), for
#: failures that happen before the frame is dispatched. They ride in the same
#: top-level ``error_code`` slot as the daemon's own codes so the web UI has a
#: single localization path for every upload failure.
UPLOAD_ERR_UNSUPPORTED_DAEMON = "unsupported_daemon"
UPLOAD_ERR_NOT_CONNECTED = "not_connected"
UPLOAD_ERR_TIMEOUT = "timeout"
UPLOAD_ERR_NO_TARGET = "no_target"

#: Maps the daemon's stable upload ``error_code`` (see
#: :data:`~tianluo.daemon.protocol.UPLOAD_ERROR_CODES`) onto an HTTP status.
#: Daemon-sent codes only — the server-minted codes above carry their status at
#: the raise site. ``not_registered`` is 409 rather than 404: the project root
#: came from this server's own mirror, so the daemon refusing it means the two
#: sides disagree about what is registered *right now*, which the operator fixes
#: by re-adding the project — not a "no such thing" the browser should hide.
UPLOAD_ERROR_STATUS: Dict[str, int] = {
    protocol.UPLOAD_ERR_TOO_LARGE: 413,
    protocol.UPLOAD_ERR_NOT_REGISTERED: 409,
    protocol.UPLOAD_ERR_INVALID_PATH: 422,
    protocol.UPLOAD_ERR_INVALID_FILENAME: 422,
    protocol.UPLOAD_ERR_INVALID_PAYLOAD: 422,
    protocol.UPLOAD_ERR_UNSUPPORTED: 501,
    # The request was well-formed and accepted — the daemon's disk refused it.
    # 500 (not 4xx) so a non-browser client reads it as "retry".
    protocol.UPLOAD_ERR_WRITE_FAILED: 500,
}

#: Status for an upload failure whose ``error_code`` this server revision does
#: not know (a newer daemon, or a bare ``ok=false``). 502, not 400: the request
#: reached a daemon that answered with something this server cannot interpret,
#: which is an upstream fault rather than the browser's.
UPLOAD_ERROR_STATUS_DEFAULT = 502

#: Seconds ``GET /api/uploads/file`` waits for the daemon's ``MSG_FETCH_RESULT``.
#: Held equal to :data:`UPLOAD_COMMAND_TIMEOUT` on purpose: the daemon-side work
#: is the mirror image (one bounded read instead of one bounded write, likewise
#: offloaded to a thread), and the browser's fallback for a slow answer is the
#: same as for a failed one — the thumbnail simply never appears and the message
#: stays plain path text.
FETCH_COMMAND_TIMEOUT = 10.0

#: Maps the daemon's stable fetch ``error_code`` (see
#: :data:`~tianluo.daemon.protocol.FETCH_ERROR_CODES`) onto an HTTP status. The
#: server-minted pre-dispatch codes are the ``UPLOAD_ERR_*`` values above rather
#: than a parallel set: their strings are the frontend's localization keys, and
#: "not connected" / "timed out" mean exactly the same thing on both legs, so a
#: second keyspace would only force the UI to translate each phrase twice.
#: ``not_registered`` is 409 for the upload leg's reason (server and daemon
#: disagree about what is registered right now); ``not_found`` is a plain 404 —
#: the referenced attachment is genuinely gone from the daemon's disk.
FETCH_ERROR_STATUS: Dict[str, int] = {
    protocol.FETCH_ERR_INVALID_PATH: 422,
    protocol.FETCH_ERR_NOT_REGISTERED: 409,
    protocol.FETCH_ERR_NOT_FOUND: 404,
    protocol.FETCH_ERR_TOO_LARGE: 413,
    protocol.FETCH_ERR_UNSUPPORTED: 501,
    # Well-formed request, existing file, daemon's disk refused the read. 500
    # (not 4xx) so a non-browser client reads it as "retry".
    protocol.FETCH_ERR_READ_FAILED: 500,
}

#: Status for a fetch failure whose ``error_code`` this server revision does not
#: know. 502 for :data:`UPLOAD_ERROR_STATUS_DEFAULT`'s reason.
FETCH_ERROR_STATUS_DEFAULT = 502

#: Extension → ``Content-Type`` whitelist for the read-back endpoint. WHY a
#: whitelist rather than :func:`mimetypes.guess_type`: the bytes are
#: operator-supplied and served same-origin, so guessing would happily label an
#: uploaded ``.html`` / ``.svg`` as a renderable document and turn the
#: attachment store into a stored-XSS surface. Only the raster image types the
#: inline-thumbnail feature actually needs are named; everything else falls
#: through to :data:`FETCH_CONTENT_TYPE_DEFAULT`, which no browser renders.
#: SVG is excluded deliberately — it is a script-bearing document, not a raster
#: image, and an ``<img>`` thumbnail of it is not worth the execution surface.
FETCH_CONTENT_TYPES: Dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".avif": "image/avif",
    ".ico": "image/x-icon",
}

#: Content type for a fetched file whose extension is not in the whitelist.
FETCH_CONTENT_TYPE_DEFAULT = "application/octet-stream"

#: ``Cache-Control`` for a successful read-back. WHY it is safe to cache this
#: aggressively: the stored filename carries a content-hash prefix (see
#: :func:`tianluo.daemon.uploads.store_upload`), so one project-relative uploads
#: path can only ever denote one byte string — a stale cache entry is
#: unreachable by construction. WHY it is *necessary*: the URL is rendered by
#: every inline thumbnail in a conversation and re-rendered on every repaint, so
#: without it a scroll through history would punch dozens of round trips through
#: to the daemon. INVARIANT: the directive is ``private``, never ``public`` —
#: content-hash naming makes the bytes *stable*, not *public*, and the route is
#: owner-scoped behind ``Depends(require_owner)``. ``public`` would license the
#: documented TLS reverse proxy (nginx ``proxy_cache`` on /api/) to store one
#: tenant's attachment and replay it to an unauthenticated request for the same
#: URL, bypassing the owner gate entirely. ``private`` keeps the whole benefit
#: — the cache that matters is the requesting browser's — inside the trust
#: boundary that the owner check established.
FETCH_CACHE_CONTROL = "private, max-age=31536000, immutable"

#: ``Vary`` for a successful read-back. Defence in depth behind ``private``:
#: owner identity is resolved from the session cookie, so a cache entry is only
#: ever valid for the cookie that produced it. Belt to the ``private`` braces in
#: case an intermediary honours ``Vary`` but not ``private``.
FETCH_VARY = "Cookie"

#: Seconds an issue/call detail endpoint waits for the owning daemon to answer
#: the on-demand ``MSG_DETAIL_REQUEST`` with the full text. A single issue YAML
#: / call file read the daemon offloads to a thread, so a short window suffices;
#: a slow / disconnected daemon degrades to a 504 / 503 rather than hanging.
DETAIL_PULL_TIMEOUT = 10.0

#: Minimum uncompressed response size (bytes) at which the GZip middleware
#: kicks in. The multi-MB ``GET /api/history/{flow_id}`` full bundle is the
#: target — gzip is 5–10x on that JSON — while tiny not-modified / delta / index
#: replies stay uncompressed so their per-response CPU cost is not paid for no
#: win.
GZIP_MIN_SIZE = 1024

#: Record count at or above which a history response takes the loop-friendly
#: render path (batched JSON on the loop + gzip in a worker thread) instead of a
#: single inline ``json.dumps``. Reproduce with
#: ``scripts/measure_server_loop_stalls.py``; on history payloads of this shape
#: (CPython, this host) a 3 MiB bundle costs ~17 ms to serialize plus ~23 ms to
#: gzip, and a 16 MiB one ~68 ms plus ~134 ms — over two tenths of a second of
#: frozen loop per full bundle, during which no daemon heartbeat, no other
#: browser poll and no history frame is processed. That is the LARGEST confirmed
#: part of the observed server stutter; the inbound frame parse, by contrast, is
#: bounded at the daemon side and measures sub-millisecond (see
#: ``ws.LARGE_FRAME_WARN_BYTES``). ~200 records is roughly where the render
#: leaves the sub-millisecond range.
#:
#: This is only ONE of the two gates — see
#: :data:`HISTORY_RESPONSE_OFFLOAD_BYTES` for why a count cannot be the whole
#: decision.
HISTORY_RESPONSE_OFFLOAD_RECORDS = 200

#: Estimated payload size at or above which a history response takes the
#: loop-friendly path however FEW records it holds.
#:
#: WHY a second gate: what costs event-loop time is bytes, not records, and the
#: two are not proportional. Real history records are heavy-tailed (sampled over
#: this repo's own ``tianluo/history/``: mean 40.7 KB, p90 12.3 KB, p99 1.1 MB),
#: so a 150-record / 10 MiB bundle sits under the count gate and used to be
#: classified small — rendered inline AND, because the small path deliberately
#: declares no ``Content-Encoding``, gzipped by ``GZipMiddleware`` on the loop
#: too: ~87 ms of frozen loop per request, re-paid on the running-flow view's
#: 3 s poll, versus ~34-47 ms for the same payload on the big path.
#:
#: 1 MiB is where the count gate already sat under its own sizing assumption
#: (200 records x ~4 KB), so the two gates describe one line rather than two.
#: The estimate is a resident-cost approximation and runs to at most
#: :data:`HISTORY_RESPONSE_OFFLOAD_RECORDS` records (the count gate
#: short-circuits first), so the check itself is well under a millisecond.
HISTORY_RESPONSE_OFFLOAD_BYTES = 1024 * 1024

#: How many big history responses may be rendered concurrently.
#: WHY a gate at all: the render is a latency fix, but each in-flight render
#: materialises a SECOND full copy of the bundle (the serialized bytes) plus
#: gzip's own buffer, on top of the records the payload keeps alive — and it does
#: so with ``ServerState._lock`` released, so the budget sweep cannot see or
#: bound it. Batching the JSON makes that WORSE, not better, without a gate: the
#: render now yields to the loop, so more of them genuinely overlap than when
#: each one ran to completion in one uninterrupted pass. Without a gate a handful
#: of browser tabs re-opening a 16 MiB conversation at once could hold well over
#: a gigabyte of transient buffers on the same memory-capped container this
#: whole fix exists to keep alive — trading an OOM by cache growth for an OOM by
#: response burst. Four is comfortably above the real concurrency of a console
#: (a page open per operator) while capping the transient at a few multiples of
#: one bundle.
HISTORY_RENDER_CONCURRENCY = 4

#: Seconds between two UNPROTECTED history-cache budget sweeps. Deliberately
#: independent of ``server.history_cache.report_interval_seconds``: that setting
#: governs a diagnostic log line, and turning a log line off must never turn
#: memory enforcement off (the write-path sweep cannot evict the flow it is
#: writing, so this is the only thing that bounds a single hot-pushed,
#: unwatched flow). One minute is far below the time it takes a daemon push
#: stream to add hundreds of MiB, and the sweep is a no-op when the cache is
#: under budget.
HISTORY_CACHE_SWEEP_INTERVAL = 60

#: Per-event-loop render gates. Keyed weakly by the running loop rather than
#: created once at import: a module-level ``asyncio.Semaphore`` would bind to
#: whichever loop happened to import this module, which is wrong for a test
#: suite that runs many apps on many loops (and, on 3.9, wrong at import time
#: full stop).
_RENDER_GATES: "MutableMapping[Any, asyncio.Semaphore]" = weakref.WeakKeyDictionary()


def _history_render_gate() -> asyncio.Semaphore:
    """The current loop's history-render concurrency gate."""
    loop = asyncio.get_running_loop()
    gate = _RENDER_GATES.get(loop)
    if gate is None:
        gate = asyncio.Semaphore(HISTORY_RENDER_CONCURRENCY)
        _RENDER_GATES[loop] = gate
    return gate


#: JSON encoder settings shared by every history response, so the inline and the
#: batched render paths cannot drift into producing different bytes.
_HISTORY_JSON_KWARGS = {
    "ensure_ascii": False,
    "allow_nan": False,
    "separators": (",", ":"),
    "default": str,
}


def _render_history_body(payload: dict, gzip_ok: bool) -> Tuple[bytes, bool]:
    """Serialize (and optionally gzip) a history payload in one pass.

    Returns ``(body, is_gzipped)``. Used directly for the small replies that are
    the steady state; the big-payload path calls :func:`_gzip_history_body` on a
    body that :func:`dump_json_chunked` already produced.

    Compressing HERE rather than leaving it to :class:`GZipMiddleware` is the
    point of the second return value: the middleware compresses on the event
    loop, and gzip is the single most expensive step on this path. Starlette
    passes a response through untouched when it already declares a
    ``Content-Encoding``, which is what the caller sets.
    """
    body = json.dumps(payload, **_HISTORY_JSON_KWARGS).encode("utf-8")
    if gzip_ok and len(body) >= GZIP_MIN_SIZE:
        return _gzip_history_body(body), True
    return body, False


def _gzip_history_body(body: bytes) -> bytes:
    """Compress an already-serialized history body. Genuinely runs off the loop.

    WHY this half — and only this half — is worth a thread hop: ``zlib`` releases
    the GIL around the compression, so the loop really is free while it runs.
    Measured on this host on a 16 MiB body (idle loop, worst lateness of a 5 ms
    timer): ~144 ms inline vs ~1 ms in a worker thread. The JSON render is the
    opposite case — the C encoder keeps the GIL, so it is batched on the loop
    instead of offloaded (see :func:`dump_json_chunked`).
    """
    import gzip as _gzip

    return _gzip.compress(body, 9)


async def _history_response(payload: dict, request: Request) -> Response:
    """Return *payload* as a JSON response without freezing the event loop.

    Small replies (the not-modified / delta / index steady state, which is the
    overwhelming majority) render inline — the extra machinery would cost more
    than the render. "Small" is decided on record count AND estimated bytes,
    because neither alone identifies a payload that stalls the loop: a few
    multi-MB records cost as much as thousands of ordinary ones. A payload big
    enough to stall the loop is split in two:
    the JSON render is batched on the loop (it cannot be escaped by a thread —
    the C encoder holds the GIL), and only the gzip pass, which does release it,
    goes to a worker thread.

    A client sending ``Accept-Encoding: identity`` therefore still gets the
    batched render and so still leaves the loop responsive; it merely skips the
    compression step it asked not to have.
    """
    gzip_ok = "gzip" in request.headers.get("accept-encoding", "").lower()
    records = payload.get("records")
    big = isinstance(records, list) and (
        len(records) >= HISTORY_RESPONSE_OFFLOAD_RECORDS
        or records_reach_bytes(records, HISTORY_RESPONSE_OFFLOAD_BYTES)
    )
    # Summary shaping happens in THIS function — the single funnel every history
    # snapshot leaves through — so no delivery (full / delta / backfill / a
    # reconciled re-read) can escape it. The cache keeps the full bundle; only
    # the wire copy loses the detail bodies.
    #
    # WHY it now runs INSIDE the gate on the big path, and why the size decision
    # is taken BEFORE it: shaping is itself an O(records) pass over the whole
    # bundle, so it belongs under the same concurrency discipline as the render
    # and the gzip rather than running unbounded ahead of them — N concurrent
    # big-bundle opens otherwise hold N shaped copies before the gate has
    # admitted even one. Deciding ``big`` on the UNSHAPED records only ever
    # over-estimates (shaping strictly removes bytes and never adds a record),
    # so a payload that ships small can take the batched path but one that ships
    # big can never take the inline one — the direction that keeps the loop safe.
    if big:
        # The gate bounds how many full-bundle copies exist off-lock at once
        # (see HISTORY_RENDER_CONCURRENCY); waiting here costs latency, never
        # correctness — the payload was detached from the cache under
        # ``ServerState._lock``, so a bundle evicted while this request queues or
        # renders cannot change what it will serve.
        async with _history_render_gate():
            payload["records"] = summarize_history_records(
                records, payload.get("flow_id") or ""
            )
            body = await dump_json_chunked(payload, **_HISTORY_JSON_KWARGS)
            gzipped = gzip_ok and len(body) >= GZIP_MIN_SIZE
            if gzipped:
                body = await asyncio.to_thread(_gzip_history_body, body)
    else:
        payload["records"] = summarize_history_records(
            records, payload.get("flow_id") or ""
        )
        body, gzipped = _render_history_body(payload, False)
    headers = {}
    if gzipped:
        headers["Content-Encoding"] = "gzip"
        headers["Vary"] = "Accept-Encoding"
    return Response(
        content=body, media_type="application/json", headers=headers
    )


def _window_payload_from_daemon(
    flow_id: str,
    machine_id: str,
    reply: Dict[str, Any],
    *,
    count: int,
    before_step: str,
    usage: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Shape a daemon window reply into the REST snapshot contract.

    The reply is served straight through and never cached, so this payload
    carries NO ``progress`` / ``signature`` — there is no bundle for a token to
    pin, and minting one would have the client's next poll present a cursor the
    server cannot bind, which the snapshot path answers with a whole-flow
    rebuild: precisely the pull this leg exists to avoid.

    What the client polls with instead is ``window.signature``: the daemon's own
    constant-cost fingerprint of the read, echoed back as ``wsig`` and answered
    ``not_modified`` while the flow is unchanged (see
    :func:`_window_not_modified_payload`). WHY the leg needs one at all: a
    windowed view holds no token, so its 3 s self-heal poll would otherwise
    re-request the tail every tick — the daemon re-reading tens of MB of jsonl
    and the server re-shaping and re-gzipping the whole window — for as long as
    the flow is merely watched. The signature is bound STATELESSLY: the server
    stores nothing, it only relays the probe to the daemon that minted it.

    ``incomplete`` is ``False`` rather than absent: the read is a direct,
    complete answer for the blocks it names, so declaring it settled is the
    truth — and a windowed view must never inherit the interrupted-delivery
    repair loop of a bundle that does not exist.

    *usage* is the flow's usage/cost payload, supplied by the caller from the
    reported session index (:meth:`ServerState.get_history_flow_usage_summary`).
    WHY it cannot be derived here: the cached legs answer with
    ``_bundle_usage``, but this leg has no bundle by design, and aggregating the
    window's own records would report the cost of ten blocks as the session's
    total — worse than reporting none. The index summary is the whole flow's,
    computed by the daemon from engine state, so the usage surface renders the
    same numbers the whole-flow pull used to deliver.

    An EMPTY ``window`` (the daemon resolved the anchor to no block) reports
    ``first_index = len(steps)``, i.e. "nothing loaded, and NOT at the head":
    ``has_earlier`` stays true so the client keeps its page-up and keeps its
    completeness self-check scoped to the blocks it actually holds. The cached
    leg answers the same condition the same way (``_window_block_range``).
    """
    steps = [str(x) for x in (reply.get("steps") or [])]
    loaded = [str(x) for x in (reply.get("window") or [])]
    if loaded:
        first_index = steps.index(loaded[0]) if loaded[0] in steps else 0
        last_index = steps.index(loaded[-1]) if loaded[-1] in steps else first_index
    else:
        first_index, last_index = len(steps), len(steps) - 1
    return {
        "machine_id": machine_id,
        "mode": protocol.HISTORY_MODE_FULL,
        "delivery": "window",
        "records": list(reply.get("records") or []),
        "progress": None,
        "signature": None,
        "cursor": dict(reply.get("counts") or {}),
        "generation": None,
        "unfillable": {},
        "pending": {},
        "incomplete": False,
        "resync": False,
        "usage": usage if isinstance(usage, dict) and usage else None,
        "window": {
            "mode": "before" if before_step else "tail",
            "steps": steps,
            "loaded": loaded,
            "first_index": first_index,
            "last_index": last_index,
            "has_earlier": first_index > 0,
            "block_size": count,
            "source": "daemon",
            # Empty when the daemon predates the conditional read; the client
            # then simply polls unconditionally, exactly as before.
            "signature": str(reply.get("signature") or ""),
        },
    }


def _window_not_modified_payload(flow_id: str, machine_id: str) -> Dict[str, Any]:
    """The reply to a windowed poll whose ``wsig`` still describes the flow.

    Deliberately carries NO ``window`` block and NO ``cursor``: the client keeps
    the window description and the block index it already holds (a reply with no
    window block is inert to ``adoptWindowMeta``), and there is nothing new to
    self-check against. ``incomplete: False`` for the same reason the full
    relayed window says so — the read is a complete answer, and a windowed view
    must never inherit the repair loop of a bundle that does not exist.

    The confirmed signature is not echoed back: it is by construction the one
    the client just sent, so the value it already holds IS the current one.
    """
    return {
        "flow_id": flow_id,
        "cached": False,
        "machine_id": machine_id,
        "mode": protocol.HISTORY_MODE_FULL,
        "delivery": "not_modified",
        "records": [],
        "progress": None,
        "signature": None,
        "generation": None,
        "unfillable": {},
        "pending": {},
        "incomplete": False,
        "resync": False,
    }


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

#: Ceiling on how many record numbers one ``missing=`` backfill request may name.
#: A client that has genuinely fallen far behind is better served by one full
#: rebuild than by an enormous numbered pick-list, and the cap keeps a malformed
#: or hostile query from turning into an unbounded index walk.
MISSING_MAX_ORDINALS = 200

#: The identity-binding discriminator + external id of the single break-glass
#: admin subject. Break-glass is deliberately a *single* admin subject (not a
#: per-user impersonation channel), so every consumed break-glass token resolves
#: to this same stable internal owner; distinguishing real owners is the local
#: auth provider's job. See the multi-tenant design's break-glass section.
BREAKGLASS_PROVIDER = "breakglass"
BREAKGLASS_EXTERNAL_ID = "admin"


def parse_missing_param(raw: Optional[str]) -> Optional[Dict[str, List[int]]]:
    """Parse the ``missing=`` backfill query parameter.

    Wire form: ``stepId:ord[,ord…];stepId:ord…`` — the record numbers a client's
    cursor self-check found it does not hold.

    WHY every malformed input degrades to ``None`` (= "no missing list") instead
    of raising: ``missing`` is an *optimisation over* the existing full fallback,
    never a precondition of it. A client on an older/newer encoding, a truncated
    URL, or an over-long list must still get a correct history reply — it simply
    gets the complete bundle instead of a numbered slice. Refusing the request
    would turn a self-heal poll into a user-visible error.
    """
    if not raw:
        return None
    result: Dict[str, List[int]] = {}
    total = 0
    for group in raw.split(";"):
        group = group.strip()
        if not group:
            continue
        key, sep, ordinals_raw = group.partition(":")
        key = key.strip()
        if not sep or not key:
            return None
        ordinals = result.setdefault(key, [])
        for token in ordinals_raw.split(","):
            token = token.strip()
            # ``isdigit`` rejects the empty string, a sign, and any non-decimal
            # token in one check — ordinals are 0-based line positions.
            if not token.isdigit():
                return None
            total += 1
            if total > MISSING_MAX_ORDINALS:
                return None
            value = int(token)
            if value not in ordinals:
                ordinals.append(value)
        if not ordinals:
            return None
    return result or None


# -- request models --------------------------------------------------------


class NewFlowRequest(BaseModel):
    """Body of ``POST /api/flows`` — publish a new task to a machine.

    When *from_issue_id* is supplied the flow is sourced from an existing
    issue: the issue's owner-scoped record is the authoritative source of the
    target machine / project, the request's *task* content is ignored (the
    issue description becomes the task), and the daemon drives the issue
    lifecycle via the ``luo run --from-issue`` CLI path.  *task* is optional in
    that case, so it defaults to an empty string.

    *plan_decomposition* / *plan_granularity* (optional, protocol revision 8)
    are the explicit PLAN decomposition doctrine and group-count pressure the
    published flow must use. Omitted by older clients — the fields stay legal
    and the daemon adds no CLI option, so the project configuration / default
    resolves them. When either is supplied, the owning daemon must advertise
    revision 8 or newer; otherwise the request is refused with an explicit
    capability error instead of silently running a different flow shape than
    requested.
    """

    machine_id: str = ""
    task: str = ""
    task_type: str = "feature"
    project_root: str = ""
    discover: bool = False
    worktree: bool = False
    from_issue_id: str = ""
    plan_decomposition: str = ""
    plan_granularity: str = ""


class RespondRequest(BaseModel):
    """Body of ``POST /api/flows/{id}/respond`` — answer a pending call."""

    response: Any
    call_id: str = ""


class InterjectRequest(BaseModel):
    """Body of ``POST /api/flows/{id}/interject`` — inject a mid-flow instruction."""

    text: str


class EndSessionRequest(BaseModel):
    """Body of ``POST /api/flows/{id}/end`` — terminate + archive the session."""

    reason: str = ""


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


class AppendHistoryRequest(BaseModel):
    """Body of ``POST /api/message-history/{channel}`` — record a sent prompt.

    Only text the console has actually delivered reaches here; the browser
    posts it *after* the send succeeded, so a failed send leaves no trace in
    the recall list.
    """

    text: str


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
    tags: list = []


class EditIssueRequest(BaseModel):
    """Body of ``PATCH /api/issues/{id}`` — edit an existing issue."""

    machine_id: str = ""
    project_root: str = ""
    title: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[str] = None
    type: Optional[str] = None
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


class AddProjectRequest(BaseModel):
    """Body of ``POST /api/machines/{id}/projects`` — register a project root.

    *project_root* is an absolute path **on the daemon's machine**, so the
    server can only reject the obviously-malformed shapes; existence and
    directory-ness are the daemon's to check.
    """

    project_root: str


class _UploadRelayError(Exception):
    """An attachment-relay failure raised out of a helper, carrying its status.

    WHY a bespoke exception rather than ``HTTPException``: every upload / fetch
    failure must answer with a *top-level* ``error_code`` (it is the browser's
    localization key), and ``HTTPException`` can only nest a structured body
    under ``detail``. Raising this lets the shared target-resolution and
    dispatch helpers keep their bookkeeping in one place while each endpoint
    still renders a flat body.
    """

    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


def _upload_error(status: int, code: str, detail: str) -> JSONResponse:
    """Render one upload failure with its stable code at the body's top level."""
    return JSONResponse(
        status_code=status, content={"detail": detail, "error_code": code}
    )


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
    history_cache_budget_bytes: Optional[int] = None,
    history_cache_report_interval: Optional[int] = None,
    history_cache_report_threshold_percent: Optional[int] = None,
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

    ``history_cache_*`` carry ``server.history_cache`` through to
    :class:`ServerState` (the in-RAM history relay's memory ceiling) and to the
    periodic occupancy report started below. ``None`` keeps the built-in
    defaults, so a bare ``create_app()`` is still bounded.
    """
    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        # Background history-cache maintenance: an UNPROTECTED budget sweep on a
        # fixed cadence, plus an occupancy report on the operator-configured one.
        #
        # WHY the sweep is a background task and not only a write-path trigger:
        # the write-path sweep always exempts the flow whose frame it is applying
        # (``_enforce_history_budget(protect=...)``), so it can never evict that
        # flow — and a single actively pushed flow nobody is watching IS the
        # oom-kill scenario this budget exists to stop. Only a sweep with no
        # protected flow can drop it. The occupancy report has the same
        # background-only property for a different reason: a server sitting on a
        # large resident cache with the daemons quiet emits nothing on the write
        # path, which is exactly the state an operator most needs attributed.
        #
        # WHY the two cadences are separate: ``report_interval_seconds: 0`` means
        # "stop logging the periodic line", and an operator setting it must not
        # thereby switch memory enforcement off. The sweep keeps its own fixed
        # cadence regardless.
        report_interval = (
            300
            if history_cache_report_interval is None
            else int(history_cache_report_interval)
        )
        sweep_interval = (
            HISTORY_CACHE_SWEEP_INTERVAL
            if report_interval <= 0
            else min(HISTORY_CACHE_SWEEP_INTERVAL, report_interval)
        )

        async def _report_loop() -> None:
            next_report = (
                time.monotonic() + report_interval
                if report_interval > 0
                else None
            )
            while True:
                await asyncio.sleep(sweep_interval)
                try:
                    if next_report is not None and time.monotonic() >= next_report:
                        next_report = time.monotonic() + report_interval
                        # Reports AND sweeps — one pass, one log line.
                        await state.report_history_cache()
                    else:
                        await state.sweep_history_cache()
                except asyncio.CancelledError:
                    raise
                except Exception:  # pragma: no cover - defensive
                    logger.warning(
                        "history-cache maintenance failed", exc_info=True
                    )

        report_task: Optional[asyncio.Task] = asyncio.create_task(_report_loop())
        app.state.history_cache_report_task = report_task
        yield
        # Teardown: cancel any in-flight presence offline-grace tasks so
        # server shutdown leaves no dangling asyncio task and no pending
        # mark_offline fires against a torn-down state.
        if report_task is not None:
            report_task.cancel()
        debouncer = getattr(app.state, "presence_debouncer", None)
        if debouncer is not None:
            debouncer.shutdown()

    app = FastAPI(
        title="tianluo Central Server",
        version=protocol.PROTOCOL_VERSION,
        lifespan=_lifespan,
    )
    # GZip the large JSON responses (chiefly a real ``delivery: "full"`` history
    # bundle re-build, which stays multi-MB even after differential/​not-modified
    # shrinks the steady state). Compression is the second, orthogonal止血 layer
    # to the delta protocol; the size floor keeps it off the many tiny
    # not-modified / delta / status replies. Added before the routes so it wraps
    # every response.
    app.add_middleware(GZipMiddleware, minimum_size=GZIP_MIN_SIZE)
    # One process-wide byte-accounting instance shared by the server→daemon
    # downlink (ConnectionManager) and the server→browser fan-out (UiHub), so a
    # single snapshot attributes traffic by message type across both legs.
    wire_metrics = WireMetrics()
    state = ServerState(
        history_cache_budget_bytes=history_cache_budget_bytes,
        history_cache_report_threshold_percent=(
            history_cache_report_threshold_percent
        ),
    )
    manager = ConnectionManager(metrics=wire_metrics)
    # Flow→machine resolution must ask the connection pool, not the machine
    # record's ``online`` flag: that flag is debounced by 60 s (below) for the
    # presence badge, so for a whole minute after a daemon dies its record still
    # claims to be online and would keep shadowing the machine that just took a
    # shared filesystem over. The pool is the only component that knows whether
    # a frame can actually be delivered right now.
    state.set_connectivity_probe(manager.is_connected)
    # Presence wiring (revision 4): the hub is the only component that knows
    # the exact browser connection count, the manager the only one that can
    # reach every daemon — so the 0↔non-0 edge is bridged here at assembly
    # rather than by either module importing the other.
    ui_hub = UiHub(
        metrics=wire_metrics, on_presence_edge=manager.broadcast_viewers
    )
    history_registry = HistoryRequestRegistry()
    index_refresh_registry = IndexRefreshRegistry()
    issue_command_registry = IssueCommandRegistry()
    project_command_registry = ProjectCommandRegistry()
    upload_command_registry = UploadRequestRegistry()
    # A second UploadRequestRegistry instance, not a reuse of the one above: the
    # upload and fetch legs mint their request ids independently, so sharing the
    # keyspace would let an upload ack resolve a fetch waiter on a collision.
    # The bookkeeping is identical, hence the shared class.
    fetch_command_registry = UploadRequestRegistry()
    detail_registry = DetailRequestRegistry()
    window_registry = HistoryWindowRegistry()
    interjection_tracker = InterjectionEventTracker()
    # Grace the daemon-offline transition by 60s so a lossy-link reconnect
    # (keepalive churn on node007-class networks) does not flap the WebUI
    # online badge; only a daemon gone past the window is shown offline.
    presence_debouncer = PresenceDebouncer(delay=60.0)

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
    app.state.issue_command_registry = issue_command_registry
    app.state.project_command_registry = project_command_registry
    app.state.upload_command_registry = upload_command_registry
    app.state.fetch_command_registry = fetch_command_registry
    app.state.detail_registry = detail_registry
    app.state.history_window_registry = window_registry
    app.state.wire_metrics = wire_metrics
    app.state.interjection_tracker = interjection_tracker
    app.state.presence_debouncer = presence_debouncer
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
            detail_registry=detail_registry,
            window_registry=window_registry,
            project_registry=project_command_registry,
            upload_registry=upload_command_registry,
            fetch_registry=fetch_command_registry,
            presence_debouncer=presence_debouncer,
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

    @app.get("/api/wire-metrics")
    async def wire_metrics_snapshot(
        identity_: OwnerIdentity = Depends(require_owner),
    ) -> dict:
        """Per-message-type sent-byte counters for both server links.

        The acceptance / regression surface for the traffic-reduction work:
        keys prefixed ``ui:`` are the server→browser (/ws/ui) fan-out, the rest
        are the server→daemon downlink, plus a synthetic ``__total__`` roll-up.
        Idle (no active flow, no browser) traffic should stay keepalive-sized;
        an active session should scale with new content, not with history / issue
        / bundle size. Process-in-memory only.
        """
        return {"metrics": wire_metrics.snapshot()}

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

    # -- project registry management ---------------------------------------

    async def _owned_machine_or_404(machine_id: str, identity_: OwnerIdentity) -> dict:
        """Resolve *machine_id* within the caller's trust domain or raise 404.

        A machine belonging to another owner is reported exactly like an unknown
        one, so these endpoints cannot be used to probe whether a given machine
        id exists on the server.
        """
        owned = await state.get_machine(machine_id, owner=_scope_for(identity_))
        if owned is None:
            raise HTTPException(
                status_code=404, detail=f"machine '{machine_id}' not found"
            )
        return owned

    def _validated_project_root(raw: str) -> str:
        """Reject the shapes the server can judge without seeing the filesystem."""
        project_root = (raw or "").strip()
        if not project_root:
            raise HTTPException(
                status_code=422, detail="'project_root' must not be empty"
            )
        if not os.path.isabs(project_root):
            raise HTTPException(
                status_code=422,
                detail=(
                    "'project_root' must be an absolute path, "
                    f"got {project_root!r}"
                ),
            )
        return project_root

    async def _send_project_command(
        machine_id: str, message: protocol.Message, request_id: str
    ) -> dict:
        """Dispatch a project-registry command and await the daemon's ack.

        Returns the daemon's :data:`~tianluo.daemon.protocol.MSG_PROJECT_RESULT`
        payload. Raises 503 when the frame could not be delivered and 504 when
        the ack does not arrive inside :data:`PROJECT_COMMAND_TIMEOUT` — in both
        cases the parked future is discarded so a late ack cannot accumulate
        waiters.
        """
        fut = project_command_registry.register(request_id)
        sent = await manager.send_to(machine_id, message)
        if not sent:
            project_command_registry.discard(request_id, fut)
            raise HTTPException(
                status_code=503,
                detail=f"failed to deliver PROJECT_COMMAND to '{machine_id}'",
            )
        try:
            return await asyncio.wait_for(fut, timeout=PROJECT_COMMAND_TIMEOUT)
        except asyncio.TimeoutError:
            project_command_registry.discard(request_id, fut)
            raise HTTPException(
                status_code=504,
                detail=(
                    "timed out waiting for project command result from "
                    f"'{machine_id}'"
                ),
            )

    def _project_failure(result: dict, fallback: str) -> JSONResponse:
        """Render a daemon ``ok=false`` as a status-mapped error response.

        The body keeps ``error_code`` at the top level (not nested under
        ``detail``) because it is the frontend's localization key — burying it
        in a prose field would force the UI back onto the daemon's untranslated
        English.
        """
        code = str(result.get("error_code") or "")
        status = PROJECT_ERROR_STATUS.get(code, PROJECT_ERROR_STATUS_DEFAULT)
        return JSONResponse(
            status_code=status,
            content={
                "detail": str(result.get("error") or fallback),
                "error_code": code,
            },
        )

    @app.get("/api/machines/{machine_id}/projects")
    async def machine_projects(
        machine_id: str, identity_: OwnerIdentity = Depends(require_owner)
    ) -> dict:
        """List one machine's persistently-registered project roots.

        Served straight from the STATUS_UPDATE mirror — no downlink frame, so
        the dialog opens instantly and works even while the daemon is offline
        (showing the last known registry). Freshness comes from the fast push
        the daemon fires after every registry write.
        """
        owned = await _owned_machine_or_404(machine_id, identity_)
        projects = owned.get("registered_projects") or []
        return {
            "machine_id": machine_id,
            "projects": projects,
            "count": len(projects),
        }

    @app.post("/api/machines/{machine_id}/projects")
    async def add_machine_project(
        machine_id: str,
        req: AddProjectRequest,
        identity_: OwnerIdentity = Depends(require_owner),
    ) -> JSONResponse:
        """Manually register a project root on one machine's daemon."""
        project_root = _validated_project_root(req.project_root)
        await _owned_machine_or_404(machine_id, identity_)
        if not manager.is_connected(machine_id):
            raise HTTPException(
                status_code=503, detail=f"machine '{machine_id}' is not connected"
            )
        request_id = uuid.uuid4().hex
        result = await _send_project_command(
            machine_id,
            protocol.make_project_command(
                protocol.PROJECT_OP_ADD, project_root, request_id=request_id
            ),
            request_id,
        )
        if not result.get("ok"):
            return _project_failure(result, "project registration failed on daemon")
        return JSONResponse(
            status_code=201,
            content={
                "status": "registered",
                "machine_id": machine_id,
                # The daemon echoes the NORMALIZED root it actually stored
                # (worktree-folded / realpath'd), which may differ from what the
                # operator typed; fall back to the request when it does not.
                "project_root": str(result.get("project_root") or project_root),
            },
        )

    @app.delete("/api/machines/{machine_id}/projects")
    async def remove_machine_project(
        machine_id: str,
        project_root: str = "",
        identity_: OwnerIdentity = Depends(require_owner),
    ) -> JSONResponse:
        """Deregister a project root from one machine's daemon.

        Registry-only: the project's on-disk data is never touched.
        """
        project_root = _validated_project_root(project_root)
        await _owned_machine_or_404(machine_id, identity_)
        if not manager.is_connected(machine_id):
            raise HTTPException(
                status_code=503, detail=f"machine '{machine_id}' is not connected"
            )
        request_id = uuid.uuid4().hex
        result = await _send_project_command(
            machine_id,
            protocol.make_project_command(
                protocol.PROJECT_OP_REMOVE, project_root, request_id=request_id
            ),
            request_id,
        )
        if not result.get("ok"):
            return _project_failure(result, "project removal failed on daemon")
        return JSONResponse(
            status_code=200,
            content={
                "status": "removed",
                "machine_id": machine_id,
                "project_root": str(result.get("project_root") or project_root),
            },
        )

    # -- attachment uploads -------------------------------------------------

    async def _resolve_attachment_target(
        identity_: OwnerIdentity,
        flow_id: str,
        machine_id: str,
        project_root: str,
    ) -> tuple:
        """Resolve an attachment request's target machine, root and machine record.

        Two ways to name the target, matching the two places the web UI deals in
        attachments: *flow_id* for a docked reply / a rendered conversation (the
        flow already knows its machine and project root), or an explicit
        *machine_id* + *project_root* pair for the New Task form, where no flow
        exists yet. Both go through the same ownership gate, so another owner's
        flow or machine reads exactly like an unknown one.

        Shared by the store (``POST /api/uploads``) and read-back
        (``GET /api/uploads/file``) legs on purpose: the ownership gate is the
        only thing standing between one owner's attachments and another's, and a
        second copy of it is a second place for that gate to drift. Raises
        ``HTTPException`` (404 / 422) for the shapes those endpoints already
        answer that way, and :class:`_UploadRelayError` where the answer must
        carry a top-level ``error_code``.
        """
        scope = _scope_for(identity_)
        flow_ref = (flow_id or "").strip()
        machine_ref = (machine_id or "").strip()
        root_ref = (project_root or "").strip()

        if flow_ref:
            resolved = await state.get_flow(flow_ref, owner=scope)
            if resolved is None:
                raise HTTPException(
                    status_code=404, detail=f"flow '{flow_ref}' not found"
                )
            target_machine, flow = resolved
            target_root = str(flow.get("project_root") or "").strip()
            if not target_root:
                raise _UploadRelayError(
                    422,
                    protocol.UPLOAD_ERR_INVALID_PATH,
                    f"flow '{flow_ref}' reports no project root to store under",
                )
            owned = await state.get_machine(target_machine, owner=scope)
            if owned is None:  # pragma: no cover - get_flow already owner-scoped
                raise HTTPException(
                    status_code=404, detail=f"machine '{target_machine}' not found"
                )
            return target_machine, target_root, owned
        if machine_ref and root_ref:
            target_root = _validated_project_root(root_ref)
            owned = await _owned_machine_or_404(machine_ref, identity_)
            return machine_ref, target_root, owned
        raise _UploadRelayError(
            422,
            UPLOAD_ERR_NO_TARGET,
            "supply either 'flow_id' or both 'machine_id' and 'project_root'",
        )

    async def _send_upload_command(
        machine_id: str, message: protocol.Message, request_id: str
    ) -> dict:
        """Dispatch an upload command and await the daemon's ack.

        Returns the daemon's :data:`~tianluo.daemon.protocol.MSG_UPLOAD_RESULT`
        payload. Raises :class:`_UploadRelayError` (503) when the frame could
        not be delivered and (504) when the ack does not arrive inside
        :data:`UPLOAD_COMMAND_TIMEOUT` — both paths discard the parked future so
        a silent daemon cannot leak one waiter per retry.
        """
        fut = upload_command_registry.register(request_id)
        sent = await manager.send_to(machine_id, message)
        if not sent:
            upload_command_registry.discard(request_id, fut)
            raise _UploadRelayError(
                503,
                UPLOAD_ERR_NOT_CONNECTED,
                f"failed to deliver UPLOAD_COMMAND to '{machine_id}'",
            )
        try:
            return await asyncio.wait_for(fut, timeout=UPLOAD_COMMAND_TIMEOUT)
        except asyncio.TimeoutError:
            upload_command_registry.discard(request_id, fut)
            raise _UploadRelayError(
                504,
                UPLOAD_ERR_TIMEOUT,
                f"timed out waiting for upload result from '{machine_id}'",
            )

    def _upload_failure(result: dict, fallback: str) -> JSONResponse:
        """Render a daemon ``ok=false`` upload result as a mapped error.

        Mirrors :func:`_project_failure`: the code stays at the top level
        because it is the frontend's localization key, and burying it in a prose
        field would force the UI back onto the daemon's untranslated English.
        """
        code = str(result.get("error_code") or "")
        status = UPLOAD_ERROR_STATUS.get(code, UPLOAD_ERROR_STATUS_DEFAULT)
        return _upload_error(status, code, str(result.get("error") or fallback))

    @app.post("/api/uploads")
    async def upload_attachment(
        request: Request,
        filename: str = "",
        flow_id: str = "",
        machine_id: str = "",
        project_root: str = "",
        identity_: OwnerIdentity = Depends(require_owner),
    ) -> JSONResponse:
        """Store one pasted / dropped attachment on the target project's machine.

        The body is the raw file bytes (``application/octet-stream``); the
        metadata rides in the query string. Deliberately NOT multipart: parsing
        it would pull ``python-multipart`` into the server extra, and the raw
        body also lets the size gate fire on ``Content-Length`` alone.

        The target is named either by ``flow_id`` or by an explicit
        ``machine_id`` + ``project_root`` pair — see
        :func:`_resolve_attachment_target`, which also owns the ownership gate.
        """
        name = (filename or "").strip()

        try:
            target_machine, target_root, owned = await _resolve_attachment_target(
                identity_, flow_id, machine_id, project_root
            )
        except _UploadRelayError as exc:
            return _upload_error(exc.status, exc.code, exc.detail)

        if not name:
            raise HTTPException(
                status_code=422, detail="'filename' must not be empty"
            )

        # WHY the Content-Length gate runs before ``await request.body()``: the
        # body is the only large thing this endpoint touches, so refusing on the
        # declared length is what keeps an oversized upload from being pulled
        # into server memory at all. The header is client-supplied and therefore
        # advisory — the real length is re-checked below once the bytes are in
        # hand — but a well-behaved browser gets its 413 for free.
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                declared_size = int(declared)
            except (TypeError, ValueError):
                declared_size = 0
            if declared_size > protocol.MAX_UPLOAD_BYTES:
                return _upload_error(
                    413,
                    protocol.UPLOAD_ERR_TOO_LARGE,
                    f"upload exceeds the {protocol.MAX_UPLOAD_BYTES}-byte limit",
                )

        data = await request.body()
        if len(data) > protocol.MAX_UPLOAD_BYTES:
            return _upload_error(
                413,
                protocol.UPLOAD_ERR_TOO_LARGE,
                f"upload exceeds the {protocol.MAX_UPLOAD_BYTES}-byte limit",
            )

        if not manager.is_connected(target_machine):
            return _upload_error(
                503,
                UPLOAD_ERR_NOT_CONNECTED,
                f"machine '{target_machine}' is not connected",
            )
        # Capability gate, not a timeout: a pre-revision-5 daemon drops the
        # unknown frame silently, and the operator would sit behind an
        # unresolvable placeholder token for the full ack window before learning
        # anything. Answer immediately with a code the UI can explain instead.
        if not protocol.supports_uploads(owned.get("protocol_version")):
            return _upload_error(
                501,
                UPLOAD_ERR_UNSUPPORTED_DAEMON,
                (
                    f"daemon on '{target_machine}' does not support file uploads; "
                    "upgrade it to a build speaking protocol revision "
                    f"{protocol.MIN_UPLOAD_PROTOCOL_VERSION} or newer"
                ),
            )

        request_id = uuid.uuid4().hex
        try:
            message = protocol.make_upload_command(
                target_root,
                name,
                base64.b64encode(data).decode("ascii"),
                size=len(data),
                request_id=request_id,
            )
        except protocol.ProtocolError as exc:
            # The remaining constructor rejections are shapes the resolution
            # above cannot rule out on its own (a mirrored flow whose
            # project_root is relative, say) — surface them rather than putting
            # a frame the daemon would refuse on the wire.
            return _upload_error(422, protocol.UPLOAD_ERR_INVALID_PATH, str(exc))

        try:
            result = await _send_upload_command(target_machine, message, request_id)
        except _UploadRelayError as exc:
            return _upload_error(exc.status, exc.code, exc.detail)

        if not result.get("ok"):
            return _upload_failure(result, "upload failed on daemon")
        return JSONResponse(
            status_code=201,
            content={
                "status": "stored",
                # The daemon returns the path RELATIVE to the project root — the
                # exact string the operator's prompt will carry, and the reason
                # the daemon machine's absolute layout never reaches the browser.
                "path": str(result.get("path") or ""),
                "filename": name,
                "size": int(result.get("size") or 0),
                "machine_id": target_machine,
                "project_root": target_root,
                "deduplicated": bool(result.get("deduplicated")),
            },
        )

    # -- attachment read-back (protocol revision 6) -------------------------

    async def _send_fetch_command(
        machine_id: str, message: protocol.Message, request_id: str
    ) -> dict:
        """Dispatch a fetch command and await the daemon's answer.

        The mirror of :func:`_send_upload_command`, down to the discard-on-both-
        failure-paths rule: a fetch is issued once per inline thumbnail per
        render, so a silent daemon that leaked one waiter per attempt would grow
        the registry for as long as the operator keeps a conversation open.
        """
        fut = fetch_command_registry.register(request_id)
        sent = await manager.send_to(machine_id, message)
        if not sent:
            fetch_command_registry.discard(request_id, fut)
            raise _UploadRelayError(
                503,
                UPLOAD_ERR_NOT_CONNECTED,
                f"failed to deliver FETCH_COMMAND to '{machine_id}'",
            )
        try:
            return await asyncio.wait_for(fut, timeout=FETCH_COMMAND_TIMEOUT)
        except asyncio.TimeoutError:
            fetch_command_registry.discard(request_id, fut)
            raise _UploadRelayError(
                504,
                UPLOAD_ERR_TIMEOUT,
                f"timed out waiting for fetch result from '{machine_id}'",
            )

    def _fetch_failure(result: dict, fallback: str) -> JSONResponse:
        """Render a daemon ``ok=false`` fetch result as a mapped error."""
        code = str(result.get("error_code") or "")
        status = FETCH_ERROR_STATUS.get(code, FETCH_ERROR_STATUS_DEFAULT)
        return _upload_error(status, code, str(result.get("error") or fallback))

    @app.get("/api/uploads/file")
    async def fetch_attachment(
        path: str = "",
        flow_id: str = "",
        machine_id: str = "",
        project_root: str = "",
        identity_: OwnerIdentity = Depends(require_owner),
    ) -> Response:
        """Read one stored attachment back out of the target project's machine.

        The counterpart of ``POST /api/uploads``: the file lives on the daemon's
        disk, and the only channel to that machine is the daemon's own outbound
        socket, so the bytes come back the way they went out — base64 over the
        wire, raw in the HTTP body. *path* is the project-relative path the
        upload returned (and the operator's prompt carries); the daemon, not
        this endpoint, decides whether it points inside the uploads directory.

        Every failure mode is one the browser must be able to shrug off: this
        backs inline thumbnails, so an offline daemon / deleted file / legacy
        daemon simply leaves the message as the plain path text it already is.
        """
        rel_path = (path or "").strip()

        try:
            target_machine, target_root, owned = await _resolve_attachment_target(
                identity_, flow_id, machine_id, project_root
            )
        except _UploadRelayError as exc:
            return _upload_error(exc.status, exc.code, exc.detail)

        if not rel_path:
            return _upload_error(
                422,
                protocol.FETCH_ERR_INVALID_PATH,
                "'path' must not be empty",
            )

        if not manager.is_connected(target_machine):
            return _upload_error(
                503,
                UPLOAD_ERR_NOT_CONNECTED,
                f"machine '{target_machine}' is not connected",
            )
        # Capability gate, not a timeout — and it matters more here than on the
        # upload leg: a conversation can hold many inline images, and a
        # pre-revision-6 daemon drops every unknown frame silently, so without
        # this each thumbnail would hold a browser connection open for the full
        # ack window before failing.
        if not protocol.supports_fetch(owned.get("protocol_version")):
            return _upload_error(
                501,
                UPLOAD_ERR_UNSUPPORTED_DAEMON,
                (
                    f"daemon on '{target_machine}' does not support file "
                    "read-back; upgrade it to a build speaking protocol revision "
                    f"{protocol.MIN_FETCH_PROTOCOL_VERSION} or newer"
                ),
            )

        request_id = uuid.uuid4().hex
        try:
            message = protocol.make_fetch_command(
                target_root, rel_path, request_id=request_id
            )
        except protocol.ProtocolError as exc:
            # Cheap early reject of an obviously malformed path (absolute, or
            # carrying a '..' segment). The real containment boundary is the
            # daemon's resolved-path check — this only keeps a frame the daemon
            # would refuse anyway off the wire.
            return _upload_error(422, protocol.FETCH_ERR_INVALID_PATH, str(exc))

        try:
            result = await _send_fetch_command(target_machine, message, request_id)
        except _UploadRelayError as exc:
            return _upload_error(exc.status, exc.code, exc.detail)

        if not result.get("ok"):
            return _fetch_failure(result, "fetch failed on daemon")

        try:
            data = base64.b64decode(str(result.get("content_b64") or ""), validate=True)
        except (ValueError, TypeError):
            # A daemon that answered ok=true with an undecodable body is an
            # upstream fault, not the browser's — 502 for the same reason as
            # FETCH_ERROR_STATUS_DEFAULT.
            return _upload_error(
                502,
                protocol.FETCH_ERR_READ_FAILED,
                f"daemon on '{target_machine}' returned an undecodable payload",
            )

        suffix = os.path.splitext(rel_path)[1].lower()
        return Response(
            content=data,
            media_type=FETCH_CONTENT_TYPES.get(suffix, FETCH_CONTENT_TYPE_DEFAULT),
            headers={
                "Cache-Control": FETCH_CACHE_CONTROL,
                "Vary": FETCH_VARY,
                # Belt to the whitelist's braces: a non-whitelisted extension is
                # served as octet-stream, and nosniff is what stops the browser
                # from second-guessing that into a renderable type.
                "X-Content-Type-Options": "nosniff",
                # No filename parameter: the stored name is operator-supplied and
                # may be non-ASCII, and the browser only ever renders these in an
                # <img> or a new tab, never as a download.
                "Content-Disposition": "inline",
            },
        )

    @app.get("/api/flows/{flow_id}")
    async def flow_detail(
        flow_id: str, identity_: OwnerIdentity = Depends(require_owner)
    ) -> dict:
        result = await state.get_flow(flow_id, owner=_scope_for(identity_))
        if result is None:
            raise HTTPException(status_code=404, detail=f"flow '{flow_id}' not found")
        machine_id, flow = result
        return {"machine_id": machine_id, "flow": flow}

    def _validated_plan_mode(req: NewFlowRequest) -> Tuple[str, str]:
        """Validate the request's optional plan-mode fields.

        Returns the trimmed ``(decomposition, granularity)`` pair (``""`` for
        an omitted field — it stays off the wire so the daemon/CLI resolve
        project configuration / default). Raises ``HTTPException(422)`` for a
        non-empty value outside the protocol's legal set.
        """
        values = []
        for name, raw, allowed in (
            (
                "plan_decomposition",
                req.plan_decomposition,
                protocol.SPAWN_PLAN_DECOMPOSITION_VALUES,
            ),
            (
                "plan_granularity",
                req.plan_granularity,
                protocol.SPAWN_PLAN_GRANULARITY_VALUES,
            ),
        ):
            value = (raw or "").strip()
            if value and value not in allowed:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"'{name}' must be one of {sorted(allowed)}, got {value!r}"
                    ),
                )
            values.append(value)
        return values[0], values[1]

    # WHY: a plain nested function, NOT a @staticmethod — a staticmethod object
    # is only directly callable from Python 3.10, and this package supports
    # 3.9+. Decorating it would make the compatibility gate itself raise a
    # TypeError (HTTP 500) exactly where it is supposed to explain the refusal.
    def _plan_mode_unsupported(machine_id: str) -> JSONResponse:
        """Refuse an explicit plan mode for a pre-revision-8 daemon.

        A silent drop of the fields would downgrade the operator's explicit
        choice to the project configuration / default doctrine — a behavioural
        substitution, not a missing nicety — so it must surface as an
        immediate capability error the UI can explain (mirroring the upload /
        fetch refusal). The machine-readable ``reason`` lets the browser
        localize the message.
        """
        return JSONResponse(
            status_code=501,
            content={
                "detail": (
                    f"daemon on '{machine_id}' does not support an explicit "
                    "plan decomposition mode; upgrade it to a build speaking "
                    f"protocol revision {protocol.MIN_SPAWN_PLAN_MODE_PROTOCOL_VERSION} "
                    "or newer"
                ),
                "reason": "unsupported_daemon",
            },
        )

    async def _publish_flow_from_issue(
        req: NewFlowRequest,
        identity_: OwnerIdentity,
        from_issue_id: str,
    ) -> JSONResponse:
        """Dispatch a SPAWN_FLOW sourced from an existing issue.

        The issue's owner-scoped record is the authoritative source of the
        target machine / project — the request's *task* content is ignored and
        ``from_issue_id`` is threaded through to the daemon, which runs
        ``luo run --from-issue <id>`` and owns the full issue lifecycle
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
        decomposition, granularity = _validated_plan_mode(req)
        if decomposition or granularity:
            owned_issue_machine = await state.get_machine(
                machine_id, owner=scope
            )
            # Fail CLOSED on a missing machine record: an unverifiable daemon
            # cannot be assumed to speak revision 8, and dispatching anyway
            # would let a pre-revision-8 daemon silently ignore the fields and
            # downgrade the operator's explicit plan mode to project config.
            if owned_issue_machine is None or not protocol.supports_spawn_plan_mode(
                owned_issue_machine.get("protocol_version")
            ):
                return _plan_mode_unsupported(machine_id)
        message = protocol.make_spawn_flow(
            "",
            project_root=project_root,
            task_type=req.task_type,
            discover=req.discover,
            worktree=req.worktree,
            from_issue_id=from_issue_id,
            plan_decomposition=decomposition,
            plan_granularity=granularity,
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
        # machine.project_roots entry. The owning daemon auto-runs `luo init`
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
        decomposition, granularity = _validated_plan_mode(req)
        if (decomposition or granularity) and not protocol.supports_spawn_plan_mode(
            owned.get("protocol_version")
        ):
            return _plan_mode_unsupported(machine_id)
        message = protocol.make_spawn_flow(
            task,
            project_root=project_root,
            task_type=req.task_type,
            discover=req.discover,
            worktree=req.worktree,
            plan_decomposition=decomposition,
            plan_granularity=granularity,
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
        # WHY reachable-first resolution matters here: a response is a file drop
        # — the daemon writes ``tianluo/calls/<id>.response`` under the flow's
        # project_root, which the live ``luo run`` drains from that same (here
        # shared) disk. Any reachable daemon mounting it serves the answer, so
        # routing to a disconnected peer that merely reported the flow first
        # would bounce the operator's reply while the flow sits blocked.
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
        file, and ``luo run`` acts on it immediately — gracefully stopping the
        LLM call in flight and opening the interjection dialog with this text
        as its opening message, or opening that dialog at the pause point the
        flow is sitting on.
        """
        text = req.text.strip()
        if not text:
            raise HTTPException(status_code=422, detail="'text' must not be empty")
        # Ownership gate: a flow on another owner's machine reads as absent.
        # Same reasoning as ``/respond``: an interjection is a file drop into the
        # flow's shared call directory, so it must go to a daemon that can be
        # reached rather than the first one that reported the flow.
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

        The flow must be in a directly-resumable status (FAILED or PAUSED, or
        carry the daemon's authoritative ``resumable`` flag), must not be
        archived/history-only, and must belong to the requesting owner. The
        owning daemon receives a ``MSG_SPAWN_FLOW`` carrying the
        ``resume_flow_id`` field; the daemon validates the local
        ``engine.json`` and spawns ``luo run --resume --flow-id <id>``.

        Validation mirrors ``daemon.request_resume()`` so the endpoint's
        receipt is honest about what actually happens:

        * unknown flow / cross-owner → 404 (the caller can neither see nor
          control it);
        * existing but still running (a live process holds it, so its
          ``resumable`` flag is ``False`` after the live-process gate) → 409
          with an explicit "still running, cannot resume" message rather than
          a misleading ``resume_dispatched``;
        * completed → 409 (terminal, nothing to resume);
        * resumable → dispatch and return 202 ``resume_dispatched``.
        """
        scope = _scope_for(identity_)
        # Same reachable-first resolution the resumability gate below uses, so
        # the 409 reason is read off the SAME snapshot it judged, not a peer's.
        existing = await state.get_flow(flow_id, owner=scope)
        if existing is None:
            # Unknown flow or owned by a different owner — leak nothing.
            raise HTTPException(
                status_code=404,
                detail=f"flow '{flow_id}' not found",
            )
        result = await state.is_flow_resumable(flow_id, owner=scope)
        if result is None:
            # The flow exists and belongs to the caller but is not resumable.
            # Distinguish the two reasons so the user gets an honest receipt
            # instead of an optimistic dispatched.
            _resolved_machine, existing_flow = existing
            status = str(existing_flow.get("status") or "").lower()
            if status == "completed":
                raise HTTPException(
                    status_code=409,
                    detail="该 flow 已完成，无法 resume",
                )
            # running / init / recovering (or any other in-progress state):
            # there is a live process for this flow, so resuming would be a
            # no-op double-spawn that the daemon's request_resume() guard
            # bounces anyway. Name the owning machine so the WebUI "继续" path
            # can tell the operator *where* it is running — on a shared
            # filesystem the flow may be held by a run on another host that this
            # server (and that host's process table) cannot terminate remotely.
            #
            # WHY the holder is resolved separately instead of reusing the
            # machine ``get_flow`` returned: that resolution is reachable-first
            # (it answers "which daemon can serve this request"), so on a shared
            # filesystem — where every daemon aggregating the same engine.json
            # reports the same flow — it can name a mere observer. Naming the
            # wrong host is worse than naming none: the operator would go run
            # ``luo end-session`` on a machine that holds nothing.
            # ``find_live_holder_machine`` returns ``None`` unless exactly one
            # reporter shows the flow live on itself, and the refusal then stays
            # machine-agnostic.
            #
            # WHY a JSONResponse instead of HTTPException: the machine id has to
            # reach the browser as a MACHINE-READABLE field so the WebUI renders
            # the refusal from its own language pack (an en-US console must not
            # be shown this Chinese sentence). ``detail`` stays as the
            # non-browser (curl / API) fallback wording, and ``reason`` lets the
            # browser localize the machine-agnostic variant too.
            holder_machine = await state.find_live_holder_machine(
                flow_id, owner=scope
            )
            if holder_machine:
                return JSONResponse(
                    status_code=409,
                    content={
                        "detail": (
                            f"该 flow 正在机器 {holder_machine} 上运行，无法 resume"
                        ),
                        "holder_machine": holder_machine,
                        "reason": "still_running",
                    },
                )
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "该 flow 仍在运行，无法 resume",
                    "reason": "still_running",
                },
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

    @app.post("/api/flows/{flow_id}/end")
    async def end_flow(
        flow_id: str,
        req: EndSessionRequest = EndSessionRequest(),
        identity_: OwnerIdentity = Depends(require_owner),
    ) -> JSONResponse:
        """End (terminate + archive) a session.

        For a main-branch session this just terminates the live ``luo run``
        process; for a worktree session it additionally archives the worktree
        the way a normally-completed session is cleaned up, so no dangling
        worktree is left behind. The owning daemon receives a
        ``MSG_END_SESSION`` and off-loads the work to an ``luo end-session``
        subprocess.

        The receipt is honest, mirroring the resume gate:

        * unknown flow / cross-owner → 404 (leak nothing);
        * already completed → 409 (nothing left to end);
        * owning machine not connected / delivery failure → 503;
        * otherwise dispatch and return 202 ``end_dispatched``.
        """
        scope = _scope_for(identity_)
        # Same reachable-first resolution the endability gate judges.
        existing = await state.get_flow(flow_id, owner=scope)
        if existing is None:
            raise HTTPException(
                status_code=404,
                detail=f"flow '{flow_id}' not found",
            )
        result = await state.is_flow_endable(flow_id, owner=scope)
        if result is None:
            # Visible to the caller but already completed — nothing to end.
            raise HTTPException(
                status_code=409,
                detail="该 flow 已完成/已结束，无法 end",
            )
        machine_id, flow = result
        if not manager.is_connected(machine_id):
            raise HTTPException(
                status_code=503,
                detail=f"machine '{machine_id}' owning flow '{flow_id}' is not connected",
            )
        message = protocol.make_end_session(
            flow_id,
            project_root=flow.get("project_root", ""),
            reason=req.reason or "user terminated",
        )
        ok = await manager.send_to(machine_id, message)
        if not ok:
            raise HTTPException(
                status_code=503,
                detail=f"failed to deliver end SESSION to '{machine_id}'",
            )
        return JSONResponse(
            status_code=202,
            content={
                "status": "end_dispatched",
                "machine_id": machine_id,
                "flow_id": flow_id,
                "reason": req.reason or "user terminated",
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

    # -- on-demand full-text detail (issue description / call prompt) --------
    # STATUS_UPDATE now carries only truncated issue descriptions and call
    # prompts (wire economy); the untruncated body is fetched on demand here.
    # The server routes a MSG_DETAIL_REQUEST to the owning daemon and awaits the
    # MSG_DETAIL_DATA reply via the DetailRequestRegistry, mirroring the issue
    # command / history pull request-registry pattern.

    async def _pull_detail(
        kind: str,
        target_id: str,
        machine_id: str,
        project_root: str,
    ) -> Dict[str, Any]:
        """Pull one issue/call full-text record from *machine_id*'s daemon.

        Concurrent openers of the same physical target
        ``(kind, target_id, machine_id, project_root)`` share ONE downlink pull
        (leader/follower). The machine/root are part of the coalescing key so two
        owners with the same local issue/call id on different machines/projects
        never join one pull (which would leak one owner's detail to the other).
        Raises 503 when the daemon is not connected / the send fails, 504 on
        reply timeout, and 404 when the daemon reports the target missing /
        unreadable.
        """
        if not manager.is_connected(machine_id):
            raise HTTPException(
                status_code=503,
                detail=f"machine '{machine_id}' is not connected",
            )
        request_id = uuid.uuid4().hex
        fut, is_leader, active_rid = detail_registry.begin(
            request_id, kind, target_id, machine_id, project_root
        )
        if is_leader:
            sent = await request_detail(
                manager,
                machine_id,
                kind,
                target_id,
                request_id,
                project_root=project_root,
            )
            if not sent:
                detail_registry.discard(request_id, fut)
                raise HTTPException(
                    status_code=503,
                    detail=f"failed to deliver DETAIL_REQUEST to '{machine_id}'",
                )
        try:
            result = await asyncio.wait_for(fut, timeout=DETAIL_PULL_TIMEOUT)
        except asyncio.TimeoutError:
            detail_registry.discard(active_rid, fut)
            raise HTTPException(
                status_code=504,
                detail=f"timed out fetching {kind} detail for '{target_id}'",
            )
        if not result.get("ok"):
            raise HTTPException(
                status_code=404,
                detail=result.get("error") or f"{kind} detail unavailable",
            )
        detail = result.get("detail")
        return detail if isinstance(detail, dict) else {}

    @app.get("/api/issues/{issue_id}/detail")
    async def issue_detail(
        issue_id: str,
        machine_id: str = "",
        project_root: str = "",
        identity_: OwnerIdentity = Depends(require_owner),
    ) -> dict:
        """Fetch an issue's untruncated description from the owning daemon."""
        scope = _scope_for(identity_)
        result = await state.get_issue_by_id(
            issue_id,
            owner=scope,
            machine_id=machine_id or None,
            project_root=project_root or None,
        )
        if result is None:
            raise HTTPException(
                status_code=404, detail=f"issue '{issue_id}' not found"
            )
        mid, root, mirror_issue = result
        # A pre-v3 daemon never received MSG_DETAIL_REQUEST and would silently
        # drop it (parking a doomed waiter until DETAIL_PULL_TIMEOUT → 504). But
        # its STATUS_UPDATE mirror still carries the untruncated description
        # (it does no wire-economy clipping), so serve that directly. This keeps
        # the WebUI working against an un-upgraded daemon in a staggered rollout:
        # no 10 s wait, no spurious "load failed" hint, no locked edit textarea.
        if not await state.machine_supports_detail_pull(mid, owner=scope):
            return {"machine_id": mid, "project_root": root, "issue": mirror_issue}
        detail = await _pull_detail(
            protocol.DETAIL_KIND_ISSUE, issue_id, mid, root
        )
        return {"machine_id": mid, "project_root": root, "issue": detail}

    @app.get("/api/calls/{call_id}/detail")
    async def call_detail(
        call_id: str,
        machine_id: str = "",
        project_root: str = "",
        identity_: OwnerIdentity = Depends(require_owner),
    ) -> dict:
        """Fetch a pending call's untruncated prompt from the owning daemon.

        When *machine_id* is supplied it is ownership-checked; otherwise the
        owning machine (and its project root) is resolved by scanning the
        owner's flows for the call.
        """
        scope = _scope_for(identity_)
        mid = machine_id.strip()
        root = project_root.strip()
        if mid:
            owned = await state.get_machine(mid, owner=scope)
            if owned is None:
                raise HTTPException(
                    status_code=404, detail=f"machine '{mid}' not found"
                )
        else:
            # Pin resolution to the supplied project_root: a local call_id is
            # only unique within one project, so a bare scan could match an
            # earlier owner-scoped project's same-id call and misroute the
            # detail request to the wrong daemon.
            resolved = await state.find_call_owner(
                call_id, owner=scope, project_root=root or None
            )
            if resolved is None:
                raise HTTPException(
                    status_code=404, detail=f"call '{call_id}' not found"
                )
            mid, resolved_root = resolved
            if not root:
                root = resolved_root
        # Pre-v3 daemons drop MSG_DETAIL_REQUEST silently; their STATUS_UPDATE
        # mirror already carries the untruncated prompt, so serve it directly
        # instead of parking a waiter that can only time out (→ 504). Mirrors the
        # issue_detail fall-back so the WebUI degrades gracefully in a staggered
        # multi-machine rollout.
        if not await state.machine_supports_detail_pull(mid, owner=scope):
            mirror = await state.get_pending_call(
                call_id, owner=scope, machine_id=mid, project_root=root or None
            )
            return {"machine_id": mid, "call": mirror or {}}
        detail = await _pull_detail(
            protocol.DETAIL_KIND_CALL, call_id, mid, root
        )
        return {"machine_id": mid, "call": detail}

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
                    if key == "description":
                        # The STATUS_UPDATE issue mirror carries only the
                        # _DESC_CLIP preview of each description, not the full
                        # body (detail-on-demand). Compare the expected text
                        # clipped the same way, else any edit whose new
                        # description exceeds _DESC_CLIP chars could never match
                        # the (already-clipped) mirror and this ack-timeout
                        # fallback would falsely report a successful edit as
                        # failed.
                        if str(iss.get(key) or "") != _clip_desc(str(val or "")):
                            return None
                        continue
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

    # -- web-console message history ---------------------------------------
    # The up/down-arrow recall list behind the two prompt inputs. Both routes
    # are scoped to ``identity_.owner_id`` and the owner is NEVER a parameter:
    # there is no request shape that reads or writes another owner's history,
    # so cross-owner leakage is not a check that can be forgotten. Unsent text
    # (the draft) is deliberately not here — that belongs to the device and
    # stays in the browser's localStorage.

    def _history_channel(channel: str) -> str:
        """Validate a channel path segment, or 404 like any unknown resource.

        An unknown channel is a nonexistent collection, not a malformed
        request — same semantics as an unknown flow / issue id elsewhere.
        """
        ch = (channel or "").strip()
        if ch not in MESSAGE_HISTORY_CHANNELS:
            raise HTTPException(
                status_code=404, detail=f"unknown history channel '{channel}'"
            )
        return ch

    @app.get("/api/message-history/{channel}")
    async def get_message_history(
        channel: str,
        identity_: OwnerIdentity = Depends(require_owner),
    ) -> dict:
        ch = _history_channel(channel)
        entries = await asyncio.to_thread(
            store.list_message_history, identity_.owner_id, ch
        )
        # WHY each entry is an object rather than a bare string: the browser
        # folds this list together with what it sent itself, and only the
        # server-assigned id can say whether a row here *is* one of those
        # sends. The same text legitimately appears twice in one history, so a
        # text comparison would silently swallow a genuine repeat.
        return {
            "channel": ch,
            "entries": [{"id": e.entry_id, "text": e.text} for e in entries],
            "count": len(entries),
        }

    @app.post("/api/message-history/{channel}")
    async def append_message_history(
        channel: str,
        req: AppendHistoryRequest,
        identity_: OwnerIdentity = Depends(require_owner),
    ) -> dict:
        ch = _history_channel(channel)
        # Blank text, and text identical to the newest entry, are dropped by
        # the store rather than rejected: the browser fires this off after a
        # successful send and has nothing useful to do with an error.
        #
        # WHY the reply carries an id in BOTH outcomes: it is the caller's only
        # way to recognise this append in a list it reads later. On "appended"
        # that is the new row; on "skipped" it is the existing row the
        # adjacent-repeat rule folded this send onto — which is a different
        # fact from "some older row happens to hold the same words", and the
        # browser must not confuse the two. Blank text is the one case with no
        # row to name, and reports a null id.
        result = await asyncio.to_thread(
            store.append_message_history, identity_.owner_id, ch, req.text
        )
        return {
            "status": "appended" if result.appended else "skipped",
            "channel": ch,
            "appended": result.appended,
            "entry_id": result.entry_id,
            "max_entries": MESSAGE_HISTORY_MAX_ENTRIES,
        }

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

    async def _refresh_history_index() -> None:
        """Ask every connected daemon to rebuild + re-push its history index.

        Briefly waits for the re-pushes to land; with no connected daemon —
        or when a daemon is slow and the wait times out — the caller degrades
        gracefully to the currently cached index.
        """
        waiters = await broadcast_index_refresh(manager, index_refresh_registry)
        if not waiters:
            return
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

    @app.get("/api/history")
    async def list_history(
        identity_: OwnerIdentity = Depends(require_owner),
    ) -> dict:
        # Entering the history view must always reflect the latest sessions, not
        # whatever index a daemon last happened to push.
        await _refresh_history_index()
        index = await state.get_history_index(owner=_scope_for(identity_))
        return {"sessions": index, "count": len(index)}

    async def _resolve_history_owner(
        flow_id: str, scope: Optional[str]
    ) -> Tuple[str, str]:
        """Resolve ``(owning_machine, owner)`` for *flow_id* or raise 404.

        Ownership gate shared by every history read: a flow whose owning machine
        belongs to another owner (or is unknown) reads as absent — even if its
        records happen to be cached server-side — so one owner can never reach
        another's history, bundle or single detail alike.

        WHY a miss here is a plain 404 with NO daemon-side work: a stale browser
        tab polling the URL of a deleted / never-existing flow would otherwise
        drive a full build_index cold rebuild (~17.5k stats) on every connected
        daemon per request — re-creating on demand the exact rebuild storm the
        presence gating removed. Discovery of a freshly created flow is the list
        endpoint's job (it alone pays the forced re-push).
        """
        owner_machine = await state.find_machine_for_history_flow(
            flow_id, owner=scope
        )
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
        return owner_machine, target_owner

    async def _pull_history_from_daemon(
        flow_id: str,
        owner_machine: str,
        connection: Any,
        project_root: str,
        *,
        deadline: Optional[float] = None,
        cursor: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Dispatch a coalesced daemon full pull for this flow and await it.

        Shared by the bundle cache-miss path, the running-worktree self-heal
        reconcile and the on-demand detail endpoint, so all three go through the
        SAME leader/follower coalescing:
        concurrent callers for ``(flow_id, owner_machine)`` collapse onto one
        in-flight ``MSG_HISTORY_REQUEST``; a follower whose leader failed
        before dispatching (``_PullAbandoned``) retries as a fresh leader
        rather than waiting out the timeout behind a pull that will never be
        answered. Raises ``HTTPException`` 404 when no connected daemon owns
        the flow and 504 on a pull timeout.

        INVARIANT: the ENTIRE round trip is bounded by one deadline — the
        leader's dispatch as much as the wait for the reply, and every
        ``_PullAbandoned`` retry out of the same remaining budget rather than a
        fresh one. *deadline* (a ``loop.time()`` instant) lets a caller that has
        further work to do under the same bound — the detail route follows the
        reply's later frames — share its budget instead of stacking a second
        one; omitted, the call gets its own ``HISTORY_PULL_TIMEOUT``.
        *cursor* makes the request an INCREMENTAL one: the daemon answers with
        ``append`` frames from that water mark rather than rebuilding the whole
        bundle. Only the interrupted-delivery repair uses it — every other
        caller wants the cursorless full — and the coalescing key deliberately
        ignores it: a concurrent full pull already brings back everything an
        incremental would, so collapsing the two onto one reply is correct.

        WHY the dispatch needs bounding too: ``send_to_connection`` ends in
        ``websocket.send_text``, which has no timeout of its own and blocks for
        as long as a backpressured or half-open daemon socket refuses to drain.
        A caller parked there never reached the ``wait_for`` below, so the
        browser sat on the loading state with no upper bound at all.
        """
        loop = asyncio.get_running_loop()
        if deadline is None:
            deadline = loop.time() + HISTORY_PULL_TIMEOUT
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise HTTPException(
                    status_code=504,
                    detail=f"timed out pulling history for flow '{flow_id}'",
                )
            fut, is_leader = history_registry.begin_pull(flow_id, owner_machine)
            pull_dispatched = False
            try:
                if is_leader:
                    # The leader's daemon send is INSIDE this try so the
                    # ``finally`` also covers a cancellation that fires while
                    # ``send_text`` is blocked: without it, a client
                    # disconnecting mid-send would leave the leader's waiter
                    # parked and the key marked in-flight forever, turning
                    # every later request into a follower that sends no new
                    # ``MSG_HISTORY_REQUEST`` and merely times out.
                    sent = await asyncio.wait_for(
                        request_history(
                            manager,
                            state,
                            flow_id,
                            machine_id=owner_machine,
                            connection=connection,
                            project_root=project_root or "",
                            cursor=cursor,
                        ),
                        timeout=remaining,
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
                await asyncio.wait_for(
                    fut, timeout=max(0.0, deadline - loop.time())
                )
                return
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
                    # cancelled before / while sending). Release every
                    # follower parked behind it and clear the in-flight marker
                    # so the next request leads a fresh pull immediately —
                    # otherwise ``discard`` would leave the marker set
                    # (followers remain) and strand them until
                    # ``HISTORY_PULL_TIMEOUT`` waiting on a
                    # ``MSG_HISTORY_REQUEST`` that was never sent.
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
                    # the marker. Because the leader keeps the pull genuinely
                    # in flight here, followers correctly stay parked on the
                    # already-dispatched request.
                    history_registry.discard(flow_id, fut, owner_machine)

    async def _pull_history_window_from_daemon(
        flow_id: str,
        owner_machine: str,
        project_root: str,
        *,
        blocks: int,
        before_step: str = "",
        steps: Optional[List[str]] = None,
        if_signature: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Read one step-block window straight from the owning daemon.

        Returns the daemon's accumulated reply, or ``None`` when the daemon is
        unreachable / declined / timed out — every one of which the caller
        answers by degrading to the cursorless full pull, never by failing the
        request.

        *if_signature*, when the daemon honours it and the flow is unchanged,
        comes back as ``{"not_modified": True}`` with no records — the caller
        must answer that with :func:`_window_not_modified_payload` rather than
        treating it as an empty window.

        INVARIANT: this reply is served to the browser and then DROPPED — it is
        never installed as a bundle. That is the whole point of the leg: a flow
        whose history exceeds the server's entire history-cache budget cannot be
        held, and caching it anyway is what produced the eviction⇄整量回源 storm
        (the bundle is evicted, the next page misses, the whole flow is pulled
        again, ad infinitum). Serving windows statelessly makes a flow of any
        size browsable to its first block at a bounded, constant cost per page.
        """
        request_id = uuid.uuid4().hex
        fut = window_registry.begin(request_id)
        try:
            sent = await asyncio.wait_for(
                request_history_window(
                    manager,
                    owner_machine,
                    flow_id,
                    request_id,
                    project_root=project_root or "",
                    count=blocks,
                    before_step=before_step,
                    steps=steps or (),
                    if_signature=if_signature,
                ),
                timeout=HISTORY_PULL_TIMEOUT,
            )
            if not sent:
                return None
            reply = await asyncio.wait_for(fut, timeout=HISTORY_PULL_TIMEOUT)
        except asyncio.TimeoutError:
            # A daemon that did not answer in time is a reason to DEGRADE, not
            # to fail: the caller falls back to the whole-flow pull. Cancellation
            # is deliberately NOT caught — the client is gone, and swallowing it
            # would leave this task running past its request.
            return None
        finally:
            window_registry.discard(request_id)
        if not isinstance(reply, dict) or not reply.get("ok", True):
            return None
        return reply

    async def _history_window_reply(
        request: Request,
        flow_id: str,
        *,
        blocks: int,
        before_step: str,
        if_signature: str,
        owner_machine: str,
        target_owner: Optional[str],
    ) -> Response:
        """Serve one step-block window of *flow_id*, cache-first then daemon.

        Three sources, in order:

        1. the cached bundle, sliced by step block — free, and the common case
           for any flow small enough to be cached at all;
        2. a direct daemon window read, served through without ever creating a
           bundle — the path that makes a flow larger than the whole cache
           budget browsable;
        3. the existing cursorless full pull, then (1) again — the compatibility
           fallback for a daemon too old to know the window request.

        WHY (3) cannot reintroduce the defect: it is reached only for a
        pre-revision-9 daemon, and while its pull runs the flow is PINNED against
        eviction (see ``ServerState.pin_history_pull``), so the bundle the drain
        fills survives to be sliced instead of being swept away mid-drain and
        re-pulled by the next page.

        *if_signature* (the client's ``wsig``) only ever reaches leg (2): it is a
        probe against the DAEMON's own view of the files, so the cached leg —
        which already mints a real progress token and is compared by bundle
        signature — ignores it and answers as it always did.
        """
        try:
            count = max(1, min(int(blocks), HISTORY_WINDOW_MAX_BLOCKS))
        except (TypeError, ValueError):
            count = 10
        cached = await state.get_history_window_snapshot(
            flow_id,
            count=count,
            before_step=before_step or None,
            expected_machine_id=owner_machine,
            expected_owner=target_owner,
        )
        if cached is not None:
            return await _history_response(
                {"flow_id": flow_id, "cached": True, **cached}, request
            )
        owner_connection = await manager.get_connection(owner_machine)
        if owner_connection is None:
            raise HTTPException(
                status_code=404,
                detail=f"no connected daemon owns history for flow '{flow_id}'",
            )
        flow_project_root = await state.get_history_flow_project_root(
            flow_id, owner=target_owner
        )
        if await state.machine_supports_history_window(
            owner_machine, owner=target_owner
        ):
            reply = await _pull_history_window_from_daemon(
                flow_id,
                owner_machine,
                flow_project_root or "",
                blocks=count,
                before_step=before_step,
                if_signature=if_signature,
            )
            if reply is not None and reply.get("not_modified"):
                # The steady state of a watched over-budget flow: nothing was
                # read on the daemon, nothing is shaped here, and the browser
                # keeps the window it already holds.
                return await _history_response(
                    _window_not_modified_payload(flow_id, owner_machine),
                    request,
                )
            if reply is not None:
                # The relayed window builds no bundle, so the usage/cost surface
                # has to be answered from the index the daemon already pushes —
                # otherwise the flows this leg exists for (the big ones) would be
                # the only ones opening with the usage region hidden.
                usage = await state.get_history_flow_usage_summary(
                    flow_id, owner=target_owner
                )
                return await _history_response(
                    {
                        "flow_id": flow_id,
                        "cached": False,
                        **_window_payload_from_daemon(
                            flow_id, owner_machine, reply,
                            count=count, before_step=before_step,
                            usage=usage,
                        ),
                    },
                    request,
                )
        # Legacy daemon (or a window read that failed): fall back to the
        # whole-flow pull and slice the resulting bundle. Throttled and pinned
        # exactly like the unwindowed miss path.
        if not await state.full_pull_throttled(flow_id):
            await state.mark_full_pull(flow_id)
            await state.pin_history_pull(flow_id)
            try:
                await _pull_history_from_daemon(
                    flow_id, owner_machine, owner_connection,
                    flow_project_root or "",
                )
            except HTTPException:
                # Availability over freshness: a failed fallback pull still gets
                # one more cache read below, and an empty window reply is a state
                # the client can retry from — a 5xx here would instead wedge the
                # view on an error for a transient daemon hiccup.
                pass
            finally:
                await state.release_history_pull(flow_id)
        rebuilt = await state.get_history_window_snapshot(
            flow_id,
            count=count,
            before_step=before_step or None,
            expected_machine_id=owner_machine,
            expected_owner=target_owner,
        )
        if rebuilt is not None:
            return await _history_response(
                {"flow_id": flow_id, "cached": False, **rebuilt}, request
            )
        raise HTTPException(
            status_code=503,
            detail=(
                "history for flow "
                f"'{flow_id}' is not available yet; retry shortly"
            ),
        )

    @app.get("/api/history/{flow_id}")
    async def history_detail(
        request: Request,
        flow_id: str,
        identity_: OwnerIdentity = Depends(require_owner),
        after: Optional[str] = None,
        sig: Optional[str] = None,
        missing: Optional[str] = None,
        window: Optional[int] = None,
        before: Optional[str] = None,
        wsig: Optional[str] = None,
    ) -> Response:
        # ``missing`` names the records the client's own cursor self-check found
        # it does not hold (``stepId:ord,ord;…``). Any unparseable value degrades
        # to "no missing list" — the client then falls back to a full rebuild,
        # which is correct, just less frugal (see ``parse_missing_param``).
        missing_map = parse_missing_param(missing)
        # WHY the signed cursor (``after``/``sig``) plays NO part in authn/authz:
        # ``require_owner`` resolves identity from the session cookie ALONE and
        # runs BEFORE this body, so a stale / expired / rotated signed cursor can
        # never produce a 401 — it is only decoded inside ``get_history_snapshot``
        # (well after the owner gate) and fail-closes to a recoverable ``full``
        # snapshot flagged ``resync`` so the client resynchronises its cursor
        # rather than bare-retrying. This is the Defect-C finding: the field's
        # intermittent 401s under a daemon-reconnect storm are genuine session
        # failures (the daemon ``/ws`` key channel never touches the browser
        # ``SessionStore``), and MUST stay fail-closed — cross-owner still reads
        # 404 below, unauthenticated still 401s at ``require_owner``.
        # Ownership gate first (see _resolve_history_owner): a flow owned by
        # another owner reads as absent even when its records are cached here.
        scope = _scope_for(identity_)
        owner_machine, target_owner = await _resolve_history_owner(flow_id, scope)

        # WINDOWED READ. ``window`` asks for N STEP BLOCKS rather than the whole
        # flow: the tail N on an open, the N before ``before`` on a page-up. It
        # is a wholly separate delivery from the progress-token dialect below and
        # deliberately shares none of its state — a window is addressed by step
        # id, not by a cursor offset, so there is no token to honour, invalidate
        # or resync. An older client never sends the parameter and reaches the
        # unchanged path below.
        #
        # ``wsig`` is the windowed dialect's conditional-read probe (the
        # ``window.signature`` of the reply the client is polling from). It is
        # NOT a cursor and pins nothing here: the server holds no state for it
        # and merely relays it to the daemon that minted it, which answers
        # ``not_modified`` while the flow's blocks are untouched. It is what
        # gives a token-less windowed view a cheap steady-state poll.
        if window is not None:
            return await _history_window_reply(
                request,
                flow_id,
                blocks=window,
                before_step=before or "",
                if_signature=wsig or "",
                owner_machine=owner_machine,
                target_owner=target_owner,
            )

        async def _pull_from_daemon(
            connection: Any,
            project_root: str,
            *,
            cursor: Optional[Dict[str, Any]] = None,
        ) -> None:
            """This route's binding of the shared coalesced daemon pull.

            *cursor* asks the daemon for an ``append`` backfill from that water
            mark instead of a full rebuild (see the interrupted-delivery repair
            below); ``None`` is the cursorless full every other caller wants.
            """
            await _pull_history_from_daemon(
                flow_id, owner_machine, connection, project_root, cursor=cursor
            )

        # Cache hit: serve a not-modified / delta / full snapshot atomically.
        # ``after`` is the opaque progress token the client echoes on a WS
        # reconnect; ``sig`` is the bundle content signature it holds. When the
        # token is still in sync AND the signature matches, the snapshot answers
        # ``delivery: "not_modified"`` (extra-small — the self-heal poll's cheap
        # "nothing changed" reply); a valid token behind the record count yields
        # ``delivery: "delta"`` (the tail); every fallback yields
        # ``delivery: "full"``. Binding ``expected_machine_id`` to the owning
        # machine makes a bundle that has since moved daemons read as a miss, so
        # we re-pull the authoritative records below rather than serve a stale
        # snapshot.
        # A ``missing`` list additionally yields ``delivery: "backfill"`` — the
        # named records taken out of the SAME cached bundle — so a client whose
        # cursor self-check found a hole can close it without a full rebuild.
        snapshot = await state.get_history_snapshot(
            flow_id,
            after=after,
            expected_machine_id=owner_machine,
            expected_owner=target_owner,
            known_signature=sig,
            missing=missing_map,
        )
        if snapshot is not None:
            # Running-worktree self-heal. A ``not_modified`` reply means the
            # client is provably in sync with the SERVER CACHE — but for a live
            # ``--worktree`` flow whose discovery is still appending rounds, the
            # cache itself can be behind the daemon: a round the live push dropped
            # or collided on never landed, so both cache and client freeze at the
            # first round and every later poll keeps answering ``not_modified``.
            # When that flow is still active under a worktree root — RUNNING or
            # PAUSED-on-a-human-reply, the exact state a discovery round enters
            # right after writing its records — reconcile the cache against the
            # daemon ONCE — subject to the same
            # ``full_pull_throttled`` floor the cache-miss path uses, so a 3 s
            # self-heal poll cannot fan out one回源 pull per tick. A no-op re-pull
            # keeps the bundle generation (see ``append_history``) so an already
            # in-sync client still gets ``not_modified``; a re-pull that brings
            # the missing round rolls the generation and the re-read below serves
            # it as ``full``. Ordinary (non-worktree / completed) flows skip this
            # entirely and are served straight from cache, unchanged.
            #
            # WHY the paused window is safe to reconcile in at all: the reconcile
            # is only ever allowed to *top up* the bundle, never to replace it
            # with whatever the daemon happened to answer. Widening the gate to
            # ``paused`` without that floor is what turned a mis-resolved daemon
            # read into a blank chat pane (#287) — so the add-only semantics is
            # the precondition of this branch, enforced both in
            # ``append_history`` and by the shrinking-full guard below.
            #
            # WHY a ``missing`` request never reconciles: it is a targeted read
            # of records the cache already holds, not a claim that the cache is
            # behind the daemon. Letting it fire a回源 pull would also spend the
            # ``mark_full_pull`` throttle budget that the genuine self-heal poll
            # depends on.
            if (
                not missing_map
                and snapshot.get("delivery") == "not_modified"
                and not await state.full_pull_throttled(flow_id)
                and (
                    await state.is_active_worktree_flow(
                        flow_id, owner=target_owner
                    )
                    # INTERRUPTED-DELIVERY REPAIR. The worktree predicate above
                    # is about a cache that may be behind a still-WRITING flow;
                    # this one is about a cache that is behind what the daemon
                    # already tried to send it. A flow's history is delivered in
                    # up to ~150 frames, and a socket that dies in the middle
                    # leaves a self-consistent PREFIX behind (see
                    # ``ServerState._OpenDelivery``): the client is provably in
                    # sync with the cache, so this poll answers
                    # ``not_modified``, and nothing else will ever ask for the
                    # rest.
                    #
                    # WHY it must NOT be gated on ``is_active_worktree_flow``:
                    # the flow this happens to is typically COMPLETED — opening a
                    # big archived session is what triggers a multi-frame pull at
                    # all — and a completed flow receives no further appends, so
                    # the append-driven self-heal that repairs a live flow never
                    # fires for it. Under the old gate the one flow that could
                    # not repair itself was the exact one that needed to. The
                    # gate is widened, not removed: a bundle with no interrupted
                    # delivery still never reconciles, so an ordinary completed
                    # flow's idle poll costs what it always did.
                    or await state.history_delivery_repair_due(flow_id)
                )
            ):
                owner_connection = await manager.get_connection(owner_machine)
                if owner_connection is not None:
                    reconcile_root = await state.get_history_flow_project_root(
                        flow_id, owner=target_owner
                    )
                    # Record count the cache held BEFORE the reconcile, so the
                    # add-only floor below can be an actual comparison rather than
                    # a mere emptiness test: a daemon read that resolves only part
                    # of a worktree flow's history answers with FEWER (but not
                    # zero) records, and adopting it would drop the later rounds.
                    cached_bundle = await state.get_history(flow_id)
                    cached_record_count = len(
                        (cached_bundle or {}).get("records") or []
                    )
                    # How to pull. A worktree self-heal stays what it was — a
                    # cursorless full, because that flow's history is split
                    # across two roots and only a whole re-read merges them.
                    # An interrupted delivery instead goes through
                    # ``plan_recovery_pull``, which gives two things a bare
                    # cursorless pull cannot:
                    #
                    #   * an INCREMENTAL backfill anchored at the server's own
                    #     water mark whenever the bundle is reusable. A repair
                    #     that re-drains from zero re-runs the very risk that
                    #     broke the bundle — a second interruption would leave a
                    #     SHORTER prefix — while an append-only backfill can only
                    #     extend it, so repeated repairs converge instead of
                    #     oscillating;
                    #   * the at-most-one-recovery-per-flow dedup, so a 3 s poll
                    #     cannot stack repairs on top of the drain already
                    #     filling the bundle. ``None`` means one is running: fall
                    #     through and serve the cache, unrepaired for now.
                    repair_cursor = None
                    if await state.history_delivery_incomplete(flow_id):
                        plan = await state.plan_recovery_pull(
                            flow_id, owner_machine, repair=True
                        )
                        if plan is None:
                            return await _history_response(
                                {"flow_id": flow_id, "cached": True, **snapshot},
                                request,
                            )
                        _kind, repair_cursor = plan
                    await state.mark_full_pull(flow_id)
                    try:
                        await _pull_from_daemon(
                            owner_connection,
                            reconcile_root or "",
                            cursor=repair_cursor,
                        )
                    except HTTPException:
                        # The reconcile is best-effort robustness: if the daemon
                        # pull fails (no connection) or times out, fall through
                        # and serve the cache we already hold rather than turning
                        # a routine self-heal poll into a user-visible error.
                        pass
                    reconciled = await state.get_history_snapshot(
                        flow_id,
                        after=after,
                        expected_machine_id=owner_machine,
                        expected_owner=target_owner,
                        known_signature=sig,
                    )
                    # INVARIANT: the reconcile has ADD-only semantics — its reply
                    # may extend the bundle, never shrink it. ``append_history``
                    # enforces that at the cache (a full frame carrying fewer
                    # records than the cached bundle can no longer overwrite it),
                    # and this is the matching floor on the wire: if the re-read
                    # somehow still comes back as a ``full`` rebuild carrying
                    # FEWER records than the cache held a moment ago (zero being
                    # the degenerate case), the client — which was provably in
                    # sync — would rebuild its chat pane with rounds missing
                    # (#287). Serve the snapshot we already validated instead, so
                    # a degraded reconcile costs a wasted pull and nothing else.
                    # ``None`` (bundle dropped / moved machine mid-pull) falls
                    # through to the same cached snapshot below.
                    if reconciled is not None and not (
                        reconciled.get("delivery") == "full"
                        and len(reconciled.get("records") or [])
                        < cached_record_count
                    ):
                        return await _history_response(
                            {"flow_id": flow_id, "cached": True, **reconciled},
                            request,
                        )
            return await _history_response(
                {"flow_id": flow_id, "cached": True, **snapshot}, request
            )
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
        # Resolve the flow's authoritative run root (its SessionMeta.project_root)
        # and hand it to the daemon as the single source of truth for *which*
        # root to read. A worktree-mode flow's history is split across the main
        # repo root (discovery) and the worktree root (later steps); telling the
        # daemon the authoritative root lets it merge both rather than guessing
        # the first registry root that matches and returning only discovery.
        # Resolving to ``None`` degrades to the legacy empty-root behaviour, so
        # an ordinary non-worktree pull is unaffected.
        flow_project_root = await state.get_history_flow_project_root(
            flow_id, owner=target_owner
        )
        # Full-rebuild throttle: a client stuck presenting a diverged token would
        # otherwise force a fresh multi-MB回源 pull on every self-heal poll. If a
        # full pull for this flow fired within the floor window, re-read the
        # cache once — that recent pull may have just populated an authoritative
        # bundle we can serve without another daemon round-trip — and only fall
        # through to a fresh pull when it is still a genuine miss. (Concurrent
        # misses already collapse onto one pull via the leader/follower registry
        # below; this floor additionally rate-limits *sequential* rapid misses.)
        if await state.full_pull_throttled(flow_id):
            throttled = await state.get_history_snapshot(
                flow_id,
                after=after,
                expected_machine_id=owner_machine,
                expected_owner=target_owner,
                known_signature=sig,
            )
            if throttled is not None:
                return await _history_response(
                    {"flow_id": flow_id, "cached": True, **throttled}, request
                )
        await state.mark_full_pull(flow_id)
        # Pin the flow against eviction for the DURATION of the pull. A large
        # flow drains as a ``full`` head plus dozens of ``append`` tails and
        # takes far longer than ``_HISTORY_VIEW_HOT_WINDOW``; nothing re-reads
        # meanwhile (this request is the reader, and it is parked here), so the
        # flow went cold MID-DRAIN and the budget evicted the very bundle the
        # drain was filling — the reply the browser finally got was a prefix,
        # whose next read missed and re-pulled, and so on. See
        # ``ServerState.pin_history_pull``.
        await state.pin_history_pull(flow_id)
        # Concurrent cache-miss requests for the same flow/machine (e.g. the
        # running-flow view and the history-detail view reconnecting at once)
        # share ONE in-flight daemon pull via ``_pull_from_daemon``: only the
        # leader sends the ``MSG_HISTORY_REQUEST``, the followers park on the same
        # reply. This prevents a second daemon reply from arriving after both
        # waiters were already resolved by the first — which, finding no waiter,
        # would replace the cache generation and broadcast ``mode: full`` to every
        # UI consumer, clearing the progress tokens REST just handed back.
        try:
            await _pull_from_daemon(owner_connection, flow_project_root or "")
        finally:
            await state.release_history_pull(flow_id)
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
            return await _history_response(
                {"flow_id": flow_id, "cached": False, **full}, request
            )
        raise HTTPException(
            status_code=409,
            detail=f"history ownership changed while pulling flow '{flow_id}'",
        )

    @app.get("/api/history/{flow_id}/detail")
    async def history_record_detail(
        flow_id: str,
        tool_use_id: str = "",
        step_id: str = "",
        ordinal: int = -1,
        source: str = DETAIL_SOURCE_PROGRESS,
        identity_: OwnerIdentity = Depends(require_owner),
    ) -> dict:
        """Serve ONE tool chip's detail body, on demand.

        The bundle response ships collapsed-state fields only (see
        :func:`~tianluo.server.history_summary.summarize_history_records`); this
        is where the browser comes back for the body when the user expands a
        chip. It follows the shape of ``GET /api/calls/{id}/detail``: an
        owner-scoped lookup that prefers the server's own memory and falls back
        to the owning daemon, whose on-disk jsonl is authoritative.

        Addressed by the record's stable ``step_id`` + ``ordinal`` identity plus
        the ``tool_use_id`` inside it. WHY not ``tool_use_id`` alone: it is
        unique only within a record — codex synthesizes ids like
        ``codex_tool_1`` per call, so two steps of one flow can each hold that
        id and a flow-wide scan would answer the first chip with the second
        call's body. ``step_id#ordinal`` is the same identity the frontend
        reconciles records by, and it survives the bundle rotations (append /
        full replace / re-pull) that a positional address would not.

        ``source`` is answered exactly as asked: the live ``stream_progress``
        payload and the final record's ``raw_json`` render visibly different
        panels (the daemon-built one can carry a pre-write diff the browser
        cannot reconstruct), so a source the bundle does not hold is reported
        unavailable rather than silently substituted. ``step`` is the third
        source — an engine step event's held-back ``data.step.inputs``, returned
        as the original message so "View raw" prints the record unchanged. It is
        the one source that takes no ``tool_use_id``: a step event carries no
        tool call, so the record address is the whole request.

        Status codes are what the frontend's "detail unavailable" state reads:
        503 the owning daemon is not reachable, 504 it did not answer in time,
        404 the flow (or the call) genuinely does not exist here.
        """
        tool_use_id = (tool_use_id or "").strip()
        step_id = (step_id or "").strip()
        # Anything but an explicitly named source reads as the stream_progress
        # payload, so an unknown value degrades to the common case rather than
        # 4xx-ing a browser that is merely newer or older.
        wanted = (
            DETAIL_SOURCE_RAW if source == DETAIL_SOURCE_RAW
            else DETAIL_SOURCE_STEP if source == DETAIL_SOURCE_STEP
            else DETAIL_SOURCE_PROGRESS
        )
        # A step event holds no tool call: its record address IS the whole
        # request. Every other source names one call inside the addressed
        # record, so a missing id there is still the ambiguous request it was.
        if not tool_use_id and wanted != DETAIL_SOURCE_STEP:
            raise HTTPException(
                status_code=422, detail="tool_use_id is required"
            )
        if not step_id or ordinal < 0:
            raise HTTPException(
                status_code=422,
                detail="step_id and a non-negative ordinal are required",
            )
        scope = _scope_for(identity_)
        owner_machine, target_owner = await _resolve_history_owner(flow_id, scope)

        async def _read() -> Tuple[bool, Dict[str, Any]]:
            return await state.get_history_record_detail(
                flow_id,
                step_id=step_id,
                ordinal=ordinal,
                tool_use_id=tool_use_id,
                source=wanted,
                expected_machine_id=owner_machine,
                expected_owner=target_owner,
            )

        loop = asyncio.get_running_loop()
        # The whole route — the daemon DISPATCH, the wait for its reply, and the
        # follow of that reply's later frames — is bounded by this one instant,
        # which is the one existing pull timeout. It is handed to
        # ``_pull_history_from_daemon`` rather than left to bound only the tail
        # follow: the send at the head of the round trip has no timeout of its
        # own, so a stalled daemon socket used to park the route before any
        # deadline was ever consulted and the expanded panel sat on its loading
        # message forever instead of falling back to the unavailable state.
        deadline = loop.time() + HISTORY_PULL_TIMEOUT

        async def _read_through_pull_tail() -> Optional[Dict[str, Any]]:
            """Re-read the just-pulled bundle, following the daemon's tail.

            WHY a single re-read is not enough: a pull whose history exceeds the
            daemon's per-frame byte budget is answered as a ``full`` head
            followed by ``append`` tails (see ``_handle_history_request``), and
            the shared pull waiter is resolved by the FIRST authoritative frame.
            A chip whose body sits in a later tail would therefore read as
            absent — the browser would paint the localized "unavailable" state
            while its detail was still on the wire from a perfectly healthy
            daemon.

            INVARIANT: the follow ends only on a verdict the BUNDLE states, never
            on elapsed silence. ``settled`` is the bundle's own "this line will
            not arrive": the addressed record is here, or the daemon already
            streamed a higher ordinal for the step, so it read past this line and
            skipped it. A cursor total is deliberately NOT such a verdict — a
            drain advances it frame by frame, and its head can declare the whole
            file while the records lag — see ``get_history_record_detail``. There
            is no daemon "recovery complete" signal on the wire either, and
            adding one would change the daemon→server protocol this split
            explicitly does not touch, so a quiet gap between two 256 KiB tails
            must read as "still coming", not as completion. The only other exit
            is the shared deadline above, and it exits as 504 rather than as
            ``None``: running out of time is "the daemon did not answer in
            time", not "this call has no such body" — the browser paints the
            same unavailable state either way, but an operator reading the
            status code must not see a timeout reported as a genuine 404.

            WHY it watches the bundle rather than parking another waiter on
            ``history_registry``: a parked waiter makes the next ``mode: full``
            frame for this flow read as a pull reply and suppresses its
            ``/ws/ui`` broadcast. That is correct while a pull is genuinely in
            flight, but this follow outlives the pull it started from, so it
            must observe the cache without changing how frames are relayed.
            """
            while True:
                _cached, found = await _read()
                if found["detail"] is not None:
                    return found["detail"]
                if found["settled"]:
                    return None
                now = loop.time()
                if now >= deadline:
                    raise HTTPException(
                        status_code=504,
                        detail=(
                            "timed out pulling detail for flow "
                            f"'{flow_id}'"
                        ),
                    )
                await asyncio.sleep(min(HISTORY_PULL_TAIL_POLL, deadline - now))

        cached, found = await _read()
        detail = found["detail"]
        if detail is None and cached and not found["settled"]:
            # A bundle IS cached but has not yet reached this line: another
            # request's recovery installed the drain's head and its later frames
            # are still arriving. Dispatching a rival pull would fight that
            # drain, so follow the one already in flight — under the same single
            # deadline, and ending on the same bundle-stated verdict, never on a
            # silent gap between two frames.
            detail = await _read_through_pull_tail()
        elif detail is None and not cached:
            # The bundle was never cached, or the budget sweep evicted it since
            # the summary was served. Re-pull it through the SAME coalesced
            # leader/follower path the bundle cache-miss uses, then follow the
            # reply's frames. A bundle that IS cached and whose addressed record
            # is settled falls straight through to the 404 below — see
            # get_history_record_detail.
            owner_connection = await manager.get_connection(owner_machine)
            if owner_connection is None:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "no connected daemon owns history for flow "
                        f"'{flow_id}'"
                    ),
                )
            flow_project_root = await state.get_history_flow_project_root(
                flow_id, owner=target_owner
            )
            # Prefer a single-BLOCK window read when the daemon knows one: the
            # addressed record's step file is the smallest unit that can hold
            # it, and reading it directly answers the chip without pulling — and
            # then having to evict — a whole flow whose history may exceed the
            # entire cache budget. The reply is never cached (same invariant as
            # the windowed snapshot leg), so the record is located in the reply
            # itself rather than by re-reading the bundle.
            if await state.machine_supports_history_window(
                owner_machine, owner=target_owner
            ):
                block = await _pull_history_window_from_daemon(
                    flow_id,
                    owner_machine,
                    flow_project_root or "",
                    blocks=1,
                    steps=[step_id],
                )
                if block is not None:
                    located = locate_record_detail(
                        block.get("records") or [],
                        step_id=step_id,
                        ordinal=ordinal,
                        tool_use_id=tool_use_id,
                        source=wanted,
                    )
                    if located["detail"] is not None:
                        return {
                            "flow_id": flow_id,
                            "tool_use_id": tool_use_id,
                            **located["detail"],
                        }
                    if located["record_found"] or located["passed"]:
                        # The block IS the authoritative copy of this step, so a
                        # miss here is a genuine 404 — falling through to a whole
                        # flow pull could only re-learn the same answer.
                        raise HTTPException(
                            status_code=404,
                            detail=(
                                f"no detail for step record '{step_id}#{ordinal}'"
                                if wanted == DETAIL_SOURCE_STEP
                                else f"no detail for tool call '{tool_use_id}'"
                            ),
                        )
            try:
                await _pull_history_from_daemon(
                    flow_id,
                    owner_machine,
                    owner_connection,
                    flow_project_root or "",
                    deadline=deadline,
                )
            except HTTPException as exc:
                # The pull path reports "no daemon took the request" as 404
                # because for a whole bundle that is indistinguishable from an
                # unknown flow. Here the flow IS known (the owner gate passed),
                # so the honest answer is that the body is temporarily
                # unreachable — which is the state the chip renders.
                if exc.status_code == 404:
                    raise HTTPException(
                        status_code=503,
                        detail=(
                            "could not reach the daemon owning flow "
                            f"'{flow_id}'"
                        ),
                    ) from None
                raise
            detail = await _read_through_pull_tail()
        if detail is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"no detail for step record '{step_id}#{ordinal}'"
                    if wanted == DETAIL_SOURCE_STEP
                    else f"no detail for tool call '{tool_use_id}'"
                ),
            )
        return {"flow_id": flow_id, "tool_use_id": tool_use_id, **detail}

    # -- frontend (static files) -------------------------------------------

    # The language registry the frontend boots from. Unauthenticated like the
    # static assets themselves: it is picked before the operator signs in, and it
    # exposes nothing but the shipped locale codes.
    @app.get("/i18n/index.json")
    async def i18n_manifest() -> dict:
        return {"languages": _discover_ui_languages()}

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
    """Translate a :class:`tianluo.config.ServerConfig` into ``create_app`` kwargs.

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
    ``tianluo.yaml`` / the global config actually take effect on the running
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
    history_cache = getattr(server_cfg, "history_cache", None)
    kwargs = {
        "db_path": str(server_cfg.db_path),
        "auth_config": {"providers": provider_entries},
        "session_store": session_store,
        "rate_limiter": rate_limiter,
    }
    if history_cache is not None:
        # ``server.history_cache`` is what makes the in-RAM history relay's
        # ceiling a deployment decision (a 1 GB LXC container and a workstation
        # need different numbers); without this leg the dataclass would parse and
        # be silently ignored, exactly the drift this helper exists to prevent.
        kwargs["history_cache_budget_bytes"] = history_cache.budget_bytes()
        kwargs["history_cache_report_interval"] = (
            history_cache.report_interval_seconds
        )
        kwargs["history_cache_report_threshold_percent"] = (
            history_cache.report_threshold_percent
        )
    return kwargs


def run(
    host: str = "127.0.0.1",
    port: int = protocol.DEFAULT_SERVER_PORT,
    *,
    db_path: Optional[str] = None,
    auth_config: Optional[dict] = None,
    session_store: Optional[SessionStore] = None,
    rate_limiter: Optional[LoginRateLimiter] = None,
    history_cache_budget_bytes: Optional[int] = None,
    history_cache_report_interval: Optional[int] = None,
    history_cache_report_threshold_percent: Optional[int] = None,
    log_level: str = "info",
) -> None:
    """Start the SE3 central server with uvicorn (blocking).

    *db_path* selects the sqlite store backing owners / identities / daemon
    keys / break-glass tokens. The CLI passes the persistent default so a token
    minted via ``tianluo-server bootstrap-token`` is consumable by the live server;
    ``None`` falls back to an in-memory store (used by tests). *auth_config* /
    *session_store* / *rate_limiter* carry the resolved ``server.auth.*``
    configuration through to :func:`create_app`, and ``history_cache_*`` the
    resolved ``server.history_cache.*``.
    """
    import uvicorn

    app = create_app(
        db_path=db_path,
        auth_config=auth_config,
        session_store=session_store,
        rate_limiter=rate_limiter,
        history_cache_budget_bytes=history_cache_budget_bytes,
        history_cache_report_interval=history_cache_report_interval,
        history_cache_report_threshold_percent=(
            history_cache_report_threshold_percent
        ),
    )
    # Explicitly assert permessage-deflate on the server↔(daemon|browser) WS
    # legs. uvicorn's ``websockets`` protocol negotiates it by default, but the
    # traffic-reduction work depends on it, so we make the intent visible and
    # confirmed rather than relying on an undocumented default. ``ws_per_message_deflate``
    # only exists on newer uvicorn; fall back cleanly (the default already
    # enables deflate) so an older pin does not break startup.
    run_kwargs = dict(
        host=host,
        port=port,
        log_level=log_level,
        ws_max_size=protocol.MAX_WS_MESSAGE_BYTES,
        # State the transport keepalive instead of inheriting uvicorn's
        # undeclared 20 s / 20 s. A daemon draining a large flow's history sends
        # ~150 frames back to back, and uvicorn's ping rides the same inbound
        # stream the app is consuming — so a receive loop held up behind that
        # backlog used to lose the connection to
        # ``1011 INTERNAL_ERROR: keepalive ping timeout`` mid-drain, taking the
        # rest of the reply with it. Sized to the application-level liveness
        # rule this server already runs on (``ws.PING_INTERVAL`` /
        # ``ws.HEARTBEAT_TIMEOUT``), so the transport never declares a peer dead
        # that the protocol above it still considers alive.
        #
        # WHY this is a margin and NOT the fix: the head-of-line block that
        # created those delays is removed by the per-client outbound queue in
        # ``ws._UiClientChannel`` — this only stops a merely-slow drain from
        # being killed by a timeout tighter than the app's own.
        ws_ping_interval=PING_INTERVAL,
        ws_ping_timeout=HEARTBEAT_TIMEOUT,
    )
    try:
        uvicorn.run(app, ws_per_message_deflate=True, **run_kwargs)
    except TypeError:
        logger.debug(
            "uvicorn lacks ws_per_message_deflate; relying on its default "
            "(permessage-deflate still negotiated by the websockets protocol)"
        )
        uvicorn.run(app, **run_kwargs)


def main(argv: Optional[list] = None) -> None:
    """``tianluo-server`` console-script entry point.

    Parses ``--host`` / ``--port`` / ``--db-path``, loads the ``server:``
    configuration (``tianluo.yaml`` + global ``~/.se3/config.yaml``), and runs the
    server with the resolved auth providers / cookie / lockout / db-path
    settings. Kept dependency-light (argparse + the core config loader) so the
    friendly missing-extra check in :func:`tianluo.server.main` stays the first
    thing a user without the extra sees.
    """
    import argparse

    from tianluo.config import load_server_config

    parser = argparse.ArgumentParser(
        prog="tianluo-server", description="SE3 central control-plane server"
    )
    parser.add_argument(
        "--version",
        "-v",
        action="version",
        version=f"tianluo-server version {__version__}",
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
