"""State persistence for the flow engine.

Handles JSON serialization/deserialization with atomic writes
to prevent state corruption during interruptions.
"""

import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .models import ENGINE_FORMAT_SPLIT, FlowInstance, FlowStatus, State, Step, StepStatus
from .schema import build_context_from_flow

logger = logging.getLogger(__name__)

# Legacy engine.json / archive snapshots written before issue #244 一期 inline
# every step's inputs/outputs, so a single file can reach tens of MB. The
# CLI-side ``list_all_flows`` only needs a handful of top-level keys, so a file
# above this guard is never fully parsed there — it is read head+tail for its
# identity fields instead (mirrors ``daemon.disk_json_cache.MAX_PARSE_BYTES``).
LIST_MAX_PARSE_BYTES = 5 * 1024 * 1024


class PersistenceManager:
    """Manages persistence of flow engine state.

    Uses atomic writes (write to temp file, then rename) to ensure
    state file integrity even if interrupted mid-write.
    """

    STATE_FILENAME = "engine.json"
    CONTEXT_FILENAME = "context.json"
    BACKUP_EXTENSION = ".bak"
    RESUMABLE_DIRNAME = "resumable"
    # Per-flow cold-data partition (issue #244 一期). Each flow's per-step
    # inputs/outputs and shared context/fix_history are externalized here,
    # partitioned by flow_id so a resumable snapshot's cold files never collide
    # with a later flow that happens to reuse the same short step_id.
    STEPS_DIRNAME = "steps"
    COLD_CONTEXT_FILENAME = "_context.json"

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
        # Per-cold-file sha1 of the last content this manager wrote, keyed by
        # absolute path. Drives the incremental write path (issue #244 B2): a
        # step whose inputs/outputs are unchanged since the previous save is not
        # rewritten, so per-step persistence I/O is proportional to what that
        # step actually produced rather than to the whole flow's size. Held
        # in-memory for the manager's lifetime; a fresh manager (e.g. on resume)
        # simply rewrites every cold file once, which is correct and one-time.
        self._cold_write_hashes: Dict[str, str] = {}

    def ensure_directories(self) -> None:
        """Ensure state directories exist."""
        self.state_dir.mkdir(parents=True, exist_ok=True)

    # -- split-format cold storage (issue #244 一期) ------------------------

    def _cold_dir_for(self, flow_id: str, state_dir: Optional[Path] = None) -> Path:
        """Return the cold-data directory for *flow_id* under *state_dir*."""
        base = state_dir if state_dir is not None else self.state_dir
        return base / self.STEPS_DIRNAME / flow_id

    @staticmethod
    def _atomic_write_text(path: Path, content: str) -> None:
        """Write *content* to *path* atomically (temp file + rename)."""
        temp_file = path.with_suffix(path.suffix + ".tmp")
        try:
            temp_file.write_text(content, encoding="utf-8")
            temp_file.replace(path)
        except Exception:
            if temp_file.exists():
                temp_file.unlink(missing_ok=True)
            raise

    def _write_cold_file_guarded(self, path: Path, content: str) -> bool:
        """Write a cold file only when its content actually changed.

        Returns True when a write happened. The sha1 guard makes per-step
        persistence touch only the cold files whose payload changed since this
        manager's last save (issue #244 B2), so a 31-step flow advancing one
        step rewrites one step file, not all of them.
        """
        key = str(path)
        digest = hashlib.sha1(content.encode("utf-8")).hexdigest()
        if self._cold_write_hashes.get(key) == digest and path.exists():
            return False
        self._atomic_write_text(path, content)
        self._cold_write_hashes[key] = digest
        return True

    def _write_cold_files(self, flow: FlowInstance, state_dir: Optional[Path] = None) -> None:
        """Persist a flow's per-step and shared cold data under steps/<flow_id>/."""
        cold_dir = self._cold_dir_for(flow.flow_id, state_dir)
        cold_dir.mkdir(parents=True, exist_ok=True)
        for step_id, step in flow.state.steps.items():
            content = json.dumps(
                step.cold_payload(), indent=2, ensure_ascii=False, default=str
            )
            self._write_cold_file_guarded(cold_dir / f"{step_id}.json", content)
        context_content = json.dumps(
            flow.state.cold_meta(), indent=2, ensure_ascii=False, default=str
        )
        self._write_cold_file_guarded(
            cold_dir / self.COLD_CONTEXT_FILENAME, context_content
        )

    def _write_header(self, path: Path, flow: FlowInstance) -> None:
        """Atomically write a flow's split-format KB-scale header to *path*."""
        content = json.dumps(
            flow.to_header_dict(), indent=2, ensure_ascii=False, default=str
        )
        self._atomic_write_text(path, content)

    def _hydrate_from_cold(
        self, flow: FlowInstance, state_dir: Optional[Path] = None
    ) -> None:
        """Populate a split-format flow's step IO and shared context from disk.

        Tolerant of a missing / corrupt cold file (issue #244 B3): the affected
        step's inputs/outputs (or the shared context) are left empty and a
        warning logged, rather than failing the whole load.
        """
        cold_dir = self._cold_dir_for(flow.flow_id, state_dir)

        context_data = self._read_cold_json(cold_dir / self.COLD_CONTEXT_FILENAME)
        if isinstance(context_data, dict):
            ctx = context_data.get(State.COLD_META_CONTEXT_KEY)
            if isinstance(ctx, dict):
                flow.state.context = ctx
            fix_history = context_data.get(State.COLD_META_FIX_HISTORY_KEY)
            if isinstance(fix_history, list):
                flow.state.fix_history = fix_history
                # Keep the context mirror consistent with the top-level list, as
                # increment_fix_iteration maintains it (both are read by steps).
                if "fix_history" in flow.state.context:
                    flow.state.context["fix_history"] = fix_history
        else:
            logger.warning(
                "Cold context file missing/corrupt for flow %s at %s; "
                "loading with empty shared context",
                flow.flow_id,
                cold_dir / self.COLD_CONTEXT_FILENAME,
            )

        for step_id, step in flow.state.steps.items():
            io = self._read_cold_json(cold_dir / f"{step_id}.json")
            if isinstance(io, dict):
                inputs = io.get("inputs")
                outputs = io.get("outputs")
                step.inputs = inputs if isinstance(inputs, dict) else {}
                step.outputs = outputs if isinstance(outputs, dict) else {}
            else:
                logger.warning(
                    "Cold step file missing/corrupt for flow %s step %s; "
                    "loading with empty inputs/outputs",
                    flow.flow_id,
                    step_id,
                )
                step.inputs = {}
                step.outputs = {}

    @staticmethod
    def _read_cold_json(path: Path) -> Optional[dict]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _reconstruct_flow(
        self, data: Dict[str, Any], state_dir: Optional[Path] = None
    ) -> FlowInstance:
        """Build a FlowInstance from a header/legacy dict, hydrating cold data.

        A legacy inline dict (no :data:`ENGINE_FORMAT_SPLIT` marker) is returned
        as-is by ``from_dict``. A split-format header additionally has its cold
        step IO and shared context hydrated from ``steps/<flow_id>/`` under
        *state_dir* (defaults to this manager's state dir).
        """
        flow = FlowInstance.from_dict(data)
        if data.get("engine_format") == ENGINE_FORMAT_SPLIT:
            self._hydrate_from_cold(flow, state_dir)
        return flow

    def hydrate_step(
        self, flow: FlowInstance, step_id: str, state_dir: Optional[Path] = None
    ) -> Optional[Step]:
        """On-demand load a single step's cold inputs/outputs into *flow*.

        Supports the resume read side (issue #244 B4): a caller can load a
        header-only flow (see :meth:`load_flow` ``hydrate=False``) and pull in
        exactly the steps it needs, without materializing every step's cold
        data. Returns the hydrated step, or None when *step_id* is unknown.
        """
        step = flow.state.steps.get(step_id)
        if step is None:
            return None
        cold_dir = self._cold_dir_for(flow.flow_id, state_dir)
        io = self._read_cold_json(cold_dir / f"{step_id}.json")
        if isinstance(io, dict):
            inputs = io.get("inputs")
            outputs = io.get("outputs")
            step.inputs = inputs if isinstance(inputs, dict) else {}
            step.outputs = outputs if isinstance(outputs, dict) else {}
        return step

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

        # Split-format write (issue #244 一期): the KB-scale header goes to
        # engine.json, while each step's inputs/outputs and the shared
        # context/fix_history go to external cold files under steps/<flow_id>/.
        # Cold files are written first (and incrementally — only changed ones),
        # then the header, both atomically. The daemon / webui hot path then
        # reads only the KB header; write volume is proportional to the step's
        # own output, not to the accumulated flow size.
        self._write_cold_files(flow)
        self._write_header(self.state_file, flow)

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

        # Split format: the snapshot is a KB header referencing the same per-flow
        # cold files under steps/<flow_id>/ that engine.json references — writing
        # them (incrementally) keeps an in-flight resumable snapshot from growing
        # linearly with flow progress (issue #244 B2). Cold files are shared with
        # save_flow via the sha1 guard, so calling both back-to-back writes them
        # once. Partitioning cold data by flow_id means this snapshot's cold
        # files survive a later run overwriting engine.json.
        self._write_cold_files(flow)
        self._write_header(snapshot_file, flow)

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
            # Split-format cold files live under the manager's own
            # steps/<flow_id>/ (shared with engine.json), not beside the
            # snapshot header, so reconstruct against the default state dir.
            flow = self._reconstruct_flow(data)
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
                flow = self._reconstruct_flow(data)
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
            return self._reconstruct_flow(data)
        except (json.JSONDecodeError, KeyError, ValueError):
            # Try backup if main file is corrupted
            backup_file = self.state_file.with_suffix(self.BACKUP_EXTENSION)
            if backup_file.exists():
                content = backup_file.read_text(encoding="utf-8")
                data = json.loads(content)
                return self._reconstruct_flow(data)
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

            # Try normal parsing first. The JSON-repair logic below operates on
            # the split-format *header* (issue #244 B3): a truncated header is
            # repaired, then its (intact) cold step/context files are hydrated
            # by _reconstruct_flow / the explicit hydrate calls; a missing or
            # corrupt cold file degrades that step to empty IO without failing.
            try:
                data = json.loads(content)
                flow = self._reconstruct_flow(data)
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
                        if repaired.get("engine_format") == ENGINE_FORMAT_SPLIT:
                            self._hydrate_from_cold(flow)
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
                    if data.get("engine_format") == ENGINE_FORMAT_SPLIT:
                        self._hydrate_from_cold(flow)
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
        """Archive the current flow's state instead of deleting it.

        Split-format archival (issue #244 B5) keeps the flow *whole*: the
        engine.json header AND the flow's entire cold-data directory
        (``steps/<flow_id>/`` — per-step inputs/outputs plus ``_context.json``)
        move together into ``se3/state/archive/`` so no artifact is lost. The
        archived header keeps its ``engine_format`` marker and cold files sit at
        ``archive/steps/<flow_id>/``, mirroring the live layout, so a full-
        fidelity reload against the archive dir still finds them; the history /
        archive *listing* only needs the header (``read_engine_header``). A
        legacy inline engine.json has no cold dir and archives as a single file,
        exactly as before.
        """
        if not self.state_file.exists():
            return

        # Move to archive instead of deleting
        archive_dir = self.state_dir / "archive"
        archive_dir.mkdir(exist_ok=True)

        # Capture the flow_id before renaming so the cold dir can follow. Read
        # is size-guarded: a tens-of-MB legacy engine.json is not fully parsed.
        header = _read_snapshot_header(self.state_file) or {}
        flow_id = header.get("flow_id")

        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        archived = archive_dir / f"engine_{timestamp}.json"

        self.state_file.rename(archived)

        # Archive the flow's cold-data directory alongside its header, keeping
        # the flow fully self-contained under archive/.
        if flow_id:
            cold_src = self._cold_dir_for(str(flow_id))
            if cold_src.is_dir():
                archive_steps = archive_dir / self.STEPS_DIRNAME
                archive_steps.mkdir(exist_ok=True)
                cold_dst = archive_steps / str(flow_id)
                # A prior archive of the same flow_id would collide; suffix with
                # the timestamp to keep both intact rather than clobbering.
                if cold_dst.exists():
                    cold_dst = archive_steps / f"{flow_id}_{timestamp}"
                try:
                    cold_src.rename(cold_dst)
                except OSError:
                    logger.warning(
                        "Failed to archive cold data for flow %s (%s -> %s)",
                        flow_id,
                        cold_src,
                        cold_dst,
                        exc_info=True,
                    )

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
                    # Size-guarded header read: a legacy multi-MB archived
                    # engine.json is scanned head+tail for its identity fields
                    # rather than fully parsed, so listing never stalls on a
                    # giant snapshot. A split-format archive is a KB header.
                    data = _read_snapshot_header(archive_file)
                    if data is None:
                        continue
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


