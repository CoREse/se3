"""Generic "rewind the flow to an earlier step" facility.

The state machine used to have no way to go *backwards* other than the two
purpose-built loops (the fix loop and the CONFIRM revision loop), each of which
knows exactly which counters to touch. The interjection dialog's ``restart``
decision needs something the state machine did not have: send the flow back to
an arbitrary step of ``step_history`` and re-enter it as if it were being
reached for the first time.

The hard part is not deleting steps — it is *state*. A step's execution leaves
derived facts scattered across ``flow.state`` (fix-loop counters, review-round
bookkeeping, the review scope, the self-check round controller's state, the
per-step context entries every handler writes). Re-entering a step with a later
step's derived facts still in place produces a flow that is neither the old one
nor a fresh one.

The mechanism is therefore an *entry snapshot*: the first time a step is
entered, the rewindable part of ``flow.state`` is captured under that step's id;
rewinding to that step restores the capture. The split between "rewindable" and
"never rewound" is expressed as an allowlist of flow-level FACTS
(:data:`FLOW_LEVEL_CONTEXT_KEYS`) rather than a list of derived keys — a new
handler that writes a new context key is derived by default, so the invariant
holds without anyone remembering to register it. Getting that inversion the
other way round is what would silently rot.
"""

from __future__ import annotations

import copy
import logging
import threading
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .models import FlowInstance

logger = logging.getLogger(__name__)

#: ``flow.state.context`` key holding this flow's rewind generation.
GENERATION_CONTEXT_KEY = "rewind_generation"

#: ``flow.state.context`` key holding the per-step generation assignments
#: (``step_id -> int``). The flow-global generation is only the counter new
#: steps are born into; each step's OWN generation is what its history records
#: are stamped and filtered by.
STEP_GENERATIONS_CONTEXT_KEY = "rewind_step_generations"

#: ``flow.state.context`` key holding the per-step entry snapshots.
ENTRY_SNAPSHOT_CONTEXT_KEY = "_step_entry_snapshots"

#: Context keys that are flow-level FACTS, not step-derived data. They survive
#: every rewind because rewinding them would either lose information the user
#: supplied (the description revision chain, interjections), re-open a decision
#: the flow already made once and must never re-decide (task type, plan
#: doctrine), or corrupt an accounting ledger that spans the whole flow
#: (session usage). Everything NOT listed here is treated as derived and is
#: restored to its value at the target step's first entry.
FLOW_LEVEL_CONTEXT_KEYS = frozenset(
    {
        # User-supplied requirement facts (decision 6's revision chain and the
        # legacy interjection list) — never rewound.
        "user_interjections",
        "description_revisions",
        # The covering-layer counter orders revisions against ADJUDICATE
        # rulings. Rewinding it would re-issue ordinals already stamped on
        # surviving layers, so a later revision could tie or lose against an
        # older ruling — the exact ordering ambiguity the counter replaced.
        "description_layer_seq",
        # Frozen-once anchors and routing decisions.
        "invariant_anchors",
        "explicit_type",
        "analyzed_type",
        "resolved_type",
        "task_type",
        "plan_decomposition",
        "plan_granularity",
        "plan_mode_reason",
        # Environment / addressing, not step output.
        "project_root",
        "merge_checkout_root",
        # Once-per-flow guards over EXTERNAL side effects already performed:
        # the inherited-failure issues are files written under tianluo/issues/,
        # and a rewind does not un-write them. Rewinding the guard would have
        # the rebuilt TEST file the same A-class issues a second time — the
        # duplicate-issue explosion the guard exists to prevent. The e2e
        # suggestion is the same shape: shown once per flow, and a rewind does
        # not un-show it.
        "inherited_failures_filed",
        "e2e_suggestion_shown",
        # Rewind's own bookkeeping must not be rewound by a rewind.
        GENERATION_CONTEXT_KEY,
        STEP_GENERATIONS_CONTEXT_KEY,
        ENTRY_SNAPSHOT_CONTEXT_KEY,
    }
)

#: ``State`` scalar/collection attributes captured by an entry snapshot. These
#: are the state-machine-owned derived counters; ``baseline_failures``,
#: ``session_usage_records`` and ``session_token_usage`` are deliberately
#: absent — they are flow-level facts.
#:
#: INVARIANT: ``selected_steps`` belongs here. It looks like a frozen routing
#: plan decided at flow creation, but it is not: the state machine SPLICES
#: slots into it at runtime (an ADJUDICATE slot when self-check triggers
#: adjudication, a CONFIRM gate after a confirmable step). A rewind that
#: deletes those step objects while leaving their slots behind would have the
#: next transition rebuild an un-triggered ADJUDICATE / a spurious CONFIRM, and
#: would shift every later step's index relative to the entry snapshot's — so
#: rewinding to the step that triggered the insertion would run it twice.
_SNAPSHOT_STATE_ATTRS = (
    "fix_iterations",
    "fix_history",
    "review_iterations",
    "selected_steps",
)


# ---------------------------------------------------------------------------
# Ambient generation
# ---------------------------------------------------------------------------
#
# WHY an ambient module-level value rather than a constructor argument: the
# generation is a property of the flow, and LLMCaller is constructed at dozens
# of call sites (plus once per DAG group, on worker threads that share the
# flow). A plain guarded module value is visible to every one of them without
# threading a parameter through every construction site, and threads SHOULD
# share it — all groups of one step belong to the same generation.
#
# WHY per-STEP generations and not one flow-global number: a rewind supersedes
# the target and everything after it, but steps BEFORE the target keep running
# under their original step_id (the fix loop re-enters IMPLEMENT after a rewind
# to TEST, a CONFIRM revision re-enters the reviewed step). Stamping those
# later calls with a bumped global generation would sever them from their own
# earlier records — a retry-context rebuild filtered on the new generation
# would drop exactly the conversation the step legitimately continues. So the
# flow-global value is only the counter new steps are born into; each step's
# OWN generation is assigned at its first entry and follows it for the rest of
# its life, and a rewind re-assigns only the steps it removes.

_generation_lock = threading.Lock()
#: flow_id -> {"current": int, "steps": {step_id: int}}. ``None``-keyed entry
#: is the anonymous scope for callers that carry no flow id.
_flow_generations: Dict[Optional[str], Dict[str, Any]] = {}


def current_generation(
    flow_id: Optional[str] = None, step_id: Optional[str] = None
) -> int:
    """Return the ambient rewind generation for the running flow.

    With a ``step_id`` this resolves to that step's OWN generation (a DAG
    group id like ``05_implement_x_G2`` resolves to its base step's), falling
    back to the flow's current counter for a step that has not been entered
    yet. Without any ids it is the last published flow generation — the
    historical single-value behaviour, kept for ad-hoc callers.
    """
    with _generation_lock:
        entry = _flow_generations.get(flow_id)
        if entry is None and flow_id is not None:
            entry = _flow_generations.get(None)
        if entry is None:
            return 0
        if step_id:
            resolved = _resolve_step_generation(entry.get("steps") or {}, step_id)
            if resolved is not None:
                return resolved
        return int(entry.get("current") or 0)


