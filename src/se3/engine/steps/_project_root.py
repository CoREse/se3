"""Shared resolver for a flow's authoritative project root.

Single convergence point for the write side of "one flow, one history home".

WHY: In ``se3 run --worktree`` the wrapper process keeps its cwd in the MAIN
checkout (intentional — see ``src/se3/commands/run.py``), and modern flows carry
``change_path = None``. So the historical
``flow.change_path.parent if flow.change_path else Path.cwd()`` idiom resolves a
worktree flow's project_root to the main checkout, and the in-process discovery
round-1 LLMCaller writes the first user prompt / stream / assistant chat records
into the MAIN repo's ``se3/history/<flow_id>/`` while the orchestrator (which
holds the correct project_root) writes ``step_started``/``step_status`` events
into the WORKTREE copy — the head of the transcript forks across two files and
the daemon/WebUI (which serve the worktree copy) can never show it.

INVARIANT: every step handler resolves a flow's project_root through this
helper so the flow's history has exactly one home — the worktree root while the
flow is alive (merge/salvage relocates it back to the main repo afterwards).

The authoritative value is ``flow.state.context['project_root']``, written by
the StateMachine at flow creation (worktree mode → the worktree root) and
correctly rebound to the main checkout by ``_step_cwd_override`` during
merge-side steps, so this resolver is semantically compatible with merges. The
legacy ``change_path.parent → Path.cwd()`` fallback chain is preserved only so
older persisted flows and bare test-constructed flows (no context value) do not
regress.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models import FlowInstance


def resolve_flow_project_root(flow: "FlowInstance") -> Path:
    """Resolve the authoritative project root for ``flow``.

    Priority: ``flow.state.context['project_root']`` (when present and
    non-empty) → ``flow.change_path.parent`` → ``Path.cwd()``.
    """
    # ``getattr`` (not attribute access) so a bare test-constructed flow — e.g. a
    # ``MagicMock(spec=FlowInstance)`` / ``MagicMock(spec=State)`` that never sets
    # ``state`` / ``context`` (both default_factory fields, absent from the spec)
    # — falls through to the legacy chain instead of raising AttributeError. On a
    # real flow ``context`` is always a dict, so ``isinstance`` narrows to the
    # authoritative path without affecting production behavior.
    state = getattr(flow, "state", None)
    context = getattr(state, "context", None) if state is not None else None
    if isinstance(context, dict):
        root = context.get("project_root")
        if root:
            return Path(root)
    if flow.change_path:
        return flow.change_path.parent
    return Path.cwd()