# -- CLI-side size-guarded header reader -----------------------------------
#
# The engine layer deliberately does NOT import ``daemon.disk_json_cache`` (that
# would invert the engine→daemon layering). ``list_all_flows`` / ``clear_state``
# only ever need a handful of top-level identity keys, so this self-contained
# reader mirrors the daemon guard: an at/under-guard file is parsed whole; an
# oversized legacy engine.json / archive snapshot is scanned head+tail for its
# top-level ``indent=2`` keys instead of being fully decoded.
import re as _re

_HEADER_STR_KEYS = ("flow_id", "status", "task_description", "task_type", "updated_at")
_HEADER_BOOL_KEYS = ("is_worktree_mode",)
_HEADER_WINDOW = 128 * 1024


def _read_snapshot_header(path: Path) -> Optional[Dict[str, Any]]:
    """Read a snapshot's top-level header, guarding against oversized files."""
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size <= LIST_MAX_PARSE_BYTES:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    try:
        with open(path, "rb") as fh:
            head = fh.read(_HEADER_WINDOW)
            if size > _HEADER_WINDOW * 2:
                fh.seek(size - _HEADER_WINDOW)
                tail = fh.read(_HEADER_WINDOW)
            else:
                tail = fh.read()
    except OSError:
        return None

    text = head.decode("utf-8", "replace") + "\n" + tail.decode("utf-8", "replace")
    result: Dict[str, Any] = {}
    for key in _HEADER_STR_KEYS:
        m = _re.search(r'\n  "' + _re.escape(key) + r'":\s*"((?:[^"\\]|\\.)*)"', text)
        if m is not None:
            try:
                result[key] = json.loads('"' + m.group(1) + '"')
            except ValueError:
                result[key] = m.group(1)
    for key in _HEADER_BOOL_KEYS:
        m = _re.search(r'\n  "' + _re.escape(key) + r'":\s*(true|false)', text)
        if m is not None:
            result[key] = m.group(1) == "true"
    if "flow_id" not in result:
        return None
    return result
