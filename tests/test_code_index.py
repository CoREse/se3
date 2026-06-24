"""Tests for the code-index core subsystem (group G1).

Covers:
- ``file_enum`` gitignore-respecting enumeration + binary/exclude guards;
- ``code_index`` deterministic AST / structural extraction + per-symbol
  fingerprints;
- incremental (re)build: unchanged symbols reuse the md summary (no LLM), human
  corrections survive, deleted symbols are pruned, new ones enumerated;
- ``code_index_render`` root-view vs drill-in, reading only the md.

The degrade-mode three-condition gate has its own co-located module,
``src/se3/engine/test_code_index_degrade.py``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from se3.engine import code_index, code_index_render, file_enum
from se3.engine.code_index import (
    DEGRADED_MARKER,
    CodeIndex,
    build_index,
    load_or_build,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(root),
        check=True,
        capture_output=True,
        text=True,
    )


def _init_git_project(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Tester")


def _commit_all(root: Path, msg: str = "snapshot") -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", msg)


class RecordingSummarizer:
    """Fake summariser: records each batch's target ids, returns deterministic
    ``S:<name>`` summaries — never touches the LLM."""

    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    def __call__(self, targets):
        self.batches.append([t.id for t in targets])
        return {t.id: f"S:{t.name}" for t in targets}

    @property
    def last(self) -> list[str]:
        return self.batches[-1] if self.batches else []

    @property
    def call_count(self) -> int:
        return len(self.batches)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    _init_git_project(root)
    (root / ".gitignore").write_text(
        "ignored_dir/\n*.log\n/se3/*\n", encoding="utf-8"
    )
    (root / "src").mkdir()
    (root / "src" / "mod.py").write_text(
        "def alpha():\n    return 1\n\n\n"
        "class Greeter:\n    def hello(self):\n        return 'hi'\n"
        "    def bye(self):\n        return 'bye'\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text(
        "# Title\n\nintro\n\n## Section A\n\nbody a\n", encoding="utf-8"
    )
    (root / "ignored_dir").mkdir()
    (root / "ignored_dir" / "junk.py").write_text("x = 1\n", encoding="utf-8")
    (root / "noise.log").write_text("log line\n", encoding="utf-8")
    _commit_all(root)
    return root


# ---------------------------------------------------------------------------
# file_enum
# ---------------------------------------------------------------------------

class TestFileEnum:
    def test_enumerate_respects_gitignore_and_excludes_se3(self, project: Path):
        # se3/ runtime content must never be enumerated.
        (project / "se3").mkdir()
        (project / "se3" / "state.json").write_text("{}", encoding="utf-8")
        rels = {
            p.resolve().relative_to(project.resolve()).as_posix()
            for p in file_enum.enumerate_index_files(project)
        }
        assert "src/mod.py" in rels
        assert "README.md" in rels
        # gitignored files / dirs are absent.
        assert "ignored_dir/junk.py" not in rels
        assert "noise.log" not in rels
        # se3/ excluded.
        assert not any(r.startswith("se3/") for r in rels)

    def test_new_untracked_file_picked_up(self, project: Path):
        # A brand-new, non-ignored, uncommitted file is captured via --others.
        (project / "src" / "fresh.py").write_text("y = 2\n", encoding="utf-8")
        rels = {
            p.resolve().relative_to(project.resolve()).as_posix()
            for p in file_enum.enumerate_index_files(project)
        }
        assert "src/fresh.py" in rels

    def test_explicit_exclude_list(self, project: Path):
        (project / "vendor.py").write_text("v = 1\n", encoding="utf-8")
        rels = {
            p.resolve().relative_to(project.resolve()).as_posix()
            for p in file_enum.enumerate_index_files(project, ["vendor.py"])
        }
        assert "vendor.py" not in rels
        assert "src/mod.py" in rels

    def test_matches_exclude_dir_and_basename(self):
        assert file_enum.matches_exclude("a/b/big.min.js", ["*.min.js"])
        assert file_enum.matches_exclude("vendor/lib/x.js", ["vendor"])
        assert file_enum.matches_exclude("vendor/lib/x.js", ["vendor/"])
        assert not file_enum.matches_exclude("src/app.js", ["vendor"])

    def test_is_binary(self, project: Path):
        text = project / "src" / "mod.py"
        assert not file_enum.is_binary(text)
        blob = project / "blob.bin"
        blob.write_bytes(b"\x00\x01\x02\x03data")
        assert file_enum.is_binary(blob)
        empty = project / "empty.txt"
        empty.write_text("", encoding="utf-8")
        assert not file_enum.is_binary(empty)


# ---------------------------------------------------------------------------
# Structural extraction
# ---------------------------------------------------------------------------

class TestExtraction:
    def test_python_drills_to_function_method(self):
        src = (
            "def top():\n"
            "    def nested():\n"  # nested def must NOT be enumerated
            "        pass\n"
            "    return nested\n\n"
            "class C:\n"
            "    def m(self):\n"
            "        pass\n"
        )
        syms = code_index._extract_python(src)
        ids = {(s.local_id, s.kind) for s in syms}
        assert ("top", "function") in ids
        assert ("C", "class") in ids
        assert ("C.m", "method") in ids
        # The nested function inside ``top`` is the floor we do not cross.
        assert not any(s.local_id == "top.nested" for s in syms)
        assert not any(s.local_id == "nested" for s in syms)

    def test_markdown_headings(self):
        syms = code_index._extract_markdown("# A\n\n## B\n\ntext\n")
        names = [(s.name, s.depth) for s in syms]
        assert ("A", 0) in names
        assert ("B", 1) in names

    def test_yaml_top_keys(self):
        syms = code_index._extract_yaml("alpha: 1\nbeta:\n  nested: 2\n")
        assert {s.local_id for s in syms} == {"alpha", "beta"}

    def test_json_top_keys(self):
        syms = code_index._extract_json('{"a": 1, "b": {"c": 2}}')
        assert {s.local_id for s in syms} == {"a", "b"}

    def test_symbol_fingerprint_changes_with_content(self):
        a = code_index._extract_python("def f():\n    return 1\n")
        b = code_index._extract_python("def f():\n    return 2\n")
        assert a[0].sha256 != b[0].sha256


# ---------------------------------------------------------------------------
# Build / incremental cache
# ---------------------------------------------------------------------------

class TestBuild:
    def test_first_build_summarizes_all(self, project: Path):
        summ = RecordingSummarizer()
        index = build_index(project, summarizer=summ)
        # Both physical files written.
        assert code_index.md_path(project).exists()
        assert code_index.cache_path(project).exists()
        # File nodes + symbols all summarised on first build.
        assert "src/mod.py" in index.files
        assert "src/mod.py" in summ.last
        assert "src/mod.py::Greeter.hello" in summ.last
        # The map is complete for the current symbol set.
        mod = index.files["src/mod.py"]
        assert {s.local_id for s in mod.symbols} == {
            "alpha", "Greeter", "Greeter.hello", "Greeter.bye"
        }

    def test_unchanged_rebuild_skips_llm(self, project: Path):
        summ = RecordingSummarizer()
        build_index(project, summarizer=summ)
        first = summ.call_count
        build_index(project, summarizer=summ)
        # No source change => no targets => summariser not invoked again.
        assert summ.call_count == first

    def test_changed_symbol_only_resummarized(self, project: Path):
        summ = RecordingSummarizer()
        build_index(project, summarizer=summ)
        # Edit only Greeter.hello's body.
        (project / "src" / "mod.py").write_text(
            "def alpha():\n    return 1\n\n\n"
            "class Greeter:\n    def hello(self):\n        return 'HELLO'\n"
            "    def bye(self):\n        return 'bye'\n",
            encoding="utf-8",
        )
        build_index(project, summarizer=summ)
        batch = set(summ.last)
        # The edited method + the file node (file content changed) re-summarised.
        assert "src/mod.py::Greeter.hello" in batch
        assert "src/mod.py" in batch
        # Untouched sibling is reused, not re-summarised.
        assert "src/mod.py::Greeter.bye" not in batch
        assert "src/mod.py::alpha" not in batch

    def test_human_correction_preserved(self, project: Path):
        summ = RecordingSummarizer()
        build_index(project, summarizer=summ)
        md = code_index.md_path(project)
        text = md.read_text(encoding="utf-8")
        # Simulate a human correcting alpha's summary in the authoritative md.
        text = text.replace("`alpha` (function) — S:alpha",
                            "`alpha` (function) — HUMAN FIX")
        md.write_text(text, encoding="utf-8")
        index = build_index(project, summarizer=summ)
        alpha = next(s for s in index.files["src/mod.py"].symbols
                     if s.local_id == "alpha")
        assert alpha.summary == "HUMAN FIX"

    def test_deleted_file_pruned_new_added(self, project: Path):
        summ = RecordingSummarizer()
        build_index(project, summarizer=summ)
        # Delete README.md, add a new module.
        (project / "README.md").unlink()
        (project / "src" / "new.py").write_text("def g():\n    pass\n", encoding="utf-8")
        index = build_index(project, summarizer=summ)
        assert "README.md" not in index.files
        assert "src/new.py" in index.files
        assert "src/new.py::g" in summ.last

    def test_force_resummarizes_everything(self, project: Path):
        summ = RecordingSummarizer()
        build_index(project, summarizer=summ)
        build_index(project, summarizer=summ, force=True)
        # force ignores the memo => every node re-summarised.
        assert "src/mod.py::Greeter.hello" in summ.last
        assert "src/mod.py::alpha" in summ.last

    def test_binary_file_is_file_level_only(self, project: Path):
        (project / "data.bin").write_bytes(b"\x00\x01\x02\x03\x04")
        summ = RecordingSummarizer()
        index = build_index(project, summarizer=summ)
        fe = index.files["data.bin"]
        assert fe.kind == "binary"
        assert fe.symbols == []
        assert "binary" in fe.summary.lower()
        # Binary file node is not sent to the LLM.
        assert "data.bin" not in summ.last

    def test_load_or_build_alias(self, project: Path):
        summ = RecordingSummarizer()
        index = load_or_build(project, summarizer=summ)
        assert "src/mod.py" in index.files


# ---------------------------------------------------------------------------
# Degrade mode (mirrors the co-located test_code_index_degrade module so the
# default tests/ suite also exercises the three-condition gate)
# ---------------------------------------------------------------------------

class TestDegrade:
    def _cfg(self):
        from se3.config import CodeIndexConfig

        return CodeIndexConfig(
            degrade_trigger_lines=5,
            degrade_trigger_bytes=1024 * 1024,
            chunk_lines=3,
            chunk_bytes=1024 * 1024,
        )

    def test_gate_requires_all_three(self):
        cfg = self._cfg()
        big = "\n".join(f"line {i}" for i in range(20))
        small = "a\nb\n"
        assert code_index.is_degrade_eligible(big, has_structure=False, cfg=cfg)
        assert not code_index.is_degrade_eligible(big, has_structure=True, cfg=cfg)
        assert not code_index.is_degrade_eligible(small, has_structure=False, cfg=cfg)

    def test_large_structureless_file_chunks_with_marker(self, project: Path):
        cfg = self._cfg()
        big = project / "big.txt"
        big.write_text("\n".join(f"row {i}" for i in range(30)), encoding="utf-8")
        index = build_index(project, summarizer=RecordingSummarizer(), cfg=cfg)
        fe = index.files["big.txt"]
        assert fe.symbols and all(s.degraded for s in fe.symbols)
        detail = code_index_render.render_path(index, "big.txt")
        assert DEGRADED_MARKER in detail


# ---------------------------------------------------------------------------
# Rendering (reads md only)
# ---------------------------------------------------------------------------

class TestRender:
    def test_top_map_lists_files_not_symbols(self, project: Path):
        summ = RecordingSummarizer()
        index = build_index(project, summarizer=summ)
        top = code_index_render.render_top_map(index)
        assert "`src/mod.py`" in top
        # Symbol bullets are NOT in the top map.
        assert "Greeter.hello" not in top

    def test_render_path_file_shows_symbols(self, project: Path):
        summ = RecordingSummarizer()
        index = build_index(project, summarizer=summ)
        detail = code_index_render.render_path(index, "src/mod.py")
        assert "Greeter.hello" in detail
        assert "`alpha`" in detail

    def test_render_path_directory_lists_files(self, project: Path):
        summ = RecordingSummarizer()
        index = build_index(project, summarizer=summ)
        detail = code_index_render.render_path(index, "src")
        assert "`src/mod.py`" in detail
        assert "Greeter.hello" not in detail

    def test_render_path_unknown(self, project: Path):
        summ = RecordingSummarizer()
        index = build_index(project, summarizer=summ)
        assert "No code-index entry" in code_index_render.render_path(index, "nope/x.py")

    def test_load_for_display_reads_md_round_trip(self, project: Path):
        summ = RecordingSummarizer()
        build_index(project, summarizer=summ)
        index = code_index_render.load_for_display(project)
        assert index is not None
        assert "src/mod.py" in index.files
        # Summaries and symbols survive the md round-trip.
        mod = index.files["src/mod.py"]
        assert {s.local_id for s in mod.symbols} == {
            "alpha", "Greeter", "Greeter.hello", "Greeter.bye"
        }

    def test_from_md_does_not_need_json(self, project: Path):
        summ = RecordingSummarizer()
        build_index(project, summarizer=summ)
        # Delete the json memo; rendering must still work from md alone.
        code_index.cache_path(project).unlink()
        index = code_index_render.load_for_display(project)
        assert index is not None
        top = code_index_render.render_top_map(index)
        assert "`README.md`" in top
