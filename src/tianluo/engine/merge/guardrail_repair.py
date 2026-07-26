"""GuardrailRepairer — LLM-driven spec repair for fast-mode guardrail violations.

When post-merge guardrails detect violations in ``fast`` strategy, this module
constructs a repair prompt, calls the LLM, parses the corrected spec content,
writes it back to the working tree, commits the fix (preferring a fix-up
commit on top of the merge commit, with amend as a fallback), and re-runs
the guardrails check to verify the fix.

All write-back paths are restricted to ``tianluo/specs/**/spec.md``.

**Amend safety contract** (defense against the user-accident root cause A1-A4):
All amend operations MUST save ``pre_amend_sha`` before ``git commit --amend``.
If rollback is needed, ``git reset --soft <pre_amend_sha>`` is used instead of
``git reset --soft HEAD~1``.  The ``HEAD^2`` existence check confirms HEAD is
still a merge commit before amending.
"""

from __future__ import annotations
from tianluo.runtime_paths import runtime_dir

import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from ..json_extractor import extract_json_two_phase
from ..worktree import _run_git
from ...commands.merge.secret_redact import redact_text
from .guardrails import GuardrailViolation, MergeGuardrailsCheck, _is_spec_path

if TYPE_CHECKING:
    from ..llm_caller import LLMCaller

logger = logging.getLogger(__name__)

_REPAIR_SCHEMA = """{
  "files": [
    {
      "path": "relative/path/to/spec.md",
      "corrected_content": "full corrected file content with no conflict markers"
    }
  ]
}"""

_REPAIR_PROMPT_TEMPLATE = """You are a spec-file corrector. A merge commit introduced guardrail violations in spec files. Your task is to fix the violations and return the corrected spec file contents as JSON.

## Guardrail Violations Detected

{violations_text}

## Original Spec Files (before merge)

{original_specs_text}

## Merged Spec Files (after merge — these contain violations)

{merged_specs_text}

## Rules

1. You MUST restore any weakened requirements:
   - SHALL → SHOULD: change back to SHALL
   - MUST → SHOULD: change back to MUST
   - REQUIRED → RECOMMENDED/OPTIONAL: change back to REQUIRED
   - all → some: change back to all
   - every → some: change back to every
2. You MUST restore any deleted WHEN clauses (scenarios).
3. You MUST NOT invent new requirements that did not exist in the original.
4. You MUST NOT delete requirements that existed in the original.
5. Preserve all other changes from the merge that do not violate guardrails.
6. Return ONLY a valid JSON object matching this schema (no markdown fences, no prose):

{schema}

Respond with valid JSON only:"""


@dataclass
class RepairResult:
    """Result of a guardrail repair attempt."""

    success: bool = False
    repaired_files: list[str] = field(default_factory=list)
    error: Optional[str] = None
    # Track whether the repair used amend (True) or fix-up (False).
    # The orchestrator uses this to decide whether allow_fixup_parent
    # should be True: only fix-up mode creates a single-parent HEAD
    # that requires HEAD^1 fallback for the merge-commit check.
    used_amend: bool = False
    # Files the LLM listed in its ``files`` array but for which it
    # produced no ``corrected_content`` (None or empty string).  These
    # entries are skipped silently inside the repair loop so the run
    # can still benefit from the files the LLM did correct, but the
    # caller MUST be able to see them — without this signal an
    # incomplete LLM response can pass the post-repair re-check after
    # side-effect clearance and silently mask unrepaired violations.
    # Populated even when the repair otherwise reports success.
    skipped_missing_content: list[str] = field(default_factory=list)


def _is_test_environment() -> bool:
    """Return True when the process appears to be a test run.

    Checks two signals:

    * ``PYTEST_CURRENT_TEST`` — set by pytest for the lifetime of each
      test invocation.  Reliable signal that we're inside a pytest
      test body.
    * ``SE3_GUARDRAIL_REPAIR_TEST_MODE`` — explicit opt-in for
      non-pytest test harnesses (custom integration runners, etc.).

    The check is intentionally conservative: a False return causes
    ``GuardrailRepairer(test_mode=True)`` to raise loudly, so a
    production caller cannot accidentally disable the silent-merge-loss
    defenses.  Test fixtures that still need the relaxed behavior
    should set ``SE3_GUARDRAIL_REPAIR_TEST_MODE=1`` in the harness.
    """
    import os as _os

    if _os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    if _os.environ.get("SE3_GUARDRAIL_REPAIR_TEST_MODE"):
        return True
    return False


class GuardrailRepairResponseParseError(ValueError):
    """Raised when the LLM returns a response that cannot be parsed as JSON.

    This is a domain-specific subclass of :class:`ValueError` so that the
    outer :func:`repair_violations` loop can distinguish a malformed-JSON
    response (the LLM responded but produced unusable text) from an
    actual transport / infrastructure error (subprocess failure, network
    timeout, etc.).  Operators investigating repair failures get a
    distinct error string instead of the conflated ``"LLM call failed:
    <message>"`` form.
    """


