"""Post-condition assertions for merge success paths.

Every branch that the orchestrator marks as "successfully merged"
MUST pass three independent post-conditions before the result is
considered final:

  1. **Ancestry**: the branch is an ancestor of HEAD.
  2. **Merge commit**: HEAD is a merge commit (at least 2 parents),
     unless the branch was already an ancestor (no-op).
  3. **Version bumped**: the version actually advanced (only checked
     when version aggregation is enabled and expected).

Any violation raises :class:`PostConditionViolated` carrying a typed
:class:`FailureReason` so the orchestrator can route the failure to
the correct diagnostic bucket.
"""

from __future__ import annotations

import errno
import logging
import os
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Optional

from .failure_reason import FailureReason

logger = logging.getLogger(__name__)


# Bounded-read caps for the version-file post-condition check.  These
# mirror the threat model handled by ``runtime_sync._bounded_read_chunks``:
# a pathological ``pyproject.toml`` (multi-GB by accident, a streaming
# FIFO that slipped past ``Path.exists()``, an editor backup that was
# auto-resolved by symlink) would otherwise hang or OOM the post-merge
# check.  Reaching either cap is a loud :class:`PostConditionViolated`
# rather than a silent skip.  Sized for legitimate version files: 16 MiB
# / 10 s is far above any sane pyproject.toml or package.json (typical
# ones are <100 KiB).
_VERSION_FILE_MAX_BYTES: Final[int] = 16 * 1024 * 1024  # 16 MiB
_VERSION_FILE_READ_CHUNK_SIZE: Final[int] = 65536  # 64 KiB
_VERSION_FILE_MAX_DURATION_S: Final[float] = 10.0


def _bounded_read_version_file(
    version_file: Path,
) -> str:
    """Read *version_file* with size and duration caps.

    Refuses non-regular files (FIFOs, sockets, device nodes) up-front so
    a streaming source that slipped past ``Path.exists()`` cannot hang
    the post-condition check.  Reads are capped at
    :data:`_VERSION_FILE_MAX_BYTES` bytes and
    :data:`_VERSION_FILE_MAX_DURATION_S` seconds; reaching either cap
    raises :class:`OSError` with a descriptive errno.

    Note on deadline semantics: the time cap is checked between chunk
    reads, NOT during a single ``os.read`` call.  A single hung
    ``os.read`` (e.g. on a slow FUSE filesystem) can therefore block
    past the deadline; the cap fires only when the read returns.  The
    cap is best-effort — a strict ceiling would require non-blocking
    I/O loops or ``signal.alarm``, neither of which is portable across
    the platforms this code targets.

    Returns the file contents decoded as UTF-8.

    Raises:
        OSError: For non-regular files, size cap exceeded, time cap
            exceeded, no-progress reads, or any underlying I/O failure.
    """
    open_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        # Don't follow symlinks — symlinks at the version-file path
        # would invite TOCTOU and expand the read surface to arbitrary
        # paths the attacker can swing.
        open_flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        # On Linux/macOS, opening a FIFO with O_NONBLOCK returns ENXIO
        # when no writer is connected — preferable to blocking forever.
        # For regular files O_NONBLOCK is a no-op.
        open_flags |= os.O_NONBLOCK

    fd = os.open(str(version_file), open_flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(
                errno.EINVAL,
                f"Version file is not a regular file (mode={oct(st.st_mode)})",
                str(version_file),
            )
        if st.st_size > _VERSION_FILE_MAX_BYTES:
            raise OSError(
                errno.EFBIG,
                f"Version file exceeds size cap "
                f"({st.st_size} > {_VERSION_FILE_MAX_BYTES} bytes)",
                str(version_file),
            )

        # Some platforms still let O_NONBLOCK affect read on regular
        # files (rare); restore blocking semantics so a normal read
        # cannot return EAGAIN spuriously.
        if hasattr(os, "O_NONBLOCK"):
            try:
                import fcntl
                flags = fcntl.fcntl(fd, fcntl.F_GETFL)
                fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
            except (OSError, ImportError):
                # Best-effort — fall through to the bounded read loop,
                # which will surface any persistent EAGAIN as an OSError.
                pass

        deadline = time.monotonic() + _VERSION_FILE_MAX_DURATION_S
        chunks: list[bytes] = []
        total = 0
        while True:
            if total >= _VERSION_FILE_MAX_BYTES:
                raise OSError(
                    errno.EFBIG,
                    f"Bounded read byte cap exceeded "
                    f"({_VERSION_FILE_MAX_BYTES} bytes)",
                    str(version_file),
                )
            if time.monotonic() > deadline:
                raise OSError(
                    errno.ETIMEDOUT,
                    f"Bounded read time cap exceeded "
                    f"({_VERSION_FILE_MAX_DURATION_S} s)",
                    str(version_file),
                )
            chunk = os.read(fd, _VERSION_FILE_READ_CHUNK_SIZE)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        return b"".join(chunks).decode("utf-8")
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


class PostConditionViolated(RuntimeError):
    """A post-condition assertion failed.

    Attributes:
        reason: The typed failure reason.
        branch: The branch being checked (if applicable).
        detail: Human-readable diagnostic detail.
    """

    def __init__(
        self,
        reason: FailureReason,
        branch: Optional[str] = None,
        detail: str = "",
    ) -> None:
        self.reason = reason
        self.branch = branch
        self.detail = detail
        parts = [f"Post-condition violated: {reason.name}"]
        if branch:
            parts.append(f"(branch={branch})")
        if detail:
            parts.append(f"— {detail}")
        super().__init__(" ".join(parts))


class CorruptCommitGraphError(RuntimeError):
    """The commit graph appears malformed (e.g. >64 parents).

    Raised by :func:`_count_parents` when the parent count hits the
    safety cap, so callers do not silently treat the result as
    ``not a merge commit``.
    """

    def __init__(self, ref: str, cap: int) -> None:
        self.ref = ref
        self.cap = cap
        super().__init__(
            f"Commit {ref} exceeds the {cap}-parent safety cap — "
            f"the commit graph is almost certainly corrupt or malformed."
        )


def _run_git(
    project_root: Path,
    *args: str,
    check: bool = False,
    timeout: int = 30,
) -> subprocess.CompletedProcess:
    """Run a git command and return the result."""
    cmd = ["git", "-C", str(project_root)] + list(args)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
        stdin=subprocess.DEVNULL,
    )
    return result


