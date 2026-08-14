"""Provider-neutral usage records and terminal-event aggregation.

This module intentionally depends only on the Python standard library.  It is
shared by the engine, runner adapters, history readers, and the daemon without
forcing any of those layers to import one another.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union


USAGE_SCHEMA_VERSION = 2

_UNEXPANDED_ENV_RE = re.compile(
    r"\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[^{}]+\})"
)


def canonicalize_model_name(value: Any) -> Optional[str]:
    """Return a pricing-safe model identity, or ``None`` when unresolved."""
    model = _optional_text(value)
    if model is None or _UNEXPANDED_ENV_RE.search(model):
        return None
    return model.lower()


def expand_configured_model(
    value: Any, environ: Optional[Mapping[str, str]] = None
) -> Optional[str]:
    """Expand an AgentDef model without admitting unresolved ``$VAR`` text."""
    model = _optional_text(value)
    if model is None:
        return None
    source_env = os.environ if environ is None else environ
    expanded = _expandvars(model, source_env)
    expanded = _optional_text(expanded)
    if expanded is None or _UNEXPANDED_ENV_RE.search(expanded):
        return None
    return expanded


def _expandvars(value: str, environ: Mapping[str, str]) -> str:
    """Expand shell-style variables against an explicit subprocess env."""

    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        name = token[2:-1] if token.startswith("${") else token[1:]
        replacement = environ.get(name)
        return replacement if replacement is not None else token

    return _UNEXPANDED_ENV_RE.sub(replace, value)


def resolve_model_identity(
    *,
    reported_model: Any = None,
    configured_model: Any = None,
    runner_startup_model: Any = None,
    legacy_resolved_model: Any = None,
) -> Tuple[str, str]:
    """Resolve provider > AgentDef > verified runner startup > unknown."""
    candidates = (
        ("provider", reported_model),
        ("agent_config", configured_model),
        ("runner_startup", runner_startup_model),
        ("legacy", legacy_resolved_model),
    )
    for source, candidate in candidates:
        canonical = canonicalize_model_name(candidate)
        if canonical is not None and canonical != "unknown":
            return canonical, source
    return "unknown", "unknown"


class UsageStatus(str, Enum):
    """Completeness of one call/attempt's provider usage report."""

    AVAILABLE = "available"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    LEGACY_AMBIGUOUS = "legacy_ambiguous"


class UsageSemantics(str, Enum):
    """How token snapshots from distinct terminal events compose."""

    CALL_DELTA = "call_delta"
    EVENT_DELTA = "event_delta"
    PROVIDER_SESSION_CUMULATIVE = "provider_session_cumulative"
    MIXED = "mixed"


class CostSemantics(str, Enum):
    """How an actual-cost value is billed and aggregated."""

    CALL_DELTA = "call_delta"
    EVENT_DELTA = "event_delta"
    PROVIDER_SESSION_CUMULATIVE = "provider_session_cumulative"
    MIXED = "mixed"


def _optional_text(value: Any) -> Optional[str]:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def _non_negative_int(value: Any) -> Tuple[int, bool]:
    if value is None:
        return 0, False
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0, True
    if not math.isfinite(parsed) or parsed < 0:
        return 0, True
    return parsed, False


def _optional_cost(value: Any) -> Tuple[Optional[float], bool]:
    if value is None:
        return None, False
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None, True
    if not math.isfinite(parsed) or parsed < 0:
        return None, True
    return parsed, False


def _enum_value(enum_type: Any, value: Any, default: Any) -> Any:
    try:
        return enum_type(value)
    except (TypeError, ValueError):
        return default


def _string_list(value: Any) -> List[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value]


@dataclass
class SessionCostSnapshot:
    """One cumulative actual-cost snapshot bound to its provider session.

    A single call can report cumulative costs for several provider sessions
    (e.g. a multi-turn interactive session plus a subagent session). Each
    snapshot keeps its billing identity so downstream aggregation can take the
    latest valid snapshot PER session before summing across sessions, instead
    of freezing the call's mixed total as an opaque call-level cost.
    """

    provider: Optional[str]
    provider_session_id: str
    actual_cost_usd: float


