"""Context class for workflow execution.

Provides read-only context for step execution and UI display.
Encapsulates workflow state and provides display-friendly properties.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .models import State


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
            return resolved

        # Check for explicit type from --type flag
        explicit = self._state.context.get("explicit_type")
        if explicit:
            return explicit

        # Check for explicit_type in flow's task_type attribute (backward compat)
        flow_type = getattr(self._state, "task_type", None)
        if flow_type:
            return flow_type

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
        current_type = self.task_type
        if current_type == self.PENDING_TYPE:
            return None
        return current_type

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
