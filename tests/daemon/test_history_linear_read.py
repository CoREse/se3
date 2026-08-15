"""Linearity + semantic-equivalence tests for the ``read_flow`` read path.

The defect these lock down: ``read_flow`` used to materialise its input —
``fh.read()`` on the seeked handle for the incremental branch, ``fh.read()`` plus
``raw.split("\\n")`` for the full branch. Because a single frame is capped at
:data:`~tianluo.daemon.history.MAX_BYTES_PER_REPORT`, draining a large step file
takes ``file_size / cap`` rounds, and EVERY round re-read (and re-decoded) the
whole remaining tail before throwing all but one cap's worth away. Total I/O for
one drain was therefore quadratic in the file size — the 24 MB / 253 MB history
records that pinned the daemon.

The fix streams both branches line by line off a binary handle, so a round reads
only what it is about to ship and nothing past its truncation point is ever
touched. These tests assert that in two ways:

* **per round** — one incremental ``read_flow`` reads at most one byte budget
  plus the single record that crosses it;
* **per drain** — the cumulative bytes a full N-round drain reads stay in the
  same order as the file itself (~2n: one full-branch scan + one line pass),
  not the ``n²/cap`` a materialising reader spends.

The remaining O(n²) term — the rewrite-detection prefix re-hash — is measured
and fixed separately, so the byte counter here deliberately excludes
``_consumed_signature``'s reads.

The second half re-asserts, on the streaming implementation, every read-path
semantic the old buffer-and-split code carried: a partial (un-terminated) tail
is not consumed, blank lines still occupy a physical line number, an
un-terminated but parseable final record IS consumed, a physical-copy switch
re-reads from line 0, and a cursor pointing past the end of the file resets to
the head. ``ordinal`` is a physical line number, so all of these are ordinal
invariants, not merely counting details.
"""

from __future__ import annotations

import builtins
import json

import pytest

