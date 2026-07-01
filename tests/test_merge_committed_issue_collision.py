"""Tests for the git three-way-merge channel of ``se3 merge`` issue-ID
reconciliation (G3).

When two branches independently commit *different* issue files that parse to
the same numeric ID, a clean ``git merge`` leaves both files in the tree under
one ID — a silent duplicate. ``MergeOrchestrator._reconcile_committed_issue_ids``
detects this after the merge, keeps the side that already existed on the current
branch, renumbers the merge-introduced side to ``max(ID)+1`` via the shared G1
primitives (rename + trace + reference rewrite + ``.next_id`` advance), and lands
the change as an independent fix-up commit on top of the merge commit.

A byte-identical issue committed at the same path on both branches is folded
into one file by git itself, so it never looks like a collision and triggers no
renumber.

These tests drive ``_reconcile_committed_issue_ids`` directly after a real
``git merge`` — the exact call site the orchestrator uses inside
``_merge_single_branch`` right after a clean merge.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from se3.commands.merge.result_model import MergeReport
from se3.engine.merge.orchestrator import MergeOrchestrator
from se3.engine.merge.runtime_sync import IssueMergeRecord


# --------------------------------------------------------------------------
# git helpers
# --------------------------------------------------------------------------


def _git(path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True, text=True, check=True,
    )


def _init_repo(path: Path) -> None:
    """Init a repo whose se3/issues store is tracked but other se3/ runtime is not."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.email", "test@test.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("# Test\n")
    # Track the issue store; ignore the rest of se3/ runtime (locks, logs).
    (path / ".gitignore").write_text("/se3/*\n!/se3/specs/\n!/se3/issues/\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "initial")


def _head(path: Path) -> str:
    return _git(path, "rev-parse", "HEAD").stdout.strip()


def _parent(path: Path, ref: str) -> str:
    return _git(path, "rev-parse", f"{ref}^1").stdout.strip()


def _commits_since(path: Path, base: str) -> int:
    out = _git(path, "rev-list", "--count", f"{base}..HEAD").stdout.strip()
    return int(out)


def _working_tree_clean(path: Path) -> bool:
    out = _git(
        path, "status", "--porcelain", "--untracked-files=no",
    ).stdout.strip()
    return not out


def _write_issue_file(
    root: Path, status: str, issue_id: str, slug: str, description: str,
) -> Path:
    """Write a minimal valid issue YAML at se3/issues/<status>/<id>_<slug>.yaml."""
    directory = root / "se3" / "issues" / status
    directory.mkdir(parents=True, exist_ok=True)
    data = {
        "id": issue_id,
        "description": description,
        "status": "open" if status == "open" else "resolved",
        "tags": [],
        "source": "system",
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
    }
    path = directory / f"{issue_id}_{slug}.yaml"
    path.write_text(
        yaml.dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8",
    )
    return path


def _set_next_id(root: Path, value: str) -> None:
    (root / "se3" / "issues" / ".next_id").write_text(value)


def _read_next_id(root: Path) -> int:
    return int((root / "se3" / "issues" / ".next_id").read_text().strip())


def _open_issue_ids(root: Path) -> list[int]:
    directory = root / "se3" / "issues" / "open"
    ids: list[int] = []
    for f in directory.glob("*.yaml"):
        ids.append(int(f.name.split("_", 1)[0]))
    return sorted(ids)


def _find_issue_by_slug(root: Path, status: str, slug: str) -> Path | None:
    directory = root / "se3" / "issues" / status
    for f in directory.glob(f"*_{slug}.yaml"):
        return f
    return None


# --------------------------------------------------------------------------
# collision case: two different issues share a numeric ID
# --------------------------------------------------------------------------


def test_committed_id_collision_is_renumbered_and_committed(tmp_path: Path) -> None:
    """Two branches commit different issue #005; merge renumbers the incoming one."""
    root = tmp_path / "repo"
    _init_repo(root)
    main_branch = _default_branch(root)

    # Shared baseline (common ancestor): a base issue, a ref-holder that
    # points at #005, and a .next_id past the highest ID (010 -> next 11).
    _write_issue_file(root, "open", "001", "base", "Base issue one")
    _write_issue_file(
        root, "open", "010", "refholder",
        "Ref holder\n\nBlocked by #005 until it lands.",
    )
    _set_next_id(root, "11")
    _git(root, "add", "-A", "--", "se3/issues")
    _git(root, "commit", "-m", "seed issues")

    # Branch feature from the shared baseline (before main gains its 005).
    _git(root, "branch", "feature")

    # main creates its own issue 005.
    _write_issue_file(root, "open", "005", "main-issue", "Main issue five")
    _git(root, "add", "-A", "--", "se3/issues")
    _git(root, "commit", "-m", "main adds 005")

    # feature independently creates a *different* issue 005.
    _git(root, "checkout", "feature")
    _write_issue_file(root, "open", "005", "feature-issue", "Feature issue five")
    _git(root, "add", "-A", "--", "se3/issues")
    _git(root, "commit", "-m", "feature adds 005")

    # Back on main, capture pre-merge HEAD and run the real git merge.
    _git(root, "checkout", main_branch)
    pre_merge_sha = _head(root)
    _git(root, "merge", "feature", "--no-ff", "--no-edit", "-m", "Merge feature")
    merge_sha = _head(root)

    # Both 005 files are now present in the tree (a genuine collision).
    assert _find_issue_by_slug(root, "open", "main-issue") is not None
    assert _find_issue_by_slug(root, "open", "feature-issue") is not None

    orch = MergeOrchestrator(
        project_root=root, delete_merged=False, acquire_lock=False,
    )
    report = MergeReport()
    orch._reconcile_committed_issue_ids("feature", pre_merge_sha, report)

    # --- both issues preserved, no duplicate numeric ID ---
    ids = _open_issue_ids(root)
    assert len(ids) == len(set(ids)), f"duplicate numeric IDs remain: {ids}"
    # main's 005 is kept (it existed at pre_merge_sha); feature's is renumbered.
    kept = _find_issue_by_slug(root, "open", "main-issue")
    assert kept is not None and kept.name == "005_main-issue.yaml"
    renumbered = _find_issue_by_slug(root, "open", "feature-issue")
    assert renumbered is not None
    new_num = int(renumbered.name.split("_", 1)[0])
    # New ID is the global max + 1 (010 was highest -> 011).
    assert new_num == 11
    assert set(ids) == {1, 5, 10, 11}

    # --- renumbered issue carries the old->new trace ---
    body = renumbered.read_text(encoding="utf-8")
    data = yaml.safe_load(body)
    assert data["id"] == "011"
    assert "旧号 #005 → 新号 #011" in data["description"]
    # display-title source (first non-empty line) is unchanged by the trace.
    assert data["description"].splitlines()[0] == "Feature issue five"

    # --- the #005 cross-reference was rewritten to #011 ---
    ref_holder = _find_issue_by_slug(root, "open", "refholder")
    assert ref_holder is not None
    ref_text = ref_holder.read_text(encoding="utf-8")
    assert "#011" in ref_text
    assert "#005" not in ref_text

    # --- .next_id advanced to the new global max + 1 ---
    assert _read_next_id(root) == 12

    # --- the renumber landed as one independent commit on top of the merge ---
    assert _commits_since(root, merge_sha) == 1
    assert _parent(root, _head(root)) == merge_sha
    assert _working_tree_clean(root)

    # --- the report records the renumber as an IssueMergeRecord ---
    assert len(report.committed_issue_renumbers) == 1
    rec = report.committed_issue_renumbers[0]
    assert isinstance(rec, IssueMergeRecord)
    assert rec.old_id == "005"
    assert rec.new_id == "011"
    assert rec.status_dir == "open"


# --------------------------------------------------------------------------
# no-collision case: byte-identical same-path issue is folded by git
# --------------------------------------------------------------------------


def test_identical_issue_same_path_is_not_renumbered(tmp_path: Path) -> None:
    """Both branches commit the *same* issue #005; git folds it — no renumber."""
    root = tmp_path / "repo"
    _init_repo(root)
    main_branch = _default_branch(root)

    _write_issue_file(root, "open", "001", "base", "Base issue one")
    _set_next_id(root, "6")
    _git(root, "add", "-A", "--", "se3/issues")
    _git(root, "commit", "-m", "seed issues")
    _git(root, "branch", "feature")

    identical = "Shared issue five\n\nExact same content on both branches."

    # main adds 005 with content X.
    _write_issue_file(root, "open", "005", "shared", identical)
    _git(root, "add", "-A", "--", "se3/issues")
    _git(root, "commit", "-m", "main adds shared 005")

    # feature adds the byte-identical 005 at the same path.
    _git(root, "checkout", "feature")
    _write_issue_file(root, "open", "005", "shared", identical)
    _git(root, "add", "-A", "--", "se3/issues")
    _git(root, "commit", "-m", "feature adds shared 005")

    _git(root, "checkout", main_branch)
    pre_merge_sha = _head(root)
    # Identical add/add resolves cleanly to a single file (no conflict).
    _git(root, "merge", "feature", "--no-ff", "--no-edit", "-m", "Merge feature")
    merge_sha = _head(root)

    # Only one 005 file exists — git already folded the two identical adds.
    assert len(list((root / "se3" / "issues" / "open").glob("005_*.yaml"))) == 1

    orch = MergeOrchestrator(
        project_root=root, delete_merged=False, acquire_lock=False,
    )
    report = MergeReport()
    orch._reconcile_committed_issue_ids("feature", pre_merge_sha, report)

    # No collision -> no renumber, no extra commit, counter untouched.
    assert report.committed_issue_renumbers == []
    assert _head(root) == merge_sha
    assert _commits_since(root, merge_sha) == 0
    assert _read_next_id(root) == 6
    assert _working_tree_clean(root)
    assert _open_issue_ids(root) == [1, 5]


# --------------------------------------------------------------------------
# default-branch name helper (git init may produce master or main)
# --------------------------------------------------------------------------


def _default_branch(root: Path) -> str:
    return _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
