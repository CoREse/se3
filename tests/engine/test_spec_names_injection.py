"""Tests for context_builder.get_spec_names_injection.

Covers whitelist/blacklist gating, yaml override behavior, forbidden-step
precedence, loaded-specs rendering, all-specs sorting, and empty-input edge
cases.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from se3.engine.context_builder import (
    SPEC_NAMES_INJECTION_DEFAULT_STEPS,
    SPEC_NAMES_INJECTION_FORBIDDEN_STEPS,
    get_spec_names_injection,
)


def _make_project_root(tmp_path: Path, spec_names: list[str]) -> Path:
    """Create a minimal project root with se3/specs/<name>/spec.md files."""
    specs_dir = tmp_path / "se3" / "specs"
    specs_dir.mkdir(parents=True)
    for name in spec_names:
        spec_dir = specs_dir / name
        spec_dir.mkdir()
        (spec_dir / "spec.md").write_text(f"# {name}\n", encoding="utf-8")
    return tmp_path


def test_forbidden_step_returns_empty(tmp_path):
    project_root = _make_project_root(tmp_path, ["base", "flow-engine"])
    for step in SPEC_NAMES_INJECTION_FORBIDDEN_STEPS:
        assert get_spec_names_injection(step, project_root, ["base"]) == ""


def test_default_whitelist_returns_non_empty(tmp_path):
    project_root = _make_project_root(tmp_path, ["base", "flow-engine"])
    result = get_spec_names_injection("plan", project_root, ["base"])
    assert "## Available Specifications" in result
    assert "base" in result
    assert "flow-engine" in result
    assert "se3/specs/<name>/spec.md" in result
    assert "MAY" in result
    assert "avoid reading broadly" in result


def test_yaml_override_narrows_whitelist(tmp_path):
    project_root = _make_project_root(tmp_path, ["base", "flow-engine"])
    (project_root / "se3.yaml").write_text(
        "spec_names_injection:\n  steps: [only_plan]\n",
        encoding="utf-8",
    )
    # implement is in DEFAULT but not in the override -> empty
    assert get_spec_names_injection("implement", project_root, ["base"]) == ""
    # only_plan is in the override -> non-empty
    result = get_spec_names_injection("only_plan", project_root, ["base"])
    assert "## Available Specifications" in result


def test_forbidden_takes_precedence_over_yaml(tmp_path):
    project_root = _make_project_root(tmp_path, ["base"])
    (project_root / "se3.yaml").write_text(
        "spec_names_injection:\n  steps: [summarize, plan]\n",
        encoding="utf-8",
    )
    # summarize is FORBIDDEN — yaml cannot re-enable it
    assert get_spec_names_injection("summarize", project_root, ["base"]) == ""
    # plan remains enabled via yaml
    assert "## Available Specifications" in get_spec_names_injection(
        "plan", project_root, ["base"]
    )


def test_loaded_list_rendering(tmp_path):
    project_root = _make_project_root(tmp_path, ["base", "flow-engine", "issue-discovery"])
    result = get_spec_names_injection(
        "plan", project_root, ["base", "flow-engine"]
    )
    assert "Specs already loaded above: base, flow-engine" in result


def test_all_spec_names_sorted(tmp_path):
    # Intentionally create specs in non-alphabetical order
    project_root = _make_project_root(tmp_path, ["zulu", "alpha", "mike"])
    result = get_spec_names_injection("plan", project_root, None)
    listing_line = next(
        line for line in result.splitlines()
        if line.startswith("All available specs in this project:")
    )
    # Extract names in the rendered order
    tail = listing_line.split(":", 1)[1].rstrip(".").strip()
    names = [n.strip() for n in tail.split(",")]
    assert names == ["alpha", "mike", "zulu"]


def test_empty_relevant_specs_renders_none(tmp_path):
    project_root = _make_project_root(tmp_path, ["base"])
    # None
    result_none = get_spec_names_injection("plan", project_root, None)
    assert "Specs already loaded above: none" in result_none
    # Empty list
    result_empty = get_spec_names_injection("plan", project_root, [])
    assert "Specs already loaded above: none" in result_empty


def test_defaults_cover_expected_steps():
    # Sanity check: the task spec requires these steps to be default-enabled
    for step in [
        "plan",
        "plan_tasks",
        "implement",
        "verify_spec",
        "update_spec",
        "self_check",
        "design",
    ]:
        assert step in SPEC_NAMES_INJECTION_DEFAULT_STEPS