def set_current_generation(value: int) -> None:
    """Publish the running flow's current generation counter.

    Retained for the anonymous (flow-id-less) scope; flow-aware callers should
    use :func:`bind_flow_generation`, which publishes the per-step map too.
    """
    with _generation_lock:
        entry = _flow_generations.setdefault(None, {"current": 0, "steps": {}})
        entry["current"] = int(value or 0)


def _resolve_step_generation(
    steps: Dict[str, Any], step_id: str
) -> Optional[int]:
    """Resolve a step's generation, unwrapping a DAG group suffix.

    Group history lives under ``<step_id>_<group_id>`` and shares the base
    step's generation: the rewind that supersedes the step supersedes all of
    its groups with it. The longest matching prefix wins so nested ids cannot
    bind to the wrong base.
    """
    if step_id in steps:
        try:
            return int(steps[step_id])
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return None
    best: Optional[str] = None
    for key in steps:
        if step_id.startswith(str(key) + "_"):
            if best is None or len(str(key)) > len(best):
                best = str(key)
    if best is None:
        return None
    try:
        return int(steps[best])
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None


#: Generation every flow starts in.
#:
#: WHY it is 1 and not 0: ``0`` is reserved as the *legacy wildcard* — a record
#: written before the field existed deserializes to 0 and must stay visible to
#: every rebuild, since nothing can say which generation it belonged to. If a
#: live flow also started at 0 its records would inherit that wildcard and a
#: rewind could never exclude them, which is exactly the isolation the field
#: exists to provide. Starting at 1 keeps "unknown" and "the first generation"
#: distinguishable.
FIRST_GENERATION = 1


def flow_generation(flow: "FlowInstance") -> int:
    """Read a flow's persisted rewind generation counter.

    Returns :data:`FIRST_GENERATION` for a flow that has none recorded yet
    (a new flow, or one created before rewind existed).
    """
    try:
        value = int(flow.state.context.get(GENERATION_CONTEXT_KEY, 0) or 0)
    except (AttributeError, TypeError, ValueError):
        return FIRST_GENERATION
    return value or FIRST_GENERATION


def step_generation(flow: "FlowInstance", step_id: str) -> int:
    """The generation *step_id*'s history records belong to.

    A step keeps the generation it was first entered under for its whole life;
    a rewind re-assigns only the steps it removes. DAG group step ids resolve
    to their base step's generation. A step with no recorded assignment yet
    reads as the flow's current counter (it will be born into it), and a flow
    predating per-step assignments falls back the same way — its records are
    stamped with the flow generation, which is what the fallback reproduces.
    """
    try:
        steps = flow.state.context.get(STEP_GENERATIONS_CONTEXT_KEY)
    except AttributeError:  # pragma: no cover - unit-test stubs
        steps = None
    if isinstance(steps, dict) and step_id:
        resolved = _resolve_step_generation(steps, step_id)
        if resolved is not None:
            return resolved
    return flow_generation(flow)


def _step_generation_map(flow: "FlowInstance") -> Dict[str, int]:
    """Return (creating if needed) the flow's mutable step-generation map."""
    generations = flow.state.context.get(STEP_GENERATIONS_CONTEXT_KEY)
    if not isinstance(generations, dict):
        generations = {}
        flow.state.context[STEP_GENERATIONS_CONTEXT_KEY] = generations
    return generations


def bind_flow_generation(flow: "FlowInstance") -> int:
    """Persist (if absent) and publish *flow*'s generation state as ambient."""
    generation = flow_generation(flow)
    try:
        flow.state.context.setdefault(GENERATION_CONTEXT_KEY, generation)
        steps = dict(_step_generation_map(flow))
    except AttributeError:  # pragma: no cover - unit-test stubs
        steps = {}
    with _generation_lock:
        published = {
            "current": generation,
            "steps": steps,
        }
        _flow_generations[getattr(flow, "flow_id", None)] = published
        # The anonymous scope tracks the most recently bound flow: callers that
        # carry no flow id (and the historical ``current_generation()`` read)
        # resolve against it.
        _flow_generations[None] = published
    return generation


# ---------------------------------------------------------------------------
# Entry snapshots
# ---------------------------------------------------------------------------


def _rewindable_context(context: Dict[str, Any]) -> Dict[str, Any]:
    """Deep-copy the derived (rewindable) half of ``flow.state.context``."""
    return {
        key: copy.deepcopy(value)
        for key, value in context.items()
        if key not in FLOW_LEVEL_CONTEXT_KEYS
    }


def _encode_state_value(attr: str, value: Any) -> Any:
    """Render a snapshotted ``flow.state`` attribute JSON-round-trippable.

    INVARIANT: the snapshot lives inside ``flow.state.context``, which the
    persistence layer serialises with ``json.dumps(..., default=str)`` — and
    ``default=str`` turns a ``StepType`` into ``"StepType.IMPLEMENT"``, a string
    that ``StepType(...)`` cannot parse back. Only the TOP-LEVEL
    ``selected_steps`` is re-coerced on load, so a nested copy left as enums
    would come back from any save+reload (every json/daemon resume) as junk
    strings and be restored verbatim into ``flow.state.selected_steps``,
    bricking the next transition. Enum members are therefore stored by value.
    """
    if attr == "selected_steps" and isinstance(value, list):
        return [getattr(item, "value", item) for item in value]
    return value


def _decode_state_value(attr: str, value: Any) -> Any:
    """Inverse of :func:`_encode_state_value`, tolerant of legacy snapshots.

    Snapshots written before the encoding existed hold ``"StepType.IMPLEMENT"``
    (``default=str``'s rendering) or live enum members; both are accepted so an
    in-flight flow stays rewindable across the upgrade.
    """
    if attr != "selected_steps" or not isinstance(value, list):
        return value
    from .models import StepType

    decoded: List[Any] = []
    for item in value:
        if isinstance(item, StepType):
            decoded.append(item)
            continue
        text = str(item)
        if text.startswith("StepType."):
            member = getattr(StepType, text.split(".", 1)[1], None)
            if member is not None:
                decoded.append(member)
                continue
        try:
            decoded.append(StepType(text))
        except ValueError:
            # An unknown step type cannot be restored as anything meaningful;
            # dropping it would silently shorten the routing sequence, so it is
            # kept as-is and surfaces at the transition rather than here.
            decoded.append(item)
    return decoded


