#!/usr/bin/env python3
"""
SE3 Collaboration MCP Server

Provides human-as-MCP tools for worker and manager agents running in claude -p mode.
Implements the escalation chain: Worker → Manager → Human.

Tools:
  - ask_human: Request human input (blocks until response or timeout)
  - notify_human: Non-blocking notification to human
  - report_progress: Heartbeat + progress update for health monitoring
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# MCP protocol via stdio
# This server implements the MCP (Model Context Protocol) stdio transport

PROJECT_ROOT = os.environ.get("SE3_PROJECT_ROOT", os.getcwd())
COLLAB_DIR = Path(PROJECT_ROOT) / ".collab"
HUMAN_CALLS_DIR = Path(PROJECT_ROOT) / "human-calls"
POLL_INTERVAL = 5  # seconds between polls for human response
MAX_WAIT = 600     # max seconds to wait for human response (10 min)


def ensure_dirs():
    COLLAB_DIR.mkdir(parents=True, exist_ok=True)
    HUMAN_CALLS_DIR.mkdir(parents=True, exist_ok=True)
    (COLLAB_DIR / "tasks").mkdir(exist_ok=True)
    (COLLAB_DIR / "events").mkdir(exist_ok=True)


def write_human_call(question: str, call_type: str = "decision",
                     urgent: bool = False, options: list = None) -> Path:
    """Write a human call file and return its path."""
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = question[:40].replace(" ", "-").lower()
    slug = "".join(c for c in slug if c.isalnum() or c == "-")
    filename = f"{timestamp}-{slug}.md"
    filepath = HUMAN_CALLS_DIR / filename

    options_text = ""
    if options:
        options_text = "\n### Options\n"
        for i, opt in enumerate(options, 1):
            options_text += f"{i}. {opt}\n"

    content = f"""## Request: {question}
**Type**: {call_type}
**Urgency**: {"high" if urgent else "normal"}
**Source**: {os.environ.get("SE3_AGENT_ROLE", "worker")}
**Task**: {os.environ.get("SE3_TASK_ID", "unknown")}
{options_text}
### Context
{question}

### Response
<!-- Human: write your response below -->
"""
    filepath.write_text(content)
    return filepath


def wait_for_response(filepath: Path, timeout: int = MAX_WAIT) -> str | None:
    """Poll a human call file until human writes a response."""
    start = time.time()
    marker = "<!-- Human: write your response below -->"

    while time.time() - start < timeout:
        content = filepath.read_text()
        response_section = content.split("### Response")[-1] if "### Response" in content else ""

        # Check if human has replaced the placeholder
        if marker not in response_section and response_section.strip():
            # Human wrote something
            response = response_section.strip()
            # Rename to .responded.md
            responded = filepath.with_suffix(".responded.md")
            filepath.rename(responded)
            return response

        time.sleep(POLL_INTERVAL)

    return None


def update_task_health(task_id: str, message: str, percent: int = None):
    """Update task health info for the orchestrator's health monitor."""
    task_file = COLLAB_DIR / "tasks" / f"{task_id}.json"
    if not task_file.exists():
        return

    task = json.loads(task_file.read_text())
    task.setdefault("health", {})
    task["health"]["last_activity"] = datetime.now().isoformat()
    task["health"]["last_message"] = message
    if percent is not None:
        task["health"]["percent"] = percent

    task_file.write_text(json.dumps(task, indent=2))


# =============================================================================
# MCP Protocol Implementation (stdio transport)
# =============================================================================

def send_response(id: int, result: dict):
    msg = {"jsonrpc": "2.0", "id": id, "result": result}
    out = json.dumps(msg)
    sys.stdout.write(f"Content-Length: {len(out)}\r\n\r\n{out}")
    sys.stdout.flush()


