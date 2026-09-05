"""Flow-workspace capture and reset (decision 4's ``workspace: reset``).

``reset`` throws away everything the flow did to the working tree and puts it
back to the state it started from. Two facts make that safe rather than
destructive:

* **The pre-flow dirty snapshot.** ``baseline_commit`` alone is not the state
  the flow started from — a flow routinely starts on a tree that already
  carries uncommitted work, and resetting to the commit would delete work the
  flow never touched. So the *dirty* state is captured at flow start as a real
  git commit (tree + parent HEAD) held by
  ``refs/tianluo/baseline-dirty/<flow_id>``, and reset replays it.
* **The discard safe-ref.** Before anything is undone, the CURRENT tree (plus
  every commit the flow made since the baseline) is written to
  ``refs/tianluo/discarded/<flow_id>/<timestamp>/workspace``. Nothing is ever
  unrecoverable: the ref keeps the objects alive against gc and the caller is
  shown how to get back to them.

INVARIANT: every capture and every restore excludes the project's *volatile*
runtime sub-directories (``tianluo/state``, ``history``, ``calls``, …) — and
only those. The engine writes the running flow's own state and conversation log
there *while this code runs*, so folding them into a snapshot would make the
reset roll back the flow record that is describing the reset. The rest of the
runtime dir holds tracked project assets that the reset genuinely reverts, so
they stay inside the capture, the safety ref and the preview.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..runtime_paths import LEGACY_RUNTIME_DIR_NAME, RUNTIME_DIR_NAME

logger = logging.getLogger(__name__)

#: ``flow.state.context`` key holding the pre-flow dirty snapshot record.
DIRTY_SNAPSHOT_CONTEXT_KEY = "workspace_dirty_snapshot"

_REF_BASELINE_DIRTY = "refs/tianluo/baseline-dirty"
_REF_DISCARDED = "refs/tianluo/discarded"

# INVARIANT: every recovery ref is a LEAF under ``<flow_id>/<stamp>/``, never
# the ``<stamp>`` node itself. A restart of a parallel IMPLEMENT writes the
# main-tree discard and then, typically within the same one-second stamp, one
# ref per DAG group; git cannot create a ref underneath an existing ref, so
# parking the main-tree discard at ``<stamp>`` made every group's update-ref
# fail — aborting the rewind on a workspace that had already been reset.
_REF_WORKSPACE_LEAF = "workspace"
_REF_GROUPS_LEAF = "groups"

# Volatile sub-directories of the runtime dir: the engine writes the RUNNING
# flow's own state, conversation log and pending calls here while this code
# runs, so folding them into a snapshot would make the reset roll back the flow
# record describing the reset.
#
# INVARIANT: the exclusion is exactly these, never the whole runtime dir. The
# runtime dir also holds tracked PROJECT assets (charter.md, code-index.md,
# issues/**, prompts, version-intents) which ``git reset --hard`` *does* revert
# — anything the reset can revert has to be inside the discard safety ref, the
# pre-flow dirty snapshot and the pre-confirmation status preview, or the
# operator loses work that was never shown to them.
_RUNTIME_VOLATILE_SUBDIRS = (
    "state",
    "history",
    "calls",
    "logs",
    "cache",
    "tmp",
    "uploads",
    "worktrees",
    "collab",
)

_EXCLUDED_DIRS = tuple(
    [".git"]
    + [
        f"{base}/{sub}"
        for base in (RUNTIME_DIR_NAME, LEGACY_RUNTIME_DIR_NAME)
        if base
        for sub in _RUNTIME_VOLATILE_SUBDIRS
    ]
)


def _pathspec_excludes() -> List[str]:
    return [f":(exclude){name}" for name in _EXCLUDED_DIRS if name]


def _remove_untracked_files(root: Path) -> List[str]:
    """Delete the untracked files a reset must undo, one FILE at a time.

    Returns the paths that could NOT be removed. WHY they are reported rather
    than only logged: a survivor is flow output the reset promised to discard,
    and the rewound step would read it back as if it were its own fresh work.
    The caller turns a non-empty list into a failed reset — one bad path must
    not abort the remaining deletions, but it must not pass for success either.

    WHY not ``git clean -fd``: ``-d`` removes an untracked DIRECTORY as a
    single unit, which no pathspec exclusion can narrow — so keeping the live
    flow's state, history and calls out of its reach forced excluding the WHOLE
    runtime dir, and flow-created untracked project files under it
    (``tianluo/issues/**`` above all) then survived the reset and were read back
    by the rewound step as stale output from the very attempt just discarded.
    Deleting file-by-file lets the exclusion stay exactly the volatile
    sub-dirs, so the promise reset makes — the tree is the baseline plus the
    captured pre-flow snapshot, and nothing else — actually holds.

    Ignored files are left alone, matching ``git clean`` without ``-x``.
    """
    listing = _git(
        root, "ls-files", "--others", "--exclude-standard", "-z",
        "--", ".", *_pathspec_excludes(),
    )
    emptied: List[Path] = []
    failed: List[str] = []
    for rel in (listing.stdout or "").split("\0"):
        if not rel:
            continue
        path = root / rel
        try:
            path.unlink()
        except IsADirectoryError:
            # ``ls-files --others`` only ever names a DIRECTORY when it is an
            # embedded git repository, and ``git add -A`` folded that into the
            # safety ref as a bare gitlink — its contents are NOT in the ref.
            # So it can neither be deleted (unrecoverable loss) nor left in
            # place silently (the rewound step would read the discarded
            # attempt's output back as its own). It is reported as a survivor,
            # which fails the reset and tells the operator the one path they
            # have to resolve by hand.
            logger.warning(
                "Untracked directory entry %s cannot be removed by the reset "
                "(embedded repository?); reporting the reset as incomplete", rel,
            )
            failed.append(rel)
            continue
        except FileNotFoundError:
            pass
        except OSError as exc:  # noqa: PERF203 - one bad path must not abort
            logger.warning("Could not remove untracked file %s: %s", rel, exc)
            failed.append(rel)
            continue
        emptied.append(path.parent)
    _prune_empty_dirs(root, emptied)
    return failed


def _prune_empty_dirs(root: Path, candidates: List[Path]) -> None:
    """Remove directories left empty by :func:`_remove_untracked_files`.

    Git does not track directories, so a directory that only ever held
    flow-created files is itself flow debris — leaving the husk behind would
    make the reset tree differ from the baseline in a way ``git status`` cannot
    even show. Walks upward and stops at the first non-empty parent (and never
    at or above *root*).
    """
    for start in sorted(candidates, key=lambda p: len(p.parts), reverse=True):
        current = start
        while True:
            try:
                if current == root or root not in current.parents:
                    break
                current.rmdir()
            except OSError:
                # Not empty, gone already, or not ours — either way, stop.
                break
            current = current.parent


def _git(
    root: Path, *args: str, env: Optional[Dict[str, str]] = None, check: bool = True
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed ({result.returncode}): "
            f"{(result.stderr or result.stdout).strip()}"
        )
    return result


def _capture_tree(root: Path) -> str:
    """Write the working tree (minus volatile runtime state) as a git tree object.

    Uses a THROWAWAY index file so the user's real staging area is untouched —
    a capture must never disturb what the operator has staged.
    """
    with tempfile.TemporaryDirectory(prefix="tianluo-ws-index-") as tmp:
        index_path = os.path.join(tmp, "index")
        env = dict(os.environ)
        env["GIT_INDEX_FILE"] = index_path
        # Seed from HEAD so tracked-but-unmodified files are present, then fold
        # in every working-tree change (including untracked, excluding ignored).
        _git(root, "read-tree", "HEAD", env=env, check=False)
        _git(root, "add", "-A", "--", ".", *_pathspec_excludes(), env=env)
        return _git(root, "write-tree", env=env).stdout.strip()


def _commit_tree(root: Path, tree: str, message: str, parent: Optional[str]) -> str:
    args = ["commit-tree", tree, "-m", message]
    if parent:
        args += ["-p", parent]
    env = dict(os.environ)
    # A capture commit must never fail because the repo has no user.name set,
    # and it must not be attributed to the operator either — it is machinery.
    env.setdefault("GIT_AUTHOR_NAME", "tianluo")
    env.setdefault("GIT_AUTHOR_EMAIL", "tianluo@localhost")
    env.setdefault("GIT_COMMITTER_NAME", "tianluo")
    env.setdefault("GIT_COMMITTER_EMAIL", "tianluo@localhost")
    return _git(root, *args, env=env).stdout.strip()


def _ref_namespace_taken(root: Path, ref: str) -> bool:
    """True when *ref* cannot be created: it (or a descendant/ancestor) exists.

    Both directions matter. A descendant makes *ref* a directory git refuses to
    overwrite; an ancestor (a legacy discard ref parked at the ``<stamp>`` node)
    makes *ref* a path git refuses to create underneath it.
    """
    listing = _git(root, "for-each-ref", "--format=%(refname)", ref, check=False)
    if listing.returncode == 0 and (listing.stdout or "").strip():
        return True
    parts = ref.split("/")
    for depth in range(len(parts) - 1, 0, -1):
        ancestor = "/".join(parts[:depth])
        if not ancestor.startswith(f"{_REF_DISCARDED}/"):
            break
        probe = _git(root, "show-ref", "--verify", "--quiet", ancestor, check=False)
        if probe.returncode == 0:
            return True
    return False


def _reserve_ref(root: Path, prefix: str, leaf: str) -> str:
    """Return a free recovery ref ``<prefix>/<leaf>``, bumping *prefix* if taken.

    WHY reserve at all: the stamp has one-second resolution, so two discards of
    the same flow inside one second would otherwise silently clobber each other
    — and the ref is the ONLY thing keeping the discarded objects reachable.

    WHY the suffix lands on the stamped PREFIX and not on the leaf: a conflict
    can come from either direction — a ref already at the leaf, or a legacy ref
    parked at the prefix node itself, which no amount of leaf renaming can get
    out from under. Bumping the prefix is the one move that resolves both.
    """
    candidate = f"{prefix}/{leaf}"
    suffix = 1
    while _ref_namespace_taken(root, candidate):
        suffix += 1
        if suffix > 1000:
            raise RuntimeError(f"no free recovery ref under {prefix}")
        candidate = f"{prefix}-{suffix}/{leaf}"
    return candidate


def _head(root: Path) -> Optional[str]:
    result = _git(root, "rev-parse", "HEAD", check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def workspace_is_dirty(root: Path) -> bool:
    """True when the tree carries changes outside the volatile runtime dirs."""
    result = _git(
        root, "status", "--porcelain", "--", ".", *_pathspec_excludes(), check=False
    )
    return bool(result.stdout.strip())


def status_summary(root: Path, limit: int = 40) -> str:
    """Human-facing ``git status --porcelain`` summary, volatile state excluded.

    RAISES on a git failure rather than returning "". WHY: this text is the
    ONLY thing standing between the operator and an irreversible-looking
    discard, and empty output reads as "the tree is clean". A transient
    ``git status`` fault must therefore surface as "no preview could be taken"
    — which blocks the confirmation — never as "there is nothing to lose".
    """
    result = _git(root, "status", "--porcelain", "--", ".", *_pathspec_excludes())
    lines = [ln for ln in (result.stdout or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    if len(lines) > limit:
        extra = len(lines) - limit
        lines = lines[:limit] + [f"... (+{extra} more)"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Capture at flow start
# ---------------------------------------------------------------------------


def capture_baseline_dirty_state(
    flow: Any, project_root: Path, *, flow_started: bool = False
) -> Optional[Dict[str, Any]]:
    """Record the working tree's pre-flow dirty state, once per flow.

    Idempotent (a resumed flow keeps its original capture) and total — a repo
    without commits, or a git failure of any kind, degrades to "no snapshot"
    and is reported as such by :func:`reset_workspace_to_baseline` rather than
    aborting the flow.

    INVARIANT: the capture happens at flow START or not at all. *flow_started*
    says the flow has already executed steps (a resumed legacy flow, or one
    whose original capture failed); capturing then would photograph the tree
    the flow has ALREADY modified and label the flow's own edits "pre-flow
    state" — a later ``workspace: reset`` would then dutifully replay exactly
    the work it was asked to discard. Such a flow stays explicitly
    snapshot-less and takes the tracked-only reset fallback instead.
    """
    existing = flow.state.context.get(DIRTY_SNAPSHOT_CONTEXT_KEY)
    if isinstance(existing, dict) and existing.get("captured"):
        return existing
    if flow_started:
        record = {
            "captured": False,
            "reason": "flow had already started; pre-flow state is unknowable",
        }
        flow.state.context[DIRTY_SNAPSHOT_CONTEXT_KEY] = record
        return record
    record: Dict[str, Any] = {"captured": False}
    try:
        head = _head(project_root)
        if head is None:
            record["reason"] = "repository has no commits"
            flow.state.context[DIRTY_SNAPSHOT_CONTEXT_KEY] = record
            return record
        dirty = workspace_is_dirty(project_root)
        tree = _capture_tree(project_root)
        commit = _commit_tree(
            project_root,
            tree,
            f"tianluo: pre-flow workspace snapshot for {flow.flow_id}",
            head,
        )
        ref = f"{_REF_BASELINE_DIRTY}/{flow.flow_id}"
        _git(project_root, "update-ref", ref, commit)
        record = {
            "captured": True,
            "ref": ref,
            "commit": commit,
            "tree": tree,
            "head": head,
            "was_dirty": dirty,
            "captured_at": time.time(),
        }
        logger.info(
            "Captured pre-flow workspace snapshot %s (dirty=%s) at %s",
            commit[:8], dirty, ref,
        )
    except Exception as exc:  # noqa: BLE001 - never abort a flow over a snapshot
        record = {"captured": False, "reason": str(exc)}
        logger.warning("Failed to capture pre-flow workspace snapshot: %s", exc)
    flow.state.context[DIRTY_SNAPSHOT_CONTEXT_KEY] = record
    return record


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


@dataclass
class ResetPreview:
    """What a reset is about to discard, shown to the user before they confirm.

    ``ok`` is the gate, not a hint: a preview that could not be taken (git
    failure, no project root) leaves ``ok`` False, and the caller must refuse to
    let the reset be confirmed. A reset whose preview never rendered is a blind
    discard, which is exactly what this class exists to prevent.
    """

    baseline_commit: str = ""
    status_summary: str = ""
    flow_commits: List[str] = field(default_factory=list)
    has_dirty_snapshot: bool = False
    snapshot_reason: str = ""
    ok: bool = True
    error: str = ""

    @property
    def snapshot_warning(self) -> bool:
        """True when untracked / dirty pre-flow state cannot be restored."""
        return not self.has_dirty_snapshot


@dataclass
class ResetResult:
    """Outcome of an executed reset, including how to get the work back."""

    ok: bool = True
    error: str = ""
    safe_ref: str = ""
    safe_commit: str = ""
    baseline_commit: str = ""
    restored_snapshot: bool = False
    discarded_summary: str = ""
    flow_commits: List[str] = field(default_factory=list)
    #: Localised note about what the reset could NOT restore (no snapshot).
    warning: str = ""

    def recovery_hint(self) -> str:
        """The two commands that get the discarded work back, localised."""
        if not self.safe_ref:
            return ""
        from ..i18n import t

        return (
            f"git diff {self.baseline_commit} {self.safe_ref}"
            f"   # {t('engine.workspace.recover_inspect')}\n"
            f"git checkout {self.safe_ref} -- ."
            f"   # {t('engine.workspace.recover_restore_all')}"
        )


def _flow_commits_since_baseline(root: Path, baseline: str) -> List[str]:
    """Commits the flow made since *baseline*; raises if git cannot say.

    Same reason ``status_summary`` raises: "git failed" and "the flow made no
    commits" must not render as the same preview.
    """
    result = _git(root, "log", "--oneline", f"{baseline}..HEAD")
    return [ln for ln in (result.stdout or "").splitlines() if ln.strip()]


def preview_reset(flow: Any, project_root: Path) -> ResetPreview:
    """Describe what :func:`reset_workspace_to_baseline` would throw away.

    INVARIANT: a reset is never executed without this having been shown — the
    operation destroys the flow's whole visible output, and the operator's
    confirmation has to be informed by the actual tree, not by a label. A
    preview that cannot be taken comes back with ``ok=False`` so the caller
    withholds the confirmation instead of offering it over a blank panel.
    """
    snapshot = flow.state.context.get(DIRTY_SNAPSHOT_CONTEXT_KEY) or {}
    baseline = str(getattr(flow, "baseline_commit", "") or "")
    try:
        summary = status_summary(project_root)
        commits = (
            _flow_commits_since_baseline(project_root, baseline) if baseline else []
        )
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        logger.warning("Failed to build the workspace reset preview: %s", exc)
        return ResetPreview(
            baseline_commit=baseline,
            has_dirty_snapshot=bool(snapshot.get("captured")),
            snapshot_reason=str(snapshot.get("reason") or ""),
            ok=False,
            error=str(exc),
        )
    return ResetPreview(
        baseline_commit=baseline,
        status_summary=summary,
        flow_commits=commits,
        has_dirty_snapshot=bool(snapshot.get("captured")),
        snapshot_reason=str(snapshot.get("reason") or ""),
    )


def reset_workspace_to_baseline(flow: Any, project_root: Path) -> ResetResult:
    """Restore the flow's workspace to its pre-flow state.

    Order matters and is not negotiable: SAVE first (the safe ref), then undo.
    A failure after the save leaves an inconsistent tree but never lost work;
    a failure before it leaves the tree untouched.

    Without a pre-flow dirty snapshot (an older flow, or a capture that failed)
    the reset degrades to restoring TRACKED files to ``baseline_commit``,
    leaves untracked files untouched, and reports that the pre-flow untracked /
    dirty state could not be reconstructed — it never guesses, and never
    deletes what it could not put back.
    """
    from ..i18n import t

    baseline = str(getattr(flow, "baseline_commit", "") or "")
    if not baseline:
        return ResetResult(ok=False, error=t("engine.workspace.reset_no_baseline"))
    snapshot = flow.state.context.get(DIRTY_SNAPSHOT_CONTEXT_KEY) or {}
    result = ResetResult(baseline_commit=baseline)
    try:
        # --- 1. Save everything that is about to disappear -----------------
        result.discarded_summary = status_summary(project_root)
        result.flow_commits = _flow_commits_since_baseline(project_root, baseline)
        head = _head(project_root)
        tree = _capture_tree(project_root)
        safe_commit = _commit_tree(
            project_root,
            tree,
            f"tianluo: discarded workspace for flow {flow.flow_id}",
            head,
        )
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe_ref = _reserve_ref(
            project_root,
            f"{_REF_DISCARDED}/{flow.flow_id}/{stamp}",
            _REF_WORKSPACE_LEAF,
        )
        _git(project_root, "update-ref", safe_ref, safe_commit)
        result.safe_ref = safe_ref
        result.safe_commit = safe_commit

        # --- 2. Undo -------------------------------------------------------
        # Tracked files go back to the baseline either way. Removing untracked
        # files is conditional on holding a pre-flow snapshot: ``git clean``
        # cannot tell a file the flow created from one that was already there,
        # and only the snapshot replay in step 3 can put the latter back. With
        # no snapshot the fallback is deliberately tracked-only — deleting
        # untracked files we could never restore would be an unrecoverable
        # loss, which is exactly what this whole module exists to prevent.
        _git(project_root, "reset", "--hard", baseline)

        # --- 3. Replay the pre-flow dirty state ----------------------------
        if snapshot.get("captured") and snapshot.get("commit"):
            # Scoped by the SAME volatile-only exclusion as every other
            # operation here (see _remove_untracked_files for why it is not
            # ``git clean -fd``): everything the flow created untracked goes,
            # the live flow's own state stays.
            undeleted = _remove_untracked_files(project_root)
            undeleted += _restore_snapshot(
                project_root, baseline, str(snapshot["commit"])
            )
            if undeleted:
                # The tree is NOT "baseline plus the pre-flow snapshot" — some
                # of the flow's own output is still sitting there. Reporting
                # success would let the rewind run and hand the rebuilt step
                # the very artifacts the operator asked to throw away, so this
                # is a failed reset even though the safety ref was written and
                # everything else was undone.
                result.ok = False
                result.error = t(
                    "engine.workspace.reset_incomplete",
                    count=len(undeleted),
                    paths=", ".join(sorted(undeleted)[:10]),
                )
                logger.error(
                    "Workspace reset for flow %s left %d path(s) behind: %s",
                    flow.flow_id, len(undeleted), sorted(undeleted)[:10],
                )
            else:
                result.restored_snapshot = True
        else:
            result.warning = t("engine.workspace.reset_no_snapshot")
            logger.warning(
                "Flow %s has no pre-flow workspace snapshot (%s); reset restored "
                "tracked files to %s only and left untracked files in place",
                flow.flow_id, snapshot.get("reason") or "not captured", baseline[:8],
            )
    except Exception as exc:  # noqa: BLE001 - report, never raise into the flow
        result.ok = False
        result.error = str(exc)
        logger.exception("Workspace reset failed for flow %s", flow.flow_id)
    return result


def _restore_snapshot(root: Path, baseline: str, snapshot_commit: str) -> List[str]:
    """Re-apply the captured pre-flow dirty state on top of *baseline*.

    Returns the paths whose deletion failed — the snapshot recorded them as
    absent before the flow started, so a survivor leaves the tree different
    from what the reset claims to have restored.

    Driven by the baseline→snapshot name-status diff rather than a patch apply:
    the snapshot is a real tree, so a path can simply be checked out of it, and
    a path the snapshot deleted is deleted again. A patch apply would fail on
    binary files and on any path whose baseline content the reset just changed.
    """
    # WHY this raises rather than returning quietly: by the time it runs, the
    # reset --hard and the git clean have already happened. Swallowing a failed
    # read here would leave the tree at the bare baseline while the caller
    # reported ``restored_snapshot=True`` — telling the user their pre-flow
    # untracked files are back when they are only in the safety ref. A failure
    # must reach the caller so it can surface that ref and the recovery
    # commands instead.
    diff = _git(
        root, "diff", "--name-status", "-z", baseline, snapshot_commit, check=False
    )
    if diff.returncode != 0:
        raise RuntimeError(
            f"could not read the pre-flow snapshot {snapshot_commit[:12]}: "
            f"{(diff.stderr or diff.stdout).strip()}"
        )
    if not diff.stdout:
        # An identical tree is a legitimate answer: the flow started clean.
        return []
    fields = [f for f in diff.stdout.split("\0") if f != ""]
    to_checkout: List[str] = []
    to_delete: List[str] = []

    def _excluded(path: str) -> bool:
        return any(path == d or path.startswith(f"{d}/") for d in _EXCLUDED_DIRS)

    i = 0
    while i < len(fields):
        status = fields[i]
        i += 1
        if status.startswith("R") or status.startswith("C"):
            # Rename and copy both carry <source> <destination>, but they say
            # OPPOSITE things about the source. A rename means the snapshot no
            # longer holds it, so restoring the snapshot deletes it. A COPY
            # means the snapshot holds BOTH paths — treating it like a rename
            # deleted a file that was there before the flow started and then
            # still reported the snapshot as fully restored.
            if i + 1 >= len(fields):
                break
            source, destination = fields[i], fields[i + 1]
            i += 2
            if not _excluded(destination):
                to_checkout.append(destination)
            if _excluded(source):
                continue
            if status.startswith("R"):
                to_delete.append(source)
            else:
                to_checkout.append(source)
            continue
        if i >= len(fields):
            break
        path = fields[i]
        i += 1
        if _excluded(path):
            continue
        if status.startswith("D"):
            to_delete.append(path)
        else:
            to_checkout.append(path)
    if to_checkout:
        _git(root, "checkout", snapshot_commit, "--", *to_checkout)
        # ``git checkout <commit> -- <paths>`` also STAGES those paths. The
        # pre-flow state was (mostly) unstaged working-tree content, so unstage
        # them to reproduce it rather than handing the user a staged tree they
        # never staged.
        _git(root, "reset", "-q", baseline, "--", *to_checkout, check=False)
    failed: List[str] = []
    for path in to_delete:
        target = root / path
        try:
            if target.is_file() or target.is_symlink():
                target.unlink()
        except OSError as exc:
            logger.warning(
                "Failed to remove %s during snapshot replay: %s", path, exc
            )
            failed.append(path)
    return failed


def describe_reset(result: ResetResult) -> Tuple[str, str]:
    """Return ``(summary, recovery)`` strings for user display."""
    from ..i18n import t

    parts: List[str] = []
    if result.discarded_summary:
        parts.append(result.discarded_summary)
    if result.flow_commits:
        parts.append(
            t("engine.workspace.discarded_commits")
            + "\n"
            + "\n".join(result.flow_commits)
        )
    return "\n\n".join(parts), result.recovery_hint()


# ---------------------------------------------------------------------------
# DAG group worktrees / leaf branches
# ---------------------------------------------------------------------------
#
# A parallel IMPLEMENT step does not put its work in the flow's own tree: each
# group commits to its own leaf branch inside its own worktree, and the merge
# back happens only after every group finishes. So the two facilities above —
# whose whole world is the main tree and ``baseline..HEAD`` — are blind to it.
# A restart deletes those worktrees and branches, and without what follows the
# operator confirmed a discard they were never shown and whose objects nothing
# kept reachable.


@dataclass
class GroupWorkPreview:
    """The work one DAG group holds that a restart is about to delete."""

    branch: str = ""
    worktree_path: str = ""
    commits: List[str] = field(default_factory=list)
    status_summary: str = ""

    @property
    def has_work(self) -> bool:
        return bool(self.commits or self.status_summary)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "branch": self.branch,
            "worktree_path": self.worktree_path,
            "commits": list(self.commits),
            "status_summary": self.status_summary,
        }


def _branch_exists(root: Path, branch: str) -> bool:
    """Whether ``refs/heads/<branch>`` resolves.

    RAISES when the probe itself fails, and only git's own verdict of *absent*
    returns ``False``. WHY: ``False`` is read by :func:`preserve_group_work` as
    "no branch exists under this name", which — with no worktree either — hands
    the group straight to a cleanup that deletes the branch. A transient git
    fault answered as "absent" therefore lets the later deletion succeed once
    the fault clears, leaving the group's commits unreachable with no recovery
    ref. ``rev-parse --verify --quiet`` answers the genuinely missing ref by
    exiting 1 in complete silence, so that exact signature — exit 1, nothing on
    stderr, nothing on stdout — is the only "not there". Anything else is
    "cannot tell": another exit code, a missing git binary, a timeout, but also
    exit 1 accompanied by any diagnostic at all. Classifying that diagnostic is
    not safe here — a broken or unreadable ref store reports itself as a
    ``warning:``, git's messages are localisable, and a rule keyed on
    ``fatal:``/``error:`` prefixes would read exactly that case as proof of
    absence. Refusing to answer costs an aborted rewind; guessing costs the
    group's commits.
    """
    try:
        result = _git(
            root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}",
            check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise RuntimeError(f"could not probe branch {branch}: {exc}") from exc
    if result.returncode == 0:
        return True
    stderr = (result.stderr or "").strip()
    stdout = (result.stdout or "").strip()
    if result.returncode == 1 and not stderr and not stdout:
        return False
    raise RuntimeError(
        f"could not probe branch {branch} (git rev-parse exited "
        f"{result.returncode}): {stderr or stdout or 'no diagnostic'}"
    )


def _worktree_path_for_branch(root: Path, branch: str) -> str:
    """The checkout directory ``branch`` is currently checked out in, if any.

    RAISES when the probe itself fails. WHY: "" is read by
    :func:`preserve_group_work` as "this group has no worktree, so its branch
    tip is all there is to save" — and the cleanup that follows deletes the
    worktree. A transient ``git worktree list`` fault must therefore never be
    downgraded into that verdict, or an interrupted group's uncommitted edits
    are destroyed with only its committed work under the recovery ref.
    """
    listing = _git(root, "worktree", "list", "--porcelain")
    path = ""
    for line in (listing.stdout or "").splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):].strip()
        elif line.startswith("branch "):
            ref = line[len("branch "):].strip()
            if ref in (f"refs/heads/{branch}", branch):
                return path
    return ""


def materialised_group_branches(root: Path, branches: List[str]) -> List[str]:
    """Which of *branches* really exist, as a ref or as a checked-out worktree.

    Asked BEFORE a rewind's cleanup deletes them, and only to decide whether
    deleting them made a step's recorded group results dangle. WHY the
    distinction matters: the branch names are DERIVED from planned group ids,
    so a sequential (non-DAG) implement run — whose groups execute in the
    flow's own tree and never materialise a branch at all — yields a list of
    names that name nothing. Treating those as discarded work would drop that
    step's ``implemented_groups`` and re-run groups whose output is sitting
    safely in the working tree.

    A probe that cannot answer counts the branch as materialised: erring that
    way costs a group re-run, while the other way silently skips a group whose
    only copy is gone.
    """
    live: List[str] = []
    for branch in branches:
        try:
            exists = _branch_exists(root, branch)
            worktree = _worktree_path_for_branch(root, branch)
            has_worktree = bool(worktree) and Path(worktree).is_dir()
        except Exception as exc:  # noqa: BLE001 - unanswerable == materialised
            logger.warning(
                "Could not tell whether group branch %s was materialised (%s); "
                "assuming it was", branch, exc,
            )
            live.append(branch)
            continue
        if exists or has_worktree:
            live.append(branch)
    return live


def group_cleanup_residue(root: Path, branch: str) -> List[str]:
    """What git still holds for *branch* after a rewind's cleanup ran.

    Empty means the leaf branch and its worktree registration are really gone.

    RAISES when git cannot answer, and the caller treats that exactly like a
    leftover. WHY: the rewind's cleanup helpers log-and-return on a failed
    removal, so "the calls came back" is not evidence of anything. A branch or
    a registered worktree that survives makes the rebuilt IMPLEMENT step
    collide with the discarded attempt's leftovers instead of starting fresh —
    and an unanswerable probe is not proof that it did not survive.
    """
    # Localized: these fragments are interpolated into the user-facing
    # ``engine.rewind.group_cleanup_failed`` refusal, so an English literal
    # here would surface untranslated inside an otherwise translated message.
    from ..i18n import t

    residue: List[str] = []
    listing = _git(root, "for-each-ref", "--format=%(refname)", f"refs/heads/{branch}")
    if (listing.stdout or "").strip():
        residue.append(t("engine.rewind.residue_branch", branch=branch))
    worktree = _worktree_path_for_branch(root, branch)
    if worktree:
        residue.append(t("engine.rewind.residue_worktree", path=worktree))
    return residue


def preview_group_work(
    project_root: Path, branches: List[str], baseline: str = ""
) -> List[GroupWorkPreview]:
    """Describe the commits and uncommitted edits held by each group *branch*.

    Best-effort per branch: a group whose branch or worktree cannot be read is
    reported as carrying no visible work rather than aborting the whole
    preview — a partial list still beats confirming a discard over a blank
    panel, and :func:`preserve_group_work` saves what it finds regardless.
    """
    previews: List[GroupWorkPreview] = []
    for branch in branches:
        preview = GroupWorkPreview(branch=branch)
        try:
            if _branch_exists(project_root, branch):
                base = baseline or _head(project_root) or ""
                if base:
                    log = _git(
                        project_root, "log", "--oneline", f"{base}..{branch}",
                        check=False,
                    )
                    if log.returncode == 0:
                        preview.commits = [
                            ln for ln in (log.stdout or "").splitlines() if ln.strip()
                        ]
            worktree = _worktree_path_for_branch(project_root, branch)
            if worktree and Path(worktree).is_dir():
                preview.worktree_path = worktree
                preview.status_summary = status_summary(Path(worktree))
        except Exception as exc:  # noqa: BLE001 - a blind spot, never a crash
            logger.warning(
                "Failed to preview group work for branch %s: %s", branch, exc
            )
        if preview.has_work or preview.worktree_path:
            previews.append(preview)
    return previews


class GroupPreservationError(RuntimeError):
    """A DAG group's recoverable work could not be written to a safety ref.

    Carries the branch that failed and the refs already written, so the caller
    can tell the operator exactly which group blocked the restart and which
    ones are already safe.
    """

    def __init__(self, branch: str, reason: str, preserved: Optional[List[str]] = None):
        super().__init__(
            f"could not preserve group branch {branch}: {reason}"
        )
        self.branch = branch
        self.reason = reason
        self.preserved = list(preserved or [])


def preserve_group_work(
    project_root: Path, flow_id: str, branches: List[str]
) -> List[str]:
    """Save each group's commits and uncommitted edits under a safe ref.

    INVARIANT: this runs BEFORE the worktrees are removed and the branches
    deleted, and it FAILS CLOSED. Deleting a leaf branch makes its commits
    unreachable and a ``rmtree`` of the worktree destroys the uncommitted edits
    outright — both are the flow's produced work, and decision 4's promise
    ("nothing the restart discards is unrecoverable") has to cover them just as
    it covers the main tree. A group may therefore be handed to the cleanup
    only once it is either captured under a ref or verified to hold nothing
    recoverable (no branch and no worktree under that name); anything else —
    an unreadable worktree, a ``commit-tree`` that fails, an ``update-ref``
    that fails — raises :class:`GroupPreservationError` so the whole rewind
    aborts with every worktree and branch still on disk. Proceeding on a logged
    warning would delete work no ref points at, which is the one outcome this
    module exists to prevent; a restart the operator has to retry is strictly
    better than one that silently eats a group's edits.

    Returns the refs written, newest-work-first order preserved.
    """
    saved: List[str] = []
    if not branches:
        return saved
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for branch in branches:
        try:
            exists = _branch_exists(project_root, branch)
            tip = ""
            if exists:
                tip = _git(
                    project_root, "rev-parse", f"refs/heads/{branch}", check=False
                ).stdout.strip()
                if not tip:
                    raise RuntimeError(
                        "branch exists but its tip could not be resolved"
                    )
            worktree = _worktree_path_for_branch(project_root, branch)
            has_worktree = bool(worktree) and Path(worktree).is_dir()
            if not exists and not has_worktree:
                # Nothing was ever materialised under this name — a planned
                # group whose worktree the interrupted run never created. There
                # is no work to lose, so the cleanup may proceed on it.
                continue
            commit = tip
            if has_worktree:
                root = Path(worktree)
                # The worktree is about to be deleted, so its uncommitted edits
                # only survive as a commit. Parented on the leaf tip so the
                # group's committed history stays reachable through the same
                # ref.
                tree = _capture_tree(root)
                commit = _commit_tree(
                    root,
                    tree,
                    f"tianluo: discarded group worktree {branch} for flow {flow_id}",
                    tip or _head(root),
                )
            if not commit:
                raise RuntimeError("no commit object could be captured")
            leaf = branch.replace("refs/heads/", "").strip("/").replace("/", "_")
            ref = _reserve_ref(
                project_root,
                f"{_REF_DISCARDED}/{flow_id}/{stamp}",
                f"{_REF_GROUPS_LEAF}/{leaf}",
            )
            _git(project_root, "update-ref", ref, commit)
            saved.append(ref)
            logger.info(
                "Preserved group branch %s at %s before rewind cleanup",
                branch, ref,
            )
        except GroupPreservationError:
            raise
        except Exception as exc:  # noqa: BLE001 - re-raised as a typed failure
            logger.warning(
                "Failed to preserve group work for branch %s: %s", branch, exc
            )
            raise GroupPreservationError(branch, str(exc), saved) from exc
    return saved
