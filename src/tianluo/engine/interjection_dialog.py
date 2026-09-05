"""The mid-flow interjection dialog — a small discovery held at the breakpoint.

An interjection used to be a one-shot text box whose content was appended to
the step's task description and the step re-run. That shape cannot answer the
question the user actually has when they hit Ctrl-C ("what are you doing, and
why?"), and it forces them to guess a correction blind.

This module makes an interjection a *conversation*, and — crucially — a
conversation with the agent that was doing the work, inside its own provider
session, under a read-only tool lock. It still has its whole context: the files
it read, the reasoning it was midway through, the edit it had half made. An
independent LLM given a reconstructed transcript is a strictly worse
interlocutor, so it is the fallback, not the design.

The module owns the *engine* of that dialog: the prompt, the structured reply
contract, the decision model, and applying a decision to the flow. The two
front ends (the interactive terminal loop and the daemon/web call-file loop)
live in ``commands/run.py`` and share everything here.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Decision actions the dialog can settle on.
ACTION_CONTINUE = "continue"
ACTION_RESTART = "restart"
ACTION_EXIT = "exit"
ACTIONS = (ACTION_CONTINUE, ACTION_RESTART, ACTION_EXIT)

#: Workspace handling for a ``restart``.
WORKSPACE_KEEP = "keep"
WORKSPACE_RESET = "reset"
WORKSPACES = (WORKSPACE_KEEP, WORKSPACE_RESET)

#: Reply modes.
MODE_QUESTION = "question"
MODE_DECISION = "decision"

# Cap on the workspace diff summary injected into the dialog prompt. The dialog
# is a conversation, not a review: a full diff would crowd out the actual
# question and, in the same-session case, duplicate what the agent already
# knows about its own edits.
_DIFF_SUMMARY_MAX_CHARS = 4000


@dataclass
class DialogDecision:
    """A settled decision from the dialog, pending the user's confirmation."""

    action: str = ACTION_CONTINUE
    #: Temporary instruction that applies ONLY to this step's next run.
    instruction: str = ""
    #: A complete replacement task description; persists for the whole flow.
    revised_description: str = ""
    #: Rewind target (``restart`` only); empty means "the current step".
    restart_step_id: str = ""
    #: ``keep`` / ``reset`` (``restart`` only).
    workspace: str = WORKSPACE_KEEP

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "instruction": self.instruction,
            "revised_description": self.revised_description,
            "restart_step_id": self.restart_step_id,
            "workspace": self.workspace,
        }

    @classmethod
    def from_dict(
        cls, data: Any, *, strict: bool = False
    ) -> Optional["DialogDecision"]:
        """Build a decision from a (possibly sloppy) LLM / web payload.

        Lenient by default — an unrecognised value from the LLM is normalised
        to the safest reading rather than rejecting the whole conversation.
        ``strict=True`` is for a USER-edited decision (the web console's
        structured reply): an unknown action or workspace there is a mistake
        to hand back for correction, and coercing it to ``continue``/``keep``
        would execute a decision the operator never made.
        """
        if not isinstance(data, dict):
            return None
        action = str(data.get("action") or "").strip().lower()
        if action not in ACTIONS:
            if strict and action:
                return None
            action = ACTION_CONTINUE
        workspace = str(data.get("workspace") or "").strip().lower()
        if workspace not in WORKSPACES:
            if strict and workspace:
                return None
            workspace = WORKSPACE_KEEP
        return cls(
            action=action,
            instruction=str(data.get("instruction") or "").strip(),
            revised_description=str(data.get("revised_description") or "").strip(),
            restart_step_id=str(data.get("restart_step_id") or "").strip(),
            workspace=workspace,
        )


@dataclass
class DialogTurn:
    """One assistant reply: either a question back, or a proposed decision."""

    mode: str = MODE_QUESTION
    content: str = ""
    decision: Optional[DialogDecision] = None
    raw: str = ""

    @property
    def is_decision(self) -> bool:
        return self.mode == MODE_DECISION and self.decision is not None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "content": self.content,
            "decision": self.decision.to_dict() if self.decision else None,
        }


@dataclass
class DialogContext:
    """Everything the dialog LLM is told about the flow it is interrupting."""

    task_description: str = ""
    step_lines: List[str] = field(default_factory=list)
    current_step_id: str = ""
    current_step_type: str = ""
    workspace_summary: str = ""
    same_session: bool = False
    rebuilt_history: str = ""
    #: One line per DAG group of a parallel implement step.
    group_lines: List[str] = field(default_factory=list)

    def render(self, include_history: Optional[bool] = None) -> str:
        """Render the injected flow context.

        *include_history* decides whether the reconstructed step conversation
        is folded in. It is omitted when the reply comes from the working
        agent's own session — that agent already holds the conversation, and
        re-feeding a lossy reconstruction of it invites it to contradict its
        own memory. Every other interlocutor (a standalone assistant, or the
        fallback after a native resume was refused) needs it, so the default
        follows :attr:`same_session`.
        """
        if include_history is None:
            include_history = not self.same_session
        parts = [
            "## The task this flow is executing",
            self.task_description or "(no description recorded)",
            "",
            "## Flow steps and their status (▶ marks where it was interrupted)",
            "\n".join(self.step_lines) or "(no steps recorded)",
            "",
            "## Workspace changes so far",
            self.workspace_summary or "(clean working tree)",
        ]
        if self.group_lines:
            parts += [
                "",
                "## Parallel implementation groups",
                "\n".join(self.group_lines),
            ]
        if include_history and self.rebuilt_history:
            parts += [
                "",
                "## Reconstructed conversation for the interrupted step",
                self.rebuilt_history,
            ]
        return "\n".join(parts)


