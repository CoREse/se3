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
from typing import Any, Dict, List, Optional, Set, Tuple

from .models import FlowInstance, FlowStatus, State, Step, StepStatus
from .schema import build_context_from_flow

logger = logging.getLogger(__name__)


# Hot/cold split format marker (issue #244 一期). A header written in the new
# layout carries this key at top level; its absence identifies a legacy
# (fully-inline) engine.json / snapshot, which every load path reads verbatim
# for backward compatibility (no in-place migration — see B3).
ENGINE_FORMAT_KEY = "engine_format"
ENGINE_FORMAT_HOTCOLD = "hotcold/1"


def _canonical_json(obj: Any) -> str:
    """Stable JSON encoding for content-hashing cold payloads.

    ``sort_keys`` makes the hash independent of dict ordering so an unchanged
    step is recognised as unchanged across saves (the whole point of the
    incremental write path). ``default=str`` mirrors the persistence writer so
    non-JSON-native values (e.g. Path) hash the same way they serialize.
    """
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)


def _content_hash(obj: Any) -> str:
    """Content hash of a cold payload, used to skip rewriting unchanged files."""
    return hashlib.sha256(_canonical_json(obj).encode("utf-8")).hexdigest()


def _is_hotcold(data: Any) -> bool:
    """True when ``data`` is a new-format (header + cold-ref) engine payload."""
    return (
        isinstance(data, dict)
        and str(data.get(ENGINE_FORMAT_KEY, "")).startswith("hotcold/")
    )


def _split_flow_dict(
    full: Dict[str, Any]
) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """Split a full ``FlowInstance.to_dict()`` into (header, cold_steps, context_payload).

    The header keeps only KB-scale fields — flow identity, status, the per-step
    *status table* (timestamps / retry counts / model, but NOT the step's
    inputs/outputs/artifacts bodies), and small State scalars — with each step's
    heavy payload replaced by a ``cold_ref`` carrying the payload's content hash.
    The shared ``State.context`` and ``fix_history`` (the other unbounded
    growers, the latter able to embed a fix_context copy of test_results) are
    externalized together into a single per-flow context cold payload, again
    referenced from the header by hash.

    Returns:
        header: the header dict to write to engine.json / the snapshot file.
        cold_steps: step_id -> {"inputs", "outputs", "artifacts"} cold payloads.
        context_payload: {"context", "fix_history"} shared cold payload.
    """
    header = {k: v for k, v in full.items() if k != "state"}
    header[ENGINE_FORMAT_KEY] = ENGINE_FORMAT_HOTCOLD

    state = dict(full.get("state", {}))
    steps = state.pop("steps", {}) or {}
    context = state.pop("context", {})
    fix_history = state.pop("fix_history", [])

    cold_steps: Dict[str, Dict[str, Any]] = {}
    header_steps: Dict[str, Any] = {}
    for sid, sdata in steps.items():
        cold = {
            "inputs": sdata.get("inputs", {}),
            "outputs": sdata.get("outputs", {}),
            "artifacts": sdata.get("artifacts", []),
        }
        cold_steps[sid] = cold
        entry = {
            k: v
            for k, v in sdata.items()
            if k not in ("inputs", "outputs", "artifacts")
        }
        entry["cold_ref"] = {"file": f"{sid}.json", "hash": _content_hash(cold)}
        header_steps[sid] = entry

    context_payload = {"context": context, "fix_history": fix_history}
    state["steps"] = header_steps
    state["context_ref"] = {
        "file": PersistenceManager.CONTEXT_COLD_FILENAME,
        "hash": _content_hash(context_payload),
    }
    header["state"] = state
    return header, cold_steps, context_payload


