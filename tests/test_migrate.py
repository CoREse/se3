"""Tests for the ``se3 migrate`` command + the first spec->new-system migrator
(group G9).

Covers:
- the reusable registry (register / list / get, duplicate-id rejection);
- CLI registration (``se3 migrate`` is wired into the app, ``list`` / ``run``);
- the first migrator's ordered, each-step-fault-tolerant pipeline:
  - charter.md written exactly once (no overwrite window);
  - cross-file why -> charter, code-bound why -> colocated comments;
  - code-index first build produces md + json;
  - se3/specs deleted ONLY after charter + colocate confirmed;
  - .gitignore rewrite (idempotent whitelists);
  - per-step independent fault tolerance.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from se3.commands import migrate_cmd
from se3.commands.migrate_cmd import (
    ColocatedWhy,
    Migrator,
    SalvageResult,
    get_migrator,
    list_migrators,
    register_migrator,
    run_spec_to_new_system,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(root), check=True, capture_output=True, text=True
    )


def _init_git_project(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Tester")


class FakeSummarizer:
    """Deterministic code-index summariser (never touches the LLM)."""

    def __call__(self, targets):
        return {t.id: f"S:{t.name}" for t in targets}


def _make_salvager(charter_body: str, colocations=None):
    """Return a fake salvager that ignores its input and yields fixed output."""

    def _sal(_inp):
        return SalvageResult(
            charter_body=charter_body,
            colocations=list(colocations or []),
        )

    return _sal


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A git project with a base spec, two non-base specs, and one source file."""
    root = tmp_path / "proj"
    (root / "se3" / "specs" / "base").mkdir(parents=True)
    (root / "se3" / "specs" / "flow-engine").mkdir(parents=True)
    (root / "se3" / "specs" / "_changelog").mkdir(parents=True)
    (root / "src").mkdir(parents=True)

    (root / "se3" / "specs" / "base" / "spec.md").write_text(
        "<!-- spec-format: v1 -->\n# MyProj — Base Specification\n\n## Purpose\nx\n",
        encoding="utf-8",
    )
    (root / "se3" / "specs" / "flow-engine" / "spec.md").write_text(
        "<!-- spec-format: v1 -->\n# flow-engine Specification\n\n## Purpose\ny\n",
        encoding="utf-8",
    )
    (root / "se3" / "specs" / "_changelog" / "spec.md").write_text(
        "internal changelog — must be skipped\n", encoding="utf-8"
    )
    (root / "src" / "mod.py").write_text(
        '"""Module."""\n\n\ndef f():\n    return 1\n', encoding="utf-8"
    )
    (root / ".gitignore").write_text(
        "__pycache__/\n\n/se3/*\n!/se3/specs/\n", encoding="utf-8"
    )

    _init_git_project(root)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "snapshot")
    return root


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_registry_has_first_migrator():
    m = get_migrator("spec-to-new-system")
    assert m is not None
    assert m.run is run_spec_to_new_system
    assert m in list_migrators()


def test_registry_register_and_duplicate_rejection():
    mid = "test-temp-migrator"
    assert get_migrator(mid) is None
    try:
        register_migrator(Migrator(id=mid, description="d", run=lambda r, **k: None))
        assert get_migrator(mid) is not None
        with pytest.raises(ValueError):
            register_migrator(Migrator(id=mid, description="d2", run=lambda r, **k: None))
    finally:
        migrate_cmd.MIGRATORS.pop(mid, None)


def test_list_migrators_sorted():
    ids = [m.id for m in list_migrators()]
    assert ids == sorted(ids)


# ---------------------------------------------------------------------------
# CLI registration
# ---------------------------------------------------------------------------

def test_cli_registers_migrate():
    from se3 import cli

    # add_typer registers a (typer_instance, name) entry.
    names = {getattr(g, "name", None) for g in cli.app.registered_groups}
    assert "migrate" in names


def test_migrate_list_command_runs():
    from typer.testing import CliRunner

    from se3 import cli

    result = CliRunner().invoke(cli.app, ["migrate", "list"])
    assert result.exit_code == 0
    assert "spec-to-new-system" in result.stdout


def test_migrate_run_unknown_id_errors():
    from typer.testing import CliRunner

    from se3 import cli

    result = CliRunner().invoke(cli.app, ["migrate", "run", "no-such-migrator"])
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# First migrator — happy path
# ---------------------------------------------------------------------------

def test_full_migration_happy_path(project: Path):
    salvager = _make_salvager(
        charter_body="# MyProj — Charter\n\n## Purpose\nhigh-altitude only\n",
        colocations=[ColocatedWhy(file_path="src/mod.py", why="kept for X reason")],
    )
    report = run_spec_to_new_system(
        project, salvager=salvager, summarizer=FakeSummarizer()
    )
    assert report.ok, [(r.name, r.status, r.detail) for r in report.results]

    # charter written
    charter = project / "se3" / "charter.md"
    assert charter.exists()
    assert "high-altitude only" in charter.read_text(encoding="utf-8")

    # colocated why-comment landed in the source file
    src = (project / "src" / "mod.py").read_text(encoding="utf-8")
    assert migrate_cmd.WHY_MARKER in src
    assert "kept for X reason" in src
    # ... and the file still parses (comment above the docstring is safe)
    import ast

    ast.parse(src)

    # code-index built (md + json)
    assert (project / "se3" / "code-index.md").exists()
    assert (project / "se3" / "cache" / "code-index.json").exists()

    # specs deleted after salvage confirmed
    assert not (project / "se3" / "specs").exists()

    # gitignore rewritten
    gi = (project / ".gitignore").read_text(encoding="utf-8")
    assert "!/se3/code-index.md" in gi
    assert "!/se3/charter.md" in gi
    assert "!/se3/specs/" not in gi


