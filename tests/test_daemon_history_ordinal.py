"""Tests for the stable per-record ``ordinal`` identity injected by the daemon.

:meth:`DaemonHistoryReader.read_flow` tags every record envelope with an
``ordinal`` — the record's 0-based physical line position in its step file.
Because the ``jsonl`` files are append-only, that position is fixed at write
time and is the stable identity the frontend keys on to reconcile records
idempotently (``step_id#ordinal``). These tests pin the three invariants the
whole self-heal model relies on:

1. every record carries an ``ordinal`` equal to its 0-based line index;
2. the SAME logical line gets the SAME ordinal in a ``full`` snapshot and in a
   later ``append`` delta (so an append never re-keys an already-rendered line);
3. after a step's file is rewritten in place by a retry (equal-length, grown,
   or shrunk), a given ordinal maps to the CURRENT line's new content — the
   frontend updates that key in place instead of leaving a stale bubble.
"""

from __future__ import annotations

import json

from se3.daemon.history import DaemonHistoryReader


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _write_jsonl(path, lines):
    """Write *lines* (list of dicts) as a newline-terminated jsonl file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )


def _make_reader(*roots):
    return DaemonHistoryReader(project_roots_provider=lambda: list(roots))


# --------------------------------------------------------------------------
# basic ordinal injection
# --------------------------------------------------------------------------


def test_records_carry_zero_based_ordinal(tmp_path):
    """Each record's ordinal equals its 0-based physical line index."""
    hist = tmp_path / "se3" / "history" / "f1"
    _write_jsonl(
        hist / "01_discovery_975607bb.jsonl",
        [
            {"role": "assistant", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "assistant", "content": "c"},
        ],
    )

    read = _make_reader(tmp_path).read_flow("f1")
    assert [r["ordinal"] for r in read.records] == [0, 1, 2]
    assert [r["message"]["content"] for r in read.records] == ["a", "b", "c"]


def test_ordinal_is_per_step_not_global(tmp_path):
    """Ordinal restarts at 0 for each step file (per-step line position)."""
    hist = tmp_path / "se3" / "history" / "f1"
    _write_jsonl(
        hist / "01_discovery_975607bb.jsonl",
        [{"role": "assistant", "content": "d0"}, {"role": "assistant", "content": "d1"}],
    )
    _write_jsonl(
        hist / "02_analyze_cafe1234.jsonl",
        [{"role": "assistant", "content": "a0"}],
    )

    read = _make_reader(tmp_path).read_flow("f1")
    by_step = {}
    for r in read.records:
        by_step.setdefault(r["step_id"], []).append((r["ordinal"], r["message"]["content"]))

    assert by_step["01_discovery_975607bb"] == [(0, "d0"), (1, "d1")]
    # Second step's first line is ordinal 0, NOT a continuation of the first.
    assert by_step["02_analyze_cafe1234"] == [(0, "a0")]


def test_marker_records_with_empty_content_get_distinct_ordinals(tmp_path):
    """Marker records (empty content, no status) stay distinct by ordinal.

    This is the exact shape that made the old content+timestamp key collapse
    several markers into one and drop the batch. Distinct ordinals keep them
    individually addressable.
    """
    hist = tmp_path / "se3" / "history" / "f1"
    _write_jsonl(
        hist / "01_discovery_975607bb.jsonl",
        [
            {"type": "index_progress", "content": ""},
            {"type": "index_progress", "content": ""},
            {"type": "index_progress", "content": ""},
        ],
    )

    read = _make_reader(tmp_path).read_flow("f1")
    assert len(read.records) == 3
    assert sorted(r["ordinal"] for r in read.records) == [0, 1, 2]


# --------------------------------------------------------------------------
# full -> append ordinal consistency
# --------------------------------------------------------------------------


def test_same_logical_line_same_ordinal_across_full_and_append(tmp_path):
    """An append delta assigns new lines the same ordinal a full read would."""
    hist = tmp_path / "se3" / "history" / "f1"
    step = hist / "01_discovery_975607bb.jsonl"
    _write_jsonl(
        step,
        [{"role": "assistant", "content": "a"}, {"role": "assistant", "content": "b"}],
    )

    reader = _make_reader(tmp_path)

    full = reader.read_flow("f1")
    assert full.mode == "full"
    assert [(r["ordinal"], r["message"]["content"]) for r in full.records] == [
        (0, "a"),
        (1, "b"),
    ]

    # Append two more physical lines to the same file.
    with step.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"role": "assistant", "content": "c"}) + "\n")
        fh.write(json.dumps({"role": "assistant", "content": "d"}) + "\n")

    delta = reader.read_flow("f1", cursor=full.cursor)
    assert delta.mode == "append"
    # ONLY the two new lines come back, keyed at their true physical ordinals 2,3.
    assert [(r["ordinal"], r["message"]["content"]) for r in delta.records] == [
        (2, "c"),
        (3, "d"),
    ]

    # A fresh full read from a clean reader agrees on every ordinal.
    fresh = _make_reader(tmp_path).read_flow("f1")
    assert [(r["ordinal"], r["message"]["content"]) for r in fresh.records] == [
        (0, "a"),
        (1, "b"),
        (2, "c"),
        (3, "d"),
    ]


