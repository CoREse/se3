"""Extended workflow state management for the flow engine."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .models import State
from .persistence import PersistenceManager

logger = logging.getLogger(__name__)


@dataclass
class WorkflowState:
    """Extended state with task type management capabilities."""

    base_state: State = field(default_factory=State)
    explicit_type: Optional[str] = None  # User-provided via --type
    resolved_type: str = "pending"  # LLM-determined or pending

    _persistence: Optional[PersistenceManager] = None
    _flow_id: Optional[str] = None

    @property
    def task_type(self) -> str:
        """Return the effective task type."""
        if self.resolved_type and self.resolved_type != "pending":
            return self.resolved_type
        if self.explicit_type:
            return self.explicit_type
        return "pending"

    @property
    def display_type(self) -> Optional[str]:
        """Return formatted type string for UI display."""
        if self.resolved_type == "pending":
            return None
        return self.resolved_type

    def is_type_pending(self) -> bool:
        """Check if task type is still pending."""
        return self.resolved_type == "pending"

    def update_task_type(self, analyze_result: Dict[str, Any]) -> None:
        """Update task type based on analyze step output."""
        task_type = analyze_result.get("task_type")
        if not task_type:
            logger.warning("No task_type found in analyze result")
            return
        
        old_type = self.resolved_type
        self.resolved_type = task_type
        self.base_state.context["task_type"] = task_type
        self.base_state.context["resolved_type"] = task_type
        logger.info(f"Task type updated from '{old_type}' to '{task_type}'")

    # ... additional methods for backward compatibility