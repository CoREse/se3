# tianluo Daemon & Central Server

tianluo's core (`luo run`, `luo sync`, …) is a one-shot CLI: each command runs in
the foreground and exits. The **daemon** and the **central server** add an
optional, always-on control plane on top of that core:

- **`luo daemon`** — a resident process on each machine. It discovers and
  supervises this machine's `luo run` flows, can spawn new flows on behalf of a
  remote caller, aggregates the on-disk state of every flow into a single
  snapshot, and (optionally) maintains an outbound connection to a central
  server.
- **`tianluo-server`** — a standalone central server that accepts connections from
  any number of daemons, merges their snapshots into a multi-machine /
  multi-flow view, exposes a REST API, and serves a bundled web frontend.

Both are entirely optional. A plain `pip install tianluo` and `luo run` need
neither.

## Table of Contents

1. [Quick Start](#quick-start)
   - [Installation](#installation)
   - [The `luo daemon` command](#the-luo-daemon-command)
   - [The `tianluo-server` command](#the-tianluo-server-command)
   - [A typical session](#a-typical-session)
2. [Deployment & Operations](#deployment--operations)
   - [The outbound connection model](#the-outbound-connection-model)
   - [Foreground vs. background (detached) mode](#foreground-vs-background-detached-mode)
   - [Runtime files: pidfile and status file](#runtime-files-pidfile-and-status-file)
   - [Flow discovery and supervision](#flow-discovery-and-supervision)
3. [Architecture & How It Works](#architecture--how-it-works)
   - [Inside the daemon](#inside-the-daemon)
   - [Inside the central server](#inside-the-central-server)
   - [End-to-end: publishing a task from a remote machine](#end-to-end-publishing-a-task-from-a-remote-machine)
   - [The file-upload channel](#the-file-upload-channel)
4. [Authentication & Multi-Tenant Access](#authentication--multi-tenant-access)
   - [Why authentication is mandatory](#why-authentication-is-mandatory)
   - [The persistence layer (`~/.se3/server.db`)](#the-persistence-layer-se3serverdb)
   - [Bootstrapping the first admin (`bootstrap-token`)](#bootstrapping-the-first-admin-bootstrap-token)
   - [Logging in and creating users](#logging-in-and-creating-users)
   - [Issuing daemon keys and binding machines](#issuing-daemon-keys-and-binding-machines)
   - [Deploying behind a TLS reverse proxy (wss)](#deploying-behind-a-tls-reverse-proxy-wss)
   - [Owner isolation](#owner-isolation)
5. [The Web Frontend](#the-web-frontend)

---

## Quick Start

### Installation

The daemon ships with the core `tianluo` package — `luo daemon` works out of the
box after a normal install. The **central server** pulls in heavier web
dependencies (FastAPI, uvicorn, websockets), so those are kept in an optional
`server` extra:

```bash
# Core install — includes the `luo daemon` command.
pip install tianluo

# Add the central server (`tianluo-server`) and the daemon's WebSocket client.
pip install 'tianluo[server]'
```

The `server` extra is also what the daemon's *outbound client* uses to dial a
server. A daemon started without `--server-url` runs purely locally and never
touches the extra; a daemon started with `--server-url` but without the extra
installed logs an install hint and degrades to local-only operation rather
than crashing.

### The `luo daemon` command

`luo daemon` is a subcommand group of the core CLI (not a separate binary):

```bash
luo daemon start                          # Start the daemon (detached background process)
luo daemon start --foreground             # Run the daemon in the current terminal
luo daemon start --server-url ws://host   # Start and dial out to a central server
luo daemon stop                           # Stop the running daemon
luo daemon status                         # Show running state and tracked flows
luo daemon status --json                  # Emit the status as JSON
```

| Subcommand | Options | Behavior |
|------------|---------|----------|
| `start` | `--server-url <url>`, `--daemon-key <key>`, `--foreground` | Starts the daemon. By default it is launched as a **detached background process**; `--foreground` runs it in the current terminal instead. `--server-url` records the central-server URL the daemon dials out to — a port may be given explicitly (`ws://host:9000`, `wss://host:8443`), and when omitted it is completed **per the scheme**: `wss://` (and `https://`) default to **443**, `ws://` (and `http://`) default to **8080** (the `tianluo-server` plaintext default). So a bare `wss://host` dials `:443`, not `:8080` — see [Port handling](#the-outbound-connection-model). `--daemon-key` records the secret the daemon presents in HELLO so a multi-tenant server binds the machine to an owner. If a daemon is already running, the command reports it and exits non-zero. |
| `stop` | — | Stops the running daemon (sends `SIGTERM` and waits for it to exit). Reports `not running` and exits `0` when none is up; reports a stop timeout with a non-zero exit if the process does not exit within the grace period. |
| `status` | `--json`, `-j` | Reports whether the daemon is running, its pid, machine id, configured server URL, the **real outbound-connection state** (see below), and the list of tracked flows. `--json` emits the same information as JSON instead of a rendered panel. |

### The `tianluo-server` command

The central server is started through its own `console_scripts` entry point,
`tianluo-server` (installed by the `tianluo[server]` extra):

```bash
tianluo-server                                # Bind to 127.0.0.1:8080 (defaults)
tianluo-server --host 0.0.0.0 --port 9000     # Listen on all interfaces, port 9000
tianluo-server --db-path /var/lib/tianluo.db      # Override the sqlite store location
tianluo-server --log-level debug              # Raise the uvicorn log level
tianluo-server bootstrap-token                # Mint a one-time break-glass admin token
```

| Option / Subcommand | Default | Description |
|---------------------|---------|-------------|
| `--host` | `127.0.0.1` | Bind host for the server. Use `0.0.0.0` to accept connections from other machines. |
| `--port` | `8080` | Bind port. |
| `--db-path <path>` | `~/.se3/server.db` | Path of the embedded sqlite store backing the identity / auth layer. Overrides the configured `server.db_path` for a single launch. |
| `--log-level` | `info` | uvicorn log level. |
| `bootstrap-token` | — | Mint a one-time **break-glass admin token**, print its plaintext to the console exactly once, and store only its hash. The first entrance into a fresh server — see [Authentication & Multi-Tenant Access](#authentication--multi-tenant-access). Re-runnable. |

`tianluo-server` runs in the foreground and blocks. Run it under a process manager
(systemd, supervisor, a container, …) for an always-on deployment.

> **The server requires authentication.** Since 8.0.0 every web/REST request and
> every daemon connection must resolve to an *owner* — there is no anonymous
> mode. Before the server is useful you must mint a break-glass admin token and
> log in; see [Authentication & Multi-Tenant Access](#authentication--multi-tenant-access).

### A typical session

```bash
# On the machine hosting the control plane — start the server.
pip install 'tianluo[server]'
tianluo-server --host 0.0.0.0 --port 8080

# Mint a one-time break-glass admin token, then open the server in a browser,
# log in with it, and mint a daemon key for your worker (see Authentication
# & Multi-Tenant Access below).
tianluo-server bootstrap-token

# On a worker machine — start a daemon that dials your central server,
# carrying the daemon key so the machine is bound to your owner.
pip install 'tianluo[server]'
luo daemon start --server-url ws://control.example.com:8080 --daemon-key <key>
luo daemon status

# Then open http://control.example.com:8080 in a browser, log in, and watch
# your connected machines, their flows, and publish new tasks.

# When done:
luo daemon stop
```

---

## Deployment & Operations

### The outbound connection model

The daemon and the server connect in exactly one direction: **the daemon dials
out to the server**, over a single WebSocket. The server never initiates a
connection back to a daemon.

```
  machine A                          machine B
  ┌─────────────┐                    ┌─────────────┐
  │ luo daemon  │──┐              ┌──│ luo daemon  │
  └─────────────┘  │  outbound    │  └─────────────┘
                   │  WebSocket   │
                   ▼   /ws        ▼
              ┌────────────────────────┐
              │       tianluo-server       │
              │  (aggregates A + B …)  │
              └────────────────────────┘
```

This design has two practical consequences:

- **NAT-friendly.** Worker machines never need an inbound port or a public
  address. As long as a daemon can reach the server, it can join the control
  plane — laptops, machines behind NAT, and cloud instances all work the same.
- **Resilient.** If the connection drops, the daemon reconnects automatically
  with **exponential backoff** (starting at 1 s, doubling each attempt, capped
  at 60 s). After every (re)connect it re-announces itself and immediately
  pushes a full status snapshot, so the server is never left with stale state.

Once connected, the daemon pushes a status snapshot every few seconds and
answers the server's heartbeat pings; the server routes task-publish and
call-response instructions back down the same socket.

A daemon started **without** `--server-url` skips all of this — it opens no
outbound connection and just supervises and aggregates flows locally.

**Port handling.** The `--server-url` value may carry an explicit port
(`ws://host:9000`, `wss://host:8443`), which is always preserved as given.
When the port is omitted, the daemon completes the URL with a **scheme-aware
default** instead of letting the WebSocket scheme fall back to its implicit
port (80 for `ws`, 443 for `wss`):

| Scheme (after normalizing `http→ws`, `https→wss`) | Default port filled in |
|---------------------------------------------------|------------------------|
| `ws://` (and `http://`) | **8080** — the `tianluo-server` plaintext default |
| `wss://` (and `https://`) | **443** — the standard HTTPS port a TLS reverse proxy listens on |

This matters because a `wss://` daemon almost always terminates TLS at a
reverse proxy on **443**, not at tianluo-server's plaintext **8080**. Before this
rule, a bare `wss://host` was wrongly completed to `wss://host:8080`, so the
daemon dialed TLS at the wrong port and never connected (`luo daemon status`
showed `not connected`). Now `wss://host` dials `:443` out of the box, while
`ws://host` still agrees with a server started with no `--port` flag. The two
plaintext/TLS defaults live in a single shared module
(`tianluo.daemon.protocol`), so the two ends cannot drift. Need a non-standard
port? Give it explicitly — `wss://host:8443` is preserved untouched.

**Seeing the real connection state.** Connecting to a server is best-effort:
if the `tianluo[server]` extra is missing or the dial fails, the daemon logs the
reason and degrades to local-only operation instead of crashing. Because
`--server-url` only *records* a URL, it is not proof the daemon actually
connected — always confirm with `luo daemon status` (see
[the status / runtime files](#runtime-files-pidfile-and-status-file) below),
which reports the true outbound-connection state.

### Foreground vs. background (detached) mode

`luo daemon start` supports two modes:

- **Background (default).** The daemon is fully detached via a double-fork: its
  parent becomes `init`, so it cannot be left as a zombie of the launching
  shell and it survives the terminal closing. `luo daemon start` returns
  immediately after the daemon has claimed its pidfile. Standard streams are
  redirected to a log file (see below).
- **Foreground (`--foreground`).** The daemon runs in the current terminal and
  `luo daemon start` blocks until it stops. Useful for debugging, for running
  under a process supervisor (systemd, Docker, …) that expects a non-detaching
  process, and for watching daemon logs live.

In both modes, only one daemon may run per pid directory — a second
`luo daemon start` detects the live pidfile and exits non-zero.

### Runtime files: pidfile and status file

The daemon keeps its runtime files in `~/.se3/` by default. The directory is
overridable with the `SE3_DAEMON_DIR` environment variable (useful for tests
or for running isolated daemons side by side):

| File | Purpose |
|------|---------|
| `~/.se3/daemon.pid` | Pidfile. Holds the daemon's pid, start time, server URL, and machine id. Guards against duplicate starts and is the source of truth for `stop` / `status`. Removed on clean shutdown. |
| `~/.se3/daemon_status.json` | Latest aggregated status snapshot, rewritten on every poll. This is what `luo daemon status` reads to list tracked flows without contacting the daemon process. It also carries the **real outbound-connection state** — see below. Removed on clean shutdown. |
| `~/.se3/daemon.log` | Log output of a detached (background) daemon — its stdout and stderr are redirected here. Every line is timestamped, so logs from different daemon starts can be told apart. |

Both the pidfile and the status file are written atomically (temp file +
rename) so a crash mid-write cannot corrupt them.

#### Connection state in `status`

`daemon_status.json` records the daemon's **actual** outbound-connection
result, not just the configured URL, and `luo daemon status` surfaces it on a
dedicated `Connection:` line:

- `Connection: local-only (no server configured)` — started without
  `--server-url`.
- `Connection: connected` — the outbound WebSocket to the server is up.
- `Connection: not connected (<reason>)` — a `--server-url` was given but the
  daemon is not connected; the **real, readable reason** is shown verbatim, so
  you can diagnose the failure without digging through logs. The reason is
  populated on every failure path — a missing dependency
  (`websockets not installed`, the `tianluo[server]` extra), a handshake failure,
  a connection refused / timeout (`TimeoutError`), a TLS / wrong-port error, or
  a `WELCOME(accepted=false)` rejection of the daemon key — and never collapses
  to an empty `()` (a bare timeout whose message is empty falls back to the
  exception type name). This is the case where the machine will *not* appear in
  the server's machine list even though `luo daemon start` reported success.
  If the reason is somehow unavailable, the line points you at
  `~/.se3/daemon.log` instead of repeating an information-free literal.

So a configured `Server:` URL plus a `Connection: not connected` line is the
signature of a silent degrade — the fix is usually `pip install 'tianluo[server]'`
or correcting the URL/port.

### Flow discovery and supervision

The daemon tracks two kinds of `luo run` flows on its machine:

- **Spawned flows** — flows the daemon itself started (typically on behalf of a
  remote task-publish request). The daemon is the parent process of these.
- **Discovered flows** — `luo run` processes started independently by a user on
  the same machine. The daemon finds them by scanning process command lines
  (best-effort, via `psutil`) and adopts them into its tracking table.

For every tracked flow the daemon resolves the `flow_id` from that project's
`tianluo/state/engine.json`. It polls liveness on a fixed interval (default every
2 seconds), prunes records for processes that have exited, and — on shutdown —
gracefully terminates any flows it spawned itself (`SIGTERM`, then `SIGKILL`
after a grace period). Discovered flows are *not* killed: the daemon only
supervises the lifecycle of processes it owns.

---

## Architecture & How It Works

### Inside the daemon

The daemon is a single long-lived `asyncio` process composed of four pieces:

- **Supervisor** — discovers and tracks the machine's `luo run` processes
  (spawned + discovered), polling liveness and reaping exited flows.
- **Spawner** — starts new flows as `luo run <task> --type <type>
  --output-format json` child processes. The daemon is the *parent* of each
  flow, never an in-process caller, so a daemon crash never takes a flow down
  with it. Child stdout/stderr are redirected to per-flow log files under
  `<project_root>/tianluo/logs/daemon/` (the child emits a structured NDJSON event
  stream that the daemon can later tail).
- **Aggregator** — a pure reader of the files a flow leaves on disk. It polls
  each tracked project's `tianluo/state/` (engine state + summaries), `tianluo/calls/`
  (the human-call queue), `tianluo/logs/`, and `tianluo/issues/`, and folds them into a
  single `MachineStatus` snapshot: every flow's progress, current step, pending
  calls, log and issue counts. It never reaches into a flow's process.
- **Client** — the optional outbound WebSocket client to the central server.
  It pushes `MachineStatus` snapshots, answers heartbeats, and routes inbound
  instructions (`SPAWN_FLOW` → spawner, `RESPOND_CALL` → a `tianluo/calls/`
  response file) back into the local machine.

The supervisor, aggregator poll loop, and outbound client all run as
concurrent tasks on the daemon's single event loop and share one graceful-stop
signal.

### Inside the central server

`tianluo-server` is a FastAPI application that:

- accepts daemon WebSocket connections on `/ws`, validating each daemon's
  opening handshake and maintaining a `machine_id → connection` pool with
  heartbeats;
- keeps an in-memory **multi-machine / multi-flow** aggregated view —
  `ServerState` — built from the `MachineStatus` snapshots daemons push. This
  *live* machine / flow / history state deliberately lives only in memory and is
  rebuilt as daemons reconnect, so it is never written to the persistence layer;
- persists, in an **embedded single-file sqlite store** (`~/.se3/server.db`,
  stdlib `sqlite3` — no extra dependency), only the identity facts that a daemon
  reconnect *cannot* rebuild: owner records, `(provider, external_id)` identity
  bindings, local password hashes, issued daemon-key hashes, and break-glass
  token hashes (see [Authentication & Multi-Tenant Access](#authentication--multi-tenant-access));
- exposes a REST API for querying and acting on that view:
  `GET /api/machines`, `GET /api/machines/{id}/flows`, `GET /api/flows/{id}`,
  `POST /api/flows` (publish a new task), `POST /api/flows/{id}/respond`
  (answer a flow's pending interjection/call), plus `GET /api/health`. Every
  `/api/*` data route resolves and is filtered by the calling owner;
- serves the bundled web frontend and a frontend WebSocket on `/ws/ui`.

The daemon↔server wire protocol has a single source of truth — the
`tianluo.daemon.protocol` module — imported by both ends so the schema cannot
drift.

### End-to-end: publishing a task from a remote machine

When you publish a task from the web frontend (or directly via
`POST /api/flows`), the full path is:

1. The browser / API client sends the task, target `machine_id`, and task type
   to the **server**.
2. The server looks up that machine's live daemon connection and sends a
   `SPAWN_FLOW` instruction *down* the existing outbound WebSocket.
3. The target **daemon** receives `SPAWN_FLOW` and asks its spawner to start a
   real `luo run --output-format json` child process in the requested project.
4. The new flow runs exactly like any local `luo run`. Its on-disk state is
   picked up by the daemon's **aggregator** on the next poll.
5. The daemon pushes an updated `MachineStatus` snapshot to the server, which
   merges it into `ServerState` and broadcasts the change to every connected
   web frontend.

Answering a flow's pending interjection/call follows the mirror-image path:
`POST /api/flows/{id}/respond` → server → `RESPOND_CALL` down the socket →
the daemon writes a response file into that project's `tianluo/calls/` queue,
which unblocks the paused flow.

### The file-upload channel

The web frontend lets you attach any file — a pasted screenshot, a log, a
small archive — to a prompt: in the New Task box, in a running flow's reply
box, or in the interjection box. The file does **not** travel to the agent as
an attachment; it is landed on disk on the machine that owns the flow, and the
prompt carries its **project-relative path**. The agent then opens that path
with the project root as its working directory, exactly as it would any other
file in the repository.

The path follows the same outbound-only topology as every other command:

1. The browser `POST`s the raw bytes to `POST /api/uploads` over the **existing
   authenticated session** (an unauthenticated request is rejected — there is no
   anonymous upload), naming either the target `flow_id` or an explicit
   `machine_id` + `project_root`.
2. The server resolves the target to a machine it can prove the caller owns,
   then sends an `UPLOAD_COMMAND` frame *down* that machine's existing outbound
   WebSocket and waits for the matching `UPLOAD_RESULT`.
3. The **daemon** decodes the payload and writes it under the project's runtime
   directory, `<project_root>/tianluo/uploads/`.
4. The daemon replies with the project-relative path, which the server returns
   to the browser and the web UI substitutes into the prompt text in place of
   the "uploading…" placeholder that was inserted at the caret.

Properties worth knowing before you rely on it:

- **Size limit: 20 MiB per file**, enforced independently at three layers — the
  browser pre-checks so an oversized paste never leaves the page, the server
  re-checks the request body because the browser is not trusted, and the daemon
  re-checks the decoded payload because the server is not trusted and the
  daemon's disk is the resource actually being protected. `tianluo.daemon.protocol`
  holds the single constant all three follow.
- **Naming and dedup.** A stored file is named `<sha256[:12]>_<original name>`,
  with the original name sanitized (path separators and control characters are
  replaced, so a name can never address a directory outside `uploads/`). The
  hash prefix means two different files called `screenshot.png` coexist instead
  of overwriting each other; re-uploading *identical* content reuses the file
  already on disk and writes nothing. Writes go through a temporary file and an
  atomic rename, so an agent never reads a half-written attachment.
- **The target project must be registered** with that machine's daemon. The
  daemon refuses to write into a directory it does not already track, so a
  compromised or buggy server cannot use this channel to drop files anywhere on
  a worker machine.
- **The daemon must speak protocol revision 5 or newer** (revision 6 for the
  read-back direction described below). The server checks the connected daemon's
  advertised version *before* dispatching and rejects the upload with an explicit
  "daemon too old" error, rather than letting the request sit until it times out
  — an upload happens in the middle of typing, so a silent stall would be
  indistinguishable from a hang. Upgrade the worker's `tianluo` install to
  enable uploads there.
- **Uploads are not version-controlled.** `luo init` ensures the project's
  `.gitignore` carries the upload directory (`se3/uploads/` on a project still
  on the legacy runtime layout), because these are runtime artifacts of
  unbounded size that may carry whatever was dropped into a prompt.
- **Nothing is cleaned up automatically.** There is no retention policy, no TTL
  and no size cap on the directory as a whole: `tianluo/uploads/` grows for as
  long as people attach files, and pruning it is an operator task. Deleting an
  attachment from the web UI's attachment strip only removes the path text from
  the prompt — the file on the project's machine stays.

#### Reading an attachment back

An uploaded file lives on the *daemon's* disk, so a browser rendering the
conversation cannot open it: the only channel to that machine is the daemon's
own outbound socket. **Protocol revision 6** adds the read-back direction, the
mirror image of the upload leg, so the web UI can show an attached screenshot
inline in the conversation instead of a bare path string:

1. `GET /api/uploads/file?path=<project-relative path>` over the same
   authenticated session, naming the target the same two ways as the upload
   (`flow_id`, or `machine_id` + `project_root`). Authentication and the owner
   check are identical — you can only read attachments of a machine you own.
2. The server sends a `FETCH_COMMAND` frame down that machine's socket and waits
   (10 s) for the matching `FETCH_RESULT`.
3. The daemon reads the file and returns the bytes base64-encoded; the server
   decodes them and answers with the raw bytes in the HTTP body.

The rules that make this safe to expose:

- **Containment is decided on the resolved path.** The daemon accepts the read
  only if the requested path resolves to a *direct child* of that project's
  uploads directory. One check covers `..` segments, absolute paths, and — a
  concern unique to the read direction — a symlink planted inside `uploads/` that
  points at some other file on the worker machine. Anything else fails closed as
  `invalid_path`; a legitimate attachment never sits outside that directory.
  The target project must be registered with the daemon, exactly as for uploads.
- **The same 20 MiB limit applies**, decided from `stat()` before a byte is read,
  so an oversized file costs the worker machine no memory.
- **Revision 6 is gated before dispatch.** The server checks the daemon's
  advertised protocol version and answers `501` immediately rather than sending a
  frame an older daemon would silently drop. This matters more here than for
  uploads: a conversation can hold many inline images, and without the gate each
  one would hold a browser connection open for the full timeout.
- **Responses are cached hard**: `Cache-Control: public, max-age=31536000,
  immutable`. This is sound *because* of the content-hash naming above — one
  project-relative uploads path can only ever denote one byte string, so a stale
  entry is unreachable by construction. Without it, scrolling back through a
  conversation would punch a fresh round trip to the daemon per thumbnail per
  repaint.
- **`Content-Type` comes from a small whitelist** of raster image types. Anything
  else is served as `application/octet-stream` with `X-Content-Type-Options:
  nosniff`, so an uploaded `.html` — or an `.svg`, deliberately excluded as a
  script-bearing document — can never be rendered as a document on the server's
  own origin.

**Degradation is silent by design.** Every failure of this leg — a daemon that is
offline (`503`), too old (`501`), slow (`504`), a file that was pruned (`404`),
or a path that does not pass containment (`422`) — surfaces in the browser as a
failed image load, and the web UI simply hides that thumbnail. The message keeps
the path text it always showed, which is the string the agent actually read; no
error is raised at the reader. The inline thumbnail is an addition to the
conversation, never a replacement for its text.

---

## Authentication & Multi-Tenant Access

Since 8.0.0 the central server is a **multi-tenant control plane**: every
web/REST request and every daemon connection must resolve to an *owner*, and
all visibility and control are filtered by that owner. The earlier
identity-unaware "bare" mode — where anyone reaching the server could list all
machines and `POST /api/flows` to dispatch `luo run` on any daemon — has been
removed.

This section walks the end-to-end setup motion:

```
tianluo-server bootstrap-token   →   log in (break-glass)   →   create local users
        →   each owner mints a daemon key   →   luo daemon start --daemon-key
        →   machines & flows are isolated per owner
```

### Why authentication is mandatory

The server **fails closed**. The set of authentication providers is configured
by `server.auth.providers` in `tianluo.yaml`, defaulting to `["local"]` (the
built-in username + password provider). The recognized names are `local`,
`oidc`, and `proxy_header`; `oidc` and `proxy_header` are disabled-by-default
seams not required in v1. If the resolved provider chain ends up with **no
usable provider** (e.g. `local` is explicitly disabled and nothing else is
enabled), the server raises `AuthNotConfigured` at startup and **refuses to
serve** rather than reverting to anonymous access. Likewise, an `/api/*`
request that resolves to no owner is rejected with **HTTP 401**.

### The persistence layer (`~/.se3/server.db`)

The server's only persistence is an **embedded single-file sqlite store**
(stdlib `sqlite3`, no extra dependency), at `~/.se3/server.db` by default. Its
path comes from `server.db_path` in `tianluo.yaml` and can be overridden for a
single launch with `tianluo-server --db-path <path>` (an explicit `--db-path`
wins). It stores **only the identity facts that a daemon reconnect cannot
rebuild**:

- owner records (keyed by an opaque, stable internal `owner_id`);
- `(provider, external_id) → owner_id` identity bindings (one owner may carry
  many bindings; one external identity maps to one owner);
- local password hashes (argon2id preferred, bcrypt fallback — never plaintext
  or a fast hash);
- issued **daemon-key hashes**;
- one-time **break-glass token hashes**.

Machine / flow / history live state is *not* stored here — it stays in memory
and is rebuilt as daemons reconnect.

### Bootstrapping the first admin (`bootstrap-token`)

A fresh server has no accounts, so there is a single IdP-independent entrance:

```bash
tianluo-server bootstrap-token
```

This mints a **one-time break-glass admin token**, prints the plaintext to the
server console **exactly once**, and persists only its SHA-256 hash (it is
never logged). Break-glass is a single admin subject used for two orthogonal
jobs: first-admin bootstrap, and a fail-closed fallback entrance when the
configured provider is unreachable. The command is **re-runnable** — each run
mints a fresh token and prior tokens stay valid until consumed or purged. The
subcommand is dependency-light and works even on a core-only install (without
the `[server]` extra).

You consume the token from the web login screen, or directly:

```
POST /api/auth/breakglass     # consume a one-time token → the break-glass admin owner
```

### Logging in and creating users

Human / UI authentication flows through the provider chain (daemons never
traverse it). The local provider's login ceremony exchanges a username +
password for a server-side session cookie:

```
POST /api/auth/login          # username + password → session cookie
POST /api/auth/logout         # end the session
GET  /api/auth/me             # the current OwnerIdentity
```

Once logged in as an admin (the break-glass admin, or any admin owner), you
create or invite further local users:

```
POST /api/users               # admin-only: create / invite a local user
```

**v1 does not open public self-service registration** — accounts are created by
the first-boot bootstrap admin plus admin-provisioned users. Multiple distinct
owners are distinguished by the local provider, never by minting one
break-glass token per user.

### Issuing daemon keys and binding machines

After logging in, an owner self-services their own **daemon keys** — the
credential a daemon presents so the server can bind the reporting machine to
that owner:

```
POST   /api/daemon-keys           # mint a key bound to the current owner (plaintext returned ONCE)
GET    /api/daemon-keys           # list the owner's key metadata (hashes, never plaintext)
DELETE /api/daemon-keys/{key_id}  # revoke a key
```

The plaintext key is returned **only once** at mint time; only its hash is
persisted. You then start a daemon with that key so its machine joins the
owner's trust domain:

```bash
luo daemon start --daemon-key <key> --server-url wss://control.example.com
# or, equivalently:
SE3_DAEMON_KEY=<key> luo daemon start --server-url wss://control.example.com
```

The daemon carries the key in its HELLO handshake; the server resolves
`key → owner_id`, binds the machine (`MachineRecord.owner_id`), and answers
`WELCOME(accepted=true)`. A **missing or invalid** key is answered with
`WELCOME(accepted=false)` (with a key-free reason) and the socket is closed
without entering the receive loop — the daemon records the rejection and stops
replaying the rejected key in a tight reconnect loop. The key lives only in
memory and on the HELLO wire and is never written to the daemon status file or
any log. A keyless daemon (no `--daemon-key`) stays compatible with local /
legacy single-tenant operation and is simply not bound to any owner.

The daemon→server reverse trust is carried by **TLS**: the daemon dials a known
`wss://` address whose server identity is certificate-backed (the server itself
does not terminate TLS — a reverse proxy does). The application layer builds no
separate server-authentication mechanism on top of that.

### Deploying behind a TLS reverse proxy (wss)

`tianluo-server` speaks plaintext HTTP/WebSocket and does **not** terminate TLS
itself. For a public `wss://` deployment you put a reverse proxy (nginx, Caddy,
…) in front of it to terminate TLS and forward to `tianluo-server` on its plaintext
port (default `8080`). The proxy carries two very different kinds of traffic to
the *same* backend:

- **Static web requests** — ordinary HTTP `GET`/`POST` for the bundled frontend
  (`/`, `/app.js`, `/api/*`). These need no special handling.
- **WebSocket long-lived connections** — the daemon dials `/ws` and the browser
  frontend dials `/ws/ui`. These start as an HTTP `GET` carrying an
  `Upgrade: websocket` header and must be **upgraded to a persistent
  connection**; the proxy has to pass the `Upgrade`/`Connection` headers through
  and keep the connection open.

A **single `location /`** can cover both — you do not need a separate block per
endpoint. The trick is to forward the upgrade headers unconditionally; an
ordinary request simply carries no `Upgrade` header and is proxied as plain
HTTP, while a `/ws` or `/ws/ui` request carries one and gets upgraded.

#### nginx

WebSocket upgrade needs HTTP/1.1 and the `Upgrade`/`Connection` headers passed
through verbatim. The idiomatic nginx pattern uses a `map` to derive the
`Connection` header value from the request's own `Upgrade` header:

```nginx
# MUST live at the http{} level (e.g. inside conf.d or the main nginx.conf),
# NOT inside server{} — `map` is only valid in the http context. Panels such as
# 宝塔(BT)/ openresty that drop your snippet into the server block will error
# with "map directive is not allowed here".
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 443 ssl;
    server_name tianluo.example.com;

    ssl_certificate     /etc/ssl/tianluo.example.com.crt;
    ssl_certificate_key /etc/ssl/tianluo.example.com.key;

    # One location bottoms out both the static frontend and the /ws, /ws/ui
    # long-lived WebSockets. ^~ wins over regex locations a panel may inject.
    location ^~ / {
        proxy_pass http://127.0.0.1:8080;

        # WebSocket upgrade — handshake can ONLY ride HTTP/1.1.
        proxy_http_version 1.1;
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection $connection_upgrade;

        # Preserve the original host / client for the backend.
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # The /ws connection is mostly idle between status snapshots; the
        # default 60s read timeout would tear it down. Lengthen it generously.
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
```

Notes / common pitfalls:

- **`map` must be at the `http{}` level.** It cannot live inside `server{}`. On
  宝塔 (BT) / openresty panels whose "reverse proxy" box drops your snippet into
  the `server` block, defining `map` there fails to start nginx — put the `map`
  in the panel's main config / an `http`-level include and reference
  `$connection_upgrade` from the server block.
- **`proxy_http_version 1.1` is mandatory.** nginx defaults to HTTP/1.0
  upstream, which cannot upgrade; without it the handshake never reaches `101`.
- **Lengthen `proxy_read_timeout`.** A daemon `/ws` connection is idle between
  status pushes; the stock 60s timeout silently drops it and you see the daemon
  flap reconnect.

#### Caddy

Caddy terminates TLS automatically (Let's Encrypt) and proxies WebSockets with
no extra directives — `reverse_proxy` handles the upgrade transparently:

```caddy
tianluo.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

If you want the same generous idle timeout as the nginx example:

```caddy
tianluo.example.com {
    reverse_proxy 127.0.0.1:8080 {
        transport http {
            read_timeout 3600s
        }
    }
}
```

#### Verifying the proxy: a `curl --http1.1` handshake probe

To confirm the proxy actually upgrades a WebSocket through to the backend,
send a raw handshake with `curl` and look for **`HTTP/1.1 101 Switching
Protocols`**:

```bash
curl -i --http1.1 \
  -H "Connection: Upgrade" \
  -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" \
  -H "Sec-WebSocket-Key: $(head -c16 /dev/urandom | base64)" \
  https://tianluo.example.com/ws
```

Expected response (the upgrade succeeded end-to-end):

```
HTTP/1.1 101 Switching Protocols
Upgrade: websocket
Connection: Upgrade
Sec-WebSocket-Accept: <base64 digest of your key>
```

Avoidance points when reading the result:

- **A plain `GET /ws` returning a FastAPI `404 {"detail":"Not Found"}` is
  expected — and is *good news*.** It proves the request traversed the proxy and
  reached the tianluo-server backend; `/ws` simply rejects a non-upgrade GET. If you
  get the proxy's own 404/502 page instead, the request never reached the
  backend.
- **The WebSocket handshake can only ride HTTP/1.1.** Over HTTP/2 you will
  *never* get a `101` — drop `--http1.1` and an HTTP/2-fronted proxy returns a
  normal response, not an upgrade. This is why the nginx example pins
  `proxy_http_version 1.1`.
- **The default port follows the scheme.** A `wss://host` with no port is
  completed to **443** (a TLS reverse proxy's HTTPS port), and `ws://host` to
  **8080** (see [Port handling](#the-outbound-connection-model)). A bare
  `wss://tianluo.example.com` therefore dials `:443` automatically — only add an
  explicit `:port` if your proxy listens elsewhere.

### Owner isolation

Both channels are owner-scoped:

- **The frontend `/ws/ui` and all `/api/*` REST routes** resolve an owner via
  the provider chain and filter visibility and control by owner. An owner sees
  only its own machines / flows / history, and may `POST /api/flows`,
  `respond`, or `interject` **only** against its own daemons. A cross-owner
  target reads as **not-found (404)** rather than being dispatched.
- **The daemon `/ws`** resolves the HELLO key to an owner and binds the
  machine, as described above.

Adding a second auth provider later is purely additive: a new
`(provider, external_id)` binding is linked (through a trust gate) to an
existing `owner_id`, so the `owner_id`, the daemon→owner binding, and
already-issued daemon keys all stay unchanged — daemons never need to be
re-enrolled.

---

## The Web Frontend

`tianluo-server` bundles a small, pure-static web frontend (`index.html`,
`style.css`, `app.js` — no build step). It is mounted at the server root, so
once `tianluo-server` is running you open the server's address in a browser:

```
http://<server-host>:<port>/        # e.g. http://127.0.0.1:8080/
```

**You must log in first.** The control plane is multi-tenant (see
[Authentication & Multi-Tenant Access](#authentication--multi-tenant-access)),
so the frontend presents a login screen — sign in with a local username +
password, or consume a one-time break-glass token on a fresh server — before
any machines or flows are shown. Everything below is then scoped to **your**
owner: you only ever see and act on your own machines, flows, and history; a
cross-owner target reads as not-found.

The page connects back to the server over the owner-scoped `/ws/ui` WebSocket.
The server pushes that owner's machine list down the socket: an initial
`snapshot` on connect, then a `status_update` every time any of *your* daemons'
state changes — so the view updates in real time without polling.

From the frontend you can:

- **Watch flow progress.** The left pane lists every connected machine; select
  one to see its flows, each with current step, progress, and log/issue
  counts. Open a flow for its detail drawer.
- **Publish a task remotely.** The **+ New Task** button opens a dialog to pick
  a target machine and task type and enter a task description. Submitting it
  triggers the end-to-end publish path described above — the task runs on the
  selected remote machine.
- **Respond to an interjection/call.** When a flow pauses for human input, it
  surfaces a pending call in the UI. The response dialog lets you type an
  answer and send it; the server routes it as a `RESPOND_CALL` to the owning
  daemon, which writes the response into that project's `tianluo/calls/` queue and
  resumes the flow.

The frontend is read-only over the WebSocket (it only *listens* for state
pushes); the New Task and respond actions go through the server's REST API.
