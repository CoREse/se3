"""SE3 Merge-Respond command — Process MCP call response files for merge conflicts.

Usage:
    luo merge-respond <call-file-path>
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

from ..engine.display import render_text
from ..engine.merge.human_call import NO_ACTIVE_MERGE_CALL_TYPES
from ..i18n import t

logger = logging.getLogger(__name__)

_STRICT_SENTINEL = "[__SE3_STRICT_PLACEHOLDER__:"


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
        version field is missing). A merge that broke the version file's
        syntax must NOT be silently treated as a no-op:
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
            # This sentence is embedded in the localized postcondition-failure
            # block shown to the user, so it renders through i18n too.
            return t("merge_respond.version_unparseable", filename=filename)
        if (
            pre_version is not None
            and post_version is not None
            and pre_version == post_version
        ):
            return t(
                "merge_respond.version_unchanged",
                filename=filename,
                version=pre_version,
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
    # section so a concurrent ``luo merge <branch>`` started by another
    # shell cannot race with us over the working tree, the index,
    # ``git commit --no-edit`` and ``git reset --hard``. Use the
    # context-manager form so an exception
    # raised between MergeLock construction and the try/finally entry
    # (e.g. a lazy import inside _process_merge_response_locked failing
    # before the function body executes) cannot leak the lock — the
    # ``with`` statement binds acquire and release into the same scope.
    #
    # The lock is the project-wide "main-worktree mutex" shared by every
    # merge-completing path (``run_merge``, the orchestrator, and the
    # synchronous ``luo run`` flow), so acquire it in BLOCKING mode
    # (``blocking=True``): an operator answering a paused merge with
    # ``luo merge-respond`` must QUEUE behind a running synchronous
    # ``luo run`` or another ``luo merge`` and complete once that holder
    # releases, rather than fail fast with MergeLockBusy. Blocking mode
    # relies on the kernel releasing an flock when the holder process
    # exits, so a crashed holder cannot wedge the queue and no PID
    # stale-break path is needed; MergeLockBusy / MergeLockStale are not
    # raised on this path, but the handlers are retained as defensive
    # fallbacks.
    from .merge.merge_lock import MergeLock, MergeLockBusy, MergeLockStale
    from .run import _resolve_main_lock_root

    call_path = Path(call_file)
    if not call_path.exists():
        render_text(
            t("merge_respond.call_file_not_found", call_path=call_path),
            title=t("merge_respond.title.error"),
        )
        return 1

    # Resolve the lock target back to the main repository so that a
    # ``luo merge-respond`` invoked with cwd inside a linked worktree still
    # contends on the single project-wide ``<main_repo>/tianluo/state/merge.lock``
    # — the same lock a synchronous ``luo run`` and ``luo merge`` acquire.
    lock_root = _resolve_main_lock_root(project_root)

    try:
        with MergeLock(lock_root, blocking=True):
            return _process_merge_response_locked(
                call_path=call_path, project_root=project_root,
            )
    except MergeLockBusy as exc:
        render_text(
            t(
                "merge_respond.lock_busy",
                holder_pid=exc.holder_pid,
                lock_file=exc.lock_file,
            ),
            title=t("merge_respond.title.error"),
        )
        return 1
    except MergeLockStale as exc:
        if exc.holder_pid is None:
            pid_msg = t("merge_respond.stale_pid_unparseable")
        else:
            pid_msg = t("merge_respond.stale_pid_missing", holder_pid=exc.holder_pid)
        render_text(
            t("merge_respond.lock_stale", pid_msg=pid_msg, lock_file=exc.lock_file),
            title=t("merge_respond.title.error"),
        )
        return 1


def _process_merge_response_locked(
    *,
    call_path: Path,
    project_root: Path,
) -> int:
    """Body of :func:`process_merge_response` executed under the merge lock."""

    response_path = Path(str(call_path) + ".response")
    if not response_path.exists():
        render_text(
            t("merge_respond.response_file_not_found", response_path=response_path),
            title=t("merge_respond.title.error"),
        )
        return 1

    try:
        call_data = json.loads(call_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        render_text(
            t("merge_respond.call_file_parse_failed", exc=exc),
            title=t("merge_respond.title.error"),
        )
        return 1

    try:
        response_data = json.loads(response_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        render_text(
            t("merge_respond.response_file_parse_failed", exc=exc),
            title=t("merge_respond.title.error"),
        )
        return 1

    choice = response_data.get("choice", "").strip().lower()
    feedback = response_data.get("feedback", "")

    if choice not in ("accept", "abort", "manual"):
        render_text(
            t("merge_respond.invalid_choice", choice=choice),
            title=t("merge_respond.title.error"),
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
                    t(
                        "merge_respond.strict_placeholder",
                        affected_files=", ".join(sentinel_files),
                    ),
                    title=t("merge_respond.title.strict_placeholder"),
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
                    t("merge_respond.write_resolved_failed", exc=exc),
                    title=t("merge_respond.title.error"),
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
                    t("merge_respond.commit_failed", stderr=commit_result.stderr.strip()),
                    title=t("merge_respond.title.error"),
                )
                return 1

            # B1 post-condition: verify the branch is actually an ancestor
            # of HEAD and that HEAD is a merge commit.  This catches the
            # case where the user committed only a subset of files or
            # MERGE_HEAD was already cleared.
            theirs_branch = call_data.get("theirs_branch", "")
            if theirs_branch:
                try:
                    from tianluo.commands.merge.postcondition import (
                        PostConditionViolated,
                        assert_branch_merged,
                        assert_head_is_merge_commit,
                    )
                    assert_branch_merged(project_root, theirs_branch, timeout=15)
                    assert_head_is_merge_commit(project_root, theirs_branch, timeout=15)
                except PostConditionViolated as pc_exc:
                    render_text(
                        t("merge_respond.postcondition_failed", detail=pc_exc),
                        title=t("merge_respond.title.error"),
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
                            t("merge_respond.postcondition_failed", detail=version_issue),
                            title=t("merge_respond.title.error"),
                        )
                        return 1

            render_text(
                t("merge_respond.accepted_committed")
                + (t("merge_respond.feedback_suffix", feedback=feedback) if feedback else ""),
                title=t("merge_respond.title.accepted"),
            )
            return 0

        # Any other call type carries no per-file resolution to write back,
        # so acceptance is only an acknowledgement — the user fixes by hand.
        render_text(
            t("merge_respond.accept_manual_fix")
            + (t("merge_respond.feedback_suffix", feedback=feedback) if feedback else ""),
            title=t("merge_respond.title.accepted_manual_fix"),
        )
        return 0

    if choice == "abort":
        if call_type in NO_ACTIVE_MERGE_CALL_TYPES:
            # These call files are only produced after the merge was already
            # aborted or rolled back, so `git merge --abort` would fail with
            # "no merge to abort". Report clean success instead.
            render_text(
                t("merge_respond.aborted_already_rolledback")
                + (t("merge_respond.feedback_suffix", feedback=feedback) if feedback else ""),
                title=t("merge_respond.title.aborted"),
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
                t("merge_respond.abort_failed", stderr=abort_result.stderr.strip()),
                title=t("merge_respond.title.error"),
            )
            return 1

        render_text(
            t("merge_respond.aborted")
            + (t("merge_respond.feedback_suffix", feedback=feedback) if feedback else ""),
            title=t("merge_respond.title.aborted"),
        )
        return 0

    # choice == "manual"
    render_text(
        t("merge_respond.manual_resolve")
        + (t("merge_respond.feedback_suffix", feedback=feedback) if feedback else ""),
        title=t("merge_respond.title.manual_resolution"),
    )
    return 0
