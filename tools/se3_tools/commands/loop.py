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
import time
import threading
import queue
import signal
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
    Name must start with a letter.
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
    # Ensure name starts with a letter (openspec requirement)
    if name and not name[0].isalpha():
        name = "t" + name
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


def run_claude_with_renderer(claude_cmd: str, prompt_text: str, timeout_sec: int = 1800,
                              loop_state: Optional["LoopState"] = None) -> tuple[int, bool]:
    """Run claude with stream-json output and real-time rendering.

    Args:
        claude_cmd: The claude command to run
        prompt_text: The prompt text to send via stdin
        timeout_sec: Timeout in seconds
        loop_state: Optional LoopState for Ctrl-C handling

    Returns tuple of (exit_code, was_interrupted) where was_interrupted indicates
    if the process was interrupted by Ctrl-C for supplemental prompt.
    """
    env = {**dict(os.environ)}
    env.pop("CLAUDECODE", None)

    cmd = [
        claude_cmd,
        "--dangerously-skip-permissions",
        "--print",
        "--output-format", "stream-json",
        "--verbose",
        "--max-turns", "50",
    ]

    print(f"[SE3 Loop] Executing: {claude_cmd} --print --output-format stream-json --verbose --max-turns 50")
    print(f"[SE3 Loop] Prompt (first 200 chars): {prompt_text[:200]}...")
    print("")

    output_queue = queue.Queue()
    exit_code = [None]  # Use list to allow modification in closure
    proc_container = [None]  # Store process for signal handler access

    def reader_thread():
        """Read stdout in a separate thread to avoid blocking."""
        try:
            # Start Claude in a new process group so Ctrl-C doesn't interrupt it
            # This allows us to handle Ctrl-C in the parent while Claude continues running
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                bufsize=1,
                start_new_session=True,  # New process group - isolates from Ctrl-C
            )
            proc_container[0] = proc

            # Store process for main thread to wait
            output_queue.put(proc)

            # Send prompt via stdin
            try:
                proc.stdin.write(prompt_text)
                proc.stdin.close()
            except BrokenPipeError:
                # Process exited before we could write - this is okay, we'll capture the exit code
                pass
            except Exception as e:
                output_queue.put(f"ERROR writing to stdin: {e}")

            # Read output line by line
            for line in proc.stdout:
                output_queue.put(line)

            proc.wait()
            exit_code[0] = proc.returncode
            output_queue.put(None)  # Signal completion

        except Exception as e:
            output_queue.put(f"ERROR: {e}")
            output_queue.put(None)

    # Define signal handler that only sets flag, doesn't kill process
    def sigint_handler(signum, frame):
        if loop_state:
            loop_state.handle_sigint(signum, frame)
            # Note: We don't raise KeyboardInterrupt here - let Claude continue running

    # Install signal handler (only if loop_state is provided)
    old_sigint_handler = None
    if loop_state:
        old_sigint_handler = signal.signal(signal.SIGINT, sigint_handler)

    # Start reader thread
    thread = threading.Thread(target=reader_thread, daemon=True)
    thread.start()

    # Wait for process object
    try:
        proc = output_queue.get(timeout=5)
        if isinstance(proc, str) and proc.startswith("ERROR:"):
            print(f"{MAGENTA}[SE3 Loop] {proc}{RESET}")
            if old_sigint_handler:
                signal.signal(signal.SIGINT, old_sigint_handler)
            return 1, False
    except queue.Empty:
        print(f"{MAGENTA}[SE3 Loop] Failed to start claude process{RESET}")
        if old_sigint_handler:
            signal.signal(signal.SIGINT, old_sigint_handler)
        return 1, False

    # Process output with timeout
    start_time = time.time()
    was_interrupted = False
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
                if old_sigint_handler:
                    signal.signal(signal.SIGINT, old_sigint_handler)
                return 124, False

            # Check if we should enter supplemental mode (first Ctrl-C pressed)
            if loop_state and loop_state.in_supplemental_mode and not loop_state.should_exit:
                # First Ctrl-C - interrupt Claude to enter supplemental mode
                was_interrupted = True
                proc.kill()
                print(f"\n{YELLOW}[SE3 Loop] Interrupted for supplemental prompt{RESET}")
                if old_sigint_handler:
                    signal.signal(signal.SIGINT, old_sigint_handler)
                return 130, True

            # Check if we should exit (second Ctrl-C pressed)
            if loop_state and loop_state.should_exit:
                proc.kill()
                if old_sigint_handler:
                    signal.signal(signal.SIGINT, old_sigint_handler)
                return 130, False

        except queue.Empty:
            # No output available, check if process is still running
            if exit_code[0] is not None:
                break
            # Check if we should enter supplemental mode while waiting
            if loop_state and loop_state.in_supplemental_mode and not loop_state.should_exit:
                was_interrupted = True
                proc.kill()
                if old_sigint_handler:
                    signal.signal(signal.SIGINT, old_sigint_handler)
                return 130, True
            # Check if we should exit while waiting
            if loop_state and loop_state.should_exit:
                proc.kill()
                if old_sigint_handler:
                    signal.signal(signal.SIGINT, old_sigint_handler)
                return 130, False
            continue

    # Wait for thread to complete (give it more time to clean up)
    thread.join(timeout=5)

    # Restore old signal handler
    if old_sigint_handler:
        signal.signal(signal.SIGINT, old_sigint_handler)

    return (exit_code[0] if exit_code[0] is not None else 0), was_interrupted


