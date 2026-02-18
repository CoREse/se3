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
    """Convert a description into a valid change name.

    Only lowercase letters, numbers, and hyphens are allowed.
    Non-ASCII characters (e.g., Chinese) are filtered out.
    """
    name = description.lower().strip()
    # Keep only ASCII alphanumeric and allowed separators
    name = "".join(c for c in name if (ord(c) < 128 and c.isalnum()) or c in " -_/")
    name = name.replace(" ", "-").replace("_", "-").replace("/", "-")
    name = re.sub(r'-+', '-', name)
    if len(name) > 40:
        name = name[:40].rsplit("-", 1)[0]
    name = name.strip("-")
    # If name is empty (e.g., Chinese-only input), use timestamp-based fallback
    if not name:
        import time
        name = f"loop-{int(time.time()) % 10000}"
    return name


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

    # Handle assistant messages (contain tool_use or text)
    if msg_type == "assistant":
        message = msg.get("message", {})
        content = message.get("content", [])
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "tool_use":
                    name = item.get("name", "unknown")
                    input_data = item.get("input", {})
                    print(f"{CYAN}🔧 {name}{RESET}", flush=True)
                    for key, value in list(input_data.items())[:3]:
                        preview = truncate_text(str(value), 80)
                        print(f"{DIM}  {key}: {preview}{RESET}", flush=True)
                elif item.get("type") == "text":
                    text = item.get("text", "")
                    if text:
                        print(f"{RESET}{text}{RESET}", flush=True)
        return

    # Handle tool results (inside user messages)
    if msg_type == "user":
        message = msg.get("message", {})
        content = message.get("content", [])
        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_result":
                name = item.get("name", "unknown")
                tool_result = item.get("result", {})
                error = tool_result.get("error") if isinstance(tool_result, dict) else None
                if error:
                    print(f"{MAGENTA}❌ {name} failed: {truncate_text(str(error))}{RESET}", flush=True)
                else:
                    print(f"{GREEN}✓ {name} complete{RESET}", flush=True)
        return

    # Handle final result
    if msg_type == "result":
        result_text = msg.get("result", "")
        if result_text:
            print(f"{RESET}{result_text}{RESET}", flush=True)
        return

    # Handle legacy/thinking messages
    if msg_type == "thinking":
        thinking = msg.get("thinking", "")
        if thinking:
            print(f"{GRAY}{DIM}💭 {truncate_text(thinking)}{RESET}", flush=True)
        return

    # Handle system messages (just show init)
    if msg_type == "system":
        subtype = msg.get("subtype", "")
        if subtype == "init":
            print(f"{DIM}[System initialized]{RESET}", flush=True)
        return

    # Handle error messages
    if msg_type == "error":
        error_msg = msg.get("error", "Unknown error")
        print(f"{MAGENTA}❌ Error: {error_msg}{RESET}", flush=True)
        return


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


def run_claude_summary(claude_cmd: str, change_dir: Path, timeout_sec: int = 60) -> str:
    """Generate a summary of the completed iteration using Claude Code.

    Returns a brief summary string or empty string if failed.
    """
    # Read tasks.md and work.md if they exist
    tasks_file = change_dir / "tasks.md"
    work_file = change_dir / "work.md"

    content_parts = []

    if tasks_file.exists():
        content_parts.append(f"Tasks:\n{tasks_file.read_text()}")

    if work_file.exists():
        work_text = work_file.read_text()
        # Limit work content to avoid too large prompt
        if len(work_text) > 2000:
            work_text = work_text[:2000] + "\n... (truncated)"
        content_parts.append(f"Work log:\n{work_text}")

    if not content_parts:
        return "No work records found."

    full_content = "\n\n".join(content_parts)

    # Create summary prompt
    summary_prompt = f"""Please provide a brief summary (2-3 sentences) of what was accomplished in this iteration.

{full_content}

Summary:"""

    env = {**dict(os.environ)}
    env.pop("CLAUDECODE", None)

    cmd = [
        claude_cmd,
        "--dangerously-skip-permissions",
        "--print",
        "-p", summary_prompt
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            env=env
        )
        if result.returncode == 0:
            summary = result.stdout.strip()
            # Clean up the summary - remove any quotes or extra whitespace
            summary = summary.strip('"\'').strip()
            return summary if summary else "Iteration completed."
        else:
            return f"Iteration completed (summary generation failed: {result.stderr[:100]})."
    except subprocess.TimeoutExpired:
        return "Iteration completed (summary generation timed out)."
    except Exception as e:
        return f"Iteration completed (summary generation error: {e})."


def run_exclusive_loop(
    prompt: str,
    project_root: str = ".",
    iterations: int = 10,
    quick: bool = False,
    no_summary: bool = False,
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
    print(f"Summary: {'disabled' if no_summary else 'enabled (use --no-summary to disable)'}")
    print(f"\nPress Ctrl+C to stop")
    print(f"{BOLD}{'=' * 60}{RESET}\n")

    previous_summary = None

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
            print(f"{YELLOW}[SE3 Loop] Failed to create change{RESET}")
            if result.stderr:
                print(f"{YELLOW}Error: {result.stderr.strip()}{RESET}")
            if result.stdout:
                print(f"{YELLOW}Output: {result.stdout.strip()}{RESET}")
            print(f"{YELLOW}Retrying in 2 seconds...{RESET}")
            time.sleep(2)
            continue

        # Create tasks.md
        tasks_file = root / "openspec" / "changes" / change_name / "tasks.md"
        tasks_file.write_text(f"""# {prompt} (Iteration {iteration}/{iterations})

## Tasks

- [ ] {prompt}
""")

        # Build prompt content with previous summary if available
        previous_summary_section = ""
        if previous_summary:
            previous_summary_section = f"""
## 上一次迭代总结

{previous_summary}

---
"""

        # Create prompt file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', prefix='se3-loop-prompt-', delete=False) as f:
            f.write(f"""/se3:work {change_name}
{previous_summary_section}
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

        # Generate summary for next iteration (if not disabled and not the last iteration)
        if not no_summary and iteration < iterations and exit_code == 0:
            change_dir = root / "openspec" / "changes" / change_name
            print(f"\n{CYAN}[SE3 Loop] Generating summary for next iteration...{RESET}")
            previous_summary = run_claude_summary(claude_cmd, change_dir)
            print(f"{GRAY}{DIM}Summary: {previous_summary[:100]}{'...' if len(previous_summary) > 100 else ''}{RESET}")

        if iteration < iterations:
            print(f"\n[SE3 Loop] Continuing in 2 seconds...")
            time.sleep(2)

    print(f"\n{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}SE3 Loop Complete{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}\n")
