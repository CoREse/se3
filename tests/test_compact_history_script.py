"""Tests for scripts/compact_history_jsonl.py (stored-record backfill).

The script rewrites irreplaceable conversation history in place, so what is
locked in here is mostly what it must NOT do: it must write nothing without
``--apply``, and when it does write, the physical line count and line order must
be identical to the source, because ``ordinal`` -- the key the WebUI reconciles
bundles by -- IS the physical line number. Lines that cannot be compacted (empty
lines, a file with no trailing newline, unparseable JSON, a compaction product
that is not one valid JSON line) must survive byte-for-byte, and the line-count
guard must abandon the rewrite rather than land a file that shifts ordinals.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "compact_history_jsonl.py"
)
_spec = importlib.util.spec_from_file_location("compact_history_jsonl", _SCRIPT)
compact_mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
# ``@dataclass`` looks its own module up in ``sys.modules``; register before exec.
sys.modules[_spec.name] = compact_mod
_spec.loader.exec_module(compact_mod)

record_budget = compact_mod.record_budget


def _small_record(index: int) -> dict:
    return {
        "role": "assistant",
        "content": "small record %d" % index,
        "raw_json": [{"type": "assistant", "message": {"content": "hi %d" % index}}],
    }


def _oversized_record(chips: int = 6, body_bytes: int = 400_000) -> dict:
    """A record shaped like the pathological one: many chips, huge bodies."""
    events = [{"type": "system", "subtype": "init", "session_id": "s-1"}]
    for i in range(chips):
        events.append(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_%d" % i,
                            "name": "Read",
                            "input": {"file_path": "/tmp/f%d" % i},
                        }
                    ]
                },
            }
        )
        # Zero-render telemetry between chips -- foldable, must not disturb order.
        events.append({"type": "system", "subtype": "thinking_tokens", "count": i})
        events.append(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_%d" % i,
                            "content": "X" * body_bytes,
                        }
                    ]
                },
            }
        )
    events.append(
        {"type": "result", "subtype": "success", "usage": {"input_tokens": 12}}
    )
    return {
        "role": "assistant",
        "content": "oversized",
        "token_usage": {"input_tokens": 12, "output_tokens": 34},
        "raw_json": events,
    }


def _dump(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False, default=str)


def _chip_ids(record: dict) -> list:
    """(kind, tool_use_id) for every tool chip, in document order."""
    chips = []
    for event in record.get("raw_json", []):
        message = event.get("message") if isinstance(event, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                chips.append(("tool_use", block.get("id")))
            elif block.get("type") == "tool_result":
                chips.append(("tool_result", block.get("tool_use_id")))
    return chips


@pytest.fixture()
def history(tmp_path: Path) -> Path:
    """A history dir whose one step file mixes every line shape that matters."""
    hist = tmp_path / "history"
    flow = hist / "20260706-013803_96453dd6"
    flow.mkdir(parents=True)
    step = flow / "01_discovery_abcd1234.jsonl"

    lines = [
        _dump(_small_record(0)),
        _dump(_oversized_record()),
        "",  # empty line -- occupies an ordinal slot, must be preserved
        _dump(_small_record(1)),
        "{not json at all}",  # unparseable -- must be written back verbatim
        _dump(_oversized_record(chips=3)),
        _dump(_small_record(2)),  # last line, deliberately no trailing newline
    ]
    step.write_text("\n".join(lines), encoding="utf-8")
    return hist


def _read_lines(path: Path) -> list:
    return path.read_bytes().split(b"\n")


def _step_file(history_dir: Path) -> Path:
    return next(history_dir.glob("*/*.jsonl"))


# --------------------------------------------------------------------------
# dry-run is the default and writes nothing
# --------------------------------------------------------------------------


def test_no_args_is_dry_run_and_writes_nothing(history: Path) -> None:
    step = _step_file(history)
    before = step.read_bytes()
    before_mtime = step.stat().st_mtime_ns

    assert compact_mod.main(["--history-dir", str(history)]) == 0

    assert step.read_bytes() == before
    assert step.stat().st_mtime_ns == before_mtime
    assert list(history.glob("*/*.compact-tmp")) == []


def test_dry_run_reports_accurate_statistics(history: Path) -> None:
    step = _step_file(history)
    exit_code, run = compact_mod.compact_history(history, apply=False)

    assert exit_code == 0
    assert len(run.files) == 1
    report = run.files[0]
    assert report.total_lines == 7
    assert report.oversized_lines == 2
    assert report.compacted_lines == 2
    assert report.failed_lines == 0
    assert report.rewritten is False
    assert report.saved_bytes > 0
    assert report.compacted_bytes < report.oversized_bytes
    # Reported savings must match what an --apply pass really achieves.
    size_before = step.stat().st_size
    compact_mod.compact_history(history, apply=True)
    assert size_before - step.stat().st_size == report.saved_bytes


def test_explicit_dry_run_overrides_apply(history: Path) -> None:
    step = _step_file(history)
    before = step.read_bytes()

    assert compact_mod.main(["--history-dir", str(history), "--apply", "--dry-run"]) == 0

    assert step.read_bytes() == before


# --------------------------------------------------------------------------
# --apply: 1:1 line rewrite
# --------------------------------------------------------------------------


def test_apply_preserves_line_count_order_and_ordinals(history: Path) -> None:
    step = _step_file(history)
    before = _read_lines(step)

    assert compact_mod.main(["--history-dir", str(history), "--apply"]) == 0

    after = _read_lines(step)
    assert len(after) == len(before)
    assert step.stat().st_size < sum(len(line) + 1 for line in before)

    # ordinal == physical line number: every non-compacted line, and every line
    # position, is exactly where it was.
    assert after[0] == before[0]  # small record untouched
    assert after[2] == b""  # empty line still an empty line
    assert after[3] == before[3]
    assert after[4] == before[4]  # unparseable line byte-identical
    assert after[6] == before[6]  # last line, still no trailing newline
    assert not step.read_bytes().endswith(b"\n")

    # The two oversized lines shrank but stay parseable at their own ordinals.
    for ordinal in (1, 5):
        assert len(after[ordinal]) < len(before[ordinal])
        assert json.loads(after[ordinal])["role"] == "assistant"


def test_apply_keeps_every_line_valid_json(history: Path) -> None:
    step = _step_file(history)
    assert compact_mod.main(["--history-dir", str(history), "--apply"]) == 0

    for ordinal, line in enumerate(_read_lines(step)):
        if not line or line == b"{not json at all}":
            continue
        record = json.loads(line)
        assert isinstance(record, dict), "line %d" % ordinal


def test_apply_preserves_all_tool_chips_and_usage(history: Path) -> None:
    step = _step_file(history)
    before_chips = _chip_ids(json.loads(_read_lines(step)[1]))
    assert before_chips  # fixture really does carry chips

    assert compact_mod.main(["--history-dir", str(history), "--apply"]) == 0

    after_record = json.loads(_read_lines(step)[1])
    assert _chip_ids(after_record) == before_chips
    assert after_record["token_usage"] == {"input_tokens": 12, "output_tokens": 34}
    # The terminal result event is compaction-immune and stays last.
    assert after_record["raw_json"][-1] == {
        "type": "result",
        "subtype": "success",
        "usage": {"input_tokens": 12},
    }
    assert len(_dump(after_record).encode("utf-8")) <= (
        record_budget.MAX_RECORD_RAW_JSON_BYTES + 64 * 1024
    )


def test_apply_is_idempotent(history: Path) -> None:
    step = _step_file(history)
    assert compact_mod.main(["--history-dir", str(history), "--apply"]) == 0
    once = step.read_bytes()

    assert compact_mod.main(["--history-dir", str(history), "--apply"]) == 0

    assert step.read_bytes() == once


def test_file_without_oversized_lines_is_never_touched(tmp_path: Path) -> None:
    hist = tmp_path / "history"
    flow = hist / "20260706-013803_96453dd6"
    flow.mkdir(parents=True)
    step = flow / "01_analyze_x.jsonl"
    step.write_text("\n".join(_dump(_small_record(i)) for i in range(5)) + "\n")
    before = step.read_bytes()
    before_mtime = step.stat().st_mtime_ns

    exit_code, run = compact_mod.compact_history(hist, apply=True)

    assert exit_code == 0
    assert step.read_bytes() == before
    assert step.stat().st_mtime_ns == before_mtime
    assert run.files[0].total_lines == 5
    assert run.files[0].oversized_lines == 0
    assert run.files[0].rewritten is False


def test_flow_id_restricts_the_pass(history: Path) -> None:
    other = history / "20260706-999999_00000000"
    other.mkdir()
    other_step = other / "01_plan_y.jsonl"
    other_step.write_text(_dump(_oversized_record()) + "\n")
    before = other_step.read_bytes()

    assert (
        compact_mod.main(
            [
                "--history-dir",
                str(history),
                "--flow-id",
                "20260706-013803_96453dd6",
                "--apply",
            ]
        )
        == 0
    )

    assert other_step.read_bytes() == before
    assert _step_file(history).stat().st_size < len(before) * 2


# --------------------------------------------------------------------------
# guards
# --------------------------------------------------------------------------


def test_line_count_guard_blocks_replacement(
    history: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transform that splits a line must never reach the real file."""
    step = _step_file(history)
    before = step.read_bytes()
    real_compact_line = compact_mod.compact_line

    def splitting_compact_line(line: bytes):
        new_line, outcome = real_compact_line(line)
        if outcome.compacted:
            # Inject the exact catastrophe the guard exists for: one record
            # written as two physical lines, shifting every later ordinal.
            return b'{"role": "assistant"}\n' + new_line, outcome
        return new_line, outcome

    monkeypatch.setattr(compact_mod, "compact_line", splitting_compact_line)

    exit_code = compact_mod.main(["--history-dir", str(history), "--apply"])

    assert exit_code != 0
    assert step.read_bytes() == before
    assert list(history.glob("*/*.compact-tmp")) == []


