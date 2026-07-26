"""Unit tests for the deterministic merge resolvers.

Everything here stays off real git: ``_git_show_stage`` and ``_actual_content_fp``
are monkeypatched, and the working tree is a handful of files under ``tmp_path``.
The git-backed behaviour (staging, unmerged-index bookkeeping) is covered by the
integration layer.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Sequence

import pytest

from tianluo.engine.merge import deterministic_resolvers as dr
from tianluo.engine.merge.deterministic_resolvers import (
    CodeIndexResolver,
    FileBlock,
    NextIdResolver,
    _parse_md_blocks,
    _render,
    resolve_deterministic,
)

MARKERS = ("<<<<<<<", "=======", ">>>>>>>")

FP_A = "1111111111111111"
FP_B = "2222222222222222"
FP_C = "3333333333333333"
LFP = "aaaaaaaaaaaaaaaa"


def _fp_comment(content_fp: str, list_fp: str = LFP) -> str:
    return f"<!--#{content_fp}|{list_fp}-->"


def _md(entries: list[str]) -> str:
    """Assemble a minimal but format-faithful code-index md."""
    lines = ["# Code Index", "", "## `(root)` — root summary <!--#dddd|eeee-->", ""]
    for entry in entries:
        lines.extend(entry.splitlines())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _entry(relpath: str, summary: str, content_fp: str, bullets: Sequence[str] = ()) -> str:
    head = f"### `{relpath}` (python) — {summary} {_fp_comment(content_fp)}"
    return "\n".join([head, *bullets])


def _block(
    content_fp: str | None,
    summary: str,
    list_fp: str | None = LFP,
    bullets: Sequence[str] = (),
) -> FileBlock:
    return FileBlock(
        relpath="x.py",
        heading_line=f"### `x.py` (python) — {summary}",
        bullet_lines=list(bullets),
        content_fp=content_fp,
        list_fp=list_fp,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# parsing + rendering
# ---------------------------------------------------------------------------

def test_parse_extracts_fingerprints_and_bullets():
    text = _md([_entry("a.py", "does a", FP_A, ["- `f` (function) — does f <!--#3333-->"])])
    dirs, files = _parse_md_blocks(text)

    assert set(dirs) == {"(root)"}
    block = files["a.py"]
    assert block.content_fp == FP_A
    assert block.list_fp == LFP
    assert block.summary == "does a"
    assert block.bullet_lines == ["- `f` (function) — does f <!--#3333-->"]


def test_render_is_deterministic_and_marker_free():
    text = _md([_entry("b.py", "does b", FP_B), _entry("a.py", "does a", FP_A)])
    first = _render(*_parse_md_blocks(text))
    second = _render(*_parse_md_blocks(text))

    assert first == second
    assert not any(marker in first for marker in MARKERS)
    # Entries are emitted in sorted relpath order regardless of input order.
    assert first.index("`a.py`") < first.index("`b.py`")


def test_roundtrip_on_repository_index_is_byte_identical():
    md_path = Path(__file__).resolve().parents[2] / "se3" / "code-index.md"
    if not md_path.exists():
        pytest.skip("se3/code-index.md has not been built in this checkout")
    source = md_path.read_text(encoding="utf-8")

    assert _render(*_parse_md_blocks(source)) == source


# ---------------------------------------------------------------------------
# the six adjudication rules
# ---------------------------------------------------------------------------

def test_pick_keeps_the_only_side_that_has_the_entry():
    ours, theirs = _block(FP_A, "ours"), _block(FP_B, "theirs")

    assert CodeIndexResolver._pick(ours, None, None) == (ours, "only-ours")
    assert CodeIndexResolver._pick(None, theirs, None) == (theirs, "only-theirs")


def test_pick_takes_theirs_when_both_sides_are_identical():
    ours, theirs = _block(FP_A, "same"), _block(FP_A, "same")

    assert CodeIndexResolver._pick(ours, theirs, None) == (theirs, "identical")


def test_pick_takes_the_side_whose_fingerprint_matches_the_worktree():
    ours, theirs = _block(FP_A, "ours"), _block(FP_B, "theirs")

    assert CodeIndexResolver._pick(ours, theirs, FP_A) == (ours, "ours-matches-worktree")
    assert CodeIndexResolver._pick(ours, theirs, FP_B) == (theirs, "theirs-matches-worktree")


def test_pick_falls_back_to_theirs_when_neither_side_matches_the_worktree():
    ours, theirs = _block(FP_A, "ours"), _block(FP_B, "theirs")

    assert CodeIndexResolver._pick(ours, theirs, FP_C) == (theirs, "neither-matches-worktree")
    assert CodeIndexResolver._pick(ours, theirs, None) == (theirs, "neither-matches-worktree")


def test_pick_keeps_theirs_symbols_whole_when_neither_side_matches_the_worktree():
    # Neither side describes the post-merge file, so both symbol lists are
    # stale; theirs is taken whole rather than mixed, keeping the output a pure
    # function of its inputs. The index's rebuild step fixes the staleness.
    ours = _block(FP_A, "ours", bullets=["- `f` (function) — f", "- `g_ours` (function) — g"])
    theirs = _block(FP_B, "theirs", bullets=["- `f` (function) — f", "- `h_theirs` (function) — h"])

    picked, reason = CodeIndexResolver._pick(ours, theirs, FP_C)

    assert (picked, reason) == (theirs, "neither-matches-worktree")
    assert picked.bullet_lines == ["- `f` (function) — f", "- `h_theirs` (function) — h"]


def test_pick_takes_the_matching_sides_symbols_whole():
    # ours' symbol list is exactly the post-merge file's; theirs' extra symbol
    # names something the file no longer has, so grafting it in would be a lie.
    ours = _block(FP_A, "ours", bullets=["- `f` (function) — f"])
    theirs = _block(FP_B, "theirs", bullets=["- `f` (function) — f", "- `stale` (function) — s"])

    picked, reason = CodeIndexResolver._pick(ours, theirs, FP_A)

    assert (picked, reason) == (ours, "ours-matches-worktree")


def test_pick_takes_theirs_when_fingerprints_agree_but_summaries_differ():
    ours, theirs = _block(FP_A, "ours wording"), _block(FP_A, "theirs wording")

    assert CodeIndexResolver._pick(ours, theirs, None) == (theirs, "same-fp-different-summary")


# ---------------------------------------------------------------------------
# CodeIndexResolver.resolve
# ---------------------------------------------------------------------------

def _stage_stub(ours_md: str | None, theirs_md: str | None):
    def _show(project_root, stage, relpath):
        return {dr.STAGE_OURS: ours_md, dr.STAGE_THEIRS: theirs_md}[stage]

    return _show


def test_resolve_unions_entries_and_drops_those_whose_file_vanished(tmp_path, monkeypatch):
    for name in ("only_ours.py", "only_theirs.py", "both.py", "differ.py"):
        (tmp_path / name).write_text("x")

    ours_md = _md([
        _entry("only_ours.py", "ours only", FP_A, ["- `f` (function) — f <!--#3333-->"]),
        _entry("both.py", "shared", FP_B),
        _entry("differ.py", "ours wording", FP_A),
        _entry("gone.py", "deleted by the merge", FP_C),
    ])
    theirs_md = _md([
        _entry("only_theirs.py", "theirs only", FP_A),
        _entry("both.py", "shared", FP_B),
        _entry("differ.py", "theirs wording", FP_B),
        _entry("gone.py", "deleted by the merge", FP_C),
    ])
    monkeypatch.setattr(dr, "_git_show_stage", _stage_stub(ours_md, theirs_md))
    monkeypatch.setattr(dr, "_actual_content_fp", lambda root, rel: FP_A)

    merged = CodeIndexResolver().resolve(tmp_path, dr.CODE_INDEX_RELPATH)
    _, files = _parse_md_blocks(merged)

    assert set(files) == {"only_ours.py", "only_theirs.py", "both.py", "differ.py"}
    # `gone.py` has no working-tree file left, so its entry (and its bullets) go too.
    assert "gone.py" not in merged
    # Worktree fp is FP_A, so ours wins the disagreement over `differ.py`.
    assert files["differ.py"].summary == "ours wording"
    assert files["only_ours.py"].bullet_lines == ["- `f` (function) — f <!--#3333-->"]
    assert not any(marker in merged for marker in MARKERS)


def test_resolve_is_deterministic(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x")
    ours_md = _md([_entry("a.py", "ours", FP_A)])
    theirs_md = _md([_entry("a.py", "theirs", FP_B)])
    monkeypatch.setattr(dr, "_git_show_stage", _stage_stub(ours_md, theirs_md))
    monkeypatch.setattr(dr, "_actual_content_fp", lambda root, rel: FP_C)

    resolver = CodeIndexResolver()
    first = resolver.resolve(tmp_path, dr.CODE_INDEX_RELPATH)

    assert first == resolver.resolve(tmp_path, dr.CODE_INDEX_RELPATH)
    assert "theirs" in first


def test_resolve_survives_a_missing_side(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x")
    monkeypatch.setattr(dr, "_git_show_stage", _stage_stub(_md([_entry("a.py", "ours", FP_A)]), None))

    merged = CodeIndexResolver().resolve(tmp_path, dr.CODE_INDEX_RELPATH)

    assert "a.py" in merged


def test_resolve_raises_when_both_sides_are_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(dr, "_git_show_stage", _stage_stub(None, None))

    with pytest.raises(ValueError):
        CodeIndexResolver().resolve(tmp_path, dr.CODE_INDEX_RELPATH)


def test_code_index_resolver_matches_only_its_own_path():
    resolver = CodeIndexResolver()

    assert resolver.matches("se3/code-index.md")
    assert not resolver.matches("se3/code-index.md.bak")
    assert not resolver.matches("docs/code-index.md")


# ---------------------------------------------------------------------------
# NextIdResolver
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "ours, theirs, expected",
    [
        ("281", "285", "285\n"),
        ("290", "285", "290\n"),
        ("  281  \n", "285\n", "285\n"),
        ("", "285", "285\n"),
        ("not-a-number", "285", "285\n"),
        (None, "285", "285\n"),
        ("285", None, "285\n"),
    ],
)
def test_next_id_resolver_takes_the_maximum(tmp_path, monkeypatch, ours, theirs, expected):
    monkeypatch.setattr(dr, "_git_show_stage", _stage_stub(ours, theirs))

    assert NextIdResolver().resolve(tmp_path, dr.NEXT_ID_RELPATH) == expected


@pytest.mark.parametrize("ours, theirs", [(None, None), ("", "  "), ("x", None)])
def test_next_id_resolver_raises_when_no_side_is_usable(tmp_path, monkeypatch, ours, theirs):
    # Staging a value that came from neither side would reissue allocated IDs.
    monkeypatch.setattr(dr, "_git_show_stage", _stage_stub(ours, theirs))

    with pytest.raises(ValueError):
        NextIdResolver().resolve(tmp_path, dr.NEXT_ID_RELPATH)


def test_next_id_resolver_propagates_git_failure(tmp_path, monkeypatch):
    # A git failure must not masquerade as an absent side (which would silently
    # drop the larger counter); it has to reach resolve_deterministic's fallback.
    def boom(project_root, stage, relpath):
        raise subprocess.TimeoutExpired(cmd="git show", timeout=120)

    monkeypatch.setattr(dr, "_git_show_stage", boom)

    with pytest.raises(subprocess.TimeoutExpired):
        NextIdResolver().resolve(tmp_path, dr.NEXT_ID_RELPATH)


def test_resolve_deterministic_falls_back_when_git_show_fails(tmp_path, monkeypatch):
    def boom(project_root, stage, relpath):
        raise OSError("git unavailable")

    monkeypatch.setattr(dr, "_git_show_stage", boom)

    outcome = dr.resolve_deterministic(tmp_path, [dr.NEXT_ID_RELPATH])

    assert outcome.resolved == []
    assert outcome.remaining == [dr.NEXT_ID_RELPATH]
    assert dr.NEXT_ID_RELPATH in outcome.failures


def test_registry_holds_two_resolvers_with_disjoint_matches():
    assert len(dr.REGISTRY) == 2
    for path in (dr.CODE_INDEX_RELPATH, dr.NEXT_ID_RELPATH):
        assert len([r for r in dr.REGISTRY if r.matches(path)]) == 1


# ---------------------------------------------------------------------------
# dispatch: resolve_deterministic
# ---------------------------------------------------------------------------

class _StubResolver:
    name = "stub"

    def __init__(self, relpath: str, output: str | Exception):
        self._relpath = relpath
        self._output = output

    def matches(self, relpath: str) -> bool:
        return relpath == self._relpath

    def resolve(self, project_root: Path, relpath: str) -> str:
        if isinstance(self._output, Exception):
            raise self._output
        return self._output


@pytest.fixture
def git_calls(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(dr, "_git_add", lambda root, rel: calls.append(("add", rel)))
    monkeypatch.setattr(dr, "_git_rm", lambda root, rel: calls.append(("rm", rel)))
    return calls


def test_unmatched_paths_are_left_for_the_llm(tmp_path, monkeypatch, git_calls):
    monkeypatch.setattr(dr, "REGISTRY", [])

    outcome = resolve_deterministic(tmp_path, ["src/a.py", "src/b.py"])

    assert outcome.resolved == []
    assert outcome.remaining == ["src/a.py", "src/b.py"]
    assert outcome.failures == {}
    assert git_calls == []


def test_resolved_path_is_written_and_staged(tmp_path, monkeypatch, git_calls):
    (tmp_path / "counter").write_text("<<<<<<< ours\n1\n=======\n2\n>>>>>>> theirs\n")
    monkeypatch.setattr(dr, "REGISTRY", [_StubResolver("counter", "2\n")])

    outcome = resolve_deterministic(tmp_path, ["counter", "src/a.py"])

    assert outcome.resolved == ["counter"]
    assert outcome.remaining == ["src/a.py"]
    assert (tmp_path / "counter").read_text() == "2\n"
    assert git_calls == [("add", "counter")]


def test_empty_output_deletes_the_path(tmp_path, monkeypatch, git_calls):
    (tmp_path / "gone").write_text("conflicted")
    monkeypatch.setattr(dr, "REGISTRY", [_StubResolver("gone", "")])

    outcome = resolve_deterministic(tmp_path, ["gone"])

    assert outcome.resolved == ["gone"]
    assert git_calls == [("rm", "gone")]


def test_a_raising_resolver_degrades_to_the_llm_without_staging(tmp_path, monkeypatch, git_calls):
    conflicted = "<<<<<<< ours\n1\n=======\n2\n>>>>>>> theirs\n"
    (tmp_path / "boom").write_text(conflicted)
    monkeypatch.setattr(dr, "REGISTRY", [_StubResolver("boom", RuntimeError("kaboom"))])

    outcome = resolve_deterministic(tmp_path, ["boom"])

    assert outcome.resolved == []
    assert outcome.remaining == ["boom"]
    assert "kaboom" in outcome.failures["boom"]
    assert git_calls == []
    assert (tmp_path / "boom").read_text() == conflicted


def test_output_with_conflict_markers_is_never_staged(tmp_path, monkeypatch, git_calls):
    conflicted = "<<<<<<< ours\n1\n=======\n2\n>>>>>>> theirs\n"
    (tmp_path / "half").write_text(conflicted)
    monkeypatch.setattr(dr, "REGISTRY", [_StubResolver("half", "1\n=======\n2\n")])

    outcome = resolve_deterministic(tmp_path, ["half"])

    assert outcome.remaining == ["half"]
    assert "conflict markers" in outcome.failures["half"]
    assert git_calls == []
    assert (tmp_path / "half").read_text() == conflicted


def test_a_failed_stage_restores_the_conflicted_working_tree(tmp_path, monkeypatch, git_calls):
    conflicted = "<<<<<<< ours\n1\n=======\n2\n>>>>>>> theirs\n"
    (tmp_path / "counter").write_text(conflicted)
    monkeypatch.setattr(dr, "REGISTRY", [_StubResolver("counter", "2\n")])

    def _boom(root, rel):
        raise RuntimeError("git add exploded")

    monkeypatch.setattr(dr, "_git_add", _boom)

    outcome = resolve_deterministic(tmp_path, ["counter"])

    assert outcome.remaining == ["counter"]
    # The LLM must inherit the original conflict, not a resolved-but-unstaged file.
    assert (tmp_path / "counter").read_text() == conflicted


def test_a_failed_stage_removes_the_file_the_conflict_never_had(tmp_path, monkeypatch, git_calls):
    # A rename/delete-shaped conflict leaves no working-tree copy.  A resolved
    # file left behind after a failed stage would read as an already-handled
    # conflict while the index entry is still unmerged.
    monkeypatch.setattr(dr, "REGISTRY", [_StubResolver("ghost", "merged\n")])

    def _boom(root, rel):
        raise RuntimeError("git add exploded")

    monkeypatch.setattr(dr, "_git_add", _boom)

    outcome = resolve_deterministic(tmp_path, ["ghost"])

    assert outcome.remaining == ["ghost"]
    assert not (tmp_path / "ghost").exists()


def test_resolved_and_remaining_partition_the_input_in_order(tmp_path, monkeypatch, git_calls):
    (tmp_path / "ok").write_text("x")
    monkeypatch.setattr(dr, "REGISTRY", [_StubResolver("ok", "merged\n")])
    paths = ["a", "ok", "b", "c"]

    outcome = resolve_deterministic(tmp_path, paths)

    assert outcome.resolved == ["ok"]
    assert outcome.remaining == ["a", "b", "c"]
    assert sorted(outcome.resolved + outcome.remaining) == sorted(paths)


# ---------------------------------------------------------------------------
# integration: a real git repository, a real conflicted merge
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    import subprocess

    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )
    return result.stdout


def _real_fp(text: str) -> str:
    """The content fingerprint code_index would record for a file of *text*."""
    from tianluo.engine.code_index import _fp, _sha256_prefix

    return _fp(_sha256_prefix(text.encode("utf-8")))


def _index_md(dir_summary: str, entries: list[tuple[str, str, str]]) -> str:
    """Render a code-index md for ``(relpath, summary, content_fp)`` entries."""
    lines = ["# Code Index", "", f"## `src/` — {dir_summary} <!--#dddd|eeee-->", ""]
    for relpath, summary, content_fp in sorted(entries):
        lines.append(
            f"### `{relpath}` (python) — {summary} <!--#{content_fp}|{LFP}-->"
        )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _write(repo: Path, relpath: str, text: str) -> None:
    path = repo / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


KEEP_SRC = "def keep():\n    return 1\n"
GONE_SRC = "def gone():\n    return 0\n"
EDIT_BASE_SRC = "def edit():\n    return 'base'\n"
EDIT_OURS_SRC = "def edit():\n    return 'ours'\n"
ADDED_OURS_SRC = "def added_ours():\n    pass\n"
ADDED_THEIRS_SRC = "def added_theirs():\n    pass\n"


@pytest.fixture
def conflicted_repo(tmp_path: Path) -> Path:
    """A repo mid-merge, with both deterministic files genuinely conflicted.

    The two sides regenerate ``se3/code-index.md`` the way the real index step
    does — every entry rewritten, the dir heading reworded — so git's textual
    merge conflicts even though the entries themselves merge cleanly. That is
    the exact shape of the failure this resolver exists to absorb.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")

    # --- base ---
    for relpath, src in [
        ("src/keep.py", KEEP_SRC),
        ("src/gone.py", GONE_SRC),
        ("src/edit.py", EDIT_BASE_SRC),
    ]:
        _write(repo, relpath, src)
    _write(repo, "se3/issues/.next_id", "5\n")
    _write(repo, "se3/code-index.md", _index_md("base dir", [
        ("src/keep.py", "keeps things", _real_fp(KEEP_SRC)),
        ("src/gone.py", "will vanish", _real_fp(GONE_SRC)),
        ("src/edit.py", "base wording", _real_fp(EDIT_BASE_SRC)),
    ]))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "branch", "feature")

    # --- ours (master): edits edit.py, adds a file, bumps the counter ---
    _write(repo, "src/edit.py", EDIT_OURS_SRC)
    _write(repo, "src/added_ours.py", ADDED_OURS_SRC)
    _write(repo, "se3/issues/.next_id", "9\n")
    _write(repo, "se3/code-index.md", _index_md("ours dir wording", [
        ("src/keep.py", "keeps things (ours wording)", _real_fp(KEEP_SRC)),
        ("src/gone.py", "will vanish", _real_fp(GONE_SRC)),
        ("src/edit.py", "returns ours", _real_fp(EDIT_OURS_SRC)),
        ("src/added_ours.py", "added on ours", _real_fp(ADDED_OURS_SRC)),
    ]))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "ours")

    # --- theirs: deletes gone.py, adds a file, bumps the counter higher ---
    _git(repo, "checkout", "-q", "feature")
    (repo / "src/gone.py").unlink()
    _write(repo, "src/added_theirs.py", ADDED_THEIRS_SRC)
    _write(repo, "se3/issues/.next_id", "12\n")
    _write(repo, "se3/code-index.md", _index_md("theirs dir wording", [
        ("src/keep.py", "keeps things (theirs wording)", _real_fp(KEEP_SRC)),
        # A stale entry for edit.py: theirs never saw the ours-side edit.
        ("src/edit.py", "base wording", _real_fp(EDIT_BASE_SRC)),
        ("src/added_theirs.py", "added on theirs", _real_fp(ADDED_THEIRS_SRC)),
    ]))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "theirs")

    _git(repo, "checkout", "-q", "master")
    import subprocess

    merge = subprocess.run(
        ["git", "-C", str(repo), "merge", "feature"],
        capture_output=True, text=True,
    )
    assert merge.returncode != 0, "fixture must produce a real conflict"
    return repo


