"""SE3 Merge-Respond command — Process MCP call response files for merge conflicts.

Usage:
    se3 merge-respond <call-file-path>
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

from ..engine.display import render_text

logger = logging.getLogger(__name__)

_STRICT_SENTINEL = "[__SE3_STRICT_PLACEHOLDER__:"


def _is_spec_path(path: str) -> bool:
    """Return True when *path* points to a se3 spec file.

    Delegates to the canonical implementation in
    :mod:`se3.engine.merge.guardrails` so the merge subsystem shares
    one detector across modules.  The canonical implementation
    normalises backslashes to forward slashes and rejects empty
    intermediate path segments.
    """
    from se3.engine.merge.guardrails import _is_spec_path as _canonical
    return _canonical(path)


def _first_parent_sha(project_root: Path) -> str:
    """Return the first-parent SHA of HEAD.

    G1: Uses ``git rev-list --parents -n 1 HEAD`` instead of
    ``git rev-parse HEAD^1`` so that octopus merges (>2 parents) are
    handled by treating the first parent as the pre-merge ours-side
    state, exactly the same as a 2-parent merge.

    Raises:
        RuntimeError: If git fails or HEAD has no parents (root commit).
    """
    result = subprocess.run(
        ["git", "-C", str(project_root), "rev-list", "--parents", "-n", "1", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git rev-list --parents -n 1 HEAD failed "
            f"(rc={result.returncode}): {result.stderr.strip()}"
        )
    parts = result.stdout.strip().split()
    # parts[0] is HEAD itself; parts[1:] are parents in order
    if len(parts) < 2:
        raise RuntimeError(
            f"HEAD has no parents — cannot determine pre-merge SHA "
            f"(rev-list output: {result.stdout!r})"
        )
    return parts[1]


def _head_parent_count(project_root: Path) -> int | None:
    """Return the number of parents of HEAD, or ``None`` when undetermined.

    A return value of ``0`` is a legitimate but extremely unusual state
    (HEAD is the root commit). Returning ``0`` for a transient git
    failure would silently mask octopus-merge detection in the
    manual-resolution path; callers decide separately whether to treat
    that masking as fatal. Returning ``None`` lets the caller render an
    explicit "could not determine parent count" warning rather than
    proceeding under a false-zero assumption.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "rev-list", "--parents", "-n", "1", "HEAD"],
            capture_output=True, text=True, check=False, timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    parts = result.stdout.strip().split()
    return max(0, len(parts) - 1)


