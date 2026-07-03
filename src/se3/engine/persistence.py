"""State persistence for the flow engine.

Handles JSON serialization/deserialization with atomic writes
to prevent state corruption during interruptions.
"""

import copy
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .models import FlowInstance, FlowStatus, State, Step, StepStatus
from .schema import build_context_from_flow

logger = logging.getLogger(__name__)

# --- engine.json hot/cold split format (issue #244 一期 / Part B) -----------
# The new-format engine.json is a KB-level *header*: flow identity + status +
# per-step status table (no inputs/outputs bodies) + the small round-tripped
# fields. A step's inputs/outputs and the shared State.context are externalised
# to per-flow *cold* files under ``se3/state/steps/<flow_id>/`` and loaded on
# demand. The header carries ENGINE_FORMAT_KEY so a reader distinguishes it from
# the legacy inline format by a single top-level key — no filesystem probing,
# no aggregator-side format branch (the daemon just reads the KB header whole).
#
# Cold data is partitioned by flow_id (not by header file): the live engine.json
# and a resumable/<flow_id>.json snapshot of the *same* flow reference the one
# ``steps/<flow_id>/`` directory, so a later flow reusing a sequential step_id
# (e.g. ``01_analyze_...``) never collides with an earlier flow's cold files.
ENGINE_FORMAT_KEY = "engine_format"
NEW_FORMAT_MARKER = "hot-cold/1"
STEPS_DIRNAME = "steps"
COLD_CONTEXT_FILENAME = "_context.json"


