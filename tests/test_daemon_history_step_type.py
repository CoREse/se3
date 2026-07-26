"""Tests for daemon-side step_type file-name parsing and envelope injection.

Covers :func:`tianluo.daemon.history.parse_step_type_from_step_id` across all
file-name conventions (sequence prefix, underscore-bearing type names, ``_Gk``
group suffixes, hexadecimal hashes, legacy non-conforming names) and verifies
that :meth:`DaemonHistoryReader.read_flow` injects the authoritative
``step_type`` at the record envelope without mutating ``message`` content.
"""

from __future__ import annotations

import json

import pytest

from tianluo.daemon.history import (
    DaemonHistoryReader,
    parse_step_type_from_step_id,
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _write_jsonl(path, lines):
    """Write *lines* (list of dicts) as a jsonl file at *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )


def _make_reader(*roots):
    return DaemonHistoryReader(project_roots_provider=lambda: list(roots))


# --------------------------------------------------------------------------
# parser — happy path / acceptance examples
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stem, expected",
    [
        # The three acceptance-criteria examples.
        ("01_discovery_975607bb", "discovery"),
        ("13_version_analyze_def456", "version_analyze"),
        ("05_implement_61605e42_G2", "implement"),
        # Other simple single-word types with a hex hash.
        ("02_plan_abc123", "plan"),
        ("04_test_deadbeef", "test"),
        ("99_commit_0", "commit"),
    ],
)
def test_parse_known_types_with_seq_and_hash(stem, expected):
    assert parse_step_type_from_step_id(stem) == expected


def test_parse_underscore_type_name():
    """A type whose name itself contains underscores survives hash stripping."""
    assert parse_step_type_from_step_id("13_version_analyze_def456") == "version_analyze"
    assert parse_step_type_from_step_id("07_self_check_aa11bb22") == "self_check"
    assert parse_step_type_from_step_id("08_verify_spec_ff00") == "verify_spec"
    assert parse_step_type_from_step_id("09_update_spec_1234abcd") == "update_spec"
    assert parse_step_type_from_step_id("03_plan_tasks_c0ffee") == "plan_tasks"
    assert (
        parse_step_type_from_step_id("01_project_summary_dead00") == "project_summary"
    )


@pytest.mark.parametrize(
    "stem, expected",
    [
        ("05_implement_61605e42_G2", "implement"),
        ("06_implement_aabbcc_G10", "implement"),
        ("13_version_analyze_def456_G1", "version_analyze"),
        ("07_self_check_aa11_G3", "self_check"),
    ],
)
def test_parse_group_suffix(stem, expected):
    """The optional ``_Gk`` group suffix is stripped before the hash."""
    assert parse_step_type_from_step_id(stem) == expected


def test_parse_no_hash_just_seq():
    """A conforming name without a hash tail still resolves to the type."""
    assert parse_step_type_from_step_id("01_discovery") == "discovery"
    assert parse_step_type_from_step_id("13_version_analyze") == "version_analyze"


# --------------------------------------------------------------------------
# parser — legacy / fallback / robustness
# --------------------------------------------------------------------------


def test_parse_legacy_name_falls_back_to_stem():
    """Old names with no NN prefix and no hash tail fall back, never raise."""
    # commit_summary: no sequence prefix, "summary" is not hex -> not stripped.
    assert parse_step_type_from_step_id("commit_summary") == "commit_summary"


def test_parse_unknown_but_conforming_returns_middle():
    """A future type following the convention parses to its middle segment."""
    # Not in the known set, but clearly NN_<type>_<hash>: stay self-describing.
    assert parse_step_type_from_step_id("12_newstep_abc123") == "newstep"
    assert parse_step_type_from_step_id("12_brand_new_step_abc123") == "brand_new_step"


def test_parse_empty_and_non_string():
    assert parse_step_type_from_step_id("") == ""
    assert parse_step_type_from_step_id("   ") == ""
    assert parse_step_type_from_step_id(None) == ""  # type: ignore[arg-type]
    assert parse_step_type_from_step_id(123) == ""  # type: ignore[arg-type]


def test_parse_is_pure_no_side_effects():
    """Calling the parser repeatedly is deterministic and mutates nothing."""
    stem = "05_implement_61605e42_G2"
    first = parse_step_type_from_step_id(stem)
    second = parse_step_type_from_step_id(stem)
    assert first == second == "implement"
    # The input string is unchanged (strings are immutable, but assert intent).
    assert stem == "05_implement_61605e42_G2"


# --------------------------------------------------------------------------
# read_flow — envelope injection
# --------------------------------------------------------------------------


def test_read_flow_injects_step_type_envelope(tmp_path):
    """Each returned record carries an authoritative envelope ``step_type``."""
    hist = tmp_path / "se3" / "history" / "f1"
    _write_jsonl(
        hist / "01_discovery_975607bb.jsonl",
        [{"role": "assistant", "content": "hi"}],
    )
    _write_jsonl(
        hist / "13_version_analyze_def456.jsonl",
        [{"role": "assistant", "content": "v"}],
    )
    _write_jsonl(
        hist / "05_implement_61605e42_G2.jsonl",
        [{"role": "assistant", "content": "code"}],
    )

    read = _make_reader(tmp_path).read_flow("f1")
    by_step = {r["step_id"]: r for r in read.records}

    assert by_step["01_discovery_975607bb"]["step_type"] == "discovery"
    assert by_step["13_version_analyze_def456"]["step_type"] == "version_analyze"
    assert by_step["05_implement_61605e42_G2"]["step_type"] == "implement"


def test_read_flow_does_not_mutate_message_content(tmp_path):
    """The injected envelope leaves the original ``message`` bytes untouched."""
    hist = tmp_path / "se3" / "history" / "f1"
    original = {
        "role": "assistant",
        "content": "payload",
        "raw_json": [{"type": "text", "text": "payload"}],
        "timestamp": "2026-05-21T00:00:00",
    }
    _write_jsonl(hist / "01_discovery_975607bb.jsonl", [original])

    read = _make_reader(tmp_path).read_flow("f1")
    assert len(read.records) == 1
    record = read.records[0]
    # Envelope field is present...
    assert record["step_type"] == "discovery"
    assert record["step_id"] == "01_discovery_975607bb"
    # ...and the message dict is byte-for-byte the original record.
    assert record["message"] == original


def test_read_flow_legacy_step_completed_event_not_broken(tmp_path):
    """A step_completed event line (already carrying step_type) round-trips.

    The envelope injection adds a sibling ``step_type`` field; it must not
    overwrite or corrupt a message body that already carries its own
    ``step_type`` (e.g. a HistorySink ``step_completed`` event).
    """
    hist = tmp_path / "se3" / "history" / "f1"
    event = {
        "type": "step_completed",
        "step_type": "analyze",
        "outputs": {"summary": "done"},
    }
    _write_jsonl(hist / "02_analyze_cafe1234.jsonl", [event])

    read = _make_reader(tmp_path).read_flow("f1")
    assert len(read.records) == 1
    record = read.records[0]
    assert record["step_type"] == "analyze"  # parsed from file name
    assert record["message"] == event  # inner event untouched
    assert record["message"]["step_type"] == "analyze"


def test_read_flow_legacy_filename_envelope_falls_back(tmp_path):
    """A history file with a legacy stem yields a graceful envelope value."""
    hist = tmp_path / "se3" / "history" / "f1"
    _write_jsonl(
        hist / "commit_summary.jsonl",
        [{"role": "assistant", "content": "x"}],
    )

    read = _make_reader(tmp_path).read_flow("f1")
    assert len(read.records) == 1
    # Fallback is the original stem (acceptable, never raises).
    assert read.records[0]["step_type"] == "commit_summary"
