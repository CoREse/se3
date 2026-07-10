"""Integration tests for the DocumentationUpdater wiring in commit_handler.

Covers subtask 1.4 cases (a)-(f):
- (a) README has a badge + VERSIONS has history -> both updated and committed
- (b) README without a badge -> badge inserted after the heading and staged
- (c) no VERSIONS.md -> created with a `# Version History` title
- (d) no README.md -> commit still succeeds, VERSIONS.md still written
- (e) DocumentationUpdater raises RuntimeError -> commit still COMPLETED + warning
- (f) version_bumped == False -> docs untouched, DocumentationUpdater not built

These mirror the mocking conventions of tests/engine/steps/test_commit.py:
VersionBumper / subprocess are mocked so no real git or version files are
touched, while the real DocumentationUpdater is allowed to perform its
deterministic file writes against a tmp_path project root (cases a-d), so the
README badge swap and VERSIONS.md insertion can be asserted on disk.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from se3.engine.models import FlowInstance, State, Step, StepStatus, StepType
from se3.engine.steps.commit import commit_handler
from se3.engine.version_bumper import VersionBumper, VersionConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_flow(tmp_path: Path, **kwargs) -> FlowInstance:
    defaults = {
        "flow_id": "docs-flow-001",
        "task_description": "Add a shiny feature",
        "task_type": "feature",
        # project_root == flow.change_path.parent == tmp_path
        "change_path": tmp_path / "se3.yaml",
        "baseline_commit": None,
        # Mirror the real FlowInstance default: a MagicMock(spec=…) otherwise
        # reads is_worktree_mode as a truthy MagicMock and diverts the commit
        # into the worktree de-versioning branch, which skips the version bump
        # and the docs update these tests assert.
        "is_worktree_mode": False,
    }
    defaults.update(kwargs)

    flow = MagicMock(spec=FlowInstance)
    for k, v in defaults.items():
        setattr(flow, k, v)

    state = MagicMock(spec=State)
    # Include SUMMARIZE so the commit step does NOT also generate a template
    # summary — keeps these tests focused on the docs wiring.
    state.selected_steps = kwargs.get("selected_steps", [
        StepType.ANALYZE, StepType.IMPLEMENT, StepType.COMMIT, StepType.SUMMARIZE,
    ])
    state.step_history = []
    state.steps = {}
    flow.state = state
    return flow


def _make_step(inputs: dict | None = None) -> Step:
    step = MagicMock(spec=Step)
    base_inputs = {"suggested_version": "0.2.0", "bump_type": "minor"}
    if inputs:
        base_inputs.update(inputs)
    step.inputs = base_inputs
    step.outputs = {}
    return step


def _version_config(**overrides) -> VersionConfig:
    cfg = MagicMock(spec=VersionConfig)
    cfg.enabled = True
    cfg.include_in_commit_message = True
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _bumper(tmp_path: Path, new_version: str = "0.2.0") -> VersionBumper:
    mock_bumper = MagicMock(spec=VersionBumper)
    mock_bumper.detect_version_file.return_value = tmp_path / "pyproject.toml"
    mock_bumper._use_script_mode = False
    mock_bumper._script_runner = None
    mock_bumper.read_version.return_value = "0.1.0"
    mock_bumper.set_version.return_value = new_version
    return mock_bumper


def _docs_config():
    """A DocsConfig-like stub feeding a deterministic versions_entry template.

    Keeps the integration tests hermetic (no real se3.yaml / git probing) and
    independent of the packaged ``versions_md.md`` content, while still
    exercising the real wiring path: ``load_docs_config(...).to_updater_config()``
    is forwarded verbatim into ``DocumentationUpdater(config=...)``. The badge
    template falls back to the updater's built-in default.
    """
    cfg = MagicMock()
    cfg.to_updater_config.return_value = {
        "versions_entry_template": "## {{version}} - {{date}}\n\n{{changes}}\n",
    }
    return cfg


# ---------------------------------------------------------------------------
# (a) README has badge + VERSIONS has history -> both updated and committed
# ---------------------------------------------------------------------------

class TestReadmeAndVersionsUpdated:
    @patch("se3.config.load_docs_config")
    @patch("se3.engine.steps.commit._read_head_commit", return_value=("abc123", ""))
    @patch("se3.engine.steps.commit.subprocess")
    @patch("se3.engine.steps.commit._has_changes", return_value=True)
    @patch("se3.engine.steps.commit._load_version_config")
    def test_badge_and_versions_updated(
        self, mock_load_cfg, mock_has_changes, mock_subprocess, mock_hash,
        mock_load_docs, tmp_path
    ):
        mock_load_cfg.return_value = _version_config()
        mock_load_docs.return_value = _docs_config()
        mock_subprocess.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        readme = tmp_path / "README.md"
        readme.write_text(
            "# My Project\n\n"
            "![Version](https://img.shields.io/badge/version-0.1.0-blue)\n\n"
            "Some description.\n",
            encoding="utf-8",
        )
        versions = tmp_path / "VERSIONS.md"
        versions.write_text(
            "# Version History\n\n## 0.1.0 - 2026-01-01\n\n- initial release\n",
            encoding="utf-8",
        )

        flow = _make_flow(tmp_path)
        step = _make_step({"versions_changes": ["Add feature A", "Fix bug B"]})

        with patch("se3.engine.steps.commit.VersionBumper", return_value=_bumper(tmp_path)):
            result = commit_handler(step, flow)

        assert result == StepStatus.COMPLETED
        assert step.outputs.get("version") == "0.2.0"
        assert step.outputs.get("version_bumped") is True

        readme_content = readme.read_text(encoding="utf-8")
        assert "version-0.2.0-blue" in readme_content
        assert "version-0.1.0-blue" not in readme_content

        versions_content = versions.read_text(encoding="utf-8")
        assert "## 0.2.0" in versions_content
        assert "- Add feature A" in versions_content
        assert "- Fix bug B" in versions_content
        # Prior history is preserved.
        assert "## 0.1.0 - 2026-01-01" in versions_content
        # New entry comes before the old one.
        assert versions_content.index("## 0.2.0") < versions_content.index("## 0.1.0")


# ---------------------------------------------------------------------------
# (b) README without a badge -> badge inserted after the heading and staged
# ---------------------------------------------------------------------------

class TestReadmeNoBadgeInsertsAfterHeading:
    @patch("se3.config.load_docs_config")
    @patch("se3.engine.steps.commit._read_head_commit", return_value=("abc123", ""))
    @patch("se3.engine.steps.commit.subprocess")
    @patch("se3.engine.steps.commit._has_changes", return_value=True)
    @patch("se3.engine.steps.commit._load_version_config")
    def test_badge_inserted_after_title(
        self, mock_load_cfg, mock_has_changes, mock_subprocess, mock_hash,
        mock_load_docs, tmp_path
    ):
        mock_load_cfg.return_value = _version_config()
        mock_load_docs.return_value = _docs_config()
        mock_subprocess.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        readme = tmp_path / "README.md"
        readme.write_text("# My Project\n\nSome description.\n", encoding="utf-8")

        flow = _make_flow(tmp_path)
        step = _make_step({"suggested_version": "1.0.0", "versions_changes": ["Initial cut"]})

        with patch(
            "se3.engine.steps.commit.VersionBumper",
            return_value=_bumper(tmp_path, new_version="1.0.0"),
        ):
            result = commit_handler(step, flow)

        assert result == StepStatus.COMPLETED

        content = readme.read_text(encoding="utf-8")
        assert "version-1.0.0-blue" in content
        # Heading preserved and the new badge lands after it.
        assert content.index("# My Project") < content.index("version-1.0.0-blue")

        # README.md was explicitly staged (in addition to the final git add -A).
        staged_calls = [
            c.args[0] for c in mock_subprocess.run.call_args_list
            if c.args and isinstance(c.args[0], list)
        ]
        assert ["git", "add", "README.md"] in staged_calls
        assert ["git", "add", "VERSIONS.md"] in staged_calls


# ---------------------------------------------------------------------------
# (c) no VERSIONS.md -> created with a `# Version History` title
# ---------------------------------------------------------------------------

class TestVersionsCreatedWhenMissing:
    @patch("se3.config.load_docs_config")
    @patch("se3.engine.steps.commit._read_head_commit", return_value=("abc123", ""))
    @patch("se3.engine.steps.commit.subprocess")
    @patch("se3.engine.steps.commit._has_changes", return_value=True)
    @patch("se3.engine.steps.commit._load_version_config")
    def test_versions_md_created_with_title(
        self, mock_load_cfg, mock_has_changes, mock_subprocess, mock_hash,
        mock_load_docs, tmp_path
    ):
        mock_load_cfg.return_value = _version_config()
        mock_load_docs.return_value = _docs_config()
        mock_subprocess.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        readme = tmp_path / "README.md"
        readme.write_text(
            "# My Project\n\n"
            "![Version](https://img.shields.io/badge/version-0.1.0-blue)\n",
            encoding="utf-8",
        )
        versions = tmp_path / "VERSIONS.md"
        assert not versions.exists()

        flow = _make_flow(tmp_path)
        step = _make_step({"versions_changes": ["Add feature A"]})

        with patch("se3.engine.steps.commit.VersionBumper", return_value=_bumper(tmp_path)):
            result = commit_handler(step, flow)

        assert result == StepStatus.COMPLETED
        assert versions.exists()
        content = versions.read_text(encoding="utf-8")
        assert content.startswith("# Version History")
        assert "## 0.2.0" in content
        assert "- Add feature A" in content


# ---------------------------------------------------------------------------
# (d) no README.md -> commit still succeeds, VERSIONS.md still written
# ---------------------------------------------------------------------------

class TestNoReadmeStillCommits:
    @patch("se3.config.load_docs_config")
    @patch("se3.engine.steps.commit._read_head_commit", return_value=("abc123", ""))
    @patch("se3.engine.steps.commit.subprocess")
    @patch("se3.engine.steps.commit._has_changes", return_value=True)
    @patch("se3.engine.steps.commit._load_version_config")
    def test_missing_readme_does_not_block_commit(
        self, mock_load_cfg, mock_has_changes, mock_subprocess, mock_hash,
        mock_load_docs, tmp_path
    ):
        mock_load_cfg.return_value = _version_config()
        mock_load_docs.return_value = _docs_config()
        mock_subprocess.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        readme = tmp_path / "README.md"
        assert not readme.exists()
        versions = tmp_path / "VERSIONS.md"
        assert not versions.exists()

        flow = _make_flow(tmp_path)
        step = _make_step({"versions_changes": ["Add feature A"]})

        with patch("se3.engine.steps.commit.VersionBumper", return_value=_bumper(tmp_path)):
            result = commit_handler(step, flow)

        assert result == StepStatus.COMPLETED
        # README is never fabricated.
        assert not readme.exists()
        # VERSIONS.md is still created and written.
        assert versions.exists()
        content = versions.read_text(encoding="utf-8")
        assert "## 0.2.0" in content
        assert "- Add feature A" in content


# ---------------------------------------------------------------------------
# (e) DocumentationUpdater raises RuntimeError -> commit still COMPLETED + warn
# ---------------------------------------------------------------------------

class TestDocsUpdateFailureDoesNotBlockCommit:
    @patch("se3.config.load_docs_config")
    @patch("se3.engine.docs_updater.DocumentationUpdater")
    @patch("se3.engine.steps.commit._read_head_commit", return_value=("abc123", ""))
    @patch("se3.engine.steps.commit.subprocess")
    @patch("se3.engine.steps.commit._has_changes", return_value=True)
    @patch("se3.engine.steps.commit._load_version_config")
    def test_runtime_error_logs_warning_and_completes(
        self, mock_load_cfg, mock_has_changes, mock_subprocess, mock_hash,
        mock_updater_cls, mock_load_docs, tmp_path, caplog
    ):
        mock_load_cfg.return_value = _version_config()
        mock_load_docs.return_value = _docs_config()
        mock_subprocess.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        # DocumentationUpdater(...).update_both(...) blows up.
        mock_updater = MagicMock()
        mock_updater.update_both.side_effect = RuntimeError("docs boom")
        mock_updater_cls.return_value = mock_updater

        readme = tmp_path / "README.md"
        original_readme = (
            "# My Project\n\n"
            "![Version](https://img.shields.io/badge/version-0.1.0-blue)\n"
        )
        readme.write_text(original_readme, encoding="utf-8")

        flow = _make_flow(tmp_path)
        step = _make_step({"versions_changes": ["Add feature A"]})

        with caplog.at_level(logging.WARNING, logger="se3.engine.steps.commit"):
            with patch("se3.engine.steps.commit.VersionBumper", return_value=_bumper(tmp_path)):
                result = commit_handler(step, flow)

        assert result == StepStatus.COMPLETED
        assert "Documentation auto-update failed" in caplog.text
        # The failing updater left the README untouched (no partial write).
        assert readme.read_text(encoding="utf-8") == original_readme


# ---------------------------------------------------------------------------
# (f) version_bumped == False -> docs untouched, DocumentationUpdater not built
# ---------------------------------------------------------------------------

class TestNoBumpSkipsDocs:
    @patch("se3.config.load_docs_config")
    @patch("se3.engine.docs_updater.DocumentationUpdater")
    @patch("se3.engine.steps.commit._read_head_commit", return_value=("abc123", ""))
    @patch("se3.engine.steps.commit.subprocess")
    @patch("se3.engine.steps.commit._has_changes", return_value=True)
    @patch("se3.engine.steps.commit._load_version_config")
    def test_docs_not_touched_when_no_bump(
        self, mock_load_cfg, mock_has_changes, mock_subprocess, mock_hash,
        mock_updater_cls, mock_load_docs, tmp_path
    ):
        # Version bumping disabled -> version_bumped stays False, new_version None.
        mock_load_cfg.return_value = _version_config(enabled=False)
        mock_load_docs.return_value = _docs_config()
        mock_subprocess.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        readme = tmp_path / "README.md"
        original_readme = (
            "# My Project\n\n"
            "![Version](https://img.shields.io/badge/version-0.1.0-blue)\n"
        )
        readme.write_text(original_readme, encoding="utf-8")

        flow = _make_flow(tmp_path)
        step = _make_step({"versions_changes": ["Add feature A"]})

        result = commit_handler(step, flow)

        assert result == StepStatus.COMPLETED
        # DocumentationUpdater was never instantiated.
        mock_updater_cls.assert_not_called()
        mock_load_docs.assert_not_called()
        # README is unchanged.
        assert readme.read_text(encoding="utf-8") == original_readme
        assert step.outputs.get("version_bumped") is None
