"""Tests for the spec volume-governance size guardrails (G5).

Covers the pure ``check_spec_sizes`` function and the ``se3 guardrails
--sizes`` CLI pathway in both the ``warn`` (non-blocking) and ``enforce``
(intercepting) tiers.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from tianluo.cli import app
from tianluo.config import SpecGovernanceConfig
from tianluo.engine.merge.guardrails import (
    SIZE_BASE,
    SIZE_REQUIREMENT,
    SIZE_SPEC_FILE,
    check_spec_sizes,
)


def _write_spec(specs_dir: Path, name: str, body: str) -> None:
    p = specs_dir / name / "spec.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


def _spec_body(spec_name: str, req_name: str, filler: int) -> str:
    return (
        "<!-- spec-format: v1 -->\n"
        f"# {spec_name} Specification\n"
        "## Purpose\n"
        "A purpose line.\n\n"
        f"### Requirement: {req_name}\n"
        + ("x" * filler)
        + "\n"
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project with an oversized base, an oversized spec file, and an
    oversized single Requirement — one of each so all three checks fire."""
    specs = tmp_path / "tianluo" / "specs"
    # base: large file (also has a small requirement)
    _write_spec(specs, "base", _spec_body("base", "Base R1", 4000))
    # foo: large file AND a single large requirement
    _write_spec(specs, "foo", _spec_body("foo", "Huge Req", 5000))
    # small: within every budget
    _write_spec(specs, "small", _spec_body("small", "Tiny", 50))
    return tmp_path


# ---------------------------------------------------------------------------
# Pure function: check_spec_sizes
# ---------------------------------------------------------------------------

def test_three_oversize_kinds_each_produce_a_violation(project: Path) -> None:
    config = SpecGovernanceConfig(
        base_max_bytes=1000,
        spec_file_warn_bytes=2000,
        requirement_warn_bytes=1000,
    )
    violations = check_spec_sizes(project, config)
    types = {v.violation_type for v in violations}
    assert SIZE_BASE in types
    assert SIZE_SPEC_FILE in types
    assert SIZE_REQUIREMENT in types


def test_evidence_contains_size_and_limit(project: Path) -> None:
    config = SpecGovernanceConfig(
        base_max_bytes=1000,
        spec_file_warn_bytes=2000,
        requirement_warn_bytes=1000,
    )
    violations = check_spec_sizes(project, config)
    assert violations
    for v in violations:
        ev = v.evidence or {}
        assert "size_bytes" in ev
        assert "limit_bytes" in ev
        assert ev["size_bytes"] > ev["limit_bytes"]
        assert "spec_name" in ev


def test_base_reported_as_size_base_not_spec_file(project: Path) -> None:
    # base exceeds both base_max_bytes and spec_file_warn_bytes, but is
    # reported only under SIZE_BASE (its stricter, dedicated budget).
    config = SpecGovernanceConfig(
        base_max_bytes=1000,
        spec_file_warn_bytes=1000,
        requirement_warn_bytes=100000,
    )
    violations = check_spec_sizes(project, config)
    base_violations = [v for v in violations if v.evidence and v.evidence.get("spec_name") == "base"]
    assert base_violations
    assert all(v.violation_type == SIZE_BASE for v in base_violations)


def test_requirement_violation_names_the_requirement(project: Path) -> None:
    config = SpecGovernanceConfig(
        base_max_bytes=100000,
        spec_file_warn_bytes=100000,
        requirement_warn_bytes=1000,
    )
    violations = check_spec_sizes(project, config)
    req_violations = [v for v in violations if v.violation_type == SIZE_REQUIREMENT]
    assert req_violations
    assert any(
        v.evidence and v.evidence.get("requirement_name") == "Huge Req"
        for v in req_violations
    )


def test_no_violations_when_within_budget(project: Path) -> None:
    config = SpecGovernanceConfig(
        base_max_bytes=100000,
        spec_file_warn_bytes=100000,
        requirement_warn_bytes=100000,
    )
    assert check_spec_sizes(project, config) == []


