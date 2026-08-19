"""Regression tests for human_call.py defects F1–F5.

Covers:
- F1: Collision-resistant filename generation
- F2: Atomic write with fsync
- F3: Orphan resolution files are always rejected
- F5: Naming convention (__ instead of -)
- Degraded call files (``merge_context_unavailable``)
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
    DEGRADED_CALL_TYPE,
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


# --------- F3: every orphan resolution file is rejected ---------


class TestOrphanFileHandling:
    def test_orphan_file_rejected_and_recorded(self, tmp_path: Path) -> None:
        """An orphan is rejected and recorded under ``rejected_orphans``
        rather than passed through to ``files``.

        F3: a resolution file whose path is not present in the conflict
        context is content the LLM invented — there is nothing to
        validate it against, so it is always rejected.  The rejected
        orphan is surfaced via ``rejected_orphans`` so the operator can
        audit what the LLM tried to write.
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

        # Only the matching file is included; the orphan is rejected.
        assert [f["path"] for f in data["files"]] == ["foo.txt"]
        assert "is_orphan" not in data["files"][0]
        # The orphan is recorded for the operator to audit.
        assert len(data["rejected_orphans"]) == 1
        orphan = data["rejected_orphans"][0]
        assert orphan["path"] == "orphan.txt"
        assert orphan["reason"] == "orphan path not present in the conflict context"
        assert orphan["evidence"]["content_preview"] == "resolved orphan"
        assert orphan["evidence"]["content_size"] == len("resolved orphan")

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
        assert "rejected_orphans" not in data

    def test_every_orphan_rejected_regardless_of_path_shape(
        self, tmp_path: Path,
    ) -> None:
        """EVERY orphan is rejected — no path shape is privileged.

        The retired spec-guardrails chain used to treat orphans under
        ``tianluo/specs/**`` differently (reading the original from the
        ours-side ref and re-running guardrails).  Now every orphan gets
        the same uniform rejection, so a spec-shaped path, a nested
        path and a plain path must all land in ``rejected_orphans``
        with the same reason and none of them in ``files``.
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
                    working_content="conflict",
                ),
            ],
        )
        orphan_paths = [
            "tianluo/specs/base/spec.md",
            "deeply/nested/dir/thing.py",
            "toplevel.txt",
        ]
        res = _make_resolution(
            _make_file_resolution(path="foo.txt"),
            *[
                _make_file_resolution(path=op, resolved_content=f"content for {op}")
                for op in orphan_paths
            ],
        )
        decision = StrategyDecision(
            action=DecisionAction.HUMAN_CALL, reason="test",
        )
        call_file = writer.write_call(ctx, res, decision)
        data = json.loads(call_file.read_text(encoding="utf-8"))

        assert [f["path"] for f in data["files"]] == ["foo.txt"]
        rejected = data["rejected_orphans"]
        assert [r["path"] for r in rejected] == orphan_paths
        assert {r["reason"] for r in rejected} == {
            "orphan path not present in the conflict context"
        }
        # The retired spec-specific evidence keys are not emitted.
        for r in rejected:
            assert "has_spec_keywords" not in r["evidence"]
            assert "spec_keyword_count" not in r["evidence"]
            assert "looks_like_spec_path" not in r["evidence"]

    def test_orphan_evidence_flags_conflict_markers(self, tmp_path: Path) -> None:
        """Leftover conflict markers in orphan content are surfaced."""
        writer = HumanCallWriter(tmp_path)
        ctx = _make_context(tmp_path, files=[])
        res = _make_resolution(
            _make_file_resolution(
                path="orphan.txt",
                resolved_content="<<<<<<< HEAD\na\n=======\nb\n>>>>>>> x\n",
            ),
        )
        decision = StrategyDecision(
            action=DecisionAction.HUMAN_CALL, reason="test",
        )
        call_file = writer.write_call(ctx, res, decision)
        data = json.loads(call_file.read_text(encoding="utf-8"))

        assert data["files"] == []
        assert data["rejected_orphans"][0]["evidence"]["has_conflict_markers"] is True


# --------- degraded call files (context unavailable) ---------


class TestWriteDegradedCall:
    def test_writes_degraded_call_file(self, tmp_path: Path) -> None:
        """A degraded call carries the new ``merge_context_unavailable`` type
        plus the branch, pre-merge SHA and operator-facing message."""
        writer = HumanCallWriter(tmp_path)
        call_file = writer.write_degraded_call(
            branch="feature",
            message="build_conflict_context raised: boom",
            pre_merge_sha="abc123",
        )
        assert call_file.exists()
        assert call_file.parent == tmp_path / "tianluo" / "calls"

        data = json.loads(call_file.read_text(encoding="utf-8"))
        assert data["type"] == DEGRADED_CALL_TYPE == "merge_context_unavailable"
        assert data["branch"] == "feature"
        assert data["pre_merge_sha"] == "abc123"
        assert data["message"] == "build_conflict_context raised: boom"
        assert set(data["options"]) == {"accept", "abort", "manual"}
        assert data["created_at"]
        # Instructions must name the exact response file the operator writes.
        assert f"{call_file.name}.response" in data["instructions"]
        assert "build_conflict_context raised: boom" in data["instructions"]
        # A degraded call has no per-file resolution to write back.
        assert "files" not in data

    def test_degraded_call_defaults_pre_merge_sha_to_empty(
        self, tmp_path: Path,
    ) -> None:
        writer = HumanCallWriter(tmp_path)
        call_file = writer.write_degraded_call("feature", "no context")
        data = json.loads(call_file.read_text(encoding="utf-8"))
        assert data["pre_merge_sha"] == ""

    def test_degraded_calls_do_not_collide(self, tmp_path: Path) -> None:
        """Two degraded calls for the same branch get distinct files."""
        writer = HumanCallWriter(tmp_path)
        first = writer.write_degraded_call("feature", "one")
        second = writer.write_degraded_call("feature", "two")
        assert first != second
        assert json.loads(first.read_text(encoding="utf-8"))["message"] == "one"
        assert json.loads(second.read_text(encoding="utf-8"))["message"] == "two"


# --------- F5: naming convention ---------


class TestNamingConvention:
    def test_branch_slash_becomes_double_underscore_in_degraded_call(self, tmp_path: Path) -> None:
        """Branch names with / must use __ in degraded call filenames."""
        writer = HumanCallWriter(tmp_path)
        call_file = writer.write_degraded_call(
            branch="feature/foo/bar",
            message="context unavailable",
            pre_merge_sha="abc123",
        )
        name = call_file.name
        assert "feature__foo__bar" in name
        assert "feature-foo-bar" not in name
        assert "/" not in name

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