def send_error(id: int, code: int, message: str):
    msg = {"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}
    out = json.dumps(msg)
    sys.stdout.write(f"Content-Length: {len(out)}\r\n\r\n{out}")
    sys.stdout.flush()


TOOLS = [
    {
        "name": "ask_human",
        "description": (
            "Request input from a human. Follows the human-as-MCP principle. "
            "Blocks until the human responds or timeout is reached. "
            "Use this when you need a decision, clarification, or action from a human."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question or request for the human"
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of choices for the human"
                },
                "urgent": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, marks as high urgency"
                },
                "call_type": {
                    "type": "string",
                    "enum": ["decision", "action", "information"],
                    "default": "decision",
                    "description": "Type of human call"
                }
            },
            "required": ["question"]
        }
    },
    {
        "name": "notify_human",
        "description": (
            "Send a non-blocking notification to the human. "
            "Returns immediately without waiting for response. "
            "Use for progress updates, warnings, or informational messages."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "The notification message"
                },
                "level": {
                    "type": "string",
                    "enum": ["info", "warning", "error"],
                    "default": "info"
                }
            },
            "required": ["message"]
        }
    },
    {
        "name": "report_progress",
        "description": (
            "Report progress on the current task. Also serves as a heartbeat "
            "for the health monitoring system. Call this periodically during "
            "long-running operations to prevent the orchestrator from marking "
            "you as stale."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task ID (e.g., task-001)"
                },
                "message": {
                    "type": "string",
                    "description": "Description of current progress"
                },
                "percent": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100,
                    "description": "Optional completion percentage"
                }
            },
            "required": ["task_id", "message"]
        }
    }
]


def handle_tool_call(name: str, arguments: dict) -> dict:
    """Execute a tool and return the result."""
    ensure_dirs()

    if name == "ask_human":
        question = arguments["question"]
        options = arguments.get("options")
        urgent = arguments.get("urgent", False)
        call_type = arguments.get("call_type", "decision")

        filepath = write_human_call(question, call_type, urgent, options)

        # Also print to stderr so it shows in terminal
        print(f"\n[MCP] Human call written: {filepath}", file=sys.stderr)
        if urgent:
            print(f"[MCP] ⚠ URGENT: {question}", file=sys.stderr)

        response = wait_for_response(filepath)

        if response is None:
            return {
                "content": [{
                    "type": "text",
                    "text": json.dumps({
                        "status": "timeout",
                        "message": f"Human did not respond within {MAX_WAIT}s. "
                                   f"Call file: {filepath.name}. "
                                   "Consider exiting with code 2 (blocked) to let "
                                   "the orchestrator handle this."
                    })
                }]
            }

        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "status": "responded",
                    "response": response
                })
            }]
        }

    elif name == "notify_human":
        message = arguments["message"]
        level = arguments.get("level", "info")

        # Write notification file (non-blocking)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        notify_file = COLLAB_DIR / "events" / f"{timestamp}-notify-{level}.json"
        notify_file.write_text(json.dumps({
            "type": "notification",
            "level": level,
            "message": message,
            "source": os.environ.get("SE3_AGENT_ROLE", "worker"),
            "task_id": os.environ.get("SE3_TASK_ID", "unknown"),
            "timestamp": datetime.now().isoformat()
        }, indent=2))

        print(f"[MCP] [{level.upper()}] {message}", file=sys.stderr)

        return {
            "content": [{
                "type": "text",
                "text": json.dumps({"status": "sent"})
            }]
        }

    elif name == "report_progress":
        task_id = arguments["task_id"]
        message = arguments["message"]
        percent = arguments.get("percent")

        update_task_health(task_id, message, percent)

        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "status": "recorded",
                    "task_id": task_id,
                    "message": message
                })
            }]
        }

    else:
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({"error": f"Unknown tool: {name}"})
            }],
            "isError": True
        }


def read_message() -> dict | None:
    """Read a JSON-RPC message from stdin (MCP stdio transport)."""
    # Read headers
    headers = {}
    while True:
        line = sys.stdin.readline()
        if not line:
            return None
        line = line.strip()
        if line == "":
            break
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip()] = value.strip()

    content_length = int(headers.get("Content-Length", 0))
    if content_length == 0:
        return None

    body = sys.stdin.read(content_length)
    return json.loads(body)


def main():
    """MCP server main loop."""
    ensure_dirs()

    while True:
        msg = read_message()
        if msg is None:
            break

        method = msg.get("method", "")
        msg_id = msg.get("id")
        params = msg.get("params", {})

        if method == "initialize":
            send_response(msg_id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {}
                },
                "serverInfo": {
                    "name": "se3-collab",
                    "version": "1.0.0"
                }
            })

        elif method == "notifications/initialized":
            pass  # No response needed for notifications

        elif method == "tools/list":
            send_response(msg_id, {"tools": TOOLS})

        elif method == "tools/call":
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})
            try:
                result = handle_tool_call(tool_name, arguments)
                send_response(msg_id, result)
            except Exception as e:
                send_error(msg_id, -32000, str(e))

        elif method == "ping":
            send_response(msg_id, {})

        else:
            if msg_id is not None:
                send_error(msg_id, -32601, f"Method not found: {method}")


if __name__ == "__main__":
    main()
