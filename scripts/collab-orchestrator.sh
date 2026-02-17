#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# SE3 Git Worktree Collaboration Orchestrator
# =============================================================================
# Layer 1: Pure bash event loop. Zero AI token cost while idle.
# Manages worker/manager process lifecycles and routes events.
# All state persisted in .collab/ — fully restartable.
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${PROJECT_ROOT:-$(pwd)}"

# --- Setup jq (system or Python fallback) ---
export JQ_CMD="jq"
if ! command -v jq &>/dev/null; then
  JQ_CMD="python3 $SCRIPT_DIR/jq-complete.py"
fi

# --- Configuration (defaults, overridden by se3.config.yaml) ---
MAX_PARALLEL_WORKERS=3
WORKER_TIMEOUT_MINUTES=60
MANAGER_TIMEOUT_MINUTES=15
HEALTH_CHECK_INTERVAL=60
STALE_THRESHOLD_MINUTES=30
MAX_WORKER_ATTEMPTS=3
MAX_MANAGER_RETRIES=2
MANAGER_MODEL="sonnet"
WORKER_MODEL="sonnet"
WORKTREE_DIR="$PROJECT_ROOT/.worktrees"
COLLAB_DIR="$PROJECT_ROOT/.collab"
HUMAN_CALLS_DIR="$PROJECT_ROOT/human-calls"
HUMAN_CALL_LANGUAGE="en"  # Default language for human call messages
MCP_CONFIG=""  # Set if collab MCP server config exists
WORKER_RULES=""
MANAGER_RULES=""
MOCK_MODE="${MOCK_MODE:-false}"  # Set to true for testing with mock-claude

# --- Claude command resolution via se3 claude-cmd ---
resolve_claude_cmd() {
  # Returns the highest-priority configured Claude command.
  # Falls back to "claude" if se3 claude-cmd is unavailable.
  if [ "$MOCK_MODE" = "true" ]; then
    echo "$SCRIPT_DIR/mock-claude"
    return
  fi
  local resolved
  resolved=$(se3 claude-cmd -p "$PROJECT_ROOT" 2>/dev/null) || resolved="claude"
  echo "${resolved:-claude}"
}

resolve_next_claude_cmd() {
  # Returns the next Claude command after the given one.
  # Empty string if no more commands available.
  local current="$1"
  if [ "$MOCK_MODE" = "true" ]; then
    echo ""
    return
  fi
  se3 claude-cmd --next "$current" -p "$PROJECT_ROOT" 2>/dev/null || echo ""
}

is_usage_limit() {
  # Check if output indicates a usage/rate limit.
  local output="$1"
  local lower
  lower=$(echo "$output" | tr '[:upper:]' '[:lower:]')
  case "$lower" in
    *"usage limit"*|*"rate limit"*|*"too many requests"*|*"rate_limit"*|*"overloaded"*|*"capacity"*)
      return 0 ;;
  esac
  return 1
}

# --- Color output ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info()  { echo -e "${BLUE}[orch]${NC} $*" >&2; }
log_ok()    { echo -e "${GREEN}[orch]${NC} $*" >&2; }
log_warn()  { echo -e "${YELLOW}[orch]${NC} $*" >&2; }
log_error() { echo -e "${RED}[orch]${NC} $*" >&2; }

# --- Load config from se3.config.yaml if available ---
load_config() {
  local config="$PROJECT_ROOT/se3.config.yaml"
  if [ -f "$config" ] && command -v python3 &>/dev/null; then
    eval "$(python3 -c "
import yaml, sys
try:
    with open('$config') as f:
        cfg = yaml.safe_load(f)
        c = cfg.get('collab', {})
    for k, v in c.items():
        print(f'{k.upper()}=\"{v}\"')
    # Load human_call language setting
    hc = cfg.get('human_call', {})
    if 'language' in hc:
        print(f'HUMAN_CALL_LANGUAGE=\"{hc[\"language\"]}\"')
except:
    pass
")"
  fi

  # Load role-specific rules
  local rules_dir="$SCRIPT_DIR"
  if [ -f "$rules_dir/rules-worker.md" ]; then
    WORKER_RULES=$(cat "$rules_dir/rules-worker.md")
  else
    log_warn "Worker rules not found at $rules_dir/rules-worker.md"
    WORKER_RULES="You are a worker agent. Implement the task, run tests, commit."
  fi
  if [ -f "$rules_dir/rules-manager.md" ]; then
    MANAGER_RULES=$(cat "$rules_dir/rules-manager.md")
  else
    log_warn "Manager rules not found at $rules_dir/rules-manager.md"
    MANAGER_RULES="You are a manager agent. Respond with valid JSON."
  fi

  # Auto-detect MCP config
  if [ -z "$MCP_CONFIG" ] && [ -f "$SCRIPT_DIR/mcp-collab/mcp-config.json" ]; then
    MCP_CONFIG="$SCRIPT_DIR/mcp-collab/mcp-config.json"
  fi
}

# --- Ensure directories exist ---
init_dirs() {
  mkdir -p "$COLLAB_DIR/tasks" "$COLLAB_DIR/logs" "$COLLAB_DIR/events"
  mkdir -p "$WORKTREE_DIR" "$HUMAN_CALLS_DIR"

  # Add to gitignore if not already
  for entry in ".worktrees/" ".collab/"; do
    grep -qxF "$entry" "$PROJECT_ROOT/.gitignore" 2>/dev/null || \
      echo "$entry" >> "$PROJECT_ROOT/.gitignore"
  done
}