def assert_branch_merged(
    project_root: Path,
    branch: str,
    *,
    timeout: int = 15,
) -> None:
    """Assert that *branch* is an ancestor of HEAD.

    Uses ``git merge-base --is-ancestor`` which exits 0 when the
    branch is reachable from HEAD.

    Args:
        project_root: Path to the git repository.
        branch: Branch name to check.
        timeout: Subprocess timeout in seconds. Default matches every
            existing caller (15 s) so a future caller relying on the
            default does not get a different value than every other
            call site.

    Raises:
        PostConditionViolated: If the branch is NOT an ancestor of
            HEAD (the merge did not actually integrate the branch).
    """
    result = _run_git(
        project_root,
        "merge-base",
        "--is-ancestor",
        branch,
        "HEAD",
        timeout=timeout,
    )
    if result.returncode != 0:
        # ``git merge-base --is-ancestor`` documents only two definitive
        # outcomes: rc=0 → ancestor, rc=1 → NOT an ancestor.  Any other
        # returncode (rc=2 usage error / rc=128+ object-resolution error
        # / signal-killed children with rc>=128) is git telling us it
        # could NOT reach a verdict — treating any of those as
        # "definitively not merged" would be a silent-merge-loss false
        # positive.  Only rc==1 expresses the "branch is not an ancestor"
        # verdict; everything else is an unresolvable diagnostic state.
        if result.returncode == 1:
            reason = FailureReason.POSTCOND_BRANCH_NOT_MERGED
            detail = (
                f"git merge-base --is-ancestor {branch} HEAD returned "
                f"{result.returncode}; branch is not an ancestor of HEAD"
            )
        else:
            reason = FailureReason.POSTCOND_BRANCH_UNRESOLVABLE
            detail = (
                f"git merge-base --is-ancestor {branch} HEAD returned "
                f"{result.returncode} (git error — branch may have been "
                f"deleted, .git/HEAD may be invalid, the repository state "
                f"may be corrupted, or the subprocess was signalled). "
                f"Returncode is not the documented '1=not ancestor' verdict, "
                f"so this is treated as unresolvable rather than as a "
                f"definitive merge-loss diagnosis."
            )
        raise PostConditionViolated(
            reason,
            branch=branch,
            detail=detail,
        )
    logger.debug("Post-condition OK: %s is ancestor of HEAD", branch)


_DEFAULT_PARENT_COUNT_CAP: Final[int] = 64
_PARENT_COUNT_CAP_ENV: Final[str] = "SE3_MERGE_PARENT_COUNT_CAP"


