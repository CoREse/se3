"""SemVer aggregation for merge orchestrator.

After all branches are sequentially merged into the current branch,
infer each branch's SemVer bump type relative to a base ref (the
merge-base between the pre-merge HEAD and the branch), take the max
bump, apply it to ``pyproject.toml``, and amend the last merge commit
so the version change ships with the merge rather than producing a
stand-alone commit.
"""

from __future__ import annotations

import logging
import re
import subprocess
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


_VERSION_FIELD_RE = re.compile(r'(?m)^\s*version\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)


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
    """Outcome of ``aggregate_and_apply``."""

    success: bool = False
    pre_version: Optional[str] = None
    new_version: Optional[str] = None
    bump_type: Optional[BumpType] = None
    error: Optional[str] = None


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
    """
    result = AggregateResult(pre_version=pre_merge_version)

    if not bumps:
        result.error = "no bumps to aggregate"
        return result

    try:
        base = Version.parse(pre_merge_version)
    except ValueError as exc:
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

    # Check if the current on-disk version already matches the target.
    # This avoids amending HEAD with no actual diff (e.g. retry after
    # partial failure where a prior merge already bumped the version).
    try:
        current_content = pyproject.read_text(encoding="utf-8")
    except Exception as exc:
        result.error = f"failed to read pyproject.toml: {exc}"
        return result

    current_version = _parse_pyproject_version(current_content)
    if current_version:
        try:
            current_v = Version.parse(current_version)
            if current_v >= new_version:
                if current_v == new_version:
                    logger.info(
                        "Version aggregation skipped: current version %s already "
                        "matches target",
                        current_version,
                    )
                else:
                    logger.info(
                        "Version aggregation skipped: current version %s is higher "
                        "than aggregated target %s — possible manual bump or "
                        "anomalous state",
                        current_version,
                        new_version,
                    )
                result.success = True
                result.new_version = current_version
                return result
        except ValueError:
            pass  # Unparseable current version — fall through to write

    # Preserve original content for rollback on amend failure
    original_content = current_content

    handler = TomlVersionHandler()
    try:
        handler.write_version(pyproject, str(new_version))
    except Exception as exc:
        result.error = f"failed to write pyproject.toml: {exc}"
        return result

    # Helper to restore pyproject.toml content on failure.
    # Wrapped in try/except so a restore failure does not mask the original
    # error and does not leave the file in a partially-written state.
    def _restore_content():
        try:
            pyproject.write_text(original_content, encoding="utf-8")
        except Exception as restore_exc:
            return f"pyproject.toml restore also failed: {restore_exc}"
        return None

    try:
        add_result = _run_git(
            project_root, "add", "pyproject.toml",
            check=False, timeout=15,
        )
    except Exception as exc:
        restore_err = _restore_content()
        result.error = f"git add failed: {exc}"
        if restore_err:
            result.error += f". {restore_err}"
        return result

    if add_result.returncode != 0:
        restore_err = _restore_content()
        result.error = f"git add failed: {add_result.stderr.strip()}"
        if restore_err:
            result.error += f". {restore_err}"
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
    except Exception as exc:
        restore_err = _restore_content()
        # Attempt to unstage even on exception
        try:
            _run_git(
                project_root, "reset", "HEAD", "pyproject.toml",
                check=False, timeout=15,
            )
        except Exception:
            pass
        verb = "amend" if amend else "commit"
        result.error = f"git {verb} failed: {exc}"
        if restore_err:
            result.error += f". {restore_err}"
        return result

    if commit_result.returncode != 0:
        restore_err = _restore_content()
        reset_result = _run_git(
            project_root, "reset", "HEAD", "pyproject.toml",
            check=False, timeout=15,
        )
        verb = "amend" if amend else "commit"
        if reset_result.returncode != 0:
            result.error = (
                f"git {verb} failed: {commit_result.stderr.strip()}. "
                f"Rollback also failed (git reset HEAD): {reset_result.stderr.strip()}. "
            )
        else:
            result.error = f"git {verb} failed: {commit_result.stderr.strip()}"
        if restore_err:
            result.error += f" {restore_err}"
        if reset_result.returncode != 0:
            result.error += " Working tree/index may be in an inconsistent state."
        return result

    result.success = True
    return result