@pytest.fixture
def no_llm(monkeypatch):
    """Any LLM call from this point on is a test failure."""
    from tianluo.engine.llm_caller import LLMCaller

    def _forbidden(*args, **kwargs):
        raise AssertionError("the LLM must not be called for deterministic conflicts")

    monkeypatch.setattr(LLMCaller, "call", _forbidden)


def test_real_merge_conflict_is_resolved_without_the_llm(conflicted_repo, no_llm):
    from tianluo.engine.worktree import get_conflicting_files

    repo = conflicted_repo
    conflicts = get_conflicting_files(repo)
    assert set(conflicts) == {"se3/code-index.md", "se3/issues/.next_id"}

    outcome = resolve_deterministic(repo, conflicts)

    assert sorted(outcome.resolved) == ["se3/code-index.md", "se3/issues/.next_id"]
    assert outcome.remaining == []
    assert outcome.failures == {}
    # Both paths left git's index: nothing is unmerged, so a merge commit is possible.
    assert _git(repo, "diff", "--name-only", "--diff-filter=U").strip() == ""


def test_resolved_index_unions_entries_and_drops_the_deleted_file(conflicted_repo, no_llm):
    from tianluo.engine.worktree import get_conflicting_files

    repo = conflicted_repo
    resolve_deterministic(repo, get_conflicting_files(repo))
    merged = (repo / "se3/code-index.md").read_text(encoding="utf-8")

    assert not any(marker in merged for marker in MARKERS)
    # Single-sided entries survive from both sides.
    assert "`src/added_ours.py`" in merged
    assert "`src/added_theirs.py`" in merged
    # The file that did not survive the merge takes its entry with it.
    assert "`src/gone.py`" not in merged
    # edit.py: the fingerprints disagree, and it is ours that matches the
    # post-merge working tree — so ours wins over the stale theirs entry.
    assert "returns ours" in merged
    assert "base wording" not in merged
    assert _real_fp(EDIT_OURS_SRC) in merged
    # keep.py: same fingerprint, different regenerated wording → fixed on theirs.
    assert "keeps things (theirs wording)" in merged
    assert "keeps things (ours wording)" not in merged
    # A dir-heading disagreement is likewise settled on theirs.
    assert "theirs dir wording" in merged