def snapshot_step_entry(
    flow: "FlowInstance", step_id: str, *, amend: bool = False
) -> None:
    """Capture the rewindable state the FIRST time *step_id* is entered.

    Idempotent by design: a step re-entered by the fix loop, by ``--resume`` or
    by a retry keeps its ORIGINAL entry snapshot, because "the state as this
    step first saw it" is what a rewind must restore — not the state left by a
    later failed attempt at the same step.

    ``amend`` re-captures over an existing snapshot. It exists for exactly one
    caller: the snapshot is taken the moment a step is entered — so a restart is
    possible even while a long pre-step baseline run is still going — and then
    amended once that pre-step work has frozen its results, so the baselines a
    ``workspace: keep`` restart must get back are inside the capture. It is
    valid only during the step's own first entry; using it on a re-entry would
    overwrite the first-entry state with a later attempt's.

    Never raises: a flow that cannot snapshot must still run; it merely loses
    the ability to be rewound to this step later, which
    :func:`ensure_rewindable` reports as a refusal rather than performing a
    partial restore.
    """
    if not step_id:
        return
    try:
        snapshots = flow.state.context.setdefault(ENTRY_SNAPSHOT_CONTEXT_KEY, {})
        if not isinstance(snapshots, dict):
            snapshots = {}
            flow.state.context[ENTRY_SNAPSHOT_CONTEXT_KEY] = snapshots
        # The step is born into the flow's CURRENT generation at its first
        # entry and keeps it for life — a rewind re-assigns only the steps it
        # removes, so a pre-target step re-entered afterwards still stamps (and
        # rebuilds from) its own generation's records.
        generations = _step_generation_map(flow)
        if step_id not in generations:
            generations[step_id] = flow_generation(flow)
            bind_flow_generation(flow)
        if step_id in snapshots and not amend:
            return
        snapshots[step_id] = {
            "context": _rewindable_context(flow.state.context),
            "state": {
                attr: _encode_state_value(
                    attr, copy.deepcopy(getattr(flow.state, attr, None))
                )
                for attr in _SNAPSHOT_STATE_ATTRS
            },
            "current_step_index": getattr(flow.state, "current_step_index", 0),
        }
    except Exception:  # pragma: no cover - defensive
        logger.debug("Failed to snapshot step entry for %s", step_id, exc_info=True)


def _snapshot_step_index(flow: "FlowInstance", step_id: str) -> int:
    """The ``selected_steps`` index *step_id* held when it was first entered."""
    snapshots = flow.state.context.get(ENTRY_SNAPSHOT_CONTEXT_KEY)
    snapshot = snapshots.get(step_id) if isinstance(snapshots, dict) else None
    if isinstance(snapshot, dict):
        try:
            return max(0, int(snapshot.get("current_step_index", 0) or 0))
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return 0
    return 0


def _restore_entry_snapshot(flow: "FlowInstance", step_id: str) -> bool:
    """Restore the derived state captured at *step_id*'s first entry."""
    snapshots = flow.state.context.get(ENTRY_SNAPSHOT_CONTEXT_KEY)
    if not isinstance(snapshots, dict):
        return False
    snapshot = snapshots.get(step_id)
    if not isinstance(snapshot, dict):
        return False

    preserved = {
        key: value
        for key, value in flow.state.context.items()
        if key in FLOW_LEVEL_CONTEXT_KEYS
    }
    restored_context = copy.deepcopy(snapshot.get("context") or {})
    restored_context.update(preserved)
    flow.state.context.clear()
    flow.state.context.update(restored_context)

    for attr, value in (snapshot.get("state") or {}).items():
        if attr in _SNAPSHOT_STATE_ATTRS:
            setattr(
                flow.state, attr, _decode_state_value(attr, copy.deepcopy(value))
            )
    return True


# ---------------------------------------------------------------------------
# Rewind
# ---------------------------------------------------------------------------


class RewindError(Exception):
    """Raised when a rewind target cannot be resolved.

    ``cleaned_branches`` carries the group branches the refused rewind had
    ALREADY removed AND verified clean when it gave up. A refusal is normally
    "nothing happened", but the cleanup loop deletes branch by branch and only
    reports its leftovers at the end — so a refusal raised from it has a real
    deletion behind it, and the caller has to invalidate the step state those
    deletions made dangle rather than hand it back as if it still pointed at
    anything.

    INVARIANT: ``deleted_branches`` — not ``cleaned_branches`` — is what the
    caller invalidates against. The two answer different questions: "is this
    branch fully verified gone, worktree directory included" decides whether
    the rewind may proceed, while "did the ref go away" decides whether a
    recorded group result still reaches the tree. A branch whose ref really was
    deleted but whose verification then failed (an unanswerable probe, a
    directory ``rmtree`` could not finish) is unclean AND dangling: it must
    refuse the rewind and still have its group state dropped, or the flow keeps
    a group recorded as done whose only copy is a safety ref.
    """

    def __init__(
        self,
        message: str,
        *,
        cleaned_branches: Optional[List[str]] = None,
        deleted_branches: Optional[List[str]] = None,
    ):
        super().__init__(message)
        self.cleaned_branches = list(cleaned_branches or [])
        #: Branches whose ref this refusal removed, whether or not the cleanup
        #: around it verified clean. Defaults to ``cleaned_branches`` for
        #: callers that only know the verified set.
        self.deleted_branches = (
            list(deleted_branches)
            if deleted_branches is not None
            else list(self.cleaned_branches)
        )
        #: Steps whose recorded group results the refusal had to drop because
        #: ``deleted_branches`` were their only copy. Reported to the operator:
        #: those groups WILL run again.
        self.invalidated_group_steps: List[str] = []


class RewindResult:
    """What a rewind actually did, for reporting back to the user."""

    def __init__(
        self,
        target_step_id: str,
        target_step_type: str,
        removed_step_ids: List[str],
        generation: int,
        state_restored: bool,
        cleaned_worktrees: List[str],
        preserved_refs: Optional[List[str]] = None,
    ) -> None:
        self.target_step_id = target_step_id
        self.target_step_type = target_step_type
        self.removed_step_ids = removed_step_ids
        self.generation = generation
        self.state_restored = state_restored
        self.cleaned_worktrees = cleaned_worktrees
        #: Safe refs holding the group work deleted with the worktrees, so the
        #: operator is never told "discarded" without being told "recoverable".
        self.preserved_refs = list(preserved_refs or [])

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target_step_id": self.target_step_id,
            "target_step_type": self.target_step_type,
            "removed_step_ids": list(self.removed_step_ids),
            "generation": self.generation,
            "state_restored": self.state_restored,
            "cleaned_worktrees": list(self.cleaned_worktrees),
            "preserved_refs": list(self.preserved_refs),
        }


