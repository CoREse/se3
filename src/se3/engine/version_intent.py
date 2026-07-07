"""Version-reconcile intent metadata (shared geodesic for de-versioning + reconcile).

Background: two concurrent worktree sessions that diverge from the same
baseline used to each write a final version number into their own commit.
When both landed on master the second write was a verbatim no-op and its
changelog entry was silently deduped away, so two features shared one
version. The fix moves the version *decision* to the merge side: a worktree
session's commit step no longer writes a version — it emits an **intent**
(a change summary + changelog bullets + an auxiliary bump hint), committed on
the flow branch. The merge-side ``version_reconcile`` step later reads every
merged-in branch's intent and derives the final version once, against
master's current version.

This module is the shared foundation for that flow:

  * :class:`VersionIntent` — the structured, branch-committed metadata.
  * read/write helpers that persist an intent to a path tracked by git (so it
    survives the merge into master and is readable from the merged tree).
  * :func:`collect_intents` — gather every merged-in branch's intent from the
    merged master checkout.
  * :func:`mark_consumed` / :func:`is_consumed` / :func:`reconcile_commit_exists`
    — idempotency markers so a resumed / re-entered reconcile never double-bumps.

Design constraint honoured here: ``bump_type`` is auxiliary and MAY be absent
or lossy (date versions, build numbers, other non-SemVer custom rules). The
intent's *substance* is ``change_summary`` + ``versions_changes``; those are
what the LLM/custom-rules reconcile channel consumes, so an intent with no
usable ``bump_type`` is still a complete intent.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import stat
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from .worktree import _run_git

logger = logging.getLogger(__name__)


# Directory (relative to project root) holding one JSON intent file per flow.
# Whitelisted in .gitignore so it is committed with the flow branch — the
# whole point is that the merge side reads it from master after the merge.
VERSION_INTENT_DIR_RELPATH = "se3/version-intents"

# Commit-message trailer the reconcile step stamps onto its commit. Consulted
# by :func:`reconcile_commit_exists` as a git-durable idempotency signal that
# survives even when the on-disk intent file's ``consumed`` flag was lost
# (e.g. the marking write landed but was never committed before a crash).
RECONCILE_TRAILER = "Version-Reconcile-Session"


class IntentReadError(RuntimeError):
    """Raised when the intents present at a git ref cannot be determined.

    Distinguishes a genuine "no intents here" (an *empty* result) from an
    infrastructure failure (git timeout / bad invocation / nonzero exit). The
    two MUST NOT be conflated by the merge CLI: silently reading a git fault as
    "this branch carries no version intent" would let the merge publish with an
    empty reconcile scope and then let ``--delete-merged`` remove the source
    branch — stranding a merged feature on master with no version bump /
    changelog, the exact accident this redesign exists to prevent. The caller
    must surface this (fail before branch cleanup / force a whole-command rerun)
    rather than treat it as an empty contribution.
    """


class VersionIntentIgnoredError(OSError):
    """Raised when the intent path is gitignored so it can never be committed.

    Subclasses :class:`OSError` deliberately: the version_analyze step's
    intent-emit path already treats a failed persist (OSError) as a FAILED,
    resumable step, and an ignored path is the same class of "this intent will
    not travel with the branch" fault — surfaced HERE, at write time, with the
    actual ``.gitignore`` root cause, instead of much later at the merge-side
    ``version_reconcile`` step as a misleading "restore the intent file" error
    (the file is present locally; git just refuses to stage it).
    """


@dataclass
class VersionIntent:
    """Structured version-bump intent produced by a worktree session's commit.

    Travels with the flow branch (as a committed JSON file) so the merge-side
    reconcile step can read it from master and derive the final version.

    Attributes:
        flow_id: The owning flow's id — identity for collection, consumption
            marking, and the reconcile-commit trailer. Also the file stem.
        change_summary: Free-form prose summarising what this session changed
            (the inductive digest of changes_made / updated_specs /
            verification). This is the intent's substance for the custom-rules
            (LLM) reconcile channel, which cannot rely on ``bump_type``.
        versions_changes: Changelog-grade bullet strings (VERSIONS.md entries)
            WITHOUT a version-number header — the reconcile step files them
            under whatever final version it derives.
        bump_type: Auxiliary SemVer hint ("major"/"minor"/"patch"/"none") used
            by the default deterministic channel and for commit-message
            display only. MAY be ``None`` or lossy under custom version-rules;
            never the sole carrier of intent.
        pre_session_baseline: The master version the session diverged from,
            recorded for audit / drift diagnosis (NOT used to compute the
            final version — reconcile re-bases on master's *current* version).
        provisional_suggested_version: version_analyze's suggested_version,
            demoted to a non-authoritative reference. Never written to any
            version file; reconcile may cite it in the commit message.
        consumed: Set once the reconcile step has applied this intent, so a
            re-entry does not bump again. See :func:`mark_consumed`.
        consumed_by: The reconcile commit sha (or other marker) that consumed
            this intent, for traceability.
    """

    flow_id: str
    change_summary: str = ""
    versions_changes: list[str] = field(default_factory=list)
    bump_type: Optional[str] = None
    pre_session_baseline: Optional[str] = None
    provisional_suggested_version: Optional[str] = None
    consumed: bool = False
    consumed_by: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain JSON-safe dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VersionIntent":
        """Reconstruct from a dict, tolerating unknown/missing keys.

        Unknown keys (from a newer writer) are ignored rather than raising so
        an older reader on master can still consume a forward-written intent;
        missing keys fall back to the dataclass defaults so a minimal payload
        (just ``flow_id``) still deserializes.
        """
        if not isinstance(data, dict):
            raise TypeError(f"VersionIntent.from_dict expects a dict, got {type(data)!r}")

        flow_id = data.get("flow_id")
        if not isinstance(flow_id, str) or not flow_id.strip():
            raise ValueError("VersionIntent requires a non-empty 'flow_id'")

        raw_changes = data.get("versions_changes")
        versions_changes: list[str] = []
        if isinstance(raw_changes, list):
            versions_changes = [
                c.strip() for c in raw_changes if isinstance(c, str) and c.strip()
            ]

        return cls(
            flow_id=flow_id.strip(),
            change_summary=str(data.get("change_summary") or ""),
            versions_changes=versions_changes,
            bump_type=_normalize_optional_str(data.get("bump_type")),
            pre_session_baseline=_normalize_optional_str(data.get("pre_session_baseline")),
            provisional_suggested_version=_normalize_optional_str(
                data.get("provisional_suggested_version")
            ),
            consumed=bool(data.get("consumed", False)),
            consumed_by=_normalize_optional_str(data.get("consumed_by")),
        )


def _normalize_optional_str(value: Any) -> Optional[str]:
    """Coerce to a stripped string, mapping empty / None to ``None``.

    Keeps "" and None indistinguishable at the field level so a bump_type the
    LLM omitted and one it emitted as "" both read back as the same absent
    intent hint.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def intent_path(project_root: Path, flow_id: str) -> Path:
    """Return the committed path for *flow_id*'s intent file.

    The flow_id is the file stem so multiple merged-in branches (each a
    distinct flow) contribute distinct files that coexist in the merged tree.
    """
    return Path(project_root) / VERSION_INTENT_DIR_RELPATH / f"{flow_id}.json"


def write_intent(project_root: Path, intent: VersionIntent) -> Path:
    """Persist *intent* atomically to its committed path; return the path.

    Creates the intent directory when missing. Written atomically (temp file
    + ``os.replace``) so a crash mid-write never leaves a half-JSON file that
    the merge side would choke on.
    """
    path = intent_path(project_root, intent.flow_id)
    # Fail loudly at write time if the intent path is gitignored: the commit
    # step's ``git add -A`` would then silently skip it, and the flow would only
    # fail much later at the merge-side version_reconcile with a misleading
    # "restore the intent file" message. On an EXISTING project whose committed
    # .gitignore predates the ``!/se3/version-intents/`` whitelist (init/migrate
    # not re-run), every worktree flow would loop through that dead end. Surface
    # the real cause — the ignore rule — with the actionable fix instead.
    _assert_intent_path_not_ignored(project_root, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(intent.to_dict(), indent=2, ensure_ascii=False) + "\n"
    _atomic_write_text(path, payload)
    logger.debug("Wrote version intent for flow %s to %s", intent.flow_id, path)
    return path


def _assert_intent_path_not_ignored(project_root: Path, path: Path) -> None:
    """Raise :class:`VersionIntentIgnoredError` if *path* is gitignored.

    Best-effort: a missing git / non-repo / probe fault does NOT block the write
    (``git check-ignore`` exit 128 or a subprocess error) — only a definitive
    "this path is ignored" (exit 0) raises. The whole point is to catch the one
    concrete, actionable misconfiguration (an old committed .gitignore that
    ignores ``se3/`` without the ``!/se3/version-intents/`` whitelist) at its
    source rather than let it fail the flow's final step with a wrong diagnosis.
    """
    try:
        rel = os.path.relpath(path, project_root)
    except ValueError:
        return
    try:
        # -q: quiet, exit status only. 0 => ignored, 1 => not ignored, 128 =>
        # fatal (not a repo / no HEAD). check=False so only a real "ignored"
        # signal (rc 0) blocks; everything else falls through to the write.
        result = _run_git(
            project_root, "check-ignore", "-q", "--", rel, check=False, timeout=15
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.debug("check-ignore probe failed for %s: %s", rel, exc)
        return
    if result.returncode == 0:
        raise VersionIntentIgnoredError(
            f"the version-intent path {rel!r} is gitignored, so the commit step's "
            f"'git add -A' cannot stage it and this worktree session's version "
            f"bump/changelog would never reach the merge-side version_reconcile. "
            f"Add a whitelist line '!/{VERSION_INTENT_DIR_RELPATH}/' to .gitignore "
            f"(re-running 'se3 init' / 'se3 migrate' adds it), commit it, and "
            f"resume."
        )


def read_intent(project_root: Path, flow_id: str) -> Optional[VersionIntent]:
    """Read *flow_id*'s intent from the working tree, or ``None`` if absent."""
    return read_intent_file(intent_path(project_root, flow_id))


def read_intent_file(path: Path) -> Optional[VersionIntent]:
    """Parse a single intent file; return ``None`` on missing/corrupt file.

    A corrupt or unreadable intent file is logged and skipped rather than
    raising: one branch's damaged intent must not abort collection of the
    others (the reconcile step still needs to bump for the readable intents).
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("Could not read version intent file %s: %s", path, exc)
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("Version intent file %s is not valid JSON: %s", path, exc)
        return None

    try:
        return VersionIntent.from_dict(data)
    except (TypeError, ValueError) as exc:
        logger.warning("Version intent file %s has invalid content: %s", path, exc)
        return None


def collect_intents(
    project_root: Path, *, include_consumed: bool = False
) -> list[VersionIntent]:
    """Collect every merged-in branch's intent from the merged master tree.

    Scans ``se3/version-intents/*.json`` in *project_root*'s working tree.
    After the merge step has landed all branches into master, each merged-in
    session's intent file coexists here, so a single directory scan yields all
    of them. Results are sorted by ``flow_id`` for deterministic ordering.

    Args:
        project_root: The (main-checkout) project root.
        include_consumed: When ``False`` (default), already-consumed intents
            are filtered out — the reconcile step only wants the intents it has
            not yet applied, so a resume re-collects only the outstanding work.

    Returns:
        A list of :class:`VersionIntent`; empty when the directory is absent
        or holds no readable intents.
    """
    directory = Path(project_root) / VERSION_INTENT_DIR_RELPATH
    if not directory.is_dir():
        return []

    intents: list[VersionIntent] = []
    for entry in sorted(directory.glob("*.json")):
        intent = read_intent_file(entry)
        if intent is None:
            continue
        if intent.consumed and not include_consumed:
            continue
        intents.append(intent)

    intents.sort(key=lambda i: i.flow_id)
    return intents


def intent_flow_ids_at_ref(project_root: Path, ref: str) -> set[str]:
    """Return the set of intent flow_ids present in the intents dir at *ref*.

    Reads the committed tree at *ref* (not the working tree) so the caller can
    tell which intents already existed on master *before* a merge from those a
    merge *introduces*. Used by the merge orchestrator/CLI so a leftover
    unconsumed intent from an unrelated flow does not masquerade as an intent
    contributed by the branches being merged.

    A ref that simply holds no intents (``git ls-tree`` succeeds with empty
    output — a legacy branch, or the intents dir absent) yields an empty set:
    that is a real, trustworthy "no intents here".

    Raises:
        IntentReadError: on a git INFRASTRUCTURE failure (subprocess error,
            timeout, or a nonzero ``ls-tree`` exit such as a bad ref / not a
            repo). This is deliberately NOT degraded to an empty set: the CLI
            must be able to tell "branch carries no intent" apart from "could
            not read the branch's intents", because publishing a merge with a
            silently-empty scope and then deleting the source branch would strand
            a merged feature with no reconcile bump. Callers that genuinely want
            a best-effort probe (e.g. the orchestrator's legacy-aggregation
            suppression, where an empty set is the conservative default) catch
            this and fall back themselves.
    """
    try:
        result = _run_git(
            project_root,
            "ls-tree",
            "--name-only",
            ref,
            f"{VERSION_INTENT_DIR_RELPATH}/",
            check=False,
            timeout=15,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.debug(
            "intent_flow_ids_at_ref: git ls-tree failed for %s: %s", ref, exc
        )
        raise IntentReadError(
            f"could not read version intents at {ref!r}: {exc}"
        ) from exc
    if result.returncode != 0:
        # A nonzero ls-tree is an infrastructure fault (bad ref, not a repo),
        # NOT "no intents" — git reports a genuinely-absent path with exit 0 and
        # empty output. Surface it so the caller does not mistake it for an empty
        # scope.
        raise IntentReadError(
            f"git ls-tree failed for {ref!r} (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
    ids: set[str] = set()
    for line in result.stdout.splitlines():
        name = line.strip()
        if name.endswith(".json"):
            ids.add(Path(name).stem)
    return ids


def _merge_base(project_root: Path, ref_a: str, ref_b: str) -> Optional[str]:
    """Return the merge-base sha of two refs, or ``None`` when there is none.

    Raises:
        IntentReadError: on a git infrastructure fault (subprocess error /
            timeout). A ``git merge-base`` that simply finds no common ancestor
            (exit 1, empty output) is a legitimate ``None``, not a fault.
    """
    try:
        result = _run_git(
            project_root, "merge-base", ref_a, ref_b, check=False, timeout=15
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise IntentReadError(
            f"could not compute merge-base of {ref_a!r} and {ref_b!r}: {exc}"
        ) from exc
    base = result.stdout.strip()
    if result.returncode != 0 or not base:
        # No common ancestor (unrelated histories) — a real, non-fault outcome.
        return None
    return base


def _is_ancestor(project_root: Path, ref_a: str, ref_b: str) -> bool:
    """Return True iff *ref_a* is an ancestor of *ref_b*.

    Raises:
        IntentReadError: on a git infrastructure fault. ``merge-base
        --is-ancestor`` uses exit 0 (ancestor) / exit 1 (not) for the answer, so
        only OTHER exit codes / subprocess errors are treated as faults.
    """
    try:
        result = _run_git(
            project_root,
            "merge-base", "--is-ancestor", ref_a, ref_b,
            check=False, timeout=15,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise IntentReadError(
            f"could not test ancestry of {ref_a!r} in {ref_b!r}: {exc}"
        ) from exc
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise IntentReadError(
        f"git merge-base --is-ancestor failed for {ref_a!r}/{ref_b!r} "
        f"(exit {result.returncode}): {result.stderr.strip()}"
    )


def intent_flow_ids_introduced(
    project_root: Path, branch: str, target_ref: str = "HEAD"
) -> set[str]:
    """Return the intent flow_ids *branch* CONTRIBUTES to a merge into *target_ref*.

    An intent that *branch* merely INHERITED from the merge target — a concurrent
    flow that finished ``merge_integrate`` but has not yet run its own
    ``version_reconcile`` leaves its intent outstanding on master, and a branch
    cut from that master carries a verbatim copy — must NOT be in scope: consuming
    it would commit another flow's version outside its own step lifecycle,
    bypassing its confirmation/resume gate and collapsing two releases into one
    max bump. The branch's OWN intents (added by its own commits since it forked)
    are what this merge contributes.

    The scope is the branch tree's intents minus those present at its fork point
    (``merge-base(branch, target)``): an inherited intent sits at (or below) the
    fork point and is subtracted; the branch's own intents were added after the
    fork and survive.

    Rerun-recovery carve-out: once ``branch`` has already been integrated into
    ``target`` (a prior run whose ``version_reconcile`` faulted, leaving the
    branch's own intents outstanding on master for a whole-command rerun), the
    plain ``merge-base(branch, target)`` collapses to the branch tip and would
    subtract the branch's OWN intents away — silently dropping the very bump the
    rerun must land. Naively returning the branch's *full* intent set on that
    path is equally wrong in the other direction: it re-sweeps intents the branch
    only INHERITED (Flow A's outstanding intent, copied into a branch cut from
    that master), consuming them outside their own flow's confirmation/resume
    boundary. So we instead reconstruct the branch's true fork point from the
    integration itself: integrate always merges with ``--no-ff``
    (orchestrator), so the merge commit that brought ``branch`` into ``target``
    has ``branch`` as a parent, and ``merge-base(branch, its OTHER parent)`` is
    the pre-integration baseline. Subtracting that baseline's intents keeps the
    subtraction identical to the first run — inherited intents drop, the branch's
    own survive — regardless of whether ``branch`` is already merged.

    Raises:
        IntentReadError: propagated when the branch's / fork-point's intents or
            the ancestry test cannot be read. The caller must NOT treat an
            unreadable scope as an empty contribution (that would publish a merge
            with no reconcile bump and let branch cleanup delete the source).
    """
    branch_ids = intent_flow_ids_at_ref(project_root, branch)
    if not branch_ids:
        return set()
    if _is_ancestor(project_root, branch, target_ref):
        # Already integrated (rerun after a reconcile fault): the branch's own
        # intents live on the target now and the plain merge-base == branch tip,
        # so the ordinary subtraction would drop them. Recover the pre-merge
        # baseline from the integration merge commit and subtract THAT, so an
        # intent the branch merely inherited (still outstanding for its own flow's
        # reconcile) is not swept into this rerun's scope. reconcile()'s
        # git-durable reconcile-commit trailer keeps re-deriving the branch's own
        # already-committed intent a safe no-op.
        baseline_ids = _integration_baseline_ids(
            project_root, branch, target_ref
        )
        if baseline_ids is None:
            # No integration merge located (unexpected under --no-ff). Fall back
            # to the full set: dropping the branch's own bump on a rerun is the
            # worse failure, and the trailer idempotency still blocks a double
            # bump. Over-inclusion here only occurs in this already-degraded
            # path, never on the normal first-run scoping.
            return branch_ids
        return branch_ids - baseline_ids
    base = _merge_base(project_root, branch, target_ref)
    if base is None:
        # Unrelated histories: nothing was inherited from the target, so every
        # intent in the branch is contributed by it.
        return branch_ids
    baseline_ids = intent_flow_ids_at_ref(project_root, base)
    return branch_ids - baseline_ids


def _integration_baseline_ids(
    project_root: Path, branch: str, target_ref: str
) -> Optional[set[str]]:
    """Return the intents *branch* inherited, reconstructed after it was merged.

    Used only on the rerun-recovery path, where ``branch`` is already an ancestor
    of *target_ref* so ``merge-base(branch, target_ref)`` has collapsed to the
    branch tip and can no longer name the fork point. Because integrate merges
    with ``--no-ff``, the commit that integrated ``branch`` is a merge reachable
    from *target_ref* but not from ``branch`` and carries ``branch``'s tip as a
    parent; its OTHER parent(s) are master's pre-merge tip. The fork point is
    ``merge-base(branch, other_parent)`` and its intents are exactly what the
    branch inherited.

    Returns the inherited flow_id set, or ``None`` when no integration merge
    could be located (so the caller can fall back conservatively).

    Raises:
        IntentReadError: on a git infrastructure fault (subprocess error,
            timeout, or a nonzero exit while resolving the branch sha / walking
            history), mirroring the sibling readers so an unreadable scope is
            never silently degraded to "nothing inherited".
    """
    try:
        head = _run_git(
            project_root, "rev-parse", "--verify", f"{branch}^{{commit}}",
            check=False, timeout=15,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise IntentReadError(
            f"could not resolve branch tip for {branch!r}: {exc}"
        ) from exc
    if head.returncode != 0:
        raise IntentReadError(
            f"could not resolve branch tip for {branch!r} "
            f"(exit {head.returncode}): {head.stderr.strip()}"
        )
    branch_sha = head.stdout.strip()
    if not branch_sha:
        raise IntentReadError(f"empty branch tip for {branch!r}")

    try:
        walk = _run_git(
            project_root,
            "rev-list", "--parents", target_ref, f"^{branch}",
            check=False, timeout=15,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise IntentReadError(
            f"could not walk history from {target_ref!r}: {exc}"
        ) from exc
    if walk.returncode != 0:
        raise IntentReadError(
            f"git rev-list failed for {target_ref!r} (exit {walk.returncode}): "
            f"{walk.stderr.strip()}"
        )

    other_parents: set[str] = set()
    for line in walk.stdout.splitlines():
        shas = line.split()
        if len(shas) < 2:
            continue
        parents = shas[1:]
        if branch_sha in parents:
            # A commit that merged `branch` in; its non-branch parents are the
            # pre-merge master tip(s) `branch` was integrated onto.
            other_parents.update(p for p in parents if p != branch_sha)

    if not other_parents:
        return None

    baseline_ids: set[str] = set()
    for parent in sorted(other_parents):
        fork = _merge_base(project_root, branch, parent)
        if fork is None:
            continue
        baseline_ids |= intent_flow_ids_at_ref(project_root, fork)
    return baseline_ids


def mark_consumed(
    project_root: Path,
    flow_id: str,
    *,
    reconcile_commit: Optional[str] = None,
) -> bool:
    """Mark *flow_id*'s intent consumed so re-entry does not bump again.

    Idempotent: rewrites the on-disk intent with ``consumed=True`` (recording
    the reconcile commit sha when supplied). Returns ``True`` when a marking
    write happened, ``False`` when there was no intent file to mark or it was
    already consumed (both safe no-ops for a resumed reconcile).

    This on-disk flag is the fast path; :func:`reconcile_commit_exists` is the
    git-durable backstop for the window where this write landed but its commit
    did not.
    """
    intent = read_intent(project_root, flow_id)
    if intent is None:
        logger.debug("mark_consumed: no intent file for flow %s", flow_id)
        return False
    if intent.consumed:
        return False

    intent.consumed = True
    if reconcile_commit:
        intent.consumed_by = reconcile_commit
    write_intent(project_root, intent)
    return True


def is_consumed(
    project_root: Path,
    flow_id: str,
    *,
    ref: str = "HEAD",
    check_reconcile_commit: bool = True,
) -> bool:
    """Report whether *flow_id*'s intent has already been reconciled.

    The AUTHORITATIVE idempotency signal is the git-durable reconcile commit,
    NOT the on-disk ``consumed`` flag. The flag is only an auxiliary record that
    is meant to land atomically *inside* the reconcile commit; a ``consumed``
    flag with no matching reconcile commit is residue — a run that crashed after
    marking but before committing. Treating that residue as proof of completion
    would strand a feature with no committed version (the exact failure this
    redesign exists to prevent). So in the default path (``check_reconcile_commit``
    true) the decision rests solely on :func:`reconcile_commit_exists`; because
    the flag ships inside the reconcile commit, a legitimately-committed flag is
    always accompanied by that commit, so nothing durable is lost by ignoring the
    bare flag.

    ``check_reconcile_commit=False`` opts out of the git probe and consults only
    the on-disk flag; it exists for unit tests and callers that deliberately want
    the raw file-marker state, never as an idempotency verdict.

    A missing intent file means there is nothing to consume, so the file-only
    path returns ``False``.
    """
    if check_reconcile_commit:
        return reconcile_commit_exists(project_root, flow_id, ref=ref)
    intent = read_intent(project_root, flow_id)
    return intent is not None and intent.consumed


def reconcile_commit_exists(
    project_root: Path, flow_id: str, *, ref: str = "HEAD"
) -> bool:
    """Return ``True`` if a reconcile commit for *flow_id* exists under *ref*.

    Searches commit messages reachable from *ref* for the
    ``Version-Reconcile-Session: <flow_id>`` trailer the reconcile step stamps,
    matched as an EXACT full message line. A substring/prefix match (which the
    bare ``--grep --fixed-strings`` filter gives) would classify flow_id ``X`` as
    already reconciled by a sibling whose trailer is
    ``Version-Reconcile-Session: Xy`` — reachable via the CLI path, which derives
    flow_ids from arbitrary intent filenames — silently skipping X's bump. So
    ``--grep`` is used only as a coarse pre-filter and each candidate commit body
    is confirmed to contain the trailer as a stripped full line (the same
    hardening undo_last_reconcile applies).

    This is the git-durable, AUTHORITATIVE idempotency signal: even if the intent
    file's ``consumed`` flag was never persisted/committed, the presence of the
    reconcile commit itself proves the bump already happened.

    A DEFINITIVELY-ABSENT result — git ran and the repo/ref simply has no such
    commit (non-zero returncode from a non-repo/bad ref, or no full-line match) —
    returns ``False``. A PROBE FAILURE (timeout / subprocess error) must NOT fail
    open to "absent": returning ``False`` on a transient fault would let an
    already-reconciled intent re-register as outstanding and get bumped a second
    time (the committed on-disk flag is deliberately ignored by the reconcile
    caller, so there is no fallback to catch it). It is therefore retried once
    and, if still failing, raised as :class:`IntentReadError`.

    Raises:
        IntentReadError: when the git probe cannot be completed (transient
            subprocess/OS fault persisting across a retry) — distinct from a
            successful probe that found no commit.
    """
    marker = f"{RECONCILE_TRAILER}: {flow_id}"
    last_exc: Optional[Exception] = None
    for attempt in range(2):
        try:
            result = _run_git(
                project_root,
                "log",
                ref,
                f"--grep={marker}",
                "--fixed-strings",
                "-z",
                "--format=%B",
                check=False,
                timeout=15,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            last_exc = exc
            logger.debug(
                "reconcile_commit_exists: git log failed for flow %s (attempt %d): %s",
                flow_id,
                attempt + 1,
                exc,
            )
            continue
        if result.returncode != 0:
            # git ran and reported an error (not a repo, bad/empty ref): there is
            # definitively no reconcile commit reachable here.
            return False
        # ``--grep`` is a coarse substring pre-filter; confirm an EXACT full-line
        # trailer match so a prefix sibling (``...: Xy``) never counts as X's own
        # reconcile commit. ``-z`` NUL-separates the matched commit bodies.
        for body in (result.stdout or "").split("\0"):
            if marker in {line.strip() for line in body.splitlines()}:
                return True
        return False
    # Both attempts hit a transient probe fault — refuse to fail open into a
    # potential double-bump; surface the fault so the caller aborts/retries.
    raise IntentReadError(
        f"reconcile_commit_exists: could not probe git for flow {flow_id}: {last_exc}"
    ) from last_exc


def _atomic_write_text(path: Path, content: str) -> None:
    """Write *content* to *path* atomically (temp file + ``os.replace``).

    Refuses to overwrite a symlink at the destination (defense-in-depth,
    matching version_aggregator._atomic_write_text) so a planted symlink can't
    redirect the write onto an unrelated tracked file.
    """
    parent = path.parent
    try:
        lst = os.lstat(str(path))
    except OSError:
        lst = None
    if lst is not None and stat.S_ISLNK(lst.st_mode):
        raise OSError(
            errno.ELOOP,
            "Refusing to overwrite symlink at destination",
            str(path),
        )

    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                # Some filesystems (tmpfs) reject fsync; the write itself
                # succeeded, only durability is reduced.
                pass
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
