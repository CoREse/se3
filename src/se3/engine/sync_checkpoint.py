"""SyncCheckpoint — persistent state for resumable ``se3 sync`` runs.

When ``SyncLoop`` detects sustained infrastructure failures (quota
exhaustion, repeated empty responses, network errors) it writes a
checkpoint to ``se3/state/sync_checkpoint.json`` so the next invocation
can resume work without re-analyzing every spec from scratch.

The checkpoint records which specs were already considered in-sync
along with their content hashes; on resume, ``recompute_in_sync``
compares each hash against disk and decides which specs can be skipped
and which must be re-analyzed because they changed since the
interruption.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


CHECKPOINT_SCHEMA_VERSION = 1
_CHECKPOINT_REL_PATH = Path("se3") / "state" / "sync_checkpoint.json"


@dataclass
class SyncCheckpoint:
    """A snapshot of sync progress when the loop was interrupted."""

    round_index: int
    max_rounds: int
    in_sync_specs: Dict[str, str] = field(default_factory=dict)
    failed_analyses: Dict[str, str] = field(default_factory=dict)
    reason: str = "quota_exhausted"
    started_at: str = field(default_factory=lambda: datetime.now().isoformat())
    checkpoint_version: int = CHECKPOINT_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "checkpoint_version": self.checkpoint_version,
            "started_at": self.started_at,
            "round_index": self.round_index,
            "max_rounds": self.max_rounds,
            "in_sync_specs": dict(self.in_sync_specs),
            "failed_analyses": dict(self.failed_analyses),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SyncCheckpoint:
        return cls(
            checkpoint_version=int(data.get("checkpoint_version", CHECKPOINT_SCHEMA_VERSION)),
            started_at=str(data.get("started_at") or datetime.now().isoformat()),
            round_index=int(data.get("round_index", 1)),
            max_rounds=int(data.get("max_rounds", 10)),
            in_sync_specs=dict(data.get("in_sync_specs") or {}),
            failed_analyses=dict(data.get("failed_analyses") or {}),
            reason=str(data.get("reason") or "quota_exhausted"),
        )


def checkpoint_path(project_root: Path) -> Path:
    return Path(project_root) / _CHECKPOINT_REL_PATH


def save(checkpoint: SyncCheckpoint, project_root: Path) -> Path:
    """Atomically write the checkpoint to ``se3/state/sync_checkpoint.json``.

    Writes to a ``.tmp`` file in the same directory and ``os.replace`` it
    onto the final path so a mid-write crash never leaves a half-written
    checkpoint behind.
    """
    path = checkpoint_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = json.dumps(
        checkpoint.to_dict(), indent=2, ensure_ascii=False, default=str
    )

    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix=".sync_checkpoint.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    logger.info("Wrote sync checkpoint to %s (reason=%s)", path, checkpoint.reason)
    return path


def load(project_root: Path) -> Optional[SyncCheckpoint]:
    """Read the checkpoint if it exists. Returns None when missing/invalid."""
    path = checkpoint_path(project_root)
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load sync checkpoint at %s: %s", path, exc)
        return None
    try:
        return SyncCheckpoint.from_dict(data)
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("Invalid sync checkpoint at %s: %s", path, exc)
        return None


def clear(project_root: Path) -> None:
    """Remove the checkpoint file. No-op when it doesn't exist."""
    path = checkpoint_path(project_root)
    try:
        path.unlink()
        logger.debug("Cleared sync checkpoint at %s", path)
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("Failed to clear sync checkpoint at %s: %s", path, exc)


def _hash_disk_spec(spec_path: Path) -> Optional[str]:
    """Return SHA-256 of the spec file using the same normalization as sync_engine."""
    try:
        content = spec_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    normalized = "\n".join(line.rstrip() for line in content.splitlines())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def recompute_in_sync(
    checkpoint: SyncCheckpoint, project_root: Path
) -> Tuple[set[str], set[str]]:
    """Compare on-disk sha256 of each recorded spec against the checkpoint.

    Returns ``(still_in_sync, changed_specs)``:

    * ``still_in_sync`` — specs whose disk content matches the recorded
      hash; these can be skipped on the resumed run.
    * ``changed_specs`` — specs whose disk content changed (or whose
      file disappeared); these need to be re-analyzed.
    """
    specs_root = Path(project_root) / "se3" / "specs"
    still_in_sync: set[str] = set()
    changed: set[str] = set()
    for name, recorded_hash in checkpoint.in_sync_specs.items():
        spec_path = specs_root / name / "spec.md"
        current = _hash_disk_spec(spec_path)
        if current is not None and current == recorded_hash:
            still_in_sync.add(name)
        else:
            changed.add(name)
    return still_in_sync, changed
