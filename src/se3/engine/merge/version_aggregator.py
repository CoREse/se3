"""SemVer aggregation for merge orchestrator.

After all branches are sequentially merged into the current branch,
infer each branch's SemVer bump type relative to a base SHA (the
pre-merge HEAD), take the max bump, apply it to ``pyproject.toml``,
and amend the last merge commit so the version change ships with the
merge rather than producing a stand-alone commit.
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


_SECTION_START_RE = re.compile(r'(?m)^\s*\[', re.MULTILINE)
_VERSION_FIELD_RE = re.compile(r'(?m)^\s*version\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)


def _parse_pyproject_version(content: str) -> Optional[str]:
    """Extract the version string from pyproject.toml content.

    Looks for ``[project]`` (PEP 621) first, then ``[tool.poetry]``.
    Section boundaries are respected so a ``version`` field in
    ``[tool.poetry]`` is never returned when ``[project]`` exists.
    Inline arrays (e.g. ``keywords = ["py"]``) inside a section are
    handled correctly.

    Returns ``None`` when no version field is found.
    """
    for section_header in ("[project]", "[tool.poetry]"):
        idx = content.find(section_header)
        if idx == -1:
            continue
        # Slice from section start to next section (or end of file)
        after_section = content[idx + len(section_header):]
        next_section = _SECTION_START_RE.search(after_section)
        if next_section:
            section_content = after_section[:next_section.start()]
        else:
            section_content = after_section
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
    return None


def infer_branch_bump(
    project_root: Path,
    branch: str,
    base_sha: str,
) -> BumpType | None:
    """Infer the SemVer bump type for ``branch`` relative to ``base_sha``.

    Compares pyproject.toml's version at ``base_sha`` against the
    branch tip.

    - When both sides have readable versions and the branch advanced
      the version, returns the detected bump type.
    - When at least one side has a readable version but the branch did
      not advance it, returns ``None`` (no version-worthy change).
    - When one side is missing or unparseable, returns ``None`` to
      avoid inflating the version on speculative merges.
    - When **neither** side has a readable version (no pyproject.toml
      or unparseable on both sides), returns ``None``. The orchestrator
      skips aggregation if *all* branches return ``None``.
    """
    base_version_str = read_version_at_ref(project_root, base_sha)
    branch_version_str = read_version_at_ref(project_root, branch)

    # Neither side has readable version → no version metadata at all
    if not base_version_str and not branch_version_str:
        return None

    # One side readable, the other not → no version metadata to compare
    if not base_version_str or not branch_version_str:
        return None

    try:
        base_version = Version.parse(base_version_str)
        branch_version = Version.parse(branch_version_str)
    except ValueError:
        return None

    bump = _diff_bump(base_version, branch_version)
    if bump is None:
        # Versions are identical — no bump needed
        return None
    return bump


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
) -> AggregateResult:
    """Apply the max bump to ``pyproject.toml`` and amend the last merge commit.

    Args:
        project_root: Project root directory.
        bumps: List of bump types from each merged branch.
        pre_merge_version: Version string before any merges (the base
            on which the chosen bump is applied).

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

    # Preserve original content for rollback on amend failure
    try:
        original_content = pyproject.read_text(encoding="utf-8")
    except Exception as exc:
        result.error = f"failed to read pyproject.toml: {exc}"
        return result

    handler = TomlVersionHandler()
    try:
        handler.write_version(pyproject, str(new_version))
    except Exception as exc:
        result.error = f"failed to write pyproject.toml: {exc}"
        return result

    # Helper to restore pyproject.toml content on failure
    def _restore_content():
        pyproject.write_text(original_content, encoding="utf-8")

    try:
        add_result = _run_git(
            project_root, "add", "pyproject.toml",
            check=False, timeout=15,
        )
    except (subprocess.TimeoutExpired, Exception) as exc:
        _restore_content()
        result.error = f"git add failed: {exc}"
        return result

    if add_result.returncode != 0:
        _restore_content()
        result.error = f"git add failed: {add_result.stderr.strip()}"
        return result

    try:
        amend_result = _run_git(
            project_root, "commit", "--amend", "--no-edit",
            check=False, timeout=30,
        )
    except (subprocess.TimeoutExpired, Exception) as exc:
        _restore_content()
        # Attempt to unstage even on exception
        try:
            _run_git(
                project_root, "reset", "HEAD", "pyproject.toml",
                check=False, timeout=15,
            )
        except (subprocess.TimeoutExpired, Exception):
            pass
        result.error = f"git commit --amend failed: {exc}"
        return result

    if amend_result.returncode != 0:
        _restore_content()
        reset_result = _run_git(
            project_root, "reset", "HEAD", "pyproject.toml",
            check=False, timeout=15,
        )
        if reset_result.returncode != 0:
            result.error = (
                f"git commit --amend failed: {amend_result.stderr.strip()}. "
                f"Rollback also failed (git reset HEAD): {reset_result.stderr.strip()}. "
                f"Working tree/index may be in an inconsistent state."
            )
            return result
        result.error = f"git commit --amend failed: {amend_result.stderr.strip()}"
        return result

    result.success = True
    return result
