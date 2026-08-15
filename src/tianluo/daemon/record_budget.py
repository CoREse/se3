"""Tiered size budgeting and water-fill compaction for history records.

A history record is one physical ``jsonl`` line of a ``luo run`` step file. Most
are small, but a long agentic step can emit a single record of tens of MB — one
observed ``discovery`` record was 23.6 MB, carrying 206 ``tool_result`` events
buried among ~46 000 zero-render telemetry events. Delivering such a record
whole stalls the daemon→server link, so it has to be *shrunk* before it is put
on the wire.

The shape of that shrinking is the point of this module, and it is deliberately
NOT "cut the record off when the budget runs out". The WebUI reconciles tool
chips idempotently by ``step_id#ordinal``; an event dropped at the truncation
point never comes back on a later frame, so the whole back half of a step's tool
calls would silently vanish. Instead the budget is met by *water filling*: every
event survives, in its original position, and the ones that are large enough to
matter give up preview text until the record fits. Event count and order are
invariants of every function here.

The module is a pure, I/O-free, stateless function set on purpose: the daemon
read path (``daemon/history.py``) and the offline backfill script both import it
so that an online-degraded record and a compacted stored record come out the
same shape, and so the water-fill maths can be unit-tested without a filesystem.
It must stay free of daemon-internal imports for the script's sake.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: Records at or above this raw line size are analysed; everything below is
#: passed through untouched, without so much as a look at ``raw_json``.
#:
#: WHY: the tiering exists to keep compaction off the hot path. Measured over
#: the 8954 records of the pathological flow, the distribution is p50 = 750 B,
#: p99 ~ 36 KB — only 0.95 % of records reach 64 KB, and only 0.29 % actually
#: exceed the 1 MB record budget below. Putting the gate at a size that is
#: already known from ``len(line)`` at read time means ~99 % of traffic pays
#: literally one integer comparison: no structural walk, no re-serialisation.
#: A lower gate would drag thousands of harmless records through a full
#: ``raw_json`` traversal for nothing; a higher one would let records that do
#: need shrinking through.
RECORD_FAST_PATH_BYTES = 64 * 1024

#: Hard ceiling on a single serialised ``raw_json`` event, and the cap the
#: water level can never exceed.
#:
#: WHY: the water level alone is not enough. A record can hold few events but
#: one enormous one (a ``tool_result`` carrying a whole file), in which case the
#: level solved from the record budget would leave that single event at ~1 MB —
#: still large enough to dominate a frame and to freeze a browser laying out one
#: chip. 256 KB is far above any legible preview, so the cap only ever bites
#: pathological payloads, and it matches :data:`MAX_BYTES_PER_REPORT` in
#: ``daemon/history.py`` so one capped event can never on its own overshoot a
#: delivery chunk by more than the intended one-record overshoot.
MAX_EVENT_BYTES = 256 * 1024

#: Budget for one record's serialised ``raw_json`` array.
#:
#: WHY: this is the target the water level is solved against. At 1 MB, the 0.29 %
#: of records that exceed it get compacted while the 99.7 % that do not are
#: bit-identical to what the step wrote; and a compacted record still fits in a
#: handful of ``MAX_BYTES_PER_REPORT`` chunks, so catch-up over a bad link stays
#: a few round-trips rather than the unbounded livelock a 23.6 MB record caused.
#: The budget covers ``raw_json`` only — top-level record fields (``content``,
#: ``usage_records``, ``token_usage``, …) are compaction-immune, so they are not
#: charged against it and cannot be damaged by it.
MAX_RECORD_RAW_JSON_BYTES = 1024 * 1024

#: Event ``(type, subtype)`` pairs that may be folded into a run marker.
#:
#: WHY: this whitelist is the entire safety argument for folding, so it is
#: expressed as an explicit closed set rather than as a heuristic. Every kind
#: listed here has *zero* consumers in ``src/`` — the WebUI never mentions
#: ``thinking_tokens`` or ``tool_progress``, and token accounting does not come
#: from them either: usage is read from the record's top-level ``usage_records``
#: / ``token_usage`` and from the terminal ``result`` event, all of which are
#: immune below. Folding them therefore loses no renderable and no billable
#: information. It is also a *necessary* step, not an optimisation: 46 163
#: ``thinking_tokens`` events cost ~2.8 MB even at a 60-byte stub each, which
#: alone blows the 1 MB budget before any preview text is considered.
#: ``assistant`` / ``user`` / ``result`` are conspicuously absent and must stay
#: absent — they carry the tool chips the frontend renders.
FOLDABLE_EVENT_KINDS = frozenset(
    {
        ("system", "thinking_tokens"),
        ("system", "tool_progress"),
        ("tool_progress", None),
    }
)

#: Event types never folded and never shrunk.
#:
#: WHY: the ``result`` event carries the step's authoritative ``usage`` /
#: ``total_cost_usd`` tallies, which the usage subsystem reads back verbatim.
#: Truncating a string inside it would corrupt accounting rather than a preview.
IMMUNE_EVENT_TYPES = frozenset({"result"})

#: Object keys whose string values are structural identity, never preview text.
#:
#: WHY: the frontend pairs a ``tool_use`` with its ``tool_result`` by
#: ``tool_use_id`` and picks a renderer by ``type`` / ``subtype``. Shortening any
#: of these would not save meaningful bytes but would break chip pairing — the
#: exact failure mode compaction exists to avoid.
IMMUNE_STRING_KEYS = frozenset(
    {
        "type",
        "subtype",
        "role",
        "name",
        "id",
        "uuid",
        "tool_use_id",
        "parent_tool_use_id",
        "session_id",
        "model",
        "stop_reason",
        "status",
    }
)

#: Subtype of the marker event a folded telemetry run collapses into.
FOLDED_EVENT_SUBTYPE = "tianluo_folded_telemetry"

#: Suffix appended where preview text was cut, with the dropped byte count.
#:
#: WHY: the marker is the frontend's only signal that a body is partial, so it
#: has to survive a round trip through JSON and be matchable without ambiguity —
#: hence a fixed literal with a machine-readable count rather than an ellipsis.
TRUNCATION_MARKER_TEMPLATE = "\n\n[tianluo:truncated {dropped} bytes]"

#: Matches a marker previously appended by :func:`shrink_event`.
TRUNCATION_MARKER_PATTERN = re.compile(r"\n\n\[tianluo:truncated (\d+) bytes\]\Z")

#: Event-level flag recording how many bytes an event lost.
TRUNCATION_FLAG_KEY = "tianluo_truncated"

#: Strings shorter than this are never candidates for truncation.
#:
#: WHY: below a few hundred bytes the marker costs more than the cut saves, and
#: short strings are overwhelmingly labels and paths whose value is all-or-
#: nothing. Leaving them alone is why a pathologically wide event made of many
#: tiny strings reports overflow instead of being mangled into noise.
_MIN_SHRINKABLE_LEAF_BYTES = 256

#: Byte allowance reserved for the marker when sizing a truncated leaf.
_MARKER_RESERVE_BYTES = 64

#: Byte allowance reserved for the event-level truncation flag.
#:
#: WHY: the flag is written after the shrinking passes have converged, so its
#: own bytes are not visible to the level solver. Without this reserve an event
#: shrunk to exactly the limit ends up a few bytes over it once flagged, which
#: turns a hard cap into an approximate one.
_FLAG_RESERVE_BYTES = 48

#: Refinement passes inside :func:`shrink_event`.
#:
#: WHY: appending markers adds bytes the water level did not account for, so a
#: single pass can land just above the limit. Re-solving against the measured
#: size converges in one or two extra passes; the bound just guarantees
#: termination when no leaf can give up any more bytes.
_SHRINK_PASSES = 4


@dataclass
class CompactionStats:
    """What :func:`compact_record` did to one record.

    ``compacted`` is False for the fast path and for records that turned out to
    fit; ``overflow`` marks the residual case where the structural floor of the
    events exceeds the budget and no further preview text could be given up.
    """

    original_bytes: int = 0
    compacted_bytes: int = 0
    raw_json_bytes: int = 0
    folded_events: int = 0
    shrunk_events: int = 0
    dropped_bytes: int = 0
    watermark: Optional[int] = None
    compacted: bool = False
    overflow: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "original_bytes": self.original_bytes,
            "compacted_bytes": self.compacted_bytes,
            "raw_json_bytes": self.raw_json_bytes,
            "folded_events": self.folded_events,
            "shrunk_events": self.shrunk_events,
            "dropped_bytes": self.dropped_bytes,
            "watermark": self.watermark,
            "compacted": self.compacted,
            "overflow": self.overflow,
        }


def _dumps(obj: Any) -> str:
    """Serialise *obj* the way the daemon→server link serialises it.

    WHY: the budget is a *wire* budget, so sizes have to be measured with the
    encoder the wire actually uses (``daemon/client.py`` dumps with
    ``ensure_ascii=False`` and default separators). Measuring with compact
    separators or escaped non-ASCII would under- or over-count by a wide margin
    on CJK-heavy records and let the real frame miss its budget.
    """
    return json.dumps(obj, ensure_ascii=False, default=str)


def _byte_len(text: str) -> int:
    return len(text.encode("utf-8"))


def event_size(event: Any) -> int:
    """Serialised byte size of one ``raw_json`` event."""
    return _byte_len(_dumps(event))


def record_size(message: Any) -> int:
    """Serialised byte size of a whole record."""
    return _byte_len(_dumps(message))


def needs_compaction(raw_len: int) -> bool:
    """Whether a record of *raw_len* bytes must be analysed at all."""
    return raw_len >= RECORD_FAST_PATH_BYTES


def _event_kind(event: Any) -> Tuple[Optional[str], Optional[str]]:
    if not isinstance(event, dict):
        return (None, None)
    etype = event.get("type")
    subtype = event.get("subtype")
    return (
        etype if isinstance(etype, str) else None,
        subtype if isinstance(subtype, str) else None,
    )


def is_foldable_event(event: Any) -> bool:
    """Whether *event* is a whitelisted zero-render telemetry event."""
    return _event_kind(event) in FOLDABLE_EVENT_KINDS


def is_immune_event(event: Any) -> bool:
    """Whether *event* must be passed through byte-for-byte."""
    etype, _ = _event_kind(event)
    return etype in IMMUNE_EVENT_TYPES


def _fold_marker(kinds: Sequence[Tuple[Optional[str], Optional[str]]]) -> Dict[str, Any]:
    labels = []
    for etype, subtype in kinds:
        label = "%s/%s" % (etype or "?", subtype) if subtype else (etype or "?")
        if label not in labels:
            labels.append(label)
    return {
        "type": "system",
        "subtype": FOLDED_EVENT_SUBTYPE,
        "count": len(kinds),
        "kinds": labels,
    }


def fold_telemetry_events(raw_json: Sequence[Any]) -> Tuple[List[Any], int]:
    """Collapse consecutive whitelisted telemetry runs into count markers.

    Returns the new event list and the number of original events folded away.
    Folding acts on *runs*, so it is applied evenly across the whole record and
    never favours the head over the tail: a run that sits between two tool calls
    is replaced in place by one marker, leaving the surrounding events — and
    their relative order — untouched.
    """
    folded: List[Any] = []
    run: List[Tuple[Optional[str], Optional[str]]] = []
    folded_count = 0

    def flush() -> None:
        nonlocal folded_count
        if not run:
            return
        folded.append(_fold_marker(run))
        folded_count += len(run)
        run.clear()

    for event in raw_json:
        if is_foldable_event(event):
            run.append(_event_kind(event))
            continue
        flush()
        folded.append(event)
    flush()
    return folded, folded_count


def solve_watermark(sizes: Sequence[int], budget: int, cap: int) -> int:
    """Largest level ``L <= cap`` with ``sum(min(size, L)) <= budget``.

    This is the water-filling solution, and it is exactly equivalent to
    repeatedly shaving the currently largest event until the total fits — which
    is what makes it fair: the level depends only on the size distribution, not
    on where an event sits in the record, so no positional bias (and therefore
    no vanishing tail) can creep in.
    """
    cap = max(0, int(cap))
    if budget < 0:
        return 0
    ordered = sorted(int(size) for size in sizes)
    count = len(ordered)
    if count == 0:
        return cap
    prefix = 0
    for index, size in enumerate(ordered):
        remaining = count - index
        if prefix + remaining * size > budget:
            level = (budget - prefix) // remaining
            return max(0, min(cap, level))
        prefix += size
    return cap


def _collect_string_leaves(node: Any, out: List[Tuple[Any, Any, str]]) -> None:
    """Gather ``(container, key, text)`` for every shrinkable string leaf."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str):
                if key in IMMUNE_STRING_KEYS:
                    continue
                if _byte_len(value) >= _MIN_SHRINKABLE_LEAF_BYTES:
                    out.append((node, key, value))
            else:
                _collect_string_leaves(value, out)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            if isinstance(value, str):
                if _byte_len(value) >= _MIN_SHRINKABLE_LEAF_BYTES:
                    out.append((node, index, value))
            else:
                _collect_string_leaves(value, out)


