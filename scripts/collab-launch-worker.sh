#!/usr/bin/env bash
# Launch worker agent for se3 collab
# Usage: collab-launch-worker.sh <task_id>

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${PROJECT_ROOT:-$(pwd)}"
COLLAB_DIR="$PROJECT_ROOT/.collab"

# Claude CLI command
CLAUDE_CMD="${SE3_CLAUDE_CMD:-kclaude}"

# Check arguments
if [ $# -lt 1 ]; then
    echo "Usage: $0 <task_id>" >&2
    echo "  task_id: e.g., task-001" >&2
    exit 1
fi

TASK_ID="$1"
TASK_FILE="$COLLAB_DIR/tasks/${TASK_ID}.json"

if [ ! -f "$TASK_FILE" ]; then
    echo "Error: Task file not found: $TASK_FILE" >&2
    exit 1
fi

# Parse task to get worktree
JQ_CMD="jq"
if ! command -v jq &>/dev/null; then
    JQ_CMD="python3 $SCRIPT_DIR/jq-complete.py"
fi

WORKTREE=$($JQ_CMD -r '.worktree' "$TASK_FILE")
BRANCH=$($JQ_CMD -r '.branch' "$TASK_FILE")
BASE_BRANCH=$($JQ_CMD -r '.base_branch // "master"' "$TASK_FILE")

# Ensure worktree exists
if [ ! -d "$WORKTREE" ]; then
    echo "[worker] Creating worktree: $WORKTREE (branch: $BRANCH)" >&2
    git -C "$PROJECT_ROOT" worktree add "$WORKTREE" -b "$BRANCH" "$BASE_BRANCH"
fi

# Update task status
$JQ_CMD '.status = "in_progress" | .started_at = now | .health.attempts += 1' "$TASK_FILE" > "${TASK_FILE}.tmp" && mv "${TASK_FILE}.tmp" "$TASK_FILE"

# Generate prompt
PROMPT=$(python3 "$SCRIPT_DIR/collab-worker-prompt.py" "$PROJECT_ROOT" "$TASK_ID")

# Write prompt to file
PROMPT_FILE="$COLLAB_DIR/logs/worker-${TASK_ID}-$(date +%Y%m%d-%H%M%S).prompt"
echo "$PROMPT" > "$PROMPT_FILE"

echo "[worker] Launching for task: $TASK_ID" >&2
echo "[worker] Worktree: $WORKTREE" >&2
echo "[worker] Log: $COLLAB_DIR/logs/worker-*.log" >&2

# Launch Claude in worktree
cd "$WORKTREE"
exec "$CLAUDE_CMD" \
    -p "$PROMPT" \
    --max-turns 50 \
    2> >(tee "$COLLAB_DIR/logs/worker-${TASK_ID}-$(date +%Y%m%d-%H%M%S).log" >&2)
