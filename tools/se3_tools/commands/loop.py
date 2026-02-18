"""SE3 Loop command — repeatedly run full-cycle workflow.

This command takes over the terminal and runs the se3 workflow in a loop
for a specified number of iterations.

Usage:
    se3 loop "prompt" [--iterations 10]
    se3 loop "prompt" --quick
"""

import json
import os
import re
import subprocess
import shutil
from pathlib import Path
from datetime import datetime

from ..config import load_claude_commands


def sanitize_change_name(description: str) -> str:
    """Convert a description into a valid change name.

    Change names must only contain lowercase ASCII letters, numbers, and hyphens.
    Non-ASCII characters (like Chinese) are removed.
    """
    name = description.lower().strip()
    # Only keep ASCII alphanumeric characters (a-z, 0-9), spaces, and separators
    name = "".join(c for c in name if (ord(c) < 128 and c.isalnum()) or c in " -_/")
    name = name.replace(" ", "-").replace("_", "-").replace("/", "-")
    # Collapse multiple consecutive hyphens
    name = re.sub(r'-+', '-', name)
    if len(name) > 40:
        name = name[:40].rsplit("-", 1)[0]
    return name.strip("-")


def generate_loop_script(
    prompt: str,
    project_root: str,
    iterations: int,
    quick: bool,
    base_name: str,
    claude_cmd: str,
) -> str:
    """Generate the bash loop script for execution."""
    from .work import WORKFLOWS

    workflow_type = "small" if quick else "feature"
    steps = WORKFLOWS.get(workflow_type, WORKFLOWS["feature"])
    first_step = steps[0] if steps else "analyze"

    quick_flag = "--quick" if quick else ""

    script = f'''#!/bin/bash
#
# SE3 Loop - Auto-generated execution script
#

set -e

PROMPT="{prompt.replace('"', '\\"')}"
ITERATIONS={iterations}
PROJECT_ROOT="{project_root}"
QUICK_FLAG="{quick_flag}"
CLAUDE_CMD="{claude_cmd}"

echo "============================================================"
echo "SE3 Loop"
echo "============================================================"
echo ""
echo "Prompt: $PROMPT"
echo "Iterations: $ITERATIONS"
echo "Project: $PROJECT_ROOT"
echo "Claude Command: $CLAUDE_CMD"
echo ""
echo "Press Ctrl+C to stop at any time."
echo "============================================================"
echo ""

# Check for claude command
if ! command -v "$CLAUDE_CMD" &> /dev/null; then
    echo "❌ Error: '$CLAUDE_CMD' command not found in PATH"
    exit 1
fi

cd "$PROJECT_ROOT"

ITERATION=0
while [ $ITERATION -lt $ITERATIONS ]; do
    ITERATION=$((ITERATION + 1))

    echo ""
    echo "╔════════════════════════════════════════════════════════════╗"
    echo "║  Iteration $ITERATION / $ITERATIONS"
    echo "╚════════════════════════════════════════════════════════════╝"
    echo ""

    # Use pre-sanitized base name from Python (handles non-ASCII characters)
    BASE_NAME="{base_name}"
    CHANGE_NAME="${{BASE_NAME}}-$(printf '%02d' $ITERATION)"

    # Ensure unique name
    COUNTER=1
    while [ -d "openspec/changes/$CHANGE_NAME" ]; do
        CHANGE_NAME="${{BASE_NAME}}-$(printf '%02d' $ITERATION)-$COUNTER"
        COUNTER=$((COUNTER + 1))
    done

    echo "[SE3 Loop] Creating change: $CHANGE_NAME"

    # Create the change using openspec
    if ! openspec new change "$CHANGE_NAME" 2>/dev/null; then
        echo "[SE3 Loop] Failed to create change, retrying..."
        sleep 2
        continue
    fi

    # Create tasks.md
    cat > "openspec/changes/$CHANGE_NAME/tasks.md" << EOF
# $PROMPT (Iteration $ITERATION/$ITERATIONS)

## Tasks

- [ ] $PROMPT
EOF

    echo "[SE3 Loop] Starting Claude Code..."
    echo ""

    # Create the prompt file for Claude
    PROMPT_FILE=$(mktemp /tmp/se3-loop-prompt-XXXXXX.md)
    cat > "$PROMPT_FILE" << CLAUDE_EOF
/se3:work $CHANGE_NAME

$PROMPT

(这是 SE3 Loop 的迭代 $ITERATION / $ITERATIONS。请按照 SE3 流程完成这个 change：
1. 读取相关 spec
2. 实现需求
3. 运行测试
4. 提交更改
5. 运行 /se3:done 结束会话)
CLAUDE_EOF

    # Execute Claude Code (auto-exit after completion with --max-turns 0)
    # Timeout after 30 minutes to prevent infinite hanging
    echo "------------------------------------------------------------"
    if timeout 1800 "$CLAUDE_CMD" --dangerously-skip-permissions --print --output-format text --max-turns 0 "$PROMPT_FILE" 2>&1; then
        echo "------------------------------------------------------------"
        echo "[Claude] Session completed"
    else
        EXIT_CODE=$?
        echo "------------------------------------------------------------"
        if [ $EXIT_CODE -eq 124 ]; then
            echo "[Claude] Session timed out (30 min limit)"
        else
            echo "[Claude] Session exited (code: $EXIT_CODE)"
        fi
    fi

    rm -f "$PROMPT_FILE"

    echo ""
    echo "[SE3 Loop] Iteration $ITERATION complete"

    if [ $ITERATION -lt $ITERATIONS ]; then
        echo "[SE3 Loop] Continuing..."
        sleep 2
    fi
done

echo ""
echo "============================================================"
echo "SE3 Loop Complete - $ITERATION iterations"
echo "============================================================"
'''
    return script