def _split_marker(text: str) -> Tuple[str, int]:
    """Split an already-truncated leaf into its body and prior dropped count.

    WHY: a leaf can be revisited by a later refinement pass. Re-truncating the
    marker-bearing text would nest markers and make the reported byte count a
    lie, so the previous marker is peeled off and its count carried forward.
    """
    match = TRUNCATION_MARKER_PATTERN.search(text)
    if not match:
        return text, 0
    return text[: match.start()], int(match.group(1))


def _truncate_leaf(text: str, keep_bytes: int) -> Tuple[str, int]:
    body, prior_dropped = _split_marker(text)
    encoded = body.encode("utf-8")
    keep = max(0, keep_bytes - _MARKER_RESERVE_BYTES)
    if keep >= len(encoded) and prior_dropped == 0:
        return text, 0
    # ``errors="ignore"`` drops a multi-byte character split by the byte cut.
    kept = encoded[:keep].decode("utf-8", "ignore")
    dropped = prior_dropped + (len(encoded) - _byte_len(kept))
    if dropped <= 0:
        return text, 0
    return kept + TRUNCATION_MARKER_TEMPLATE.format(dropped=dropped), dropped - prior_dropped


def shrink_event(event: Any, limit: int) -> Tuple[Any, int]:
    """Shrink *event* to at most *limit* serialised bytes.

    Returns ``(event, dropped_bytes)``. An event already within *limit* is
    returned as the very same object — no copy is made. Shrinking cuts the
    largest string leaves first (water-filling again, one level down), so a
    ``tool_result`` body or a ``thinking`` block gives up text before a short
    field does, and structural fields (``tool_use_id``, block ``type``,
    ``is_error``, …) are never touched at all.
    """
    if event_size(event) <= limit:
        return event, 0

    clone = copy.deepcopy(event)
    working_limit = limit - _FLAG_RESERVE_BYTES if isinstance(event, dict) else limit
    working_limit = max(0, working_limit)
    total_dropped = 0
    for _ in range(_SHRINK_PASSES):
        size = event_size(clone)
        if size <= working_limit:
            break
        leaves: List[Tuple[Any, Any, str]] = []
        _collect_string_leaves(clone, leaves)
        if not leaves:
            break
        sizes = [_byte_len(text) for _, _, text in leaves]
        target = max(0, sum(sizes) - (size - working_limit))
        level = solve_watermark(sizes, target, max(sizes))
        pass_dropped = 0
        for (container, key, text), leaf_size in zip(leaves, sizes):
            if leaf_size <= level:
                continue
            new_text, dropped = _truncate_leaf(text, level)
            if dropped <= 0:
                continue
            container[key] = new_text
            pass_dropped += dropped
        if pass_dropped == 0:
            break
        total_dropped += pass_dropped

    if total_dropped == 0:
        # Nothing could be given up (all leaves are structural noise): hand back
        # the original object so the caller can report overflow instead of
        # shipping a pointlessly copied event.
        return event, 0
    if isinstance(clone, dict):
        clone[TRUNCATION_FLAG_KEY] = total_dropped
    return clone, total_dropped