def _extract_version_from_string(content: str, filename: str) -> str | None:
    """Extract version from file content (TOML or JSON).

    Returns the version string, or None if no version field is found
    or the format is unsupported.
    """
    if filename.endswith(".toml"):
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib  # type: ignore
        try:
            data = tomllib.loads(content)
        except Exception as exc:
            # The caller treats None as "cannot extract" and silently
            # bypasses the version-unchanged hard-error check. Surface
            # the parse failure at WARNING so a corrupted PRE
            # pyproject.toml does not silently disable that guard.
            logger.warning(
                "Could not parse TOML version from %s: %s — "
                "version-unchanged check will be bypassed",
                filename, exc,
            )
            return None
        version = data.get("project", {}).get("version")
        if version is None:
            version = data.get("tool", {}).get("poetry", {}).get("version")
        return version
    if filename == "package.json":
        try:
            import json
            data = json.loads(content)
            return data.get("version")
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def _check_version_unchanged(
    project_root: Path,
    pre_sha: str,
) -> str | None:
    """Check if version in pyproject.toml or package.json changed from pre_sha to HEAD.

    Returns an error message when:
      * a version file exists at both refs but the version is unchanged, OR
      * the version file IS present at the post_sha (HEAD) but unparseable
        (e.g. a corrupted pyproject.toml could not be parsed as TOML, or the
        version field is missing). A spec-touching merge that broke the
        version file's syntax must NOT be silently treated as a no-op:
        this branch surfaces the corruption as a hard failure so the
        operator does not conclude "merge success" against a broken
        version file.

    Returns ``None`` when no issue is detected: version changed,
    file absent at one or both refs, or the file is absent post-merge
    (a clean removal is treated as out-of-scope for this checker).
    """
    for filename in ("pyproject.toml", "package.json"):
        pre_result = subprocess.run(
            ["git", "-C", str(project_root), "show", f"{pre_sha}:{filename}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        post_result = subprocess.run(
            ["git", "-C", str(project_root), "show", f"HEAD:{filename}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        # Distinguish "file absent at pre_sha" (legitimate skip) from
        # "file present at pre_sha but vanished at HEAD" (also a legitimate
        # skip — checker is not in the business of flagging deletions).
        # Both produce a continue. Neither rises to a hard error.
        if pre_result.returncode != 0:
            continue
        if post_result.returncode != 0:
            continue
        pre_version = _extract_version_from_string(pre_result.stdout, filename)
        post_version = _extract_version_from_string(post_result.stdout, filename)
        # Hard error: file IS present at HEAD but parsing failed (post_version
        # is None). Distinguishing this from "absent" is what prevents a
        # corrupt version file from masquerading as "no version change".
        # We only flag this when pre_version was parseable (otherwise the
        # input was already broken at pre_sha and this checker should not
        # claim the merge introduced a parse failure).
        if pre_version is not None and post_version is None:
            return (
                f"Version file {filename} is present at HEAD but the version "
                f"field could not be parsed (corrupted file or missing version "
                f"field). The merge has produced an unparseable version file "
                f"— refusing to treat as a no-op."
            )
        if (
            pre_version is not None
            and post_version is not None
            and pre_version == post_version
        ):
            return (
                f"Version in {filename} unchanged at {pre_version} "
                f"(pre-merge == HEAD)"
            )
    return None


def process_merge_response(
    call_file: Path,
    project_root: Optional[Path] = None,
) -> int:
    """Process an MCP call response file for merge conflicts.

    Reads the corresponding ``.response`` file next to *call_file*,
    validates the user's choice, and executes the action.

    Args:
        call_file: Path to the original merge call file.
        project_root: Project root directory. Auto-detected if None.

    Returns:
        Exit code (0 for success, non-zero for failure).
    """
    if project_root is None:
        from .run import get_project_root

        project_root = get_project_root()

    # K1 fix: acquire the merge lock around the git-touching critical
    # section so a concurrent ``se3 merge <branch>`` started by another
    # shell cannot race with us over the working tree, the index,
    # ``git commit --no-edit``, ``git reset --hard``, and the spec-files
    # guardrails check. Use the context-manager form so an exception
    # raised between MergeLock construction and the try/finally entry
    # (e.g. a lazy import inside _process_merge_response_locked failing
    # before the function body executes) cannot leak the lock — the
    # ``with`` statement binds acquire and release into the same scope.
    from .merge.merge_lock import MergeLock, MergeLockBusy, MergeLockStale

    call_path = Path(call_file)
    if not call_path.exists():
        render_text(f"Call file not found: {call_path}", title="SE3 Merge Error")
        return 1

    try:
        with MergeLock(project_root):
            return _process_merge_response_locked(
                call_path=call_path, project_root=project_root,
            )
    except MergeLockBusy as exc:
        render_text(
            f"Another se3 merge process is currently running "
            f"(holder pid={exc.holder_pid}). Wait for it to finish and "
            f"retry.\nLock file: {exc.lock_file}",
            title="SE3 Merge Error",
        )
        return 1
    except MergeLockStale as exc:
        if exc.holder_pid is None:
            pid_msg = "(unparseable pid)"
        else:
            pid_msg = f"(holder pid={exc.holder_pid} does not exist)"
        render_text(
            f"Merge lock appears stale {pid_msg}. If you are sure no other "
            f"se3 merge is running, remove {exc.lock_file} and retry.",
            title="SE3 Merge Error",
        )
        return 1


_PENDING_GUARDRAILS_SUFFIX = ".pending_guardrails"


def _verify_pending_guardrails(
    call_path: Path,
    project_root: Path,
    feedback: str,
) -> int:
    """Verify guardrails for a parked manual-resolution call file.

    Reads the ``<call>.pending_guardrails`` marker (written by the
    manual-resolution path), runs ``MergeGuardrailsCheck`` against the
    user's just-created commit, and either deletes the marker on
    success or rolls back HEAD to the pre-merge SHA on failure.

    Returns a CLI exit code (0 = success, 1 = failure).
    """
    marker_path = Path(str(call_path) + _PENDING_GUARDRAILS_SUFFIX)
    try:
        marker_data = json.loads(marker_path.read_text(encoding="utf-8"))
    except Exception as exc:
        render_text(
            f"Failed to parse pending-guardrails marker at {marker_path}: {exc}",
            title="SE3 Merge Error",
        )
        return 1

    pre_sha = marker_data.get("pre_sha", "")
    if not pre_sha:
        render_text(
            f"Pending-guardrails marker {marker_path} is missing 'pre_sha'.",
            title="SE3 Merge Error",
        )
        return 1

    head_result = subprocess.run(
        ["git", "-C", str(project_root), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False, timeout=15,
    )
    if head_result.returncode != 0:
        render_text(
            f"Failed to read HEAD: {head_result.stderr.strip()}",
            title="SE3 Merge Error",
        )
        return 1
    post_sha = head_result.stdout.strip()

    # If the user has not committed yet, HEAD is still pre_sha — refuse
    # to proceed and tell them to commit first.
    if post_sha == pre_sha:
        render_text(
            "No new commit detected since the manual resolution was started. "
            "Please complete the merge first (`git add . && git commit`), "
            "then re-run this command to verify guardrails.",
            title="SE3 Merge — Manual Resolution Pending",
        )
        return 1

    # Guard against multi-commit advancement: if the user committed
    # multiple times (intentionally or via amend), a hard reset would
    # destroy intermediate work.
    count_result = subprocess.run(
        ["git", "-C", str(project_root), "rev-list", "--count", f"{pre_sha}..HEAD"],
        capture_output=True, text=True, check=False, timeout=15,
    )
    if count_result.returncode == 0:
        try:
            commit_count = int(count_result.stdout.strip())
        except ValueError:
            commit_count = 0
        if commit_count > 1:
            render_text(
                f"HEAD has advanced {commit_count} commits since the manual "
                f"resolution was started. A hard reset would destroy those "
                f"commits. Please resolve the guardrail violations manually "
                f"and create a new commit, or reset to {pre_sha[:8]} yourself.",
                title="SE3 Merge — Multi-Commit Advancement Detected",
            )
            return 1

    try:
        from se3.engine.merge.guardrails import MergeGuardrailsCheck

        guardrails = MergeGuardrailsCheck(project_root)
        gr_report = guardrails.check_merge_result(pre_sha, post_sha)
    except Exception as exc:
        render_text(
            f"Guardrails check failed after manual resolution: {exc}",
            title="SE3 Merge Error",
        )
        return 1

    if gr_report.passed:
        try:
            marker_path.unlink()
        except OSError:
            pass
        render_text(
            "Manual resolution accepted: guardrails passed."
            + (f"\nFeedback: {feedback}" if feedback else "")
            + "\n\nNote: If you had uncommitted changes that were auto-stashed "
            "during a previous guardrails failure, recover them with "
            "`git stash pop`.",
            title="SE3 Merge — Verified",
        )
        return 0

    # Guardrails failed — roll back the user's commit so the spec
    # contract is honored, and surface the violations.
    # K9: Before the hard reset, attempt to stash uncommitted work so
    # that non-spec changes the user made after the merge are not
    # silently destroyed.  Stash is best-effort: if it fails (e.g.
    # no changes to stash, index locked), we continue with the reset
    # after warning the user.
    stash_attempted = False
    stash_result = subprocess.run(
        ["git", "-C", str(project_root), "stash", "push", "-m",
         f"se3-merge-respond auto-stash before rollback to {pre_sha[:8]}"],
        capture_output=True, text=True, check=False, timeout=30,
    )
    if stash_result.returncode == 0:
        stash_attempted = True
    rollback_result = subprocess.run(
        ["git", "-C", str(project_root), "reset", "--hard", pre_sha],
        capture_output=True, text=True, check=False, timeout=30,
    )
    rollback_note = ""
    if rollback_result.returncode != 0:
        rollback_note = (
            f"\n\nWARNING: Rollback to {pre_sha[:8]} failed "
            f"(rc={rollback_result.returncode}): "
            f"{rollback_result.stderr.strip() or 'unknown'}. "
            f"Working tree may still contain the guardrail-violating "
            f"commit. Manual `git reset --hard {pre_sha[:8]}` is required."
        )
        if stash_attempted:
            rollback_note += (
                f"\n\nUncommitted changes were also stashed before the "
                f"failed reset. After running the manual reset above, "
                f"recover them with `git stash pop`."
            )
    else:
        rollback_note = (
            f"\n\nThe guardrail-violating commit was rolled back to "
            f"{pre_sha[:8]}. Please fix the spec files and re-run."
        )
        if stash_attempted:
            rollback_note += (
                f"\nUncommitted changes were stashed before rollback; "
                f"recover with `git stash pop` if needed."
            )
        try:
            marker_path.unlink()
        except OSError:
            pass

    violations_lines = [
        f"  [{v.violation_type}] {v.file_path}: {v.message}"
        for v in gr_report.violations
    ]
    render_text(
        "REFUSED: Guardrail violations were detected in spec files "
        "after the manual resolution commit:\n\n"
        + "\n".join(violations_lines)
        + rollback_note,
        title="SE3 Merge — Guardrail Violations (Rolled Back)",
    )
    return 1


def _process_merge_response_locked(
    *,
    call_path: Path,
    project_root: Path,
) -> int:
    """Body of :func:`process_merge_response` executed under the merge lock."""

    # Pending-guardrails marker handling: if the user previously chose
    # "manual" on a spec-touching merge, we parked the call file and
    # asked them to re-invoke after committing.  On re-entry we run
    # guardrails before falling through to the normal response flow.
    marker_path = Path(str(call_path) + _PENDING_GUARDRAILS_SUFFIX)
    if marker_path.exists():
        # Read feedback from the .response file if present, but tolerate
        # absence — the marker takes precedence.
        feedback = ""
        response_path_marker = Path(str(call_path) + ".response")
        if response_path_marker.exists():
            try:
                rdata = json.loads(response_path_marker.read_text(encoding="utf-8"))
                feedback = rdata.get("feedback", "")
            except Exception as exc:
                logger.warning(
                    "Failed to parse response file %s for pending-guardrails "
                    "re-entry: %s — feedback discarded.",
                    response_path_marker, exc,
                )
        return _verify_pending_guardrails(call_path, project_root, feedback)

    response_path = Path(str(call_path) + ".response")
    if not response_path.exists():
        render_text(
            f"Response file not found: {response_path}\n"
            "Create it with JSON: {\"choice\": \"accept|abort|manual\", "
            "\"feedback\": \"optional notes\"}",
            title="SE3 Merge Error",
        )
        return 1

    try:
        call_data = json.loads(call_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        render_text(
            f"Failed to parse call file: {exc}",
            title="SE3 Merge Error",
        )
        return 1

    try:
        response_data = json.loads(response_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        render_text(
            f"Failed to parse response file: {exc}",
            title="SE3 Merge Error",
        )
        return 1

    choice = response_data.get("choice", "").strip().lower()
    feedback = response_data.get("feedback", "")

    if choice not in ("accept", "abort", "manual"):
        render_text(
            f"Invalid choice '{choice}'. Must be one of: accept, abort, manual",
            title="SE3 Merge Error",
        )
        return 1

    call_type = call_data.get("type", "merge_conflict")

    if choice == "accept":
        # Guard against strict-mode placeholder content being accepted.
        # Defense-in-depth: (a) check the structural flag llm_invoked=False,
        # (b) verify the strategy field matches "strict", and (c) the
        # sentinel string prefix as a belt-and-braces third layer.  Any
        # one of these signals can trigger refusal.
        if call_type == "merge_conflict":
            files = call_data.get("files", [])
            call_strategy = call_data.get("strategy")
            sentinel_files: list[str] = []
            for f in files:
                llm_res = f.get("llm_resolution") or {}
                resolved = llm_res.get("resolved_content", "")
                flags = llm_res.get("flags") or {}
                is_strict_placeholder = (
                    # Structural flag: the orchestrator explicitly marks
                    # strict-mode placeholders with llm_invoked=False.
                    (flags.get("llm_invoked") is False and call_strategy == "strict")
                    # Belt-and-braces: sentinel string prefix.
                    or resolved.startswith(_STRICT_SENTINEL)
                )
                if is_strict_placeholder:
                    sentinel_files.append(f.get("path", "<unknown>"))

            if sentinel_files:
                render_text(
                    "REFUSED: The LLM resolution contains the strict-mode "
                    "placeholder sentinel.\n\n"
                    f"Affected file(s): {', '.join(sentinel_files)}\n\n"
                    "This merge was created with --strategy=strict, which skips "
                    "LLM resolution. You MUST manually edit the files to resolve "
                    "conflicts before accepting.\n\n"
                    "To proceed manually:\n"
                    "  1. Edit the conflicting files to resolve conflicts\n"
                    "  2. Run: git add . && git commit\n"
                    "  3. Or update the .response file to 'manual' or 'abort'.",
                    title="SE3 Merge — Strict Placeholder Detected",
                )
                return 1

            # Refuse to accept if the call file recorded orphan-spec
            # guardrail violations: writing those resolutions would land
            # spec changes that violate the guardrails contract. The
            # user must use 'manual' and edit the spec files explicitly.
            orphan_violations = call_data.get("orphan_guardrails_violations") or []
            if orphan_violations:
                lines = [
                    f"  [{v.get('violation_type', 'unknown')}] "
                    f"{v.get('file_path', '<unknown>')}: "
                    f"{v.get('message', '')}"
                    for v in orphan_violations
                ]
                render_text(
                    "REFUSED: The call file contains LLM-proposed orphan "
                    "spec resolutions that violate guardrails. Accepting "
                    "would write guardrail-violating spec content.\n\n"
                    "Recorded violations:\n"
                    + "\n".join(lines)
                    + "\n\n"
                    "To proceed: choose 'manual', edit the spec files "
                    "yourself, then `git add . && git commit`.",
                    title="SE3 Merge — Orphan Guardrail Violations",
                )
                return 1

            # Write resolved content back to files
            try:
                for f in files:
                    llm_res = f.get("llm_resolution") or {}
                    resolved = llm_res.get("resolved_content", "")
                    file_path_str = f.get("path", "")
                    if not file_path_str:
                        continue
                    if not resolved:
                        # Empty resolved_content represents a deletion
                        # (matches orchestrator._apply_resolution
                        # semantics). Use `git rm -f` so the deletion
                        # actually lands; otherwise the subsequent
                        # commit would either fail (unmerged path) or
                        # silently keep the file.
                        full_path = project_root / file_path_str
                        if full_path.exists():
                            rm_result = subprocess.run(
                                [
                                    "git", "-C", str(project_root),
                                    "rm", "-f", file_path_str,
                                ],
                                capture_output=True,
                                text=True,
                                check=False,
                                timeout=30,
                            )
                        else:
                            # File absent from working tree (e.g. rename
                            # conflict) but may still have unmerged
                            # index entries — stage removal with
                            # --ignore-unmatch so a never-existed path
                            # does not blow up the loop.
                            rm_result = subprocess.run(
                                [
                                    "git", "-C", str(project_root),
                                    "rm", "-f", "--ignore-unmatch",
                                    file_path_str,
                                ],
                                capture_output=True,
                                text=True,
                                check=False,
                                timeout=30,
                            )
                        if rm_result.returncode != 0:
                            raise RuntimeError(
                                f"git rm -f {file_path_str} failed "
                                f"(rc={rm_result.returncode}): "
                                f"{rm_result.stderr.strip() or 'unknown error'}"
                            )
                        continue
                    file_path = project_root / file_path_str
                    file_path.parent.mkdir(parents=True, exist_ok=True)
                    file_path.write_text(resolved, encoding="utf-8")

                    # G3: Stage the file with returncode validation.
                    # Previously check=False with no returncode inspection
                    # silently swallowed `git add` failures (e.g. invalid
                    # path, locked index), leaving the merge half-staged.
                    add_result = subprocess.run(
                        ["git", "-C", str(project_root), "add", file_path_str],
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=30,
                    )
                    if add_result.returncode != 0:
                        raise RuntimeError(
                            f"git add {file_path_str} failed "
                            f"(rc={add_result.returncode}): "
                            f"{add_result.stderr.strip() or 'unknown error'}"
                        )
            except Exception as exc:
                render_text(
                    f"Failed to write resolved content: {exc}",
                    title="SE3 Merge Error",
                )
                return 1

            # Commit the merge
            commit_result = subprocess.run(
                ["git", "-C", str(project_root), "commit", "--no-edit"],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            if commit_result.returncode != 0:
                render_text(
                    f"Merge commit failed: {commit_result.stderr.strip()}",
                    title="SE3 Merge Error",
                )
                return 1

            # B1 post-condition: verify the branch is actually an ancestor
            # of HEAD and that HEAD is a merge commit.  This catches the
            # case where the user committed only a subset of files or
            # MERGE_HEAD was already cleared.
            theirs_branch = call_data.get("theirs_branch", "")
            if theirs_branch:
                try:
                    from se3.commands.merge.postcondition import (
                        PostConditionViolated,
                        assert_branch_merged,
                        assert_head_is_merge_commit,
                    )
                    assert_branch_merged(project_root, theirs_branch, timeout=15)
                    assert_head_is_merge_commit(project_root, theirs_branch, timeout=15)
                except PostConditionViolated as pc_exc:
                    render_text(
                        f"Post-condition check failed after merge commit: {pc_exc}",
                        title="SE3 Merge Error",
                    )
                    return 1

            # After successful commit, run guardrails on any spec files
            # that were part of the resolution to close the gap between
            # LLM-resolved and human-resolved merge paths.
            #
            # Per the spec contract (Mandatory guardrails after every
            # `se3 merge` commit), spec-touching merge commits with
            # violations MUST be rolled back and escalated to a human
            # call file — they cannot be silently downgraded to a
            # warning + exit 0.
            spec_paths = [
                f["path"] for f in files
                if _is_spec_path(f.get("path", ""))
            ]
            if spec_paths:
                try:
                    post_sha = subprocess.run(
                        ["git", "-C", str(project_root), "rev-parse", "HEAD"],
                        capture_output=True, text=True, check=True,
                        timeout=15,
                    ).stdout.strip()
                    # G1: Use first-parent walk via rev-list --parents so
                    # octopus merges with >2 parents are handled
                    # consistently (the first parent is always the
                    # ours-side pre-merge state).
                    pre_sha = _first_parent_sha(project_root)
                    parent_count = _head_parent_count(project_root)
                    if parent_count is None:
                        logger.warning(
                            "merge-respond: could not determine HEAD parent "
                            "count (rev-list failed). Octopus merges may go "
                            "undetected; guardrails compare first-parent "
                            "(ours-side) against HEAD as the conservative "
                            "default."
                        )
                    elif parent_count > 2:
                        logger.warning(
                            "merge-respond: HEAD is an octopus merge (%d parents). "
                            "Guardrails compare first-parent (ours-side) against HEAD; "
                            "changes from other merged branches are also in the ancestry.",
                            parent_count,
                        )

                    from se3.engine.merge.guardrails import MergeGuardrailsCheck
                    guardrails = MergeGuardrailsCheck(project_root)
                    gr_report = guardrails.check_merge_result(pre_sha, post_sha)

                    if not gr_report.passed:
                        violations_lines = [
                            f"  [{v.violation_type}] {v.file_path}: {v.message}"
                            for v in gr_report.violations
                        ]
                        # Hard-roll back to pre_sha so the guardrail-
                        # violating commit does not stand. The spec
                        # contract requires that spec-touching merge
                        # commits with violations MUST NOT remain on
                        # HEAD; downgrading to a warning would land a
                        # guardrail-violating spec change with exit 0.
                        rollback_result = subprocess.run(
                            [
                                "git", "-C", str(project_root),
                                "reset", "--hard", pre_sha,
                            ],
                            capture_output=True, text=True,
                            check=False, timeout=30,
                        )
                        rollback_note = ""
                        if rollback_result.returncode != 0:
                            rollback_note = (
                                f"\n\nWARNING: Rollback to {pre_sha[:8]} "
                                f"failed (rc={rollback_result.returncode}): "
                                f"{rollback_result.stderr.strip() or 'unknown'}. "
                                f"Working tree may still contain the "
                                f"guardrail-violating commit. Manual "
                                f"`git reset --hard {pre_sha[:8]}` is required."
                            )
                        else:
                            rollback_note = (
                                f"\n\nThe guardrail-violating commit was "
                                f"rolled back to {pre_sha[:8]}. Please fix "
                                f"the spec files and re-run the merge."
                            )
                        render_text(
                            "REFUSED: Guardrail violations were detected in "
                            "spec files after the merge commit:\n\n"
                            + "\n".join(violations_lines)
                            + rollback_note,
                            title="SE3 Merge — Guardrail Violations (Rolled Back)",
                        )
                        return 1
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                        RuntimeError) as exc:
                    logger.warning(
                        "Guardrails check failed after merge-respond: %s", exc
                    )
                    render_text(
                        "The merge was accepted but the post-merge guardrails "
                        "check could not complete. The guardrail-violating "
                        "commit remains on HEAD — manual review is required.",
                        title="SE3 Merge — Guardrails Check Failed",
                    )
                    return 1

            # Version bump verification: if the resolution touches a version
            # file, verify the version actually advanced from pre-merge.
            # This closes the gap between the orchestrator's main merge path
            # (which calls assert_version_bumped via check_all) and the
            # merge-respond accept path.
            resolved_paths = [f.get("path", "") for f in files]
            version_file_names = {"pyproject.toml", "package.json"}
            if (
                any(os.path.basename(p) in version_file_names for p in resolved_paths)
                and theirs_branch
            ):
                pre_sha = call_data.get("ours_head_sha", "")
                if not pre_sha:
                    try:
                        pre_sha = _first_parent_sha(project_root)
                    except RuntimeError:
                        pre_sha = ""
                if pre_sha:
                    version_issue = _check_version_unchanged(project_root, pre_sha)
                    if version_issue:
                        render_text(
                            f"Post-condition check failed after merge commit: "
                            f"{version_issue}",
                            title="SE3 Merge Error",
                        )
                        return 1

            render_text(
                "Merge conflict resolved and committed successfully."
                + (f"\nFeedback: {feedback}" if feedback else ""),
                title="SE3 Merge — Accepted",
            )
            return 0

        # guardrail_violation type — no auto-write, user must fix manually
        render_text(
            "Guardrail violations must be fixed manually. "
            "Please edit the spec files and re-run the merge."
            + (f"\nFeedback: {feedback}" if feedback else ""),
            title="SE3 Merge — Accepted (Manual Fix Required)",
        )
        return 0

    if choice == "abort":
        if call_type == "guardrail_violation":
            # For guardrail violations, the merge was already rolled back
            # (git reset --hard). Attempting git merge --abort would fail
            # because no merge is in progress. Report clean success instead.
            render_text(
                "Merge aborted. The rollback to pre-merge state is already complete."
                + (f"\nFeedback: {feedback}" if feedback else ""),
                title="SE3 Merge — Aborted",
            )
            return 0

        abort_result = subprocess.run(
            ["git", "-C", str(project_root), "merge", "--abort"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if abort_result.returncode != 0:
            render_text(
                f"git merge --abort failed: {abort_result.stderr.strip()}",
                title="SE3 Merge Error",
            )
            return 1

        render_text(
            "Merge aborted."
            + (f"\nFeedback: {feedback}" if feedback else ""),
            title="SE3 Merge — Aborted",
        )
        return 0

    # choice == "manual"
    #
    # The user has chosen to resolve manually.  We cannot run guardrails
    # right now because the working tree still has unresolved markers
    # (or pre-resolution content).  When the merge touches spec files,
    # exit 0 here would let a SHALL→SHOULD rewrite slip past the spec
    # contract, so we *park* the call file by writing a sidecar marker
    # and ask the user to re-invoke ``se3 merge-respond`` after their
    # commit.  The re-entry path (``_verify_pending_guardrails`` above)
    # then runs ``MergeGuardrailsCheck`` against the new commit and
    # rolls back HEAD on violation.
    spec_paths_for_manual: list[str] = []
    if call_type == "merge_conflict":
        for f in call_data.get("files", []):
            p = f.get("path", "")
            if _is_spec_path(p):
                spec_paths_for_manual.append(p)

    if spec_paths_for_manual:
        # G1: Determine the pre-merge SHA so the post-commit guardrails
        # check has a known-good rollback target.  We use the call
        # file's recorded ``ours_head_sha`` which is the orchestrator's
        # captured pre-merge HEAD.  Falls back to first-parent walk
        # when the field is absent (older call files).
        pre_sha = call_data.get("ours_head_sha", "")
        if not pre_sha:
            try:
                pre_sha = _first_parent_sha(project_root)
            except RuntimeError:
                pre_sha = ""

        if not pre_sha:
            render_text(
                "Cannot park manual resolution: pre-merge SHA is unknown "
                "and HEAD has no parents. Please resolve manually and "
                "run `se3 guardrails` on each spec file before re-merging.",
                title="SE3 Merge Error",
            )
            return 1

        parent_count = _head_parent_count(project_root)
        if parent_count is None:
            logger.warning(
                "merge-respond: could not determine HEAD parent count "
                "(rev-list failed). Octopus merges may go undetected; the "
                "parked marker uses first-parent as the rollback target."
            )
        elif parent_count > 2:
            logger.warning(
                "merge-respond: HEAD is an octopus merge (%d parents). "
                "The parked marker uses first-parent as rollback target; "
                "changes from other merged branches are also in the ancestry.",
                parent_count,
            )

        marker_path = Path(str(call_path) + _PENDING_GUARDRAILS_SUFFIX)
        marker_data = {
            "pre_sha": pre_sha,
            "spec_paths": spec_paths_for_manual,
        }
        try:
            marker_path.write_text(
                json.dumps(marker_data, indent=2), encoding="utf-8",
            )
        except OSError as exc:
            render_text(
                f"Failed to write pending-guardrails marker {marker_path}: "
                f"{exc}.\nResolve manually and run `se3 guardrails` on "
                f"each spec file: {', '.join(spec_paths_for_manual)}",
                title="SE3 Merge Error",
            )
            return 1

        render_text(
            "This merge touches spec files:\n"
            + "\n".join(f"  - {p}" for p in spec_paths_for_manual)
            + "\n\nResolve manually, then commit:\n"
            "  git add . && git commit\n\n"
            f"After committing, RE-RUN this command to enforce the spec "
            f"contract:\n"
            f"  se3 merge-respond {call_path}\n\n"
            "If guardrails detect a weakened requirement or deleted "
            "scenario, the commit will be rolled back automatically."
            + (f"\nFeedback: {feedback}" if feedback else ""),
            title="SE3 Merge — Manual Resolution (Parked)",
        )
        return 0

    render_text(
        "Please resolve the conflicts manually, then run:\n"
        "  git add . && git commit"
        + (f"\nFeedback: {feedback}" if feedback else ""),
        title="SE3 Merge — Manual Resolution",
    )
    return 0