def test_charter_written_exactly_once(project: Path, monkeypatch):
    """The charter body is assembled fully in memory then written ONCE (no
    overwrite window)."""
    writes = {"count": 0}
    real_write = Path.write_text

    def _counting_write(self, data, *a, **k):
        if self == (project / "se3" / "charter.md"):
            writes["count"] += 1
        return real_write(self, data, *a, **k)

    monkeypatch.setattr(Path, "write_text", _counting_write)
    run_spec_to_new_system(
        project,
        salvager=_make_salvager("# C\n\ncontent\n"),
        summarizer=FakeSummarizer(),
    )
    assert writes["count"] == 1


# ---------------------------------------------------------------------------
# Safety: specs are NOT deleted when salvage is incomplete
# ---------------------------------------------------------------------------

def test_specs_kept_when_charter_fails(project: Path):
    def _boom(_inp):
        raise RuntimeError("salvage exploded")

    report = run_spec_to_new_system(
        project, salvager=_boom, summarizer=FakeSummarizer()
    )
    # charter step failed; specs must survive
    assert (project / "se3" / "specs").exists()
    statuses = {r.name: r.status for r in report.results}
    assert statuses["Assemble charter"] == "FAIL"
    assert statuses["Delete se3/specs"] == "SKIP"
    # but the report is overall not-ok (a FAIL happened)
    assert not report.ok


def test_no_delete_specs_flag_keeps_specs(project: Path):
    report = run_spec_to_new_system(
        project,
        salvager=_make_salvager("# C\n\nx\n"),
        summarizer=FakeSummarizer(),
        delete_specs=False,
    )
    assert report.ok
    assert (project / "se3" / "specs").exists()
    statuses = {r.name: r.status for r in report.results}
    assert statuses["Delete se3/specs"] == "SKIP"


# ---------------------------------------------------------------------------
# Per-step fault tolerance: a colocation to a missing file is skipped, not fatal
# ---------------------------------------------------------------------------

def test_missing_colocation_target_is_skipped(project: Path):
    salvager = _make_salvager(
        "# C\n\nx\n",
        colocations=[
            ColocatedWhy(file_path="src/mod.py", why="real"),
            ColocatedWhy(file_path="src/does_not_exist.py", why="ghost"),
        ],
    )
    report = run_spec_to_new_system(
        project, salvager=salvager, summarizer=FakeSummarizer()
    )
    assert report.ok
    statuses = {r.name: r.status for r in report.results}
    assert statuses["Colocate why-comments"] == "OK"
    # the real one applied, the ghost noted as skipped
    assert any("does_not_exist" in n for n in report.notes)
    # A skipped colocation means that code-bound intent never landed in source,
    # so the spec corpus must be KEPT (salvage incomplete), not deleted.
    assert (project / "se3" / "specs").exists()
    assert statuses["Delete se3/specs"] == "SKIP"


def test_all_colocations_skipped_keeps_specs(project: Path):
    """If EVERY colocation is skipped (all targets unresolvable), the spec
    corpus must survive — the code-bound why/intent was never salvaged."""
    salvager = _make_salvager(
        "# C\n\nx\n",
        colocations=[
            ColocatedWhy(file_path="src/ghost_a.py", why="lost a"),
            ColocatedWhy(file_path="src/ghost_b.py", why="lost b"),
        ],
    )
    report = run_spec_to_new_system(
        project, salvager=salvager, summarizer=FakeSummarizer()
    )
    statuses = {r.name: r.status for r in report.results}
    assert statuses["Delete se3/specs"] == "SKIP"
    assert (project / "se3" / "specs").exists()


# ---------------------------------------------------------------------------
# .gitignore rewrite idempotency
# ---------------------------------------------------------------------------

def test_gitignore_rewrite_idempotent(project: Path):
    first = migrate_cmd._rewrite_gitignore(project)
    assert any("!/se3/code-index.md" in c for c in first)
    assert any("removed !/se3/specs/" in c for c in first)
    # second run is a no-op
    second = migrate_cmd._rewrite_gitignore(project)
    assert second == []


def test_gitignore_whitelists_after_se3_anchor(project: Path):
    migrate_cmd._rewrite_gitignore(project)
    lines = (project / ".gitignore").read_text(encoding="utf-8").splitlines()
    anchor = lines.index("/se3/*")
    # whitelists immediately follow the /se3/* anchor so the negations apply
    assert lines[anchor + 1] in migrate_cmd._GITIGNORE_WHITELISTS
    assert "!/se3/specs/" not in lines


# ---------------------------------------------------------------------------
# Corpus loading skips internal/hidden dirs and splits base vs non-base
# ---------------------------------------------------------------------------

def test_load_spec_corpus_splits_and_skips(project: Path):
    base, non_base = migrate_cmd._load_spec_corpus(project / "se3" / "specs")
    assert "Base Specification" in base
    assert set(non_base) == {"flow-engine"}  # _changelog skipped


# ---------------------------------------------------------------------------
# Colocation insertion preserves a shebang on line 1
# ---------------------------------------------------------------------------

def test_colocation_preserves_shebang(tmp_path: Path):
    f = tmp_path / "script.py"
    f.write_text("#!/usr/bin/env python\nx = 1\n", encoding="utf-8")
    migrate_cmd._insert_why_comment(f, "because")
    out = f.read_text(encoding="utf-8")
    assert out.splitlines()[0] == "#!/usr/bin/env python"
    assert migrate_cmd.WHY_MARKER in out