DIALOG_SYSTEM_PROMPT = """\
You have been interrupted mid-run by the user, who wants to talk to you about \
what you are doing. You are READ-ONLY for the whole of this conversation: do \
not edit, create or delete anything, and do not run commands with side effects.

Your job in each turn is either to answer/ask, or to settle what should happen \
next. Reply with ONLY a JSON object of this shape:

```json
{
  "mode": "question" | "decision",
  "content": "what you say to the user, in their language",
  "decision": {
    "action": "continue" | "restart" | "exit",
    "instruction": "a temporary instruction for THIS step's next run, or \"\"",
    "revised_description": "a COMPLETE replacement task description, or \"\"",
    "restart_step_id": "step id to restart from (restart only), or \"\"",
    "workspace": "keep" | "reset"
  }
}
```

Rules:
- Use "question" while you still need to understand what the user wants, or \
when they are just asking you something. Set "decision" to null then.
- Use "decision" only once you and the user have settled what to do.
- Choosing between "instruction" and "revised_description" is YOUR judgement, \
and it matters: an "instruction" is a one-off nudge for the current step and is \
forgotten afterwards; a "revised_description" replaces what the whole task IS, \
persists for every later step, and becomes what the quality checks accept \
against. If the user corrected the REQUIREMENT, write the full revised \
description (not a diff, not a note — the whole thing). If they only corrected \
HOW you are doing this step right now, use "instruction".
- "continue" resumes the interrupted step. "restart" rewinds the flow to \
"restart_step_id" and runs it fresh. "exit" saves and stops.
- "workspace": "keep" leaves the working tree as it is; "reset" throws the \
flow's changes away and returns the tree to how it was before the flow started. \
Only ever propose "reset" when the user has clearly asked to start over.
- Answer "content" in the same language the user is writing to you in.
"""


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------


def _step_status_lines(flow: Any) -> List[str]:
    lines: List[str] = []
    current = getattr(flow.state, "current_step_id", None)
    for sid in list(getattr(flow.state, "step_history", []) or []):
        step = flow.state.steps.get(sid)
        if step is None:
            continue
        step_type = (
            step.step_type.value
            if hasattr(step.step_type, "value")
            else str(step.step_type)
        )
        status = (
            step.status.value if hasattr(step.status, "value") else str(step.status)
        )
        marker = "▶" if sid == current else " "
        lines.append(f"{marker} {sid}  {step_type}  [{status}]")
    return lines


def workspace_change_summary(
    project_root: Path, baseline_commit: str = "", max_chars: int = _DIFF_SUMMARY_MAX_CHARS
) -> str:
    """Summarise what the flow has done to the tree, for the dialog prompt.

    Deliberately a *summary* (status + diffstat), never a full diff: in the
    same-session case the agent already knows its own edits, and in the
    fallback case a full diff would drown the actual question.
    """
    parts: List[str] = []
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(project_root), capture_output=True, text=True, timeout=30,
        )
        if status.returncode == 0 and status.stdout.strip():
            parts.append("git status --porcelain:\n" + status.stdout.strip())
        ref = baseline_commit or "HEAD"
        stat = subprocess.run(
            ["git", "diff", "--stat", ref],
            cwd=str(project_root), capture_output=True, text=True, timeout=30,
        )
        if stat.returncode == 0 and stat.stdout.strip():
            parts.append(f"git diff --stat {ref[:8] or ref}:\n" + stat.stdout.strip())
    except Exception:  # noqa: BLE001 - a summary is never worth failing over
        logger.debug("Failed to summarise workspace changes", exc_info=True)
    text = "\n\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n... (truncated)"
    return text


def _step_fix_iteration(flow: Any, step: Any) -> int:
    """The fix-loop iteration the interrupted call ran under.

    WHY the step's own input wins over ``flow.state.fix_iterations``: that is
    the exact value the LLMCaller was constructed with, so it is what tagged
    the records now being rebuilt. The flow-level counter is only the fallback
    for a step whose inputs never carried one (a DAG group step object, legacy
    state), where it is the same number by construction.
    """
    try:
        value = int((getattr(step, "inputs", None) or {}).get("fix_iteration", 0) or 0)
    except (TypeError, ValueError):
        value = 0
    if value:
        return value
    try:
        return int(getattr(getattr(flow, "state", None), "fix_iterations", 0) or 0)
    except (TypeError, ValueError):
        return 0


def build_dialog_context(
    flow: Any,
    step: Any,
    project_root: Path,
    *,
    binding: Optional[Dict[str, Any]] = None,
    include_rebuilt_history: bool = True,
) -> DialogContext:
    """Assemble the flow-side context injected into every dialog turn.

    The rebuilt step conversation is always ASSEMBLED, and
    :meth:`DialogContext.render` decides whether to emit it. WHY it is not
    skipped in the same-session case: whether the working agent's session is
    actually reachable is only knowable at call time — the agent may have been
    removed from the chain, its runner may not support resume, or the provider
    may reject the session — and the standalone assistant that then answers
    instead must not be left without the interrupted step's conversation.
    """
    from .state_machine import _compose_effective_task_description

    step_id = getattr(step, "step_id", "") or ""
    step_type = ""
    if step is not None:
        step_type = (
            step.step_type.value
            if hasattr(getattr(step, "step_type", None), "value")
            else str(getattr(step, "step_type", ""))
        )
    rebuilt = ""
    if include_rebuilt_history and step_id:
        try:
            from .chat_history import format_history_for_retry
            from .rewind import step_generation

            # Scoped to the in-flight fix iteration as well as the current
            # generation: the implement step re-uses one step_id across fix
            # iterations, so an unscoped rebuild would hand the dialog agent
            # superseded instructions from an earlier iteration alongside the
            # live ones.
            rebuilt = (
                format_history_for_retry(
                    project_root, flow.flow_id, step_id,
                    current_fix_iteration=_step_fix_iteration(flow, step),
                    current_generation=step_generation(flow, step_id),
                )
                or ""
            )
        except Exception:  # noqa: BLE001 - degrade to no history
            logger.debug("Failed to rebuild step history for dialog", exc_info=True)
    return DialogContext(
        task_description=_compose_effective_task_description(flow),
        step_lines=_step_status_lines(flow),
        current_step_id=step_id,
        current_step_type=step_type,
        workspace_summary=workspace_change_summary(
            project_root, str(getattr(flow, "baseline_commit", "") or "")
        ),
        same_session=binding is not None,
        rebuilt_history=rebuilt,
        group_lines=group_session_lines(flow, step, project_root),
    )


