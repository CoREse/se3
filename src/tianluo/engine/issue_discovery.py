"""Issue auto-discovery module for the SE3 flow engine.

Manages automatic issue discovery through two mechanisms:
- A-class: System-level triggers (fix loop exhaustion)
- B-class: Whitelist-based prompt injection + collection for check/read-only steps
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from .issue_manager import Issue, IssueManager
from .models import FlowInstance, Step, StepType

logger = logging.getLogger(__name__)

# Fields the discovery issue-operation executor is permitted to pass through
# when updating an issue. Deliberately excludes status / source so this path
# can never perform a state transition or change origin.
_DISCOVERY_UPDATE_FIELDS = ("title", "description", "priority", "type", "tags")


def _normalize_issue_id(issue_id: Any) -> str:
    """Normalize an issue ID for zero-padding-tolerant membership checks.

    Mirrors ``IssueManager._find_issue_file``: strips leading zeros so that
    ``"5"`` and ``"005"`` compare equal.
    """
    return str(issue_id).lstrip("0") or "0"


def apply_discovery_issue_operations(
    issue_manager: IssueManager,
    operations: List[Dict[str, Any]],
    tracked_ids: List[str],
) -> Tuple[List[str], List[Dict[str, Any]]]:
    """Execute scoped create/update/delete issue operations for a discovery step.

    This is the engine-side executor behind the discovery step's
    ``issue_operations`` response contract. The LLM only emits *intent*; the
    engine owns execution and the scope guarantee. Three actions are supported:

    - ``create``: always creates with ``source="human"`` (distinguishing it from
      fully-automatic ``source="system"`` discovered issues), and adds the new
      issue ID to the tracking set.
    - ``update``: only honored when the target ID is already in the tracking set
      (i.e. it was created earlier within *this* discovery step). Only the
      ``title`` / ``description`` / ``priority`` / ``type`` / ``tags`` fields are
      passed through — never status or source. Out-of-scope IDs (historical
      issues, issues from other sessions, in-progress issues) are rejected
      without touching the underlying issue.
    - ``delete``: only honored when the target ID is in the tracking set; the
      issue file is deleted and the ID is removed from the tracking set.
      Out-of-scope IDs are rejected.

    Each operation is isolated with try/except: a single failing op records a
    result and does not abort the remaining ops. Unknown actions are skipped
    with a留痕 (recorded) result. This method NEVER performs close / reopen /
    reset / status transitions.

    Args:
        issue_manager: The :class:`IssueManager` used for the writes.
        operations: List of operation dicts. Each carries an ``action`` key
            (``"create"`` / ``"update"`` / ``"delete"``) plus the relevant
            fields (``id`` for update/delete; ``title`` / ``description`` /
            ``priority`` / ``type`` / ``tags`` for create/update).
        tracked_ids: The IDs created by this discovery step so far (the legal
            scope for update/delete).

    Returns:
        A ``(new_tracked_ids, results)`` tuple where ``new_tracked_ids`` is the
        updated tracking set (deduplicated, order-preserving) and ``results`` is
        a per-operation record list, each a dict with at least ``action`` and
        ``status`` keys.
    """
    # Working copy preserving order, deduplicated by normalized form.
    tracked: List[str] = []
    seen_norm = set()
    for tid in tracked_ids or []:
        norm = _normalize_issue_id(tid)
        if norm not in seen_norm:
            seen_norm.add(norm)
            tracked.append(str(tid))

    results: List[Dict[str, Any]] = []

    for op in operations or []:
        if not isinstance(op, dict):
            logger.debug("Skipping non-dict issue operation: %r", op)
            results.append({"action": None, "status": "skipped", "reason": "not a dict"})
            continue

        action = None

        try:
            # Compute action inside the try so a non-string (or otherwise
            # malformed) action value is recorded as a per-op error rather than
            # raising before the loop can record it and aborting the whole batch.
            raw_action = op.get("action")
            action = (raw_action or "").strip().lower() if isinstance(raw_action, str) else None
            if action == "create":
                results.append(_apply_create(issue_manager, op, tracked, seen_norm))
            elif action == "update":
                results.append(_apply_update(issue_manager, op, tracked, seen_norm))
            elif action == "delete":
                results.append(_apply_delete(issue_manager, op, tracked, seen_norm))
            else:
                logger.debug("Skipping unknown issue operation action: %r", action)
                results.append({
                    "action": action or None,
                    "status": "skipped",
                    "reason": f"unknown action: {action!r}",
                })
        except Exception as e:  # isolate one failing op from the rest
            logger.warning("Issue operation %r failed: %s", action, e)
            results.append({
                "action": action or None,
                "id": op.get("id"),
                "status": "error",
                "reason": str(e),
            })

    return tracked, results


def _apply_create(
    issue_manager: IssueManager,
    op: Dict[str, Any],
    tracked: List[str],
    seen_norm: set,
) -> Dict[str, Any]:
    """Create an issue with ``source="human"`` and track its new ID."""
    description = op.get("description") or ""
    tags = op.get("tags")
    if tags is not None and not isinstance(tags, list):
        tags = None
    issue = issue_manager.create(
        description=description,
        title=op.get("title"),
        priority=op.get("priority"),
        tags=tags,
        type=op.get("type"),
        source="human",
    )
    norm = _normalize_issue_id(issue.id)
    if norm not in seen_norm:
        seen_norm.add(norm)
        tracked.append(issue.id)
    logger.info("Discovery created issue %s (source=human)", issue.id)
    return {"action": "create", "status": "created", "id": issue.id}


def _apply_update(
    issue_manager: IssueManager,
    op: Dict[str, Any],
    tracked: List[str],
    seen_norm: set,
) -> Dict[str, Any]:
    """Update a tracked issue's editable fields; reject out-of-scope IDs."""
    issue_id = op.get("id")
    if issue_id is None or _normalize_issue_id(issue_id) not in seen_norm:
        logger.warning(
            "Rejected out-of-scope update for issue %r (not created in this discovery step)",
            issue_id,
        )
        return {
            "action": "update",
            "id": issue_id,
            "status": "rejected",
            "reason": "id not in this discovery step's scope",
        }

    # Only pass through the explicitly permitted, present fields. Absent fields
    # stay None so update_fields leaves them unchanged.
    kwargs = {f: op[f] for f in _DISCOVERY_UPDATE_FIELDS if f in op}
    # A scalar tags value (the LLM emitted a string instead of a list) would be
    # stored verbatim by update_fields and silently corrupt the issue record
    # (downstream ", ".join(issue.tags) would iterate over characters). Drop a
    # non-list tags so the field is left unchanged, mirroring _apply_create's
    # coercion of a malformed tags to None.
    if "tags" in kwargs and not isinstance(kwargs["tags"], list):
        logger.warning(
            "Discovery update for issue %r dropped a non-list tags value %r",
            issue_id,
            kwargs["tags"],
        )
        kwargs.pop("tags")
    issue = issue_manager.update_fields(str(issue_id), **kwargs)
    logger.info("Discovery updated issue %s", issue.id)
    return {"action": "update", "status": "updated", "id": issue.id}


