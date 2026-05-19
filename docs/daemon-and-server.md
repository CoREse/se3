# SE3 Daemon & Central Server

SE3's core (`se3 run`, `se3 sync`, …) is a one-shot CLI: each command runs in
the foreground and exits. The **daemon** and the **central server** add an
optional, always-on control plane on top of that core:

- **`se3 daemon`** — a resident process on each machine. It discovers and
  supervises this machine's `se3 run` flows, can spawn new flows on behalf of a
  remote caller, aggregates the on-disk state of every flow into a single
  snapshot, and (optionally) maintains an outbound connection to a central
  server.
- **`se3-server`** — a standalone central server that accepts connections from
  any number of daemons, merges their snapshots into a multi-machine /
  multi-flow view, exposes a REST API, and serves a bundled web frontend.

Both are entirely optional. A plain `pip install se3` and `se3 run` need
neither.

## Table of Contents

1. [Quick Start](#quick-start)
   - [Installation](#installation)
   - [The `se3 daemon` command](#the-se3-daemon-command)
   - [The `se3-server` command](#the-se3-server-command)
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
4. [The Web Frontend](#the-web-frontend)

---

## Quick Start

### Installation

The daemon ships with the core `se3` package — `se3 daemon` works out of the
box after a normal install. The **central server** pulls in heavier web
dependencies (FastAPI, uvicorn, websockets), so those are kept in an optional
`server` extra:

```bash
# Core install — includes the `se3 daemon` command.
pip install se3

# Add the central server (`se3-server`) and the daemon's WebSocket client.
pip install 'se3[server]'
```

The `server` extra is also what the daemon's *outbound client* uses to dial a
server. A daemon started without `--server-url` runs purely locally and never
touches the extra; a daemon started with `--server-url` but without the extra
installed logs an install hint and degrades to local-only operation rather
than crashing.

### The `se3 daemon` command

`se3 daemon` is a subcommand group of the core CLI (not a separate binary):

```bash
se3 daemon start                          # Start the daemon (detached background process)
se3 daemon start --foreground             # Run the daemon in the current terminal
se3 daemon start --server-url ws://host   # Start and dial out to a central server
se3 daemon stop                           # Stop the running daemon
se3 daemon status                         # Show running state and tracked flows
se3 daemon status --json                  # Emit the status as JSON
```

| Subcommand | Options | Behavior |
|------------|---------|----------|
| `start` | `--server-url <url>`, `--foreground` | Starts the daemon. By default it is launched as a **detached background process**; `--foreground` runs it in the current terminal instead. `--server-url` records the central-server URL the daemon dials out to — a port may be given explicitly (`ws://host:9000`), and when omitted it is completed to the default server port **8080** (matching the `se3-server` default). If a daemon is already running, the command reports it and exits non-zero. |
| `stop` | — | Stops the running daemon (sends `SIGTERM` and waits for it to exit). Reports `not running` and exits `0` when none is up; reports a stop timeout with a non-zero exit if the process does not exit within the grace period. |
| `status` | `--json`, `-j` | Reports whether the daemon is running, its pid, machine id, configured server URL, the **real outbound-connection state** (see below), and the list of tracked flows. `--json` emits the same information as JSON instead of a rendered panel. |

### The `se3-server` command

The central server is started through its own `console_scripts` entry point,
`se3-server` (installed by the `se3[server]` extra):

```bash
se3-server                                # Bind to 127.0.0.1:8080 (defaults)
se3-server --host 0.0.0.0 --port 9000     # Listen on all interfaces, port 9000
se3-server --log-level debug              # Raise the uvicorn log level
```

| Option | Default | Description |
|--------|---------|-------------|
| `--host` | `127.0.0.1` | Bind host for the server. Use `0.0.0.0` to accept connections from other machines. |
| `--port` | `8080` | Bind port. |
| `--log-level` | `info` | uvicorn log level. |

`se3-server` runs in the foreground and blocks. Run it under a process manager
(systemd, supervisor, a container, …) for an always-on deployment.

### A typical session

```bash
# On a worker machine — start a daemon that dials your central server.
pip install 'se3[server]'
se3 daemon start --server-url ws://control.example.com:8080
se3 daemon status

# On the machine hosting the control plane — start the server.
pip install 'se3[server]'
se3-server --host 0.0.0.0 --port 8080

# Then open http://control.example.com:8080 in a browser to watch every
# connected machine, its flows, and publish new tasks.

# When done:
se3 daemon stop
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
  │ se3 daemon  │──┐              ┌──│ se3 daemon  │
  └─────────────┘  │  outbound    │  └─────────────┘
                   │  WebSocket   │
                   ▼   /ws        ▼
              ┌────────────────────────┐
              │       se3-server       │
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
(`ws://host:9000`). When the port is omitted (`ws://host`), the daemon
completes the URL to the **default server port 8080** instead of letting the
WebSocket scheme fall back to its implicit port 80. `8080` is the same default
`se3-server` binds to, so a daemon started with `ws://host` and a server
started with no `--port` flag agree out of the box; defining the default in a
single shared constant keeps the two ends aligned.

**Seeing the real connection state.** Connecting to a server is best-effort:
if the `se3[server]` extra is missing or the dial fails, the daemon logs the
reason and degrades to local-only operation instead of crashing. Because
`--server-url` only *records* a URL, it is not proof the daemon actually
connected — always confirm with `se3 daemon status` (see
[the status / runtime files](#runtime-files-pidfile-and-status-file) below),
which reports the true outbound-connection state.

### Foreground vs. background (detached) mode

`se3 daemon start` supports two modes:

- **Background (default).** The daemon is fully detached via a double-fork: its
  parent becomes `init`, so it cannot be left as a zombie of the launching
  shell and it survives the terminal closing. `se3 daemon start` returns
  immediately after the daemon has claimed its pidfile. Standard streams are
  redirected to a log file (see below).
- **Foreground (`--foreground`).** The daemon runs in the current terminal and
  `se3 daemon start` blocks until it stops. Useful for debugging, for running
  under a process supervisor (systemd, Docker, …) that expects a non-detaching
  process, and for watching daemon logs live.

In both modes, only one daemon may run per pid directory — a second
`se3 daemon start` detects the live pidfile and exits non-zero.

### Runtime files: pidfile and status file

The daemon keeps its runtime files in `~/.se3/` by default. The directory is
overridable with the `SE3_DAEMON_DIR` environment variable (useful for tests
or for running isolated daemons side by side):

| File | Purpose |
|------|---------|
| `~/.se3/daemon.pid` | Pidfile. Holds the daemon's pid, start time, server URL, and machine id. Guards against duplicate starts and is the source of truth for `stop` / `status`. Removed on clean shutdown. |
| `~/.se3/daemon_status.json` | Latest aggregated status snapshot, rewritten on every poll. This is what `se3 daemon status` reads to list tracked flows without contacting the daemon process. It also carries the **real outbound-connection state** — see below. Removed on clean shutdown. |
| `~/.se3/daemon.log` | Log output of a detached (background) daemon — its stdout and stderr are redirected here. Every line is timestamped, so logs from different daemon starts can be told apart. |

Both the pidfile and the status file are written atomically (temp file +
rename) so a crash mid-write cannot corrupt them.

#### Connection state in `status`

`daemon_status.json` records the daemon's **actual** outbound-connection
result, not just the configured URL, and `se3 daemon status` surfaces it on a
dedicated `Connection:` line:

- `Connection: local-only (no server configured)` — started without
  `--server-url`.
- `Connection: connected` — the outbound WebSocket to the server is up.
- `Connection: not connected (<reason>)` — a `--server-url` was given but the
  daemon is not connected; the reason is shown verbatim, e.g.
  `websockets not installed` (the `se3[server]` extra is missing) or the dial
  error. This is the case where the machine will *not* appear in the server's
  machine list even though `se3 daemon start` reported success.

So a configured `Server:` URL plus a `Connection: not connected` line is the
signature of a silent degrade — the fix is usually `pip install 'se3[server]'`
or correcting the URL/port.

### Flow discovery and supervision

The daemon tracks two kinds of `se3 run` flows on its machine:

- **Spawned flows** — flows the daemon itself started (typically on behalf of a
  remote task-publish request). The daemon is the parent process of these.
- **Discovered flows** — `se3 run` processes started independently by a user on
  the same machine. The daemon finds them by scanning process command lines
  (best-effort, via `psutil`) and adopts them into its tracking table.

For every tracked flow the daemon resolves the `flow_id` from that project's
`se3/state/engine.json`. It polls liveness on a fixed interval (default every
2 seconds), prunes records for processes that have exited, and — on shutdown —
gracefully terminates any flows it spawned itself (`SIGTERM`, then `SIGKILL`
after a grace period). Discovered flows are *not* killed: the daemon only
supervises the lifecycle of processes it owns.

---

## Architecture & How It Works

### Inside the daemon

The daemon is a single long-lived `asyncio` process composed of four pieces:

- **Supervisor** — discovers and tracks the machine's `se3 run` processes
  (spawned + discovered), polling liveness and reaping exited flows.
- **Spawner** — starts new flows as `se3 run <task> --type <type>
  --output-format json` child processes. The daemon is the *parent* of each
  flow, never an in-process caller, so a daemon crash never takes a flow down
  with it. Child stdout/stderr are redirected to per-flow log files under
  `<project_root>/se3/logs/daemon/` (the child emits a structured NDJSON event
  stream that the daemon can later tail).
- **Aggregator** — a pure reader of the files a flow leaves on disk. It polls
  each tracked project's `se3/state/` (engine state + summaries), `se3/calls/`
  (the human-call queue), `se3/logs/`, and `se3/issues/`, and folds them into a
  single `MachineStatus` snapshot: every flow's progress, current step, pending
  calls, log and issue counts. It never reaches into a flow's process.
- **Client** — the optional outbound WebSocket client to the central server.
  It pushes `MachineStatus` snapshots, answers heartbeats, and routes inbound
  instructions (`SPAWN_FLOW` → spawner, `RESPOND_CALL` → a `se3/calls/`
  response file) back into the local machine.

The supervisor, aggregator poll loop, and outbound client all run as
concurrent tasks on the daemon's single event loop and share one graceful-stop
signal.

### Inside the central server

`se3-server` is a FastAPI application that:

- accepts daemon WebSocket connections on `/ws`, validating each daemon's
  opening handshake and maintaining a `machine_id → connection` pool with
  heartbeats;
- keeps an in-memory **multi-machine / multi-flow** aggregated view —
  `ServerState` — built from the `MachineStatus` snapshots daemons push (this
  delivery has no database; state is rebuilt as daemons reconnect);
- exposes a REST API for querying and acting on that view:
  `GET /api/machines`, `GET /api/machines/{id}/flows`, `GET /api/flows/{id}`,
  `POST /api/flows` (publish a new task), `POST /api/flows/{id}/respond`
  (answer a flow's pending interjection/call), plus `GET /api/health`;
- serves the bundled web frontend and a frontend WebSocket on `/ws/ui`.

The daemon↔server wire protocol has a single source of truth — the
`se3.daemon.protocol` module — imported by both ends so the schema cannot
drift.

### End-to-end: publishing a task from a remote machine

When you publish a task from the web frontend (or directly via
`POST /api/flows`), the full path is:

1. The browser / API client sends the task, target `machine_id`, and task type
   to the **server**.
2. The server looks up that machine's live daemon connection and sends a
   `SPAWN_FLOW` instruction *down* the existing outbound WebSocket.
3. The target **daemon** receives `SPAWN_FLOW` and asks its spawner to start a
   real `se3 run --output-format json` child process in the requested project.
4. The new flow runs exactly like any local `se3 run`. Its on-disk state is
   picked up by the daemon's **aggregator** on the next poll.
5. The daemon pushes an updated `MachineStatus` snapshot to the server, which
   merges it into `ServerState` and broadcasts the change to every connected
   web frontend.

Answering a flow's pending interjection/call follows the mirror-image path:
`POST /api/flows/{id}/respond` → server → `RESPOND_CALL` down the socket →
the daemon writes a response file into that project's `se3/calls/` queue,
which unblocks the paused flow.

---

## The Web Frontend

`se3-server` bundles a small, pure-static web frontend (`index.html`,
`style.css`, `app.js` — no build step). It is mounted at the server root, so
once `se3-server` is running you simply open the server's address in a
browser:

```
http://<server-host>:<port>/        # e.g. http://127.0.0.1:8080/
```

The page connects back to the server over the `/ws/ui` WebSocket. The server
pushes the full machine list down that socket: an initial `snapshot` on
connect, then a `status_update` every time any daemon's state changes — so the
view updates in real time without polling.

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
  daemon, which writes the response into that project's `se3/calls/` queue and
  resumes the flow.

The frontend is read-only over the WebSocket (it only *listens* for state
pushes); the New Task and respond actions go through the server's REST API.
