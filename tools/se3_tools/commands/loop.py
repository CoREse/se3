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
import sys
import tempfile
from pathlib import Path
from datetime import datetime

from ..config import load_claude_commands


# ANSI colors
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
GRAY = "\033[90m"
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"


def truncate_text(text, max_len=200):
    """Truncate long text for preview."""
    if not text:
        return ""
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text


def render_stream_json_line(line: str) -> None:
    """Render a single stream-json line."""
    line = line.strip()
    if not line:
        return
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        return

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


def run_claude_with_renderer(claude_cmd: str, prompt_file: Path, timeout_sec: int = 1800) -> int:
    """Run claude and render output in real-time.

    Returns the exit code.
    """
    env = {**dict(os.environ)}
    env.pop("CLAUDECODE", None)  # Avoid nested session detection

    cmd = [
        claude_cmd,
        "--dangerously-skip-permissions",
        "--print",
        "--output-format", "stream-json",
        "--verbose",  # Required for stream-json mode
        "--max-turns", "0",
        str(prompt_file)
    ]

    print(f"[SE3 Loop] Executing: {' '.join(cmd[:5])} ... --max-turns 0 {prompt_file}")
    print("")

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )

        # Read and render output line by line
        for line in proc.stdout:
            render_stream_json_line(line)

        proc.wait(timeout=timeout_sec)
        return proc.returncode

    except subprocess.TimeoutExpired:
        proc.kill()
        print(f"\n{YELLOW}[Claude] Session timed out ({timeout_sec}s limit){RESET}")
        return 124
    except FileNotFoundError:
        print(f"\n{MAGENTA}❌ Error: '{claude_cmd}' not found{RESET}")
        return 127
    except Exception as e:
        print(f"\n{MAGENTA}❌ Error running claude: {e}{RESET}")
        return 1


def run_exclusive_loop(
    prompt: str,
    project_root: str = ".",
    iterations: int = 10,
    quick: bool = False,
) -> None:
    """Run the loop - directly in Python without bash scripts."""
    root = Path(project_root).resolve()

    print(f"\n{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}SE3 Loop{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}")
    print("")
    print(f"Prompt: {prompt}")
    print(f"Iterations: {iterations}")
    print(f"Project: {root}")
    print("")
    print("Press Ctrl+C to stop at any time.")
    print(f"{BOLD}{'=' * 60}{RESET}")
    print("")

    # Load claude command from config (priority-based)
    commands = load_claude_commands(root)
    claude_cmd = commands[0]["cmd"] if commands else "claude"

    # Verify the command exists
    if not shutil.which(claude_cmd):
        print(f"\n{MAGENTA}❌ Error: Configured claude command '{claude_cmd}' not found in PATH{RESET}")
        print("Please check your se3.config.yaml or install the required CLI.")
        return

    # Generate base_name
    base_name = sanitize_change_name(prompt)
    if not base_name:
        base_name = "loop-task"

    for iteration in range(1, iterations + 1):
        print("")
        print(f"{BOLD}╔{'═' * 60}╗{RESET}")
        print(f"{BOLD}║  Iteration {iteration} / {iterations}{RESET}")
        print(f"{BOLD}╚{'═' * 60}╝{RESET}")
        print("")

        # Generate change name
        change_name = f"{base_name}-{iteration:02d}"
        counter = 1
        while (root / "openspec" / "changes" / change_name).exists():
            change_name = f"{base_name}-{iteration:02d}-{counter}"
            counter += 1

        print(f"{CYAN}[SE3 Loop] Creating change: {change_name}{RESET}")

        # Create the change using openspec (find in PATH)
        openspec_cmd = shutil.which("openspec") or "openspec"
        result = subprocess.run(
            [openspec_cmd, "new", "change", change_name],
            cwd=root,
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            print(f"{YELLOW}[SE3 Loop] Failed to create change, retrying...{RESET}")
            import time
            time.sleep(2)
            continue

        # Create tasks.md
        tasks_file = root / "openspec" / "changes" / change_name / "tasks.md"
        tasks_content = f"""# {prompt} (Iteration {iteration}/{iterations})

## Tasks

- [ ] {prompt}
"""
        tasks_file.write_text(tasks_content)

        # Create the prompt file for Claude
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', prefix='se3-loop-prompt-', delete=False) as f:
            prompt_content = f"""/se3:work {change_name}

{prompt}

(这是 SE3 Loop 的迭代 {iteration} / {iterations}。请按照 SE3 流程完成这个 change：
1. 读取相关 spec
2. 实现需求
3. 运行测试
4. 提交更改
5. 运行 /se3:done 结束会话)
"""
            f.write(prompt_content)
            prompt_file = Path(f.name)

        print(f"{CYAN}[SE3 Loop] Starting Claude Code...{RESET}")
        print("")
        print("-" * 60)

        try:
            exit_code = run_claude_with_renderer(claude_cmd, prompt_file)
        finally:
            # Clean up prompt file
            prompt_file.unlink(missing_ok=True)

        print("")
        print("-" * 60)

        if exit_code == 0:
            print(f"{GREEN}[Claude] Session completed{RESET}")
        elif exit_code == 124:
            print(f"{YELLOW}[Claude] Session timed out (30 min limit){RESET}")
        else:
            print(f"{YELLOW}[Claude] Session exited (code: {exit_code}){RESET}")

        print("")
        print(f"{GREEN}[SE3 Loop] Iteration {iteration} complete{RESET}")

        if iteration < iterations:
            print(f"[SE3 Loop] Continuing...")
            import time
            time.sleep(2)

    print("")
    print(f"{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}SE3 Loop Complete - {iterations} iterations{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}")
