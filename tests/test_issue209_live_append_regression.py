"""Issue #209 — deterministic regression lock on the **real** captured frames.

issue #209: after a ``se3 run`` confirms its discovery plan and steps into
``analyze`` (discovery→analyze), or when a later step errors and is manually
retried, the WebUI conversation freezes — the left status bar keeps advancing
(driven by the request/response REST poll) but the push-driven live
``history_data`` append never arrives, until the operator exits and re-enters
the session. This regression had recurred through ~ten prior fixes.

G1 (``tests/ISSUE_209_FREEZE_DIAGNOSIS.md``) localized the layer with *real*
reproductions — not static analysis: the freeze is **daemon push-loop
starvation under realistic project load**, NOT the frontend / server-cache /
``read_flow`` / ``dedupeAppendRecords`` layers. Those were all proven correct on
the real frames. The CPU sink that starves the push loop is the repeated
``json.loads`` of the *active* ``engine.json`` (which grows to ~1 MB on a
long-running flow) several times per push tick across
``active_flow_signature`` + ``build_index`` (→ ``_index_root``) +
``_is_still_active`` + ``live_flow_ids``. Those parses are GIL-bound, so under
load they serialize the push loop and the discovery→analyze (and the
step-agnostic retry) live-append frame never reaches the web.

G2 fixed the root cause in ``daemon/history.py`` by memoizing the active
``engine.json`` *parse* by its raw content (``_read_engine_cached`` /
``_parse_engine_json``), collapsing the per-tick re-parses to one per *actual*
change shared across every per-tick reader of the single reader instance.

This module locks that fix in place by **replaying the real captured frame
sequence** — the on-disk records in ``tests/frontend/fixtures/issue_209/`` and
the daemon frame sequence they produce in ``daemon_frames.json`` — through the
real :class:`DaemonHistoryReader`, rather than idealized hand-built frames (the
sibling ``tests/test_issue_209_push_starvation.py`` uses synthetic data; the G3
contract is that the lock rides the *real* frame序). It asserts, on those real
frames and both #209 trigger boundaries:

* the discovery→analyze transition (a brand-new ``02_analyze`` jsonl) and the
  step error (``03_plan`` ends in ``step_failed``) surface incrementally as the
  captured ``mode`` / record-count / cursor sequence, with **no loss, no
  duplication, no truncation** (the delivered records equal the on-disk records
  in order); and
* the retry boundary — the same step re-run appending to the **same** jsonl
  (engine writes are append-only) — surfaces as an append delta too; and
* the active ``engine.json`` is parsed **at most once per actual change** even
  though every per-tick reader touches it each tick.

The parse-count assertion is the bite: pre-fix the symbol it counts
(``_parse_engine_json``) did not exist and each reader re-parsed the file every
tick, so this test fails before the fix and passes after.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import se3.daemon.history as history_mod
from se3.daemon.history import DaemonHistoryReader
from se3.daemon.protocol import HISTORY_MODE_APPEND, HISTORY_MODE_FULL

# --------------------------------------------------------------------------
# real captured fixtures (see tests/frontend/fixtures/issue_209/README.md)
# --------------------------------------------------------------------------

_FIXTURE_DIR = Path(__file__).parent / "frontend" / "fixtures" / "issue_209"
_JSONL_NAMES = (
    "01_discovery_d91c8bf8.jsonl",
    "02_analyze_5f00d94e.jsonl",
    "03_plan_2b8171e0.jsonl",
)


def _load_fixture_lines() -> dict[str, list[str]]:
    """Return each real fixture jsonl as its list of raw (newline-kept) lines."""
    out: dict[str, list[str]] = {}
    for name in _JSONL_NAMES:
        out[name] = (_FIXTURE_DIR / name).read_text(encoding="utf-8").splitlines(
            keepends=True
        )
    return out


def _load_captured_frames() -> list[dict]:
    """The real daemon frame sequence these records produce (``read_active_flows``)."""
    return json.loads((_FIXTURE_DIR / "daemon_frames.json").read_text())["frames"]


def _ondisk_messages() -> list[dict]:
    """Every on-disk record, in file then line order — the no-loss ground truth.

    The daemon delivers each line verbatim as the record envelope's ``message``
    (it injects ``step_id`` / ``step_type`` at the envelope layer from the
    file-name and leaves the line body untouched — verified against the captured
    frames), so the concatenation of the on-disk lines is exactly what a
    no-loss / no-duplication / no-truncation read must deliver, in order.
    """
    lines = _load_fixture_lines()
    return [json.loads(line) for name in _JSONL_NAMES for line in lines[name]]


def _write_active_engine(root: Path, flow_id: str, *, blob_steps: int = 0) -> None:
    """Write a RUNNING active ``engine.json``; ``blob_steps`` inflates its size.

    A long-running flow's ``engine.json`` grows to ~1 MB; that large parse is
    the per-tick CPU sink #209 traced. ``blob_steps`` mimics it so the
    parse-count guard measures the operation the fix collapses.
    """
    state_dir = root / "se3" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    payload: dict = {"flow_id": flow_id, "status": "RUNNING"}
    if blob_steps:
        payload["state"] = {
            "steps": {
                f"{i:02d}_step_{i:08x}": {"status": "RUNNING", "blob": "x" * 600}
                for i in range(blob_steps)
            }
        }
    (state_dir / "engine.json").write_text(json.dumps(payload), encoding="utf-8")


def _replay_real_frames(reader: DaemonHistoryReader, hist_dir: Path, flow_id: str):
    """Drive ``reader`` through the captured tick sequence, staging the real lines.

    For each captured frame, the on-disk jsonl files are grown to that frame's
    cursor line-counts (a brand-new file the first time it appears, otherwise an
    append) exactly as the engine wrote them across the pause→resume /
    discovery→analyze / plan-failure boundaries, then ``read_active_flows`` is
    called the way the daemon push loop does (with the index cache invalidated,
    as a continuously-appending flow forces each tick). Returns the list of
    produced :class:`FlowRead` lists, one per tick.
    """
    lines = _load_fixture_lines()
    frames = _load_captured_frames()
    hist_dir.mkdir(parents=True, exist_ok=True)
    written = {name: 0 for name in _JSONL_NAMES}
    cursors: dict = {}
    produced = []
    for frame in frames:
        for name, target in frame["cursor"].items():
            have = written[name]
            if target > have:
                with (hist_dir / name).open("a", encoding="utf-8") as fh:
                    fh.writelines(lines[name][have:target])
                written[name] = target
        reader.invalidate_index_cache()  # an appending flow invalidates each tick
        reads = reader.read_active_flows(cursors=cursors)
        cursors = {r.flow_id: r.cursor for r in reads}
        produced.append(reads)
    return produced


# --------------------------------------------------------------------------
# correctness: the real frame sequence is reproduced, with no loss / dup / trunc
# --------------------------------------------------------------------------


def test_real_frame_sequence_matches_captured_daemon_frames(tmp_path):
    """Replaying the real on-disk records reproduces the captured frame sequence.

    Covers BOTH #209 trigger boundaries in one real flow: the discovery→analyze
    transition (a brand-new ``02_analyze`` jsonl, read inside an overall
    ``mode:append`` frame) and the step error (``03_plan`` ends in
    ``step_failed``). The produced ``mode`` / record-count / cursor of each tick
    must equal the real captured ``daemon_frames.json`` — the daemon reader does
    NOT drop, duplicate, or truncate the transition / failure batch.
    """
    root = tmp_path / "proj"
    flow_id = "20260618-125615_issue209"
    _write_active_engine(root, flow_id, blob_steps=40)
    reader = DaemonHistoryReader(project_roots_provider=lambda: [root])

    produced = _replay_real_frames(reader, root / "se3" / "history" / flow_id, flow_id)
    captured = _load_captured_frames()
    assert len(produced) == len(captured)

    for tick, (reads, frame) in enumerate(zip(produced, captured)):
        assert len(reads) == 1, f"tick {tick}: one active flow expected, got {reads}"
        read = reads[0]
        assert read.flow_id == flow_id
        assert read.mode == frame["mode"], (
            f"tick {tick}: mode {read.mode!r} != captured {frame['mode']!r}"
        )
        assert len(read.records) == len(frame["records"]), (
            f"tick {tick}: {len(read.records)} records != captured "
            f"{len(frame['records'])} (frame dropped/duplicated)"
        )
        # cursor is keyed by jsonl file-name, identical across flows.
        assert read.cursor == frame["cursor"], (
            f"tick {tick}: cursor {read.cursor} != captured {frame['cursor']}"
        )

    # The transition specifics: analyze records appear ONLY in the final
    # (resume-burst) tick, and as an append delta — never in the first full read.
    first_step_ids = {r["step_id"] for r in produced[0][0].records}
    assert not any("analyze" in s for s in first_step_ids)
    last = produced[-1][0]
    assert last.mode == HISTORY_MODE_APPEND
    last_step_ids = {r["step_id"] for r in last.records}
    assert any("analyze" in s for s in last_step_ids), "analyze step never surfaced"
    assert any("plan" in s for s in last_step_ids), "plan step never surfaced"


def test_real_frame_replay_no_loss_no_dup_no_truncation(tmp_path):
    """Across the whole replay the delivered records equal the on-disk records.

    The strongest no-loss / no-duplication / no-truncation / correct-order
    assertion: gather every record delivered across all ticks and require it to
    equal, item-for-item and in order, the concatenation of the real on-disk
    fixture lines (discovery 8 + analyze 6 + plan 6 = 20).
    """
    root = tmp_path / "proj"
    flow_id = "20260618-125615_issue209"
    _write_active_engine(root, flow_id, blob_steps=40)
    reader = DaemonHistoryReader(project_roots_provider=lambda: [root])

    produced = _replay_real_frames(reader, root / "se3" / "history" / flow_id, flow_id)
    delivered = [rec["message"] for reads in produced for r in reads for rec in r.records]

    expected = _ondisk_messages()
    assert len(delivered) == len(expected) == 20
    assert delivered == expected, (
        "the delivered live-append records must equal the on-disk records "
        "in order — no loss, no duplication, no truncation"
    )


def test_retry_after_error_same_jsonl_append_surfaces(tmp_path):
    """The retry boundary (same step_id, appended to the SAME jsonl) surfaces.

    #209's second trigger: after ``03_plan`` ends in ``step_failed`` the
    operator retries; the engine re-runs the step under the SAME step_id and
    **appends** more records to the same jsonl (engine writes are append-only —
    no truncate/rewrite, per the G1 diagnosis). Those appended retry records
    must stream incrementally as a fresh append delta with the parse cache on,
    with no loss and no re-delivery of the already-read failure records.
    """
    root = tmp_path / "proj"
    flow_id = "20260618-125615_issue209"
    _write_active_engine(root, flow_id, blob_steps=40)
    reader = DaemonHistoryReader(project_roots_provider=lambda: [root])
    hist_dir = root / "se3" / "history" / flow_id

    # Run the real flow to its captured end (plan has just failed).
    produced = _replay_real_frames(reader, hist_dir, flow_id)
    cursors = {r.flow_id: r.cursor for r in produced[-1]}
    plan_jsonl = hist_dir / "03_plan_2b8171e0.jsonl"
    plan_lines_before = plan_jsonl.read_text(encoding="utf-8").count("\n")

    # The operator retries plan: the SAME jsonl gets MORE records appended under
    # the same step_id (a fresh step_started running anchor + assistant turns).
    retry_records = [
        {"type": "step_started", "step_id": "03_plan_2b8171e0",
         "step_type": "plan", "status": "running", "timestamp": "2026-06-18T13:00:00"},
        {"role": "assistant", "step_type": "plan",
         "content": "Retrying the plan step.", "timestamp": "2026-06-18T13:00:01"},
        {"type": "step_completed", "step_id": "03_plan_2b8171e0",
         "step_type": "plan", "timestamp": "2026-06-18T13:00:02"},
    ]
    with plan_jsonl.open("a", encoding="utf-8") as fh:
        for rec in retry_records:
            fh.write(json.dumps(rec) + "\n")
    _write_active_engine(root, flow_id, blob_steps=41)  # engine.json advances
    reader.invalidate_index_cache()

    reads = reader.read_active_flows(cursors=cursors)
    assert len(reads) == 1
    read = reads[0]
    assert read.mode == HISTORY_MODE_APPEND, "retry append must be a delta, not a full re-read"
    delivered = [rec["message"] for rec in read.records]
    assert delivered == retry_records, (
        "exactly the appended retry records surface — none of the prior "
        "(already-read) failure records are re-delivered, none of the new ones lost"
    )
    # The cursor advanced past the failure records to the new tail.
    assert read.cursor["03_plan_2b8171e0.jsonl"] == plan_lines_before + len(retry_records)


# --------------------------------------------------------------------------
# the lock: the active engine.json is parsed at most once per actual change
# --------------------------------------------------------------------------


def _count_engine_parses(monkeypatch) -> dict:
    """Patch ``history._parse_engine_json`` to count active-engine.json *parses*.

    ``_read_engine_cached`` always *reads* the file (cheap) but only *parses*
    (``_parse_engine_json``, the GIL-bound ``json.loads``) when the raw content
    changed; counting that single seam measures exactly the expensive operation
    the #209 fix collapses. With a single project root there is exactly one
    ``engine.json``, so every call counts that file's parses.

    Pre-fix the ``_parse_engine_json`` symbol did not exist (``_read_json``
    parsed inline, uncached), so this patch fails to bind and the test errors —
    the regression lock biting before the fix.
    """
    counter = {"n": 0}
    original = history_mod._parse_engine_json

    def counting_parse(raw):
        counter["n"] += 1
        return original(raw)

    monkeypatch.setattr(history_mod, "_parse_engine_json", counting_parse)
    return counter


def test_real_frame_replay_parses_active_engine_json_once_per_change(tmp_path, monkeypatch):
    """Replaying the real frames parses the active engine.json once, not per reader.

    The regression bite: every per-tick reader that touches the active
    ``engine.json`` (``active_flow_signature``, ``build_index`` →
    ``_index_root``, ``read_active_flows`` → ``_is_still_active``,
    ``live_flow_ids``) runs each tick while the file is UNCHANGED across the
    replay. The large parse must happen at most once — pre-fix each call
    re-parsed it, growing with tick × reader count, which is the starvation that
    kept the discovery→analyze / retry live-append frame from ever being pushed.
    """
    root = tmp_path / "proj"
    flow_id = "20260618-125615_issue209"
    _write_active_engine(root, flow_id, blob_steps=1200)  # ~1 MB, like a real flow
    counter = _count_engine_parses(monkeypatch)

    reader = DaemonHistoryReader(project_roots_provider=lambda: [root])
    lines = _load_fixture_lines()
    frames = _load_captured_frames()
    hist_dir = root / "se3" / "history" / flow_id
    hist_dir.mkdir(parents=True, exist_ok=True)
    written = {name: 0 for name in _JSONL_NAMES}
    cursors: dict = {}

    for frame in frames:
        for name, target in frame["cursor"].items():
            have = written[name]
            if target > have:
                with (hist_dir / name).open("a", encoding="utf-8") as fh:
                    fh.writelines(lines[name][have:target])
                written[name] = target
        # Every per-tick reader the real push loop / aggregator touches, with the
        # active engine.json UNCHANGED across the whole replay.
        reader.active_flow_signature()
        reader.invalidate_index_cache()
        reads = reader.read_active_flows(cursors=cursors)
        cursors = {r.flow_id: r.cursor for r in reads}
        reader.live_flow_ids()

    assert counter["n"] <= 1, (
        f"active engine.json was parsed {counter['n']} times across an unchanged "
        f"{len(frames)}-tick real-frame replay (4 readers/tick); the per-tick "
        "re-parse starvation that froze the WebUI regressed"
    )


def test_active_engine_json_reparsed_on_transition_and_retry(tmp_path, monkeypatch):
    """A genuine engine.json rewrite (transition / retry) is re-parsed, not stale.

    The content-keyed cache must serve a fresh parse across a real change: the
    discovery→analyze transition and the retry boundary both rewrite the active
    ``engine.json`` (new step / status), and the reader must observe the new
    state rather than a stale cached parse — otherwise the fix would trade the
    freeze for a different staleness bug.
    """
    root = tmp_path / "proj"
    flow_id = "20260618-125615_issue209"
    _write_active_engine(root, flow_id, blob_steps=10)
    counter = _count_engine_parses(monkeypatch)
    reader = DaemonHistoryReader(project_roots_provider=lambda: [root])

    assert reader.live_flow_ids() == {flow_id}
    first = counter["n"]
    assert first >= 1

    # Unchanged re-read is served from the cache — no new parse.
    reader.live_flow_ids()
    assert counter["n"] == first

    # A step transition / retry rewrites engine.json with different content.
    _write_active_engine(root, flow_id, blob_steps=60)
    reader.live_flow_ids()
    assert counter["n"] == first + 1, (
        "a genuine engine.json rewrite must invalidate the content-keyed cache "
        "(no stale parse across a transition / retry)"
    )