def test_staged_index_matches_the_working_tree(conflicted_repo, no_llm):
    from tianluo.engine.worktree import get_conflicting_files

    repo = conflicted_repo
    resolve_deterministic(repo, get_conflicting_files(repo))

    staged = _git(repo, "show", ":se3/code-index.md")
    assert staged == (repo / "se3/code-index.md").read_text(encoding="utf-8")


def test_next_id_takes_the_larger_counter(conflicted_repo, no_llm):
    from tianluo.engine.worktree import get_conflicting_files

    repo = conflicted_repo
    resolve_deterministic(repo, get_conflicting_files(repo))

    assert (repo / "se3/issues/.next_id").read_text(encoding="utf-8") == "12\n"


def test_the_merge_commits_cleanly_after_deterministic_resolution(conflicted_repo, no_llm):
    from tianluo.engine.worktree import get_conflicting_files

    repo = conflicted_repo
    resolve_deterministic(repo, get_conflicting_files(repo))
    _git(repo, "commit", "--no-edit", "-m", "Merge branch 'feature'")

    # A real merge commit: two parents, and both sides' files in the tree.
    parents = _git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()
    assert len(parents) == 3
    assert (repo / "src/added_theirs.py").exists()
    assert not (repo / "src/gone.py").exists()


# ---------------------------------------------------------------------------
# MergeOrchestrator._handle_conflict — the deterministic short-circuit
#
# The production failure this whole module exists for was a merge whose only
# conflict was se3/code-index.md.  These tests drive the orchestrator through
# that exact shape, so a regression in the short-circuit condition (or an
# early-exit added to _apply_resolution) cannot silently route it back to the
# LLM.
# ---------------------------------------------------------------------------

