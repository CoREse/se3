"""Tests for scripts/cleanup_history_leak.py (one-time history-leak cleanup).

Locks in the classification logic so the destructive script can never grow to
delete a real flow: only empty dirs, single-``.jsonl`` (empty-step_id) leak dirs
regardless of file content, and explicit test-fixture names are removed; flow-id
/ recovered_ / old uuid-style multi-step dirs are preserved.

A real flow always names its step files ``<step_id>.jsonl`` with a non-empty
``step_id``, so a directory whose sole file is a bare ``.jsonl`` is unambiguously
the leak no matter what that file contains -- an empty / truncated /
signature-less leak file must still be removed, never kept.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "cleanup_history_leak.py"
)
_spec = importlib.util.spec_from_file_location("cleanup_history_leak", _SCRIPT)
cleanup_mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(cleanup_mod)


_LEAK_LINE = (
    '{"role": "user", "content": "Test execution (fix iteration 0): '
    'pytest", "step_type": "test", "attempt": 0}\n'
)


def _mk(root: Path, name: str, files: dict[str, str] | None) -> Path:
    """Create a history subdir; ``files=None`` means an empty dir."""
    d = root / name
    d.mkdir()
    for fname, content in (files or {}).items():
        (d / fname).write_text(content, encoding="utf-8")
    return d


def _build_history(tmp_path: Path) -> Path:
    hist = tmp_path / "history"
    hist.mkdir()
    # --- leaks (should be removed) ---
    _mk(hist, "20260706-013803_deadbeef", None)  # empty flow-id dir
    _mk(hist, "20260706-013804_cafef00d", {".jsonl": _LEAK_LINE})  # only-.jsonl leak
    _mk(hist, "test-flow-123", {".jsonl": _LEAK_LINE, ".jsonl.from-x": "x"})
    _mk(hist, "test-flow-neg", {".jsonl": _LEAK_LINE})
    (hist / "se3" / "history").mkdir(parents=True)  # nested test-residue dir
    (hist / "prompt_history").write_text("stray", encoding="utf-8")
    # prompt_history* DIRECTORY residue: multi-file so criteria (a)/(b) never
    # catch it -- only the name-based residue rule (c) removes it.
    _mk(hist, "prompt_history_old", {"a.txt": "x", "b.txt": "y"})
    # A lone bare ``.jsonl`` is the empty-step_id leak REGARDLESS of content: a
    # non-signature body is still a leak (the name, not the content, is the
    # signature) and must be removed, not preserved.
    _mk(hist, "20260706-013806_33334444", {".jsonl": '{"role": "user"}\n'})
    # ...and even an empty / truncated leak file (no readable body at all).
    _mk(hist, "20260706-013807_55556666", {".jsonl": ""})
    # --- real flows (must be preserved) ---
    _mk(hist, "20260706-013805_11112222", {"01_analyze_ab.jsonl": "real"})
    _mk(hist, "recovered_20260706_120000", {"engine.json": "{}"})
    _mk(hist, "173a47a7-c95", {"387e4498.jsonl": "real", "3be5f132.jsonl": "real"})
    return hist


def test_dry_run_removes_nothing(tmp_path, capsys):
    hist = _build_history(tmp_path)
    before = sorted(p.name for p in hist.iterdir())
    rc = cleanup_mod.cleanup(hist, dry_run=True)
    assert rc == 0
    assert sorted(p.name for p in hist.iterdir()) == before  # nothing deleted
    out = capsys.readouterr().out
    assert "DRY-RUN" in out


def test_deletes_only_leaks(tmp_path):
    hist = _build_history(tmp_path)
    cleanup_mod.cleanup(hist, dry_run=False)
    remaining = sorted(p.name for p in hist.iterdir())
    assert remaining == [
        "173a47a7-c95",
        "20260706-013805_11112222",
        "recovered_20260706_120000",
    ]


def test_flow_id_and_recovered_protected(tmp_path):
    hist = _build_history(tmp_path)
    # A flow-id dir with real content and a recovered_ snapshot are never touched.
    assert cleanup_mod.classify_dir(hist / "20260706-013805_11112222") is None
    assert cleanup_mod.classify_dir(hist / "recovered_20260706_120000") is None
    # A multi-step uuid-style dir holds REAL historical flow content and must
    # never be name-deleted -- there is no opt-in that can destroy it. (An empty
    # or single-``.jsonl`` uuid dir is still a content leak caught by (a)/(b);
    # this one has two real step files, so it is always preserved.)
    assert cleanup_mod.classify_dir(hist / "173a47a7-c95") is None


def test_bare_jsonl_leak_removed_regardless_of_content(tmp_path):
    hist = _build_history(tmp_path)
    # A lone bare ``.jsonl`` is the empty-step_id leak by its NAME, so a
    # non-signature body and an empty/truncated body are both removed. (A real
    # flow can never produce a bare ``.jsonl`` -- its step_id is always non-empty
    # -- so this can never delete real history.)
    assert cleanup_mod.classify_dir(hist / "20260706-013806_33334444") == "only_jsonl"
    assert cleanup_mod.classify_dir(hist / "20260706-013807_55556666") == "only_jsonl"


def test_multistep_flow_with_real_step_file_preserved(tmp_path):
    hist = _build_history(tmp_path)
    # A dir holding a real ``<step_id>.jsonl`` (non-empty step_id) is a real flow
    # and is never a single-bare-.jsonl leak.
    assert cleanup_mod.classify_dir(hist / "20260706-013805_11112222") is None


def test_prompt_history_dir_is_residue(tmp_path):
    hist = _build_history(tmp_path)
    # A multi-file prompt_history* dir escapes the content-leak criteria and is
    # removed purely on its name (residue), not preserved as an unknown dir.
    assert cleanup_mod.classify_dir(hist / "prompt_history_old") == "residue_name"


def test_report_counts(tmp_path, capsys):
    hist = _build_history(tmp_path)
    cleanup_mod.cleanup(hist, dry_run=True)
    out = capsys.readouterr().out
    assert "Empty leak dirs:            1" in out
    # cafef00d + test-flow-neg + 33334444 + 55556666 (bare .jsonl caught before
    # the name-based rules, regardless of content).
    assert "Single-'.jsonl' leak dirs:  4" in out
    # test-flow-123 + se3 + prompt_history_old
    assert "Residue-name dirs:          3" in out
    assert "Residue root files:         1" in out  # prompt_history
