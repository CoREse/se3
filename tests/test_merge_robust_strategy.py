"""Tests for the robust merge strategy.

Commit 1 (this file's initial scope): verifies that ``robust`` is accepted by
the type system, CLI plumbing, and MergeConfig default. Behavior is still
delegated to default-equivalent semantics; commits 2-4 add the behavioral
differentiation (auto-stash, take-theirs fallback, guardrail-as-issue).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from se3.engine.merge.conflict_resolver import (
    Confidence,
    FileResolution,
    HunkResolution,
    LLMResolution,
    MergeStrategy,
)
from se3.engine.merge.strategy import DecisionAction, StrategyDecider


def _init_repo(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=tmp_path, check=True,
    )
    (tmp_path / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True,
    )


def _make_resolution(
    overall: Confidence = Confidence.HIGH,
    requires_human_review: bool = False,
) -> LLMResolution:
    return LLMResolution(
        files=[
            FileResolution(
                path="a.txt",
                resolved_content="resolved\n",
                hunks=[
                    HunkResolution(
                        start_line=1,
                        end_line=1,
                        confidence=Confidence.HIGH,
                        reasoning="r",
                    ),
                ],
                overall_confidence=overall,
                flags={
                    "requires_human_review": requires_human_review,
                    "spec_guardrail_concern": False,
                },
                is_spec=False,
            ),
        ],
        overall_confidence=overall,
        flags={
            "requires_human_review": requires_human_review,
            "spec_guardrail_concern": False,
        },
    )


class TestRobustEnumWired:
    def test_robust_is_in_merge_strategy_enum(self) -> None:
        assert MergeStrategy("robust") is MergeStrategy.ROBUST
        assert MergeStrategy.ROBUST.value == "robust"

    def test_orchestrator_accepts_robust(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        from se3.engine.merge.orchestrator import MergeOrchestrator

        orch = MergeOrchestrator(
            tmp_path, strategy="robust", acquire_lock=False,
        )
        assert orch.strategy is MergeStrategy.ROBUST

    def test_orchestrator_rejects_unknown_strategy(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        from se3.engine.merge.orchestrator import MergeOrchestrator

        with pytest.raises(ValueError, match="Unknown merge strategy"):
            MergeOrchestrator(
                tmp_path, strategy="nonsense", acquire_lock=False,
            )


class TestRobustIsDefaultConfig:
    def test_default_merge_config_strategy_is_robust(self) -> None:
        from se3.config import MergeConfig

        assert MergeConfig().strategy == "robust"

    def test_config_loads_robust_from_yaml(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        (tmp_path / "se3.yaml").write_text(
            "merge:\n  strategy: robust\n"
        )
        from se3.config import MergeConfig

        cfg = MergeConfig.load(tmp_path)
        assert cfg.strategy == "robust"

    def test_run_merge_default_strategy_arg_is_robust(self) -> None:
        # Static guard on the default to catch accidental regressions in
        # the CLI plumbing when refactoring run_merge.
        import inspect

        from se3.commands.merge_cmd import run_merge

        sig = inspect.signature(run_merge)
        assert sig.parameters["strategy"].default == "robust"


class TestRobustDecideDelegatesToDefault:
    """Commit 1 keeps robust as a behavioral alias of default. Commits 3
    will change orchestrator-side handling of HUMAN_CALL under robust;
    the decider keeps returning HUMAN_CALL the same way default does, but
    the orchestrator will interpret it differently."""

    def test_high_confidence_clean_accepts_under_robust(self) -> None:
        decider = StrategyDecider()
        resolution = _make_resolution(overall=Confidence.HIGH)
        decision = decider.decide(
            resolution, has_spec_files=False, strategy=MergeStrategy.ROBUST,
        )
        assert decision.action is DecisionAction.ACCEPT

    def test_human_review_flag_returns_human_call_under_robust(self) -> None:
        decider = StrategyDecider()
        resolution = _make_resolution(requires_human_review=True)
        decision = decider.decide(
            resolution, has_spec_files=False, strategy=MergeStrategy.ROBUST,
        )
        # Commit 1: same decision as default (HUMAN_CALL). Commit 3 will
        # change orchestrator-side handling, not the decider itself.
        assert decision.action is DecisionAction.HUMAN_CALL

    def test_low_confidence_returns_human_call_under_robust(self) -> None:
        decider = StrategyDecider()
        resolution = _make_resolution(overall=Confidence.LOW)
        decision = decider.decide(
            resolution, has_spec_files=False, strategy=MergeStrategy.ROBUST,
        )
        assert decision.action is DecisionAction.HUMAN_CALL


class TestCLIRobustAccepted:
    def test_cli_help_lists_robust(self) -> None:
        from typer.testing import CliRunner

        from se3.cli import app

        runner = CliRunner()
        result = runner.invoke(app, ["merge", "--help"])
        assert result.exit_code == 0
        assert "robust" in result.stdout

    def test_cli_rejects_invalid_strategy(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        from typer.testing import CliRunner

        from se3.cli import app

        runner = CliRunner()
        # Mock get_project_root so we don't escape tmp_path
        with patch(
            "se3.commands.run.get_project_root", return_value=tmp_path,
        ):
            result = runner.invoke(
                app, ["merge", "feat", "--strategy", "nonsense"],
            )
        assert result.exit_code != 0
        # Error message lists robust as a valid option
        out = result.stdout + (result.stderr or "")
        assert "robust" in out
