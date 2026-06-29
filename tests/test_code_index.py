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
    ``S:<name>`` summaries — never touches the LLM.

    A single build now summarises bottom-up in several waves (symbols, then
    files, then directories deepest-first), so one ``build_index`` call can
    invoke this summariser multiple times. ``all`` is the union of every recorded
    batch's ids; ``reset`` clears the record (but not the on-disk md) so a test
    can isolate the work done by a *subsequent* incremental build.
    """

    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    def __call__(self, targets):
        self.batches.append([t.id for t in targets])
        return {t.id: f"S:{t.name}" for t in targets}

    @property
    def all(self) -> set:
        return {i for batch in self.batches for i in batch}

    @property
    def last(self) -> list[str]:
        return self.batches[-1] if self.batches else []

    @property
    def call_count(self) -> int:
        return len(self.batches)

    def reset(self) -> None:
        self.batches = []


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
        # The single authoritative md is written (no separate json cache exists).
        assert code_index.md_path(project).exists()
        # File nodes + symbols all summarised on first build (across waves).
        assert "src/mod.py" in index.files
        assert "src/mod.py" in summ.all
        assert "src/mod.py::Greeter.hello" in summ.all
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
        summ.reset()
        # Edit only Greeter.hello's body (its name+kind are unchanged).
        (project / "src" / "mod.py").write_text(
            "def alpha():\n    return 1\n\n\n"
            "class Greeter:\n    def hello(self):\n        return 'HELLO'\n"
            "    def bye(self):\n        return 'bye'\n",
            encoding="utf-8",
        )
        build_index(project, summarizer=summ)
        touched = summ.all
        # Normal mode gates files/dirs on their list-fp (symbol name+kind roster),
        # not whole-file content: a body-only edit leaves the roster unchanged, so
        # ONLY the edited symbol is re-summarised — the file and its dirs are not.
        assert "src/mod.py::Greeter.hello" in touched
        assert "src/mod.py" not in touched
        assert "src/" not in touched
        # Untouched sibling symbols are reused, not re-summarised.
        assert "src/mod.py::Greeter.bye" not in touched
        assert "src/mod.py::alpha" not in touched

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
        assert "src/new.py::g" in summ.all

    def test_force_resummarizes_everything(self, project: Path):
        summ = RecordingSummarizer()
        build_index(project, summarizer=summ)
        summ.reset()
        build_index(project, summarizer=summ, force=True)
        # force ignores the md fingerprints => every node re-summarised.
        assert "src/mod.py::Greeter.hello" in summ.all
        assert "src/mod.py::alpha" in summ.all

    def test_summary_is_bottom_up(self, project: Path):
        """A node's summary is synthesised from its children's summaries: a file
        with symbols is summarised from those symbols' summaries (not raw source),
        and a directory from its child files' summaries."""
        captured: dict[str, str] = {}

        class Capturing:
            def __call__(self, targets):
                out = {}
                for t in targets:
                    captured[t.id] = t.content
                    out[t.id] = f"S:{t.name}"
                return out

        build_index(project, summarizer=Capturing())
        # The file node is fed its child symbols' summaries, NOT raw source.
        file_content = captured["src/mod.py"]
        assert "S:Greeter.hello" in file_content
        assert "def hello" not in file_content
        # The directory node is fed its child files' summaries.
        assert "S:src/mod.py" in captured["src/"]

    def test_checkpoint_flushes_partial_md_for_resume(self, project: Path):
        """A crash mid-build leaves a resumable partial md: files summarised
        before the crash carry fingerprints and are reused on the next build,
        rather than the whole run being lost."""
        class Boom:
            def __init__(self) -> None:
                self.n = 0

            def __call__(self, targets):
                self.n += 1
                if self.n > 2:  # let the first file's batches through, then crash
                    raise RuntimeError("boom")
                return {t.id: f"S:{t.name}" for t in targets}

        with pytest.raises(RuntimeError):
            build_index(project, summarizer=Boom())

        # A partial md was flushed before the crash and carries real fingerprints.
        md_text = code_index.md_path(project).read_text(encoding="utf-8")
        assert "<!--#" in md_text
        _, fps, _ = code_index._parse_md(md_text)
        assert fps, "expected at least one checkpointed node with a fingerprint"

        # Resume with a working summariser: the build completes, and the nodes
        # the partial md already fingerprinted are reused (not re-summarised).
        summ = RecordingSummarizer()
        build_index(project, summarizer=summ)
        reused_ids = set(fps)
        assert reused_ids and not (reused_ids & summ.all), (
            "checkpointed nodes should be reused, not re-summarised on resume"
        )

    def test_checkpoint_flush_preserves_unchanged_file_fingerprints(self, project: Path):
        """A checkpoint flush re-renders the whole index; an unchanged file the
        work loop has not reached yet must KEEP its fingerprint in the flushed md
        (seeded up front), so an interrupted build never blanks it and forces a
        needless re-summarisation next time."""
        (project / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
        (project / "b.py").write_text("def b():\n    return 2\n", encoding="utf-8")
        (project / "c.py").write_text("def c():\n    return 3\n", encoding="utf-8")
        build_index(project, summarizer=RecordingSummarizer())

        # Edit a and c (NOT b); b must survive untouched across the flush.
        (project / "a.py").write_text("def a():\n    return 11\n", encoding="utf-8")
        (project / "c.py").write_text("def c():\n    return 33\n", encoding="utf-8")

        class CrashOnC:
            def __call__(self, targets):
                if any(t.path == "c.py" for t in targets):
                    raise RuntimeError("boom")
                return {t.id: f"S:{t.name}" for t in targets}

        with pytest.raises(RuntimeError):
            build_index(project, summarizer=CrashOnC())

        # b.py (unchanged, never reached before the crash) keeps its fingerprint.
        md = code_index.md_path(project).read_text(encoding="utf-8")
        _, fps, list_fps = code_index._parse_md(md)
        assert "b.py" in fps
        assert "b.py::b" in fps
        # The untouched file also keeps its list-fp across the flush (file/dir
        # lines now carry both fingerprints), so a later build doesn't see it as
        # unmigrated and needlessly recompute the reuse decision.
        assert "b.py" in list_fps

    def test_binary_file_is_file_level_only(self, project: Path):
        (project / "data.bin").write_bytes(b"\x00\x01\x02\x03\x04")
        summ = RecordingSummarizer()
        index = build_index(project, summarizer=summ)
        fe = index.files["data.bin"]
        assert fe.kind == "binary"
        assert fe.symbols == []
        assert "binary" in fe.summary.lower()
        # Binary file node is not sent to the LLM.
        assert "data.bin" not in summ.all

    def test_load_or_build_alias(self, project: Path):
        summ = RecordingSummarizer()
        index = load_or_build(project, summarizer=summ)
        assert "src/mod.py" in index.files


# ---------------------------------------------------------------------------
# List-fp reuse gate (G3): normal mode gates files/dirs on their direct-member
# roster (list-fp); --force keeps the whole-content cascade; migration is lazy.
# ---------------------------------------------------------------------------

class TestListFp:
    def test_body_only_change_resummarises_symbol_only(self, project: Path):
        """Acceptance (a): a body-only edit re-summarises ONLY the edited symbol —
        its file and dirs reuse (list-fps unchanged)."""
        summ = RecordingSummarizer()
        build_index(project, summarizer=summ)
        summ.reset()
        (project / "src" / "mod.py").write_text(
            "def alpha():\n    return 42\n\n\n"
            "class Greeter:\n    def hello(self):\n        return 'hi'\n"
            "    def bye(self):\n        return 'bye'\n",
            encoding="utf-8",
        )
        build_index(project, summarizer=summ)
        assert "src/mod.py::alpha" in summ.all
        assert "src/mod.py" not in summ.all
        assert "src/" not in summ.all
        assert "(root)" not in summ.all

    def test_new_symbol_resummarises_symbol_and_file_not_dir(self, project: Path):
        """Acceptance (b): adding a symbol re-summarises that symbol and its file
        (roster changed) but NOT the parent dir (dir roster unchanged)."""
        summ = RecordingSummarizer()
        build_index(project, summarizer=summ)
        summ.reset()
        (project / "src" / "mod.py").write_text(
            "def alpha():\n    return 1\n\n\n"
            "class Greeter:\n    def hello(self):\n        return 'hi'\n"
            "    def bye(self):\n        return 'bye'\n\n\n"
            "def gamma():\n    return 3\n",
            encoding="utf-8",
        )
        build_index(project, summarizer=summ)
        assert "src/mod.py::gamma" in summ.all
        assert "src/mod.py" in summ.all
        assert "src/" not in summ.all
        assert "(root)" not in summ.all

    def test_renamed_symbol_resummarises_file_not_dir(self, project: Path):
        """Acceptance (b): renaming a symbol changes the file roster → file +
        new symbol re-summarised, parent dir untouched."""
        summ = RecordingSummarizer()
        build_index(project, summarizer=summ)
        summ.reset()
        (project / "src" / "mod.py").write_text(
            "def alpha_renamed():\n    return 1\n\n\n"
            "class Greeter:\n    def hello(self):\n        return 'hi'\n"
            "    def bye(self):\n        return 'bye'\n",
            encoding="utf-8",
        )
        build_index(project, summarizer=summ)
        assert "src/mod.py::alpha_renamed" in summ.all
        assert "src/mod.py" in summ.all
        assert "src/" not in summ.all

    def test_kind_change_same_name_resummarises_file(self, project: Path):
        """Acceptance (b), kind component: changing a symbol's kind while keeping
        its name (function→class for ``alpha``) changes the file roster's
        ``(kind, name)`` entry → the file is re-summarised in normal mode. Locks
        in the ``kind`` half of the list-fp: were it dropped, the name-only
        roster would be unchanged and the file would wrongly reuse."""
        summ = RecordingSummarizer()
        build_index(project, summarizer=summ)
        summ.reset()
        (project / "src" / "mod.py").write_text(
            "class alpha:\n    x = 1\n\n\n"
            "class Greeter:\n    def hello(self):\n        return 'hi'\n"
            "    def bye(self):\n        return 'bye'\n",
            encoding="utf-8",
        )
        build_index(project, summarizer=summ)
        # File roster's (kind, name) for alpha flipped function→class.
        assert "src/mod.py" in summ.all
        # Dir roster (direct child names) is unchanged → ancestors reuse.
        assert "src/" not in summ.all
        assert "(root)" not in summ.all

    def test_reorder_symbols_no_resummary(self, project: Path):
        """The file list-fp is order-insensitive: moving an unchanged symbol
        above/below another (no add/remove/rename/re-kind) leaves the roster's
        sorted (kind, name) set identical → the file is NOT re-summarised."""
        summ = RecordingSummarizer()
        build_index(project, summarizer=summ)
        summ.reset()
        # Greeter moved above alpha; neither symbol's name, kind, or body changed.
        (project / "src" / "mod.py").write_text(
            "class Greeter:\n    def hello(self):\n        return 'hi'\n"
            "    def bye(self):\n        return 'bye'\n\n\n"
            "def alpha():\n    return 1\n",
            encoding="utf-8",
        )
        build_index(project, summarizer=summ)
        assert "src/mod.py" not in summ.all
        assert "src/" not in summ.all
        assert "(root)" not in summ.all

    def test_dir_membership_change_resummarises_dir_not_ancestors(self, project: Path):
        """Acceptance (c): adding a file to a subdir re-summarises ONLY that
        subdir; its ancestor dirs (whose direct rosters are unchanged) reuse."""
        (project / "src" / "sub").mkdir()
        (project / "src" / "sub" / "inner.py").write_text(
            "def inner():\n    return 1\n", encoding="utf-8"
        )
        summ = RecordingSummarizer()
        build_index(project, summarizer=summ)
        summ.reset()
        # Add a sibling file inside src/sub/: only src/sub/'s direct roster grows.
        (project / "src" / "sub" / "added.py").write_text(
            "def added():\n    return 2\n", encoding="utf-8"
        )
        build_index(project, summarizer=summ)
        assert "src/sub/" in summ.all
        # Ancestors' direct child names are unchanged (src/ still has mod.py+sub/,
        # root still has the same direct members), so they are NOT re-summarised.
        assert "src/" not in summ.all
        assert "(root)" not in summ.all

    def test_dir_child_removal_resummarises_dir_not_ancestors(self, project: Path):
        """Acceptance (c): removing a direct child re-summarises that dir only."""
        (project / "src" / "sub").mkdir()
        (project / "src" / "sub" / "inner.py").write_text(
            "def inner():\n    return 1\n", encoding="utf-8"
        )
        (project / "src" / "sub" / "extra.py").write_text(
            "def extra():\n    return 2\n", encoding="utf-8"
        )
        summ = RecordingSummarizer()
        build_index(project, summarizer=summ)
        summ.reset()
        (project / "src" / "sub" / "extra.py").unlink()
        build_index(project, summarizer=summ)
        assert "src/sub/" in summ.all
        assert "src/" not in summ.all
        assert "(root)" not in summ.all

    def test_symbolless_file_content_edit_resummarises_file(self, project: Path):
        """A file with an EMPTY symbol roster (plain prose/config: no functions,
        no headings, no keys) has a constant empty list-fp, so the list gate would
        match forever and freeze its summary. Its file-level summary is the only
        representation of its content and it has no child symbol leaf, so a content
        edit must fall back to the content-fp and re-summarise the file node."""
        notes = project / "notes.txt"
        notes.write_text("first draft of the notes\n", encoding="utf-8")
        summ = RecordingSummarizer()
        index = build_index(project, summarizer=summ)
        # Precondition: the file really is symbol-less (the gap this guards).
        assert index.files["notes.txt"].symbols == []
        assert "notes.txt" in summ.all

        # An untouched symbol-less file reuses (content-fp unchanged → zero LLM).
        summ.reset()
        build_index(project, summarizer=summ)
        assert "notes.txt" not in summ.all

        # A pure body rewrite (no roster — there is none) must still re-summarise
        # the file node in normal mode, exactly as the old content-fp gate did.
        summ.reset()
        notes.write_text("completely rewritten body of the notes\n", encoding="utf-8")
        build_index(project, summarizer=summ)
        assert "notes.txt" in summ.all
        # Its parent dir roster (root's direct child names) is unchanged → no
        # ancestor cascade in normal mode.
        assert "(root)" not in summ.all

    def test_force_cascades_whole_content(self, project: Path):
        """Acceptance (d): --force ignores list-fps and re-summarises every node
        (symbols, files, all dirs) — the whole-content cascade, unchanged."""
        summ = RecordingSummarizer()
        build_index(project, summarizer=summ)
        summ.reset()
        build_index(project, summarizer=summ, force=True)
        touched = summ.all
        assert "src/mod.py::alpha" in touched
        assert "src/mod.py::Greeter.hello" in touched
        assert "src/mod.py" in touched
        assert "src/" in touched
        assert "(root)" in touched

    def test_lazy_migration_reuses_old_md_zero_llm(self, project: Path):
        """Acceptance (e): an md predating list-fps (legacy single-fp lines) is
        reused for free on the first normal rebuild — zero LLM — and the
        list-fps are mechanically backfilled into the rewritten md."""
        import re

        build_index(project, summarizer=RecordingSummarizer())
        md = code_index.md_path(project)
        text = md.read_text(encoding="utf-8")
        # Strip the list-fp segment from every file/dir line → legacy md shape.
        legacy = re.sub(r"<!--#([0-9a-f]+)\|[0-9a-f]+-->", r"<!--#\1-->", text)
        assert legacy != text, "fixture md should have had list-fps to strip"
        md.write_text(legacy, encoding="utf-8")
        # Sanity: the downgraded md parses with content-fps but no list-fps.
        _s, content_fps, list_fps = code_index._parse_md(legacy)
        assert content_fps
        assert not list_fps

        # First normal rebuild after the upgrade: every node reuses via the
        # content-fp fallback, so the summariser is never called.
        summ = RecordingSummarizer()
        index = build_index(project, summarizer=summ)
        assert summ.call_count == 0
        assert summ.all == set()

        # Zero LLM is only half the contract: the OLD summaries from the legacy
        # md must be carried into the rebuilt index (reuse, not a blank wipe).
        assert index.files["src/mod.py"].summary == _s["src/mod.py"]
        assert index.dir_summaries["src/"] == _s["src/"]
        hello = next(s for s in index.files["src/mod.py"].symbols
                     if s.local_id == "Greeter.hello")
        assert hello.summary == _s["src/mod.py::Greeter.hello"]

        # The rebuild mechanically backfilled list-fps into the rewritten md.
        _s2, _c2, list_fps2 = code_index._parse_md(md.read_text(encoding="utf-8"))
        assert list_fps2
        assert "src/mod.py" in list_fps2
        assert "src/" in list_fps2


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
    def test_adaptive_lists_files_not_symbols(self, project: Path):
        summ = RecordingSummarizer()
        index = build_index(project, summarizer=summ)
        view = code_index_render.render_adaptive(index)
        assert "`src/mod.py`" in view
        # Symbol bullets are NOT in the root view.
        assert "Greeter.hello" not in view

    def test_adaptive_shows_directory_summaries(self, project: Path):
        """Every structural level — including dir/package — carries a one-line
        summary so the orientation map is zoomable at the dir level too."""
        summ = RecordingSummarizer()
        index = build_index(project, summarizer=summ)
        # The directory nodes were summarised (id == dir key).
        assert "src/" in index.dir_summaries
        assert index.dir_summaries["src/"] == "S:src/"
        view = code_index_render.render_adaptive(index)
        # The dir line carries its summary, not just the bare path.
        assert "`src/` — S:src/" in view

    def test_dir_summary_survives_md_round_trip(self, project: Path):
        summ = RecordingSummarizer()
        build_index(project, summarizer=summ)
        index = code_index_render.load_for_display(project)
        assert index is not None
        assert index.dir_summaries.get("src/") == "S:src/"

    def test_unchanged_sibling_dir_summary_reused(self, project: Path):
        """Each directory's normal-mode reuse is independent: adding a direct
        member to one directory re-summarises only that directory; a sibling whose
        own direct-member roster is untouched reuses its cached summary."""
        summ = RecordingSummarizer()
        build_index(project, summarizer=summ)
        summ.reset()
        # Add a root-level file: the root dir's direct-child roster changes, but
        # the "src/" directory's roster is untouched.
        (project / "extra.py").write_text(
            "def extra():\n    return 0\n", encoding="utf-8"
        )
        build_index(project, summarizer=summ)
        # The root dir is re-summarised (its membership changed) but the untouched
        # sibling "src/" dir reuses its cached summary.
        assert "(root)" in summ.all
        assert "src/" not in summ.all

    def test_dir_summary_not_refreshed_on_member_content_change(self, project: Path):
        """Normal mode: a pure content edit inside an existing file does NOT
        re-summarise its ancestor directories (the dir list-fp — its direct child
        names — is unchanged). This is the cascade this design removes; --force
        restores the content-fp cascade for catching deep semantic drift."""
        summ = RecordingSummarizer()
        build_index(project, summarizer=summ)
        summ.reset()
        # Body-only edit: alpha's body changes, its name+kind do not.
        (project / "src" / "mod.py").write_text(
            "def alpha():\n    return 2\n\n\n"
            "class Greeter:\n    def hello(self):\n        return 'hi'\n"
            "    def bye(self):\n        return 'bye'\n",
            encoding="utf-8",
        )
        build_index(project, summarizer=summ)
        # Neither the file nor the dir is touched — only the edited symbol.
        assert "src/mod.py::alpha" in summ.all
        assert "src/mod.py" not in summ.all
        assert "src/" not in summ.all
        # --force restores the whole-content cascade.
        summ.reset()
        build_index(project, summarizer=summ, force=True)
        assert "src/" in summ.all
        assert "src/mod.py" in summ.all

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

    def test_md_is_self_sufficient_for_incremental_rebuild(self, project: Path):
        """The md alone (no out-of-band cache) carries the fingerprints, so a
        rebuild on an unchanged tree re-summarises nothing and preserves human
        corrections — incrementality does not depend on anything outside git."""
        summ = RecordingSummarizer()
        build_index(project, summarizer=summ)
        # No json sidecar is written at all.
        assert not (project / "se3" / "cache" / "code-index.json").exists()
        # A second build with the source unchanged does zero LLM work, driven
        # purely by the fingerprints embedded in the committed md.
        summ.reset()
        build_index(project, summarizer=summ)
        assert summ.call_count == 0
        # And the md still renders fine on its own.
        index = code_index_render.load_for_display(project)
        assert index is not None
        view = code_index_render.render_adaptive(index)
        assert "`README.md`" in view

    def test_md_embeds_node_fingerprints(self, project: Path):
        """Every rendered node line carries a terse 16-hex fingerprint comment so
        staleness is decidable from the committed md alone."""
        build_index(project, summarizer=RecordingSummarizer())
        md = code_index.md_path(project).read_text(encoding="utf-8")
        import re

        # A file heading line carries a content-fp plus the optional list-fp
        # segment: <!--#<16-hex>(|<16-hex>)?-->.
        assert re.search(r"### `src/mod\.py`.*<!--#[0-9a-f]{16}(\|[0-9a-f]{16})?-->", md)
        # The fingerprints are invisible to the parsed summary (stripped first).
        summaries, fps, list_fps = code_index._parse_md(md)
        assert "src/mod.py" in fps and len(fps["src/mod.py"]) == 16
        assert "<!--" not in summaries.get("src/mod.py", "")
        # A built file node carries a list-fp; a symbol bullet never does.
        assert "src/mod.py" in list_fps
        assert not any("::" in key for key in list_fps)


# ---------------------------------------------------------------------------
# md double-fingerprint round-trip (group G2)
# ---------------------------------------------------------------------------

class TestDoubleFpRoundTrip:
    """The trailing fp comment carries a content-fp plus an optional list-fp for
    file/dir lines, while staying byte-compatible with the legacy single-fp form
    used by symbol lines and any pre-migration md."""

    def test_split_fp_legacy_single_fp(self):
        line, content, lst = code_index._split_fp("### `a.py` — s <!--#abc123-->")
        # The leading whitespace before the comment is consumed by the regex.
        assert line == "### `a.py` — s"
        assert content == "abc123"
        assert lst is None

    def test_split_fp_double_fp(self):
        line, content, lst = code_index._split_fp(
            "### `a.py` — s <!--#abc123|def456-->"
        )
        assert line == "### `a.py` — s"
        assert content == "abc123"
        assert lst == "def456"

    def test_split_fp_no_comment(self):
        line, content, lst = code_index._split_fp("### `a.py` — plain summary")
        assert line == "### `a.py` — plain summary"
        assert content is None and lst is None

    def test_fp_comment_round_trip(self):
        # content-only → legacy shape; content+list → double shape.
        assert code_index._fp_comment("aa11") == "<!--#aa11-->"
        assert code_index._fp_comment("aa11", "bb22") == "<!--#aa11|bb22-->"
        # An empty list-fp falls back to the legacy single-fp shape.
        assert code_index._fp_comment("aa11", "") == "<!--#aa11-->"

    def test_render_parse_round_trip(self, project: Path):
        """render_full → _parse_md restores both fingerprints for every file/dir
        node, no list-fp leaks onto symbol nodes, and an unsummarised node embeds
        nothing (keeping a checkpointed md a safe resume point)."""
        index = build_index(project, summarizer=RecordingSummarizer())
        md = code_index.render_full(index)
        _summaries, content_fps, list_fps = code_index._parse_md(md)

        # Every file node round-trips both fps.
        for relpath, fe in index.files.items():
            if not fe.summary:
                continue
            assert content_fps.get(relpath) == code_index._fp(fe.fingerprint.sha256)
            if fe.list_fp:
                assert list_fps.get(relpath) == fe.list_fp

        # Every summarised dir node round-trips both fps.
        for dirkey, dsum in index.dir_summaries.items():
            if not dsum:
                continue
            assert content_fps.get(dirkey) == index.dir_fingerprints.get(dirkey)
            assert list_fps.get(dirkey) == index.dir_list_fingerprints.get(dirkey)

        # Symbol nodes carry only a content-fp — never a list-fp.
        assert any("::" in k for k in content_fps)
        assert not any("::" in k for k in list_fps)

    def test_unsummarised_node_embeds_no_fp(self, project: Path):
        """A node with no summary embeds neither fingerprint, so a partial md is a
        safe resume point (no fp → treated as stale next build)."""
        index = build_index(project, summarizer=RecordingSummarizer())
        fe = next(iter(index.files.values()))
        fe.summary = ""
        md = code_index.render_full(index)
        _summaries, content_fps, list_fps = code_index._parse_md(md)
        assert fe.path not in content_fps
        assert fe.path not in list_fps


# ---------------------------------------------------------------------------
# CLI: se3 code-index (group G2)
# ---------------------------------------------------------------------------

class TestCodeIndexCLI:
    @pytest.fixture
    def built_project(self, project: Path, monkeypatch) -> Path:
        """A project whose code-index md is built (no LLM — fake summariser),
        with ``get_project_root`` pointed here so the CLI resolves to it."""
        build_index(project, summarizer=RecordingSummarizer())
        monkeypatch.setattr(code_index_cmd, "get_project_root", lambda: project)
        return project

    def test_index_no_arg_renders_literal_root_level(self, built_project: Path):
        # `index` with no path shows exactly the literal root level: top-level
        # directories collapsed (one line) + root files — NOT one level deeper.
        result = runner.invoke(app, ["code-index", "index"])
        assert result.exit_code == 0, result.output
        assert "`src/`" in result.output
        # src/mod.py lives one level below src/, so it is NOT shown here.
        assert "`src/mod.py`" not in result.output
        assert "Greeter.hello" not in result.output

    def test_bare_invocation_renders_adaptive_map(self, built_project: Path):
        # Bare `se3 code-index` (no subcommand) renders the adaptive root view,
        # NOT exit with Typer's "Missing command" error. src/ is a code root, so
        # the budget expands it and src/mod.py appears.
        bare = runner.invoke(app, ["code-index"])
        assert bare.exit_code == 0, bare.output
        assert "Missing command" not in bare.output
        assert "`src/mod.py`" in bare.output
        # The adaptive view still omits function-level symbols.
        assert "Greeter.hello" not in bare.output

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
        # The authoritative md exists after the rebuild.
        assert code_index.md_path(project).exists()

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
        assert md.exists()
        # The fixed-name temp file must not be left behind.
        assert not (md.parent / "code-index.md.tmp").exists()
        # No stray *.tmp scratch files survive a successful write either.
        assert not list(md.parent.glob("code-index.md.*.tmp"))

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

    def test_lock_path_lives_under_gitignored_cache(self, project: Path):
        lp = code_index.lock_path(project)
        assert lp.name == "code-index.lock"
        assert lp.parent == project / "se3" / "cache"
