"""Tests for the git three-way-merge channel of ``luo merge`` issue-ID
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

import logging
import subprocess
from pathlib import Path

import yaml

from tianluo.commands.merge.result_model import MergeReport
from tianluo.engine.merge.orchestrator import MergeOrchestrator
from tianluo.engine.merge.runtime_sync import IssueMergeRecord


# --------------------------------------------------------------------------
# git helpers
# --------------------------------------------------------------------------


def _git(path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True, text=True, check=True,
    )


def _init_repo(path: Path) -> None:
    """Init a repo whose tianluo/issues store is tracked but other tianluo/ runtime is not."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.email", "test@test.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("# Test\n")
    # Track the issue store; ignore the rest of tianluo/ runtime (locks, logs).
    (path / ".gitignore").write_text("/tianluo/*\n!/tianluo/specs/\n!/tianluo/issues/\n")
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
    """Write a minimal valid issue YAML at tianluo/issues/<status>/<id>_<slug>.yaml."""
    directory = root / "tianluo" / "issues" / status
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
    (root / "tianluo" / "issues" / ".next_id").write_text(value)


def _read_next_id(root: Path) -> int:
    return int((root / "tianluo" / "issues" / ".next_id").read_text().strip())


def _open_issue_ids(root: Path) -> list[int]:
    directory = root / "tianluo" / "issues" / "open"
    ids: list[int] = []
    for f in directory.glob("*.yaml"):
        ids.append(int(f.name.split("_", 1)[0]))
    return sorted(ids)


def _find_issue_by_slug(root: Path, status: str, slug: str) -> Path | None:
    directory = root / "tianluo" / "issues" / status
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
    _git(root, "add", "-A", "--", "tianluo/issues")
    _git(root, "commit", "-m", "seed issues")

    # Branch feature from the shared baseline (before main gains its 005).
    _git(root, "branch", "feature")

    # main creates its own issue 005.
    _write_issue_file(root, "open", "005", "main-issue", "Main issue five")
    _git(root, "add", "-A", "--", "tianluo/issues")
    _git(root, "commit", "-m", "main adds 005")

    # feature independently creates a *different* issue 005 whose body carries
    # an incoming-side self-reference to its own #005.
    _git(root, "checkout", "feature")
    _write_issue_file(
        root, "open", "005", "feature-issue",
        "Feature issue five\n\nSee #005 for context.",
    )
    _git(root, "add", "-A", "--", "tianluo/issues")
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
    # The incoming issue's *own* #005 self-reference points at the renumbered
    # issue, so it is repointed to #011 (the trace's #005 is historical, kept).
    assert "See #011 for context." in data["description"]

    # --- the kept side's #005 reference is left untouched ---
    # The ancestor ref-holder existed before the merge; its "Blocked by #005"
    # names the issue that KEPT id 005 (main's), not the renumbered incoming
    # one. Repointing it to #011 would silently corrupt the reference.
    ref_holder = _find_issue_by_slug(root, "open", "refholder")
    assert ref_holder is not None
    ref_text = ref_holder.read_text(encoding="utf-8")
    assert "#005" in ref_text
    assert "#011" not in ref_text

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
# collision case: the branch also EDITED a pre-existing file to reference
# its (renumbered) issue, and .next_id was stale-ahead
# --------------------------------------------------------------------------


def test_references_added_to_preexisting_files_follow_the_renumber(
    tmp_path: Path,
) -> None:
    """A #old reference the branch ADDED to an already-existing issue file is
    repointed to #new, while that same file's pre-existing #old stays.

    Also pins the allocation discipline: the committed channel reserves the new
    number through the SHARED ``.next_id`` primitive — ``max(counter, on-disk
    max + 1)`` under the counter lock — not a bare on-disk ``max + 1`` scan. A
    counter sitting AHEAD of the store (100, with the store's max at 010) is the
    next free number, so the renumber takes 100 and the counter advances to 101.
    Routing through the shared primitive is what stops the collision-repair
    machinery from re-minting a number a concurrent ``luo issue create`` (which
    does not contend on the merge lock) has just reserved.
    """
    root = tmp_path / "repo"
    _init_repo(root)
    main_branch = _default_branch(root)

    # Shared baseline: a ref-holder whose body ALREADY references #005 (it
    # will mean main's kept issue), and a counter ahead of the store (100 is
    # the next free number the shared allocator will hand out).
    _write_issue_file(root, "open", "001", "base", "Base issue one")
    _write_issue_file(
        root, "open", "010", "refholder",
        "Ref holder\n\nLegacy pointer #005.",
    )
    _set_next_id(root, "100")
    _git(root, "add", "-A", "--", "tianluo/issues")
    _git(root, "commit", "-m", "seed issues")
    _git(root, "branch", "feature")

    # main creates its own issue 005 (the keeper).
    _write_issue_file(root, "open", "005", "main-issue", "Main issue five")
    _git(root, "add", "-A", "--", "tianluo/issues")
    _git(root, "commit", "-m", "main adds 005")

    # feature creates a colliding 005 AND edits the pre-existing ref-holder,
    # appending a #005 reference that means ITS new issue.
    _git(root, "checkout", "feature")
    _write_issue_file(root, "open", "005", "feature-issue", "Feature issue five")
    _write_issue_file(
        root, "open", "010", "refholder",
        "Ref holder\n\nLegacy pointer #005.\nBlocked by #005 until it lands.",
    )
    _git(root, "add", "-A", "--", "tianluo/issues")
    _git(root, "commit", "-m", "feature adds 005 and edits refholder")

    _git(root, "checkout", main_branch)
    pre_merge_sha = _head(root)
    _git(root, "merge", "feature", "--no-ff", "--no-edit", "-m", "Merge feature")
    merge_sha = _head(root)

    orch = MergeOrchestrator(
        project_root=root, delete_merged=False, acquire_lock=False,
    )
    report = MergeReport()
    orch._reconcile_committed_issue_ids("feature", pre_merge_sha, report)

    # Feature's issue renumbered to the shared allocator's next value: the
    # counter (100) is ahead of the store's on-disk max (010), so the reserve
    # hands out 100, not a bare on-disk max+1 (011).
    renumbered = _find_issue_by_slug(root, "open", "feature-issue")
    assert renumbered is not None
    assert renumbered.name.startswith("100_")

    # The ref-holder existed at pre_merge_sha, so it is NOT a wholesale
    # incoming file — but the line the branch ADDED meant the branch's own
    # issue and must follow the renumber; the pre-existing line still names
    # main's kept #005 and must not move.
    ref_holder = _find_issue_by_slug(root, "open", "refholder")
    assert ref_holder is not None
    ref_text = ref_holder.read_text(encoding="utf-8")
    assert "Legacy pointer #005." in ref_text
    assert "Blocked by #100 until it lands." in ref_text

    # The reserve consumed the ahead counter (100 -> handed out, counter now
    # 101). The renumbered file now IS the on-disk max, so the counter ends
    # exactly one past it — consistent, and never lowered.
    assert _read_next_id(root) == 101

    # The renumber (including the ref-holder edit) landed in the fix-up commit.
    assert _commits_since(root, merge_sha) == 1
    assert _working_tree_clean(root)


# --------------------------------------------------------------------------
# collision case: the parsed ``id`` fields collide even though the filename
# prefixes differ
# --------------------------------------------------------------------------


