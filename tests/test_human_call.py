"""Regression tests for human_call.py defects F1–F5.

Covers:
- F1: Collision-resistant filename generation
- F2: Atomic write with fsync
- F3: Orphan file guardrail re-check
- F4: Violation dict validation (raise instead of <unknown>)
- F5: Naming convention (__ instead of -)
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tianluo.engine.merge.conflict_context import (
    ConflictContext,
    ConflictFile,
    ConflictHunk,
)
from tianluo.engine.merge.conflict_resolver import (
    Confidence,
    FileResolution,
    HunkResolution,
    LLMResolution,
)
from tianluo.engine.merge.human_call import (
    HumanCallWriter,
    _atomic_write_json,
    _generate_call_filename,
)
from tianluo.engine.merge.strategy import DecisionAction, StrategyDecision


# --------- helpers ---------


def _make_context(
    tmp_path: Path,
    *,
    theirs_branch: str = "feature",
    files: list[ConflictFile] | None = None,
    ours_head_sha: str = "",
) -> ConflictContext:
    return ConflictContext(
        project_root=tmp_path,
        ours_branch="main",
        theirs_branch=theirs_branch,
        merge_base="abc123",
        ours_head_sha=ours_head_sha,
        theirs_head_sha="ghi789",
        files=files or [],
    )


def _make_resolution(
    *files: FileResolution,
) -> LLMResolution:
    return LLMResolution(
        files=list(files),
        overall_confidence=Confidence.HIGH,
        flags={},
    )


def _make_file_resolution(
    path: str = "foo.txt",
    resolved_content: str = "resolved\n",
    is_spec: bool = False,
) -> FileResolution:
    return FileResolution(
        path=path,
        resolved_content=resolved_content,
        hunks=[
            HunkResolution(
                start_line=1,
                end_line=5,
                confidence=Confidence.HIGH,
                reasoning="test",
            )
        ],
        overall_confidence=Confidence.HIGH,
        flags={},
        is_spec=is_spec,
    )


# --------- F1: filename collision resistance ---------


class TestGenerateCallFilename:
    def test_format_contains_required_segments(self) -> None:
        """Filename must contain utc_iso, pid, seq, sha8, safe_branch."""
        name = _generate_call_filename("merge", "feature/foo")
        assert name.startswith("merge_")
        assert name.endswith(".json")

        # Extract segments: merge_<ts>_<pid>_<seq>_<sha8>_<safe_branch>.json
        core = name[:-5]  # strip .json
        parts = core.split("_")
        # Expected: merge, YYYYMMDDTHHMMSS, microsec, pid, seq, sha8, safe_branch...
        assert len(parts) >= 7, f"Expected >= 7 parts, got {parts}"
        # Timestamp part must contain T
        assert "T" in parts[1], f"Timestamp missing T separator: {parts[1]}"
        # PID is numeric
        assert parts[3].isdigit(), f"PID not numeric: {parts[3]}"
        # seq is numeric
        assert parts[4].isdigit(), f"seq not numeric: {parts[4]}"
        # sha8 is hex
        assert re.fullmatch(r"[0-9a-f]{8}", parts[5]), f"sha8 not valid: {parts[5]}"

    def test_branch_slash_replaced_with_double_underscore(self) -> None:
        """F5: / in branch names becomes __, not -."""
        name = _generate_call_filename("merge", "feature/foo/bar")
        assert "__" in name
        assert "feature__foo__bar" in name
        # Old style used - which must not appear for the slash replacement
        # (the timestamp itself has no - so only __ should appear)
        core = name[name.index("_", 6) + 1 :]  # after timestamp
        assert "-" not in core.split("_")[-1], "Old '-' separator found for /"

    def test_unique_under_rapid_fire(self) -> None:
        """100 sequential calls with same branch must all be unique."""
        names = [_generate_call_filename("merge", "feature") for _ in range(100)]
        assert len(set(names)) == 100, f"Collision: {len(names)} calls, {len(set(names))} unique"

    def test_unique_under_threaded_race(self) -> None:
        """Concurrent threads must not collide."""
        names: list[str] = []
        lock = threading.Lock()

        def generate() -> None:
            for _ in range(25):
                name = _generate_call_filename("merge", "feature")
                with lock:
                    names.append(name)

        threads = [threading.Thread(target=generate) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(names) == 100
        assert len(set(names)) == 100

    def test_monotonic_seq(self) -> None:
        """Sequence numbers increase monotonically."""
        names = [_generate_call_filename("merge", "feature") for _ in range(10)]
        seqs = []
        for name in names:
            core = name[:-5]
            parts = core.split("_")
            seqs.append(int(parts[4]))
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == 10


class TestWriteCallFilename:
    def test_call_file_uses_new_format(self, tmp_path: Path) -> None:
        """write_call must generate new-style filenames."""
        writer = HumanCallWriter(tmp_path)
        ctx = _make_context(tmp_path)
        resolution = _make_resolution()
        decision = StrategyDecision(
            action=DecisionAction.HUMAN_CALL,
            reason="test",
        )
        call_file = writer.write_call(ctx, resolution, decision)
        assert call_file.exists()
        name = call_file.name
        # Must use T separator in timestamp (new format)
        assert "T" in name, f"Old timestamp format in {name}"
        # Must NOT have old YYYYMMDD_HHMMSS_microsec pattern without T
        assert not re.search(r"merge_\d{8}_\d{6}_\d{6}", name), f"Old format in {name}"


# --------- F2: atomic write with fsync ---------


class TestAtomicWriteJson:
    def test_produces_valid_json(self, tmp_path: Path) -> None:
        """Written file must contain valid JSON."""
        target = tmp_path / "test.json"
        data = {"key": "value", "nested": {"a": 1}}
        _atomic_write_json(target, data)
        assert target.exists()
        parsed = json.loads(target.read_text(encoding="utf-8"))
        assert parsed == data

    def test_atomic_replacement(self, tmp_path: Path) -> None:
        """Existing file must be atomically replaced."""
        target = tmp_path / "test.json"
        target.write_text('{"old": true}', encoding="utf-8")
        _atomic_write_json(target, {"new": True})
        parsed = json.loads(target.read_text(encoding="utf-8"))
        assert parsed == {"new": True}

    def test_no_tmp_file_left_behind(self, tmp_path: Path) -> None:
        """Temporary file must be cleaned up after successful write."""
        target = tmp_path / "test.json"
        _atomic_write_json(target, {"ok": True})
        tmp_files = list(tmp_path.glob(".tmp_*"))
        assert len(tmp_files) == 0, f"Leftover tmp files: {tmp_files}"

    def test_tmp_file_cleaned_on_error(self, tmp_path: Path) -> None:
        """Temporary file must be cleaned up on write failure."""
        target = tmp_path / "readonly_dir" / "test.json"
        target.parent.mkdir(parents=True)
        # Make directory read-only to force failure
        target.parent.chmod(0o555)
        try:
            with pytest.raises(OSError):
                _atomic_write_json(target, {"ok": True})
        finally:
            target.parent.chmod(0o755)
        tmp_files = list(target.parent.glob(".tmp_*"))
        assert len(tmp_files) == 0, f"Leftover tmp files after error: {tmp_files}"


# --------- F3: orphan file guardrail re-check ---------


class TestOrphanFileHandling:
    def test_orphan_file_detected_and_flagged(self, tmp_path: Path) -> None:
        """Non-spec orphans are rejected and recorded under
        ``rejected_orphans`` rather than passed through to ``files``.

        F3 (extended): orphan files not in context.files cannot be
        guardrails-validated for non-spec paths, and the LLM should
        never invent paths outside the conflict context.  The
        rejected orphan is surfaced via ``rejected_orphans`` so the
        operator can audit what the LLM tried to write.
        """
        writer = HumanCallWriter(tmp_path)
        ctx = _make_context(
            tmp_path,
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
        # Resolution has both matching file and orphan
        res = _make_resolution(
            _make_file_resolution(path="foo.txt", resolved_content="resolved foo"),
            _make_file_resolution(path="orphan.txt", resolved_content="resolved orphan"),
        )
        decision = StrategyDecision(
            action=DecisionAction.HUMAN_CALL,
            reason="test",
        )
        call_file = writer.write_call(ctx, res, decision)
        data = json.loads(call_file.read_text(encoding="utf-8"))

        # Only the matching file is included; the non-spec orphan is rejected.
        assert len(data["files"]) == 1
        assert data["files"][0]["path"] == "foo.txt"
        assert "is_orphan" not in data["files"][0]
        # The non-spec orphan is recorded for the operator to audit.
        assert "rejected_orphans" in data
        assert any(
            r["path"] == "orphan.txt" for r in data["rejected_orphans"]
        ), data.get("rejected_orphans")

    def test_no_orphan_when_all_match(self, tmp_path: Path) -> None:
        """When all resolution files match context files, no orphan flag."""
        writer = HumanCallWriter(tmp_path)
        ctx = _make_context(
            tmp_path,
            files=[
                ConflictFile(
                    path="foo.txt",
                    hunks=[ConflictHunk(1, 5)],
                    base_content="base",
                    ours_content="ours",
                    theirs_content="theirs",
                    working_content="conflict",
                    is_spec=False,
                ),
            ],
        )
        res = _make_resolution(
            _make_file_resolution(path="foo.txt"),
        )
        decision = StrategyDecision(
            action=DecisionAction.HUMAN_CALL,
            reason="test",
        )
        call_file = writer.write_call(ctx, res, decision)
        data = json.loads(call_file.read_text(encoding="utf-8"))

        assert len(data["files"]) == 1
        assert "is_orphan" not in data["files"][0]
        assert "orphan_guardrails_violations" not in data

    def test_orphan_spec_file_guardrails_checked(self, tmp_path: Path) -> None:
        """Orphan spec files must be checked against guardrails."""
        writer = HumanCallWriter(tmp_path)
        # Create a git repo so _read_original_for_orphan can work
        import subprocess
        subprocess.run(
            ["git", "init", str(tmp_path)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "user.email", "t@test.com"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "user.name", "Test"],
            check=True, capture_output=True,
        )

        spec_dir = tmp_path / "se3" / "specs" / "base"
        spec_dir.mkdir(parents=True)
        spec_file = spec_dir / "spec.md"
        spec_file.write_text(
            "## Requirement\n\n- SHALL validate all inputs.\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "base"],
            check=True, capture_output=True,
        )
        head_sha = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        ctx = _make_context(
            tmp_path,
            files=[
                ConflictFile(
                    path="foo.txt",
                    hunks=[ConflictHunk(1, 5)],
                    base_content="base",
                    ours_content="ours",
                    theirs_content="theirs",
                    working_content="conflict",
                    is_spec=False,
                ),
            ],
            ours_head_sha=head_sha,
        )
        # Orphan spec file that weakens SHALL to SHOULD
        res = _make_resolution(
            _make_file_resolution(path="foo.txt"),
            _make_file_resolution(
                path="se3/specs/base/spec.md",
                resolved_content="## Requirement\n\n- SHOULD validate all inputs.\n",
                is_spec=True,
            ),
        )
        decision = StrategyDecision(
            action=DecisionAction.HUMAN_CALL,
            reason="test",
        )
        call_file = writer.write_call(ctx, res, decision)
        data = json.loads(call_file.read_text(encoding="utf-8"))

        # Should have orphan guardrails violations
        assert "orphan_guardrails_violations" in data
        violations = data["orphan_guardrails_violations"]
        assert len(violations) >= 1
        assert any(
            v["violation_type"] == "WEAKENING" and "SHALL" in v["message"]
            for v in violations
        )

    def test_orphan_new_spec_file_no_violation(self, tmp_path: Path) -> None:
        """A completely new orphan spec file has no original to weaken."""
        writer = HumanCallWriter(tmp_path)
        import subprocess
        subprocess.run(
            ["git", "init", str(tmp_path)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "user.email", "t@test.com"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "user.name", "Test"],
            check=True, capture_output=True,
        )
        # Commit something so HEAD exists
        (tmp_path / "README.md").write_text("hello")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "base"],
            check=True, capture_output=True,
        )
        head_sha = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        ctx = _make_context(
            tmp_path,
            files=[],
            ours_head_sha=head_sha,
        )
        # Orphan spec file that does NOT exist in HEAD
        res = _make_resolution(
            _make_file_resolution(
                path="se3/specs/new/spec.md",
                resolved_content="## Requirement\n\n- SHALL do something.\n",
                is_spec=True,
            ),
        )
        decision = StrategyDecision(
            action=DecisionAction.HUMAN_CALL,
            reason="test",
        )
        call_file = writer.write_call(ctx, res, decision)
        data = json.loads(call_file.read_text(encoding="utf-8"))

        # No guardrails violations for new file
        assert "orphan_guardrails_violations" not in data or data.get("orphan_guardrails_violations") == []


# --------- F4: violation dict validation ---------


class TestGuardrailCallViolationValidation:
    def test_valid_violations_succeed(self, tmp_path: Path) -> None:
        """Valid violation dicts produce a valid call file."""
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
        )
        data = json.loads(call_file.read_text(encoding="utf-8"))
        assert data["violations"][0]["file_path"] == "se3/specs/base/spec.md"
        assert data["violations"][0]["violation_type"] == "WEAKENING"
        assert data["violations"][0]["message"] == "SHALL weakened to SHOULD"

    def test_missing_file_path_raises_valueerror(self, tmp_path: Path) -> None:
        """Missing file_path must raise ValueError, not substitute <unknown>."""
        writer = HumanCallWriter(tmp_path)
        violations = [
            {
                "violation_type": "WEAKENING",
                "message": "SHALL weakened to SHOULD",
            }
        ]
        with pytest.raises(ValueError, match="missing required keys"):
            writer.write_guardrail_call(
                branch="feature",
                violations=violations,
                pre_merge_sha="abc123",
            )

    def test_missing_violation_type_raises_valueerror(self, tmp_path: Path) -> None:
        """Missing violation_type must raise ValueError."""
        writer = HumanCallWriter(tmp_path)
        violations = [
            {
                "file_path": "se3/specs/base/spec.md",
                "message": "SHALL weakened to SHOULD",
            }
        ]
        with pytest.raises(ValueError, match="missing required keys"):
            writer.write_guardrail_call(
                branch="feature",
                violations=violations,
                pre_merge_sha="abc123",
            )

    def test_missing_message_raises_valueerror(self, tmp_path: Path) -> None:
        """Missing message must raise ValueError."""
        writer = HumanCallWriter(tmp_path)
        violations = [
            {
                "file_path": "se3/specs/base/spec.md",
                "violation_type": "WEAKENING",
            }
        ]
        with pytest.raises(ValueError, match="missing required keys"):
            writer.write_guardrail_call(
                branch="feature",
                violations=violations,
                pre_merge_sha="abc123",
            )

    def test_non_dict_violation_raises_typeerror(self, tmp_path: Path) -> None:
        """Non-dict violation must raise TypeError."""
        writer = HumanCallWriter(tmp_path)
        violations = ["not a dict"]
        with pytest.raises(TypeError, match="expected dict violation"):
            writer.write_guardrail_call(
                branch="feature",
                violations=violations,
                pre_merge_sha="abc123",
            )

    def test_multiple_missing_keys_in_error(self, tmp_path: Path) -> None:
        """Error message should list all missing keys."""
        writer = HumanCallWriter(tmp_path)
        violations = [
            {
                "file_path": "se3/specs/base/spec.md",
                # missing violation_type AND message
            }
        ]
        with pytest.raises(ValueError, match="violation_type") as exc_info:
            writer.write_guardrail_call(
                branch="feature",
                violations=violations,
                pre_merge_sha="abc123",
            )
        assert "message" in str(exc_info.value)

    def test_extra_keys_preserved(self, tmp_path: Path) -> None:
        """Extra keys in violation dict should be preserved."""
        writer = HumanCallWriter(tmp_path)
        violations = [
            {
                "file_path": "se3/specs/base/spec.md",
                "violation_type": "WEAKENING",
                "message": "SHALL weakened to SHOULD",
                "evidence": {"strong_line": "SHALL x", "weak_line": "SHOULD x"},
                "custom_field": "custom_value",
            }
        ]
        call_file = writer.write_guardrail_call(
            branch="feature",
            violations=violations,
            pre_merge_sha="abc123",
        )
        data = json.loads(call_file.read_text(encoding="utf-8"))
        v = data["violations"][0]
        assert v["evidence"]["strong_line"] == "SHALL x"
        assert v["custom_field"] == "custom_value"


# --------- F5: naming convention ---------


class TestNamingConvention:
    def test_branch_slash_becomes_double_underscore_in_guardrail_call(self, tmp_path: Path) -> None:
        """Branch names with / must use __ in guardrail call filenames."""
        writer = HumanCallWriter(tmp_path)
        violations = [
            {
                "file_path": "se3/specs/base/spec.md",
                "violation_type": "WEAKENING",
                "message": "SHALL weakened to SHOULD",
            }
        ]
        call_file = writer.write_guardrail_call(
            branch="feature/foo/bar",
            violations=violations,
            pre_merge_sha="abc123",
        )
        name = call_file.name
        assert "feature__foo__bar" in name
        assert "feature-foo-bar" not in name

    def test_branch_slash_becomes_double_underscore_in_merge_call(self, tmp_path: Path) -> None:
        """Branch names with / must use __ in merge call filenames."""
        writer = HumanCallWriter(tmp_path)
        ctx = _make_context(tmp_path, theirs_branch="feature/foo")
        res = _make_resolution()
        decision = StrategyDecision(
            action=DecisionAction.HUMAN_CALL,
            reason="test",
        )
        call_file = writer.write_call(ctx, res, decision)
        name = call_file.name
        assert "feature__foo" in name
        assert "feature-foo" not in name

    def test_plain_branch_no_separator(self, tmp_path: Path) -> None:
        """Branch names without / should not have __ or -."""
        writer = HumanCallWriter(tmp_path)
        ctx = _make_context(tmp_path, theirs_branch="feature")
        res = _make_resolution()
        decision = StrategyDecision(
            action=DecisionAction.HUMAN_CALL,
            reason="test",
        )
        call_file = writer.write_call(ctx, res, decision)
        name = call_file.name
        # The branch part at the end should be just "feature"
        assert "_feature" in name
        assert "__" not in name or "feature__" not in name  # no __ around feature
