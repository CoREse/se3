"""Tests for spec_loader.py — item-level and full-spec loading modes.

Covers: items mode assembly, full_spec mode assembly, base spec always loaded,
1-hop refs expansion, unselected spec omission, and character count comparison.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from se3.engine.spec_format import SPEC_FORMAT_VERSION_MARKER
from se3.engine.spec_loader import (
    LoadResult,
    load_for_step,
    load_full,
    _read_spec_text,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_spec(project_root: Path, spec_name: str, content: str) -> Path:
    """Write a spec file and return its path."""
    spec_dir = project_root / "se3" / "specs" / spec_name
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec_file = spec_dir / "spec.md"
    spec_file.write_text(content, encoding="utf-8")
    return spec_file


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """Create a minimal SE3 project with specs for loader testing."""
    return tmp_path


# ---------------------------------------------------------------------------
# 1. items mode output structure
# ---------------------------------------------------------------------------

def test_items_mode_basic(tmp_project: Path) -> None:
    """items mode outputs header + selected items, not unselected ones."""
    _write_spec(
        tmp_project,
        "base",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Base Spec
## Purpose
Base purpose.
### Requirement: Base One
Base one body.
### Requirement: Base Two
Base two body.
""",
    )
    _write_spec(
        tmp_project,
        "alpha",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Alpha Spec
## Purpose
Alpha purpose.
## Definitions
Alpha defs.

### Requirement: A1
A1 body.
**tags**: core

### Requirement: A2
A2 body.
**tags**: edge

### Requirement: A3
A3 body.
**tags**: unused
""",
    )

    result = load_for_step(
        step_type="test",
        selected_items=[{"spec": "alpha", "requirement_name": "A1"}],
        project_root=tmp_project,
        mode="items",
    )

    assert isinstance(result, LoadResult)
    assert "Base Spec" in result.text  # base always full
    assert "Base One" in result.text
    assert "Base Two" in result.text
    # alpha header present
    assert "Alpha Spec" in result.text
    assert "Alpha purpose" in result.text
    assert "Alpha defs" in result.text
    # selected item present
    assert "### Requirement: A1" in result.text
    assert "A1 body" in result.text
    # unselected items absent
    assert "A2 body" not in result.text
    assert "A3 body" not in result.text
    # metadata
    assert "base" in result.relevant_specs
    assert "alpha" in result.relevant_specs
    assert "alpha::A1" in result.loaded_items
    assert "alpha::A2" not in result.loaded_items


# ---------------------------------------------------------------------------
# 2. full_spec mode output structure
# ---------------------------------------------------------------------------

def test_full_spec_mode(tmp_project: Path) -> None:
    """full_spec mode outputs the entire text of each involved spec."""
    _write_spec(
        tmp_project,
        "base",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Base
## Purpose
Base purpose.
### Requirement: B1
B1 body.
""",
    )
    _write_spec(
        tmp_project,
        "gamma",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Gamma
## Purpose
Gamma purpose.
### Requirement: G1
G1 body.
### Requirement: G2
G2 body.
""",
    )

    result = load_for_step(
        step_type="test",
        selected_items=[{"spec": "gamma", "requirement_name": "G1"}],
        project_root=tmp_project,
        mode="full_spec",
    )

    assert "# Base" in result.text
    assert "# Gamma" in result.text
    assert "G1 body" in result.text
    assert "G2 body" in result.text  # full_spec includes all requirements
    assert "gamma::G1" in result.loaded_items
    assert "gamma::G2" in result.loaded_items


# ---------------------------------------------------------------------------
# 3. base spec always loaded
# ---------------------------------------------------------------------------

def test_base_always_loaded_even_when_not_selected(tmp_project: Path) -> None:
    """base spec is always included regardless of selection."""
    _write_spec(
        tmp_project,
        "base",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Base
## Purpose
Always here.
""",
    )
    _write_spec(
        tmp_project,
        "delta",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Delta
## Purpose
Delta purpose.
### Requirement: D1
D1 body.
""",
    )

    result = load_for_step(
        step_type="test",
        selected_items=[{"spec": "delta", "requirement_name": "D1"}],
        project_root=tmp_project,
        mode="items",
    )

    assert "# Base" in result.text
    assert "Always here" in result.text
    assert "base" in result.relevant_specs


def test_base_omitted_when_not_present(tmp_project: Path) -> None:
    """If base spec is missing, loader should handle gracefully."""
    _write_spec(
        tmp_project,
        "other",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Other
## Purpose
Other purpose.
### Requirement: O1
O1 body.
""",
    )

    result = load_for_step(
        step_type="test",
        selected_items=[{"spec": "other", "requirement_name": "O1"}],
        project_root=tmp_project,
        mode="items",
    )

    assert "# Other" in result.text
    assert "base" not in result.relevant_specs


