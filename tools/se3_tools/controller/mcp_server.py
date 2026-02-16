"""MCP Server for SE3 Controller.

Provides tools for Claude to communicate with the External Controller daemon.
"""

import json
import os
import sys
from pathlib import Path
from typing import Any

# Try to import mcp, fallback to mock if not available
try:
    from mcp.server import Server
    from mcp.types import TextContent, Tool
    HAS_MCP = True
except ImportError:
    HAS_MCP = False

# Controller communication path
CONTROLLER_DIR = Path.home() / ".se3" / "controller"
EVENTS_DIR = CONTROLLER_DIR / "events"


class ControllerMCPClient:
    """Client to communicate with External Controller via file system."""

    def __init__(self):
        self.project_root = Path(os.environ.get("SE3_PROJECT_ROOT", os.getcwd()))
        self.session_id = os.environ.get("SE3_SESSION_ID")
        self.agent_role = os.environ.get("SE3_AGENT_ROLE", "worker")  # worker, manager, interactive

    def _send_event(self, event_type: str, data: dict) -> dict:
        """Send event to controller via file."""
        EVENTS_DIR.mkdir(parents=True, exist_ok=True)

        event = {
            "type": event_type,
            "timestamp": json.dumps({}),  # Will be filled with actual time
            "session_id": self.session_id,
            "agent_role": self.agent_role,
            "data": data,
        }

        # Write event file
        event_file = EVENTS_DIR / f"{event_type}-{self.agent_role}-{os.getpid()}.json"
        event_file.write_text(json.dumps(event, indent=2))

        return {"status": "sent", "event_file": str(event_file)}

    def report_task_complete(self, task_id: str, summary: str, success: bool = True) -> dict:
        """Report task completion to controller."""
        return self._send_event("task_complete", {
            "task_id": task_id,
            "summary": summary,
            "success": success,
            "pid": os.getpid(),
        })

    def request_human_input(self, question: str, urgency: str = "normal") -> dict:
        """Request input from human."""
        return self._send_event("human_input_request", {
            "question": question,
            "urgency": urgency,
        })

    def trigger_commit(self, reason: str, message: str = "") -> dict:
        """Request immediate commit."""
        return self._send_event("commit_request", {
            "reason": reason,
            "message": message,
        })

    def spawn_worker_task(self, task_spec: dict) -> dict:
        """Request controller to spawn a worker task."""
        return self._send_event("spawn_worker", {
            "task_spec": task_spec,
        })

    def report_status(self, status: str, details: dict = None) -> dict:
        """Report current status to controller."""
        return self._send_event("status_report", {
            "status": status,
            "details": details or {},
        })

    def request_pause(self, reason: str) -> dict:
        """Request session pause."""
        return self._send_event("pause_request", {
            "reason": reason,
        })


def create_mcp_server():
    """Create MCP server instance."""
    if not HAS_MCP:
        raise RuntimeError("MCP package not installed. Run: pip install mcp")

    server = Server("se3-controller")
    client = ControllerMCPClient()

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="report_task_complete",
                description="Report that a task has been completed",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string"},
                        "summary": {"type": "string"},
                        "success": {"type": "boolean", "default": True},
                    },
                    "required": ["task_id", "summary"],
                },
            ),
            Tool(
                name="request_human_input",
                description="Request input from human user",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "urgency": {"type": "string", "enum": ["low", "normal", "high"], "default": "normal"},
                    },
                    "required": ["question"],
                },
            ),
            Tool(
                name="trigger_commit",
                description="Trigger immediate git commit",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "reason": {"type": "string"},
                        "message": {"type": "string", "default": ""},
                    },
                    "required": ["reason"],
                },
            ),
            Tool(
                name="spawn_worker_task",
                description="Request controller to spawn a new worker task",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "task_spec": {"type": "object"},
                    },
                    "required": ["task_spec"],
                },
            ),
            Tool(
                name="report_status",
                description="Report current status to controller",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "status": {"type": "string"},
                        "details": {"type": "object"},
                    },
                    "required": ["status"],
                },
            ),
            Tool(
                name="request_pause",
                description="Request session pause",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "reason": {"type": "string"},
                    },
                    "required": ["reason"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        try:
            if name == "report_task_complete":
                result = client.report_task_complete(
                    arguments["task_id"],
                    arguments["summary"],
                    arguments.get("success", True),
                )
            elif name == "request_human_input":
                result = client.request_human_input(
                    arguments["question"],
                    arguments.get("urgency", "normal"),
                )
            elif name == "trigger_commit":
                result = client.trigger_commit(
                    arguments["reason"],
                    arguments.get("message", ""),
                )
            elif name == "spawn_worker_task":
                result = client.spawn_worker_task(arguments["task_spec"])
            elif name == "report_status":
                result = client.report_status(
                    arguments["status"],
                    arguments.get("details", {}),
                )
            elif name == "request_pause":
                result = client.request_pause(arguments["reason"])
            else:
                return [TextContent(type="text", text=f"Unknown tool: {name}")]

            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        except Exception as e:
            return [TextContent(type="text", text=f"Error: {str(e)}")]

    return server


def main():
    """Run MCP server."""
    if not HAS_MCP:
        print("Error: MCP package not installed", file=sys.stderr)
        print("Install with: pip install mcp", file=sys.stderr)
        sys.exit(1)

    server = create_mcp_server()
    server.run()


if __name__ == "__main__":
    main()
