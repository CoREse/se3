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


# Python script for rendering --stream-json output (embedded in bash)
STREAM_JSON_RENDERER = '''import sys
import json

# ANSI colors
CYAN = "\\033[36m"
GREEN = "\\033[32m"
YELLOW = "\\033[33m"
BLUE = "\\033[34m"
MAGENTA = "\\033[35m"
GRAY = "\\033[90m"
RESET = "\\033[0m"
BOLD = "\\033[1m"
DIM = "\\033[2m"

def truncate_text(text, max_len=200):
    """Truncate long text for preview."""
    if not text:
        return ""
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text

def render_stream():
    """Render stream-json output from Claude Code."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_type = msg.get("type", "")

        if msg_type == "thinking":
            thinking = msg.get("thinking", "")
            if thinking:
                print(f"{GRAY}{DIM}💭 {truncate_text(thinking)}{RESET}", flush=True)

        elif msg_type == "tool_use":
            name = msg.get("name", "unknown")
            params = msg.get("parameters", {})
            print(f"{CYAN}🔧 {name}{RESET}", flush=True)
            for key, value in list(params.items())[:3]:
                preview = truncate_text(str(value), 80)
                print(f"{DIM}  {key}: {preview}{RESET}", flush=True)

        elif msg_type == "tool_result":
            name = msg.get("name", "unknown")
            error = msg.get("error")
            if error:
                print(f"{MAGENTA}❌ {name} failed: {truncate_text(str(error))}{RESET}", flush=True)
            else:
                print(f"{GREEN}✓ {name} complete{RESET}", flush=True)

        elif msg_type == "output":
            content = msg.get("content", "")
            if content:
                print(f"{RESET}{content}{RESET}", end="", flush=True)

        elif msg_type == "message":
            content = msg.get("content", "")
            if content:
                print(f"{RESET}{content}{RESET}", flush=True)

        elif msg_type == "error":
            error_msg = msg.get("error", "Unknown error")
            print(f"{MAGENTA}❌ Error: {error_msg}{RESET}", flush=True)

if __name__ == "__main__":
    render_stream()
'''


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

    # Embed the Python renderer script
    renderer_script = STREAM_JSON_RENDERER

    script = f'''#!/bin/bash
#
# SE3 Loop - Auto-generated execution script
#

set -e
set -o pipefail

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

    # Create the Python stream renderer
    RENDERER_FILE=$(mktemp /tmp/se3-loop-renderer-XXXXXX.py)
    cat > "$RENDERER_FILE" << 'RENDERER_EOF'
''' + renderer_script + '''RENDERER_EOF

    # Execute Claude Code with --stream-json and render output
    # Timeout after 30 minutes to prevent infinite hanging
    echo "------------------------------------------------------------"
    echo "[SE3 Loop] Executing: $CLAUDE_CMD --stream-json --max-turns 0"
    echo "[SE3 Loop] Renderer: $RENDERER_FILE"
    echo ""

    EXIT_CODE=0
    timeout 1800 "$CLAUDE_CMD" --dangerously-skip-permissions --stream-json --max-turns 0 "$PROMPT_FILE" 2>&1 | python3 "$RENDERER_FILE" || EXIT_CODE=$?

    echo ""
    echo "------------------------------------------------------------"

    if [ $EXIT_CODE -eq 0 ]; then
        echo "[Claude] Session completed"
    elif [ $EXIT_CODE -eq 124 ]; then
        echo "[Claude] Session timed out (30 min limit)"
    else
        echo "[Claude] Session exited (code: $EXIT_CODE)"
    fi

    rm -f "$PROMPT_FILE" "$RENDERER_FILE"

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