def run_claude_summary(claude_cmd: str, change_dir: Path, timeout_sec: int = 300) -> str:
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


class LoopState:
    """State for the SE3 Loop to handle Ctrl-C and supplemental prompts."""

    def __init__(self):
        self.supplemental_prompts: list[str] = []
        self.in_supplemental_mode = False
        self.should_exit = False

    def handle_sigint(self, signum, frame):
        """Handle Ctrl-C signal.

        First press: enter supplemental mode
        Second press (in supplemental mode): exit loop
        """
        if self.in_supplemental_mode:
            # Second Ctrl-C in supplemental mode - exit
            self.should_exit = True
            print(f"\n{YELLOW}[SE3 Loop] Second Ctrl-C pressed, exiting...{RESET}")
        else:
            # First Ctrl-C - enter supplemental mode
            self.in_supplemental_mode = True
            print(f"\n{YELLOW}[SE3 Loop] Ctrl-C pressed - entering supplemental mode{RESET}")
            print(f"{YELLOW}Press Ctrl-C again to exit, or enter your supplemental prompt below:{RESET}")

    def get_full_prompt(self, base_prompt: str, iteration: int, iterations: int,
                       change_name: Optional[str] = None, quick: bool = False,
                       previous_summary: Optional[str] = None) -> str:
        """Build the full prompt text with all supplemental prompts."""
        # Build prompt content with previous summary if available
        previous_summary_section = ""
        if previous_summary:
            previous_summary_section = f"""
## Previous Iteration Summary

{previous_summary}

---
"""

        # Build supplemental section
        supplemental_section = ""
        if self.supplemental_prompts:
            supplemental_section = """
## Supplemental Instructions (from user during loop)

"""
            for i, sp in enumerate(self.supplemental_prompts, 1):
                supplemental_section += f"{i}. {sp}\n"
            supplemental_section += "\n---\n"

        if quick:
            # Quick mode: use se3:fc (full-cycle) instead of se3:work to skip formal change creation
            prompt_text = f"""/se3:fc {base_prompt}
{previous_summary_section}{supplemental_section}

(This is SE3 Loop iteration {iteration} / {iterations}. Please complete this work using the full-cycle workflow:
1. Read relevant specs
2. Implement the requirements
3. Run tests
4. Commit changes
5. Run /se3:done to end the session)
"""
        else:
            prompt_text = f"""/se3:work {change_name}
{previous_summary_section}{supplemental_section}
{base_prompt}

(This is SE3 Loop iteration {iteration} / {iterations}. Please complete this change following the SE3 process:
1. Read relevant specs
2. Implement the requirements
3. Run tests
4. Commit changes
5. Run /se3:done to end the session)
"""
        return prompt_text