@pytest.fixture
def orchestrator_repo(conflicted_repo, monkeypatch):
    """``conflicted_repo`` with the LLM, guardrails and context-build disarmed."""
    from tianluo.engine.merge import orchestrator as orch_mod

    # The short-circuit must return before any conflict context is built: that
    # call reads merge metadata whose failure would abort a merge with nothing
    # left to inspect.
    def _forbidden_context(*args, **kwargs):
        raise AssertionError("conflict context must not be built")

    monkeypatch.setattr(orch_mod, "build_conflict_context", _forbidden_context)
    monkeypatch.setattr(
        orch_mod.MergeOrchestrator, "_run_guardrails", lambda *a, **k: None
    )
    return conflicted_repo


def _handle_conflict(repo: Path, strategy: str):
    from tianluo.commands.merge.result_model import MergeReport
    from tianluo.engine.merge.orchestrator import MergeOrchestrator

    pre_merge_sha = _git(repo, "rev-parse", "HEAD").strip()
    orch = MergeOrchestrator(
        repo, strategy=strategy, delete_merged=False, acquire_lock=False
    )
    report = MergeReport()
    result = orch._handle_conflict("feature", pre_merge_sha, report)
    return result, report


@pytest.mark.parametrize("strategy", ["fast", "safe", "strict"])
def test_a_fully_deterministic_conflict_merges_without_the_llm(
    orchestrator_repo, no_llm, strategy
):
    repo = orchestrator_repo

    result, report = _handle_conflict(repo, strategy)

    assert result == "merged"
    # Even STRICT commits: its contract is that *contended content* reaches a
    # human, and a regenerated index carries no decision to review.
    assert report.human_call_file is None
    # A real two-parent merge commit, with both sides' work in the tree.
    parents = _git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()
    assert len(parents) == 3
    assert (repo / "src/added_theirs.py").exists()
    assert (repo / "src/added_ours.py").exists()
    assert _git(repo, "diff", "--name-only", "--diff-filter=U").strip() == ""
    assert (repo / "se3/issues/.next_id").read_text(encoding="utf-8") == "12\n"
    merged_index = (repo / "se3/code-index.md").read_text(encoding="utf-8")
    assert not any(marker in merged_index for marker in MARKERS)


