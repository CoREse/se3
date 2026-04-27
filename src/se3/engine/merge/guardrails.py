"""MergeGuardrailsCheck — Check spec file integrity after a merge.

Runs guardrails on any spec files (se3/specs/**/spec.md) that changed
during the merge. Detects deleted requirements, weakened language,
and weakened quantifiers.

Also exposes ``check_spec_diff()`` as a reusable pure function so the
CLI ``se3 guardrails`` command can share the same logic.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..worktree import _run_git

logger = logging.getLogger(__name__)

_SPEC_PATH_RE = re.compile(r"^se3/specs/.+/spec\.md$")

_WEAKEN_PATTERNS = [
    (r'\bMUST\b', r'\b(SHOULD|MAY)\b', "MUST weakened to SHOULD/MAY"),
    (r'\bSHALL\b', r'\b(SHOULD|MAY)\b', "SHALL weakened to SHOULD/MAY"),
    (r'\bREQUIRED\b', r'\b(RECOMMENDED|OPTIONAL)\b', "REQUIRED weakened to RECOMMENDED/OPTIONAL"),
]

_QUANTIFIER_PATTERNS = [
    (r'\ball\b', r'\bsome\b', "quantifier weakened: all → some"),
    (r'\bevery\b', r'\bsome\b', "quantifier weakened: every → some"),
]


@dataclass
class GuardrailViolation:
    """A single guardrail violation."""

    file_path: str
    violation_type: str
    message: str


@dataclass
class GuardrailReport:
    """Result of a guardrails check."""

    passed: bool = True
    violations: list[GuardrailViolation] = field(default_factory=list)


def check_spec_diff(original_text: str, new_text: str, file_path: str = "") -> list[GuardrailViolation]:
    """Check for guardrail violations between two versions of a spec file.

    Args:
        original_text: The original spec content.
        new_text: The modified spec content.
        file_path: Optional path for violation reporting.

    Returns:
        List of GuardrailViolation objects. Empty list means no violations.
    """
    violations: list[GuardrailViolation] = []
    orig_lines = original_text.splitlines()
    new_lines = new_text.splitlines()

    # Check for weakened language patterns.
    # Two-layer detection:
    #   1. Occurrence counting: if total strong occurrences decrease and weak
    #      occurrences appear, it's a weakening.
    #   2. Line-content fallback: if a specific strong line disappears and weak
    #      lines appear (net-zero count corner case — e.g. one SHALL→SHOULD
    #      plus a brand-new SHALL elsewhere), still flag it.
    for strong, weak, message in _WEAKEN_PATTERNS:
        strong_orig = sum(len(re.findall(strong, line)) for line in orig_lines)
        strong_new = sum(len(re.findall(strong, line)) for line in new_lines)
        weak_new = sum(len(re.findall(weak, line)) for line in new_lines)
        detected = False
        if strong_orig > 0 and strong_new < strong_orig and weak_new > 0:
            detected = True
        elif strong_orig > 0 and weak_new > 0:
            # Corner case: net-zero count but a specific line was weakened.
            # (e.g. one SHALL→SHOULD and a brand-new SHALL elsewhere).
            # To avoid false positives when a strong line was merely extended
            # ("SHALL A" → "SHALL A. SHOULD B"), we only flag when a strong
            # line disappeared AND a weak-only line (weak without strong) appeared.
            orig_strong_lines = [
                line.strip() for line in orig_lines if re.search(strong, line)
            ]
            new_strong_lines = [
                line.strip() for line in new_lines if re.search(strong, line)
            ]
            missing_strong = [
                s for s in orig_strong_lines if s not in new_strong_lines
            ]
            if missing_strong:
                weak_only_lines = [
                    line.strip() for line in new_lines
                    if re.search(weak, line) and not re.search(strong, line)
                ]
                if weak_only_lines:
                    detected = True
        if detected:
            violations.append(GuardrailViolation(
                file_path=file_path,
                violation_type="WEAKENING",
                message=message,
            ))

    # Check for weakened quantifiers (same two-layer approach)
    for strong, weak, message in _QUANTIFIER_PATTERNS:
        strong_orig = sum(len(re.findall(strong, line)) for line in orig_lines)
        strong_new = sum(len(re.findall(strong, line)) for line in new_lines)
        weak_new = sum(len(re.findall(weak, line)) for line in new_lines)
        detected = False
        if strong_orig > 0 and strong_new < strong_orig and weak_new > 0:
            detected = True
        elif strong_orig > 0 and weak_new > 0:
            # Same weak-only-line guard as above for quantifiers.
            orig_strong_lines = [
                line.strip() for line in orig_lines if re.search(strong, line)
            ]
            new_strong_lines = [
                line.strip() for line in new_lines if re.search(strong, line)
            ]
            missing_strong = [
                s for s in orig_strong_lines if s not in new_strong_lines
            ]
            if missing_strong:
                weak_only_lines = [
                    line.strip() for line in new_lines
                    if re.search(weak, line) and not re.search(strong, line)
                ]
                if weak_only_lines:
                    detected = True
        if detected:
            violations.append(GuardrailViolation(
                file_path=file_path,
                violation_type="WEAKENING",
                message=message,
            ))

    # Check for deleted scenarios (WHEN clauses).
    # Compare by line content rather than net count so that deleting one
    # WHEN while adding an unrelated WHEN is still detected.
    orig_when_lines = [line.strip() for line in orig_lines if re.search(r'\bWHEN\b', line)]
    new_when_lines = [line.strip() for line in new_lines if re.search(r'\bWHEN\b', line)]
    missing_when = [w for w in orig_when_lines if w not in new_when_lines]
    if missing_when:
        violations.append(GuardrailViolation(
            file_path=file_path,
            violation_type="DELETE",
            message=f"Scenarios deleted: {len(missing_when)} WHEN clause(s) removed",
        ))

    return violations


def _is_spec_path(path: str) -> bool:
    """Return True when path matches se3/specs/**/spec.md."""
    normalized = path.replace("\\", "/")
    return bool(_SPEC_PATH_RE.match(normalized))


def _get_changed_spec_files(project_root: Path, base_ref: str, head_ref: str) -> list[str]:
    """Get list of spec files changed between base_ref and head_ref."""
    result = _run_git(
        project_root, "diff", "--name-only", f"{base_ref}..{head_ref}",
        check=False, timeout=30,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    changed = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return [p for p in changed if _is_spec_path(p)]


def _read_file_from_ref(project_root: Path, rel_path: str, ref: str) -> str | None:
    """Read file content from a git ref. Returns None if unavailable."""
    result = _run_git(
        project_root, "show", f"{ref}:{rel_path}",
        check=False, timeout=15,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def _check_spec_file_against_ref(
    project_root: Path,
    rel_path: str,
    old_ref: str,
    new_ref: str,
) -> list[GuardrailViolation]:
    """Check a single spec file comparing two git refs.

    Args:
        project_root: Path to the project root.
        rel_path: Relative path to the spec file.
        old_ref: Git ref for the original version.
        new_ref: Git ref for the new version (or "WORKTREE" for working tree).

    Returns:
        List of guardrail violations.
    """
    # Get original content
    original_content = _read_file_from_ref(project_root, rel_path, old_ref)
    if original_content is None:
        # File didn't exist in old ref — no guardrail to enforce
        return []

    # Get new content
    if new_ref == "WORKTREE":
        full_path = project_root / rel_path
        if not full_path.exists():
            return [GuardrailViolation(
                file_path=rel_path,
                violation_type="DELETE",
                message="Spec file was deleted in merge",
            )]
        try:
            new_content = full_path.read_text(encoding="utf-8")
        except Exception as exc:
            logger.warning("Could not read spec file %s: %s", rel_path, exc)
            return []
    else:
        new_content = _read_file_from_ref(project_root, rel_path, new_ref)
        if new_content is None:
            # File was deleted in new ref
            return [GuardrailViolation(
                file_path=rel_path,
                violation_type="DELETE",
                message="Spec file was deleted in merge",
            )]

    return check_spec_diff(original_content, new_content, file_path=rel_path)


class MergeGuardrailsCheck:
    """Check merged spec files against guardrails."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root

    def check(
        self,
        ours_before_sha: str,
        merge_commit_sha: str,
    ) -> GuardrailReport:
        """Check spec files changed in the merge for guardrail violations.

        .. deprecated::
            Use :meth:`check_merge_result` instead for proper ref-based
            comparison. This method is kept for backward compatibility
            and compares against the working tree.

        Args:
            ours_before_sha: The SHA of HEAD before the merge started.
            merge_commit_sha: The SHA of the merge commit (or current HEAD).

        Returns:
            GuardrailReport with pass/fail status and any violations.
        """
        return self.check_merge_result(ours_before_sha, merge_commit_sha)

    def check_merge_result(
        self,
        ours_before_sha: str,
        merge_commit_sha: str,
    ) -> GuardrailReport:
        """Check spec files changed between two commits for violations.

        Lists the merge commit's touched ``se3/specs/**/spec.md`` files,
        fetches the pre-merge HEAD version and the merge-commit version,
        and runs :func:`check_spec_diff` on each.

        Args:
            ours_before_sha: The SHA of HEAD before the merge started.
            merge_commit_sha: The SHA of the merge commit.

        Returns:
            GuardrailReport with pass/fail status and any violations.
        """
        spec_files = _get_changed_spec_files(
            self.project_root, ours_before_sha, merge_commit_sha,
        )
        if not spec_files:
            return GuardrailReport(passed=True)

        violations: list[GuardrailViolation] = []
        for rel_path in spec_files:
            file_violations = _check_spec_file_against_ref(
                self.project_root, rel_path, ours_before_sha, merge_commit_sha,
            )
            violations.extend(file_violations)

        return GuardrailReport(
            passed=len(violations) == 0,
            violations=violations,
        )
