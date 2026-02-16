#!/usr/bin/env python3
"""Launch collab manager with activity-based monitoring and command fallback.

Usage:
    python3 collab-manager-launcher.py <event_type> <context> [options]

Exit codes:
    0 - Success (outputs JSON to stdout)
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
    print(f"[manager-launcher] Received signal {signum}, exiting...", file=sys.stderr)
    sys.exit(128 + signum)

def main():
    # Set up signal handlers
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    parser = argparse.ArgumentParser(description="Launch collab manager with monitoring")
    parser.add_argument("event_type", help="Event type (plan, review, etc.)")
    parser.add_argument("context", help="Event context (or @file to read from file)")
    parser.add_argument("--log-file", help="Log file path")
    parser.add_argument("--wall-timeout", type=int, default=900, help="Wall clock timeout (seconds)")
    parser.add_argument("--inactivity-timeout", type=int, default=120, help="Inactivity timeout (seconds)")
    parser.add_argument("--model", help="Model to use")
    parser.add_argument("--mcp-config", help="MCP config file")
    parser.add_argument("--project-root", help="Project root directory")
    parser.add_argument("--config-file", help="Path to se3.config.yaml")
    parser.add_argument("--rules-file", help="Manager rules file")
    parser.add_argument("--tasks-file", help="Tasks summary file")
    parser.add_argument("--base-branch", default="master", help="Base branch")

    args = parser.parse_args()

    project_root = Path(args.project_root) if args.project_root else Path.cwd()

    # Load context from file if @file syntax
    context = args.context
    if context.startswith("@"):
        context_file = Path(context[1:])
        if context_file.exists():
            context = context_file.read_text()
        else:
            print(f"Error: Context file not found: {context_file}", file=sys.stderr)
            sys.exit(1)

    # Load rules
    rules = "You are a manager agent. Respond with valid JSON."
    if args.rules_file and Path(args.rules_file).exists():
        rules = Path(args.rules_file).read_text()

    # Load tasks summary
    tasks_summary = "(no tasks yet)"
    if args.tasks_file and Path(args.tasks_file).exists():
        tasks_summary = Path(args.tasks_file).read_text()

    # Build prompt
    prompt = f"""{rules}

---

## Current State
Project root: {project_root}
Base branch: {args.base_branch}

## All Tasks
{tasks_summary}

## Event
Type: {args.event_type}
Context:
{context}

## Instructions
Analyze the event and decide the next action. Respond ONLY with valid JSON matching this schema:
{{
  "action": "plan|merge|reject|retry|split|escalate|complete",
  "tasks": [...],
  "target_task": "task-id",
  "merge_branch": "branch-name",
  "retry_prompt": "adjusted prompt for retry",
  "reason": "explanation",
  "summary": "human-readable summary of decision"
}}

