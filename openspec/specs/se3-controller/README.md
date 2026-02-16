# SE3 External Controller

External Controller for SE3 Framework - Solves nested Claude limitation and ensures deterministic commits.

## Overview

The External Controller runs outside of Claude Code and manages:
- **Session lifecycle**: Start/stop/resume Claude sessions
- **Auto-commit**: Automatically commit based on file change patterns
- **Multi-agent collaboration**: Spawn worker/manager processes without nesting
- **Recovery**: Automatic crash detection and recovery

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      User Terminal                          │
│  ┌─────────────────────────────────────────────────────┐   │
│  │  $ se3 session start                                │   │
│  │  [External Controller Daemon starts]                │   │
│  │  [Auto-commit watcher starts]                       │   │
│  │  [Claude Process spawned in subprocess]             │   │
│  └────────────────────┬────────────────────────────────┘   │
└───────────────────────┼─────────────────────────────────────┘
                        │ stdio/pty
                        ▼
┌─────────────────────────────────────────────────────────────┐
│                   Claude Interactive Mode                    │
│  ┌─────────────────────────────────────────────────────┐   │
│  │  User ↔ Claude 交互（正常对话模式）                 │   │
│  │  Claude 通过 MCP/文件与 External Controller 通信    │   │
│  └────────────────────┬────────────────────────────────┘   │
└───────────────────────┼─────────────────────────────────────┘
                        │ MCP / File System
                        ▼
┌─────────────────────────────────────────────────────────────┐
│              External Controller Daemon (Python)            │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐ │
│  │ Session     │  │ Auto-Commit │  │ Worker Coordinator │ │
│  │ Manager     │  │ Watcher     │  │ (for collab mode)  │ │
│  └─────────────┘  └─────────────┘  └─────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

## Quick Start

### 1. Start Controller Daemon

```bash
# Start the daemon (run once per machine)
se3 daemon

# Check status
se3 daemon --status

# Stop daemon
se3 daemon --stop
```

### 2. Start Managed Session

```bash
# Start interactive session with auto-commit
se3 session "Implement feature X"

# Or start without objective
se3 session
```

### 3. Multi-Agent Collaboration

```bash
# Start collaboration (workers run outside, no nesting)
se3 collab-v2 --daemon "Implement authentication system"

# Check status
se3 collab-v2 --status

# Abort
se3 collab-v2 --abort
```

## Commands

### `se3 daemon`

Manage the External Controller daemon.

```bash
se3 daemon              # Start daemon
se3 daemon --status     # Check if running
se3 daemon --stop       # Stop daemon
```

### `se3 session`

Start an interactive Claude session managed by External Controller.

```bash
se3 session "Objective"     # Start with objective
se3 session                 # Start without objective
```

Features:
- Auto-commit on file changes (5 min silence)
- Session persistence
- Graceful shutdown with commit

### `se3 collab-v2`

Multi-agent collaboration using External Controller.

```bash
se3 collab-v2 --daemon "Objective"      # Auto mode
se3 collab-v2 --manual "Objective"      # Manual mode
se3 collab-v2 --status                  # Show status
se3 collab-v2 --abort                   # Stop and cleanup
```

### Legacy Commands

Old `se3 collab` still works (v1 fallback):

```bash
se3 collab --daemon "Objective"         # Uses v1 (bash orchestrator)
se3 collab --v1 --daemon "Objective"    # Explicit v1
se3 collab --daemon "Objective"         # Auto-fallback if daemon not running
```

## Auto-Commit Configuration

Create `.claude/auto-commit.json`:

```json
{
  "enabled": true,
  "silence_timeout": 300,
  "commit_on_test_pass": true,
  "commit_on_session_end": true
}
```

| Option | Default | Description |
|--------|---------|-------------|
| `enabled` | `true` | Enable auto-commit |
| `silence_timeout` | `300` | Seconds of inactivity before auto-commit |
| `commit_on_test_pass` | `true` | Commit after tests pass |
| `commit_on_session_end` | `true` | Commit when session ends |

## MCP Tools

Claude can use these MCP tools to communicate with Controller:

- `report_task_complete` - Report task completion
- `request_human_input` - Request human intervention
- `trigger_commit` - Request immediate commit
- `spawn_worker_task` - Spawn new worker
- `report_status` - Report current status
- `request_pause` - Request session pause

## API Server

The controller exposes HTTP API for programmatic access:

```python
from se3_tools.controller.api_server import app

# Run server
# Default: TCP on localhost:8765
# Unix socket: /home/user/.se3/controller/daemon.sock
```

### Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/session/start` | POST | Start new session |
| `/session/stop` | POST | Stop session |
| `/session/status` | GET | Get session status |
| `/commit/trigger` | POST | Trigger commit |
| `/commit/config` | GET/PUT | Auto-commit config |
| `/collab/start` | POST | Start collaboration |
| `/collab/spawn` | POST | Spawn worker/manager |
| `/health` | GET | Health check |

## Troubleshooting

### Daemon not starting

```bash
# Check logs
~/.se3/controller/daemon.log

# Check port/socket conflicts
lsof -i :8765
```

### Auto-commit not working

```bash
# Check config
cat .claude/auto-commit.json

# Verify git status
git status
```

### Workers not spawning

```bash
# Check if controller is running
se3 daemon --status

# Check collab config
cat .collab/config.json
```

## Migration from v1

Old workflow:
```bash
se3 collab --daemon "Objective"  # May fail in Claude Code due to nesting
```

New workflow:
```bash
# Terminal 1: Start daemon
se3 daemon

# Terminal 2: Start collaboration (or use se3 session)
se3 collab-v2 --daemon "Objective"
```

Or use `se3 session` for interactive mode with auto-commit.

## Development

### Project Structure

```
tools/se3_tools/controller/
├── __init__.py          # Package init
├── daemon.py            # Main daemon & CLI
├── api_server.py        # HTTP API server
├── mcp_server.py        # MCP server for Claude
├── persistence.py       # Session persistence & recovery
└── mcp-config.json      # MCP configuration
```

### Running Tests

```bash
cd tools
pytest tests/ -v
```

## License

MIT - Same as SE3 Framework