def _apply_delete(
    issue_manager: IssueManager,
    op: Dict[str, Any],
    tracked: List[str],
    seen_norm: set,
) -> Dict[str, Any]:
    """Delete a tracked issue and drop it from the tracking set."""
    issue_id = op.get("id")
    norm = _normalize_issue_id(issue_id) if issue_id is not None else None
    if norm is None or norm not in seen_norm:
        logger.warning(
            "Rejected out-of-scope delete for issue %r (not created in this discovery step)",
            issue_id,
        )
        return {
            "action": "delete",
            "id": issue_id,
            "status": "rejected",
            "reason": "id not in this discovery step's scope",
        }

    issue = issue_manager.delete_issue(str(issue_id))
    # Drop from tracking (by normalized form, preserving the others' order).
    seen_norm.discard(norm)
    tracked[:] = [t for t in tracked if _normalize_issue_id(t) != norm]
    logger.info("Discovery deleted issue %s", issue.id)
    return {"action": "delete", "status": "deleted", "id": issue.id}

# Steps that receive issue-discovery prompt injection and collection
ISSUE_DISCOVERY_STEPS = {"summarize"}

# Steps explicitly forbidden from producing issues
ISSUE_FORBIDDEN_STEPS = {"implement", "test"}

# Valid priority values for direct priority_hint usage
_VALID_PRIORITIES = {"critical", "high", "medium", "low"}

# Source tag mapping
_SOURCE_TAG_MAP = {
    "verify_spec": "source:verify-spec",
    "summarize": "source:summarize",
}

ISSUE_DISCOVERY_PROMPT = """
## Issue Discovery (Optional)

While performing your primary task above, if you notice any issues, concerns, or potential problems that are **outside the scope of the current step** but worth tracking, report them in a `discovered_issues` field in your output.

Each discovered issue should have:
- `title`: A short descriptive title
- `description`: Details about the issue
- `priority_hint`: One of "critical", "high", "medium", or "low"

Only report genuine concerns — do NOT fabricate issues. If there are no issues to report, omit the `discovered_issues` field or use an empty array.

Example (add to your JSON output or append as a JSON block):
```json
{
    "discovered_issues": [
        {
            "title": "Missing error handling in auth module",
            "description": "The authentication module does not handle token expiration gracefully.",
            "priority_hint": "medium"
        }
    ]
}
```
"""