def _resolve_parent_count_cap() -> int:
    """Return the configured parent-count safety cap.

    The default 64 is comfortable for any realistic octopus merge:
    Linux kernel history occasionally reaches double-digit parents, and
    64 leaves an order-of-magnitude buffer above that.  Operators on
    repos that legitimately rebase very-wide octopuses can override the
    cap via the ``SE3_MERGE_PARENT_COUNT_CAP`` environment variable
    (positive integer; values below 2 are clamped to the default to
    keep the merge-commit-shape check meaningful).
    """
    raw = os.environ.get(_PARENT_COUNT_CAP_ENV, "").strip()
    if not raw:
        return _DEFAULT_PARENT_COUNT_CAP
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not an integer; using default cap %d",
            _PARENT_COUNT_CAP_ENV, raw, _DEFAULT_PARENT_COUNT_CAP,
        )
        return _DEFAULT_PARENT_COUNT_CAP
    if value < 2:
        logger.warning(
            "%s=%d is below the minimum (2); using default cap %d",
            _PARENT_COUNT_CAP_ENV, value, _DEFAULT_PARENT_COUNT_CAP,
        )
        return _DEFAULT_PARENT_COUNT_CAP
    return value


def _count_parents(
    project_root: Path,
    ref: str,
    timeout: int,
    *,
    cap: Optional[int] = None,
) -> tuple[int, bool]:
    """Count parents of *ref* via rev-parse --verify ref^N.

    Returns a tuple ``(count, ref_exists)``:

    - ``count``: number of parents found before the first ``rev-parse``
      failure or the safety cap.
    - ``ref_exists``: ``True`` when *ref* itself can be resolved by
      ``git rev-parse --verify``, ``False`` when *ref* is unverifiable
      (detached, garbage-collected, or otherwise unreadable).  Callers
      can use this to distinguish "ref unreadable" from "ref has too
      few parents" in diagnostic messages.

    The safety cap defaults to :data:`_DEFAULT_PARENT_COUNT_CAP` (64)
    but can be overridden via ``SE3_MERGE_PARENT_COUNT_CAP`` for
    repositories with legitimately wide octopus merges.

    Raises:
        CorruptCommitGraphError: If the parent count exceeds the
            configured safety cap (pathological or malformed commit
            graph).
    """
    effective_cap = cap if cap is not None else _resolve_parent_count_cap()

    # First verify *ref* itself exists so callers can disambiguate
    # the "ref unreadable" branch from "ref has 0 parents".
    verify_self = _run_git(
        project_root,
        "rev-parse",
        "--verify",
        ref,
        timeout=timeout,
    )
    ref_exists = verify_self.returncode == 0

    count = 0
    n = 1
    while True:
        result = _run_git(
            project_root,
            "rev-parse",
            "--verify",
            f"{ref}^{n}",
            timeout=timeout,
        )
        if result.returncode != 0:
            break
        # Defense against extremely rare repository corruption: a
        # rev-parse that exits 0 but emits empty/garbage stdout would
        # otherwise let the loop walk to the safety cap before giving
        # up.  Treat a non-SHA stdout as a "no more parents" signal —
        # the safety cap remains a backstop, but the common-case
        # termination is now driven by stdout content.
        out_text = (result.stdout or "").strip()
        if not out_text or not all(
            "0" <= ch <= "9" or "a" <= ch <= "f" or "A" <= ch <= "F"
            for ch in out_text
        ):
            logger.warning(
                "_count_parents: rev-parse for %s^%d exited 0 but produced "
                "non-SHA stdout %r — treating as no further parents.",
                ref, n, out_text,
            )
            break
        count += 1
        n += 1
        # Safety cap: octopus merges with absurd parent counts are
        # pathological.  See ``_resolve_parent_count_cap`` for the
        # operator override path.
        if n > effective_cap:
            logger.warning(
                "_count_parents: parent count for %s hit the safety cap (%d). "
                "This is almost certainly a corrupt or malformed commit graph. "
                "Treating as error.",
                ref, effective_cap,
            )
            # Cap exceeded — the graph is pathological.  Raise a typed
            # exception so callers cannot accidentally treat this as
            # ``not a merge commit`` (e.g. via ``parent_count == 0``).
            raise CorruptCommitGraphError(ref, effective_cap)
    return count, ref_exists