def group_session_lines(flow: Any, step: Any, project_root: Path) -> List[str]:
    """Summarise each DAG group of a parallel implement step.

    WHY the dialog needs this and cannot simply resume one group's session:
    when a parallel implement is interrupted there is no single agent to talk
    to — there are N, each in its own worktree. The conversation therefore
    happens at the *scheduling* level, and what it needs is which groups
    finished, which were still running, which never started (and why), what
    depends on what, and which sessions each of them holds, so the decision
    can be made about the step as a whole.
    """
    from .models import StepType

    if getattr(step, "step_type", None) != StepType.IMPLEMENT:
        return []
    groups: List[Dict[str, Any]] = []
    group_ids: List[str] = []
    seen = set()
    for value in (step.inputs or {}).get("task_groups") or []:
        if isinstance(value, dict):
            gid = value.get("group_id") or value.get("id") or value.get("name")
            groups.append(value if gid else {})
        else:
            gid = value
            groups.append({})
        if gid and str(gid) not in seen:
            seen.add(str(gid))
            group_ids.append(str(gid))
    if len(group_ids) < 2:
        # A single group is a whole-task call, not a parallel schedule.
        return []
    done = {
        str(g) if not isinstance(g, dict) else str(g.get("group_id") or "")
        for g in ((step.outputs or {}).get("implemented_groups") or [])
    }
    # Per-group outcome recorded by the interrupted run itself. This is what
    # separates "was in flight when the stop landed" from "never started
    # because a dependency failed" — labelling both "interrupted" would tell
    # the user (and the dialog agent) the wrong story about what is where.
    preserved = (step.outputs or {}).get("dag_preserved_worktrees") or {}
    summaries: Dict[str, str] = {}
    for entry in (step.outputs or {}).get("group_summaries") or []:
        if isinstance(entry, dict) and entry.get("group_id"):
            summaries[str(entry["group_id"])] = str(entry.get("summary") or "")
    fix_iteration = _step_fix_iteration(flow, step)
    step_id = getattr(step, "step_id", "") or ""
    group_meta = {gid: meta for gid, meta in zip(group_ids, groups)}
    lines: List[str] = []
    for gid in group_ids:
        record = preserved.get(gid) if isinstance(preserved, dict) else None
        recorded_status = (
            str(record.get("status") or "") if isinstance(record, dict) else ""
        )
        if gid in done:
            state = "completed"
        elif recorded_status and recorded_status != "completed":
            state = recorded_status
        else:
            state = "interrupted / not started"
        deps = (group_meta.get(gid) or {}).get("depends_on") or []
        deps_note = f"; depends on: {', '.join(str(d) for d in deps)}" if deps else ""
        group_step_id = f"{step_id}_{gid}"
        binding = None
        try:
            from .chat_history import last_session_binding
            from .rewind import step_generation

            binding = last_session_binding(
                project_root, flow.flow_id, group_step_id,
                fix_iteration=fix_iteration,
                generation=step_generation(flow, group_step_id),
            )
        except Exception:  # noqa: BLE001 - a summary is never worth failing over
            logger.debug("Failed to read group session for %s", gid, exc_info=True)
        if binding and binding.get("provider_session_id"):
            lines.append(
                f"- {gid}: {state}{deps_note}; agent={binding.get('agent_name') or '?'} "
                f"session={binding['provider_session_id']} "
                f"cwd={binding.get('session_cwd') or '?'}"
            )
        else:
            lines.append(f"- {gid}: {state}{deps_note}; no recorded agent session")
        # WHY the substance and not just the identity: the dialog happens at
        # the SCHEDULING level, so the user is asking about work done by agents
        # this conversation is not talking to. Session ids answer "can it be
        # resumed"; they answer nothing about what a group changed, attempted,
        # or was doing when it stopped — which is the whole subject of the
        # interruption. A completed group's persisted summary and an
        # interrupted group's salvaged conversation are the only records of it.
        summary = summaries.get(gid, "").strip()
        if summary:
            lines.append(_indent_block("summary: " + summary, limit=_GROUP_SUMMARY_MAX_CHARS))
        excerpt = _group_conversation_excerpt(
            flow, project_root, group_step_id, fix_iteration=fix_iteration
        )
        if excerpt:
            lines.append(_indent_block("conversation so far:\n" + excerpt))
    return lines


#: Per-group caps for the DAG dialog context. Generous enough to say what a
#: group actually did, small enough that N groups cannot crowd out the user's
#: own question.
_GROUP_SUMMARY_MAX_CHARS = 800
_GROUP_HISTORY_MAX_CHARS = 2000


def _indent_block(text: str, limit: Optional[int] = None) -> str:
    """Indent a multi-line block under its group bullet, truncating to *limit*."""
    if limit is not None and len(text) > limit:
        text = text[:limit] + " ... (truncated)"
    return "\n".join("  " + line for line in text.splitlines())


def _group_conversation_excerpt(
    flow: Any, project_root: Path, group_step_id: str, *, fix_iteration: int = 0
) -> str:
    """Tail of a group's salvaged conversation, for the scheduling-level dialog.

    The TAIL specifically: what the group was doing when it stopped is the part
    the user interrupted to ask about, and the opening prompt is already
    implied by the task groups shown elsewhere in the context.
    """
    try:
        from .chat_history import format_history_for_retry
        from .rewind import step_generation

        text = (
            format_history_for_retry(
                project_root, flow.flow_id, group_step_id,
                current_fix_iteration=fix_iteration,
                current_generation=step_generation(flow, group_step_id),
            )
            or ""
        ).strip()
    except Exception:  # noqa: BLE001 - a summary is never worth failing over
        logger.debug(
            "Failed to rebuild group conversation for %s", group_step_id,
            exc_info=True,
        )
        return ""
    if not text:
        return ""
    if len(text) > _GROUP_HISTORY_MAX_CHARS:
        text = "... (earlier turns omitted)\n" + text[-_GROUP_HISTORY_MAX_CHARS:]
    return text


# ---------------------------------------------------------------------------
# Session lookup
# ---------------------------------------------------------------------------


def find_dialog_session(
    flow: Any, step: Any, project_root: Path
) -> Optional[Dict[str, Any]]:
    """Locate the provider session of the agent that produced *step*'s work.

    Returns ``None`` when there is nothing to talk to — a non-LLM step (TEST /
    COMMIT / merge), a step whose runner has no session, a flow configured for
    ``rebuild``, or simply a step that never got as far as one LLM call. Every
    one of those routes the dialog to the independent read-only call instead.
    """
    step_id = getattr(step, "step_id", "") or ""
    if not step_id:
        return None
    try:
        from ..config import load_resume_strategy

        if load_resume_strategy(project_root) != "native":
            return None
    except Exception:  # pragma: no cover - defensive
        pass
    try:
        from .chat_history import last_session_binding
        from .rewind import step_generation

        fix_iteration = 0
        try:
            fix_iteration = int(getattr(flow.state, "fix_iterations", 0) or 0)
        except (TypeError, ValueError):
            fix_iteration = 0
        return last_session_binding(
            project_root, flow.flow_id, step_id,
            fix_iteration=fix_iteration,
            generation=step_generation(flow, step_id),
        )
    except Exception:  # noqa: BLE001
        logger.debug("Failed to locate a dialog session", exc_info=True)
        return None


