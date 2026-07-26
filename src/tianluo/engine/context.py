"""Context class for workflow execution.

Provides read-only context for step execution and UI display.
Encapsulates workflow state and provides display-friendly properties.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .models import State


# Run modes are invocation modes (triggered by flags like --discover), NOT task
# types. They legitimately drive the flow's step sequence / resume via
# flow.task_type / resolved_type, but they must never surface as the *type* in a
# commit-message prefix or version display — the real type is whatever analyze
# inferred (feature/bugfix/…). Extend this set when a new run mode is added.
RUN_MODE_TYPES: set[str] = {"discovery"}


def effective_task_type(
    context: dict, fallback_flow_type: Optional[str] = None
) -> str:
    """Resolve the real task type for commit-message / version consumers.

    Single source of truth for "what type is this work" at the display/commit
    boundary. Never returns a run mode (see :data:`RUN_MODE_TYPES`): a
    ``--discover`` run whose flow.task_type stays ``'discovery'`` (to keep its
    step sequence) still yields the type analyze actually inferred.

    Resolution order:

    1. A real explicit ``--type`` (``context['explicit_type']`` that is not a
       run mode) — the user's stated intent wins.
    2. The analyzed type analyze persisted (``context['analyzed_type']``) — the
       real type behind a run mode; already sanitized to a non-run-mode value.
    3. ``fallback_flow_type`` sanitized: when it is empty or a run mode, degrade
       to ``'feature'`` (an old state predating ``analyzed_type`` that only has
       ``flow.task_type == 'discovery'`` still resolves to a usable type).

    Pure, side-effect-free.
    """
    if not isinstance(context, dict):
        context = {}

    explicit = context.get("explicit_type")
    if isinstance(explicit, str) and explicit and explicit not in RUN_MODE_TYPES:
        return explicit

    analyzed = context.get("analyzed_type")
    if isinstance(analyzed, str) and analyzed and analyzed not in RUN_MODE_TYPES:
        return analyzed

    if (
        isinstance(fallback_flow_type, str)
        and fallback_flow_type
        and fallback_flow_type not in RUN_MODE_TYPES
    ):
        return fallback_flow_type

    return "feature"


class Context:
    """Read-only context for workflow step execution.

    Provides access to workflow state with display-friendly properties.
    Encapsulates the logic for determining task type display values.
    """

    # Sentinel value for uninitialized task types
    PENDING_TYPE = "pending"

    def __init__(
        self,
        task_description: str,
        state: State,
    ):
        """Initialize context.

        Args:
            task_description: The original task description
            state: The workflow state (provides type information)
        """
        self._task_description = task_description
        self._state = state

    @property
    def task_description(self) -> str:
        """Get the task description."""
        return self._task_description

    @property
    def task_type(self) -> str:
        """Get the current task type.

        Returns the resolved type from state, or 'pending' if not yet resolved.

        Returns:
            Task type string (e.g., 'feature', 'bugfix', 'pending')
        """
        # Check for resolved type from analyze step
        resolved = self._state.context.get("resolved_type")
        if resolved:
            # A run mode (e.g. discovery) is not a task type: fall back to the
            # real analyzed type so callers never see 'discovery' as the type.
            if resolved in RUN_MODE_TYPES:
                return effective_task_type(self._state.context, resolved)
            return resolved

        # Check for explicit type from --type flag — but a run mode (e.g. a
        # --discover run's explicit_type='discovery') is never a task type, so
        # skip it here and let it resolve below.
        explicit = self._state.context.get("explicit_type")
        if explicit and explicit not in RUN_MODE_TYPES:
            return explicit

        # Check for explicit_type in flow's task_type attribute (backward compat)
        flow_type = getattr(self._state, "task_type", None)
        if flow_type and flow_type not in RUN_MODE_TYPES:
            return flow_type

        # A run mode is in play (e.g. discovery pre-analyze) but no real type is
        # resolved yet: surface the analyzed type if analyze already persisted
        # it, otherwise stay pending — a run mode must never be the task type.
        analyzed = self._state.context.get("analyzed_type")
        if analyzed and analyzed not in RUN_MODE_TYPES:
            return analyzed

        # Default to pending
        return self.PENDING_TYPE

    @property
    def display_type(self) -> Optional[str]:
        """Get the display-friendly task type.

        Returns None when type is still pending (for UI to hide or show placeholder).
        Returns the actual type string when resolved.

        Returns:
            Type string for display, or None if pending
        """
        resolved = self._state.context.get("resolved_type")
        if resolved:
            # A run mode (e.g. discovery) is not a displayable task type: show
            # the real analyzed type instead of 'discovery'.
            if resolved in RUN_MODE_TYPES:
                return effective_task_type(self._state.context, resolved)
            return resolved
        return None

    def is_type_pending(self) -> bool:
        """Check if task type is still pending (not yet resolved by analyze).

        Delegates to state's is_type_pending() method if available,
        otherwise checks local task_type against pending sentinel.

        Returns:
            True if type is pending, False otherwise
        """
        # Delegate to state if it has the method
        if hasattr(self._state, "is_type_pending"):
            return self._state.is_type_pending()

        # Fallback to local check
        return self.task_type == self.PENDING_TYPE

    def __repr__(self) -> str:
        """String representation for debugging."""
        return f"Context(task_type={self.task_type!r}, pending={self.is_type_pending()})"
