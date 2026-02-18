"""SE3 Loop command — repeatedly run full-cycle workflow.

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
import time
import threading
import queue
from pathlib import Path
from typing import Optional

from ..config import load_claude_commands


# ANSI colors for rendering
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
MAGENTA = "\033[35m"
GRAY = "\033[90m"
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"


def sanitize_change_name(description: str) -> str:
    """Convert a description into a valid change name."""
    name = description.lower().strip()
    name = "".join(c for c in name if (ord(c) < 128 and c.isalnum()) or c in " -_/")
    name = name.replace(" ", "-").replace("_", "-").replace("/", "-")
    name = re.sub(r'-+', '-', name)
    if len(name) > 40:
        name = name[:40].rsplit("-", 1)[0]
    return name.strip("-") or "loop-task"


def truncate_text(text: str, max_len: int = 200) -> str:
    """Truncate long text for preview."""
    if not text:
        return ""
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text


def render_stream_json_line(line: str) -> None:
    """Render a single stream-json line to terminal."""
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


def run_claude_with_renderer(claude_cmd: str, prompt_file: Path, timeout_sec: int = 1800) -> int:
    """Run claude with stream-json output and real-time rendering.

    Returns exit code from claude.
    """
    env = {**dict(os.environ)}
    env.pop("CLAUDECODE", None)

    cmd = [
        claude_cmd,
        "--dangerously-skip-permissions",
        "--print",
        "--output-format", "stream-json",
        "--verbose",
        "--max-turns", "0",
        str(prompt_file)
    ]

    print(f"[SE3 Loop] Executing: {claude_cmd} --print --output-format stream-json --verbose --max-turns 0 {prompt_file}")
    print("")

    output_queue = queue.Queue()
    exit_code = [None]  # Use list to allow modification in closure

    def reader_thread():
        """Read stdout in a separate thread to avoid blocking."""
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                bufsize=1,
            )

            # Store process for main thread to wait
            output_queue.put(proc)

            # Read output line by line
            for line in proc.stdout:
                output_queue.put(line)

            proc.wait()
            exit_code[0] = proc.returncode
            output_queue.put(None)  # Signal completion

        except Exception as e:
            output_queue.put(f"ERROR: {e}")
            output_queue.put(None)

    # Start reader thread
    thread = threading.Thread(target=reader_thread, daemon=True)
    thread.start()

    # Wait for process object
    try:
        proc = output_queue.get(timeout=5)
        if isinstance(proc, str) and proc.startswith("ERROR:"):
            print(f"{MAGENTA}[SE3 Loop] {proc}{RESET}")
            return 1
    except queue.Empty:
        print(f"{MAGENTA}[SE3 Loop] Failed to start claude process{RESET}")
        return 1

    # Process output with timeout
    start_time = time.time()
    while True:
        try:
            # Check for output with short timeout
            item = output_queue.get(timeout=0.1)

            if item is None:
                # Done
                break
            elif isinstance(item, str):
                # Output line
                render_stream_json_line(item)

            # Check timeout
            if time.time() - start_time > timeout_sec:
                proc.kill()
                print(f"\n{YELLOW}[SE3 Loop] Session timed out ({timeout_sec}s limit){RESET}")
                return 124

        except queue.Empty:
            # No output available, check if process is still running
            if exit_code[0] is not None:
                break
            continue

    # Wait for thread to complete
    thread.join(timeout=1)

    return exit_code[0] if exit_code[0] is not None else 0


def run_exclusive_loop(
    prompt: str,
    project_root: str = ".",
    iterations: int = 10,
    quick: bool = False,
) -> None:
    """Run the loop - execute claude with real-time rendering for each iteration."""
    root = Path(project_root).resolve()

    # Load claude command
    commands = load_claude_commands(root)
    claude_cmd = commands[0]["cmd"] if commands else "claude"

    if not shutil.which(claude_cmd):
        print(f"\n[SE3 Loop] Error: '{claude_cmd}' not found in PATH")
        return

    base_name = sanitize_change_name(prompt)
    openspec_cmd = shutil.which("openspec") or "openspec"

    print(f"\n{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}SE3 Loop{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}")
    print(f"\nPrompt: {prompt}")
    print(f"Iterations: {iterations}")
    print(f"Project: {root}")
    print(f"Claude: {claude_cmd}")
    print(f"\nPress Ctrl+C to stop")
    print(f"{BOLD}{'=' * 60}{RESET}\n")

    for iteration in range(1, iterations + 1):
        print(f"\n{BOLD}{'─' * 60}{RESET}")
        print(f"{BOLD}Iteration {iteration} / {iterations}{RESET}")
        print(f"{BOLD}{'─' * 60}{RESET}\n")

        # Generate unique change name
        change_name = f"{base_name}-{iteration:02d}"
        counter = 1
        while (root / "openspec" / "changes" / change_name).exists():
            change_name = f"{base_name}-{iteration:02d}-{counter}"
            counter += 1

        print(f"{CYAN}[SE3 Loop] Creating change: {change_name}{RESET}")

        # Create change
        result = subprocess.run(
            [openspec_cmd, "new", "change", change_name],
            cwd=root,
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            print(f"{YELLOW}[SE3 Loop] Failed to create change, retrying...{RESET}")
            time.sleep(2)
            continue

        # Create tasks.md
        tasks_file = root / "openspec" / "changes" / change_name / "tasks.md"
        tasks_file.write_text(f"""# {prompt} (Iteration {iteration}/{iterations})

## Tasks

- [ ] {prompt}
""")

        # Create prompt file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', prefix='se3-loop-prompt-', delete=False) as f:
            f.write(f"""/se3:work {change_name}

{prompt}

(这是 SE3 Loop 的迭代 {iteration} / {iterations}。请按照 SE3 流程完成这个 change：
1. 读取相关 spec
2. 实现需求
3. 运行测试
4. 提交更改
5. 运行 /se3:done 结束会话)
""")
            prompt_file = Path(f.name)

        print(f"{CYAN}[SE3 Loop] Starting Claude Code...{RESET}\n")
        print(f"{'─' * 60}")

        try:
            exit_code = run_claude_with_renderer(claude_cmd, prompt_file)
        except KeyboardInterrupt:
            print(f"\n{YELLOW}[SE3 Loop] Interrupted by user{RESET}")
            exit_code = 130
        finally:
            prompt_file.unlink(missing_ok=True)

        print(f"\n{'─' * 60}")

        if exit_code == 0:
            print(f"\n{GREEN}[SE3 Loop] Iteration {iteration} completed successfully{RESET}")
        elif exit_code == 124:
            print(f"\n{YELLOW}[SE3 Loop] Iteration {iteration} timed out{RESET}")
        elif exit_code == 130:
            print(f"\n{YELLOW}[SE3 Loop] Iteration {iteration} interrupted{RESET}")
            break
        else:
            print(f"\n{YELLOW}[SE3 Loop] Iteration {iteration} exited with code {exit_code}{RESET}")

        if iteration < iterations:
            print(f"\n[SE3 Loop] Continuing in 2 seconds...")
            time.sleep(2)

    print(f"\n{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}SE3 Loop Complete{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}\n")
