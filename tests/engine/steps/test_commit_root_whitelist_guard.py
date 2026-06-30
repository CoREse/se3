"""Tests for the commit-time root-whitelist exclusion guard.

Covers ``_detect_root_whitelist_exclusions`` (and its ``_root_deny_excludes``
helper) in ``se3.engine.steps.commit``: the soft, diagnostic-only backstop that
warns when a new top-level path is silently excluded by the root ``/*``
default-deny gitignore rule. The guard must only告警, never touch .gitignore or
staging, and never raise / block / fail a commit.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from se3.engine.models import FlowInstance, State, Step, StepStatus, StepType
from se3.engine.steps.commit import (
    _detect_root_whitelist_exclusions,
    _root_deny_excludes,
    commit_handler,
)
from se3.engine.version_bumper import VersionConfig


# --- Repo / fixture helpers -------------------------------------------------

def _init_git_repo(tmp_path: Path, gitignore: str) -> Path:
    """Init a repo whose committed .gitignore uses the root-whitelist form."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True,
    )
    (tmp_path / ".gitignore").write_text(gitignore)
    (tmp_path / "README.md").write_text("init\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "init"], check=True,
    )
    return tmp_path


# A minimal root default-deny + whitelist .gitignore: everything at root is
# denied except the handful of explicitly re-admitted top-level entries.
_ROOT_DENY = "/*\n!/.gitignore\n!/README.md\n!/src.py\n!/se3/\n"


