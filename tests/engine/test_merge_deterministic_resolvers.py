"""Unit tests for the deterministic merge resolvers.

Everything here stays off real git: ``_git_show_stage`` and ``_actual_content_fp``
are monkeypatched, and the working tree is a handful of files under ``tmp_path``.
The git-backed behaviour (staging, unmerged-index bookkeeping) is covered by the
integration layer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import pytest

from se3.engine.merge import deterministic_resolvers as dr
from se3.engine.merge.deterministic_resolvers import (
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


def _block(content_fp: str | None, summary: str, list_fp: str | None = LFP) -> FileBlock:
    return FileBlock(
        relpath="x.py",
        heading_line=f"### `x.py` (python) — {summary}",
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
        (None, None, "0\n"),
    ],
)
def test_next_id_resolver_takes_the_maximum(tmp_path, monkeypatch, ours, theirs, expected):
    monkeypatch.setattr(dr, "_git_show_stage", _stage_stub(ours, theirs))

    assert NextIdResolver().resolve(tmp_path, dr.NEXT_ID_RELPATH) == expected


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


def test_resolved_and_remaining_partition_the_input_in_order(tmp_path, monkeypatch, git_calls):
    (tmp_path / "ok").write_text("x")
    monkeypatch.setattr(dr, "REGISTRY", [_StubResolver("ok", "merged\n")])
    paths = ["a", "ok", "b", "c"]

    outcome = resolve_deterministic(tmp_path, paths)

    assert outcome.resolved == ["ok"]
    assert outcome.remaining == ["a", "b", "c"]
    assert sorted(outcome.resolved + outcome.remaining) == sorted(paths)
