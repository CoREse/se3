"""Tests for scripts/cleanup_history_leak.py (one-time history-leak cleanup).

Locks in the classification logic so the destructive script can never grow to
delete a real flow: only empty dirs, content-verified single-``.jsonl`` leak
dirs, and explicit test-fixture names are removed; flow-id / recovered_ /
old uuid-style multi-step dirs are preserved.
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
    # --- real flows (must be preserved) ---
    _mk(hist, "20260706-013805_11112222", {"01_analyze_ab.jsonl": "real"})
    _mk(hist, "recovered_20260706_120000", {"engine.json": "{}"})
    _mk(hist, "173a47a7-c95", {"387e4498.jsonl": "real", "3be5f132.jsonl": "real"})
    # single-.jsonl file but NOT the test signature -> must be preserved
    _mk(hist, "20260706-013806_33334444", {".jsonl": '{"role": "user"}\n'})
    return hist


def test_dry_run_removes_nothing(tmp_path, capsys):
    hist = _build_history(tmp_path)
    before = sorted(p.name for p in hist.iterdir())
    rc = cleanup_mod.cleanup(hist, dry_run=True, include_uuid_dirs=False)
    assert rc == 0
    assert sorted(p.name for p in hist.iterdir()) == before  # nothing deleted
    out = capsys.readouterr().out
    assert "DRY-RUN" in out


def test_deletes_only_leaks(tmp_path):
    hist = _build_history(tmp_path)
    cleanup_mod.cleanup(hist, dry_run=False, include_uuid_dirs=False)
    remaining = sorted(p.name for p in hist.iterdir())
    assert remaining == [
        "173a47a7-c95",
        "20260706-013805_11112222",
        "20260706-013806_33334444",
        "recovered_20260706_120000",
    ]


def test_flow_id_and_recovered_protected(tmp_path):
    hist = _build_history(tmp_path)
    # A flow-id dir with real content and a recovered_ snapshot are never touched.
    assert cleanup_mod.classify_dir(hist / "20260706-013805_11112222", False) is None
    assert cleanup_mod.classify_dir(hist / "recovered_20260706_120000", False) is None
    # uuid-style dir preserved by default (real history); only the explicit
    # --include-uuid-dirs opt-in makes it a name-based deletion candidate.
    assert cleanup_mod.classify_dir(hist / "173a47a7-c95", False) is None
    assert cleanup_mod.classify_dir(hist / "173a47a7-c95", True) == "uuid_name"


def test_multifile_dir_not_misdeleted(tmp_path):
    hist = _build_history(tmp_path)
    # non-signature single-.jsonl is preserved (not a verified leak).
    assert cleanup_mod.classify_dir(hist / "20260706-013806_33334444", False) is None


def test_report_counts(tmp_path, capsys):
    hist = _build_history(tmp_path)
    cleanup_mod.cleanup(hist, dry_run=True, include_uuid_dirs=False)
    out = capsys.readouterr().out
    assert "Empty leak dirs:            1" in out
    assert "Single-'.jsonl' leak dirs:  2" in out  # cafef00d + test-flow-neg
    assert "Residue-name dirs:          2" in out  # test-flow-123 + se3
    assert "Residue root files:         1" in out  # prompt_history