from tianluo.daemon import history as history_mod
from tianluo.daemon.history import (
    HEAD_SIGNATURE_BYTES,
    MAX_BYTES_PER_REPORT,
    DaemonHistoryReader,
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _make_reader(*roots):
    return DaemonHistoryReader(project_roots_provider=lambda: [str(r) for r in roots])


def _flow_dir(root, flow_id):
    d = root / "tianluo" / "history" / flow_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _msg(content, role="assistant"):
    return {"role": role, "content": content, "raw_json": [], "step_type": "implement"}


def _write_lines(path, texts):
    """Write raw *texts* verbatim — the caller owns every newline.

    Raw rather than ``json.dumps``-per-record on purpose: the partial-tail,
    blank-line and no-trailing-newline cases are all about exact byte layout.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(texts), encoding="utf-8")


def _keys(records):
    return [(r["step_id"], r["ordinal"]) for r in records]


class _CountingHandle:
    """Delegating file wrapper that bills every byte handed to the reader."""

    def __init__(self, fh, state):
        self._fh = fh
        self._state = state

    def read(self, size=-1):
        data = self._fh.read(size)
        self._state["bytes"] += len(data)
        return data

    def readline(self, size=-1):
        data = self._fh.readline(size)
        self._state["bytes"] += len(data)
        return data

    def __iter__(self):
        return self

    def __next__(self):
        data = next(self._fh)
        self._state["bytes"] += len(data)
        return data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self._fh.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self._fh, name)


@pytest.fixture()
def read_bytes(monkeypatch):
    """Count the bytes ``read_flow``'s RECORD path pulls out of ``*.jsonl``.

    Reads performed for rewrite detection (every one of which goes through
    ``history._hash_span``) are muted: that re-hash is the *other* quadratic term
    in this defect and is owned by the prefix-verifier work, so billing it here
    would drown the signal this fixture exists to measure.
    """
    state = {"bytes": 0, "muted": False}
    real_open = builtins.open

    def counting_open(file, *args, **kwargs):
        fh = real_open(file, *args, **kwargs)
        if state["muted"] or not str(file).endswith(".jsonl"):
            return fh
        return _CountingHandle(fh, state)

    monkeypatch.setattr(builtins, "open", counting_open)

    real_span = history_mod._hash_span

    def muted_span(path, start, end, hasher):
        state["muted"] = True
        try:
            return real_span(path, start, end, hasher)
        finally:
            state["muted"] = False

    monkeypatch.setattr(history_mod, "_hash_span", muted_span)

    def take():
        value = state["bytes"]
        state["bytes"] = 0
        return value

    state["take"] = take
    return state


def _big_flow(tmp_path, *, records=300, payload=20 * 1024):
    """A multi-MB single-step flow: *records* lines of ~*payload* bytes each."""
    flow_dir = _flow_dir(tmp_path, "big")
    jsonl = flow_dir / "06_implement.jsonl"
    with jsonl.open("w", encoding="utf-8") as fh:
        for i in range(records):
            fh.write(json.dumps(_msg(f"{i:06d}" + "x" * payload)) + "\n")
    return jsonl


def _drain(reader, flow_id, *, cursor=None, max_rounds=500):
    """Read *flow_id* to exhaustion; return ``(rounds, all_records)``."""
    rounds = []
    all_records = []
    for _ in range(max_rounds):
        result = reader.read_flow(flow_id, cursor=cursor)
        rounds.append(result)
        all_records.extend(result.records)
        cursor = result.cursor
        if not result.truncated:
            break
    else:  # pragma: no cover - a runaway drain is a test bug, not a result
        pytest.fail("drain did not terminate")
    return rounds, all_records


# --------------------------------------------------------------------------
# bounded / linear reading
# --------------------------------------------------------------------------


def test_incremental_round_reads_at_most_one_budget(tmp_path, read_bytes):
    """One incremental round reads a budget plus the record that crosses it.

    This is the per-round half of the fix: a round must never pull in the tail
    it is NOT going to ship. The bound is the byte budget plus one record (the
    intended single-record overshoot when a cap trips mid-record) plus the small
    fixed cost of re-stamping the bounded head/boundary fingerprints.
    """
    jsonl = _big_flow(tmp_path)
    record_bytes = max(
        len(line) for line in jsonl.read_bytes().split(b"\n") if line
    )
    reader = _make_reader(tmp_path)

    # Round 1 is the FULL branch (no cursor), which is inherently O(file): it
    # must walk the file to resolve the caller's cursor against it.
    first = reader.read_flow("big")
    assert first.truncated
    read_bytes["take"]()

    ceiling = MAX_BYTES_PER_REPORT + record_bytes + 4 * HEAD_SIGNATURE_BYTES + 64
    cursor = first.cursor
    rounds = 0
    while True:
        result = reader.read_flow("big", cursor=cursor)
        spent = read_bytes["take"]()
        assert spent <= ceiling, (
            f"incremental round {rounds} read {spent} bytes, above the "
            f"one-budget-plus-one-record ceiling {ceiling}"
        )
        cursor = result.cursor
        rounds += 1
        if not result.truncated:
            break
    assert rounds > 5, "fixture too small to exercise a multi-round drain"


def test_full_drain_read_volume_stays_linear(tmp_path, read_bytes):
    """Draining a multi-MB backlog reads O(file), not O(file² / budget).

    The materialising reader re-read the whole remaining tail every round, so a
    drain of ``k = size / budget`` rounds spent ``~k/2`` passes over the file.
    Streaming spends one full-branch scan plus one line pass — ~2n — so a 4x
    ceiling both passes comfortably and fails loudly on any return to
    re-read-the-tail behaviour.
    """
    jsonl = _big_flow(tmp_path)
    size = jsonl.stat().st_size
    assert size > 4 * 1024 * 1024, "fixture must be multi-MB to be meaningful"
    reader = _make_reader(tmp_path)

    read_bytes["take"]()
    rounds, records = _drain(reader, "big")
    spent = read_bytes["take"]()

    assert len(rounds) > 10, "fixture must span many capped rounds"
    assert len(records) == 300
    assert spent < 4 * size, (
        f"drain of {len(rounds)} rounds read {spent} bytes over a {size}-byte "
        f"file — a materialising (quadratic) read path"
    )


def test_drain_reproduces_the_full_read_record_sequence(tmp_path):
    """A capped multi-round drain and one uncapped full read agree exactly.

    ``ordinal`` is the physical line number and the frontend's reconcile key, so
    the streaming read path is only correct if the incremental chain reproduces
    the same ``(step_id, ordinal, message)`` sequence a from-scratch full read of
    the final file produces.
    """
    _big_flow(tmp_path, records=40, payload=1024)
    _, drained = _drain(_make_reader(tmp_path), "big")

    # The reference starts from a cursorless FULL read (the other branch), so
    # the comparison also pins full-vs-incremental ordinal agreement.
    reference = _make_reader(tmp_path)
    ref_records = []
    ref = reference.read_flow("big")
    ref_records.extend(ref.records)
    while ref.truncated:
        ref = reference.read_flow("big", cursor=ref.cursor)
        ref_records.extend(ref.records)

    assert _keys(drained) == _keys(ref_records)
    assert [r["message"] for r in drained] == [r["message"] for r in ref_records]
    assert _keys(drained) == [("06_implement", i) for i in range(40)]


# --------------------------------------------------------------------------
# semantic equivalence: the five layout scenarios
# --------------------------------------------------------------------------


def test_partial_tail_is_not_consumed_until_it_is_terminated(tmp_path):
    """A half-flushed final line stays unconsumed and keeps its ordinal.

    Both branches must leave an un-terminated line for the next round when it is
    not a complete record; consuming it would advance ``consumed``/``offset`` past
    a record the reader dropped, so the record would never be delivered once the
    writer finished it.
    """
    flow_dir = _flow_dir(tmp_path, "f1")
    jsonl = flow_dir / "01_discovery.jsonl"
    complete = json.dumps(_msg("done")) + "\n"
    half = json.dumps(_msg("streaming"))[:-8]  # truncated mid-JSON
    _write_lines(jsonl, [complete, half])

    reader = _make_reader(tmp_path)
    first = reader.read_flow("f1")
    assert _keys(first.records) == [("01_discovery", 0)]
    assert first.cursor["01_discovery.jsonl"] == 1
    assert reader._read_offsets[str(jsonl)][1] == len(complete.encode())

    # The writer finishes the line; the incremental branch now picks it up at
    # ordinal 1 — the physical line it always occupied.
    _write_lines(jsonl, [complete, json.dumps(_msg("streaming")) + "\n"])
    second = reader.read_flow("f1", cursor=first.cursor)
    assert _keys(second.records) == [("01_discovery", 1)]
    assert second.records[0]["message"]["content"] == "streaming"
    assert second.cursor["01_discovery.jsonl"] == 2


def test_blank_lines_occupy_physical_line_numbers(tmp_path):
    """Blank lines advance ``consumed`` so ``ordinal`` stays a line number.

    A blank line emits no record but is a physical line, so every record after it
    must carry the ordinal of its actual line — the identity a later full read
    reproduces and the frontend dedupes on.
    """
    flow_dir = _flow_dir(tmp_path, "f1")
    jsonl = flow_dir / "01_discovery.jsonl"
    _write_lines(
        jsonl,
        [
            json.dumps(_msg("a")) + "\n",
            "\n",
            "   \n",
            json.dumps(_msg("b")) + "\n",
            "not-json\n",
            json.dumps(_msg("c")) + "\n",
        ],
    )

    reader = _make_reader(tmp_path)
    first = reader.read_flow("f1")
    assert _keys(first.records) == [
        ("01_discovery", 0),
        ("01_discovery", 3),
        ("01_discovery", 5),
    ]
    assert first.cursor["01_discovery.jsonl"] == 6

    # The incremental branch must number the appended line identically.
    with jsonl.open("a", encoding="utf-8") as fh:
        fh.write("\n")
        fh.write(json.dumps(_msg("d")) + "\n")
    second = reader.read_flow("f1", cursor=first.cursor)
    assert _keys(second.records) == [("01_discovery", 7)]
    assert second.cursor["01_discovery.jsonl"] == 8


def test_unterminated_final_record_is_consumed_when_parseable(tmp_path):
    """A complete record written without a trailing newline is still read.

    Terminal step files and sidecars are written atomically as one
    ``json.dumps`` with no newline, so the full branch must tell that shape apart
    from a mid-write line by parseability alone.
    """
    flow_dir = _flow_dir(tmp_path, "f1")
    jsonl = flow_dir / "09_commit.jsonl"
    body = json.dumps(_msg("committed"))
    _write_lines(jsonl, [body])

    reader = _make_reader(tmp_path)
    result = reader.read_flow("f1")
    assert _keys(result.records) == [("09_commit", 0)]
    assert result.cursor["09_commit.jsonl"] == 1
    # Consumed to EOF: the tail carries no newline, so offset == file size.
    assert reader._read_offsets[str(jsonl)][1] == len(body.encode())

    # A parseable-but-not-a-dict tail is NOT a record and must not be consumed.
    other = flow_dir / "10_report.jsonl"
    _write_lines(other, [json.dumps(_msg("real")) + "\n", "[1, 2, 3]"])
    fresh = _make_reader(tmp_path)
    scalar = fresh.read_flow("f1")
    assert scalar.cursor["10_report.jsonl"] == 1


def test_copy_switch_rereads_the_new_copy_from_line_zero(tmp_path):
    """When a bare filename resolves to a different file, ``start`` resets to 0.

    The wire cursor is keyed by bare filename but the offset table by absolute
    path, so honouring the old copy's line count against a new copy would skip
    the new copy's leading lines outright.
    """
    main = tmp_path / "main"
    (main / "tianluo").mkdir(parents=True)
    wt = main / "tianluo" / "worktrees" / "wt__b"
    wt.mkdir(parents=True)

    main_file = _flow_dir(main, "f1") / "01_discovery.jsonl"
    _write_lines(
        main_file,
        [json.dumps(_msg("r1a")) + "\n", json.dumps(_msg("r1b")) + "\n"],
    )

    reader = _make_reader(main, wt)
    first = reader.read_flow("f1", project_root=str(wt))
    assert _keys(first.records) == [("01_discovery", 0), ("01_discovery", 1)]

    wt_file = _flow_dir(wt, "f1") / "01_discovery.jsonl"
    _write_lines(
        wt_file,
        [
            json.dumps(_msg("r1a")) + "\n",
            json.dumps(_msg("r1b")) + "\n",
            json.dumps(_msg("r2a")) + "\n",
        ],
    )

    second = reader.read_flow("f1", project_root=str(wt), cursor=first.cursor)
    # Re-delivered from the head of the NEW copy — same ordinals for the shared
    # prefix (idempotent on the frontend) and the genuinely new line included.
    assert _keys(second.records) == [
        ("01_discovery", 0),
        ("01_discovery", 1),
        ("01_discovery", 2),
    ]
    assert second.cursor_base["01_discovery.jsonl"] == 0
    assert second.cursor["01_discovery.jsonl"] == 3


def test_cursor_past_end_of_file_resets_to_the_head(tmp_path):
    """A cursor beyond the file's physical line count is discarded.

    Only reachable through the full branch, whose physical-line total now comes
    from a chunked newline count rather than a materialised line list — so this
    pins that the substituted count still drives the same reset decision.
    """
    flow_dir = _flow_dir(tmp_path, "f1")
    jsonl = flow_dir / "01_discovery.jsonl"
    _write_lines(
        jsonl,
        [json.dumps(_msg("a")) + "\n", json.dumps(_msg("b")) + "\n"],
    )

    reader = _make_reader(tmp_path)
    result = reader.read_flow("f1", cursor={"01_discovery.jsonl": 99})
    assert _keys(result.records) == [("01_discovery", 0), ("01_discovery", 1)]
    assert result.cursor_base["01_discovery.jsonl"] == 0
    assert result.cursor["01_discovery.jsonl"] == 2

    # A cursor exactly AT the line count is honoured, not reset: nothing new.
    settled = _make_reader(tmp_path).read_flow(
        "f1", cursor={"01_discovery.jsonl": 2}
    )
    assert settled.records == []
    assert settled.cursor["01_discovery.jsonl"] == 2


def test_invalid_utf8_line_is_skipped_not_fatal(tmp_path):
    """A corrupt byte sequence costs its own line, not the whole flow read.

    The text-mode read decoded the entire buffer up front, so one bad byte
    anywhere raised out of ``read_flow`` and no step of the flow rendered.
    Decoding per line inside ``json.loads`` demotes that to the same skip any
    other unparseable line gets.
    """
    flow_dir = _flow_dir(tmp_path, "f1")
    jsonl = flow_dir / "01_discovery.jsonl"
    jsonl.write_bytes(
        json.dumps(_msg("before")).encode()
        + b"\n"
        + b'{"role": "assistant", "content": "\xff\xfe"}\n'
        + json.dumps(_msg("after")).encode()
        + b"\n"
    )

    result = _make_reader(tmp_path).read_flow("f1")
    assert _keys(result.records) == [("01_discovery", 0), ("01_discovery", 2)]
    assert result.cursor["01_discovery.jsonl"] == 3
