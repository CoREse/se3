"""Lazy tool-call details on the server→browser history leg.

Opening a long session used to download every tool call's whole body — a
Write's file content, a Bash/Read output, a large ``tool_result`` — even though
the console renders every successful chip COLLAPSED. The bundle response now
ships collapsed-state fields only and the bodies are fetched on expand through
``GET /api/history/{flow_id}/detail``.

Two things are load-bearing and asserted here:

* the collapsed chip must stay byte-identical, so everything the header reads
  (the bracket-marker ``content``, the tool_use ``input``'s line counts and
  leading characters, the ``tool_result``'s line count) survives the shaping;
* the daemon→server leg is untouched — the cache still holds the full bundle,
  which is what the on-demand endpoint reads back.

Failed calls are exempt end to end: their chip auto-expands, so holding their
body back would turn a failure-heavy session into a burst of on-demand requests
at load time. Their detail is asserted to ride inline, unshaped.
"""

from __future__ import annotations

import json
import time

import pytest

from _authsrv import authed_app, authed_hello, login
from tianluo.daemon import protocol
from tianluo.daemon.history import MAX_BYTES_PER_REPORT
from tianluo.server.history_summary import (
    DETAIL_SOURCE_PROGRESS,
    DETAIL_SOURCE_RAW,
    ELIDED_KEY,
    ELIDE_HEAD_CHARS,
    ELIDE_MIN_CHARS,
    LAZY_BODY_MASK_KEY,
    locate_record_detail,
    record_address,
    summarize_history_records,
)

FLOW = "flow-lazy-detail"
MACHINE = "m1"
STEP = "01_implement_abcd1234"

BIG_TEXT = "\n".join("line %04d of a very long tool output" % i for i in range(400))
BIG_FILE = "\n".join("def f%03d():\n    return %d" % (i, i) for i in range(300))


def _detail(records, ordinal, tool_use_id, source=DETAIL_SOURCE_PROGRESS,
            step_id=STEP):
    """The endpoint's lookup for ONE addressed record, detail only."""
    return locate_record_detail(
        records,
        step_id=step_id,
        ordinal=ordinal,
        tool_use_id=tool_use_id,
        source=source,
    )["detail"]



# --------------------------------------------------------------------------
# record builders
# --------------------------------------------------------------------------


def _live_stamp(offset=0.0):
    """The ``timestamp`` a record written *offset* seconds from now would carry.

    Records stamp their own creation the way the engine does — a naive local
    ``datetime.now().isoformat()`` — and that stamp is what tells a delayed
    pre-subscription backlog line apart from a genuine post-subscription tail
    append. A builder default in the fixed past therefore means "written before
    this test's browser subscribed"; this is how a test says "written after".
    """
    from datetime import datetime, timedelta

    return (datetime.now() + timedelta(seconds=offset)).isoformat()


def _progress(ordinal, tool_use_id, content, *, detail, is_error=None,
              timestamp="2026-08-31T00:00:00"):
    message = {
        "type": "stream_progress",
        "role": "assistant",
        "partial": True,
        "content": content,
        "timestamp": timestamp,
        "tool_use_id": tool_use_id,
        "tool_detail": detail,
    }
    if is_error is not None:
        message["is_error"] = is_error
    return {
        "step_id": STEP,
        "step_type": "implement",
        "ordinal": ordinal,
        "message": message,
    }


def _assistant(ordinal, blocks, content="done",
               timestamp="2026-08-31T00:00:01"):
    """A normal assistant record: it carries its own rendered ``content``.

    WHY the default is non-empty: a record with NO content of its own has its
    folded bubble recovered from ``raw_json`` itself, which makes the tool
    bodies inside it boundary-rule (c) fold-visible material that must ride
    inline. That case has its own tests; this builder is the ordinary shape.
    """
    return {
        "step_id": STEP,
        "step_type": "implement",
        "ordinal": ordinal,
        "message": {
            "role": "assistant",
            "content": content,
            "timestamp": timestamp,
            "raw_json": [
                {"type": "assistant", "message": {"content": blocks}},
            ],
        },
    }


def _tool_use(tool_id, name, tool_input):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}


def _tool_result(tool_id, content, is_error=False):
    block = {"type": "tool_result", "tool_use_id": tool_id, "content": content}
    if is_error:
        block["is_error"] = True
    return block


# --------------------------------------------------------------------------
# pure shaping
# --------------------------------------------------------------------------


def test_stream_progress_detail_is_stripped_and_marked():
    detail = {"kind": "read_text", "file_path": "a.py", "text": BIG_TEXT}
    rec = _progress(0, "tu_1", "[Read ✓ a.py · 400 lines]", detail=detail,
                    is_error=False)
    out = summarize_history_records([rec], FLOW)
    msg = out[0]["message"]
    assert "tool_detail" not in msg
    assert msg["tool_detail_lazy"] is True
    assert msg["detail_flow"] == FLOW
    # Everything the collapsed chip renders from is untouched.
    for key in ("content", "tool_use_id", "is_error", "timestamp", "role", "type"):
        assert msg[key] == rec["message"][key]
    # The source record was NOT mutated — the cache still holds the full body.
    assert rec["message"]["tool_detail"] is detail


def test_failed_stream_progress_detail_rides_inline():
    detail = {"kind": "text", "text": BIG_TEXT}
    rec = _progress(0, "tu_err", "[Bash ✗ boom]", detail=detail, is_error=True)
    out = summarize_history_records([rec], FLOW)
    assert out is [rec] or out[0] is rec
    assert out[0]["message"]["tool_detail"] == detail
    assert "tool_detail_lazy" not in out[0]["message"]
    assert "detail_flow" not in out[0]["message"]


def test_detail_free_records_pass_through_by_reference():
    plain = {
        "step_id": STEP,
        "ordinal": 0,
        "message": {"role": "user", "content": "hello"},
    }
    records = [plain]
    assert summarize_history_records(records, FLOW) is records


def test_raw_json_elision_keeps_every_header_input():
    long_content = BIG_FILE
    short_command = "pytest -q tests/server"
    blocks = [
        _tool_use("tu_w", "Write", {"file_path": "x.py", "content": long_content}),
        _tool_result("tu_w", "ok"),
        _tool_use("tu_b", "Bash", {"command": short_command}),
        _tool_result("tu_b", BIG_TEXT),
    ]
    rec = _assistant(0, blocks)
    out = summarize_history_records([rec], FLOW)
    msg = out[0]["message"]
    assert sorted(msg["lazy_tool_use_ids"]) == ["tu_b", "tu_w"]
    assert msg["detail_flow"] == FLOW
    shaped = msg["raw_json"][0]["message"]["content"]

    stub = shaped[0]["input"]["content"]
    assert stub[ELIDED_KEY] is True
    # INVARIANT: the stub preserves exactly what a header formatter reads.
    assert stub["lines"] == long_content.count("\n") + 1
    assert stub["head"] == long_content[:ELIDE_HEAD_CHARS]
    assert stub["chars"] == len(long_content)
    # file_path is header material and short — it is never touched.
    assert shaped[0]["input"]["file_path"] == "x.py"
    # A short command stays a plain string; a big result is elided.
    assert shaped[2]["input"]["command"] == short_command
    assert shaped[3]["content"][ELIDED_KEY] is True
    # A small result is left alone (nothing to gain, and the header reads it).
    assert shaped[1]["content"] == "ok"
    # The cached record is untouched.
    assert rec["message"]["raw_json"][0]["message"]["content"][0]["input"][
        "content"
    ] == long_content


def test_a_structured_result_is_collapsed_whole():
    """A result made of many SMALL blocks is what a long session downloads.

    Its header reads the content only through ``_toolExtractText`` (a line count
    plus, for an unregistered tool, a 60-char preview), so the whole block list
    collapses into ONE stub built from that same extracted text. Eliding it
    string by string used to leave every block on the wire, because no single
    one crossed the floor.
    """
    blocks_text = ["chunk %03d of a paginated tool result" % i for i in range(60)]
    content = [{"type": "text", "text": t} for t in blocks_text]
    assert all(len(t) < ELIDE_MIN_CHARS for t in blocks_text)
    rec = _assistant(0, [
        _tool_use("tu_s", "Bash", {"command": "ls"}),
        _tool_result("tu_s", content),
    ])
    out = summarize_history_records([rec], FLOW)
    msg = out[0]["message"]
    assert msg["lazy_tool_use_ids"] == ["tu_s"]
    stub = msg["raw_json"][0]["message"]["content"][1]["content"]
    assert stub[ELIDED_KEY] is True
    # INVARIANT: the stub agrees with the joined text the header would read.
    joined = "\n".join(blocks_text)
    assert stub["lines"] == len(blocks_text)
    assert stub["head"] == joined[:ELIDE_HEAD_CHARS]
    assert len(json.dumps(out)) * 4 < len(json.dumps([rec]))
    # ... and the endpoint still hands back the ORIGINAL structure.
    assert _detail([rec], 0, "tu_s", DETAIL_SOURCE_RAW)["result"] == content


def test_a_small_result_is_never_held_back():
    """Below the stub's own wire cost, holding a body back costs MORE.

    It would grow the response and buy the browser a request for something it
    already had, so the floor is exactly where eliding starts paying.
    """
    small = "x" * (ELIDE_MIN_CHARS - 40)
    records = [_assistant(0, [
        _tool_use("tu_s", "Bash", {"command": "ls"}),
        _tool_result("tu_s", small),
    ])]
    assert summarize_history_records(records, FLOW) is records


def _bare_result_record(long_text, content=""):
    return {
        "step_id": STEP,
        "step_type": "implement",
        "ordinal": 0,
        "message": {
            "role": "user",
            "content": content,
            "raw_json": [
                {"type": "tool_result", "tool_use_id": "tu_bare",
                 "content": [{"type": "text", "text": long_text}]},
            ],
        },
    }


def test_a_fold_visible_bare_tool_result_rides_inline():
    """Boundary rule (c): a record's own narrative is never held back.

    With no ``content`` of its own, this record's FOLDED bubble is recovered by
    walking this very line's blocks (``extractAssistantText``) — and that walk
    reads a block's ``text`` only when it is a STRING, so an elided one is not
    shortened but dropped, leaving the bubble reading "no readable content". The
    body is therefore fold-visible, not detail: it ships whole, the record is
    never marked lazy, and the frontend renders it where it stands with no
    on-demand request.
    """
    long_text = "y" * (ELIDE_MIN_CHARS * 3)
    records = [_bare_result_record(long_text)]
    assert summarize_history_records(records, FLOW) is records


def test_a_bare_tool_result_line_keeps_its_block_shape():
    """Once the record HAS its own content, the line is detail again.

    ``extractAssistantText`` is not consulted at all then, so the block list is
    pure chip material — but only its strings go, because the frontend still
    walks the list element by element when rebuilding the panel.
    """
    long_text = "y" * (ELIDE_MIN_CHARS * 3)
    rec = _bare_result_record(long_text, content="ran a tool")
    out = summarize_history_records([rec], FLOW)
    shaped = out[0]["message"]["raw_json"][0]["content"]
    assert isinstance(shaped, list), "the block list shape must survive"
    assert shaped[0]["text"][ELIDED_KEY] is True
    assert out[0]["message"]["lazy_tool_use_ids"] == ["tu_bare"]


def test_a_fold_visible_tool_use_input_rides_inline():
    """Boundary rule (c): an input the FOLDED bubble prints stays inline.

    With no ``content`` of its own, this record's bubble body is
    ``[Write: {"file_path":…,"content":…}]`` — the input itself. A stub there
    would put a synthetic prefix in the message body, and the expanded view
    could not differ from it because the two are the same text.
    """
    records = [_assistant(0, [
        _tool_use("tu_w", "Write", {"file_path": "x.py", "content": BIG_FILE}),
        _tool_result("tu_w", "ok"),
    ], content="")]
    assert summarize_history_records(records, FLOW) is records


def test_an_envelope_tool_result_is_still_collapsed_when_content_is_empty():
    """A tool_result INSIDE a message envelope is never narrative.

    ``extractAssistantText`` skips it outright (it is paired into a chip
    instead), so it stays detail even for a record with no content of its own —
    boundary rule (c) is about what the FOLDED view actually reads.
    """
    records = [_assistant(0, [
        _tool_use("tu_e", "Bash", {"command": "ls"}),
        _tool_result("tu_e", BIG_TEXT),
    ], content="")]
    out = summarize_history_records(records, FLOW)
    msg = out[0]["message"]
    assert msg["lazy_tool_use_ids"] == ["tu_e"]
    blocks = msg["raw_json"][0]["message"]["content"]
    assert blocks[1]["content"][ELIDED_KEY] is True
    # ... and the tool_use input beside it, which IS fold-visible, is untouched.
    assert blocks[0]["input"] == {"command": "ls"}
    # The id names the CALL; the mask says which of its two bodies actually
    # lost text, so the frontend does not rewrite the input that rode inline.
    assert msg[LAZY_BODY_MASK_KEY] == "r"


