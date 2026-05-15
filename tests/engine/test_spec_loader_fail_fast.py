"""Tests for spec_loader full_spec mode and * wildcard behavior.

The ``*`` wildcard in ``requirement_name`` means "all items in this spec".
``base::*`` explicitly signals "no non-base items needed" — the analyze step
prompt instructs the LLM to output this instead of an empty list.

``_assemble_full_text`` keeps its ValueError guard as a safety net for
direct callers who pass a truly empty list.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from se3.engine.spec_format import SPEC_FORMAT_VERSION_MARKER
from se3.engine.spec_loader import load_for_step, _assemble_full_text


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


# ── _assemble_full_text direct calls (safety net) ──────────────────────

def test_assemble_full_text_empty_raises(tmp_path: Path) -> None:
    """Direct call with empty list raises ValueError (safety net)."""
    _write_base_spec(tmp_path)
    specs_dir = tmp_path / "se3" / "specs"
    with pytest.raises(ValueError, match="analyze"):
        _assemble_full_text(specs_dir, [])


def test_assemble_full_text_base_wildcard_is_valid(tmp_path: Path) -> None:
    """base::* is a legitimate item — loads base spec only."""
    _write_base_spec(tmp_path)
    specs_dir = tmp_path / "se3" / "specs"
    text, relevant_specs, loaded_items = _assemble_full_text(
        specs_dir,
        [{"spec": "base", "requirement_name": "*"}],
    )
    assert relevant_specs == ["base"]
    assert "Base One" in text


# ── load_for_step (public API) ────────────────────────────────────────

def test_load_empty_full_spec_loads_base(tmp_path: Path) -> None:
    """Empty selected_items via load_for_step loads base (backward compat)."""
    _write_base_spec(tmp_path)
    result = load_for_step(
        step_type="update_spec",
        selected_items=[],
        project_root=tmp_path,
        mode="full_spec",
    )
    assert "base" in result.relevant_specs
    assert "Base One" in result.text


def test_load_none_full_spec_loads_base(tmp_path: Path) -> None:
    """None selected_items via load_for_step loads base."""
    _write_base_spec(tmp_path)
    result = load_for_step(
        step_type="update_spec",
        selected_items=None,
        project_root=tmp_path,
        mode="full_spec",
    )
    assert "base" in result.relevant_specs
    assert "Base One" in result.text


def test_load_base_wildcard_full_spec(tmp_path: Path) -> None:
    """base::* means 'just base' — loads base only."""
    _write_base_spec(tmp_path)
    result = load_for_step(
        step_type="update_spec",
        selected_items=[{"spec": "base", "requirement_name": "*"}],
        project_root=tmp_path,
        mode="full_spec",
    )
    assert result.relevant_specs == ["base"]
    assert "Base One" in result.text


def test_load_base_wildcard_with_other_items(tmp_path: Path) -> None:
    """base::* alongside real items — base wildcard is harmless, real items used."""
    _write_base_spec(tmp_path)
    result = load_for_step(
        step_type="update_spec",
        selected_items=[
            {"spec": "base", "requirement_name": "*"},
            {"spec": "base", "requirement_name": "Base One"},
        ],
        project_root=tmp_path,
        mode="full_spec",
    )
    assert "base" in result.relevant_specs
    assert "Base One" in result.text


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
