"""Prompt-size invariance for the LLM-as-editor conflict prompt.

Regression guard for the 2026-07-10 failure where a 2.5MB tracked
``se3/code-index.md`` conflict produced a ~9.9MB prompt (99.97% of it
four inlined copies of the file), blowing past every agent CLI's input
limit so no LLM ever ran.  The prompt must now be bounded by the number
of conflicting files and hunks — never by their content size.
"""

from __future__ import annotations

from pathlib import Path

from tianluo.engine.merge.conflict_context import ConflictFile, ConflictHunk
from tianluo.engine.merge.conflict_resolver import (
    BatchContext,
    ConflictResolver,
    MergeStrategy,
)


# Distinctive strings that only ever appear inside the three-way
# contents — if any of them shows up in the prompt, some content block
# is being inlined again.
SENTINELS = {
    "base": "SENTINEL_BASE_2ff9",
    "ours": "SENTINEL_OURS_71ac",
    "theirs": "SENTINEL_THEIRS_c30d",
    "working": "SENTINEL_WORKING_e15b",
}


def _conflict_file(size_mb: int) -> ConflictFile:
    """A conflict file whose four contents each weigh ``size_mb`` MiB."""
    pad = 1024 * 1024 * size_mb

    def body(kind: str) -> str:
        return SENTINELS[kind] + "x" * pad

    return ConflictFile(
        path="se3/code-index.md",
        base_content=body("base"),
        ours_content=body("ours"),
        theirs_content=body("theirs"),
        working_content=body("working"),
        base_exists=True,
        ours_exists=True,
        theirs_exists=True,
        hunks=[ConflictHunk(start_line=10, end_line=42),
               ConflictHunk(start_line=100, end_line=137)],
    )


def _context(tmp_path: Path) -> BatchContext:
    return BatchContext(
        project_root=tmp_path,
        ours_branch="master",
        theirs_branch="impl/feature",
        merge_base="abc1234",
        ours_head_sha="deadbee",
        theirs_head_sha="cafe123",
        strategy=MergeStrategy.FAST,
    )


def _build(tmp_path: Path, size_mb: int) -> str:
    resolver = ConflictResolver(project_root=tmp_path)
    return resolver._build_editor_prompt(
        [_conflict_file(size_mb)],
        _context(tmp_path),
        [],
        1,
        10,
    )


def test_prompt_is_bounded_regardless_of_file_size(tmp_path: Path) -> None:
    assert len(_build(tmp_path, 5)) < 20_000


def test_prompt_size_is_independent_of_content_size(tmp_path: Path) -> None:
    small = _build(tmp_path, 5)
    large = _build(tmp_path, 50)
    assert small == large


def test_prompt_omits_three_way_contents(tmp_path: Path) -> None:
    prompt = _build(tmp_path, 5)
    for sentinel in SENTINELS.values():
        assert sentinel not in prompt


def test_prompt_keeps_paths_hunks_and_git_show_hint(tmp_path: Path) -> None:
    prompt = _build(tmp_path, 5)
    assert str(tmp_path / "se3/code-index.md") in prompt
    assert "Lines 10-42" in prompt
    assert "Lines 100-137" in prompt
    assert "git show :2:" in prompt


def test_binary_file_branch_is_unchanged(tmp_path: Path) -> None:
    cf = _conflict_file(1)
    cf.is_binary = True
    resolver = ConflictResolver(project_root=tmp_path)
    prompt = resolver._build_editor_prompt([cf], _context(tmp_path), [], 1, 10)
    assert "[BINARY FILE" in prompt
    # `continue` short-circuits before the hunk list for binaries.
    assert "Lines 10-42" not in prompt
