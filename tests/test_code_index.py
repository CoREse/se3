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
from typer.testing import CliRunner

from se3.cli import app
from se3.commands import code_index_cmd
from se3.engine import code_index, code_index_render, file_enum
from se3.engine.code_index import (
    DEGRADED_MARKER,
    CodeIndex,
    build_index,
    load_or_build,
)

runner = CliRunner()


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

    def test_structured_keys_carry_value_content(self):
        # json/yaml top-level keys have no line range (json) or only the key
        # declaration line (yaml), so they must carry their own value content
        # for the summariser instead of an empty / key-only segment.
        ysyms = {s.local_id: s for s in code_index._extract_yaml("beta:\n  nested: 2\n")}
        assert "2" in ysyms["beta"].content and "nested" in ysyms["beta"].content
        jsyms = {s.local_id: s for s in code_index._extract_json('{"b": {"c": 2}}')}
        assert "c" in jsyms["b"].content and "2" in jsyms["b"].content

    def test_make_target_summarizes_json_key_from_value(self):
        # _make_target must feed the key's value content (not an empty string)
        # to the summariser for a json-key whose line range is 0/0.
        fe = code_index.FileEntry(
            path="conf.json",
            kind="json",
            fingerprint=code_index.Fingerprint(0.0, 0, ""),
        )
        sym = code_index._extract_json('{"database": {"host": "x", "pool": 10}}')[0]
        target = code_index._make_target(fe, sym, Path("conf.json"))
        assert "host" in target.content and "pool" in target.content

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

    def test_oversized_structured_file_drops_to_file_level(self, project: Path):
        """Size-cap secondary guard: a structured BUT oversized file (huge
        generated module / large data file) is dropped to a single file-level
        line instead of enumerating every symbol — independent of structure."""
        cfg = self._cfg()  # degrade_trigger_lines=5
        # A real Python module with many top-level functions, well over the
        # 5-line trigger: it HAS structure (ast yields symbols) yet is oversized.
        body = "\n\n".join(f"def f{i}():\n    return {i}" for i in range(40))
        gen = project / "src" / "generated.py"
        gen.write_text(body, encoding="utf-8")
        index = build_index(project, summarizer=RecordingSummarizer(), cfg=cfg)
        fe = index.files["src/generated.py"]
        # The size cap drops it to a file-level line: no per-symbol enumeration.
        assert fe.symbols == []
        # ... and it is NOT degrade-chunked either (structure present).
        assert fe.kind == "python"


# ---------------------------------------------------------------------------
# Summary flattening (one node per physical md line)
# ---------------------------------------------------------------------------

class TestSummaryFlatten:
    def test_flatten_collapses_newlines(self):
        assert code_index._flatten_summary("a\nb") == "a b"
        assert code_index._flatten_summary("  a \n\n  b  ") == "a b"
        assert code_index._flatten_summary("single") == "single"

    def test_newline_summary_survives_md_round_trip(self, project: Path):
        """A multi-line LLM summary must collapse to one physical md line so the
        md→summary round-trip (which reuses human-correctable summaries) does
        not lose the orphaned tail on the next incremental build."""

        class NewlineSummarizer:
            def __call__(self, targets):
                return {t.id: "first sentence\nsecond sentence" for t in targets}

        index = build_index(project, summarizer=NewlineSummarizer())
        md = code_index.md_path(project).read_text(encoding="utf-8")
        # No summary spills onto its own orphan line.
        parsed = code_index._parse_md_summaries(md)
        fe = index.files["src/mod.py"]
        # Every symbol's summary is recoverable from the md (single line each).
        for sym in fe.symbols:
            assert parsed.get(fe.symbol_id(sym)) == "first sentence second sentence"


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

    def test_top_map_shows_directory_summaries(self, project: Path):
        """Every structural level — including dir/package — carries a one-line
        summary so the orientation map is zoomable at the dir level too."""
        summ = RecordingSummarizer()
        index = build_index(project, summarizer=summ)
        # The directory group nodes were summarised (id == dir group name).
        assert "src/" in index.dir_summaries
        assert index.dir_summaries["src/"] == "S:src/"
        top = code_index_render.render_top_map(index)
        # The dir heading line carries its summary, not just the bare path.
        assert "## `src/` — S:src/" in top

    def test_dir_summary_survives_md_round_trip(self, project: Path):
        summ = RecordingSummarizer()
        build_index(project, summarizer=summ)
        index = code_index_render.load_for_display(project)
        assert index is not None
        assert index.dir_summaries.get("src/") == "S:src/"

    def test_unchanged_sibling_dir_summary_reused(self, project: Path):
        """The content-aware dir fingerprint is still incremental per directory:
        editing a file in one directory re-summarises only that directory; a
        sibling directory whose members are untouched reuses its cached summary."""
        summ = RecordingSummarizer()
        build_index(project, summarizer=summ)
        # Edit a root-level file; the "src/" directory is untouched.
        (project / "README.md").write_text(
            "# Title\n\nchanged intro\n\n## Section A\n\nbody a\n", encoding="utf-8"
        )
        build_index(project, summarizer=summ)
        # The root dir is re-summarised (its member changed) but the untouched
        # sibling "src/" dir reuses its cached summary.
        assert "(root)" in summ.last
        assert "src/" not in summ.last

    def test_dir_summary_refreshed_when_member_content_changes(self, project: Path):
        """A pure content edit inside an existing file re-summarises the dir, so
        the top map never carries a dir summary stale relative to the refreshed
        file/symbol summaries underneath it."""
        summ = RecordingSummarizer()
        build_index(project, summarizer=summ)
        (project / "src" / "mod.py").write_text(
            "def alpha():\n    return 2\n", encoding="utf-8"
        )
        build_index(project, summarizer=summ)
        # The member file's content changed, so the dir is re-summarised even
        # though its membership is unchanged.
        assert "src/" in summ.last

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


