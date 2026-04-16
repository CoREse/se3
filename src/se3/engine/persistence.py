"""State persistence for the flow engine.

Handles JSON serialization/deserialization with atomic writes
to prevent state corruption during interruptions.
"""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .models import FlowInstance, FlowStatus, State, Step, StepStatus
from .schema import build_context_from_flow

logger = logging.getLogger(__name__)


class PersistenceManager:
    """Manages persistence of flow engine state.

    Uses atomic writes (write to temp file, then rename) to ensure
    state file integrity even if interrupted mid-write.
    """

    STATE_FILENAME = "engine.json"
    CONTEXT_FILENAME = "context.json"
    BACKUP_EXTENSION = ".bak"

    def __init__(self, project_root: Path):
        """Initialize with project root.

        Args:
            project_root: Root directory of the project
        """
        self.project_root = Path(project_root)
        self.state_dir = self.project_root / "se3" / "state"
        self.state_file = self.state_dir / self.STATE_FILENAME
        self.context_file = self.state_dir / self.CONTEXT_FILENAME

    def ensure_directories(self) -> None:
        """Ensure state directories exist."""
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def save_flow(self, flow: FlowInstance) -> Path:
        """Save flow instance to state file atomically.

        Args:
            flow: Flow instance to save

        Returns:
            Path to the saved state file
        """
        self.ensure_directories()

        # Update timestamp
        from datetime import datetime
        flow.updated_at = datetime.now()

        # Serialize to JSON
        data = flow.to_dict()
        json_content = json.dumps(data, indent=2, ensure_ascii=False, default=str)

        # Atomic write: write to temp file, then rename
        temp_file = self.state_file.with_suffix(".tmp")
        try:
            temp_file.write_text(json_content, encoding="utf-8")
            # Atomic rename on POSIX systems
            temp_file.replace(self.state_file)
        except Exception:
            # Clean up temp file on failure
            if temp_file.exists():
                temp_file.unlink(missing_ok=True)
            raise

        return self.state_file

    def load_flow(self) -> Optional[FlowInstance]:
        """Load flow instance from state file.

        Returns:
            FlowInstance if state file exists, None otherwise
        """
        if not self.state_file.exists():
            return None

        try:
            content = self.state_file.read_text(encoding="utf-8")
            data = json.loads(content)
            return FlowInstance.from_dict(data)
        except (json.JSONDecodeError, KeyError, ValueError):
            # Try backup if main file is corrupted
            backup_file = self.state_file.with_suffix(self.BACKUP_EXTENSION)
            if backup_file.exists():
                content = backup_file.read_text(encoding="utf-8")
                data = json.loads(content)
                return FlowInstance.from_dict(data)
            return None

    def load_flow_tolerant(self) -> Tuple[Optional[FlowInstance], List[str]]:
        """Load flow instance with maximum tolerance for corruption.

        Unlike load_flow(), this method:
        - Attempts to repair truncated JSON
        - Fills missing fields with defaults
        - Falls back to .bak file
        - Never raises exceptions

        Returns:
            Tuple of (FlowInstance or None, list of warning messages)
        """
        warnings: List[str] = []

        # Try main file first, then backup
        candidates = [self.state_file]
        backup_file = self.state_file.with_suffix(self.BACKUP_EXTENSION)
        if backup_file.exists():
            candidates.append(backup_file)

        for filepath in candidates:
            if not filepath.exists():
                continue

            try:
                content = filepath.read_text(encoding="utf-8")
            except Exception as e:
                warnings.append(f"Failed to read {filepath.name}: {e}")
                continue

            if not content.strip():
                warnings.append(f"{filepath.name} is empty")
                continue

            # Try normal parsing first
            try:
                data = json.loads(content)
                flow = FlowInstance.from_dict(data)
                if filepath != self.state_file:
                    warnings.append(f"Loaded from backup {filepath.name}")
                return flow, warnings
            except json.JSONDecodeError as e:
                warnings.append(f"JSON parse error in {filepath.name}: {e}")
                # Try to repair truncated JSON
                repaired = self._try_repair_json(content)
                if repaired is not None:
                    try:
                        flow = self._tolerant_from_dict(repaired, warnings)
                        warnings.append(f"Recovered from truncated JSON in {filepath.name}")
                        return flow, warnings
                    except Exception as e2:
                        warnings.append(f"Failed to deserialize repaired JSON: {e2}")
            except (KeyError, ValueError, TypeError) as e:
                warnings.append(f"Deserialization error in {filepath.name}: {e}")
                # Try with tolerant deserialization
                try:
                    data = json.loads(content)
                    flow = self._tolerant_from_dict(data, warnings)
                    return flow, warnings
                except Exception as e2:
                    warnings.append(f"Tolerant deserialization also failed: {e2}")

        if not any(f.exists() for f in candidates):
            warnings.append("No state file found")

        return None, warnings

    @staticmethod
    def _try_repair_json(content: str) -> Optional[dict]:
        """Try to repair truncated JSON by closing open brackets.

        Args:
            content: Potentially truncated JSON string

        Returns:
            Parsed dict if repair successful, None otherwise
        """
        # Count open/close brackets
        open_braces = content.count("{") - content.count("}")
        open_brackets = content.count("[") - content.count("]")

        if open_braces <= 0 and open_brackets <= 0:
            return None  # Not a truncation issue

        # Strip trailing incomplete values (partial strings, numbers, etc.)
        repaired = content.rstrip()
        # Remove trailing comma if present
        repaired = repaired.rstrip(",")

        # Close open brackets and braces
        repaired += "]" * max(0, open_brackets)
        repaired += "}" * max(0, open_braces)

        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            # Try more aggressive repair: strip last partial key-value pair
            # Find last complete value (ending with comma, }, or ])
            import re
            # Strip back to last clean boundary
            match = re.search(r'(.*[}\]",\d])\s*[^}\]]*$', content, re.DOTALL)
            if match:
                repaired = match.group(1)
                open_braces = repaired.count("{") - repaired.count("}")
                open_brackets = repaired.count("[") - repaired.count("]")
                repaired += "]" * max(0, open_brackets)
                repaired += "}" * max(0, open_braces)
                try:
                    return json.loads(repaired)
                except json.JSONDecodeError:
                    pass

        return None

    @staticmethod
    def _tolerant_from_dict(data: dict, warnings: List[str]) -> FlowInstance:
        """Create FlowInstance from dict with tolerance for missing fields.

        Args:
            data: Possibly incomplete dict
            warnings: List to append warnings to

        Returns:
            FlowInstance with defaults for missing fields
        """
        from datetime import datetime

        # Ensure required fields exist with defaults
        if "flow_id" not in data:
            data["flow_id"] = f"recovered_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            warnings.append("Missing flow_id, generated recovery ID")

        if "task_description" not in data:
            data["task_description"] = "(unknown - recovered from corrupted state)"
            warnings.append("Missing task_description")

        if "status" not in data:
            data["status"] = FlowStatus.FAILED.value
            warnings.append("Missing status, defaulting to FAILED")

        if "state" not in data or not isinstance(data.get("state"), dict):
            data["state"] = {}
            warnings.append("Missing or invalid state, using empty state")

        # Ensure state has required fields
        state_data = data["state"]
        if "steps" not in state_data:
            state_data["steps"] = {}
            warnings.append("Missing steps in state")
        if "step_history" not in state_data:
            state_data["step_history"] = list(state_data.get("steps", {}).keys())
        if "selected_steps" not in state_data:
            state_data["selected_steps"] = []
        if "context" not in state_data:
            state_data["context"] = {}

        return FlowInstance.from_dict(data)

    def create_backup(self) -> Optional[Path]:
        """Create a backup of the current state file.

        Returns:
            Path to backup file if successful, None otherwise
        """
        if not self.state_file.exists():
            return None

        backup_file = self.state_file.with_suffix(self.BACKUP_EXTENSION)
        try:
            import shutil
            shutil.copy2(self.state_file, backup_file)
            return backup_file
        except Exception:
            return None

    def clear_state(self) -> None:
        """Clear the current state (mark as completed/failed)."""
        if self.state_file.exists():
            # Move to archive instead of deleting
            archive_dir = self.state_dir / "archive"
            archive_dir.mkdir(exist_ok=True)

            from datetime import datetime
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            archived = archive_dir / f"engine_{timestamp}.json"

            self.state_file.rename(archived)

    def list_active_flows(self) -> List[Dict[str, Any]]:
        """List all active (non-archived) flow states.

        Returns:
            List of flow metadata dictionaries
        """
        flows = []
        if self.state_file.exists():
            try:
                flow = self.load_flow()
                if flow:
                    completed, total = flow.get_progress()
                    flows.append({
                        "flow_id": flow.flow_id,
                        "status": flow.status.value,
                        "task_description": flow.task_description[:100] + "..." if len(flow.task_description) > 100 else flow.task_description,
                        "progress": f"{completed}/{total}",
                        "updated_at": flow.updated_at.isoformat(),
                    })
            except Exception:
                pass
        return flows

    def list_all_flows(self) -> List[Dict[str, Any]]:
        """List all flows from all data sources: active, archived, and history-only.

        Combines se3/state/engine.json, se3/state/archive/engine_*.json,
        and se3/history/{flow_id}/ directories. De-duplicates by flow_id
        and sorts by updated_at descending.

        Returns:
            List of flow metadata dicts with keys:
              flow_id, status, task_description, progress, updated_at, source
        """
        import re
        from datetime import datetime

        seen: set = set()
        flows: List[Dict[str, Any]] = []

        # 1. Active flow from engine.json
        if self.state_file.exists():
            try:
                flow = self.load_flow()
                if flow:
                    completed, total = flow.get_progress()
                    desc = flow.task_description
                    flows.append({
                        "flow_id": flow.flow_id,
                        "status": flow.status.value,
                        "task_description": desc[:100] + "..." if len(desc) > 100 else desc,
                        "progress": f"{completed}/{total}",
                        "updated_at": flow.updated_at.isoformat(),
                        "source": "active",
                    })
                    seen.add(flow.flow_id)
            except Exception:
                pass

        # 2. Archived flows from se3/state/archive/
        archive_dir = self.state_dir / "archive"
        if archive_dir.exists():
            for archive_file in archive_dir.glob("engine_*.json"):
                try:
                    data = json.loads(archive_file.read_text(encoding="utf-8"))
                    flow_id = data.get("flow_id", "unknown")
                    if flow_id in seen:
                        continue
                    seen.add(flow_id)
                    desc = data.get("task_description", "No description")
                    updated = data.get("updated_at") or datetime.fromtimestamp(
                        archive_file.stat().st_mtime
                    ).isoformat()
                    flows.append({
                        "flow_id": flow_id,
                        "status": data.get("status", "unknown"),
                        "task_description": desc[:100] + "..." if len(desc) > 100 else desc,
                        "progress": "-",
                        "updated_at": updated,
                        "source": "archived",
                    })
                except Exception:
                    continue

        # 3. History-only flows from se3/history/{flow_id}/
        history_dir = self.project_root / "se3" / "history"
        if history_dir.exists():
            for flow_dir in history_dir.iterdir():
                if not flow_dir.is_dir():
                    continue
                flow_id = flow_dir.name
                if flow_id in seen:
                    continue
                seen.add(flow_id)

                # updated_at = mtime of most recent file in the directory
                try:
                    latest_mtime = max(
                        (f.stat().st_mtime for f in flow_dir.iterdir() if f.is_file()),
                        default=0,
                    )
                    updated_at = datetime.fromtimestamp(latest_mtime).isoformat() if latest_mtime else ""
                except Exception:
                    updated_at = ""

                task_description = self.extract_history_summary(flow_dir)
                flows.append({
                    "flow_id": flow_id,
                    "status": "history",
                    "task_description": task_description,
                    "progress": "-",
                    "updated_at": updated_at,
                    "source": "history",
                })

        # Sort by updated_at descending (empty strings sort to end)
        flows.sort(key=lambda f: f.get("updated_at", ""), reverse=True)
        return flows

    @staticmethod
    def extract_history_summary(flow_dir: "Path") -> str:
        """Extract a short task description from the first JSONL file in a history dir."""
        import re

        jsonl_files = sorted(flow_dir.glob("*.jsonl"))
        if not jsonl_files:
            return "(no history data)"
        try:
            first_line = jsonl_files[0].read_text(encoding="utf-8").split("\n")[0]
            data = json.loads(first_line)
            content = data.get("content", "")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        content = block.get("text", "")
                        break
                else:
                    content = str(content)
            # Extract embedded task description if present
            match = re.search(
                r"Task description:\s*-+\s*(.*?)\s*-+",
                content,
                re.DOTALL,
            )
            if match:
                desc = match.group(1).strip()
                return desc[:100] + "..." if len(desc) > 100 else desc
            # Fallback: truncated raw content
            return content[:100] + "..." if len(content) > 100 else content
        except Exception:
            return "(no state data)"

    def save_context(self, context: Dict[str, Any]) -> Path:
        """Save AI context export for handoff/resumption.

        This is a separate file optimized for AI consumption,
        containing the essential context for resuming work.

        Args:
            context: Context dictionary to save

        Returns:
            Path to the saved context file
        """
        self.ensure_directories()

        json_content = json.dumps(context, indent=2, ensure_ascii=False, default=str)

        # Atomic write
        temp_file = self.context_file.with_suffix(".tmp")
        try:
            temp_file.write_text(json_content, encoding="utf-8")
            temp_file.replace(self.context_file)
        except Exception:
            if temp_file.exists():
                temp_file.unlink(missing_ok=True)
            raise

        return self.context_file

    def export_context_from_flow(self, flow: FlowInstance) -> Path:
        """Export AI context from a flow instance.

        Automatically builds the context.json from the flow state
        using the schema-defined transformation.

        Args:
            flow: Flow instance to export context from

        Returns:
            Path to the saved context file
        """
        context = build_context_from_flow(flow.to_dict())
        return self.save_context(context)

    def load_context(self) -> Optional[Dict[str, Any]]:
        """Load AI context export.

        Returns:
            Context dictionary if file exists, None otherwise
        """
        if not self.context_file.exists():
            return None

        try:
            content = self.context_file.read_text(encoding="utf-8")
            data = json.loads(content)
            # Handle both new format (direct content) and old format (nested under "content")
            if data.get("type") == "se3_context":
                return data
            return data.get("content", {})
        except (json.JSONDecodeError, KeyError):
            return None

    def export_progress_markdown(self, flow: FlowInstance) -> str:
        """Export flow progress to markdown for human readability.

        This creates a markdown representation similar to the old
        progress.md but derived from the JSON state.

        Args:
            flow: Flow instance to export

        Returns:
            Markdown content
        """
        lines = [
            f"# SE3 Session Progress",
            "",
            f"**Flow ID:** {flow.flow_id}",
            f"**Status:** {flow.status.value}",
            f"**Task:** {flow.task_description}",
            "",
            "## Steps",
            "",
        ]

        for step_id in flow.state.step_history:
            step = flow.state.steps.get(step_id)
            if not step:
                continue

            status_icon = {
                StepStatus.PENDING: "⬜",
                StepStatus.RUNNING: "🔄",
                StepStatus.COMPLETED: "✅",
                StepStatus.FAILED: "❌",
                StepStatus.RETRYING: "🔁",
                StepStatus.PAUSED: "⏸️",
            }.get(step.status, "⬜")

            lines.append(f"{status_icon} **{step.step_type.value}** ({step.status.value})")

            if step.error_message:
                lines.append(f"   - Error: {step.error_message}")

            if step.artifacts:
                lines.append(f"   - Artifacts: {', '.join(str(a) for a in step.artifacts)}")

        lines.extend([
            "",
            "## Context",
            "",
            "```json",
            json.dumps(flow.state.context, indent=2, default=str),
            "```",
        ])

        return "\n".join(lines)