def run_loop_collab(
    prompt: str,
    project_root: str = ".",
    iterations: int = 10,
    quick: bool = False,
    no_summary: bool = False,
    mock: bool = False,
) -> None:
    """Run the loop with collab integration for each iteration.

    Each iteration runs as a collab session with foreground orchestrator,
    allowing parallel work across multiple workers with real-time visibility.

    Uses LoopCollabRunner for proper state management and interactive menus.
    """
    import asyncio

    root = Path(project_root).resolve()

    print(f"\n{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}SE3 Loop + Collab{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}")
    print(f"\nBase prompt: {prompt}")
    print(f"Iterations: {iterations}")
    print(f"Project: {root}")
    print(f"\n{YELLOW}Each iteration runs as a collab session with parallel workers{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}\n")

    # Use LoopCollabRunner for proper state management
    from ..loop_collab import LoopCollabRunner

    async def _run_loop():
        runner = LoopCollabRunner(
            base_prompt=prompt,
            iterations=iterations,
            project_root=root,
            max_parallel=3,
            mock=mock,
        )
        return await runner.run()

    try:
        success = asyncio.run(_run_loop())
        if success:
            print(f"\n{GREEN}[SE3 Loop] All iterations completed successfully{RESET}")
        else:
            print(f"\n{YELLOW}[SE3 Loop] Loop ended with some issues{RESET}")
    except KeyboardInterrupt:
        print(f"\n{YELLOW}[SE3 Loop] Interrupted by user{RESET}")
    except Exception as e:
        print(f"\n{MAGENTA}[SE3 Loop] Error: {e}{RESET}")
        import traceback
        traceback.print_exc()

    print(f"\n{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}SE3 Loop + Collab Complete{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}\n")


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

    # Initialize loop state for Ctrl-C handling
    loop_state = LoopState()

    print(f"\n{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}SE3 Loop{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}")
    print(f"\nBase prompt: {prompt}")
    print(f"Iterations: {iterations}")
    print(f"Project: {root}")
    print(f"Claude: {claude_cmd}")
    print(f"Summary: {'disabled' if no_summary else 'enabled (use --no-summary to disable)'}")
    print(f"\n{YELLOW}Press Ctrl+C once for supplemental prompt mode{RESET}")
    print(f"{YELLOW}Press Ctrl+C twice to exit{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}\n")

    previous_summary = None

    for iteration in range(1, iterations + 1):
        # Reset supplemental mode for this iteration
        loop_state.in_supplemental_mode = False
        loop_state.should_exit = False

        print(f"\n{BOLD}{'─' * 60}{RESET}")
        print(f"{BOLD}Iteration {iteration} / {iterations}{RESET}")
        print(f"{BOLD}{'─' * 60}{RESET}\n")

        change_name = None
        change_dir = None

        if not quick:
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
        else:
            print(f"{CYAN}[SE3 Loop] Quick mode: skipping formal change creation{RESET}")

        # Build prompt text
        prompt_text = loop_state.get_full_prompt(
            base_prompt=prompt,
            iteration=iteration,
            iterations=iterations,
            change_name=change_name,
            quick=quick,
            previous_summary=previous_summary
        )

        # Inner loop to handle supplemental prompts within the same iteration
        iteration_complete = False
        while not iteration_complete:
            print(f"{CYAN}[SE3 Loop] Starting Claude Code...{RESET}\n")
            print(f"{'─' * 60}")

            # Run claude with loop_state for Ctrl-C handling
            exit_code, was_interrupted = run_claude_with_renderer(claude_cmd, prompt_text, loop_state=loop_state)

            print(f"\n{'─' * 60}")

            # Handle supplemental mode after claude finishes
            if was_interrupted and not loop_state.should_exit:
                print(f"\n{YELLOW}[SE3 Loop] Supplemental mode - enter additional prompt (press Enter to skip):{RESET}")
                print(f"{YELLOW}Press Ctrl-C again to exit the loop{RESET}")
                try:
                    # Reset supplemental mode flag before asking for input
                    loop_state.in_supplemental_mode = False
                    supplemental = input("> ")
                    if supplemental.strip():
                        loop_state.supplemental_prompts.append(supplemental.strip())
                        print(f"{GREEN}[SE3 Loop] Supplemental prompt added. Restarting Claude with updated prompt...{RESET}")
                        # Rebuild prompt with supplemental prompts and restart this iteration
                        prompt_text = loop_state.get_full_prompt(
                            base_prompt=prompt,
                            iteration=iteration,
                            iterations=iterations,
                            change_name=change_name,
                            quick=quick,
                            previous_summary=previous_summary
                        )
                        continue  # Restart the inner loop with updated prompt
                    else:
                        print(f"{GRAY}[SE3 Loop] No supplemental prompt added. Continuing...{RESET}")
                except EOFError:
                    print(f"{GRAY}[SE3 Loop] No supplemental prompt added.{RESET}")
                except KeyboardInterrupt:
                    # User pressed Ctrl-C during input - exit the loop
                    print(f"\n{YELLOW}[SE3 Loop] Second Ctrl-C pressed, exiting...{RESET}")
                    loop_state.should_exit = True
                    exit_code = 130

            iteration_complete = True

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
            if not quick and change_name:
                change_dir = root / "openspec" / "changes" / change_name
                print(f"\n{CYAN}[SE3 Loop] Generating summary for next iteration...{RESET}")
                previous_summary = run_claude_summary(claude_cmd, change_dir)
                print(f"{GRAY}{DIM}Summary: {previous_summary[:100]}{'...' if len(previous_summary) > 100 else ''}{RESET}")
            else:
                # Quick mode: use a simpler summary approach
                previous_summary = f"Iteration {iteration} completed successfully."

        if iteration < iterations and exit_code != 130:
            print(f"\n[SE3 Loop] Continuing in 2 seconds...")
            time.sleep(2)

    print(f"\n{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}SE3 Loop Complete{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}\n")
