"""Tests for spec_loader fail-fast behavior in full_spec mode.

`full_spec` mode with empty (or None) selected_items is a strong signal that
the analyze step failed to pick any relevant spec items — surface it as a hard
error instead of silently degrading to base-spec-only loading.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from se3.engine.spec_format import SPEC_FORMAT_VERSION_MARKER
from se3.engine.spec_loader import load_for_step


def _write_base_spec(project_root: Path) -> None:
    spec_dir = project_root / "se3" / "specs" / "base"
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.md").write_text(
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Base Spec
## Purpose
Base purpose.
### Requirement: Base One
Base one body.
""",
        encoding="utf-8",
    )


def test_full_spec_empty_selected_items_raises(tmp_path: Path) -> None:
    _write_base_spec(tmp_path)
    with pytest.raises(ValueError, match="analyze"):
        load_for_step(
            step_type="update_spec",
            selected_items=[],
            project_root=tmp_path,
            mode="full_spec",
        )


def test_full_spec_none_selected_items_raises(tmp_path: Path) -> None:
    _write_base_spec(tmp_path)
    with pytest.raises(ValueError, match="analyze"):
        load_for_step(
            step_type="update_spec",
            selected_items=None,
            project_root=tmp_path,
            mode="full_spec",
        )


def test_items_mode_empty_selected_items_still_allowed(tmp_path: Path) -> None:
    _write_base_spec(tmp_path)
    result = load_for_step(
        step_type="plan",
        selected_items=[],
        project_root=tmp_path,
        mode="items",
    )
    assert "base" in result.relevant_specs
    assert "Base purpose." in result.text