@dataclass
class UsageRecord:
    """Authoritative usage for one configured LLM call attempt."""

    call_id: str
    attempt: int
    usage_status: UsageStatus = UsageStatus.UNAVAILABLE
    agent_name: Optional[str] = None
    runner_type: Optional[str] = None
    provider: Optional[str] = None
    provider_session_id: Optional[str] = None
    usage_event_id: Optional[str] = None
    reported_model: Optional[str] = None
    configured_model: Optional[str] = None
    runner_startup_model: Optional[str] = None
    resolved_model: str = "unknown"
    resolved_model_source: str = "unknown"
    logical_input_tokens: int = 0
    uncached_input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_creation_5m_input_tokens: int = 0
    cache_creation_1h_input_tokens: int = 0
    actual_cost_usd: Optional[float] = None
    usage_semantics: UsageSemantics = UsageSemantics.EVENT_DELTA
    cost_semantics: CostSemantics = CostSemantics.EVENT_DELTA
    usage_event_ids: List[str] = field(default_factory=list)
    provider_session_ids: List[str] = field(default_factory=list)
    cost_breakdown: List[SessionCostSnapshot] = field(default_factory=list)
    diagnostics: List[str] = field(default_factory=list)
    schema_version: int = USAGE_SCHEMA_VERSION

    @property
    def input_tokens(self) -> int:
        """Compatibility alias for the logical provider input total."""
        return self.logical_input_tokens

    @property
    def generic_cache_creation_input_tokens(self) -> int:
        return self.cache_creation_input_tokens

    @property
    def cache_creation_5_minute_input_tokens(self) -> int:
        return self.cache_creation_5m_input_tokens

    @property
    def cache_creation_1_hour_input_tokens(self) -> int:
        return self.cache_creation_1h_input_tokens

    @property
    def total_tokens(self) -> int:
        """Logical input plus output; cached input is already an input subset."""
        return self.logical_input_tokens + self.output_tokens

    @property
    def cache_creation_total_input_tokens(self) -> int:
        return (
            self.cache_creation_input_tokens
            + self.cache_creation_5m_input_tokens
            + self.cache_creation_1h_input_tokens
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize without collapsing missing actual cost into zero."""
        return {
            "schema_version": self.schema_version,
            "call_id": self.call_id,
            "attempt": self.attempt,
            "usage_status": self.usage_status.value,
            "agent_name": self.agent_name,
            "runner_type": self.runner_type,
            "provider": self.provider,
            "provider_session_id": self.provider_session_id,
            "usage_event_id": self.usage_event_id,
            "reported_model": self.reported_model,
            "configured_model": self.configured_model,
            "runner_startup_model": self.runner_startup_model,
            "resolved_model": self.resolved_model,
            "resolved_model_source": self.resolved_model_source,
            "logical_input_tokens": self.logical_input_tokens,
            "uncached_input_tokens": self.uncached_input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_creation_5m_input_tokens": self.cache_creation_5m_input_tokens,
            "cache_creation_1h_input_tokens": self.cache_creation_1h_input_tokens,
            "actual_cost_usd": self.actual_cost_usd,
            "usage_semantics": self.usage_semantics.value,
            "cost_semantics": self.cost_semantics.value,
            "usage_event_ids": list(self.usage_event_ids),
            "provider_session_ids": list(self.provider_session_ids),
            "cost_breakdown": [
                {
                    "provider": snapshot.provider,
                    "provider_session_id": snapshot.provider_session_id,
                    "actual_cost_usd": snapshot.actual_cost_usd,
                }
                for snapshot in self.cost_breakdown
            ],
            "diagnostics": list(self.diagnostics),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "UsageRecord":
        """Read the versioned schema while tolerating future extra fields."""
        actual_cost, invalid_cost = _optional_cost(data.get("actual_cost_usd"))
        diagnostics = _string_list(data.get("diagnostics"))
        if invalid_cost:
            diagnostics.append("invalid actual_cost_usd in serialized record")

        invalid_serialized = False

        def token(name: str) -> int:
            nonlocal invalid_serialized
            value, invalid = _non_negative_int(data.get(name))
            if invalid:
                invalid_serialized = True
                diagnostics.append(f"invalid {name} in serialized record")
            return value

        status = _enum_value(
            UsageStatus, data.get("usage_status"), UsageStatus.PARTIAL
        )
        if invalid_cost and status == UsageStatus.AVAILABLE:
            status = UsageStatus.PARTIAL
        raw_breakdown = data.get("cost_breakdown")
        cost_breakdown: List[SessionCostSnapshot] = []
        if isinstance(raw_breakdown, list):
            for item in raw_breakdown:
                if not isinstance(item, Mapping):
                    continue
                snapshot_cost, snapshot_invalid = _optional_cost(
                    item.get("actual_cost_usd")
                )
                if snapshot_invalid or snapshot_cost is None:
                    invalid_serialized = True
                    diagnostics.append("invalid cost_breakdown entry")
                    continue
                session = _optional_text(item.get("provider_session_id"))
                if session is None:
                    invalid_serialized = True
                    diagnostics.append("cost_breakdown entry missing session")
                    continue
                cost_breakdown.append(
                    SessionCostSnapshot(
                        provider=_optional_text(item.get("provider")),
                        provider_session_id=session,
                        actual_cost_usd=snapshot_cost,
                    )
                )
        if cost_breakdown and actual_cost is None:
            # A record whose breakdown snapshots exist without a call-level
            # actual cost is an inconsistent shape: downstream surfaces must
            # all derive cost from the same rule (the breakdown snapshots)
            # and the record must not pass as a complete report.
            invalid_serialized = True
            diagnostics.append(
                "record carries cost_breakdown entries without an "
                "actual_cost_usd; breakdown snapshots are the cost source"
            )
        elif cost_breakdown and actual_cost is not None:
            breakdown_total = sum(
                snapshot.actual_cost_usd for snapshot in cost_breakdown
            )
            if breakdown_total > actual_cost and not math.isclose(
                breakdown_total, actual_cost, rel_tol=1e-9, abs_tol=1e-12
            ):
                # A valid record's breakdown snapshots are a subset of its
                # call-level cost, so snapshots summing above the actual cost
                # are self-contradictory — the same invariant
                # UsageEventAggregator.to_record enforces when building the
                # record. Marking it partial keeps the two disagreeing
                # figures from ever passing as a complete report.
                invalid_serialized = True
                diagnostics.append(
                    "record actual_cost_usd is less than the sum of its "
                    "cost_breakdown snapshots; marked partial"
                )
        input_tokens = token("input_tokens")
        cache_read = token("cache_read_input_tokens")
        cache_creation = (
            token("cache_creation_input_tokens")
            if "cache_creation_input_tokens" in data
            else token("generic_cache_creation_input_tokens")
        )
        cache_creation_5m = token(
            "cache_creation_5m_input_tokens"
            if "cache_creation_5m_input_tokens" in data
            else "cache_creation_5_minute_input_tokens"
        )
        cache_creation_1h = token(
            "cache_creation_1h_input_tokens"
            if "cache_creation_1h_input_tokens" in data
            else "cache_creation_1_hour_input_tokens"
        )
        cache_total = (
            cache_read + cache_creation + cache_creation_5m + cache_creation_1h
        )
        if "logical_input_tokens" in data:
            logical_input = token("logical_input_tokens")
            if "uncached_input_tokens" in data:
                uncached_input = token("uncached_input_tokens")
                if uncached_input + cache_total != logical_input:
                    # The invariant of the whole model: logical input is the
                    # sum of uncached input and every cache category, each
                    # token counted once. A payload that contradicts it cannot
                    # be priced as a complete report.
                    invalid_serialized = True
                    diagnostics.append(
                        "serialized record's logical input total does not "
                        "equal uncached input plus its cache categories"
                    )
            else:
                # WHY derive instead of defaulting to 0: a foreign or
                # partially-merged payload that names the logical total and
                # its cache categories but omits the uncached field would
                # otherwise read as "all input cached" and price the uncached
                # portion at zero — an order-of-magnitude understatement
                # reported as AVAILABLE.
                uncached_input = max(logical_input - cache_total, 0)
                if cache_total > logical_input:
                    invalid_serialized = True
                    diagnostics.append(
                        "serialized record's cache categories exceed its "
                        "logical input total"
                    )
        else:
            # Serializations that predate the explicit logical/uncached pair
            # are normalized by their token-field SHAPE — the same rule the
            # event parser applies. Anthropic-shape payloads report input
            # EXCLUDING the cache categories, so the logical total is their
            # sum and the plain input is the uncached portion; subset-shape
            # (OpenAI/Codex) payloads embed the cache categories inside
            # ``input_tokens``, so uncached is input minus the cached subset.
            if any(key in data for key in _ANTHROPIC_SHAPE_KEYS):
                logical_input = input_tokens + cache_total
                uncached_input = (
                    token("uncached_input_tokens")
                    if "uncached_input_tokens" in data
                    else input_tokens
                )
            elif any(key in data for key in _SUBSET_CACHE_SHAPE_KEYS):
                logical_input = input_tokens
                uncached_input = (
                    token("uncached_input_tokens")
                    if "uncached_input_tokens" in data
                    else max(input_tokens - cache_total, 0)
                )
                if cache_total > input_tokens:
                    # A cache subset larger than its own input total is not
                    # credibly normalizable; surface it like the event parser.
                    invalid_serialized = True
                    diagnostics.append(
                        "serialized record's cached subset exceeds its "
                        "input total"
                    )
            else:
                # No cache fields at all: the two shapes coincide and every
                # input token is uncached.
                logical_input = input_tokens
                uncached_input = (
                    token("uncached_input_tokens")
                    if "uncached_input_tokens" in data
                    else input_tokens
                )
        record = cls(
            call_id=str(data.get("call_id") or LEGACY_UNKNOWN_CALL_ID),
            attempt=token("attempt"),
            usage_status=status,
            agent_name=_optional_text(data.get("agent_name")),
            runner_type=_optional_text(data.get("runner_type")),
            provider=_optional_text(data.get("provider")),
            provider_session_id=_optional_text(data.get("provider_session_id")),
            usage_event_id=_optional_text(data.get("usage_event_id")),
            reported_model=_optional_text(data.get("reported_model")),
            configured_model=_optional_text(data.get("configured_model")),
            runner_startup_model=_optional_text(data.get("runner_startup_model")),
            resolved_model=_optional_text(data.get("resolved_model")) or "unknown",
            resolved_model_source=(
                _optional_text(data.get("resolved_model_source")) or "unknown"
            ),
            logical_input_tokens=logical_input,
            uncached_input_tokens=uncached_input,
            output_tokens=token("output_tokens"),
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_creation,
            cache_creation_5m_input_tokens=cache_creation_5m,
            cache_creation_1h_input_tokens=cache_creation_1h,
            actual_cost_usd=actual_cost,
            usage_semantics=_enum_value(
                UsageSemantics,
                data.get("usage_semantics"),
                UsageSemantics.EVENT_DELTA,
            ),
            cost_semantics=_enum_value(
                CostSemantics,
                data.get("cost_semantics"),
                CostSemantics.EVENT_DELTA,
            ),
            usage_event_ids=_string_list(data.get("usage_event_ids")),
            provider_session_ids=_string_list(data.get("provider_session_ids")),
            cost_breakdown=cost_breakdown,
            diagnostics=diagnostics,
            schema_version=token("schema_version") or USAGE_SCHEMA_VERSION,
        )
        if invalid_serialized and record.usage_status == UsageStatus.AVAILABLE:
            record.usage_status = UsageStatus.PARTIAL
        return record


@dataclass
class _NormalizedEvent:
    event_id: str
    explicit_event_id: Optional[str]
    provider: Optional[str]
    provider_session_id: Optional[str]
    reported_model: Optional[str]
    usage_seen: bool
    logical_input_tokens: int
    uncached_input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    cache_creation_5m_input_tokens: int
    cache_creation_1h_input_tokens: int
    actual_cost_usd: Optional[float]
    usage_semantics: UsageSemantics
    cost_semantics: CostSemantics
    partial: bool = False
    # WHY: a re-emitted event id replaces its first-seen entry IN PLACE so the
    # record's ``usage_event_ids`` keeps first-seen order — but the absorption
    # rule needs the re-emission's REAL arrival, since a snapshot can only
    # absorb deltas that arrived before it. List position and arrival order
    # therefore diverge, and only this field carries the latter.
    arrival_index: int = 0

    def token_tuple(self) -> Tuple[int, ...]:
        return (
            self.logical_input_tokens,
            self.uncached_input_tokens,
            self.output_tokens,
            self.cache_read_input_tokens,
            self.cache_creation_input_tokens,
            self.cache_creation_5m_input_tokens,
            self.cache_creation_1h_input_tokens,
        )


def _explicit_event(
    events: List[_NormalizedEvent], event_id: str
) -> Optional[_NormalizedEvent]:
    """The already-normalized event recorded under a provider event id."""
    for event in events:
        if event.explicit_event_id == event_id:
            return event
    return None


_TERMINAL_TYPES = frozenset(
    {
        "result",
        "terminal",
        "turn.completed",
        "turn.failed",
        "turn.error",
        "error",
        "message_stop",
        "response.completed",
        "response.failed",
    }
)


def parse_ndjson_events(
    raw: Union[
        str,
        Mapping[str, Any],
        Sequence[Mapping[str, Any]],
        Iterable[Mapping[str, Any]],
        None,
    ]
) -> List[Dict[str, Any]]:
    """Best-effort conversion of NDJSON or parsed objects to event dicts."""
    if raw is None:
        return []
    if isinstance(raw, Mapping):
        return [dict(raw)]
    if isinstance(raw, str):
        events: List[Dict[str, Any]] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("==="):
                continue
            try:
                item = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(item, dict):
                events.append(item)
        return events
    events = []
    try:
        for item in raw:
            if isinstance(item, Mapping):
                events.append(dict(item))
    except TypeError:
        return []
    return events


def _nested_mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key)
    return value if isinstance(value, Mapping) else {}


def _event_containers(
    data: Mapping[str, Any]
) -> Tuple[Tuple[str, Mapping[str, Any]], ...]:
    """The ordered ``(scope name, mapping)`` pairs one terminal event carries.

    WHY tokens and cost scan the SAME ordered scopes, event body first: a
    record's tokens and its cost must describe one measurement. A compat proxy
    relays the provider's per-exchange ``message`` block while adding its own
    top-level snapshot, so a token scan that preferred the nested block while
    the cost scan preferred the outer one would pair a per-exchange delta with
    a session total — an undercount wearing a complete-looking status.
    """
    return (
        ("event", data),
        ("message", _nested_mapping(data, "message")),
        ("turn", _nested_mapping(data, "turn")),
        ("response", _nested_mapping(data, "response")),
    )


def _extract_usage(data: Mapping[str, Any]) -> Tuple[Optional[Mapping[str, Any]], bool]:
    for _name, container in _event_containers(data):
        if "usage" not in container:
            continue
        value = container.get("usage")
        return (value if isinstance(value, Mapping) else None), True
    return None, False


def _usage_scope(data: Mapping[str, Any]) -> Optional[str]:
    for name, container in _event_containers(data):
        if "usage" in container:
            return name
    return None


_COST_KEYS = ("actual_cost_usd", "total_cost_usd", "cost_usd")


def _cost_key_present(data: Mapping[str, Any]) -> bool:
    for _name, container in _event_containers(data):
        if any(key in container for key in _COST_KEYS):
            return True
    return False


def _extract_cost_scoped(
    data: Mapping[str, Any], prefer_total: bool = False
) -> Tuple[Optional[float], bool, bool, Optional[str]]:
    containers = _event_containers(data)
    if prefer_total:
        # A cumulative declaration must extract the session TOTAL: a nested
        # ``total_cost_usd`` is the snapshot, while a sibling ``cost_usd`` is
        # the per-result delta the snapshot already contains. Extracting the
        # delta and labelling it cumulative would silently discard the real
        # session total.
        for name, container in containers:
            if "total_cost_usd" in container:
                cost, invalid = _optional_cost(container.get("total_cost_usd"))
                return cost, True, invalid, name
    for name, container in containers:
        for key in ("actual_cost_usd", "cost_usd", "total_cost_usd"):
            if key in container:
                cost, invalid = _optional_cost(container.get(key))
                return cost, True, invalid, name
    return None, False, False, None


def _extract_cost(
    data: Mapping[str, Any], prefer_total: bool = False
) -> Tuple[Optional[float], bool, bool]:
    cost, seen, invalid, _scope = _extract_cost_scoped(data, prefer_total)
    return cost, seen, invalid


def _usage_cost_scope_mismatch(
    data: Mapping[str, Any], prefer_total: bool
) -> Optional[str]:
    """Diagnostic when a record's tokens and cost come from competing scopes.

    Only a cost scope that ALSO carries its own ``usage`` proves a competing
    measurement was passed over: a cost sitting alone in a nested container is
    simply where this event reported the same call's cost.
    """
    usage_scope = _usage_scope(data)
    _cost, cost_seen, _invalid, cost_scope = _extract_cost_scoped(data, prefer_total)
    if usage_scope is None or not cost_seen or cost_scope is None:
        return None
    if usage_scope == cost_scope:
        return None
    scopes = dict(_event_containers(data))
    if "usage" not in scopes.get(cost_scope, {}):
        return None
    return (
        f"token usage read from the {usage_scope} scope while cost came from "
        f"the {cost_scope} scope, which reports its own usage; the two "
        "figures describe different measurement scopes"
    )


def _first_text(data: Mapping[str, Any], keys: Sequence[str]) -> Optional[str]:
    containers = (
        data,
        _nested_mapping(data, "message"),
        _nested_mapping(data, "session"),
        _nested_mapping(data, "turn"),
        _nested_mapping(data, "response"),
        _nested_mapping(data, "thread"),
    )
    for container in containers:
        for key in keys:
            value = _optional_text(container.get(key))
            if value:
                return value
    return None


def _provider_name(value: Optional[str], usage: Optional[Mapping[str, Any]]) -> Optional[str]:
    if value:
        lowered = value.lower()
        if lowered in {"claude", "anthropic"}:
            return "anthropic"
        if lowered in {"codex", "openai"}:
            return "openai"
        return lowered
    if not usage:
        return None
    if "cached_input_tokens" in usage or "input_tokens_details" in usage:
        return "openai"
    if any(
        key in usage
        for key in (
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "cache_creation",
        )
    ):
        return "anthropic"
    return None


def _provider_from_runner(runner_type: Optional[str]) -> Optional[str]:
    lowered = (runner_type or "").lower()
    if lowered in {"claude-code", "claude-interactive", "claude"}:
        return "anthropic"
    if lowered in {"codex", "openai"}:
        return "openai"
    return None


def _usage_value(
    usage: Mapping[str, Any], names: Sequence[str]
) -> Tuple[int, bool, bool]:
    for name in names:
        if name in usage:
            value, invalid = _non_negative_int(usage.get(name))
            return value, True, invalid
    return 0, False, False


def _usage_mapping_has_tokens(usage: Mapping[str, Any]) -> bool:
    """True when a usage payload declares at least one recognized token field.

    A re-emission of a usage event id whose ``usage`` object holds only
    non-token content is a degraded replay, not an explicit zero — it must
    not erase the measured snapshot it duplicates.
    """
    return any(
        key in usage
        for key in (
            "input_tokens",
            "prompt_tokens",
            "output_tokens",
            "completion_tokens",
            "cache_read_input_tokens",
            "cached_input_tokens",
            "input_tokens_details",
            "cache_creation_input_tokens",
            "generic_cache_creation_input_tokens",
            "cache_creation",
            "cache_creation_5m_input_tokens",
            "cache_creation_5_minute_input_tokens",
            "ephemeral_5m_input_tokens",
            "cache_creation_1h_input_tokens",
            "cache_creation_1_hour_input_tokens",
            "ephemeral_1h_input_tokens",
        )
    )


def _cache_creation_breakdown(
    usage: Mapping[str, Any]
) -> Tuple[int, int, int, bool]:
    generic, generic_seen, invalid_generic = _usage_value(
        usage, ("cache_creation_input_tokens", "generic_cache_creation_input_tokens")
    )
    five, five_seen, invalid_five = _usage_value(
        usage,
        (
            "cache_creation_5m_input_tokens",
            "cache_creation_5_minute_input_tokens",
            "ephemeral_5m_input_tokens",
        ),
    )
    hour, hour_seen, invalid_hour = _usage_value(
        usage,
        (
            "cache_creation_1h_input_tokens",
            "cache_creation_1_hour_input_tokens",
            "ephemeral_1h_input_tokens",
        ),
    )
    nested = usage.get("cache_creation")
    if isinstance(nested, Mapping):
        if not five_seen:
            five, five_seen, invalid = _usage_value(
                nested, ("ephemeral_5m_input_tokens", "5m_input_tokens")
            )
            invalid_five = invalid_five or invalid
        if not hour_seen:
            hour, hour_seen, invalid = _usage_value(
                nested, ("ephemeral_1h_input_tokens", "1h_input_tokens")
            )
            invalid_hour = invalid_hour or invalid
    partial = invalid_generic or invalid_five or invalid_hour
    if generic_seen and (five_seen or hour_seen):
        if five + hour > generic:
            partial = True
            generic = 0
        else:
            generic -= five + hour
    return generic, five, hour, partial


# ``prompt_tokens_details`` is the OpenAI Chat-Completions spelling of
# ``input_tokens_details`` (it carries ``cached_tokens``); a compat proxy or
# wrapper emitting the standard Chat-Completions usage object must normalize
# through the same subset rule, or its cached tokens get billed as uncached.
_SUBSET_CACHE_SHAPE_KEYS = (
    "cached_input_tokens",
    "input_tokens_details",
    "prompt_tokens_details",
)

# Anthropic-shaped payloads report input EXCLUDING the cache categories; the
# record spelling of the same shape (including the legacy tally aliases).
_ANTHROPIC_SHAPE_KEYS = (
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "generic_cache_creation_input_tokens",
    "cache_creation_5m_input_tokens",
    "cache_creation_5_minute_input_tokens",
    "cache_creation_1h_input_tokens",
    "cache_creation_1_hour_input_tokens",
)


def _is_subset_cache_shape(usage: Mapping[str, Any]) -> bool:
    """True when the payload uses the OpenAI/Codex cached-input-subset shape.

    The two provider shapes disagree on whether ``input_tokens`` contains the
    cached tokens, so the shape marker — not the provider name — decides which
    arithmetic keeps the priced categories mutually exclusive.
    """
    return any(key in usage for key in _SUBSET_CACHE_SHAPE_KEYS)


def _normalize_tokens(
    usage: Mapping[str, Any], provider: Optional[str] = None
) -> Tuple[Tuple[int, ...], bool]:
    input_tokens, _, invalid_input = _usage_value(
        usage, ("input_tokens", "prompt_tokens")
    )
    output_tokens, _, invalid_output = _usage_value(
        usage, ("output_tokens", "completion_tokens")
    )
    cache_read, cache_seen, invalid_cache = _usage_value(
        usage, ("cache_read_input_tokens", "cached_input_tokens")
    )
    for details_key in ("input_tokens_details", "prompt_tokens_details"):
        if cache_seen:
            break
        details = usage.get(details_key)
        if isinstance(details, Mapping):
            cache_read, cache_seen, invalid = _usage_value(
                details, ("cached_tokens", "cached_input_tokens")
            )
            invalid_cache = invalid_cache or invalid
    generic_create, create_5m, create_1h, invalid_creation = (
        _cache_creation_breakdown(usage)
    )
    partial = invalid_input or invalid_output or invalid_cache or invalid_creation

    cache_total = cache_read + generic_create + create_5m + create_1h
    # WHY: normalization follows the payload's *token field shape*, never the
    # declared provider/runner name — a compat proxy or wrapper may report an
    # arbitrary provider string while emitting one of the two known shapes, and
    # keying on the name would either skip normalization or apply the wrong
    # subset rule and double-bill the cached tokens.
    if _is_subset_cache_shape(usage):
        # OpenAI/Codex shape: ``cached_input_tokens`` is a subset of
        # ``input_tokens``, and so is any cache-creation category a payload
        # also declares. Subtract the full cache subset so each priced category
        # counts its tokens exactly once; leaving them inside ``uncached``
        # would bill them both as uncached input and in their own category.
        logical_input = input_tokens
        if cache_total > logical_input:
            # Not credibly normalizable: a subset larger than its own total.
            partial = True
        uncached = max(logical_input - cache_total, 0)
    else:
        # Anthropic shape (and the no-cache-field case, where cache_total is
        # 0 and the two shapes coincide): ``input_tokens`` already EXCLUDES
        # cache reads and cache creation, so the reported categories are
        # mutually exclusive and the logical input total is their sum.
        uncached = input_tokens
        logical_input = uncached + cache_total
    return (
        logical_input,
        uncached,
        output_tokens,
        cache_read,
        generic_create,
        create_5m,
        create_1h,
    ), partial


def _stable_event_key(data: Mapping[str, Any], usage: Any, cost: Any) -> str:
    payload = {
        "type": data.get("type"),
        "subtype": data.get("subtype"),
        "session": _first_text(data, ("session_id", "thread_id", "id")),
        "usage": usage,
        "cost": cost,
        "result": data.get("result") if isinstance(data.get("result"), str) else None,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "synthetic:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def _terminal_fingerprint(data: Mapping[str, Any], usage: Any, cost: Any) -> str:
    """Content identity of one terminal event, for replay detection.

    Two events sharing a container id are the same measurement only when their
    usage, cost and outcome text all match; a differing measurement under the
    same container is a distinct event that must contribute.
    """
    payload = {
        "type": data.get("type"),
        "subtype": data.get("subtype"),
        "result": data.get("result"),
        "usage": usage,
        "cost": cost,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _declared_usage_semantics(
    data: Mapping[str, Any], default: UsageSemantics
) -> UsageSemantics:
    value = data.get("usage_semantics", data.get("token_semantics"))
    aliases = {
        "delta": UsageSemantics.EVENT_DELTA,
        "independent": UsageSemantics.EVENT_DELTA,
        "cumulative": UsageSemantics.PROVIDER_SESSION_CUMULATIVE,
        "session_cumulative": UsageSemantics.PROVIDER_SESSION_CUMULATIVE,
    }
    if value in aliases:
        return aliases[value]
    return _enum_value(UsageSemantics, value, default)


def _declared_cost_semantics(
    data: Mapping[str, Any],
    default: CostSemantics,
    provider: Optional[str],
    session_id: Optional[str],
) -> CostSemantics:
    value = data.get("cost_semantics")
    if value is not None:
        aliases = {
            "delta": CostSemantics.EVENT_DELTA,
            "cumulative": CostSemantics.PROVIDER_SESSION_CUMULATIVE,
            "session_cumulative": CostSemantics.PROVIDER_SESSION_CUMULATIVE,
        }
        if value in aliases:
            return aliases[value]
        return _enum_value(CostSemantics, value, default)
    if provider == "anthropic" and session_id:
        # A session-cumulative total may sit in any container the cost
        # extractor reads (message / turn / response) — the semantics
        # declaration must see the same shapes extraction accepts, otherwise
        # a nested cumulative series is declared delta and blindly summed.
        for container in (
            data,
            _nested_mapping(data, "message"),
            _nested_mapping(data, "turn"),
            _nested_mapping(data, "response"),
        ):
            if "total_cost_usd" in container:
                return CostSemantics.PROVIDER_SESSION_CUMULATIVE
    return default


# Internal markers set on a breakdown child whose cost / usage is already
# included in the retained parent terminal's totals (never in provider
# payloads).
_PARENT_COST_COVERS_KEY = "_parent_cost_covers"
_PARENT_USAGE_COVERS_KEY = "_parent_usage_covers"

_SESSION_KEYS = ("provider_session_id", "session_id", "thread_id")


def _terminal_candidates(data: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    event_type = str(data.get("type", ""))
    if event_type not in _TERMINAL_TYPES and not data.get("terminal"):
        return []
    usage, usage_key_seen = _extract_usage(data)
    _, cost_key_seen, _ = _extract_cost(data)
    parent_session = _first_text(data, _SESSION_KEYS)

    candidates: List[Mapping[str, Any]] = (
        [data] if (usage_key_seen or cost_key_seen) else []
    )
    for key in ("iterations", "subagents", "results"):
        children = data.get(key)
        if not isinstance(children, list):
            continue
        for child in children:
            if not isinstance(child, Mapping):
                continue
            _child_usage, child_usage_seen = _extract_usage(child)
            _child_cost, child_cost_seen, _invalid = _extract_cost(child)
            if usage_key_seen and not (child_usage_seen or child_cost_seen):
                # With a parent snapshot in hand, a breakdown entry that
                # reports nothing adds no measurement of its own; scanning it
                # would only manufacture a missing-measurement diagnostic for
                # data the parent already carries.
                continue
            child_session = _first_text(child, _SESSION_KEYS)
            # WHY the parent's totals cover a child only within the SAME
            # provider session: an iteration/turn breakdown of one session is
            # summarized by that session's snapshot, but a subagent running in
            # its OWN provider session is billed separately — dropping it
            # would make its tokens and cost vanish from every usage surface
            # with no diagnostic (requirement: reported usage is never
            # silently discarded).
            same_session = child_session is None or child_session == parent_session
            merged = dict(child)
            if usage_key_seen and same_session:
                merged[_PARENT_USAGE_COVERS_KEY] = True
            if cost_key_seen and same_session:
                # The retained parent cost is the turn/response TOTAL, so it
                # already covers this same-session breakdown entry. The child
                # still contributes its tokens (and keeps its own event
                # identity), only its cost is suppressed, otherwise the flow
                # is billed the parent total plus every child line again.
                merged[_PARENT_COST_COVERS_KEY] = True
            if "type" not in child:
                merged["type"] = "result"
            for metadata_key in (
                "provider",
                "provider_name",
                "provider_session_id",
                "session_id",
                "thread_id",
                "model",
                "model_name",
            ):
                if metadata_key not in merged and metadata_key in data:
                    merged[metadata_key] = data[metadata_key]
            candidates.extend(_terminal_candidates(merged))
    return candidates or [data]


class UsageEventAggregator:
    """Incrementally aggregate every terminal usage event for one attempt."""

    def __init__(
        self,
        *,
        call_id: Optional[str] = None,
        attempt: int = 0,
        agent_name: Optional[str] = None,
        runner_type: Optional[str] = None,
        provider: Optional[str] = None,
        provider_session_id: Optional[str] = None,
        reported_model: Optional[str] = None,
        configured_model: Optional[str] = None,
        runner_startup_model: Optional[str] = None,
        resolved_model: Optional[str] = None,
        usage_semantics: UsageSemantics = UsageSemantics.EVENT_DELTA,
        cost_semantics: CostSemantics = CostSemantics.EVENT_DELTA,
    ) -> None:
        self.call_id = call_id or str(uuid.uuid4())
        self.attempt = max(int(attempt or 0), 0)
        self.agent_name = agent_name
        self.runner_type = runner_type
        self.provider = _provider_name(provider, None) or _provider_from_runner(
            runner_type
        )
        self.provider_session_id = provider_session_id
        self.reported_model = reported_model
        self.configured_model = _optional_text(configured_model)
        self.runner_startup_model = _optional_text(runner_startup_model)
        self.resolved_model, self.resolved_model_source = resolve_model_identity(
            reported_model=reported_model,
            configured_model=self.configured_model,
            runner_startup_model=self.runner_startup_model,
            legacy_resolved_model=resolved_model,
        )
        self.default_usage_semantics = usage_semantics
        self.default_cost_semantics = cost_semantics
        self._events: List[_NormalizedEvent] = []
        self._seen_event_keys: set[str] = set()
        self._container_fingerprints: Dict[str, str] = {}
        self._diagnostics: List[str] = []
        self._terminal_seen = False
        self._partial = False
        self._missing_usage_terminals = False
        self._arrival_counter = 0

    def add_event(self, data: Mapping[str, Any]) -> None:
        """Consume metadata and all usage-bearing terminal candidates."""
        if not isinstance(data, Mapping):
            return
        model = _first_text(data, ("model", "model_name"))
        if model:
            self.reported_model = model
            self.resolved_model, self.resolved_model_source = resolve_model_identity(
                reported_model=model,
                configured_model=self.configured_model,
                runner_startup_model=self.runner_startup_model,
            )
        provider = _provider_name(
            _first_text(data, ("provider", "provider_name")), None
        )
        if provider:
            self.provider = provider
        session_id = _first_text(
            data, ("provider_session_id", "session_id", "thread_id")
        )
        if session_id:
            self.provider_session_id = session_id

        candidates = _terminal_candidates(data)
        if not candidates:
            return
        self._terminal_seen = True
        for candidate in candidates:
            self._add_terminal(candidate)

    def _add_terminal(self, data: Mapping[str, Any]) -> None:
        usage, usage_key_seen = _extract_usage(data)
        if data.get(_PARENT_USAGE_COVERS_KEY):
            # An explicit absorption decision, not a silent drop: the parent
            # terminal's snapshot for THIS provider session already measures
            # this same-session breakdown entry, so counting it again would
            # double-bill the session.
            label = (
                _first_text(data, ("usage_event_id",))
                or _first_text(data, ("event_id", "request_id", "uuid", "id"))
                or "breakdown entry"
            )
            self._diagnostics.append(
                f"breakdown usage already covered by the parent terminal's "
                f"same-session snapshot: {label}"
            )
            return
        # The parent terminal's retained total already bills this breakdown
        # entry; its identity/fingerprint below still uses the reported cost so
        # two same-token children with different costs stay distinct events.
        cost_covered_by_parent = (
            bool(data.get(_PARENT_COST_COVERS_KEY)) and _cost_key_present(data)
        )
        if cost_covered_by_parent and not usage_key_seen:
            # A cost-only breakdown entry has nothing left to contribute once
            # the parent total covers it — and it is not a missing measurement,
            # so it must not register an identity or mark the record partial.
            return
        provider = _provider_name(
            _first_text(data, ("provider", "provider_name")) or self.provider,
            usage,
        )
        session_id = _first_text(
            data, ("provider_session_id", "session_id", "thread_id")
        ) or self.provider_session_id
        # The declared cost semantics decide WHICH cost value is the measured
        # one (a per-result delta vs. the session total), so semantics must be
        # resolved before extraction — extracting first and declaring second
        # lets a delta ``cost_usd`` sit beside a nested ``total_cost_usd`` and
        # be mislabeled as the cumulative snapshot.
        cost_semantics = _declared_cost_semantics(
            data,
            self.default_cost_semantics,
            provider,
            session_id,
        )
        prefer_total = cost_semantics == CostSemantics.PROVIDER_SESSION_CUMULATIVE
        cost, cost_key_seen, invalid_cost = _extract_cost(
            data, prefer_total=prefer_total
        )
        scope_mismatch = _usage_cost_scope_mismatch(data, prefer_total)
        model = _first_text(data, ("model", "model_name")) or self.reported_model
        usage_event_id = _first_text(data, ("usage_event_id",))
        # ``uuid`` is what the Claude CLI actually stamps on each stream-json
        # event; without it a real result event has no identity at all and
        # every replay falls through to the synthetic content hash.
        container_id = _first_text(
            data, ("event_id", "request_id", "response_id", "uuid", "id")
        )
        if container_id is None:
            message = _nested_mapping(data, "message")
            container_id = _optional_text(message.get("id"))

        duplicate_explicit_id: Optional[str] = None
        if usage_event_id is not None:
            # The provider's per-measurement identity: re-emissions of the
            # same event (JSON Phase 2 replay, proxy echo) count exactly once.
            # A byte-identical replay is dropped; a re-emission carrying
            # DIFFERENT values under provider-session-cumulative semantics is
            # the newer complete snapshot and replaces the earlier one — that
            # decision is made after the new values are normalized, below.
            event_key = usage_event_id
            explicit_event_id: Optional[str] = usage_event_id
            if event_key in self._seen_event_keys:
                duplicate_explicit_id = event_key
            else:
                self._seen_event_keys.add(event_key)
        elif container_id is not None:
            # Container ids (request/response/message) are shared by several
            # terminal events of one exchange, not per-measurement identity:
            # distinct events under the same container must all contribute.
            # Only a byte-identical replay under the same container id is
            # dropped, and that forced drop must surface as partial — never
            # as a silently complete report.
            explicit_event_id = None
            fingerprint = _terminal_fingerprint(data, usage, cost)
            previous = self._container_fingerprints.get(container_id)
            if previous == fingerprint:
                self._diagnostics.append(
                    f"duplicate terminal event ignored on container id "
                    f"collision: {container_id}"
                )
                if usage_key_seen or cost_key_seen:
                    self._partial = True
                    self._diagnostics.append(
                        f"usage-bearing event dropped on container id "
                        f"collision; record marked partial: {container_id}"
                    )
                return
            self._container_fingerprints[container_id] = fingerprint
            event_key = container_id
            suffix = 1
            while event_key in self._seen_event_keys:
                suffix += 1
                event_key = f"{container_id}#{suffix}"
            self._seen_event_keys.add(event_key)
        else:
            # No identity of any kind: the content-derived key is
            # attempt-local and a collision on it cannot distinguish a true
            # replay from a distinct measurement — so a usage-bearing drop is
            # marked partial instead of silently losing tokens.
            explicit_event_id = None
            event_key = _stable_event_key(data, usage, cost)
            if event_key in self._seen_event_keys:
                self._diagnostics.append(f"duplicate usage event ignored: {event_key}")
                if usage_key_seen or cost_key_seen:
                    self._partial = True
                    self._diagnostics.append(
                        f"usage-bearing event dropped on attempt-local key "
                        f"collision; record marked partial: {event_key}"
                    )
                return
            self._seen_event_keys.add(event_key)
            self._diagnostics.append(
                f"usage event missing id; attempt-local key {event_key} used"
            )

        if cost_covered_by_parent:
            # Identity is settled above (fingerprints saw the reported cost);
            # from here the entry contributes tokens only.
            cost = None
            cost_key_seen = False
            invalid_cost = False
            self._diagnostics.append(
                f"breakdown cost already covered by parent terminal total: {event_key}"
            )

        partial = invalid_cost
        if scope_mismatch is not None:
            # Tokens and cost that provably describe different scopes must not
            # read as one complete measurement.
            partial = True
            self._diagnostics.append(f"{scope_mismatch} in {event_key}")
        # An emitter that already converted a cumulative snapshot into a delta
        # (e.g. the interactive transcript watcher) declares an anomaly here —
        # a regressed snapshot must not pass as a legal zero delta.
        if data.get("partial") is True:
            partial = True
        for diagnostic in data.get("diagnostics") or []:
            if isinstance(diagnostic, str) and diagnostic:
                self._diagnostics.append(diagnostic)
        if usage_key_seen and usage is None:
            partial = True
            self._diagnostics.append(f"malformed usage payload in {event_key}")
        measured_tokens = (
            usage_key_seen
            and usage is not None
            and _usage_mapping_has_tokens(usage)
        )
        if usage is not None:
            tokens, invalid_tokens = _normalize_tokens(usage, provider)
            partial = partial or invalid_tokens
            if invalid_tokens:
                self._diagnostics.append(
                    f"invalid or inconsistent token payload in {event_key}"
                )
            if not measured_tokens:
                # A ``usage`` object declaring no recognized token field is
                # missing data wearing a measurement's clothes — the same
                # judgment ``_usage_mapping_has_tokens`` already applies to a
                # degraded re-emission. Reporting it as an explicit zero would
                # make an unmeasured call indistinguishable from a real
                # zero-consumption one on every downstream surface.
                partial = True
                self._diagnostics.append(
                    f"usage payload declares no recognized token field in "
                    f"{event_key}; not an explicit zero"
                )
        else:
            tokens = (0, 0, 0, 0, 0, 0, 0)
        usage_semantics = _declared_usage_semantics(
            data, self.default_usage_semantics
        )
        replace_index: Optional[int] = None
        if duplicate_explicit_id is not None:
            previous = _explicit_event(self._events, duplicate_explicit_id)
            new_measured = cost_key_seen or measured_tokens
            if previous is not None and not new_measured:
                # A re-emission carrying no measurement must not erase the
                # measured snapshot; keep the earlier values.
                self._diagnostics.append(
                    f"duplicate usage event ignored: {duplicate_explicit_id}"
                )
                return
            if previous is not None:
                if (
                    previous.usage_seen == measured_tokens
                    and previous.token_tuple() == tuple(tokens)
                    and previous.actual_cost_usd == cost
                ):
                    self._diagnostics.append(
                        f"duplicate usage event ignored: {duplicate_explicit_id}"
                    )
                    return
                # INVARIANT: a cumulative snapshot never shrinks. A re-emission
                # of the same event id reporting LESS than the value already
                # retained contradicts cumulative semantics, so the trusted
                # value is kept — replacing it would let the regressed figure
                # become the record's total before the per-scope monotonic
                # guards in ``_aggregate_tokens``/``_aggregate_cost`` (which
                # only see one entry per event id) could ever reject it.
                regressed = []
                if (
                    measured_tokens
                    and previous.usage_seen
                    and previous.usage_semantics
                    == UsageSemantics.PROVIDER_SESSION_CUMULATIVE
                    and usage_semantics
                    == UsageSemantics.PROVIDER_SESSION_CUMULATIVE
                    and any(
                        new < old
                        for new, old in zip(tokens, previous.token_tuple())
                    )
                ):
                    regressed.append("token")
                if (
                    cost is not None
                    and previous.actual_cost_usd is not None
                    and previous.cost_semantics
                    == CostSemantics.PROVIDER_SESSION_CUMULATIVE
                    and cost_semantics
                    == CostSemantics.PROVIDER_SESSION_CUMULATIVE
                    and cost < previous.actual_cost_usd
                ):
                    regressed.append("cost")
                if regressed:
                    self._partial = True
                    self._diagnostics.append(
                        f"non-monotonic re-emission of usage event "
                        f"{duplicate_explicit_id} ("
                        + "/".join(regressed)
                        + "); keeping the trusted earlier snapshot"
                    )
                    return
                # Replacing in place (not remove+append) keeps the first-seen
                # event order the record's ``usage_event_ids`` exposes; the new
                # entry still carries its own ``arrival_index`` so absorption
                # judges it at its real arrival.
                replace_index = self._events.index(previous)
        if cost_key_seen and cost is None and data.get("total_cost_usd") is not None:
            partial = True
        if cost_key_seen and not usage_key_seen:
            partial = True
            self._diagnostics.append(f"cost reported without token usage in {event_key}")
        if not usage_key_seen and not cost_key_seen:
            # A terminal event reporting neither usage nor cost is a missing
            # measurement, not an empty delta: when other terminal events of
            # the same call DID report usage, the record must read partial
            # (with this diagnostic) rather than silently available.
            self._missing_usage_terminals = True
            self._diagnostics.append(
                f"terminal event reported neither usage nor cost: {event_key}"
            )
            return

        self._arrival_counter += 1
        normalized = _NormalizedEvent(
            event_id=event_key,
            explicit_event_id=explicit_event_id,
            provider=provider,
            provider_session_id=session_id,
            reported_model=model,
            usage_seen=usage_key_seen and usage is not None,
            logical_input_tokens=tokens[0],
            uncached_input_tokens=tokens[1],
            output_tokens=tokens[2],
            cache_read_input_tokens=tokens[3],
            cache_creation_input_tokens=tokens[4],
            cache_creation_5m_input_tokens=tokens[5],
            cache_creation_1h_input_tokens=tokens[6],
            actual_cost_usd=cost,
            usage_semantics=usage_semantics,
            cost_semantics=cost_semantics,
            partial=partial,
            arrival_index=self._arrival_counter,
        )
        if replace_index is not None:
            superseded = self._events[replace_index]
            self._events[replace_index] = normalized
            # WHY only a NON-monotonic re-emission degrades the record: the
            # same usage_event_id names the same measurement, so a re-report
            # whose every category grew is the fuller report of that one
            # measurement and the replacement loses nothing. Anything else —
            # a shrinking value, or a re-emission that stops reporting a
            # surface the earlier one measured — drops data the record can no
            # longer account for and must not pass as complete.
            grown = (
                superseded.usage_seen == normalized.usage_seen
                and all(
                    new >= old
                    for new, old in zip(
                        normalized.token_tuple(), superseded.token_tuple()
                    )
                )
                and (superseded.actual_cost_usd is None) == (cost is None)
                and (
                    cost is None
                    or superseded.actual_cost_usd is None
                    or cost >= superseded.actual_cost_usd
                )
            )
            if not grown:
                self._partial = True
            self._diagnostics.append(
                f"usage event {duplicate_explicit_id} re-emitted with "
                "different values; later snapshot replaces the earlier one"
            )
        else:
            self._events.append(normalized)
        self._partial = self._partial or partial

    @staticmethod
    def _scope_key(event: _NormalizedEvent, call_id: str) -> Tuple[str, str]:
        """The (provider, scope) key events and snapshots share.

        Session-less events and snapshots key on the CALL id — the same scope
        a session-less cumulative snapshot is stored under — so a
        session-less delta can be absorbed by a later same-call snapshot
        exactly like a sessioned delta by its session snapshot.
        """
        return (event.provider or "unknown", event.provider_session_id or call_id)

    def _arrival_ordered(self) -> List[Tuple[int, _NormalizedEvent]]:
        """``(list_position, event)`` pairs in true arrival order.

        WHY: a re-emitted event id keeps its first-seen LIST slot (that order is
        exposed as ``usage_event_ids``), so list order is not arrival order.
        Absorption is a claim about time — a snapshot only provably contains
        deltas that arrived before it — and must be judged on arrival. The list
        position rides along because ``to_record`` cross-references the
        absorbed/decision sets against ``enumerate(self._events)``.
        """
        return sorted(
            enumerate(self._events), key=lambda item: item[1].arrival_index
        )

    def _aggregate_tokens(self) -> Tuple[Tuple[int, ...], set, set]:
        total = [0] * 7
        cumulative: Dict[Tuple[str, str], _NormalizedEvent] = {}
        cumulative_arrival: Dict[Tuple[str, str], int] = {}
        absorbed: set = set()
        decisions: set = set()
        # First pass: resolve each scope's trusted cumulative snapshot (with
        # its arrival) BEFORE any delta is judged, so a delta early in the
        # call can be absorbed by a snapshot that arrives LATER in the same
        # call.
        for position, event in self._arrival_ordered():
            if not event.usage_seen:
                continue
            if event.usage_semantics != UsageSemantics.PROVIDER_SESSION_CUMULATIVE:
                continue
            key = self._scope_key(event, self.call_id)
            previous = cumulative.get(key)
            if previous is not None and any(
                current < old
                for current, old in zip(event.token_tuple(), previous.token_tuple())
            ):
                # A regression snapshot contradicts cumulative semantics;
                # keep the latest valid monotonic snapshot so the retained
                # token totals and the retained cost (which mirrors this
                # rule) describe the same trusted snapshot.
                self._partial = True
                self._diagnostics.append(
                    "non-monotonic cumulative token snapshot for "
                    + key[1]
                    + "; keeping trusted value"
                )
            else:
                cumulative[key] = event
                cumulative_arrival[key] = event.arrival_index
        for position, event in self._arrival_ordered():
            if not event.usage_seen:
                continue
            if event.usage_semantics == UsageSemantics.PROVIDER_SESSION_CUMULATIVE:
                continue
            key = self._scope_key(event, self.call_id)
            snapshot = cumulative.get(key)
            snapshot_arrival = cumulative_arrival.get(key)
            if snapshot_arrival is not None and snapshot_arrival > event.arrival_index:
                # A later same-scope snapshot exists, so an absorption
                # DECISION is made for this delta (absorb vs. bill by
                # magnitude). Positions without one are never compared
                # against the cost side.
                decisions.add(position)
                if snapshot is not None and all(
                    snapshot_token >= delta_token
                    for snapshot_token, delta_token in zip(
                        snapshot.token_tuple(), event.token_tuple()
                    )
                ):
                    # The delta is inside a later same-scope snapshot whose
                    # magnitude covers every token category: the snapshot
                    # absorbs it instead of billing it twice.
                    absorbed.add(position)
                    self._diagnostics.append(
                        f"delta token event covered by a later provider "
                        f"session snapshot for {key[1]}"
                    )
                    continue
            for index, value in enumerate(event.token_tuple()):
                total[index] += value
        for event in cumulative.values():
            for index, value in enumerate(event.token_tuple()):
                total[index] += value
        return tuple(total), absorbed, decisions

    def _aggregate_cost(
        self,
    ) -> Tuple[float, Dict[Tuple[str, str], float], bool, set, set]:
        """Return ``(delta_total, cumulative_map, any_cost, absorbed, decisions)``.

        The cumulative map is keyed by ``(provider, session_id_or_call_id)`` so
        :meth:`to_record` can retain each provider session's billing identity
        in the record's cost breakdown instead of collapsing them into one
        call-level sum. A delta event is absorbed into the scope's cumulative
        snapshot only when a later same-scope snapshot provably contains it
        (mirrors the flow-level rule) — a blind sum would double-count the
        delta that is already inside the snapshot.
        """
        delta_total = 0.0
        any_cost = False
        cumulative: Dict[Tuple[str, str], float] = {}
        cumulative_arrival: Dict[Tuple[str, str], int] = {}
        absorbed: set = set()
        decisions: set = set()
        # First pass: resolve each scope's trusted cumulative snapshot (with
        # its arrival) BEFORE any delta is judged, so a delta early in the
        # call can be absorbed by a snapshot that arrives LATER in the same
        # call.
        for position, event in self._arrival_ordered():
            if event.actual_cost_usd is None:
                continue
            if event.cost_semantics != CostSemantics.PROVIDER_SESSION_CUMULATIVE:
                continue
            key = self._scope_key(event, self.call_id)
            previous = cumulative.get(key)
            if previous is not None and event.actual_cost_usd < previous:
                # A regression snapshot contradicts cumulative semantics;
                # keep the latest valid monotonic snapshot — the lower
                # value must not overwrite a trusted total.
                self._partial = True
                self._diagnostics.append(
                    "non-monotonic cumulative cost snapshot for "
                    + key[1]
                    + "; keeping trusted value"
                )
            else:
                cumulative[key] = event.actual_cost_usd
                cumulative_arrival[key] = event.arrival_index
        for position, event in self._arrival_ordered():
            if event.actual_cost_usd is None:
                continue
            any_cost = True
            if event.cost_semantics == CostSemantics.PROVIDER_SESSION_CUMULATIVE:
                continue
            key = self._scope_key(event, self.call_id)
            snapshot_arrival = cumulative_arrival.get(key)
            if snapshot_arrival is not None and snapshot_arrival > event.arrival_index:
                # A later same-scope snapshot exists: the delta's absorption
                # is DECIDED here (absorb vs. bill by magnitude), so the
                # outcome is comparable with the token side's.
                decisions.add(position)
                if cumulative[key] >= event.actual_cost_usd:
                    # The delta is inside a later same-scope snapshot: the
                    # snapshot absorbs it instead of billing it twice.
                    absorbed.add(position)
                    self._diagnostics.append(
                        f"delta cost event covered by a later provider "
                        f"session snapshot for {key[1]}"
                    )
                    continue
            delta_total += event.actual_cost_usd
        return delta_total, cumulative, any_cost, absorbed, decisions

    def to_record(self) -> UsageRecord:
        tokens, token_absorbed, token_decisions = self._aggregate_tokens()
        delta_cost, cumulative, any_cost, cost_absorbed, cost_decisions = (
            self._aggregate_cost()
        )
        # INVARIANT: tokens and cost apply ONE absorption rule. When both
        # sides faced an absorption candidate (a later same-scope cumulative
        # snapshot) and their verdicts disagree for the same measured event —
        # its tokens absorbed while its cost bills, or vice versa — the
        # record's two columns no longer describe the same consumption and
        # must read partial with a diagnostic. A side WITHOUT a candidate
        # made no absorption decision at all, so its plain delta billing is
        # not a disagreement to flag.
        for position, event in enumerate(self._events):
            if not event.usage_seen or event.actual_cost_usd is None:
                continue
            if position not in token_decisions or position not in cost_decisions:
                continue
            if (position in token_absorbed) != (position in cost_absorbed):
                self._partial = True
                self._diagnostics.append(
                    f"token and cost absorption disagree for usage event "
                    f"{event.event_id}: one side was absorbed into a later "
                    "snapshot while the other was billed; marked partial"
                )
        cost_breakdown: List[SessionCostSnapshot] = []
        # Session-less cumulative events keyed on the call id have no billing
        # identity to retain; they stay part of the call-level cost like a
        # delta. Only real provider sessions enter the breakdown.
        for (provider, session), value in cumulative.items():
            if session == self.call_id:
                continue
            cost_breakdown.append(
                SessionCostSnapshot(
                    provider=None if provider == "unknown" else provider,
                    provider_session_id=session,
                    actual_cost_usd=value,
                )
            )
        actual_cost = (delta_cost + sum(cumulative.values())) if any_cost else None
        # INVARIANT: the record's cost must always equal its breakdown
        # snapshots plus the unabsorbed remainder (delta events and
        # session-less cumulative snapshots). A record that contradicts itself
        # must read partial with a diagnostic instead of exposing two
        # disagreeing costs to downstream surfaces.
        breakdown_total = sum(
            snapshot.actual_cost_usd for snapshot in cost_breakdown
        )
        session_less_cumulative = sum(
            value
            for (_, session), value in cumulative.items()
            if session == self.call_id
        )
        remainder = delta_cost + session_less_cumulative
        if actual_cost is not None and not math.isclose(
            actual_cost,
            breakdown_total + remainder,
            rel_tol=1e-9,
            abs_tol=1e-12,
        ):
            self._partial = True
            self._diagnostics.append(
                "record cost inconsistent with its cost_breakdown; "
                "marked partial"
            )
        usage_seen = any(event.usage_seen for event in self._events)
        if not usage_seen and actual_cost is None:
            # Nothing was measured at all: UNAVAILABLE, unless a malformed
            # payload (not a missing one) already demanded partial.
            status = UsageStatus.PARTIAL if self._partial else UsageStatus.UNAVAILABLE
        elif self._partial or not usage_seen or self._missing_usage_terminals:
            status = UsageStatus.PARTIAL
        else:
            status = UsageStatus.AVAILABLE

        providers = list(
            dict.fromkeys(event.provider for event in self._events if event.provider)
        )
        sessions = list(
            dict.fromkeys(
                event.provider_session_id
                for event in self._events
                if event.provider_session_id
            )
        )
        models = [
            event.reported_model for event in self._events if event.reported_model
        ]
        usage_semantics = list(
            dict.fromkeys(event.usage_semantics for event in self._events)
        )
        cost_semantics = list(
            dict.fromkeys(event.cost_semantics for event in self._events)
        )
        event_ids = [event.event_id for event in self._events]
        explicit_ids = [
            event.explicit_event_id
            for event in self._events
            if event.explicit_event_id is not None
        ]
        reported_model = models[-1] if models else self.reported_model
        resolved_model, resolved_model_source = resolve_model_identity(
            reported_model=reported_model,
            configured_model=self.configured_model,
            runner_startup_model=self.runner_startup_model,
            legacy_resolved_model=self.resolved_model,
        )
        return UsageRecord(
            call_id=self.call_id,
            attempt=self.attempt,
            usage_status=status,
            agent_name=self.agent_name,
            runner_type=self.runner_type,
            provider=(
                providers[0]
                if len(providers) == 1
                else ("mixed" if len(providers) > 1 else self.provider)
            ),
            provider_session_id=(
                sessions[0]
                if len(sessions) == 1
                else (None if len(sessions) > 1 else self.provider_session_id)
            ),
            usage_event_id=explicit_ids[0] if len(explicit_ids) == 1 else None,
            reported_model=reported_model,
            configured_model=self.configured_model,
            runner_startup_model=self.runner_startup_model,
            resolved_model=resolved_model,
            resolved_model_source=resolved_model_source,
            logical_input_tokens=tokens[0],
            uncached_input_tokens=tokens[1],
            output_tokens=tokens[2],
            cache_read_input_tokens=tokens[3],
            cache_creation_input_tokens=tokens[4],
            cache_creation_5m_input_tokens=tokens[5],
            cache_creation_1h_input_tokens=tokens[6],
            actual_cost_usd=actual_cost,
            usage_semantics=(
                usage_semantics[0]
                if len(usage_semantics) == 1
                else (
                    UsageSemantics.MIXED
                    if usage_semantics
                    else self.default_usage_semantics
                )
            ),
            cost_semantics=(
                cost_semantics[0]
                if len(cost_semantics) == 1
                else (
                    CostSemantics.MIXED
                    if cost_semantics
                    else self.default_cost_semantics
                )
            ),
            usage_event_ids=event_ids,
            provider_session_ids=sessions,
            cost_breakdown=cost_breakdown,
            diagnostics=list(dict.fromkeys(self._diagnostics)),
        )


def parse_usage_record(
    raw: Union[
        str,
        Mapping[str, Any],
        Sequence[Mapping[str, Any]],
        Iterable[Mapping[str, Any]],
        None,
    ],
    **metadata: Any,
) -> UsageRecord:
    """Parse every terminal usage event into one call/attempt record."""
    aggregator = UsageEventAggregator(**metadata)
    for event in parse_ndjson_events(raw):
        aggregator.add_event(event)
    return aggregator.to_record()


#: The call id :func:`legacy_usage_record` stamps when the caller supplied no
#: attribution. It identifies nothing — every record-less legacy tally adapted
#: without an explicit call id shares it — so it must never act as an identity.
LEGACY_UNKNOWN_CALL_ID = "legacy-unknown"


def _has_stable_call_identity(record: UsageRecord) -> bool:
    """Whether a record's call/attempt pair is a real source attribution."""
    call_id = (record.call_id or "").strip()
    return bool(call_id) and call_id != LEGACY_UNKNOWN_CALL_ID


def deduplicate_usage_records(
    records: Iterable[UsageRecord],
) -> Tuple[List[UsageRecord], List[str]]:
    """Drop duplicate records keyed by usage-event identity or call/attempt.

    The same record can reach an aggregation twice (a step accumulator that
    also carried records forward, a history replay plus a live tracker), and
    double-counting a cumulative snapshot would corrupt both tokens and cost.

    Event identity de-duplicates per *event*, not per record: within one
    (provider, session) scope, records whose event-id sets overlap are
    overlapping measurements of the same stream, so all but one collapse to
    guarantee every usage event contributes exactly once. A record whose
    event set is contained in another's is superseded — the aggregate already
    carries the partial record's contribution. Intersecting-but-uncontained
    records are merged down to a single representative as well; when the
    dropped records measured events the representative does not carry, that
    contribution is unrecoverable from aggregated records, so the kept record
    is marked partial with a diagnostic rather than reading as a complete
    report (a blind sum would double-count the shared events instead).

    Content-derived ``synthetic:`` fallback keys are attempt-local (they are
    only produced when a payload carries no real usage_event_id/message.id):
    two distinct attempts with byte-identical id-less payloads must not
    collapse into one record, so both the overlap and identity scopes include
    the call/attempt for records keyed only by fallback keys.
    """
    records = list(records)
    scoped: Dict[Tuple[Any, ...], List[int]] = {}
    indexed: List[Tuple[UsageRecord, Optional[frozenset[str]], bool]] = []
    for record in records:
        event_ids = (
            frozenset(record.usage_event_ids)
            if record.usage_event_ids
            else (
                frozenset((record.usage_event_id,))
                if record.usage_event_id
                else None
            )
        )
        attempt_local = bool(event_ids) and all(
            isinstance(event_id, str) and event_id.startswith("synthetic:")
            for event_id in event_ids
        )
        indexed.append((record, event_ids, attempt_local))
        if event_ids is not None:
            scope = (record.provider, record.provider_session_id)
            if attempt_local:
                scope += (record.call_id, record.attempt)
            scoped.setdefault(scope, []).append(len(indexed) - 1)

    dropped: set[int] = set()
    for entries in scoped.values():
        if len(entries) < 2:
            continue
        # Union-find over shared event ids: records reporting the same usage
        # event are one connected component of overlapping measurements.
        parent = list(range(len(entries)))

        def find(pos: int) -> int:
            while parent[pos] != pos:
                parent[pos] = parent[parent[pos]]
                pos = parent[pos]
            return pos

        def union(first: int, second: int) -> None:
            root_first, root_second = find(first), find(second)
            if root_first != root_second:
                parent[root_second] = root_first

        owner: Dict[str, int] = {}
        for pos, index in enumerate(entries):
            ids = indexed[index][1]
            assert ids is not None
            for event_id in ids:
                if event_id in owner:
                    union(pos, owner[event_id])
                else:
                    owner[event_id] = pos

        components: Dict[int, List[int]] = {}
        for pos in range(len(entries)):
            components.setdefault(find(pos), []).append(pos)

        for members in components.values():
            if len(members) < 2:
                continue

            def rank(pos: int) -> Tuple[bool, int, int]:
                record = indexed[entries[pos]][0]
                ids = indexed[entries[pos]][1]
                assert ids is not None
                # Prefer a record carrying real usage, then the most complete
                # event set, then the earliest-seen: an UNAVAILABLE aggregate
                # must not supersede a usage-bearing partial.
                return (
                    record.usage_status != UsageStatus.UNAVAILABLE,
                    len(ids),
                    -pos,
                )

            representative = max(members, key=rank)
            rep_index = entries[representative]
            rep_record = indexed[rep_index][0]
            rep_ids = indexed[rep_index][1]
            assert rep_ids is not None
            lost_events = False
            for pos in members:
                if pos == representative:
                    continue
                index = entries[pos]
                record, ids, _ = indexed[index]
                assert ids is not None
                if ids - rep_ids and record.usage_status != UsageStatus.UNAVAILABLE:
                    # The dropped record measured events the representative
                    # does not carry; their tokens and cost are gone from the
                    # merged result and must not pass for a full report.
                    lost_events = True
                dropped.add(index)
            if lost_events:
                # WHY: dedup is re-run on every summary render over the same
                # persisted ledger, so the downgrade and diagnostic go onto a
                # copy — mutating the caller's record would make the PARTIAL
                # status sticky and grow its diagnostics list without bound.
                merged = replace(
                    rep_record,
                    usage_status=(
                        UsageStatus.PARTIAL
                        if rep_record.usage_status == UsageStatus.AVAILABLE
                        else rep_record.usage_status
                    ),
                    diagnostics=list(rep_record.diagnostics)
                    + [
                        "overlapping usage records merged per event; dropped "
                        "record(s) measured events outside the kept record"
                    ],
                )
                indexed[rep_index] = (merged, rep_ids, indexed[rep_index][2])

    unique: List[UsageRecord] = []
    seen: Dict[Tuple[Any, ...], int] = {}
    diagnostics: List[str] = []
    unattributed: List[int] = []

    def richer_than(record: UsageRecord, kept: UsageRecord) -> bool:
        return (
            kept.usage_status == UsageStatus.UNAVAILABLE
            and record.usage_status != UsageStatus.UNAVAILABLE
        )

    for index, (record, event_ids, attempt_local) in enumerate(indexed):
        if event_ids is not None:
            if index in dropped:
                diagnostics.append(
                    f"usage record ignored: {record.call_id}/{record.attempt} "
                    "events overlap another record for the same provider session"
                )
                continue
            identity = (
                "events",
                record.provider,
                record.provider_session_id,
                tuple(sorted(event_ids)),
            )
            if attempt_local:
                identity += (record.call_id, record.attempt)
        else:
            if not _has_stable_call_identity(record):
                # WHY these are never collapsed against each other: a
                # record-less legacy tally carries no identity at all — the
                # placeholder call id is a formatting default, not a source.
                # Two distinct tallies that merely share it are two distinct
                # sources whose arithmetic fold must stay additive; collapsing
                # them by the shared placeholder silently deletes reported
                # usage. Non-duplication for such sources is guaranteed by the
                # aggregation topology (each source folded once per level),
                # never by matching values.
                unattributed.append(len(unique))
                unique.append(record)
                continue
            identity = ("call", record.call_id, record.attempt)
        kept_index = seen.get(identity)
        if kept_index is not None:
            # Equal identity: keep the copy carrying real usage when a replay
            # produced an UNAVAILABLE twin of the same events.
            if richer_than(record, unique[kept_index]):
                unique[kept_index] = record
                diagnostics.append(
                    f"usage record replaced with richer copy: "
                    f"{record.call_id}/{record.attempt}"
                )
            else:
                diagnostics.append(
                    f"duplicate usage record ignored: "
                    f"{record.call_id}/{record.attempt}"
                )
            continue
        seen[identity] = len(unique)
        unique.append(record)
    if len(unattributed) > 1:
        # Several identity-less sources reached one aggregation: they are all
        # counted as reported (never dropped), but whether they are genuinely
        # distinct sources or one source folded twice is undecidable here, so
        # the ambiguity is surfaced instead of hidden behind a complete status.
        note = (
            "multiple usage records carry no stable identity; counted as "
            "reported because source attribution is undecidable"
        )
        diagnostics.append(note)
        for position in unattributed:
            record = unique[position]
            unique[position] = replace(
                record,
                usage_status=(
                    UsageStatus.PARTIAL
                    if record.usage_status == UsageStatus.AVAILABLE
                    else record.usage_status
                ),
                diagnostics=list(record.diagnostics) + [note],
            )
    return unique, diagnostics


def _record_session_scope(record: UsageRecord) -> Tuple[str, str]:
    """The (provider, scope) billing scope a delta-cost record belongs to.

    Session-less records key on their CALL id — the same scope a session-less
    cumulative snapshot is stored under — so a later same-call snapshot can
    provably absorb them exactly like a session snapshot absorbs its deltas.
    """
    return (
        record.provider or "unknown",
        record.provider_session_id or record.call_id,
    )


def _record_token_tuple(record: UsageRecord) -> Tuple[int, ...]:
    """The seven token categories of a record, in canonical order."""
    return (
        record.logical_input_tokens,
        record.uncached_input_tokens,
        record.output_tokens,
        record.cache_read_input_tokens,
        record.cache_creation_input_tokens,
        record.cache_creation_5m_input_tokens,
        record.cache_creation_1h_input_tokens,
    )


def _cost_trusted_snapshots(
    records: Sequence[UsageRecord],
) -> Tuple[Dict[Tuple[str, str], Tuple[int, float]], List[Tuple[str, str]]]:
    """Latest valid cumulative cost snapshot per (provider, session) scope.

    Returns the trusted ``(position, magnitude)`` per scope plus the keys of
    rejected regression snapshots. Position and magnitude together are the
    provable-containment evidence for delta absorption: a delta is absorbed
    only when the scope's trusted snapshot is NOT EARLIER than the delta and
    its magnitude covers it — "a snapshot exists" alone is not proof, or a
    delta emitted after the snapshot (or larger than it) would be silently
    dropped. Breakdown snapshots and whole-record cumulative reports feed the
    same scope, so :func:`aggregate_usage_records` and
    :func:`_build_billing_units` apply one rule.
    """
    trusted: Dict[Tuple[str, str], Tuple[int, float]] = {}
    rejected: List[Tuple[str, str]] = []
    for position, record in enumerate(records):
        snapshots: List[Tuple[Optional[str], str, float]] = [
            (
                snapshot.provider,
                snapshot.provider_session_id,
                snapshot.actual_cost_usd,
            )
            for snapshot in record.cost_breakdown
        ]
        if not record.cost_breakdown and (
            record.cost_semantics == CostSemantics.PROVIDER_SESSION_CUMULATIVE
            and record.actual_cost_usd is not None
        ):
            # Session-less cumulative reports key on the call id: each call
            # keeps its own snapshot instead of two calls collapsing into one
            # scope and one of them being lost.
            snapshots.append(
                (
                    record.provider,
                    record.provider_session_id or record.call_id,
                    record.actual_cost_usd,
                )
            )
        for provider, session, magnitude in snapshots:
            key = (provider or "unknown", session)
            previous = trusted.get(key)
            if previous is not None and magnitude < previous[1]:
                # A regression snapshot contradicts cumulative semantics;
                # keep the trusted value AND its earlier position — the
                # rejected snapshot contributes no containment evidence.
                rejected.append(key)
            else:
                trusted[key] = (position, magnitude)
    return trusted, rejected


def aggregate_usage_records(
    records: Iterable[UsageRecord], *, call_id: str = "aggregate"
) -> UsageRecord:
    """Aggregate records with call/event and billing-session de-duplication."""
    unique, diagnostics = deduplicate_usage_records(records)
    partial = False

    # Trusted cumulative snapshots per scope, resolved up front so tokens and
    # cost apply ONE absorption rule: a delta (token or cost) is absorbed only
    # when the scope's latest valid snapshot is later than the delta AND its
    # magnitude covers it — otherwise the delta counts per delta semantics. A
    # later, larger snapshot proves the delta is inside it; an earlier or
    # smaller one proves nothing and must not drop reported usage.
    cost_trusted, cost_regressions = _cost_trusted_snapshots(unique)
    for key in cost_regressions:
        partial = True
        diagnostics.append(
            "non-monotonic cumulative cost record for "
            + key[1]
            + "; keeping trusted value"
        )
    token_trusted: Dict[Tuple[str, str], Tuple[int, UsageRecord]] = {}
    for position, record in enumerate(unique):
        if (
            record.usage_status == UsageStatus.UNAVAILABLE
            or record.usage_semantics != UsageSemantics.PROVIDER_SESSION_CUMULATIVE
        ):
            continue
        key = (
            record.provider or "unknown",
            record.provider_session_id or record.call_id,
        )
        previous = token_trusted.get(key)
        if previous is None:
            token_trusted[key] = (position, record)
            continue
        if any(
            new < old
            for new, old in zip(_record_token_tuple(record), _record_token_tuple(previous[1]))
        ):
            # A regression snapshot contradicts cumulative semantics; keep
            # the trusted monotonic totals and their earlier position.
            partial = True
            diagnostics.append(
                "non-monotonic cumulative token record for "
                + key[1]
                + "; keeping trusted value"
            )
        else:
            token_trusted[key] = (position, record)

    token_totals = [0] * 7
    cost_total = 0.0
    any_cost = False
    token_absorbed_positions: set = set()
    token_decision_positions: set = set()
    cost_absorbed_positions: set = set()
    cost_decision_positions: set = set()
    for position, record in enumerate(unique):
        diagnostics.extend(record.diagnostics)
        partial = partial or record.usage_status in (
            UsageStatus.PARTIAL,
            UsageStatus.LEGACY_AMBIGUOUS,
        )
        if record.usage_status != UsageStatus.UNAVAILABLE:
            if record.usage_semantics != UsageSemantics.PROVIDER_SESSION_CUMULATIVE:
                absorbed = False
                key = _record_session_scope(record)
                trusted = token_trusted.get(key)
                if trusted is not None and trusted[0] > position:
                    # A later same-scope snapshot exists: an absorption
                    # decision is made for this record's tokens.
                    token_decision_positions.add(position)
                    if all(
                        snapshot_token >= delta_token
                        for snapshot_token, delta_token in zip(
                            _record_token_tuple(trusted[1]),
                            _record_token_tuple(record),
                        )
                    ):
                        absorbed = True
                        token_absorbed_positions.add(position)
                        diagnostics.append(
                            "delta tokens of "
                            + record.call_id
                            + " covered by a provider session snapshot"
                        )
                if not absorbed:
                    for index, value in enumerate(_record_token_tuple(record)):
                        token_totals[index] += value
            # Cumulative token records were folded into ``token_trusted``.

        if record.cost_breakdown:
            any_cost = True
            # Decompose the call's mixed cost the same way the billing units
            # do: each session snapshot keeps its identity (latest valid
            # monotonic snapshot per session, resolved above), only the
            # remainder adds as a call-level delta.
            covered = sum(
                snapshot.actual_cost_usd for snapshot in record.cost_breakdown
            )
            remainder = (record.actual_cost_usd or 0.0) - covered
            if remainder > 1e-9:
                scope = _record_session_scope(record)
                trusted = cost_trusted.get(scope) if scope is not None else None
                if trusted is not None and trusted[0] > position:
                    cost_decision_positions.add(position)
                    if trusted[1] >= remainder:
                        # The remainder is inside a later same-session snapshot
                        # already counted — billing it here would bill it
                        # twice. A record's own snapshot sits at its own
                        # position and therefore never covers its own delta.
                        cost_absorbed_positions.add(position)
                        diagnostics.append(
                            "delta cost of "
                            + record.call_id
                            + " covered by a provider session snapshot"
                        )
                    else:
                        cost_total += remainder
                else:
                    cost_total += remainder
        elif record.cost_semantics == CostSemantics.PROVIDER_SESSION_CUMULATIVE:
            if record.actual_cost_usd is not None:
                any_cost = True
            # The snapshot itself was folded into ``cost_trusted`` above.
        elif record.actual_cost_usd is not None:
            any_cost = True
            scope = _record_session_scope(record)
            trusted = cost_trusted.get(scope) if scope is not None else None
            if trusted is not None and trusted[0] > position:
                cost_decision_positions.add(position)
                if trusted[1] >= record.actual_cost_usd:
                    # Delta billing for a session whose later cumulative
                    # snapshot provably contains the delta: the snapshot
                    # absorbs it.
                    cost_absorbed_positions.add(position)
                    diagnostics.append(
                        "delta cost of "
                        + record.call_id
                        + " covered by a provider session snapshot"
                    )
                else:
                    cost_total += record.actual_cost_usd
            else:
                cost_total += record.actual_cost_usd

    # The same one-rule invariant the event aggregator enforces: when a
    # record's tokens and cost both faced an absorption candidate and took
    # different paths, the aggregate must not surface as a complete
    # consistent report.
    for position, record in enumerate(unique):
        if (
            position not in token_decision_positions
            or position not in cost_decision_positions
        ):
            continue
        if (position in token_absorbed_positions) != (
            position in cost_absorbed_positions
        ):
            partial = True
            diagnostics.append(
                "token and cost absorption disagree for record "
                + record.call_id
                + ": one side was absorbed into a later snapshot while "
                "the other was billed; marked partial"
            )

    for _, record in token_trusted.values():
        for index, value in enumerate(_record_token_tuple(record)):
            token_totals[index] += value
    if any_cost:
        cost_total += sum(magnitude for _, magnitude in cost_trusted.values())

    available = [r for r in unique if r.usage_status != UsageStatus.UNAVAILABLE]
    if not unique or not available:
        status = UsageStatus.UNAVAILABLE
    elif partial or any(r.usage_status == UsageStatus.UNAVAILABLE for r in unique):
        status = UsageStatus.PARTIAL
    else:
        status = UsageStatus.AVAILABLE
    return UsageRecord(
        call_id=call_id,
        attempt=0,
        usage_status=status,
        provider=(
            unique[0].provider
            if unique and all(r.provider == unique[0].provider for r in unique)
            else "mixed"
        ),
        resolved_model=(
            unique[0].resolved_model
            if unique
            and all(r.resolved_model == unique[0].resolved_model for r in unique)
            else "mixed"
        ),
        resolved_model_source=(
            unique[0].resolved_model_source
            if unique
            and all(
                r.resolved_model_source == unique[0].resolved_model_source
                for r in unique
            )
            else "mixed"
        ),
        logical_input_tokens=token_totals[0],
        uncached_input_tokens=token_totals[1],
        output_tokens=token_totals[2],
        cache_read_input_tokens=token_totals[3],
        cache_creation_input_tokens=token_totals[4],
        cache_creation_5m_input_tokens=token_totals[5],
        cache_creation_1h_input_tokens=token_totals[6],
        actual_cost_usd=cost_total if any_cost else None,
        usage_semantics=UsageSemantics.EVENT_DELTA,
        cost_semantics=CostSemantics.EVENT_DELTA,
        diagnostics=list(dict.fromkeys(diagnostics)),
    )


LEGACY_TALLY_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "total_cost_usd",
)