Rules:
- For 'plan': include full task definitions in 'tasks' array
- For 'merge': set target_task and merge_branch
- For 'reject': set target_task and reason (becomes feedback for worker retry)
- For 'retry': set target_task and retry_prompt
- For 'split': set target_task and new sub-tasks in 'tasks'
- For 'escalate': set reason (will be sent to human)
- For 'complete': when all tasks are merged and done
- If unsure, use 'escalate' rather than guessing
"""

    # Build claude args
    # Note: -p/--print is a flag (no value), prompt is a positional argument
    claude_args = [
        "--dangerously-skip-permissions",
        "--print",
        "--output-format", "json",
        "--max-turns", "3",
        prompt,  # positional argument
    ]
    if args.model:
        claude_args.extend(["--model", args.model])
    if args.mcp_config:
        claude_args.extend(["--mcp-config", args.mcp_config])

    # Environment
    env = {**dict(os.environ)}
    env["SE3_AGENT_ROLE"] = "manager"
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

    print(f"[manager-launcher] Starting manager for event: {args.event_type}", file=sys.stderr)
    print(f"[manager-launcher] Log: {log_path}", file=sys.stderr)

    # Ensure cmd_info directory exists
    cmd_info_file = project_root / ".collab" / ".manager-cmdinfo.json"
    cmd_info_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        result = runner.run_with_monitor(
            args=claude_args,
            log_file=log_path,
            wall_timeout=args.wall_timeout,
            inactivity_timeout=args.inactivity_timeout,
            cwd=project_root,
            env=env,
        )

        # Write command info
        cmd_info = {
            "cmd_used": result.cmd_used,
            "cmd_index": result.cmd_index,
            "was_retry": result.was_retry,
            "event_type": args.event_type,
        }
        cmd_info_file.write_text(json.dumps(cmd_info))

        print(f"[manager-launcher] Exit code: {result.returncode}", file=sys.stderr)
        print(f"[manager-launcher] Command used: {result.cmd_used}", file=sys.stderr)

        # Output the result JSON to stdout for orchestrator to parse (only on success)
        if result.success and result.output:
            # Try to extract JSON from the output
            import re
            try:
                # First, try to find JSON in the Claude CLI envelope's "result" field
                envelope_match = re.search(r'\{[^{}]*"type"\s*:\s*"result"[^{}]*"result"\s*:\s*"([^"]*)"', result.output.replace('\n', ' '), re.DOTALL)
                if not envelope_match:
                    # Try without newlines removed
                    envelope_match = re.search(r'\{[^}]*"type"\s*:\s*"result"[^}]*"result"\s*:', result.output, re.DOTALL)

                search_text = result.output
                if envelope_match:
                    # Try to parse the envelope to get the result field
                    try:
                        # Find the full envelope JSON
                        env_start = result.output.find('{"type":"result"')
                        if env_start >= 0:
                            # Find the matching closing brace
                            depth = 0
                            env_end = env_start
                            in_string = False
                            escape_next = False
                            for i, c in enumerate(result.output[env_start:]):
                                if escape_next:
                                    escape_next = False
                                    continue
                                if c == '\\':
                                    escape_next = True
                                    continue
                                if c == '"' and not in_string:
                                    in_string = True
                                elif c == '"' and in_string:
                                    in_string = False
                                elif not in_string:
                                    if c == '{':
                                        depth += 1
                                    elif c == '}':
                                        depth -= 1
                                        if depth == 0:
                                            env_end = env_start + i + 1
                                            break

                            envelope_str = result.output[env_start:env_end]
                            envelope = json.loads(envelope_str)
                            search_text = envelope.get('result', result.output)
                    except Exception:
                        pass

                # Strip markdown code fences
                text = search_text.strip()
                text = re.sub(r'^```json\s*\n', '', text)
                text = re.sub(r'\n```\s*$', '', text)
                text = text.strip()

                # Find all JSON objects (handle nested braces)
                found = False
                # Try to find the outermost JSON object with "action" field
                # by scanning from the start for valid JSON
                i = 0
                while i < len(text):
                    if text[i] == '{':
                        # Try to find matching closing brace
                        depth = 0
                        for j in range(i, len(text)):
                            if text[j] == '{':
                                depth += 1
                            elif text[j] == '}':
                                depth -= 1
                                if depth == 0:
                                    # Found a complete object
                                    try:
                                        obj = json.loads(text[i:j+1])
                                        if 'action' in obj:
                                            print(json.dumps(obj))
                                            found = True
                                            break
                                    except json.JSONDecodeError:
                                        pass
                                    break
                    if found:
                        break
                    i += 1

                if not found:
                    # Fallback: try regex approach
                    json_pattern = r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}'
                    matches = list(re.finditer(json_pattern, text, re.DOTALL))
                    for match in reversed(matches):
                        try:
                            obj = json.loads(match.group(0))
                            if 'action' in obj:
                                print(json.dumps(obj))
                                found = True
                                break
                        except json.JSONDecodeError:
                            continue

                if not found:
                    print(f'{{"action": "escalate", "reason": "Manager output did not contain valid JSON"}}', file=sys.stderr)
            except Exception as e:
                print(f'{{"action": "escalate", "reason": "Failed to parse manager output: {e}"}}', file=sys.stderr)
        elif not result.success:
            # On failure, output escalation JSON so orchestrator can handle it
            # The actual error details are in stderr/log
            error_reason = "all_commands_failed"
            if result.returncode == 124:
                error_reason = "timeout_or_inactivity"
            print(f'{{"action": "escalate", "reason": "Manager {error_reason} (last cmd: {result.cmd_used})"}}')

        sys.exit(result.returncode)

    except Exception as e:
        # Ensure we always output valid JSON even on crash
        print(f"[manager-launcher] CRASH: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        cmd_info_file.write_text(json.dumps({
            "cmd_used": "none",
            "cmd_index": -1,
            "was_retry": False,
            "event_type": args.event_type,
            "error": str(e),
        }))
        # Output valid escalation JSON to stdout for orchestrator
        print(f'{{"action": "escalate", "reason": "Manager launcher crashed: {e}"}}')
        sys.exit(1)


if __name__ == "__main__":
    main()
