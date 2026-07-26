"""Tests for the ``rename-to-tianluo`` migrator (issue #270).

Covers the one-shot layout rename of an existing legacy project:
``se3/`` -> ``tianluo/`` (git mv, history preserved, untracked runtime
content carried along), config-file renames, ``.gitignore`` rule rewrite,
and the single reviewable commit — plus idempotence and the dirty-index
commit guard.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tianluo.commands.migrate_cmd import (
    get_migrator,
    run_rename_to_tianluo,
)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, check=True,
    )


@pytest.fixture
def legacy_project(tmp_path: Path) -> Path:
    """A committed legacy-layout project: se3/ runtime + se3.yaml + gitignore."""
    root = tmp_path / "proj"
    (root / "se3" / "issues" / "open").mkdir(parents=True)
    (root / "se3" / "charter.md").write_text("# charter\n", encoding="utf-8")
    (root / "se3" / "code-index.md").write_text("# index\n", encoding="utf-8")
    (root / "se3.yaml").write_text("language:\n  language: en-US\n", encoding="utf-8")
    (root / "se3.local.yaml").write_text("test:\n  command: pytest\n", encoding="utf-8")
    (root / ".gitignore").write_text(
        "/se3/*\n!/se3/charter.md\n!/se3/code-index.md\n!/se3/issues/\n"
        "se3.local.yaml\n",
        encoding="utf-8",
    )
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Tester")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "legacy snapshot")
    # Untracked runtime state must ride along with the directory rename.
    (root / "se3" / "state").mkdir()
    (root / "se3" / "state" / "engine.json").write_text("{}", encoding="utf-8")
    return root


def test_registered():
    m = get_migrator("rename-to-tianluo")
    assert m is not None
    assert m.run is run_rename_to_tianluo


def test_full_rename_happy_path(legacy_project: Path):
    report = run_rename_to_tianluo(legacy_project)
    assert report.ok, [(r.name, r.status, r.detail) for r in report.results]

    # layout moved, untracked state carried along
    assert (legacy_project / "tianluo" / "charter.md").is_file()
    assert (legacy_project / "tianluo" / "state" / "engine.json").is_file()
    assert not (legacy_project / "se3").exists()
    assert (legacy_project / "tianluo.yaml").is_file()
    assert (legacy_project / "tianluo.local.yaml").is_file()
    assert not (legacy_project / "se3.yaml").exists()

    # gitignore rewritten to tianluo names
    gi = (legacy_project / ".gitignore").read_text(encoding="utf-8")
    assert "/tianluo/*" in gi and "!/tianluo/charter.md" in gi
    assert "tianluo.local.yaml" in gi and "se3" not in gi

    # exactly one new commit, and it is a rename commit (history preserved)
    log = _git(legacy_project, "log", "--oneline").stdout.strip().splitlines()
    assert len(log) == 2
    assert "migrate(rename)" in log[0]
    show = _git(
        legacy_project, "show", "--name-status", "--format=", "-M", "HEAD"
    ).stdout
    assert "tianluo/charter.md" in show
    # working tree + index clean afterwards (untracked state is gitignored)
    status = _git(legacy_project, "status", "--porcelain").stdout.strip()
    assert status == ""


def test_idempotent_second_run(legacy_project: Path):
    assert run_rename_to_tianluo(legacy_project).ok
    second = run_rename_to_tianluo(legacy_project)
    assert second.ok
    statuses = {r.name: r.status for r in second.results}
    assert statuses["rename-dir"] == "SKIP"
    assert statuses["rename-se3.yaml"] == "SKIP"
    assert statuses["commit"] == "SKIP"
    # still only the one migration commit
    log = _git(legacy_project, "log", "--oneline").stdout.strip().splitlines()
    assert len(log) == 2


def test_dirty_index_renames_but_skips_commit(legacy_project: Path):
    (legacy_project / "unrelated.txt").write_text("x\n", encoding="utf-8")
    _git(legacy_project, "add", "unrelated.txt")

    report = run_rename_to_tianluo(legacy_project)
    assert report.ok
    statuses = {r.name: r.status for r in report.results}
    assert statuses["rename-dir"] == "OK"
    assert statuses["commit"] == "SKIP"
    assert any("staged changes" in n for n in report.notes)
    # nothing was committed
    log = _git(legacy_project, "log", "--oneline").stdout.strip().splitlines()
    assert len(log) == 1


def test_non_git_directory_fails_preflight(tmp_path: Path):
    (tmp_path / "se3").mkdir()
    report = run_rename_to_tianluo(tmp_path)
    assert not report.ok
    assert report.results[0].name == "preflight"
    assert report.results[0].status == "FAIL"


def test_existing_tianluo_dir_skips_dir_rename(legacy_project: Path):
    (legacy_project / "tianluo").mkdir()
    report = run_rename_to_tianluo(legacy_project)
    statuses = {r.name: r.status for r in report.results}
    assert statuses["rename-dir"] == "SKIP"