def _agent_entry_for(project_root: Path, name: Optional[str]) -> Optional[Dict[str, Any]]:
    """Return the configured agent entry called *name*, if it still exists."""
    if not name:
        return None
    try:
        from ..config import resolve_agents

        agents, _ = resolve_agents(project_root, None)
    except Exception:  # pragma: no cover - defensive
        return None
    for agent in agents or []:
        if agent.get("name") == name:
            return agent
    return None


def session_cwd_reachable(binding: Optional[Dict[str, Any]]) -> bool:
    """Whether the directory a recorded session is bound to still exists.

    WHY this gates the binding rather than only the native attempt: a provider
    session is addressable only from the cwd it was opened in, and LLMCaller
    runs BOTH the native resume and its rebuild fallback in the cwd it was
    constructed with. A DAG group worktree removed between the interruption and
    the dialog would therefore fail twice and leave the round with no answer at
    all, where the contract calls for a standalone read-only call in the live
    flow workspace.
    """
    if not binding:
        return False
    cwd = binding.get("session_cwd")
    if not cwd:
        # Nothing recorded: the session belongs to the flow workspace itself,
        # which is where the dialog runs anyway.
        return True
    try:
        return Path(cwd).is_dir()
    except OSError:  # pragma: no cover - defensive
        return False


