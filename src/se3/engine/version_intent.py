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
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(intent.to_dict(), indent=2, ensure_ascii=False) + "\n"
    _atomic_write_text(path, payload)
    logger.debug("Wrote version intent for flow %s to %s", intent.flow_id, path)
    return path


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

    True when EITHER the on-disk intent carries ``consumed=True`` OR (unless
    disabled) a reconcile commit stamped for this session already exists in
    git history. The two sources are OR'd so that either the file-marker path
    or the commit-trailer path alone is enough to stop a double bump — closing
    the gap where one signal was written but the other was not.

    A missing intent file does not by itself mean "consumed"; it means there is
    nothing to consume, so this returns ``False`` (the reconcile-commit check
    can still flip it to ``True`` if a commit trailer is present).
    """
    intent = read_intent(project_root, flow_id)
    if intent is not None and intent.consumed:
        return True
    if check_reconcile_commit and reconcile_commit_exists(
        project_root, flow_id, ref=ref
    ):
        return True
    return False


def reconcile_commit_exists(
    project_root: Path, flow_id: str, *, ref: str = "HEAD"
) -> bool:
    """Return ``True`` if a reconcile commit for *flow_id* exists under *ref*.

    Searches commit messages reachable from *ref* for the
    ``Version-Reconcile-Session: <flow_id>`` trailer the reconcile step stamps.
    This is the git-durable idempotency signal: even if the intent file's
    ``consumed`` flag was never persisted/committed, the presence of the
    reconcile commit itself proves the bump already happened.

    Any git failure (not a repo, bad ref, timeout) is treated as "not found"
    rather than raising — the caller falls back to the on-disk marker.
    """
    marker = f"{RECONCILE_TRAILER}: {flow_id}"
    try:
        result = _run_git(
            project_root,
            "log",
            ref,
            f"--grep={marker}",
            "--fixed-strings",
            "-n",
            "1",
            "--format=%H",
            check=False,
            timeout=15,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.debug(
            "reconcile_commit_exists: git log failed for flow %s: %s",
            flow_id,
            exc,
        )
        return False
    if result.returncode != 0:
        return False
    return bool(result.stdout.strip())


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