def test_the_body_mask_names_only_the_bodies_that_were_replaced():
    """Per BODY, not per call: an inline body is never marked as stripped.

    ``__elided__`` is legal in a tool's real arguments, so a frontend that read
    the id list as "both bodies were replaced" would rewrite a marker-shaped
    argument the server deliberately kept and change the collapsed chip header.
    """
    literal = {ELIDED_KEY: True, "head": "abc", "lines": 2, "chars": 3}
    records = [_assistant(0, [
        _tool_use("tu_mask", "Weird", {"spec": literal}),
        _tool_result("tu_mask", BIG_TEXT),
    ], content="")]
    out = summarize_history_records(records, FLOW)
    msg = out[0]["message"]
    assert msg["lazy_tool_use_ids"] == ["tu_mask"]
    assert msg[LAZY_BODY_MASK_KEY] == "r"
    blocks = msg["raw_json"][0]["message"]["content"]
    assert blocks[0]["input"] == {"spec": literal}, (
        "the fold-visible input was rewritten"
    )
    # Both bodies stripped reads as "b".
    both = summarize_history_records([_assistant(1, [
        _tool_use("tu_both", "Write",
                  {"body": BIG_FILE, "note": "x"}),
        _tool_result("tu_both", BIG_TEXT),
    ])], FLOW)
    assert both[0]["message"][LAZY_BODY_MASK_KEY] == "b"
    # Input only reads as "u".
    only_input = summarize_history_records([_assistant(2, [
        _tool_use("tu_in", "Write", {"body": BIG_FILE, "note": "x"}),
        _tool_result("tu_in", "ok"),
    ])], FLOW)
    assert only_input[0]["message"][LAZY_BODY_MASK_KEY] == "u"


def test_failed_raw_json_pair_is_never_elided():
    blocks = [
        _tool_use("tu_f", "Bash", {"command": "x" * (ELIDE_MIN_CHARS + 10)}),
        _tool_result("tu_f", BIG_TEXT, is_error=True),
    ]
    records = [_assistant(0, blocks)]
    out = summarize_history_records(records, FLOW)
    # Nothing changed at all, so the list is handed back by reference.
    assert out is records


def test_elision_shrinks_the_wire_payload():
    blocks = []
    for i in range(20):
        blocks.append(_tool_use("tu_%d" % i, "Write",
                                {"file_path": "f%d.py" % i, "content": BIG_FILE}))
        blocks.append(_tool_result("tu_%d" % i, BIG_TEXT))
    records = [_assistant(0, blocks)]
    before = len(json.dumps(records))
    after = len(json.dumps(summarize_history_records(records, FLOW)))
    assert after * 10 < before, (before, after)


def test_unidentified_blocks_keep_their_body_inline():
    """A chip the detail endpoint could not address must stay self-sufficient."""
    blocks = [
        # tool_use with no id, and an orphan result with no tool_use_id.
        {"type": "tool_use", "name": "Bash", "input": {"command": BIG_TEXT}},
        {"type": "tool_result", "content": BIG_TEXT},
    ]
    records = [_assistant(0, blocks)]
    assert summarize_history_records(records, FLOW) is records


# --------------------------------------------------------------------------
# detail extraction (pure)
# --------------------------------------------------------------------------


def test_extract_progress_detail_is_scoped_to_the_addressed_record():
    """Each fragment answers for ITS OWN record, not for the last one to match.

    The in-flight and the settled fragment of one call are two records carrying
    the same ``tool_use_id``; the chip the user expanded is one of them, and the
    address says which.
    """
    in_flight = _progress(0, "tu_1", "[Read: a.py]",
                          detail={"kind": "tool_input", "input": {"file_path": "a.py"}})
    settled = _progress(1, "tu_1", "[Read ✓ a.py]",
                        detail={"kind": "read_text", "text": BIG_TEXT},
                        is_error=False)
    records = [in_flight, settled]
    got = _detail(records, 1, "tu_1")
    assert got["source"] == DETAIL_SOURCE_PROGRESS
    assert got["detail"]["kind"] == "read_text"
    assert got["detail"]["text"] == BIG_TEXT
    assert _detail(records, 0, "tu_1")["detail"]["kind"] == "tool_input"
    assert _detail(records, 1, "nope") is None
    # An address the bundle does not hold at all reads as absent.
    assert _detail(records, 7, "tu_1") is None
    assert _detail(records, 1, "tu_1", step_id="other_step") is None


def test_a_repeated_tool_use_id_answers_per_record():
    """INVARIANT: ``tool_use_id`` is unique only inside ONE record.

    codex synthesizes ids like ``codex_tool_1`` per call, so two steps of a flow
    can each hold that id. A flow-wide scan used to hand the first chip the
    second call's body — the address is what stops that.
    """
    first = _progress(0, "codex_tool_1", "[Bash ✓ one]",
                      detail={"kind": "text", "text": "FIRST"}, is_error=False)
    second = dict(_progress(4, "codex_tool_1", "[Bash ✓ two]",
                            detail={"kind": "text", "text": "SECOND"},
                            is_error=False))
    second["step_id"] = "02_test_beefcafe"
    records = [first, second]
    assert _detail(records, 0, "codex_tool_1")["detail"]["text"] == "FIRST"
    assert _detail(records, 4, "codex_tool_1",
                   step_id="02_test_beefcafe")["detail"]["text"] == "SECOND"


def test_a_missing_source_is_not_answered_from_the_other_one():
    """The two sources render visibly different panels, so neither substitutes.

    A daemon-built ``stream_progress`` payload can carry a pre-write diff the
    browser cannot reconstruct; answering that request out of ``raw_json`` would
    silently show a full-file panel where the chip promised a diff.
    """
    progress = _progress(0, "tu_x", "[Write ✓ x.py]",
                         detail={"kind": "write_diff", "diff": "@@"},
                         is_error=False)
    raw = _assistant(1, [
        _tool_use("tu_y", "Write", {"file_path": "x.py", "content": BIG_FILE}),
        _tool_result("tu_y", "written"),
    ])
    records = [progress, raw]
    assert _detail(records, 0, "tu_x", DETAIL_SOURCE_RAW) is None
    assert _detail(records, 1, "tu_y", DETAIL_SOURCE_PROGRESS) is None
    # ... while each source still answers for itself.
    assert _detail(records, 0, "tu_x")["detail"]["kind"] == "write_diff"
    assert _detail(records, 1, "tu_y", DETAIL_SOURCE_RAW)["tool_name"] == "Write"


def test_locate_reports_a_line_the_daemon_already_read_past():
    """``passed`` is the drain's own completion signal for one line.

    A daemon streams a step's lines in ascending order, so a HIGHER ordinal
    having arrived proves the addressed line was read past and skipped. That —
    not elapsed silence — is what lets the detail route stop waiting on a
    multi-frame recovery.
    """
    records = [_progress(5, "tu_late", "[Bash ✓ ls]",
                         detail={"kind": "text", "text": "x"}, is_error=False)]
    late = locate_record_detail(records, step_id=STEP, ordinal=9,
                                tool_use_id="tu_late",
                                source=DETAIL_SOURCE_PROGRESS)
    assert late == {"detail": None, "record_found": False, "passed": False}
    hole = locate_record_detail(records, step_id=STEP, ordinal=2,
                                tool_use_id="tu_late",
                                source=DETAIL_SOURCE_PROGRESS)
    assert hole["passed"] is True and hole["record_found"] is False
    here = locate_record_detail(records, step_id=STEP, ordinal=5,
                                tool_use_id="tu_late",
                                source=DETAIL_SOURCE_PROGRESS)
    assert here["record_found"] is True and here["detail"] is not None


def test_an_unaddressable_record_is_never_lazified():
    """No address, no fetch — so its body must ride inline instead."""
    rec = _progress(0, "tu_n", "[Read ✓ a.py]",
                    detail={"kind": "text", "text": BIG_TEXT}, is_error=False)
    rec.pop("ordinal")
    assert record_address(rec) is None
    records = [rec]
    assert summarize_history_records(records, FLOW) is records


def test_extract_raw_pair_returns_unelided_blocks():
    blocks = [
        _tool_use("tu_w", "Write", {"file_path": "x.py", "content": BIG_FILE}),
        _tool_result("tu_w", "written"),
        _tool_use("tu_i", "Bash", {"command": "sleep 1"}),
    ]
    records = [_assistant(0, blocks)]
    got = _detail(records, 0, "tu_w", DETAIL_SOURCE_RAW)
    assert got == {
        "source": DETAIL_SOURCE_RAW,
        "tool_name": "Write",
        "input": {"file_path": "x.py", "content": BIG_FILE},
        "result": "written",
        "status": "success",
    }
    # A call the step never settled is reported as in-flight, input included.
    unpaired = _detail(records, 0, "tu_i", DETAIL_SOURCE_RAW)
    assert unpaired["status"] == "in-flight"
    assert unpaired["input"] == {"command": "sleep 1"}


def test_long_path_inputs_are_never_elided():
    """INVARIANT: header material a formatter reads WHOLE survives the shaping.

    ``file_path`` / ``path`` are rendered verbatim by the Read / Edit / Write /
    Grep / Glob headers and middle-shortened TAIL-first by ``truncate_path``
    for a generic file tool. A stub keeps only a prefix, so eliding them would
    silently drop the filename the collapsed chip exists to name.
    """
    long_path = "/srv/" + "deep_dir/" * 80 + "target_module.py"
    assert len(long_path) >= ELIDE_MIN_CHARS
    long_dir = "/srv/" + "nested/" * 90
    blocks = [
        _tool_use("tu_r", "Read", {"file_path": long_path}),
        _tool_result("tu_r", BIG_TEXT),
        _tool_use("tu_g", "Grep", {"pattern": "needle", "path": long_dir}),
        _tool_result("tu_g", BIG_TEXT),
        # An unregistered file tool: the generic key=value fallback shortens a
        # file_path tail-first, so it is header material there too.
        _tool_use("tu_d", "Delete", {"file_path": long_path}),
        _tool_result("tu_d", BIG_TEXT),
    ]
    out = summarize_history_records([_assistant(0, blocks)], FLOW)
    shaped = out[0]["message"]["raw_json"][0]["message"]["content"]
    assert shaped[0]["input"]["file_path"] == long_path
    assert shaped[2]["input"]["path"] == long_dir
    assert shaped[4]["input"]["file_path"] == long_path
    # The heavy results next to them are still elided, so nothing is lost by
    # keeping the paths whole.
    for pos in (1, 3, 5):
        assert shaped[pos]["content"][ELIDED_KEY] is True


def test_a_long_path_alone_leaves_the_call_inline():
    """Nothing was held back, so the chip must not be told to fetch anything."""
    long_path = "/srv/" + "deep_dir/" * 80 + "target_module.py"
    records = [_assistant(0, [
        _tool_use("tu_w", "Write", {"file_path": long_path, "content": "ok"}),
        _tool_result("tu_w", "written"),
    ])]
    assert summarize_history_records(records, FLOW) is records


# --------------------------------------------------------------------------
# envelope-level tool fields (a wrapped / version-skewed record)
# --------------------------------------------------------------------------


def _envelope_progress(ordinal, tool_use_id, content, *, detail):
    """A record whose tool fields sit on the OUTER envelope, not the message.

    ``normalizeRecord``'s ``pick`` is message-first with an envelope fallback,
    so this shape renders identically in the browser — and the shaping already
    followed it. Extraction must follow the same lookup or the browser gets a
    lazy marker whose detail request 404s on a body the cache is holding.
    """
    return {
        "step_id": STEP,
        "step_type": "implement",
        "ordinal": ordinal,
        "tool_use_id": tool_use_id,
        "tool_detail": detail,
        "is_error": False,
        "message": {
            "type": "stream_progress",
            "role": "assistant",
            "partial": True,
            "content": content,
            "timestamp": "2026-08-31T00:00:00",
        },
    }


def _envelope_assistant(ordinal, blocks):
    return {
        "step_id": STEP,
        "step_type": "implement",
        "ordinal": ordinal,
        "raw_json": [{"type": "assistant", "message": {"content": blocks}}],
        "message": {
            "role": "assistant",
            "content": "done",
            "timestamp": "2026-08-31T00:00:01",
        },
    }