def _write_issue_file_with_id(
    root: Path, status: str, filename_id: str, record_id: str,
    slug: str, description: str,
) -> Path:
    """Write an issue whose filename prefix and stored ``id`` field differ."""
    directory = root / "tianluo" / "issues" / status
    directory.mkdir(parents=True, exist_ok=True)
    data = {
        "id": record_id,
        "description": description,
        "status": "open" if status == "open" else "resolved",
        "tags": [],
        "source": "system",
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
    }
    path = directory / f"{filename_id}_{slug}.yaml"
    path.write_text(
        yaml.dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8",
    )
    return path


def test_collision_detected_by_parsed_id_not_filename_prefix(
    tmp_path: Path,
) -> None:
    """Two files whose parsed ``id`` fields agree collide even when their
    filename prefixes differ (005_a.yaml and 006_b.yaml both carrying id 007).
    """
    root = tmp_path / "repo"
    _init_repo(root)
    main_branch = _default_branch(root)

    _write_issue_file(root, "open", "001", "base", "Base issue one")
    _set_next_id(root, "2")
    _git(root, "add", "-A", "--", "tianluo/issues")
    _git(root, "commit", "-m", "seed issues")
    _git(root, "branch", "feature")

    # main commits a record whose stored id (007) disagrees with its filename
    # prefix (005).
    _write_issue_file_with_id(
        root, "open", "005", "007", "main-mismatch", "Main issue seven",
    )
    _git(root, "add", "-A", "--", "tianluo/issues")
    _git(root, "commit", "-m", "main adds id-007 record")

    # feature independently commits a DIFFERENT record that also parses to
    # id 007, under yet another filename prefix.
    _git(root, "checkout", "feature")
    _write_issue_file_with_id(
        root, "open", "006", "007", "feature-mismatch", "Feature issue seven",
    )
    _git(root, "add", "-A", "--", "tianluo/issues")
    _git(root, "commit", "-m", "feature adds id-007 record")

    _git(root, "checkout", main_branch)
    pre_merge_sha = _head(root)
    _git(root, "merge", "feature", "--no-ff", "--no-edit", "-m", "Merge feature")
    merge_sha = _head(root)

    orch = MergeOrchestrator(
        project_root=root, delete_merged=False, acquire_lock=False,
    )
    report = MergeReport()
    orch._reconcile_committed_issue_ids("feature", pre_merge_sha, report)

    # main's record kept its id; feature's was renumbered past the global max
    # (which the parsed id 007 defines, not the filename prefixes 005/006).
    kept = _find_issue_by_slug(root, "open", "main-mismatch")
    assert kept is not None and kept.name == "005_main-mismatch.yaml"
    assert yaml.safe_load(kept.read_text(encoding="utf-8"))["id"] == "007"

    renumbered = _find_issue_by_slug(root, "open", "feature-mismatch")
    assert renumbered is not None
    assert renumbered.name == "008_feature-mismatch.yaml"
    data = yaml.safe_load(renumbered.read_text(encoding="utf-8"))
    assert data["id"] == "008"
    assert "旧号 #007 → 新号 #008" in data["description"]

    # No two records share a parsed numeric id anymore.
    parsed_ids = [
        yaml.safe_load(f.read_text(encoding="utf-8"))["id"]
        for f in (root / "tianluo" / "issues" / "open").glob("*.yaml")
    ]
    assert len(parsed_ids) == len(set(parsed_ids))

    assert len(report.committed_issue_renumbers) == 1
    rec = report.committed_issue_renumbers[0]
    assert (rec.old_id, rec.new_id) == ("007", "008")

    assert _commits_since(root, merge_sha) == 1
    assert _working_tree_clean(root)


# --------------------------------------------------------------------------
# collision case: TWO incoming files share the same old ID — their own #005
# tokens are ambiguous (self OR peer) and must not be forced to self
# --------------------------------------------------------------------------


def test_multiple_incoming_files_sharing_old_id_keep_ambiguous_references(
    tmp_path: Path,
) -> None:
    """When the merge brings in two different files both carrying #005, a
    ``#005`` inside either one could mean the issue itself OR its colliding
    peer — rewriting it to the holder's own new ID would silently turn a peer
    reference into a self-reference. The token keeps its digits and the
    ambiguity is recorded durably in each holder and in the merge report.
    """
    root = tmp_path / "repo"
    _init_repo(root)
    main_branch = _default_branch(root)

    _write_issue_file(root, "open", "001", "base", "Base issue one")
    _write_issue_file(
        root, "open", "010", "refholder",
        "Ref holder\n\nBlocked by #005 until it lands.",
    )
    _set_next_id(root, "11")
    _git(root, "add", "-A", "--", "tianluo/issues")
    _git(root, "commit", "-m", "seed issues")
    _git(root, "branch", "feature")

    # main creates the keeper 005.
    _write_issue_file(root, "open", "005", "main-issue", "Main issue five")
    _git(root, "add", "-A", "--", "tianluo/issues")
    _git(root, "commit", "-m", "main adds 005")

    # feature commits TWO different files that both parse to id 005, each
    # with a self-reference to #005.
    _git(root, "checkout", "feature")
    _write_issue_file(
        root, "open", "005", "feature-a",
        "Feature issue A\n\nSee #005 for context.",
    )
    _write_issue_file(
        root, "open", "005", "feature-b",
        "Feature issue B\n\nSee #005 for context.",
    )
    _git(root, "add", "-A", "--", "tianluo/issues")
    _git(root, "commit", "-m", "feature adds two 005 files")

    _git(root, "checkout", main_branch)
    pre_merge_sha = _head(root)
    _git(root, "merge", "feature", "--no-ff", "--no-edit", "-m", "Merge feature")
    merge_sha = _head(root)

    orch = MergeOrchestrator(
        project_root=root, delete_merged=False, acquire_lock=False,
    )
    report = MergeReport()
    orch._reconcile_committed_issue_ids("feature", pre_merge_sha, report)

    # Both incoming issues preserved under distinct fresh IDs; keeper intact.
    ids = _open_issue_ids(root)
    assert len(ids) == len(set(ids)), f"duplicate numeric IDs remain: {ids}"
    assert set(ids) == {1, 5, 10, 11, 12}
    kept = _find_issue_by_slug(root, "open", "main-issue")
    assert kept is not None and kept.name == "005_main-issue.yaml"

    # Deterministic loser order (sorted paths): feature-a -> 011, then
    # feature-b -> 012. Each body's #005 keeps its digits — it could have
    # meant the colliding peer — and carries the durable ambiguity note
    # listing both candidates, plus its own old->new trace.
    issue_a = _find_issue_by_slug(root, "open", "feature-a")
    assert issue_a is not None and issue_a.name.startswith("011_")
    data_a = yaml.safe_load(issue_a.read_text(encoding="utf-8"))
    assert data_a["id"] == "011"
    assert "See #005 for context." in data_a["description"]
    assert "See #011 for context." not in data_a["description"]
    assert "See #012 for context." not in data_a["description"]
    assert "歧义引用 #005 → 候选 #011 / #012 (luo merge)" in data_a["description"]
    assert "旧号 #005 → 新号 #011" in data_a["description"]

    issue_b = _find_issue_by_slug(root, "open", "feature-b")
    assert issue_b is not None and issue_b.name.startswith("012_")
    data_b = yaml.safe_load(issue_b.read_text(encoding="utf-8"))
    assert data_b["id"] == "012"
    assert "See #005 for context." in data_b["description"]
    assert "See #011 for context." not in data_b["description"]
    assert "See #012 for context." not in data_b["description"]
    assert "歧义引用 #005 → 候选 #011 / #012 (luo merge)" in data_b["description"]
    assert "旧号 #005 → 新号 #012" in data_b["description"]

    # The pre-existing #005 reference still names the kept issue: it was on
    # the current branch before the merge, so it is not ambiguous — no note,
    # no rewrite.
    ref_holder = _find_issue_by_slug(root, "open", "refholder")
    ref_text = ref_holder.read_text(encoding="utf-8")
    assert "#005" in ref_text
    assert "#011" not in ref_text and "#012" not in ref_text
    assert "歧义引用" not in ref_text

    # Both ambiguous holders are surfaced in the report (order follows the
    # directory scan, so compare order-insensitively).
    assert sorted(
        (e["file"], e["old_id"], tuple(e["candidates"]))
        for e in report.ambiguous_issue_references
    ) == [
        ("tianluo/issues/open/011_feature-a.yaml", "005", ("011", "012")),
        ("tianluo/issues/open/012_feature-b.yaml", "005", ("011", "012")),
    ]

    assert _read_next_id(root) == 13
    assert len(report.committed_issue_renumbers) == 2
    assert {(r.old_id, r.new_id) for r in report.committed_issue_renumbers} == {
        ("005", "011"), ("005", "012"),
    }
    assert _commits_since(root, merge_sha) == 1
    assert _working_tree_clean(root)


