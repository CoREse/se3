"""Tests for ``se3 sync`` commands.

Covers:

* ``validate_only_command`` — exit codes and output for valid / broken specs.
* ``sync_command`` KeyboardInterrupt handler — message accuracy depending on
  whether a checkpoint was persisted.

Drives helpers directly with ``tmp_path`` project layouts; does not shell
out to the CLI runner.
"""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from se3.commands.sync import sync_command, validate_only_command
from se3.engine.spec_validator import V1_MARKER


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _good_spec_body(name: str = "auth") -> str:
    return (
        f"{V1_MARKER}\n"
        f"# {name} Specification\n"
        "\n"
        "## Purpose\n"
        f"The {name} subsystem manages a thing.\n"
        "\n"
        "## Requirements\n"
        "\n"
        f"### Requirement: {name} core\n"
        f"The system SHALL handle {name}.\n"
        "\n"
        "#### Scenario: Happy path\n"
        "- **WHEN** invoked\n"
        "- **THEN** it works\n"
    )


def _make_spec(project_root: Path, name: str, body: str) -> Path:
    spec_dir = project_root / "se3" / "specs" / name
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec_path = spec_dir / "spec.md"
    spec_path.write_text(body, encoding="utf-8")
    return spec_path


@pytest.fixture
def captured_console():
    """Capture get_console() output via a Rich Console pointed at StringIO.

    The CLI helper uses ``get_console()`` from ``engine.display``;
    monkey-patching it gives us a deterministic string we can assert
    against without touching the real terminal. ``render_text`` /
    ``render_block_header`` / ``render_block_footer`` go to a different
    function that prints to stdout — those we capture via capsys in the
    individual tests when needed.
    """
    from rich.console import Console
    buf = io.StringIO()
    test_console = Console(file=buf, width=200, force_terminal=False)
    with patch("se3.commands.sync.get_console", return_value=test_console):
        yield buf


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_clean_repo_exits_zero(tmp_path, captured_console, capsys):
    """All-valid specs → exit code 0, table shows PASS for each."""
    _make_spec(tmp_path, "auth", _good_spec_body("auth"))
    _make_spec(tmp_path, "billing", _good_spec_body("billing"))

    code = validate_only_command(project_root=tmp_path)
    output = captured_console.getvalue() + capsys.readouterr().out

    assert code == 0
    assert "auth" in output
    assert "billing" in output
    assert "PASS" in output
    assert "FAIL" not in output


def test_meta_summary_spec_fails(tmp_path, captured_console, capsys):
    """A documentation-updater style meta summary triggers exit 1.

    The fail row MUST name the spec and surface validator errors so
    the operator can tell *which* spec is broken.
    """
    _make_spec(tmp_path, "auth", _good_spec_body("auth"))
    meta = (
        "I have explored the documentation-updater module. The class "
        "DocumentationUpdater handles README and VERSIONS updates. I "
        "will now produce the spec.\n"
    )
    _make_spec(tmp_path, "documentation-updater", meta)

    code = validate_only_command(project_root=tmp_path)
    output = captured_console.getvalue() + capsys.readouterr().out

    assert code == 1
    assert "documentation-updater" in output
    assert "FAIL" in output
    # At least one validator error mentioned (v1 marker missing or
    # narrative-prose start).
    assert ("v1 marker" in output) or ("narrative" in output)


def test_missing_v1_header_fails(tmp_path, captured_console, capsys):
    """A spec body that's structurally fine but missing the v1 marker
    triggers exit 1 with a marker-specific error."""
    _make_spec(tmp_path, "auth", _good_spec_body("auth"))
    no_marker = _good_spec_body("billing").replace(V1_MARKER + "\n", "", 1)
    _make_spec(tmp_path, "billing", no_marker)

    code = validate_only_command(project_root=tmp_path)
    output = captured_console.getvalue() + capsys.readouterr().out

    assert code == 1
    assert "billing" in output
    assert "v1 marker" in output


def test_no_specs_dir_returns_zero(tmp_path, captured_console, capsys):
    """An empty project (no se3/specs/) is not a failure."""
    code = validate_only_command(project_root=tmp_path)
    assert code == 0


