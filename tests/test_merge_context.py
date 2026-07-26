"""Tests for ConflictContextBuilder three-way merge context collection."""

from __future__ import annotations

import subprocess
from pathlib import Path

from tianluo.engine.merge.conflict_context import (
    ConflictContext,
    _is_spec_path,
    _looks_binary,
    _parse_hunks,
    build,
)


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


def _setup_basic_conflict(
    tmp_path: Path,
    rel_path: str = "shared.txt",
    base_content: str = "line1\nline2\nline3\n",
    ours_content: str = "line1\nOURS\nline3\n",
    theirs_content: str = "line1\nTHEIRS\nline3\n",
    base_message: str = "base",
) -> tuple[str, str]:
    """Create a repo with a single-file single-hunk conflict.

    Returns (default_branch, theirs_branch). The merge is left in
    conflicting state so the caller can call build().
    """
    _init_repo(tmp_path)
    (tmp_path / rel_path).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / rel_path).write_text(base_content)
    _commit(tmp_path, base_message)
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


# --------- pure-function unit tests ---------


class TestPureHelpers:
    def test_is_spec_path_matches_spec_md(self) -> None:
        assert _is_spec_path("tianluo/specs/auth/spec.md") is True
        assert _is_spec_path("tianluo/specs/nested/deep/spec.md") is True

    def test_is_spec_path_rejects_non_spec(self) -> None:
        assert _is_spec_path("tianluo/specs/auth/notes.md") is False
        assert _is_spec_path("src/foo.py") is False
        assert _is_spec_path("tianluo/state/foo.json") is False
        assert _is_spec_path("tianluo/specs/spec.md") is False  # missing dir

    def test_looks_binary_detects_null_bytes(self) -> None:
        assert _looks_binary(b"hello\x00world") is True
        assert _looks_binary(b"hello world\n") is False
        assert _looks_binary(b"") is False

    def test_parse_hunks_single_hunk(self) -> None:
        text = "a\n<<<<<<< HEAD\nfoo\n=======\nbar\n>>>>>>> branch\nb\n"
        hunks = _parse_hunks(text)
        assert len(hunks) == 1
        assert hunks[0].start_line == 2
        assert hunks[0].end_line == 6

    def test_parse_hunks_multiple_hunks(self) -> None:
        text = (
            "a\n"
            "<<<<<<< HEAD\n"  # line 2
            "x\n=======\ny\n"
            ">>>>>>> branch\n"  # line 6
            "middle\n"
            "<<<<<<< HEAD\n"  # line 8
            "p\n=======\nq\n"
            ">>>>>>> branch\n"  # line 12
            "end\n"
        )
        hunks = _parse_hunks(text)
        assert len(hunks) == 2
        assert (hunks[0].start_line, hunks[0].end_line) == (2, 6)
        assert (hunks[1].start_line, hunks[1].end_line) == (8, 12)

    def test_parse_hunks_diff3_marker_inside(self) -> None:
        text = (
            "<<<<<<< HEAD\n"
            "ours\n"
            "||||||| base\n"
            "base\n"
            "=======\n"
            "theirs\n"
            ">>>>>>> branch\n"
        )
        hunks = _parse_hunks(text)
        assert len(hunks) == 1
        assert hunks[0].start_line == 1
        assert hunks[0].end_line == 7

    def test_parse_hunks_no_markers(self) -> None:
        assert _parse_hunks("just regular text\nno markers here\n") == []


# --------- integration tests against a real git repo ---------


class TestBuildSingleFileSingleHunk:
    def test_three_way_contents_non_empty(self, tmp_path: Path) -> None:
        ours_branch, theirs_branch = _setup_basic_conflict(tmp_path)
        ctx = build(tmp_path, ours_branch, theirs_branch)

        assert isinstance(ctx, ConflictContext)
        assert len(ctx.files) == 1
        cf = ctx.files[0]
        assert cf.path == "shared.txt"
        assert cf.base_content == "line1\nline2\nline3\n"
        assert cf.ours_content == "line1\nOURS\nline3\n"
        assert cf.theirs_content == "line1\nTHEIRS\nline3\n"
        assert cf.working_content != ""
        assert "<<<<<<<" in cf.working_content
        assert "=======" in cf.working_content
        assert ">>>>>>>" in cf.working_content
        assert cf.is_binary is False
        assert cf.is_spec is False

    def test_hunk_line_numbers_recorded(self, tmp_path: Path) -> None:
        ours_branch, theirs_branch = _setup_basic_conflict(tmp_path)
        ctx = build(tmp_path, ours_branch, theirs_branch)

        cf = ctx.files[0]
        assert len(cf.hunks) == 1
        assert cf.hunks[0].start_line >= 1
        assert cf.hunks[0].end_line > cf.hunks[0].start_line

        lines = cf.working_content.splitlines()
        start_idx = cf.hunks[0].start_line - 1
        end_idx = cf.hunks[0].end_line - 1
        assert lines[start_idx].startswith("<<<<<<<")
        assert lines[end_idx].startswith(">>>>>>>")

    def test_merge_metadata_populated(self, tmp_path: Path) -> None:
        ours_branch, theirs_branch = _setup_basic_conflict(tmp_path)
        ctx = build(tmp_path, ours_branch, theirs_branch)

        assert ctx.ours_branch == ours_branch
        assert ctx.theirs_branch == theirs_branch
        assert len(ctx.merge_base) == 40
        assert len(ctx.ours_head_sha) == 40
        assert len(ctx.theirs_head_sha) == 40
        assert ctx.ours_head_sha != ctx.theirs_head_sha
        assert "ours change" in ctx.ours_head_message
        assert "theirs change" in ctx.theirs_head_message

    def test_oneline_logs_collected(self, tmp_path: Path) -> None:
        ours_branch, theirs_branch = _setup_basic_conflict(tmp_path)
        ctx = build(tmp_path, ours_branch, theirs_branch)

        assert any("ours change" in line for line in ctx.ours_log_oneline)
        assert any("theirs change" in line for line in ctx.theirs_log_oneline)