def compact_record(
    message: Any, raw_len: Optional[int] = None
) -> Tuple[Any, CompactionStats]:
    """Bring one record within budget, preserving every event.

    *raw_len* is the record's known raw line size; it is the fast-path gate, so
    passing the value the reader already has avoids serialising the record just
    to decide it does not need compacting. Returns ``(message, stats)`` — the
    same ``message`` object when nothing had to change.
    """
    original = record_size(message) if raw_len is None else int(raw_len)
    stats = CompactionStats(original_bytes=original, compacted_bytes=original)

    if not needs_compaction(original) or not isinstance(message, dict):
        return message, stats

    raw_json = message.get("raw_json")
    if not isinstance(raw_json, list) or not raw_json:
        return message, stats

    # WHY: folding is skipped below the record budget because it cannot be
    # needed there — a raw_json array nested inside a sub-budget record is
    # itself under budget, so only the per-event cap can bite, and that is
    # handled by the water level alone. Skipping keeps records in the
    # 64 KB–1 MB band byte-identical to what the step wrote.
    if original > MAX_RECORD_RAW_JSON_BYTES:
        events, folded_count = fold_telemetry_events(raw_json)
    else:
        events, folded_count = list(raw_json), 0
    stats.folded_events = folded_count

    sizes = [event_size(event) for event in events]
    immune = [is_immune_event(event) for event in events]
    # Brackets plus ", " between elements — the array's own structural cost.
    overhead = 2 + max(0, len(events) - 1) * 2
    immune_total = sum(size for size, is_im in zip(sizes, immune) if is_im)
    budget = MAX_RECORD_RAW_JSON_BYTES - overhead - immune_total
    shrinkable_sizes = [size for size, is_im in zip(sizes, immune) if not is_im]
    level = solve_watermark(shrinkable_sizes, budget, MAX_EVENT_BYTES)
    stats.watermark = level

    compacted_events: List[Any] = []
    shrunk = 0
    dropped_total = 0
    for event, size, is_im in zip(events, sizes, immune):
        if is_im or size <= level:
            compacted_events.append(event)
            continue
        new_event, dropped = shrink_event(event, level)
        compacted_events.append(new_event)
        if dropped:
            shrunk += 1
            dropped_total += dropped
    stats.shrunk_events = shrunk
    stats.dropped_bytes = dropped_total

    if folded_count == 0 and shrunk == 0:
        # Untouched: keep the caller's object identity so the read path can pass
        # the record straight through. This branch also covers the structural
        # floor case — thousands of events whose strings are all too short to be
        # worth cutting — where the honest answer is to keep every event and
        # report the overshoot rather than start deleting events to hit a number.
        stats.raw_json_bytes = sum(sizes) + overhead
        stats.overflow = stats.raw_json_bytes > MAX_RECORD_RAW_JSON_BYTES
        return message, stats

    compacted = dict(message)
    compacted["raw_json"] = compacted_events
    raw_json_bytes = _byte_len(_dumps(compacted_events))
    stats.compacted = True
    stats.raw_json_bytes = raw_json_bytes
    stats.compacted_bytes = record_size(compacted)
    stats.overflow = raw_json_bytes > MAX_RECORD_RAW_JSON_BYTES
    return compacted, stats