# ---------------------------------------------------------------------------
# CLI: se3 code-index (group G2)
# ---------------------------------------------------------------------------

class TestCodeIndexCLI:
    @pytest.fixture
    def built_project(self, project: Path, monkeypatch) -> Path:
        """A project whose code-index md/json are built (no LLM — fake summariser),
        with ``get_project_root`` pointed here so the CLI resolves to it."""
        build_index(project, summarizer=RecordingSummarizer())
        monkeypatch.setattr(code_index_cmd, "get_project_root", lambda: project)
        return project

    def test_index_no_arg_renders_top_map(self, built_project: Path):
        result = runner.invoke(app, ["code-index", "index"])
        assert result.exit_code == 0, result.output
        assert "`src/mod.py`" in result.output
        # Top map lists files, NOT function-level symbols.
        assert "Greeter.hello" not in result.output

    def test_bare_invocation_renders_top_map(self, built_project: Path):
        # Bare `se3 code-index` (no subcommand) must render the root/top map,
        # NOT exit with Typer's "Missing command" error.
        bare = runner.invoke(app, ["code-index"])
        assert bare.exit_code == 0, bare.output
        assert "Missing command" not in bare.output
        assert "`src/mod.py`" in bare.output
        # And it must match the explicit `index` (no-arg) subcommand output.
        sub = runner.invoke(app, ["code-index", "index"])
        assert bare.output == sub.output

    def test_bare_invocation_unbuilt_errors_without_missing_command(
        self, project: Path, monkeypatch
    ):
        # Bare invocation on an unbuilt project hits the not-built hint (exit 1),
        # never Typer's "Missing command" usage error.
        monkeypatch.setattr(code_index_cmd, "get_project_root", lambda: project)
        bare = runner.invoke(app, ["code-index"])
        assert bare.exit_code == 1
        assert "Missing command" not in bare.output
        assert "code-index rebuild" in bare.output

    def test_index_with_path_drills_into_file(self, built_project: Path):
        result = runner.invoke(app, ["code-index", "index", "src/mod.py"])
        assert result.exit_code == 0, result.output
        assert "Greeter.hello" in result.output
        assert "`alpha`" in result.output

    def test_index_with_directory_lists_files(self, built_project: Path):
        result = runner.invoke(app, ["code-index", "index", "src"])
        assert result.exit_code == 0, result.output
        assert "`src/mod.py`" in result.output
        assert "Greeter.hello" not in result.output

    def test_show_pulls_function_level(self, built_project: Path):
        result = runner.invoke(app, ["code-index", "show", "src/mod.py"])
        assert result.exit_code == 0, result.output
        assert "Greeter.hello" in result.output
        assert "Greeter.bye" in result.output

    def test_index_unbuilt_errors(self, project: Path, monkeypatch):
        # No build has run -> no md on disk -> non-zero exit with a hint.
        monkeypatch.setattr(code_index_cmd, "get_project_root", lambda: project)
        result = runner.invoke(app, ["code-index", "index"])
        assert result.exit_code == 1
        assert "code-index rebuild" in result.output

    def test_rebuild_force_full_rebuild(self, project: Path, monkeypatch):
        # First build with a fake summariser so no LLM is touched on the build path.
        build_index(project, summarizer=RecordingSummarizer())
        monkeypatch.setattr(code_index_cmd, "get_project_root", lambda: project)

        # Force the rebuild summariser to a fake so --force does not call the LLM.
        from se3.engine import code_index as ci

        recorder = RecordingSummarizer()
        monkeypatch.setattr(
            ci, "_make_llm_summarizer", lambda project_root: recorder
        )

        result = runner.invoke(app, ["code-index", "rebuild", "--force"])
        assert result.exit_code == 0, result.output
        assert "full rebuild" in result.output
        # --force re-summarises everything, so the fake summariser saw symbols.
        assert any("src/mod.py::alpha" in batch for batch in recorder.batches)
        # Both physical files exist after the rebuild.
        assert code_index.md_path(project).exists()
        assert code_index.cache_path(project).exists()

    def test_inspect_reports_stats(self, built_project: Path):
        result = runner.invoke(app, ["code-index", "inspect"])
        assert result.exit_code == 0, result.output
        assert "Files:" in result.output
        assert "Symbols:" in result.output

    def test_help_mentions_md_authoritative_product(self):
        result = runner.invoke(app, ["code-index", "--help"])
        assert result.exit_code == 0, result.output
        assert "se3/code-index.md" in result.output