class PersistenceManager:
    """Manages persistence of flow engine state.

    Uses atomic writes (write to temp file, then rename) to ensure
    state file integrity even if interrupted mid-write.
    """

    STATE_FILENAME = "engine.json"
    CONTEXT_FILENAME = "context.json"
    BACKUP_EXTENSION = ".bak"
    RESUMABLE_DIRNAME = "resumable"
    # Hot/cold split (issue #244 一期): heavy per-step inputs/outputs and the
    # shared context are externalized to se3/state/steps/<flow_id>/. Partitioned
    # by flow_id so a resumable snapshot's cold files never collide with a later
    # flow that reuses the same auto-generated step ids.
    STEPS_DIRNAME = "steps"
    CONTEXT_COLD_FILENAME = "_context.json"

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
        # Root of the per-flow cold-file partitions (steps/<flow_id>/). The
        # engine.json header and the resumable snapshot header for the same flow
        # both reference this one partition, so a snapshot adds only its KB-scale
        # header — never a second copy of the cold data.
        self.steps_dir = self.state_dir / self.STEPS_DIRNAME

    def ensure_directories(self) -> None:
        """Ensure state directories exist."""
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _cold_dir(self, flow_id: str) -> Path:
        """Path to a flow's cold-file partition (steps/<flow_id>/)."""
        return self.steps_dir / str(flow_id)

    @staticmethod
    def _atomic_write_json(path: Path, data: Any) -> None:
        """Atomically write ``data`` as pretty JSON (temp file + rename).

        The single write seam for the header and every cold file, so the
        incremental-write regression tests can patch/observe exactly which files
        a persist touched. A distinct ``<name>.tmp`` sibling (not ``with_suffix``)
        keeps two files that share a stem — e.g. ``engine.json`` cold refs — from
        colliding on the temp name.
        """
        content = json.dumps(data, indent=2, ensure_ascii=False, default=str)
        temp_file = path.with_name(path.name + ".tmp")
        try:
            temp_file.write_text(content, encoding="utf-8")
            temp_file.replace(path)
        except Exception:
            if temp_file.exists():
                temp_file.unlink(missing_ok=True)
            raise

    def _prior_cold_hashes(
        self, header_path: Path, flow_id: str
    ) -> Tuple[Dict[str, str], Optional[str]]:
        """Read the hashes recorded by the last header written to ``header_path``.

        Returns (per-step-hash, context-hash) for the *same* flow's previously
        persisted header, enabling the incremental write path to skip cold files
        whose content is unchanged. Any condition that makes reuse unsafe —
        missing file, unreadable/legacy-inline header, or a header describing a
        *different* flow (the single-slot engine.json now holds another run) —
        returns empty hashes so every cold file is (re)written.
        """
        if not header_path.exists():
            return {}, None
        try:
            data = json.loads(header_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}, None
        if not _is_hotcold(data) or str(data.get("flow_id")) != str(flow_id):
            return {}, None
        state = data.get("state", {}) if isinstance(data, dict) else {}
        per_step: Dict[str, str] = {}
        for sid, entry in (state.get("steps", {}) or {}).items():
            cold_ref = entry.get("cold_ref") if isinstance(entry, dict) else None
            if isinstance(cold_ref, dict) and "hash" in cold_ref:
                per_step[sid] = cold_ref["hash"]
        context_ref = state.get("context_ref") or {}
        ctx_hash = context_ref.get("hash") if isinstance(context_ref, dict) else None
        return per_step, ctx_hash

    @staticmethod
    def _dirty_step_ids(
        header: Dict[str, Any], prior_step_hashes: Dict[str, str]
    ) -> Set[str]:
        """Step ids whose cold payload differs from the last persisted header."""
        dirty: Set[str] = set()
        for sid, entry in header.get("state", {}).get("steps", {}).items():
            new_hash = entry.get("cold_ref", {}).get("hash")
            if prior_step_hashes.get(sid) != new_hash:
                dirty.add(sid)
        return dirty

    def _write_cold(
        self,
        flow_id: str,
        header: Dict[str, Any],
        cold_steps: Dict[str, Dict[str, Any]],
        context_payload: Dict[str, Any],
        prior_step_hashes: Dict[str, str],
        prior_ctx_hash: Optional[str],
    ) -> None:
        """Write only the cold files whose content changed since the last persist.

        Write volume is proportional to what actually changed this step, not to
        the flow's step count (issue #244 B2). Completed/archived cold files are
        never rewritten because their hash keeps matching.
        """
        dirty = self._dirty_step_ids(header, prior_step_hashes)
        new_ctx_hash = header["state"]["context_ref"]["hash"]
        ctx_dirty = new_ctx_hash != prior_ctx_hash

        if not dirty and not ctx_dirty:
            return

        cold_dir = self._cold_dir(flow_id)
        cold_dir.mkdir(parents=True, exist_ok=True)
        for sid in dirty:
            self._atomic_write_json(cold_dir / f"{sid}.json", cold_steps[sid])
        if ctx_dirty:
            self._atomic_write_json(
                cold_dir / self.CONTEXT_COLD_FILENAME, context_payload
            )

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

        # Hot/cold split (issue #244 一期): engine.json holds only the KB-scale
        # header; per-step inputs/outputs and the shared context live in
        # steps/<flow_id>/. Compare against the header already on disk so only
        # the cold files that actually changed this step are rewritten — the
        # write volume then tracks the step's own output, not the flow's step
        # count. Cold files first, then the header (which references them), so a
        # crash between the two never leaves the header pointing at absent data.
        header, cold_steps, context_payload = _split_flow_dict(flow.to_dict())
        prior_step_hashes, prior_ctx_hash = self._prior_cold_hashes(
            self.state_file, flow.flow_id
        )
        self._write_cold(
            flow.flow_id,
            header,
            cold_steps,
            context_payload,
            prior_step_hashes,
            prior_ctx_hash,
        )
        self._atomic_write_json(self.state_file, header)

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
                # Reuse the header save_flow just built and the cold files it
                # just wrote: the snapshot references the same steps/<flow_id>/
                # partition, so it costs only a KB-scale header write and never
                # duplicates the cold payloads.
                self.save_resumable_snapshot(flow, _header=header)
        except Exception:
            logger.debug(
                "Failed to update resumable snapshot for flow %s",
                getattr(flow, "flow_id", "?"),
                exc_info=True,
            )

        return self.state_file

    @staticmethod
    def _read_cold_json(
        path: Path, label: str, warnings: Optional[List[str]]
    ) -> Optional[Any]:
        """Read+parse a cold file; return None (and warn) if missing/corrupt.

        Tolerant by design (issue #244 B3): a missing or damaged cold file must
        degrade that step's payload to empty, never crash the whole flow load.
        """
        if not path.exists():
            msg = f"Cold file missing for {label}: {path.name}; using empty payload"
            logger.warning("Cold file missing (%s): %s", label, path)
            if warnings is not None:
                warnings.append(msg)
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            msg = f"Cold file unreadable for {label}: {exc}; using empty payload"
            logger.warning("Cold file unreadable (%s): %s: %s", label, path, exc)
            if warnings is not None:
                warnings.append(msg)
            return None

    def _reconstruct_full_dict(
        self, header: Dict[str, Any], warnings: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Inline a new-format header's cold files back into a full flow dict.

        Legacy (fully-inline) payloads are returned unchanged, so the same load
        path serves both formats (B3). A new-format header has its per-step
        ``cold_ref`` and the shared ``context_ref`` resolved from
        steps/<flow_id>/ back into the inline shape ``FlowInstance.from_dict``
        expects; missing/corrupt cold files degrade to empty payloads.
        """
        if not _is_hotcold(header):
            return header

        cold_dir = self._cold_dir(header.get("flow_id"))
        full = {k: v for k, v in header.items() if k != ENGINE_FORMAT_KEY}
        state = dict(full.get("state", {}))

        context_ref = state.pop("context_ref", None)
        payload = None
        if isinstance(context_ref, dict) and context_ref.get("file"):
            payload = self._read_cold_json(
                cold_dir / context_ref["file"], "context", warnings
            )
        if isinstance(payload, dict):
            state["context"] = payload.get("context", {})
            state["fix_history"] = payload.get("fix_history", [])
        else:
            state["context"] = {}
            state["fix_history"] = []

        rebuilt_steps: Dict[str, Any] = {}
        for sid, entry in (state.get("steps", {}) or {}).items():
            step = {k: v for k, v in entry.items() if k != "cold_ref"}
            cold_ref = entry.get("cold_ref") if isinstance(entry, dict) else None
            cold = None
            if isinstance(cold_ref, dict) and cold_ref.get("file"):
                cold = self._read_cold_json(
                    cold_dir / cold_ref["file"], f"step {sid}", warnings
                )
            if isinstance(cold, dict):
                step["inputs"] = cold.get("inputs", {})
                step["outputs"] = cold.get("outputs", {})
                step["artifacts"] = cold.get("artifacts", [])
            else:
                step["inputs"] = {}
                step["outputs"] = {}
                step["artifacts"] = []
            rebuilt_steps[sid] = step
        state["steps"] = rebuilt_steps

        full["state"] = state
        return full

    def save_resumable_snapshot(
        self, flow: FlowInstance, *, _header: Optional[Dict[str, Any]] = None
    ) -> Path:
        """Persist a per-flow resumable snapshot to resumable/<flow_id>.json.

        The snapshot uses the same header + cold-reference layout as engine.json
        and shares the same ``steps/<flow_id>/`` cold partition, so it is only a
        KB-scale header write — it no longer grows linearly with an in-flight
        flow's inputs/outputs (issue #244 B2). It is the durable, per-flow copy
        that survives a later ``se3 run`` overwriting the single-slot
        engine.json, so an interrupted/paused/failed flow can still be located
        and resumed by flow_id.

        Args:
            flow: Flow instance to snapshot
            _header: internal fast-path — the header already built (and whose
                cold files were already written) by ``save_flow`` for this same
                persist. When omitted (a standalone call), the snapshot recomputes
                the split and writes any changed cold files itself.

        Returns:
            Path to the snapshot file.
        """
        self.resumable_dir.mkdir(parents=True, exist_ok=True)
        snapshot_file = self.resumable_dir / f"{flow.flow_id}.json"

        if _header is None:
            header, cold_steps, context_payload = _split_flow_dict(flow.to_dict())
            prior_step_hashes, prior_ctx_hash = self._prior_cold_hashes(
                snapshot_file, flow.flow_id
            )
            self._write_cold(
                flow.flow_id,
                header,
                cold_steps,
                context_payload,
                prior_step_hashes,
                prior_ctx_hash,
            )
        else:
            header = _header

        self._atomic_write_json(snapshot_file, header)
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
            flow = FlowInstance.from_dict(self._reconstruct_full_dict(data))
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
                flow = FlowInstance.from_dict(self._reconstruct_full_dict(data))
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
            return FlowInstance.from_dict(self._reconstruct_full_dict(data))
        except (json.JSONDecodeError, KeyError, ValueError):
            # Try backup if main file is corrupted
            backup_file = self.state_file.with_suffix(self.BACKUP_EXTENSION)
            if backup_file.exists():
                content = backup_file.read_text(encoding="utf-8")
                data = json.loads(content)
                return FlowInstance.from_dict(self._reconstruct_full_dict(data))
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

            # Try normal parsing first. The JSON-repair path (below) operates on
            # the KB-scale *header*; the cold files it references are resolved
            # after repair and degrade independently to empty payloads (B3), so a
            # truncated header never blocks recovery of intact cold data.
            try:
                data = json.loads(content)
                flow = FlowInstance.from_dict(
                    self._reconstruct_full_dict(data, warnings)
                )
                if filepath != self.state_file:
                    warnings.append(f"Loaded from backup {filepath.name}")
                return flow, warnings
            except json.JSONDecodeError as e:
                warnings.append(f"JSON parse error in {filepath.name}: {e}")
                # Try to repair truncated JSON
                repaired = self._try_repair_json(content)
                if repaired is not None:
                    try:
                        flow = self._tolerant_from_dict(
                            self._reconstruct_full_dict(repaired, warnings), warnings
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
                    flow = self._tolerant_from_dict(
                        self._reconstruct_full_dict(data, warnings), warnings
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