def test_line_count_guard_blocks_dropped_line(
    history: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transform that drops a line's terminator must be rejected too."""
    step = _step_file(history)
    before = step.read_bytes()
    real_compact_line = compact_mod.compact_line

    def dropping_compact_line(line: bytes):
        new_line, outcome = real_compact_line(line)
        if outcome.compacted:
            return b"", outcome
        return new_line, outcome

    monkeypatch.setattr(compact_mod, "compact_line", dropping_compact_line)

    assert compact_mod.main(["--history-dir", str(history), "--apply"]) != 0
    assert step.read_bytes() == before


def test_compaction_failure_writes_the_line_back_verbatim(
    history: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    step = _step_file(history)
    before = step.read_bytes()

    def boom(message, raw_len=None):
        raise RuntimeError("compaction exploded")

    monkeypatch.setattr(compact_mod.record_budget, "compact_record", boom)

    exit_code, run = compact_mod.compact_history(history, apply=True)

    assert exit_code == 0
    assert step.read_bytes() == before
    assert run.files[0].failed_lines == 2
    assert run.files[0].compacted_lines == 0
    assert run.files[0].rewritten is False


def test_multiline_product_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A compaction product spanning two lines must be discarded, not stored."""
    line = (_dump(_oversized_record()) + "\n").encode("utf-8")

    def compact_with_newline(message, raw_len=None):
        stats = record_budget.CompactionStats(compacted=True)
        return {"role": "assistant", "content": "a\nb"}, stats

    monkeypatch.setattr(compact_mod.record_budget, "compact_record", compact_with_newline)
    monkeypatch.setattr(
        compact_mod, "_dumps", lambda record: json.dumps(record).replace("\\n", "\n")
    )

    new_line, outcome = compact_mod.compact_line(line)

    assert new_line == line
    assert outcome.failed is True
    assert outcome.compacted is False


def test_compact_line_preserves_terminators() -> None:
    with_newline = (_dump(_oversized_record()) + "\n").encode("utf-8")
    without_newline = _dump(_oversized_record()).encode("utf-8")

    a, outcome_a = compact_mod.compact_line(with_newline)
    b, outcome_b = compact_mod.compact_line(without_newline)

    assert outcome_a.compacted and outcome_b.compacted
    assert a.endswith(b"\n") and not a.endswith(b"\n\n")
    assert not b.endswith(b"\n")


def test_small_line_takes_the_fast_path_untouched() -> None:
    line = (_dump(_small_record(0)) + "\n").encode("utf-8")

    new_line, outcome = compact_mod.compact_line(line)

    assert new_line is line
    assert outcome.oversized is False
    assert outcome.compacted is False


def test_count_physical_lines_matches_ordinal_numbering(tmp_path: Path) -> None:
    trailing = tmp_path / "trailing.jsonl"
    trailing.write_bytes(b"a\nb\nc\n")
    partial = tmp_path / "partial.jsonl"
    partial.write_bytes(b"a\nb\nc")
    empty = tmp_path / "empty.jsonl"
    empty.write_bytes(b"")
    blanks = tmp_path / "blanks.jsonl"
    blanks.write_bytes(b"a\n\n\nb\n")

    assert compact_mod.count_physical_lines(trailing) == 3
    assert compact_mod.count_physical_lines(partial) == 3
    assert compact_mod.count_physical_lines(empty) == 0
    assert compact_mod.count_physical_lines(blanks) == 4


def test_missing_history_dir_exits_nonzero(tmp_path: Path) -> None:
    assert compact_mod.main(["--history-dir", str(tmp_path / "nope")]) == 1