# --------------------------------------------------------------------------
# retry in-place rewrite -> ordinal maps to NEW content
# --------------------------------------------------------------------------


def test_retry_equal_length_rewrite_maps_ordinal_to_new_content(tmp_path):
    """An equal-length in-place rewrite re-maps each ordinal to new content."""
    hist = tmp_path / "se3" / "history" / "f1"
    step = hist / "01_discovery_975607bb.jsonl"
    _write_jsonl(
        step,
        [{"role": "assistant", "content": "old0"}, {"role": "assistant", "content": "old1"}],
    )

    reader = _make_reader(tmp_path)
    first = reader.read_flow("f1")
    assert [(r["ordinal"], r["message"]["content"]) for r in first.records] == [
        (0, "old0"),
        (1, "old1"),
    ]

    # Retry rewrites the step in place, same number of lines / same size class.
    _write_jsonl(
        step,
        [{"role": "assistant", "content": "new0"}, {"role": "assistant", "content": "new1"}],
    )

    second = reader.read_flow("f1", cursor=first.cursor)
    # The rewrite is detected, the full replacement is delivered, and ordinal N
    # now carries the NEW content at line N.
    by_ord = {r["ordinal"]: r["message"]["content"] for r in second.records}
    assert by_ord[0] == "new0"
    assert by_ord[1] == "new1"


def test_retry_grown_rewrite_maps_ordinal_to_new_content(tmp_path):
    """A rewrite that grows the file still re-maps every consumed ordinal."""
    hist = tmp_path / "se3" / "history" / "f1"
    step = hist / "01_discovery_975607bb.jsonl"
    _write_jsonl(
        step,
        [{"role": "assistant", "content": "old0"}, {"role": "assistant", "content": "old1"}],
    )

    reader = _make_reader(tmp_path)
    first = reader.read_flow("f1")
    assert len(first.records) == 2

    # Retry replaces the two lines and adds a third (file grows).
    _write_jsonl(
        step,
        [
            {"role": "assistant", "content": "new0"},
            {"role": "assistant", "content": "new1"},
            {"role": "assistant", "content": "new2"},
        ],
    )

    second = reader.read_flow("f1", cursor=first.cursor)
    by_ord = {r["ordinal"]: r["message"]["content"] for r in second.records}
    assert by_ord == {0: "new0", 1: "new1", 2: "new2"}


def test_retry_shrunk_rewrite_maps_ordinal_to_new_content(tmp_path):
    """A rewrite that shrinks the file re-maps the surviving ordinals."""
    hist = tmp_path / "se3" / "history" / "f1"
    step = hist / "01_discovery_975607bb.jsonl"
    _write_jsonl(
        step,
        [
            {"role": "assistant", "content": "old0"},
            {"role": "assistant", "content": "old1"},
            {"role": "assistant", "content": "old2"},
        ],
    )

    reader = _make_reader(tmp_path)
    first = reader.read_flow("f1")
    assert len(first.records) == 3

    # Retry collapses the step to a single, shorter record (file shrinks below
    # the previously consumed byte offset).
    _write_jsonl(step, [{"role": "assistant", "content": "n0"}])

    second = reader.read_flow("f1", cursor=first.cursor)
    by_ord = {r["ordinal"]: r["message"]["content"] for r in second.records}
    assert by_ord[0] == "n0"
    # No stale high-ordinal survivors leak from the old, longer batch.
    assert set(by_ord) == {0}


def test_ordinal_stable_across_repeated_discovery_rewrites(tmp_path):
    """Multiple PAUSE->resume rewrites keep ordinal->current-content coherent.

    Mirrors the discovery multi-round symptom: each round the engine rewrites
    the step file. Every round the reader must deliver the current line N at
    ordinal N, never a stale one.
    """
    hist = tmp_path / "se3" / "history" / "f1"
    step = hist / "01_discovery_975607bb.jsonl"

    reader = _make_reader(tmp_path)
    cursor = None
    latest = {}
    for round_no in range(4):
        _write_jsonl(
            step,
            [
                {"role": "assistant", "content": f"r{round_no}-l0"},
                {"role": "assistant", "content": f"r{round_no}-l1"},
            ],
        )
        read = reader.read_flow("f1", cursor=cursor)
        cursor = read.cursor
        for r in read.records:
            latest[r["ordinal"]] = r["message"]["content"]

    # After the final round, each ordinal reflects the LAST round's content.
    assert latest[0] == "r3-l0"
    assert latest[1] == "r3-l1"
