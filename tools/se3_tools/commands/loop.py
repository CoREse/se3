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
from pathlib import Path
from typing import Optional

from ..config import load_claude_commands


def sanitize_change_name(description: str) -> str:
    """Convert a description into a valid change name."""
    name = description.lower().strip()
    name = "".join(c for c in name if (ord(c) < 128 and c.isalnum()) or c in " -_/")
    name = name.replace(" ", "-").replace("_", "-").replace("/", "-")
    name = re.sub(r'-+', '-', name)
    if len(name) > 40:
        name = name[:40].rsplit("-", 1)[0]
    return name.strip("-") or "loop-task"


def run_claude_iteration(
    claude_cmd: str,
    prompt_file: Path,
    timeout_sec: int = 1800,
    capture_output: bool = False
) -> int:
    """Run claude for one iteration.

    Args:
        claude_cmd: The claude command to use
        prompt_file: Path to the prompt file
        timeout_sec: Timeout in seconds
        capture_output: If True, capture output and return it. If False, output goes directly to terminal.

    Returns:
        Exit code from claude
    """
    env = {**dict(os.environ)}
    env.pop("CLAUDECODE", None)

    cmd = [
        claude_cmd,
        "--dangerously-skip-permissions",
        "--print",
        "--max-turns", "0",
        str(prompt_file)
    ]

    try:
        if capture_output:
            # For testing/debugging - capture output
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env=env,
                timeout=timeout_sec
            )
            return result.returncode
        else:
            # Direct to terminal - most reliable
            result = subprocess.run(
                cmd,
                env=env,
                timeout=timeout_sec
            )
            return result.returncode
    except subprocess.TimeoutExpired:
        print(f"\n[SE3 Loop] Session timed out ({timeout_sec}s limit)", file=sys.stderr)
        return 124
    except FileNotFoundError:
        print(f"\n[SE3 Loop] Error: '{claude_cmd}' not found", file=sys.stderr)
        return 127
    except Exception as e:
        print(f"\n[SE3 Loop] Error: {e}", file=sys.stderr)
        return 1


def run_exclusive_loop(
    prompt: str,
    project_root: str = ".",
    iterations: int = 10,
    quick: bool = False,
) -> None:
    """Run the loop - execute claude directly for each iteration."""
    root = Path(project_root).resolve()

    # Load claude command
    commands = load_claude_commands(root)
    claude_cmd = commands[0]["cmd"] if commands else "claude"

    if not shutil.which(claude_cmd):
        print(f"\n[SE3 Loop] Error: '{claude_cmd}' not found in PATH")
        return

    base_name = sanitize_change_name(prompt)
    openspec_cmd = shutil.which("openspec") or "openspec"

    print(f"\n{'=' * 60}")
    print("SE3 Loop")
    print(f"{'=' * 60}")
    print(f"\nPrompt: {prompt}")
    print(f"Iterations: {iterations}")
    print(f"Project: {root}")
    print(f"Claude: {claude_cmd}")
    print(f"\nPress Ctrl+C to stop")
    print(f"{'=' * 60}\n")

    for iteration in range(1, iterations + 1):
        print(f"\n{'─' * 60}")
        print(f"Iteration {iteration} / {iterations}")
        print(f"{'─' * 60}\n")

        # Generate unique change name
        change_name = f"{base_name}-{iteration:02d}"
        counter = 1
        while (root / "openspec" / "changes" / change_name).exists():
            change_name = f"{base_name}-{iteration:02d}-{counter}"
            counter += 1

        print(f"[SE3 Loop] Creating change: {change_name}")

        # Create change
        result = subprocess.run(
            [openspec_cmd, "new", "change", change_name],
            cwd=root,
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            print(f"[SE3 Loop] Failed to create change, retrying...")
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

        print(f"[SE3 Loop] Starting Claude Code...\n")
        print(f"{'─' * 60}")

        try:
            exit_code = run_claude_iteration(claude_cmd, prompt_file)
        finally:
            prompt_file.unlink(missing_ok=True)

        print(f"{'─' * 60}")

        if exit_code == 0:
            print(f"\n[SE3 Loop] Iteration {iteration} completed successfully")
        elif exit_code == 124:
            print(f"\n[SE3 Loop] Iteration {iteration} timed out")
        else:
            print(f"\n[SE3 Loop] Iteration {iteration} exited with code {exit_code}")

        if iteration < iterations:
            print(f"\n[SE3 Loop] Continuing in 2 seconds...")
            time.sleep(2)

    print(f"\n{'=' * 60}")
    print("SE3 Loop Complete")
    print(f"{'=' * 60}\n")
