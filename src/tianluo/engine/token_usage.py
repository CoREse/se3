"""Token-usage accounting for se3 flow execution.

Single source of truth for the token / cost data structure shared by the
collection layer (``StreamJSONTracker``), the aggregation layer
(``state_machine.run_step``), and both display ends (CLI ``display.py`` and the
WebUI report cards).

Three pieces live here:

1. :class:`UsageTotals` — the per-step / per-session token + cost tally, with
   field-wise :meth:`UsageTotals.add` merging, JSON-primitive
   :meth:`UsageTotals.to_dict` / :meth:`UsageTotals.from_dict` round-tripping
   (tolerant of missing / ``None`` fields → ``0``), and ``is_empty``.
2. A **step-scoped accumulator** built on a :class:`contextvars.ContextVar`:
   :func:`accumulate_step_usage` opens a fresh accumulator for the duration of a
   step's handler, and :func:`add_call_usage` best-effort folds each LLM
   subprocess's usage into whatever accumulator is currently in scope. Outside a
   scope, :func:`add_call_usage` is a safe no-op.
3. Human-readable formatting helpers (:func:`format_cost`,
   :func:`format_usage_line`) that render labelled, unit-suffixed strings for
   the CLI / Web ends, safe against empty / ``None`` input.
"""

from __future__ import annotations

import contextvars
import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, Mapping, Optional

logger = logging.getLogger(__name__)


def _coerce_int(value: Any) -> int:
    """Best-effort coerce ``value`` to a non-negative-tolerant int.

    Missing / ``None`` / unparseable values become ``0`` so a partial or
    malformed usage payload never raises.
    """
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _coerce_float(value: Any) -> float:
    """Best-effort coerce ``value`` to a float; missing / ``None`` / bad → ``0.0``."""
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class UsageTotals:
    """Aggregate token counts and cost for a step or a whole session.

    All four token fields are integers and ``total_cost_usd`` is a float. The
    type is additive: :meth:`add` folds another tally in field-by-field, which
    is how a step's many LLM calls (retry / rotation / two-phase extraction)
    merge into one step total, and how each step total merges into the session
    total.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    total_cost_usd: float = 0.0

    def add(self, other: Optional["UsageTotals"]) -> "UsageTotals":
        """Accumulate ``other`` into ``self`` field-by-field; returns ``self``.

        A ``None`` ``other`` is ignored (no-op), so callers can fold an
        optional usage value without a guard.
        """
        if other is None:
            return self
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_creation_input_tokens += other.cache_creation_input_tokens
        self.cache_read_input_tokens += other.cache_read_input_tokens
        self.total_cost_usd += other.total_cost_usd
        return self

    @property
    def total_tokens(self) -> int:
        """Sum of all four token counts (input + output + both cache kinds)."""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )

    def is_empty(self) -> bool:
        """True when every token field is zero and the cost is (near) zero.

        Used by the display layer to suppress an empty usage block / footnote.
        """
        return self.total_tokens == 0 and self.total_cost_usd == 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a JSON-primitive dict (ints + a float).

        The key set is stable so it can be persisted in ``step.outputs`` /
        ``State.session_token_usage`` and read back by both display ends.
        """
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "total_cost_usd": self.total_cost_usd,
        }

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "UsageTotals":
        """Reconstruct from a (possibly partial / ``None``) mapping.

        Missing or ``None`` fields fall back to ``0`` / ``0.0`` via the
        ``_coerce_*`` helpers, so an older ``engine.json`` lacking the field —
        or a raw CLI ``usage`` payload with only some keys — loads cleanly.
        """
        if not data:
            return cls()
        return cls(
            input_tokens=_coerce_int(data.get("input_tokens")),
            output_tokens=_coerce_int(data.get("output_tokens")),
            cache_creation_input_tokens=_coerce_int(
                data.get("cache_creation_input_tokens")
            ),
            cache_read_input_tokens=_coerce_int(data.get("cache_read_input_tokens")),
            total_cost_usd=_coerce_float(data.get("total_cost_usd")),
        )


# ---------------------------------------------------------------------------
# Step-scoped accumulator
# ---------------------------------------------------------------------------

# Holds the UsageTotals accumulator for the step currently executing in this
# context, or None when no step scope is active. A ContextVar (not a plain
# module global) is used so that the scope is correctly isolated per-thread /
# per-async-task and restored on exit — multiple LLMCaller instances within one
# step (main call + json_extractor) all see the same in-scope accumulator.
_current_step_usage: contextvars.ContextVar[Optional[UsageTotals]] = (
    contextvars.ContextVar("se3_current_step_usage", default=None)
)

# Serializes concurrent folds into a step accumulator that is shared across
# threads. The DAG-parallel implement path runs each task group on its own
# ThreadPoolExecutor worker, all bound (via use_step_usage) to the SAME step
# accumulator object; without this lock their concurrent UsageTotals.add()
# read-modify-write on the shared int/float fields could lose updates. The lock
# is held only for the brief field-wise fold, so contention is negligible.
_accumulate_lock = threading.Lock()


@contextmanager
def accumulate_step_usage() -> Iterator[UsageTotals]:
    """Open a fresh step-scoped usage accumulator for the ``with`` body.

    Every :func:`add_call_usage` call made while this scope is active folds into
    the yielded :class:`UsageTotals`. The previous scope (if any) is restored on
    exit, so nested scopes behave sanely. The accumulator is yielded so the
    caller can read the step total after the body returns::

        with accumulate_step_usage() as step_usage:
            handler(step, flow)
        # step_usage now holds this step's merged token / cost total
    """
    totals = UsageTotals()
    token = _current_step_usage.set(totals)
    try:
        yield totals
    finally:
        _current_step_usage.reset(token)