def run_exclusive_loop(
    prompt: str,
    project_root: str = ".",
    iterations: int = 10,
    quick: bool = False,
) -> None:
    """Run the loop - generates and executes bash script."""
    root = Path(project_root).resolve()

    print(f"\n{'=' * 60}")
    print("SE3 Loop")
    print(f"{'=' * 60}")
    print("")
    print(f"Prompt: {prompt}")
    print(f"Iterations: {iterations}")
    print(f"Project: {root}")
    print("")
    print("Generating loop script...")

    # Generate base_name in Python (shell tr can't handle non-ASCII)
    base_name = sanitize_change_name(prompt)
    if not base_name:
        base_name = "loop-task"

    # Load claude command from config (priority-based)
    commands = load_claude_commands(root)
    claude_cmd = commands[0]["cmd"] if commands else "claude"

    # Verify the command exists
    if not shutil.which(claude_cmd):
        print(f"\n❌ Error: Configured claude command '{claude_cmd}' not found in PATH")
        print("Please check your se3.config.yaml or install the required CLI.")
        return

    # Generate the script
    script_content = generate_loop_script(prompt, str(root), iterations, quick, base_name, claude_cmd)

    # Write to temporary file
    script_path = root / ".se3-loop-exec.sh"
    script_path.write_text(script_content)
    script_path.chmod(0o755)

    print(f"Script created: {script_path}")
    print("")
    print("Starting loop execution...")
    print("This will takeover the terminal and run Claude Code repeatedly.")
    print("Press Ctrl+C to stop at any time.")
    print("")
    print(f"{'=' * 60}")
    print("")

    # Execute the script with modified environment (clear CLAUDECODE for nested sessions)
    env = {**dict(os.environ)}
    env.pop("CLAUDECODE", None)  # Avoid nested session detection

    try:
        subprocess.run([str(script_path)], check=False, env=env)
    except KeyboardInterrupt:
        print("")
        print("\n[SE3 Loop] Interrupted by user")
    finally:
        # Clean up script
        if script_path.exists():
            script_path.unlink()