def legacy_tally_has_value(tally: Optional[Mapping[str, Any]]) -> bool:
    """True when the five-field tally carries a non-zero measurement.

    An unparsable value counts as a measurement: the tally recorded
    *something*, and :func:`legacy_usage_record` surfaces it as ``partial``
    with diagnostics — dropping it would be a silent loss of reported usage.
    """
    if not isinstance(tally, Mapping):
        return False
    for key in LEGACY_TALLY_KEYS:
        if key not in tally:
            continue
        value = tally.get(key)
        if value is None:
            continue
        try:
            if float(value) != 0.0:
                return True
        except (TypeError, ValueError):
            return True
    return False


def legacy_session_tally_is_authoritative(
    state: Optional[Mapping[str, Any]],
) -> bool:
    """True when a serialized state's only usage fact is its legacy tally.

    WHY: presence of the ``session_usage_records`` key alone cannot decide
    this. A pre-ledger flow that is merely re-saved by the modern serializer
    gains an empty ``session_usage_records: []`` next to its still non-zero
    five-field tally, so a presence-only test would make real accumulated
    usage vanish from every surface on the first save. The structural
    invariant survives the round trip instead: in a modern state the tally is
    a PROJECTION of the record ledger, so an empty ledger implies a zero
    tally — an empty ledger beside a non-zero tally can therefore only be
    legacy data. Only the genuinely ambiguous all-zero shape still falls back
    to key presence, where a missing key means "pre-ledger" (adapt, it becomes
    ``legacy_ambiguous``) and a present-but-empty key means "modern flow, zero
    LLM calls so far" (report nothing rather than fabricate a call).
    """
    if not isinstance(state, Mapping):
        return False
    raw_records = state.get("session_usage_records")
    ledger_present = isinstance(raw_records, list)
    if ledger_present and any(isinstance(item, Mapping) for item in raw_records):
        return False
    legacy = state.get("session_token_usage")
    if not isinstance(legacy, Mapping):
        return False
    embedded = legacy.get("usage_records")
    # An old tally that embedded its own per-call records is not record-less:
    # those records are authoritative and load as the ledger itself.
    if isinstance(embedded, list) and any(
        isinstance(item, Mapping) for item in embedded
    ):
        return False
    if legacy_tally_has_value(legacy):
        return True
    return not ledger_present and any(key in legacy for key in LEGACY_TALLY_KEYS)


