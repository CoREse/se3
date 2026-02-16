# git-worktree-collab Specification (v2 - Simplified)

## Purpose

Replace the experimental External Controller (v2) with a simplified architecture based on the proven bash orchestrator (v1). Remove unnecessary complexity: no daemon, no HTTP API, no MCP server, no real-time manager-worker communication.

**Core principle**: Git worktree-based isolation + file-based async communication only.

## Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│  Layer 1: Orchestrator (bash)                       │
│  - Event loop: spawn → wait → react                 │
│  - File system is the ONLY communication channel    │
│  - Zero AI token cost while idle                    │
├─────────────────────────────────────────────────────┤
│  Layer 2: Manager (claude -p)                       │
│  - Invoked on-demand when decisions needed          │
│  - Stateless: reads .collab/, writes decisions      │
│  - NO real-time communication with workers          │
├─────────────────────────────────────────────────────┤
│  Layer 3: Workers (claude -p)                       │
│  - Isolated in git worktrees                        │
│  - Independent full context window                  │
│  - Commit & exit — process exit = task complete     │
└─────────────────────────────────────────────────────┘
```

## Key Design Decisions

### 1. No Daemon, No API, No MCP

**REMOVED from v2:**
- ❌ Python daemon process (`daemon.py`)
- ❌ HTTP API server (`api_server.py`)
- ❌ MCP server for manager-worker communication (`mcp-collab/server`)
- ❌ Real-time progress reporting

**RATIONALE:**
- Bash orchestrator is sufficient — it spawns processes and waits for exit
- File system communication is simpler and more reliable
- Manager and Worker do NOT need to communicate during worker execution
- Worker progress is tracked via git commits, not real-time reports

### 2. Async Communication Only

**Communication pattern:**
```
Manager creates Task File ──► Worker reads Task File (at start)
                              Worker works independently
                              Worker writes result (git commit + exit)
