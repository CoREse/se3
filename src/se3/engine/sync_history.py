"""Lightweight flow context for sync history recording.

Generates flow_id and step_id values so that LLMCaller can automatically
record prompts/responses via the existing ChatHistory infrastructure.
"""

from __future__ import annotations

import json
import logging
import platform
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class SyncFlowContext:
    """Manages flow_id and step_id generation for a single sync session.

    The actual prompt/response recording is handled by LLMCaller when
    flow_id and step_id are passed to its constructor — this class only
    generates the identifiers and writes ``_meta.json``.
    """

    def __init__(self, project_root: Path, flow_id: Optional[str] = None) -> None:
        self.project_root = Path(project_root)
        self.flow_id = flow_id or self._generate_flow_id()
        self._step_counters: dict[str, int] = {}

    @staticmethod
    def _generate_flow_id() -> str:
        now = datetime.now()
        hex8 = uuid.uuid4().hex[:8]
        return f"{now.strftime('%Y%m%d-%H%M%S')}_{hex8}"

    def make_step_id(self, step_type: str, suffix: Optional[str] = None) -> str:
        """Build a step_id like ``sync_scan_0`` or ``sync_analyze_flow-engine``.

        Args:
            step_type: One of sync_scan, sync_analyze, sync_resolve.
            suffix: Optional qualifier (spec name, item id, etc.).
                    When omitted, an auto-incrementing counter is used.
        """
        if suffix is not None:
            return f"{step_type}_{suffix}"
        count = self._step_counters.get(step_type, 0)
        self._step_counters[step_type] = count + 1
        return f"{step_type}_{count}"

    def write_meta(self) -> Path:
        """Write ``_meta.json`` to ``se3/history/{flow_id}/``.

        Returns:
            Path to the written file.
        """
        history_dir = self.project_root / "se3" / "history" / self.flow_id
        meta_path = history_dir / "_meta.json"

        if meta_path.exists():
            logger.debug("_meta.json already exists for sync flow %s, skipping", self.flow_id)
            return meta_path

        history_dir.mkdir(parents=True, exist_ok=True)

        try:
            from se3 import __version__ as se3_version
        except Exception:
            se3_version = "unknown"

        meta = {
            "se3_version": se3_version,
            "python_version": platform.python_version(),
            "created_at": datetime.now().isoformat(),
            "type": "sync",
        }

        try:
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2, ensure_ascii=False)
            logger.debug("Wrote _meta.json for sync flow %s", self.flow_id)
        except OSError as e:
            logger.warning("Failed to write _meta.json: %s", e)

        return meta_path
