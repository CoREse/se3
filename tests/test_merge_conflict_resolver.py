"""Tests for ConflictResolver, StrategyDecider, and HumanCallWriter."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from se3.engine.merge.conflict_context import (
    ConflictContext,
    ConflictFile,
    ConflictHunk,
    build,
)
from se3.engine.merge.conflict_resolver import (
    Confidence,
    ConflictResolver,
    FileResolution,
    HunkResolution,
    LLMResolution,
    MergeStrategy,
)
from se3.engine.merge.human_call import HumanCallWriter
from se3.engine.merge.strategy import DecisionAction, StrategyDecider, StrategyDecision


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
    spec_guardrail_concern: bool = False,
    is_spec: bool = False,
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
                flags={
                    "requires_human_review": requires_human_review,
                    "spec_guardrail_concern": spec_guardrail_concern,
                },
                is_spec=is_spec,
            ),
        ],
        overall_confidence=overall_confidence,
        flags={
            "requires_human_review": requires_human_review,
            "spec_guardrail_concern": spec_guardrail_concern,
        },
    )


# --------- ConflictResolver unit tests ---------


class TestConflictResolverPrompt:
    def test_prompt_contains_merge_metadata(self, tmp_path: Path) -> None:
        _setup_conflict(tmp_path)
        resolver = ConflictResolver(tmp_path)
        ctx = build(tmp_path, "HEAD", "theirs-branch")
        prompt = resolver._build_prompt(ctx, MergeStrategy.SAFE)

        assert ctx.ours_branch in prompt
        assert ctx.theirs_branch in prompt
        assert ctx.merge_base in prompt
        assert "Merge Metadata" in prompt

    def test_prompt_contains_strategy_indicator(self, tmp_path: Path) -> None:
        _setup_conflict(tmp_path)
        resolver = ConflictResolver(tmp_path)
        ctx = build(tmp_path, "HEAD", "theirs-branch")
        prompt = resolver._build_prompt(ctx, MergeStrategy.STRICT)

        assert "Strategy: strict" in prompt
        assert "Strict mode" in prompt

    def test_prompt_contains_file_versions(self, tmp_path: Path) -> None:
        _setup_conflict(tmp_path)
        resolver = ConflictResolver(tmp_path)
        ctx = build(tmp_path, "HEAD", "theirs-branch")
        prompt = resolver._build_prompt(ctx, MergeStrategy.SAFE)

        assert "Base version" in prompt
        assert "Ours version" in prompt
        assert "Theirs version" in prompt
        assert "Working tree" in prompt
        assert "line1" in prompt  # content from base/ours/theirs

    def test_prompt_contains_json_schema(self, tmp_path: Path) -> None:
        _setup_conflict(tmp_path)
        resolver = ConflictResolver(tmp_path)
        ctx = build(tmp_path, "HEAD", "theirs-branch")
        prompt = resolver._build_prompt(ctx, MergeStrategy.SAFE)

        assert "resolved_content" in prompt
        assert "overall_confidence" in prompt
        assert "requires_human_review" in prompt
        assert "spec_guardrail_concern" in prompt

    def test_prompt_warns_for_spec_files(self, tmp_path: Path) -> None:
        _setup_conflict(tmp_path, rel_path="se3/specs/test/spec.md")
        resolver = ConflictResolver(tmp_path)
        ctx = build(tmp_path, "HEAD", "theirs-branch")
        prompt = resolver._build_prompt(ctx, MergeStrategy.SAFE)

        assert "SPEC FILES DETECTED" in prompt
        assert "guardrails" in prompt.lower() or "SHALL" in prompt


class TestConflictResolverParse:
    def test_parse_valid_json(self, tmp_path: Path) -> None:
        resolver = ConflictResolver(tmp_path)
        # Use a multi-line resolved_content so the hunk's end_line=5 is
        # within bounds; end_line beyond the resolved file's line count
        # forces human review (a separate bug-fix described in the
        # high-severity bounds-check issue).
        raw = json.dumps({
            "files": [
                {
                    "path": "foo.txt",
                    "resolved_content": "line1\nline2\nline3\nline4\nline5\n",
                    "hunks": [
                        {
                            "start_line": 1,
                            "end_line": 5,
                            "confidence": "high",
                            "reasoning": "merged both",
                        }
                    ],
                    "overall_confidence": "high",
                    "flags": {
                        "requires_human_review": False,
                        "spec_guardrail_concern": False,
                    },
                }
            ],
            "overall_confidence": "high",
            "flags": {
                "requires_human_review": False,
                "spec_guardrail_concern": False,
            },
        })

        # Build a minimal context
        cf = ConflictFile(path="foo.txt", hunks=[ConflictHunk(1, 5)])
        ctx = ConflictContext(
            project_root=tmp_path,
            ours_branch="main",
            theirs_branch="feat",
            files=[cf],
        )

        result = resolver._parse_response(raw, ctx)
        assert result.overall_confidence == Confidence.HIGH
        assert len(result.files) == 1
        assert result.files[0].path == "foo.txt"
        assert result.files[0].resolved_content == "line1\nline2\nline3\nline4\nline5\n"
        assert len(result.files[0].hunks) == 1
        assert result.files[0].hunks[0].confidence == Confidence.HIGH
        assert result.files[0].flags["requires_human_review"] is False

    def test_parse_invalid_json_fallback(self, tmp_path: Path, monkeypatch) -> None:
        resolver = ConflictResolver(tmp_path)
        cf = ConflictFile(path="foo.txt", hunks=[ConflictHunk(1, 5)], working_content="conflict\n")
        ctx = ConflictContext(
            project_root=tmp_path,
            ours_branch="main",
            theirs_branch="feat",
            files=[cf],
        )

        # Mock extract_json_two_phase to return None (simulating parse failure)
        monkeypatch.setattr(
            "se3.engine.merge.conflict_resolver.extract_json_two_phase",
            lambda *args, **kwargs: None,
        )

        result = resolver._parse_response("not json at all", ctx)
        assert result.overall_confidence == Confidence.LOW
        assert result.flags["requires_human_review"] is True
        assert result.parse_error is not None

    def test_parse_conflict_markers_flag_human_review(self, tmp_path: Path) -> None:
        resolver = ConflictResolver(tmp_path)
        raw = json.dumps({
            "files": [
                {
                    "path": "foo.txt",
                    "resolved_content": "line\n<<<<<<< HEAD\n ours\n=======\ntheirs\n>>>>>>> branch\nend",
                    "hunks": [],
                    "overall_confidence": "high",
                    "flags": {
                        "requires_human_review": False,
                        "spec_guardrail_concern": False,
                    },
                }
            ],
            "overall_confidence": "high",
            "flags": {
                "requires_human_review": False,
                "spec_guardrail_concern": False,
            },
        })

        cf = ConflictFile(path="foo.txt", hunks=[])
        ctx = ConflictContext(
            project_root=tmp_path,
            ours_branch="main",
            theirs_branch="feat",
            files=[cf],
        )

        result = resolver._parse_response(raw, ctx)
        # Conflict markers in resolved content should trigger human review flag
        assert result.files[0].flags["requires_human_review"] is True

    def test_parse_conflict_markers_without_flags_field(self, tmp_path: Path) -> None:
        """When LLM JSON omits 'flags' but content has markers, force_review must still be set."""
        resolver = ConflictResolver(tmp_path)
        raw = json.dumps({
            "files": [
                {
                    "path": "foo.txt",
                    "resolved_content": "line\n<<<<<<< HEAD\n ours\n=======\ntheirs\n>>>>>>> branch\nend",
                    "hunks": [],
                    "overall_confidence": "high",
                    # flags field deliberately omitted
                }
            ],
            "overall_confidence": "high",
            "flags": {
                "requires_human_review": False,
                "spec_guardrail_concern": False,
            },
        })

        cf = ConflictFile(path="foo.txt", hunks=[])
        ctx = ConflictContext(
            project_root=tmp_path,
            ours_branch="main",
            theirs_branch="feat",
            files=[cf],
        )

        result = resolver._parse_response(raw, ctx)
        assert result.files[0].flags["requires_human_review"] is True

    def test_parse_confidence_case_insensitive(self, tmp_path: Path) -> None:
        resolver = ConflictResolver(tmp_path)
        raw = json.dumps({
            "files": [],
            "overall_confidence": "HIGH",
            "flags": {},
        })

        ctx = ConflictContext(
            project_root=tmp_path,
            ours_branch="main",
            theirs_branch="feat",
            files=[],
        )

        result = resolver._parse_response(raw, ctx)
        assert result.overall_confidence == Confidence.HIGH


# --------- StrategyDecider unit tests ---------


class TestStrategyDeciderDefault:
    def test_default_high_confidence_no_flags_accept(self) -> None:
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.HIGH,
            hunk_confidence=Confidence.HIGH,
        )
        decision = decider.decide(resolution, has_spec_files=False, strategy=MergeStrategy.SAFE)
        assert decision.action == DecisionAction.ACCEPT

    def test_default_low_overall_confidence_human_call(self) -> None:
        decider = StrategyDecider()
        resolution = _make_resolution(overall_confidence=Confidence.LOW)
        decision = decider.decide(resolution, has_spec_files=False, strategy=MergeStrategy.SAFE)
        assert decision.action == DecisionAction.HUMAN_CALL
        assert "overall confidence" in decision.reason

    def test_default_requires_human_review_flag_human_call(self) -> None:
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.HIGH,
            requires_human_review=True,
        )
        decision = decider.decide(resolution, has_spec_files=False, strategy=MergeStrategy.SAFE)
        assert decision.action == DecisionAction.HUMAN_CALL
        assert "requires_human_review" in decision.reason

    def test_default_spec_guardrail_concern_human_call(self) -> None:
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.HIGH,
            spec_guardrail_concern=True,
        )
        decision = decider.decide(resolution, has_spec_files=True, strategy=MergeStrategy.SAFE)
        assert decision.action == DecisionAction.HUMAN_CALL
        assert "spec_guardrail_concern" in decision.reason

    def test_default_per_file_flag_human_call(self) -> None:
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.HIGH,
            requires_human_review=False,
        )
        # Add per-file flag
        resolution.files[0].flags["requires_human_review"] = True
        decision = decider.decide(resolution, has_spec_files=False, strategy=MergeStrategy.SAFE)
        assert decision.action == DecisionAction.HUMAN_CALL

    def test_default_medium_hunk_high_overall_accept(self) -> None:
        """Default strategy accepts when hunks are MEDIUM but file+global overall is HIGH."""
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.HIGH,
            hunk_confidence=Confidence.MEDIUM,
        )
        decision = decider.decide(resolution, has_spec_files=False, strategy=MergeStrategy.SAFE)
        assert decision.action == DecisionAction.ACCEPT

    def test_default_low_file_overall_still_human_call(self) -> None:
        """Default strategy still rejects when file-level overall confidence is LOW."""
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.HIGH,
            hunk_confidence=Confidence.HIGH,
        )
        resolution.files[0].overall_confidence = Confidence.LOW
        decision = decider.decide(resolution, has_spec_files=False, strategy=MergeStrategy.SAFE)
        assert decision.action == DecisionAction.HUMAN_CALL


class TestStrategyDeciderStrict:
    def test_strict_all_high_accept(self) -> None:
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.HIGH,
            hunk_confidence=Confidence.HIGH,
        )
        decision = decider.decide(resolution, has_spec_files=False, strategy=MergeStrategy.STRICT)
        assert decision.action == DecisionAction.ACCEPT

    def test_strict_low_hunk_human_call(self) -> None:
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.HIGH,
            hunk_confidence=Confidence.LOW,
        )
        decision = decider.decide(resolution, has_spec_files=False, strategy=MergeStrategy.STRICT)
        assert decision.action == DecisionAction.HUMAN_CALL
        assert "hunk" in decision.reason.lower() or "confidence" in decision.reason.lower()

    def test_strict_low_file_overall_human_call(self) -> None:
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.HIGH,
            hunk_confidence=Confidence.HIGH,
        )
        resolution.files[0].overall_confidence = Confidence.MEDIUM
        decision = decider.decide(resolution, has_spec_files=False, strategy=MergeStrategy.STRICT)
        assert decision.action == DecisionAction.HUMAN_CALL

    def test_strict_low_global_overall_human_call(self) -> None:
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.MEDIUM,
            hunk_confidence=Confidence.HIGH,
        )
        decision = decider.decide(resolution, has_spec_files=False, strategy=MergeStrategy.STRICT)
        assert decision.action == DecisionAction.HUMAN_CALL


class TestStrategyDeciderFast:
    def test_fast_regular_low_confidence_accept(self) -> None:
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.LOW,
            hunk_confidence=Confidence.LOW,
            is_spec=False,
        )
        decision = decider.decide(resolution, has_spec_files=False, strategy=MergeStrategy.FAST)
        assert decision.action == DecisionAction.ACCEPT

    def test_fast_spec_guardrail_concern_accept(self) -> None:
        """spec_guardrail_concern is deferred to post-merge guardrails in fast mode."""
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.HIGH,
            spec_guardrail_concern=True,
            is_spec=True,
        )
        decision = decider.decide(resolution, has_spec_files=True, strategy=MergeStrategy.FAST)
        assert decision.action == DecisionAction.ACCEPT
        assert "deferred" in decision.reason.lower()

    def test_fast_spec_low_confidence_reject(self) -> None:
        """Low confidence on spec file in fast mode → REJECT (abort, no human call)."""
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.HIGH,
            hunk_confidence=Confidence.HIGH,
            is_spec=True,
        )
        resolution.files[0].overall_confidence = Confidence.LOW
        decision = decider.decide(resolution, has_spec_files=True, strategy=MergeStrategy.FAST)
        assert decision.action == DecisionAction.REJECT
        assert "fast strategy aborts" in decision.reason.lower()

    def test_fast_spec_requires_human_review_reject(self) -> None:
        """requires_human_review on spec file in fast mode -> REJECT (abort, no human call)."""
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.HIGH,
            hunk_confidence=Confidence.HIGH,
            is_spec=True,
            requires_human_review=True,
        )
        decision = decider.decide(resolution, has_spec_files=True, strategy=MergeStrategy.FAST)
        assert decision.action == DecisionAction.REJECT
        assert "fast strategy aborts" in decision.reason.lower()

    def test_fast_mixed_regular_and_spec_accept(self) -> None:
        decider = StrategyDecider()
        # Regular file with low confidence
        file_regular = FileResolution(
            path="regular.txt",
            resolved_content="ok",
            hunks=[HunkResolution(1, 3, Confidence.LOW, "low")],
            overall_confidence=Confidence.LOW,
            flags={},
            is_spec=False,
        )
        # Spec file with high confidence
        file_spec = FileResolution(
            path="se3/specs/test/spec.md",
            resolved_content="ok",
            hunks=[HunkResolution(1, 3, Confidence.HIGH, "high")],
            overall_confidence=Confidence.HIGH,
            flags={},
            is_spec=True,
        )
        resolution = LLMResolution(
            files=[file_regular, file_spec],
            overall_confidence=Confidence.HIGH,
            flags={},
        )
        decision = decider.decide(resolution, has_spec_files=True, strategy=MergeStrategy.FAST)
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
                    is_spec=False,
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

    def test_guardrail_call_instructions_differ_for_stalled_type(self, tmp_path: Path) -> None:
        """guardrail_repair_stalled call files mention LLM attempts in instructions."""
        writer = HumanCallWriter(tmp_path)

        violations = [
            {
                "file_path": "se3/specs/base/spec.md",
                "violation_type": "WEAKENING",
                "message": "SHALL weakened to SHOULD",
            }
        ]

        # Standard violation call
        call_violation = writer.write_guardrail_call(
            branch="feature",
            violations=violations,
            pre_merge_sha="abc123",
            call_type="guardrail_violation",
        )
        data_v = json.loads(call_violation.read_text(encoding="utf-8"))

        # Stalled repair call
        call_stalled = writer.write_guardrail_call(
            branch="feature",
            violations=violations,
            pre_merge_sha="abc123",
            call_type="guardrail_repair_stalled",
            iteration_count=2,
        )
        data_s = json.loads(call_stalled.read_text(encoding="utf-8"))

        # Both should have instructions
        assert "instructions" in data_v
        assert "instructions" in data_s

        # Stalled instructions should mention LLM repair attempts
        assert "LLM repair was attempted" in data_s["instructions"]
        assert "2 time(s)" in data_s["instructions"]
        assert "stalled" in data_s["instructions"]

        # Regular violation instructions should NOT mention LLM repair attempts
        assert "LLM repair was attempted" not in data_v["instructions"]

        # Both should still mention rollback
        assert "rolled back" in data_v["instructions"]
        assert "rolled back" in data_s["instructions"]

    def test_print_instructions_no_evidence_shows_header(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Violations without evidence must still show type, file, and message."""
        writer = HumanCallWriter(tmp_path)
        violations = [
            {
                "file_path": "se3/specs/base/spec.md",
                "violation_type": "WEAKENING",
                "message": "SHALL weakened to SHOULD",
            }
        ]
        call_file = writer.write_guardrail_call(
            branch="feature",
            violations=violations,
            pre_merge_sha="abc123",
            call_type="guardrail_violation",
        )
        writer.print_instructions(call_file)
        captured = capsys.readouterr()
        assert "[WEAKENING] se3/specs/base/spec.md" in captured.out
        assert "Message: SHALL weakened to SHOULD" in captured.out

    def test_print_instructions_many_violations_trailing_once(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """With 3+ violations the trailing '... and N more' must print exactly once."""
        writer = HumanCallWriter(tmp_path)
        violations = [
            {
                "file_path": f"se3/specs/base/spec{i}.md",
                "violation_type": "WEAKENING",
                "message": f"msg {i}",
                "evidence": {
                    "strong_line": f"SHALL {i}",
                    "weak_line": f"SHOULD {i}",
                    "pairing_score": 0.8,
                },
            }
            for i in range(3)
        ]
        call_file = writer.write_guardrail_call(
            branch="feature",
            violations=violations,
            pre_merge_sha="abc123",
            call_type="guardrail_violation",
        )
        writer.print_instructions(call_file)
        captured = capsys.readouterr()
        # Both first two violation headers should appear
        assert captured.out.count("[WEAKENING]") == 2
        # The trailing message must appear exactly once
        assert captured.out.count("... and 1 more violation(s)") == 1


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
        decision = decider.decide(
            resolution,
            has_spec_files=ctx.has_spec_files,
            strategy=MergeStrategy.SAFE,
        )
        assert decision.action == DecisionAction.ACCEPT

    def test_end_to_end_mock_llm_low_confidence_human_call(self, tmp_path: Path) -> None:
        """Simulate low-confidence resolution leading to HUMAN_CALL."""
        _setup_conflict(tmp_path)
        resolver = ConflictResolver(tmp_path)
        ctx = build(tmp_path, "HEAD", "theirs-branch")

        resolution = _make_resolution(
            path="shared.txt",
            resolved_content="line1\nRESOLVED\nline3\n",
            overall_confidence=Confidence.LOW,
            hunk_confidence=Confidence.LOW,
        )

        decider = StrategyDecider()
        decision = decider.decide(
            resolution,
            has_spec_files=ctx.has_spec_files,
            strategy=MergeStrategy.SAFE,
        )
        assert decision.action == DecisionAction.HUMAN_CALL
