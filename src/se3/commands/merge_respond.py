"""SE3 Merge-Respond command — Process MCP call response files for merge conflicts.

Usage:
    se3 merge-respond <call-file-path>
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Optional

from ..engine.display import render_text

logger = logging.getLogger(__name__)

_STRICT_SENTINEL = "[__SE3_STRICT_PLACEHOLDER__:"


def _warn_deprecated_filename(call_path: Path) -> None:
    """Warn if call file uses deprecated naming convention.

    Old-style filenames use ``-`` to replace ``/`` in branch names and
    a timestamp format without the ``T`` separator (e.g.
    ``merge_20240101_120000_000000_branch-name.json``).  New-style
    filenames use ``__`` for ``/`` and ISO-8601-like timestamps with
    ``T`` (e.g.
    ``merge_20240101T120000_000000_1234_0_a1b2c3d4_branch__name.json``).

    This compatibility layer will be removed in the next release.
    """
    name = call_path.name
    if not name.startswith("merge_"):
        return
    # Extract the timestamp portion (first segment after 'merge_')
    rest = name[6:]  # strip 'merge_'
    if "_" not in rest:
        return
    timestamp_part = rest.split("_")[0]
    # Old format: YYYYMMDD (8 digits, no T); new format: YYYYMMDDTHHMMSS
    if len(timestamp_part) == 8 and timestamp_part.isdigit():
        logger.warning(
            "Deprecated call file naming '%s': old timestamp format without 'T' "
            "separator and '-' for '/' in branch names. Use new format with '__' "
            "instead. This compatibility will be removed in the next release.",
            name,
        )


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

    call_path = Path(call_file)
    if not call_path.exists():
        render_text(f"Call file not found: {call_path}", title="SE3 Merge Error")
        return 1

    _warn_deprecated_filename(call_path)

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
        # Guard against strict-mode placeholder content being accepted
        if call_type == "merge_conflict":
            files = call_data.get("files", [])
            sentinel_files: list[str] = []
            for f in files:
                llm_res = f.get("llm_resolution") or {}
                resolved = llm_res.get("resolved_content", "")
                if resolved.startswith(_STRICT_SENTINEL):
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

            # Check for orphan files with guardrail violations
            orphan_violations = call_data.get("orphan_guardrails_violations", [])
            if orphan_violations:
                logger.warning(
                    "Accepting merge with %d orphan file guardrail violation(s). "
                    "These were pre-checked during call-file creation; the human "
                    "reviewer has chosen to accept them.",
                    len(orphan_violations),
                )

            # Write resolved content back to files
            try:
                for f in files:
                    llm_res = f.get("llm_resolution") or {}
                    resolved = llm_res.get("resolved_content", "")
                    if not resolved:
                        continue
                    file_path = project_root / f["path"]
                    file_path.parent.mkdir(parents=True, exist_ok=True)
                    file_path.write_text(resolved, encoding="utf-8")

                    # Stage the file
                    subprocess.run(
                        ["git", "-C", str(project_root), "add", f["path"]],
                        capture_output=True,
                        check=False,
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
            )
            if commit_result.returncode != 0:
                render_text(
                    f"Merge commit failed: {commit_result.stderr.strip()}",
                    title="SE3 Merge Error",
                )
                return 1

            # After successful commit, run guardrails on any spec files
            # that were part of the resolution to close the gap between
            # LLM-resolved and human-resolved merge paths.
            spec_paths = [
                f["path"] for f in files
                if re.match(r"^se3/specs/.+/spec\.md$", f.get("path", ""))
            ]
            if spec_paths:
                try:
                    post_sha = subprocess.run(
                        ["git", "-C", str(project_root), "rev-parse", "HEAD"],
                        capture_output=True, text=True, check=True,
                    ).stdout.strip()
                    pre_sha = subprocess.run(
                        ["git", "-C", str(project_root), "rev-parse", "HEAD^1"],
                        capture_output=True, text=True, check=True,
                    ).stdout.strip()

                    from se3.engine.merge.guardrails import MergeGuardrailsCheck
                    guardrails = MergeGuardrailsCheck(project_root)
                    gr_report = guardrails.check_merge_result(pre_sha, post_sha)

                    if not gr_report.passed:
                        violations_lines = [
                            f"  [{v.violation_type}] {v.file_path}: {v.message}"
                            for v in gr_report.violations
                        ]
                        render_text(
                            "Merge committed successfully, but guardrail violations "
                            "were detected in spec files:\n\n"
                            + "\n".join(violations_lines)
                            + "\n\n"
                            "Please review and fix the spec files manually.",
                            title="SE3 Merge — Guardrail Violations Detected",
                        )
                        return 0
                except Exception as exc:
                    logger.warning(
                        "Guardrails check failed after merge-respond: %s", exc
                    )

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
    render_text(
        "Please resolve the conflicts manually, then run:\n"
        "  git add . && git commit"
        + (f"\nFeedback: {feedback}" if feedback else ""),
        title="SE3 Merge — Manual Resolution",
    )
    return 0