def test_deterministic_output_order(project: Path) -> None:
    config = SpecGovernanceConfig(
        base_max_bytes=1000,
        spec_file_warn_bytes=2000,
        requirement_warn_bytes=1000,
    )
    first = [
        (v.violation_type, v.file_path, (v.evidence or {}).get("requirement_name"))
        for v in check_spec_sizes(project, config)
    ]
    second = [
        (v.violation_type, v.file_path, (v.evidence or {}).get("requirement_name"))
        for v in check_spec_sizes(project, config)
    ]
    assert first == second


def test_unreadable_or_empty_project_does_not_raise(tmp_path: Path) -> None:
    # No tianluo/specs at all — must not raise and returns nothing.
    config = SpecGovernanceConfig()
    assert check_spec_sizes(tmp_path, config) == []


def test_unreadable_spec_yields_check_incomplete(tmp_path: Path, monkeypatch) -> None:
    """An OSError reading a spec must NOT be silently skipped: it yields a
    CHECK_INCOMPLETE violation so enforce mode blocks (its size limits were
    never verified)."""
    specs = tmp_path / "tianluo" / "specs"
    _write_spec(specs, "base", _spec_body("base", "Base R1", 50))

    real_read_bytes = Path.read_bytes

    def fake_read_bytes(self: Path):
        if self.parent.name == "base":
            raise OSError("permission denied")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)

    config = SpecGovernanceConfig()
    violations = check_spec_sizes(tmp_path, config)
    incomplete = [v for v in violations if v.violation_type == "CHECK_INCOMPLETE"]
    assert incomplete, "unreadable spec should produce a CHECK_INCOMPLETE violation"
    assert (incomplete[0].evidence or {}).get("exception_type") == "OSError"


def test_cli_enforce_blocks_on_unreadable_spec(tmp_path: Path, monkeypatch) -> None:
    """Under enforce, an unreadable (un-verifiable) spec fails the CLI rather
    than exiting 0."""
    specs = tmp_path / "tianluo" / "specs"
    _write_spec(specs, "base", _spec_body("base", "Base R1", 50))
    _write_yaml(tmp_path, "enforce")

    real_read_bytes = Path.read_bytes

    def fake_read_bytes(self: Path):
        if self.parent.name == "base":
            raise OSError("permission denied")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)

    runner = CliRunner()
    result = runner.invoke(app, ["guardrails", "--sizes", "-p", str(tmp_path)])
    assert result.exit_code == 1
    assert "CHECK_INCOMPLETE" in result.stdout


# ---------------------------------------------------------------------------
# CLI pathway: se3 guardrails --sizes
# ---------------------------------------------------------------------------

def _write_yaml(root: Path, tier: str) -> None:
    (root / "tianluo.yaml").write_text(
        "spec_governance:\n"
        "  base_max_bytes: 1000\n"
        "  spec_file_warn_bytes: 2000\n"
        "  requirement_warn_bytes: 1000\n"
        f"  guardrails_size_tier: {tier}\n"
    )


def test_cli_warn_tier_reports_but_does_not_block(project: Path) -> None:
    _write_yaml(project, "warn")
    runner = CliRunner()
    result = runner.invoke(app, ["guardrails", "--sizes", "-p", str(project)])
    assert result.exit_code == 0
    assert "violation" in result.stdout.lower()


def test_cli_enforce_tier_intercepts(project: Path) -> None:
    _write_yaml(project, "enforce")
    runner = CliRunner()
    result = runner.invoke(app, ["guardrails", "--sizes", "-p", str(project)])
    assert result.exit_code == 1
    assert "violation" in result.stdout.lower()


def test_cli_default_tier_is_warn(project: Path) -> None:
    # No tianluo.yaml → default tier (warn) → non-blocking even with violations.
    runner = CliRunner()
    result = runner.invoke(app, ["guardrails", "--sizes", "-p", str(project)])
    assert result.exit_code == 0


def test_cli_passes_when_within_budget(tmp_path: Path) -> None:
    specs = tmp_path / "tianluo" / "specs"
    _write_spec(specs, "base", _spec_body("base", "R1", 50))
    _write_spec(specs, "foo", _spec_body("foo", "R2", 50))
    runner = CliRunner()
    result = runner.invoke(app, ["guardrails", "--sizes", "-p", str(tmp_path)])
    assert result.exit_code == 0
    assert "passed" in result.stdout.lower()
