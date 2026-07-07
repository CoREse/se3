"""Commit step handler.

Commits the changes using git.
Integrates with VersionBumper for automatic version bumping — but only for a
synchronous (non-worktree) flow, whose commit is the release point. Such a
commit consumes the authoritative ``suggested_version`` from the
version_analyze step and writes it verbatim to the project version file. A
worktree flow's commit is de-versioned (the merge is its release point): it
writes no version file / VERSIONS.md / ``Version:`` line and carries only the
``bump_type`` message decoration. ``bump_type`` is otherwise read only for
commit-message decoration and template summary display.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path

from ..models import FlowInstance, Step, StepStatus, StepType
from ..version_bumper import VersionBumper, VersionConfig

logger = logging.getLogger(__name__)

# The finite, known closed set of se3 runtime subtree names that live under
# the project's sole ignored runtime root ``se3/`` (no leading dot) and are
# covered by the ``/se3/*`` gitignore rule. Every se3 runtime artifact MUST
# land inside ``se3/<subtree>/``; a path carrying one of these subtree names
# but anchored OUTSIDE the top-level ``se3/`` root (e.g. under ``.se3/`` or a
# nested ``foo/se3/logs/...``) is a runtime leak that should never be
# committed. This is a pure-path signature — no content-based classification.
_RUNTIME_SUBTREES = frozenset(
    {"cache", "history", "logs", "state", "tmp", "worktrees", "calls", "collab"}
)


def _detect_runtime_leaks(staged_paths: list[str]) -> list[str]:
    """Return staged paths that carry an se3 runtime signature outside ``se3/``.

    Pure path判断, no IO. ``staged_paths`` are repo-root-relative, posix-style
    paths (as emitted by ``git diff --cached --name-only``). A path is a leak
    when:

    * (A) its top-level component is ``.se3`` — the dotted runtime root is the
      mistyped/illegitimate location that leaks (never gitignored), so any
      path under it is a leak; OR
    * (B) some NON-top-level component is ``se3`` or ``.se3`` and its
      immediately following component is one of the closed-set runtime
      subtree names in :data:`_RUNTIME_SUBTREES` (e.g. ``foo/se3/logs/x`` or
      ``.se3/archive/<slug>/se3/state/engine.json``).

    A path whose top-level component is exactly ``se3`` is always exempt —
    it is either gitignored (``/se3/*``) or an explicitly whitelist-tracked
    artifact (``se3/specs/``, ``se3/issues/`` …) and is normal working
    output. Source code where ``se3`` is merely a package directory
    (``src/se3/engine/...``) is also exempt because the component following
    ``se3`` is not a runtime subtree name.

    Args:
        staged_paths: Repo-root-relative staged path strings.

    Returns:
        The subset of ``staged_paths`` identified as runtime leaks, in input
        order (verbatim, so callers can pass them straight to ``git``).
    """
    leaks: list[str] = []
    for raw in staged_paths:
        path = raw.strip()
        if not path:
            continue
        parts = [p for p in path.replace("\\", "/").split("/") if p and p != "."]
        if not parts:
            continue
        top = parts[0]
        # The legitimate runtime root is the top-level ``se3/`` — exempt.
        if top == "se3":
            continue
        # Rule (A): a top-level ``.se3/`` is never legitimate.
        if top == ".se3":
            leaks.append(raw)
            continue
        # Rule (B): a non-top-level ``se3``/``.se3`` immediately followed by a
        # known runtime subtree name.
        for i in range(1, len(parts) - 1):
            if parts[i] in ("se3", ".se3") and parts[i + 1] in _RUNTIME_SUBTREES:
                leaks.append(raw)
                break
    return leaks


def _strip_runtime_leaks(project_root: Path) -> bool:
    """Soft-remove runtime-signature paths leaking outside ``se3/`` (scheme B).

    Runs between ``git add -A`` and ``git commit``: reads the staged path
    list, identifies leaks via :func:`_detect_runtime_leaks`, unstages them
    (``git restore --staged``) and logs a WARNING. The commit then proceeds
    with the remaining staged content.

    This is a regression backstop, NOT a gate: every git subprocess here is
    fault-tolerant — any failure (reading the staged list, unstaging) only
    warns and lets the commit continue. It never raises and never causes the
    commit step to fail.

    Args:
        project_root: Project root directory (git cwd).

    Returns:
        ``True`` if at least one leaked path was detected (and an unstage was
        attempted), ``False`` otherwise. The caller uses this to decide
        whether it needs to re-check for an emptied index (so that stripping
        leaks never turns into a failed ``git commit``).
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "-z"],
            capture_output=True,
            text=True,
            cwd=project_root,
        )
        if result.returncode != 0:
            logger.warning(
                "Runtime-leak guard: could not list staged paths (%s); "
                "skipping leak check",
                result.stderr.strip(),
            )
            return False
        staged = [p for p in result.stdout.split("\0") if p]
    except Exception as exc:
        logger.warning(
            "Runtime-leak guard: listing staged paths raised (%s); "
            "skipping leak check",
            exc,
        )
        return False

    leaks = _detect_runtime_leaks(staged)
    if not leaks:
        return False

    logger.warning(
        "Runtime-leak guard: unstaging %d path(s) carrying an se3 runtime "
        "signature outside the ignored se3/ root (soft-removed from this "
        "commit): %s",
        len(leaks),
        ", ".join(sorted(leaks)),
    )
    try:
        unstage = subprocess.run(
            ["git", "restore", "--staged", "--", *leaks],
            capture_output=True,
            text=True,
            cwd=project_root,
        )
        if unstage.returncode != 0:
            logger.warning(
                "Runtime-leak guard: failed to unstage leaked paths (%s); "
                "commit proceeds without removing them",
                unstage.stderr.strip(),
            )
    except Exception as exc:
        logger.warning(
            "Runtime-leak guard: unstaging raised (%s); commit proceeds",
            exc,
        )
    return True


def _index_has_staged_changes(project_root: Path) -> bool:
    """Return whether anything remains staged for the next commit.

    Used after :func:`_strip_runtime_leaks` to detect the case where the only
    staged content was runtime leaks — stripping them leaves an empty index,
    which would otherwise make ``git commit`` fail with "nothing to commit".

    Fault-tolerant: any subprocess error is treated as "assume there is
    something to commit" (``True``) so the guard never short-circuits a
    legitimate commit on a transient git error.

    Args:
        project_root: Project root directory (git cwd).

    Returns:
        ``True`` if the index has staged changes (or the check could not be
        performed), ``False`` only when the index is confirmed empty.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "-z"],
            capture_output=True,
            text=True,
            cwd=project_root,
        )
        if result.returncode != 0:
            return True
        return any(p for p in result.stdout.split("\0") if p)
    except Exception as exc:
        logger.warning(
            "Runtime-leak guard: post-strip index check raised (%s); "
            "assuming there is something to commit",
            exc,
        )
        return True


def _root_deny_excludes(project_root: Path, path: str) -> bool:
    """Return whether ``path`` is ignored *specifically* by the root ``/*`` rule.

    Confirms the matching gitignore rule via ``git check-ignore -v`` and checks
    that the winning pattern is exactly ``/*`` — the root default-deny line. A
    path ignored by some other rule (e.g. ``/se3/*`` or ``*.pyc``) is NOT a
    root-whitelist exclusion and is intentionally left out: those are ordinary,
    expected ignores, not the "stray top-level artifact the whitelist swept up"
    case this guard exists to surface.

    Fully fault-tolerant: any subprocess error is treated as "not a root-deny
    exclusion" (``False``) so the guard never raises.
    """
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-v", "--", path],
            capture_output=True,
            text=True,
            cwd=project_root,
        )
    except Exception:
        return False
    # check-ignore -v prints "<source>:<linenum>:<pattern>\t<pathname>" for an
    # ignored path, and nothing (exit 1) for a non-ignored one. The pattern is
    # the last colon-delimited field of the rule (source/linenum precede it),
    # so rsplit isolates it without tripping on a ``:`` in the source filename.
    line = result.stdout.strip()
    if not line:
        return False
    rule = line.split("\t", 1)[0]
    pattern = rule.rsplit(":", 1)[-1].strip()
    return pattern == "/*"


def _detect_root_whitelist_exclusions(project_root: Path) -> list[str]:
    """Warn about top-level new paths the root ``/*`` default-deny rule excluded.

    Runs between ``git add -A`` and ``git commit`` on the canonical commit path.
    The root ``/*`` whitelist convention turns the repo root into default-deny:
    a freshly-created top-level file/dir that lacks a matching ``!/<name>``
    whitelist line is silently *not* tracked, so ``git add -A`` never stages it.
    This helper makes that silence loud — it enumerates the top-level untracked
    paths excluded *specifically* by ``/*`` and emits a single WARNING listing
    them, so a human notices the dropped work instead of it vanishing.

    Deliberately diagnostic-only: it does NOT touch ``.gitignore``, does NOT
    add ``!/<name>`` whitelist lines, and does NOT alter staging. Auto-
    whitelisting whatever the agent happened to drop would re-admit exactly the
    stray artifacts the whitelist is meant to block, defeating its purpose. The
    load-bearing defense is the whitelist itself; this guard only restores
    visibility.

    Like :func:`_strip_runtime_leaks`, it is a soft backstop: every git
    subprocess is fully fault-tolerant (any failure only warns and returns an
    empty list). It never raises and never blocks or fails the commit.

    Args:
        project_root: Project root directory (git cwd).

    Returns:
        The top-level path strings excluded by the root ``/*`` rule (sorted),
        or an empty list when there are none / the check could not run. The
        return value is for logging/diagnostics only — it never participates in
        commit control flow.
    """
    try:
        # ``--directory`` collapses an entirely-ignored top-level dir to a
        # single ``name/`` entry instead of recursing into every file inside
        # it; ``--ignored --others --exclude-standard`` lists exactly the
        # untracked-but-ignored paths (the ones ``git add -A`` skipped).
        result = subprocess.run(
            [
                "git",
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "--directory",
            ],
            capture_output=True,
            text=True,
            cwd=project_root,
        )
        if result.returncode != 0:
            logger.warning(
                "Root-whitelist guard: could not list ignored paths (%s); "
                "skipping root-exclusion check",
                result.stderr.strip(),
            )
            return []
        entries = [p for p in result.stdout.splitlines() if p.strip()]
    except Exception as exc:
        logger.warning(
            "Root-whitelist guard: listing ignored paths raised (%s); "
            "skipping root-exclusion check",
            exc,
        )
        return []

    excluded: list[str] = []
    for raw in entries:
        entry = raw.strip()
        # Only the top level: ``/*`` governs root entries, and an ``!/<name>``
        # that re-admits a dir lets its interior follow normal gitignore rules.
        # A trailing slash marks a directory entry from ``--directory``.
        inner = entry.rstrip("/")
        if not inner or "/" in inner:
            continue
        if _root_deny_excludes(project_root, entry):
            excluded.append(inner)

    if excluded:
        excluded = sorted(excluded)
        logger.warning(
            "Root-whitelist guard: %d top-level path(s) are excluded by the "
            "root '/*' default-deny rule and were NOT committed; add a "
            "'!/<name>' whitelist line to .gitignore if they should be "
            "tracked: %s",
            len(excluded),
            ", ".join(excluded),
        )
    return excluded


def commit_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the commit step.

    Commits changes using git commands. If version bumping is enabled AND this
    flow is a synchronous (non-worktree) run — whose commit is the release
    point — it writes the authoritative ``suggested_version`` produced by the
    preceding version_analyze step to the project version file, updates
    VERSIONS.md, and stamps a ``Version:`` line, before committing. Just before
    writing, and while holding the merge lock, it re-checks the disk version
    against the version version_analyze observed on disk (its ``current_version``,
    NOT the pre-session baseline — :func:`_guard_version_race`); if a concurrent
    direct-run flow bumped it first, version_analyze is re-run against the drifted
    baseline so the two flows do not land on the same number. A worktree flow's commit is de-versioned: it writes no version
    file, no VERSIONS.md entry, and no ``Version:`` line (the version decision
    is deferred to the merge-side reconcile), carrying only the bump-intent
    message decoration.

    Args:
        step: The current step being executed
        flow: The flow instance containing context

    Returns:
        StepStatus.COMPLETED on success, StepStatus.FAILED on error
    """
    project_root = flow.change_path.parent if flow.change_path else Path.cwd()

    # Check if there are changes to commit
    baseline_commit = getattr(flow, "baseline_commit", None)
    if not _has_changes(project_root, baseline_commit=baseline_commit):
        logger.info("No changes to commit")
        # A top-level path excluded by the root ``/*`` default-deny rule is
        # invisible to ``git status``/``git diff`` (it is ignored, not
        # untracked-and-visible), so a new root file like ``notes.txt`` can be
        # the *only* work present yet still register as "no changes". Run the
        # root-whitelist guard here too — otherwise the dropped work stays
        # silent precisely in the case the guard exists to surface. Pure
        # diagnostic, fully fault-tolerant: it only warns, never blocks.
        _detect_root_whitelist_exclusions(project_root)
        step.outputs["commit_hash"] = "no-changes"
        step.outputs["committed"] = False
        return StepStatus.COMPLETED

    # Load version bumping configuration
    version_config = _load_version_config(project_root)

    # De-versioning split (accident-driven, 2026-07-06): a worktree session's
    # commit is NOT its release point — the merge is. So it must not write the
    # version file, must not touch VERSIONS.md, and must not stamp a
    # `Version: X.Y.Z` message line (the final version is unknowable here and
    # would collide with a concurrent worktree flow's identical write). It
    # carries only the bump *intent* (the `(minor bump)` message decoration and
    # the branch-committed VersionIntent from version_analyze); the merge-side
    # version_reconcile step writes the authoritative version. A non-worktree
    # (synchronous) flow's commit IS the release point, so it keeps writing the
    # version verbatim — behaviour unchanged. By skipping the whole bump block,
    # `new_version` stays None, which in turn suppresses the docs update and the
    # `Version:` message line downstream with no further branching.
    is_worktree = getattr(flow, "is_worktree_mode", False)

    # Initialize version bumping state
    version_bumper: VersionBumper | None = None
    version_file: Path | None = None
    original_version: str | None = None
    new_version: str | None = None
    version_bumped = False

    try:
        # Attempt version bumping if enabled (and this commit is a release
        # point — worktree flows defer versioning to the merge).
        if version_config.enabled and not is_worktree:
            version_bumper = VersionBumper(version_config)
            version_file = version_bumper.detect_version_file(project_root)

            # Resolve target version up front — this is the authoritative
            # value from version_analyze. Raises RuntimeError if missing or
            # if version_analyze failed, halting the commit.
            target_version = _resolve_target_version(step, flow)

            if version_file:
                try:
                    # Save original version for potential rollback
                    original_version = version_bumper.read_version(version_file)
                except (ValueError, KeyError, RuntimeError):
                    # File exists but has no readable version — auto-repair
                    logger.warning(
                        f"Version file {version_file} exists but has no readable version. "
                        "Attempting auto-repair."
                    )
                    if version_bumper._use_script_mode and version_bumper._script_runner:
                        # Script mode: regenerate version script
                        logger.info("Script mode detected, regenerating version script.")
                        from ..version_script_interface import generate_version_script
                        generate_version_script(project_root)
                    else:
                        # File mode: reinitialize version system
                        logger.info("File mode detected, reinitializing version system.")
                        version_file = version_bumper.initialize_version_system(
                            project_root=project_root,
                            initial_version="0.1.0"
                        )
                    # Retry — let any exception propagate normally
                    original_version = version_bumper.read_version(version_file)

                # Concurrency race guard (change D, accident-driven 2026-07-06):
                # this synchronous commit is the release point and the merge
                # lock is already held for the whole run, but ``target_version``
                # (version_analyze's suggested_version) was computed earlier,
                # against the then-current disk version. ``original_version`` is
                # the version re-read just now, in-lock. If a concurrent
                # direct-run flow grabbed the lock first and bumped between our
                # version_analyze and here, disk has drifted ahead of the
                # pre-session baseline — writing our stale target would land the
                # same number twice (the 10.7.1 double-bump). On drift, recompute
                # the target against the drifted disk baseline so we advance past
                # it instead of colliding.
                target_version = _guard_version_race(
                    step, flow, original_version, target_version,
                    version_file=version_file,
                    version_bumper=version_bumper,
                )

                # Write the authoritative target version directly
                new_version = version_bumper.set_version(
                    version=target_version,
                    path=version_file,
                )
                version_bumped = True
                logger.info(f"Set version: {original_version} -> {new_version}")

                # Stage the version file
                _stage_file(project_root, version_file)
            else:
                # No version file exists - initialize version system
                logger.info("No version file detected, initializing version system")
                try:
                    version_file = version_bumper.initialize_version_system(
                        project_root=project_root,
                        initial_version="0.1.0"
                    )
                    logger.info(f"Created version file: {version_file}")

                    # Save original version for potential rollback
                    try:
                        original_version = version_bumper.read_version(version_file)
                    except (ValueError, KeyError, RuntimeError):
                        logger.warning(
                            f"Freshly created version file {version_file} is not readable. "
                            "Attempting auto-repair."
                        )
                        if version_bumper._use_script_mode and version_bumper._script_runner:
                            logger.info("Script mode detected, regenerating version script.")
                            from ..version_script_interface import generate_version_script
                            generate_version_script(project_root)
                        else:
                            logger.info("File mode detected, reinitializing version system.")
                            version_file = version_bumper.initialize_version_system(
                                project_root=project_root,
                                initial_version="0.1.0"
                            )
                        # Retry — let any exception propagate normally
                        original_version = version_bumper.read_version(version_file)

                    # Write the authoritative target version directly
                    new_version = version_bumper.set_version(
                        version=target_version,
                        path=version_file,
                    )
                    version_bumped = True
                    logger.info(f"Set version: {original_version} -> {new_version}")

                    # Stage the new version file
                    _stage_file(project_root, version_file)
                except Exception as e:
                    logger.error(f"Failed to initialize version system: {e}")
                    raise

        # Generate commit message (including version if bumped)
        commit_message = _generate_commit_message(flow, step, new_version, version_config)

        logger.info(f"Committing changes with message: {commit_message[:60]}...")

        # Auto-update README badge + VERSIONS.md changelog entry.
        #
        # This is a deterministic, mechanical write: the LLM already decided
        # the changelog *content* upstream in the version_analyze step (the
        # forwarded ``versions_changes``); here we only apply the badge swap
        # and prepend the VERSIONS entry via the already-tested
        # DocumentationUpdater. Documentation failures must NEVER block the
        # commit, so the entire block is best-effort and only runs once a
        # real version bump has been applied and staged.
        if version_bumped and new_version:
            _update_docs(project_root, new_version, step, commit_message)

        # Write-side freshness boundary: regenerate the code-index just before
        # staging so the committed `se3/code-index.md` folds in the code this
        # flow's implement step wrote. This is the ONLY refresh point after code
        # changes (the read-side refresh in analyze runs before implement); skip
        # it and every committed map would lag one flow behind. Best-effort — a
        # rebuild hiccup must never block the commit (it only reads + writes the
        # tracked map under se3/, which `git add -A` then stages).
        from ..context_builder import ensure_code_index_fresh
        # Pass the flow/step context so the rebuild streams per-node progress to
        # the running flow's web console (chat_history.record_index_progress);
        # without it the refresh stays silent as on the read side. The context is
        # a best-effort progress channel, so identifiers are read defensively —
        # a stray flow/step missing one just falls back to a silent refresh
        # rather than aborting the commit.
        _step_type_val = getattr(getattr(step, "step_type", None), "value", None)
        ensure_code_index_fresh(
            project_root,
            flow_id=getattr(flow, "flow_id", None),
            step_id=getattr(step, "step_id", None),
            step_type=_step_type_val or "commit",
        )

        # Add all changes
        result = subprocess.run(
            ["git", "add", "-A"],
            capture_output=True,
            text=True,
            cwd=project_root,
        )

        if result.returncode != 0:
            # Rollback version if staging failed
            if version_bumped and version_bumper and version_file and original_version:
                _rollback_version(version_bumper, version_file, original_version)
            step.error_message = f"Failed to stage changes: {result.stderr}"
            return StepStatus.FAILED

        # Runtime-leak guard (scheme B, regression backstop): after staging
        # the full tree, soft-remove any staged path that carries an se3
        # runtime signature but lives OUTSIDE the sole ignored runtime root
        # ``se3/`` (e.g. a stray ``.se3/archive/...``). Such paths are
        # unstaged and a WARNING is logged; the commit then proceeds with the
        # remaining (legitimate) staged content. Entirely fault-tolerant — it
        # never blocks or fails the commit.
        stripped_leaks = _strip_runtime_leaks(project_root)

        # Root-whitelist guard: under the root ``/*`` default-deny convention,
        # a new top-level path with no ``!/<name>`` whitelist line is silently
        # not tracked, so ``git add -A`` never staged it. Surface that as a
        # WARNING so dropped work is loud, not silent. Pure diagnostic — it
        # touches neither .gitignore nor staging, and its return value never
        # feeds control flow, so the commit proceeds identically either way.
        _detect_root_whitelist_exclusions(project_root)

        # If stripping leaks emptied the index (the only working-tree change
        # was a runtime leak outside se3/), there is nothing legitimate left
        # to commit. The leak guard is a soft backstop — it must never turn a
        # commit into a failure. Treat this exactly like the upfront "No
        # changes to commit" no-op path rather than letting ``git commit`` fail
        # with "nothing to commit".
        if stripped_leaks and not _index_has_staged_changes(project_root):
            logger.info(
                "Runtime-leak guard: all staged changes were runtime leaks "
                "outside se3/; nothing left to commit (treating as no-op "
                "success)"
            )
            # Roll back any version bump that was applied/staged — it will not
            # be committed, so the version file must not be left dirty.
            if version_bumped and version_bumper and version_file and original_version:
                _rollback_version(version_bumper, version_file, original_version)
            step.outputs["commit_hash"] = "no-changes"
            step.outputs["committed"] = False
            return StepStatus.COMPLETED

        # Commit
        result = subprocess.run(
            ["git", "commit", "-m", commit_message],
            capture_output=True,
            text=True,
            cwd=project_root,
        )

        if result.returncode != 0:
            # Rollback version if commit failed
            if version_bumped and version_bumper and version_file and original_version:
                _rollback_version(version_bumper, version_file, original_version)
            step.error_message = f"Failed to commit: {result.stderr}"
            return StepStatus.FAILED

        # Get commit hash
        commit_hash = _get_commit_hash(project_root)

        # Clear version backup on successful commit (make bump permanent)
        if version_bumper:
            version_bumper.clear_backup()

        # Store outputs
        step.outputs["commit_hash"] = commit_hash
        step.outputs["committed"] = True
        step.outputs["commit_message"] = commit_message
        if new_version:
            step.outputs["version"] = new_version
            step.outputs["version_bumped"] = True
            # Durable own-replay marker for the version race guard: record the
            # version this flow just committed onto the flow's persisted state so
            # a later re-entry of the commit step can tell its own already-landed
            # bump apart from a concurrent flow's — the only signal that works in
            # script mode (no reconstructable version-file blob) and under
            # version.include_in_commit_message: false (no Version: trailer). See
            # _guard_version_race.
            _record_flow_committed_version(flow, new_version)

        logger.info(f"Changes committed: {commit_hash[:8]}")

        # Generate template summary when summarize step is not in the flow
        if StepType.SUMMARIZE not in flow.state.selected_steps:
            try:
                _generate_template_summary(flow, step)
            except Exception as e:
                logger.warning(f"Failed to generate template summary: {e}")

        return StepStatus.COMPLETED

    except Exception as e:
        # Rollback version on any exception
        if version_bumped and version_bumper and version_file and original_version:
            try:
                _rollback_version(version_bumper, version_file, original_version)
            except Exception as rollback_error:
                logger.error(f"Failed to rollback version: {rollback_error}")

        logger.exception("Commit step failed")
        step.error_message = f"Failed to commit: {str(e)}"
        return StepStatus.FAILED


def _load_version_config(project_root: Path) -> VersionConfig:
    """Load version bumping configuration.

    Args:
        project_root: Project root directory

    Returns:
        VersionConfig instance
    """
    # Import here to avoid circular imports
    from ...config import load_version_config as load_cfg
    return load_cfg(project_root)


def _resolve_target_version(step: Step, flow: FlowInstance) -> str:
    """Resolve the authoritative target version from the version_analyze step.

    The version_analyze step's ``suggested_version`` is the sole authority on
    the new version number — this function reads it from ``step.inputs``
    (forwarded by the state machine) with a fallback to the most recent
    version_analyze step's outputs. If the version_analyze step is FAILED, or
    no ``suggested_version`` is available, a ``RuntimeError`` is raised so
    the commit step halts instead of inventing a version.

    Args:
        step: The commit step (its ``inputs`` carry forwarded version_analyze
            outputs)
        flow: The flow instance — used to locate the version_analyze step for
            status and current_version

    Returns:
        The authoritative version string to write.

    Raises:
        RuntimeError: When the version_analyze step failed or did not produce
            a ``suggested_version``. The message names the current version
            (when known) and directs the user toward human intervention.
    """
    # Locate the most recent version_analyze step (if any) for status and
    # current_version context.
    va_step: Step | None = None
    for step_id in reversed(flow.state.step_history):
        s = flow.state.steps.get(step_id)
        if s and s.step_type == StepType.VERSION_ANALYZE:
            va_step = s
            break

    suggested = step.inputs.get("suggested_version")
    if not suggested and va_step is not None:
        suggested = va_step.outputs.get("suggested_version")

    current_version = (
        (va_step.outputs.get("current_version") if va_step else None)
        or step.inputs.get("current_version")
        or "<unknown>"
    )

    if va_step is not None and va_step.status == StepStatus.FAILED:
        raise RuntimeError(
            f"version_analyze step failed; cannot determine target version "
            f"(current_version='{current_version}'). "
            "Provide a version via human intervention: rerun the version_analyze "
            "step, or create a human call under se3/calls/ to supply the version "
            "manually."
        )

    if not isinstance(suggested, str) or not suggested.strip():
        raise RuntimeError(
            "version_analyze did not produce a suggested_version "
            f"(current_version='{current_version}'). "
            "The commit step requires an explicit target version. "
            "Provide one via human intervention: rerun the version_analyze "
            "step, or create a human call under se3/calls/ to supply the "
            "version manually."
        )

    return suggested.strip()


def _normalize_version(value: str | None) -> str:
    """Normalize a version string for equality comparison.

    Strips surrounding whitespace and a leading ``v``/``V`` prefix so a raw
    disk read (``10.7.0``) and a baseline that happens to carry a ``v`` prefix
    compare equal. Non-strings collapse to ``""`` (never equal to a real
    version), so a missing baseline can never masquerade as a match.
    """
    if not isinstance(value, str):
        return ""
    return value.strip().lstrip("vV")


def _resolve_analyze_baseline(step: Step, flow: FlowInstance) -> str | None:
    """Resolve the disk version the race guard compares the in-lock read against.

    Per the drift model (改动 D): drift means *another concurrent flow bumped
    the version file out from under us*. The authoritative reference for "what
    the version file held when our decision was made" is the disk version
    version_analyze actually observed — its ``current_version`` output. That
    value already folds in THIS flow's own session/implement-phase commits
    (version_analyze reads disk after them), so those advances are, by
    construction, not drift.

    ``pre_session_version`` is deliberately NOT the drift criterion: it predates
    this flow's own session commits, so comparing against it would misread the
    flow's own already-accounted bump as concurrent drift. It is kept only as an
    audit/diagnostic fallback for the pathological case where version_analyze
    recorded no ``current_version`` at all.

    Returns ``None`` when neither is available, in which case the guard has no
    reference and leaves the target untouched.
    """
    for step_id in reversed(flow.state.step_history):
        s = flow.state.steps.get(step_id)
        if s and s.step_type == StepType.VERSION_ANALYZE:
            current = s.outputs.get("current_version")
            if isinstance(current, str) and current.strip():
                stripped = current.strip()
                # ``_get_current_version`` emits ``unknown`` / ``unknown (...)``
                # sentinels on a transient version-file detection failure. A
                # sentinel is NOT an observed disk version — it can never equal
                # the real in-lock read, so using it as the drift reference would
                # spuriously declare concurrent drift and burn a non-deterministic
                # LLM re-analysis inside the global merge lock. Treat it exactly
                # like a missing baseline and fall through to the fallback.
                if not stripped.lower().startswith("unknown"):
                    return stripped
            break

    # Fallback only: version_analyze recorded no observed disk version. This is
    # audit metadata, not a true drift baseline, but it is better than no
    # reference at all.
    baseline = step.inputs.get("pre_session_version")
    if isinstance(baseline, str) and baseline.strip():
        return baseline.strip()
    return None


def _version_at_commit(
    project_root: Path,
    commit: str,
    version_file: Path,
    version_bumper: VersionBumper,
) -> str | None:
    """Parse the version recorded in *version_file* at *commit*, or ``None``.

    Reads the file's blob at that commit (``git show <commit>:<relpath>``) and
    parses it through the SAME handler the commit path uses, so every project
    type / file shape is covered. Any fault (git error, unparseable blob) yields
    ``None`` — the caller treats that as "cannot confirm", never as a match.
    """
    try:
        rel = os.path.relpath(version_file, project_root)
    except ValueError:
        return None
    try:
        blob = subprocess.run(
            ["git", "show", f"{commit}:{rel}"],
            capture_output=True,
            text=True,
            cwd=project_root,
            timeout=15,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if blob.returncode != 0:
        return None
    # Write the blob under the SAME filename so the handler's name/suffix-based
    # can_handle() selects the right parser (pyproject.toml vs package.json vs …).
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / version_file.name
        try:
            tmp.write_text(blob.stdout, encoding="utf-8")
            return version_bumper.read_version(tmp)
        except Exception:  # noqa: BLE001 - an unparseable historical blob is not a match
            return None


def _record_flow_committed_version(flow: FlowInstance, version: str | None) -> None:
    """Persist the version this flow just committed onto its durable state.

    Read back by :func:`_guard_version_race` as a mode-independent own-replay
    signal: it is the only way a re-entered commit step can recognise its own
    already-landed bump in script mode (no reconstructable version-file blob)
    or under ``version.include_in_commit_message: false`` (no ``Version:``
    commit-message trailer). Written on the flow's ``state.context`` so the
    state machine's post-step save carries it into a later resume/fix-loop
    re-entry. Best-effort — a missing state must never fail an otherwise
    successful commit.
    """
    if not version:
        return
    # Best-effort and fully defensive: a state whose ``context`` is missing or
    # not a mutable mapping (e.g. a test double) must never turn an otherwise
    # successful commit into a failure.
    try:
        context = getattr(getattr(flow, "state", None), "context", None)
        if isinstance(context, dict):
            context["flow_committed_version"] = version.strip()
    except Exception:  # noqa: BLE001 - recording is a non-critical optimisation
        logger.debug("Could not record flow-committed version", exc_info=True)


def _flow_wrote_version(
    project_root: Path,
    flow_id: str | None,
    version: str | None,
    version_file: Path | None = None,
    version_bumper: VersionBumper | None = None,
) -> bool:
    """True when this flow's OWN earlier commit already stamped *version*.

    Distinguishes a replay/resume over the flow's own already-accounted session
    commit from a genuine concurrent bump by another direct-run flow. Both leave
    the disk version ahead of the pre-session baseline, but only our own commit
    carries THIS flow's ``Flow: <flow_id>`` trailer (always stamped by
    :func:`_generate_commit_message`).

    Crucially the match must NOT depend on the optional ``Version: <version>``
    commit-message line: that line is only emitted when
    ``version.include_in_commit_message`` is true (default true, but frequently
    disabled), and requiring it would misclassify a legitimate own-replay as
    concurrent drift for those projects — double-bumping or failing a healthy
    resume. So the primary signal is: a commit carrying our ``Flow:`` trailer
    whose *version-file blob* parses to *version*. The ``Version:``-line grep is
    retained only as a fallback for cases where the blob cannot be inspected
    (script-mode version scheme, or no version file/bumper available).

    Best-effort: any git error → ``False`` (treat as drift, the
    safe-from-collision default).
    """
    if not flow_id or not version:
        return False
    version = version.strip()

    # Collect THIS flow's own commits via the always-present Flow: trailer.
    try:
        own = subprocess.run(
            [
                "git", "log", "--fixed-strings",
                f"--grep=Flow: {flow_id}",
                "--pretty=%H", "-n", "20",
            ],
            capture_output=True,
            text=True,
            cwd=project_root,
            timeout=15,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    commits = own.stdout.split() if own.returncode == 0 else []

    # Primary: verify the version-file blob at one of our own commits equals the
    # disk version — independent of the optional Version: commit-message line.
    # Skipped in script mode, where read_version ignores the path and reads live
    # disk state (a historical blob cannot be reconstructed that way).
    if (
        commits
        and version_file is not None
        and version_bumper is not None
        and not getattr(version_bumper, "_use_script_mode", False)
    ):
        for commit in commits:
            blob_version = _version_at_commit(
                project_root, commit, version_file, version_bumper
            )
            if blob_version is not None and (
                _normalize_version(blob_version) == _normalize_version(version)
            ):
                return True
        # Blob inspection ran and confirmed none of our own commits stamped this
        # version — do NOT fall through to the looser Version:-line grep, which
        # could only agree or (given the blob already disagreed) mislead.
        return False

    # Fallback (no version file/bumper to inspect, or script mode): the legacy
    # Flow+Version double-grep, effective only when the Version: line is present.
    try:
        legacy = subprocess.run(
            [
                "git", "log", "--all-match", "--fixed-strings",
                f"--grep=Flow: {flow_id}",
                f"--grep=Version: {version}",
                "--pretty=%H", "-n", "1",
            ],
            capture_output=True,
            text=True,
            cwd=project_root,
            timeout=15,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return legacy.returncode == 0 and bool(legacy.stdout.strip())


def _guard_version_race(
    step: Step,
    flow: FlowInstance,
    disk_version: str | None,
    target_version: str,
    version_file: Path | None = None,
    version_bumper: VersionBumper | None = None,
) -> str:
    """Return the version to write, recomputed if the disk baseline drifted.

    Compares the in-lock disk version against the version version_analyze
    actually OBSERVED on disk (its ``current_version`` output — see
    :func:`_resolve_analyze_baseline`), NOT the pre-session baseline. The
    distinction is load-bearing: ``current_version`` already folds in this flow's
    own session/implement commits, so those advances are by construction not
    drift; comparing against ``pre_session_version`` instead would misread the
    flow's own already-accounted bump as concurrent drift and trigger a spurious
    recompute. When they agree there was no concurrent bump — the behaviour is
    unchanged and ``target_version`` is returned verbatim. When they differ, a
    concurrent direct-run flow bumped the version file after our version_analyze
    ran (or a prior crashed attempt of THIS flow left an uncommitted write — see
    the crash-resume recognition below), so ``target_version`` is stale and would
    collide; version_analyze is re-run against the drifted disk version and its
    fresh ``suggested_version`` is returned instead.

    Args:
        step: The commit step (its ``inputs`` carry the forwarded baseline and
            version_analyze artifacts, refreshed here on drift).
        flow: The flow instance — locates the version_analyze step to re-run.
        disk_version: The version read from the version file just now, in-lock.
        target_version: The version_analyze ``suggested_version`` resolved
            earlier (the stale candidate on drift).

    Returns:
        The version string to write: ``target_version`` when the baseline held,
        or the recomputed version when it drifted.
    """
    baseline = _resolve_analyze_baseline(step, flow)
    if baseline is None:
        # No baseline to compare against — cannot tell drift from a normal
        # first bump, so leave the resolved target untouched.
        return target_version

    if _normalize_version(disk_version) == _normalize_version(baseline):
        return target_version

    # The disk version is ahead of the pre-session baseline — but not every
    # advance is a *concurrent* bump. A replay/resume of THIS flow's own commit
    # step (e.g. after a fix-loop re-entry) sees disk already at the version this
    # flow itself committed earlier; that is our own already-accounted write, not
    # another flow's. Re-analysing against it would rebase the decision and
    # double-bump (5.1.0 baseline, own 5.2.0 on disk, suggested 5.2.0 → a
    # baseline-sensitive LLM returns 5.2.1/5.3.0).
    #
    # Primary, mode-independent signal: the version THIS flow already committed,
    # recorded on the flow's durable state at its prior successful commit
    # (``_record_flow_committed_version``). This is the ONLY reliable own-replay
    # signal in script mode, where ``read_version`` ignores the path and reads
    # live disk state (so no historical version-file blob can be reconstructed)
    # and the optional ``Version:`` commit-message line may be absent under
    # ``version.include_in_commit_message: false`` — leaving the git-durable
    # blob/trailer probe below unable to recognise our own commit.
    _guard_context = getattr(getattr(flow, "state", None), "context", None)
    own_committed = (
        _guard_context.get("flow_committed_version")
        if isinstance(_guard_context, dict)
        else None
    )
    if own_committed and (
        _normalize_version(own_committed) == _normalize_version(disk_version)
    ):
        logger.info(
            "Version race guard: disk version %r matches the version this flow "
            "recorded committing earlier (durable flow state); treating as a "
            "replay, not concurrent drift — keeping target %r.",
            disk_version,
            target_version,
        )
        return target_version

    project_root = flow.change_path.parent if flow.change_path else Path.cwd()

    # Crash-resume own-write recognition (change D, issue: set_version→commit
    # crash window). If the process died AFTER set_version wrote the version file
    # but BEFORE ``git commit``, a later resume re-enters here with disk already
    # at our target while version_analyze's observed baseline is still the old
    # version — the durable flow_committed_version marker (written only post-
    # commit) and the committed-blob probe below both legitimately miss, because
    # NO commit was ever made. The distinguishing fact: a *concurrent* flow's
    # bump is always committed (HEAD's version-file blob == disk), whereas our
    # crashed write is uncommitted (HEAD still holds the pre-crash version, only
    # the working tree is ahead). So when HEAD's committed version differs from
    # the drifted disk version, the drift is our OWN uncommitted residue — keep
    # the target and re-write it verbatim (the pre-change self-healing behaviour)
    # rather than misclassifying it as concurrent drift and over-advancing.
    # File mode only: script mode has no reconstructable version-file blob, and
    # its live-disk read model makes this crash window far less reachable.
    if (
        version_file is not None
        and version_bumper is not None
        and not getattr(version_bumper, "_use_script_mode", False)
    ):
        head_version = _version_at_commit(
            project_root, "HEAD", version_file, version_bumper
        )
        if head_version is not None and (
            _normalize_version(head_version) != _normalize_version(disk_version)
        ):
            logger.info(
                "Version race guard: disk version %r is uncommitted (HEAD still "
                "at %r); the drift is this flow's own set_version write from a "
                "prior attempt that crashed before commit — keeping target %r and "
                "re-writing verbatim (self-heal).",
                disk_version,
                head_version,
                target_version,
            )
            return target_version

    # Secondary, git-durable signal for file-backed versioning: our own prior
    # write is recognisable via the always-present Flow: trailer + version-file
    # blob (NOT the optional Version: commit-message line). Survives a flow-state
    # reset that would erase the durable record above; a no-op idempotent write.
    if _flow_wrote_version(
        project_root,
        getattr(flow, "flow_id", None),
        disk_version,
        version_file=version_file,
        version_bumper=version_bumper,
    ):
        logger.info(
            "Version race guard: disk version %r was written by this flow's own "
            "prior commit (Flow trailer + version-file blob match); treating as a "
            "replay, not concurrent drift — keeping target %r.",
            disk_version,
            target_version,
        )
        return target_version

    logger.warning(
        "Version race guard: disk version %r drifted from the pre-session "
        "baseline %r (a concurrent flow bumped first); re-running "
        "version_analyze against the drifted baseline so the stale target %r "
        "does not collide.",
        disk_version,
        baseline,
        target_version,
    )
    new_target = _reanalyze_version_with_baseline(step, flow, disk_version)
    logger.info(
        "Version race guard: recomputed target version %r -> %r (new baseline "
        "%r)",
        target_version,
        new_target,
        disk_version,
    )
    if _normalize_version(new_target) == _normalize_version(disk_version):
        # The re-analysis produced a number equal to the drifted disk version,
        # which would still collide — writing it would file this flow's changelog
        # under the number a concurrent flow just released (the 10.7.1-type
        # shared-version accident this guard exists to block). This is an upstream
        # (version_analyze) correctness issue, not a race we can fix by retrying
        # here; HALT the commit rather than silently write the colliding version,
        # matching the other refusal paths in _reanalyze_version_with_baseline.
        raise RuntimeError(
            "Version race guard: re-running version_analyze against the drifted "
            f"baseline {disk_version!r} returned {new_target!r}, equal to the "
            "version a concurrent flow just released; refusing to write a "
            "colliding version. Rerun the version_analyze step or supply a "
            "version via human intervention."
        )

    # Equality alone is not enough: a re-analysis LLM can also hallucinate a
    # number LOWER than the drifted disk version (a regression) or one equal to
    # an earlier historically-released version (a collision with a past release).
    # Both are the same class of accident — a bad version silently committed on
    # the exact code path whose purpose is collision prevention — so mirror
    # validate_no_regression's contract on the merge-side reconcile path here
    # (final >= drifted current, no reuse of an already-released number).
    try:
        from ..version_bumper import Version

        disk_v = Version.parse(disk_version)
        new_v = Version.parse(new_target)
    except Exception:  # noqa: BLE001 - non-SemVer / unparseable: skip numeric check
        disk_v = new_v = None
    if disk_v is not None and new_v is not None and new_v < disk_v:
        raise RuntimeError(
            "Version race guard: re-running version_analyze against the drifted "
            f"baseline {disk_version!r} returned {new_target!r}, which regresses "
            "below the version a concurrent flow just released; refusing to write "
            "a regressing version. Rerun the version_analyze step or supply a "
            "version via human intervention."
        )

    try:
        from ..merge.reconcile import historical_versions

        history = historical_versions(project_root)
    except Exception:  # noqa: BLE001 - unreadable changelog: skip the history guard
        history = set()
    if new_target.strip() in history:
        raise RuntimeError(
            "Version race guard: re-running version_analyze against the drifted "
            f"baseline {disk_version!r} returned {new_target!r}, which reuses a "
            "version already recorded in VERSIONS.md; refusing to write a "
            "colliding version. Rerun the version_analyze step or supply a "
            "version via human intervention."
        )
    return new_target


def _reanalyze_version_with_baseline(
    step: Step, flow: FlowInstance, new_baseline: str | None
) -> str:
    """Re-run version_analyze against a drifted disk baseline, return its version.

    Locates the flow's version_analyze step, overrides its
    ``pre_session_version`` input with the drifted disk version, and re-invokes
    ``version_analyze_handler`` so the new number is derived against the version
    a concurrent flow just wrote (honouring any ``se3/version-rules.md``, which a
    mechanical SemVer bump could not). The refreshed artifacts (bump_type,
    commit_message, versions_changes, reasoning) are forwarded back onto the
    commit step's inputs so the commit message and VERSIONS.md entry match the
    recomputed version.

    Raises:
        RuntimeError: When no version_analyze step can be located, or the re-run
            fails / yields no ``suggested_version``. Halting the commit is the
            safe outcome — writing the stale, colliding version is exactly the
            accident this guard exists to prevent.
    """
    from .version_analyze import version_analyze_handler

    va_step: Step | None = None
    for step_id in reversed(flow.state.step_history):
        s = flow.state.steps.get(step_id)
        if s and s.step_type == StepType.VERSION_ANALYZE:
            va_step = s
            break

    if va_step is None:
        raise RuntimeError(
            "Version race guard: disk version drifted from the pre-session "
            "baseline but no version_analyze step was found to re-run; refusing "
            "to write a possibly-colliding version."
        )

    # Override the baseline so the re-analysis computes against the drifted disk
    # version, and drop the stale suggested_version so a fresh one is produced.
    va_step.inputs["pre_session_version"] = new_baseline
    va_step.outputs.pop("suggested_version", None)

    status = version_analyze_handler(va_step, flow)
    new_suggested = va_step.outputs.get("suggested_version")
    if status != StepStatus.COMPLETED or not (
        isinstance(new_suggested, str) and new_suggested.strip()
    ):
        raise RuntimeError(
            "Version race guard: re-running version_analyze against the drifted "
            f"baseline {new_baseline!r} did not yield a suggested_version "
            f"(status={status}); refusing to write a possibly-colliding version. "
            "Rerun the version_analyze step or supply a version via human "
            "intervention."
        )

    new_suggested = new_suggested.strip()
    # Forward the refreshed artifacts so the commit message / changelog match
    # the recomputed version rather than the superseded one.
    step.inputs["suggested_version"] = new_suggested
    for key in ("bump_type", "commit_message", "versions_changes", "reasoning"):
        if key in va_step.outputs:
            step.inputs[key] = va_step.outputs[key]
    return new_suggested


def _stage_file(project_root: Path, file_path: Path) -> None:
    """Stage a specific file for commit.

    Args:
        project_root: Project root directory
        file_path: Path to the file to stage
    """
    # Get relative path if file is within project root
    try:
        rel_path = file_path.relative_to(project_root)
    except ValueError:
        rel_path = file_path

    result = subprocess.run(
        ["git", "add", str(rel_path)],
        capture_output=True,
        text=True,
        cwd=project_root,
    )

    if result.returncode != 0:
        raise RuntimeError(f"Failed to stage {rel_path}: {result.stderr}")


def _update_docs(
    project_root: Path,
    new_version: str,
    step: Step,
    commit_message: str,
) -> None:
    """Mechanically update README.md badge and VERSIONS.md changelog.

    Runs the already-tested :class:`DocumentationUpdater` to apply a
    deterministic badge swap and prepend a VERSIONS.md entry, then stages
    both files so they ride along with the version bump in the same commit.

    The changelog body comes from the ``versions_changes`` list produced by
    the version_analyze step (forwarded via ``step.inputs``); when that is
    absent or empty it falls back to the first line of the commit message.

    This is strictly best-effort: any failure (missing README, I/O error,
    staging failure) is swallowed with a ``logger.warning`` so documentation
    problems never abort the commit. The subsequent ``git add -A`` will pick
    up any docs the explicit staging missed.

    Args:
        project_root: Project root directory.
        new_version: The version just written to the version file.
        step: The commit step (its ``inputs`` carry forwarded
            ``versions_changes``).
        commit_message: The generated commit message (used for the changelog
            fallback when ``versions_changes`` is unavailable).
    """
    try:
        # Import lazily to keep the module import graph light and avoid any
        # import-time coupling on the docs subsystem / config loader.
        from ..docs_updater import DocumentationUpdater
        from ...config import load_docs_config

        first_line = commit_message.strip().splitlines()[0] if commit_message.strip() else new_version
        changes = step.inputs.get("versions_changes") or [first_line]

        updater = DocumentationUpdater(
            project_root,
            config=load_docs_config(project_root).to_updater_config(),
        )
        updater.update_both(new_version, changes=changes)

        _stage_file(project_root, project_root / "README.md")
        _stage_file(project_root, project_root / "VERSIONS.md")
    except Exception as e:
        logger.warning(f"Documentation auto-update failed (commit continues): {e}")


def _rollback_version(
    version_bumper: VersionBumper,
    version_file: Path,
    original_version: str
) -> None:
    """Rollback version to original value.

    Args:
        version_bumper: VersionBumper instance
        version_file: Path to version file
        original_version: Original version string to restore
    """
    logger.warning(f"Rolling back version to {original_version}")
    try:
        version_bumper.rollback()
        logger.info(f"Version rolled back to {original_version}")
    except Exception as e:
        logger.error(f"Version rollback failed: {e}")
        raise


def _has_changes(project_root: Path, baseline_commit: str | None = None) -> bool:
    """Check if there are code changes to commit.

    When a baseline_commit is provided, uses ``git diff`` to compare the
    baseline against HEAD.  This correctly detects changes in multi-worktree
    scenarios where commits have been merged but the working tree is clean.

    Falls back to ``git status --porcelain`` when no baseline is available
    (backward compatibility).

    Args:
        project_root: Project root directory
        baseline_commit: Optional baseline commit hash to diff against HEAD

    Returns:
        True if there are changes to commit
    """
    # When a baseline commit is available, compare it against HEAD.
    if baseline_commit:
        try:
            result = subprocess.run(
                ["git", "diff", baseline_commit, "HEAD", "--quiet"],
                capture_output=True,
                text=True,
                cwd=project_root,
            )
            # --quiet: exit code 0 means no diff, 1 means there are diffs
            if result.returncode == 1:
                return True
            if result.returncode == 0:
                # No diff between baseline and HEAD; still check working tree
                # in case there are unstaged/uncommitted changes on top.
                pass
            # returncode > 1 indicates an error (e.g. bad commit ref) — fall through
        except Exception:
            pass

    # Fallback: check working tree for uncommitted changes
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            cwd=project_root,
        )
        return len(result.stdout.strip()) > 0
    except Exception:
        return False



def _generate_commit_message(
    flow: FlowInstance,
    step: Step,
    new_version: str | None = None,
    version_config: VersionConfig | None = None
) -> str:
    """Generate a commit message based on the flow context.

    Priority chain for the subject line:
    1. commit_message from version_analyze step (via step.inputs)
    2. proposal summary from plan step
    3. implement_summary from implement step
    4. Template fallback from task description

    Args:
        flow: The flow instance
        step: The current step
        new_version: Optional new version string if version was bumped
        version_config: Version configuration

    Returns:
        Commit message string
    """
    task_type = flow.task_type or "feature"
    task_description = step.inputs.get("task_description", flow.task_description) or ""

    # Get inputs from previous steps
    changes_made = step.inputs.get("changes_made") or {}
    proposal = step.inputs.get("proposal") or {}

    # Get completion status from implement step (defaults for backward compatibility)
    completion_status = step.inputs.get("completion_status", "complete")
    incomplete_tasks = step.inputs.get("incomplete_tasks", [])
    implement_summary = step.inputs.get("implement_summary", "")
    restricted_edits_applied = step.inputs.get("restricted_edits_applied", [])

    # Priority 1: commit_message from version_analyze
    commit_msg_from_va = step.inputs.get("commit_message", "")
    if commit_msg_from_va:
        first_line = commit_msg_from_va.strip()
        if len(first_line) > 72:
            first_line = first_line[:69] + "..."
        message = f"{task_type}: {first_line}"
    else:
        # Priority 2: proposal summary, Priority 3: implement_summary
        summary = proposal.get("summary", "") or implement_summary
        if summary:
            first_line = summary.split(".")[0]
            if len(first_line) > 72:
                first_line = first_line[:69] + "..."
            message = f"{task_type}: {first_line}"
        else:
            # Priority 4: template fallback from task description
            desc = task_description[:60] if len(task_description) > 60 else task_description
            message = f"{task_type}: {desc}"

    # Decorate the subject line with the version_analyze bump_type when
    # available. bump_type is auxiliary — it never determines the new version
    # number, but it provides useful context in the commit message.
    bump_type = step.inputs.get("bump_type")
    if isinstance(bump_type, str):
        bump_type = bump_type.strip().lower()
        if bump_type and bump_type != "none":
            message += f" ({bump_type} bump)"

    # Add context about the change
    files_changed = changes_made.get("files_changed", [])
    if files_changed:
        file_paths = []
        for f in files_changed[:3]:
            if isinstance(f, str):
                file_paths.append(f)
            elif isinstance(f, dict):
                file_paths.append(f.get("path", "?"))
            else:
                file_paths.append(str(f))
        file_list = ", ".join(file_paths)
        if len(files_changed) > 3:
            file_list += f" and {len(files_changed) - 3} more"

        message += f"\n\nFiles: {file_list}"

    # Add incomplete tasks section when partial completion
    if completion_status == "partial" and incomplete_tasks:
        message += "\n\nIncomplete tasks (partial completion):"
        for task in incomplete_tasks:
            if isinstance(task, str):
                message += f"\n- {task}"
            elif isinstance(task, dict):
                desc = task.get("description", task.get("task", str(task)))
                reason = task.get("reason", "")
                message += f"\n- {desc}"
                if reason:
                    message += f" ({reason})"

    # Add version information if bumping occurred and is configured to include
    include_version = (
        version_config is None or
        version_config.include_in_commit_message
    )
    if new_version and include_version:
        message += f"\n\nVersion: {new_version}"

    # Add flow reference
    message += f"\n\nFlow: {flow.flow_id}"

    return message


def _get_commit_hash(project_root: Path) -> str:
    """Get the current commit hash.

    Returns ``'unknown'`` on repos with no commits or on any git failure.

    Args:
        project_root: Project root directory

    Returns:
        Commit hash string, or ``'unknown'`` if unavailable
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=project_root,
        )
        if result.returncode != 0:
            return "unknown"
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _generate_template_summary(flow: FlowInstance, step: Step) -> None:
    """Generate a template-based summary document when the summarize step is not in the flow.

    Uses commit message as the primary info, combined with structured data
    from the flow state (changed files, test results, version) to produce
    a summary without an LLM call.

    Args:
        flow: The flow instance
        step: The commit step (with outputs populated)
    """
    project_root = flow.change_path.parent if flow.change_path else Path.cwd()

    commit_message = step.outputs.get("commit_message", "")
    commit_hash = step.outputs.get("commit_hash", "unknown")
    version = step.outputs.get("version")
    version_bumped = step.outputs.get("version_bumped", False)

    changes = _collect_changes_from_flow(flow)
    test_results = _collect_test_results_from_flow(flow)

    task_description = flow.task_description or ""
    task_type = flow.task_type or "task"

    # Build summary document
    lines = [f"## Work Summary\n"]
    lines.append(f"**Task:** {task_description[:200]}\n")
    lines.append(f"**Type:** {task_type}\n")

    if commit_hash and commit_hash != "unknown":
        lines.append(f"**Commit:** `{commit_hash[:8]}`\n")

    if version_bumped and version:
        lines.append(f"**Version:** {version}\n")

    # Version analysis reasoning (from version_analyze step via inputs)
    reasoning = step.inputs.get("reasoning", "")
    if reasoning and reasoning.strip():
        lines.append(f"\n### Version Analysis\n")
        lines.append(f"{reasoning.strip()}\n")

    # Commit message section
    if commit_message:
        lines.append(f"\n### Commit Message\n")
        lines.append(f"{commit_message}\n")

    # Files changed section
    if changes:
        lines.append(f"\n### Files Changed ({len(changes)})\n")
        for f in changes[:20]:
            lines.append(f"- {f}")
        if len(changes) > 20:
            lines.append(f"- ... and {len(changes) - 20} more")

    # Test results section
    if test_results:
        passed = test_results.get("passed", False)
        status = "Passed" if passed else "Failed"
        lines.append(f"\n### Test Results\n")
        lines.append(f"- Status: **{status}**")

    lines.append(f"\n---\n*Generated by commit step (template mode)*\n")

    summary_text = "\n".join(lines)

    # Save to se3/state/summary-{flow_id}.md
    summary_dir = project_root / "se3" / "state"
    summary_dir.mkdir(parents=True, exist_ok=True)
    summary_file = summary_dir / f"summary-{flow.flow_id}.md"

    try:
        with open(summary_file, "w", encoding="utf-8") as f:
            f.write(summary_text)
        logger.info(f"Template summary saved to {summary_file}")
    except OSError as e:
        logger.warning(f"Failed to save template summary: {e}")


def _collect_changes_from_flow(flow: FlowInstance) -> list[str]:
    """Collect file change paths from the flow's implement step outputs.

    Args:
        flow: The flow instance

    Returns:
        List of file path strings
    """
    file_paths: list[str] = []

    for step_id in flow.state.step_history:
        step = flow.state.steps.get(step_id)
        if step and step.step_type == StepType.IMPLEMENT:
            files_changed = step.outputs.get("files_changed", [])
            for f in files_changed:
                if isinstance(f, str):
                    file_paths.append(f)
                elif isinstance(f, dict):
                    file_paths.append(f.get("path", "?"))
                else:
                    file_paths.append(str(f))

    return file_paths


def _collect_test_results_from_flow(flow: FlowInstance) -> dict:
    """Collect the most recent test results from the flow's test step outputs.

    Args:
        flow: The flow instance

    Returns:
        Test results dict, or empty dict if no test step found
    """
    for step_id in reversed(flow.state.step_history):
        step = flow.state.steps.get(step_id)
        if step and step.step_type == StepType.TEST:
            return step.outputs.get("test_results") or {}

    return {}