def assert_head_is_merge_commit(
    project_root: Path,
    branch: str,
    *,
    min_parents: int = 2,
    allow_fixup_parent: bool = False,
    max_fixup_depth: int = 1,
    timeout: int = 30,
) -> None:
    """Assert that HEAD is a merge commit.

    This is a stronger check than ``assert_branch_merged``: even if
    the branch is an ancestor, we want to confirm that the LAST
    commit on HEAD is actually a merge (not e.g. a fast-forward or
    an unrelated commit pushed after the merge).

    **Fix-up commit tolerance** (opt-in):  After guardrail repair, a
    fix-up commit may be placed on top of the merge commit.  In that
    case HEAD itself has 1 parent, but HEAD^1 is the merge commit.
    Pass ``allow_fixup_parent=True`` from the repair-completed path
    to accept this layout.  The default is ``False`` so that a stray
    commit appended on top of a merge (e.g. by a hook) does NOT pass
    the post-condition silently for clean-merge / LLM-resolved
    callers.

    **Stacked fix-up tolerance**: When fast-mode guardrail repair
    creates a fix-up commit AND the subsequent version aggregation
    runs in non-amend mode (HEAD already published), HEAD ends up as
    ``[bump_commit → fix_up_commit → merge_commit]`` — depth 2.  Pass
    ``max_fixup_depth=2`` (or higher) to walk back further.  Each
    intermediate commit must itself be a single-parent (linear) commit
    so we cannot accidentally accept a side-branch merge.

    **Octopus limitation (documented, not active bug)**: The
    parent-count assertion is shape-only — it confirms HEAD has
    ``>= min_parents`` parents but does NOT verify the parent *set*
    matches an expected list of branch SHAs.  ``se3 merge`` today
    runs strictly pairwise (one ``git merge`` per branch in the
    argument list), so every commit produced has exactly 2 parents
    and the shape check is sufficient.  If a future change ever
    routes through ``git merge -s octopus`` with N>=2 branches, this
    function alone cannot prove that all N expected branches landed:
    callers SHOULD pair this assertion with explicit
    ``assert_branch_merged`` calls per branch (which performs the
    ancestry check) so coverage is shape-and-membership.  See the
    spec-guardrails Requirement section for the contract.

    Args:
        project_root: Path to the git repository.
        branch: Branch name (for diagnostic messages only).
        min_parents: Minimum number of parents HEAD must have.
        allow_fixup_parent: When ``True``, also accept an ancestor
            of HEAD as a merge commit (fix-up layout).  Default
            ``False``.
        max_fixup_depth: When ``allow_fixup_parent=True``, walk back
            up to this many parents looking for a merge commit.
            ``1`` (default) accepts HEAD^1; ``2`` accepts HEAD^1 or
            HEAD~2; etc.  Ignored when ``allow_fixup_parent=False``.
        timeout: Subprocess timeout in seconds.

    Raises:
        PostConditionViolated: If HEAD does not satisfy the merge-
            commit shape (and, when ``allow_fixup_parent=True``,
            no ancestor up to ``max_fixup_depth`` does either).
    """
    result = _run_git(
        project_root,
        "rev-parse",
        "HEAD",
        timeout=timeout,
    )
    if result.returncode != 0:
        raise PostConditionViolated(
            FailureReason.POSTCOND_HEAD_NOT_MERGE_COMMIT,
            branch=branch,
            detail="git rev-parse HEAD failed — detached or empty repository?",
        )
    head_sha = result.stdout.strip()

    try:
        head_parents, _ = _count_parents(project_root, "HEAD", timeout)
    except CorruptCommitGraphError as exc:
        raise PostConditionViolated(
            FailureReason.POSTCOND_HEAD_NOT_MERGE_COMMIT,
            branch=branch,
            detail=str(exc),
        )
    if head_parents >= min_parents:
        logger.debug(
            "Post-condition OK: HEAD (%s) has %s parent(s)",
            head_sha[:8],
            head_parents,
        )
        return

    if not allow_fixup_parent:
        raise PostConditionViolated(
            FailureReason.POSTCOND_HEAD_NOT_MERGE_COMMIT,
            branch=branch,
            detail=(
                f"HEAD ({head_sha[:8]}) has {head_parents} parent(s), "
                f"expected >= {min_parents}"
            ),
        )

    if max_fixup_depth < 1:
        # Defensive: a caller that passes allow_fixup_parent=True with a
        # non-positive depth almost certainly intended depth=1; treat
        # zero as a configuration nudge rather than silently accepting.
        max_fixup_depth = 1

    # Opt-in fix-up tolerance: walk up to max_fixup_depth parents.  Each
    # intermediate ancestor must be a single-parent (linear) commit; we
    # only accept the FIRST ancestor whose parent_count >= min_parents.
    # An intermediate ancestor with parent_count==0 is treated as
    # unverifiable (the chain has ended before a merge commit was
    # found).
    #
    # Strict linearity contract: when an intermediate commit (depth N
    # whose parent_count is < min_parents) is encountered, require
    # parent_count == 1 explicitly.  A non-1, non-merge value would
    # otherwise be silently swallowed by the implicit "continue past
    # this depth" branch — for example, with min_parents=3 (octopus),
    # a 2-parent intermediate would slip through and we would
    # falsely declare the next-deeper commit "the merge".  Refusing
    # to traverse past a non-linear intermediate makes the contract
    # explicit and gives operators a precise error pointer.
    last_parents_per_depth: list[int] = []
    last_unreadable_depth: Optional[int] = None
    for depth in range(1, max_fixup_depth + 1):
        ref = f"HEAD~{depth}" if depth > 1 else "HEAD^1"
        try:
            anc_parents, anc_exists = _count_parents(
                project_root, ref, timeout
            )
        except CorruptCommitGraphError as exc:
            raise PostConditionViolated(
                FailureReason.POSTCOND_HEAD_NOT_MERGE_COMMIT,
                branch=branch,
                detail=str(exc),
            )
        if not anc_exists:
            last_unreadable_depth = depth
            break
        last_parents_per_depth.append(anc_parents)
        if anc_parents >= min_parents:
            logger.debug(
                "Post-condition OK: HEAD (%s) is a fix-up chain (depth=%d) "
                "on top of merge commit %s (%s parent(s))",
                head_sha[:8],
                depth,
                ref,
                anc_parents,
            )
            return
        if anc_parents == 0:
            # Initial commit — chain ended without finding a merge.
            break
        if anc_parents != 1:
            # Non-linear intermediate (e.g. 2-parent commit when
            # min_parents=3): refuse to traverse past it.  This
            # closes the gap where a stray side-branch merge in the
            # fix-up chain would be silently bypassed in search of a
            # deeper merge ancestor.
            raise PostConditionViolated(
                FailureReason.POSTCOND_HEAD_NOT_MERGE_COMMIT,
                branch=branch,
                detail=(
                    f"HEAD ({head_sha[:8]}) fix-up chain at depth {depth} "
                    f"has {anc_parents} parent(s) — expected exactly 1 "
                    f"(linear fix-up) before reaching the merge commit. "
                    f"A non-linear intermediate may indicate a stray "
                    f"side-branch merge or unexpected commit topology."
                ),
            )

    if last_unreadable_depth is not None:
        raise PostConditionViolated(
            FailureReason.POSTCOND_HEAD_NOT_MERGE_COMMIT,
            branch=branch,
            detail=(
                f"HEAD ({head_sha[:8]}) has {head_parents} parent(s); "
                f"HEAD~{last_unreadable_depth} is unverifiable (detached, "
                f"garbage-collected, or unreadable). Cannot confirm "
                f"merge-commit shape within fix-up depth "
                f"{max_fixup_depth}."
            ),
        )

    chain_summary = ", ".join(
        f"HEAD~{d if d > 1 else 1} parents={p}"
        for d, p in enumerate(last_parents_per_depth, start=1)
    )
    raise PostConditionViolated(
        FailureReason.POSTCOND_HEAD_NOT_MERGE_COMMIT,
        branch=branch,
        detail=(
            f"HEAD ({head_sha[:8]}) has {head_parents} parent(s); "
            f"fix-up chain up to depth {max_fixup_depth} did not reach "
            f"a merge commit ({chain_summary}); expected >= {min_parents}"
        ),
    )