def resolve_rewind_target(flow: "FlowInstance", step_id: Optional[str]) -> str:
    """Validate *step_id* as a rewind target, defaulting to the current step."""
    from ..i18n import t

    history = list(getattr(flow.state, "step_history", []) or [])
    current = flow.state.current_step_id
    if not step_id:
        if not current:
            raise RewindError(t("engine.rewind.no_current_step"))
        return str(current)
    if step_id not in history:
        raise RewindError(
            t("engine.rewind.not_in_history", step_id=step_id)
        )
    # INVARIANT: only the current step or an EARLIER one is a valid target.
    # A rewind deletes from the target forward, so a target after the current
    # step removes only itself and leaves the steps between it and the current
    # position untouched — while restoring the target's entry snapshot, which
    # unwinds the very counters (fix_iterations above all) those surviving
    # steps were re-armed under. The fix loop's re-armed IMPLEMENT would then
    # sit PENDING and never run, and the rebuilt TEST would run against the
    # un-fixed tree.
    if current and current in history:
        if history.index(str(step_id)) > history.index(str(current)):
            raise RewindError(
                t("engine.rewind.not_earlier", step_id=step_id)
            )
    return str(step_id)


def has_entry_snapshot(flow: "FlowInstance", step_id: str) -> bool:
    """True when *step_id* recorded the entry state a rewind would restore."""
    try:
        snapshots = flow.state.context.get(ENTRY_SNAPSHOT_CONTEXT_KEY)
    except AttributeError:  # pragma: no cover - unit-test stubs
        return False
    return isinstance(snapshots, dict) and isinstance(snapshots.get(step_id), dict)


def ensure_rewindable(flow: "FlowInstance", step_id: str) -> None:
    """Refuse a rewind that could not restore the target's entry invariants.

    INVARIANT: a restart either puts the flow back exactly as the target step
    first saw it, or it does not happen at all. Rewinding routing while leaving
    a later step's fix counters, review scope and self-check rounds in place
    would produce a flow that is neither the old one nor a fresh one — a
    mixed-generation state whose quality gates accept against the wrong round.
    A flow that predates entry snapshots (or whose snapshot capture failed)
    therefore cannot be rewound, and is told so rather than silently corrupted.
    """
    from ..i18n import t

    if not has_entry_snapshot(flow, step_id):
        raise RewindError(t("engine.rewind.no_entry_snapshot", step_id=step_id))


class RewindPlan:
    """A validated, already-captured rewind, ready to be committed.

    INVARIANT: everything a rewind can REFUSE over happens while building this
    object — target resolution, the entry-snapshot requirement, and the capture
    plus removal of the discarded DAG groups. Committing the plan
    (:func:`rewind_to_step`) is then pure state mutation that cannot fail.

    WHY the split exists: ``restart`` with ``workspace: reset`` discards the
    working tree to a safety ref before the flow state moves. A refusal
    discovered after that point would leave a tree reset to the baseline while
    every step still claims to be done — the one outcome the restart must never
    produce. The caller therefore plans first, resets second, commits third, so
    a refused restart leaves the tree exactly as a refused target does.

    The price of planning early is that a plan which is then ABANDONED (the
    reset failed) has already deleted the discarded groups' worktrees and
    branches. Abandoning it therefore obliges the caller to call
    :func:`invalidate_discarded_group_state`: the flow keeps running from here,
    and a step still claiming a group whose branch is gone would have the
    continuation skip it and the leaf merge find nothing to merge.
    """

    def __init__(
        self,
        target_step_id: str,
        removed_step_ids: List[str],
        cleaned_worktrees: List[str],
        preserved_refs: List[str],
        discarded_group_steps: Optional[Dict[str, List[str]]] = None,
    ) -> None:
        self.target_step_id = target_step_id
        self.removed_step_ids = list(removed_step_ids)
        self.cleaned_worktrees = list(cleaned_worktrees)
        self.preserved_refs = list(preserved_refs)
        #: ``step_id -> the materialised group branches this plan deleted``.
        #: Only meaningful while the plan is UNCOMMITTED: committing it deletes
        #: those steps outright, but a caller that abandons the plan (a failed
        #: workspace reset) still has to invalidate the group state they name.
        self.discarded_group_steps: Dict[str, List[str]] = {
            k: list(v) for k, v in (discarded_group_steps or {}).items()
        }


def prepare_rewind(
    flow: "FlowInstance",
    step_id: Optional[str] = None,
    *,
    cleanup_worktrees: bool = True,
    project_root: Optional[Any] = None,
) -> RewindPlan:
    """Decide and perform everything a rewind can refuse over.

    Raises :class:`RewindError` — with the flow state, its worktrees and its
    branches all as they were — when the target cannot be resolved, has no
    entry snapshot, or a discarded DAG group's work cannot be captured to a
    safety ref. The one exception is a cleanup that fails PART WAY: some
    branches are already gone by then, so that refusal first invalidates the
    step state those deletions made dangle (see
    :func:`invalidate_discarded_group_state`) and only then raises.
    """
    from ..i18n import t

    target_id = resolve_rewind_target(flow, step_id)
    ensure_rewindable(flow, target_id)
    history = list(flow.state.step_history or [])
    try:
        cut = history.index(target_id)
    except ValueError as exc:  # pragma: no cover - resolve_rewind_target guards
        raise RewindError(t("engine.rewind.not_in_history", step_id=target_id)) from exc

    removed_ids = history[cut:]
    cleaned_worktrees: List[str] = []
    preserved_refs: List[str] = []
    discarded_group_steps: Dict[str, List[str]] = {}
    if cleanup_worktrees:
        branches = rewind_group_branches(flow, target_id, removed_ids=removed_ids)
        if branches:
            # INVARIANT: preserve BEFORE deleting, and delete NOTHING if the
            # preservation fails. A leaf branch's commits go unreachable the
            # moment the branch is deleted and the worktree's uncommitted edits
            # die with the rmtree — both are this flow's produced work, and the
            # restart promises nothing it discards is unrecoverable. So a
            # capture failure raises out of here (before any state mutation and
            # before the caller touches the working tree) rather than letting
            # the cleanup run over an unpreserved group.
            preserved_refs = _preserve_group_work(flow, branches, project_root)
            # Which names actually held something, asked while they still do.
            # Deleting a name that never existed discards nothing, so it must
            # not be counted as work the step can no longer skip over.
            live = _materialised_branches(flow, branches, project_root)
            # Likewise fail-closed: a branch or worktree that survives the
            # cleanup would make the rebuilt implement step collide with the
            # discarded attempt, so it aborts here — still before any FLOW
            # ROUTING is touched — instead of being logged and rewound over.
            try:
                cleaned_worktrees = _cleanup_branches(flow, branches, project_root)
            except RewindError as exc:
                # The loop deletes branch by branch and reports its leftovers
                # only at the end, so this refusal has real deletions behind
                # it. The steps naming them would otherwise keep claiming those
                # groups are done while their only copy is a safety ref nobody
                # re-reads — and the very next `continue` would skip them.
                exc.invalidated_group_steps = _invalidate_group_state(
                    flow,
                    _group_state_owners(
                        flow,
                        removed_ids,
                        # Asked of the refs that went away, NOT of the branches
                        # the cleanup managed to verify clean: a ref deleted
                        # under a failed verification dangles just the same.
                        [b for b in exc.deleted_branches if b in live],
                    ),
                )
                raise
            discarded_group_steps = _group_state_owners(
                flow, removed_ids, [b for b in cleaned_worktrees if b in live]
            )

    return RewindPlan(
        target_id,
        removed_ids,
        cleaned_worktrees,
        preserved_refs,
        discarded_group_steps,
    )


