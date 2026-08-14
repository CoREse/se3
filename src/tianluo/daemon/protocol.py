"""The daemon↔server WebSocket protocol.

This module is the *single source of truth* for the wire protocol spoken
between an :class:`~tianluo.daemon.client.DaemonClient` (running inside a resident
``luo daemon``) and the central server (``tianluo-server``). Both sides import this
module — the daemon from the core package, the server from ``tianluo.server`` —
so the message schema can never drift between them.

Wire format
-----------
Every message is a JSON object with exactly four top-level keys::

    {"type": <str>, "seq": <int>, "timestamp": <float>, "payload": <object>}

* ``type`` — one of the message-type constants below.
* ``seq`` — a monotonically increasing per-connection sequence number,
  assigned by the sender (0 when not tracked).
* ``timestamp`` — Unix epoch seconds at send time.
* ``payload`` — a type-specific JSON object (see the per-type helpers).

Message directions
------------------
* daemon → server: :data:`MSG_HELLO`, :data:`MSG_STATUS_UPDATE`,
  :data:`MSG_KEEPALIVE`, :data:`MSG_CALL_NOTIFICATION`, :data:`MSG_PONG`,
  :data:`MSG_HISTORY_INDEX`, :data:`MSG_HISTORY_INDEX_DELTA`,
  :data:`MSG_HISTORY_DATA`, :data:`MSG_DETAIL_DATA`, :data:`MSG_ISSUE_RESULT`,
  :data:`MSG_PROJECT_RESULT`, :data:`MSG_SPAWN_FAILED`,
  :data:`MSG_UPLOAD_RESULT`, :data:`MSG_FETCH_RESULT`.
* server → daemon: :data:`MSG_WELCOME`, :data:`MSG_SPAWN_FLOW`,
  :data:`MSG_RESPOND_CALL`, :data:`MSG_PING`, :data:`MSG_HISTORY_REQUEST`,
  :data:`MSG_HISTORY_INDEX_REQUEST`, :data:`MSG_INTERJECT_FLOW`,
  :data:`MSG_ISSUE_COMMAND`, :data:`MSG_PROJECT_COMMAND`,
  :data:`MSG_DETAIL_REQUEST`, :data:`MSG_END_SESSION`, :data:`MSG_VIEWERS`,
  :data:`MSG_UPLOAD_COMMAND`, :data:`MSG_FETCH_COMMAND`.

Backward compatibility
----------------------
Protocol version 2 added the history messages. A peer speaking an older
revision will never *send* them; if it ever *receives* one it does not
recognise, the frame is rejected as an unknown type — callers decoding
untrusted frames should therefore tolerate :class:`ProtocolError` rather
than crash, so new and old peers can interoperate.

Protocol version 3 added the *traffic-reduction* messages
(:data:`MSG_KEEPALIVE`, :data:`MSG_HISTORY_INDEX_DELTA`,
:data:`MSG_DETAIL_REQUEST`, :data:`MSG_DETAIL_DATA`). Unlike the earlier
additive types, these carry a real behavioural downgrade risk: if a daemon
sent a KEEPALIVE (in place of a periodic STATUS_UPDATE) or an incremental
HISTORY_INDEX_DELTA to a version-2 server, that server would reject the frame
as an unknown type and lose the heartbeat / index update entirely. The version
was therefore bumped to ``3`` so each side can read the peer's advertised
``protocol_version`` (HELLO / WELCOME) and **fall back to the full-frame
semantics** — periodic full STATUS_UPDATE and full HISTORY_INDEX, no keepalive
or delta, detail inlined rather than fetched on demand — whenever the peer
speaks a revision older than 3. The version-negotiation and fall-back logic
lives in the daemon client and server relay; this module only owns the wire
schema, the version constant, and this contract. Callers decoding untrusted
frames must still tolerate :class:`ProtocolError` for genuinely unknown types.

The multi-tenant control plane added an optional ``key`` field to the HELLO
payload (the daemon credential the server resolves to an owner). It is purely
additive: a daemon with no key omits the field, and an older single-tenant
server that does not understand it simply ignores it — so the version was not
bumped for it. The key is a secret and MUST never be logged.

Protocol version 4 added the *presence* signalling (:data:`MSG_VIEWERS` and
the optional ``viewers`` field piggybacked on :data:`MSG_PING`), which lets a
daemon throttle its polling / push cadence to a low-power gear while no
browser is watching the web UI. The behavioural risk runs the other way from
revision 3: a daemon must never downshift on the strength of *absent* viewer
information — an older server simply never reports it. The version was bumped
to ``4`` so the daemon can gate the low-power gear on the peer's advertised
``protocol_version`` (see :func:`supports_presence`) and **fail open to
today's full-speed behaviour** whenever the server speaks an older revision
or has not yet reported a count. The gear-shifting logic lives in the daemon
client and server hub; this module only owns the wire schema, the version
constant, and this contract.

Protocol version 5 added the *upload* channel (:data:`MSG_UPLOAD_COMMAND` /
:data:`MSG_UPLOAD_RESULT`), which relays a file the operator pasted into the
web UI's prompt box to the daemon owning that flow's project, where it is
written under the project's runtime ``uploads/`` directory. Earlier additive
server→daemon commands (END_SESSION, PROJECT_COMMAND) deliberately did *not*
bump the version, because an older daemon that ignores the frame merely costs
one visible timeout on a low-frequency, human-initiated action. Uploads are
different in kind: they happen inline in the user's typing, several per
message, and the placeholder token sitting in the textarea cannot be resolved
until the ack arrives — silently waiting out a request timeout on every paste
is not an acceptable outcome. The version was therefore bumped to ``5`` so the
server can consult the daemon's advertised ``protocol_version`` (see
:func:`supports_uploads`) *before* dispatching and answer with an immediate,
explainable "this machine's daemon is too old" instead of a timeout. As with
every other gate here, this module owns only the wire schema, the version
constant, and this contract; the dispatch decision lives in the server.

Protocol version 6 added the *fetch* channel (:data:`MSG_FETCH_COMMAND` /
:data:`MSG_FETCH_RESULT`) — the read-back counterpart of revision 5's upload
channel. Revision 5 could only push a file *to* the daemon's machine; the bytes
then lived under that project's ``uploads/`` directory with no way back, so the
web UI could render an attached screenshot's *path* but never its content. The
fetch channel closes that loop: the server asks the owning daemon for one file
under the project's uploads directory and gets the bytes back, so the browser
can show the image inline in the conversation. The version was bumped to ``6``
for the same reason revision 5 was bumped, only more acutely: an inline
thumbnail is a *rendering* path, not a human-initiated action — a single
conversation may reference many images and re-render on every scroll, so an
older daemon that silently drops the unknown frame would leave every one of
those requests waiting out the full dispatch timeout and exhaust the browser's
per-origin connection budget. The server therefore consults
:func:`supports_fetch` before dispatching and answers "this machine's daemon is
too old" up front, which the UI degrades to plain path text. This module owns
only the wire schema, the version constant, and this contract; the containment
check that keeps a fetch inside the uploads directory lives in the daemon, and
the dispatch decision lives in the server.

Protocol version 7 added the optional ``implementation_strategy`` field on
:data:`MSG_SPAWN_FLOW`. It is a *behavioural* field, not merely additive: a
pre-7 daemon that silently ignores it would quietly fall back to the project's
``workflow.implementation_strategy`` / the planned default — turning an
operator's explicit web choice into a different flow than the one they
published. The version was therefore bumped to ``7`` so the server can consult
the daemon's advertised ``protocol_version`` (see
:func:`supports_spawn_strategy`) *before* dispatching and answer with an
immediate, explainable "this machine's daemon is too old" capability error
instead of a silent downgrade. A daemon that never receives the field (an
older server, or a request without an explicit strategy) behaves exactly as
before: the CLI adds no ``--implementation-strategy`` option, so the project
configuration / default resolves the strategy. As with every other gate here,
this module owns only the wire schema, the version constant, and this
contract; the dispatch decision lives in the server. The usage-summary fields
added to :data:`MSG_STATUS_UPDATE` / :data:`MSG_HISTORY_DATA` snapshots in the
same revision are purely additive payload keys and are not themselves the
reason for the bump — as is the ``usage_catalog`` field on
:data:`MSG_HISTORY_DATA`, which carries the project's pricing table so the
server's append-time re-aggregation never rebuilds with no catalog.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional

# Protocol revision. Bumped only on a breaking wire change; both daemon and
# server advertise it in HELLO / WELCOME so a mismatch can be surfaced.
# Revision "2" added the history messages (MSG_HISTORY_*).
# Revision "3" added the traffic-reduction messages (MSG_KEEPALIVE,
# MSG_HISTORY_INDEX_DELTA, MSG_DETAIL_REQUEST/DATA); a peer advertising "2" or
# older must be driven with full-frame semantics (see the module docstring).
# Revision "4" added the presence signalling (MSG_VIEWERS + the optional
# ``viewers`` field on MSG_PING); a daemon may only enter its low-power idle
# gear when the server advertises "4" or newer — otherwise it assumes viewers
# are present and stays at full speed (see the module docstring).
# Revision "5" added the upload channel (MSG_UPLOAD_COMMAND / MSG_UPLOAD_RESULT);
# the server must not dispatch an upload to a daemon advertising "4" or older —
# it answers "unsupported daemon" up front rather than making the browser wait
# out a timeout on every pasted file (see the module docstring).
# Revision "6" added the fetch channel (MSG_FETCH_COMMAND / MSG_FETCH_RESULT),
# the read-back half of the upload channel. The same up-front refusal applies
# and matters more here: fetches ride a rendering path (many per conversation,
# repeated on every re-render), so letting them each wait out a timeout against
# a pre-fetch daemon would saturate the browser's connections rather than cost
# one visible delay (see the module docstring).
# Revision "7" added the optional implementation_strategy field on SPAWN_FLOW;
# the server refuses to dispatch an explicit strategy to a peer advertising
# "6" or older so a silent downgrade to project config / planned can never
# masquerade as the requested strategy (see the module docstring).
PROTOCOL_VERSION = "7"

#: Minimum peer ``protocol_version`` that understands the revision-3
#: traffic-reduction messages. When a peer advertises a value below this in its
#: HELLO / WELCOME, the sender MUST fall back to the full-frame semantics
#: (periodic full STATUS_UPDATE + full HISTORY_INDEX, no keepalive/delta, detail
#: inlined) instead of emitting a keepalive / index delta the peer would reject.
#: Version strings are compared as integers with a safe fallback so a
#: non-numeric or missing value degrades to "legacy" (full semantics).
MIN_VERSION_TRAFFIC_REDUCTION = 3


def supports_traffic_reduction(peer_version: Any) -> bool:
    """Return whether *peer_version* understands the revision-3 lean messages.

    Used by both the daemon client and the server relay to decide, per peer,
    whether it is safe to emit :data:`MSG_KEEPALIVE` /
    :data:`MSG_HISTORY_INDEX_DELTA` / detail messages, or whether the peer is a
    legacy revision that must be driven with full-frame semantics. A missing or
    non-numeric version degrades safely to ``False`` (full semantics).
    """
    try:
        return int(str(peer_version).strip()) >= MIN_VERSION_TRAFFIC_REDUCTION
    except (TypeError, ValueError):
        return False


#: Minimum peer ``protocol_version`` that reports browser-presence information
#: (:data:`MSG_VIEWERS` edges + the ``viewers`` level field on PING). When the
#: server advertises a value below this in its WELCOME, the daemon MUST assume
#: viewers are present and keep its full-speed cadence — a legacy server never
#: reports a count, and downshifting on that silence would trade away the web
#: UI's real-time behaviour. Version strings are compared as integers with a
#: safe fallback so a non-numeric or missing value degrades to "legacy"
#: (full speed).
MIN_VERSION_PRESENCE = 4


def supports_presence(peer_version: Any) -> bool:
    """Return whether *peer_version* reports the revision-4 presence signals.

    Used by the daemon client to decide whether the peer server will ever send
    :data:`MSG_VIEWERS` / PING ``viewers`` information — i.e. whether a
    ``viewers == 0`` belief is trustworthy enough to enter the low-power idle
    gear. A missing or non-numeric version degrades safely to ``False``
    (assume watched, run full speed), mirroring
    :func:`supports_traffic_reduction`.
    """
    try:
        return int(str(peer_version).strip()) >= MIN_VERSION_PRESENCE
    except (TypeError, ValueError):
        return False


#: Minimum peer ``protocol_version`` that understands the revision-5 upload
#: channel (:data:`MSG_UPLOAD_COMMAND` / :data:`MSG_UPLOAD_RESULT`). The server
#: checks this *before* dispatching so an older daemon is reported as
#: unsupported immediately: an upload sits in the user's typing path with a
#: placeholder token that cannot be resolved until the ack lands, so falling
#: back to "wait for the request timeout" would stall every paste. Version
#: strings are compared as integers with a safe fallback so a non-numeric or
#: missing value degrades to "legacy" (no upload support).
MIN_UPLOAD_PROTOCOL_VERSION = 5


def supports_uploads(peer_version: Any) -> bool:
    """Return whether *peer_version* understands the revision-5 upload channel.

    Used by the server's upload endpoint to decide whether the target machine's
    daemon can accept a :data:`MSG_UPLOAD_COMMAND` at all. A missing or
    non-numeric version degrades safely to ``False`` (report unsupported rather
    than dispatch a frame the peer would reject), mirroring
    :func:`supports_traffic_reduction` and :func:`supports_presence`.
    """
    try:
        return int(str(peer_version).strip()) >= MIN_UPLOAD_PROTOCOL_VERSION
    except (TypeError, ValueError):
        return False


#: Minimum peer ``protocol_version`` that understands the revision-6 fetch
#: channel (:data:`MSG_FETCH_COMMAND` / :data:`MSG_FETCH_RESULT`). The server
#: checks this *before* dispatching for a sharper version of the upload
#: channel's reason: a fetch backs an inline thumbnail, so a single conversation
#: issues many of them and re-issues them on every re-render. Against a
#: pre-fetch daemon that silently drops the unknown frame, "wait for the request
#: timeout" would not cost one visible delay but pin the browser's whole
#: per-origin connection budget on requests that can never succeed. Version
#: strings are compared as integers with a safe fallback so a non-numeric or
#: missing value degrades to "legacy" (no fetch support).
MIN_FETCH_PROTOCOL_VERSION = 6


def supports_fetch(peer_version: Any) -> bool:
    """Return whether *peer_version* understands the revision-6 fetch channel.

    Used by the server's file read-back endpoint to decide whether the target
    machine's daemon can accept a :data:`MSG_FETCH_COMMAND` at all. A missing or
    non-numeric version degrades safely to ``False`` (report unsupported, which
    the web UI degrades to plain path text, rather than dispatch a frame the
    peer would drop), mirroring :func:`supports_uploads`.
    """
    try:
        return int(str(peer_version).strip()) >= MIN_FETCH_PROTOCOL_VERSION
    except (TypeError, ValueError):
        return False


#: Minimum peer ``protocol_version`` that understands the optional
#: ``implementation_strategy`` field on :data:`MSG_SPAWN_FLOW`. The server
#: checks this *before* dispatching a request that carries an explicit
#: strategy: a pre-7 daemon that silently dropped the field would run the
#: project-configured / default strategy instead — a *behavioural* downgrade
#: that must surface as an explainable capability error, never as a quiet
#: substitution. A missing or non-numeric version degrades safely to ``False``
#: (refuse, report unsupported), mirroring :func:`supports_uploads` and
#: :func:`supports_fetch`.
MIN_SPAWN_STRATEGY_PROTOCOL_VERSION = 7


#: Valid values for the optional ``implementation_strategy`` spawn field.
SPAWN_STRATEGY_VALUES: FrozenSet[str] = frozenset({"auto", "direct", "planned"})


def supports_spawn_strategy(peer_version: Any) -> bool:
    """Return whether *peer_version* understands the revision-7 spawn-strategy field."""
    try:
        return int(str(peer_version).strip()) >= MIN_SPAWN_STRATEGY_PROTOCOL_VERSION
    except (TypeError, ValueError):
        return False


# Default TCP port for the central server. This is the *single source of
# truth* for the default port: ``tianluo-server`` binds it when ``--port`` is
# omitted, and the daemon client fills it in when ``--server-url`` carries no
# explicit port. Keeping it here — alongside the wire protocol — guarantees
# both sides agree and removes the duplicated ``8080`` magic numbers.
DEFAULT_SERVER_PORT = 8080

# Default TCP port for the *TLS* (``wss://``) scheme. The daemon client fills
# this in — instead of :data:`DEFAULT_SERVER_PORT` — when ``--server-url``
# carries a ``wss://`` (or ``https://`` normalized to ``wss://``) scheme with
# no explicit port, because a TLS connection terminates at the reverse proxy's
# HTTPS port (443), not at tianluo-server's plaintext default (8080). In short:
# 8080 is the plaintext / ``ws`` default (and ``tianluo-server --port`` default),
# 443 is the ``wss`` scheme-aware default. This keeps both defaults as named
# constants here — the single source of truth — rather than as magic numbers
# scattered through the client.
DEFAULT_SERVER_TLS_PORT = 443

# Maximum size, in bytes, of a single daemon↔server WebSocket message frame.
# This is the *single source of truth* for the per-frame inbound cap on both
# sides: the daemon passes it as ``websockets.connect(max_size=…)`` and the
# server passes it as ``uvicorn.run(ws_max_size=…)``. Sharing the one constant
# keeps the two ends from drifting apart, exactly like DEFAULT_SERVER_PORT.
#
# It is raised well above the library defaults (websockets' 1 MiB, uvicorn's
# 16 MiB) because a ``MSG_HISTORY_DATA`` frame carrying a full session's
# conversation records is currently ~33-39 MB — under the old defaults the
# server silently dropped the oversized frame, so ``GET /api/history/{flow_id}``
# never resolved and returned 504. 256 MiB is a bounded large ceiling: it
# comfortably absorbs today's frames with headroom while still capping a
# pathological frame to protect server memory (we deliberately do not use
# ``None``/unbounded).
MAX_WS_MESSAGE_BYTES = 256 * 1024 * 1024

# Maximum size, in bytes, of a single file uploaded through the revision-5
# upload channel. This is the *single source of truth* for a limit enforced
# independently at three layers — the browser pre-checks ``file.size`` so an
# oversized paste never leaves the page, the server re-checks the request body
# because the browser is not trusted, and the daemon re-checks the decoded
# payload because the server is not trusted and the daemon's disk is the
# resource actually being protected. The frontend cannot import this module, so
# its mirrored literal is pinned by a static guard test instead.
#
# 20 MiB is chosen to cover the pasted screenshots / logs / small archives this
# channel exists for while staying an order of magnitude below
# MAX_WS_MESSAGE_BYTES: the daemon leg base64-encodes the bytes (a ~33% blow-up)
# and wraps them in a JSON frame, so the largest legal upload still leaves ample
# room under the per-frame cap.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

# -- message types: daemon -> server --------------------------------------
MSG_HELLO = "hello"
MSG_STATUS_UPDATE = "status_update"
MSG_CALL_NOTIFICATION = "call_notification"
MSG_PONG = "pong"
MSG_HISTORY_INDEX = "history_index"
MSG_HISTORY_DATA = "history_data"
#: daemon → server: an extra-small heartbeat frame emitted in place of a
#: periodic STATUS_UPDATE when the aggregated snapshot's content signature is
#: unchanged since the last push. It carries only the signature (and the seq /
#: timestamp every frame has), so the server can refresh the daemon's
#: online/last-seen time — preserving the exact offline-detection semantics of a
#: STATUS_UPDATE — without the daemon re-sending the full snapshot. Revision 3;
#: only sent to a peer that advertises support (see the module docstring).
MSG_KEEPALIVE = "keepalive"
#: daemon → server: an incremental history-index update carrying only the
#: SessionMeta rows that changed (``upserts``, keyed by ``flow_id``) and the
#: flow ids that disappeared (``removed``), instead of the whole index. The
#: server merges it into its in-memory full index. Full :data:`MSG_HISTORY_INDEX`
#: frames are still sent on connect / reconnect / HISTORY_INDEX_REQUEST as the
#: reconciliation baseline. Revision 3; only sent to a supporting peer.
MSG_HISTORY_INDEX_DELTA = "history_index_delta"
#: daemon → server: deliver the full text requested by a :data:`MSG_DETAIL_REQUEST`
#: (an issue's untruncated description, or a pending call's full prompt). Carries
#: the echoed ``request_id`` so the server can correlate it to the waiting
#: REST request, plus ``ok`` / ``detail`` / ``error``. Revision 3.
MSG_DETAIL_DATA = "detail_data"
#: daemon → server: report that a server-requested spawn / resume / project
#: init failed *after* the SPAWN_FLOW was dispatched. ``POST /api/flows``
#: replies ``202 dispatched`` immediately (the daemon spawns asynchronously),
#: so a failure that happens during ``ensure_se3_project`` / the fresh spawn /
#: a resume would otherwise be silent and leave the web UI stuck on the
#: "published" pseudo-success state. This frame carries the project root, the
#: real error text, and the originating task / issue / resume id so the server
#: can route it back to the UI as a visible error. Older servers that do not
#: recognise the type simply ignore it (mixed-version compatibility).
MSG_SPAWN_FAILED = "spawn_failed"

# -- message types: server -> daemon --------------------------------------
MSG_WELCOME = "welcome"
MSG_SPAWN_FLOW = "spawn_flow"
MSG_RESPOND_CALL = "respond_call"
MSG_PING = "ping"
MSG_HISTORY_REQUEST = "history_request"
#: server → daemon: force a fresh rebuild + immediate re-push of the history
#: index (:data:`MSG_HISTORY_INDEX`), bypassing the daemon's change-debounce.
#: The web ``GET /api/history`` broadcasts this to every connected daemon so
#: entering the history view always reflects the latest sessions rather than
#: the last index a daemon happened to push. The payload is empty — it has no
#: flow dimension and merely triggers the re-push.
MSG_HISTORY_INDEX_REQUEST = "history_index_request"
#: server → daemon: deliver a mid-flow user interjection to a running flow.
#: The daemon turns it into an ``interjection``-kind call file under
#: ``tianluo/calls/`` which ``luo run`` drains at the next step boundary.
MSG_INTERJECT_FLOW = "interject_flow"

#: server → daemon: end (terminate + archive) a session by ``flow_id``. The
#: daemon validates the flow against its supervisor and then off-loads the heavy
#: work — gracefully terminating the live ``luo run`` process and archiving a
#: worktree session the way a normally-completed session would be cleaned up —
#: to an ``luo end-session`` subprocess, so the event loop is never blocked by
#: the grace wait or the on-disk archival. Older daemons that do not recognise
#: the type simply ignore it (mixed-version compatibility), so no
#: ``PROTOCOL_VERSION`` bump is required.
MSG_END_SESSION = "end_session"

#: server → daemon: instruct the daemon to execute an issue write operation
#: (create / edit / close / reopen). The daemon resolves the project root,
#: validates the operation and delegates to :class:`IssueManager`.
MSG_ISSUE_COMMAND = "issue_command"

#: server → daemon: manage the machine's *project registry* — manually register
#: (``add``) or deregister (``remove``) a project root. The daemon validates the
#: path against its own filesystem and routes the change through the aggregator's
#: single registration seam (worktree→main folding, realpath dedup, registry
#: write-through), then fast-pushes a fresh snapshot so the web UI's project
#: views refresh. Like :data:`MSG_END_SESSION`, this is a purely *additive* type:
#: an older daemon that does not recognise it simply ignores the frame (the
#: server then surfaces one visible timeout for a low-frequency, human-initiated
#: action), so no ``PROTOCOL_VERSION`` bump is required. A bump would only be
#: warranted if an existing behaviour silently degraded — nothing here does.
MSG_PROJECT_COMMAND = "project_command"

#: server → daemon: write one file the operator attached in the web UI's prompt
#: box into the project's runtime ``uploads/`` directory, so the agent can later
#: read it by the project-relative path echoed back in
#: :data:`MSG_UPLOAD_RESULT`. The payload carries ``project_root``, the original
#: ``filename``, the base64-encoded ``content_b64`` (the wire is JSON lines, so
#: raw bytes cannot ride directly), the declared ``size`` and a ``request_id``.
#: Unlike :data:`MSG_END_SESSION` / :data:`MSG_PROJECT_COMMAND` — additive types
#: an older daemon may silently ignore at the cost of one timeout on a rare
#: human-initiated action — this one DID force a ``PROTOCOL_VERSION`` bump to
#: ``5``: an upload happens inline while the user types, once per attached file,
#: and the placeholder token in the textarea stays unresolved until the ack
#: lands, so "wait out the timeout" is not a survivable degradation. The bump
#: lets the server refuse up front (see :func:`supports_uploads`) with an error
#: the UI can explain. Revision 5.
MSG_UPLOAD_COMMAND = "upload_command"

#: server → daemon: read one file back out of the project's runtime
#: ``uploads/`` directory, so the web UI can render an attached image inline in
#: the conversation instead of showing only the project-relative path the agent
#: sees. The payload carries ``project_root``, the project-relative ``path``
#: (never an absolute one — the browser must not learn the daemon machine's
#: layout, and the daemon re-derives the real location itself) and a
#: ``request_id``. This is the exact inverse of :data:`MSG_UPLOAD_COMMAND` and
#: forced a ``PROTOCOL_VERSION`` bump to ``6`` for a sharper version of the same
#: reason: fetches back a *rendering* path, so they arrive many at a time and
#: repeat on every re-render, and a pre-fetch daemon that drops the frame would
#: leave each of them stalled until the dispatch timeout. Revision 6.
MSG_FETCH_COMMAND = "fetch_command"

#: server → daemon: report the number of browsers currently watching the web
#: UI. Sent as an *edge* only on the 0↔non-0 transitions (open the first page /
#: close the last one) — 1→2 or 2→1 changes are not broadcast, because the
#: daemon only cares whether *anyone* is watching, not how many. The same count
#: also rides on every :data:`MSG_PING` as a *level* field, which self-heals a
#: lost edge within one heartbeat interval. On ``count > 0`` the daemon resumes
#: its full-speed cadence (and fast-pushes fresh data immediately); on
#: ``count == 0`` it may drop to a low-power idle gear. Revision 4; a daemon
#: never downshifts unless the server advertises support (see
#: :func:`supports_presence`).
MSG_VIEWERS = "viewers"

#: daemon → server: acknowledge the result of a :data:`MSG_ISSUE_COMMAND`.
#: Carries ``request_id`` (echoed from the command) and either ``ok=true``
#: or ``ok=false`` with an ``error`` message.
MSG_ISSUE_RESULT = "issue_result"

#: daemon → server: acknowledge the result of a :data:`MSG_PROJECT_COMMAND`.
#: Carries ``request_id`` (echoed from the command) plus ``ok`` and, on success,
#: the normalized ``project_root`` that was actually registered / deregistered.
#: On failure it carries both a human ``error`` and a stable ``error_code`` —
#: the code is what the web UI maps to a localized message (the prose is only a
#: diagnostic fallback), which is why this reply is not just an ISSUE_RESULT
#: clone.
MSG_PROJECT_RESULT = "project_result"

#: daemon → server: acknowledge the result of a :data:`MSG_UPLOAD_COMMAND`.
#: Carries ``request_id`` (echoed from the command) plus ``ok`` and, on success,
#: the ``path`` the file landed at **relative to the project root** — never the
#: daemon machine's absolute path, both because the prompt consumes it with the
#: project root as the working directory and because the server must not leak a
#: remote machine's directory layout to the browser — along with ``size`` and
#: ``deduplicated`` (the content was already on disk, so nothing was written).
#: On failure it carries a human ``error`` plus a stable ``error_code`` from
#: :data:`UPLOAD_ERROR_CODES`, which is what the web UI maps to a localized
#: message. Revision 5.
MSG_UPLOAD_RESULT = "upload_result"

#: daemon → server: deliver the bytes requested by a :data:`MSG_FETCH_COMMAND`.
#: Carries ``request_id`` (echoed from the command) plus ``ok`` and, on success,
#: the base64-encoded ``content_b64`` (the wire is JSON lines and cannot carry
#: raw bytes), the decoded ``size`` and the file's ``name``. On failure it
#: carries a human ``error`` plus a stable :data:`FETCH_ERROR_CODES` member,
#: which is what the server maps to an HTTP status. Unlike an upload failure the
#: code never reaches a human as a message: the browser turns *any* fetch
#: failure into "keep showing the plain path", so the code exists for the
#: server's status mapping and for diagnosis, not for the UI. Revision 6.
MSG_FETCH_RESULT = "fetch_result"

#: server → daemon: pull the *full text* of a single issue or pending call on
#: demand. STATUS_UPDATE now carries only truncated summaries (issue
#: descriptions / call prompts clipped for wire economy); when the operator
#: opens a detail view the server routes this request to the owning daemon,
#: which reads the untruncated content and replies with :data:`MSG_DETAIL_DATA`.
#: Revision 3; only used when the daemon advertises support.
MSG_DETAIL_REQUEST = "detail_request"

# -- detail-request kinds -------------------------------------------------
# The ``kind`` field of a MSG_DETAIL_REQUEST / MSG_DETAIL_DATA payload names
# which on-demand full-text artifact is being fetched, so the daemon knows
# whether to read an issue record or a pending call file.
DETAIL_KIND_ISSUE = "issue"
DETAIL_KIND_CALL = "call"
#: Every recognised detail-request kind.
DETAIL_KINDS: FrozenSet[str] = frozenset({DETAIL_KIND_ISSUE, DETAIL_KIND_CALL})

# -- project-registry operations ------------------------------------------
# The ``operation`` field of a MSG_PROJECT_COMMAND payload. Only the two
# registry mutations exist: listing is served from the STATUS_UPDATE snapshot
# (``registered_projects``) rather than by a request/response round trip, so no
# ``list`` operation is needed here.
PROJECT_OP_ADD = "add"
PROJECT_OP_REMOVE = "remove"
#: Every recognised project-registry operation.
PROJECT_OPERATIONS: FrozenSet[str] = frozenset({PROJECT_OP_ADD, PROJECT_OP_REMOVE})

# -- upload failure codes (protocol revision 5) ---------------------------
# The ``error_code`` field of a failed MSG_UPLOAD_RESULT. These codes — not the
# accompanying ``error`` prose — are the contract: the server maps each to an
# HTTP status and the web UI maps each to a localized message, so the prose can
# change freely while the codes stay stable.
UPLOAD_ERR_INVALID_PATH = "invalid_path"
UPLOAD_ERR_NOT_REGISTERED = "not_registered"
UPLOAD_ERR_TOO_LARGE = "too_large"
UPLOAD_ERR_INVALID_FILENAME = "invalid_filename"
UPLOAD_ERR_INVALID_PAYLOAD = "invalid_payload"
UPLOAD_ERR_WRITE_FAILED = "write_failed"
UPLOAD_ERR_UNSUPPORTED = "unsupported"
#: Every recognised upload failure code.
UPLOAD_ERROR_CODES: FrozenSet[str] = frozenset(
    {
        UPLOAD_ERR_INVALID_PATH,
        UPLOAD_ERR_NOT_REGISTERED,
        UPLOAD_ERR_TOO_LARGE,
        UPLOAD_ERR_INVALID_FILENAME,
        UPLOAD_ERR_INVALID_PAYLOAD,
        UPLOAD_ERR_WRITE_FAILED,
        UPLOAD_ERR_UNSUPPORTED,
    }
)

# -- fetch failure codes (protocol revision 6) ----------------------------
# The ``error_code`` field of a failed MSG_FETCH_RESULT. As with the upload
# codes, these — not the accompanying prose — are the contract the server maps
# to an HTTP status. The set deliberately differs from UPLOAD_ERROR_CODES: a
# read has failure modes a write does not (``not_found``, ``read_failed``) and
# lacks the ones that only apply to a name the browser supplied
# (``invalid_filename``, ``invalid_payload``). Keeping them separate means
# neither channel can drift into accepting a code the other side never emits.
FETCH_ERR_INVALID_PATH = "invalid_path"
FETCH_ERR_NOT_REGISTERED = "not_registered"
FETCH_ERR_NOT_FOUND = "not_found"
FETCH_ERR_TOO_LARGE = "too_large"
FETCH_ERR_UNSUPPORTED = "unsupported"
FETCH_ERR_READ_FAILED = "read_failed"
#: Every recognised fetch failure code.
FETCH_ERROR_CODES: FrozenSet[str] = frozenset(
    {
        FETCH_ERR_INVALID_PATH,
        FETCH_ERR_NOT_REGISTERED,
        FETCH_ERR_NOT_FOUND,
        FETCH_ERR_TOO_LARGE,
        FETCH_ERR_UNSUPPORTED,
        FETCH_ERR_READ_FAILED,
    }
)

#: Valid values for the ``mode`` field of a :data:`MSG_HISTORY_DATA` payload.
HISTORY_MODE_FULL = "full"
HISTORY_MODE_APPEND = "append"
HISTORY_MODES: FrozenSet[str] = frozenset({HISTORY_MODE_FULL, HISTORY_MODE_APPEND})

# -- interaction-call kinds -----------------------------------------------
# Every human-in-the-loop interaction inside a running flow is carried by a
# single artifact: a JSON call file under ``<project>/tianluo/calls/``. Its
# ``kind`` field is one of the constants below, so the daemon aggregator and
# the web console can render and route each interaction without guessing.
# Legacy call files written before this field existed have no ``kind`` key
# and MUST be treated as :data:`CALL_KIND_CALL` for backward compatibility.
CALL_KIND_CALL = "call"
CALL_KIND_INTERJECTION = "interjection"
CALL_KIND_RETRY_DECISION = "retry_decision"
CALL_KIND_CLI_CONFIRM = "cli_confirm"
#: A non-interactive discovery confirmation gate: the flow has produced a
#: refined task description and is waiting for the user to confirm (reply with
#: the literal ``"1"``) before transitioning to ANALYZE. The call carries the
#: refined description in its prompt and a one-click confirm ``option`` whose
#: value is ``"1"`` so the web console can render both the ``输入 1 确认``
#: textual fallback and a GUI confirm button.
CALL_KIND_DISCOVERY_CONFIRM = "discovery_confirm"
#: A human review/approval gate for a completed step (plan / adjudicate / …):
#: the flow is PAUSED waiting for the operator to approve the reviewed step or
#: request changes. The call carries a human-readable ``prompt`` and, in its
#: ``context``, ``step_to_review_type`` / ``step_to_review_id`` (and, for an
#: ``adjudicate`` gate, the ruling's ``adjudication_rationale`` /
#: ``adjudicated_description`` / pre-ruling ``baseline``) so the web console can
#: render an Approve/Reject button pair plus the diff instead of forcing the
#: operator to guess a free-text answer. The structured reply travels back as
#: ``{"approved": bool, "feedback": ...}`` through the existing respond path.
CALL_KIND_CONFIRM = "confirm"
#: Every recognised interaction-call kind.
CALL_KINDS: FrozenSet[str] = frozenset(
    {
        CALL_KIND_CALL,
        CALL_KIND_INTERJECTION,
        CALL_KIND_RETRY_DECISION,
        CALL_KIND_CLI_CONFIRM,
        CALL_KIND_DISCOVERY_CONFIRM,
        CALL_KIND_CONFIRM,
    }
)

#: Messages a daemon is allowed to send to the server.
DAEMON_TO_SERVER: FrozenSet[str] = frozenset(
    {
        MSG_HELLO,
        MSG_STATUS_UPDATE,
        MSG_CALL_NOTIFICATION,
        MSG_PONG,
        MSG_HISTORY_INDEX,
        MSG_HISTORY_INDEX_DELTA,
        MSG_HISTORY_DATA,
        MSG_KEEPALIVE,
        MSG_DETAIL_DATA,
        MSG_ISSUE_RESULT,
        MSG_PROJECT_RESULT,
        MSG_SPAWN_FAILED,
        MSG_UPLOAD_RESULT,
        MSG_FETCH_RESULT,
    }
)
#: Messages a server is allowed to send to a daemon.
SERVER_TO_DAEMON: FrozenSet[str] = frozenset(
    {
        MSG_WELCOME,
        MSG_SPAWN_FLOW,
        MSG_RESPOND_CALL,
        MSG_PING,
        MSG_HISTORY_REQUEST,
        MSG_HISTORY_INDEX_REQUEST,
        MSG_INTERJECT_FLOW,
        MSG_ISSUE_COMMAND,
        MSG_PROJECT_COMMAND,
        MSG_DETAIL_REQUEST,
        MSG_END_SESSION,
        MSG_VIEWERS,
        MSG_UPLOAD_COMMAND,
        MSG_FETCH_COMMAND,
    }
)
#: Every known message type.
ALL_MESSAGE_TYPES: FrozenSet[str] = DAEMON_TO_SERVER | SERVER_TO_DAEMON


class ProtocolError(ValueError):
    """Raised when a frame cannot be parsed as a valid protocol message."""


@dataclass
class Message:
    """A single protocol frame.

    Attributes:
        type: One of the ``MSG_*`` constants.
        payload: Type-specific JSON-serializable object.
        seq: Per-connection sequence number assigned by the sender.
        timestamp: Unix epoch seconds at construction time.
    """

    type: str
    payload: Dict[str, Any] = field(default_factory=dict)
    seq: int = 0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        """Return the JSON-friendly dict form of this message."""
        return {
            "type": self.type,
            "seq": self.seq,
            "timestamp": self.timestamp,
            "payload": self.payload,
        }

    def to_json(self) -> str:
        """Serialize this message to a compact JSON string."""
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)

    @classmethod
    def from_dict(cls, data: Any) -> "Message":
        """Build a :class:`Message` from a decoded JSON object.

        Raises :class:`ProtocolError` when *data* is not a well-formed frame.
        """
        if not isinstance(data, dict):
            raise ProtocolError(f"protocol frame must be an object, got {type(data).__name__}")
        msg_type = data.get("type")
        if not isinstance(msg_type, str) or not msg_type:
            raise ProtocolError("protocol frame is missing a string 'type'")
        if msg_type not in ALL_MESSAGE_TYPES:
            raise ProtocolError(f"unknown message type: {msg_type!r}")
        payload = data.get("payload", {})
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise ProtocolError("protocol frame 'payload' must be an object")
        seq_raw = data.get("seq", 0)
        try:
            seq = int(seq_raw)
        except (TypeError, ValueError):
            seq = 0
        ts_raw = data.get("timestamp", time.time())
        try:
            timestamp = float(ts_raw)
        except (TypeError, ValueError):
            timestamp = time.time()
        return cls(type=msg_type, payload=payload, seq=seq, timestamp=timestamp)

    @classmethod
    def from_json(cls, raw: str) -> "Message":
        """Parse a JSON string into a :class:`Message`.

        Raises :class:`ProtocolError` on malformed JSON or an invalid frame.
        """
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise ProtocolError(f"invalid JSON frame: {exc}") from exc
        return cls.from_dict(data)


def encode(msg_type: str, payload: Dict[str, Any], *, seq: int = 0) -> str:
    """Build and JSON-encode a message of *msg_type* in one call."""
    return Message(type=msg_type, payload=dict(payload), seq=seq).to_json()


def decode(raw: str) -> Message:
    """Parse a JSON wire string into a validated :class:`Message`."""
    return Message.from_json(raw)


# -- typed payload constructors -------------------------------------------
# Thin helpers so call sites do not hand-roll payload dicts; the server and
# daemon both build messages exclusively through these.


def make_hello(
    machine_id: str, hostname: str, se3_version: str, key: str = ""
) -> Message:
    """daemon → server: announce a daemon and identify its machine.

    *key* is the daemon credential the multi-tenant server resolves to an owner
    (``key → owner_id``) so it can bind the reporting machine to a trust domain.
    It is **optional on the wire**: when *key* is empty the ``key`` field is
    omitted entirely, so a daemon running purely locally (or against a legacy
    single-tenant server) produces the exact same payload as before — the field
    is additive and an older server simply ignores it. A server that requires a
    key treats a HELLO without one as unauthenticated and answers
    ``WELCOME(accepted=false)``.

    The key is a secret credential: it lives only in memory and on the wire here
    and MUST NOT be logged. Callers logging a HELLO must never echo this field.
    """
    payload: Dict[str, Any] = {
        "machine_id": machine_id,
        "hostname": hostname,
        "se3_version": se3_version,
        "protocol_version": PROTOCOL_VERSION,
    }
    if key:
        payload["key"] = key
    return Message(type=MSG_HELLO, payload=payload)


def make_welcome(server_version: str, accepted: bool = True, reason: str = "") -> Message:
    """server → daemon: acknowledge a HELLO."""
    return Message(
        type=MSG_WELCOME,
        payload={
            "server_version": server_version,
            "protocol_version": PROTOCOL_VERSION,
            "accepted": accepted,
            "reason": reason,
        },
    )


def make_status_update(snapshot: Dict[str, Any], *, seq: int = 0) -> Message:
    """daemon → server: report an aggregated machine-status snapshot."""
    return Message(type=MSG_STATUS_UPDATE, payload={"snapshot": snapshot}, seq=seq)


def make_call_notification(call: Dict[str, Any]) -> Message:
    """daemon → server: notify of a freshly-detected pending human call."""
    return Message(type=MSG_CALL_NOTIFICATION, payload={"call": call})


def make_spawn_flow(
    task_description: str,
    *,
    project_root: str = "",
    task_type: str = "feature",
    discover: bool = False,
    worktree: bool = False,
    resume_flow_id: str = "",
    from_issue_id: str = "",
    implementation_strategy: str = "",
) -> Message:
    """server → daemon: instruct a daemon to spawn a new ``luo run`` flow.

    When *discover* is true the daemon's spawner appends ``--discover`` so the
    flow starts from the discovery step (see the spawner command assembly).

    When *worktree* is true the daemon's spawner appends ``--worktree`` so the
    flow runs in an isolated worktree and auto-merges back on success. The key
    is omitted from the wire when false, so a plain (non-isolated) fresh-spawn
    payload stays byte-for-byte backward compatible and ``PROTOCOL_VERSION`` is
    not bumped.

    When *resume_flow_id* is non-empty, the daemon resumes the named flow
    (``luo run --resume --flow-id <id>``) instead of starting a fresh one.
    The ``task_description`` is ignored in this case — the flow's own
    persisted state supplies the task. ``implementation_strategy`` is likewise
    never sent on a resume: the persisted flow strategy is authoritative and
    must not be re-decided.

    When *from_issue_id* is non-empty, the daemon spawns the flow from an
    existing issue (``luo run --from-issue <id>``); the issue's description
    becomes the task and the request's ``task_description`` is ignored. It may
    be combined with *discover* (the daemon then also appends ``--discover``).
    Like *resume_flow_id*, the field is omitted from the wire when empty, so a
    plain fresh-spawn payload stays byte-for-byte backward compatible and the
    ``PROTOCOL_VERSION`` is not bumped.

    When *implementation_strategy* is non-empty (revision 7), the daemon's
    spawner appends ``--implementation-strategy <value>`` so the flow's
    explicit web choice overrides the project configuration.  The value must
    be a :data:`SPAWN_STRATEGY_VALUES` member; the server must check
    :func:`supports_spawn_strategy` against the daemon's advertised protocol
    version BEFORE sending a frame that carries it — a pre-7 daemon would
    silently ignore the field and run a different strategy than requested.
    """
    payload: Dict[str, Any] = {
        "task_description": task_description,
        "project_root": project_root,
        "task_type": task_type,
        "discover": bool(discover),
    }
    if worktree:
        payload["worktree"] = True
    if resume_flow_id:
        payload["resume_flow_id"] = resume_flow_id
    if from_issue_id:
        payload["from_issue_id"] = from_issue_id
    if implementation_strategy:
        strategy = str(implementation_strategy).strip()
        if strategy not in SPAWN_STRATEGY_VALUES:
            raise ProtocolError(
                f"spawn implementation_strategy must be one of "
                f"{sorted(SPAWN_STRATEGY_VALUES)}, got {implementation_strategy!r}"
            )
        payload["implementation_strategy"] = strategy
    return Message(type=MSG_SPAWN_FLOW, payload=payload)


def make_spawn_failed(
    project_root: str,
    error: str,
    *,
    task_description: str = "",
    from_issue_id: str = "",
    resume_flow_id: str = "",
) -> Message:
    """daemon → server: report a failed spawn / resume / project-init.

    Sent when a server-dispatched :data:`MSG_SPAWN_FLOW` could not be carried
    out *after* the server already answered ``202 dispatched`` — e.g. the
    ``ensure_se3_project`` init failed, the fresh ``luo run`` could not be
    launched, or a resume could not be started. *project_root* and *error*
    locate and explain the failure; the optional *task_description* /
    *from_issue_id* / *resume_flow_id* echo the originating request so the
    server / web UI can correlate the failure with the task the user just
    published instead of leaving it stuck on the "published" state.

    Empty optional fields are omitted from the wire so the payload stays
    compact; ``project_root`` and ``error`` are always present.
    """
    payload: Dict[str, Any] = {
        "project_root": project_root,
        "error": error,
    }
    if task_description:
        payload["task_description"] = task_description
    if from_issue_id:
        payload["from_issue_id"] = from_issue_id
    if resume_flow_id:
        payload["resume_flow_id"] = resume_flow_id
    return Message(type=MSG_SPAWN_FAILED, payload=payload)


def make_respond_call(
    call_id: str,
    response: Any,
    *,
    project_root: str = "",
) -> Message:
    """server → daemon: deliver a human response for a pending call."""
    return Message(
        type=MSG_RESPOND_CALL,
        payload={
            "call_id": call_id,
            "project_root": project_root,
            "response": response,
        },
    )


def make_interject_flow(
    flow_id: str,
    text: str,
    *,
    project_root: str = "",
) -> Message:
    """server → daemon: deliver a mid-flow user interjection for a running flow.

    *text* is the user-typed instruction to fold into the running flow (the
    same content a local operator would type at the Ctrl-C interjection
    prompt). The daemon writes *text* as an ``interjection``-kind call file
    under the flow's ``tianluo/calls/`` directory; the running ``luo run`` process
    drains it at the next step boundary and folds it into ``user_interjections``.
    """
    return Message(
        type=MSG_INTERJECT_FLOW,
        payload={
            "flow_id": flow_id,
            "text": text,
            "project_root": project_root,
        },
    )


def make_end_session(
    flow_id: str,
    *,
    project_root: str = "",
    reason: str = "user terminated",
) -> Message:
    """server → daemon: end (terminate + archive) the session *flow_id*.

    The daemon locates *flow_id* among its supervised flows, then off-loads the
    actual work to an ``luo end-session`` subprocess: it gracefully terminates
    the live ``luo run`` process and, for a worktree session, archives it the
    way a normally-completed session is cleaned up (``tianluo/worktrees/.archive``
    + a promoted main-repo ``engine_<flow_id>.json`` + history sync + branch /
    worktree-metadata removal). The work is never done on the event loop.

    *project_root* is the main project root the daemon should pass through to
    the subprocess; when empty the daemon reverse-resolves it from its history
    index (mirroring the INTERJECT path). *reason* is free-form prose recorded
    for diagnostics.

    Empty optional fields are omitted from the wire so a payload carrying only a
    ``flow_id`` stays compact, and an older daemon that does not recognise the
    type simply ignores the frame — so no ``PROTOCOL_VERSION`` bump is needed.
    """
    payload: Dict[str, Any] = {"flow_id": flow_id}
    if project_root:
        payload["project_root"] = project_root
    if reason:
        payload["reason"] = reason
    return Message(type=MSG_END_SESSION, payload=payload)


def make_issue_command(
    operation: str,
    project_root: str,
    *,
    issue_id: str = "",
    description: Optional[str] = None,
    title: Optional[str] = None,
    priority: Optional[str] = None,
    type: Optional[str] = None,
    tags: Optional[List[str]] = None,
    reason: str = "",
    request_id: str = "",
) -> Message:
    """server → daemon: execute an issue write operation.

    *operation* is one of ``"create"``, ``"edit"``, ``"close"``, ``"reopen"``.
    ``project_root`` is required and must be an absolute path to a registered
    SE3 project.  The remaining fields are operation-specific:

    * ``create``: *description* is required; *title*, *priority*, *type*,
      *tags* are optional.
    * ``edit``: *issue_id* is required; *title*, *description*, *priority*,
      *type*, *tags* are optional.  ``None`` means "do not change";
      an empty string means "clear the field".
    * ``close``: *issue_id* is required; *reason* is optional.
    * ``reopen``: *issue_id* is required.

    When *request_id* is supplied the daemon will echo it back in its
    :data:`MSG_ISSUE_RESULT` reply so the server can correlate the response.
    """
    payload: Dict[str, Any] = {
        "operation": operation,
        "project_root": project_root,
    }
    if issue_id:
        payload["issue_id"] = issue_id
    if description is not None:
        payload["description"] = description
    if title is not None:
        payload["title"] = title
    if priority is not None:
        payload["priority"] = priority
    if type is not None:
        payload["type"] = type
    if tags is not None:
        payload["tags"] = list(tags)
    if reason:
        payload["reason"] = reason
    if request_id:
        payload["request_id"] = request_id
    return Message(type=MSG_ISSUE_COMMAND, payload=payload)


def make_issue_result(
    request_id: str,
    *,
    ok: bool = True,
    error: str = "",
    issue_id: str = "",
) -> Message:
    """daemon → server: acknowledge the result of an issue write command.

    *request_id* echoes the ``request_id`` from the originating
    :data:`MSG_ISSUE_COMMAND` so the server can correlate.  When *ok* is
    ``False`` the *error* string describes what went wrong.
    """
    payload: Dict[str, Any] = {
        "request_id": request_id,
        "ok": ok,
    }
    if error:
        payload["error"] = error
    if issue_id:
        payload["issue_id"] = issue_id
    return Message(type=MSG_ISSUE_RESULT, payload=payload)


def make_project_command(
    operation: str,
    project_root: str,
    *,
    request_id: str = "",
) -> Message:
    """server → daemon: register or deregister a project root.

    *operation* is :data:`PROJECT_OP_ADD` or :data:`PROJECT_OP_REMOVE`;
    *project_root* is the absolute path on the daemon's machine. The daemon
    revalidates the path itself — the server cannot see the daemon's filesystem,
    so its own check is only a cheap early reject.

    When *request_id* is supplied the daemon echoes it in its
    :data:`MSG_PROJECT_RESULT` reply so the server can wake the waiting REST
    request.

    Raises :class:`ProtocolError` when *operation* is not a recognised
    :data:`PROJECT_OPERATIONS` value.
    """
    if operation not in PROJECT_OPERATIONS:
        raise ProtocolError(
            f"project operation must be one of {sorted(PROJECT_OPERATIONS)}, "
            f"got {operation!r}"
        )
    payload: Dict[str, Any] = {
        "operation": operation,
        "project_root": project_root,
    }
    if request_id:
        payload["request_id"] = request_id
    return Message(type=MSG_PROJECT_COMMAND, payload=payload)


def make_project_result(
    request_id: str,
    *,
    ok: bool = True,
    error: str = "",
    error_code: str = "",
    project_root: str = "",
) -> Message:
    """daemon → server: acknowledge the result of a project-registry command.

    *request_id* echoes the originating :data:`MSG_PROJECT_COMMAND`. On success
    *project_root* is the **normalized** path actually registered / deregistered
    (worktree-folded and realpath'd), which may differ from the one requested —
    the web UI shows what really landed, not what was typed.

    On failure *error_code* is a stable machine-readable reason
    (``invalid_path`` / ``not_found`` / ``not_a_directory`` / ``live_flow`` /
    ``not_registered`` / ``unsupported``) that the server maps to an HTTP status
    and the web UI maps to a localized message; *error* is the untranslated
    diagnostic prose kept only as a fallback.
    """
    payload: Dict[str, Any] = {
        "request_id": request_id,
        "ok": ok,
    }
    if error:
        payload["error"] = error
    if error_code:
        payload["error_code"] = error_code
    if project_root:
        payload["project_root"] = project_root
    return Message(type=MSG_PROJECT_RESULT, payload=payload)


# -- upload messages (protocol revision 5) --------------------------------


def make_upload_command(
    project_root: str,
    filename: str,
    content_b64: str,
    *,
    size: int,
    request_id: str = "",
) -> Message:
    """server → daemon: store one attached file under the project's uploads dir.

    *project_root* is the absolute path on the daemon's machine; the daemon
    re-validates it against its own registry, so this check is only a cheap
    early reject. *filename* is the browser-supplied original name — carried
    verbatim so the stored file keeps a name a human and an agent can read; the
    daemon sanitizes it before touching the filesystem. *content_b64* is the
    base64 encoding of the file bytes, required because the wire is JSON lines
    and cannot carry raw bytes. *size* is the declared *decoded* length, so both
    ends can reject an oversized upload without materializing it. *request_id*
    correlates the :data:`MSG_UPLOAD_RESULT` reply back to the waiting REST
    request.

    Raises :class:`ProtocolError` when *filename* is empty, *project_root* is
    not absolute, or *size* exceeds :data:`MAX_UPLOAD_BYTES` — the three
    conditions no downstream layer can recover from, caught here so a malformed
    frame is never put on the wire in the first place.
    """
    if not filename or not str(filename).strip():
        raise ProtocolError("upload command requires a non-empty filename")
    if not project_root or not os.path.isabs(str(project_root)):
        raise ProtocolError(
            f"upload project_root must be an absolute path, got {project_root!r}"
        )
    try:
        declared_size = int(size)
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"upload size must be an integer, got {size!r}") from exc
    if declared_size < 0:
        raise ProtocolError(f"upload size must not be negative, got {declared_size}")
    if declared_size > MAX_UPLOAD_BYTES:
        raise ProtocolError(
            f"upload size {declared_size} exceeds the {MAX_UPLOAD_BYTES}-byte limit"
        )
    payload: Dict[str, Any] = {
        "project_root": str(project_root),
        "filename": str(filename),
        "content_b64": content_b64,
        "size": declared_size,
    }
    if request_id:
        payload["request_id"] = request_id
    return Message(type=MSG_UPLOAD_COMMAND, payload=payload)


def make_upload_result(
    request_id: str,
    *,
    ok: bool = True,
    path: str = "",
    error: str = "",
    error_code: str = "",
    size: int = 0,
    deduplicated: bool = False,
) -> Message:
    """daemon → server: acknowledge the result of an upload command.

    *request_id* echoes the originating :data:`MSG_UPLOAD_COMMAND`. On success
    *path* is the stored file's path **relative to the project root** (posix
    separators) — that is exactly the string the operator's prompt will carry,
    and it keeps the daemon machine's absolute layout off the wire. *size* and
    *deduplicated* are always emitted on success even at their zero values: a
    0-byte file and "the content was already on disk" are real answers, not
    absences, and the server distinguishes them by key presence.

    On failure *error_code* is a stable :data:`UPLOAD_ERROR_CODES` member that
    the server maps to an HTTP status and the web UI maps to a localized
    message; *error* is untranslated diagnostic prose kept only as a fallback.

    Raises :class:`ProtocolError` when *error_code* is not a recognised
    :data:`UPLOAD_ERROR_CODES` value.
    """
    if error_code and error_code not in UPLOAD_ERROR_CODES:
        raise ProtocolError(
            f"upload error_code must be one of {sorted(UPLOAD_ERROR_CODES)}, "
            f"got {error_code!r}"
        )
    payload: Dict[str, Any] = {
        "request_id": request_id,
        "ok": bool(ok),
    }
    if ok:
        payload["path"] = path
        payload["size"] = int(size)
        payload["deduplicated"] = bool(deduplicated)
    else:
        if error:
            payload["error"] = error
        if error_code:
            payload["error_code"] = error_code
    return Message(type=MSG_UPLOAD_RESULT, payload=payload)


# -- fetch messages (protocol revision 6) ---------------------------------


def make_fetch_command(
    project_root: str,
    path: str,
    *,
    request_id: str = "",
) -> Message:
    """server → daemon: read one file back out of the project's uploads dir.

    *project_root* is the absolute path on the daemon's machine; as with
    :func:`make_upload_command` the daemon re-validates it against its own
    registry, so the check here is only a cheap early reject. *path* is the
    file's location **relative to that project root** — the same string
    :func:`make_upload_result` handed back and the same one the operator's
    prompt carries — never an absolute path, both because the browser must not
    learn the daemon machine's layout and because a fetch may only ever reach
    inside the project. *request_id* correlates the :data:`MSG_FETCH_RESULT`
    reply back to the waiting REST request.

    Raises :class:`ProtocolError` when *project_root* is not absolute, or when
    *path* is empty, absolute, or contains a ``..`` segment. The traversal check
    is a *cheap early* one, not the security boundary: the daemon's own
    containment check — resolving the path and requiring its parent to be the
    uploads directory — is what actually holds, because only a resolved path
    catches symlinks and normalization tricks a string scan cannot see. This one
    exists so an obviously malformed frame never reaches the wire at all.
    """
    if not project_root or not os.path.isabs(str(project_root)):
        raise ProtocolError(
            f"fetch project_root must be an absolute path, got {project_root!r}"
        )
    if not path or not str(path).strip():
        raise ProtocolError("fetch command requires a non-empty path")
    rel_path = str(path).strip()
    if os.path.isabs(rel_path) or rel_path.startswith("/") or rel_path.startswith("\\"):
        raise ProtocolError(
            f"fetch path must be relative to the project root, got {path!r}"
        )
    if ".." in rel_path.replace("\\", "/").split("/"):
        raise ProtocolError(f"fetch path must not contain a '..' segment, got {path!r}")
    payload: Dict[str, Any] = {
        "project_root": str(project_root),
        "path": rel_path,
    }
    if request_id:
        payload["request_id"] = request_id
    return Message(type=MSG_FETCH_COMMAND, payload=payload)


def make_fetch_result(
    request_id: str,
    *,
    ok: bool = True,
    content_b64: str = "",
    size: int = 0,
    name: str = "",
    error: str = "",
    error_code: str = "",
) -> Message:
    """daemon → server: deliver the bytes requested by a fetch command.

    *request_id* echoes the originating :data:`MSG_FETCH_COMMAND`. On success
    *content_b64* is the base64 encoding of the file bytes (the wire is JSON
    lines and cannot carry raw bytes), *size* the decoded length and *name* the
    stored file's basename. *size* is always emitted on success even at ``0``:
    an empty file is a real answer, not an absence, and the server tells the two
    apart by key presence — the same contract :func:`make_upload_result` keeps.

    On failure *error_code* is a stable :data:`FETCH_ERROR_CODES` member the
    server maps to an HTTP status; *error* is untranslated diagnostic prose.

    Raises :class:`ProtocolError` when *size* is negative or exceeds
    :data:`MAX_UPLOAD_BYTES` (the read-back leg shares the upload channel's
    ceiling — the same base64 blow-up has to fit the same frame cap), or when
    *error_code* is not a recognised :data:`FETCH_ERROR_CODES` value.
    """
    if error_code and error_code not in FETCH_ERROR_CODES:
        raise ProtocolError(
            f"fetch error_code must be one of {sorted(FETCH_ERROR_CODES)}, "
            f"got {error_code!r}"
        )
    try:
        declared_size = int(size)
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"fetch size must be an integer, got {size!r}") from exc
    if declared_size < 0:
        raise ProtocolError(f"fetch size must not be negative, got {declared_size}")
    if declared_size > MAX_UPLOAD_BYTES:
        raise ProtocolError(
            f"fetch size {declared_size} exceeds the {MAX_UPLOAD_BYTES}-byte limit"
        )
    payload: Dict[str, Any] = {
        "request_id": request_id,
        "ok": bool(ok),
    }
    if ok:
        payload["content_b64"] = content_b64
        payload["size"] = declared_size
        payload["name"] = name
    else:
        if error:
            payload["error"] = error
        if error_code:
            payload["error_code"] = error_code
    return Message(type=MSG_FETCH_RESULT, payload=payload)


def make_ping(*, seq: int = 0, viewers: Optional[int] = None) -> Message:
    """server → daemon: heartbeat probe.

    *viewers* optionally piggybacks the current browser-presence count
    (revision 4) on the heartbeat as the *level* half of the presence scheme:
    the 0↔non-0 :data:`MSG_VIEWERS` edges carry the transition instantly, and
    this per-PING level repairs any edge lost across a disconnect / dropped
    frame within one heartbeat interval — at zero extra frames. ``None`` omits
    the field entirely, keeping the payload byte-identical to the revision-3
    PING for older callers and peers.
    """
    payload: Dict[str, Any] = {}
    if viewers is not None:
        payload["viewers"] = int(viewers)
    return Message(type=MSG_PING, payload=payload, seq=seq)


def make_pong(*, seq: int = 0) -> Message:
    """daemon → server: heartbeat reply."""
    return Message(type=MSG_PONG, payload={}, seq=seq)


# -- presence messages (protocol revision 4) -------------------------------


def make_viewers(count: int, *, seq: int = 0) -> Message:
    """server → daemon: report the browser-presence count (edge message).

    Broadcast to every connected daemon only when the web UI's connection
    count crosses the 0↔non-0 boundary — that single bit is all the daemon's
    gear selection needs, so intermediate 1→2 / 2→1 changes cost no frames.
    The daemon shifts to its full-speed cadence (with an immediate fast push)
    on ``count > 0`` and may enter the low-power idle gear on ``count == 0``.
    The lost-edge case is repaired by the ``viewers`` level riding on every
    :data:`MSG_PING` (see :func:`make_ping`). Revision 4.
    """
    return Message(type=MSG_VIEWERS, payload={"count": int(count)}, seq=seq)


# -- history messages (protocol revision 2) -------------------------------
# These carry the per-machine `luo history` records to the central server so
# the web UI can list and inspect historical sessions. The server is only an
# in-memory relay/cache — it does not persist history to disk.


def make_history_index(sessions: Any, *, seq: int = 0) -> Message:
    """daemon → server: report the index of known history sessions.

    *sessions* is a list of session-meta dicts (flow id, task description,
    status, timestamps, active flag, …) — one per ``luo history`` entry the
    daemon can serve. Sent on connect and whenever the index changes.
    """
    return Message(
        type=MSG_HISTORY_INDEX,
        payload={"sessions": list(sessions)},
        seq=seq,
    )


def make_history_index_request(*, seq: int = 0) -> Message:
    """server → daemon: force a fresh rebuild + re-push of the history index.

    Carries no payload — it has no flow dimension and merely instructs the
    daemon to rebuild its index from disk and send a :data:`MSG_HISTORY_INDEX`
    immediately, even if the index has not changed since the last push (it
    bypasses the daemon's change-debounce via ``force_index``). The web
    ``GET /api/history`` broadcasts this to every connected daemon so the
    history list always reflects the latest sessions.
    """
    return Message(type=MSG_HISTORY_INDEX_REQUEST, payload={}, seq=seq)


def make_history_request(
    flow_id: str,
    *,
    project_root: str = "",
    cursor: Dict[str, Any] | None = None,
    seq: int = 0,
) -> Message:
    """server → daemon: pull the history records for *flow_id* on demand.

    *cursor* is an optional per-step file-cursor dict ``{step_id: position}``;
    when supplied the daemon may answer with an incremental ``append`` rather
    than a ``full`` snapshot. ``None`` requests a full snapshot.
    """
    return Message(
        type=MSG_HISTORY_REQUEST,
        payload={
            "flow_id": flow_id,
            "project_root": project_root,
            "cursor": dict(cursor) if cursor else {},
        },
        seq=seq,
    )


def make_history_data(
    flow_id: str,
    mode: str,
    records: Any,
    *,
    cursor: Dict[str, Any] | None = None,
    cursor_base: Dict[str, Any] | None = None,
    usage: Optional[Dict[str, Any]] = None,
    usage_catalog: Optional[Dict[str, Any]] = None,
    seq: int = 0,
) -> Message:
    """daemon → server: deliver history records for *flow_id*.

    *mode* is :data:`HISTORY_MODE_FULL` (a complete snapshot) or
    :data:`HISTORY_MODE_APPEND` (records newer than the requester's cursor).
    *records* is the list of history record dicts. *cursor* is the updated
    per-step file-cursor dict the recipient should send back on its next
    request to continue incrementally.

    *cursor_base* is the per-file line index the read STARTED at, so the frame
    states the window ``[cursor_base, cursor)`` it covers instead of leaving the
    receiver to guess it from the record count. WHY: the cursor counts every
    physical line, while blank / unparseable lines yield no record, so a
    count-derived start line is wrong for any delta that skipped one — and the
    server's gap check would reject a contiguous frame as a hole. It is OPTIONAL
    on the wire: a version-skewed daemon omits it and the receiver falls back to
    its count-derived estimate.

    *usage* (revision 7, optional) is the flow's structured usage/cost payload
    (:func:`~tianluo.usage.build_usage_payload`), computed by the daemon with
    the same backend the CLI and server use — the server relays it verbatim
    and never re-prices it. It rides *full* snapshots (the whole-flow view);
    *append* deltas omit it, and the server re-aggregates from its cached
    records when no full snapshot has landed since connect. Omitted from the
    wire when ``None`` so pre-7 peers never see the key.

    *usage_catalog* (additive, optional) is the serialized pricing catalog
    (:class:`~tianluo.pricing.PricingCatalog` as dict) that priced *usage* —
    the project's ``pricing.models`` overrides merged onto the built-in
    table. It rides ANY frame whose records carry usage (full or append), so
    the server's append-time re-aggregation prices the SAME cached records
    with the SAME table instead of degrading to a catalog-less rebuild. The
    server cannot reach the project's ``tianluo.yaml`` itself (it lives on the
    owning machine), which is why the catalog must ride the wire. Purely
    additive like *usage*: omitted when ``None``, ignored by pre-7 peers.

    Raises :class:`ProtocolError` when *mode* is not a recognized value.
    """
    if mode not in HISTORY_MODES:
        raise ProtocolError(
            f"history data mode must be one of {sorted(HISTORY_MODES)}, got {mode!r}"
        )
    payload: Dict[str, Any] = {
        "flow_id": flow_id,
        "mode": mode,
        "records": list(records),
        "cursor": dict(cursor) if cursor else {},
        "cursor_base": dict(cursor_base) if cursor_base else {},
    }
    if usage is not None:
        payload["usage"] = dict(usage)
    if usage_catalog is not None:
        payload["usage_catalog"] = dict(usage_catalog)
    return Message(type=MSG_HISTORY_DATA, payload=payload, seq=seq)


# -- traffic-reduction messages (protocol revision 3) ---------------------
# These replace, in the steady state, the periodic *full* STATUS_UPDATE and
# HISTORY_INDEX frames with change-driven / incremental ones, so an idle daemon
# costs a keepalive rather than a ~573 KB snapshot every 5 s, and an active flow
# costs only the meta row that changed rather than the whole index. Only emitted
# to a peer that advertises protocol_version >= 3 (see supports_traffic_reduction).


def make_keepalive(signature: str = "", *, seq: int = 0) -> Message:
    """daemon → server: a minimal heartbeat sent when the status snapshot is
    unchanged.

    Emitted in place of a periodic :data:`MSG_STATUS_UPDATE` when the aggregated
    snapshot's content *signature* matches the last one pushed: nothing changed,
    so re-sending the (potentially large) snapshot is pure waste, but the server
    still needs a liveness signal to keep its offline-detection timer from
    tripping. The server treats a keepalive exactly like a STATUS_UPDATE for the
    purpose of the daemon's last-seen time and does **not** re-broadcast state to
    browsers. *signature* is the same content hash the daemon gates on, carried
    so the server can confirm both ends agree on "nothing changed".
    """
    return Message(type=MSG_KEEPALIVE, payload={"signature": signature}, seq=seq)


def make_history_index_delta(
    upserts: Any = (),
    removed: Any = (),
    *,
    seq: int = 0,
) -> Message:
    """daemon → server: an incremental history-index update.

    *upserts* is a list of SessionMeta dicts (each carrying a ``flow_id``) that
    were added or changed since the last index push; the server upserts them
    into its in-memory full index keyed by ``flow_id``. *removed* is a list of
    ``flow_id`` strings whose sessions disappeared and should be dropped. Sent
    instead of a whole :data:`MSG_HISTORY_INDEX` for the common case where only a
    few active flows' metas changed, so index traffic scales with the number of
    *changed* flows rather than the total flow count. The full index is still
    sent on connect / reconnect / HISTORY_INDEX_REQUEST as the baseline both
    sides reconcile against.
    """
    return Message(
        type=MSG_HISTORY_INDEX_DELTA,
        payload={
            "upserts": list(upserts),
            "removed": list(removed),
        },
        seq=seq,
    )


def make_detail_request(
    kind: str,
    target_id: str,
    *,
    project_root: str = "",
    request_id: str = "",
    seq: int = 0,
) -> Message:
    """server → daemon: fetch the full text of one issue or pending call.

    *kind* is :data:`DETAIL_KIND_ISSUE` or :data:`DETAIL_KIND_CALL`; *target_id*
    is the issue id or call id whose untruncated content is wanted (STATUS_UPDATE
    now carries only clipped summaries). *project_root* scopes the lookup to a
    specific SE3 project when the server knows it. *request_id* correlates the
    eventual :data:`MSG_DETAIL_DATA` reply back to the waiting REST request.

    Raises :class:`ProtocolError` when *kind* is not a recognised detail kind.
    """
    if kind not in DETAIL_KINDS:
        raise ProtocolError(
            f"detail kind must be one of {sorted(DETAIL_KINDS)}, got {kind!r}"
        )
    payload: Dict[str, Any] = {
        "kind": kind,
        "target_id": target_id,
    }
    if project_root:
        payload["project_root"] = project_root
    if request_id:
        payload["request_id"] = request_id
    return Message(type=MSG_DETAIL_REQUEST, payload=payload, seq=seq)


def make_detail_data(
    request_id: str,
    kind: str,
    *,
    detail: Optional[Dict[str, Any]] = None,
    ok: bool = True,
    error: str = "",
    seq: int = 0,
) -> Message:
    """daemon → server: deliver the full text for a :data:`MSG_DETAIL_REQUEST`.

    *request_id* echoes the request so the server can wake the correct waiter;
    *kind* echoes the requested :data:`DETAIL_KINDS` value. On success *detail*
    is the full-text record (e.g. the issue with its untruncated description, or
    the call with its full prompt). When *ok* is ``False`` *error* explains why
    the lookup failed (missing id, unreadable file, …) and *detail* is omitted.

    Raises :class:`ProtocolError` when *kind* is not a recognised detail kind.
    """
    if kind not in DETAIL_KINDS:
        raise ProtocolError(
            f"detail kind must be one of {sorted(DETAIL_KINDS)}, got {kind!r}"
        )
    payload: Dict[str, Any] = {
        "request_id": request_id,
        "kind": kind,
        "ok": ok,
    }
    if detail is not None:
        payload["detail"] = dict(detail)
    if error:
        payload["error"] = error
    return Message(type=MSG_DETAIL_DATA, payload=payload, seq=seq)