def assert_version_bumped(
    project_root: Path,
    expected_version: str,
    *,
    version_file: Optional[Path] = None,
    timeout: int = 30,
    check_commit_tree: bool = True,
) -> None:
    """Assert that the version file contains *expected_version*.

    This is the final post-condition check after version aggregation:
    the version file on disk must reflect the bump we just applied.

    By default, both the working-tree file and the committed (HEAD)
    version of that file are checked.  This catches the silent-loss
    vector where a commit hook (e.g. pre-commit auto-formatter) modifies
    the version file after the orchestrator wrote it but before
    ``git commit --amend`` finalized: the working tree and HEAD would
    diverge, and a working-tree-only check would pass even though the
    committed version reflects whatever the hook wrote.

    Args:
        project_root: Path to the project root.
        expected_version: The version string that should be present.
        version_file: Path to the version file (if ``None``, defaults
            to ``pyproject.toml`` in the project root).
        timeout: Subprocess timeout for any git commands.
        check_commit_tree: When ``True`` (default), also verify the
            version recorded in the HEAD commit's tree matches the
            working-tree version, defending against commit hooks
            silently rewriting the version between disk write and
            commit finalization.  Set to ``False`` only in test
            harnesses that operate without a real git history.

    Raises:
        PostConditionViolated: If the version file does not contain
            the expected version.
    """
    if version_file is None:
        version_file = project_root / "pyproject.toml"

    if not version_file.exists():
        raise PostConditionViolated(
            FailureReason.POSTCOND_VERSION_NOT_BUMPED,
            detail=f"Version file not found: {version_file}",
        )

    try:
        # Bounded read: refuses FIFOs/devices, caps total bytes
        # (_VERSION_FILE_MAX_BYTES) and duration
        # (_VERSION_FILE_MAX_DURATION_S) so a pathological pyproject.toml
        # cannot hang the post-condition check (mirrors the threat
        # model handled by runtime_sync._bounded_read_chunks).
        content = _bounded_read_version_file(version_file)
    except OSError as exc:
        raise PostConditionViolated(
            FailureReason.POSTCOND_VERSION_NOT_BUMPED,
            detail=f"Cannot read version file {version_file}: {exc}",
        )

    actual_version: str | None = None
    is_toml_file = (
        version_file.suffix == ".toml" or version_file.name == "pyproject.toml"
    )
    if is_toml_file:
        # Parse TOML to read the actual version field (not a comment or
        # dependency pin that happens to contain the version string).
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib  # type: ignore

        try:
            data = tomllib.loads(content)
        except Exception as exc:
            # For TOML files, parse failure is itself a post-condition
            # violation: the bare-line substring check (used for plain
            # text files) would be unsound on TOML — a dependency pin or
            # comment containing the expected version string would
            # spuriously satisfy the assertion.
            raise PostConditionViolated(
                FailureReason.POSTCOND_VERSION_NOT_BUMPED,
                detail=(
                    f"Failed to parse TOML version file {version_file}: {exc}. "
                    f"Cannot verify version bump from a malformed TOML file."
                ),
            )
        actual_version = data.get("project", {}).get("version")
        if actual_version is None:
            actual_version = data.get("tool", {}).get("poetry", {}).get("version")
        if actual_version is None:
            raise PostConditionViolated(
                FailureReason.POSTCOND_VERSION_NOT_BUMPED,
                detail=(
                    f"TOML file {version_file} does not declare a version "
                    f"(neither project.version nor tool.poetry.version). "
                    f"Expected '{expected_version}'."
                ),
            )

    # For package.json, parse JSON to avoid substring false-positives.
    if actual_version is None and version_file.name == "package.json":
        try:
            import json
            data = json.loads(content)
            actual_version = data.get("version")
        except (json.JSONDecodeError, ValueError) as exc:
            raise PostConditionViolated(
                FailureReason.POSTCOND_VERSION_NOT_BUMPED,
                detail=(
                    f"Failed to parse package.json {version_file}: {exc}. "
                    f"Cannot verify version bump from a malformed JSON file."
                ),
            )
        if actual_version != expected_version:
            raise PostConditionViolated(
                FailureReason.POSTCOND_VERSION_NOT_BUMPED,
                detail=(
                    f"package.json has version '{actual_version}' "
                    f"(expected '{expected_version}')"
                ),
            )
        logger.debug(
            "Post-condition OK: package.json version %s", expected_version
        )
        if check_commit_tree:
            _assert_committed_version_matches(
                project_root,
                version_file,
                expected_version,
                timeout=timeout,
            )
        return

    # For non-TOML, non-JSON files, accept ONLY a file whose sole content
    # is exactly the version string (e.g. a plain VERSION file).  Substring
    # or prefix matching (e.g. a line starting with "version" anywhere in
    # the file) is intentionally rejected because it produces false positives
    # on CHANGELOG, README, and NOTES files that mention the version in an
    # unrelated context.  Projects using non-standard version files should
    # either configure version.file_path explicitly or adopt a plain
    # VERSION file.
    if actual_version is None:
        stripped = content.strip()
        if stripped == expected_version:
            logger.debug(
                "Post-condition OK: version file contains %s", expected_version
            )
            if check_commit_tree:
                _assert_committed_version_matches(
                    project_root,
                    version_file,
                    expected_version,
                    timeout=timeout,
                )
            return
        raise PostConditionViolated(
            FailureReason.POSTCOND_VERSION_NOT_BUMPED,
            detail=(
                f"Version file {version_file} is not a supported structured "
                f"format (TOML / JSON) and does not contain exactly the "
                f"version string '{expected_version}'. "
                f"Use pyproject.toml, package.json, or a plain VERSION file."
            ),
        )

    if actual_version != expected_version:
        raise PostConditionViolated(
            FailureReason.POSTCOND_VERSION_NOT_BUMPED,
            detail=(
                f"Version file {version_file} has version "
                f"'{actual_version}' (expected '{expected_version}')"
            ),
        )
    logger.debug(
        "Post-condition OK: version file contains %s", expected_version
    )
    if check_commit_tree:
        _assert_committed_version_matches(
            project_root,
            version_file,
            expected_version,
            timeout=timeout,
        )