# ---------------------------------------------------------------------------
# 4. refs 1-hop expansion
# ---------------------------------------------------------------------------

def test_items_mode_refs_one_hop(tmp_project: Path) -> None:
    """Selecting an item that references another expands the ref 1 hop."""
    _write_spec(
        tmp_project,
        "base",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Base
## Purpose
Base.
""",
    )
    _write_spec(
        tmp_project,
        "ref",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Ref Spec
## Purpose
Ref purpose.
### Requirement: Primary
Primary body. Also see Requirement: Secondary.

### Requirement: Secondary
Secondary body.

### Requirement: Tertiary
Tertiary body refers to Requirement: Secondary.
""",
    )

    result = load_for_step(
        step_type="test",
        selected_items=[{"spec": "ref", "requirement_name": "Primary"}],
        project_root=tmp_project,
        mode="items",
    )

    # Primary is selected
    assert "Primary body" in result.text
    # Secondary is referenced by Primary → expanded 1 hop
    assert "Secondary body" in result.text
    # Tertiary is not selected nor referenced by Primary
    assert "Tertiary body" not in result.text

    assert "ref::Primary" in result.loaded_items
    assert "ref::Secondary" in result.loaded_items
    assert "ref::Tertiary" not in result.loaded_items


def test_inter_spec_ref_expansion(tmp_project: Path) -> None:
    """Inter-spec refs (spec::requirement) are also expanded 1 hop."""
    _write_spec(
        tmp_project,
        "base",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Base
## Purpose
Base.
""",
    )
    _write_spec(
        tmp_project,
        "spec-a",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Spec A
## Purpose
A purpose.
### Requirement: Foo
Foo body.
""",
    )
    _write_spec(
        tmp_project,
        "spec-b",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Spec B
## Purpose
B purpose.
### Requirement: Bar
Bar body refers to spec-a::Foo.
""",
    )

    result = load_for_step(
        step_type="test",
        selected_items=[{"spec": "spec-b", "requirement_name": "Bar"}],
        project_root=tmp_project,
        mode="items",
    )

    assert "Bar body" in result.text
    # spec-a::Foo is referenced by Bar → expanded
    assert "Foo body" in result.text
    assert "spec-b::Bar" in result.loaded_items
    assert "spec-a::Foo" in result.loaded_items


# ---------------------------------------------------------------------------
# 5. unselected spec completely omitted
# ---------------------------------------------------------------------------

def test_unselected_spec_omitted(tmp_project: Path) -> None:
    """Specs with no selected items should not appear in output."""
    _write_spec(
        tmp_project,
        "base",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Base
## Purpose
Base.
""",
    )
    _write_spec(
        tmp_project,
        "included",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Included
## Purpose
Included purpose.
### Requirement: I1
I1 body.
""",
    )
    _write_spec(
        tmp_project,
        "excluded",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Excluded
## Purpose
Excluded purpose.
### Requirement: E1
E1 body.
""",
    )

    result = load_for_step(
        step_type="test",
        selected_items=[{"spec": "included", "requirement_name": "I1"}],
        project_root=tmp_project,
        mode="items",
    )

    assert "# Included" in result.text
    assert "# Excluded" not in result.text
    assert "E1 body" not in result.text
    assert "included" in result.relevant_specs
    assert "excluded" not in result.relevant_specs


# ---------------------------------------------------------------------------
# 6. fallback when selected_items is empty/None
# ---------------------------------------------------------------------------