def test_envelope_level_progress_detail_round_trips():
    detail = {"kind": "read_text", "file_path": "a.py", "text": BIG_TEXT}
    rec = _envelope_progress(0, "tu_e", "[Read ✓ a.py · 400 lines]", detail=detail)
    out = summarize_history_records([rec], FLOW)
    # Shaped where the field actually lives...
    assert "tool_detail" not in out[0]
    assert out[0]["tool_detail_lazy"] is True
    # ... and found again by the endpoint's extraction.
    got = _detail([rec], 0, "tu_e")
    assert got is not None and got["detail"] is detail
    assert got["is_error"] is False


def test_envelope_level_raw_json_round_trips():
    blocks = [
        _tool_use("tu_ew", "Write", {"file_path": "x.py", "content": BIG_FILE}),
        _tool_result("tu_ew", "written"),
    ]
    rec = _envelope_assistant(0, blocks)
    out = summarize_history_records([rec], FLOW)
    assert out[0]["lazy_tool_use_ids"] == ["tu_ew"]
    got = _detail([rec], 0, "tu_ew", DETAIL_SOURCE_RAW)
    assert got is not None
    assert got["input"]["content"] == BIG_FILE
    assert got["status"] == "success"


def test_a_large_structured_input_is_held_back_too():
    """A body need not contain a single oversize STRING to be worth holding.

    A generic tool whose input is a million-element numeric array has no elidable
    leaf, yet the collapsed chip reads at most a 30-character JSON preview of it
    — so the whole array used to ride to the browser for nothing.
    """
    payload = list(range(50000))
    rec = _assistant(0, [
        _tool_use("tu_arr", "Weird", {"data": payload}),
        _tool_result("tu_arr", "ok"),
    ])
    out = summarize_history_records([rec], FLOW)
    shaped = out[0]["message"]
    assert shaped["lazy_tool_use_ids"] == ["tu_arr"]
    wire = json.dumps(out[0])
    assert len(wire) < len(json.dumps(rec)) / 100
    # The kept head is a genuine JSON PREFIX of the original, which is what
    # keeps the generic chip's 30-character preview byte-identical.
    kept = shaped["raw_json"][0]["message"]["content"][0]["input"]["data"]
    assert kept[:8] == payload[:8]
    assert kept[-1][ELIDED_KEY] is True
    # The cache still answers with the whole array.
    got = _detail([rec], 0, "tu_arr", DETAIL_SOURCE_RAW)
    assert got["input"]["data"] == payload


def test_a_list_input_travels_back_as_a_list():
    """The input IS a list — the detail reply must hand a list back.

    The browser enumerates an array input with ``Object.keys`` exactly like an
    object, so the un-summarized panel printed its entries. Answering with an
    empty object would render an argument-less panel for a body the shaping
    itself held back.
    """
    # Big enough that holding it back pays for BOTH the stub and the markers
    # the record then has to carry (benefit rule (b)).
    payload = [BIG_FILE, "tail entry"]
    rec = _assistant(0, [
        _tool_use("tu_list", "Weird", payload),
        _tool_result("tu_list", "ok"),
    ])
    out = summarize_history_records([rec], FLOW)
    shaped = out[0]["message"]
    assert shaped["lazy_tool_use_ids"] == ["tu_list"]
    kept = shaped["raw_json"][0]["message"]["content"][0]["input"]
    assert isinstance(kept, list)
    assert kept[0][ELIDED_KEY] is True
    got = _detail([rec], 0, "tu_list", DETAIL_SOURCE_RAW)
    assert got["input"] == payload


def test_a_scalar_input_rides_inline():
    """Boundary rule (a): a bare-string input has no shape to restore into.

    Neither the detail reply nor the "View raw" splice can put a scalar back
    where a JSON structure is expected, so it is never lazified in the first
    place.
    """
    rec = _assistant(0, [
        _tool_use("tu_scalar", "Weird", "x" * (ELIDE_MIN_CHARS + 40)),
        _tool_result("tu_scalar", "ok"),
    ])
    records = [rec]
    assert summarize_history_records(records, FLOW) is records


def test_a_short_list_input_is_left_whole():
    """Benefit rule (b): a list smaller than its stub keeps riding inline."""
    rec = _assistant(0, [
        _tool_use("tu_small", "Weird", {"data": [1, 2, 3]}),
        _tool_result("tu_small", "ok"),
    ])
    assert summarize_history_records([rec], FLOW) is not None
    out = summarize_history_records([rec], FLOW)
    assert out[0] is rec


def test_a_list_whose_tail_costs_more_than_the_stub_stays_whole():
    """The head budget is reached, but dropping what is left would not pay."""
    # Long enough to fill the kept head, then a couple of tiny trailing items:
    # the stub replacing them is bigger than they are.
    rec = _assistant(0, [
        _tool_use("tu_edge", "Weird", {"data": [BIG_FILE, 1, 2]}),
        _tool_result("tu_edge", "ok"),
    ])
    out = summarize_history_records([rec], FLOW)
    kept = out[0]["message"]["raw_json"][0]["message"]["content"][0]["input"]["data"]
    # The oversize string leaf is still elided; the numeric tail rides along.
    assert kept[0][ELIDED_KEY] is True
    assert kept[1:] == [1, 2]


def test_an_empty_progress_detail_is_not_worth_holding_back():
    """Benefit rule (b) on the stream_progress source.

    ``tool_detail_lazy`` + ``detail_flow`` are bigger than an empty payload, so
    dropping it would GROW the response and buy the browser a request for
    something it already had.
    """
    rec = _progress(0, "tu_empty", "[Bash ✓ ok]", detail={}, is_error=False)
    out = summarize_history_records([rec], FLOW)
    assert out[0] is rec
    assert out[0]["message"]["tool_detail"] == {}
    assert "tool_detail_lazy" not in out[0]["message"]


def test_a_tiny_deeply_nested_result_is_left_inline():
    """Reaching the walk's depth guard must read as SMALL, never as large.

    A nine-level nest of one integer serializes to a couple of dozen bytes; a
    stub replacing it is several times bigger.
    """
    tiny = 1
    for _ in range(9):
        tiny = {"a": tiny}
    rec = _assistant(0, [
        _tool_use("tu_deep", "Weird", {"q": 1}),
        _tool_result("tu_deep", tiny),
    ])
    out = summarize_history_records([rec], FLOW)
    assert out[0] is rec


# --------------------------------------------------------------------------
# HTTP surface
# --------------------------------------------------------------------------


BUNDLE = [
    _progress(0, "tu_p", "[Read ✓ a.py · 400 lines]",
              detail={"kind": "read_text", "file_path": "a.py", "text": BIG_TEXT},
              is_error=False),
    _progress(1, "tu_bad", "[Bash ✗ boom]",
              detail={"kind": "text", "text": "boom: " + BIG_TEXT},
              is_error=True),
    _assistant(2, [
        _tool_use("tu_w", "Write", {"file_path": "x.py", "content": BIG_FILE}),
        _tool_result("tu_w", "written"),
    ], content="done"),
]


#: A step of its own, so the filler below cannot disturb the ordinals the
#: assertions address.
FILLER_STEP = "00_discover_00000000"


def _filler(ordinal=0):
    """A record heavy enough to bill its frame at the daemon's chunk bound.

    ``read_flow`` stops at ``MAX_BYTES_PER_REPORT`` of BILLED bytes — the
    records' own on-disk line sizes, NOT the encoded frame — and the pull
    handler keeps reading from the advancing cursor for exactly as long as its
    reads truncate. So a frame at that bound is the wire's only statement that a
    reply has more frames coming, and a test that wants a multi-frame reply has
    to make its head genuinely reach it.
    """
    return {
        "step_id": FILLER_STEP,
        "step_type": "discover",
        "ordinal": ordinal,
        "message": {
            "role": "assistant",
            "content": "x" * (MAX_BYTES_PER_REPORT + 4096),
            "timestamp": "2026-08-31T00:00:00",
        },
    }


@pytest.fixture()
def client_and_app():
    from fastapi.testclient import TestClient

    app, _key = authed_app()
    with TestClient(app) as client:
        login(client)
        yield client, app


def _seed(client, app, records=BUNDLE, flow=FLOW, protocol_version=""):
    """Connect a daemon and get *flow*'s bundle cached.

    *protocol_version* pins the peer's advertised wire revision. The cache-miss
    tests below pass ``"8"`` on purpose: since revision 9 the detail route
    prefers a single-step WINDOW read against a capable daemon, and these cases
    exist to pin the whole-flow pull that remains the fallback for every older
    daemon (its multi-frame follow, its single deadline, its 404 promptness).
    The window path has its own coverage in ``test_history_step_window.py``.
    """
    daemon = client.websocket_connect("/ws")
    sock = daemon.__enter__()
    sock.send_text(
        authed_hello(
            app, MACHINE, "host", "6.4.0", protocol_version=protocol_version
        )
    )
    protocol.decode(sock.receive_text())  # WELCOME
    # The index is what makes the flow *addressable* after its bundle has been
    # evicted — the exact state the on-demand detail path has to survive.
    sock.send_text(protocol.make_history_index([{"flow_id": flow}]).to_json())
    sock.send_text(
        protocol.make_history_data(
            flow, protocol.HISTORY_MODE_FULL, records
        ).to_json()
    )
    for _ in range(50):
        resp = client.get("/api/history/%s" % flow)
        if resp.status_code == 200 and resp.json().get("cached"):
            return daemon, sock, resp
    daemon.__exit__(None, None, None)
    raise AssertionError("bundle never became cache-visible")


def test_bundle_response_is_summarized(client_and_app):
    client, app = client_and_app
    daemon, _sock, resp = _seed(client, app)
    try:
        body = resp.json()
        records = body["records"]
        assert len(records) == len(BUNDLE)

        progress = records[0]["message"]
        assert "tool_detail" not in progress
        assert progress["tool_detail_lazy"] is True
        assert progress["detail_flow"] == FLOW
        assert progress["content"] == BUNDLE[0]["message"]["content"]

        # (5) the failed call's body rides inline and is NOT lazified, so a
        # failure-heavy session opens without a burst of detail requests.
        failed = records[1]["message"]
        assert failed["tool_detail"]["text"].startswith("boom: ")
        assert "tool_detail_lazy" not in failed

        final = records[2]["message"]
        assert final["lazy_tool_use_ids"] == ["tu_w"]
        written = final["raw_json"][0]["message"]["content"][0]["input"]["content"]
        assert written[ELIDED_KEY] is True

        # The two lazified records shed almost everything they carried; the
        # failed one keeps its body by design, so the whole-bundle saving is
        # measured on the records the shaping actually touches.
        lazified_before = len(json.dumps([BUNDLE[0], BUNDLE[2]]))
        lazified_after = len(json.dumps([records[0], records[2]]))
        assert lazified_after * 10 < lazified_before, (
            lazified_before, lazified_after,
        )
        assert len(json.dumps(records)) < len(json.dumps(BUNDLE))
    finally:
        daemon.__exit__(None, None, None)


def test_cache_still_holds_the_full_bundle(client_and_app):
    """Summarization happens on the way OUT — the relay cache is unchanged.

    This is the load-bearing half of "the daemon leg is untouched": the
    on-demand endpoint reads the body back out of exactly this cache.
    """
    client, app = client_and_app
    daemon, _sock, _resp = _seed(client, app)
    try:
        cached = app.state.server_state._history_data[FLOW]
        msg = cached["records"][0]["message"]
        assert msg["tool_detail"]["text"] == BIG_TEXT
        assert "tool_detail_lazy" not in msg
        assert "detail_flow" not in msg
        raw = cached["records"][2]["message"]["raw_json"]
        assert raw[0]["message"]["content"][0]["input"]["content"] == BIG_FILE
    finally:
        daemon.__exit__(None, None, None)