def test_incoming_keeper_keeps_group_ambiguous(tmp_path: Path) -> None:
    """A merge-introduced fallback keeper still makes ``#old`` ambiguous.

    The branch commits TWO different files both carrying id 005 while main
    never had a 005: no colliding copy existed at pre_merge_sha, so the
    lexicographically-first branch file becomes the keeper and RETAINS 005
    while its peer is renumbered. A remaining ``#005`` may then mean either
    branch file — judging ambiguity only by the renumbered count would call
    this a single-loser case and repoint every incoming ``#005`` at the
    loser, silently corrupting keeper references. The tokens must keep their
    digits, with the keeper's kept number on the recorded candidate list.
    """
    root = tmp_path / "repo"
    _init_repo(root)
    main_branch = _default_branch(root)

    _write_issue_file(root, "open", "001", "base", "Base issue one")
    _write_issue_file(root, "open", "010", "highest", "Highest issue ten")
    _set_next_id(root, "11")
    _git(root, "add", "-A", "--", "tianluo/issues")
    _git(root, "commit", "-m", "seed issues")
    _git(root, "branch", "feature")

    # main gains an unrelated commit so the merge is a real three-way merge —
    # but never creates a 005 of its own.
    (root / "README.md").write_text("# Test\nmain moved on\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "main non-issue work")

    # feature commits TWO different files that both parse to id 005, each
    # with a #005 reference that could mean itself OR its colliding peer.
    _git(root, "checkout", "feature")
    _write_issue_file(
        root, "open", "005", "feature-a",
        "Feature issue A\n\nSee #005 for context.",
    )
    _write_issue_file(
        root, "open", "005", "feature-b",
        "Feature issue B\n\nSee #005 for context.",
    )
    _git(root, "add", "-A", "--", "tianluo/issues")
    _git(root, "commit", "-m", "feature adds two 005 files")

    _git(root, "checkout", main_branch)
    pre_merge_sha = _head(root)
    _git(root, "merge", "feature", "--no-ff", "--no-edit", "-m", "Merge feature")
    merge_sha = _head(root)

    orch = MergeOrchestrator(
        project_root=root, delete_merged=False, acquire_lock=False,
    )
    report = MergeReport()
    orch._reconcile_committed_issue_ids("feature", pre_merge_sha, report)

    # Both issues preserved, no duplicate ID: feature-a (lexicographically
    # first) kept 005, feature-b renumbered to max+1.
    ids = _open_issue_ids(root)
    assert len(ids) == len(set(ids)), f"duplicate numeric IDs remain: {ids}"
    assert set(ids) == {1, 5, 10, 11}
    kept = _find_issue_by_slug(root, "open", "feature-a")
    assert kept is not None and kept.name == "005_feature-a.yaml"
    renumbered = _find_issue_by_slug(root, "open", "feature-b")
    assert renumbered is not None and renumbered.name.startswith("011_")

    # Neither file's #005 was repointed at the loser's new ID — it could
    # equally mean the keeper, which still holds 005 — and both carry the
    # durable note listing the keeper AND the renumbered peer as candidates.
    data_kept = yaml.safe_load(kept.read_text(encoding="utf-8"))
    assert data_kept["id"] == "005"
    assert "See #005 for context." in data_kept["description"]
    assert "See #011 for context." not in data_kept["description"]
    assert "歧义引用 #005 → 候选 #005 / #011 (luo merge)" in data_kept["description"]
    assert "旧号" not in data_kept["description"]  # the keeper kept its number

    data_ren = yaml.safe_load(renumbered.read_text(encoding="utf-8"))
    assert data_ren["id"] == "011"
    assert "See #005 for context." in data_ren["description"]
    assert "See #011 for context." not in data_ren["description"]
    assert "歧义引用 #005 → 候选 #005 / #011 (luo merge)" in data_ren["description"]
    assert "旧号 #005 → 新号 #011" in data_ren["description"]

    # Both ambiguous holders surface in the report with the kept number on
    # the candidate list.
    assert sorted(
        (e["file"], e["old_id"], tuple(e["candidates"]))
        for e in report.ambiguous_issue_references
    ) == [
        ("tianluo/issues/open/005_feature-a.yaml", "005", ("005", "011")),
        ("tianluo/issues/open/011_feature-b.yaml", "005", ("005", "011")),
    ]

    assert _read_next_id(root) == 12
    assert len(report.committed_issue_renumbers) == 1
    rec = report.committed_issue_renumbers[0]
    assert (rec.old_id, rec.new_id) == ("005", "011")
    assert _commits_since(root, merge_sha) == 1
    assert _working_tree_clean(root)


def test_preexisting_keeper_on_branch_keeps_references_ambiguous(
    tmp_path: Path,
) -> None:
    """A keeper that predates the fork makes branch ``#old`` refs ambiguous.

    Issue #005 exists BEFORE the feature branch forks, so the branch's own
    store inherits it. The branch then adds a second distinct issue also
    numbered 005 plus a dependent issue saying ``Blocked by #005`` — inside
    the branch's store that reference could mean the inherited keeper OR the
    branch's new issue. A single renumbered loser next to a pre-existing
    keeper must NOT be read as proof that ``#005`` meant the loser: the
    tokens keep their digits and the ambiguity is recorded durably, with the
    keeper's kept number on the candidate list.
    """
    root = tmp_path / "repo"
    _init_repo(root)
    main_branch = _default_branch(root)

    # Keeper 005 is part of the shared baseline — the branch inherits it.
    _write_issue_file(root, "open", "001", "base", "Base issue one")
    _write_issue_file(root, "open", "005", "main-issue", "Main issue five")
    _write_issue_file(root, "open", "010", "highest", "Highest issue ten")
    _set_next_id(root, "11")
    _git(root, "add", "-A", "--", "tianluo/issues")
    _git(root, "commit", "-m", "seed issues incl. keeper 005")
    _git(root, "branch", "feature")

    # main gains an unrelated commit so the merge is a real three-way merge.
    (root / "README.md").write_text("# Test\nmain moved on\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "main non-issue work")

    # feature adds a SECOND distinct 005 (with a #005 self-or-keeper
    # reference) plus a dependent issue referencing #005.
    _git(root, "checkout", "feature")
    _write_issue_file(
        root, "open", "005", "feature-issue",
        "Feature issue five\n\nSee #005 for context.",
    )
    _write_issue_file(
        root, "open", "006", "dep",
        "Dependent feature issue\n\nBlocked by #005 until it lands.",
    )
    _git(root, "add", "-A", "--", "tianluo/issues")
    _git(root, "commit", "-m", "feature adds colliding 005 and a dependent")

    _git(root, "checkout", main_branch)
    pre_merge_sha = _head(root)
    _git(root, "merge", "feature", "--no-ff", "--no-edit", "-m", "Merge feature")
    merge_sha = _head(root)

    orch = MergeOrchestrator(
        project_root=root, delete_merged=False, acquire_lock=False,
    )
    report = MergeReport()
    orch._reconcile_committed_issue_ids("feature", pre_merge_sha, report)

    # Both issues preserved, no duplicate ID: the pre-existing keeper stays
    # on 005, the branch's file is renumbered to max+1.
    ids = _open_issue_ids(root)
    assert len(ids) == len(set(ids)), f"duplicate numeric IDs remain: {ids}"
    assert set(ids) == {1, 5, 6, 10, 11}
    kept = _find_issue_by_slug(root, "open", "main-issue")
    assert kept is not None and kept.name == "005_main-issue.yaml"
    renumbered = _find_issue_by_slug(root, "open", "feature-issue")
    assert renumbered is not None and renumbered.name.startswith("011_")

    # The dependent issue's #005 keeps its digits — it could have meant the
    # inherited keeper — and carries the durable note naming both candidates.
    dep = _find_issue_by_slug(root, "open", "dep")
    dep_body = yaml.safe_load(dep.read_text(encoding="utf-8"))["description"]
    assert "Blocked by #005 until it lands." in dep_body
    assert "Blocked by #011" not in dep_body
    assert "歧义引用 #005 → 候选 #005 / #011 (luo merge)" in dep_body

    # The renumbered file's own #005 is equally undecidable (keeper or self):
    # not repointed, same note, plus its old->new trace.
    data_ren = yaml.safe_load(renumbered.read_text(encoding="utf-8"))
    assert data_ren["id"] == "011"
    assert "See #005 for context." in data_ren["description"]
    assert "See #011 for context." not in data_ren["description"]
    assert "歧义引用 #005 → 候选 #005 / #011 (luo merge)" in data_ren["description"]
    assert "旧号 #005 → 新号 #011" in data_ren["description"]

    # The keeper itself is untouched — no note, no rewrite.
    kept_text = kept.read_text(encoding="utf-8")
    assert "歧义引用" not in kept_text and "#011" not in kept_text

    # Both ambiguous holders surface in the report with the keeper's kept
    # number on the candidate list.
    assert sorted(
        (e["file"], e["old_id"], tuple(e["candidates"]))
        for e in report.ambiguous_issue_references
    ) == [
        ("tianluo/issues/open/006_dep.yaml", "005", ("005", "011")),
        ("tianluo/issues/open/011_feature-issue.yaml", "005", ("005", "011")),
    ]

    assert _read_next_id(root) == 12
    assert len(report.committed_issue_renumbers) == 1
    rec = report.committed_issue_renumbers[0]
    assert (rec.old_id, rec.new_id) == ("005", "011")
    assert _commits_since(root, merge_sha) == 1
    assert _working_tree_clean(root)


def test_ambiguous_incoming_reference_is_recorded_not_guessed(
    tmp_path: Path,
) -> None:
    """A merge-added reference to a SHARED old ID is annotated, not repointed.

    The feature branch contributes two different files both parsing to id 005
    AND a third issue saying ``Blocked by #005``. Nothing proves which of the
    two colliding feature issues that reference meant, so it must keep its
    original digits — rewriting it to either candidate would silently corrupt
    the cross-reference — while the ambiguity is recorded durably: a note in
    the referencing issue and an entry in the merge report.
    """
    root = tmp_path / "repo"
    _init_repo(root)
    main_branch = _default_branch(root)

    _write_issue_file(root, "open", "001", "base", "Base issue one")
    _set_next_id(root, "2")
    _git(root, "add", "-A", "--", "tianluo/issues")
    _git(root, "commit", "-m", "seed issues")
    _git(root, "branch", "feature")

    # main creates the keeper 005.
    _write_issue_file(root, "open", "005", "main-issue", "Main issue five")
    _git(root, "add", "-A", "--", "tianluo/issues")
    _git(root, "commit", "-m", "main adds 005")

    # feature commits two different 005 files plus a third issue whose body
    # references #005 — intending one of ITS OWN 005 issues.
    _git(root, "checkout", "feature")
    _write_issue_file(
        root, "open", "005", "feature-a",
        "Feature issue A\n\nSee #005 for context.",
    )
    _write_issue_file(
        root, "open", "005", "feature-b",
        "Feature issue B\n\nSee #005 for context.",
    )
    _write_issue_file(
        root, "open", "006", "dep",
        "Dependent feature issue\n\nBlocked by #005 until it lands.",
    )
    _git(root, "add", "-A", "--", "tianluo/issues")
    _git(root, "commit", "-m", "feature adds colliding 005s and a dependent")

    _git(root, "checkout", main_branch)
    pre_merge_sha = _head(root)
    _git(root, "merge", "feature", "--no-ff", "--no-edit", "-m", "Merge feature")
    merge_sha = _head(root)

    orch = MergeOrchestrator(
        project_root=root, delete_merged=False, acquire_lock=False,
    )
    report = MergeReport()
    orch._reconcile_committed_issue_ids("feature", pre_merge_sha, report)

    # Both colliding issues renumbered past the store max (006): a -> 007,
    # b -> 008 (deterministic sorted-path loser order); keeper intact.
    ids = _open_issue_ids(root)
    assert len(ids) == len(set(ids)), f"duplicate numeric IDs remain: {ids}"
    assert set(ids) == {1, 5, 6, 7, 8}

    # The dependent issue's #005 keeps its digits — NOT rewritten to either
    # candidate — and carries the durable ambiguity note.
    dep = _find_issue_by_slug(root, "open", "dep")
    data = yaml.safe_load(dep.read_text(encoding="utf-8"))
    assert "Blocked by #005 until it lands." in data["description"]
    assert "Blocked by #007" not in data["description"]
    assert "Blocked by #008" not in data["description"]
    assert "歧义引用 #005 → 候选 #007 / #008 (luo merge)" in data["description"]

    # ... and the report surfaces every ambiguous holder for operators — the
    # dependent issue AND the two colliding files themselves, whose own #005
    # tokens are equally undecidable (self or peer).
    assert sorted(
        (e["file"], e["old_id"], tuple(e["candidates"]))
        for e in report.ambiguous_issue_references
    ) == [
        ("tianluo/issues/open/006_dep.yaml", "005", ("007", "008")),
        ("tianluo/issues/open/007_feature-a.yaml", "005", ("007", "008")),
        ("tianluo/issues/open/008_feature-b.yaml", "005", ("007", "008")),
    ]

    # Renumbered files' own #005 tokens keep their digits (they may have
    # meant the colliding peer) and carry the same durable note.
    for slug, new_id in (("feature-a", "007"), ("feature-b", "008")):
        f = _find_issue_by_slug(root, "open", slug)
        assert f is not None and f.name.startswith(f"{new_id}_")
        body = yaml.safe_load(f.read_text(encoding="utf-8"))["description"]
        assert "See #005 for context." in body
        assert f"See #{new_id} for context." not in body
        assert "歧义引用 #005 → 候选 #007 / #008 (luo merge)" in body

    assert _read_next_id(root) == 9
    assert len(report.committed_issue_renumbers) == 2
    # The whole reconcile (renames + note) landed as ONE fix-up commit.
    assert _commits_since(root, merge_sha) == 1
    assert _working_tree_clean(root)


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
    _git(root, "add", "-A", "--", "tianluo/issues")
    _git(root, "commit", "-m", "seed issues")
    _git(root, "branch", "feature")

    identical = "Shared issue five\n\nExact same content on both branches."

    # main adds 005 with content X.
    _write_issue_file(root, "open", "005", "shared", identical)
    _git(root, "add", "-A", "--", "tianluo/issues")
    _git(root, "commit", "-m", "main adds shared 005")

    # feature adds the byte-identical 005 at the same path.
    _git(root, "checkout", "feature")
    _write_issue_file(root, "open", "005", "shared", identical)
    _git(root, "add", "-A", "--", "tianluo/issues")
    _git(root, "commit", "-m", "feature adds shared 005")

    _git(root, "checkout", main_branch)
    pre_merge_sha = _head(root)
    # Identical add/add resolves cleanly to a single file (no conflict).
    _git(root, "merge", "feature", "--no-ff", "--no-edit", "-m", "Merge feature")
    merge_sha = _head(root)

    # Only one 005 file exists — git already folded the two identical adds.
    assert len(list((root / "tianluo" / "issues" / "open").glob("005_*.yaml"))) == 1

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
# LLM-resolved conflict path: reconciliation must run there too
# --------------------------------------------------------------------------


def test_llm_resolved_conflict_merge_still_reconciles_issue_ids(
    tmp_path: Path,
) -> None:
    """A conflicted merge resolved by the LLM path still renumbers collisions.

    Two colliding issue files live at DIFFERENT paths, so they merge cleanly
    even when some other file conflicts. The success tail of
    ``_apply_resolution`` (the LLM-resolved commit path) must therefore run
    the same committed-issue reconciliation as the clean-merge path —
    otherwise a duplicate numeric ID survives whenever the branch happens to
    conflict elsewhere.
    """
    from tianluo.engine.merge.conflict_context import build as build_conflict_context
    from tianluo.engine.merge.conflict_resolver import (
        Confidence, FileResolution, LLMResolution,
    )

    root = tmp_path / "repo"
    _init_repo(root)
    main_branch = _default_branch(root)

    # Baseline: a base issue plus a tracked non-issue file both sides edit.
    _write_issue_file(root, "open", "001", "base", "Base issue one")
    _set_next_id(root, "2")
    (root / "notes.txt").write_text("baseline\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")
    _git(root, "branch", "feature")

    # main: its own issue 005 + a conflicting notes.txt edit.
    _write_issue_file(root, "open", "005", "main-issue", "Main issue five")
    (root / "notes.txt").write_text("main version\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "main adds 005 and edits notes")

    # feature: a *different* issue 005 + a conflicting notes.txt edit.
    _git(root, "checkout", "feature")
    _write_issue_file(
        root, "open", "005", "feature-issue",
        "Feature issue five\n\nSee #005 for context.",
    )
    (root / "notes.txt").write_text("feature version\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "feature adds 005 and edits notes")

    _git(root, "checkout", main_branch)
    pre_merge_sha = _head(root)
    merge_result = subprocess.run(
        ["git", "-C", str(root), "merge", "feature", "--no-ff", "--no-edit"],
        capture_output=True, text=True, check=False,
    )
    assert merge_result.returncode != 0, "expected a conflict on notes.txt"

    orch = MergeOrchestrator(
        project_root=root, delete_merged=False, acquire_lock=False,
    )
    # Drive the exact success tail _handle_conflict uses: a validated
    # resolution applied while mid-merge, committed by _apply_resolution.
    context = build_conflict_context(root, main_branch, "feature")
    resolution = LLMResolution(
        files=[
            FileResolution(
                path="notes.txt",
                resolved_content="merged version\n",
                overall_confidence=Confidence.HIGH,
            ),
        ],
        overall_confidence=Confidence.HIGH,
    )
    report = MergeReport()
    result = orch._apply_resolution(
        "feature", resolution, pre_merge_sha, context, report,
    )
    assert result == "merged"
    assert (root / "notes.txt").read_text(encoding="utf-8") == "merged version\n"

    # --- both issues preserved, no duplicate numeric ID ---
    ids = _open_issue_ids(root)
    assert len(ids) == len(set(ids)), f"duplicate numeric IDs remain: {ids}"
    kept = _find_issue_by_slug(root, "open", "main-issue")
    assert kept is not None and kept.name == "005_main-issue.yaml"
    renumbered = _find_issue_by_slug(root, "open", "feature-issue")
    assert renumbered is not None
    assert int(renumbered.name.split("_", 1)[0]) == 6

    # --- trace + self-reference rewrite + counter, same as the clean path ---
    data = yaml.safe_load(renumbered.read_text(encoding="utf-8"))
    assert data["id"] == "006"
    assert "旧号 #005 → 新号 #006" in data["description"]
    assert "See #006 for context." in data["description"]
    assert _read_next_id(root) == 7

    # --- the renumber landed as one fix-up on top of the resolved merge ---
    merge_sha = _parent(root, "HEAD")
    # The fix-up's parent is the merge commit, whose first parent is the
    # pre-merge HEAD — i.e. reconciliation did not disturb the merge itself.
    assert _git(root, "rev-parse", f"{merge_sha}^1").stdout.strip() == pre_merge_sha
    assert _working_tree_clean(root)
    assert len(report.committed_issue_renumbers) == 1
    assert report.committed_issue_renumbers[0].old_id == "005"
    assert report.committed_issue_renumbers[0].new_id == "006"


# --------------------------------------------------------------------------
# collision case: the merge-introduced loser's body is not an issue mapping
# --------------------------------------------------------------------------


def test_non_mapping_colliding_file_is_still_renumbered(
    tmp_path: Path, caplog,
) -> None:
    """A colliding file whose body is not a YAML mapping is renamed anyway.

    Detection grouped the file into the collision via its filename prefix, so
    skipping it would leave two files sharing one numeric ID after the merge
    with no diagnostic. The rename alone restores uniqueness; the body (which
    carries no ``id`` field to rewrite and no description for the trace) is
    left byte-identical, and the skipped in-file rewrite is surfaced as a
    WARNING plus a normal report record.
    """
    root = tmp_path / "repo"
    _init_repo(root)
    main_branch = _default_branch(root)

    _write_issue_file(root, "open", "001", "base", "Base issue one")
    _git(root, "add", "-A", "--", "tianluo/issues")
    _git(root, "commit", "-m", "seed issues")
    _git(root, "branch", "feature")

    # main creates a valid issue 005 (the keeper).
    _write_issue_file(root, "open", "005", "main-issue", "Main issue five")
    _git(root, "add", "-A", "--", "tianluo/issues")
    _git(root, "commit", "-m", "main adds 005")

    # feature commits a corrupted 005 whose body parses to a YAML *list*.
    _git(root, "checkout", "feature")
    corrupt_body = "- not\n- an\n- issue mapping\n"
    corrupt = root / "tianluo" / "issues" / "open" / "005_corrupt.yaml"
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_text(corrupt_body, encoding="utf-8")
    _git(root, "add", "-A", "--", "tianluo/issues")
    _git(root, "commit", "-m", "feature adds corrupt 005")

    _git(root, "checkout", main_branch)
    pre_merge_sha = _head(root)
    _git(root, "merge", "feature", "--no-ff", "--no-edit", "-m", "Merge feature")
    merge_sha = _head(root)

    orch = MergeOrchestrator(
        project_root=root, delete_merged=False, acquire_lock=False,
    )
    report = MergeReport()
    with caplog.at_level(logging.WARNING):
        orch._reconcile_committed_issue_ids("feature", pre_merge_sha, report)

    # The keeper is untouched; the corrupt file was renamed to max+1 with its
    # body byte-identical — uniqueness restored without guessing at content.
    kept = _find_issue_by_slug(root, "open", "main-issue")
    assert kept is not None and kept.name == "005_main-issue.yaml"
    assert not corrupt.exists()
    renamed = root / "tianluo" / "issues" / "open" / "006_corrupt.yaml"
    assert renamed.exists()
    assert renamed.read_text(encoding="utf-8") == corrupt_body

    ids = _open_issue_ids(root)
    assert len(ids) == len(set(ids)), f"duplicate numeric IDs remain: {ids}"

    # The repair is diagnosed, recorded, and committed like any renumber.
    assert any(
        "does not parse to an issue mapping" in rec.message
        for rec in caplog.records
    )
    assert len(report.committed_issue_renumbers) == 1
    assert report.committed_issue_renumbers[0].old_id == "005"
    assert report.committed_issue_renumbers[0].new_id == "006"
    assert _read_next_id(root) == 7
    assert _commits_since(root, merge_sha) == 1
    assert _working_tree_clean(root)


# --------------------------------------------------------------------------
# collision case: main already held a pre-existing duplicate — the keeper must
# still come from the pre-merge side, never from the merged-in branch
# --------------------------------------------------------------------------


def test_keeper_chosen_among_preexisting_survivors(tmp_path: Path) -> None:
    """With several pre-merge survivors, the incoming file never keeps the ID.

    Main's store already held two files under #010 before the merge; the
    branch brings a third with a lexicographically smaller path. The keeper
    must be picked among the pre-merge survivors (adopt_issue's "keep the
    current branch's copy" direction), not fall back to min() over ALL paths
    — which would hand the number to the merge-introduced file and renumber
    both of main's copies instead.
    """
    root = tmp_path / "repo"
    _init_repo(root)
    main_branch = _default_branch(root)

    _write_issue_file(root, "open", "001", "base", "Base issue one")
    _git(root, "add", "-A", "--", "tianluo/issues")
    _git(root, "commit", "-m", "seed issues")
    _git(root, "branch", "feature")

    # main accumulates a pre-existing duplicate: two files parsing to #010.
    _write_issue_file(root, "open", "010", "b", "Main issue ten (b)")
    _write_issue_file(root, "open", "010", "z", "Main issue ten (z)")
    _set_next_id(root, "11")
    _git(root, "add", "-A", "--", "tianluo/issues")
    _git(root, "commit", "-m", "main adds duplicate 010s")

    # feature adds a third #010 whose path sorts BEFORE both of main's.
    _git(root, "checkout", "feature")
    _write_issue_file(root, "open", "010", "a", "Feature issue ten (a)")
    _git(root, "add", "-A", "--", "tianluo/issues")
    _git(root, "commit", "-m", "feature adds 010")

    _git(root, "checkout", main_branch)
    pre_merge_sha = _head(root)
    _git(root, "merge", "feature", "--no-ff", "--no-edit", "-m", "Merge feature")

    orch = MergeOrchestrator(
        project_root=root, delete_merged=False, acquire_lock=False,
    )
    report = MergeReport()
    orch._reconcile_committed_issue_ids("feature", pre_merge_sha, report)

    # The keeper is main's lexicographically-smallest survivor, NOT the
    # incoming 010_a.yaml; the incoming file and main's extra duplicate are
    # the ones renumbered (in path order: a -> 011, z -> 012).
    kept = _find_issue_by_slug(root, "open", "b")
    assert kept is not None and kept.name == "010_b.yaml"
    incoming = _find_issue_by_slug(root, "open", "a")
    assert incoming is not None
    assert int(incoming.name.split("_", 1)[0]) == 11
    extra = _find_issue_by_slug(root, "open", "z")
    assert extra is not None
    assert int(extra.name.split("_", 1)[0]) == 12

    ids = _open_issue_ids(root)
    assert len(ids) == len(set(ids)), f"duplicate numeric IDs remain: {ids}"
    assert len(report.committed_issue_renumbers) == 2
    assert _read_next_id(root) == 13
    assert _working_tree_clean(root)


# --------------------------------------------------------------------------
# rollback safety: a failed fix-up commit must not destroy unrelated issues
# --------------------------------------------------------------------------


def test_reconcile_rollback_preserves_unrelated_uncommitted_issues(
    tmp_path: Path, monkeypatch,
) -> None:
    """When the renumber fix-up commit fails, the rollback is surgical.

    The reconcile runs on top of the already-committed merge, so a rollback
    must undo ONLY what this run wrote. The former blanket ``git clean -fdq``
    over ``tianluo/issues`` would delete EVERY untracked file under the issue
    store — including pre-existing uncommitted issue YAMLs the user is still
    drafting (the repo routinely holds such files) that the reconciliation
    never created or touched. That is data loss by the very machinery whose
    hard guarantee is "never lose an issue". This pins the surgical behaviour:
    an unrelated untracked draft survives, while the renumber itself is fully
    undone.
    """
    root = tmp_path / "repo"
    _init_repo(root)
    main_branch = _default_branch(root)

    _write_issue_file(root, "open", "001", "base", "Base issue one")
    _git(root, "add", "-A", "--", "tianluo/issues")
    _git(root, "commit", "-m", "seed issues")
    _git(root, "branch", "feature")

    _write_issue_file(root, "open", "005", "main-issue", "Main issue five")
    _git(root, "add", "-A", "--", "tianluo/issues")
    _git(root, "commit", "-m", "main adds 005")

    _git(root, "checkout", "feature")
    _write_issue_file(root, "open", "005", "feature-issue", "Feature issue five")
    _git(root, "add", "-A", "--", "tianluo/issues")
    _git(root, "commit", "-m", "feature adds 005")

    _git(root, "checkout", main_branch)
    pre_merge_sha = _head(root)
    _git(root, "merge", "feature", "--no-ff", "--no-edit", "-m", "Merge feature")
    merge_sha = _head(root)

    # Pre-existing UNTRACKED issues the user is still drafting — never
    # committed, entirely unrelated to the collision the merge introduced (the
    # real repo carries several such files, e.g. 243..247).
    draft = _write_issue_file(
        root, "open", "243", "draft", "Draft issue two four three",
    )
    draft2 = _write_issue_file(
        root, "open", "244", "draft-two", "Draft issue two four four",
    )
    assert draft.exists() and draft2.exists()  # untracked, in the working tree

    orch = MergeOrchestrator(
        project_root=root, delete_merged=False, acquire_lock=False,
    )
    # Force the fix-up commit to fail so the rollback path fires.
    monkeypatch.setattr(
        orch, "_commit_issue_reconciliation", lambda *a, **k: False,
    )
    report = MergeReport()
    orch._reconcile_committed_issue_ids("feature", pre_merge_sha, report)

    # The user's untracked drafts survived — the whole point of the guarantee.
    assert draft.exists()
    assert "Draft issue two four three" in draft.read_text(encoding="utf-8")
    assert draft2.exists()
    assert "Draft issue two four four" in draft2.read_text(encoding="utf-8")

    # The renumber itself was fully rolled back: the loser is restored to its
    # merge-commit path (#005), and no merge-introduced #006 artifact lingers.
    loser = _find_issue_by_slug(root, "open", "feature-issue")
    assert loser is not None and loser.name.startswith("005_")
    assert _find_issue_by_slug(root, "open", "feature-issue").name == (
        "005_feature-issue.yaml"
    )
    # No renumber fix-up commit landed on top of the merge.
    assert _commits_since(root, merge_sha) == 0
    # The rollback left no renumbered-loser file behind anywhere.
    open_dir = root / "tianluo" / "issues" / "open"
    assert not list(open_dir.glob("006_feature-issue.yaml"))


def test_reconcile_rollback_never_pulls_next_id_backwards(
    tmp_path: Path, monkeypatch,
) -> None:
    """A failed fix-up commit must NOT roll ``.next_id`` back — the counter is
    strictly monotonic.

    ``.next_id`` is a TRACKED file in the real repo, so a naive
    ``git checkout HEAD -- tianluo/issues/.next_id`` on rollback would overwrite the
    live counter with the merge commit's committed value, pulling it BACKWARDS
    past any advance made since — including a reservation written by a
    concurrent ``luo issue create`` that does not hold the merge lock. The next
    allocation would then re-mint the reserved number and two distinct issues
    would share it, violating the never-duplicate hard guarantee. This pins
    that the counter stays at its advanced (live) value while the reconcile's
    actual file changes are still fully rolled back.
    """
    root = tmp_path / "repo"
    _init_repo(root)
    main_branch = _default_branch(root)

    _write_issue_file(root, "open", "001", "base", "Base issue one")
    _set_next_id(root, "6")
    _git(root, "add", "-A", "--", "tianluo/issues")
    _git(root, "commit", "-m", "seed issues + tracked .next_id")
    _git(root, "branch", "feature")

    _write_issue_file(root, "open", "005", "main-issue", "Main issue five")
    _git(root, "add", "-A", "--", "tianluo/issues")
    _git(root, "commit", "-m", "main adds 005")

    _git(root, "checkout", "feature")
    _write_issue_file(root, "open", "005", "feature-issue", "Feature issue five")
    _git(root, "add", "-A", "--", "tianluo/issues")
    _git(root, "commit", "-m", "feature adds 005")

    _git(root, "checkout", main_branch)
    pre_merge_sha = _head(root)
    _git(root, "merge", "feature", "--no-ff", "--no-edit", "-m", "Merge feature")
    merge_sha = _head(root)
    # The merge commit's committed ``.next_id`` is 6 (both sides). This is what
    # a rollback checkout would wrongly restore.
    assert int(
        _git(root, "show", "HEAD:tianluo/issues/.next_id").stdout.strip()
    ) == 6

    # A concurrent ``luo issue create`` (no merge lock) reserved #250 after the
    # merge: it wrote 251 to the working-tree ``.next_id`` but its 250_*.yaml is
    # not yet on disk. This live reservation must survive the rollback.
    _set_next_id(root, "251")

    orch = MergeOrchestrator(
        project_root=root, delete_merged=False, acquire_lock=False,
    )
    monkeypatch.setattr(
        orch, "_commit_issue_reconciliation", lambda *a, **k: False,
    )
    report = MergeReport()
    orch._reconcile_committed_issue_ids("feature", pre_merge_sha, report)

    # The counter kept (at least) its live reservation — it was NOT pulled back
    # to the merge commit's 6, which would let the next allocation re-mint #250.
    # (Monotonic-up: it may sit slightly higher if the renumber consumed the
    # reserved slot, but it can never drop below the reservation.)
    assert _read_next_id(root) >= 251

    # The reconcile's own file changes were still fully rolled back.
    loser = _find_issue_by_slug(root, "open", "feature-issue")
    assert loser is not None and loser.name == "005_feature-issue.yaml"
    assert _commits_since(root, merge_sha) == 0
    open_dir = root / "tianluo" / "issues" / "open"
    assert not list(open_dir.glob("006_feature-issue.yaml"))


def test_untracked_draft_reference_to_keeper_is_not_corrupted(
    tmp_path: Path,
) -> None:
    """A main-side UNTRACKED draft that references the KEEPER's #old is left
    alone — never rewritten, never swept into the fix-up commit.

    Authorship for the committed channel is read from the git trees (present at
    HEAD, absent at pre-merge), not the dirty working directory. A working-tree
    scan would classify the untracked draft as "merge-introduced" and, in the
    unambiguous single-loser case, repoint its ``#005`` (which names main's
    kept issue) to the loser's new number — silent kept-side corruption — and
    stage the user's private draft into the renumber commit.
    """
    root = tmp_path / "repo"
    _init_repo(root)
    main_branch = _default_branch(root)

    _write_issue_file(root, "open", "001", "base", "Base issue one")
    _set_next_id(root, "6")
    _git(root, "add", "-A", "--", "tianluo/issues")
    _git(root, "commit", "-m", "seed issues")
    _git(root, "branch", "feature")

    # main creates the keeper #005.
    _write_issue_file(root, "open", "005", "main-issue", "Main issue five")
    _git(root, "add", "-A", "--", "tianluo/issues")
    _git(root, "commit", "-m", "main adds 005")

    # feature creates a colliding, DIFFERENT #005 (single unambiguous loser).
    _git(root, "checkout", "feature")
    _write_issue_file(root, "open", "005", "feature-issue", "Feature issue five")
    _git(root, "add", "-A", "--", "tianluo/issues")
    _git(root, "commit", "-m", "feature adds 005")

    _git(root, "checkout", main_branch)
    pre_merge_sha = _head(root)
    _git(root, "merge", "feature", "--no-ff", "--no-edit", "-m", "Merge feature")
    merge_sha = _head(root)

    # A pre-existing UNTRACKED draft in the main worktree whose body references
    # #005 — meaning main's kept issue, which retains that number.
    draft = _write_issue_file(
        root, "open", "250", "draft",
        "Draft two five zero\n\nSee #005 for the main issue.",
    )
    assert draft.exists()

    orch = MergeOrchestrator(
        project_root=root, delete_merged=False, acquire_lock=False,
    )
    report = MergeReport()
    orch._reconcile_committed_issue_ids("feature", pre_merge_sha, report)

    # The collision was still repaired: feature's #005 was renumbered. The new
    # number clears the whole on-disk store — including the untracked draft's
    # id 250 — so the allocator hands out 251 (a harmless number-range hole,
    # never a reuse of a live number).
    renumbered = _find_issue_by_slug(root, "open", "feature-issue")
    assert renumbered is not None and renumbered.name == "251_feature-issue.yaml"
    new_num = int(renumbered.name.split("_", 1)[0])
    assert len(report.committed_issue_renumbers) == 1

    # The draft's #005 reference is UNTOUCHED (it names the keeper) ...
    draft_text = draft.read_text(encoding="utf-8")
    assert "See #005 for the main issue." in draft_text
    assert f"#{new_num:03d}" not in draft_text

    # ... and the draft was NOT committed into the renumber fix-up commit — it
    # is still an untracked working-tree file.
    tracked = _git(
        root, "ls-files", "--", "tianluo/issues/open/250_draft.yaml",
    ).stdout.strip()
    assert tracked == "", "the user's private draft must not be committed"
    # Exactly one fix-up commit (the renumber) landed; the draft is not in it.
    assert _commits_since(root, merge_sha) == 1


def test_untracked_draft_sharing_id_is_not_a_collision_loser(
    tmp_path: Path,
) -> None:
    """An untracked draft sharing a number with a merged committed issue is NOT
    renumbered — it belongs to the uncommitted domain and must survive intact.

    If the committed channel globbed the working tree, the draft would join the
    collision group; picked as the loser it would be unlinked and renumbered,
    and a fix-up-commit failure could then destroy it (its original path is not
    at HEAD, so a rollback ``git checkout`` cannot restore it). Restricting
    detection to files tracked at the merge commit keeps the draft out of the
    group entirely.
    """
    root = tmp_path / "repo"
    _init_repo(root)
    main_branch = _default_branch(root)

    _write_issue_file(root, "open", "001", "base", "Base issue one")
    _set_next_id(root, "2")
    _git(root, "add", "-A", "--", "tianluo/issues")
    _git(root, "commit", "-m", "seed issues")
    _git(root, "branch", "feature")

    # feature commits a real issue #250.
    _git(root, "checkout", "feature")
    _write_issue_file(root, "open", "250", "committed", "Committed two five zero")
    _git(root, "add", "-A", "--", "tianluo/issues")
    _git(root, "commit", "-m", "feature adds 250")

    _git(root, "checkout", main_branch)
    pre_merge_sha = _head(root)
    _git(root, "merge", "feature", "--no-ff", "--no-edit", "-m", "Merge feature")
    merge_sha = _head(root)

    # A pre-existing UNTRACKED draft that parses to the SAME id 250.
    draft = _write_issue_file(
        root, "open", "250", "draft", "Draft two five zero (private)",
    )
    committed = _find_issue_by_slug(root, "open", "committed")
    assert draft.exists() and committed is not None

    orch = MergeOrchestrator(
        project_root=root, delete_merged=False, acquire_lock=False,
    )
    report = MergeReport()
    orch._reconcile_committed_issue_ids("feature", pre_merge_sha, report)

    # No renumber happened — the untracked draft is not a committed collision.
    assert report.committed_issue_renumbers == []
    assert _commits_since(root, merge_sha) == 0

    # Both files survive with their original content; the draft is untouched.
    assert draft.exists()
    assert "Draft two five zero (private)" in draft.read_text(encoding="utf-8")
    committed = _find_issue_by_slug(root, "open", "committed")
    assert committed is not None and committed.name == "250_committed.yaml"


# --------------------------------------------------------------------------
# dirty-tracked-file case: an uncommitted local edit to an issue's id must
# NOT be able to manufacture a committed-channel collision
# --------------------------------------------------------------------------


def test_dirty_tracked_id_edit_does_not_fabricate_a_collision(
    tmp_path: Path,
) -> None:
    """A tracked issue file with an uncommitted ``id`` edit does not trigger a
    committed-channel renumber.

    Main has committed ``010_main.yaml`` (id 010). The user then edits its YAML
    ``id`` to 005 WITHOUT committing. A branch cleanly merges a committed
    ``005_feature.yaml`` (id 005). At the merge commit HEAD there is NO
    duplicate parsed ID — 010's committed blob still says 010. Detection must
    read the id from the committed merge tree, not the dirty working tree; if it
    parsed the working-tree ``id`` it would see two #005 files, renumber the
    branch's genuinely-unique committed #005, rewrite its references, and cut a
    fix-up commit that should not exist.
    """
    root = tmp_path / "repo"
    _init_repo(root)
    main_branch = _default_branch(root)

    _write_issue_file(root, "open", "001", "base", "Base issue one")
    _write_issue_file(root, "open", "010", "main", "Main issue ten")
    _set_next_id(root, "11")
    _git(root, "add", "-A", "--", "tianluo/issues")
    _git(root, "commit", "-m", "seed issues")
    _git(root, "branch", "feature")

    # feature commits a genuinely-unique issue #005 that self-references #005.
    _git(root, "checkout", "feature")
    _write_issue_file(
        root, "open", "005", "feature",
        "Feature issue five\n\nSee #005 for context.",
    )
    _git(root, "add", "-A", "--", "tianluo/issues")
    _git(root, "commit", "-m", "feature adds 005")

    _git(root, "checkout", main_branch)
    pre_merge_sha = _head(root)
    _git(root, "merge", "feature", "--no-ff", "--no-edit", "-m", "Merge feature")
    merge_sha = _head(root)

    # UNCOMMITTED working-tree edit: rewrite main's committed 010 file's YAML id
    # to 005. The blob at HEAD is unchanged (still id 010); only the disk copy
    # now parses to 005.
    main_file = _find_issue_by_slug(root, "open", "main")
    assert main_file is not None
    main_data = yaml.safe_load(main_file.read_text(encoding="utf-8"))
    main_data["id"] = "005"
    main_file.write_text(
        yaml.dump(main_data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    orch = MergeOrchestrator(
        project_root=root, delete_merged=False, acquire_lock=False,
    )
    report = MergeReport()
    orch._reconcile_committed_issue_ids("feature", pre_merge_sha, report)

    # No renumber: the committed tree held no duplicate ID.
    assert report.committed_issue_renumbers == []
    assert _commits_since(root, merge_sha) == 0

    # The branch's #005 issue is untouched — still 005, references intact.
    feature_file = _find_issue_by_slug(root, "open", "feature")
    assert feature_file is not None and feature_file.name == "005_feature.yaml"
    feature_text = feature_file.read_text(encoding="utf-8")
    assert "See #005 for context." in feature_text
    assert "旧号" not in feature_text
    # The user's uncommitted edit is left exactly as they made it.
    assert yaml.safe_load(main_file.read_text(encoding="utf-8"))["id"] == "005"


# --------------------------------------------------------------------------
# default-branch name helper (git init may produce master or main)
# --------------------------------------------------------------------------


def _default_branch(root: Path) -> str:
    return _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
