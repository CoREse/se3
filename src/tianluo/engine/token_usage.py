"""Token-usage accounting for luo flow execution.

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
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Mapping, Optional

from tianluo.usage import (
    LEGACY_UNKNOWN_CALL_ID,
    UsageRecord,
    UsageStatus,
    aggregate_usage_records,
    legacy_usage_record,
)

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
    usage_records: List[UsageRecord] = field(default_factory=list, repr=False)
    legacy_usage_missing: bool = field(default=False, repr=False, compare=False)
    # WHY: a record-less legacy tally folded into another accumulator needs a
    # call id that is unique per SOURCE tally. A positional id ("legacy-add-1")
    # collides whenever two accumulators each fold one legacy tally, and the
    # identity-based dedup then silently drops one distinct measurement from
    # the flow totals. This per-instance token makes each tally's adapted
    # record its own billing unit. Excluded from equality/serialization: it is
    # provenance for the adapter, not part of the tally's value.
    _legacy_id_token: str = field(default="", repr=False, compare=False)

    # Completeness and provenance of the last fold, surfaced in-memory (the
    # legacy five-field serialization shape is fixed, so these never ride
    # ``to_dict``). A duplicate record-less fold — the same tally object
    # folded twice — has no record identity to dedup against and must not
    # silently double a possibly-duplicate measurement.
    partial: bool = field(default=False, repr=False, compare=False)
    diagnostics: List[str] = field(default_factory=list, repr=False, compare=False)
    # Per-instance identities of record-less tallies already folded into this
    # accumulator via the blind field-wise path, so a duplicate fold is
    # skipped instead of summed.
    _folded_sources: set = field(default_factory=set, repr=False, compare=False)

    @classmethod
    def from_usage_records(
        cls, records: List[UsageRecord]
    ) -> "UsageTotals":
        """Build the legacy projection from authoritative usage records."""
        records = list(records)
        if not records:
            return cls()
        aggregate = aggregate_usage_records(records)
        return cls(
            input_tokens=aggregate.logical_input_tokens,
            output_tokens=aggregate.output_tokens,
            cache_creation_input_tokens=(
                aggregate.cache_creation_total_input_tokens
            ),
            cache_read_input_tokens=aggregate.cache_read_input_tokens,
            total_cost_usd=aggregate.actual_cost_usd or 0.0,
            usage_records=records,
            partial=aggregate.usage_status in (
                UsageStatus.PARTIAL,
                UsageStatus.LEGACY_AMBIGUOUS,
            ),
            diagnostics=list(aggregate.diagnostics),
        )

    @classmethod
    def from_usage_record(cls, record: UsageRecord) -> "UsageTotals":
        return cls.from_usage_records([record])

    def add(self, other: Optional["UsageTotals"]) -> "UsageTotals":
        """Accumulate ``other`` into ``self`` field-by-field; returns ``self``.

        A ``None`` ``other`` is ignored (no-op), so callers can fold an
        optional usage value without a guard.
        """
        if other is None:
            return self
        if self.usage_records or other.usage_records:
            records = list(self.usage_records)
            if not records and not self.is_empty():
                records.append(
                    legacy_usage_record(
                        self.to_dict(), call_id=self._legacy_call_id("accumulator")
                    )
                )
            if other.usage_records:
                records.extend(other.usage_records)
            elif not other.is_empty():
                records.append(
                    legacy_usage_record(
                        other.to_dict(), call_id=other._legacy_call_id("add")
                    )
                )
            authoritative = UsageTotals.from_usage_records(records)
            self.input_tokens = authoritative.input_tokens
            self.output_tokens = authoritative.output_tokens
            self.cache_creation_input_tokens = (
                authoritative.cache_creation_input_tokens
            )
            self.cache_read_input_tokens = authoritative.cache_read_input_tokens
            self.total_cost_usd = authoritative.total_cost_usd
            self.usage_records = records
            self.legacy_usage_missing = False
        else:
            # WHY: record-less tallies have no record identity to dedup
            # against, so the field-wise sum must attribute its sources — a
            # duplicate fold (the same tally object folded twice) is skipped
            # with a diagnostic instead of silently doubling a
            # possibly-duplicate measurement into an available-looking total.
            if other is self:
                self.partial = True
                self.diagnostics.append(
                    "record-less tally folded into itself; fold skipped "
                    "and marked partial"
                )
                return self
            source = other._source_identity()
            if source in self._folded_sources:
                self.partial = True
                self.diagnostics.append(
                    "duplicate record-less tally fold skipped by source "
                    "identity; marked partial"
                )
                return self
            self._folded_sources.add(source)
            self.input_tokens += other.input_tokens
            self.output_tokens += other.output_tokens
            self.cache_creation_input_tokens += other.cache_creation_input_tokens
            self.cache_read_input_tokens += other.cache_read_input_tokens
            self.total_cost_usd += other.total_cost_usd
            if not other.is_empty():
                self.legacy_usage_missing = False
        return self

    def _source_identity(self) -> str:
        """Per-instance identity used to attribute record-less folds."""
        if not self._legacy_id_token:
            self._legacy_id_token = uuid.uuid4().hex[:12]
        return self._legacy_id_token

    def _legacy_call_id(self, kind: str) -> str:
        """Stable, per-tally call id for this instance's legacy adaptation."""
        return f"legacy:{kind}:{self._source_identity()}"

    @property
    def total_tokens(self) -> int:
        """Logical input plus output; cache fields are input classifications."""
        return self.input_tokens + self.output_tokens

    @property
    def has_usage_records(self) -> bool:
        return bool(self.usage_records)

    def is_empty(self) -> bool:
        """True when every token field is zero and the cost is (near) zero.

        Used by the display layer to suppress an empty usage block / footnote.
        """
        return (
            self.input_tokens == 0
            and self.output_tokens == 0
            and self.cache_creation_input_tokens == 0
            and self.cache_read_input_tokens == 0
            and self.total_cost_usd == 0.0
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a JSON-primitive dict (ints + a float).

        The key set is stable so it can be persisted in ``step.outputs`` /
        ``State.session_token_usage`` and read back by both display ends.
        """
        data = {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "total_cost_usd": self.total_cost_usd,
        }
        if self.usage_records:
            data["usage_records"] = [record.to_dict() for record in self.usage_records]
        return data

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "UsageTotals":
        """Reconstruct from a (possibly partial / ``None``) mapping.

        Missing or ``None`` fields fall back to ``0`` / ``0.0`` via the
        ``_coerce_*`` helpers, so an older ``engine.json`` lacking the field —
        or a raw CLI ``usage`` payload with only some keys — loads cleanly.
        """
        if data is None:
            return cls(legacy_usage_missing=True)
        if not data:
            return cls(legacy_usage_missing=True)
        if "usage_status" in data or "logical_input_tokens" in data:
            return cls.from_usage_record(UsageRecord.from_dict(data))
        raw_records = data.get("usage_records", [])
        records = [
            UsageRecord.from_dict(item)
            for item in raw_records
            if isinstance(item, Mapping)
        ] if isinstance(raw_records, list) else []
        if records:
            return cls.from_usage_records(records)
        return cls(
            input_tokens=_coerce_int(data.get("input_tokens")),
            output_tokens=_coerce_int(data.get("output_tokens")),
            cache_creation_input_tokens=_coerce_int(
                data.get("cache_creation_input_tokens")
            ),
            cache_read_input_tokens=_coerce_int(data.get("cache_read_input_tokens")),
            total_cost_usd=_coerce_float(data.get("total_cost_usd")),
        )

    def to_usage_records(
        self, call_id: str = LEGACY_UNKNOWN_CALL_ID
    ) -> List[UsageRecord]:
        """Expose records, adapting an old tally without inventing provenance."""
        if self.usage_records:
            return list(self.usage_records)
        if self.legacy_usage_missing:
            return [legacy_usage_record(None, call_id=call_id)]
        return [legacy_usage_record(self.to_dict(), call_id=call_id)]


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
        if isinstance(usage, UsageRecord):
            increment = UsageTotals.from_usage_record(usage)
        elif isinstance(usage, UsageTotals):
            increment = usage
        elif isinstance(usage, Mapping):
            if "usage_status" in usage or "logical_input_tokens" in usage:
                increment = UsageTotals.from_usage_record(
                    UsageRecord.from_dict(usage)
                )
            else:
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