def current_step_usage() -> Optional[UsageTotals]:
    """Return the in-scope step accumulator, or ``None`` outside any scope."""
    return _current_step_usage.get()


@contextmanager
def use_step_usage(accumulator: Optional[UsageTotals]) -> Iterator[None]:
    """Bind an existing step accumulator into the current context.

    Unlike :func:`accumulate_step_usage` (which opens a *fresh* accumulator),
    this re-establishes a *given* accumulator object as the in-scope step total.
    It is used to carry a parent step's scope across a thread boundary: a
    ``ThreadPoolExecutor`` worker starts with a fresh contextvars context that
    does not see the scope opened on the main thread, so the DAG-parallel
    implement path captures the parent accumulator on the scheduling thread and
    re-binds it inside each worker via this helper. Every :func:`add_call_usage`
    made inside the ``with`` body then folds into that shared accumulator
    (serialized by ``_accumulate_lock``).

    Passing ``None`` is a no-op so callers need no guard when no step scope is
    active (e.g. an ad-hoc DAG run outside ``run_step``).
    """
    if accumulator is None:
        yield
        return
    token = _current_step_usage.set(accumulator)
    try:
        yield
    finally:
        _current_step_usage.reset(token)


def add_call_usage(usage: Any) -> None:
    """Best-effort fold one LLM call's usage into the in-scope step accumulator.

    ``usage`` may be a :class:`UsageTotals`, a mapping (raw CLI ``usage`` dict),
    or ``None``. Outside a step scope this is a no-op (e.g. the sync flow, or
    ad-hoc callers). Any failure is swallowed and debug-logged so usage
    accounting never disrupts the LLM call path.
    """
    try:
        target = _current_step_usage.get()
        if target is None:
            return  # No active step scope — safe no-op.
        if usage is None:
            return
        if isinstance(usage, UsageTotals):
            increment = usage
        elif isinstance(usage, Mapping):
            increment = UsageTotals.from_dict(usage)
        else:
            return
        # The accumulator may be shared across DAG worker threads (see
        # use_step_usage); serialize the read-modify-write fold so concurrent
        # group calls never lose an update.
        with _accumulate_lock:
            target.add(increment)
    except Exception:  # pragma: no cover - defensive; never break the call path
        logger.debug("Failed to accumulate call usage", exc_info=True)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def format_cost(total_cost_usd: Any) -> str:
    """Render a USD cost as ``$0.0123`` (4 decimal places).

    Tolerates ``None`` / non-numeric input (→ ``$0.0000``). The 4-dp precision
    keeps sub-cent LLM costs legible without scientific notation.
    """
    return f"${_coerce_float(total_cost_usd):.4f}"


def _format_tokens(n: int) -> str:
    """Render a token count with thousands separators (e.g. ``12,345``)."""
    return f"{n:,}"


def format_round_usage_footer(
    round_totals: Optional[UsageTotals],
    cumulative_totals: Optional[UsageTotals],
) -> str:
    """Render a compact single-line per-round usage footer for CLI interactive steps.

    Used by the interactive multi-round steps (discovery / confirm) to show, at
    the tail of an assistant message block, both this round's incremental token
    usage and the running cumulative total. Example (en-US)::

        This round 1,234 in / 567 out · Total 12,345 in / 6,789 out

    The label chrome ("this round" / "total") is UI text, so it renders through
    ``tianluo.i18n`` and follows the active language; only the input / output token
    counts are surfaced (per the task copy format), with the same
    thousands-separator style as :func:`format_usage_line` /
    ``render_usage_block`` so the numbers stay consistent across the whole
    project. ``None`` inputs degrade to zeros; the decision to suppress the
    footer for a round that issued no LLM call is the caller's (it gates on
    :meth:`UsageTotals.is_empty`), not this function's.
    """
    from ..i18n import t

    if round_totals is None:
        round_totals = UsageTotals()
    if cumulative_totals is None:
        cumulative_totals = UsageTotals()
    return t(
        "engine.usage.round_footer",
        round_in=_format_tokens(round_totals.input_tokens),
        round_out=_format_tokens(round_totals.output_tokens),
        cum_in=_format_tokens(cumulative_totals.input_tokens),
        cum_out=_format_tokens(cumulative_totals.output_tokens),
    )


def format_usage_line(totals: Optional[UsageTotals]) -> str:
    """Render a compact, single-line labelled usage summary.

    Example (en-US)::

        in 12,345 · out 6,789 · cache(r/w) 1,000/200 · $0.0123

    The labels come from ``tianluo.i18n`` and follow the active language: this line
    is embedded inside already-localized wrappers (e.g. the discovery cumulative
    footer), so hardcoding them would render a mixed-language line. Safe for
    ``None`` / empty input — an empty tally renders as the same labelled line
    with zeros, so callers that still want to show "no usage" can, while display
    layers typically guard on :meth:`UsageTotals.is_empty` first.
    """
    from ..i18n import t

    if totals is None:
        totals = UsageTotals()
    return t(
        "engine.usage.line",
        input=_format_tokens(totals.input_tokens),
        output=_format_tokens(totals.output_tokens),
        cache_read=_format_tokens(totals.cache_read_input_tokens),
        cache_write=_format_tokens(totals.cache_creation_input_tokens),
        cost=format_cost(totals.total_cost_usd),
    )