def legacy_usage_record(
    data: Optional[Mapping[str, Any]],
    *,
    call_id: str = LEGACY_UNKNOWN_CALL_ID,
    attempt: int = 0,
) -> UsageRecord:
    """Adapt the old five-field tally without guessing missing provenance."""
    if not data:
        return UsageRecord(
            call_id=call_id,
            attempt=attempt,
            usage_status=UsageStatus.UNAVAILABLE,
            diagnostics=["legacy record contains no usage"],
        )
    if "usage_status" in data or "logical_input_tokens" in data:
        return UsageRecord.from_dict(data)

    input_tokens, bad_input = _non_negative_int(data.get("input_tokens"))
    output_tokens, bad_output = _non_negative_int(data.get("output_tokens"))
    creation, bad_creation = _non_negative_int(
        data.get("cache_creation_input_tokens")
    )
    cache_read, bad_read = _non_negative_int(data.get("cache_read_input_tokens"))
    nonzero_tokens = any((input_tokens, output_tokens, creation, cache_read))
    raw_cost = data.get("total_cost_usd") if "total_cost_usd" in data else None
    cost, bad_cost = _optional_cost(raw_cost)
    all_zero = not nonzero_tokens and (cost is None or cost == 0.0)
    diagnostics = ["adapted from legacy UsageTotals without provider metadata"]
    status = UsageStatus.LEGACY_AMBIGUOUS if all_zero else UsageStatus.AVAILABLE
    if any((bad_input, bad_output, bad_creation, bad_read, bad_cost)):
        status = UsageStatus.PARTIAL
        diagnostics.append("legacy usage contains invalid values")
    if cost == 0.0:
        cost = None
        diagnostics.append("legacy zero cost cannot be distinguished from missing cost")
    # WHY: legacy tallies were accumulated from Anthropic-shaped events, where
    # ``input_tokens`` EXCLUDES the cache read/creation fields. Projecting them
    # without reinterpreting their semantics — uncached = input_tokens, logical
    # = input + cache categories — is the only reading that does not
    # retroactively guess (and zero out) real measured uncached input.
    uncached = input_tokens
    logical_input = uncached + cache_read + creation
    return UsageRecord(
        call_id=call_id,
        attempt=attempt,
        usage_status=status,
        logical_input_tokens=logical_input,
        uncached_input_tokens=uncached,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=creation,
        actual_cost_usd=cost,
        diagnostics=diagnostics,
    )


