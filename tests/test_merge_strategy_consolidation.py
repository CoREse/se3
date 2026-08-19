"""Regression tests for merge strategy consolidation (G7 task 2).

Covers tasks (iv), (v), (viii), and (ix) from the design doc:

* (iv)  ``safe`` mode that exhausts ``max_conflict_resolve_iterations``
        escalates to a human MCP call.
* (v)   ``strict`` mode hands off every conflicting file directly to
        a human MCP call without invoking the LLM.
* (viii) Omitting ``--strategy`` on the CLI surface defaults to ``fast``.
* (ix)  ``--strategy=robust`` and ``--strategy=default`` are rejected
        fail-fast (``typer.BadParameter`` / non-zero exit).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tianluo.engine.merge.conflict_context import ConflictFile
from tianluo.engine.merge.conflict_resolver import (
    BatchContext,
    ConflictResolver,
    MergeStrategy,
)
from tianluo.engine.merge.strategy import DecisionAction, StrategyDecider


# --------- helpers shared with task 1 ---------


def _git(path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    _git(path, "config", "user.email", "test@test.com")
    _git(path, "config", "user.name", "Test")


def _write_with_markers(path: Path, ours: str, theirs: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "<<<<<<< HEAD\n"
        f"{ours}"
        "=======\n"
        f"{theirs}"
        ">>>>>>> theirs-branch\n"
    )


def _make_conflict_file(rel_path: str, working_content: str) -> ConflictFile:
    return ConflictFile(
        path=rel_path,
        base_content="",
        ours_content="",
        theirs_content="",
        working_content=working_content,
        base_exists=True,
        ours_exists=True,
        theirs_exists=True,
        is_binary=False,
    )


def _make_batch_context(tmp_path: Path, strategy: MergeStrategy) -> BatchContext:
    return BatchContext(
        project_root=tmp_path,
        ours_branch="ours",
        theirs_branch="theirs-branch",
        merge_base="deadbeef",
        ours_head_sha="aaaa",
        theirs_head_sha="bbbb",
        ours_head_message="ours change",
        theirs_head_message="theirs change",
        ours_log_oneline=[],
        theirs_log_oneline=[],
        strategy=strategy,
    )


# ---------------------------------------------------------------------
# Task (iv): safe mode exhaustion → human call surface
# ---------------------------------------------------------------------


def test_safe_exhausts_iterations_escalates_to_human(
    tmp_path: Path, monkeypatch,
) -> None:
    target = tmp_path / "doomed.txt"
    _write_with_markers(target, "ours\n", "theirs\n")

    # LLM never clears the markers.
    monkeypatch.setattr(
        ConflictResolver,
        "_call_llm",
        lambda self, prompt: "I could not fix this",
    )

    resolver = ConflictResolver(tmp_path)
    ctx = _make_batch_context(tmp_path, MergeStrategy.SAFE)
    cf = _make_conflict_file("doomed.txt", target.read_text())

    decider = StrategyDecider()
    decision = decider.resolve_and_decide(
        resolver, [cf], ctx, max_iterations=3,
    )

    assert decision.action == DecisionAction.HUMAN_CALL
    assert decision.outcome is not None
    assert decision.outcome.escalation_reason == "safe_to_human"
    assert decision.outcome.iterations_used == 3
    # Markers are preserved on disk so the human reviewer sees them.
    assert "<<<<<<<" in target.read_text()


# ---------------------------------------------------------------------
# Task (v): strict mode skips LLM entirely
# ---------------------------------------------------------------------


def test_strict_skips_llm_and_routes_to_human(
    tmp_path: Path, monkeypatch,
) -> None:
    target = tmp_path / "strictly.txt"
    _write_with_markers(target, "ours\n", "theirs\n")

    invoked = []

    def stub_llm(self, prompt):
        invoked.append(prompt)
        return ""

    monkeypatch.setattr(ConflictResolver, "_call_llm", stub_llm)

    resolver = ConflictResolver(tmp_path)
    ctx = _make_batch_context(tmp_path, MergeStrategy.STRICT)
    cf = _make_conflict_file("strictly.txt", target.read_text())

    decider = StrategyDecider()
    decision = decider.resolve_and_decide(
        resolver, [cf], ctx, max_iterations=10,
    )

    assert decision.action == DecisionAction.HUMAN_CALL
    assert decision.outcome is not None
    assert decision.outcome.escalation_reason == "strict_to_human"
    assert decision.outcome.iterations_used == 0
    # LLM was never called.
    assert invoked == []


# ---------------------------------------------------------------------
# Task (viii): default strategy is fast
# ---------------------------------------------------------------------


def test_run_merge_default_strategy_is_fast(monkeypatch) -> None:
    """``run_merge`` signature defaults to strategy='fast'."""
    import inspect

    from tianluo.commands.merge_cmd import run_merge

    sig = inspect.signature(run_merge)
    assert sig.parameters["strategy"].default == "fast"


def test_cli_merge_defaults_to_fast(tmp_path: Path, monkeypatch) -> None:
    """Invoking ``se3 merge feature`` without ``--strategy`` resolves
    to ``strategy='fast'`` in the call to ``run_merge``.
    """
    _init_repo(tmp_path)
    # commit so cli detects a valid branch
    (tmp_path / "file.txt").write_text("x")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "."],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "init"],
        check=True, capture_output=True,
    )

    captured: dict[str, object] = {}

    def stub_run_merge(
        branches, strategy="fast", delete_merged=True,
        strict_runtime_sync=False, project_root=None,
        suppress_human_call=False,
    ):
        captured["strategy"] = strategy
        captured["delete_merged"] = delete_merged
        return 0

    monkeypatch.setattr("tianluo.commands.merge_cmd.run_merge", stub_run_merge)

    import os
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        from typer.testing import CliRunner
        from tianluo.cli import app

        runner = CliRunner()
        result = runner.invoke(app, ["merge", "feature"])
        assert result.exit_code == 0, result.output
        assert captured.get("strategy") == "fast"
    finally:
        os.chdir(old_cwd)


# ---------------------------------------------------------------------
# Task (ix): legacy strategy names are rejected fail-fast
# ---------------------------------------------------------------------


def test_legacy_robust_strategy_name_rejected() -> None:
    from tianluo.engine.merge.conflict_resolver import MergeStrategy

    with pytest.raises(ValueError) as exc:
        MergeStrategy.from_str("robust")
    assert "robust" in str(exc.value)
    assert "fast" in str(exc.value).lower()


def test_legacy_default_strategy_name_rejected() -> None:
    from tianluo.engine.merge.conflict_resolver import MergeStrategy

    with pytest.raises(ValueError) as exc:
        MergeStrategy.from_str("default")
    assert "default" in str(exc.value)
    assert "safe" in str(exc.value).lower()


def test_cli_rejects_robust_strategy(tmp_path: Path, monkeypatch) -> None:
    _init_repo(tmp_path)
    (tmp_path / "f.txt").write_text("x")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "."],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "init"],
        check=True, capture_output=True,
    )

    def should_not_be_called(*args, **kwargs):
        raise AssertionError("run_merge must not be invoked for legacy strategy")

    monkeypatch.setattr("tianluo.commands.merge_cmd.run_merge", should_not_be_called)

    import os
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        from typer.testing import CliRunner
        from tianluo.cli import app

        runner = CliRunner()
        result = runner.invoke(app, ["merge", "feature", "--strategy", "robust"])
        assert result.exit_code != 0
        assert "robust" in result.output.lower() or "fast" in result.output.lower()
    finally:
        os.chdir(old_cwd)


def test_cli_rejects_default_strategy(tmp_path: Path, monkeypatch) -> None:
    _init_repo(tmp_path)
    (tmp_path / "f.txt").write_text("x")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "."],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "init"],
        check=True, capture_output=True,
    )

    def should_not_be_called(*args, **kwargs):
        raise AssertionError("run_merge must not be invoked for legacy strategy")

    monkeypatch.setattr("tianluo.commands.merge_cmd.run_merge", should_not_be_called)

    import os
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        from typer.testing import CliRunner
        from tianluo.cli import app

        runner = CliRunner()
        result = runner.invoke(app, ["merge", "feature", "--strategy", "default"])
        assert result.exit_code != 0
        assert "default" in result.output.lower() or "safe" in result.output.lower()
    finally:
        os.chdir(old_cwd)
