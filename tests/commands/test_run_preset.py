"""CLI integration tests for `se3 run --preset` (preset prompt mechanism).

Covers acceptance items 2.5 (a)-(f):
- (a) --preset and --type are mutually exclusive.
- (b) Unknown preset error includes the available list.
- (c) The preset's type and full prompt reach run_flow (→ FlowInstance).
- (d) A declared-but-missing prompt_file errors (not silently swallowed).
- (e) `--preset list` lists both built-in and project layers.
- (f) A project preset overrides the built-in one of the same name.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from se3.cli import app

runner = CliRunner()


@pytest.fixture
def project_root(tmp_path):
    # A bare directory is enough; get_project_root is patched to return it.
    return tmp_path


def _invoke(args, project_root, run_flow_mock=None):
    """Invoke the CLI `run` command with project root + run_flow patched."""
    rf = run_flow_mock if run_flow_mock is not None else MagicMock(return_value=0)
    with patch("se3.commands.run.get_project_root", return_value=project_root), patch(
        "se3.commands.run.run_flow", rf
    ):
        result = runner.invoke(app, ["run"] + args)
    return result, rf


def _write_project_doc_sync(root: Path, body: str):
    prompts_dir = root / "se3" / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / "doc-sync.md").write_text(body, encoding="utf-8")
    (root / "se3.yaml").write_text(
        "presets:\n"
        "  doc-sync:\n"
        "    type: feature\n"
        "    prompt_file: se3/prompts/doc-sync.md\n",
        encoding="utf-8",
    )


def test_preset_and_type_mutually_exclusive(project_root):
    result, rf = _invoke(["--preset", "doc-sync", "--type", "bugfix"], project_root)
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output.lower()
    rf.assert_not_called()


def test_unknown_preset_lists_available(project_root):
    result, rf = _invoke(["--preset", "no-such-preset"], project_root)
    assert result.exit_code == 1
    out = result.output
    assert "no-such-preset" in out
    # The built-in doc-sync should be advertised as available.
    assert "doc-sync" in out
    rf.assert_not_called()


def test_preset_type_and_prompt_reach_run_flow(project_root):
    result, rf = _invoke(["--preset", "doc-sync"], project_root)
    assert result.exit_code == 0
    rf.assert_called_once()
    kwargs = rf.call_args.kwargs
    # Built-in doc-sync carries type=feature.
    assert kwargs.get("task_type") == "feature"
    # task_description is the full preset prompt text.
    assert kwargs.get("task_description")
    assert "README" in kwargs["task_description"]


def test_missing_prompt_file_errors(project_root):
    # Declared in se3.yaml, but the file does not exist on disk.
    (project_root / "se3.yaml").write_text(
        "presets:\n"
        "  ghost:\n"
        "    type: feature\n"
        "    prompt_file: se3/prompts/ghost.md\n",
        encoding="utf-8",
    )
    result, rf = _invoke(["--preset", "ghost"], project_root)
    assert result.exit_code == 1
    assert "missing" in result.output.lower()
    rf.assert_not_called()


def test_preset_list_shows_both_layers(project_root):
    # Add a project-only preset so both layers are represented.
    prompts_dir = project_root / "se3" / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "proj-only.md").write_text("project body", encoding="utf-8")

    result, rf = _invoke(["--preset", "list"], project_root)
    assert result.exit_code == 0
    out = result.output
    assert "doc-sync" in out
    assert "proj-only" in out
    assert "builtin" in out
    assert "project" in out
    rf.assert_not_called()


def test_project_preset_overrides_builtin(project_root):
    marker = "PROJECT-LEVEL-DOC-SYNC-OVERRIDE"
    _write_project_doc_sync(project_root, marker + "\n\nbody")

    result, rf = _invoke(["--preset", "doc-sync"], project_root)
    assert result.exit_code == 0
    rf.assert_called_once()
    kwargs = rf.call_args.kwargs
    assert marker in kwargs["task_description"]
    assert kwargs.get("task_type") == "feature"