def invalidate_discarded_group_state(
    flow: "FlowInstance", plan: RewindPlan
) -> List[str]:
    """Drop the group results *plan* already deleted the only copy of.

    Called when a planned rewind is ABANDONED after :func:`prepare_rewind` ran
    — today, when the workspace reset that follows it fails and the restart is
    handed back as refused. The flow then keeps running on its existing state,
    but that state is no longer true: the plan has already captured the
    discarded implement step's group worktrees and leaf branches to safety refs
    and removed them, so ``implemented_groups`` names groups whose commits no
    longer reach the tree and ``dag_preserved_worktrees`` names directories
    that are gone.

    INVARIANT: a refused restart never leaves a group both "already done" and
    unreachable. Forgetting a completed group's results costs one re-run;
    remembering them makes the continuation skip the group and the end-of-DAG
    leaf merge silently find nothing to merge, so the flow reports work it does
    not have. The results are therefore dropped, and the group is re-run.

    Returns the step ids whose group results were dropped, for reporting.
    """
    return _invalidate_group_state(flow, plan.discarded_group_steps)


#: ``flow.state.context`` key holding the inputs a rewind carries from the
#: deleted target step into the fresh one built in its place. Popped by
#: ``StateMachine.rebuild_rewound_step``; it must survive the json/daemon
#: process boundary, which is why it travels in flow state rather than a
#: module value.
PENDING_REWIND_INPUTS_KEY = "pending_rewind_step_inputs"

#: The only step inputs a rewind carries over. INVARIANT: a rewound-to step is
#: a FRESH call — it inherits no retry_count, no resumed flag, no prior
#: outputs. The workspace baseline is not part of the attempt at all: it is a
#: photograph of the tree taken BEFORE the step first ran, and INVESTIGATE's
#: net-zero-diff guard is only meaningful against it. Rebuilding without it
#: would re-photograph a tree that still holds the abandoned attempt's
#: unreverted probe edits, so the guard would compare those leftovers against
#: themselves, pass, and let the probe patches ride on into PLAN/IMPLEMENT.
#: (``steps.investigate.BASELINE_INPUT_KEY``, spelled out rather than imported
#: so this module keeps its no-step-imports posture.)
_CARRIED_STEP_INPUT_KEYS = ("workspace_baseline",)


def _carried_step_inputs(step: Any) -> Dict[str, Any]:
    """Harvest the inputs :data:`_CARRIED_STEP_INPUT_KEYS` names from *step*."""
    inputs = getattr(step, "inputs", None) or {}
    if not isinstance(inputs, dict):
        return {}
    return {
        key: copy.deepcopy(inputs[key])
        for key in _CARRIED_STEP_INPUT_KEYS
        if inputs.get(key) is not None
    }


#: ``flow.state.context["review_scope"]`` entry a ``keep`` rewind carries over.
#:
#: INVARIANT: with ``workspace: keep`` the abandoned attempt's edits stay on
#: disk, so they remain this flow's work and must stay inside every later
#: review's diff scope. The implementation baseline is a photograph of the tree
#: taken BEFORE those edits, and it is amended into the entry snapshot only at
#: IMPLEMENT's own first entry — so a rewind to a target EARLIER than IMPLEMENT
#: restores a context that has none, and the rebuilt IMPLEMENT photographs a
#: tree that already contains them. SELF_CHECK would then diff against that
#: photograph and never see the abandoned attempt's files at all: they ship
#: unreviewed. Same rule, one level up, as :data:`_CARRIED_STEP_INPUT_KEYS`.
_REVIEW_SCOPE_CONTEXT_KEY = "review_scope"
_CARRIED_REVIEW_BASELINE_KEY = "implementation_baseline"


def _carried_review_baseline(flow: "FlowInstance") -> Optional[Any]:
    """Read the implementation review baseline standing before the rewind."""
    scope = flow.state.context.get(_REVIEW_SCOPE_CONTEXT_KEY)
    if not isinstance(scope, dict):
        return None
    baseline = scope.get(_CARRIED_REVIEW_BASELINE_KEY)
    return copy.deepcopy(baseline) if baseline is not None else None


def _restore_carried_review_baseline(
    flow: "FlowInstance", baseline: Optional[Any]
) -> None:
    """Put *baseline* back if the restored snapshot has none of its own.

    The target's OWN snapshot wins when it has one: rewinding to a step at or
    after IMPLEMENT restores the baseline that step actually ran against.
    """
    if baseline is None:
        return
    scope = flow.state.context.get(_REVIEW_SCOPE_CONTEXT_KEY)
    if not isinstance(scope, dict):
        scope = {}
        flow.state.context[_REVIEW_SCOPE_CONTEXT_KEY] = scope
    scope.setdefault(_CARRIED_REVIEW_BASELINE_KEY, baseline)