# ---------------------------------------------------------------------------
# UsageSummary: one billing-aware aggregation backend for call/step/flow.
# ---------------------------------------------------------------------------


def _record_has_nothing(record: UsageRecord) -> bool:
    """True when a record carries neither tokens nor an actual cost.

    An AVAILABLE record that explicitly reported zero tokens is a real
    measurement of zero consumption — not missing usage — even when the
    provider omitted a cost field.
    """
    if record.usage_status == UsageStatus.AVAILABLE:
        return False
    return (
        record.logical_input_tokens == 0
        and record.output_tokens == 0
        and record.actual_cost_usd is None
    )


def _record_lacks_provenance(record: UsageRecord) -> bool:
    """True when a measured record has no attributable model or source.

    A record with tokens or cost but no resolved model (or no provider /
    agent / runner attribution at all) cannot be priced or attributed; it
    must read as incomplete instead of showing a confident "complete" label
    next to an unknown-model call row and a legacy-incompleteness note.
    Empty (UNAVAILABLE) records are counted as unknown calls elsewhere, not
    here.
    """
    if _record_has_nothing(record):
        return False
    if not record.resolved_model or record.resolved_model == "unknown":
        return True
    if (
        record.provider is None
        and record.agent_name is None
        and record.runner_type is None
    ):
        return True
    return False