def test_a_leftover_conflict_still_reaches_the_context_builder(orchestrator_repo, monkeypatch):
    """One non-deterministic path is enough to keep the whole LLM path alive."""
    from tianluo.engine.merge import orchestrator as orch_mod

    repo = orchestrator_repo
    # Make a third path conflict so ``remaining`` is non-empty.
    _write(repo, "src/extra.py", "<<<<<<< HEAD\na\n=======\nb\n>>>>>>> feature\n")

    seen: dict = {}

    def _record(project_root, ours, theirs, **kwargs):
        seen["conflict_files"] = kwargs.get("conflict_files")
        raise RuntimeError("stop here — the context builder was reached")

    monkeypatch.setattr(orch_mod, "build_conflict_context", _record)
    monkeypatch.setattr(
        orch_mod.MergeOrchestrator, "_resolve_deterministic_conflicts",
        lambda self: dr.DeterministicOutcome(
            resolved=["se3/issues/.next_id"], remaining=["src/extra.py"]
        ),
    )

    result, _report = _handle_conflict(repo, "fast")

    assert result == "fast_abort"
    assert seen["conflict_files"] == ["src/extra.py"]


@pytest.mark.parametrize("broken", ["get_conflicting_files", "resolve_deterministic"])
def test_a_broken_deterministic_pass_degrades_to_the_pre_change_behaviour(
    orchestrator_repo, monkeypatch, broken
):
    """A crash anywhere in the pass must leave the LLM path exactly as it was.

    The deterministic layer is an optimisation in front of the LLM, never a
    precondition for it, so ``conflict_files=None`` (let ``build`` enumerate
    the conflicts itself) is the required hand-off — not a half-filled outcome,
    and not an ``AttributeError`` on ``None.remaining``.
    """
    from tianluo.engine.merge import orchestrator as orch_mod

    repo = orchestrator_repo

    def _boom(*args, **kwargs):
        raise RuntimeError("deterministic pass exploded")

    monkeypatch.setattr(orch_mod, broken, _boom)

    seen: dict = {}

    def _record(project_root, ours, theirs, **kwargs):
        seen["conflict_files"] = kwargs.get("conflict_files", "<absent>")
        raise RuntimeError("stop here — the context builder was reached")

    monkeypatch.setattr(orch_mod, "build_conflict_context", _record)

    orch = orch_mod.MergeOrchestrator(
        repo, strategy="fast", delete_merged=False, acquire_lock=False
    )
    assert orch._resolve_deterministic_conflicts() is None

    result, _report = _handle_conflict(repo, "fast")

    assert result == "fast_abort"
    # Reached the builder (a missing key would KeyError) and was handed the
    # "enumerate them yourself" sentinel rather than a half-filled outcome.
    assert seen["conflict_files"] is None
