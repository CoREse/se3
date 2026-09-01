"""Summary/detail split for the server→browser history bundle.

The daemon→server leg is untouched: :class:`~tianluo.server.state.ServerState`
still caches the FULL bundle (budgeted, evictable) exactly as the daemon pushed
it. This module is the out-of-the-wire shaping applied on the way to the
browser, plus the per-message detail lookup that serves what it took out.

WHY the split exists: opening a long session used to download every tool call's
full body (a Write's file content, a Bash/Read output, a large tool_result) even
though the console renders every successful chip COLLAPSED. The collapsed chip
needs the header line and nothing else, so the bodies are held back and fetched
on expand (``GET /api/history/{flow_id}/detail``).

INVARIANT: the collapsed chip must stay byte-identical to what the un-summarized
bundle produced. Two detail sources feed it and both are handled here.

* ``stream_progress`` records carry a server-built ``tool_detail`` payload that
  the frontend feeds straight into the detail panel — it is pure detail, never
  read by the header, so the whole payload is dropped and replaced by the
  ``tool_detail_lazy`` marker. A dropped payload also means the frontend builds
  no panel DOM for that chip until it is expanded. A payload smaller than the
  markers replacing it stays inline (the floor below).
* assistant/user records carry ``raw_json``, from which the frontend derives
  BOTH the header and the detail locally (``extractAssistantChipEvents``). It
  therefore cannot be dropped. Instead the heavy bodies inside a
  ``tool_use.input`` / ``tool_result.content`` are replaced by an elision stub,
  and the frontend rehydrates it into a synthetic string before deriving the
  header. The stub preserves exactly the two properties every header formatter
  reads — the string's LINE COUNT and its first :data:`ELIDE_HEAD_CHARS`
  characters — which is why :data:`ELIDE_HEAD_CHARS` must stay above the widest
  preview any header takes (80, the failure-message preview; 60 on any path
  that can actually be elided) and why a rehydrated stub is never short enough
  to skip a truncation the original took. The few keys a header reads whole or
  tail-first instead of as a prefix (:data:`VERBATIM_INPUT_KEYS`) are exempt
  from elision entirely.

The same argument reaches the ENGINE's step events (``step_completed`` /
``step_failed`` / ``step_output``), which is where the bulk of a long flow
actually is: the record carries a full StepState snapshot whose ``inputs`` is
the machine input handed TO the step — a whole ``scope_diff``, ``test_results``,
``fix_history``, the task description repeated under three names — while the
default render reads only ``outputs``' structured result fields plus status,
error_message and token_usage. So ``inputs`` is reduced to
:data:`STEP_INPUT_INLINE_KEYS` (the handful of scalars the card really does
read) and the record is marked :data:`STEP_INPUTS_LAZY_KEY`; "View raw" fetches
the original message back under source :data:`DETAIL_SOURCE_STEP`, which is
addressed by the record alone since a step event holds no tool call.
``outputs`` is never touched — that IS the collapsed view.

A ``tool_result``'s content is collapsed STRUCTURALLY rather than string by
string: the header reads it only through ``_toolExtractText`` (line count plus,
for an unregistered tool, a 60-char preview), so the whole value — a block
list, a nested ``{content: [...]}``, a plain string alike — is replaced by one
stub built from that same extracted text. WHY: a result made of many
individually-small blocks used to ride whole because no single string crossed
the floor, which is most of what a long session actually downloads.

A ``tool_use`` input is collapsed the same way wherever it holds a LIST: the
list keeps a :data:`_LIST_HEAD_WIRE`-character head and drops its tail. WHY not
a per-string walk alone: a million-element numeric array (or ten thousand short
strings) has no oversize string leaf, yet the collapsed chip reads at most a
30-character ``JSON.stringify`` preview of it — so the un-previewed remainder
was pure download. A kept head is a genuine JSON prefix of the original, which
is what makes the header byte-identical without either side having to reproduce
the other language's serializer.

The floor is where eliding stops paying, and it is measured against the
COMPLETE replacement cost, not just the stub: a held-back body also has to name
itself in ``lazy_tool_use_ids`` (with its ``lazy_body_mask`` character) and put
``detail_flow`` — at the flow id's ACTUAL length — plus ``detail_version`` on
the record, all priced under the WIDEST of the two encoders that carry them
(the WebSocket relay renders with ``json.dumps``' default spacing, the REST
response with compact separators). A body that does not clear all of that rides
inline, because holding it back would GROW the response and add a request for
something the browser already had. :data:`ELIDE_MIN_CHARS` is only the cheap
pre-filter; the per-body and per-record arithmetic in :func:`_wire_saving` /
:func:`_summarize_record` is the decision.

Which of a call's two bodies was actually replaced is stated per id in
:data:`LAZY_BODY_MASK_KEY`, never inferred from the stub's shape: the id list
names a CALL, and one of its bodies can be lazified while the other rides inline
under boundary rule (c). ``__elided__`` is not reserved in a tool's arguments,
so a shape-based rehydration would rewrite a legitimate inline argument and
change the collapsed chip header.

Boundary rule (c) — fold-visible bodies — is the other thing that keeps a body
inline. A record whose own ``content`` is empty has its FOLDED bubble recovered
from ``raw_json`` itself (``extractAssistantText``), which makes two raw shapes
part of the collapsed view rather than of the detail: a ``tool_use`` input
(printed into the bubble as ``[Name: <JSON>]``) and a BARE top-level
``tool_result`` line's content (walked block by block as narrative — and read
only as STRINGS, so an elided text block is not shortened but dropped, leaving
the bubble empty). Both ride inline for such a record, un-stubbed, and the
frontend renders them where they are with no on-demand request.

Failed tool calls are exempt from the whole mechanism: their chip auto-expands,
so lazifying them would turn a failure-heavy session into a burst of on-demand
requests at load time. Their detail rides inline, unchanged.

INVARIANT: only a record the detail endpoint can ADDRESS is ever lazified. The
address is the record's stable identity — ``(step_id, ordinal)``, the daemon's
own per-step physical line number — because a ``tool_use_id`` alone is not
unique within a flow: codex synthesizes ids like ``codex_tool_1`` per call, so
two steps can each hold one, and a flow-wide scan would answer the first chip
with the second call's body. A record without that address keeps its bodies
inline rather than being made unfetchable.

Pure and I/O-free on purpose: ``state.py`` imports the extraction half for the
cache lookup and ``app.py`` imports the shaping half for the response, so
neither may import back into this module's callers.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "ELIDED_KEY",
    "ELIDE_HEAD_CHARS",
    "ELIDE_MIN_CHARS",
    "VERBATIM_INPUT_KEYS",
    "DETAIL_SOURCE_PROGRESS",
    "DETAIL_SOURCE_RAW",
    "DETAIL_SOURCE_STEP",
    "DETAIL_VERSION_KEY",
    "LAZY_BODY_MASK_KEY",
    "STEP_EVENT_TYPES",
    "STEP_INPUTS_LAZY_KEY",
    "STEP_INPUT_INLINE_KEYS",
    "elide_string",
    "record_address",
    "summarize_history_records",
    "locate_record_detail",
]


#: Marker key identifying an elided string stub inside a summarized ``raw_json``.
ELIDED_KEY = "__elided__"

#: Verbatim prefix kept on an elided string. The widest preview any collapsed
#: header takes is 80 characters (``_toolFailureBody`` — and failures are exempt
#: from elision entirely, so the widest an elided value can actually meet is the
#: 60-char generic success preview). 96 clears both with margin, and every
#: rehydrated stub is therefore still longer than any preview window — so the
#: "was this value truncated?" branch inside a formatter cannot flip.
ELIDE_HEAD_CHARS = 96

#: The wire cost of a stub itself: ``{"__elided__":true,"head":"…","lines":N,
#: "chars":N}`` is the head plus ~64 bytes of framing. Nothing smaller than this
#: is ever held back — replacing it would enlarge the response AND cost the
#: browser a request to get back what it already had.
#:
#: This is only the CHEAP pre-filter. The binding rule is benefit rule (b),
#: which every elision is measured against individually
#: (:func:`_wire_saving`) and which also charges the markers a lazified body
#: forces onto the record — the ``tool_use_id`` added to
#: ``lazy_tool_use_ids``, plus the per-record ``detail_flow`` /
#: ``detail_version``. A body that clears this floor but does not pay for its
#: own markers still rides inline.
ELIDE_MIN_CHARS = ELIDE_HEAD_CHARS + 64

#: Field naming WHICH version of a record's held-back bodies the browser is
#: holding. ``(step_id, ordinal)`` is stable across a retry's in-place rewrite —
#: that is exactly what makes it a good address — so the same address can name a
#: different body after the daemon rewrites the line, and a rewrite that keeps
#: the visible summary byte-identical (same length, same line count, changed
#: past the kept head) would otherwise be invisible to the browser: its detail
#: cache would keep serving the previous attempt's body under the replacement
#: chip. The version is a digest of the bodies actually held back, so it moves
#: whenever what the detail endpoint would answer moves — and, because it rides
#: the summarized record, it also makes the rewritten record compare UNEQUAL in
#: the frontend's idempotent reconcile instead of being dropped as a duplicate.
DETAIL_VERSION_KEY = "detail_version"

#: Field saying, per entry of ``lazy_tool_use_ids``, WHICH of that call's two
#: bodies the shaping actually replaced: ``"u"`` its ``tool_use`` input, ``"r"``
#: its ``tool_result`` content, ``"b"`` both. One character per id, in the same
#: order.
#:
#: INVARIANT: the frontend rehydrates a stub only where this says one was put.
#: The id list alone names a CALL, and a call can have one body lazified while
#: the other rides inline (boundary rule (c) keeps a fold-visible input whole
#: while the envelope's result is still collapsed; a scalar input or an
#: unprofitable one rides inline the same way). ``__elided__`` is not reserved
#: in a tool's arguments — a live call may legitimately pass
#: ``{__elided__: true, head: "abc", lines: 2}`` — so a frontend that rehydrated
#: by SHAPE would rewrite that inline argument into synthetic text and change
#: the collapsed chip header. The mask is what makes "was this body replaced?"
#: a server statement rather than a guess.
LAZY_BODY_MASK_KEY = "lazy_body_mask"

#: The two mask characters, and their union.
LAZY_BODY_INPUT = "u"
LAZY_BODY_RESULT = "r"
LAZY_BODY_BOTH = "b"

#: How far past a marker budget a savings measurement bothers to look. Every
#: benefit-rule decision compares against a marker cost of at most a few hundred
#: bytes, so a value measured to be this much bigger is decided — and the walk
#: stops there rather than sizing a multi-MB body exactly.
_SAVING_PROBE = 1024

#: Which of the two detail sources a browser is asking for. A single
#: ``tool_use_id`` can appear in BOTH (the live stream_progress fragment and the
#: final assistant record's raw_json), and the two produce visibly different
#: panels — the daemon-built payload can show a pre-write diff the browser has
#: no way to reconstruct. The chip names its own source so the panel it opens is
#: the one it would have rendered inline; a source that holds nothing is
#: reported as unavailable rather than silently answered from the other one.
DETAIL_SOURCE_PROGRESS = "progress"
DETAIL_SOURCE_RAW = "raw"

#: The third detail source: a step event's held-back ``data.step.inputs``. It is
#: addressed by the record alone — a step event carries no ``tool_use_id`` — so
#: the endpoint takes an empty one for this source only.
DETAIL_SOURCE_STEP = "step"

#: The engine event types whose record carries a full StepState snapshot.
STEP_EVENT_TYPES = frozenset({"step_completed", "step_failed", "step_output"})

#: Marker saying this record's step snapshot is missing its ``inputs``. Placed
#: on the message holder beside ``detail_flow`` rather than inside the stubbed
#: object: the step event's whole "View raw" payload IS that message, so the
#: restore swaps the marked container for the original the endpoint returns and
#: the printed record comes back byte-identical.
STEP_INPUTS_LAZY_KEY = "step_inputs_lazy"

#: INVARIANT: every ``inputs`` key the DEFAULT step render reads, kept inline.
#: The rest of a step snapshot's inputs is the machine input handed TO the step
#: (a full ``scope_diff``, ``test_results``, ``fix_history``, and the task
#: description repeated under three names) — 10.8 MB against 38 KB of outputs on
#: the record that motivated this — and the report card reads NONE of it: it
#: renders ``outputs``' structured result fields plus status / error_message /
#: token_usage. What is listed here is what does reach the card:
#: ``fix_iteration`` / ``is_fix_iteration`` tell an implement fix round apart
#: from round one (``implementFixIteration``), and the confirm card falls back
#: to ``reviewer`` / ``step_to_review_*`` when the verdict dict omits them
#: (``renderConfirmReport``). All are small scalars, so keeping them costs
#: nothing the hold-back exists to save.
STEP_INPUT_INLINE_KEYS = frozenset({
    "fix_iteration",
    "is_fix_iteration",
    "reviewer",
    "step_to_review_type",
    "step_to_review_id",
})

#: Cap on how deep the elision walk descends into a tool input/result value.
#: Beyond this the value is passed through untouched — a pathological nesting is
#: not worth an unbounded walk, and the header never reads that deep.
_MAX_ELIDE_DEPTH = 32

#: Depth bound for the size ESTIMATOR below. INVARIANT: reaching it must read as
#: SMALL, never as large. The estimator's verdict is what benefit rule (b) is
#: decided on AND what measures a truncated list's kept head, and BOTH readings
#: fail in the same direction on an over-estimate: a tiny-but-deep value mis-read
#: as large would be replaced by a stub BIGGER than itself, and an over-measured
#: list head would be cut before the JSON prefix the collapsed header previews.
#: So the bound returns the smallest thing a container can serialize to. In
#: practice it is unreachable — every level costs at least two counted bytes, so
#: the byte budget it counts against runs out far sooner; it is a stack guard.
_MAX_WIRE_DEPTH = 64

#: How much of a truncated list's JSON head is kept inline. The widest window
#: any collapsed header takes on a NON-string tool input is the generic chip's
#: 30-character ``JSON.stringify`` preview, so keeping this much head puts the
#: point where the truncated list diverges from the original far beyond every
#: preview: the two serialize to the same leading characters, and the kept
#: prefix is still long enough to take the same "…" truncation the original
#: took. WHY a prefix rather than an opaque stub: an opaque stub would have to
#: reproduce ``JSON.stringify``'s output for an arbitrary value across two
#: languages; a genuine prefix of the same list needs no such agreement.
_LIST_HEAD_WIRE = ELIDE_HEAD_CHARS

#: INVARIANT: keys whose string value a chip header reads WHOLE or TAIL-first
#: are never elided, however long they are. The stub only preserves a value's
#: line count and its leading :data:`ELIDE_HEAD_CHARS` characters, which is
#: enough for every prefix-preview formatter but NOT for these: ``file_path`` /
#: ``path`` are rendered verbatim by the Read / Edit / Write / Grep / Glob
#: headers and middle-shortened tail-first by ``truncate_path`` for a generic
#: file tool, so a rehydrated stub would silently drop the very filename the
#: chip exists to name. Paths are bounded in size, so keeping them inline costs
#: nothing the split was made to save.
VERBATIM_INPUT_KEYS = frozenset({"file_path", "path"})


def elide_string(text: str) -> Dict[str, Any]:
    """Replace one oversize string with its header-equivalent stub."""
    return {
        ELIDED_KEY: True,
        "head": text[:ELIDE_HEAD_CHARS],
        "lines": text.count("\n") + 1,
        "chars": len(text),
    }


def _rough_wire_len(value: Any, cap: int, depth: int = 0, pad: int = 0) -> int:
    """Approximate *value*'s JSON size, stopping once *cap* is exceeded.

    WHY an approximation: the decisions this feeds are "is this bigger than the
    stub that would replace it" and "how much of a list head have we kept", and
    a real ``json.dumps`` of every tool result on every bundle response is a
    second full serialization of the heaviest part of the payload. Counting
    stops as soon as the answer is decided.

    INVARIANT: a scalar is priced at its ACTUAL compact-JSON width, never at a
    flat constant. Both readers fail the same way on an over-estimate — the
    benefit rule would hold back a body smaller than its own stub, and the list
    head would be cut before the JSON prefix the collapsed header previews. A
    flat 8 bytes per number made ``{"data":[0,1,2,…]}`` reach the 96-character
    head budget after eleven values whose real JSON is under 24 characters, so
    the shipped list diverged from the original inside the header's own 30-char
    preview window. Containers are still counted a byte or two long (a comma is
    charged for the last element too), which is the safe direction.

    *pad* prices the value under the SPACED encoder (``", "`` / ``": "``,
    which is what ``json.dumps`` writes by default) instead of the compact one
    the REST bundle uses. The two legs of the server→browser split do not agree
    on separators — the WebSocket relay renders with the default spacing, the
    REST response with ``separators=(",", ":")`` — so a replacement is priced
    at its WIDEST and the body it displaces at its NARROWEST. Anything that
    still shows a saving shrinks BOTH legs; anything that does not rides inline.
    INVARIANT: the list-head measurement in :func:`_elide_list` must keep
    ``pad=0``, since an over-measured head is cut before the JSON prefix the
    collapsed header previews.
    """
    if isinstance(value, str):
        return len(value) + 2
    if value is None:
        return 4
    if value is True:
        return 4
    if value is False:
        return 5
    if isinstance(value, (int, float)):
        return len(repr(value))
    if depth >= _MAX_WIRE_DEPTH:
        return 2
    total = 2
    if isinstance(value, dict):
        for key, item in value.items():
            total += len(str(key)) + 4 + 2 * pad
            total += _rough_wire_len(item, cap - total, depth + 1, pad)
            if total > cap:
                return total
        return total
    if isinstance(value, (list, tuple)):
        for item in value:
            total += _rough_wire_len(item, cap - total, depth + 1, pad) + 1 + pad
            if total > cap:
                return total
        return total
    # Anything else reaches the wire through the encoder's ``default=str``.
    return len(str(value)) + 2


def _wire_saving(original: Any, replacement: Any) -> int:
    """Wire bytes benefit rule (b) credits for shipping *replacement* instead.

    Never negative-by-omission: the replacement is sized first and the original
    is only measured until it is decidedly bigger, so a body that is not
    actually worth holding back reports a saving of zero or less and stays
    inline. The replacement is priced under the spaced encoder and the original
    under the compact one, so the verdict holds on whichever leg carries it.
    """
    replacement_wire = _rough_wire_len(replacement, _SAVING_PROBE, pad=1)
    return (
        _rough_wire_len(original, replacement_wire + _SAVING_PROBE)
        - replacement_wire
    )


def _lazy_id_cost(tool_use_id: str) -> int:
    """What naming *tool_use_id* in ``lazy_tool_use_ids`` costs on the wire.

    Benefit rule (b) is decided per body against the COMPLETE replacement cost,
    and a lazified call does not only swap its body for a stub — it also has to
    name itself so the browser knows to fetch it back. A 160-character Bash
    command clears the stub floor by a dozen bytes and then loses twice that to
    its own id, which is a body that must stay inline.

    Priced for the spaced encoder (``"<id>", ``) plus the one character this id
    contributes to :data:`LAZY_BODY_MASK_KEY`.
    """
    return len(tool_use_id) + 5


def _tail_stub(dropped: int) -> Dict[str, Any]:
    """Marker standing in for the list tail :func:`_elide_list` held back.

    Shaped like every other stub (:func:`elide_string`) so the frontend's one
    rehydration rule covers it: an empty head over a single line, which reads
    back as ``""``. That keeps the truncated list's JSON a genuine prefix of the
    original's — the property the header depends on — and keeps the value
    self-describing, so "View raw" can tell that something is missing and fetch
    the original instead of printing the prefix as if it were whole.
    """
    return {
        ELIDED_KEY: True,
        "head": "",
        "lines": 1,
        "chars": 0,
        "items": dropped,
    }


def _tail_wire_len(items: Sequence[Any], start: int, cap: int) -> int:
    """Approximate the JSON size of ``items[start:]``, stopping past *cap*.

    Sliced-free on purpose: the tail being measured can be a million elements
    long, and copying it to measure whether it is worth dropping would cost
    more than shipping it.
    """
    total = 0
    for index in range(start, len(items)):
        total += _rough_wire_len(items[index], cap - total) + 1
        if total > cap:
            return total
    return total


def _elide_list(
    value: list, depth: int, truncate: bool
) -> Tuple[Any, int]:
    """Elide inside a list, optionally holding its TAIL back.

    Returns ``(value, saved_bytes)``; a saving of 0 means nothing was replaced.

    WHY the tail goes at all: a per-string walk only ever shrinks a list made of
    individually-oversize strings. A million-element numeric array — or a list
    of ten thousand short strings — has no elidable leaf, so it used to ride
    whole even though the collapsed chip reads at most a 30-character
    ``JSON.stringify`` preview of it. Keeping :data:`_LIST_HEAD_WIRE` characters
    of head makes the shipped value a genuine JSON PREFIX of the original, so
    every header formatter reads exactly what it read before, and the rest is
    fetched on expand like any other body.

    The tail is kept whenever dropping it would not pay for the stub replacing
    it (benefit rule (b)), in which case the walk simply finishes normally.
    """
    kept: List[Any] = []
    saved = 0
    wire = 2
    cut = -1
    for index, item in enumerate(value):
        new_item, item_saved = _elide_value(item, depth + 1, truncate)
        kept.append(new_item)
        saved += item_saved
        if truncate and wire < _LIST_HEAD_WIRE:
            wire += _rough_wire_len(new_item, _LIST_HEAD_WIRE - wire) + 1
            if wire >= _LIST_HEAD_WIRE and index < len(value) - 1:
                cut = index
                break
    if cut < 0:
        return (kept, saved) if saved else (value, 0)
    stub = _tail_stub(len(value) - cut - 1)
    # Spaced encoder for the replacement, compact for the tail it displaces:
    # the saving has to hold on the WebSocket leg too (see _rough_wire_len).
    stub_wire = _rough_wire_len(stub, _SAVING_PROBE, pad=1) + 2
    tail_wire = _tail_wire_len(value, cut + 1, stub_wire + _SAVING_PROBE)
    if tail_wire <= stub_wire:
        for item in value[cut + 1:]:
            new_item, item_saved = _elide_value(item, depth + 1, truncate)
            kept.append(new_item)
            saved += item_saved
        return (kept, saved) if saved else (value, 0)
    kept.append(stub)
    return kept, saved + tail_wire - stub_wire


def _elide_value(
    value: Any, depth: int = 0, truncate_lists: bool = False
) -> Tuple[Any, int]:
    """Elide every oversize body in *value*.

    Returns ``(value, saved_bytes)``, where a saving of 0 means nothing was
    replaced. Containers are copied only when something below them actually
    changed, so an untouched payload is passed through by reference rather than
    deep-copied.

    Every leaf replacement is gated on benefit rule (b) individually: a string
    that clears the cheap :data:`ELIDE_MIN_CHARS` pre-filter but whose stub
    would not actually be smaller stays inline, so the walk can never grow the
    payload it is shrinking.

    *truncate_lists* enables the list-tail hold-back described in
    :func:`_elide_list`. It is opt-in because only a ``tool_use`` input is read
    purely as a header preview; a bare ``tool_result`` line's content doubles as
    narrative blocks (``extractAssistantText`` walks it element by element), so
    shortening that list would change what the bubble renders.
    """
    if isinstance(value, str):
        if len(value) < ELIDE_MIN_CHARS:
            return value, 0
        stub = elide_string(value)
        saved = _wire_saving(value, stub)
        return (stub, saved) if saved > 0 else (value, 0)
    if depth >= _MAX_ELIDE_DEPTH:
        return value, 0
    if isinstance(value, dict):
        saved = 0
        out: Dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(item, str) and key in VERBATIM_INPUT_KEYS:
                out[key] = item
                continue
            new_item, item_saved = _elide_value(item, depth + 1, truncate_lists)
            out[key] = new_item
            saved += item_saved
        return (out, saved) if saved else (value, 0)
    if isinstance(value, list):
        return _elide_list(value, depth, truncate_lists)
    return value, 0


def _extract_result_text(data: Any, depth: int = 0) -> str:
    """Python mirror of the frontend's ``_toolExtractText``.

    INVARIANT: this must agree with the JS walk exactly. It is what a collapsed
    header reads out of a ``tool_result`` content (its line count, and a 60-char
    preview for an unregistered tool), so the stub built from it renders the
    same header the un-summarized content would have.
    """
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    if isinstance(data, (list, tuple)):
        out: List[str] = []
        for item in data:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                out.append(item["text"])
            elif isinstance(item, str):
                out.append(item)
        return "\n".join(out)
    if isinstance(data, dict):
        if isinstance(data.get("text"), str):
            return data["text"]
        content = data.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list) and depth < _MAX_ELIDE_DEPTH:
            return _extract_result_text(content, depth + 1)
    return ""


def _collapse_result_content(content: Any) -> Tuple[Any, int]:
    """Replace a whole ``tool_result`` content with its header stub.

    WHY structural rather than per-string: the header reads the content ONLY
    through :func:`_extract_result_text`, so a result assembled from a hundred
    small text blocks has exactly the same header as the single string they
    join into — and holding back only the individually-oversize strings left
    that whole shape on the wire.
    """
    if content is None:
        return content, 0
    if _rough_wire_len(content, ELIDE_MIN_CHARS) <= ELIDE_MIN_CHARS:
        return content, 0
    stub = elide_string(_extract_result_text(content))
    saved = _wire_saving(content, stub)
    return (stub, saved) if saved > 0 else (content, 0)


# --------------------------------------------------------------------------
# raw_json traversal
# --------------------------------------------------------------------------
#
# Mirrors the frontend's ``extractAssistantChipEvents`` walk exactly: an NDJSON
# line is either a message envelope whose ``content`` is a block list, or a bare
# ``tool_use`` / ``tool_result`` line. Anything the frontend would not read as a
# chip block is left alone here too, so the two views can never disagree about
# which values are header material.


def _blocks_of_line(line: Any) -> Tuple[Optional[dict], Optional[list]]:
    """Return ``(message_dict, content_blocks)`` for one raw_json line."""
    if not isinstance(line, dict):
        return None, None
    msg = line.get("message")
    msg = msg if isinstance(msg, dict) else line
    content = msg.get("content")
    return msg, content if isinstance(content, list) else None


def _block_kind(block: Any) -> str:
    if not isinstance(block, dict):
        return ""
    return str(block.get("type") or "").lower()


def _result_id(block: dict) -> str:
    raw = block.get("tool_use_id")
    if raw is None:
        raw = block.get("toolUseId")
    return str(raw) if isinstance(raw, str) else ""


def _is_error_block(block: dict) -> bool:
    return block.get("is_error") is True or block.get("isError") is True


def iter_tool_blocks(raw_json: Any):
    """Yield ``(kind, block)`` for every tool_use / tool_result in *raw_json*.

    ``kind`` is ``"tool_use"`` or ``"tool_result"``. Emitted in stream order,
    exactly as the frontend walks it.
    """
    if not isinstance(raw_json, list):
        return
    for line in raw_json:
        if not isinstance(line, dict):
            continue
        line_type = str(line.get("type") or "").lower()
        if line_type in ("tool_use", "tool_result"):
            yield line_type, line
            continue
        _msg, blocks = _blocks_of_line(line)
        if blocks is None:
            continue
        for block in blocks:
            kind = _block_kind(block)
            if kind in ("tool_use", "tool_result"):
                yield kind, block


def _failed_tool_use_ids(raw_json: Any) -> set:
    """ids whose tool_result reported an error — exempt from elision."""
    failed = set()
    for kind, block in iter_tool_blocks(raw_json):
        if kind != "tool_result" or not _is_error_block(block):
            continue
        rid = _result_id(block)
        if rid:
            failed.add(rid)
    return failed


def _elide_raw_json(
    raw_json: Any, fold_visible: bool = False
) -> Tuple[Any, List[str], str, int]:
    """Elide the heavy bodies in *raw_json*.

    Returns ``(raw_json, ids, mask, saved)``.

    ``ids`` names the ``tool_use_id``s whose chip must now fetch its detail on
    demand — precisely those whose input or result actually lost text. A chip
    that lost nothing keeps deriving its detail locally for free. ``mask`` says,
    per id and in the same order, WHICH of that call's bodies was replaced (see
    :data:`LAZY_BODY_MASK_KEY`): a call can be lazy for its result while its
    input rides inline, and only the body that was actually stubbed may be
    rehydrated. ``saved`` is the wire benefit those replacements bought, already
    net of each id's own entry in ``lazy_tool_use_ids`` and its mask character.

    *fold_visible* says this record's own ``content`` is empty, so the bubble's
    FOLDED body is recovered from ``raw_json`` itself (``extractAssistantText``).
    That flips two shapes from "detail" to boundary-rule (c) fold-visible
    material, and both then ride inline:

    * a ``tool_use`` input — folded into the bubble as ``[Name: <JSON>]``, so a
      stub would put a synthetic prefix in the message body itself;
    * a BARE top-level ``tool_result`` line's content — walked block by block as
      narrative. Its text blocks are read only as STRINGS, so an elided one is
      not merely shortened but dropped outright, leaving the bubble empty.

    A ``tool_result`` inside a message envelope is never narrative
    (``extractAssistantText`` skips it: it is paired into a chip instead), so it
    is still collapsed here even when the record has no content of its own.
    """
    if not isinstance(raw_json, list) or not raw_json:
        return raw_json, [], "", 0
    failed = _failed_tool_use_ids(raw_json)
    lazy: List[str] = []
    lazy_bodies: Dict[str, str] = {}

    def mark(tool_id: str, body: str) -> None:
        if not tool_id:
            return
        held = lazy_bodies.get(tool_id)
        if held is None:
            lazy.append(tool_id)
            lazy_bodies[tool_id] = body
        elif held != body:
            lazy_bodies[tool_id] = LAZY_BODY_BOTH

    def shape_block(
        kind: str, block: dict, in_envelope: bool
    ) -> Tuple[dict, int]:
        if kind == "tool_use":
            tool_id = block.get("id")
            tool_id = str(tool_id) if isinstance(tool_id, str) else ""
            # An unpaired / unidentified call cannot be addressed by the detail
            # endpoint, so it must keep everything it needs inline.
            if not tool_id or tool_id in failed:
                return block, 0
            # Boundary rule (c): with no content of its own, this record's
            # FOLDED bubble prints this very input.
            if fold_visible:
                return block, 0
            tool_input = block.get("input")
            # Only a container input is lazifiable: the detail reply and the
            # "View raw" restore both hand an input back as JSON structure, so a
            # SCALAR input (a bare oversize string) has nothing they could put
            # back in its place and rides inline instead — boundary rule (a).
            if not isinstance(tool_input, (dict, list)):
                return block, 0
            # List-tail hold-back is enabled only for a dict input: an input
            # that IS a list is enumerated key-by-key by the generic header
            # (``Object.keys``), so dropping its tail could drop one of the
            # three entries that header prints.
            new_input, saved = _elide_value(
                tool_input, truncate_lists=isinstance(tool_input, dict)
            )
            saved -= _lazy_id_cost(tool_id)
            if saved <= 0:
                return block, 0
            out = dict(block)
            out["input"] = new_input
            mark(tool_id, LAZY_BODY_INPUT)
            return out, saved
        tool_id = _result_id(block)
        if not tool_id or tool_id in failed:
            return block, 0
        content = block.get("content")
        if in_envelope:
            # A block inside a message envelope is skipped wholesale by
            # ``extractAssistantText`` (tool_result text is never folded into
            # the narrative — it is paired into a chip instead), so replacing
            # the whole content is invisible outside the chip header.
            new_content, saved = _collapse_result_content(content)
        elif fold_visible:
            # Boundary rule (c): a BARE top-level tool_result on a record with
            # no content of its own IS the bubble's folded body.
            return block, 0
        else:
            new_content, saved = _elide_value(content)
        saved -= _lazy_id_cost(tool_id)
        if saved <= 0:
            return block, 0
        out = dict(block)
        out["content"] = new_content
        mark(tool_id, LAZY_BODY_RESULT)
        return out, saved

    out_lines: List[Any] = []
    total_saved = 0
    for line in raw_json:
        if not isinstance(line, dict):
            out_lines.append(line)
            continue
        line_type = str(line.get("type") or "").lower()
        if line_type in ("tool_use", "tool_result"):
            new_line, saved = shape_block(line_type, line, False)
            out_lines.append(new_line)
            total_saved += saved
            continue
        msg, blocks = _blocks_of_line(line)
        if blocks is None:
            out_lines.append(line)
            continue
        new_blocks: List[Any] = []
        blocks_saved = 0
        for block in blocks:
            kind = _block_kind(block)
            if kind not in ("tool_use", "tool_result"):
                new_blocks.append(block)
                continue
            new_block, saved = shape_block(kind, block, True)
            new_blocks.append(new_block)
            blocks_saved += saved
        if not blocks_saved:
            out_lines.append(line)
            continue
        total_saved += blocks_saved
        new_msg = dict(msg)
        new_msg["content"] = new_blocks
        if msg is line:
            out_lines.append(new_msg)
        else:
            new_line = dict(line)
            new_line["message"] = new_msg
            out_lines.append(new_line)
    if not lazy:
        return raw_json, [], "", 0
    return out_lines, lazy, "".join(lazy_bodies[i] for i in lazy), total_saved


def _feed_digest(hasher: Any, value: Any, depth: int = 0) -> None:
    """Stream *value*'s content into *hasher* without serializing it first.

    A ``json.dumps`` here would build a second full copy of exactly the bodies
    the split exists to keep OFF the wire, so the walk feeds the encoder
    incrementally instead. Only stability matters, not JSON fidelity: the digest
    is compared against itself across deliveries, never parsed.
    """
    if isinstance(value, str):
        hasher.update(b"s")
        hasher.update(value.encode("utf-8", "replace"))
        return
    if value is None or isinstance(value, (bool, int, float)):
        hasher.update(repr(value).encode("ascii", "replace"))
        return
    if depth >= _MAX_ELIDE_DEPTH:
        return
    if isinstance(value, dict):
        hasher.update(b"{")
        for key, item in value.items():
            hasher.update(str(key).encode("utf-8", "replace"))
            hasher.update(b":")
            _feed_digest(hasher, item, depth + 1)
            hasher.update(b",")
        hasher.update(b"}")
        return
    if isinstance(value, (list, tuple)):
        hasher.update(b"[")
        for item in value:
            _feed_digest(hasher, item, depth + 1)
            hasher.update(b",")
        hasher.update(b"]")
        return
    hasher.update(str(value).encode("utf-8", "replace"))


def _detail_version(
    raw_json: Any,
    lazy_ids: Sequence[str],
    progress_detail: Any,
    step_inputs: Any = None,
) -> str:
    """A digest of exactly the bodies this record is holding back.

    See :data:`DETAIL_VERSION_KEY`: the browser's detail cache is keyed on the
    record's ADDRESS, which a retry's in-place rewrite deliberately preserves,
    so the address alone cannot tell a replacement body from the one already
    cached. Digesting the ORIGINALS (not the stubs, which a same-length rewrite
    leaves byte-identical) is what makes the two compare unequal.
    """
    hasher = hashlib.blake2b(digest_size=6)
    if step_inputs is not None:
        hasher.update(b"i")
        _feed_digest(hasher, step_inputs)
    if progress_detail is not None:
        hasher.update(b"p")
        _feed_digest(hasher, progress_detail)
    if lazy_ids:
        wanted = set(lazy_ids)
        for kind, block in iter_tool_blocks(raw_json):
            if kind == "tool_use":
                block_id = block.get("id")
                if isinstance(block_id, str) and block_id in wanted:
                    hasher.update(b"u")
                    _feed_digest(hasher, block.get("input"))
                continue
            if _result_id(block) in wanted:
                hasher.update(b"r")
                _feed_digest(hasher, block.get("content"))
    return hasher.hexdigest()


# --------------------------------------------------------------------------
# record shaping
# --------------------------------------------------------------------------


def _holder_of(record: dict) -> Tuple[dict, bool]:
    """Return ``(field_holder, is_message_envelope)`` for one record.

    Mirrors ``normalizeRecord``: fields live on ``record["message"]`` when the
    envelope is present, and on the record itself otherwise.
    """
    msg = record.get("message")
    if isinstance(msg, dict):
        return msg, True
    return record, False


def _owner_of(record: dict, key: str) -> Optional[dict]:
    """Which container of *record* actually holds *key*.

    Mirrors ``normalizeRecord``'s ``pick``: message-first with an envelope
    fallback. INVARIANT: shaping and extraction MUST resolve a field through
    this one lookup — a wrapped / version-skewed record carrying its tool
    fields on the outer envelope is otherwise summarized in one container and
    read back from the other, so the browser gets a valid lazy marker whose
    detail request then 404s on a body the cache is holding.
    """
    message, has_envelope = _holder_of(record)
    if message.get(key) is not None:
        return message
    if has_envelope and record.get(key) is not None:
        return record
    return None


def _pick(record: dict, key: str) -> Any:
    """The value of *key* under the message-first, envelope-fallback lookup."""
    owner = _owner_of(record, key)
    return owner.get(key) if owner is not None else None


def _record_ordinal(record: dict) -> Optional[int]:
    """The record's 0-based physical line number, or ``None``.

    Mirrors the frontend's ``recordOrdinal``: ENVELOPE-first (that is where the
    daemon history reader injects it), with ``message.ordinal`` only as a
    defensive fallback for an already-unwrapped shape.
    """
    value = record.get("ordinal")
    if value is None:
        message = record.get("message")
        if isinstance(message, dict):
            value = message.get("ordinal")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def record_address(record: Any) -> Optional[Tuple[str, int]]:
    """The record's stable ``(step_id, ordinal)`` identity, or ``None``.

    This is the address the detail endpoint is called with. WHY it is required
    for lazification: ``tool_use_id`` is unique only within a record — codex
    synthesizes ``codex_tool_1`` per call, so two steps of one flow can each
    hold that id, and answering a chip from a flow-wide scan would hand the
    first chip the second call's body. A record the address cannot name (an
    optimistic local echo, a pre-ordinal daemon) keeps its bodies inline.
    """
    if not isinstance(record, dict):
        return None
    ordinal = _record_ordinal(record)
    if ordinal is None:
        return None
    step_id = _pick(record, "step_id")
    if not isinstance(step_id, str) or not step_id:
        return None
    return step_id, ordinal


#: What the per-record ``"step_inputs_lazy": true, `` marker costs on the wire,
#: under the spaced encoder (see :func:`_rough_wire_len`).
_STEP_INPUTS_MARKER_COST = len('"step_inputs_lazy": true, ')


def _step_inputs_target(record: dict):
    """Locate a step event's effective ``inputs`` and how to replace it.

    Returns ``(inputs, rebuild)`` — where ``rebuild(new_inputs)`` yields the
    single ``(container, key, value)`` edit that puts *new_inputs* in place — or
    ``None`` when this record is not a step event carrying a dict ``inputs``.

    INVARIANT: the resolution order mirrors ``normalizeRecord``'s step-event
    branch exactly (``data.step.inputs`` → ``message.step.inputs`` →
    ``data.inputs`` → the record's own ``inputs``). Shaping one container while
    the browser reads another would hand it a record still carrying the megabyte
    it was told had been held back, and a detail request for a body nothing
    took.
    """
    event_type = _pick(record, "type")
    if not isinstance(event_type, str):
        return None
    if event_type.lower() not in STEP_EVENT_TYPES:
        return None
    message, _has_envelope = _holder_of(record)
    data_owner = _owner_of(record, "data")
    data = data_owner.get("data") if data_owner is not None else None
    if not isinstance(data, dict):
        data = None
        data_owner = None

    inner_step = None
    inner_owner = None
    if data is not None and isinstance(data.get("step"), dict):
        inner_step, inner_owner = data["step"], "data"
    elif isinstance(message.get("step"), dict):
        inner_step, inner_owner = message["step"], "message"

    if inner_step is not None and isinstance(inner_step.get("inputs"), dict):
        inputs = inner_step["inputs"]
        if inner_owner == "data":
            def rebuild(new_inputs, _data=data, _step=inner_step,
                        _owner=data_owner):
                new_step = dict(_step)
                new_step["inputs"] = new_inputs
                new_data = dict(_data)
                new_data["step"] = new_step
                return _owner, "data", new_data
        else:
            def rebuild(new_inputs, _step=inner_step, _owner=message):
                new_step = dict(_step)
                new_step["inputs"] = new_inputs
                return _owner, "step", new_step
        return inputs, rebuild

    if data is not None and isinstance(data.get("inputs"), dict):
        inputs = data["inputs"]

        def rebuild(new_inputs, _data=data, _owner=data_owner):
            new_data = dict(_data)
            new_data["inputs"] = new_inputs
            return _owner, "data", new_data

        return inputs, rebuild

    owner = _owner_of(record, "inputs")
    if owner is not None and isinstance(owner.get("inputs"), dict):
        inputs = owner["inputs"]

        def rebuild(new_inputs, _owner=owner):
            return _owner, "inputs", new_inputs

        return inputs, rebuild
    return None


def _stub_step_inputs(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """What of a step snapshot's ``inputs`` still rides inline.

    An allowlist rather than a size filter: which keys the card reads is a fixed
    property of the renderers (:data:`STEP_INPUT_INLINE_KEYS`), and a
    size-driven rule would drop a small key the card DOES read as soon as some
    record happened to make it big.
    """
    return {k: v for k, v in inputs.items() if k in STEP_INPUT_INLINE_KEYS}


def _progress_detail_saving(detail: Dict[str, Any]) -> int:
    """Wire bytes dropping *detail* buys, before the per-record markers.

    Benefit rule (b): dropping ``tool_detail`` is not free — the record gains a
    ``tool_detail_lazy`` marker in its place (and, together with the raw path,
    the per-record ``detail_flow`` / ``detail_version`` the caller charges
    separately). A payload smaller than what replaces it would GROW the response
    and buy the browser a request for something it already had, so it rides
    inline instead.
    """
    kept = len('"tool_detail":') + 1 + _rough_wire_len(detail, _SAVING_PROBE)
    return kept - len('"tool_detail_lazy": true, ')


#: Width of the :func:`_detail_version` digest, in hex characters.
_DETAIL_VERSION_CHARS = 12


def _record_marker_cost(flow_id: str) -> int:
    """What the two per-record markers cost once ANY body is held back.

    ``"detail_flow": "<flow>", `` and ``"detail_version": "<12 hex>", `` — the
    flow id at its ACTUAL length, under the spaced encoder. Pricing the value as
    an empty string was worth ~26 bytes on a 24-character flow id, which is
    enough to turn a marginal record from a saving into a GROWTH plus an
    on-demand request for a body the browser already had.
    """
    return (
        len('"detail_flow": "", ') + len(flow_id or "")
        + len('"detail_version": "", ') + _DETAIL_VERSION_CHARS
    )


#: What the two lazy-id arrays cost beyond their per-id entries (which
#: :func:`_lazy_id_cost` already charged, one id plus its mask character each):
#: ``"lazy_tool_use_ids": [], `` and ``"lazy_body_mask": "", ``.
_LAZY_IDS_FRAME = (
    len('"lazy_tool_use_ids": [], ')
    + len('"lazy_body_mask": "", ')
)


def _summarize_record(record: Any, flow_id: str) -> Any:
    if not isinstance(record, dict):
        return record
    # INVARIANT: a record the detail endpoint cannot address is never lazified —
    # the browser would be handed a marker for a body nothing can fetch back.
    if record_address(record) is None:
        return record
    message, has_envelope = _holder_of(record)

    def owner_of(key: str) -> Optional[dict]:
        return _owner_of(record, key)

    # Per-container edits, applied together at the end so a record is copied at
    # most once per container.
    updates: Dict[int, Dict[str, Any]] = {}
    drops: Dict[int, List[str]] = {}

    def edit(container: dict, key: str, value: Any) -> None:
        updates.setdefault(id(container), {})[key] = value

    def drop(container: dict, key: str) -> None:
        drops.setdefault(id(container), []).append(key)

    # Boundary rule (c): with no ``content`` of its own, this record's FOLDED
    # bubble body is recovered from ``raw_json`` (``normalizeRecord`` falls
    # through to ``extractAssistantText``), which turns several raw bodies from
    # detail into fold-visible material. Resolved through the same
    # message-first / envelope-fallback lookup the frontend uses.
    own_content = _pick(record, "content")
    fold_visible = not (isinstance(own_content, str) and own_content != "")

    # (1) stream_progress: the pre-built detail payload is pure detail.
    detail_holder = owner_of("tool_detail")
    id_holder = owner_of("tool_use_id")
    tool_use_id = id_holder.get("tool_use_id") if id_holder else None
    error_holder = owner_of("is_error")
    is_error = error_holder.get("is_error") if error_holder else None
    progress_detail: Any = None
    progress_saved = 0
    if (
        detail_holder is not None
        and isinstance(detail_holder.get("tool_detail"), dict)
        and isinstance(tool_use_id, str)
        and tool_use_id
        # A failed call's chip auto-expands (``upgradeChipToFailure``); holding
        # its body back would make a failure-heavy session open with a burst of
        # on-demand requests — the exact cost this change exists to remove.
        and is_error is not True
    ):
        progress_detail = detail_holder["tool_detail"]
        progress_saved = _progress_detail_saving(progress_detail)

    # (2) assistant/user raw_json: header material stays, bodies are elided.
    raw_holder = owner_of("raw_json")
    raw_json = raw_holder.get("raw_json") if raw_holder is not None else None
    new_raw: Any = None
    lazy_ids: List[str] = []
    lazy_mask = ""
    raw_saved = 0
    if isinstance(raw_json, list):
        new_raw, lazy_ids, lazy_mask, raw_saved = _elide_raw_json(
            raw_json, fold_visible
        )
        if lazy_ids:
            raw_saved -= _LAZY_IDS_FRAME
        else:
            raw_saved = 0

    # (3) step events: the StepState snapshot's ``inputs`` is the machine input
    # handed TO the step, not its conclusion, and the default render reads only
    # the handful of scalars :data:`STEP_INPUT_INLINE_KEYS` names. On a
    # check-class step it is where a whole ``scope_diff`` lives — megabytes of
    # payload the report card never touches, inlined on every delivery.
    step_target = _step_inputs_target(record)
    step_inputs: Any = None
    new_step_inputs: Any = None
    step_rebuild = None
    step_saved = 0
    if step_target is not None:
        step_inputs, step_rebuild = step_target
        new_step_inputs = _stub_step_inputs(step_inputs)
        step_saved = (
            _wire_saving(step_inputs, new_step_inputs) - _STEP_INPUTS_MARKER_COST
        )

    # Benefit rule (b), decided on the COMPLETE replacement cost: the candidates
    # share the per-record markers, so whichever combination actually shrinks
    # the response wins and an unprofitable one is simply not taken.
    best = 0
    use_progress = use_raw = use_step = False
    for want_progress in (True, False):
        for want_raw in (True, False):
            for want_step in (True, False):
                if not (want_progress or want_raw or want_step):
                    continue
                if want_progress and progress_saved <= 0:
                    continue
                if want_raw and raw_saved <= 0:
                    continue
                if want_step and step_saved <= 0:
                    continue
                net = (progress_saved if want_progress else 0)
                net += raw_saved if want_raw else 0
                net += step_saved if want_step else 0
                net -= _record_marker_cost(str(flow_id or ""))
                if net > best:
                    best = net
                    use_progress, use_raw, use_step = (
                        want_progress, want_raw, want_step
                    )
    if not use_progress and not use_raw and not use_step:
        return record

    if use_progress:
        drop(detail_holder, "tool_detail")
        edit(detail_holder, "tool_detail_lazy", True)
    if use_raw:
        edit(raw_holder, "raw_json", new_raw)
        edit(raw_holder, "lazy_tool_use_ids", lazy_ids)
        edit(raw_holder, LAZY_BODY_MASK_KEY, lazy_mask)
    if use_step:
        container, key, value = step_rebuild(new_step_inputs)
        edit(container, key, value)
        edit(message, STEP_INPUTS_LAZY_KEY, True)

    # The browser addresses a lazy detail by (flow_id, step_id, ordinal,
    # tool_use_id); step_id and ordinal already ride the record's envelope, so
    # only the flow id has to be added. Carrying it on the record keeps the
    # renderer free of view-scoped state, which matters because the SAME records
    # render in both the running-flow console and the history detail pane. The
    # version rides beside it so a retry that rewrites this line under the same
    # address cannot be answered out of the browser's cache (see
    # :data:`DETAIL_VERSION_KEY`).
    edit(message, "detail_flow", str(flow_id or ""))
    edit(
        message,
        DETAIL_VERSION_KEY,
        _detail_version(
            raw_json if use_raw else None,
            lazy_ids if use_raw else (),
            progress_detail if use_progress else None,
            step_inputs if use_step else None,
        ),
    )

    def rebuilt(container: dict) -> dict:
        removed = drops.get(id(container), ())
        out = {k: v for k, v in container.items() if k not in removed}
        out.update(updates.get(id(container), {}))
        return out

    touched = set(updates) | set(drops)
    new_message = rebuilt(message) if id(message) in touched else message
    if not has_envelope:
        return new_message
    new_record = rebuilt(record) if id(record) in touched else dict(record)
    new_record["message"] = new_message
    return new_record


def summarize_history_records(records: Any, flow_id: str) -> Any:
    """Shape a bundle slice for the browser: headers inline, bodies on demand.

    Returns *records* unchanged (same object) when nothing needed shaping, so a
    detail-free bundle costs one pass and no allocation.

    Every record of the slice is shaped the same way. WHY there is no per-record
    escape hatch: the caller's summarize/whole verdict is taken ONCE per
    server→browser frame from the mechanism that produced it (a REST bundle
    response or a replay frame is shaped; a live tail append never reaches
    here), so a slice is either wholly replay or wholly live. A per-record split
    only ever came from asking a record's own naive local ``timestamp`` whether
    it predated some browser's subscription — a question that clock skew, a
    differing timezone and a lagging push loop all answer wrong, and that made
    one frame leave the server in several shapes.
    """
    if not isinstance(records, list) or not records:
        return records
    out: List[Any] = []
    changed = False
    for record in records:
        shaped = _summarize_record(record, flow_id)
        if shaped is not record:
            changed = True
        out.append(shaped)
    return out if changed else records


# --------------------------------------------------------------------------
# detail extraction (server side of the on-demand fetch)
# --------------------------------------------------------------------------


def _raw_pair_detail(raw_json: Any, tool_use_id: str) -> Optional[Dict[str, Any]]:
    """Rebuild one chip's raw tool_use / tool_result pair from *raw_json*.

    The blocks travel back UNCHANGED rather than as a rendered payload: the
    browser already owns the per-tool detail formatters (the JS mirror of
    ``build_tool_detail_payload``) and running them on the un-elided input /
    result is what makes the expanded panel identical to the un-summarized one,
    with no second implementation to drift.
    """
    found = False
    tool_name = "Tool"
    tool_input: Any = None
    result: Any = None
    is_error: Optional[bool] = None
    for kind, block in iter_tool_blocks(raw_json):
        if kind == "tool_use":
            block_id = block.get("id")
            if isinstance(block_id, str) and block_id == tool_use_id:
                found = True
                name = block.get("name")
                tool_name = name if isinstance(name, str) and name else "Tool"
                tool_input = block.get("input")
        elif _result_id(block) == tool_use_id:
            found = True
            result = block.get("content")
            is_error = _is_error_block(block)
    if not found:
        return None
    if is_error is True:
        status = "failure"
    elif is_error is False:
        status = "success"
    else:
        status = "in-flight"
    return {
        "source": DETAIL_SOURCE_RAW,
        "tool_name": tool_name,
        # A LIST input travels back as a list, not as ``{}``: the browser's
        # ``_toolInputPayload`` enumerates it with ``Object.keys`` (an array IS
        # an object there), so the un-summarized panel printed its entries.
        # Answering with an empty object would render an argument-less panel for
        # a body the shaping held back — the one shape that must stay identical.
        # Only a value the shaping never lazifies (a scalar) can reach the else
        # branch, and the browser normalizes that to ``{}`` anyway.
        "input": tool_input if isinstance(tool_input, (dict, list)) else {},
        "result": result,
        "status": status,
    }


def _step_event_detail(record: dict) -> Optional[Dict[str, Any]]:
    """One step event's held-back payload, straight out of the cached record.

    ``record`` is the message holder as the daemon delivered it, so the browser
    can print the "View raw" payload byte-identically by swapping the marked
    container for this one — no second server-side renderer to drift from, the
    same argument that makes :func:`_raw_pair_detail` return blocks rather than
    a rendered panel. ``inputs`` rides beside it so a consumer that only wants
    the snapshot's machine input does not have to re-walk the record.
    """
    target = _step_inputs_target(record)
    if target is None:
        return None
    inputs, _rebuild = target
    message, _has_envelope = _holder_of(record)
    return {
        "source": DETAIL_SOURCE_STEP,
        "record": message,
        "inputs": inputs,
    }


def _detail_of_record(
    record: dict, tool_use_id: str, source: str
) -> Optional[Dict[str, Any]]:
    """One record's detail for *tool_use_id* in the REQUESTED source only.

    WHY no cross-source fallback: the two sources describe the same call from
    different records and render visibly different panels — a daemon-built
    ``stream_progress`` payload can carry a pre-write diff the browser cannot
    reconstruct, while the raw pair rebuilds a full-content panel. Answering a
    ``progress`` request out of ``raw_json`` would silently show the user a
    different panel than the chip promised, so a missing source is reported as
    unavailable instead.
    """
    if source == DETAIL_SOURCE_STEP:
        return _step_event_detail(record)
    if source == DETAIL_SOURCE_RAW:
        raw_json = _pick(record, "raw_json")
        if not isinstance(raw_json, list) or not raw_json:
            return None
        return _raw_pair_detail(raw_json, tool_use_id)
    if _pick(record, "tool_use_id") != tool_use_id:
        return None
    detail = _pick(record, "tool_detail")
    if not isinstance(detail, dict):
        return None
    return {
        "source": DETAIL_SOURCE_PROGRESS,
        "detail": detail,
        "is_error": _pick(record, "is_error"),
    }


def locate_record_detail(
    records: Sequence[Any],
    *,
    step_id: str,
    ordinal: int,
    tool_use_id: str,
    source: str = DETAIL_SOURCE_PROGRESS,
) -> Dict[str, Any]:
    """Find ONE addressed message's chip detail in a cached bundle.

    Returns ``{"detail", "record_found", "passed"}``:

    * ``detail`` — the payload, or ``None`` when this record holds no such call
      in the requested source;
    * ``record_found`` — the addressed record IS in the bundle, so its answer is
      authoritative (no amount of waiting will change it);
    * ``passed`` — the bundle already holds a HIGHER ordinal for the same step.
      A daemon streams a step's lines in ascending order, so a higher ordinal
      having arrived means the addressed line was read past and skipped (a
      blank / unparseable line — the "unfillable" verdict the cursor machinery
      draws for interior holes). It will never arrive, which is what lets the
      detail route stop waiting on a multi-frame recovery without guessing.

    Both flags are ``False`` while a record is merely LATE (still in a tail the
    daemon has not sent), which is the case the route must keep waiting on.

    Every field is resolved through :func:`_owner_of`, the SAME message-first /
    envelope-fallback lookup the shaping and the frontend use, so a record whose
    tool fields sit on the outer envelope is read back from where it was
    summarized rather than reading as absent.
    """
    out: Dict[str, Any] = {"detail": None, "record_found": False, "passed": False}
    if not isinstance(records, (list, tuple)):
        return out
    # A step event carries no tool call, so its address is the whole request;
    # every other source names one call inside the addressed record.
    if not tool_use_id and source != DETAIL_SOURCE_STEP:
        return out
    if not step_id or isinstance(ordinal, bool) or not isinstance(ordinal, int):
        return out
    for record in records:
        if not isinstance(record, dict):
            continue
        address = record_address(record)
        if address is None or address[0] != step_id:
            continue
        if address[1] != ordinal:
            if address[1] > ordinal:
                out["passed"] = True
            continue
        out["record_found"] = True
        detail = _detail_of_record(record, tool_use_id, source)
        # An ordinal normally appears once (the cache reconciles a retry's
        # rewrite in place by step_id#ordinal). Should a duplicate survive, a
        # copy that HOLDS the body wins over one that does not — the browser
        # asked for a body, not for the newest empty container.
        if detail is not None:
            out["detail"] = detail
    return out