class TestBuildSpecFile:
    def test_spec_md_path_marked_is_spec(self, tmp_path: Path) -> None:
        spec_path = "tianluo/specs/example/spec.md"
        ours_branch, theirs_branch = _setup_basic_conflict(
            tmp_path,
            rel_path=spec_path,
            base_content="### Requirement: Foo\n- SHALL do X\n",
            ours_content="### Requirement: Foo\n- SHALL do X strictly\n",
            theirs_content="### Requirement: Foo\n- SHALL do X gently\n",
        )
        ctx = build(tmp_path, ours_branch, theirs_branch)

        assert len(ctx.files) == 1
        cf = ctx.files[0]
        assert cf.path == spec_path
        assert cf.is_spec is True
        assert ctx.has_spec_files is True


class TestBuildBinaryFile:
    def test_binary_conflict_marked_is_binary(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        rel = "image.bin"
        (tmp_path / rel).write_bytes(b"\x00\x01\x02\x03base\x00\x00\x00")
        _commit(tmp_path, "base")
        default = _current_branch(tmp_path)

        _git(tmp_path, "checkout", "-b", "theirs-branch")
        (tmp_path / rel).write_bytes(b"\x00\x01\x02\x03theirs\x00\x00\x00")
        _commit(tmp_path, "theirs binary change")

        _git(tmp_path, "checkout", default)
        (tmp_path / rel).write_bytes(b"\x00\x01\x02\x03ours\x00\x00\x00")
        _commit(tmp_path, "ours binary change")

        result = _git(tmp_path, "merge", "theirs-branch", "--no-edit", check=False)
        assert result.returncode != 0

        ctx = build(tmp_path, default, "theirs-branch")
        assert len(ctx.files) == 1
        cf = ctx.files[0]
        assert cf.path == rel
        assert cf.is_binary is True
        assert cf.hunks == []


class TestBuildMultipleFilesMultipleHunks:
    def test_two_files_three_hunks_total(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)

        # Long enough file that top + bottom changes form two separate hunks
        file_a_lines = [f"a-line-{i}\n" for i in range(1, 31)]
        file_a_base = "".join(file_a_lines)
        file_b_lines = [f"b-line-{i}\n" for i in range(1, 11)]
        file_b_base = "".join(file_b_lines)

        (tmp_path / "a.txt").write_text(file_a_base)
        (tmp_path / "b.txt").write_text(file_b_base)
        _commit(tmp_path, "base")
        default = _current_branch(tmp_path)

        def _modify_a(prefix: str) -> str:
            lines = file_a_lines[:]
            lines[1] = f"a-line-2-{prefix}\n"
            lines[28] = f"a-line-29-{prefix}\n"
            return "".join(lines)

        def _modify_b(prefix: str) -> str:
            lines = file_b_lines[:]
            lines[4] = f"b-line-5-{prefix}\n"
            return "".join(lines)

        _git(tmp_path, "checkout", "-b", "theirs-branch")
        (tmp_path / "a.txt").write_text(_modify_a("T"))
        (tmp_path / "b.txt").write_text(_modify_b("T"))
        _commit(tmp_path, "theirs")

        _git(tmp_path, "checkout", default)
        (tmp_path / "a.txt").write_text(_modify_a("O"))
        (tmp_path / "b.txt").write_text(_modify_b("O"))
        _commit(tmp_path, "ours")

        result = _git(tmp_path, "merge", "theirs-branch", "--no-edit", check=False)
        assert result.returncode != 0

        ctx = build(tmp_path, default, "theirs-branch")

        assert len(ctx.files) == 2
        files_by_path = {cf.path: cf for cf in ctx.files}
        assert "a.txt" in files_by_path
        assert "b.txt" in files_by_path

        a = files_by_path["a.txt"]
        b = files_by_path["b.txt"]
        # a.txt has two distinct conflict regions, b.txt has one
        assert len(a.hunks) == 2
        assert len(b.hunks) == 1

        total_hunks = sum(len(cf.hunks) for cf in ctx.files)
        assert total_hunks == 3

        # Hunk line numbers must map to actual conflict markers
        for cf in ctx.files:
            lines = cf.working_content.splitlines()
            for hunk in cf.hunks:
                assert lines[hunk.start_line - 1].startswith("<<<<<<<")
                assert lines[hunk.end_line - 1].startswith(">>>>>>>")
                assert hunk.end_line > hunk.start_line

    def test_no_spec_files_flag_false(self, tmp_path: Path) -> None:
        ours_branch, theirs_branch = _setup_basic_conflict(tmp_path)
        ctx = build(tmp_path, ours_branch, theirs_branch)
        assert ctx.has_spec_files is False