# --- PID file management ---
write_pid() {
  echo $$ > "$COLLAB_DIR/orchestrator.pid"
}

check_already_running() {
  local pidfile="$COLLAB_DIR/orchestrator.pid"
  if [ -f "$pidfile" ]; then
    local old_pid
    old_pid=$(cat "$pidfile")
    if kill -0 "$old_pid" 2>/dev/null; then
      log_error "Orchestrator already running (PID $old_pid). Use --abort to stop it."
      exit 1
    fi
    log_warn "Stale PID file found. Previous orchestrator (PID $old_pid) is dead."
  fi
}

# =============================================================================
# Manager Invocation
# =============================================================================

invoke_manager() {
  local event_type="$1"
  local event_context="$2"
  local attempt=0
  local result=""

  # Build the full manager prompt
  local tasks_summary
  tasks_summary=$(summarize_tasks)

  local prompt="$MANAGER_RULES

---

## Current State
Project root: $PROJECT_ROOT
Base branch: $(cat "$COLLAB_DIR/config.json" 2>/dev/null | $JQ_CMD -r '.base_branch // "master"')

## All Tasks
$tasks_summary

## Event
Type: $event_type
Context:
$event_context

## Instructions
Analyze the event and decide the next action. Respond ONLY with valid JSON matching this schema:
{
  \"action\": \"plan|merge|reject|retry|split|escalate|complete\",
  \"tasks\": [...],
  \"target_task\": \"task-id\",
  \"merge_branch\": \"branch-name\",
  \"retry_prompt\": \"adjusted prompt for retry\",
  \"reason\": \"explanation\",
  \"summary\": \"human-readable summary of decision\"
}

Rules:
- For 'plan': include full task definitions in 'tasks' array
- For 'merge': set target_task and merge_branch
- For 'reject': set target_task and reason (becomes feedback for worker retry)
- For 'retry': set target_task and retry_prompt
- For 'split': set target_task and new sub-tasks in 'tasks'
- For 'escalate': set reason (will be sent to human)
- For 'complete': when all tasks are merged and done
- If unsure, use 'escalate' rather than guessing"

  local timeout_seconds=$((MANAGER_TIMEOUT_MINUTES * 60))

  # Use Python launcher with activity-based monitoring
  local logfile="$COLLAB_DIR/logs/manager-$(date +%Y%m%d-%H%M%S).log"
  # Use unique temp files to avoid race conditions with multiple managers
  local tmp_id="$$-$(date +%s%N)"
  local context_file="$COLLAB_DIR/.manager-context-${tmp_id}.txt"
  local tasks_file="$COLLAB_DIR/.manager-tasks-${tmp_id}.txt"
  local rules_file="$SCRIPT_DIR/rules-manager.md"

  # Write context and tasks to temp files
  printf '%s\n' "$event_context" > "$context_file" || { log_error "Failed to write context file"; return 1; }
  printf '%s\n' "$tasks_summary" > "$tasks_file" || { log_error "Failed to write tasks file"; rm -f "$context_file"; return 1; }

  local base_branch
  base_branch=$($JQ_CMD -r '.base_branch // "master"' "$COLLAB_DIR/config.json" 2>/dev/null)

  local launcher_args=(
    "$event_type"
    "@$context_file"
    --log-file "$logfile"
    --wall-timeout "$timeout_seconds"
    --inactivity-timeout "120"
    --project-root "$PROJECT_ROOT"
    --base-branch "$base_branch"
    --tasks-file "$tasks_file"
  )
  [ -f "$rules_file" ] && launcher_args+=(--rules-file "$rules_file")
  [ -n "$MANAGER_MODEL" ] && launcher_args+=(--model "$MANAGER_MODEL")
  [ -n "$MCP_CONFIG" ] && launcher_args+=(--mcp-config "$MCP_CONFIG")
  [ -f "$PROJECT_ROOT/se3.config.yaml" ] && launcher_args+=(--config-file "$PROJECT_ROOT/se3.config.yaml")

  # Run manager with launcher (handles command switching internally)
  log_info "Invoking manager: event=$event_type"
  # Launcher handles its own logging, capture stderr separately for errors
  local stderr_file="$COLLAB_DIR/.manager-stderr-${tmp_id}.log"
  raw_result=$(python3 "$SCRIPT_DIR/collab-manager-launcher.py" "${launcher_args[@]}" 2>"$stderr_file")
  local exit_code=$?

  # Clean up temp files
  rm -f "$context_file" "$tasks_file" "$stderr_file"

  if [ $exit_code -eq 0 ] && [ -n "$raw_result" ]; then
    # Parse JSON from launcher output
    result=$(echo "$raw_result" | python3 -c "
import json, sys, re
raw = sys.stdin.read()
try:
    # Try to parse as JSON directly
    obj = json.loads(raw)
    if 'action' in obj:
        print(json.dumps(obj))
        sys.exit(0)
except:
    pass
# Try to extract JSON object from text
m = re.search(r'\{.*\}', raw, re.DOTALL)
if m:
    try:
        obj = json.loads(m.group(0))
        if 'action' in obj:
            print(json.dumps(obj))
            sys.exit(0)
    except:
        pass
sys.exit(1)
" 2>/dev/null)

    if [ $? -eq 0 ] && [ -n "$result" ]; then
      local action
      action=$(echo "$result" | $JQ_CMD -r '.action')
      log_ok "Manager responded: $action"
      echo "$result"
      return 0
    fi
  fi

  # Check if we got error_max_turns in raw output
  if echo "$raw_result" | grep -q '"subtype":"error_max_turns"'; then
    log_warn "Manager hit max-turns limit"
  elif [ $exit_code -eq 124 ]; then
    log_warn "Manager timed out or was inactive"
  else
    log_warn "Manager failed with exit code $exit_code"
  fi

  # All commands exhausted — escalate to human
  log_error "Manager failed (all commands exhausted). Escalating to human."
  escalate_to_human "Manager Failure" \
    "The manager agent failed after trying all available Claude commands.
Event type: $event_type
Event context: $event_context
Last output: $(echo "$raw_result" | head -c 1000)
Exit code: $exit_code"

  echo '{"action": "escalate", "reason": "manager_failure"}'
  return 1
}

# =============================================================================
# Worker Management
# =============================================================================

spawn_worker() {
  local task_id="$1"
  local task_file="$COLLAB_DIR/tasks/${task_id}.json"

  local branch worktree task_prompt prompt timeout_min
  branch=$($JQ_CMD -r '.branch' "$task_file")
  worktree=$($JQ_CMD -r '.worktree' "$task_file")
  task_prompt=$($JQ_CMD -r '.prompt' "$task_file")
  timeout_min=$($JQ_CMD -r '.health.timeout_minutes // 60' "$task_file")

  # Inject worker rules before the task-specific prompt
  prompt="$WORKER_RULES

---

## Your Task (ID: $task_id)

$task_prompt"

  # Resolve worktree to absolute path
  [ "${worktree:0:1}" != "/" ] && worktree="$PROJECT_ROOT/$worktree"

  # Create worktree if not exists
  if [ ! -d "$worktree" ]; then
    local base_branch
    base_branch=$($JQ_CMD -r '.base_branch // "master"' "$COLLAB_DIR/config.json")
    log_info "Creating worktree: $worktree (branch: $branch from $base_branch)"
    git -C "$PROJECT_ROOT" worktree add "$worktree" -b "$branch" "$base_branch"
  fi

  # Update task status
  update_task "$task_id" '.status = "in_progress" | .started_at = now | .health.attempts += 1'

  local wall_timeout=$((timeout_min * 60))
  local logfile="$COLLAB_DIR/logs/worker-${task_id}-$(date +%Y%m%d-%H%M%S).log"

  # Write prompt to temp file for launcher
  local prompt_file="$COLLAB_DIR/tasks/.prompt-${task_id}.txt"
  echo "$prompt" > "$prompt_file"

  # Build launcher args
  local launcher_args=(
    "$task_id"
    "$worktree"
    "@$prompt_file"
    --log-file "$logfile"
    --wall-timeout "$wall_timeout"
    --inactivity-timeout "300"
    --project-root "$PROJECT_ROOT"
  )
  [ -n "$WORKER_MODEL" ] && launcher_args+=(--model "$WORKER_MODEL")
  [ -n "$MCP_CONFIG" ] && launcher_args+=(--mcp-config "$MCP_CONFIG")
  [ -f "$PROJECT_ROOT/se3.config.yaml" ] && launcher_args+=(--config-file "$PROJECT_ROOT/se3.config.yaml")

  # Spawn worker using Python launcher with activity monitoring
  # Launcher handles its own logging internally
  (
    cd "$worktree"
    python3 "$SCRIPT_DIR/collab-worker-launcher.py" "${launcher_args[@]}"
    local worker_exit=$?
    # Clean up prompt file
    rm -f "$prompt_file"
    exit $worker_exit
  ) &

  local pid=$!
  update_task "$task_id" ".worker_pid = $pid"

  log_info "Spawned worker for $task_id (PID $pid, timeout ${timeout_min}m, inactivity 5m)"
}

wait_for_worker() {
  # Wait for any child worker to exit
  # Returns the PID that exited (and sets exit_code global)
  local pid
  exit_code=0
  if wait -n -p pid 2>/dev/null; then
    exit_code=$?
  else
    exit_code=$?
  fi
  echo "$pid"
}

count_active_workers() {
  local count=0
  for task_file in "$COLLAB_DIR/tasks"/task-*.json; do
    [ -f "$task_file" ] || continue
    local status pid
    status=$($JQ_CMD -r '.status' "$task_file")
    pid=$($JQ_CMD -r '.worker_pid // 0' "$task_file")
    if [ "$status" = "in_progress" ] && [ "$pid" != "0" ] && [ "$pid" != "null" ] && kill -0 "$pid" 2>/dev/null; then
      count=$((count + 1))
    fi
  done
  echo "$count"
}

find_task_by_pid() {
  local target_pid="$1"
  for task_file in "$COLLAB_DIR/tasks"/task-*.json; do
    [ -f "$task_file" ] || continue
    local pid
    pid=$($JQ_CMD -r '.worker_pid // 0' "$task_file")
    if [ "$pid" = "$target_pid" ]; then
      $JQ_CMD -r '.id' "$task_file"
      return 0
    fi
  done
  return 1
}

# =============================================================================
# Task State Management
# =============================================================================

update_task() {
  local task_id="$1"
  local jq_expr="$2"
  local task_file="$COLLAB_DIR/tasks/${task_id}.json"

  local tmp="${task_file}.tmp"
  $JQ_CMD "$jq_expr" "$task_file" > "$tmp" && mv "$tmp" "$task_file"
}

summarize_tasks() {
  for task_file in "$COLLAB_DIR/tasks"/task-*.json; do
    [ -f "$task_file" ] || continue
    $JQ_CMD -r '[.id, .status, .title, .branch] | @tsv' "$task_file"
  done
}

get_pending_tasks() {
  for task_file in "$COLLAB_DIR/tasks"/task-*.json; do
    [ -f "$task_file" ] || continue
    local status
    status=$($JQ_CMD -r '.status' "$task_file")
    if [ "$status" = "pending" ]; then
      $JQ_CMD -r '.id' "$task_file"
    fi
  done
}

all_tasks_terminal() {
  # Returns 0 if all tasks are in terminal state (done, failed, escalated)
  # Returns 1 if no tasks exist (not terminal — still waiting for plan)
  local has_tasks=false
  for task_file in "$COLLAB_DIR/tasks"/task-*.json; do
    [ -f "$task_file" ] || continue
    has_tasks=true
    local status
    status=$($JQ_CMD -r '.status' "$task_file")
    case "$status" in
      done|failed|escalated) continue ;;
      *) return 1 ;;
    esac
  done
  # If no tasks exist at all, not terminal
  $has_tasks || return 1
  return 0
}