def estimate_record_cost(record: UsageRecord, catalog: Any) -> Any:
    """Estimate one call/attempt's cost from its tokens against a catalog.

    ``catalog`` is a ``PricingCatalog`` (or ``None``); the returned
    :class:`~tianluo.pricing.CostEstimate` is "unknown" — never zero — whenever
    the record has no tokens, its model is unknown, or a nonzero token
    category lacks a price.
    """
    from .pricing import CostEstimate, TokenCategory, estimate_cost

    if catalog is None:
        return CostEstimate(None, reason="no pricing catalog")
    return estimate_cost(
        catalog.get(record.resolved_model),
        {
            TokenCategory.UNCACHED_INPUT: record.uncached_input_tokens,
            TokenCategory.OUTPUT: record.output_tokens,
            TokenCategory.CACHE_READ: record.cache_read_input_tokens,
            TokenCategory.CACHE_CREATION: record.cache_creation_input_tokens,
            TokenCategory.CACHE_CREATION_5M: record.cache_creation_5m_input_tokens,
            TokenCategory.CACHE_CREATION_1H: record.cache_creation_1h_input_tokens,
        },
    )


# Cache-creation token categories, for classifying "unknown cache TTL price"
# separately from an unknown base price.
_CREATION_CATEGORIES = frozenset(
    {"cache_creation", "cache_creation_5m", "cache_creation_1h"}
)


