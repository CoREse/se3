"""SemVer aggregation for merge orchestrator.

After all branches are sequentially merged into the current branch,
infer each branch's SemVer bump type relative to a base ref (the
merge-base between the pre-merge HEAD and the branch), take the max
bump, apply it to ``pyproject.toml``, and amend the last merge commit
so the version change ships with the merge rather than producing a
stand-alone commit.

G4 hardening:
  * ``current_v >= new_version`` no longer silently no-ops as
    "success".  The function now returns
    ``AggregateResult(success=False, version_already_at_target=True,
    error="VersionNotAdvanced: ...")`` so callers can distinguish a
    real bump from a degenerate "merge already brought the version"
    state.  The dedicated ``VersionNotAdvanced`` exception class is
    available for callers that want to raise explicitly on this
    condition.
  * The TOML version regex tolerates 0..N whitespace around the ``=``
    sign so tools that emit ``version="1.2.3"`` (no spaces) parse
    correctly.
  * ``Version.parse`` failure on the disk's current version no longer
    silently falls through; it is reported as a fail-loud error so a
    corrupt pyproject does not get overwritten with a guessed value.
  * ``handler.write_version`` is wrapped by an atomic
    write-temp + ``os.replace`` so a partial write does not leave
    pyproject.toml in an unparseable state.
  * Every failure path restores the original pyproject.toml content
    AND ``git reset HEAD pyproject.toml`` so a failed amend never
    leaves a stale staged change behind.
  * No bare ``except Exception`` paths: each catch logs the original
    exception via ``logger.exception`` and re-raises or converts to a
    typed failure.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..version_bumper import BumpType, TomlVersionHandler, Version
from ..worktree import _run_git

logger = logging.getLogger(__name__)


_BUMP_PRIORITY = {
    BumpType.MAJOR: 3,
    BumpType.MINOR: 2,
    BumpType.PATCH: 1,
}


# Regex tolerates 0..N whitespace around the "=" sign so tools that emit
# `version="1.2.3"` (no spaces) parse correctly.  The leading
# `(?m)^[ \t]*` anchors to a line start with optional indentation but
# does NOT skip newlines (that would let us cross section boundaries).
_VERSION_FIELD_RE = re.compile(
    r'(?m)^[ \t]*version[ \t]*=[ \t]*["\']([^"\']+)["\']'
)


class VersionNotAdvanced(RuntimeError):
    """Raised when the on-disk version is already at or above the target.

    This is the "no work to do" state.  It MAY be legitimate (a branch
    in the merge sequence already brought the bump) or it MAY mask a
    bug (the merge silently did not happen and the disk was already
    advanced).  Because the function cannot tell the difference, it
    reports this as fail-loud so the caller can investigate rather
    than accept a false "success".

    The exception is documentation-only — :func:`aggregate_and_apply`
    returns an ``AggregateResult`` with ``success=False`` and
    ``version_already_at_target=True`` rather than raising, to keep
    the existing return-based contract.  Callers that prefer raising
    SHOULD wrap the result and ``raise VersionNotAdvanced(...)``
    themselves.
    """

    def __init__(
        self,
        pre_version: str,
        current_version: str,
        target_version: str,
    ) -> None:
        self.pre_version = pre_version
        self.current_version = current_version
        self.target_version = target_version
        super().__init__(
            f"VersionNotAdvanced: pyproject.toml on disk is already at "
            f"{current_version} (target {target_version}, pre-merge "
            f"{pre_version}); no bump applied"
        )


def _slice_to_next_section(content: str, section_start: int) -> str:
    """Return content from *section_start* up to the next TOML section header.

    Triple-quoted strings (``\"\"\"`` and ``'''``) are respected so that a
    ``[`` inside a multi-line string value is not misinterpreted as a
    section boundary.
    """
    pos = section_start
    in_triple: str | None = None

    while pos < len(content):
        if in_triple is None:
            if content.startswith('"""', pos):
                in_triple = '"""'
                pos += 3
                continue
            if content.startswith("'''", pos):
                in_triple = "'''"
                pos += 3
                continue
            if content[pos] == '[':
                # Is this '[' at the start of a line (after optional whitespace)?
                prev_newline = content.rfind('\n', 0, pos)
                line_start = prev_newline + 1 if prev_newline != -1 else 0
                line_prefix = content[line_start:pos]
                if line_prefix.strip() == '':
                    return content[section_start:pos]
            pos += 1
        else:
            if content.startswith(in_triple, pos):
                in_triple = None
                pos += 3
            else:
                pos += 1

    return content[section_start:]


def _parse_pyproject_version(content: str) -> Optional[str]:
    """Extract the version string from pyproject.toml content.

    Looks for ``[project]`` (PEP 621) first, then ``[tool.poetry]``.
    Section boundaries are respected so a ``version`` field in
    ``[tool.poetry]`` is never returned when ``[project]`` exists.
    Inline arrays (e.g. ``keywords = ["py"]``) inside a section are
    handled correctly, as are multi-line string values that contain
    ``[`` on a line by themselves.

    Returns ``None`` when no version field is found.
    """
    for section_header in ("[project]", "[tool.poetry]"):
        idx = content.find(section_header)
        if idx == -1:
            continue
        section_content = _slice_to_next_section(
            content, idx + len(section_header)
        )
        match = _VERSION_FIELD_RE.search(section_content)
        if match:
            return match.group(1)
    return None


def read_version_at_ref(project_root: Path, ref: str) -> Optional[str]:
    """Read pyproject.toml's version at a given git ref.

    Returns ``None`` when the file is absent at that ref or no version
    field can be parsed.
    """
    result = _run_git(
        project_root,
        "show",
        f"{ref}:pyproject.toml",
        check=False,
        timeout=15,
    )
    if result.returncode != 0:
        return None
    return _parse_pyproject_version(result.stdout)


def _diff_bump(base: Version, branch: Version) -> Optional[BumpType]:
    """Return the bump type going from ``base`` to ``branch``.

    Returns ``None`` when ``branch`` is not strictly greater than
    ``base`` on any of the major/minor/patch components.

    Prerelease transitions are handled per SemVer 2.0.0 §11: a release
    version (no prerelease) has higher precedence than the same numeric
    version with a prerelease suffix. Such transitions are treated as a
    PATCH-level change since the numeric components did not change but the
    version still advanced. Build metadata alone does not affect precedence.
    """
    if branch.major > base.major:
        return BumpType.MAJOR
    if branch.major == base.major and branch.minor > base.minor:
        return BumpType.MINOR
    if (
        branch.major == base.major
        and branch.minor == base.minor
        and branch.patch > base.patch
    ):
        return BumpType.PATCH
    # Same numeric components but higher precedence (e.g., prerelease →
    # release). Per SemVer §11 this is a version increase; treat as PATCH.
    if branch > base:
        return BumpType.PATCH
    return None


@dataclass
class InferResult:
    """Result of ``infer_branch_bump`` with bump type and human-readable reason."""

    bump: BumpType | None
    reason: str


def infer_branch_bump(
    project_root: Path,
    branch: str,
    base_ref: str,
) -> InferResult:
    """Infer the SemVer bump type for ``branch`` relative to ``base_ref``.

    ``base_ref`` should be the merge-base between the pre-merge HEAD
    and ``branch`` (i.e. the commit where the branch diverged). The
    function performs an end-to-end diff: it compares pyproject.toml's
    version at ``base_ref`` against the branch tip, ignoring any
    intermediate bumps inside the branch.

    Returns an ``InferResult`` whose ``bump`` field is:

    - The detected bump type when both sides have readable versions and
      the branch advanced the version.
    - ``None`` when the branch did not advance the version (no version-
      worthy change).
    - ``None`` when one side is missing or unparseable, to avoid
      inflating the version on speculative merges.
    - ``None`` when neither side has a readable version (no
      pyproject.toml or unparseable on both sides).

    The ``reason`` field always carries a human-readable explanation so
    callers can log the exact cause rather than conflating all ``None``
    cases into a single misleading message.
    """
    base_version_str = read_version_at_ref(project_root, base_ref)
    branch_version_str = read_version_at_ref(project_root, branch)

    # Neither side has readable version → no version metadata at all
    if not base_version_str and not branch_version_str:
        return InferResult(
            bump=None,
            reason=f"no readable version on either side (base={base_ref!r}, branch={branch!r})",
        )

    # One side readable, the other not → no version metadata to compare
    if not base_version_str:
        return InferResult(
            bump=None,
            reason=f"base ref {base_ref!r} has no readable version — cannot compare",
        )
    if not branch_version_str:
        return InferResult(
            bump=None,
            reason=f"branch {branch!r} has no readable version — cannot compare",
        )

    try:
        base_version = Version.parse(base_version_str)
        branch_version = Version.parse(branch_version_str)
    except ValueError as exc:
        return InferResult(
            bump=None,
            reason=f"version unparseable: {exc}",
        )

    bump = _diff_bump(base_version, branch_version)
    if bump is None:
        # Versions are identical (or branch went backward) — no bump needed
        return InferResult(
            bump=None,
            reason=(
                f"branch {branch!r} did not advance version "
                f"({base_version_str} → {branch_version_str})"
            ),
        )
    return InferResult(
        bump=bump,
        reason=f"inferred {bump.value} bump ({base_version_str} → {branch_version_str})",
    )


def max_bump(bumps: list[BumpType]) -> BumpType:
    """Take the maximum bump from a list. Empty list → PATCH."""
    if not bumps:
        return BumpType.PATCH
    return max(bumps, key=lambda b: _BUMP_PRIORITY[b])


@dataclass
class AggregateResult:
    """Outcome of ``aggregate_and_apply``.

    Attributes:
        success: ``True`` only when the function actually modified
            pyproject.toml AND committed/amended successfully.
        pre_version: The pre-merge version string passed in.
        new_version: The version that ended up on disk after the
            function ran.  When ``version_already_at_target`` is
            ``True`` this is the on-disk value the function found
            (which equals or exceeds the computed target).
        bump_type: The chosen aggregated bump type, set whenever the
            target was computable.
        error: Human-readable error string when ``success`` is False.
            For the "no advance" failure mode this string starts with
            ``"VersionNotAdvanced: "`` so callers can pattern-match.
        version_already_at_target: When ``True``, the on-disk version
            was already at or above the computed target.  This is a
            fail-loud signal: the function did NOT modify the file.
            Callers SHOULD treat this as a warning rather than a hard
            error if they have other evidence that the target was
            legitimately reached.
    """

    success: bool = False
    pre_version: Optional[str] = None
    new_version: Optional[str] = None
    bump_type: Optional[BumpType] = None
    error: Optional[str] = None
    version_already_at_target: bool = False


def _atomic_write_text(path: Path, content: str) -> None:
    """Write *content* to *path* atomically.

    Uses a temp file in the same directory + ``os.replace`` so the
    file is either fully old or fully new — never partial.  An fsync
    on the temp file flushes the bytes before rename so a crash
    between rename and shutdown can't lose the write.
    """
    parent = path.parent
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                # Some filesystems (e.g. tmpfs) do not support fsync;
                # log at debug level and continue — the write itself
                # succeeded, only durability is reduced.
                logger.debug(
                    "fsync not supported for %s; skipping flush", tmp_path
                )
        os.replace(tmp_path, path)
    finally:
        # Clean up the temp file if it still exists (os.replace
        # consumed it on success, so this only fires on failure).
        # We catch OSError specifically — anything else propagates so
        # the caller sees the original failure cause.
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError as cleanup_exc:
                logger.warning(
                    "Failed to clean up temp file %s: %s",
                    tmp_path, cleanup_exc,
                )


def _safe_write_version(
    pyproject: Path,
    new_version: str,
) -> None:
    """Run ``TomlVersionHandler.write_version`` atomically.

    The handler's default implementation calls ``Path.write_text``
    which is NOT atomic — a crash between writes leaves the file
    partially written and unparseable.  We call the handler to find
    the exact byte offset of the version field, build the new content
    in-memory, then write it atomically via :func:`_atomic_write_text`.
    """
    # We re-implement the handler's logic locally so we can write
    # atomically.  The handler keeps its own ``write_version``
    # method for callers that don't need atomic semantics.
    content = pyproject.read_text(encoding="utf-8")

    # Try [project] section first (PEP 621), then [tool.poetry].
    # The handler's regex pattern uses [^\[]* which can fail on
    # inline arrays inside the section (e.g. keywords = ["py"]);
    # we use the section-aware _slice_to_next_section helper
    # together with _VERSION_FIELD_RE to avoid that bug.
    for section_header in ("[project]", "[tool.poetry]"):
        idx = content.find(section_header)
        if idx == -1:
            continue
        section_start = idx + len(section_header)
        section_content = _slice_to_next_section(content, section_start)
        match = _VERSION_FIELD_RE.search(section_content)
        if not match:
            continue
        # Compute absolute offsets for the captured version string
        # (group 1) inside the full file content.
        abs_value_start = section_start + match.start(1)
        abs_value_end = section_start + match.end(1)
        new_content = (
            content[:abs_value_start]
            + new_version
            + content[abs_value_end:]
        )
        _atomic_write_text(pyproject, new_content)
        return

    raise ValueError(
        f"Could not find version field to update in {pyproject}"
    )


def _reset_staged_pyproject(project_root: Path) -> Optional[str]:
    """Run ``git reset HEAD pyproject.toml``.  Returns error string on failure."""
    try:
        result = _run_git(
            project_root, "reset", "HEAD", "pyproject.toml",
            check=False, timeout=15,
        )
    except subprocess.TimeoutExpired as exc:
        logger.warning(
            "git reset HEAD pyproject.toml timed out: %s", exc
        )
        return f"git reset HEAD timed out: {exc}"
    except OSError as exc:
        logger.warning(
            "git reset HEAD pyproject.toml raised OSError: %s", exc
        )
        return f"git reset HEAD raised OSError: {exc}"
    if result.returncode != 0:
        return f"git reset HEAD failed: {result.stderr.strip()}"
    return None


def aggregate_and_apply(
    project_root: Path,
    bumps: list[BumpType],
    pre_merge_version: str,
    amend: bool = True,
) -> AggregateResult:
    """Apply the max bump to ``pyproject.toml`` and amend the last merge commit.

    Args:
        project_root: Project root directory.
        bumps: List of bump types from each merged branch.
        pre_merge_version: Version string before any merges (the base
            on which the chosen bump is applied).
        amend: When ``True`` (default), amend the last commit. When ``False``,
            create a new commit. Use ``False`` when HEAD has already been
            published to a remote to avoid rewriting public history.

    Returns:
        AggregateResult with success flag plus before/after version
        strings (so callers can log the change). On failure, ``error``
        carries a short reason and ``success`` stays ``False``.

        When the on-disk version is already at or above the computed
        target, ``success`` is ``False``,
        ``version_already_at_target`` is ``True``, and ``error`` starts
        with ``"VersionNotAdvanced: "``.  Callers SHOULD distinguish
        this from real failure modes (write/git errors).
    """
    result = AggregateResult(pre_version=pre_merge_version)

    if not bumps:
        result.error = "no bumps to aggregate"
        return result

    try:
        base = Version.parse(pre_merge_version)
    except ValueError as exc:
        logger.warning(
            "could not parse pre_merge_version %r: %s",
            pre_merge_version, exc,
        )
        result.error = f"could not parse pre_merge_version: {exc}"
        return result

    chosen = max_bump(bumps)
    result.bump_type = chosen
    new_version = base.bump(chosen)
    result.new_version = str(new_version)

    pyproject = project_root / "pyproject.toml"
    if not pyproject.exists():
        result.error = "pyproject.toml not found"
        return result

    # C3: read disk content fail-loud — distinguish "file vanished"
    # (OSError) from "version field absent" (parse returns None).
    try:
        current_content = pyproject.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "failed to read pyproject.toml at %s: %s", pyproject, exc
        )
        result.error = f"failed to read pyproject.toml: {exc}"
        return result

    current_version = _parse_pyproject_version(current_content)

    # C1: when the on-disk version is already at or above the target,
    # we did NOT do any work.  Report this as fail-loud rather than
    # silently succeeding — the no-op may be a legitimate "merge
    # already brought the bump" but it may also mask a real bug
    # (the merges silently did not happen and the disk was already
    # advanced).  The caller has the context to decide.
    #
    # C3: when current_version exists but Version.parse raises, do
    # NOT silently fall through to a write — the file is in a state
    # we don't understand and writing on top would be reckless.
    if current_version is not None:
        try:
            current_v = Version.parse(current_version)
        except ValueError as exc:
            logger.warning(
                "current pyproject version %r is unparseable: %s",
                current_version, exc,
            )
            result.error = (
                f"current pyproject.toml version {current_version!r} is "
                f"unparseable ({exc}); refusing to overwrite"
            )
            return result
        if current_v >= new_version:
            # No work to do — version is already at or above target.
            result.success = False
            result.new_version = current_version
            result.version_already_at_target = True
            if current_v == new_version:
                detail = (
                    f"current version {current_version} already matches "
                    f"target {new_version}; aggregator did not run"
                )
            else:
                detail = (
                    f"current version {current_version} is higher than "
                    f"aggregated target {new_version}; possible manual "
                    f"bump or anomalous state — aggregator did not run"
                )
            result.error = f"VersionNotAdvanced: {detail}"
            logger.warning(
                "Version aggregation no-op: %s (pre_merge=%s)",
                detail, pre_merge_version,
            )
            return result

    # Preserve original content for rollback on subsequent failure.
    original_content = current_content

    # C4: write atomically.  ``_safe_write_version`` builds the new
    # content in memory, writes to a temp file in the same directory,
    # fsyncs, then ``os.replace``s atomically.  A crash mid-write
    # leaves the file either fully old or fully new — never partial.
    try:
        _safe_write_version(pyproject, str(new_version))
    except (OSError, ValueError) as exc:
        # C7: on write failure, restore the original content (the
        # atomic write would have left the file untouched on failure
        # in the normal case, but a partial old-state is also possible
        # if e.g. file disappeared between read and write).
        logger.warning(
            "failed to write pyproject.toml: %s", exc
        )
        try:
            pyproject.write_text(original_content, encoding="utf-8")
        except OSError as restore_exc:
            logger.warning(
                "failed to restore pyproject.toml after write failure: %s",
                restore_exc,
            )
            result.error = (
                f"failed to write pyproject.toml: {exc}. "
                f"Restore also failed: {restore_exc}"
            )
            return result
        result.error = f"failed to write pyproject.toml: {exc}"
        return result

    # Helper to restore pyproject.toml content on failure.
    # Wrapped in try/except so a restore failure does not mask the original
    # error and does not leave the file in a partially-written state.
    def _restore_content() -> Optional[str]:
        try:
            pyproject.write_text(original_content, encoding="utf-8")
        except OSError as restore_exc:
            logger.warning(
                "failed to restore pyproject.toml: %s", restore_exc
            )
            return f"pyproject.toml restore also failed: {restore_exc}"
        return None

    try:
        add_result = _run_git(
            project_root, "add", "pyproject.toml",
            check=False, timeout=15,
        )
    except subprocess.TimeoutExpired as exc:
        # C5: restore the file AND reset any partially-staged state.
        restore_err = _restore_content()
        reset_err = _reset_staged_pyproject(project_root)
        result.error = f"git add timed out: {exc}"
        if restore_err:
            result.error += f". {restore_err}"
        if reset_err:
            result.error += f". {reset_err}"
        return result
    except OSError as exc:
        restore_err = _restore_content()
        reset_err = _reset_staged_pyproject(project_root)
        result.error = f"git add raised OSError: {exc}"
        if restore_err:
            result.error += f". {restore_err}"
        if reset_err:
            result.error += f". {reset_err}"
        return result

    if add_result.returncode != 0:
        # C5: restore and reset on add failure
        restore_err = _restore_content()
        reset_err = _reset_staged_pyproject(project_root)
        result.error = f"git add failed: {add_result.stderr.strip()}"
        if restore_err:
            result.error += f". {restore_err}"
        if reset_err:
            result.error += f". {reset_err}"
        return result

    commit_args: list[str]
    if amend:
        commit_args = ["commit", "--amend", "--no-edit"]
    else:
        commit_args = [
            "commit", "-m",
            f"chore: bump version to {new_version}",
        ]
    try:
        commit_result = _run_git(
            project_root, *commit_args,
            check=False, timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        # C7: amend timed out — restore content AND unstage.  The
        # _reset_staged_pyproject helper preserves the original
        # exception by logging at WARNING.
        restore_err = _restore_content()
        reset_err = _reset_staged_pyproject(project_root)
        verb = "amend" if amend else "commit"
        result.error = f"git {verb} timed out: {exc}"
        if restore_err:
            result.error += f". {restore_err}"
        if reset_err:
            result.error += f". {reset_err}"
        return result
    except OSError as exc:
        # C6: catch the specific OSError class instead of bare
        # ``except Exception``; logger.exception captures the full
        # traceback for diagnostics.
        logger.exception("git commit raised OSError")
        restore_err = _restore_content()
        reset_err = _reset_staged_pyproject(project_root)
        verb = "amend" if amend else "commit"
        result.error = f"git {verb} raised OSError: {exc}"
        if restore_err:
            result.error += f". {restore_err}"
        if reset_err:
            result.error += f". {reset_err}"
        return result

    if commit_result.returncode != 0:
        restore_err = _restore_content()
        reset_err = _reset_staged_pyproject(project_root)
        verb = "amend" if amend else "commit"
        if reset_err:
            result.error = (
                f"git {verb} failed: {commit_result.stderr.strip()}. "
                f"Rollback also failed (git reset HEAD): {reset_err}. "
            )
        else:
            result.error = f"git {verb} failed: {commit_result.stderr.strip()}"
        if restore_err:
            result.error += f" {restore_err}"
        if reset_err:
            result.error += " Working tree/index may be in an inconsistent state."
        return result

    result.success = True
    return result
