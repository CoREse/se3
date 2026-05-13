"""Issue auto-discovery module for the SE3 flow engine.

Manages automatic issue discovery through two mechanisms:
- A-class: System-level triggers (fix loop exhaustion)
- B-class: Whitelist-based prompt injection + collection for check/read-only steps
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from .issue_manager import Issue, IssueManager
from .models import FlowInstance, Step, StepType

logger = logging.getLogger(__name__)

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
            f"**History path:** se3/history/{flow.flow_id}",
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
        """Create an issue for pre-existing test failures.

        A-class trigger: called by test_handler when pre-existing failures
        are detected (failures present in known_test_failures.json that
        were not introduced by the current change).

        Args:
            flow: Current flow instance
            pre_existing_failures: List of {test_id, reason} dicts

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
