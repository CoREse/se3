# Remove se3-controller Spec

## Summary

Archived the experimental se3-controller spec (External Controller architecture) in favor of the simplified git-worktree-collab approach.

## What Was Removed

The se3-controller spec documented an External Controller architecture that included:

- **Python daemon process** - External Controller Daemon for managing Claude sessions
- **HTTP API server** - REST API for session management (/session/start, /session/stop, etc.)
- **MCP server** - For manager-worker communication
- **Auto-Commit Watcher** - File system monitoring with silence detection
- **Worker Coordinator** - Alternative to collab --daemon for spawning workers

## Why It Was Removed

The git-worktree-collab spec (v2) explicitly removed the External Controller architecture in favor of a simplified approach:

> "Replace the experimental External Controller (v2) with a simplified architecture based on the proven bash orchestrator (v1). Remove unnecessary complexity: no daemon, no HTTP API, no MCP server, no real-time manager-worker communication."

**Key reasons for removal:**

1. **Unnecessary complexity** - The bash orchestrator is sufficient; it spawns processes and waits for exit
2. **File system communication is simpler** - More reliable than HTTP API or MCP
3. **No real-time communication needed** - Manager and Worker do NOT need to communicate during worker execution
4. **Git commits as progress indicator** - Worker progress tracked via git commits, not real-time reports

## What Replaced It

The **git-worktree-collab** spec provides:

- Bash orchestrator (no daemon, no resident process)
- Git worktree-based isolation
- File-based async communication only
- Stateless manager invoked on-demand via `claude -p`
- Workers as independent `claude -p` processes in git worktrees

## Files Archived

- `EXTERNAL-CONTROLLER-ARCH.md` - Full architecture documentation
- `spec.yaml` - OpenAPI specification for the HTTP API

## Migration Path

Any code referencing the se3-controller should migrate to git-worktree-collab:

| Old (se3-controller) | New (git-worktree-collab) |
|---------------------|---------------------------|
| `se3 session start` | `se3 collab --daemon` |
| Python daemon | Bash orchestrator |
| HTTP API | File-based task protocol |
| MCP server | Direct file I/O |
| Real-time progress | Git commit history |
