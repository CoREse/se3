"""Tests for ConflictResolver, StrategyDecider, and HumanCallWriter."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tianluo.engine.merge.conflict_context import (
    ConflictContext,
    ConflictFile,
    ConflictHunk,
    build,
)
from tianluo.engine.merge.conflict_resolver import (
    Confidence,
    ConflictResolver,
    FileResolution,
    HunkResolution,
    LLMResolution,
    MergeStrategy,
)
from tianluo.engine.merge.human_call import HumanCallWriter
from tianluo.engine.merge.strategy import DecisionAction, StrategyDecider, StrategyDecision


# --------- helpers ---------


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


def _commit(path: Path, message: str) -> None:
    _git(path, "add", "-A")
    _git(path, "commit", "-m", message)


def _current_branch(path: Path) -> str:
    return _git(path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def _setup_conflict(
    tmp_path: Path,
    rel_path: str = "shared.txt",
    base_content: str = "line1\nline2\nline3\n",
    ours_content: str = "line1\nOURS\nline3\n",
    theirs_content: str = "line1\nTHEIRS\nline3\n",
) -> tuple[str, str]:
    """Create a repo with a single-file conflict. Returns (ours_branch, theirs_branch)."""
    _init_repo(tmp_path)
    (tmp_path / rel_path).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / rel_path).write_text(base_content)
    _commit(tmp_path, "base")
    default = _current_branch(tmp_path)

    _git(tmp_path, "checkout", "-b", "theirs-branch")
    (tmp_path / rel_path).write_text(theirs_content)
    _commit(tmp_path, "theirs change")

    _git(tmp_path, "checkout", default)
    (tmp_path / rel_path).write_text(ours_content)
    _commit(tmp_path, "ours change")

    result = _git(tmp_path, "merge", "theirs-branch", "--no-edit", check=False)
    assert result.returncode != 0, "expected merge to conflict"

    return default, "theirs-branch"


def _make_resolution(
    path: str = "shared.txt",
    resolved_content: str = "resolved\n",
    overall_confidence: Confidence = Confidence.HIGH,
    hunk_confidence: Confidence = Confidence.HIGH,
    requires_human_review: bool = False,
) -> LLMResolution:
    """Build a mock LLMResolution."""
    return LLMResolution(
        files=[
            FileResolution(
                path=path,
                resolved_content=resolved_content,
                hunks=[
                    HunkResolution(
                        start_line=1,
                        end_line=5,
                        confidence=hunk_confidence,
                        reasoning="test reasoning",
                    ),
                ],
                overall_confidence=overall_confidence,
                flags={"requires_human_review": requires_human_review},
            ),
        ],
        overall_confidence=overall_confidence,
        flags={"requires_human_review": requires_human_review},
    )


# --------- ConflictResolver unit tests ---------


# Legacy ``TestConflictResolverPrompt`` and ``TestConflictResolverParse``
# classes have been removed.  Both exercised the deprecated JSON-decision
# pipeline (``_build_prompt`` / ``_parse_response`` /
# ``_build_resolution_from_json`` / ``_fallback_resolution`` /
# ``_RESOLUTION_SCHEMA``) which is no longer present in the resolver —
# production conflict resolution now flows through ``resolve_batch``
# with the LLM editing files directly on disk.  See
# ``TestMergeOrchestratorBatchResolverIntegration`` in
# ``test_merge_orchestrator.py`` for the new end-to-end coverage.


# --------- StrategyDecider unit tests ---------


class TestStrategyDeciderDefault:
    def test_default_high_confidence_no_flags_accept(self) -> None:
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.HIGH,
            hunk_confidence=Confidence.HIGH,
        )
        decision = decider.decide(resolution, strategy=MergeStrategy.SAFE)
        assert decision.action == DecisionAction.ACCEPT

    def test_default_low_overall_confidence_accepted_without_flags(self) -> None:
        """Under the LLM-as-editor model, confidence is informational.

        Safe strategy only gates on the explicit
        ``requires_human_review`` flag.  A resolution with LOW
        confidence but no flags is accepted because
        the LLM cleared every conflict marker on disk — which is the
        only real success signal.
        """
        decider = StrategyDecider()
        resolution = _make_resolution(overall_confidence=Confidence.LOW)
        decision = decider.decide(resolution, strategy=MergeStrategy.SAFE)
        assert decision.action == DecisionAction.ACCEPT

    def test_default_requires_human_review_flag_human_call(self) -> None:
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.HIGH,
            requires_human_review=True,
        )
        decision = decider.decide(resolution, strategy=MergeStrategy.SAFE)
        assert decision.action == DecisionAction.HUMAN_CALL
        assert "requires_human_review" in decision.reason

    def test_default_per_file_flag_human_call(self) -> None:
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.HIGH,
            requires_human_review=False,
        )
        # Add per-file flag
        resolution.files[0].flags["requires_human_review"] = True
        decision = decider.decide(resolution, strategy=MergeStrategy.SAFE)
        assert decision.action == DecisionAction.HUMAN_CALL

    def test_default_medium_hunk_high_overall_accept(self) -> None:
        """Default strategy accepts when hunks are MEDIUM but file+global overall is HIGH."""
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.HIGH,
            hunk_confidence=Confidence.MEDIUM,
        )
        decision = decider.decide(resolution, strategy=MergeStrategy.SAFE)
        assert decision.action == DecisionAction.ACCEPT

    def test_default_low_file_overall_accepted_without_flags(self) -> None:
        """Safe strategy ignores file-level confidence under the new model.

        Confidence is informational — the on-disk marker scan is the
        success signal.  A file-level LOW rating without a
        ``requires_human_review`` flag is still accepted.
        """
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.HIGH,
            hunk_confidence=Confidence.HIGH,
        )
        resolution.files[0].overall_confidence = Confidence.LOW
        decision = decider.decide(resolution, strategy=MergeStrategy.SAFE)
        assert decision.action == DecisionAction.ACCEPT


class TestStrategyDeciderStrict:
    def test_strict_all_high_accept(self) -> None:
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.HIGH,
            hunk_confidence=Confidence.HIGH,
        )
        decision = decider.decide(resolution, strategy=MergeStrategy.STRICT)
        assert decision.action == DecisionAction.ACCEPT

    def test_strict_low_hunk_human_call(self) -> None:
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.HIGH,
            hunk_confidence=Confidence.LOW,
        )
        decision = decider.decide(resolution, strategy=MergeStrategy.STRICT)
        assert decision.action == DecisionAction.HUMAN_CALL
        assert "hunk" in decision.reason.lower() or "confidence" in decision.reason.lower()

    def test_strict_low_file_overall_human_call(self) -> None:
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.HIGH,
            hunk_confidence=Confidence.HIGH,
        )
        resolution.files[0].overall_confidence = Confidence.MEDIUM
        decision = decider.decide(resolution, strategy=MergeStrategy.STRICT)
        assert decision.action == DecisionAction.HUMAN_CALL

    def test_strict_low_global_overall_human_call(self) -> None:
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.MEDIUM,
            hunk_confidence=Confidence.HIGH,
        )
        decision = decider.decide(resolution, strategy=MergeStrategy.STRICT)
        assert decision.action == DecisionAction.HUMAN_CALL


class TestStrategyDeciderFast:
    def test_fast_regular_low_confidence_accept(self) -> None:
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.LOW,
            hunk_confidence=Confidence.LOW,
        )
        decision = decider.decide(resolution, strategy=MergeStrategy.FAST)
        assert decision.action == DecisionAction.ACCEPT

    def test_fast_low_file_confidence_accept(self) -> None:
        """Fast mode has no per-file confidence gate: LOW file confidence accepts."""
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.HIGH,
            hunk_confidence=Confidence.HIGH,
        )
        resolution.files[0].overall_confidence = Confidence.LOW
        decision = decider.decide(resolution, strategy=MergeStrategy.FAST)
        assert decision.action == DecisionAction.ACCEPT
        assert decision.reason == "Fast strategy: accepted"

    def test_fast_per_file_human_review_flag_accepts_with_warning(self) -> None:
        """A per-file requires_human_review flag no longer rejects in fast mode.

        Fast mode's contract is to never park a merge waiting on a human,
        so a per-file flag is accepted and surfaced as a warning in the
        decision reason; only a *global* flag aborts.
        """
        decider = StrategyDecider()
        resolution = _make_resolution(overall_confidence=Confidence.HIGH)
        resolution.files[0].flags["requires_human_review"] = True
        decision = decider.decide(resolution, strategy=MergeStrategy.FAST)
        assert decision.action == DecisionAction.ACCEPT
        assert decision.reason.startswith("Fast strategy: accepted")
        assert "WARNING" in decision.reason
        assert "requires_human_review on shared.txt" in decision.reason

    def test_fast_global_requires_human_review_reject(self) -> None:
        """A global requires_human_review flag in fast mode -> REJECT (abort, no human call)."""
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.HIGH,
            hunk_confidence=Confidence.HIGH,
            requires_human_review=True,
        )
        decision = decider.decide(resolution, strategy=MergeStrategy.FAST)
        assert decision.action == DecisionAction.REJECT
        assert "fast strategy aborts" in decision.reason.lower()

    def test_fast_mixed_confidence_files_accept(self) -> None:
        decider = StrategyDecider()
        # Low-confidence file
        file_low = FileResolution(
            path="regular.txt",
            resolved_content="ok",
            hunks=[HunkResolution(1, 3, Confidence.LOW, "low")],
            overall_confidence=Confidence.LOW,
            flags={},
        )
        # High-confidence file
        file_high = FileResolution(
            path="docs/notes.md",
            resolved_content="ok",
            hunks=[HunkResolution(1, 3, Confidence.HIGH, "high")],
            overall_confidence=Confidence.HIGH,
            flags={},
        )
        resolution = LLMResolution(
            files=[file_low, file_high],
            overall_confidence=Confidence.HIGH,
            flags={},
        )
        decision = decider.decide(resolution, strategy=MergeStrategy.FAST)
        assert decision.action == DecisionAction.ACCEPT


# --------- HumanCallWriter unit tests ---------


class TestHumanCallWriter:
    def test_call_file_created(self, tmp_path: Path) -> None:
        writer = HumanCallWriter(tmp_path)
        ctx = ConflictContext(
            project_root=tmp_path,
            ours_branch="main",
            theirs_branch="feature",
            merge_base="abc123",
            ours_head_sha="def456",
            theirs_head_sha="ghi789",
            files=[
                ConflictFile(
                    path="foo.txt",
                    hunks=[ConflictHunk(1, 5)],
                    base_content="base",
                    ours_content="ours",
                    theirs_content="theirs",
                    working_content="<<<<<<<\nours\n=======\ntheirs\n>>>>>>>\n",
                ),
            ],
        )
        resolution = _make_resolution(path="foo.txt")
        decision = StrategyDecision(
            action=DecisionAction.HUMAN_CALL,
            reason="low confidence",
        )

        call_file = writer.write_call(ctx, resolution, decision)

        assert call_file.exists()
        assert call_file.name.startswith("merge_")
        data = json.loads(call_file.read_text(encoding="utf-8"))
        assert data["type"] == "merge_conflict"
        assert data["theirs_branch"] == "feature"
        assert data["decision_reason"] == "low confidence"
        assert "options" in data
        assert "accept" in data["options"]
        assert "abort" in data["options"]
        assert "manual" in data["options"]
        assert "files" in data
        assert len(data["files"]) == 1
        assert data["files"][0]["path"] == "foo.txt"

    def test_call_file_prefix_distinguishes_from_merge_conflict(self, tmp_path: Path) -> None:
        writer = HumanCallWriter(tmp_path)
        ctx = ConflictContext(
            project_root=tmp_path,
            ours_branch="main",
            theirs_branch="feature",
            files=[],
        )
        resolution = _make_resolution()
        decision = StrategyDecision(
            action=DecisionAction.HUMAN_CALL,
            reason="test",
        )

        call_file = writer.write_call(ctx, resolution, decision)
        # Must start with "merge_" (not "merge_conflict_") to distinguish
        # from existing loop-mode merge_conflict_*.json files
        assert call_file.name.startswith("merge_")
        assert not call_file.name.startswith("merge_conflict_")

    def test_print_instructions_renders_merge_conflict_guidance(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``print_instructions`` renders the merge-conflict variant."""
        writer = HumanCallWriter(tmp_path)
        ctx = ConflictContext(
            project_root=tmp_path,
            ours_branch="main",
            theirs_branch="feature",
            files=[
                ConflictFile(
                    path="foo.txt",
                    hunks=[ConflictHunk(1, 5)],
                    working_content="<<<<<<<\nours\n=======\ntheirs\n>>>>>>>\n",
                ),
            ],
        )
        call_file = writer.write_call(
            ctx,
            _make_resolution(path="foo.txt"),
            StrategyDecision(action=DecisionAction.HUMAN_CALL, reason="test"),
        )

        writer.print_instructions(call_file)

        out = capsys.readouterr().out
        assert "Human review required for merge conflict" in out
        assert f"{call_file}.response" in out
        assert '{"choice": "accept|abort|manual", "feedback": "notes"}' in out
        assert "git merge --abort" in out

    def test_print_instructions_degraded_call_omits_conflict_commands(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A degraded call is written after the merge was already aborted.

        Telling the operator to edit conflict markers and run
        ``git merge --abort`` there would fail with "no merge to abort".
        """
        writer = HumanCallWriter(tmp_path)
        call_file = writer.write_degraded_call(
            branch="feature",
            message="Conflict context could not be built: boom",
            pre_merge_sha="abc1234",
        )

        writer.print_instructions(call_file)

        out = capsys.readouterr().out
        assert "already rolled back" in out
        assert f"{call_file}.response" in out
        assert '{"choice": "accept|abort|manual", "feedback": "notes"}' in out
        assert "Conflict context could not be built: boom" in out
        assert "git merge --abort" not in out
        assert "Edit files to resolve conflicts" not in out

    def test_print_instructions_is_localized(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Every fixed line of the printed guidance comes from the catalogs.

        The degraded variant is the one that used to carry hardcoded English,
        so a zh-CN render is the sharpest check that no literal survives.
        """
        from tianluo import i18n

        writer = HumanCallWriter(tmp_path)
        call_file = writer.write_degraded_call(
            branch="feature",
            message="Conflict context could not be built: boom",
            pre_merge_sha="abc1234",
        )

        i18n.set_language("zh-CN")
        try:
            writer.print_instructions(call_file)
        finally:
            i18n.reset_language()

        out = capsys.readouterr().out
        for fragment in (
            "Human review required",
            "Next steps:",
            "Nothing to abort or commit",
            "Call file:",
            "To respond, create:",
        ):
            assert fragment not in out, fragment
        assert "\u5408\u5e76\u5df2\u56de\u6eda" in out
        assert str(call_file) in out

    def test_print_instructions_unreadable_call_file_degrades(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A missing/corrupt call file still prints the conflict guidance."""
        writer = HumanCallWriter(tmp_path)
        missing = tmp_path / "calls" / "merge_nonexistent.json"

        writer.print_instructions(missing)

        out = capsys.readouterr().out
        assert "Human review required for merge conflict" in out
        assert f"{missing}.response" in out


# --------- Integration: resolver + strategy ---------


class TestResolverStrategyIntegration:
    def test_end_to_end_mock_llm_high_confidence_accept(self, tmp_path: Path) -> None:
        """Simulate a full resolution flow with a mock LLM returning high confidence."""
        _setup_conflict(tmp_path)
        resolver = ConflictResolver(tmp_path)
        ctx = build(tmp_path, "HEAD", "theirs-branch")

        # Instead of calling real LLM, directly build a resolution
        resolution = _make_resolution(
            path="shared.txt",
            resolved_content="line1\nRESOLVED\nline3\n",
            overall_confidence=Confidence.HIGH,
            hunk_confidence=Confidence.HIGH,
        )

        decider = StrategyDecider()
        decision = decider.decide(resolution, strategy=MergeStrategy.SAFE)
        assert decision.action == DecisionAction.ACCEPT

    def test_end_to_end_mock_llm_review_flag_human_call(self, tmp_path: Path) -> None:
        """Simulate a review-flagged resolution leading to HUMAN_CALL.

        Under the LLM-as-editor model, the decider gates on explicit
        flags rather than confidence rating.  A resolution carrying
        ``requires_human_review=True`` (set by the synthesiser when
        ``resolve_batch`` could not clear every marker) routes to a
        human MCP call regardless of confidence.
        """
        _setup_conflict(tmp_path)
        resolver = ConflictResolver(tmp_path)
        ctx = build(tmp_path, "HEAD", "theirs-branch")

        resolution = _make_resolution(
            path="shared.txt",
            resolved_content="line1\nRESOLVED\nline3\n",
            overall_confidence=Confidence.LOW,
            hunk_confidence=Confidence.LOW,
            requires_human_review=True,
        )

        decider = StrategyDecider()
        decision = decider.decide(resolution, strategy=MergeStrategy.SAFE)
        assert decision.action == DecisionAction.HUMAN_CALL