def _head_tree_files(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "show", "--name-only", "--pretty=format:", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout


def _make_step() -> Step:
    step = MagicMock(spec=Step)
    step.inputs = {"suggested_version": "0.1.1", "bump_type": "patch"}
    step.outputs = {}
    return step


def _make_flow(repo: Path) -> FlowInstance:
    flow = MagicMock(spec=FlowInstance)
    flow.flow_id = "test-flow"
    flow.task_description = "change"
    flow.task_type = "feature"
    flow.change_path = repo / "se3.yaml"
    flow.baseline_commit = None
    state = MagicMock(spec=State)
    state.selected_steps = [StepType.COMMIT, StepType.SUMMARIZE]
    state.step_history = []
    state.steps = {}
    flow.state = state
    return flow


def _disabled_version_config() -> VersionConfig:
    cfg = MagicMock(spec=VersionConfig)
    cfg.enabled = False
    cfg.include_in_commit_message = False
    return cfg


# --- _detect_root_whitelist_exclusions: detection --------------------------

class TestDetectRootWhitelistExclusions:
    def test_top_level_file_excluded_by_root_deny_is_reported(
        self, tmp_path: Path, caplog
    ) -> None:
        repo = _init_git_repo(tmp_path, _ROOT_DENY)
        # A new top-level file with no `!/<name>` whitelist line — denied by `/*`.
        (repo / "stray").write_text("scratch\n")

        with caplog.at_level(logging.WARNING):
            result = _detect_root_whitelist_exclusions(repo)

        assert result == ["stray"]
        assert any(
            "Root-whitelist guard" in r.message and "stray" in r.message
            for r in caplog.records
        )

    def test_top_level_dir_excluded_by_root_deny_is_reported(
        self, tmp_path: Path
    ) -> None:
        repo = _init_git_repo(tmp_path, _ROOT_DENY)
        (repo / "junk").mkdir()
        (repo / "junk" / "a.txt").write_text("x\n")

        # --directory collapses the whole ignored top-level dir to one entry.
        assert _detect_root_whitelist_exclusions(repo) == ["junk"]

    def test_whitelisted_top_level_path_not_reported(self, tmp_path: Path) -> None:
        repo = _init_git_repo(tmp_path, _ROOT_DENY)
        # `src.py` is explicitly whitelisted — not ignored, so not reported.
        (repo / "src.py").write_text("print('x')\n")

        assert _detect_root_whitelist_exclusions(repo) == []

    def test_no_new_top_level_paths_returns_empty_silently(
        self, tmp_path: Path, caplog
    ) -> None:
        repo = _init_git_repo(tmp_path, _ROOT_DENY)

        with caplog.at_level(logging.WARNING):
            result = _detect_root_whitelist_exclusions(repo)

        assert result == []
        assert not [r for r in caplog.records if "Root-whitelist guard" in r.message]

    def test_path_ignored_by_non_root_rule_is_not_reported(
        self, tmp_path: Path
    ) -> None:
        # `*.log` follows `/*`, so it is the higher-precedence (last) match for
        # a .log file. The guard targets ONLY paths whose winning rule is `/*`,
        # so this ordinary, expected ignore must be left out.
        repo = _init_git_repo(tmp_path, _ROOT_DENY + "*.log\n")
        (repo / "debug.log").write_text("noise\n")
        (repo / "stray").write_text("x\n")

        assert _detect_root_whitelist_exclusions(repo) == ["stray"]

    def test_nested_path_under_whitelisted_dir_not_reported(
        self, tmp_path: Path
    ) -> None:
        repo = _init_git_repo(tmp_path, _ROOT_DENY)
        # `!/se3/` re-admits se3/; its interior follows normal rules and is not
        # a top-level exclusion the guard should surface.
        (repo / "se3").mkdir()
        (repo / "se3" / "note.md").write_text("# x\n")

        assert _detect_root_whitelist_exclusions(repo) == []


# --- _root_deny_excludes: rule discrimination ------------------------------

class TestRootDenyExcludes:
    def test_true_for_root_deny_match(self, tmp_path: Path) -> None:
        repo = _init_git_repo(tmp_path, _ROOT_DENY)
        (repo / "stray").write_text("x\n")
        assert _root_deny_excludes(repo, "stray") is True

    def test_false_for_other_rule_match(self, tmp_path: Path) -> None:
        repo = _init_git_repo(tmp_path, _ROOT_DENY + "*.log\n")
        (repo / "debug.log").write_text("x\n")
        assert _root_deny_excludes(repo, "debug.log") is False

    def test_false_for_non_ignored_path(self, tmp_path: Path) -> None:
        repo = _init_git_repo(tmp_path, _ROOT_DENY)
        (repo / "src.py").write_text("x\n")
        assert _root_deny_excludes(repo, "src.py") is False

    def test_subprocess_raise_is_swallowed(self, tmp_path: Path) -> None:
        repo = _init_git_repo(tmp_path, _ROOT_DENY)
        with patch(
            "se3.engine.steps.commit.subprocess.run",
            side_effect=OSError("boom"),
        ):
            assert _root_deny_excludes(repo, "stray") is False


# --- Fault tolerance: the guard never raises -------------------------------

class TestGuardFaultTolerance:
    def test_ls_files_nonzero_returns_empty(self, tmp_path: Path, caplog) -> None:
        repo = _init_git_repo(tmp_path, _ROOT_DENY)
        with patch(
            "se3.engine.steps.commit.subprocess.run",
            return_value=MagicMock(returncode=1, stdout="", stderr="fail"),
        ), caplog.at_level(logging.WARNING):
            result = _detect_root_whitelist_exclusions(repo)
        assert result == []
        assert any("could not list ignored paths" in r.message for r in caplog.records)

    def test_ls_files_raise_returns_empty(self, tmp_path: Path, caplog) -> None:
        repo = _init_git_repo(tmp_path, _ROOT_DENY)
        with patch(
            "se3.engine.steps.commit.subprocess.run",
            side_effect=OSError("boom"),
        ), caplog.at_level(logging.WARNING):
            result = _detect_root_whitelist_exclusions(repo)
        assert result == []
        assert any("listing ignored paths raised" in r.message for r in caplog.records)

    def test_check_ignore_failure_does_not_break_detection(
        self, tmp_path: Path
    ) -> None:
        repo = _init_git_repo(tmp_path, _ROOT_DENY)
        (repo / "stray").write_text("x\n")
        real_run = subprocess.run

        def flaky(cmd, *args, **kwargs):
            # Let the ls-files enumeration succeed but make every check-ignore
            # confirmation raise — the helper must degrade to "not root-deny".
            if "check-ignore" in cmd:
                raise OSError("boom")
            return real_run(cmd, *args, **kwargs)

        with patch("se3.engine.steps.commit.subprocess.run", side_effect=flaky):
            # No exception; check-ignore failures simply yield no confirmed hits.
            assert _detect_root_whitelist_exclusions(repo) == []


# --- commit_handler integration --------------------------------------------

class TestCommitHandlerIntegration:
    def _run_commit(self, repo: Path) -> StepStatus:
        flow = _make_flow(repo)
        with patch(
            "se3.engine.steps.commit._load_version_config",
            return_value=_disabled_version_config(),
        ), patch(
            "se3.engine.steps.commit._generate_commit_message",
            return_value="feature: change",
        ), patch(
            "se3.engine.context_builder.ensure_code_index_fresh",
        ):
            return commit_handler(_make_step(), flow)

    def test_warns_on_root_exclusion_and_commit_completes(
        self, tmp_path: Path, caplog
    ) -> None:
        repo = _init_git_repo(tmp_path, _ROOT_DENY)
        # A legit whitelisted change plus a stray top-level path denied by `/*`.
        (repo / "src.py").write_text("print('x')\n")
        (repo / "stray").write_text("scratch\n")

        with caplog.at_level(logging.WARNING):
            result = self._run_commit(repo)

        assert result == StepStatus.COMPLETED
        tree = _head_tree_files(repo)
        assert "src.py" in tree
        # The guard only warns — it never stages/whitelists the stray path.
        assert "stray" not in tree
        assert (repo / "stray").exists()
        assert any(
            "Root-whitelist guard" in r.message and "stray" in r.message
            for r in caplog.records
        )

    def test_no_root_exclusion_commits_silently(
        self, tmp_path: Path, caplog
    ) -> None:
        repo = _init_git_repo(tmp_path, _ROOT_DENY)
        (repo / "src.py").write_text("print('ok')\n")

        with caplog.at_level(logging.WARNING):
            result = self._run_commit(repo)

        assert result == StepStatus.COMPLETED
        assert "src.py" in _head_tree_files(repo)
        assert not [
            r for r in caplog.records if "Root-whitelist guard" in r.message
        ]

    def test_detector_return_value_does_not_feed_control_flow(
        self, tmp_path: Path
    ) -> None:
        # Task-2 contract: the call site uses the detector for告警 only — its
        # return value (even a non-empty hit list) must NOT alter control flow.
        repo = _init_git_repo(tmp_path, _ROOT_DENY)
        (repo / "src.py").write_text("print('x')\n")

        flow = _make_flow(repo)
        with patch(
            "se3.engine.steps.commit._load_version_config",
            return_value=_disabled_version_config(),
        ), patch(
            "se3.engine.steps.commit._generate_commit_message",
            return_value="feature: change",
        ), patch(
            "se3.engine.context_builder.ensure_code_index_fresh",
        ), patch(
            "se3.engine.steps.commit._detect_root_whitelist_exclusions",
            return_value=["phantom-a", "phantom-b"],
        ) as mock_detect:
            result = commit_handler(_make_step(), flow)

        # Called exactly once on the canonical path; commit proceeds regardless.
        mock_detect.assert_called_once()
        assert result == StepStatus.COMPLETED
        assert "src.py" in _head_tree_files(repo)