Worker exit triggers ───────► Manager reads result (at end)
```

**NO communication during worker execution:**
- Manager does not monitor worker progress
- Worker does not ask manager questions mid-task
- Worker blocks/writes human-calls/ directly if human needed

**RATIONALE:**
- Git worktree paradigm: branch is isolation boundary
- Commit history is the progress indicator
- Human calls go directly to human-calls/, not through manager

### 3. Simplified Health Monitoring

**Worker health:**
- Process timeout via bash `timeout` command
- Git commit activity check (orchestrator polls periodically)
- NO heartbeat/MCP required

**Manager health:**
- Timeout on `claude -p` invocation
- JSON validation with retry

**Orchestrator health:**
- NO watchdog layer — orchestrator crash = session ends
- State persistence in `.collab/` allows manual resume

## Requirements

### Requirement: Three-Layer Architecture (Simplified)

The system SHALL use a three-layer architecture with bash orchestrator, manager (claude -p), and workers (claude -p).

**Orchestrator** (Layer 1):
- Pure bash script — no daemon, no resident process
- Spawns manager/worker via `claude -p`, waits for exit
- Reads task files, updates status, makes spawn decisions
- All state in `.collab/` files — orchestrator is stateless

**Manager** (Layer 2):
- Invoked via `claude -p` for specific decisions only
- Reads `.collab/` state, returns JSON decision
- Does NOT communicate with running workers
- Exits after returning decision

**Workers** (Layer 3):
- Each runs as independent `claude -p` in git worktree
- Full context window, no shared state pressure
- Commits to branch, exits when done (exit code = result)
- If blocked: write to `human-calls/`, exit with code 2

#### Scenario: Event-driven orchestrator loop
- **WHEN** orchestrator starts
- **THEN** it spawns manager for initial planning
- **WHEN** manager returns task list
- **THEN** orchestrator spawns workers up to `max_parallel_workers`
- **WHEN** any worker exits
- **THEN** orchestrator spawns manager for review/failure-handling
- **WHEN** all tasks terminal
- **THEN** orchestrator exits

#### Scenario: Stateless recovery
- **WHEN** orchestrator is restarted with `--resume`
- **THEN** it reads `.collab/config.json` and `.collab/tasks/*.json`
- **AND** resumes the event loop from current state

---

### Requirement: Task File Protocol (Unchanged)

The system SHALL use JSON task files in `.collab/tasks/` as the single source of truth.

**Task lifecycle**: `pending` → `in_progress` → (`done` | `failed` | `blocked` | `timeout`)

**Task file format**: Same as v1 (see original spec for full schema)

Key fields:
- `id`, `status`, `branch`, `worktree`
- `prompt`: Full worker prompt
- `worker_pid`: Set by orchestrator when spawning
- `worker_exit_code`: Set by orchestrator on exit
- `health.attempts`: Retry counter
- `health.timeout_minutes`: Timeout setting

**Collaboration config** (`.collab/config.json`):
```json
{
  "session_id": "collab-20260215-100000",
  "objective": "Implement features X, Y, Z",
  "base_branch": "master",
  "max_parallel_workers": 3,
  "worker_timeout_minutes": 60,
  "manager_timeout_minutes": 15,
  "status": "active"
}
```

#### Scenario: Task persistence
- **WHEN** orchestrator spawns a worker
- **THEN** it writes `worker_pid` to task file
- **WHEN** worker exits
- **THEN** orchestrator records `worker_exit_code` and updates `status`

---

### Requirement: Manager-Worker Async Boundary

Manager and Worker SHALL NOT communicate during worker execution.

**Worker prompt includes:**
- Task description
- Spec references
- Rules (from `rules-worker.md`)
- Instruction: "Work independently. Commit your changes. Exit when done."

**Worker blocked/needs help:**
- Worker writes to `human-calls/` directly
- Worker exits with code 2 (blocked)
- Orchestrator sets status to `blocked`
- Human response processed in next orchestrator loop

**Manager decisions (JSON response):**
```json
{
  "action": "plan|merge|reject|retry|split|escalate|complete",
  "tasks": [...],
  "target_task": "task-001",
  "merge_branch": "collab/feature-x",
  "retry_prompt": "...",
  "reason": "...",
  "summary": "..."
}
```

#### Scenario: Worker blocked on ambiguity
- **WHEN** worker encounters unclear requirement
- **THEN** worker writes `human-calls/task-001-clarification.md`
- **AND** worker exits with code 2
- **WHEN** orchestrator sees exit code 2
- **THEN** sets status to `blocked` and continues other tasks
- **WHEN** human responds
- **THEN** orchestrator re-spawns worker with human's answer

#### Scenario: Manager reviews completed work
- **WHEN** worker exits with code 0
- **THEN** orchestrator invokes manager with review context
- **AND** manager examines git diff (not live worker)
- **THEN** manager returns merge/reject/split decision

---

### Requirement: Orchestrator Event Loop (Bash)

The orchestrator SHALL implement a simple bash event loop.

```bash
# Pseudocode
load_state() {
  CONFIG=$(cat .collab/config.json)
  TASKS=(.collab/tasks/task-*.json)
}

spawn_manager(event_type, context) {
  claude -p "$MANAGER_PROMPT" --output-format json
  # Parse JSON, return action
}

spawn_worker(task_id) {
  git worktree add ".worktrees/$task_id" -b "collab/$task_id"
  claude -p "$WORKER_PROMPT" &
  echo $! > .collab/tasks/$task_id.pid
}

main_loop() {
  load_state

  # Initial planning if no tasks
  if no_tasks; then
    action=$(spawn_manager "plan" "$OBJECTIVE")
    create_task_files "$action"
  fi

  # Main loop
  while true; do
    # Spawn workers up to limit
    for task in pending_tasks; do
      if active_workers < max_parallel; then
        spawn_worker "$task"
      fi
    done

    # Wait for any child to exit
    wait -n  # bash waits for any background job

    # Process exited workers
    for task in in_progress_tasks; do
      if ! kill -0 "$task.pid" 2>/dev/null; then
        exit_code=$?  # Get actual exit code
        handle_worker_exit "$task" "$exit_code"
      fi
    done

    # Check if all tasks terminal
    if all_terminal; then
      generate_summary
      exit 0
    fi
  done
}
```

#### Scenario: Parallel worker spawning
- **WHEN** 3 tasks pending and max_parallel=3
- **THEN** orchestrator spawns all 3 workers concurrently
- **WHEN** one worker exits
- **THEN** orchestrator spawns manager for review
- **AND** may spawn next pending worker if slot available

#### Scenario: Sequential manager invocation
- **WHEN** two workers complete simultaneously
- **THEN** orchestrator queues manager invocations
- **AND** processes them one at a time

---

### Requirement: Health Monitoring (Simplified)

**Worker timeout:**
- Bash `timeout` command wraps worker: `timeout ${TIMEOUT}m claude -p ...`
- Exit code 124 = timeout

**Stall detection:**
- Orchestrator periodically checks: `git -C "$worktree" log -1 --format=%ct`
- No commit within `stale_threshold_minutes` = stall
- Kill worker, set status to `timeout`

**Manager timeout:**
- `timeout ${MANAGER_TIMEOUT}m claude -p ...`
- On timeout: retry once with simplified prompt
- On retry failure: escalate to human

#### Scenario: Worker timeout
- **WHEN** worker exceeds `worker_timeout_minutes`
- **THEN** `timeout` kills the process, exit code 124
- **AND** orchestrator sets status to `timeout`
- **AND** invokes manager for retry/escalate decision

#### Scenario: Worker stall (no commits)
- **WHEN** worker alive but no git activity for 30 minutes
- **THEN** orchestrator kills worker
- **AND** sets status to `timeout`

---

### Requirement: Human-as-MCP (Direct)

Workers and Manager SHALL interact with human directly via `human-calls/` directory.

**Worker blocked:**
1. Worker writes `human-calls/{task-id}-{question}.md`
2. Worker exits with code 2
3. Orchestrator sets task status to `blocked`
4. Orchestrator continues other tasks
5. When human responds, orchestrator re-spawns worker

**Manager escalates:**
1. Manager writes `human-calls/escalate-{timestamp}.md`
2. Manager returns `{"action": "escalate", ...}`
3. Orchestrator pauses session, waits for human

**NO MCP server required** — direct file I/O.

---

## CLI Commands

```bash
# Start automatic collaboration
se3 collab --daemon "Implement auth and caching"

# Resume crashed session
se3 collab --resume

# Check status
se3 collab --status

# Abort and cleanup
se3 collab --abort

# Manual mode (generate plan, execute manually)
se3 collab --manual "Implement feature X"
```

## Configuration (se3.config.yaml)

```yaml
collab:
  max_parallel_workers: 3
  worker_timeout_minutes: 60
  manager_timeout_minutes: 15
  stale_threshold_minutes: 30
  max_worker_attempts: 3
  max_manager_retries: 2
```

## Migration from v2 (External Controller)

**To be removed:**
- `tools/se3_tools/controller/` (daemon, api_server, persistence)
- `tools/se3_tools/commands/collab_v2.py`
- `scripts/mcp-collab/`

**To keep:**
- `tools/se3_tools/commands/collab.py` (v1 bash orchestrator)
- `scripts/collab-orchestrator.sh`
- `scripts/rules-worker.md`
- `scripts/rules-manager.md`

**Behavior change:**
- No `se3 controller` commands
- No persistent daemon process
- `--daemon` flag starts bash orchestrator in background (not Python daemon)