@dataclass
class _BillingUnit:
    """One deduplicable billing scope: a provider session or a single call."""

    key: Tuple[str, ...]
    records: List[UsageRecord] = field(default_factory=list)
    cumulative_snapshots: List[float] = field(default_factory=list)
    delta_cost: float = 0.0
    has_delta: bool = False
    # (record position, record, amount) of each raw delta contribution, kept
    # until the coverage pass decides which ones a session snapshot provably
    # absorbs.
    delta_contributions: List[Tuple[int, UsageRecord, float]] = field(
        default_factory=list
    )
    # Set when at least one delta contribution was absorbed because a later
    # same-session cumulative snapshot provably contains it.
    covered_by_session: bool = False

    @property
    def is_session(self) -> bool:
        return self.key[0] == "session"

    def actual_cost(self, diagnostics: List[str]) -> Optional[float]:
        """The unit's deduplicated actual cost.

        A provider-session unit keeps the latest *valid monotonic* snapshot:
        a later snapshot smaller than an earlier one contradicts cumulative
        semantics, so the trusted (larger) value is kept and the anomaly is
        flagged — a regression snapshot must not overwrite a trusted total.
        """
        if self.is_session:
            if not self.cumulative_snapshots:
                return None
            trusted = max(self.cumulative_snapshots)
            if self.cumulative_snapshots != sorted(self.cumulative_snapshots):
                diagnostics.append(
                    f"non-monotonic cumulative cost snapshot for session "
                    f"{self.key[-1]}; keeping trusted value"
                )
            return trusted
        return self.delta_cost if self.has_delta else None


def _build_billing_units(records: List[UsageRecord]) -> List[_BillingUnit]:
    units: Dict[Tuple[str, ...], _BillingUnit] = {}
    for position, record in enumerate(records):
        if record.cost_breakdown:
            # A call carrying cumulative snapshots for one or more provider
            # sessions: each snapshot keeps its billing identity so the
            # session unit below takes the latest valid snapshot per session
            # — a later call's newer snapshot for one session must REPLACE the
            # earlier one, not add to a frozen call-level mix. Any remainder
            # (delta events in the same call) still bills at the call level.
            for snapshot in record.cost_breakdown:
                key: Tuple[str, ...] = (
                    "session",
                    snapshot.provider or "unknown",
                    snapshot.provider_session_id,
                )
                unit = units.setdefault(key, _BillingUnit(key=key))
                unit.records.append(record)
                unit.cumulative_snapshots.append(snapshot.actual_cost_usd)
            covered = sum(
                snapshot.actual_cost_usd for snapshot in record.cost_breakdown
            )
            remainder = (record.actual_cost_usd or 0.0) - covered
            if remainder > 1e-9:
                key = ("call", record.call_id, record.attempt)
                unit = units.setdefault(key, _BillingUnit(key=key))
                unit.records.append(record)
                unit.delta_contributions.append((position, record, remainder))
                unit.delta_cost += remainder
                unit.has_delta = True
            continue
        if (
            record.cost_semantics == CostSemantics.PROVIDER_SESSION_CUMULATIVE
            and record.provider_session_id
        ):
            key: Tuple[str, ...] = (
                "session",
                record.provider or "unknown",
                record.provider_session_id,
            )
        else:
            key = ("call", record.call_id, record.attempt)
        unit = units.setdefault(key, _BillingUnit(key=key))
        unit.records.append(record)
        if record.actual_cost_usd is None:
            continue
        if unit.is_session:
            unit.cumulative_snapshots.append(record.actual_cost_usd)
        else:
            unit.delta_contributions.append(
                (position, record, record.actual_cost_usd)
            )
            unit.delta_cost += record.actual_cost_usd
            unit.has_delta = True

    # A provider that mixes delta costs and session-cumulative snapshots for
    # the SAME session would double-count the delta calls (they are already
    # inside the snapshot). Absorb a delta contribution only when the scope's
    # latest valid snapshot provably contains it: the snapshot must not be
    # earlier than the delta and its magnitude must cover it. A delta emitted
    # after the snapshot (or larger than it) is not inside it and bills per
    # delta semantics. Coverage is a provider-aware billing identity: session
    # ids are only unique within one provider, so an OpenAI delta and an
    # Anthropic snapshot sharing the string "abc" must never suppress each
    # other's charges.
    trusted, _rejected = _cost_trusted_snapshots(records)
    for unit in units.values():
        if unit.is_session or not unit.has_delta:
            continue
        kept: List[Tuple[int, UsageRecord, float]] = []
        dropped = 0.0
        for contribution in unit.delta_contributions:
            position, record, amount = contribution
            scope = (
                record.provider or "unknown",
                record.provider_session_id or record.call_id,
            )
            snapshot = trusted.get(scope)
            if (
                snapshot is not None
                and snapshot[0] > position
                and snapshot[1] >= amount
            ):
                # The later snapshot proves containment: it absorbs the delta
                # instead of billing it twice. A record's own snapshot sits
                # at its own position and never covers its own delta.
                dropped += amount
            else:
                kept.append(contribution)
        unit.delta_contributions = kept
        if dropped > 1e-9:
            unit.delta_cost -= dropped
            unit.has_delta = unit.delta_cost > 1e-9
            unit.covered_by_session = True
    return list(units.values())


@dataclass
class _UnknownCounts:
    unknown_model: int = 0
    unknown_price: int = 0
    unknown_cache_ttl: int = 0