def test_skips_underscore_dirs(tmp_path, captured_console, capsys):
    """``_changelog/`` and similar are framework-internal, not specs."""
    _make_spec(tmp_path, "auth", _good_spec_body("auth"))
    # _changelog is a directory of changelog markdown files, not specs.
    changelog = tmp_path / "se3" / "specs" / "_changelog"
    changelog.mkdir(parents=True)
    (changelog / "2026-05-14-something.md").write_text("# changelog entry\n")

    code = validate_only_command(project_root=tmp_path)
    output = captured_console.getvalue() + capsys.readouterr().out

    assert code == 0
    assert "_changelog" not in output


def test_does_not_invoke_llm(tmp_path, monkeypatch, captured_console, capsys):
    """Validator MUST be read-only: no LLMCaller construction or calls.

    We assert this by patching the LLMCaller import path; any attempt
    to use it will raise.
    """
    _make_spec(tmp_path, "auth", _good_spec_body("auth"))

    def _explode(*a, **kw):
        raise AssertionError("validate_only_command should not call the LLM")

    monkeypatch.setattr("se3.engine.llm_caller.LLMCaller", _explode)

    code = validate_only_command(project_root=tmp_path)
    assert code == 0


def test_does_not_write_files(tmp_path, captured_console, capsys):
    """Validator MUST not modify any spec files."""
    spec_path = _make_spec(tmp_path, "auth", _good_spec_body("auth"))
    before = spec_path.read_bytes()
    before_mtime = spec_path.stat().st_mtime_ns

    code = validate_only_command(project_root=tmp_path)
    assert code == 0
    assert spec_path.read_bytes() == before
    assert spec_path.stat().st_mtime_ns == before_mtime


# ---------------------------------------------------------------------------
# KeyboardInterrupt handler tests (sync_command)
# ---------------------------------------------------------------------------


class TestSyncCommandKeyboardInterrupt:
    """Assert that the CLI KeyboardInterrupt handler reports accurately
    about whether a checkpoint was persisted, so the user is not misled
    into running ``se3 sync --resume`` when no checkpoint exists."""

    def test_no_checkpoint_on_interrupt(self, tmp_path, capsys):
        """Ctrl-C during normal analysis (no infra-failure threshold hit)
        → message says no checkpoint was written, exit code 130."""
        # Ensure no checkpoint exists on disk.
        cp_path = tmp_path / "se3" / "state" / "sync_checkpoint.json"
        assert not cp_path.exists()

        # SyncLoop is imported lazily inside sync_command via
        # ``from ..engine.sync_loop import SyncLoop`` — patch the
        # source so the local import picks up the mock.
        with patch(
            "se3.engine.sync_loop.SyncLoop",
            autospec=True,
        ) as mock_loop_cls:
            mock_loop = MagicMock()
            mock_loop.run.side_effect = KeyboardInterrupt
            mock_loop_cls.return_value = mock_loop

            with pytest.raises(SystemExit) as exc_info:
                sync_command(project_root=tmp_path)

        assert exc_info.value.code == 130

        captured = capsys.readouterr().out
        assert "no checkpoint was written" in captured
        assert "re-run" not in captured

    def test_checkpoint_exists_on_interrupt(self, tmp_path, capsys):
        """Ctrl-C after the infra-failure threshold was hit and a
        checkpoint was persisted → message confirms the checkpoint path
        and suggests resume."""
        cp_path = tmp_path / "se3" / "state" / "sync_checkpoint.json"
        cp_path.parent.mkdir(parents=True, exist_ok=True)
        cp_path.write_text(
            '{"checkpoint_version":1,"round_index":1,"max_rounds":10,'
            '"in_sync_specs":{},"failed_analyses":{},"reason":"quota_exhausted"}',
            encoding="utf-8",
        )
        assert cp_path.exists()

        with patch(
            "se3.engine.sync_loop.SyncLoop",
            autospec=True,
        ) as mock_loop_cls:
            mock_loop = MagicMock()
            mock_loop.run.side_effect = KeyboardInterrupt
            mock_loop_cls.return_value = mock_loop

            with pytest.raises(SystemExit) as exc_info:
                sync_command(project_root=tmp_path)

        assert exc_info.value.code == 130

        captured = capsys.readouterr().out
        assert "Checkpoint preserved" in captured
        assert "se3 sync --resume" in captured