class TestConcurrentRebuildSafety:
    """The lazy/incremental (re)build must serialize across processes and never
    write through a fixed, collision-prone temp filename."""

    def test_atomic_write_uses_unique_temp_no_fixed_leftover(self, project: Path):
        build_index(project, summarizer=RecordingSummarizer())
        md = code_index.md_path(project)
        cache = code_index.cache_path(project)
        assert md.exists() and cache.exists()
        # The retired fixed-name temp files must not be left behind.
        assert not (md.parent / "code-index.md.tmp").exists()
        assert not (cache.parent / "code-index.tmp").exists()
        # No stray *.tmp scratch files survive a successful write either.
        assert not list(md.parent.glob("code-index.md.*.tmp"))
        assert not list(cache.parent.glob("code-index.json.*.tmp"))

    def test_build_lock_serializes_concurrent_builds(self, project: Path):
        # Two builds run back-to-back while one holds the advisory lock: the
        # second must wait, then re-enumerate and produce a valid, current map.
        import threading

        from se3.engine import code_index as ci

        order: list[str] = []

        # First build to establish md/cache, then a concurrent pair.
        build_index(project, summarizer=RecordingSummarizer())

        barrier = threading.Event()

        def worker(tag: str):
            order.append(f"start:{tag}")
            build_index(project, summarizer=RecordingSummarizer())
            order.append(f"done:{tag}")

        t1 = threading.Thread(target=worker, args=("a",))
        t2 = threading.Thread(target=worker, args=("b",))
        t1.start(); t2.start()
        t1.join(timeout=30); t2.join(timeout=30)
        assert not t1.is_alive() and not t2.is_alive()
        # Both finished and the map is intact / parseable.
        idx = code_index_render.load_for_display(project)
        assert idx is not None and idx.files
        assert order.count("done:a") == 1 and order.count("done:b") == 1

    def test_build_proceeds_unlocked_when_fcntl_unavailable(
        self, project: Path, monkeypatch
    ):
        # When fcntl is unavailable the build still runs (best-effort), just
        # without the advisory lock.
        from se3.engine import code_index as ci

        monkeypatch.setattr(ci, "_HAVE_FCNTL", False)
        idx = build_index(project, summarizer=RecordingSummarizer())
        assert idx.files
        assert ci.md_path(project).exists()

    def test_lock_path_lives_next_to_cache(self, project: Path):
        lp = code_index.lock_path(project)
        assert lp.name == "code-index.json.lock"
        assert lp.parent == code_index.cache_path(project).parent