def _classify_estimate_failure(estimate: Any, counts: _UnknownCounts) -> None:
    """Fold one failed *billing unit* estimate into the unknown-call counters.

    Unknown *models* are deliberately NOT counted here. A billing unit's
    aggregate carries the ``mixed`` model sentinel whenever its records ran
    on different models, so an "unknown model" failure at this level cannot
    tell an unlisted model apart from several perfectly listed ones; and a
    unit whose cost is already known (provider actual, session-covered or an
    explicit zero) never reaches estimation at all, hiding its unlisted
    models. Unknown-model counting therefore lives in the per-record pass of
    :meth:`UsageSummary.summarize`, which sees exactly the rows the per-call
    table renders. The failed estimate itself still surfaces through
    ``partial``.
    """
    if estimate.reason == "unknown model":
        return
    if estimate.reason == "no pricing catalog":
        counts.unknown_price += 1
        return
    creation_unknown = [
        c for c in estimate.unknown_categories if c in _CREATION_CATEGORIES
    ]
    other_unknown = [
        c for c in estimate.unknown_categories if c not in _CREATION_CATEGORIES
    ]
    if creation_unknown:
        counts.unknown_cache_ttl += 1
    if other_unknown:
        counts.unknown_price += 1


@dataclass
class UsageSummary:
    """One aggregation backend for call/attempt, step, and flow levels.

    Token totals come from :func:`aggregate_usage_records` (event/session
    de-duplication), ``actual_cost_usd`` from billing-unit de-duplication
    (:meth:`_build_billing_units`), and ``estimated_cost_usd`` covers only
    units that have tokens but no actual cost and a fully priced model.
    Actual and estimated stay separate columns — adding them together would
    fabricate a "total" that is neither all-actual nor all-estimated.
    """

    records: List[UsageRecord] = field(default_factory=list)
    totals: UsageRecord = field(
        default_factory=lambda: UsageRecord(
            call_id="summary", attempt=0, usage_status=UsageStatus.UNAVAILABLE
        )
    )
    actual_cost_usd: Optional[float] = None
    estimated_cost_usd: Optional[float] = None
    unknown_call_count: int = 0
    unknown_model_count: int = 0
    unknown_price_count: int = 0
    unknown_cache_ttl_count: int = 0
    partial: bool = False
    diagnostics: List[str] = field(default_factory=list)

    @classmethod
    def summarize(
        cls,
        records: Iterable[UsageRecord],
        catalog: Any = None,
        *,
        call_id: str = "summary",
        mark_unknown_models: bool = True,
    ) -> "UsageSummary":
        """Build the summary from authoritative call/attempt records.

        ``catalog`` is an optional :class:`~tianluo.pricing.PricingCatalog`;
        without one, units lacking an actual cost are reported as unknown
        price rather than estimated (never as a fabricated zero).

        ``mark_unknown_models=False`` skips the per-record model-provenance
        count. It is for summaries that travel without the per-call rows the
        count annotates (the compact step-level summary in
        ``step.outputs.usage_summary``): there, a legacy-adapted record's
        missing model would only degrade the completeness label beside pure
        totals instead of flagging an unknown-model call row.
        """
        unique, dedup_diagnostics = deduplicate_usage_records(records)
        diagnostics: List[str] = list(dedup_diagnostics)
        totals = aggregate_usage_records(unique, call_id=call_id)
        partial = totals.usage_status in (
            UsageStatus.PARTIAL,
            UsageStatus.LEGACY_AMBIGUOUS,
        )

        units = _build_billing_units(unique)
        actual_parts: List[float] = []
        estimated = 0.0
        has_estimate = False
        # Unknown-usage calls are counted per RECORD so an UNAVAILABLE record
        # sharing a billing unit with a measured record still surfaces — the
        # per-call table renders one row per record and the count describes
        # that table.
        unknown_calls = sum(
            1 for record in unique if _record_has_nothing(record)
        )
        counts = _UnknownCounts()
        for unit in units:
            actual = unit.actual_cost(diagnostics)
            if actual is not None:
                actual_parts.append(actual)
                continue
            if unit.covered_by_session:
                # The unit's delta cost is provably inside a later provider
                # session snapshot already counted above — known cost,
                # nothing to estimate.
                diagnostics.append(
                    "delta cost of call unit "
                    + ":".join(str(k) for k in unit.key[1:])
                    + " covered by a provider session snapshot"
                )
                continue
            unit_totals = aggregate_usage_records(
                unit.records, call_id="unit:" + ":".join(str(k) for k in unit.key)
            )
            if _record_has_nothing(unit_totals):
                # No tokens and no cost at the unit level: each empty record
                # was already counted as an unknown call above.
                continue
            if (
                unit_totals.usage_status == UsageStatus.AVAILABLE
                and unit_totals.logical_input_tokens == 0
                and unit_totals.output_tokens == 0
            ):
                # The provider explicitly reported zero consumption: a real
                # (AVAILABLE) measurement with nothing to estimate — it must
                # not be counted as an unknown call or a partial estimate.
                continue
            estimate = estimate_record_cost(unit_totals, catalog)
            if estimate.is_estimated:
                estimated += estimate.estimated_cost_usd
                has_estimate = True
            else:
                _classify_estimate_failure(estimate, counts)
                partial = True

        for record in unique:
            if record.usage_status in (
                UsageStatus.PARTIAL,
                UsageStatus.LEGACY_AMBIGUOUS,
            ):
                partial = True
            # Unknown models are counted per RECORD, against the very estimate
            # the per-call table renders, so the count always matches the rows
            # a reader sees. Billing-unit accounting must not gate it: a unit
            # whose cost is already known (provider actual, covered by a later
            # session snapshot, or an explicit zero) never reaches unit-level
            # estimation, yet its individual records still render "unknown
            # cost" rows when their model is absent from the catalog.
            if _record_lacks_provenance(record):
                # Missing provenance (no resolved model at all) is an
                # attribution gap in its own right, so it counts even when the
                # provider reported this record's cost. It is the one kind the
                # compact step-level summary suppresses: that summary travels
                # without the per-call rows the count annotates.
                if mark_unknown_models:
                    counts.unknown_model += 1
                continue
            if (
                record.actual_cost_usd is None
                and estimate_record_cost(record, catalog).reason == "unknown model"
            ):
                # A fully attributed record whose named model is merely absent
                # from the price table only leaves a gap when nothing else
                # gives its row a cost: with a provider actual cost the row is
                # exact, and the price table's silence costs the reader
                # nothing.
                counts.unknown_model += 1
        return cls(
            records=unique,
            totals=totals,
            actual_cost_usd=sum(actual_parts) if actual_parts else None,
            estimated_cost_usd=estimated if has_estimate else None,
            unknown_call_count=unknown_calls,
            unknown_model_count=counts.unknown_model,
            unknown_price_count=counts.unknown_price,
            unknown_cache_ttl_count=counts.unknown_cache_ttl,
            partial=partial,
            diagnostics=list(dict.fromkeys(diagnostics)),
        )

    @property
    def completeness(self) -> str:
        """``complete`` when nothing is unknown, else ``partial``."""
        if (
            self.unknown_call_count
            or self.unknown_model_count
            or self.unknown_price_count
            or self.unknown_cache_ttl_count
            or self.partial
        ):
            return "partial"
        return "complete"

    def to_dict(self, include_records: bool = True) -> Dict[str, Any]:
        """Serialize; ``include_records=False`` omits the record list.

        Step outputs already persist the authoritative records under
        ``usage_records``, so storing them a second time inside the summary
        would double every step's serialized size for no new information.
        """
        data: Dict[str, Any] = {
            "actual_cost_usd": self.actual_cost_usd,
            "estimated_cost_usd": self.estimated_cost_usd,
            "unknown_call_count": self.unknown_call_count,
            "unknown_model_count": self.unknown_model_count,
            "unknown_price_count": self.unknown_price_count,
            "unknown_cache_ttl_count": self.unknown_cache_ttl_count,
            "partial": self.partial,
            "diagnostics": list(self.diagnostics),
        }
        if include_records:
            data["records"] = [record.to_dict() for record in self.records]
        return data

    def to_dict_for_wire(self) -> Dict[str, Any]:
        """Compact records-free serialization for status/history payloads.

        Rides periodic daemon/server frames, where the record list would be
        redundant (records travel separately) and too large to repeat on every
        push.  Kept distinct from ``to_dict(include_records=False)`` so call
        sites state the wire intent explicitly.
        """
        data = self.to_dict(include_records=False)
        data["totals"] = self.totals.to_dict()
        data["completeness"] = self.completeness
        return data

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "UsageSummary":
        if not data:
            return cls()
        raw_records = data.get("records")
        records = [
            UsageRecord.from_dict(item)
            for item in raw_records
            if isinstance(item, Mapping)
        ] if isinstance(raw_records, list) else []
        # Token totals and billing-deduped actual cost are re-derived from the
        # records when they are present; the estimation column and unknown
        # counters are catalog-dependent, so the persisted values are restored
        # verbatim. A records-free serialization (include_records=False) keeps
        # its stored actual cost and totals, since there is nothing to
        # re-derive from.
        summary = cls.summarize(records)
        if not records:
            summary.actual_cost_usd = data.get("actual_cost_usd")
            raw_totals = data.get("totals")
            if isinstance(raw_totals, Mapping):
                summary.totals = UsageRecord.from_dict(raw_totals)
        summary.estimated_cost_usd = data.get("estimated_cost_usd")
        summary.unknown_call_count = int(data.get("unknown_call_count", 0) or 0)
        summary.unknown_model_count = int(data.get("unknown_model_count", 0) or 0)
        summary.unknown_price_count = int(data.get("unknown_price_count", 0) or 0)
        summary.unknown_cache_ttl_count = int(
            data.get("unknown_cache_ttl_count", 0) or 0
        )
        summary.partial = bool(data.get("partial", summary.partial))
        stored_diagnostics = _string_list(data.get("diagnostics"))
        if stored_diagnostics:
            summary.diagnostics = list(
                dict.fromkeys(summary.diagnostics + stored_diagnostics)
            )
        return summary


def build_usage_payload(
    records_by_step: Mapping[str, Iterable[UsageRecord]],
    catalog: Any = None,
    *,
    flow_records: Optional[Iterable[UsageRecord]] = None,
    call_id: str = "flow",
) -> Dict[str, Any]:
    """Build the one usage/cost payload shared by CLI history, daemon and server.

    ``records_by_step`` maps a step identity to that step's call/attempt
    records (drives the per-step table); ``flow_records`` — when supplied —
    is the authoritative whole-flow record set (``State.session_usage_records``)
    and overrides the union of the per-step records for the flow totals, so a
    session accumulator and its per-step sources can never double-count.
    Without ``flow_records`` the union of the per-step records is used, which
    is the correct recovery for history-only flows.

    The returned dict is JSON-safe and self-describing:

    * ``calls`` — deduplicated per-call/attempt record dicts in first-seen
      order (the per-call table);
    * ``steps`` — ``{step_key: {"summary": <records-free UsageSummary dict>,
      "record_count": int}}``;
    * ``summary`` — records-free flow-level UsageSummary dict (or ``None``
      when no usage was recorded at all);
    * ``legacy`` — True when any record was adapted from a legacy
      five-field tally (its completeness is then marked, never a fake zero);
    * ``completeness`` — ``"complete" | "partial" | "none"``.

    Actual and estimated cost stay separate columns inside every summary —
    the payload never fabricates a combined total.
    """
    step_summaries: Dict[str, Any] = {}
    for step_key, records in records_by_step.items():
        step_records = [record for record in records]
        if not step_records:
            continue
        step_summary = UsageSummary.summarize(
            step_records, catalog, call_id=f"step:{step_key}"
        )
        step_summaries[str(step_key)] = {
            "summary": step_summary.to_dict_for_wire(),
            "record_count": len(step_summary.records),
        }
    if flow_records is None:
        flow_records = [
            record for records in records_by_step.values() for record in records
        ]
    else:
        flow_records = list(flow_records)
    if not flow_records:
        return {
            "calls": [],
            "steps": step_summaries,
            "summary": None,
            "legacy": False,
            "completeness": "none",
        }
    summary = UsageSummary.summarize(flow_records, catalog, call_id=call_id)
    # Legacy-adapted records carry an adapter diagnostic even when their
    # non-zero numbers are usable (AVAILABLE) — the *provenance* is legacy, so
    # the payload must still say so rather than presenting them as modern
    # provider reports.
    legacy = any(
        record.usage_status == UsageStatus.LEGACY_AMBIGUOUS
        or any("legacy" in diagnostic.lower() for diagnostic in record.diagnostics)
        for record in summary.records
    )
    # Per-call estimates are part of the mandated per-call display: compute
    # them HERE (the one shared backend) so the CLI text view and the WebUI
    # table render the same figure instead of the CLI estimating locally while
    # the browser shows a dash. ``None`` means "not estimable", never zero.
    calls: List[Dict[str, Any]] = []
    for record in summary.records:
        entry = record.to_dict()
        estimate = estimate_record_cost(record, catalog)
        entry["estimated_cost_usd"] = (
            estimate.estimated_cost_usd if estimate.is_estimated else None
        )
        calls.append(entry)
    return {
        "calls": calls,
        "steps": step_summaries,
        "summary": summary.to_dict_for_wire(),
        "legacy": legacy,
        "completeness": summary.completeness,
    }