def test_detail_endpoint_serves_progress_payload(client_and_app):
    client, app = client_and_app
    daemon, _sock, _resp = _seed(client, app)
    try:
        resp = client.get(
            "/api/history/%s/detail" % FLOW,
            params={"tool_use_id": "tu_p", "step_id": STEP, "ordinal": 0,
                    "source": DETAIL_SOURCE_PROGRESS},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["source"] == DETAIL_SOURCE_PROGRESS
        assert body["tool_use_id"] == "tu_p"
        assert body["detail"]["text"] == BIG_TEXT
    finally:
        daemon.__exit__(None, None, None)


def test_detail_endpoint_serves_raw_pair(client_and_app):
    client, app = client_and_app
    daemon, _sock, _resp = _seed(client, app)
    try:
        resp = client.get(
            "/api/history/%s/detail" % FLOW,
            params={"tool_use_id": "tu_w", "step_id": STEP, "ordinal": 2,
                    "source": DETAIL_SOURCE_RAW},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["source"] == DETAIL_SOURCE_RAW
        assert body["tool_name"] == "Write"
        assert body["input"]["content"] == BIG_FILE
        assert body["status"] == "success"
    finally:
        daemon.__exit__(None, None, None)


def test_detail_endpoint_never_substitutes_the_other_source(client_and_app):
    """A chip gets back the panel it promised, or the unavailable state.

    The two sources are not interchangeable: a daemon-built ``stream_progress``
    payload can show a pre-write diff the browser cannot reconstruct, while the
    raw pair rebuilds a full-content panel. Silently answering from the other
    source would show the user a different panel than the one the chip rendered
    from before it was collapsed.
    """
    client, app = client_and_app
    daemon, _sock, _resp = _seed(client, app)
    try:
        # tu_w exists only in the final record's raw_json.
        resp = client.get(
            "/api/history/%s/detail" % FLOW,
            params={"tool_use_id": "tu_w", "step_id": STEP, "ordinal": 2,
                    "source": DETAIL_SOURCE_PROGRESS},
        )
        assert resp.status_code == 404, resp.text
        # ... and symmetrically for a call that only ever streamed.
        resp = client.get(
            "/api/history/%s/detail" % FLOW,
            params={"tool_use_id": "tu_p", "step_id": STEP, "ordinal": 0,
                    "source": DETAIL_SOURCE_RAW},
        )
        assert resp.status_code == 404, resp.text
    finally:
        daemon.__exit__(None, None, None)


def test_detail_endpoint_is_scoped_to_the_expanded_message(client_and_app):
    """Two records sharing a synthesized ``tool_use_id`` answer separately."""
    client, app = client_and_app
    records = [
        _progress(0, "codex_tool_1", "[Bash ✓ one]",
                  detail={"kind": "text", "text": "FIRST " + BIG_TEXT},
                  is_error=False),
        _progress(1, "codex_tool_1", "[Bash ✓ two]",
                  detail={"kind": "text", "text": "SECOND " + BIG_TEXT},
                  is_error=False),
    ]
    daemon, _sock, _resp = _seed(client, app, records=records, flow=FLOW)
    try:
        for ordinal, expected in ((0, "FIRST"), (1, "SECOND")):
            resp = client.get(
                "/api/history/%s/detail" % FLOW,
                params={"tool_use_id": "codex_tool_1", "step_id": STEP,
                        "ordinal": ordinal},
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["detail"]["text"].startswith(expected)
    finally:
        daemon.__exit__(None, None, None)


def test_detail_endpoint_rejects_a_missing_id(client_and_app):
    client, app = client_and_app
    daemon, _sock, _resp = _seed(client, app)
    try:
        assert client.get("/api/history/%s/detail" % FLOW).status_code == 422
        # The record address is required too — a bare tool_use_id is ambiguous.
        assert client.get(
            "/api/history/%s/detail" % FLOW,
            params={"tool_use_id": "tu_p"},
        ).status_code == 422
        assert client.get(
            "/api/history/%s/detail" % FLOW,
            params={"tool_use_id": "tu_p", "step_id": STEP},
        ).status_code == 422
    finally:
        daemon.__exit__(None, None, None)


def test_detail_endpoint_404s_an_unknown_call(client_and_app):
    client, app = client_and_app
    daemon, _sock, _resp = _seed(client, app)
    try:
        resp = client.get(
            "/api/history/%s/detail" % FLOW,
            params={"tool_use_id": "tu_ghost", "step_id": STEP, "ordinal": 0},
        )
        assert resp.status_code == 404
    finally:
        daemon.__exit__(None, None, None)


def test_detail_endpoint_404s_an_unknown_flow(client_and_app):
    client, _app = client_and_app
    resp = client.get(
        "/api/history/nope/detail",
        params={"tool_use_id": "tu_p", "step_id": STEP, "ordinal": 0},
    )
    assert resp.status_code == 404


def test_detail_endpoint_requires_a_session(client_and_app):
    client, _app = client_and_app
    client.cookies.clear()
    resp = client.get(
        "/api/history/%s/detail" % FLOW,
        params={"tool_use_id": "x", "step_id": STEP, "ordinal": 0},
    )
    assert resp.status_code == 401


def test_detail_endpoint_503s_when_the_daemon_is_gone(client_and_app):
    """The unavailable-fallback the chip renders: cache miss + no daemon."""
    client, app = client_and_app
    daemon, _sock, _resp = _seed(client, app)
    daemon.__exit__(None, None, None)
    # Drop the cached bundle exactly as the memory-budget sweep would, leaving
    # the flow still indexed (so the owner gate passes) but its body gone.
    state = app.state.server_state
    state._history_data.pop(FLOW, None)
    resp = client.get(
        "/api/history/%s/detail" % FLOW,
        params={"tool_use_id": "tu_p", "step_id": STEP, "ordinal": 0},
    )
    assert resp.status_code == 503, resp.text


def test_detail_endpoint_gives_up_when_the_daemon_send_stalls(
    client_and_app, monkeypatch
):
    """Requirement 4: the回程 pull timeout is the ONE upper bound on the route.

    ``send_to_connection`` ends in ``websocket.send_text``, which has no timeout
    of its own and blocks for as long as a backpressured or half-open daemon
    socket refuses to drain. The route used to park there — before the wait, and
    before the tail-follow deadline was ever consulted — so the expanded panel
    sat on its loading message with no upper bound at all instead of falling
    back to the localized "detail unavailable" state.
    """
    import asyncio as _asyncio

    from tianluo.server import app as server_app

    client, app = client_and_app
    daemon, _sock, _resp = _seed(client, app, protocol_version="8")
    try:
        app.state.server_state._history_data.pop(FLOW, None)

        stalled = {"entered": False}

        async def _never_returns(*_args, **_kwargs):
            stalled["entered"] = True
            await _asyncio.sleep(3600)

        monkeypatch.setattr(server_app, "request_history", _never_returns)
        monkeypatch.setattr(server_app, "HISTORY_PULL_TIMEOUT", 0.5)
        resp = client.get(
            "/api/history/%s/detail" % FLOW,
            params={"tool_use_id": "tu_p", "step_id": STEP, "ordinal": 0},
        )
        assert stalled["entered"], "the route never dispatched the pull"
        # 504 (not a hang, and not a 404 that would read as "no such call").
        assert resp.status_code == 504, resp.text
    finally:
        daemon.__exit__(None, None, None)


def test_detail_endpoint_bounds_the_whole_round_trip_not_just_the_wait(
    client_and_app, monkeypatch
):
    """A ``_PullAbandoned`` retry may not restart the budget.

    Each retry used to re-enter with a FRESH ``HISTORY_PULL_TIMEOUT``, so a flow
    whose leader kept failing before dispatch could hold the route open for a
    multiple of the one timeout the browser is told about. The deadline is an
    instant, taken once, and every retry spends what is left of it.
    """
    import asyncio as _asyncio

    from tianluo.server import app as server_app

    client, app = client_and_app
    daemon, _sock, _resp = _seed(client, app, protocol_version="8")
    try:
        app.state.server_state._history_data.pop(FLOW, None)

        attempts = {"n": 0}

        async def _dispatch_fails(*_args, **_kwargs):
            attempts["n"] += 1
            await _asyncio.sleep(0.2)
            # Reported as "nobody took it", which releases the followers and
            # sends this caller back around the leader loop.
            return False

        monkeypatch.setattr(server_app, "request_history", _dispatch_fails)
        monkeypatch.setattr(server_app, "HISTORY_PULL_TIMEOUT", 1.0)
        started = time.monotonic()
        resp = client.get(
            "/api/history/%s/detail" % FLOW,
            params={"tool_use_id": "tu_p", "step_id": STEP, "ordinal": 0},
        )
        elapsed = time.monotonic() - started
        assert attempts["n"] >= 1
        # A dispatch nobody took is 503 for a KNOWN flow (the chip's unavailable
        # state); what matters here is that it answered well inside one budget.
        assert resp.status_code in (503, 504), resp.text
        assert elapsed < 5.0, "the round trip outran its single deadline"
    finally:
        daemon.__exit__(None, None, None)


def test_detail_endpoint_repulls_from_the_daemon_on_a_cache_miss(client_and_app):
    """A miss goes back to the owning daemon, whose jsonl is authoritative."""
    client, app = client_and_app
    daemon, sock, _resp = _seed(client, app, protocol_version="8")
    try:
        state = app.state.server_state
        state._history_data.pop(FLOW, None)

        import threading

        result = {}

        def _ask():
            result["resp"] = client.get(
                "/api/history/%s/detail" % FLOW,
                params={"tool_use_id": "tu_p", "step_id": STEP, "ordinal": 0},
            )

        worker = threading.Thread(target=_ask)
        worker.start()
        try:
            # The server relays a HISTORY_REQUEST; answer it with the full
            # bundle, exactly as the daemon would.
            for _ in range(20):
                msg = protocol.decode(sock.receive_text())
                if msg.type == protocol.MSG_HISTORY_REQUEST:
                    break
            else:  # pragma: no cover - defensive
                raise AssertionError("no HISTORY_REQUEST relayed")
            sock.send_text(
                protocol.make_history_data(
                    FLOW, protocol.HISTORY_MODE_FULL, BUNDLE
                ).to_json()
            )
        finally:
            worker.join(timeout=30)
        resp = result["resp"]
        assert resp.status_code == 200, resp.text
        assert resp.json()["detail"]["text"] == BIG_TEXT
    finally:
        daemon.__exit__(None, None, None)


def test_detail_endpoint_follows_a_multi_frame_pull(client_and_app):
    """A body that arrives in a LATER frame of the pull is not reported absent.

    A pull whose history exceeds the daemon's per-frame byte budget is answered
    as a ``full`` head followed by ``append`` tails, and the shared pull waiter
    is resolved by the head alone. Concluding "no such call" off that first
    re-read paints the localized unavailable state on a chip whose detail is
    still on the wire from a perfectly healthy daemon.

    The gap before the tail is deliberately WIDER than any silence-based guess:
    there is no "recovery complete" signal on the daemon→server wire (and adding
    one would change the upstream protocol this split does not touch), so a quiet
    stretch between two frames — disk scheduling, WebSocket backpressure — must
    read as "still coming", never as completion.
    """
    client, app = client_and_app
    daemon, sock, _resp = _seed(client, app, protocol_version="8")
    try:
        state = app.state.server_state
        state._history_data.pop(FLOW, None)

        import threading
        import time

        tail_detail = {"kind": "text", "text": "tail " + BIG_TEXT}
        tail = _progress(3, "tu_tail", "[Bash ✓ tail]", detail=tail_detail,
                         is_error=False)
        result = {}

        def _ask():
            result["resp"] = client.get(
                "/api/history/%s/detail" % FLOW,
                params={"tool_use_id": "tu_tail", "step_id": STEP,
                        "ordinal": 3},
            )

        worker = threading.Thread(target=_ask)
        worker.start()
        try:
            for _ in range(20):
                msg = protocol.decode(sock.receive_text())
                if msg.type == protocol.MSG_HISTORY_REQUEST:
                    break
            else:  # pragma: no cover - defensive
                raise AssertionError("no HISTORY_REQUEST relayed")
            # The head: a truncated read, so it does NOT carry tu_tail.
            sock.send_text(
                protocol.make_history_data(
                    FLOW, protocol.HISTORY_MODE_FULL, BUNDLE,
                    cursor={STEP + ".jsonl": len(BUNDLE)},
                    cursor_base={STEP + ".jsonl": 0},
                ).to_json()
            )
            # Longer than the 5 s idle window this route used to give up on.
            time.sleep(6.0)
            assert worker.is_alive(), "the handler gave up before the tail"
            sock.send_text(
                protocol.make_history_data(
                    FLOW, protocol.HISTORY_MODE_APPEND, [tail],
                    cursor={STEP + ".jsonl": len(BUNDLE) + 1},
                    cursor_base={STEP + ".jsonl": len(BUNDLE)},
                ).to_json()
            )
        finally:
            worker.join(timeout=30)
        resp = result["resp"]
        assert resp.status_code == 200, resp.text
        assert resp.json()["detail"]["text"] == tail_detail["text"]
    finally:
        daemon.__exit__(None, None, None)


def test_detail_404s_promptly_once_the_bundle_settles_the_line(client_and_app):
    """A line the daemon already read past is answered at once, not waited out.

    The follow has no idle timer any more, so its only fast exit is the bundle's
    own verdict: the frame that carried ordinal 3 also proves ordinal 1 will
    never arrive (a daemon streams a step's lines in ascending order), and the
    cursor it declared counts that line as read.
    """
    client, app = client_and_app
    daemon, sock, _resp = _seed(client, app, protocol_version="8")
    try:
        state = app.state.server_state
        state._history_data.pop(FLOW, None)

        import threading
        import time

        result = {}
        started = {}

        def _ask():
            started["at"] = time.monotonic()
            result["resp"] = client.get(
                "/api/history/%s/detail" % FLOW,
                params={"tool_use_id": "tu_ghost", "step_id": STEP,
                        "ordinal": 1},
            )
            result["elapsed"] = time.monotonic() - started["at"]

        worker = threading.Thread(target=_ask)
        worker.start()
        try:
            for _ in range(20):
                msg = protocol.decode(sock.receive_text())
                if msg.type == protocol.MSG_HISTORY_REQUEST:
                    break
            else:  # pragma: no cover - defensive
                raise AssertionError("no HISTORY_REQUEST relayed")
            sock.send_text(
                protocol.make_history_data(
                    FLOW, protocol.HISTORY_MODE_FULL, BUNDLE,
                    cursor={STEP + ".jsonl": len(BUNDLE)},
                    cursor_base={STEP + ".jsonl": 0},
                ).to_json()
            )
        finally:
            worker.join(timeout=30)
        assert result["resp"].status_code == 404, result["resp"].text
        assert result["elapsed"] < 10, (
            "a settled line must not wait out the pull timeout"
        )
    finally:
        daemon.__exit__(None, None, None)


def test_cross_owner_detail_reads_as_absent():
    """Another owner's flow is 404, never a leaked body."""
    from fastapi.testclient import TestClient

    import tianluo.server.crypto as crypto

    app, _key = authed_app()
    store = app.state.store
    other = store.create_owner("bob", is_admin=False)
    store.link_identity(other, "local", "bob")
    store.set_password(other, crypto.hash_password("pw"))

    with TestClient(app) as client:
        login(client)
        daemon, _sock, _resp = _seed(client, app)
        try:
            client.post("/api/auth/logout")
            login(client, "bob", "pw")
            resp = client.get(
                "/api/history/%s/detail" % FLOW,
                params={"tool_use_id": "tu_p", "step_id": STEP, "ordinal": 0},
            )
            assert resp.status_code == 404
        finally:
            daemon.__exit__(None, None, None)


# --------------------------------------------------------------------------
# transport boundary: shaped by ORIGIN, not by transport
# --------------------------------------------------------------------------


class _UiSocket:
    """Minimal ``/ws/ui`` stand-in capturing the frames it is sent."""

    def __init__(self):
        self.sent = []

    async def send_text(self, data):
        self.sent.append(json.loads(data))

    def history_frames(self):
        return [m for m in self.sent if m.get("type") == "history_data"]


def _relay_harness():
    """A ServerState + UiHub + registry wired the way production wires them."""
    from tianluo.server.state import ServerState
    from tianluo.server.ws import HistoryRequestRegistry, UiHub

    state = ServerState()
    hub = UiHub()
    ui = _UiSocket()
    registry = HistoryRequestRegistry()
    return state, hub, ui, registry


class _Manager:
    def is_connected(self, machine_id):
        return True

    async def send_to(self, machine_id, message):
        return True

    async def send_to_connection(self, machine_id, connection, message):
        return True


async def _relay_scenario():
    """Drive a cache-miss recovery drain, then a live append, through the relay.

    Uses the real ``_handle_message`` so the cache write, the pull-waiter
    resolution and the ``/ws/ui`` fan-out are the production ones.
    """
    from tianluo.server.ws import _handle_message, request_history

    state, hub, ui, registry = _relay_harness()
    await state.register_machine(MACHINE, "host", "1.0", owner_id="owner-A")
    await hub.register(ui, "owner-A")

    step_file = STEP + ".jsonl"
    head = BUNDLE[:2] + [_filler()]
    tail = BUNDLE[2:]

    # The server asks the daemon for this flow — the one funnel that marks
    # everything the reply brings back as a REPLAY.
    assert await request_history(_Manager(), state, FLOW, machine_id=MACHINE)

    # The drain's head: a `full` frame whose cursor already declares the tail,
    # so the bundle is knowingly incomplete. It rides at the daemon's per-frame
    # chunk bound, which is what says the reply has more to send — the real
    # reason a recovery is more than one frame.
    await _handle_message(
        protocol.make_history_data(
            FLOW, protocol.HISTORY_MODE_FULL, head,
            cursor={step_file: len(BUNDLE)},
            cursor_base={step_file: 0},
        ),
        state, MACHINE, hub, registry,
    )
    # The drain's TAIL: an `append` on the wire, indistinguishable from the
    # live push loop's frames — but it answers the same pull. Under the chunk
    # bound, so it is also the reply's LAST frame.
    await _handle_message(
        protocol.make_history_data(
            FLOW, protocol.HISTORY_MODE_APPEND, tail,
            cursor={step_file: len(BUNDLE)},
            cursor_base={step_file: 2},
        ),
        state, MACHINE, hub, registry,
    )
    # A genuine post-subscription increment: written AFTER this browser
    # subscribed, which is the boundary requirement 7 draws.
    live = _progress(3, "tu_live", "[Read x.py]",
                     detail={"kind": "read_text", "file_path": "x.py",
                             "text": BIG_TEXT},
                     is_error=False, timestamp=_live_stamp())
    await _handle_message(
        protocol.make_history_data(
            FLOW, protocol.HISTORY_MODE_APPEND, [live],
            cursor={step_file: len(BUNDLE) + 1},
            cursor_base={step_file: len(BUNDLE)},
        ),
        state, MACHINE, hub, registry,
    )
    return state, ui


def test_a_recovery_tail_is_summarized_over_the_websocket():
    """The tail of a multi-frame recovery is a REPLAY, wherever it arrives.

    It wears ``mode: append`` — the same clothes the live push loop wears — so
    the relay judges it by origin (it answers a pull this server dispatched),
    not by transport. Letting it through whole would leave most of a big
    session's download and eager panel-building exactly where they were.
    """
    import asyncio

    _state, ui = asyncio.run(_relay_scenario())
    frames = ui.history_frames()
    # The `full` head resolved no waiter here (no REST caller was parked), so
    # it is relayed too; both it and the append tail must be shaped.
    replay = [f for f in frames if any(
        r.get("ordinal") == 2 for r in f["records"])]
    assert replay, "the recovery tail never reached the browser"
    shaped = [r for f in replay for r in f["records"] if r.get("ordinal") == 2]
    assert shaped[0]["message"]["lazy_tool_use_ids"] == ["tu_w"]
    assert shaped[0]["message"]["detail_flow"] == FLOW
    body = json.dumps(shaped[0])
    assert BIG_FILE not in body, "the recovery tail still shipped its body"


def test_a_live_increment_still_arrives_whole():
    """Requirement 7: a genuine post-subscription append is NOT lazified.

    It is already in the browser's hands, so asking for it back would be a
    request for nothing — and would make a running console fire one per chip.
    """
    import asyncio

    _state, ui = asyncio.run(_relay_scenario())
    live = [r for f in ui.history_frames() for r in f["records"]
            if r.get("ordinal") == 3]
    assert live, "the live increment never reached the browser"
    msg = live[0]["message"]
    assert msg["tool_detail"]["text"] == BIG_TEXT
    assert "tool_detail_lazy" not in msg
    assert "detail_flow" not in msg


class _FakeClock:
    """Stands in for the ``time`` module inside ``state`` with a driven clock.

    Only ``monotonic`` is driven; everything else falls through to the real
    module, so patching this into ``tianluo.server.state`` leaves the event loop
    (and every other user of ``time``) on the real clock.
    """

    def __init__(self, start=1000.0):
        self._now = start

    def advance(self, seconds):
        self._now += seconds

    def monotonic(self):
        return self._now

    def __getattr__(self, name):
        import time as _time

        return getattr(_time, name)


def _replay_verdicts(monkeypatch, script, *, arm=1):
    """Run *script* — ``(gap_seconds, mode_full, chunk_bounded)`` triples.

    Returns the classification of each frame, with the clock driven so a real
    test never has to sleep out a window.
    """
    import asyncio

    import tianluo.server.state as state_mod

    clock = _FakeClock()
    monkeypatch.setattr(state_mod, "time", clock)

    async def scenario():
        state = state_mod.ServerState()
        for _ in range(arm):
            await state.mark_history_replay(FLOW)
        verdicts = []
        for gap, mode_full, chunk_bounded in script:
            clock.advance(gap)
            verdicts.append(
                await state.take_history_replay(
                    FLOW, mode_full=mode_full, chunk_bounded=chunk_bounded,
                )
            )
        return verdicts

    return asyncio.run(scenario())


def test_a_cold_first_frame_does_not_retire_the_reply(monkeypatch):
    """Requirement: a slow daemon does not turn its own reply into live traffic.

    The dispatch→first-frame gap is a cold multi-MB jsonl read — the latency
    ``HISTORY_PULL_TIMEOUT`` is sized for — so any quiet-time threshold would
    read the recovery it is waiting for as a live increment and ship the whole
    session's bodies. Every frame of the reply is a replay, however long the
    first one takes.
    """
    verdicts = _replay_verdicts(monkeypatch, [
        (25.0, True, True),    # the head, after a 25 s cold read
        (0.2, False, True),    # a tail still at the chunk bound
        (0.2, False, False),   # the reply's last frame
    ])
    assert verdicts == [True, True, True]


def test_a_live_append_after_the_reply_is_not_a_replay(monkeypatch):
    """Requirement 7: the push loop's next tick is live, not drain residue.

    The daemon holds its push loop off a flow for the whole drain and resumes on
    its ~1 s cadence, so the first genuine increment lands well inside any quiet
    window a clock-based rule could pick. It must ship whole — the console
    already has those bodies, and asking for them back is one request per chip.
    """
    verdicts = _replay_verdicts(monkeypatch, [
        (0.1, True, True),     # the reply's head
        (0.1, False, False),   # the reply's last frame
        (1.0, False, False),   # one push-loop tick later: live
        (1.0, False, False),   # and it stays live
        (60.0, False, False),
    ])
    assert verdicts == [True, True, False, False, False]


def test_a_second_pull_reply_is_not_read_as_live(monkeypatch):
    """Two pulls in flight (a REST miss plus a self-heal) drain one after another.

    Retiring on the first reply's closing frame would hand the second reply's
    frames to the browser whole.
    """
    verdicts = _replay_verdicts(monkeypatch, [
        (0.1, True, False),    # first reply, single frame
        (0.1, True, True),     # second reply's head
        (0.1, False, False),   # second reply's last frame
        (0.1, False, False),   # live
    ], arm=2)
    assert verdicts == [True, True, True, False]


def test_an_unanswered_pull_cannot_shape_a_flow_forever(monkeypatch):
    """The leak guard covers a request the daemon never answered at all."""
    verdicts = _replay_verdicts(monkeypatch, [
        (10_000.0, False, False),
    ])
    assert verdicts == [False]


def test_an_unsolicited_full_snapshot_is_summarized():
    """Requirement: a whole-bundle snapshot replacement is a replay.

    A daemon that restarts loses its ``_history_cursors``, so its first push for
    an open flow is a cursorless ``full`` carrying that flow's ENTIRE persisted
    history — nobody asked for it, and relaying it unshaped makes the browser
    re-download and eagerly build panels for the whole session.
    """
    import asyncio

    from tianluo.server.ws import _handle_message

    async def scenario():
        state, hub, ui, registry = _relay_harness()
        await state.register_machine(MACHINE, "host", "1.0", owner_id="owner-A")
        await hub.register(ui, "owner-A")
        await _handle_message(
            protocol.make_history_data(
                FLOW, protocol.HISTORY_MODE_FULL, BUNDLE,
                cursor={STEP + ".jsonl": len(BUNDLE)},
                cursor_base={STEP + ".jsonl": 0},
            ),
            state, MACHINE, hub, registry,
        )
        return ui

    ui = asyncio.run(scenario())
    frames = ui.history_frames()
    assert frames, "the snapshot never reached the browser"
    shaped = [r for f in frames for r in f["records"] if r.get("ordinal") == 2]
    assert shaped[0]["message"]["lazy_tool_use_ids"] == ["tu_w"]
    assert BIG_FILE not in json.dumps(frames), (
        "an unsolicited whole-bundle snapshot still shipped its bodies"
    )


def test_the_frame_that_triggers_a_self_heal_does_not_consume_its_marker():
    """A discarded append arms a recovery pull — and is not itself its reply.

    The append that trips the ``requires_full`` self-heal arrives BEFORE the
    pull it dispatches, so accounting it against that pull would retire the
    marker on the spot and hand the whole recovery to the browser unshaped.
    """
    import asyncio

    from tianluo.server.ws import _handle_message

    async def scenario():
        state, hub, ui, registry = _relay_harness()
        await state.register_machine(MACHINE, "host", "1.0", owner_id="owner-A")
        await hub.register(ui, "owner-A")
        step_file = STEP + ".jsonl"
        manager = _Manager()
        # First sighting of this flow is an ``append``: the cache has nothing to
        # anchor it to, discards it, and asks the daemon for a full rebuild.
        await _handle_message(
            protocol.make_history_data(
                FLOW, protocol.HISTORY_MODE_APPEND, BUNDLE[2:],
                cursor={step_file: len(BUNDLE)},
                cursor_base={step_file: 2},
            ),
            state, MACHINE, hub, registry,
            manager=manager, connection=object(),
        )
        armed = state._history_replay_pulls.get(FLOW)
        assert armed is not None and len(armed.pulls) == 1, (
            "the discarded append did not arm a recovery pull"
        )
        # The recovery's own reply, in two frames.
        await _handle_message(
            protocol.make_history_data(
                FLOW, protocol.HISTORY_MODE_FULL, BUNDLE[:2] + [_filler()],
                cursor={step_file: len(BUNDLE)},
                cursor_base={step_file: 0},
            ),
            state, MACHINE, hub, registry,
            manager=manager, connection=object(),
        )
        await _handle_message(
            protocol.make_history_data(
                FLOW, protocol.HISTORY_MODE_APPEND, BUNDLE[2:],
                cursor={step_file: len(BUNDLE)},
                cursor_base={step_file: 2},
            ),
            state, MACHINE, hub, registry,
            manager=manager, connection=object(),
        )
        return ui

    ui = asyncio.run(scenario())
    relayed = json.dumps(ui.history_frames())
    assert BIG_FILE not in relayed, "the recovery shipped its bodies whole"
    assert "lazy_tool_use_ids" in relayed, "the recovery was never shaped"


def test_a_live_append_racing_the_dispatch_does_not_retire_the_reply():
    """The daemon pauses its push loop only once the drain STARTS.

    So an append emitted in the dispatch→drain-start window — a window the
    widened pull timeouts explicitly tolerate being seconds long — reaches the
    server BEFORE the reply's head. Counting it against the marker retired the
    pull on the spot, and every tail chunk of the recovery behind it then shipped
    to the browser whole while the parked REST waiter answered the same records
    summarized: the browser held both copies of one stretch of persisted history.
    """
    import asyncio

    import tianluo.server.state as state_mod

    step_file = STEP + ".jsonl"

    async def scenario():
        state = state_mod.ServerState()
        # A cursorless full pull: its reply must start with a ``full`` head.
        await state.mark_history_replay(FLOW)
        frames = [
            # The interloper, anchored at the daemon's own push水位.
            (False, False, {step_file: 12}),
            # The reply's head, then its chunked tails.
            (True, True, {step_file: 0}),
            (False, True, {step_file: 2}),
            (False, False, {step_file: 4}),
            # Retired by the reply's own closing frame: live again.
            (False, False, {step_file: 6}),
        ]
        return [
            await state.take_history_replay(
                FLOW, mode_full=mode_full, chunk_bounded=bounded,
                cursor_base=base,
            )
            for mode_full, bounded, base in frames
        ]

    assert asyncio.run(scenario()) == [False, True, True, True, False]


def test_an_append_past_the_requested_cursor_cannot_open_an_incremental_reply():
    """An incremental backfill is answered with ``append`` frames too.

    Mode alone cannot separate that reply's head from the push loop's traffic,
    so the anchor does: the reply is read FROM the cursor we asked, while the
    append that raced the dispatch carries the daemon's own push水位, which sits
    past it.
    """
    import asyncio

    import tianluo.server.state as state_mod

    step_file = STEP + ".jsonl"

    async def scenario():
        state = state_mod.ServerState()
        await state.mark_history_replay(FLOW, cursor={step_file: 4})
        frames = [
            (False, False, {step_file: 9}),   # raced the dispatch
            (False, True, {step_file: 4}),    # the reply's head, at the bound
            (False, False, {step_file: 7}),   # its closing frame
            (False, False, {step_file: 11}),  # live again
        ]
        return [
            await state.take_history_replay(
                FLOW, mode_full=mode_full, chunk_bounded=bounded,
                cursor_base=base,
            )
            for mode_full, bounded, base in frames
        ]

    assert asyncio.run(scenario()) == [False, True, True, False]


def test_an_append_queued_behind_the_self_heal_does_not_free_the_recovery():
    """End to end: the second discarded append must not release the tails.

    The push loop emits its frames in one batch, so the append that trips the
    ``requires_full`` self-heal can already have a sibling queued behind it. That
    sibling is discarded by the cache exactly like the first — and the self-heal
    it would arm is deduped away as one is already in flight — so if it consumed
    the recovery's marker, everything past the recovery's first chunk reached
    every subscribed browser with its bodies intact.
    """
    import asyncio

    from tianluo.server.ws import _handle_message

    step_file = STEP + ".jsonl"

    async def scenario():
        state, hub, ui, registry = _relay_harness()
        await state.register_machine(MACHINE, "host", "1.0", owner_id="owner-A")
        await hub.register(ui, "owner-A")
        manager = _Manager()

        async def push(mode, records, cursor_base):
            await _handle_message(
                protocol.make_history_data(
                    FLOW, mode, records,
                    cursor={step_file: len(BUNDLE)},
                    cursor_base={step_file: cursor_base},
                ),
                state, MACHINE, hub, registry,
                manager=manager, connection=object(),
            )

        # First sighting is an ``append``: nothing to anchor it to, so the cache
        # discards it and the self-heal asks for a cursorless full rebuild.
        await push(protocol.HISTORY_MODE_APPEND, BUNDLE[2:], 2)
        # Its sibling from the same push batch, discarded the same way while the
        # recovery is still in flight.
        await push(protocol.HISTORY_MODE_APPEND, BUNDLE[2:], 2)
        # The recovery's reply: a head at the chunk bound, then its last frame.
        await push(protocol.HISTORY_MODE_FULL, BUNDLE[:2] + [_filler()], 0)
        await push(protocol.HISTORY_MODE_APPEND, BUNDLE[2:], 2)
        return ui

    ui = asyncio.run(scenario())
    relayed = json.dumps(ui.history_frames())
    assert "lazy_tool_use_ids" in relayed, "the recovery was never shaped"
    assert BIG_FILE not in relayed, (
        "an append that raced the recovery freed its tail to ship whole"
    )


def test_the_cache_keeps_the_full_bundle_through_a_replay():
    """Shaping is out-of-the-wire only: the cache still answers with the body."""
    import asyncio

    state, _ui = asyncio.run(_relay_scenario())
    cached = state._history_data[FLOW]["records"]
    got = _detail(cached, 2, "tu_w", DETAIL_SOURCE_RAW)
    assert got["input"]["content"] == BIG_FILE


def test_detail_follows_a_recovery_another_request_already_started(
    client_and_app,
):
    """A cached-but-unsettled bundle is followed, not answered 404 at once.

    A large recovery installs its head first. A chip whose ordinal lives in a
    later tail then reads as "not here" — but the bundle's own cursor says that
    line is still coming, so the honest answer is to wait for it, under the same
    single pull timeout, rather than to paint the unavailable state while a
    healthy daemon is still sending it.
    """
    client, app = client_and_app
    daemon, sock, _resp = _seed(client, app)
    try:
        import threading
        import time

        state = app.state.server_state
        step_file = STEP + ".jsonl"
        # Simulate the head of somebody else's recovery: the cache holds a
        # readable bundle whose cursor declares one more line than it has.
        state._history_data.pop(FLOW, None)
        sock.send_text(
            protocol.make_history_data(
                FLOW, protocol.HISTORY_MODE_FULL, BUNDLE,
                cursor={step_file: len(BUNDLE) + 1},
                cursor_base={step_file: 0},
            ).to_json()
        )
        for _ in range(200):
            if FLOW in state._history_data:
                break
            time.sleep(0.05)
        else:  # pragma: no cover - defensive
            raise AssertionError("the recovery head never landed")

        tail_detail = {"kind": "text", "text": "tail " + BIG_TEXT}
        tail = _progress(3, "tu_tail2", "[Bash ✓ tail]", detail=tail_detail,
                         is_error=False)
        result = {}

        def _ask():
            result["resp"] = client.get(
                "/api/history/%s/detail" % FLOW,
                params={"tool_use_id": "tu_tail2", "step_id": STEP,
                        "ordinal": 3},
            )

        worker = threading.Thread(target=_ask)
        worker.start()
        try:
            # Wider than any silence-based guess: the follow ends on the
            # bundle's verdict, never on a quiet gap between two frames.
            time.sleep(6.0)
            assert worker.is_alive(), (
                "a readable-but-unsettled bundle was answered 404 at once"
            )
            sock.send_text(
                protocol.make_history_data(
                    FLOW, protocol.HISTORY_MODE_APPEND, [tail],
                    cursor={step_file: len(BUNDLE) + 1},
                    cursor_base={step_file: len(BUNDLE)},
                ).to_json()
            )
        finally:
            worker.join(timeout=30)
        resp = result["resp"]
        assert resp.status_code == 200, resp.text
        assert resp.json()["detail"]["text"] == tail_detail["text"]
    finally:
        daemon.__exit__(None, None, None)


# --------------------------------------------------------------------------
# benefit rule (b): the COMPLETE replacement cost
# --------------------------------------------------------------------------


def test_a_body_that_cannot_pay_for_its_own_markers_rides_inline():
    """A held-back body pays for more than the stub that replaces it.

    It also has to name itself in ``lazy_tool_use_ids`` and put ``detail_flow``
    / ``detail_version`` on the record. A single 160-character Bash command
    clears the stub's own floor by a dozen bytes and then loses twice that to
    its id — so holding it back GREW the response and bought the browser a
    request for something it already had.
    """
    command = "x" * 160
    records = [_assistant(0, [
        _tool_use("toolu_01ABCDEFGHIJKLMNOP", "Bash", {"command": command}),
        _tool_result("toolu_01ABCDEFGHIJKLMNOP", "ok"),
    ])]
    out = summarize_history_records(records, FLOW)
    assert out is records
    assert len(json.dumps(out)) <= len(json.dumps(records))


def test_the_flow_id_is_charged_at_its_actual_length():
    """Benefit rule (b) prices ``detail_flow`` with the id it will carry.

    Charging it as an empty string under-stated the replacement by the flow id's
    own width (24 characters for a real one), which is enough to turn a marginal
    record from a saving into a GROWTH plus an on-demand request for a body the
    browser already had.
    """
    command = "x" * 246
    blocks = [
        _tool_use("toolu_01ABCDEFGHIJKLMNOP", "Bash", {"command": command}),
        _tool_result("toolu_01ABCDEFGHIJKLMNOP", "ok"),
    ]
    long_flow = "7f3c1d2e-9a4b-4c8d-b1e2"
    assert len(long_flow) == 23
    records = [_assistant(0, blocks)]
    out = summarize_history_records(records, long_flow)
    assert out is records, "a record that would GROW was still lazified"
    assert len(json.dumps(out)) <= len(json.dumps(records))


def test_no_shaping_ever_grows_the_serialized_record():
    """INVARIANT: shaping either shrinks a record or leaves it alone.

    Swept across the width band where the stub, the id entry and the per-record
    markers are all within a few bytes of each other — the band every
    under-priced marker slipped through — and measured on BOTH encoders the two
    server→browser legs use (the REST response is compact, the WebSocket relay
    is spaced).
    """
    flow = "7f3c1d2e-9a4b-4c8d-b1e2"
    for width in range(120, 420, 7):
        for tool_id in ("t1", "toolu_01ABCDEFGHIJKLMNOP"):
            records = [_assistant(0, [
                _tool_use(tool_id, "Bash", {"command": "x" * width}),
                _tool_result(tool_id, "ok"),
            ])]
            out = summarize_history_records(records, flow)
            for kwargs in ({"separators": (",", ":")}, {}):
                before = len(json.dumps(records, **kwargs))
                after = len(json.dumps(out, **kwargs))
                assert after <= before, (
                    "width=%d id=%s grew %d -> %d" % (
                        width, tool_id, before, after)
                )
    # The same sweep across the other two shapes that can be held back: the
    # stream_progress payload, and a tool_use input whose LIST tail is dropped.
    for width in range(0, 600, 11):
        records = [_progress(0, "toolu_01ABCDEFGHIJKLMNOP", "[Bash ok]",
                             detail={"kind": "text", "text": "y" * width},
                             is_error=False)]
        out = summarize_history_records(records, flow)
        for kwargs in ({"separators": (",", ":")}, {}):
            assert (len(json.dumps(out, **kwargs))
                    <= len(json.dumps(records, **kwargs))), width
    for count in range(0, 120, 7):
        records = [_assistant(0, [
            _tool_use("toolu_01ABCDEFGHIJKLMNOP", "X",
                      {"data": list(range(count))}),
            _tool_result("toolu_01ABCDEFGHIJKLMNOP", "ok"),
        ])]
        out = summarize_history_records(records, flow)
        for kwargs in ({"separators": (",", ":")}, {}):
            assert (len(json.dumps(out, **kwargs))
                    <= len(json.dumps(records, **kwargs))), count


def test_a_compact_progress_detail_is_not_worth_holding_back():
    """Benefit rule (b) reads a scalar at its ACTUAL width.

    ``{"a":0,"b":0,"c":0}`` is smaller than the markers that would replace it;
    pricing every number at a flat eight bytes read it as profitable and grew
    the response.
    """
    rec = _progress(0, "tu_tiny", "[Bash ✓ ok]",
                    detail={"a": 0, "b": 0, "c": 0}, is_error=False)
    out = summarize_history_records([rec], FLOW)
    assert out[0] is rec
    assert len(json.dumps(out)) <= len(json.dumps([rec]))


def test_a_truncated_list_keeps_the_headers_whole_preview_window():
    """INVARIANT: the shipped list is a JSON PREFIX past every preview window.

    The widest window a collapsed header takes on a non-string input is the
    generic chip's 30-character ``JSON.stringify`` preview. Charging a flat
    eight bytes per number reached the 96-character head budget after eleven
    values whose real JSON is under 24 characters, so the shipped list diverged
    from the original INSIDE that window and the folded chip changed.
    """
    data = list(range(2000))
    rec = _assistant(0, [
        _tool_use("tu_num", "Weird", {"data": data}),
        _tool_result("tu_num", "ok"),
    ])
    out = summarize_history_records([rec], FLOW)
    kept = out[0]["message"]["raw_json"][0]["message"]["content"][0]["input"]["data"]
    original = json.dumps(data, separators=(",", ":"))
    shipped = json.dumps(kept, separators=(",", ":"))
    common = 0
    for a, b in zip(original, shipped):
        if a != b:
            break
        common += 1
    assert common >= 30, (common, shipped[:60])
    assert original[:30] == shipped[:30]
    # ... and it still shrank the payload by an order of magnitude.
    assert len(json.dumps(out)) * 10 < len(json.dumps([rec]))


# --------------------------------------------------------------------------
# detail_version
# --------------------------------------------------------------------------


def test_a_rewrite_past_the_kept_head_moves_the_detail_version():
    """A retry rewrites its jsonl line under the SAME address.

    The address is stable across that rewrite by design, so when the change
    sits past the preserved head — same length, same line count — the
    summarized record is byte-identical and nothing tells the browser its
    cached body is stale. The version digests the ORIGINALS, so it moves.
    """
    first = BIG_FILE
    # Same length, same newline positions: everything the stub keeps agrees,
    # so the two summarized records are byte-identical but for the version.
    second = BIG_FILE[:ELIDE_HEAD_CHARS] + "".join(
        c if c == "\n" else "z" for c in BIG_FILE[ELIDE_HEAD_CHARS:]
    )
    # A same-length, same-line-count rewrite: everything the stub keeps agrees.
    def shaped(body):
        rec = _assistant(0, [
            _tool_use("tu_r", "Write", {"file_path": "x.py", "content": body}),
            _tool_result("tu_r", "ok"),
        ])
        return summarize_history_records([rec], FLOW)[0]["message"]

    a, b = shaped(first), shaped(second)
    stub_a = a["raw_json"][0]["message"]["content"][0]["input"]["content"]
    stub_b = b["raw_json"][0]["message"]["content"][0]["input"]["content"]
    assert stub_a["lines"] == stub_b["lines"]
    assert stub_a["chars"] == stub_b["chars"]
    assert a["detail_version"] != b["detail_version"], (
        "a rewritten body must not stay answerable from the cached one"
    )
    # Re-shaping the SAME record is stable, or every delivery would evict.
    assert shaped(first)["detail_version"] == a["detail_version"]


def test_the_detail_version_covers_a_dropped_progress_payload_too():
    def version(text):
        rec = _progress(0, "tu_v", "[Read ✓ a.py]",
                        detail={"kind": "read_text", "text": text},
                        is_error=False)
        return summarize_history_records([rec], FLOW)[0]["message"]["detail_version"]

    assert version(BIG_TEXT) == version(BIG_TEXT)
    assert version(BIG_TEXT) != version(BIG_TEXT + "\nmore")


# --------------------------------------------------------------------------
# replay classification: the chunk bound, the arming order, the origin rules
# --------------------------------------------------------------------------


def _just_under_the_bound(ordinal=0):
    """A record the daemon bills just UNDER its per-frame chunk cap.

    Its ENCODED frame is over that cap once the protocol and record envelopes
    are added — bytes the daemon's read budget never counted. That difference is
    exactly what the classification must not read: a reply's last frame misread
    as truncated leaves the drain open and consumes the next genuine live append
    as its closing frame.
    """
    from tianluo.server.ws import _billed_bytes

    rec = {
        "step_id": FILLER_STEP,
        "step_type": "discover",
        "ordinal": ordinal,
        "message": {
            "role": "assistant",
            "content": "x",
            "timestamp": "2026-08-31T00:00:00",
        },
    }
    pad = MAX_BYTES_PER_REPORT - 200 - _billed_bytes(rec) + 1
    rec["message"]["content"] = "x" * pad
    return rec


def test_the_chunk_bound_is_read_on_the_daemons_billing_basis():
    from tianluo.server.ws import _billed_bytes, _frame_is_chunk_bounded

    rec = _just_under_the_bound()
    frame = protocol.make_history_data(
        FLOW, protocol.HISTORY_MODE_FULL, [rec], cursor={}, cursor_base={},
    )
    assert _billed_bytes(rec) < MAX_BYTES_PER_REPORT
    assert len(frame.to_json().encode("utf-8")) > MAX_BYTES_PER_REPORT, (
        "the fixture no longer exercises the envelope-overhead gap"
    )
    assert not _frame_is_chunk_bounded([rec]), (
        "an untruncated final read was misread as still draining"
    )
    assert _frame_is_chunk_bounded([_filler()])


def test_a_live_append_after_a_borderline_final_frame_stays_whole():
    """End to end: the borderline frame closes the reply, so the next is live."""
    import asyncio

    from tianluo.server.ws import _handle_message, request_history

    step_file = STEP + ".jsonl"

    async def scenario():
        state, hub, ui, registry = _relay_harness()
        await state.register_machine(MACHINE, "host", "1.0", owner_id="owner-A")
        await hub.register(ui, "owner-A")
        assert await request_history(_Manager(), state, FLOW, machine_id=MACHINE)
        # The reply, in ONE frame that bills just under the daemon's cap.
        await _handle_message(
            protocol.make_history_data(
                FLOW, protocol.HISTORY_MODE_FULL,
                [_just_under_the_bound()],
                cursor={step_file: 2}, cursor_base={step_file: 0},
            ),
            state, MACHINE, hub, registry,
        )
        live = _progress(2, "tu_live2", "[Read x.py]",
                         detail={"kind": "read_text", "file_path": "x.py",
                                 "text": BIG_TEXT},
                         is_error=False, timestamp=_live_stamp())
        await _handle_message(
            protocol.make_history_data(
                FLOW, protocol.HISTORY_MODE_APPEND, [live],
                cursor={step_file: 3}, cursor_base={step_file: 2},
            ),
            state, MACHINE, hub, registry,
        )
        return ui

    ui = asyncio.run(scenario())
    shipped = [r for f in ui.history_frames() for r in f["records"]
               if r.get("ordinal") == 2 and r["step_id"] == STEP]
    assert shipped, "the live increment never reached the browser"
    assert shipped[-1]["message"]["tool_detail"]["text"] == BIG_TEXT
    assert "tool_detail_lazy" not in shipped[-1]["message"]


def test_a_reply_head_racing_the_send_still_opens_its_marker():
    """The marker is armed BEFORE the request leaves, not after.

    The daemon's answer can be read off the receive loop while the send
    coroutine is still resuming. A marker armed afterwards misses its own
    reply's head, and every chunked tail behind it then fails to open the
    (still-expecting-a-head) marker and is broadcast whole.
    """
    import asyncio

    from tianluo.server.ws import _handle_message, request_history

    step_file = STEP + ".jsonl"

    async def scenario():
        state, hub, ui, registry = _relay_harness()
        await state.register_machine(MACHINE, "host", "1.0", owner_id="owner-A")
        await hub.register(ui, "owner-A")

        async def deliver():
            await _handle_message(
                protocol.make_history_data(
                    FLOW, protocol.HISTORY_MODE_FULL, BUNDLE[:2] + [_filler()],
                    cursor={step_file: len(BUNDLE)},
                    cursor_base={step_file: 0},
                ),
                state, MACHINE, hub, registry,
            )
            await _handle_message(
                protocol.make_history_data(
                    FLOW, protocol.HISTORY_MODE_APPEND, BUNDLE[2:],
                    cursor={step_file: len(BUNDLE)},
                    cursor_base={step_file: 2},
                ),
                state, MACHINE, hub, registry,
            )

        class _EagerManager(_Manager):
            async def send_to(self, machine_id, message):
                await deliver()
                return True

            async def send_to_connection(self, machine_id, connection, message):
                await deliver()
                return True

        assert await request_history(
            _EagerManager(), state, FLOW, machine_id=MACHINE,
        )
        return ui

    ui = asyncio.run(scenario())
    relayed = json.dumps(ui.history_frames())
    assert "lazy_tool_use_ids" in relayed, "the recovery was never shaped"
    assert BIG_FILE not in relayed, (
        "a reply head that raced the send released its tail to ship whole"
    )


def test_a_failed_dispatch_does_not_leave_a_marker_behind():
    """Arming first costs a marker when the send fails; it is retracted."""
    import asyncio

    from tianluo.server.ws import request_history

    class _DeadManager(_Manager):
        async def send_to(self, machine_id, message):
            return False

        async def send_to_connection(self, machine_id, connection, message):
            return False

    async def scenario():
        state, _hub, _ui, _registry = _relay_harness()
        await state.register_machine(MACHINE, "host", "1.0", owner_id="owner-A")
        assert not await request_history(
            _DeadManager(), state, FLOW, machine_id=MACHINE,
        )
        return state

    state = asyncio.run(scenario())
    assert FLOW not in state._history_replay_pulls


def test_a_failed_dispatch_retracts_its_own_marker_not_a_rivals():
    """Two pulls can be armed at once; only the one that FAILED may be retracted.

    A daemon reconnect leaves one caller holding a stale socket. Its send fails
    while a rival pull — the ws self-heal's incremental recovery — is armed and
    dispatched successfully in that same window. Retracting the queue's tail
    took the marker off the pull that genuinely LEFT the server: its reply then
    arrived as ``append`` frames matching no armed shape, was classified as live
    traffic, and a server-dispatched回程 reply was relayed to the browsers with
    its bodies whole.
    """
    import asyncio

    from tianluo.server.ws import _handle_message, request_history

    step_file = STEP + ".jsonl"

    async def scenario():
        state, hub, ui, registry = _relay_harness()
        await state.register_machine(MACHINE, "host", "1.0", owner_id="owner-A")
        await hub.register(ui, "owner-A")

        class _ReconnectManager(_Manager):
            async def send_to(self, machine_id, message):
                return True

            async def send_to_connection(self, machine_id, connection, message):
                # The cursorless pull's send suspends on the stale socket; the
                # incremental recovery is armed AND dispatched in that window,
                # so it is the queue's tail when this send finally fails.
                assert not message.payload.get("cursor")
                assert await request_history(
                    self, state, FLOW, machine_id=MACHINE,
                    cursor={step_file: 2},
                )
                return False

        # A cached bundle to append onto, seeded by an unsolicited snapshot.
        await _handle_message(
            protocol.make_history_data(
                FLOW, protocol.HISTORY_MODE_FULL, BUNDLE[:2],
                cursor={step_file: 2}, cursor_base={step_file: 0},
            ),
            state, MACHINE, hub, registry,
        )
        assert not await request_history(
            _ReconnectManager(), state, FLOW,
            machine_id=MACHINE, connection=object(),
        )
        # The DISPATCHED pull's reply: append frames read from the cursor it
        # asked, at the chunk bound and then under it.
        await _handle_message(
            protocol.make_history_data(
                FLOW, protocol.HISTORY_MODE_APPEND, BUNDLE[2:] + [_filler()],
                cursor={step_file: len(BUNDLE)}, cursor_base={step_file: 2},
            ),
            state, MACHINE, hub, registry,
        )
        await _handle_message(
            protocol.make_history_data(
                FLOW, protocol.HISTORY_MODE_APPEND, [],
                cursor={step_file: len(BUNDLE)},
                cursor_base={step_file: len(BUNDLE)},
            ),
            state, MACHINE, hub, registry,
        )
        return state, ui

    state, ui = asyncio.run(scenario())
    relayed = json.dumps(ui.history_frames())
    assert BIG_FILE not in relayed, (
        "the dispatched pull's reply lost its marker and shipped whole"
    )
    assert "lazy_tool_use_ids" in relayed, "the reply was never shaped"
    # The failed caller's own marker is gone, and the reply retired its own.
    assert FLOW not in state._history_replay_pulls


def test_a_failed_dispatch_leaves_a_rivals_marker_armed():
    """State-level: the retraction picks the shape the failed caller armed."""
    import asyncio

    from tianluo.server.state import ServerState

    step_file = STEP + ".jsonl"

    async def scenario():
        state = ServerState()
        await state.mark_history_replay(FLOW)                      # A: full
        await state.mark_history_replay(FLOW, cursor={step_file: 2})  # B: sent
        await state.unmark_history_replay(FLOW)                     # A's send failed
        pulls = state._history_replay_pulls[FLOW].pulls
        assert [p.expects_full for p in pulls] == [False]
        # B's reply, anchored at the cursor B asked from, still reads as replay.
        return [
            await state.take_history_replay(
                FLOW, mode_full=False, chunk_bounded=bounded,
                cursor_base={step_file: base},
            )
            for bounded, base in [(True, 2), (False, 4)]
        ]

    assert asyncio.run(scenario()) == [True, True]


def test_a_re_delivered_line_on_the_live_path_still_arrives_whole():
    """The verdict is the FRAME's mechanism, never a per-record property.

    The daemon re-reads and re-sends a window it already delivered, mixed with a
    genuinely new line. Nothing about that frame answers a pull this server
    dispatched, so it is a live tail append and rides whole — for every record
    in it. Judging record by record ("the bundle already held this one") put a
    frame on the wire in two shapes at once, which is exactly what the per-frame
    rule exists to forbid.
    """
    import asyncio

    from tianluo.server.ws import _handle_message

    step_file = STEP + ".jsonl"
    old_line = _progress(1, "tu_old", "[Read a.py]",
                         detail={"kind": "read_text", "file_path": "a.py",
                                 "text": BIG_TEXT},
                         is_error=False, timestamp=_live_stamp())

    async def scenario():
        state, hub, ui, registry = _relay_harness()
        await state.register_machine(MACHINE, "host", "1.0", owner_id="owner-A")
        await hub.register(ui, "owner-A")
        await _handle_message(
            protocol.make_history_data(
                FLOW, protocol.HISTORY_MODE_FULL, [old_line],
                cursor={step_file: 1}, cursor_base={step_file: 0},
            ),
            state, MACHINE, hub, registry,
        )
        ui.sent.clear()
        fresh = _progress(2, "tu_fresh", "[Read b.py]",
                          detail={"kind": "read_text", "file_path": "b.py",
                                  "text": BIG_TEXT},
                          is_error=False, timestamp=_live_stamp())
        await _handle_message(
            protocol.make_history_data(
                FLOW, protocol.HISTORY_MODE_APPEND, [old_line, fresh],
                cursor={step_file: 3}, cursor_base={step_file: 1},
            ),
            state, MACHINE, hub, registry,
        )
        return ui

    ui = asyncio.run(scenario())
    by_ordinal = {}
    for frame in ui.history_frames():
        for record in frame["records"]:
            by_ordinal[record.get("ordinal")] = record
    assert set(by_ordinal) == {1, 2}, "the live append never reached the browser"
    for ordinal in (1, 2):
        message = by_ordinal[ordinal]["message"]
        assert message["tool_detail"]["text"] == BIG_TEXT
        assert "tool_detail_lazy" not in message


def test_a_delayed_append_is_not_demoted_to_a_replay():
    """Recorded-time evidence may never enter the verdict (basis, inference 1).

    The push loop lags, so a genuine tail append reaches the server carrying a
    stamp OLDER than the console that is watching. Reading that stamp — or the
    daemon host's timezone, which the naive local string silently folds in —
    made the running console fetch back a body it had just been handed. The
    frame's mechanism is the live tail-append path, so it rides whole whatever
    its records claim about when they were written.
    """
    import asyncio

    from tianluo.server.ws import _handle_message

    step_file = STEP + ".jsonl"

    async def scenario():
        state, hub, ui, registry = _relay_harness()
        await state.register_machine(MACHINE, "host", "1.0", owner_id="owner-A")
        await hub.register(ui, "owner-A")
        await _handle_message(
            protocol.make_history_data(
                FLOW, protocol.HISTORY_MODE_FULL, BUNDLE[:1],
                cursor={step_file: 1}, cursor_base={step_file: 0},
            ),
            state, MACHINE, hub, registry,
        )
        ui.sent.clear()
        # Stamped a full day before this console subscribed — and one record
        # carries no stamp at all, the other case a timestamp rule had to guess.
        stale = _progress(1, "tu_stale", "[Read old.py]",
                          detail={"kind": "read_text", "file_path": "old.py",
                                  "text": BIG_TEXT},
                          is_error=False, timestamp="2001-01-01T00:00:00")
        undated = _progress(2, "tu_undated", "[Read none.py]",
                            detail={"kind": "read_text",
                                    "file_path": "none.py",
                                    "text": BIG_TEXT},
                            is_error=False, timestamp=None)
        await _handle_message(
            protocol.make_history_data(
                FLOW, protocol.HISTORY_MODE_APPEND, [stale, undated],
                cursor={step_file: 3}, cursor_base={step_file: 1},
            ),
            state, MACHINE, hub, registry,
        )
        return ui

    ui = asyncio.run(scenario())
    relayed = [r for f in ui.history_frames() for r in f["records"]]
    assert len(relayed) == 2, "the live append never reached the browser"
    for record in relayed:
        assert record["message"]["tool_detail"]["text"] == BIG_TEXT
        assert "tool_detail_lazy" not in record["message"]
    assert "lazy_tool_use_ids" not in json.dumps(relayed)


def test_a_large_post_subscription_record_is_never_summarized_by_frame_size():
    """A frame reaching the daemon's chunk bound says nothing about creation.

    One genuinely new record can be large enough to bill at the cap on its own.
    Reading the frame's SIZE as "this is a backlog" summarized exactly the
    real-time increment requirement 7 keeps whole — and made the live console
    fetch back a body it had just been handed.
    """
    import asyncio

    from tianluo.server.ws import _frame_is_chunk_bounded, _handle_message

    step_file = STEP + ".jsonl"

    async def scenario():
        state, hub, ui, registry = _relay_harness()
        await state.register_machine(MACHINE, "host", "1.0", owner_id="owner-A")
        await hub.register(ui, "owner-A")
        await _handle_message(
            protocol.make_history_data(
                FLOW, protocol.HISTORY_MODE_FULL, BUNDLE[:1],
                cursor={step_file: 1}, cursor_base={step_file: 0},
            ),
            state, MACHINE, hub, registry,
        )
        ui.sent.clear()
        # Stamped AFTER the subscribe above, and heavy enough to bill at the
        # daemon's per-frame cap all by itself.
        live = _progress(1, "tu_big_live", "[Read x.py]",
                         detail={"kind": "read_text", "file_path": "x.py",
                                 "text": BIG_TEXT * 40},
                         is_error=False, timestamp=_live_stamp())
        assert _frame_is_chunk_bounded([live]), (
            "the fixture must reach the daemon's per-frame bound to be the "
            "case this test is about"
        )
        await _handle_message(
            protocol.make_history_data(
                FLOW, protocol.HISTORY_MODE_APPEND, [live],
                cursor={step_file: 2}, cursor_base={step_file: 1},
            ),
            state, MACHINE, hub, registry,
        )
        return ui

    ui = asyncio.run(scenario())
    shipped = [r for f in ui.history_frames() for r in f["records"]
               if r.get("ordinal") == 1]
    assert shipped, "the live increment never reached the browser"
    assert "tool_detail_lazy" not in shipped[-1]["message"]
    assert shipped[-1]["message"]["tool_detail"]["text"].startswith("line 0000")


def test_one_frame_leaves_the_server_in_exactly_one_shape():
    """The verdict is per FRAME and browser-independent (basis).

    Two consoles of the same owner subscribe at different instants and then the
    same live append arrives. Shaping it against each socket's own subscription
    put one frame on the wire in two forms — whole for the early console,
    summarized for the late one — which is the split the per-frame rule forbids.
    Both must receive the identical payload.
    """
    import asyncio

    from tianluo.server.ws import _handle_message

    step_file = STEP + ".jsonl"

    async def scenario():
        state, hub, early, registry = _relay_harness()
        late = _UiSocket()
        await state.register_machine(MACHINE, "host", "1.0", owner_id="owner-A")
        await hub.register(early, "owner-A")
        await _handle_message(
            protocol.make_history_data(
                FLOW, protocol.HISTORY_MODE_FULL, BUNDLE[:1],
                cursor={step_file: 1}, cursor_base={step_file: 0},
            ),
            state, MACHINE, hub, registry,
        )
        early.sent.clear()
        # Written between the two subscriptions — the very case the per-browser
        # split used to hand out in two shapes.
        record = _progress(1, "tu_split", "[Read x.py]",
                           detail={"kind": "read_text", "file_path": "x.py",
                                   "text": BIG_TEXT},
                           is_error=False, timestamp=_live_stamp())
        await asyncio.sleep(0.05)
        await hub.register(late, "owner-A")
        await _handle_message(
            protocol.make_history_data(
                FLOW, protocol.HISTORY_MODE_APPEND, [record],
                cursor={step_file: 2}, cursor_base={step_file: 1},
            ),
            state, MACHINE, hub, registry,
        )
        return early, late

    early, late = asyncio.run(scenario())
    early_recs = [r for f in early.history_frames() for r in f["records"]]
    late_recs = [r for f in late.history_frames() for r in f["records"]]
    assert early_recs and late_recs
    assert early_recs == late_recs, "one frame reached two browsers in two shapes"
    assert early_recs[-1]["message"]["tool_detail"]["text"] == BIG_TEXT
    assert "tool_detail_lazy" not in early_recs[-1]["message"]


def test_a_replay_frame_is_shaped_the_same_for_every_browser():
    """The other side of the same rule: a replay is summarized for ALL of them.

    A console that subscribes DURING a dispatched pull's drain must still be
    handed the reply's frames summarized — the reply's replay identity holds
    from dispatch until its own closing frame, and it is a property of the
    reply, not of who happens to be listening.
    """
    import asyncio

    from tianluo.server.ws import _handle_message, request_history

    step_file = STEP + ".jsonl"

    async def scenario():
        state, hub, early, registry = _relay_harness()
        late = _UiSocket()
        await state.register_machine(MACHINE, "host", "1.0", owner_id="owner-A")
        await hub.register(early, "owner-A")
        assert await request_history(_Manager(), state, FLOW, machine_id=MACHINE)
        await hub.register(late, "owner-A")
        await _handle_message(
            protocol.make_history_data(
                FLOW, protocol.HISTORY_MODE_FULL, BUNDLE,
                cursor={step_file: len(BUNDLE)}, cursor_base={step_file: 0},
            ),
            state, MACHINE, hub, registry,
        )
        return early, late

    early, late = asyncio.run(scenario())
    early_recs = [r for f in early.history_frames() for r in f["records"]]
    late_recs = [r for f in late.history_frames() for r in f["records"]]
    assert early_recs and early_recs == late_recs
    assert BIG_FILE not in json.dumps(early_recs), "a replay frame shipped a body"
