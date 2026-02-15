# git-worktree-collab Specification

## Purpose

Replace the experimental agent-team Task tool approach with a git worktree-based multi-agent collaboration system. Each agent runs as an independent `claude -p` process with its own full context window, communicating through git branches and file-based task definitions. A shell-script orchestrator manages process lifecycles, health monitoring, and event routing — consuming zero AI tokens between invocations.

This spec supersedes `agent-team` for parallel work coordination.

## Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│  Layer 0: Watchdog (bash)                           │
│  - Monitors orchestrator health                     │
│  - Restarts on failure                              │
│  - Escalates to human if restart fails              │
├─────────────────────────────────────────────────────┤
│  Layer 1: Orchestrator (bash)                       │
│  - Process lifecycle management                     │
│  - Timeout enforcement                              │
│  - Event routing (worker exit → manager invoke)     │
│  - Zero AI token cost                               │
├─────────────────────────────────────────────────────┤
│  Layer 2: Manager (claude -p)                       │
│  - Task planning & distribution                     │
│  - Merge review & conflict resolution               │
│  - Failure handling & task reassignment              │
│  - Invoked on-demand, not resident                  │
├─────────────────────────────────────────────────────┤
│  Layer 3: Workers (claude -p)                       │
│  - Implementation in isolated worktrees             │
│  - Each has full independent context window         │
│  - Commit to branch, exit when done                 │
│  - Process lifecycle = task lifecycle               │
└─────────────────────────────────────────────────────┘
```

## Requirements

### Requirement: Three-Layer Process Architecture

The system SHALL use a three-layer architecture: shell-script orchestrator, manager (claude -p), and workers (claude -p).

**Orchestrator** (Layer 1):
- Pure bash script — no AI model invocations, zero token cost while idle
- Manages process lifecycles: spawn, monitor, timeout, kill
- Routes events: worker exit triggers manager invocation
- Maintains `.collab/` directory as shared state

**Manager** (Layer 2):
- Invoked via `claude -p` only when decisions are needed
- Stateless between invocations — reads all state from `.collab/`
- Actions: plan tasks, create branches, review merges, handle failures
- Returns structured JSON output to orchestrator

**Workers** (Layer 3):
- Each runs as independent `claude -p` in its own git worktree
- Full context window per worker (no shared context pressure)
- Commits to its branch, writes task result, exits
- Process exit = task completion signal

#### Scenario: Event-driven manager invocation
- **WHEN** a worker process exits (success or failure)
- **THEN** the orchestrator invokes the manager via `claude -p` with the event context, consuming tokens only for actual decision-making

#### Scenario: Worker isolation via git worktree
- **WHEN** the orchestrator spawns a worker for a task
- **THEN** it creates a dedicated git worktree on a new branch, and the worker operates exclusively within that worktree

#### Scenario: Stateless manager recovery
- **WHEN** the manager is invoked after a previous manager invocation crashed
- **THEN** it reads `.collab/` state files and resumes correctly without any in-memory state dependency

---

### Requirement: Role-Specific Context Injection

The system SHALL inject role-specific rulesets into agent prompts instead of loading the full SE3 framework specification.

**Rationale**: The full SE3.md (~500 lines) contains session management, requirement intake, input classification, and other concerns irrelevant to workers. Loading it wastes context window space that workers need for actual implementation. Each role gets only the rules it needs.

**Worker ruleset** (`rules-worker.md`, ~80 lines) includes:
- Implementation workflow (read spec → code → test → commit)
- Verification protocol (spec scenarios as acceptance criteria)
- Spec guardrails (MUST NOT modify specs being implemented)
- Commit conventions
- MCP tool usage (report_progress as heartbeat, ask_human for clarification)
- Exit code semantics (0=done, 1=failed, 2=blocked)
- Context conservation guidance (don't read framework files, focus on task files only)

**Worker ruleset excludes**:
- Session protocol (startup/shutdown)
- Input classification & stage routing
- Requirement intake
- Agent team coordination
- Progress/status file management
- SDD workflow management
- Self-iterate protocol

**Manager ruleset** (`rules-manager.md`, ~120 lines) includes:
- Decision types and JSON response schema
- Task decomposition rules (parallelism, bounded scope, self-contained prompts)
- Code review criteria (diff check, test check, spec compliance)
- Failure handling decision tree
- Merge ordering strategy
- Escalation protocol

**Manager ruleset excludes**:
- Implementation details (how to write code)
- Session protocol (startup/shutdown)
- Commit conventions (manager doesn't commit)
- Verification protocol details (manager reviews, doesn't test)

**Injection mechanism**: The orchestrator reads `rules-worker.md` and `rules-manager.md` at startup and prepends the appropriate ruleset to each `claude -p` prompt.

#### Scenario: Worker receives minimal context
- **WHEN** the orchestrator spawns a worker
- **THEN** the worker's prompt begins with the worker ruleset followed by the task-specific prompt, and does NOT include the full SE3.md

#### Scenario: Manager receives decision-focused context
- **WHEN** the orchestrator invokes the manager
- **THEN** the manager's prompt begins with the manager ruleset followed by the event context, and does NOT include implementation rules

#### Scenario: Rules files missing fallback
- **WHEN** a rules file is missing from the scripts directory
- **THEN** the orchestrator uses a minimal inline fallback and logs a warning

---

### Requirement: Task File Protocol

The system SHALL use JSON task files in `.collab/tasks/` as the single source of truth for task state.

**Task lifecycle**: `pending` → `in_progress` → (`done` | `failed` | `blocked` | `timeout`)

**Task file format** (`.collab/tasks/task-{id}.json`):
```json
{
  "id": "task-001",
  "branch": "collab/feature-auth",
  "worktree": ".worktrees/feature-auth",
  "status": "pending",
  "title": "Implement authentication module",
  "prompt": "Full prompt text for the worker claude -p invocation...",
  "spec_refs": ["openspec/specs/auth/spec.md"],
  "created_at": "2026-02-15T10:00:00Z",
  "started_at": null,
  "completed_at": null,
  "worker_pid": null,
  "worker_exit_code": null,
  "result_summary": null,
  "blocked_reason": null,
  "review": {
    "status": "pending",
    "merge_commit": null,
    "comments": null
  },
  "health": {
    "last_commit_at": null,
    "timeout_minutes": 60,
    "attempts": 0,
    "max_attempts": 3
  }
}
```

**Collaboration session config** (`.collab/config.json`):
```json
{
  "session_id": "collab-20260215-100000",
  "objective": "Implement features X, Y, Z",
  "base_branch": "master",
  "created_at": "2026-02-15T10:00:00Z",
  "max_parallel_workers": 3,
  "worker_timeout_minutes": 60,
  "manager_timeout_minutes": 15,
  "manager_model": "sonnet",
  "worker_model": "sonnet",
  "status": "active"
}
```

#### Scenario: Task state persistence across crashes
- **WHEN** the orchestrator crashes and restarts
- **THEN** it reads `.collab/tasks/*.json` to reconstruct the full state of all tasks

#### Scenario: Task status transitions
- **WHEN** a worker exits with code 0
- **THEN** the orchestrator sets task status to `done` and triggers manager for review
- **WHEN** a worker exits with non-zero code
- **THEN** the orchestrator sets task status to `failed`, records exit code, and triggers manager for failure handling
- **WHEN** a worker exceeds its timeout
- **THEN** the orchestrator kills the process, sets status to `timeout`, and triggers manager

---

### Requirement: Orchestrator Event Loop

The system SHALL implement an event-driven orchestrator that reacts to process lifecycle events.

```
START
  │
  ├── [1] Invoke manager: "Plan tasks for objective: ..."
  │     └── Manager creates .collab/tasks/task-*.json
  │
  ├── [2] For each pending task (up to max_parallel):
  │     ├── Create git worktree + branch
  │     ├── Spawn worker (claude -p) with timeout
  │     └── Record PID in task file
  │
  ├── [3] WAIT for any child process to exit
  │     │
  │     ├── On worker exit (success):
  │     │   ├── Update task status → done
  │     │   ├── Invoke manager: "Review task-{id} on branch {branch}"
  │     │   │   └── Manager: merge / request-changes / create-followup
  │     │   └── Spawn next pending worker if any
  │     │
  │     ├── On worker exit (failure/timeout):
  │     │   ├── Update task status → failed/timeout
  │     │   ├── Invoke manager: "Handle failure for task-{id}"
  │     │   │   └── Manager: retry / reassign / escalate-to-human
  │     │   └── Act on manager decision
  │     │
  │     └── On health check alarm:
  │         ├── Check stale workers (no git activity)
  │         ├── Kill stale workers → set timeout
  │         └── Trigger manager
  │
  ├── [4] LOOP to step 3 until all tasks done/failed
  │
  └── [5] Cleanup worktrees, generate summary
END
```

#### Scenario: Parallel worker spawning
- **WHEN** multiple tasks are pending and worker slots are available
- **THEN** the orchestrator spawns workers up to `max_parallel_workers` concurrently

#### Scenario: Sequential review after completion
- **WHEN** a worker completes and the manager is already running for another review
- **THEN** the orchestrator queues the review and invokes the manager after the current invocation returns

---

### Requirement: Human-as-MCP Integration

Human intervention SHALL follow the human-as-MCP principle: from the AI's perspective, asking a human is a tool call, not a special case.

**Implementation**: Both manager and workers run with `--mcp-config` pointing to a collaboration MCP server that provides:

```
Tools provided by collab MCP server:
├── ask_human(question, options?, urgent?)
│   → Writes to human-calls/, notifies human, blocks until response
│   → Returns human's response text
│
├── notify_human(message, level: info|warning|error)
│   → Non-blocking notification to human
│   → Returns immediately
│
└── report_progress(task_id, message, percent?)
    → Updates .collab/tasks/{id}.json health.last_activity
    → Also serves as heartbeat for health monitoring
```

**Escalation chain**: Worker → Manager → Human
- Worker encounters ambiguity → calls `ask_human` via MCP → orchestrator routes to manager first
- Manager can't resolve → manager calls `ask_human` → orchestrator routes to actual human
- Human response written to `human-calls/` → MCP server reads and returns to caller

**Non-blocking fallback**: If `ask_human` is configured as non-blocking:
- Worker writes blocked_reason to task file, exits with special code (exit 2)
- Orchestrator sets status to `blocked`, continues other tasks
- When human responds, orchestrator re-spawns worker with original prompt + human's answer

#### Scenario: Worker needs human clarification
- **WHEN** a worker calls the `ask_human` MCP tool
- **THEN** the call is routed through the orchestrator: first to the manager (who may resolve it), then to the actual human if the manager cannot

#### Scenario: Manager escalates to human
- **WHEN** the manager itself calls `ask_human`
- **THEN** the orchestrator writes to `human-calls/` and waits for human response before continuing

#### Scenario: Non-blocking human call
- **WHEN** `ask_human` is called with `urgent: false` and there are other pending tasks
- **THEN** the worker exits with "blocked" status, and the orchestrator continues processing other tasks while waiting for human response

---

### Requirement: Health Monitoring & Fault Recovery

The system SHALL implement health monitoring at all three layers with automatic recovery.

#### Layer 3 — Worker Health

**Detection mechanisms**:
1. **Process timeout**: `timeout` command wraps each worker invocation. Configurable via `health.timeout_minutes` per task.
2. **Git activity monitoring**: Orchestrator periodically checks `git -C {worktree} log --since="{N} minutes ago" --oneline`. No recent commits + process alive = potential stall.
3. **Heartbeat via MCP**: Workers call `report_progress` MCP tool periodically. Orchestrator checks `health.last_activity` timestamp. Staleness threshold configurable.

**Recovery actions**:
1. Kill stale worker process (SIGTERM, then SIGKILL after 10s)
2. Set task status to `timeout`
3. Invoke manager with failure context
4. Manager decides: retry (increment attempts), split task, reassign, or escalate

```bash
# Orchestrator health check loop (runs in background)
while true; do
  for task_file in .collab/tasks/task-*.json; do
    status=$(jq -r .status "$task_file")
    pid=$(jq -r .worker_pid "$task_file")

    [ "$status" != "in_progress" ] && continue

    # Check 1: Process still alive?
    if ! kill -0 "$pid" 2>/dev/null; then
      # Process died without orchestrator noticing
      handle_unexpected_death "$task_file"
      continue
    fi

    # Check 2: Git activity?
    worktree=$(jq -r .worktree "$task_file")
    last_commit=$(git -C "$worktree" log -1 --format=%ct 2>/dev/null || echo 0)
    now=$(date +%s)
    stale_threshold=$(($(jq -r .health.timeout_minutes "$task_file") * 60))

    if [ $((now - last_commit)) -gt $stale_threshold ]; then
      handle_stale_worker "$task_file" "$pid"
    fi
  done
  sleep 60
done
```

#### Scenario: Worker process timeout
- **WHEN** a worker process exceeds its configured timeout
- **THEN** the orchestrator kills it, sets status to `timeout`, and invokes the manager for recovery decision

#### Scenario: Worker stall detection
- **WHEN** a worker process is alive but has no git commits for longer than the staleness threshold
- **THEN** the orchestrator treats it as a stall, kills the process, and triggers manager recovery

#### Scenario: Worker retry with attempt limit
- **WHEN** a worker fails and attempts < max_attempts
- **THEN** the manager may request a retry with adjusted prompt (e.g., narrower scope, more guidance)
- **WHEN** attempts >= max_attempts
- **THEN** the manager escalates to human

---

#### Layer 2 — Manager Health

**Detection**: Orchestrator wraps every manager invocation with a shorter timeout (`manager_timeout_minutes`).

**Recovery**:
1. If manager times out → retry once with simplified prompt
2. If retry fails → escalate to human via `human-calls/`
3. Write manager failure to `.collab/events/manager-failure-{timestamp}.json`

**Defensive design**:
- Manager prompt always includes: "Respond with JSON. If you cannot determine the right action, respond with `{\"action\": \"escalate\", \"reason\": \"...\"}` rather than guessing."
- Orchestrator validates manager JSON output. If invalid → retry with "Your previous response was not valid JSON. Please respond with valid JSON only."

#### Scenario: Manager timeout
- **WHEN** a manager invocation exceeds `manager_timeout_minutes`
- **THEN** the orchestrator kills it, retries once with a simplified prompt
- **WHEN** the retry also times out
- **THEN** the orchestrator writes a human-call requesting manual intervention

#### Scenario: Manager invalid output
- **WHEN** the manager returns non-JSON or invalid action
- **THEN** the orchestrator retries with an error correction prompt (max 2 retries)

---

#### Layer 1 — Orchestrator Health (Watchdog)

**Implementation**: A companion watchdog process (Layer 0) monitors the orchestrator.

```bash
# Watchdog (started alongside orchestrator)
ORCH_PID=$1
MAX_RESTARTS=3
restarts=0

while true; do
  if ! kill -0 "$ORCH_PID" 2>/dev/null; then
    restarts=$((restarts + 1))
    if [ $restarts -gt $MAX_RESTARTS ]; then
      # Write human-call for manual intervention
      cat > human-calls/$(date +%Y%m%d-%H%M%S)-orchestrator-failure.md << 'EOF'
## Request: Orchestrator Repeated Failure
**Type**: action
**Urgency**: high

The collaboration orchestrator has failed $MAX_RESTARTS times.
Please investigate and restart manually.

### Response
<!-- Human: write your response below -->
EOF
      exit 1
    fi

    echo "[watchdog] Orchestrator died. Restart #$restarts..."
    # Restart orchestrator — it recovers state from .collab/
    se3 collab --resume &
    ORCH_PID=$!
  fi
  sleep 30
done
```

**Key property**: Orchestrator is **stateless** — all state lives in `.collab/` files. Restart means re-read state and resume the event loop. No in-memory state is lost.

#### Scenario: Orchestrator crash and recovery
- **WHEN** the orchestrator process dies unexpectedly
- **THEN** the watchdog detects it within 30 seconds, restarts it, and the orchestrator reconstructs state from `.collab/tasks/*.json`

#### Scenario: Repeated orchestrator failure
- **WHEN** the orchestrator crashes more than `MAX_RESTARTS` times
- **THEN** the watchdog writes a human-call and exits, preventing an infinite restart loop

---

### Requirement: Manager Decision Protocol

The manager SHALL communicate with the orchestrator via structured JSON responses.

**Manager invocation pattern**:
```bash
result=$(claude -p "$MANAGER_PROMPT" \
  --model "$MANAGER_MODEL" \
  --output-format json \
  --max-turns 30 \
  --workdir "$PROJECT_ROOT")
```

**Manager response schema**:
```json
{
  "action": "plan|merge|reject|retry|split|escalate|complete",
  "tasks": [...],           // For "plan" and "split"
  "target_task": "task-001", // For "merge", "reject", "retry"
  "merge_branch": "collab/feature-x",  // For "merge"
  "retry_prompt": "...",     // For "retry" (adjusted prompt)
  "reason": "...",           // For "reject", "escalate"
  "summary": "..."           // Always present — human-readable summary
}
```

**Decision types**:
| Action | When | Orchestrator Response |
|--------|------|----------------------|
| `plan` | Initial planning | Create task files, spawn workers |
| `merge` | Worker done, code approved | `git merge`, cleanup worktree |
| `reject` | Worker done, code needs changes | Set task to pending, re-spawn with feedback |
| `retry` | Worker failed, retryable | Re-spawn with adjusted prompt |
| `split` | Task too large | Create sub-tasks, spawn workers |
| `escalate` | Cannot resolve | Write human-call, pause task |
| `complete` | All tasks done | Cleanup, generate summary |

#### Scenario: Manager plans initial tasks
- **WHEN** invoked with "plan" event
- **THEN** returns `{"action": "plan", "tasks": [...]}` with task definitions for each parallel work unit

#### Scenario: Manager merges completed work
- **WHEN** invoked with "review" event for a completed task
- **THEN** examines the branch diff, runs analysis, and returns either `merge`, `reject`, or `split`

---

### Requirement: Worktree Lifecycle Management

The system SHALL manage git worktrees for branch isolation.

**Worktree directory**: `.worktrees/` in project root (gitignored)

**Lifecycle**:
```bash
# Create
git worktree add .worktrees/feature-x -b collab/feature-x

# Worker operates in .worktrees/feature-x/
# All commits go to branch collab/feature-x

# After merge
git worktree remove .worktrees/feature-x
git branch -d collab/feature-x
```

**Branch naming convention**: `collab/{task-id}-{short-description}`

#### Scenario: Worktree creation for new task
- **WHEN** the orchestrator spawns a worker for a task
- **THEN** it first creates a git worktree on a new branch based on the current base branch

#### Scenario: Worktree cleanup after merge
- **WHEN** the manager approves a merge and it succeeds
- **THEN** the orchestrator removes the worktree and deletes the branch

#### Scenario: Worktree preserved on failure
- **WHEN** a task fails and is escalated to human
- **THEN** the worktree is preserved so the human can inspect the state

---

### Requirement: Merge Strategy & Conflict Resolution

The system SHALL handle merge conflicts through manager-driven resolution.

**Merge order**: Manager specifies merge priority when multiple branches are ready. Typically: foundational changes first, dependent changes later.

**Conflict handling**:
1. Orchestrator attempts `git merge --no-ff`
2. If conflict → abort merge, invoke manager with conflict details
3. Manager decides:
   - Rebase one branch on the other and re-test
   - Manually resolve (manager has file editing capability via claude -p)
   - Escalate to human

#### Scenario: Clean merge
- **WHEN** manager approves merge and no conflicts exist
- **THEN** orchestrator performs `git merge --no-ff collab/{branch}` with a descriptive commit message

#### Scenario: Merge conflict
- **WHEN** a merge attempt produces conflicts
- **THEN** orchestrator aborts, invokes manager with `git diff` conflict markers, and manager resolves or escalates

---

## Integration with SE3 Framework

### CLI Command

```bash
# Start a collaboration session
se3 collab "Implement auth, caching, and API rate limiting"

# Resume a crashed/paused session
se3 collab --resume

# Check collaboration status
se3 collab --status

# Abort and cleanup
se3 collab --abort
```

### Relationship to Existing Specs

- **human-as-mcp**: Collaboration MCP server implements the same human-call protocol. Human calls from workers/manager go through `human-calls/` directory.
- **session-protocol**: Each worker follows the session protocol within its worktree (read specs, implement, test, commit). Manager follows session protocol for the main worktree.
- **requirement-intake**: The initial collaboration objective is a human-initiated requirement. Manager decomposes it into tasks following the intake spec.

### Configuration (se3.config.yaml extension)

```yaml
collab:
  max_parallel_workers: 3
  worker_timeout_minutes: 60
  manager_timeout_minutes: 15
  health_check_interval_seconds: 60
  stale_threshold_minutes: 30
  max_worker_attempts: 3
  max_manager_retries: 2
  max_orchestrator_restarts: 3
  manager_model: "sonnet"
  worker_model: "sonnet"
  worktree_dir: ".worktrees"
  collab_dir: ".collab"
```