def test_empty_selected_items_returns_base_only(tmp_project: Path) -> None:
    """When selected_items is empty, only base spec is loaded."""
    _write_spec(
        tmp_project,
        "base",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Base
## Purpose
Base purpose.
""",
    )

    result = load_for_step(
        step_type="test",
        selected_items=[],
        project_root=tmp_project,
        mode="items",
    )

    assert "# Base" in result.text
    assert result.relevant_specs == ["base"]
    assert result.loaded_items == []


def test_none_selected_items(tmp_project: Path) -> None:
    """When selected_items is None, only base spec is loaded."""
    _write_spec(
        tmp_project,
        "base",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Base
## Purpose
Base.
""",
    )

    result = load_for_step(
        step_type="test",
        selected_items=None,
        project_root=tmp_project,
        mode="items",
    )

    assert "# Base" in result.text
    assert result.relevant_specs == ["base"]


# ---------------------------------------------------------------------------
# 7. Character count comparison: items << full_spec
# ---------------------------------------------------------------------------

def test_items_mode_much_smaller_than_full_spec(tmp_project: Path) -> None:
    """Selecting a single item should produce far less text than full_spec."""
    _write_spec(
        tmp_project,
        "base",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Base
## Purpose
Base purpose.
### Requirement: B1
B1 body.
""",
    )
    # Create a spec with many requirements to exaggerate the difference
    reqs = "\n\n".join(
        f"### Requirement: Req {i}\nBody of requirement {i} with some content."
        for i in range(50)
    )
    _write_spec(
        tmp_project,
        "big",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Big Spec
## Purpose
Big purpose.
## Definitions
Lots of definitions here.
## Constraints
Many constraints.

{reqs}
""",
    )

    items_result = load_for_step(
        step_type="test",
        selected_items=[{"spec": "big", "requirement_name": "Req 5"}],
        project_root=tmp_project,
        mode="items",
    )
    full_result = load_for_step(
        step_type="test",
        selected_items=[{"spec": "big", "requirement_name": "Req 5"}],
        project_root=tmp_project,
        mode="full_spec",
    )

    assert len(items_result.text) < len(full_result.text)
    # items should be dramatically smaller (at least 50% reduction)
    assert len(items_result.text) < len(full_result.text) * 0.5


# ---------------------------------------------------------------------------
# 8. load_full helper
# ---------------------------------------------------------------------------

def test_load_full_multiple_specs(tmp_project: Path) -> None:
    """load_full concatenates full text of named specs."""
    _write_spec(
        tmp_project,
        "s1",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# S1
## Purpose
S1 purpose.
### Requirement: R1
R1 body.
""",
    )
    _write_spec(
        tmp_project,
        "s2",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# S2
## Purpose
S2 purpose.
### Requirement: R2
R2 body.
""",
    )

    text = load_full(["s1", "s2"], tmp_project)
    assert "# S1" in text
    assert "# S2" in text
    assert "R1 body" in text
    assert "R2 body" in text