def rewind_to_step(
    flow: "FlowInstance",
    step_id: Optional[str] = None,
    *,
    cleanup_worktrees: bool = True,
    project_root: Optional[Any] = None,
    plan: Optional[RewindPlan] = None,
    carry_step_inputs: bool = True,
) -> RewindResult:
    """Send *flow* back to *step_id* so it is re-entered as a fresh step.

    Shape (decision 4's ``restart``):

    * every step from the target onwards is deleted from ``state.steps``, its
      cold-partition body removed, and ``step_history`` truncated;
    * ``current_step_index`` / ``current_step_id`` are wound back so the normal
      construction path (``_build_step_inputs`` and friends) rebuilds the target
      step on the next loop turn — it is a brand-new Step object and therefore
      runs as a fresh call, never as a retry;
    * flow-level derived state is restored to the target step's entry snapshot
      (a target with no snapshot is REFUSED outright — see
      :func:`ensure_rewindable`);
    * the rewind generation is bumped, so the abandoned generation's
      conversation records stay on disk for history display but are excluded
      from any rebuilt retry context.

    The DAG group worktrees and leaf branches of a deleted implement step are
    derived products, so they are cleaned up too rather than left to leak — but
    only after their commits and uncommitted edits are captured under a safety
    ref, and only in the planning phase (:func:`prepare_rewind`). Pass a *plan*
    built earlier to move every refusal ahead of a destructive workspace reset;
    without one the plan is built here and this call may still refuse.

    ``carry_step_inputs`` forwards :data:`_CARRIED_STEP_INPUT_KEYS` from the
    deleted target onto the step rebuilt in its place, and carries the
    implementation review baseline (:data:`_CARRIED_REVIEW_BASELINE_KEY`) across
    the restore. The caller turns it off for a ``workspace: reset`` restart: the
    tree is then restored to the flow's own baseline, so the abandoned attempt's
    leftovers those baselines exist to expose are gone and a fresh capture is
    the accurate one.

    Returns a :class:`RewindResult` describing what was removed; the caller
    persists the flow.
    """
    if plan is None:
        plan = prepare_rewind(
            flow,
            step_id,
            cleanup_worktrees=cleanup_worktrees,
            project_root=project_root,
        )

    from ..i18n import t

    target_id = plan.target_step_id
    history = list(flow.state.step_history or [])
    try:
        # The plan validated the target's presence and nothing between planning
        # and committing touches ``step_history``; this only guards a caller
        # that held a plan across an unrelated state change, and it guards it
        # as a typed refusal so it cannot escape as a bare ValueError.
        cut = history.index(target_id)
    except ValueError as exc:  # pragma: no cover - plan guards the target
        raise RewindError(t("engine.rewind.not_in_history", step_id=target_id)) from exc
    removed_ids = history[cut:]
    target_step = flow.state.steps.get(target_id)
    target_type = (
        target_step.step_type.value
        if target_step is not None and hasattr(target_step.step_type, "value")
        else ""
    )

    cleaned_worktrees = list(plan.cleaned_worktrees)
    preserved_refs = list(plan.preserved_refs)

    # Harvested BEFORE the target step object is dropped and its cold body
    # deleted — after that the baseline it holds is unrecoverable, and the
    # rebuilt step would silently re-baseline onto the discarded attempt's
    # leftovers. A step that reached its clean end has already popped the key,
    # so a completed target carries nothing over and the fresh capture (the
    # right one there) still happens.
    carried_inputs = (
        _carried_step_inputs(target_step)
        if carry_step_inputs and target_step is not None
        else {}
    )
    # Read before the snapshot restore rewrites ``state.context``. Same
    # ``keep``-only rule as the carried step inputs: a ``reset`` puts the tree
    # back to the flow's own baseline, so a fresh capture is the accurate one.
    carried_review_baseline = (
        _carried_review_baseline(flow) if carry_step_inputs else None
    )

    # The target's own recorded routing position, read before the restore
    # rewrites ``state.context``. It is the authoritative index: deriving one
    # from ``selected_steps.index(step_type)`` picks the FIRST occurrence of a
    # repeated step type (CONFIRM appears once per confirmed step), which would
    # send the post-approval transition into an earlier segment of the flow.
    target_index = _snapshot_step_index(flow, target_id)

    # Restore derived state BEFORE dropping the steps: the snapshot lives in
    # ``state.context`` and the restore rewrites that whole mapping, so doing
    # it after the deletions would be equally correct but harder to reason
    # about when a snapshot is missing.
    state_restored = _restore_entry_snapshot(flow, target_id)

    snapshots = flow.state.context.get(ENTRY_SNAPSHOT_CONTEXT_KEY)
    for removed_id in removed_ids:
        flow.state.steps.pop(removed_id, None)
        _discard_cold_body(flow, removed_id)
        if isinstance(snapshots, dict):
            snapshots.pop(removed_id, None)

    flow.state.step_history = history[:cut]
    flow.state.current_step_id = flow.state.step_history[-1] if cut else None

    # The step index addresses ``selected_steps``, which the entry snapshot
    # restored alongside it: index and sequence are captured together at the
    # target's first entry, so the target re-enters at exactly the position it
    # held then — including any runtime-spliced ADJUDICATE / CONFIRM slots
    # being unwound with the steps they belonged to.
    flow.state.current_step_index = target_index

    _restore_carried_review_baseline(flow, carried_review_baseline)

    # Set after the snapshot restore, which rewrites the whole context mapping.
    if carried_inputs:
        flow.state.context[PENDING_REWIND_INPUTS_KEY] = carried_inputs
    else:
        flow.state.context.pop(PENDING_REWIND_INPUTS_KEY, None)

    # The flow counter moves so the steps this rewind deleted are re-born into
    # a new generation at re-entry; the survivors keep their own assignments,
    # so a fix loop re-entering a step BEFORE the target still stamps and
    # rebuilds against that step's uninterrupted generation.
    generation = flow_generation(flow) + 1
    flow.state.context[GENERATION_CONTEXT_KEY] = generation
    generations = _step_generation_map(flow)
    for removed_id in removed_ids:
        generations.pop(removed_id, None)
    bind_flow_generation(flow)

    logger.info(
        "Rewound flow %s to step %s (%s): removed %d step(s), generation → %d",
        flow.flow_id, target_id, target_type, len(removed_ids), generation,
    )
    return RewindResult(
        target_step_id=target_id,
        target_step_type=target_type,
        removed_step_ids=removed_ids,
        generation=generation,
        state_restored=state_restored,
        cleaned_worktrees=cleaned_worktrees,
        preserved_refs=preserved_refs,
    )


def _discard_cold_body(flow: "FlowInstance", step_id: str) -> None:
    """Delete a removed step's cold-partition body file, best effort.

    A rewound-away step's body is unreachable state; leaving it behind would
    let a lazy hydrator resurrect a step the flow has decided never happened.
    """
    try:
        from .steps._project_root import resolve_flow_project_root
        from .persistence import PersistenceManager

        root = resolve_flow_project_root(flow)
        manager = PersistenceManager(root)
        body = manager._cold_dir(flow.flow_id) / f"{step_id}.json"
        if body.exists():
            body.unlink()
    except Exception:  # pragma: no cover - best effort
        logger.debug(
            "Failed to discard cold body for step %s", step_id, exc_info=True
        )


def _flow_root(flow: "FlowInstance", project_root: Optional[Any]) -> Optional[Any]:
    """Resolve the git root the group worktrees/branches live in."""
    from pathlib import Path

    if project_root:
        return Path(project_root)
    try:
        from .steps._project_root import resolve_flow_project_root

        return resolve_flow_project_root(flow)
    except Exception:  # pragma: no cover - defensive
        return None


def rewind_group_branches(
    flow: "FlowInstance",
    target_step_id: Optional[str] = None,
    *,
    removed_ids: Optional[List[str]] = None,
) -> List[str]:
    """Every DAG leaf branch a rewind to *target_step_id* would delete.

    Exposed (rather than folded into the cleanup) because the operator has to
    SEE this list before confirming: it is the only place a parallel implement
    step's work lives, and neither the main tree's ``git status`` nor
    ``baseline..HEAD`` shows any of it.
    """
    from .models import StepType

    if removed_ids is None:
        history = list(getattr(flow.state, "step_history", []) or [])
        target = target_step_id or getattr(flow.state, "current_step_id", None)
        try:
            cut = history.index(str(target))
        except ValueError:
            return []
        removed_ids = history[cut:]

    branches: List[str] = []
    seen = set()
    for removed_id in removed_ids:
        step = flow.state.steps.get(removed_id)
        if step is None or step.step_type != StepType.IMPLEMENT:
            continue
        for branch in _group_branches_for_step(flow, step):
            if branch not in seen:
                seen.add(branch)
                branches.append(branch)
    return branches