class PersistenceManager:
    """Manages persistence of flow engine state.

    Uses atomic writes (write to temp file, then rename) to ensure
    state file integrity even if interrupted mid-write.
    """

    STATE_FILENAME = "engine.json"
    CONTEXT_FILENAME = "context.json"
    BACKUP_EXTENSION = ".bak"
    RESUMABLE_DIRNAME = "resumable"

    def __init__(self, project_root: Path):
        """Initialize with project root.

        Args:
            project_root: Root directory of the project
        """
        self.project_root = Path(project_root)
        self.state_dir = self.project_root / "se3" / "state"
        self.state_file = self.state_dir / self.STATE_FILENAME
        self.context_file = self.state_dir / self.CONTEXT_FILENAME
        # Per-flow resumable snapshots: se3/state/resumable/<flow_id>.json.
        # Unlike the single-slot engine.json (overwritten by the next run) and
        # the archive/ dir (terminal/completed snapshots only), this directory
        # holds the full FlowInstance of every flow that has NOT yet completed
        # normally, so a paused/interrupted/recoverable-failed flow stays
        # resumable even after a later run overwrites engine.json.
        self.resumable_dir = self.state_dir / self.RESUMABLE_DIRNAME

    def ensure_directories(self) -> None:
        """Ensure state directories exist."""
        self.state_dir.mkdir(parents=True, exist_ok=True)

    # --- hot/cold split format: detection, cold loaders, (de)serialization ---

    def _steps_root_for(self, flow_id: Optional[str]) -> Path:
        """Directory holding a flow's externalised cold step / context files.

        Partitioned by flow_id so the live engine.json header and any
        resumable/<flow_id>.json snapshot of the same flow share one cold
        directory, while distinct flows never collide on a reused step_id.
        """
        return self.state_dir / STEPS_DIRNAME / str(flow_id)

    @staticmethod
    def _is_new_format(data: Any) -> bool:
        """True if ``data`` is a hot/cold-split header (vs legacy inline).

        Keyed solely on the top-level marker so detection is O(1) and needs no
        filesystem probe; a legacy engine.json (no marker) reads as False.
        """
        return isinstance(data, dict) and str(
            data.get(ENGINE_FORMAT_KEY, "")
        ).startswith("hot-cold")

    @staticmethod
    def _split_to_new_format(
        data: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]], Dict[str, Any]]:
        """Split a full ``FlowInstance.to_dict()`` into (header, cold_steps, context).

        Pure, no I/O — the single authoritative definition of the on-disk split
        (the write path serialises with this; the read path / tests reassemble
        the inverse). The header is a deep copy with each step's inputs/outputs
        and the shared ``state.context`` removed and the format marker prepended
        (first key, so it survives end-truncation of a partially-written file).

        Returns:
            header: KB-level dict destined for engine.json / resumable snapshot.
            cold_steps: {step_id: {"inputs": ..., "outputs": ...}} for
                steps/<flow_id>/<step_id>.json.
            context: the shared State.context for steps/<flow_id>/_context.json.
        """
        header: Dict[str, Any] = {ENGINE_FORMAT_KEY: NEW_FORMAT_MARKER}
        header.update(copy.deepcopy(data))
        cold_steps: Dict[str, Dict[str, Any]] = {}
        context: Dict[str, Any] = {}

        state = header.get("state")
        if isinstance(state, dict):
            context = state.pop("context", {}) or {}
            steps = state.get("steps")
            if isinstance(steps, dict):
                for step_id, step in steps.items():
                    if not isinstance(step, dict):
                        continue
                    cold_steps[step_id] = {
                        "inputs": step.pop("inputs", {}),
                        "outputs": step.pop("outputs", {}),
                    }
        return header, cold_steps, context

    def _load_cold_context(
        self, flow_id: Optional[str], warnings: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Load a flow's externalised shared context (steps/<flow_id>/_context.json).

        Missing file -> empty context, silently: an empty ``state.context`` is a
        legitimate, common case, so its absence must not spam warnings on every
        load. A file that exists but cannot be parsed IS data loss -> empty +
        warning (never raises), so one corrupt cold file cannot fail the load.
        """
        if not flow_id:
            return {}
        ctx_file = self._steps_root_for(flow_id) / COLD_CONTEXT_FILENAME
        if not ctx_file.exists():
            return {}
        try:
            loaded = json.loads(ctx_file.read_text(encoding="utf-8"))
            return loaded if isinstance(loaded, dict) else {}
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            msg = f"Cold context {ctx_file.name} unreadable ({exc}); using empty context"
            logger.warning("%s", msg)
            if warnings is not None:
                warnings.append(msg)
            return {}

    def _load_cold_step(
        self,
        flow_id: Optional[str],
        step_id: str,
        warnings: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Load one step's externalised inputs/outputs (steps/<flow_id>/<step_id>.json).

        A step referenced by the header always has a cold file in the new
        format, so missing OR corrupt is treated as recoverable data loss: the
        step's inputs/outputs degrade to ``{}`` with a warning rather than
        crashing the whole flow load (B3 fault tolerance / test (h)).
        """
        if not flow_id:
            return {}
        step_file = self._steps_root_for(flow_id) / f"{step_id}.json"
        try:
            loaded = json.loads(step_file.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("cold step file is not a JSON object")
            return loaded
        except FileNotFoundError:
            msg = f"Cold step file {step_id}.json missing; step inputs/outputs empty"
            logger.warning("%s", msg)
            if warnings is not None:
                warnings.append(msg)
            return {}
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            msg = f"Cold step file {step_id}.json unreadable ({exc}); step inputs/outputs empty"
            logger.warning("%s", msg)
            if warnings is not None:
                warnings.append(msg)
            return {}

    def _reassemble_new_format(
        self, header: Dict[str, Any], warnings: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Inverse of :meth:`_split_to_new_format`: header + cold files -> full dict.

        Backfills each step's inputs/outputs via :meth:`_load_cold_step` and the
        shared context via :meth:`_load_cold_context`, keyed by the header's
        flow_id. Cold-file faults degrade in place (empty + warning), so the
        returned dict is always a valid input to ``FlowInstance.from_dict``.

        Backfill is eager (all cold files read here) because a full-flow load is
        used by resume / CLI on a single flow — a few MB, acceptable. The daemon
        hot path never calls this: it reads only the KB header's top-level fields
        (flow_id/status/is_worktree_mode) directly, so per-tick cost stays bounded
        regardless of how large the externalised bodies are.
        """
        flow_id = header.get("flow_id")
        result = dict(header)
        result.pop(ENGINE_FORMAT_KEY, None)

        state = result.get("state")
        if isinstance(state, dict):
            state["context"] = self._load_cold_context(flow_id, warnings)
            steps = state.get("steps")
            if isinstance(steps, dict):
                for step_id, step in steps.items():
                    if not isinstance(step, dict):
                        continue
                    cold = self._load_cold_step(flow_id, step_id, warnings)
                    step["inputs"] = cold.get("inputs", {})
                    step["outputs"] = cold.get("outputs", {})
        return result

    def _deserialize_flow(
        self,
        data: Dict[str, Any],
        *,
        tolerant: bool = False,
        warnings: Optional[List[str]] = None,
    ) -> FlowInstance:
        """Reconstruct a FlowInstance from either on-disk format.

        New format (header marker present) is reassembled from its cold files
        first; legacy inline data passes straight through. This is the single
        choke point every read path routes through, so dual-format support lives
        in exactly one place.
        """
        if self._is_new_format(data):
            data = self._reassemble_new_format(data, warnings)
        if tolerant:
            return self._tolerant_from_dict(
                data, warnings if warnings is not None else []
            )
        return FlowInstance.from_dict(data)

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

        # Per-flow resumable snapshot bookkeeping. save_flow is the single
        # convergence point for every pause/interrupt/failure/step-advance
        # persist, so hooking here guarantees "snapshot written the moment a
        # flow pauses/is interrupted" with no need to scatter writes across
        # run.py's exception branches. A normally COMPLETED flow needs no
        # resume, so its snapshot is removed; any other status keeps a fresh
        # snapshot. Best-effort: a snapshot I/O failure must never break the
        # primary engine.json write.
        try:
            if flow.status == FlowStatus.COMPLETED:
                self.clear_resumable_snapshot(flow.flow_id)
            else:
                self.save_resumable_snapshot(flow)
        except Exception:
            logger.debug(
                "Failed to update resumable snapshot for flow %s",
                getattr(flow, "flow_id", "?"),
                exc_info=True,
            )

        return self.state_file

    def save_resumable_snapshot(self, flow: FlowInstance) -> Path:
        """Persist a per-flow resumable snapshot to resumable/<flow_id>.json.

        The snapshot format is identical to engine.json (``FlowInstance.to_dict``)
        and is written atomically (temp file + rename). It is the durable,
        per-flow copy that survives a later ``se3 run`` overwriting the
        single-slot engine.json, so an interrupted/paused/failed flow can still
        be located and resumed by flow_id.

        Args:
            flow: Flow instance to snapshot

        Returns:
            Path to the snapshot file.
        """
        self.resumable_dir.mkdir(parents=True, exist_ok=True)
        snapshot_file = self.resumable_dir / f"{flow.flow_id}.json"

        data = flow.to_dict()
        json_content = json.dumps(data, indent=2, ensure_ascii=False, default=str)

        temp_file = snapshot_file.with_suffix(".tmp")
        try:
            temp_file.write_text(json_content, encoding="utf-8")
            temp_file.replace(snapshot_file)
        except Exception:
            if temp_file.exists():
                temp_file.unlink(missing_ok=True)
            raise

        return snapshot_file

    def load_resumable_snapshot(self, flow_id: str) -> Optional[FlowInstance]:
        """Load the per-flow resumable snapshot for ``flow_id``.

        The snapshot's embedded ``flow_id`` MUST match the requested ``flow_id``;
        a snapshot whose payload describes a different flow (a stale, misnamed,
        or operator-created artifact) is rejected and treated as not found,
        rather than silently resuming the wrong flow as the live engine.json.

        Returns:
            The reconstructed FlowInstance, or None when no (readable, matching)
            snapshot exists. Corruption is tolerated by returning None rather
            than raising.
        """
        snapshot_file = self.resumable_dir / f"{flow_id}.json"
        if not snapshot_file.exists():
            return None
        try:
            content = snapshot_file.read_text(encoding="utf-8")
            data = json.loads(content)
            flow = self._deserialize_flow(data)
        except (json.JSONDecodeError, KeyError, ValueError, OSError):
            return None
        if flow.flow_id != flow_id:
            logger.warning(
                "Resumable snapshot %s contains mismatched flow_id %r (requested %r); "
                "treating as not found",
                snapshot_file,
                flow.flow_id,
                flow_id,
            )
            return None
        return flow

    def clear_resumable_snapshot(self, flow_id: str) -> None:
        """Remove the per-flow resumable snapshot for ``flow_id`` (best effort)."""
        snapshot_file = self.resumable_dir / f"{flow_id}.json"
        try:
            snapshot_file.unlink(missing_ok=True)
        except OSError:
            pass

    def list_resumable_snapshots(self) -> List[FlowInstance]:
        """List all per-flow resumable snapshots.

        Returns:
            A list of reconstructed FlowInstance objects, one per readable
            snapshot file under resumable/. Corrupt/unreadable snapshots are
            skipped silently.
        """
        flows: List[FlowInstance] = []
        if not self.resumable_dir.is_dir():
            return flows
        for snapshot_file in sorted(self.resumable_dir.glob("*.json")):
            try:
                data = json.loads(snapshot_file.read_text(encoding="utf-8"))
                flow = self._deserialize_flow(data)
            except (json.JSONDecodeError, KeyError, ValueError, OSError):
                continue
            # Only surface a snapshot whose embedded flow_id matches its
            # filename (resumable/<flow_id>.json). A mismatched payload is a
            # stale/misnamed/operator-created artifact that the load/resume
            # path (load_resumable_snapshot) would reject, so advertising it
            # here would offer a resume entry that can never actually resume.
            if flow.flow_id != snapshot_file.stem:
                logger.warning(
                    "Resumable snapshot %s contains mismatched flow_id %r; "
                    "skipping (cannot be resumed by filename)",
                    snapshot_file,
                    flow.flow_id,
                )
                continue
            flows.append(flow)
        return flows

    def load_flow_by_id(self, flow_id: str) -> Optional[FlowInstance]:
        """Locate and load a flow by id, preferring the active engine.json.

        Resolution order:

        1. The active engine.json, when it currently holds ``flow_id``.
        2. Otherwise the per-flow resumable snapshot (resumable/<flow_id>.json),
           which survives a later run overwriting engine.json.

        A normally COMPLETED flow has no resumable snapshot (it is cleared on
        completion by :meth:`save_flow`), so it is never resurrected through the
        snapshot path; only the still-active engine.json can return it.

        Returns:
            The matching FlowInstance, or None when neither source holds it.
        """
        active = self.load_flow()
        if active is not None and active.flow_id == flow_id:
            return active
        return self.load_resumable_snapshot(flow_id)

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
            return self._deserialize_flow(data)
        except (json.JSONDecodeError, KeyError, ValueError):
            # Try backup if main file is corrupted
            backup_file = self.state_file.with_suffix(self.BACKUP_EXTENSION)
            if backup_file.exists():
                content = backup_file.read_text(encoding="utf-8")
                data = json.loads(content)
                return self._deserialize_flow(data)
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

            # Try normal parsing first. For a new-format header this still routes
            # through _deserialize_flow, so a corrupt/missing *cold* file only
            # degrades that step (warning) instead of failing the whole load —
            # JSON repair below deliberately applies to the KB header only.
            try:
                data = json.loads(content)
                flow = self._deserialize_flow(data, warnings=warnings)
                if filepath != self.state_file:
                    warnings.append(f"Loaded from backup {filepath.name}")
                return flow, warnings
            except json.JSONDecodeError as e:
                warnings.append(f"JSON parse error in {filepath.name}: {e}")
                # Try to repair truncated JSON (the header; cold files are never repaired)
                repaired = self._try_repair_json(content)
                if repaired is not None:
                    try:
                        flow = self._deserialize_flow(
                            repaired, tolerant=True, warnings=warnings
                        )
                        warnings.append(f"Recovered from truncated JSON in {filepath.name}")
                        return flow, warnings
                    except Exception as e2:
                        warnings.append(f"Failed to deserialize repaired JSON: {e2}")
            except (KeyError, ValueError, TypeError) as e:
                warnings.append(f"Deserialization error in {filepath.name}: {e}")
                # Try with tolerant deserialization
                try:
                    data = json.loads(content)
                    flow = self._deserialize_flow(
                        data, tolerant=True, warnings=warnings
                    )
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

        # created_at/updated_at are required by FlowInstance.from_dict but, in
        # the hot/cold header, serialize *after* the (larger) state block — so a
        # truncated header commonly loses them. Backfill here so a repaired
        # header still yields a loadable flow rather than a KeyError.
        if "created_at" not in data:
            data["created_at"] = datetime.now().isoformat()
            warnings.append("Missing created_at, using now")
        if "updated_at" not in data:
            data["updated_at"] = data["created_at"]
            warnings.append("Missing updated_at, using created_at")

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
        """Extract a short task description from the first JSONL file in a history dir.

        Title extraction follows a three-tier priority, aligned with the web
        chat-history display (``splitUserPromptByMarker``) and the daemon's
        ``_extract_history_summary``:

        1. The user's literal input cut out by the ``USER_CONTENT`` markers
           (:func:`~se3.engine.prompt_markers.extract_user_content`);
        2. otherwise the embedded ``Task description: --- ... ---`` block (the
           first-step-not-discovery ``se3 run "task"`` flow);
        3. otherwise the truncated raw content.

        The first jsonl line is frequently a ``step_started`` (or other) *event*
        record carrying no user content, with the real user prompt on a later
        line; the extractor scans forward — skipping event records — to the
        first record actually carrying user content (see
        :func:`~se3.engine.prompt_markers.first_user_content`), bounded so a
        large file is never fully read. The CLI clips the result to 100
        characters with an ellipsis.
        """
        import re

        from .prompt_markers import extract_user_content, first_user_content

        def _clip(text: str) -> str:
            return text[:100] + "..." if len(text) > 100 else text

        jsonl_files = sorted(flow_dir.glob("*.jsonl"))
        if not jsonl_files:
            return "(no history data)"
        try:
            # Stream the leading records (bounded) rather than loading the whole
            # file: ``first_user_content`` skips ``step_started`` / progress
            # events and stops at the first record carrying user content.
            with open(
                jsonl_files[0], "r", encoding="utf-8", errors="replace"
            ) as fh:
                content = first_user_content(fh)
            if content is None:
                return "(no state data)"
            # 1. Prefer the user's literal input delimited by USER_CONTENT markers.
            user_content = extract_user_content(content)
            if user_content is not None:
                return _clip(user_content)
            # 2. Extract embedded task description if present.
            match = re.search(
                r"Task description:\s*-+\s*(.*?)\s*-+",
                content,
                re.DOTALL,
            )
            if match:
                return _clip(match.group(1).strip())
            # 3. Fallback: truncated raw content.
            return _clip(content)
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