has_pending_human_calls() {
  for call_file in "$HUMAN_CALLS_DIR"/*.md; do
    [ -f "$call_file" ] || continue
    if grep -q "^status: pending" "$call_file"; then
      return 0
    fi
  done
  return 1
}

# =============================================================================
# Health Monitoring
# =============================================================================

health_check_workers() {
  local now
  now=$(date +%s)

  for task_file in "$COLLAB_DIR/tasks"/task-*.json; do
    [ -f "$task_file" ] || continue
    local status pid worktree stale_min last_activity
    status=$($JQ_CMD -r '.status' "$task_file")
    pid=$($JQ_CMD -r '.worker_pid // 0' "$task_file")

    [ "$status" != "in_progress" ] && continue
    [ "$pid" = "0" ] || [ "$pid" = "null" ] && continue

    # Check if process is alive
    if ! kill -0 "$pid" 2>/dev/null; then
      local task_id
      task_id=$($JQ_CMD -r '.id' "$task_file")
      log_warn "Worker $task_id (PID $pid) died unexpectedly"
      handle_worker_exit "$task_id" 1
      continue
    fi

    # Check activity for staleness based on last_activity timestamp
    stale_min=$STALE_THRESHOLD_MINUTES

    # Get last_activity or fall back to started_at
    last_activity=$($JQ_CMD -r '.health.last_activity // .started_at // ""' "$task_file")
    if [ -z "$last_activity" ] || [ "$last_activity" = "null" ]; then
      log_warn "Task $task_file has no last_activity or started_at, skipping staleness check"
      continue
    fi

    # Convert ISO8601 to timestamp (seconds since epoch)
    local last_activity_ts
    last_activity_ts=$(date -d "$last_activity" +%s 2>/dev/null || echo 0)
    if [ "$last_activity_ts" -eq 0 ]; then
      log_warn "Failed to parse last_activity: $last_activity"
      continue
    fi

    local elapsed_min=$(( (now - last_activity_ts) / 60 ))
    if [ "$elapsed_min" -gt "$stale_min" ]; then
      local task_id
      task_id=$($JQ_CMD -r '.id' "$task_file")
      log_warn "Worker $task_id stale (${elapsed_min}m since last activity). Killing."
      kill -TERM "$pid" 2>/dev/null
      sleep 10
      kill -KILL "$pid" 2>/dev/null || true
      update_task "$task_id" '.status = "timeout" | .completed_at = now'
      handle_worker_exit "$task_id" 124
    fi
  done
}

# =============================================================================
# Event Handlers
# =============================================================================

handle_worker_exit() {
  local task_id="$1"
  local exit_code="${2:-}"

  # Read exit code from file if not provided
  if [ -z "$exit_code" ]; then
    local exitcode_file="$COLLAB_DIR/tasks/.exitcode-${task_id}"
    if [ -f "$exitcode_file" ]; then
      exit_code=$(cat "$exitcode_file")
      rm -f "$exitcode_file"
    else
      exit_code=1
    fi
  fi

  local task_file="$COLLAB_DIR/tasks/${task_id}.json"
  update_task "$task_id" ".worker_exit_code = $exit_code | .completed_at = now"

  if [ "$exit_code" = "0" ]; then
    # Success — ask manager to review
    update_task "$task_id" '.status = "done"'
    local branch
    branch=$($JQ_CMD -r '.branch' "$task_file")

    # Collect branch diff for manager review
    local diff_summary
    diff_summary=$(git -C "$PROJECT_ROOT" diff "$($JQ_CMD -r .base_branch "$COLLAB_DIR/config.json")...$branch" --stat 2>/dev/null || echo "(no diff available)")

    local worker_log
    local latest_log
    latest_log=$(ls -t "$COLLAB_DIR/logs/worker-${task_id}-"*.log 2>/dev/null | head -1)
    worker_log=$(tail -100 "$latest_log" 2>/dev/null || echo "(no log)")

    local manager_result
    manager_result=$(invoke_manager "review" "Task $task_id completed successfully on branch $branch.

Diff summary:
$diff_summary

Worker log (last 100 lines):
$worker_log")

    process_manager_decision "$manager_result"

  elif [ "$exit_code" = "2" ]; then
    # Blocked — worker needs human/manager input
    update_task "$task_id" '.status = "blocked"'
    local blocked_reason
    blocked_reason=$($JQ_CMD -r '.blocked_reason // "unknown"' "$task_file")

    local manager_result
    manager_result=$(invoke_manager "blocked" "Task $task_id is blocked.
Reason: $blocked_reason")

    process_manager_decision "$manager_result"

  elif [ "$exit_code" = "124" ]; then
    # Timeout
    update_task "$task_id" '.status = "timeout"'
    local attempts max_attempts
    attempts=$($JQ_CMD -r '.health.attempts' "$task_file")
    max_attempts=$($JQ_CMD -r '.health.max_attempts' "$task_file")

    local manager_result
    manager_result=$(invoke_manager "timeout" "Task $task_id timed out.
Attempts: $attempts / $max_attempts")

    process_manager_decision "$manager_result"

  else
    # General failure
    update_task "$task_id" '.status = "failed"'
    local worker_log latest_log
    latest_log=$(ls -t "$COLLAB_DIR/logs/worker-${task_id}-"*.log 2>/dev/null | head -1)
    worker_log=$(tail -50 "$latest_log" 2>/dev/null || echo "(no log)")

    local manager_result
    manager_result=$(invoke_manager "failure" "Task $task_id failed with exit code $exit_code.

Worker log (last 50 lines):
$worker_log")

    process_manager_decision "$manager_result"
  fi
}

process_manager_decision() {
  local decision="$1"
  local action
  action=$(echo "$decision" | $JQ_CMD -r '.action')

  case "$action" in
    merge)
      do_merge "$decision"
      ;;
    reject)
      do_reject "$decision"
      ;;
    retry)
      do_retry "$decision"
      ;;
    split)
      do_split "$decision"
      ;;
    plan)
      do_plan "$decision"
      ;;
    escalate)
      do_escalate "$decision"
      ;;
    complete)
      do_complete "$decision"
      ;;
    *)
      log_error "Unknown manager action: $action"
      ;;
  esac
}

# =============================================================================
# Manager Decision Executors
# =============================================================================

do_plan() {
  local decision="$1"
  local created=0

  # Extract tasks using Python (jq-complete doesn't support length/iteration well)
  # Write decision to temp file for reliable parsing
  local tmp_decision="$COLLAB_DIR/tasks/.tmp-decision.json"
  echo "$decision" > "$tmp_decision"

  # Use Python to extract and write individual task files
  python3 -c "
import json, sys
with open('$tmp_decision') as f:
    data = json.load(f)
tasks = data.get('tasks', [])
if not tasks:
    task = data.get('task')
    if task:
        tasks = [task]
for task in tasks:
    tid = task.get('id')
    if tid:
        with open('$COLLAB_DIR/tasks/' + tid + '.json', 'w') as out:
            json.dump(task, out, ensure_ascii=False)
        print(tid + '|' + task.get('title', ''))
" 2>/dev/null | while IFS='|' read -r task_id title; do
    log_info "Created task: $task_id — $title"
    created=$((created + 1))
  done

  rm -f "$tmp_decision"

  # Verify tasks were created
  local task_files
  task_files=$(ls "$COLLAB_DIR/tasks"/task-*.json 2>/dev/null | wc -l)
  if [ "$task_files" -eq 0 ]; then
    log_warn "No tasks found in plan decision"
    log_warn "Decision was: $decision"
    return 1
  fi

  log_info "Created $task_files task(s) from plan"
}

do_merge() {
  local decision="$1"
  local task_id branch
  task_id=$(echo "$decision" | $JQ_CMD -r '.target_task')
  branch=$(echo "$decision" | $JQ_CMD -r '.merge_branch')
  local base_branch
  base_branch=$($JQ_CMD -r '.base_branch // "master"' "$COLLAB_DIR/config.json")

  log_info "Merging $branch into $base_branch..."

  if git -C "$PROJECT_ROOT" merge --no-ff "$branch" -m "collab: merge $task_id ($branch)"; then
    log_ok "Merged $branch successfully"
    update_task "$task_id" '.status = "done" | .review.status = "approved"'
    cleanup_worktree "$task_id"
  else
    log_warn "Merge conflict on $branch"
    git -C "$PROJECT_ROOT" merge --abort

    # Get conflict details and ask manager
    local conflict_result
    conflict_result=$(invoke_manager "merge_conflict" "Merge conflict when merging $branch into $base_branch.
Task: $task_id

Conflicting changes:
$(git -C "$PROJECT_ROOT" merge --no-commit "$branch" 2>&1; git -C "$PROJECT_ROOT" diff --name-only --diff-filter=U 2>/dev/null; git -C "$PROJECT_ROOT" merge --abort 2>/dev/null)")

    process_manager_decision "$conflict_result"
  fi
}

do_reject() {
  local decision="$1"
  local task_id reason
  task_id=$(echo "$decision" | $JQ_CMD -r '.target_task')
  reason=$(echo "$decision" | $JQ_CMD -r '.reason')

  log_warn "Rejected $task_id: $reason"

  # Get current prompt and append feedback
  local task_file="$COLLAB_DIR/tasks/${task_id}.json"
  local original_prompt
  original_prompt=$($JQ_CMD -r '.prompt' "$task_file")

  update_task "$task_id" "
    .status = \"pending\" |
    .review.status = \"changes_requested\" |
    .review.comments = $(echo "$reason" | $JQ_CMD -Rs .) |
    .prompt = $(echo "${original_prompt}

IMPORTANT FEEDBACK FROM REVIEWER:
${reason}
Please address this feedback in your implementation." | $JQ_CMD -Rs .)
  "
}

do_retry() {
  local decision="$1"
  local task_id retry_prompt
  task_id=$(echo "$decision" | $JQ_CMD -r '.target_task')
  retry_prompt=$(echo "$decision" | $JQ_CMD -r '.retry_prompt // empty')

  local task_file="$COLLAB_DIR/tasks/${task_id}.json"
  local attempts max_attempts
  attempts=$($JQ_CMD -r '.health.attempts' "$task_file")
  max_attempts=$($JQ_CMD -r '.health.max_attempts' "$task_file")

  if [ "$attempts" -ge "$max_attempts" ]; then
    log_error "Task $task_id exceeded max attempts ($max_attempts). Escalating."
    escalate_to_human "Task Exceeded Max Attempts" \
      "Task $task_id has failed $attempts times (max: $max_attempts).
Last prompt: $($JQ_CMD -r '.prompt' "$task_file")"
    update_task "$task_id" '.status = "escalated"'
    return
  fi

  if [ -n "$retry_prompt" ]; then
    update_task "$task_id" ".prompt = $(echo "$retry_prompt" | $JQ_CMD -Rs .) | .status = \"pending\""
  else
    update_task "$task_id" '.status = "pending"'
  fi

  log_info "Retry queued for $task_id (attempt $((attempts+1))/$max_attempts)"
}

do_split() {
  local decision="$1"
  local task_id
  task_id=$(echo "$decision" | $JQ_CMD -r '.target_task')

  # Mark original task as superseded
  update_task "$task_id" '.status = "done" | .result_summary = "split into sub-tasks"'
  cleanup_worktree "$task_id"

  # Create sub-tasks (same as plan)
  do_plan "$decision"
}

do_escalate() {
  local decision="$1"
  local reason
  reason=$(echo "$decision" | $JQ_CMD -r '.reason')
  local task_id
  task_id=$(echo "$decision" | $JQ_CMD -r '.target_task // "none"')

  [ "$task_id" != "none" ] && [ "$task_id" != "null" ] && \
    update_task "$task_id" '.status = "escalated"'

  escalate_to_human "Manager Escalation" "$reason"
}

do_complete() {
  local decision="$1"
  local summary
  summary=$(echo "$decision" | $JQ_CMD -r '.summary')
  log_ok "=== Collaboration Complete ==="
  log_ok "$summary"

  # Update session config
  $JQ_CMD '.status = "completed"' "$COLLAB_DIR/config.json" > "$COLLAB_DIR/config.json.tmp" && \
    mv "$COLLAB_DIR/config.json.tmp" "$COLLAB_DIR/config.json"
}

# =============================================================================
# Worktree Cleanup
# =============================================================================

cleanup_worktree() {
  local task_id="$1"
  local task_file="$COLLAB_DIR/tasks/${task_id}.json"
  local worktree branch
  worktree=$($JQ_CMD -r '.worktree' "$task_file")
  branch=$($JQ_CMD -r '.branch' "$task_file")

  [ "${worktree:0:1}" != "/" ] && worktree="$PROJECT_ROOT/$worktree"

  if [ -d "$worktree" ]; then
    git -C "$PROJECT_ROOT" worktree remove "$worktree" --force 2>/dev/null || true
    log_info "Removed worktree: $worktree"
  fi

  git -C "$PROJECT_ROOT" branch -d "$branch" 2>/dev/null || true
}

# =============================================================================
# Human Escalation
# =============================================================================

escalate_to_human() {
  local title="$1"
  local context="$2"
  local call_type="${3:-action}"
  local priority="${4:-high}"
  local filename
  local call_id
  call_id="$(date +%Y%m%d-%H%M%S)-$(echo "$title" | tr ' ' '-' | tr '[:upper:]' '[:lower:]' | tr -cd '[:alnum:]-' | head -c 30)"
  filename="${call_id}.md"

  # Generate template based on language setting
  local header_type header_urgency header_context header_tasks header_response prompt_text
  if [[ "$HUMAN_CALL_LANGUAGE" == zh* ]]; then
    header_type="类型"
    header_urgency="紧急程度"
    header_context="上下文"
    header_tasks="当前任务状态"
    header_response="回复"
    prompt_text="<!-- 人类：请在下方输入您的回复 -->"
  else
    header_type="Type"
    header_urgency="Urgency"
    header_context="Context"
    header_tasks="Current Task States"
    header_response="Response"
    prompt_text="<!-- Human: write your response below -->"
  fi

  # Generate ISO timestamp
  local created
  created="$(date -Iseconds)"

  # Create structured human call with frontmatter
  cat > "$HUMAN_CALLS_DIR/$filename" << EOF
---
id: ${call_id}
type: ${call_type}
priority: ${priority}
status: pending
created: ${created}
source: collab-orchestrator
language: ${HUMAN_CALL_LANGUAGE}
---

## Request: $title

**${header_type}**: ${call_type}
**${header_urgency}**: ${priority}
**Source**: collab-orchestrator

### ${header_context}
${context}

### ${header_tasks}
$(summarize_tasks)

### ${header_response}
${prompt_text}
EOF

  log_warn "Human call written: $HUMAN_CALLS_DIR/$filename"
}

# =============================================================================
# Main Event Loop
# =============================================================================

run_main() {
  local objective="$1"
  local resume="${2:-false}"
  local health_pid=""

  load_config
  init_dirs
  check_already_running
  write_pid

  # Trap cleanup - health_pid will be set later, use :- to handle unset
  trap 'log_info "Orchestrator shutting down..."; _pid="${health_pid:-}"; if [ -n "$_pid" ]; then kill "$_pid" 2>/dev/null || true; fi; unset _pid; rm -f "$COLLAB_DIR/orchestrator.pid"' EXIT

  if [ "$resume" = "true" ]; then
    log_info "Resuming collaboration session..."
    # State already in .collab/ — just re-enter the event loop
  else
    log_info "Starting collaboration session: $objective"

    # Clean up old tasks from previous sessions
    rm -f "$COLLAB_DIR/tasks"/task-*.json 2>/dev/null
    rm -f "$COLLAB_DIR/tasks"/.exitcode-* 2>/dev/null

    # Create session config
    local base_branch
    base_branch=$(git -C "$PROJECT_ROOT" branch --show-current)
    cat > "$COLLAB_DIR/config.json" << EOF
{
  "session_id": "collab-$(date +%Y%m%d-%H%M%S)",
  "objective": $(echo "$objective" | $JQ_CMD -Rs .),
  "base_branch": "$base_branch",
  "created_at": "$(date -Iseconds)",
  "max_parallel_workers": $MAX_PARALLEL_WORKERS,
  "status": "active"
}
EOF

    # Phase 1: Ask manager to plan
    local plan_result
    plan_result=$(invoke_manager "plan" "New collaboration session.
Objective: $objective
Base branch: $base_branch
Max parallel workers: $MAX_PARALLEL_WORKERS

Please analyze the objective and create task definitions. Each task should:
- Have a unique id (task-001, task-002, etc.)
- Have a branch name (collab/{short-description})
- Have a worktree path (.worktrees/{short-description})
- Have a detailed prompt for the worker agent
- Reference relevant spec files if they exist
- Have appropriate timeout settings

Respond with action 'plan' and include the full task array.")

    process_manager_decision "$plan_result"
  fi

  # Start health check in background
  (
    while true; do
      sleep "$HEALTH_CHECK_INTERVAL"
      health_check_workers
    done
  ) &
  health_pid=$!
  # Trap already set at function start, health_pid now has value

  # Phase 2: Main event loop
  while true; do
    # Check if all tasks are terminal
    if all_tasks_terminal; then
      # Ask manager if we're truly done
      local complete_result
      complete_result=$(invoke_manager "all_tasks_terminal" "All tasks are in terminal state.
$(summarize_tasks)

If all work is complete, respond with action 'complete'. If there are follow-up tasks needed, respond with action 'plan'.")

      local final_action
      final_action=$(echo "$complete_result" | $JQ_CMD -r '.action')
      process_manager_decision "$complete_result"

      [ "$final_action" = "complete" ] && break
    fi

    # Spawn workers for pending tasks (up to max parallel)
    local active
    active=$(count_active_workers)
    for task_id in $(get_pending_tasks); do
      if [ "$active" -ge "$MAX_PARALLEL_WORKERS" ]; then
        break
      fi
      spawn_worker "$task_id"
      active=$((active + 1))
    done

    # Wait for any worker to exit
    if [ "$active" -gt 0 ]; then
      local exited_pid
      exited_pid=$(wait_for_worker)

      if [ -n "$exited_pid" ] && [ "$exited_pid" != "0" ]; then
        local task_id
        if task_id=$(find_task_by_pid "$exited_pid"); then
          # Use exit code from wait if available, otherwise let handle_worker_exit read from file
          if [ -n "$exit_code" ] && [ "$exit_code" != "0" ]; then
            handle_worker_exit "$task_id" "$exit_code"
          else
            handle_worker_exit "$task_id"
          fi
        fi
      fi
    else
      # No active workers, no pending tasks — check for blocked tasks
      local has_blocked=false
      for task_file in "$COLLAB_DIR/tasks"/task-*.json; do
        [ -f "$task_file" ] || continue
        local status
        status=$($JQ_CMD -r '.status' "$task_file")
        if [ "$status" = "blocked" ] || [ "$status" = "escalated" ]; then
          has_blocked=true
          break
        fi
      done

      if $has_blocked; then
        log_warn "All tasks blocked/escalated. Waiting for human response..."
        # Check for human responses every 30s
        sleep 30
        check_human_responses
      elif has_pending_human_calls; then
        log_warn "No tasks but pending human calls. Waiting for human response..."
        sleep 30
        check_human_responses
      else
        log_warn "No pending, active, or blocked tasks. Exiting."
        break
      fi
    fi
  done

  log_ok "Collaboration session finished."
}

check_human_responses() {
  # Check if any human-call files have been answered
  # Uses Python helper for optimized detection and validation
  local checker_script="$SCRIPT_DIR/check-human-responses.py"

  if [ -f "$checker_script" ]; then
    # Use optimized Python checker
    local responses
    responses=$(python3 "$checker_script" 2>/dev/null)
    local exit_code=$?

    if [ $exit_code -eq 1 ] && [ -n "$responses" ] && [ "$responses" != "[]" ]; then
      # Parse each response using Python for reliability
      local response_count
      response_count=$(echo "$responses" | python3 -c "import sys, json; print(len(json.load(sys.stdin)))")

      log_info "Detected $response_count human response(s)"

      # Process each response
      echo "$responses" | python3 -c "
import sys, json
responses = json.load(sys.stdin)
for resp in responses:
    print(f'FILE:{resp[\"file\"]}')
    print(f'RESPONSE:{resp[\"response\"][:500]}')  # Limit to 500 chars
    print('---')
" | while IFS= read -r line; do
        if [[ "$line" == FILE:* ]]; then
          current_file="${line#FILE:}"
        elif [[ "$line" == RESPONSE:* ]]; then
          current_response="${line#RESPONSE:}"
        elif [ "$line" == "---" ] && [ -n "$current_file" ] && [ -n "$current_response" ]; then
          log_info "Processing response from $current_file"

          local manager_result
          manager_result=$(invoke_manager "human_response" "Human responded to escalation.
File: $current_file
Response: $current_response")

          # Rename to processed
          local full_path="$HUMAN_CALLS_DIR/$current_file"
          if [ -f "$full_path" ]; then
            mv "$full_path" "${full_path%.md}.responded.md"
          fi

          process_manager_decision "$manager_result"

          current_file=""
          current_response=""
        fi
      done
    fi
  else
    # Fallback to legacy bash implementation
    log_warn "Python checker not found, using legacy detection"
    _check_human_responses_legacy
  fi
}

_check_human_responses_legacy() {
  # Legacy bash implementation (fallback)
  # Support multiple languages: English and Chinese
  local default_prompts=("^<!-- Human: write your response below -->"
                         "^<!-- 人类：请在下方输入您的回复 -->")

  for call_file in "$HUMAN_CALLS_DIR"/*.md; do
    [ -f "$call_file" ] || continue

    # Check if file has any human response marker (starts with <!-- Human: or <!-- 人类：)
    if grep -qE "^<!-- (Human|人类)：" "$call_file"; then
      # Check if it's not just the default prompt
      local is_default=false
      for prompt in "${default_prompts[@]}"; do
        if grep -qE "$prompt" "$call_file"; then
          is_default=true
          break
        fi
      done
      $is_default && continue
      # Human has written a response — notify manager
      local response
      # Support both English and Chinese Response headers
      response=$(sed -n '/^### \(Response\|回复\)/,$ p' "$call_file" | tail -n +2)

      if [ -n "$response" ]; then
        log_info "Human response detected in $(basename "$call_file")"
        local manager_result
        manager_result=$(invoke_manager "human_response" "Human responded to escalation.
File: $(basename "$call_file")
Response: $response")

        # Rename to processed
        mv "$call_file" "${call_file%.md}.responded.md"

        process_manager_decision "$manager_result"
      fi
    fi
  done
}

# =============================================================================
# Watchdog (Layer 0)
# =============================================================================

run_watchdog() {
  local orch_pid="$1"
  local max_restarts=3
  local restarts=0

  log_info "[watchdog] Monitoring orchestrator PID $orch_pid (max restarts: $max_restarts)"

  while true; do
    sleep 30

    if ! kill -0 "$orch_pid" 2>/dev/null; then
      restarts=$((restarts + 1))
      log_warn "[watchdog] Orchestrator died. Restart #$restarts/$max_restarts"

      if [ $restarts -gt $max_restarts ]; then
        escalate_to_human "Orchestrator Repeated Failure" \
          "The collaboration orchestrator has failed $max_restarts times. Please investigate and restart manually with: se3 collab --resume"
        log_error "[watchdog] Max restarts exceeded. Exiting."
        exit 1
      fi

      # Restart orchestrator in resume mode
      run_main "" true &
      orch_pid=$!
      log_info "[watchdog] Restarted orchestrator as PID $orch_pid"
    fi
  done
}

# =============================================================================
# CLI Entry Point
# =============================================================================

usage() {
  echo "Usage: collab-orchestrator.sh [OPTIONS] [OBJECTIVE]"
  echo ""
  echo "Options:"
  echo "  --daemon     Run as daemon (background, no terminal)"
  echo "  --resume     Resume a crashed/paused session"
  echo "  --status     Show current collaboration status"
  echo "  --abort      Abort session and cleanup worktrees"
  echo "  --no-watchdog  Run without watchdog (for testing)"
  echo "  -h, --help   Show this help"
}

main() {
  local resume=false
  local no_watchdog=false
  local daemon=false
  local objective=""

  while [ $# -gt 0 ]; do
    case "$1" in
      --daemon)     daemon=true; shift ;;
      --resume)     resume=true; shift ;;
      --status)     summarize_tasks; exit 0 ;;
      --mock)       MOCK_MODE=true; export MOCK_MODE; shift ;;
      --abort)
        log_warn "Aborting collaboration session..."
        # Kill all workers
        for task_file in "$COLLAB_DIR/tasks"/task-*.json; do
          [ -f "$task_file" ] || continue
          local pid
          pid=$($JQ_CMD -r '.worker_pid // 0' "$task_file" 2>/dev/null)
          [ "$pid" != "0" ] && [ "$pid" != "null" ] && kill -TERM "$pid" 2>/dev/null || true
        done
        # Cleanup worktrees
        for wt in "$WORKTREE_DIR"/*/; do
          [ -d "$wt" ] && git -C "$PROJECT_ROOT" worktree remove "$wt" --force 2>/dev/null || true
        done
        rm -f "$COLLAB_DIR/orchestrator.pid"
        log_ok "Aborted."
        exit 0
        ;;
      --no-watchdog) no_watchdog=true; shift ;;
      -h|--help)    usage; exit 0 ;;
      *)            objective="$1"; shift ;;
    esac
  done

  if [ "$resume" = "false" ] && [ -z "$objective" ]; then
    log_error "Please provide an objective or use --resume"
    usage
    exit 1
  fi

  if [ "$no_watchdog" = "true" ]; then
    run_main "$objective" "$resume"
  else
    # Start orchestrator, then watchdog
    run_main "$objective" "$resume" &
    local orch_pid=$!
    run_watchdog "$orch_pid"
  fi
}

main "$@"