def _preserve_group_work(
    flow: "FlowInstance", branches: List[str], project_root: Optional[Any]
) -> List[str]:
    """Save each group branch's commits and worktree edits under a safe ref.

    INVARIANT: a preservation failure ABORTS the rewind (``RewindError``) — it
    is never downgraded to a log line. The caller deletes every listed worktree
    and branch immediately after this returns, so returning normally is the
    signal that each group is either captured under a ref or holds nothing
    recoverable. Swallowing a failed ``commit-tree`` / ``update-ref`` here would
    hand an uncaptured group straight to that deletion and lose an interrupted
    parallel implement's uncommitted edits with no ref pointing at them.

    Raising happens before any flow-state mutation, so the refused restart
    leaves the flow — and every group worktree — exactly as it was.
    """
    from ..i18n import t

    root = _flow_root(flow, project_root)
    if root is None:
        # No git root resolved: _cleanup_branches short-circuits on the same
        # condition, so nothing is deleted and nothing needs preserving.
        return []
    from .flow_workspace import GroupPreservationError, preserve_group_work

    try:
        return preserve_group_work(root, flow.flow_id, branches)
    except GroupPreservationError as exc:
        logger.warning(
            "Refusing to rewind flow %s: group branch %s could not be preserved "
            "(%s)", flow.flow_id, exc.branch, exc.reason,
        )
        raise RewindError(
            t(
                "engine.rewind.group_preserve_failed",
                branch=exc.branch,
                error=exc.reason,
            )
        ) from exc
    except Exception as exc:  # noqa: BLE001 - same fail-closed rule
        logger.warning(
            "Refusing to rewind flow %s: group work could not be preserved (%s)",
            flow.flow_id, exc,
        )
        raise RewindError(
            t(
                "engine.rewind.group_preserve_failed",
                branch=", ".join(branches),
                error=str(exc),
            )
        ) from exc


def _cleanup_branches(
    flow: "FlowInstance", branches: List[str], project_root: Optional[Any]
) -> List[str]:
    """Remove the DAG group worktrees / leaf branches a rewound step created.

    They are derived products of the step being discarded: keeping them would
    leave the next attempt to either collide with a stale worktree of the same
    branch name or silently inherit its half-finished tree. Their CONTENT is
    not lost — reaching this function at all means
    :func:`_preserve_group_work` returned normally, i.e. every branch here is
    already under a safe ref or was verified to hold nothing recoverable; any
    other outcome raised and this never ran.

    INVARIANT: removal is VERIFIED, and an unremoved branch/worktree aborts the
    rewind (``RewindError``) before any flow state is touched. Both cleanup
    helpers log-and-return on failure — a lock, a permission error or damaged
    git metadata leaves the branch and its registered worktree in place while
    every call returns normally. Trusting that would delete the step state and
    report success, and the rebuilt IMPLEMENT would then fail to create the
    very worktree it needs. Refusing leaves a flow the operator can retry once
    the git problem is fixed.

    INVARIANT: "verified clean" and "the ref went away" are tracked SEPARATELY
    and both are reported on the refusal. Verification runs AFTER the deletion
    and can fail on its own (``group_cleanup_residue`` raises when git cannot
    answer; a directory ``rmtree`` could not finish is a leftover) — collapsing
    the two would file a branch whose ref really is deleted under "nothing
    happened", and the caller would then hand the flow back still recording
    that group as done while its commits only reach the tree through a safety
    ref. The refusal stands either way; the group state must go with it.
    """
    from ..i18n import t

    cleaned: List[str] = []
    root = _flow_root(flow, project_root)
    if root is None:
        return cleaned
    from . import worktree as worktree_mod
    from .flow_workspace import group_cleanup_residue

    residual: List[str] = []
    deleted: List[str] = []
    for branch in branches:
        try:
            worktree_mod.force_cleanup_worktree(root, branch)
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            # Nothing was deleted yet, so this branch is only residual.
            logger.warning(
                "Failed to clean group worktree for branch %s: %s", branch, exc,
                exc_info=True,
            )
            residual.append(
                t("engine.rewind.residue_entry", branch=branch, details=exc)
            )
            continue
        try:
            ref_gone = bool(worktree_mod.delete_branch(root, branch))
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            # A deletion that neither confirmed nor denied itself counts as
            # gone: re-running a group that survived costs one call, while
            # skipping one that did not makes the leaf merge find nothing.
            logger.warning(
                "Failed to delete group branch %s: %s", branch, exc, exc_info=True,
            )
            deleted.append(branch)
            residual.append(
                t("engine.rewind.residue_entry", branch=branch, details=exc)
            )
            continue
        if ref_gone:
            deleted.append(branch)
        try:
            leftovers = group_cleanup_residue(root, branch)
            directory = worktree_mod.worktree_path_for_branch(root, branch)
            if directory.exists():
                leftovers.append(
                    t("engine.rewind.residue_directory", path=directory)
                )
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            logger.warning(
                "Failed to verify group cleanup for branch %s: %s", branch, exc,
                exc_info=True,
            )
            residual.append(
                t("engine.rewind.residue_entry", branch=branch, details=exc)
            )
            continue
        if leftovers:
            logger.warning(
                "Group cleanup for branch %s left %s behind",
                branch, ", ".join(leftovers),
            )
            residual.append(
                t(
                    "engine.rewind.residue_entry",
                    branch=branch,
                    details=", ".join(leftovers),
                )
            )
            continue
        cleaned.append(branch)
    if residual:
        raise RewindError(
            t("engine.rewind.group_cleanup_failed", details="; ".join(residual)),
            cleaned_branches=cleaned,
            # A verified-clean branch is gone by definition, even where
            # ``delete_branch`` reported nothing (it never existed) — the
            # caller filters these against what was materialised anyway.
            deleted_branches=list(dict.fromkeys(cleaned + deleted)),
        )
    return cleaned


def _materialised_branches(
    flow: "FlowInstance", branches: List[str], project_root: Optional[Any]
) -> List[str]:
    """The subset of *branches* that really exists as a ref or a worktree."""
    root = _flow_root(flow, project_root)
    if root is None:
        # Same short-circuit as the cleanup: with no git root nothing is
        # deleted, so nothing became dangling.
        return []
    from .flow_workspace import materialised_group_branches

    try:
        return materialised_group_branches(root, branches)
    except Exception:  # noqa: BLE001 - unanswerable == treat all as real
        logger.warning(
            "Could not probe group branches for flow %s; treating all as "
            "materialised", flow.flow_id, exc_info=True,
        )
        return list(branches)


