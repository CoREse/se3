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

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .failure_reason import FailureReason

logger = logging.getLogger(__name__)


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
    timeout: int = 30,
) -> None:
    """Assert that *branch* is an ancestor of HEAD.

    Uses ``git merge-base --is-ancestor`` which exits 0 when the
    branch is reachable from HEAD.

    Args:
        project_root: Path to the git repository.
        branch: Branch name to check.
        timeout: Subprocess timeout in seconds.

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
        raise PostConditionViolated(
            FailureReason.POSTCOND_BRANCH_NOT_MERGED,
            branch=branch,
            detail=(
                f"git merge-base --is-ancestor {branch} HEAD returned "
                f"{result.returncode}; branch is not an ancestor of HEAD"
            ),
        )
    logger.debug("Post-condition OK: %s is ancestor of HEAD", branch)


def assert_head_is_merge_commit(
    project_root: Path,
    branch: str,
    *,
    min_parents: int = 2,
    timeout: int = 30,
) -> None:
    """Assert that HEAD is a merge commit with at least *min_parents*.

    This is a stronger check than ``assert_branch_merged``: even if
    the branch is an ancestor, we want to confirm that the LAST
    commit on HEAD is actually a merge (not e.g. a fast-forward or
    an unrelated commit pushed after the merge).

    Octopus merges are supported: ``min_parents`` defaults to 2 but
    can be set higher if the strategy requires it.

    Args:
        project_root: Path to the git repository.
        branch: Branch name (for diagnostic messages only).
        min_parents: Minimum number of parents HEAD must have.
        timeout: Subprocess timeout in seconds.

    Raises:
        PostConditionViolated: If HEAD has fewer than *min_parents*
            parents.
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

    # Count parents via HEAD^N existence test.
    parent_count = 0
    n = 1
    while True:
        result = _run_git(
            project_root,
            "rev-parse",
            f"--verify",
            f"HEAD^{n}",
            timeout=timeout,
        )
        if result.returncode != 0:
            break
        parent_count += 1
        n += 1
        # Safety cap: octopus merges with >64 parents are pathological.
        if n > 64:
            break

    if parent_count < min_parents:
        raise PostConditionViolated(
            FailureReason.POSTCOND_HEAD_NOT_MERGE_COMMIT,
            branch=branch,
            detail=(
                f"HEAD ({head_sha[:8]}) has {parent_count} parent(s), "
                f"expected >= {min_parents}"
            ),
        )
    logger.debug(
        "Post-condition OK: HEAD (%s) has %s parent(s)",
        head_sha[:8],
        parent_count,
    )


def assert_version_bumped(
    project_root: Path,
    expected_version: str,
    *,
    version_file: Optional[Path] = None,
    timeout: int = 30,
) -> None:
    """Assert that the version file contains *expected_version*.

    This is the final post-condition check after version aggregation:
    the version file on disk must reflect the bump we just applied.

    Args:
        project_root: Path to the project root.
        expected_version: The version string that should be present.
        version_file: Path to the version file (if ``None``, defaults
            to ``pyproject.toml`` in the project root).
        timeout: Subprocess timeout for any git commands.

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
        content = version_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise PostConditionViolated(
            FailureReason.POSTCOND_VERSION_NOT_BUMPED,
            detail=f"Cannot read version file {version_file}: {exc}",
        )

    # Check for quoted version (TOML / JSON) or bare line (plain
    # VERSION file).  The bare-line check prevents false positives
    # like "1.2" matching "11.23" by requiring newline boundaries.
    quoted = f'"{expected_version}"' in content or f"'{expected_version}'" in content
    bare_line = f"\n{expected_version}\n" in content or content.startswith(f"{expected_version}\n") or content.strip() == expected_version
    if not quoted and not bare_line:
        raise PostConditionViolated(
            FailureReason.POSTCOND_VERSION_NOT_BUMPED,
            detail=(
                f"Version file {version_file} does not contain "
                f"expected version '{expected_version}'"
            ),
        )
    logger.debug(
        "Post-condition OK: version file contains %s", expected_version
    )


def check_all(
    project_root: Path,
    branch: str,
    *,
    already_ancestor: bool = False,
    expected_version: Optional[str] = None,
    version_file: Optional[Path] = None,
    min_parents: int = 2,
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
        timeout: Subprocess timeout in seconds.

    Raises:
        PostConditionViolated: If any check fails.
    """
    assert_branch_merged(project_root, branch, timeout=timeout)
    if not already_ancestor:
        assert_head_is_merge_commit(
            project_root, branch, min_parents=min_parents, timeout=timeout
        )
    if expected_version is not None:
        assert_version_bumped(
            project_root,
            expected_version,
            version_file=version_file,
            timeout=timeout,
        )
