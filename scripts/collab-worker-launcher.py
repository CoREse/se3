#!/usr/bin/env python3
"""Launch collab worker with activity-based monitoring and command fallback.

Usage:
    python3 collab-worker-launcher.py <task_id> <worktree> <prompt> [options]

Exit codes:
    0 - Success
    1 - Failure (all commands exhausted)
    124 - Timeout / Inactivity
"""

import argparse
import json
import os
import signal
import sys
from pathlib import Path

# Add parent to path for imports
tools_path = str(Path(__file__).parent.parent / "tools")
if tools_path not in sys.path:
    sys.path.insert(0, tools_path)

try:
    from se3_tools.claude_runner import ClaudeRunner
except ImportError as e:
    print(f"Error: Cannot import ClaudeRunner from {tools_path}: {e}", file=sys.stderr)
    sys.exit(1)


def signal_handler(signum, frame):
    """Handle SIGTERM/SIGINT gracefully."""
    print(f"[worker-launcher] Received signal {signum}, exiting...", file=sys.stderr)
    sys.exit(128 + signum)


def main():
    # Set up signal handlers
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    parser = argparse.ArgumentParser(description="Launch collab worker with monitoring")
    parser.add_argument("task_id", help="Task ID")
    parser.add_argument("worktree", help="Worktree directory")
    parser.add_argument("prompt", help="Worker prompt (or @file to read from file)")
    parser.add_argument("--log-file", help="Log file path")
    parser.add_argument("--wall-timeout", type=int, default=3600, help="Wall clock timeout (seconds)")
    parser.add_argument("--inactivity-timeout", type=int, default=300, help="Inactivity timeout (seconds)")
    parser.add_argument("--model", help="Model to use")
    parser.add_argument("--mcp-config", help="MCP config file")
    parser.add_argument("--project-root", help="Project root directory")
    parser.add_argument("--config-file", help="Path to se3.config.yaml")

    args = parser.parse_args()

    project_root = Path(args.project_root) if args.project_root else Path.cwd()

    # Load prompt from file if @file syntax
    prompt = args.prompt
    if prompt.startswith("@"):
        prompt_file = Path(prompt[1:])
        if prompt_file.exists():
            prompt = prompt_file.read_text()
        else:
            print(f"Error: Prompt file not found: {prompt_file}", file=sys.stderr)
            sys.exit(1)

    # Build claude args
    claude_args = [
        "--dangerously-skip-permissions",
        "-p", prompt,
        "--max-turns", "50",
    ]
    if args.model:
        claude_args.extend(["--model", args.model])
    if args.mcp_config:
        claude_args.extend(["--mcp-config", args.mcp_config])

    # Environment
    env = {**dict(os.environ)}
    env["SE3_TASK_ID"] = args.task_id
    env["SE3_AGENT_ROLE"] = "worker"
    env["SE3_PROJECT_ROOT"] = str(project_root)
    env.pop("CLAUDECODE", None)  # Avoid nested session detection

    # Load commands from config if provided
    commands = None
    if args.config_file and Path(args.config_file).exists():
        import yaml
        try:
            with open(args.config_file) as f:
                cfg = yaml.safe_load(f)
            if cfg and "claude_commands" in cfg:
                commands = cfg["claude_commands"]
        except Exception:
            pass

    runner = ClaudeRunner(project_root if commands is None else None, commands)

    log_path = Path(args.log_file) if args.log_file else None

    print(f"[worker-launcher] Starting worker {args.task_id}", file=sys.stderr)
    print(f"[worker-launcher] Worktree: {args.worktree}", file=sys.stderr)
    print(f"[worker-launcher] Log: {log_path}", file=sys.stderr)

    # Ensure exit code file directory exists before running
    exitcode_file = project_root / ".collab" / "tasks" / f".exitcode-{args.task_id}"
    cmd_info_file = project_root / ".collab" / "tasks" / f".cmdinfo-{args.task_id}"
    exitcode_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        result = runner.run_with_monitor(
            args=claude_args,
            log_file=log_path,
            wall_timeout=args.wall_timeout,
            inactivity_timeout=args.inactivity_timeout,
            cwd=Path(args.worktree),
            env=env,
        )

        # Write exit code for orchestrator to read
        exitcode_file.write_text(str(result.returncode))

        # Also write which command was used
        cmd_info = {
            "cmd_used": result.cmd_used,
            "cmd_index": result.cmd_index,
            "was_retry": result.was_retry,
        }
        cmd_info_file.write_text(json.dumps(cmd_info))

        print(f"[worker-launcher] Exit code: {result.returncode}", file=sys.stderr)
        print(f"[worker-launcher] Command used: {result.cmd_used}", file=sys.stderr)

        sys.exit(result.returncode)

    except Exception as e:
        # Ensure we always write an exit code even on crash
        print(f"[worker-launcher] CRASH: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        exitcode_file.write_text("1")
        cmd_info_file.write_text(json.dumps({
            "cmd_used": "none",
            "cmd_index": -1,
            "was_retry": False,
            "error": str(e),
        }))
        sys.exit(1)


if __name__ == "__main__":
    main()