def resolve_session_agent(
    project_root: Path, binding: Optional[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """The agent entry able to continue *binding*'s session, or ``None``.

    Mirrors LLMCaller's own resume precondition (the recorded agent is still
    configured AND its runner declares native resume) so the dialog does not
    promise the user a same-session conversation the caller would then have to
    fall back out of. It is a check, never a decision: the strategy choice
    stays with LLMCaller.
    """
    if not binding or not binding.get("provider_session_id"):
        return None
    if not session_cwd_reachable(binding):
        logger.info(
            "Dialog cannot use the recorded session: its cwd %r no longer "
            "exists; falling back to a standalone read-only call",
            binding.get("session_cwd"),
        )
        return None
    agent = _agent_entry_for(project_root, binding.get("agent_name"))
    if agent is None:
        logger.info(
            "Dialog cannot use the recorded session: agent %r is no longer "
            "configured; falling back to a standalone read-only call",
            binding.get("agent_name"),
        )
        return None
    # INVARIANT: name AND runner type must both still match, exactly as
    # LLMCaller._agent_index_for_binding requires. A session id means nothing
    # without the runner that owns it, so an agent re-pointed from claude-code
    # to codex invalidates the binding. Checking only the name here left the
    # dialog claiming (and recording turns under) a session LLMCaller had
    # already refused to resume.
    recorded_type = str(binding.get("runner_type") or "")
    configured_type = str(agent.get("type", "claude-code"))
    if recorded_type != configured_type:
        logger.info(
            "Dialog cannot use the recorded session: agent %r is now runner "
            "type %r but the session was opened by %r",
            binding.get("agent_name"), configured_type,
            recorded_type or "(unrecorded)",
        )
        return None
    try:
        from .llm_caller import runner_supports_native_resume

        if not runner_supports_native_resume(agent.get("type")):
            logger.info(
                "Dialog cannot use the recorded session: runner type %r does "
                "not support native resume",
                agent.get("type"),
            )
            return None
    except Exception:  # pragma: no cover - defensive
        logger.debug("Failed to probe resume capability", exc_info=True)
        return None
    return agent


# ---------------------------------------------------------------------------
# The dialog session
# ---------------------------------------------------------------------------


class InterjectionDialog:
    """Multi-turn read-only conversation held at a flow's breakpoint."""

    def __init__(
        self,
        flow: Any,
        step: Any,
        project_root: Path,
        *,
        binding: Optional[Dict[str, Any]] = None,
        caller_factory: Optional[Any] = None,
    ) -> None:
        self.flow = flow
        self.step = step
        self.project_root = Path(project_root)
        self._caller_factory = caller_factory
        # A binding is only a *promise* of a same-session conversation until
        # the agent that owns it is confirmed to still exist and to have a
        # runner that can resume. Resolved here so the UI's "talking to agent
        # X" claim and the prompt's history decision are both truthful.
        self._session_agent = (
            None
            if caller_factory is not None
            else resolve_session_agent(self.project_root, binding)
        )
        if caller_factory is None and self._session_agent is None:
            binding = None
        self.binding = binding
        self.turns: List[Dict[str, Any]] = []
        self.context = build_dialog_context(
            flow, step, self.project_root, binding=binding
        )
        self._step_id = getattr(step, "step_id", "") or ""
        self._step_type = self.context.current_step_type

    # -- prompting ------------------------------------------------------

    def _build_prompt(
        self, user_text: str, *, include_history: Optional[bool] = None
    ) -> str:
        parts = [
            DIALOG_SYSTEM_PROMPT,
            "",
            self.context.render(include_history=include_history),
        ]
        if self.turns:
            history = []
            for turn in self.turns:
                speaker = "User" if turn["role"] == "user" else "You"
                history.append(f"{speaker}: {turn['content']}")
            parts += ["", "## This conversation so far", "\n".join(history)]
        parts += ["", "## The user's message", user_text]
        return "\n".join(parts)

    def _make_caller(self, fallback_prompt: Optional[str] = None) -> Any:
        if self._caller_factory is not None:
            return self._caller_factory(self)
        from .llm_caller import LLMCaller

        cwd = self.project_root
        agents = None
        resume_binding = None
        if self.binding is not None and self._session_agent is not None:
            agents = [self._session_agent]
            resume_binding = self.binding
            recorded_cwd = self.binding.get("session_cwd")
            # Only a cwd that still exists is adopted: LLMCaller runs the
            # rebuild fallback in the same directory as the native attempt, so
            # pointing it at a deleted worktree would fail both.
            if recorded_cwd and Path(recorded_cwd).is_dir():
                cwd = Path(recorded_cwd)
        # WHY no flow_id / step_id: everything LLMCaller would record here is
        # the MACHINE-facing prompt (system contract + injected flow context).
        # The human-facing conversation is written separately as ``dialog``
        # records, so a later rebuilt retry context replays what was actually
        # said and not the scaffolding around it.
        return LLMCaller(
            project_root=cwd,
            step_type="interjection_dialog",
            force_read_only=True,
            agents=agents,
            resume_binding=resume_binding,
            resume_fallback_prompt=fallback_prompt,
            # The dialog is held while the user decides what happens to the
            # workspace, so its read-only lock must also close the shell: the
            # Claude CLIs run with --dangerously-skip-permissions, where denying
            # only the edit tools would leave `rm`/`>` reachable through Bash.
            deny_shell=True,
            max_retries=2,
        )

    def ask(self, user_text: str) -> DialogTurn:
        """Send *user_text* and return the agent's structured reply."""
        # Re-checked per round, not only at construction: a DAG group worktree
        # can be cleaned up while the dialog is open, and an unreachable cwd
        # must degrade to the standalone conversation BEFORE the prompts are
        # built, so this round's prompt carries the rebuilt step history.
        if self.binding is not None and not session_cwd_reachable(self.binding):
            self._drop_binding(
                "its recorded cwd %r no longer exists"
                % (self.binding or {}).get("session_cwd")
            )
        self.record_user_turn(user_text)
        # Two prompts, one per interlocutor. The session-relative one omits the
        # reconstructed step conversation (the working agent lives it); the
        # fallback carries it, because whoever answers instead of that session
        # — a standalone assistant, or the rebuild after the provider refused
        # the session — has no other way to see what the step was doing.
        prompt = self._build_prompt(user_text)
        fallback_prompt = (
            self._build_prompt(user_text, include_history=True)
            if self.binding is not None
            else None
        )
        caller = self._make_caller(fallback_prompt)
        try:
            raw = caller.call(
                prompt,
                json_mode="extract",
                required_keys=["mode"],
                json_schema_hint=(
                    '{"mode": "question|decision", "content": "...", '
                    '"decision": {"action": "continue|restart|exit", '
                    '"instruction": "", "revised_description": "", '
                    '"restart_step_id": "", "workspace": "keep|reset"}}'
                ),
            )
        finally:
            # Checked on BOTH outcomes: whether the fallback call then answered
            # or failed, the session itself is gone and the dialog must stop
            # addressing it.
            self._note_resume_outcome(caller)
        turn = parse_dialog_reply(raw)
        self.record_assistant_turn(turn)
        return turn

    def _note_resume_outcome(self, caller: Any) -> None:
        """Drop the session binding once the provider has refused it.

        LLMCaller soft-falls-back from a rejected native resume, so the answer
        the user just read came from a STANDALONE interlocutor. Keeping the
        binding after that would (a) spend one attempt of every later round
        re-probing a dead session, (b) stamp those rounds' history records with
        a session id that no longer exists, and (c) leave both front ends
        claiming "talking to <agent> in its own session", which is simply untrue.
        """
        if self.binding is None:
            return
        if not getattr(caller, "native_resume_rejected", False):
            return
        self._drop_binding(
            "the provider refused session %s"
            % ((self.binding or {}).get("provider_session_id"),)
        )

    def _drop_binding(self, reason: str) -> None:
        """Degrade to the standalone read-only conversation, stating *reason*.

        The context was built for a session-relative conversation (it omits the
        reconstructed step history the working agent was assumed to hold), so it
        is rebuilt here — whoever answers from now on has no other way to see
        what the step was doing.
        """
        if self.binding is None:
            return
        logger.info(
            "Dialog falls back to a standalone read-only conversation: %s",
            reason,
        )
        self.binding = None
        self._session_agent = None
        try:
            self.context = build_dialog_context(
                self.flow, self.step, self.project_root, binding=None
            )
        except Exception:  # noqa: BLE001 - a rebuild fault must not end the dialog
            logger.debug("Failed to rebuild the dialog context", exc_info=True)

    # -- recording ------------------------------------------------------

    def record_user_turn(self, text: str, source: str = "terminal") -> None:
        self.turns.append({"role": "user", "content": text})
        self._write_record("user", text, source)

    def record_assistant_turn(self, turn: DialogTurn) -> None:
        self.turns.append({"role": "assistant", "content": turn.content})
        self._write_record("assistant", turn.content, "llm")

    def _write_record(self, role: str, content: str, source: str) -> None:
        if not self._step_id or not content:
            return
        try:
            from .chat_history import record_dialog_message
            from .rewind import step_generation

            record_dialog_message(
                self.project_root, self.flow.flow_id, self._step_id,
                self._step_type, role, content,
                attempt=int((self.step.inputs or {}).get("retry_count", 0) or 0),
                fix_iteration=int(getattr(self.flow.state, "fix_iterations", 0) or 0),
                generation=step_generation(self.flow, self._step_id),
                agent_name=(self.binding or {}).get("agent_name"),
                provider_session_id=(self.binding or {}).get("provider_session_id"),
                # A dialog record is the newest session-bearing line in the step
                # jsonl, so it shadows the response record a later native resume
                # would bind to. It must therefore carry the FULL binding, or the
                # shadowed lookup resolves to an unusable one and forces a rebuild.
                runner_type=(self.binding or {}).get("runner_type"),
                session_cwd=(self.binding or {}).get("session_cwd"),
                source=source,
            )
        except Exception:  # noqa: BLE001 - history must never break the dialog
            logger.debug("Failed to record dialog turn", exc_info=True)

    def transcript(self) -> List[Dict[str, Any]]:
        return list(self.turns)


def parse_dialog_reply(raw: str) -> DialogTurn:
    """Parse the dialog LLM's JSON reply into a :class:`DialogTurn`.

    Total by contract: an unparseable reply becomes a ``question`` turn
    carrying the raw text. Losing the conversation because the model wrapped
    its JSON in prose would be a far worse failure than showing the user a
    slightly ugly answer.
    """
    text = (raw or "").strip()
    data: Any = None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        from .utils.json_parser import parse_json_response

        try:
            data = parse_json_response(text)
        except Exception:  # noqa: BLE001
            data = None
    if not isinstance(data, dict):
        return DialogTurn(mode=MODE_QUESTION, content=text, raw=text)
    mode = str(data.get("mode") or MODE_QUESTION).strip().lower()
    if mode not in (MODE_QUESTION, MODE_DECISION):
        mode = MODE_QUESTION
    content = str(data.get("content") or "").strip()
    decision = DialogDecision.from_dict(data.get("decision"))
    if mode == MODE_DECISION and decision is None:
        # A "decision" with no decision body is a question in disguise.
        mode = MODE_QUESTION
    if mode != MODE_DECISION:
        decision = None
    return DialogTurn(mode=mode, content=content or text, decision=decision, raw=text)


# ---------------------------------------------------------------------------
# Direct decisions (no LLM)
# ---------------------------------------------------------------------------


def parse_direct_decision(text: str) -> Optional[DialogDecision]:
    """Recognise a decision the user typed straight into the dialog.

    Lets an operator who already knows what they want skip the conversation
    entirely — including the zero-cost case: an EMPTY message means "change
    nothing, resume now" and must never spend an LLM call.

    Accepted forms (case-insensitive): the bare verbs ``continue`` / ``restart``
    / ``exit`` (and their common aliases); ``restart`` may additionally carry a
    step id and a ``reset`` / ``keep`` workspace word, in either order.

    INVARIANT: recognition is all-or-nothing — every token has to be accounted
    for, or the whole message returns ``None`` and goes to the LLM. A message
    that merely STARTS with a verb ("continue use postgres") is natural
    language, and treating it as a bare decision dropped the operator's actual
    instruction on the floor; only the LLM can tell whether such a tail is a
    one-off instruction or a revised description.
    """
    if text is None:
        return None
    stripped = text.strip()
    if not stripped:
        return DialogDecision(action=ACTION_CONTINUE)
    tokens = stripped.split()
    verb = tokens[0].lower().lstrip("/")
    aliases = {
        "continue": ACTION_CONTINUE, "c": ACTION_CONTINUE, "go": ACTION_CONTINUE,
        "resume": ACTION_CONTINUE, "继续": ACTION_CONTINUE,
        "restart": ACTION_RESTART, "r": ACTION_RESTART, "redo": ACTION_RESTART,
        "rewind": ACTION_RESTART, "重来": ACTION_RESTART, "回退": ACTION_RESTART,
        "exit": ACTION_EXIT, "quit": ACTION_EXIT, "q": ACTION_EXIT,
        "stop": ACTION_EXIT, "退出": ACTION_EXIT,
    }
    action = aliases.get(verb)
    if action is None:
        return None
    decision = DialogDecision(action=action)
    # Tracked separately from the fields themselves: ``workspace`` defaults to
    # a real value ("keep"), so the field cannot say whether the user typed one.
    workspace_seen = False
    step_id_seen = False
    for token in tokens[1:]:
        lowered = token.lower()
        if action != ACTION_RESTART:
            # A tail on ``continue`` / ``exit`` carries no direct-decision
            # meaning, so it can only be prose.
            return None
        if lowered in WORKSPACES and not workspace_seen:
            decision.workspace = lowered
            workspace_seen = True
        elif not step_id_seen:
            decision.restart_step_id = token
            step_id_seen = True
        else:
            # An unaccounted-for token: this is prose, not a direct decision.
            return None
    return decision


# ---------------------------------------------------------------------------
# Applying a confirmed decision
# ---------------------------------------------------------------------------


@dataclass
class DecisionOutcome:
    """What applying a decision did, for the caller to render and act on."""

    action: str = ACTION_CONTINUE
    ok: bool = True
    error: str = ""
    #: Populated for ``restart``.
    rewind: Optional[Any] = None
    reset: Optional[Any] = None
    revised: bool = False
    #: Safety refs holding the DAG group work the restart discarded. Carried
    #: separately from ``rewind`` because the groups are captured and removed
    #: while PLANNING the rewind — a restart that then fails on the workspace
    #: reset must still tell the operator where that work went.
    preserved_refs: List[str] = field(default_factory=list)
    #: Steps whose recorded DAG group results a REFUSED restart had to drop:
    #: the groups' worktrees and leaf branches were already gone by the time
    #: the restart was refused, so those groups will run again on the next
    #: continuation. Reported so the operator is never told "nothing happened"
    #: about work that will now be redone.
    invalidated_group_steps: List[str] = field(default_factory=list)


def apply_decision(
    flow: Any,
    step: Any,
    decision: DialogDecision,
    project_root: Path,
    *,
    dialog_summary: str = "",
    continue_reenters_step: bool = True,
) -> DecisionOutcome:
    """Apply a user-confirmed dialog decision to the flow.

    Does NOT persist — the caller owns the save, because it also owns what
    happens next (re-enter the loop, return to a pause point, or exit).

    ``continue_reenters_step`` is False when the dialog was opened AT a pause
    point (a CONFIRM gate, a failure decision). ``continue`` there means "go
    back to waiting at that gate", so the step's status and retry counter must
    not move: re-arming the reviewed producer would corrupt its terminal
    status, and re-arming a failed step would either rerun it without ever
    showing Retry/Skip/Abort again or double-count its retries.
    """
    from .models import StepType
    from .state_machine import record_description_revision

    outcome = DecisionOutcome(action=decision.action)

    # INVARIANT: a confirmed revision is recorded for EVERY action, ``exit``
    # included. It is a fact about the requirement, not about this step's next
    # run — an operator who corrects the requirement and then saves and leaves
    # must find the correction still in force when they resume, not a flow
    # running against the description they just superseded.
    if decision.revised_description:
        record_description_revision(
            flow,
            decision.revised_description,
            step_id=getattr(step, "step_id", "") or "",
        )
        outcome.revised = True
        if StepType.SELF_CHECK in (getattr(flow.state, "selected_steps", None) or []):
            from .review_scope import SelfCheckRoundController

            SelfCheckRoundController(flow.state.context).force_full(
                "effective_requirements_changed"
            )

    if decision.action == ACTION_EXIT:
        if continue_reenters_step:
            if decision.revised_description or decision.instruction:
                # The step is deliberately left RUNNING (no re-arm, no retry
                # bump) — but the `--resume` that eventually picks it up only
                # flips those retry flags, and a native resume then sends
                # nothing but the generic continuation directive. Recomposing
                # the description here, and attaching the note that directive
                # carries, is what stops that resumed agent from carrying on
                # against the requirement the operator just superseded.
                _attach_dialog_note(step, decision, dialog_summary=dialog_summary)
                _recompose_step_description(flow, step)
        else:
            # INVARIANT: leaving the flow RESOLVES the pause, so no one-shot
            # instruction survives it. A gate note is scoped to the single
            # execution launched straight out of THIS pause (the failure gate's
            # Retry); once the operator saves and walks away, the next run
            # re-enters through a different pause entirely, and delivering the
            # instruction there would apply it to a run it was never about.
            # Parked notes from an earlier `continue` at this same pause go
            # with it.
            discard_gate_note(flow)
            if decision.revised_description:
                # The revision is flow-level and outlives the pause, so the
                # step it will be resumed into must not keep the superseded
                # description in its inputs.
                _recompose_step_description(flow, step)
        return outcome

    if decision.action == ACTION_CONTINUE:
        if continue_reenters_step:
            _attach_dialog_note(step, decision, dialog_summary=dialog_summary)
            _rearm_step_as_retry(flow, step, decision)
        else:
            # A pause-point ``continue`` re-runs NOTHING (decision 4: status,
            # retry counter and resume flag all stay put). The instruction is
            # parked as a one-shot note on the gate instead: only the failure
            # gate's Retry — the one execution of this step launched straight
            # out of this same pause — consumes it; every other resolution
            # (approve, skip, abort, rewind) discards it. Parking it in the
            # step's inputs instead would leak it into the next FIX-LOOP
            # re-entry of the step object, which is a different run entitled
            # to none of it.
            _park_gate_note(flow, step, decision, dialog_summary)
            if decision.revised_description:
                # The step IS still going to run again (the failure gate's
                # Retry, the CONFIRM gate's change request), and it would
                # otherwise run against the description the operator has just
                # replaced. Recomposing the inputs is the one part of the
                # re-arm that belongs here.
                _recompose_step_description(flow, step)
        return outcome

    # ---- restart -------------------------------------------------------
    from .rewind import (
        RewindError,
        invalidate_discarded_group_state,
        prepare_rewind,
        rewind_to_step,
    )

    # INVARIANT: EVERY refusal a restart can hit is decided BEFORE the workspace
    # is touched — an unresolvable target, a target with no entry snapshot, and
    # a discarded DAG group whose work can neither be captured to a safety ref
    # nor removed. Resetting first and only then discovering a refusal would
    # leave the tree emptied to ``baseline_commit`` while every step still
    # claims to be done: a flow running against a workspace its own state says
    # is full. Planning first makes a refused restart leave the tree exactly as
    # it was, whatever the refusal was.
    try:
        plan = prepare_rewind(
            flow, decision.restart_step_id or None, project_root=project_root
        )
    except RewindError as exc:
        outcome.ok = False
        outcome.error = str(exc)
        # A cleanup that failed part way already deleted some group branches
        # and dropped the step state naming them; carry that out so the
        # operator hears about the re-run rather than only about the refusal.
        outcome.invalidated_group_steps = list(
            getattr(exc, "invalidated_group_steps", None) or []
        )
        return outcome

    outcome.preserved_refs = list(plan.preserved_refs)

    if decision.workspace == WORKSPACE_RESET:
        from .flow_workspace import reset_workspace_to_baseline

        reset = reset_workspace_to_baseline(flow, project_root)
        outcome.reset = reset
        if not reset.ok:
            # The plan already removed the discarded groups' worktrees, but
            # their content went to safety refs first and the flow's own tree
            # is untouched — so the operator can fix git and re-issue the same
            # restart, which re-plans over branches that are simply gone.
            #
            # What must NOT survive is the interrupted implement step's belief
            # that those groups are done: the flow stays runnable from here
            # (the operator can press Enter and just continue), and a
            # continuation that skipped a completed group would then find its
            # leaf branch deleted at merge time and report work it does not
            # have. Dropping the record costs a re-run; keeping it loses the
            # work.
            outcome.ok = False
            outcome.error = reset.error
            outcome.invalidated_group_steps = invalidate_discarded_group_state(
                flow, plan
            )
            return outcome

    # Committing the plan is pure state mutation, so the reset above is never
    # followed by a refusal that would strand the tree.
    outcome.rewind = rewind_to_step(
        flow, plan.target_step_id, project_root=project_root, plan=plan,
        # With ``keep`` the discarded attempt's edits are still on disk, so the
        # target's pre-step workspace baseline must travel to the rebuilt step
        # or its net-zero-diff guard would measure those leftovers against
        # themselves. A ``reset`` has just put the tree back to the flow's own
        # baseline, so there is nothing to expose and a fresh capture is the
        # accurate one.
        carry_step_inputs=decision.workspace != WORKSPACE_RESET,
    )

    # The rewind deleted the target step object; the run loop rebuilds it via
    # ``StateMachine.rebuild_rewound_step`` on its next turn. The request is
    # left in flow state rather than executed here so this module stays free of
    # the state machine — and so it survives the process boundary of the
    # json/daemon path, where the dialog and the rebuild happen in different
    # processes.
    if outcome.rewind is not None and outcome.rewind.target_step_type:
        flow.state.context["pending_rewind_step_type"] = (
            outcome.rewind.target_step_type
        )

    # The only other thing to carry across is the conversation's conclusion —
    # recorded as a pending note the rebuilt step's prompt picks up.
    note = _restart_note(
        decision,
        dialog_summary,
        discarded_groups=list(
            getattr(outcome.rewind, "cleaned_worktrees", None) or []
        ),
        preserved_refs=list(
            getattr(outcome.rewind, "preserved_refs", None) or []
        ),
    )
    if note:
        flow.state.context["pending_dialog_note"] = note
    else:
        flow.state.context.pop("pending_dialog_note", None)
    return outcome


def _attach_dialog_note(
    step: Any, decision: DialogDecision, *, dialog_summary: str = ""
) -> None:
    """Park the dialog's conclusion on *step* for its next execution.

    INVARIANT: the temporary instruction is scoped to THIS step. It travels in
    the step's own ``inputs`` — not in LLMCaller's process-global one-shot
    slot — because a step that makes no LLM call at all (TEST, COMMIT, merge)
    would never consume that slot, and the instruction would then surface in
    whatever unrelated LLM step ran next. ``run_step`` pops it and arms the
    injection channel for the duration of that one execution.
    """
    inputs = step.inputs if step.inputs is not None else {}
    notice_parts: List[str] = []
    if decision.instruction:
        notice_parts.append(decision.instruction)
    if decision.revised_description:
        # Stated in the note as well as in the recomposed description: a
        # continuation prompt (native resume in particular) sends only the new
        # user turn, so an agent that has been working to the OLD requirement
        # for many turns needs the replacement said out loud, not merely swapped
        # into a header it will not re-read.
        notice_parts.append(
            "The task description has been replaced. Work to this from now on:\n"
            + decision.revised_description
        )
    if dialog_summary:
        notice_parts.append(
            "Conclusion of the interruption dialog:\n" + dialog_summary
        )
    if notice_parts:
        inputs["dialog_note"] = "\n\n".join(notice_parts)
    else:
        inputs.pop("dialog_note", None)
    # Selects the "you were interrupted, the discussion has concluded" framing
    # of the continuation directive rather than the "the previous attempt
    # failed" one; carried per step for the same reason as the note.
    inputs["dialog_resume"] = True
    step.inputs = inputs


#: ``flow.state.context`` key for the one-shot instruction parked at a pause
#: point. Deliberately NOT in ``FLOW_LEVEL_CONTEXT_KEYS``: it is pause-scoped
#: derived state, so a rewind wipes it with everything else the pause hung on.
PENDING_GATE_NOTE_KEY = "pending_gate_note"


def _park_gate_note(
    flow: Any, step: Any, decision: DialogDecision, dialog_summary: str = ""
) -> None:
    """Park the dialog's conclusion on the GATE, for exactly one consumption.

    A pause-point ``continue`` changes no step state (decision 4), but an
    instruction confirmed there still has precisely one legitimate consumer:
    the next execution of this step launched directly out of this same pause —
    the failure gate's Retry. The note therefore lives in flow context keyed
    to the step, never in the step's inputs: inputs would be read by ANY later
    execution of the reused step object (a fix-loop re-entry above all), which
    is a different run entitled to none of it.

    INVARIANT: parking only ever ADDS. A pause can be dialogued at repeatedly —
    the operator confirms an instruction, lands back on the same Retry/Skip menu
    and reopens the dialog — and the empty-input path ("change nothing, continue
    immediately") confirms a decision that contributes no parts at all. Deleting
    or replacing the parked note there would silently drop an instruction the
    operator already confirmed, leaving the Retry it was parked for to run
    without it. Discarding is :func:`discard_gate_note`'s job alone, driven by
    the pause resolving some other way.
    """
    notice_parts: List[str] = []
    if decision.instruction:
        notice_parts.append(decision.instruction)
    if decision.revised_description:
        notice_parts.append(
            "The task description has been replaced. Work to this from now on:\n"
            + decision.revised_description
        )
    if dialog_summary:
        notice_parts.append(
            "Conclusion of the interruption dialog:\n" + dialog_summary
        )
    step_id = getattr(step, "step_id", "") or ""
    if not notice_parts or not step_id:
        return

    parked = flow.state.context.get(PENDING_GATE_NOTE_KEY)
    note = ""
    if isinstance(parked, dict) and parked.get("step_id") == step_id:
        # Same pause, later round: accumulate. A note parked for a DIFFERENT
        # step belongs to a pause already left behind, so it is replaced.
        note = str(parked.get("note") or "")
    for part in notice_parts:
        # Re-confirming an unchanged conclusion (the dialog LLM restates the
        # instruction it just agreed on) must not stack duplicates in the
        # prompt the retry finally receives.
        if part and part not in note:
            note = note + "\n\n" + part if note else part
    flow.state.context[PENDING_GATE_NOTE_KEY] = {
        "step_id": step_id,
        "note": note,
    }


def consume_gate_note(flow: Any, step: Any) -> None:
    """Move a parked gate note onto *step*'s inputs for its next execution.

    Called by the failure gate's Retry — the one consumer the note was parked
    for. A note parked for a DIFFERENT step is dropped, not delivered: the
    pause it belonged to has been left behind.
    """
    parked = flow.state.context.pop(PENDING_GATE_NOTE_KEY, None)
    if not isinstance(parked, dict):
        return
    if parked.get("step_id") != getattr(step, "step_id", ""):
        return
    note = str(parked.get("note") or "").strip()
    if not note:
        return
    inputs = step.inputs if step.inputs is not None else {}
    inputs["dialog_note"] = note
    step.inputs = inputs


def discard_gate_note(flow: Any) -> None:
    """Drop a parked gate note when its pause resolves any other way.

    Approve / request-changes / skip / abort / a fresh transition past the
    step all end the pause without the one execution the note was scoped to,
    so it must never reach a later run.
    """
    flow.state.context.pop(PENDING_GATE_NOTE_KEY, None)


def _recompose_step_description(flow: Any, step: Any) -> None:
    """Refresh *step*'s ``task_description`` from the effective description chain."""
    from .state_machine import _compose_effective_task_description

    inputs = step.inputs if step.inputs is not None else {}
    inputs["task_description"] = _compose_effective_task_description(flow)
    step.inputs = inputs


def _rearm_step_as_retry(
    flow: Any, step: Any, decision: DialogDecision
) -> None:
    """Re-arm *step* to run again as a retry (decision 4's ``continue``)."""
    from .models import StepStatus
    from .state_machine import _compose_effective_task_description

    inputs = step.inputs if step.inputs is not None else {}
    # Marked as a retry exactly like the Retry path, which is what makes
    # LLMCaller consider a native resume of the interrupted session.
    inputs["resumed"] = True
    try:
        inputs["retry_count"] = int(inputs.get("retry_count", 0) or 0) + 1
    except (TypeError, ValueError):
        inputs["retry_count"] = 1
    if decision.revised_description:
        inputs["task_description"] = _compose_effective_task_description(flow)
    step.inputs = inputs
    step.status = StepStatus.PENDING



def _restart_note(
    decision: DialogDecision,
    dialog_summary: str,
    *,
    discarded_groups: Optional[List[str]] = None,
    preserved_refs: Optional[List[str]] = None,
) -> str:
    """Build the note a rewound-to step's prompt carries.

    ``discarded_groups`` names the DAG leaf branches the rewind actually
    deleted. WHY it changes the wording: ``workspace: keep`` keeps the flow's
    OWN tree, but a parallel implement step's work never lived there — it was
    on those leaf branches, in their own worktrees, and the rewind removed
    them. Telling the rebuilt step "the workspace still contains the changes
    from the previous attempt" would then send it looking for work that is no
    longer on disk.
    """
    parts: List[str] = []
    if decision.workspace == WORKSPACE_KEEP:
        parts.append(
            "The workspace still contains the changes from the previous "
            "attempt. Check what is already there before you start editing; do "
            "not blindly redo or duplicate it."
        )
    else:
        parts.append(
            "The workspace was reset to the state it had before this flow "
            "started. Nothing from the previous attempt remains."
        )
    if discarded_groups:
        note = (
            "The previous attempt's parallel implementation groups were "
            "discarded with the restart: their worktrees and leaf branches ("
            + ", ".join(discarded_groups)
            + ") no longer exist, so none of their work is in the workspace."
        )
        if preserved_refs:
            note += (
                " It is recoverable at " + ", ".join(preserved_refs)
                + " if you need to consult it."
            )
        parts.append(note)
    if decision.instruction:
        parts.append(decision.instruction)
    if dialog_summary:
        parts.append("Conclusion of the interruption dialog:\n" + dialog_summary)
    return "\n\n".join(parts)


def summarize_transcript(turns: List[Dict[str, Any]], max_chars: int = 2000) -> str:
    """Render the dialog transcript for injection into the next prompt."""
    lines: List[str] = []
    for turn in turns:
        speaker = "User" if turn.get("role") == "user" else "Agent"
        content = str(turn.get("content") or "").strip()
        if content:
            lines.append(f"{speaker}: {content}")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = "... (earlier turns omitted)\n" + text[-max_chars:]
    return text