class IssueDiscovery:
    """Manages automatic issue discovery across the SE3 flow engine.

    Handles both A-class (system-level) and B-class (prompt injection)
    issue discovery, including deduplication and tag/priority management.
    """

    def __init__(self, issue_manager: IssueManager, flow_id: str) -> None:
        self.issue_manager = issue_manager
        self.flow_id = flow_id
        # Track created issue titles for deduplication within this flow
        self._created_titles: List[str] = []

    def create_from_fix_loop_exhaustion(
        self,
        flow: FlowInstance,
        trigger_step: Step,
    ) -> Optional[Issue]:
        """Create an issue when fix loop reaches max iterations.

        A-class trigger: called by state_machine when fix iterations exhausted.

        Args:
            flow: Current flow instance
            trigger_step: The TEST or VERIFY_SPEC step that triggered exhaustion

        Returns:
            Created Issue, or None if deduplicated/failed
        """
        task_desc = flow.task_description[:100]
        title = f"Fix loop exhausted: {task_desc}"

        if self._is_duplicate(title):
            logger.debug(f"Deduplicated fix-loop issue: {title}")
            return None

        # Build description from available context
        desc_parts = [
            f"The fix loop reached maximum iterations while working on: {flow.task_description}",
            "",
            f"**Flow ID:** {flow.flow_id}",
            f"**History path:** tianluo/history/{flow.flow_id}",
        ]

        # Include refined_description if it differs from the original task_description.
        # Lazy import avoids module-level circular dependency: state_machine already
        # imports issue_discovery for A-class triggers.
        from .state_machine import _effective_task_description_base
        refined = _effective_task_description_base(flow)
        if refined and refined != flow.task_description:
            desc_parts.extend(["", "**Refined description:**", refined[:1500]])

        desc_parts.extend([
            "",
            f"**Trigger step:** {trigger_step.step_type.value}",
            f"**Fix iterations:** {flow.state.get_fix_iteration()}",
        ])

        # Include last test results if available
        fix_context = trigger_step.outputs.get("fix_context", {})
        test_results = fix_context.get("test_results", {})
        if test_results:
            stdout = ""
            if isinstance(test_results, dict):
                stdout = (test_results.get("stdout") or "")[-1000:]
                if not stdout:
                    # Try phases format
                    for phase in test_results.get("phases", []):
                        if not phase.get("passed", True):
                            stdout = (phase.get("stdout") or "")[-1000:]
                            break
            if stdout:
                desc_parts.extend(["", "**Last test output (truncated):**", f"```\n{stdout}\n```"])

        # Include fix history summary
        fix_history = flow.state.fix_history
        if fix_history:
            desc_parts.extend(["", "**Fix attempt history:**"])
            for entry in fix_history[-5:]:
                iteration = entry.get("iteration", "?")
                reason = entry.get("reason", "unknown")
                desc_parts.append(f"- Iteration {iteration}: {reason}")

        # Include fix instructions if available
        fix_instructions = trigger_step.outputs.get("fix_instructions", "")
        if fix_instructions:
            desc_parts.extend(["", "**Last fix instructions:**", fix_instructions[:1500]])

        description = "\n".join(desc_parts)

        try:
            issue = self.issue_manager.create(
                title=title,
                description=description,
                priority="high",
                tags=["auto-discovered", "source:fix-loop"],
                type="bug",
                source="system",
            )
            self._created_titles.append(self._normalize_title(title))
            logger.info(f"Created fix-loop exhaustion issue: {issue.id}")
            return issue
        except Exception as e:
            logger.warning(f"Failed to create fix-loop issue: {e}")
            return None

    def create_from_pre_existing_failures(
        self,
        flow: FlowInstance,
        pre_existing_failures: List[Dict[str, Any]],
    ) -> Optional[Issue]:
        """Create an issue for inherited (pre-existing) test failures.

        A-class trigger: called by test_handler when inherited failures are
        detected — failures whose test-id is present in the frozen
        pre-implement ``baseline_failures`` set (captured before this flow's
        implement step touched anything) and were therefore not introduced by
        the current change. The baseline replaces the retired
        ``known_test_failures.json`` known-list as the provenance source.

        Args:
            flow: Current flow instance
            pre_existing_failures: List of {test_id, reason} dicts of the
                inherited (baseline) failures

        Returns:
            Created Issue, or None if no failures, deduplicated, or failed
        """
        if not pre_existing_failures:
            return None

        count = len(pre_existing_failures)
        title = f"Pre-existing test failures ({count} test{'s' if count != 1 else ''})"

        if self._is_duplicate(title):
            logger.debug(f"Deduplicated pre-existing failures issue: {title}")
            return None

        # Build description
        desc_parts = [
            f"**{count} pre-existing test failure(s)** detected during flow execution.",
            "These failures were NOT introduced by the current change.",
            "",
            "**Failing tests:**",
        ]
        for entry in pre_existing_failures:
            tid = entry.get("test_id", "unknown")
            reason = entry.get("reason", "unknown")
            desc_parts.append(f"- `{tid}`: {reason}")

        desc_parts.extend([
            "",
            f"**Task:** {flow.task_description}",
            "",
            "These tests should be investigated and fixed to prevent the broken-window effect.",
        ])

        description = "\n".join(desc_parts)

        try:
            issue = self.issue_manager.create(
                title=title,
                description=description,
                priority="medium",
                tags=["auto-discovered", "source:test-pre-existing"],
                type="bug",
                source="system",
            )
            self._created_titles.append(self._normalize_title(title))
            logger.info(f"Created pre-existing failures issue: {issue.id}")
            return issue
        except Exception as e:
            logger.warning(f"Failed to create pre-existing failures issue: {e}")
            return None

    @staticmethod
    def get_injection_prompt(step_type: str) -> Optional[str]:
        """Get the issue discovery prompt fragment for a step type.

        B-class injection: returns prompt text for whitelist steps,
        None for all others.

        Args:
            step_type: Step type name (e.g., "verify_spec", "summarize")

        Returns:
            Prompt fragment string, or None if step not in whitelist
        """
        if step_type in ISSUE_DISCOVERY_STEPS:
            return ISSUE_DISCOVERY_PROMPT
        return None

    def collect_issues_from_output(
        self,
        flow: FlowInstance,
        step_type: str,
        step_outputs: Dict[str, Any],
    ) -> List[Issue]:
        """Parse and create issues from step output's discovered_issues field.

        B-class collection: called after whitelist steps complete.

        Args:
            flow: Current flow instance
            step_type: The step type that produced the output
            step_outputs: The step's outputs dict

        Returns:
            List of created Issues
        """
        if step_type not in ISSUE_DISCOVERY_STEPS:
            return []

        discovered = step_outputs.get("discovered_issues", [])
        if not discovered or not isinstance(discovered, list):
            return []

        source_tag = _SOURCE_TAG_MAP.get(step_type, f"source:{step_type}")
        created_issues = []

        for item in discovered:
            if not isinstance(item, dict):
                logger.debug(f"Skipping non-dict discovered_issues item: {item}")
                continue

            title = item.get("title", "").strip()
            if not title:
                logger.debug("Skipping discovered issue with empty title")
                continue

            if self._is_duplicate(title):
                logger.debug(f"Deduplicated discovered issue: {title}")
                continue

            description = item.get("description", "").strip()
            priority_hint = item.get("priority_hint", "medium").lower()

            # Use priority_hint directly if valid, default to medium
            priority = priority_hint if priority_hint in _VALID_PRIORITIES else "medium"

            tags = ["auto-discovered", source_tag]

            try:
                issue = self.issue_manager.create(
                    title=title,
                    description=description,
                    priority=priority,
                    tags=tags,
                    type="bug",
                    source="system",
                )
                self._created_titles.append(self._normalize_title(title))
                created_issues.append(issue)
                logger.info(f"Created discovered issue from {step_type}: {issue.id} - {title}")
            except Exception as e:
                logger.warning(f"Failed to create discovered issue '{title}': {e}")

        return created_issues

    def _is_duplicate(self, title: str) -> bool:
        """Check if a title is duplicate within this flow.

        Uses normalized string comparison (lowercase, stripped punctuation,
        token overlap) to detect near-duplicates.

        Args:
            title: Issue title to check

        Returns:
            True if duplicate detected
        """
        normalized = self._normalize_title(title)
        if not normalized:
            return False

        for existing in self._created_titles:
            if self._titles_similar(normalized, existing):
                return True
        return False

    @staticmethod
    def _normalize_title(title: str) -> str:
        """Normalize a title for comparison."""
        title = title.lower().strip()
        title = re.sub(r"[^\w\s]", "", title)
        title = re.sub(r"\s+", " ", title)
        return title.strip()

    @staticmethod
    def _titles_similar(a: str, b: str) -> bool:
        """Check if two normalized titles are similar enough to be duplicates.

        Uses token overlap ratio: if >70% of tokens overlap, consider duplicate.
        Also checks exact match after normalization.
        """
        if a == b:
            return True

        tokens_a = set(a.split())
        tokens_b = set(b.split())

        if not tokens_a or not tokens_b:
            return False

        overlap = tokens_a & tokens_b
        # Use the smaller set as denominator for asymmetric comparison
        min_size = min(len(tokens_a), len(tokens_b))
        if min_size == 0:
            return False

        similarity = len(overlap) / min_size
        return similarity > 0.7