def _assert_committed_version_matches(
    project_root: Path,
    version_file: Path,
    expected_version: str,
    *,
    timeout: int = 30,
) -> None:
    """Verify HEAD's committed version of *version_file* matches *expected_version*.

    Defends against commit hooks (pre-commit, lint-staged, etc.) silently
    rewriting the version field between the orchestrator's disk write
    and ``git commit --amend`` finalizing.  The working-tree file would
    show *expected_version* (the orchestrator's last write) while HEAD
    would record whatever the hook wrote — a silent mismatch.

    Failure-mode policy: this check is the secondary defense that
    *exists specifically* to catch commit-hook tampering.  A commit
    hook can corrupt pyproject.toml in ways that ALSO break TOML
    parsing or strip the version field altogether — exactly the
    "unable to verify" branches.  Earlier code silently downgraded
    those to WARNING + non-raising return, which means a hook that
    corrupted the committed file but happened to leave the working-
    tree write succeeding would slip past this gate.

    The new policy distinguishes "transient git plumbing problem"
    (no HEAD yet, file untracked, git binary missing) — these are
    legitimate "unable to verify" — from "committed file exists but
    is malformed or missing the version field" — which is a positive
    signal that something tampered with HEAD's content and SHALL
    raise :class:`PostConditionViolated`.
    """
    try:
        relative = version_file.resolve().relative_to(
            project_root.resolve()
        ).as_posix()
    except (ValueError, OSError) as exc:
        logger.warning(
            "Commit-tree version check unable to verify (cannot derive "
            "relative path for %s): %s",
            version_file, exc,
        )
        return

    try:
        result = _run_git(
            project_root,
            "show",
            f"HEAD:{relative}",
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        # ``git show`` could not produce output at all (subprocess
        # timeout or OSError starting the binary).  This is a
        # diagnostic-impossible state, NOT a tampering signal — the
        # committed file may well be clean; we simply could not see
        # it.  Surface as WARNING and continue.
        logger.warning(
            "Commit-tree version check unable to verify (git show failed "
            "for %s): %s",
            relative, exc,
        )
        return

    if result.returncode != 0:
        # ``git show HEAD:<path>`` returns non-zero when the path is not
        # tracked at HEAD or HEAD does not exist.  Distinguish: if HEAD
        # itself is missing (initial commit not yet made / detached
        # garbage state) the check cannot apply at all.  If HEAD exists
        # but the path is untracked, that IS a positive tampering signal
        # — the orchestrator just wrote the version file and committed
        # it; a hook that removed/renamed the file is exactly what we
        # want to catch.
        head_check = _run_git(
            project_root,
            "rev-parse",
            "--verify",
            "HEAD",
            timeout=timeout,
        )
        if head_check.returncode != 0:
            logger.warning(
                "Commit-tree version check unable to verify (no HEAD; "
                "git show returncode=%d for %s, stderr=%s)",
                result.returncode, relative, result.stderr.strip()[:200],
            )
            return
        # HEAD exists but the version file is not at HEAD — tampering.
        raise PostConditionViolated(
            FailureReason.POSTCOND_VERSION_NOT_BUMPED,
            detail=(
                f"git show HEAD:{relative} returned "
                f"{result.returncode} (stderr={result.stderr.strip()[:200]}). "
                f"The version file was just written and committed; HEAD not "
                f"containing it indicates a commit hook removed or renamed "
                f"the file between the bump write and commit finalization."
            ),
        )

    committed_content = result.stdout
    committed_version: Optional[str] = None

    is_toml_file = (
        version_file.suffix == ".toml" or version_file.name == "pyproject.toml"
    )
    if is_toml_file:
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib  # type: ignore
        try:
            data = tomllib.loads(committed_content)
        except Exception as exc:
            # The committed file PARSED CLEANLY when the orchestrator
            # wrote it (otherwise the working-tree assertion would have
            # already raised).  A parse failure on HEAD's content
            # therefore means the committed bytes diverge from what was
            # written — the canonical hook-corruption signal we exist
            # to catch.
            raise PostConditionViolated(
                FailureReason.POSTCOND_VERSION_NOT_BUMPED,
                detail=(
                    f"HEAD's committed {relative} is not parseable TOML: "
                    f"{exc}. The working tree parses cleanly, so a commit "
                    f"hook (.git/hooks/* or pre-commit framework) likely "
                    f"rewrote the file between the bump write and commit "
                    f"finalization."
                ),
            ) from exc
        committed_version = data.get("project", {}).get("version")
        if committed_version is None:
            committed_version = (
                data.get("tool", {}).get("poetry", {}).get("version")
            )
    elif version_file.name == "package.json":
        try:
            import json
            data = json.loads(committed_content)
            committed_version = data.get("version")
        except (json.JSONDecodeError, ValueError) as exc:
            # Same reasoning as TOML parse failure above.
            raise PostConditionViolated(
                FailureReason.POSTCOND_VERSION_NOT_BUMPED,
                detail=(
                    f"HEAD's committed {relative} is not parseable JSON: "
                    f"{exc}. The working tree parses cleanly, so a commit "
                    f"hook likely rewrote the file between the bump write "
                    f"and commit finalization."
                ),
            ) from exc
    else:
        committed_version = committed_content.strip() or None

    if committed_version is None:
        # Parse succeeded but the version field disappeared.  This is
        # also a positive tampering signal — the orchestrator just
        # wrote the version field, so its absence at HEAD means a hook
        # stripped it.
        raise PostConditionViolated(
            FailureReason.POSTCOND_VERSION_NOT_BUMPED,
            detail=(
                f"HEAD's committed {relative} parses but contains no "
                f"version field (looked for project.version / "
                f"tool.poetry.version / version). The orchestrator "
                f"just wrote a version field; its absence at HEAD "
                f"indicates a commit hook stripped it."
            ),
        )

    if committed_version != expected_version:
        raise PostConditionViolated(
            FailureReason.POSTCOND_VERSION_NOT_BUMPED,
            detail=(
                f"HEAD's committed {relative} has version "
                f"'{committed_version}' but working tree has "
                f"'{expected_version}' — a commit hook may have rewritten "
                f"the version between the bump write and commit finalization. "
                f"Investigate hooks under .git/hooks/ and any pre-commit framework."
            ),
        )
    logger.debug(
        "Commit-tree check OK: HEAD:%s also at version %s",
        relative, expected_version,
    )


def check_all(
    project_root: Path,
    branch: str,
    *,
    already_ancestor: bool = False,
    expected_version: Optional[str] = None,
    version_file: Optional[Path] = None,
    min_parents: int = 2,
    allow_fixup_parent: bool = False,
    max_fixup_depth: int = 1,
    timeout: int = 30,
) -> None:
    """Run all applicable post-condition checks for a branch.

    This is the convenience entry-point that orchestrator success
    paths SHOULD call.

    Args:
        project_root: Path to the project root.
        branch: The branch that was just merged.
        already_ancestor: If ``True``, skip the merge-commit check
            because no merge commit was produced (no-op).
        expected_version: If provided, assert the version file
            contains this version.
        version_file: Path to the version file (default:
            ``pyproject.toml``).
        min_parents: Minimum parent count for HEAD merge-commit check.
        allow_fixup_parent: When ``True``, accept an ancestor of HEAD
            as the merge commit (fix-up layout produced by guardrail
            repair, optionally stacked under a non-amend version-bump
            commit).  Default ``False``.
        max_fixup_depth: When ``allow_fixup_parent=True``, walk back
            up to this many parents looking for a merge commit.
        timeout: Subprocess timeout in seconds.

    Raises:
        PostConditionViolated: If any check fails.
    """
    assert_branch_merged(project_root, branch, timeout=timeout)
    if not already_ancestor:
        assert_head_is_merge_commit(
            project_root,
            branch,
            min_parents=min_parents,
            allow_fixup_parent=allow_fixup_parent,
            max_fixup_depth=max_fixup_depth,
            timeout=timeout,
        )
    if expected_version is not None:
        assert_version_bumped(
            project_root,
            expected_version,
            version_file=version_file,
            timeout=timeout,
        )