class GuardrailRepairInconsistentState(RuntimeError):
    """Raised when the repairer cannot safely rollback.

    The most common trigger is ``pre_repair_sha`` being ``None``
    (e.g. ``git rev-parse HEAD`` failed before staging).  In that
    case rollback would require ``HEAD~1``, which is unsafe after
    an amend and was the root cause of the A1-A4 incident.

    The orchestrator catches this as a dedicated failure mode with
    the ``inconsistent_repair_state`` failure_reason and hard-stops
    the merge sequence so subsequent branches are never attempted.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        from ...commands.merge.failure_reason import FailureReason
        self.failure_reason = FailureReason.INCONSISTENT_REPAIR_STATE


class GuardrailRepairer:
    """Repair guardrail violations in spec files via LLM.

    Used in ``fast`` strategy when post-merge guardrails detect violations.
    The repairer sends the violation list + spec contents to the LLM, receives
    corrected file contents, writes them back, commits the fix, and re-runs
    guardrails to verify.

    Args:
        project_root: Path to the project root.
        llm_caller: Optional shared :class:`LLMCaller` instance. When provided,
            the repairer reuses it (sharing prompt cache and retry state).
            When ``None``, the repairer creates its own per-call instance
            (legacy behavior).
        test_mode: When ``True``, the iter-2 silent-merge-loss fallback is
            relaxed: scenarios where both ``post_sha`` and ``branch`` are
            unverifiable are exempt from the fallback (so unit tests that
            mock all refs do not trigger spurious silent-loss errors).
            When ``False`` (default, production), the fallback fires
            unconditionally on unverifiable post_sha so transient
            ref-storage corruption never silently masquerades as success.
            This flag MUST only be set ``True`` by trusted test fixtures.
    """

    def __init__(
        self,
        project_root: Path,
        *,
        llm_caller: Optional["LLMCaller"] = None,
        llm_trace: Optional[Any] = None,
        test_mode: bool = False,
    ) -> None:
        self.project_root = project_root
        self._llm_caller = llm_caller
        self._llm_trace = llm_trace
        if test_mode and not _is_test_environment():
            # Run-time guard: production callers MUST NOT enable
            # ``test_mode`` because it disables the silent-merge-loss
            # fallback and the HEAD^2 precondition before fix-up.  We
            # detect a "test environment" via two heuristics — the
            # ``PYTEST_CURRENT_TEST`` env var (set by pytest while a
            # test is running) and the ``SE3_GUARDRAIL_REPAIR_TEST_MODE``
            # opt-in env var (a deliberate escape hatch for non-pytest
            # test harnesses).  When neither is set and a caller still
            # passes ``test_mode=True``, raise loudly rather than
            # silently downgrade safety.
            raise RuntimeError(
                "GuardrailRepairer(test_mode=True) is only permitted in "
                "trusted test fixtures. Set PYTEST_CURRENT_TEST or "
                "SE3_GUARDRAIL_REPAIR_TEST_MODE=1 to opt in. Production "
                "callers MUST leave test_mode=False so the silent-merge-"
                "loss defenses remain active."
            )
        self._test_mode = test_mode

    def repair_violations(
        self,
        branch: str,
        pre_sha: str,
        post_sha: str,
        violations: list[GuardrailViolation],
        original_spec_contents: dict[str, str],
        merged_spec_contents: dict[str, str],
    ) -> RepairResult:
        """Attempt to repair guardrail violations via LLM.

        Args:
            branch: The branch being merged (for logging).
            pre_sha: SHA of HEAD before the merge.
            post_sha: SHA of the merge commit. Used as the safe rollback target
                when the fix-up-commit path is used; callers should refresh
                this if amend is used.
            violations: List of detected GuardrailViolation objects.
            original_spec_contents: Dict mapping spec file paths to their
                original content (from pre_sha).
            merged_spec_contents: Dict mapping spec file paths to their
                merged content (from post_sha / working tree).

        Returns:
            RepairResult: success=True if guardrails pass after repair,
            success=False with error text otherwise.
        """
        # Build violation text
        violations_text = self._format_violations(violations)

        # Build original/merged spec text
        original_specs_text = self._format_spec_contents(original_spec_contents)
        merged_specs_text = self._format_spec_contents(merged_spec_contents)

        # Build and send repair prompt
        prompt = _REPAIR_PROMPT_TEMPLATE.format(
            violations_text=violations_text,
            original_specs_text=original_specs_text,
            merged_specs_text=merged_specs_text,
            schema=_REPAIR_SCHEMA,
        )

        try:
            raw_response = self._call_llm(prompt)
        except GuardrailRepairResponseParseError as exc:
            # The LLM responded but its output could not be parsed as
            # JSON.  Surface this as a distinct diagnostic so operators
            # can tell a malformed-LLM-output failure apart from an
            # actual transport-layer failure.
            logger.warning(
                "Guardrail repair LLM response parse failed: %s", exc,
            )
            return RepairResult(
                success=False,
                error=f"LLM response parse failed: {exc}",
            )
        except Exception as exc:
            logger.warning("Guardrail repair LLM call failed: %s", exc)
            return RepairResult(
                success=False,
                error=f"LLM call failed: {exc}",
            )

        # A7 fix: distinguish None from empty string / dict
        if raw_response is None:
            return RepairResult(
                success=False,
                error="LLM returned None for guardrail repair",
            )
        if raw_response == "":
            return RepairResult(
                success=False,
                error="LLM returned empty response for guardrail repair",
            )
        # A dict with an empty files array means the LLM produced
        # nothing actionable.  The ``raw_response == {}`` arm is kept
        # for defensive consistency only — _call_llm normalises a fully
        # empty payload to ``""`` before returning, so a literal ``{}``
        # cannot reach this point on the current contract.  Document
        # the intent rather than rely on it; if ``_call_llm`` is ever
        # refactored to forward ``{}`` directly we still want a clear
        # failure here instead of a downstream KeyError on
        # ``parsed.get("files", [])``.
        if raw_response == {} or raw_response == {"files": []}:
            return RepairResult(
                success=False,
                error="LLM returned empty response (no files to repair)",
            )

        # Parse LLM response
        parsed = self._parse_response(raw_response)
        if parsed is None:
            return RepairResult(
                success=False,
                error="Failed to parse LLM repair response as JSON",
            )
        # A7 fix: distinguish parsed being {} from None
        if parsed == {}:
            return RepairResult(
                success=False,
                error="LLM repair response parsed to empty dict",
            )

        # Write corrected files back
        files_data = parsed.get("files", [])
        if not isinstance(files_data, list):
            return RepairResult(
                success=False,
                error=f"LLM repair response 'files' is not a list: {type(files_data).__name__}",
            )

        # Build the set of spec files that were actually changed in the
        # merge. The set is computed once from the input dicts because
        # every restore in this loop is followed by an immediate return —
        # the set has no opportunity to become stale within a single
        # invocation. Older comments here described per-restore refresh
        # which was misleading: if a future change introduces a path
        # where the loop continues after a restore, this set MUST be
        # re-derived inside the loop.
        allowed_paths = set(original_spec_contents.keys()) | set(merged_spec_contents.keys())

        repaired_files: list[str] = []
        skipped_missing_content: list[str] = []
        for file_data in files_data:
            if not isinstance(file_data, dict):
                continue

            path = file_data.get("path", "")
            corrected_content = file_data.get("corrected_content", "")

            if not path:
                continue

            # Skip entries without corrected content (missing or empty)
            if not corrected_content:
                skipped_missing_content.append(path)
                continue

            # Resolve full path and validate it lies inside tianluo/specs/
            full_path = (self.project_root / path).resolve()
            specs_dir = (runtime_dir(self.project_root) / "specs").resolve()
            try:
                full_path.relative_to(specs_dir)
            except ValueError:
                logger.warning(
                    "Guardrail repair attempted to write outside spec dir: %s — rejected",
                    path,
                )
                self._restore_merged_content(repaired_files, merged_spec_contents)
                return RepairResult(
                    success=False,
                    error=f"Guardrail repair attempted to write outside spec dir: {path}",
                )

            # Must also match the spec file naming pattern
            if not _is_spec_path(path):
                logger.warning(
                    "Guardrail repair attempted to write non-spec path: %s — rejected",
                    path,
                )
                self._restore_merged_content(repaired_files, merged_spec_contents)
                return RepairResult(
                    success=False,
                    error=f"Guardrail repair attempted to write non-spec path: {path}",
                )

            # Defense-in-depth: reject files not among the changed spec set.
            # An empty allowed_paths set means the caller passed no spec contents,
            # which is a bug or race condition — reject to prevent hallucinated paths.
            if not allowed_paths or path not in allowed_paths:
                logger.warning(
                    "Guardrail repair attempted to write unexpected spec file: %s — "
                    "not in the changed spec set (%s) — rejected",
                    path,
                    ", ".join(sorted(allowed_paths)),
                )
                self._restore_merged_content(repaired_files, merged_spec_contents)
                return RepairResult(
                    success=False,
                    error=(
                        f"Guardrail repair attempted to write unexpected spec file: "
                        f"{path}. Only changed spec files may be repaired."
                    ),
                )

            # Defense-in-depth: reject content that still contains conflict markers
            if "<<<<<<<" in corrected_content or ">>>>>>>" in corrected_content:
                logger.warning(
                    "Guardrail repair rejected corrected content for %s: "
                    "contains unresolved git conflict markers",
                    path,
                )
                self._restore_merged_content(repaired_files, merged_spec_contents)
                return RepairResult(
                    success=False,
                    error=(
                        f"Guardrail repair rejected corrected content for {path}: "
                        f"contains unresolved git conflict markers"
                    ),
                )

            # Write corrected content. Narrowed to OS-level / encoding errors so
            # that programming bugs (AttributeError, TypeError, NameError from a
            # future refactor) propagate instead of being masked as a "failed to
            # write" RepairResult.
            try:
                full_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.write_text(corrected_content, encoding="utf-8")
                repaired_files.append(path)
                logger.info("Guardrail repair wrote corrected spec: %s", path)
            except (OSError, UnicodeEncodeError) as exc:
                self._restore_merged_content(repaired_files, merged_spec_contents)
                return RepairResult(
                    success=False,
                    error=f"Failed to write repaired spec {path}: {exc}",
                )

        if not repaired_files:
            if skipped_missing_content:
                return RepairResult(
                    success=False,
                    error=(
                        f"LLM repair returned {len(skipped_missing_content)} file entry(s) "
                        f"without corrected_content: {', '.join(skipped_missing_content)}"
                    ),
                )
            return RepairResult(
                success=False,
                error="LLM repair returned no valid spec files to write",
            )

        # Stage, commit, and re-check — catch timeouts so the orchestrator
        # knows the failure mode precisely instead of misattributing it as a
        # guardrails-check crash.
        commit_succeeded = False
        # Track which path produced the post-repair commit so the
        # post-condition ancestry check below can pick the correct
        # reference: the fix-up path leaves *post_sha* on HEAD~1 and
        # the original merge commit is still reachable, but the amend
        # path rewrites HEAD with a sibling SHA whose parents match
        # the dangling old commit.  Without this flag the amend path
        # spuriously reports ``silent_merge_loss`` even when the
        # repair succeeded.
        used_amend = False
        # Capture the current HEAD before any commit operation so rollback
        # can target an explicit SHA instead of HEAD~1 (defect A1 fix,
        # extended to the fix-up path).
        pre_repair_result = _run_git(
            self.project_root, "rev-parse", "HEAD",
            check=False, timeout=15,
        )
        pre_repair_sha: Optional[str] = None
        if pre_repair_result.returncode == 0:
            pre_repair_sha = pre_repair_result.stdout.strip()

        try:
            # Stage repaired files
            for path in repaired_files:
                add_result = _run_git(
                    self.project_root, "add", path,
                    check=False, timeout=15,
                )
                if add_result.returncode != 0:
                    # Unstage any previously staged files before restoring
                    for staged_path in repaired_files:
                        _run_git(
                            self.project_root, "reset", "HEAD", staged_path,
                            check=False, timeout=15,
                        )
                    self._restore_merged_content(repaired_files, merged_spec_contents)
                    return RepairResult(
                        success=False,
                        error=f"Failed to stage repaired spec {path}: {add_result.stderr.strip()}",
                    )

            # --- PREFERRED PATH: fix-up commit on top of merge commit ---
            # This avoids the amend/reset footgun entirely.  It creates a
            # separate commit that fixes the spec files, leaving the original
            # merge commit intact.
            #
            # G3 fix (high): Before creating the fix-up commit, assert HEAD
            # is still a merge commit. If HEAD has drifted between the
            # merge and the repair (hook-driven rewrite, partial rollback,
            # parallel commit, etc.), the fix-up commit would silently land
            # on top of a single-parent HEAD and bypass the
            # ``_amend_preserved_merge_topology`` check below (because
            # ``used_amend=False``). The downstream
            # ``assert_head_is_merge_commit`` call from the orchestrator
            # uses ``allow_fixup_parent=True`` for the fix-up path, which
            # would also pass when HEAD~1 is a single-parent commit. By
            # checking HEAD^2 here we guarantee the fix-up commit's parent
            # really is a merge commit before we layer onto it.
            #
            # ``test_mode=True`` exempts this check so unit tests that
            # exercise the repair logic without a real merge commit on
            # HEAD (single-parent fixture repos) can still test the
            # write-back path. Production callers leave the default
            # ``False`` so the precondition is enforced.
            if not self._test_mode:
                head_parent2_check = _run_git(
                    self.project_root, "rev-parse", "--verify", "HEAD^2",
                    check=False, timeout=15,
                )
                if head_parent2_check.returncode != 0:
                    # HEAD has drifted away from being a merge commit between
                    # the merge and the repair. Refuse to layer a fix-up
                    # commit on top of it because that would silently mask a
                    # silent-merge-loss condition.
                    for path in repaired_files:
                        _run_git(
                            self.project_root, "reset", "HEAD", path,
                            check=False, timeout=15,
                        )
                    self._restore_merged_content(repaired_files, merged_spec_contents)
                    return RepairResult(
                        success=False,
                        error=(
                            f"Cannot create fix-up commit: HEAD is not a merge "
                            f"commit (HEAD^2 check failed: "
                            f"{head_parent2_check.stderr.strip()}). HEAD may "
                            f"have been rewritten between the merge and the "
                            f"guardrail repair, indicating a silent merge loss."
                        ),
                    )

            # Sanitize branch name for the commit message: git refnames
            # cannot contain single quotes per ref-naming rules, but
            # malformed/test branches that smuggle one through would
            # corrupt the audit string in the commit message.  Strip
            # single quotes, ASCII control characters, AND shell-metachars
            # like backticks and dollar signs.  The message is passed
            # through git's argv (no shell), so this is NOT a security
            # boundary, but unstripped backticks / $-signs can still
            # produce confusingly-rendered commit history when an
            # operator views the log in a terminal that interprets
            # them (some pagers / CI surfaces echo the message into
            # their own shell-like rendering).  Restrict to a
            # printable-ASCII subset that is safe to render anywhere.
            _UNSAFE_FIXUP_CHARS = "'`$\"\\\n\r\t"
            # Cap the safe-branch substring length so a pathologically
            # long branch name (e.g. crafted refs in test fixtures or
            # imported repos) does not produce a multi-KB commit
            # message that downstream tooling parsing the first line
            # may choke on.  200 chars is well above realistic
            # branch-name lengths but well below git's hard limits.
            _SAFE_BRANCH_MAX_LEN = 200
            safe_branch = "".join(
                ch for ch in branch
                if ch not in _UNSAFE_FIXUP_CHARS and ord(ch) >= 0x20
            )
            if len(safe_branch) > _SAFE_BRANCH_MAX_LEN:
                safe_branch = (
                    safe_branch[: _SAFE_BRANCH_MAX_LEN - 3] + "..."
                )
            # Defensive late re-capture: if the initial pre_repair_sha read
            # at lines 430-436 failed (transient git error, race) and we
            # commit anyway, downstream rollback paths called with
            # ``pre_repair_sha=None`` skip the reset and HEAD is left on
            # the unverified fix-up commit.  Mirror the amend path's
            # defensive re-capture (~line 588) so the fix-up path has the
            # same rollback guarantee.
            if not pre_repair_sha:
                pre_sha_fallback = _run_git(
                    self.project_root, "rev-parse", "HEAD",
                    check=False, timeout=15,
                )
                if pre_sha_fallback.returncode == 0:
                    pre_repair_sha = pre_sha_fallback.stdout.strip()
            if not pre_repair_sha:
                # Both the initial and the late re-capture failed: refuse
                # to commit because we cannot guarantee a clean rollback.
                # Unstage and restore so the repairer remains
                # self-contained.
                for path in repaired_files:
                    _run_git(
                        self.project_root, "reset", "HEAD", path,
                        check=False, timeout=15,
                    )
                self._restore_merged_content(repaired_files, merged_spec_contents)
                return RepairResult(
                    success=False,
                    error=(
                        "Refusing to create fix-up commit: pre_repair_sha "
                        "could not be captured (rev-parse HEAD failed both "
                        "before staging and immediately before commit). "
                        "Without a known-good rollback target a downstream "
                        "failure would leave HEAD on an unverified commit."
                    ),
                )

            fixup_result = _run_git(
                self.project_root,
                "commit",
                "-m",
                f"fix(specs): repair guardrail violations from '{safe_branch}'",
                check=False, timeout=30,
            )
            if fixup_result.returncode == 0:
                commit_succeeded = True
                logger.info(
                    "Created fix-up commit for '%s' with %d repaired spec file(s)",
                    branch, len(repaired_files),
                )
                # G3 fix (high): post-commit HEAD-shape assertion for the
                # fix-up path. The pre-commit HEAD^2 check above guarantees
                # we *started* on top of a merge commit, but git hooks
                # (post-commit, prepare-commit-msg) may rewrite HEAD
                # between the commit and now. Without this verification
                # the only later proof of topology preservation is
                # ``merge-base --is-ancestor post_sha HEAD`` which passes
                # whenever post_sha is reachable — even if HEAD silently
                # became a non-merge with stale parentage. Mirror the
                # amend path's post-commit assertion here so the fix-up
                # path is no longer the weaker branch of this guard.
                #
                # ``allow_fixup_parent=True`` because HEAD itself is the
                # single-parent fix-up commit; the merge commit is HEAD^1.
                # The assertion checks HEAD^1 has 2+ parents — i.e. the
                # fix-up commit really is layered on top of a merge.
                if not self._test_mode:
                    from ...commands.merge.postcondition import (
                        PostConditionViolated,
                        assert_head_is_merge_commit,
                    )
                    try:
                        assert_head_is_merge_commit(
                            self.project_root,
                            branch,
                            min_parents=2,
                            allow_fixup_parent=True,
                            timeout=15,
                        )
                    except subprocess.TimeoutExpired as t_exc:
                        try:
                            self._rollback_commit(
                                pre_repair_sha,
                                repaired_files,
                                merged_spec_contents,
                            )
                        except Exception as rb_exc:
                            logger.error(
                                "Rollback after post-fixup HEAD check "
                                "timeout also failed: %s",
                                rb_exc,
                            )
                        return RepairResult(
                            success=False,
                            error=(
                                "Post-fixup HEAD check timed out: cannot "
                                "confirm HEAD^1 is still a merge commit "
                                f"({t_exc})."
                            ),
                        )
                    except PostConditionViolated as pc_exc:
                        try:
                            self._rollback_commit(
                                pre_repair_sha,
                                repaired_files,
                                merged_spec_contents,
                            )
                        except Exception as rb_exc:
                            logger.error(
                                "Rollback after post-fixup HEAD check "
                                "failure also failed: %s",
                                rb_exc,
                            )
                        return RepairResult(
                            success=False,
                            error=(
                                "Post-fixup HEAD check failed: HEAD^1 is "
                                f"no longer a merge commit ({pc_exc}). A "
                                "post-commit hook or concurrent rewrite "
                                "may have silently lost a parent."
                            ),
                        )
            else:
                # --- FALLBACK PATH: amend the merge commit ---
                # This is the legacy path.  Before amending, assert HEAD is
                # still a merge commit, and save pre_amend_sha for safe
                # rollback.
                logger.warning(
                    "Fix-up commit failed (%s), falling back to amend: %s",
                    fixup_result.returncode,
                    fixup_result.stderr.strip() or "unknown error",
                )

                # A5: assert HEAD is still a merge commit before amending
                head_parent2 = _run_git(
                    self.project_root, "rev-parse", "--verify", "HEAD^2",
                    check=False, timeout=15,
                )
                if head_parent2.returncode != 0:
                    # HEAD is no longer a merge commit — cannot safely amend.
                    # Unstage and restore.
                    for path in repaired_files:
                        _run_git(
                            self.project_root, "reset", "HEAD", path,
                            check=False, timeout=15,
                        )
                    self._restore_merged_content(repaired_files, merged_spec_contents)
                    return RepairResult(
                        success=False,
                        error=(
                            f"Cannot amend: HEAD is not a merge commit "
                            f"(HEAD^2 check failed: {head_parent2.stderr.strip()})"
                        ),
                    )

                # pre_repair_sha was already captured before staging (line
                # 430-441); it is still valid here because no commit has
                # been created yet. The earlier code re-captured HEAD here
                # as a "defensive fallback" if the initial capture failed,
                # but that defeats its own purpose: by this point the
                # repaired files have been staged, and if a hook or
                # concurrent process advanced HEAD between the initial
                # capture and now, the late re-capture would record the
                # *post-staging* HEAD and rollback would target the wrong
                # commit. Refuse the amend instead — it is safer to fail
                # loud than to amend with an unverified rollback target.
                if not pre_repair_sha:
                    for path in repaired_files:
                        _run_git(
                            self.project_root, "reset", "HEAD", path,
                            check=False, timeout=15,
                        )
                    self._restore_merged_content(repaired_files, merged_spec_contents)
                    return RepairResult(
                        success=False,
                        error=(
                            "Refusing to amend merge commit: pre_repair_sha "
                            "was not captured before staging (rev-parse "
                            "HEAD failed at the pre-stage capture). "
                            "Without a known-good rollback target an amend "
                            "failure could leave HEAD on an unverified "
                            "commit."
                        ),
                    )

                amend_result = _run_git(
                    self.project_root,
                    "commit",
                    "--amend",
                    "--no-edit",
                    check=False, timeout=30,
                )
                if amend_result.returncode != 0:
                    # G3 fix (high): symmetric rollback to pre_repair_sha
                    # rather than just unstaging. ``commit --amend`` has
                    # been observed to mutate HEAD partially before
                    # failing on a hook (e.g. a pre-commit-msg hook that
                    # vetoes the message after git has already advanced
                    # the index). A bare ``reset HEAD <path>`` only
                    # unstages and leaves HEAD on the partially-amended
                    # commit. ``_rollback_commit`` performs a full
                    # ``reset --hard <pre_repair_sha>`` and restores the
                    # working-tree content, mirroring the post-amend
                    # HEAD-check failure path's rollback (~line 720) so
                    # the repairer's failure paths are uniformly
                    # self-contained regardless of the failure point.
                    if pre_repair_sha:
                        try:
                            self._rollback_commit(
                                pre_repair_sha,
                                repaired_files,
                                merged_spec_contents,
                            )
                        except Exception as rb_exc:
                            logger.error(
                                "Rollback after amend-failure also failed: %s",
                                rb_exc,
                            )
                    else:
                        # No rollback target — fall back to the legacy
                        # unstage-and-restore path. This preserves the
                        # original pre-amend behaviour for fixtures that
                        # never captured a SHA (rare; would only occur
                        # if both initial and late re-capture failed,
                        # which earlier guards already refuse).
                        for path in repaired_files:
                            _run_git(
                                self.project_root, "reset", "HEAD", path,
                                check=False, timeout=15,
                            )
                        self._restore_merged_content(repaired_files, merged_spec_contents)
                    return RepairResult(
                        success=False,
                        error=f"Failed to amend merge commit with repaired specs: {amend_result.stderr.strip()}",
                    )

                commit_succeeded = True
                used_amend = True
                logger.info("Amended merge commit with repaired spec files for '%s'", branch)

                # A5/G1 fix: immediately assert HEAD is still a merge commit
                # after the amend. This catches the silent-merge-loss class
                # of bug where a hook rewrote the commit, the amend dropped
                # a parent, or some other side-effect produced a non-merge
                # HEAD. Without this immediate check, the only later
                # validation is _amend_preserved_merge_topology() which
                # compares against the dangling old SHA and could pass even
                # when HEAD has lost a parent.
                #
                # ``allow_fixup_parent`` is intentionally NOT passed here:
                # the amend path (``git commit --amend``) keeps HEAD as the
                # merge commit itself with parent_count >= 2, so the
                # assertion's strict ``min_parents=2`` form is the right
                # check.  Only the fix-up path (a separate single-parent
                # commit on top of the merge) needs the
                # ``allow_fixup_parent=True`` relaxation, and that path
                # never reaches this block.  Keep this in mind if a future
                # change to the amend strategy ever creates a fix-up child
                # *during* what is conceptually an amend — the assertion
                # would silently fail and we would need to either pass
                # ``allow_fixup_parent=True`` or replace this with a
                # parent-set equality check (mirroring
                # ``_amend_preserved_merge_topology``).  See the
                # call-after-success at the end of ``repair_violations``
                # for the parent-set comparison used as the canonical
                # post-amend topology proof.
                from ...commands.merge.postcondition import (
                    PostConditionViolated,
                    assert_head_is_merge_commit,
                )
                try:
                    assert_head_is_merge_commit(
                        self.project_root,
                        branch,
                        min_parents=2,
                        timeout=15,
                    )
                except subprocess.TimeoutExpired as t_exc:
                    # Roll back to pre-repair SHA and surface the timeout as
                    # a repair failure, mirroring the fallback block below.
                    try:
                        self._rollback_commit(
                            pre_repair_sha,
                            repaired_files,
                            merged_spec_contents,
                        )
                    except Exception as rb_exc:
                        logger.error(
                            "Rollback after post-amend HEAD check timeout "
                            "also failed: %s",
                            rb_exc,
                        )
                    return RepairResult(
                        success=False,
                        error=(
                            "Post-amend HEAD check timed out: cannot confirm "
                            f"HEAD is still a merge commit ({t_exc})."
                        ),
                    )
                except PostConditionViolated as pc_exc:
                    # Roll back to pre-repair SHA and surface the silent-
                    # loss diagnostic to the caller. Wrap rollback so a
                    # failure in cleanup does not mask the original signal.
                    try:
                        self._rollback_commit(
                            pre_repair_sha,
                            repaired_files,
                            merged_spec_contents,
                        )
                    except Exception as rb_exc:
                        logger.error(
                            "Rollback after post-amend HEAD check failure "
                            "also failed: %s",
                            rb_exc,
                        )
                    return RepairResult(
                        success=False,
                        error=(
                            "Post-amend HEAD check failed: HEAD is no "
                            f"longer a merge commit ({pc_exc}). The amend "
                            "may have silently lost a parent."
                        ),
                    )

            # Re-run guardrails on the commit (fix-up or amended)
            guardrails = MergeGuardrailsCheck(self.project_root)
            rev_parse_result = _run_git(
                self.project_root, "rev-parse", "HEAD",
                check=False, timeout=15,
            )
            if rev_parse_result.returncode != 0:
                self._rollback_commit(pre_repair_sha, repaired_files, merged_spec_contents)
                return RepairResult(
                    success=False,
                    error=f"Failed to get post-repair commit SHA: {rev_parse_result.stderr.strip() or 'git rev-parse HEAD failed'}",
                )
            new_sha = rev_parse_result.stdout.strip()
            # Topology check policy:
            #   * Fix-up path (used_amend=False): HEAD is a single-parent
            #     commit on top of the merge commit, so the >=2-parents
            #     assertion would spuriously fail.  Disable enforcement.
            #   * Amend path (used_amend=True): ``git commit --amend``
            #     keeps HEAD as the merge commit (>=2 parents) so we
            #     CAN — and SHOULD — keep enforcement on.  Defense in
            #     depth: the independent ``_amend_preserved_merge_topology``
            #     check below + the orchestrator-side
            #     ``assert_head_is_merge_commit`` already cover this case,
            #     but tying enforcement to ``used_amend`` here means a
            #     future refactor that drops one of those checks does not
            #     silently lose topology enforcement on the amend path.
            gr_report = guardrails.check_merge_result(
                pre_sha, new_sha, enforce_topology=used_amend,
            )
        except subprocess.TimeoutExpired as exc:
            # Defensive: un-amend the commit before restoring working-tree
            # files so that if the process crashes before the caller's
            # rollback, HEAD won't be left on an unverified commit.
            if commit_succeeded:
                self._rollback_commit(pre_repair_sha, repaired_files, merged_spec_contents)
            else:
                self._restore_merged_content(repaired_files, merged_spec_contents)
            return RepairResult(
                success=False,
                error=f"Timeout during guardrail repair git operation: {exc}",
            )
        except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as exc:
            # G3 fix (medium): narrow from bare ``except Exception`` to
            # the operational/IO error classes that can realistically
            # arise from subprocess + filesystem + git plumbing here.
            # AssertionError, TypeError, KeyError, AttributeError, etc.
            # (programming-error classes) propagate as crashes rather
            # than being converted to a generic "guardrails re-check
            # failed" string — masking root-cause bugs as repair
            # failures violates the project's "no silent except-Exception"
            # rule.
            if commit_succeeded:
                self._rollback_commit(pre_repair_sha, repaired_files, merged_spec_contents)
            else:
                self._restore_merged_content(repaired_files, merged_spec_contents)
            return RepairResult(
                success=False,
                error=(
                    f"Guardrails re-check failed after repair "
                    f"({type(exc).__name__}): {exc}"
                ),
            )

        if not gr_report.passed:
            # Still has violations after repair — restore and report failure.
            # Reorder: (a) rollback the commit, (b) unstage repaired
            # files from the new HEAD so index and working tree stay in sync,
            # (c) restore the original merged content.  This makes the repairer
            # self-contained regardless of whether the caller performs a
            # downstream hard reset.
            if commit_succeeded:
                self._rollback_commit(pre_repair_sha, repaired_files, merged_spec_contents)
            else:
                self._restore_merged_content(repaired_files, merged_spec_contents)
            remaining = [
                f"[{v.violation_type}] {v.file_path}: {v.message}"
                for v in gr_report.violations
            ]
            return RepairResult(
                success=False,
                repaired_files=repaired_files,
                error=(
                    f"Guardrails still fail after LLM repair. "
                    f"Remaining violations ({len(gr_report.violations)}): "
                    f"{'; '.join(remaining)}"
                ),
            )

        logger.info(
            "Guardrail repair succeeded for '%s': %d file(s) corrected",
            branch, len(repaired_files),
        )

        # A11 fix / Task 11: after repair success, verify the original merge
        # commit was preserved.  The check has two flavours depending on
        # which path produced the post-repair commit:
        #
        #   - Fix-up path: a new commit was placed on top of *post_sha*,
        #     so ``post_sha`` MUST still be an ancestor of HEAD.
        #   - Amend path: ``git commit --amend`` rewrote *post_sha* into
        #     a sibling commit at HEAD with identical parents.  The old
        #     SHA is now dangling and cannot pass the ancestry check
        #     (it has the same parents as the new HEAD, not a subset).
        #     Instead, compare the parent set of the dangling old commit
        #     to the parent set of HEAD: if they match exactly, the
        #     merge topology is preserved and the repair succeeded.
        # The check is skipped when post_sha is not a valid ref (e.g.
        # tests with mocked SHAs) to avoid false failures.
        post_sha_verified = False
        if post_sha:
            verify_ref = _run_git(
                self.project_root, "rev-parse", "--verify", post_sha,
                check=False, timeout=15,
            )
            if verify_ref.returncode == 0:
                post_sha_verified = True
                if used_amend:
                    # G3 fix (high): distinguish a real topology
                    # violation (False) from a transient git failure
                    # (raised exception). Transient failures used to be
                    # silently converted to False and treated as
                    # silent-loss, which turned a flaky environment
                    # into a "merge silently rolled back" incident.
                    try:
                        topology_ok = self._amend_preserved_merge_topology(
                            post_sha
                        )
                    except subprocess.TimeoutExpired as t_exc:
                        self._rollback_commit(
                            pre_repair_sha, repaired_files, merged_spec_contents,
                        )
                        return RepairResult(
                            success=False,
                            error=(
                                "Post-repair topology check timed out: "
                                f"could not verify amend preserved merge "
                                f"topology for {post_sha[:8]} ({t_exc}). "
                                "Treating as transient failure rather "
                                "than silent loss; operator should re-run."
                            ),
                        )
                    except RuntimeError as rt_exc:
                        self._rollback_commit(
                            pre_repair_sha, repaired_files, merged_spec_contents,
                        )
                        return RepairResult(
                            success=False,
                            error=(
                                "Post-repair topology check encountered a "
                                f"transient git failure for {post_sha[:8]}: "
                                f"{rt_exc}. Treating as transient rather "
                                "than silent loss; operator should re-run."
                            ),
                        )
                    if not topology_ok:
                        self._rollback_commit(
                            pre_repair_sha, repaired_files, merged_spec_contents,
                        )
                        return RepairResult(
                            success=False,
                            error=(
                                "Post-repair post-condition failed: amend rewrote "
                                f"the merge commit ({post_sha[:8]}) but the parent "
                                "set of HEAD does not match the parent set of the "
                                "original merge commit. The merge may have been "
                                "silently lost."
                            ),
                        )
                else:
                    ancestor_check = _run_git(
                        self.project_root,
                        "merge-base", "--is-ancestor", post_sha, "HEAD",
                        check=False, timeout=15,
                    )
                    if ancestor_check.returncode != 0:
                        self._rollback_commit(pre_repair_sha, repaired_files, merged_spec_contents)
                        return RepairResult(
                            success=False,
                            error=(
                                "Post-repair post-condition failed: the original merge "
                                f"commit ({post_sha[:8]}) is no longer an ancestor of HEAD. "
                                "The merge may have been silently lost."
                            ),
                        )

        # A11 fallback: when post_sha could NOT be verified (empty,
        # unverifiable, masked by a test stub, or otherwise unusable
        # for the topology checks above), enforce a minimum invariant
        # so the repairer does not silently declare success after a
        # silent merge loss.  The fallback ONLY runs when the post_sha
        # verification path above was skipped or failed — when post_sha
        # is verified and the topology checks pass, we trust them and
        # do not double-check via this fallback (which would cause
        # false positives for legitimately non-merge HEAD states such
        # as test fixtures).
        #
        # **Conditions for firing the fallback:**
        #   - post_sha is empty (the caller failed to capture the
        #     pre-merge HEAD SHA — a strong signal that something
        #     went wrong upstream and we should not trust the working
        #     tree state), OR
        #   - post_sha is non-empty, unverifiable, AND the branch
        #     ref is also unverifiable (test scenarios that mock
        #     all refs are exempt — the fallback has no ground
        #     truth to consume).
        #
        # When post_sha is empty, the fallback fires unconditionally
        # because empty is a deliberate "could not capture" signal
        # rather than a test mock. When post_sha is non-empty but
        # unverifiable, we additionally require the branch ref to
        # be verifiable so we do not flag pure-mock unit tests as
        # silent-loss scenarios.
        #
        # The fallback enforces two invariants when fired:
        #   1. HEAD is still a merge commit (>=2 parents) for the amend
        #      path, OR HEAD^1 is a merge commit for the fix-up path.
        #   2. The branch we just merged is an ancestor of HEAD
        #      (only when branch is verifiable).
        # Either failure → silent merge loss → rollback and refuse
        # success.
        if not post_sha_verified:
            should_fire_fallback = False
            branch_verifiable = False
            if not post_sha:
                # Empty post_sha is a strong "couldn't capture" signal,
                # fire unconditionally.
                should_fire_fallback = True
            else:
                # Non-empty but unverifiable. Two distinct scenarios:
                #   (a) production ref-storage corruption — the post_sha
                #       was real when captured but the ref is gone now
                #       (transient git ref-cache failure, manual ref
                #       cleanup, repository corruption). The fallback
                #       MUST fire because this is exactly the silent-
                #       merge-loss class A11 was added to catch.
                #   (b) test-fixture mocks — the post_sha is a fake
                #       string that never existed in the repo, the
                #       branch is also a mock. The fallback would crash
                #       with no ground truth to consume, polluting test
                #       output without uncovering real bugs.
                #
                # We default to (a) — fire the fallback — and only
                # exempt (b) when the explicit ``test_mode=True`` flag
                # is set on the GuardrailRepairer instance. This
                # prevents transient ref-storage corruption on a real
                # repo from masquerading as success.
                # G3: distinguish a *timeout* (the probe didn't finish)
                # from a clean "ref does not exist" so a hung filesystem
                # does not silently downgrade to branch_verifiable=False
                # and mask a possible silent merge loss.
                branch_verifiable = self._probe_branch_verifiable(
                    branch, location="outer-probe"
                )
                if branch_verifiable:
                    # Branch is verifiable — at least one source of
                    # ground truth exists, fire the fallback.
                    should_fire_fallback = True
                elif self._test_mode:
                    # Both refs unverifiable, but test_mode opts out:
                    # this is a fixture using fully-mocked refs.
                    should_fire_fallback = False
                else:
                    # Production: both refs unverifiable on a real
                    # repository indicates ref-storage corruption.
                    # Fire the fallback so we do not silently declare
                    # success after a possible silent-merge-loss.
                    should_fire_fallback = True

            if should_fire_fallback:
                try:
                    from ...commands.merge.postcondition import (
                        PostConditionViolated,
                        assert_branch_merged,
                        assert_head_is_merge_commit,
                    )
                    assert_head_is_merge_commit(
                        self.project_root,
                        branch,
                        min_parents=2,
                        allow_fixup_parent=not used_amend,
                        timeout=15,
                    )
                    # Only check ancestry if branch is verifiable.
                    # An unverifiable branch (even when post_sha is
                    # empty and we fire the fallback for the HEAD
                    # check) cannot be tested for ancestry — the
                    # HEAD-shape check is enough.
                    if not post_sha or branch_verifiable:
                        # Re-check whether branch is verifiable so the
                        # ancestry call does not crash on a mock branch
                        # name. When post_sha is empty (above branch),
                        # we skipped the verify probe and don't know
                        # branch_verifiable yet.
                        if not branch_verifiable:
                            branch_verifiable = self._probe_branch_verifiable(
                                branch, location="fallback-reprobe"
                            )
                        if branch_verifiable:
                            assert_branch_merged(
                                self.project_root, branch, timeout=15,
                            )
                except PostConditionViolated as pc_exc:
                    try:
                        self._rollback_commit(
                            pre_repair_sha, repaired_files, merged_spec_contents,
                        )
                    except Exception as rb_exc:
                        logger.error(
                            "Rollback after post-repair fallback HEAD check "
                            "failure also failed: %s",
                            rb_exc,
                        )
                    return RepairResult(
                        success=False,
                        error=(
                            "Post-repair fallback post-condition failed: "
                            f"{pc_exc}. The merge may have been silently "
                            "lost and post_sha verification was unavailable."
                        ),
                    )
                except subprocess.TimeoutExpired as t_exc:
                    try:
                        self._rollback_commit(
                            pre_repair_sha, repaired_files, merged_spec_contents,
                        )
                    except Exception as rb_exc:
                        logger.error(
                            "Rollback after post-repair fallback HEAD check "
                            "timeout also failed: %s",
                            rb_exc,
                        )
                    return RepairResult(
                        success=False,
                        error=(
                            "Post-repair fallback post-condition timed out: "
                            f"{t_exc}. Cannot confirm HEAD is still a merge "
                            "commit; refusing to declare success."
                        ),
                    )

        # Surface a warning when the LLM omitted corrected_content for
        # at least one file but other files succeeded.  Without this
        # warning, an incomplete LLM response can produce repairs that
        # pass the re-check after side-effect clearance even though some
        # violations were never addressed — a silent partial repair.
        if skipped_missing_content:
            logger.warning(
                "Guardrail repair completed with %d skipped file(s) due "
                "to missing/empty corrected_content from LLM: %s",
                len(skipped_missing_content),
                ", ".join(skipped_missing_content),
            )

        return RepairResult(
            success=True,
            repaired_files=repaired_files,
            used_amend=used_amend,
            skipped_missing_content=list(skipped_missing_content),
        )

    def _probe_branch_verifiable(
        self, branch: str, *, location: str = "outer"
    ) -> bool:
        """Probe whether ``branch`` is a verifiable git ref.

        Returns True only when ``git rev-parse --verify`` exits 0.  A
        timeout is logged at WARNING and reported as unverifiable so a
        hung filesystem does not silently downgrade to "branch_verifiable
        = False" without leaving an audit trail.  ``location`` is folded
        into the warning so operators can tell the outer probe from the
        in-fallback re-probe at a glance.
        """
        try:
            result = _run_git(
                self.project_root,
                "rev-parse", "--verify", branch,
                check=False, timeout=15,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            # Broadened from ``TimeoutExpired`` only: a missing ``git``
            # binary (FileNotFoundError) or a broken-pipe / EIO from the
            # subprocess (OSError) would otherwise propagate into the
            # silent-loss-fallback caller and crash the repairer instead
            # of triggering the fallback.  Any of these failure modes
            # means we cannot verify the branch — treat as unverifiable
            # so the silent-merge-loss check can fire conservatively.
            logger.warning(
                "branch verify probe for %r failed (%s: %s) at %s — "
                "treating as unverifiable so the silent-merge-loss "
                "check can fire on real refs.",
                branch, type(exc).__name__, exc, location,
            )
            return False

    def _amend_preserved_merge_topology(self, old_post_sha: str) -> bool:
        """Verify the amended HEAD has the same parents as *old_post_sha*.

        ``git commit --amend`` always retains the parent set of the
        commit it rewrites, so we use parent-set equality as the
        post-condition signal: if HEAD's parents match the dangling
        old commit's parents, the merge topology was preserved.

        G3 fix (high): the original implementation returned ``False`` on
        ANY subprocess failure (including transient ``git rev-list``
        timeouts and ref-storage flakiness), which converted a slow
        filesystem during a healthy amend into a rollback. We now
        distinguish transient failures (timeouts / unexpected non-zero
        rc on what should be a stable ref query) from real topology
        violations:

          * Real mismatch (different parent set, fewer parents than
            expected): return ``False`` — caller rolls back as a silent
            loss.
          * Transient failure: raise ``subprocess.TimeoutExpired`` (for
            timeouts) or :class:`RuntimeError` (for non-timeout
            transient errors). The outer ``try/except`` in the caller
            already routes timeouts and exceptions to a distinct
            failure mode rather than misattributing them as topology
            violations.
        """
        def _parents(ref: str) -> list[str]:
            try:
                result = _run_git(
                    self.project_root,
                    "rev-list", "--parents", "-n", "1", ref,
                    check=False, timeout=15,
                )
            except subprocess.TimeoutExpired:
                # Re-raise so the caller's outer except routes this to
                # the transient-failure path rather than treating it as
                # a topology violation.
                raise
            if result.returncode != 0:
                raise RuntimeError(
                    f"git rev-list --parents -n 1 {ref!r} returned "
                    f"{result.returncode}: "
                    f"{result.stderr.strip() or '<no stderr>'}"
                )
            stdout = result.stdout.strip()
            if not stdout:
                raise RuntimeError(
                    f"git rev-list --parents -n 1 {ref!r} produced "
                    "empty stdout (ref may be unborn or corrupt)"
                )
            parts = stdout.split()
            # parts[0] is the commit itself; parts[1:] are parents.
            return parts[1:] if len(parts) > 1 else []

        old_parents = _parents(old_post_sha)
        new_parents = _parents("HEAD")
        # Defensive: amend is only valid on merge commits (>=2 parents).
        # The HEAD^2 assertion gate at the caller already enforces this,
        # but a future refactor could loosen that gate. A real parent
        # count of <2 is a topology violation, NOT a transient failure.
        if len(old_parents) < 2 or len(new_parents) < 2:
            return False
        # Order matters in git — first parent vs. second parent has
        # semantic meaning, and amend never reorders parents.
        return old_parents == new_parents

    def _format_violations(self, violations: list[GuardrailViolation]) -> str:
        """Format violation list for the repair prompt.

        Evidence access policy: ``EvidenceRecord.to_dict()`` omits keys
        whose values are ``None``, so any subkey may be absent.  Use
        per-key ``.get()`` with explicit ``None`` checks rather than
        ``in`` membership followed by indexing — the latter assumes the
        key exists with a non-None value, which is not guaranteed.
        Each conditional branch here renders independently: missing
        values are surfaced as ``"?"`` placeholders rather than
        suppressing the whole line, so the LLM still sees partial
        evidence when one half of a pair is omitted (e.g., a strong
        line is detected but the corresponding weak line is None).
        """
        lines: list[str] = []
        for i, v in enumerate(violations, 1):
            lines.append(f"{i}. [{v.violation_type}] {v.file_path}")
            lines.append(f"   {v.message}")
            evidence = v.evidence
            if evidence:
                strong_line = evidence.get("strong_line")
                weak_line = evidence.get("weak_line")
                # Render available pairing evidence even if only one
                # half is present (rather than silently dropping both).
                # Each line is only emitted when its primary value is
                # not None — partial pairs are still informative for
                # the LLM repair prompt.
                if strong_line is not None:
                    lines.append(
                        f"   Original:  '{strong_line}' "
                        f"(line {evidence.get('strong_line_no', '?')})"
                    )
                if weak_line is not None:
                    lines.append(
                        f"   Modified:  '{weak_line}' "
                        f"(line {evidence.get('weak_line_no', '?')})"
                    )
                pairing_score = evidence.get("pairing_score")
                if pairing_score is not None:
                    lines.append(
                        f"   Pairing score: {pairing_score}"
                    )
                all_pairings = evidence.get("all_pairings")
                if isinstance(all_pairings, list) and len(all_pairings) > 1:
                    lines.append(
                        f"   Additional pairings ({len(all_pairings) - 1}):"
                    )
                    for p in all_pairings[1:]:
                        if not isinstance(p, dict):
                            continue
                        p_strong = p.get("strong_line", "?")
                        p_weak = p.get("weak_line", "?")
                        p_strong_no = p.get("strong_line_no", "?")
                        p_weak_no = p.get("weak_line_no", "?")
                        lines.append(
                            f"     - '{p_strong}' -> '{p_weak}' "
                            f"(line {p_strong_no} -> {p_weak_no})"
                        )
                deleted_line = evidence.get("deleted_line")
                if deleted_line is not None:
                    lines.append(
                        f"   Deleted:   '{deleted_line}' "
                        f"(line {evidence.get('deleted_line_no', '?')})"
                    )
                when_clauses = evidence.get("when_clauses")
                if isinstance(when_clauses, list):
                    for wc in when_clauses:
                        lines.append(f"   Deleted WHEN: '{wc}'")
                # Topology evidence (safety-critical — promoted so the LLM
                # sees it as a first-class signal rather than a raw dump).
                pre_sha = evidence.get("pre_sha")
                post_sha = evidence.get("post_sha")
                if pre_sha is not None or post_sha is not None:
                    lines.append(
                        f"   Topology: pre={pre_sha if pre_sha is not None else '?'} "
                        f"post={post_sha if post_sha is not None else '?'} "
                        f"parents={evidence.get('parent_count', '?')} "
                        f"(min={evidence.get('min_parents', '?')})"
                    )
                    topology_check = evidence.get("topology_check")
                    if topology_check is not None:
                        lines.append(
                            f"   Topology check: {topology_check}"
                        )
                # Defensive fallback: dump any unrecognized evidence keys so
                # the LLM repair prompt still gets context when evidence shape
                # evolves (e.g. new detector adds a novel key).
                # Use str() for scalars and json.dumps() for collections so
                # the prompt format stays consistent with the recognized branches.
                recognized = {
                    "strong_line", "weak_line", "strong_line_no",
                    "weak_line_no", "pairing_score", "deleted_line",
                    "deleted_line_no", "when_clauses", "all_pairings",
                    "pre_sha", "post_sha", "parent_count", "min_parents",
                    "topology_check", "branch_name", "trigger_branch",
                    "branch_kind", "exception_type", "exception_msg",
                    "prefix_score",
                }
                for key, value in evidence.items():
                    if key not in recognized:
                        if isinstance(value, (list, dict, tuple, set)):
                            lines.append(f"   {key}: {json.dumps(value, ensure_ascii=False)}")
                        else:
                            lines.append(f"   {key}: {str(value)}")
        return "\n".join(lines) if lines else "(none)"

    def _format_spec_contents(self, contents: dict[str, str]) -> str:
        """Format spec contents dict for the repair prompt."""
        lines: list[str] = []
        for path, content in sorted(contents.items()):
            lines.append(f"--- {path} ---")
            lines.append(content)
            lines.append("")
        return "\n".join(lines) if lines else "(none)"

    def _restore_merged_content(
        self,
        repaired_files: list[str],
        merged_spec_contents: dict[str, str],
    ) -> None:
        """Restore the merged spec content for files that were modified.

        Called when repair fails after files have been written, to leave the
        working tree in the same state it was before ``repair_violations`` was
        called.  This makes the repairer self-contained: callers do not have
        to perform their own cleanup.

        Best-effort across files: if one file's restore fails (OSError),
        the other files are still attempted so the working tree is not
        left half-restored.  After all files have been processed, the
        first encountered OSError is re-raised so the caller knows at
        least one file could not be restored (A6 contract).

        Callers in cleanup paths SHOULD wrap this call in their own
        ``try/except`` if they wish to suppress the OSError; the method
        does NOT swallow it (defect A6 prohibits silent masking of
        restore failures).

        Raises:
            OSError: If at least one file could not be written back.  All
            other files are still attempted before this is raised.
        """
        # Deduplicate to avoid double-writes when the caller's list contains
        # repeats (defensive against future refactors that change the
        # producer).
        seen: set[str] = set()
        first_error: Optional[OSError] = None
        for path in repaired_files:
            if path in seen:
                continue
            seen.add(path)
            merged_content = merged_spec_contents.get(path)
            if merged_content is not None:
                full_path = (self.project_root / path).resolve()
                try:
                    full_path.write_text(merged_content, encoding="utf-8")
                    logger.info("Restored merged content for %s", path)
                except OSError as exc:
                    logger.error(
                        "Failed to restore merged content for %s: %s",
                        path, exc,
                    )
                    if first_error is None:
                        first_error = exc
            else:
                # The file was written by the LLM but is NOT in the merged
                # spec contents (unexpected spec path).  Remove it so the
                # working tree is not silently dirty with hallucinated content.
                full_path = (self.project_root / path).resolve()
                try:
                    if full_path.exists():
                        checkout_result = _run_git(
                            self.project_root,
                            "checkout", "HEAD", "--", path,
                            check=False, timeout=15,
                        )
                        if checkout_result.returncode != 0:
                            # Defensive: only unlink files that are tracked
                            # by git.  An untracked file at this path could
                            # be a user's in-progress new spec, an editor
                            # backup, or any other artifact unrelated to
                            # the merge.  Deleting such a file would be
                            # silent data loss.  When git ls-files shows
                            # the path as tracked (output non-empty,
                            # returncode 0), the unlink is safe because
                            # the merged content takes precedence and the
                            # tracked version was supposed to come from
                            # HEAD.  When the path is untracked, leave it
                            # alone and log a warning so the operator can
                            # investigate.
                            ls_files_result = _run_git(
                                self.project_root,
                                "ls-files", "--error-unmatch", "--", path,
                                check=False, timeout=15,
                            )
                            is_tracked = (
                                ls_files_result.returncode == 0
                                and bool(ls_files_result.stdout.strip())
                            )
                            # Defense in depth: ``git ls-files`` may emit
                            # nothing on stdout for a path that exists in
                            # the index but is staged for deletion (e.g.
                            # the LLM hallucinated a path that was just
                            # deleted in the merge), causing
                            # ``is_tracked=False`` and the file to be
                            # left in place when it should be removed.
                            # Disambiguate via ``git ls-tree -r HEAD``
                            # which reports the path if it exists in the
                            # current commit's tree regardless of index
                            # state.
                            if not is_tracked:
                                ls_tree_result = _run_git(
                                    self.project_root,
                                    "ls-tree", "-r", "HEAD", "--", path,
                                    check=False, timeout=15,
                                )
                                if (
                                    ls_tree_result.returncode == 0
                                    and bool(ls_tree_result.stdout.strip())
                                ):
                                    is_tracked = True
                            if is_tracked:
                                full_path.unlink()
                                logger.info(
                                    "Deleted unexpected spec file (not in merged "
                                    "contents, but tracked by git): %s",
                                    path,
                                )
                            else:
                                logger.warning(
                                    "Refusing to delete unexpected spec file "
                                    "%s: file is untracked by git and may be "
                                    "user-owned (in-progress spec, editor "
                                    "backup, etc.). Working tree is left as-is; "
                                    "manual cleanup may be required.",
                                    path,
                                )
                        else:
                            logger.info(
                                "Restored unexpected spec file from HEAD: %s",
                                path,
                            )
                except OSError as exc:
                    logger.error(
                        "Failed to remove unexpected spec file %s: %s",
                        path, exc,
                    )
                    if first_error is None:
                        first_error = exc
        if first_error is not None:
            raise first_error

    def _rollback_commit(
        self,
        pre_repair_sha: Optional[str],
        repaired_files: list[str],
        merged_spec_contents: dict[str, str],
    ) -> None:
        """Rollback a commit created by the repair process.

        Always resets to the explicit *pre_repair_sha* captured before any
        commit operation.  This avoids ``HEAD~1`` which is fragile if
        parallel processes or hooks create additional commits (defect A1-A4
        fix, extended to the fix-up path).

        After resetting, unstages repaired files and restores merged content.

        **Critical safety contract:** when ``pre_repair_sha`` is ``None``
        (e.g. ``git rev-parse HEAD`` failed before staging), this method
        MUST refuse to reset rather than fall back to ``HEAD~1``.  The
        ``HEAD~1`` fallback was the original A1-A4 incident: after an
        amend, ``HEAD~1`` equals the pre-merge HEAD and the soft reset
        silently drops the entire merge commit.
        """
        if not pre_repair_sha:
            logger.error(
                "Rollback refused: pre_repair_sha is missing; will NOT "
                "fall back to HEAD~1 because that would silently drop the "
                "merge commit on the amend path. Working tree may now be "
                "inconsistent — manual intervention required."
            )
            # Best-effort: still try to unstage repaired files and restore
            # merged content so the working tree at least matches the
            # spec content captured before repair.
            for path in repaired_files:
                _run_git(
                    self.project_root, "reset", "HEAD", path,
                    check=False, timeout=15,
                )
            captured_restore_exc: Optional[OSError] = None
            captured_restore_path: Optional[str] = None
            try:
                self._restore_merged_content(repaired_files, merged_spec_contents)
            except OSError as restore_exc:
                # Preserve the OSError so we can chain it onto the
                # eventual GuardrailRepairInconsistentState raise.
                # Without `raise ... from restore_exc` the operator
                # loses the precise file path that failed to restore
                # — exactly the diagnostic they need for manual
                # recovery.
                captured_restore_exc = restore_exc
                captured_restore_path = getattr(restore_exc, "filename", None)
                logger.warning(
                    "_restore_merged_content failed after rollback refused "
                    "(filename=%s): %s",
                    captured_restore_path,
                    restore_exc,
                )
            # Raise so the orchestrator knows the sequence must stop;
            # continuing into an inconsistent state (repair commit in HEAD
            # but working tree reverted) risks corrupting the next branch.
            inconsistent_msg = (
                "Guardrail repair state is inconsistent: a repair commit "
                "was created but pre_repair_sha is missing so rollback "
                "was refused. The working tree has been restored but HEAD "
                "still contains the repair commit. Manual intervention "
                "is required before any further merge operations."
            )
            if captured_restore_exc is not None:
                # Include the path that failed to restore in the message
                # so the operator does not have to dig through logs to
                # find which file is now in an inconsistent state.
                if captured_restore_path:
                    inconsistent_msg += (
                        f" Additionally, restoring merged content for "
                        f"path '{captured_restore_path}' failed: "
                        f"{captured_restore_exc}."
                    )
                else:
                    inconsistent_msg += (
                        f" Additionally, restoring merged content "
                        f"failed: {captured_restore_exc}."
                    )
                raise GuardrailRepairInconsistentState(
                    inconsistent_msg
                ) from captured_restore_exc
            raise GuardrailRepairInconsistentState(inconsistent_msg)

        target = pre_repair_sha
        reset_result = _run_git(
            self.project_root, "reset", "--soft", target,
            check=False, timeout=15,
        )
        if reset_result.returncode != 0:
            logger.warning(
                "Rollback to %s failed: %s",
                target[:8] if target else "<none>",
                reset_result.stderr.strip() or "unknown error",
            )

        # Unstage repaired files so index and working tree stay in sync
        for path in repaired_files:
            _run_git(
                self.project_root, "reset", "HEAD", path,
                check=False, timeout=15,
            )

        # Restore merged content
        self._restore_merged_content(repaired_files, merged_spec_contents)

    def _call_llm(self, prompt: str) -> str | dict[str, Any]:
        """Call LLM with the repair prompt.

        Uses the injected :attr:`_llm_caller` if available (Task 12 / A13 fix),
        otherwise falls back to creating a per-call :class:`LLMCaller`.

        K2: If an :class:`LLMTrace` was injected, the call is timed and
        recorded as a jsonl entry.
        """
        caller = self._llm_caller
        if caller is None:
            from ..llm_caller import LLMCaller

            caller = LLMCaller(
                project_root=self.project_root,
                step_type="guardrail_repair",
                max_retries=2,
                retry_delay=1.0,
            )

        t0 = time.monotonic()
        raw: str = ""
        outcome: str = ""
        error: Optional[str] = None
        # Track the exception class so the trace records the failure
        # type even when ``str(exc)`` doesn't include it.  Helps
        # diagnose LLM-side failures (e.g. distinguishing TimeoutExpired
        # from ConnectionError from RuntimeError) without parsing the
        # human-readable error string.
        error_class: Optional[str] = None
        try:
            raw = caller.call(
                prompt=prompt,
                require_json=False,
            )
            outcome = "success"
        except Exception as exc:
            outcome = "error"
            error_class = exc.__class__.__name__
            # Prefix the error message with the class name so log
            # consumers see both pieces of context in one field.
            error = f"{error_class}: {exc}"
            raise
        finally:
            if self._llm_trace is not None:
                try:
                    # On success: ``raw`` carries the full response text
                    # because ``caller.call`` is non-streaming and only
                    # assigns ``raw`` after the call returns. On error:
                    # the assignment never happens so ``raw`` stays empty
                    # — surface that honestly with an empty string rather
                    # than the previous defensive ``isinstance`` branch
                    # that silently rotted into dead code.  If a future
                    # refactor introduces streaming, replace this with a
                    # proper partial-response capture rather than relying
                    # on a one-line ``raw`` assignment.
                    response_for_trace = (
                        redact_text(raw)
                        if outcome == "success" and isinstance(raw, str) and raw
                        else ""
                    )
                    trace_meta: dict[str, Any] = {}
                    if error_class is not None:
                        trace_meta["error_class"] = error_class
                    self._llm_trace.record(
                        agent="guardrail_repair",
                        prompt=redact_text(prompt),
                        response=response_for_trace,
                        duration_sec=time.monotonic() - t0,
                        outcome=outcome,
                        error=error,
                        meta=trace_meta or None,
                    )
                except Exception as trace_exc:
                    logger.warning(
                        "LLM trace record failed (non-fatal): %s",
                        trace_exc,
                    )

        # Try to extract JSON using two-phase extraction
        schema_hint = (
            "A JSON object with a 'files' array. Each file has 'path' and "
            "'corrected_content' fields."
        )
        try:
            parsed = extract_json_two_phase(
                raw,
                project_root=self.project_root,
                schema_hint=schema_hint,
                required_keys=["files"],
            )
        except Exception as exc:
            logger.warning(
                "Guardrail repair LLM response parsing failed: %s", exc
            )
            # Use a typed subclass so the outer ``repair_violations``
            # loop can distinguish JSON-parse failures from generic LLM
            # transport errors and surface a distinct diagnostic.
            raise GuardrailRepairResponseParseError(
                f"guardrail repair LLM response parsing failed: {exc}"
            ) from exc

        if parsed is not None:
            # Return the dict directly to avoid a wasteful
            # serialize→deserialize round-trip.
            return parsed

        # Return raw for fallback parsing
        return raw

    def _parse_response(self, raw_response: str | dict[str, Any]) -> Optional[dict[str, Any]]:
        """Parse LLM repair response as JSON dict."""
        if isinstance(raw_response, dict):
            if "files" in raw_response:
                return raw_response
            return None

        from ..utils.json_parser import parse_json_response

        return parse_json_response(raw_response, required_keys=["files"])