def _group_state_owners(
    flow: "FlowInstance", removed_ids: List[str], gone_branches: List[str]
) -> Dict[str, List[str]]:
    """Map each removed step to the deleted branches its outputs still name."""
    from .models import StepType

    gone = set(gone_branches)
    owners: Dict[str, List[str]] = {}
    if not gone:
        return owners
    for removed_id in removed_ids:
        step = flow.state.steps.get(removed_id)
        if step is None or step.step_type != StepType.IMPLEMENT:
            continue
        owned = [b for b in _group_branches_for_step(flow, step) if b in gone]
        if owned:
            owners[removed_id] = owned
    return owners


def _group_id_of(entry: Any) -> Optional[str]:
    """The group id an ``implemented_groups`` entry carries, old shapes included."""
    if isinstance(entry, str):
        return entry or None
    if isinstance(entry, dict):
        gid = entry.get("group_id") or entry.get("id") or entry.get("name")
        return str(gid) if gid else None
    return None


def _group_branch_map(flow: "FlowInstance", outputs: Dict[str, Any]) -> Dict[str, str]:
    """Map each group id an implement step's outputs name to ITS branch.

    WHY the recorded branch wins over the derived ``impl/<flow>/<group>`` name:
    a relay heir never had a branch of its own — it inherited its predecessor's,
    and that is the ref whose deletion actually invalidates its state. Deriving
    the name would answer for a branch that never existed and leave the heir's
    record standing after its real branch went away.
    """
    mapping: Dict[str, str] = {}
    preserved = outputs.get("dag_preserved_worktrees") or {}
    if isinstance(preserved, dict):
        for gid, record in preserved.items():
            branch = ""
            if isinstance(record, dict):
                branch = str(record.get("branch") or "").strip()
            mapping[str(gid)] = branch or f"impl/{flow.flow_id}/{gid}"
    for entry in outputs.get("implemented_groups") or []:
        gid = _group_id_of(entry)
        if gid and gid not in mapping:
            mapping[gid] = f"impl/{flow.flow_id}/{gid}"
    return mapping


def _invalidate_group_state(
    flow: "FlowInstance", owners: Dict[str, List[str]]
) -> List[str]:
    """Strip the resume state that names group branches no longer on disk.

    INVARIANT: the scrub is per GROUP, never per step. A cleanup that fails
    part way deletes some of a step's branches and leaves the rest — worktree
    directory included — in place, so "this step owns a deleted branch" says
    nothing about its other groups. Dropping the whole
    ``dag_preserved_worktrees`` dict there would discard the record that lets
    the continuation ADOPT a surviving worktree; without it the scheduler tries
    to create a worktree for a branch that is still checked out at the survivor
    and the group fails on every retry.

    So a group's ``dag_preserved_worktrees`` entry goes only when its own
    branch was actually deleted — reusing it would otherwise hand the
    continuation a cwd that does not exist. Its ``implemented_groups`` entry
    (with its summary) goes on the same condition AND only for a step that has
    NOT completed: a completed implement step's groups were merged into the
    flow's tree, so its record is still true and downstream steps read it for
    context, whereas an interrupted step's record is exactly the thing that
    would make the continuation skip a group whose only copy is now a safety
    ref.
    """
    from .models import StepStatus

    invalidated: List[str] = []
    for step_id, gone_branches in owners.items():
        step = flow.state.steps.get(step_id)
        if step is None:
            continue
        outputs = step.outputs if step.outputs is not None else {}
        gone = set(gone_branches or [])
        branch_of = _group_branch_map(flow, outputs)
        dropped = {gid for gid, branch in branch_of.items() if branch in gone}
        if not dropped:
            continue
        preserved = outputs.get("dag_preserved_worktrees") or {}
        if isinstance(preserved, dict) and preserved:
            survivors = {
                gid: record
                for gid, record in preserved.items()
                if str(gid) not in dropped
            }
            if len(survivors) != len(preserved):
                if survivors:
                    outputs["dag_preserved_worktrees"] = survivors
                else:
                    outputs.pop("dag_preserved_worktrees", None)
                step.outputs = outputs
        if getattr(step, "status", None) == StepStatus.COMPLETED:
            continue
        implemented = outputs.get("implemented_groups") or []
        kept = [e for e in implemented if _group_id_of(e) not in dropped]
        if len(kept) == len(implemented):
            continue
        if kept:
            outputs["implemented_groups"] = kept
        else:
            outputs.pop("implemented_groups", None)
        summaries = outputs.get("group_summaries") or []
        kept_summaries = [
            s for s in summaries
            if not (isinstance(s, dict) and s.get("group_id") in dropped)
        ]
        if kept_summaries:
            outputs["group_summaries"] = kept_summaries
        else:
            outputs.pop("group_summaries", None)
        # The verdict travels with the result it belongs to: a group that will
        # be re-run must not keep aggregating the ``partial`` it reported on the
        # attempt whose branch is now gone.
        completion = outputs.get("group_completion")
        if isinstance(completion, dict):
            kept_completion = {
                gid: entry
                for gid, entry in completion.items()
                if str(gid) not in dropped
            }
            if kept_completion:
                outputs["group_completion"] = kept_completion
            else:
                outputs.pop("group_completion", None)
        step.outputs = outputs
        invalidated.append(step_id)
        logger.warning(
            "Flow %s: dropped the recorded results of group(s) %s on step %s — "
            "the abandoned restart had already removed their branches (%s)",
            flow.flow_id, ", ".join(sorted(dropped)), step_id,
            ", ".join(gone_branches or []),
        )
    return invalidated


def _group_branches_for_step(flow: "FlowInstance", step: Any) -> List[str]:
    """Every leaf branch name an implement step may have materialised.

    Two sources, unioned: the ids the planned groups declare (implement.py
    derives its branch as ``impl/{flow_id}/{group_id or name}``, so BOTH keys
    must be read — a plan whose groups carry only ``name`` would otherwise
    leak its worktrees through the rewind), and the branch names the
    interrupted run itself recorded into ``dag_preserved_worktrees`` (the
    authoritative record for anything already materialised).
    """
    branches: List[str] = []
    seen = set()

    def _add_id(value: Any) -> None:
        gid = None
        if isinstance(value, str):
            gid = value
        elif isinstance(value, dict):
            gid = value.get("group_id") or value.get("id") or value.get("name")
        if gid:
            branch = f"impl/{flow.flow_id}/{gid}"
            if branch not in seen:
                seen.add(branch)
                branches.append(branch)

    def _add_branch(value: Any) -> None:
        branch = str(value or "").strip()
        if branch and branch not in seen:
            seen.add(branch)
            branches.append(branch)

    for value in (step.inputs or {}).get("task_groups") or []:
        _add_id(value)
    for value in (step.outputs or {}).get("implemented_groups") or []:
        _add_id(value)
    preserved = (step.outputs or {}).get("dag_preserved_worktrees") or {}
    if isinstance(preserved, dict):
        for record in preserved.values():
            if isinstance(record, dict):
                _add_branch(record.get("branch"))
    return branches