def test_load_full_missing_spec_ignored(tmp_project: Path) -> None:
    """load_full silently skips missing specs."""
    _write_spec(
        tmp_project,
        "exists",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Exists
## Purpose
Exists.
""",
    )

    text = load_full(["exists", "missing"], tmp_project)
    assert "# Exists" in text
    assert "missing" not in text


# ---------------------------------------------------------------------------
# 9. _read_spec_text edge cases
# ---------------------------------------------------------------------------

def test_read_spec_text_missing_returns_none(tmp_project: Path) -> None:
    """_read_spec_text returns None for missing specs."""
    specs_dir = tmp_project / "se3" / "specs"
    assert _read_spec_text(specs_dir, "nonexistent") is None


# ---------------------------------------------------------------------------
# 12. trailing text preservation
# ---------------------------------------------------------------------------

def test_trailing_text_preserved_in_items_mode(tmp_project: Path) -> None:
    """Orphan H2 sections after the last Requirement are preserved in items mode."""
    _write_spec(
        tmp_project,
        "base",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Base
## Purpose
Base.
""",
    )
    _write_spec(
        tmp_project,
        "flow",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Flow Spec
## Purpose
Flow purpose.

### Requirement: State Machine
State machine body.

## Architecture
Architecture overview text.

## CLI Commands
CLI details.
""",
    )

    result = load_for_step(
        step_type="test",
        selected_items=[{"spec": "flow", "requirement_name": "State Machine"}],
        project_root=tmp_project,
        mode="items",
    )

    assert "State machine body" in result.text
    assert "## Architecture" in result.text
    assert "Architecture overview text" in result.text
    assert "## CLI Commands" in result.text
    assert "CLI details" in result.text


# ---------------------------------------------------------------------------
# 10. Trailing text preserved when zero requirements match
# ---------------------------------------------------------------------------

def test_trailing_text_preserved_with_zero_matching_requirements(tmp_project: Path) -> None:
    """A spec in involved_specs with no matching reqs but trailing text still emits header+trailing."""
    _write_spec(
        tmp_project,
        "base",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Base
## Purpose
Base.
""",
    )
    _write_spec(
        tmp_project,
        "orphan",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Orphan Spec
## Purpose
Orphan purpose.
## Definitions
Orphan defs.

### Requirement: R1
R1 body.

## Architecture
Architecture overview text.

## CLI Commands
CLI details.
""",
    )

    # Select a non-existent requirement from "orphan" — the spec is in
    # involved_specs but no requirements match. It should still emit
    # header + trailing_text because trailing_text exists.
    result = load_for_step(
        step_type="test",
        selected_items=[{"spec": "orphan", "requirement_name": "DoesNotExist"}],
        project_root=tmp_project,
        mode="items",
    )

    assert "# Orphan Spec" in result.text
    assert "Orphan purpose" in result.text
    assert "Orphan defs" in result.text
    assert "## Architecture" in result.text
    assert "Architecture overview text" in result.text
    assert "## CLI Commands" in result.text
    assert "CLI details" in result.text
    # The non-existent requirement is not present
    assert "### Requirement: DoesNotExist" not in result.text
    # R1 is not selected, so it should NOT appear
    assert "R1 body" not in result.text
    assert "orphan" in result.relevant_specs


# ---------------------------------------------------------------------------
# 11. Multiple selected items across specs
# ---------------------------------------------------------------------------

def test_multiple_items_across_specs(tmp_project: Path) -> None:
    """Selecting items from multiple specs assembles each correctly."""
    _write_spec(
        tmp_project,
        "base",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Base
## Purpose
Base.
""",
    )
    _write_spec(
        tmp_project,
        "x",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# X
## Purpose
X purpose.
### Requirement: X1
X1 body.
### Requirement: X2
X2 body.
""",
    )
    _write_spec(
        tmp_project,
        "y",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Y
## Purpose
Y purpose.
### Requirement: Y1
Y1 body.
""",
    )

    result = load_for_step(
        step_type="test",
        selected_items=[
            {"spec": "x", "requirement_name": "X1"},
            {"spec": "y", "requirement_name": "Y1"},
        ],
        project_root=tmp_project,
        mode="items",
    )

    assert "# X" in result.text
    assert "X1 body" in result.text
    assert "X2 body" not in result.text
    assert "# Y" in result.text
    assert "Y1 body" in result.text
    assert result.relevant_specs == ["base", "x", "y"]


# ---------------------------------------------------------------------------
# 11. Invalid selected_items entries are filtered
# ---------------------------------------------------------------------------

def test_invalid_selected_items_filtered(tmp_project: Path) -> None:
    """Entries missing spec or requirement_name are skipped."""
    _write_spec(
        tmp_project,
        "base",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Base
## Purpose
Base.
""",
    )
    _write_spec(
        tmp_project,
        "z",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Z
## Purpose
Z purpose.
### Requirement: Z1
Z1 body.
""",
    )

    result = load_for_step(
        step_type="test",
        selected_items=[
            {"spec": "z", "requirement_name": "Z1"},
            {"spec": "z"},  # missing requirement_name
            {"requirement_name": "Z1"},  # missing spec
            "not a dict",
        ],
        project_root=tmp_project,
        mode="items",
    )

    assert "Z1 body" in result.text
    assert result.loaded_items == ["z::Z1"]
