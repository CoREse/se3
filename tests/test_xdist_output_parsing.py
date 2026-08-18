"""Tests for pytest-xdist compatibility of the TEST step output parsers.

The fixtures under ``tests/fixtures/test_output/`` are *recorded* runs of the
same nine-test suite (3 files: passes, failures, skips, parametrized ids) —
once serially and once under ``pytest -n 2 --dist loadgroup``, both with
``-v -rs`` so the recordings carry the per-test lines, the FAILURES diagnostic
block, the ``-rs`` short-summary block and the final aggregate line.

Covers:
- ``_parse_test_ids`` / ``_parse_skipped_test_ids`` on the xdist recording;
- equivalence of the *conclusions* drawn from both recordings —
  ``_classify_results`` (new vs regression) and ``_detect_critical_failures``
  (critical skipped / missing) must reach identical verdicts, i.e. the gate
  stays live under parallel output instead of silently no-op'ing;
- ``_parse_test_summary_counts`` on the xdist recording (its generic
  ``N passed`` token search runs over every line, so the parallel output must
  not make it double-count);
- zero regression on the serial / jest / go formats, including that the
  ``-rs`` short-summary and FAILURES blocks are not mistaken for per-test
  lines.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tianluo.engine.steps.test import (
    _classify_results,
    _detect_critical_failures,
    _parse_skipped_test_ids,
    _parse_test_ids,
    _parse_test_summary_counts,
    _summarize_passed_phase_output,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "test_output"

# The recorded suite, as ground truth independent of either output format.
EXPECTED_PASSED = [
    "tests/test_acceptance.py::test_console_smoke",
    "tests/test_legacy.py::test_zeta",
    "tests/test_sample.py::test_alpha",
    "tests/test_sample.py::test_delta[1]",
    "tests/test_sample.py::test_delta[2]",
]
EXPECTED_FAILED = [
    "tests/test_legacy.py::test_epsilon",
    "tests/test_sample.py::test_beta",
]
EXPECTED_SKIPPED = [
    "tests/test_acceptance.py::test_render_paradigm_in_headless_browser",
    "tests/test_legacy.py::test_eta",
]


def _fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


@pytest.fixture
def xdist_output() -> str:
    return _fixture("pytest_xdist_verbose.txt")


@pytest.fixture
def serial_output() -> str:
    return _fixture("pytest_serial_verbose.txt")


# ---------------------------------------------------------------------------
# per-test parsing on the xdist recording
# ---------------------------------------------------------------------------

class TestParseTestIdsXdist:
    def test_parses_every_per_test_line(self, xdist_output):
        ids = _parse_test_ids(xdist_output)
        assert sorted(tid for tid, ok in ids if ok) == EXPECTED_PASSED
        assert sorted(tid for tid, ok in ids if not ok) == EXPECTED_FAILED

    def test_no_duplicates_and_stable_order(self, xdist_output):
        ids = _parse_test_ids(xdist_output)
        assert len(ids) == len(set(ids))
        # Order follows the position of the result lines in the recording,
        # which interleaves the two workers.
        assert ids == [
            ("tests/test_acceptance.py::test_console_smoke", True),
            ("tests/test_legacy.py::test_zeta", True),
            ("tests/test_sample.py::test_alpha", True),
            ("tests/test_sample.py::test_delta[1]", True),
            ("tests/test_sample.py::test_delta[2]", True),
            ("tests/test_legacy.py::test_epsilon", False),
            ("tests/test_sample.py::test_beta", False),
        ]

    def test_skipped_tests_are_not_reported_as_passed(self, xdist_output):
        ids = dict(_parse_test_ids(xdist_output))
        for tid in EXPECTED_SKIPPED:
            assert tid not in ids

    def test_dispatch_lines_without_status_are_ignored(self, xdist_output):
        # xdist prints a bare ``file::test`` line when a test is handed to a
        # worker; only the later ``[gwN] ... STATUS file::test`` line is a
        # result. Counting both would double every test.
        ids = _parse_test_ids(xdist_output)
        assert len(ids) == len(EXPECTED_PASSED) + len(EXPECTED_FAILED)

    def test_parses_skipped_ids(self, xdist_output):
        assert _parse_skipped_test_ids(xdist_output) == EXPECTED_SKIPPED

    def test_summary_counts_not_double_counted(self, xdist_output):
        assert _parse_test_summary_counts(xdist_output) == (5, 2)

    def test_passed_phase_summary_uses_per_test_counts(self, xdist_output):
        slimmed, _stderr = _summarize_passed_phase_output(xdist_output, "")
        assert "5 passed, 2 failed" in slimmed


class TestParseTestIdsXdistLineShapes:
    def test_percent_field_is_optional(self):
        stdout = "[gw0] PASSED tests/test_a.py::test_x\n"
        assert _parse_test_ids(stdout) == [("tests/test_a.py::test_x", True)]

    def test_two_digit_worker_ids(self):
        stdout = "[gw11] [ 99%] FAILED tests/test_a.py::test_x\n"
        assert _parse_test_ids(stdout) == [("tests/test_a.py::test_x", False)]

    def test_xpass_xfail_not_treated_as_pass_or_fail(self):
        stdout = (
            "[gw0] [ 50%] XFAIL tests/test_a.py::test_x\n"
            "[gw0] [100%] XPASS tests/test_a.py::test_y\n"
        )
        assert _parse_test_ids(stdout) == []

    def test_worker_prefixed_traceback_header_is_not_a_result(self):
        # The FAILURES block repeats the worker prefix on its own line.
        stdout = (
            "_________ test_x _________\n"
            "[gw0] linux -- Python 3.14.2 /usr/bin/python3\n"
        )
        assert _parse_test_ids(stdout) == []
        assert _parse_skipped_test_ids(stdout) == []

    def test_short_summary_lines_are_not_per_test_results(self):
        # ``-rA`` emits ``PASSED file::test`` lines that look like the xdist
        # shape minus the worker prefix; they must not be harvested (they would
        # duplicate the real per-test lines).
        stdout = (
            "=========== short test summary info ===========\n"
            "PASSED tests/test_a.py::test_x\n"
            "SKIPPED [1] tests/test_a.py:12: env\n"
            "FAILED tests/test_a.py::test_y - assert 1 == 2\n"
        )
        assert _parse_test_ids(stdout) == []
        assert _parse_skipped_test_ids(stdout) == []

    def test_mixed_serial_and_xdist_output_is_deduplicated(self):
        stdout = (
            "tests/test_a.py::test_x PASSED                  [ 50%]\n"
            "[gw0] [ 50%] PASSED tests/test_a.py::test_x\n"
            "[gw1] [100%] FAILED tests/test_a.py::test_y\n"
        )
        assert _parse_test_ids(stdout) == [
            ("tests/test_a.py::test_x", True),
            ("tests/test_a.py::test_y", False),
        ]

    def test_same_id_with_conflicting_status_keeps_both(self):
        # A rerun plugin reports the same id twice; collapsing to the first
        # verdict would hide the failure (or the recovery).
        stdout = (
            "[gw0] [ 50%] FAILED tests/test_a.py::test_x\n"
            "[gw0] [100%] PASSED tests/test_a.py::test_x\n"
        )
        assert _parse_test_ids(stdout) == [
            ("tests/test_a.py::test_x", False),
            ("tests/test_a.py::test_x", True),
        ]


# ---------------------------------------------------------------------------
# equivalence of downstream conclusions: xdist vs serial
# ---------------------------------------------------------------------------

TESTS_ADDED = ["tests/test_sample.py"]
CRITICAL_PATTERNS = [
    "test_render_paradigm_in_headless_browser",  # recorded as SKIPPED
    "test_console_smoke",                        # recorded as PASSED
    "test_never_written",                        # collected by nobody
]


def _normalized_classification(stdout: str) -> tuple[dict, dict]:
    new_tests, regression = _classify_results(stdout, TESTS_ADDED)
    for bucket in (new_tests, regression):
        bucket["passed"] = sorted(bucket["passed"])
        bucket["failed"] = sorted(bucket["failed"])
    return new_tests, regression


class TestClassificationEquivalence:
    def test_xdist_matches_serial(self, xdist_output, serial_output):
        assert _normalized_classification(xdist_output) == _normalized_classification(
            serial_output,
        )

    def test_xdist_classification_is_correct(self, xdist_output):
        new_tests, regression = _normalized_classification(xdist_output)
        assert new_tests["passed"] == [
            "tests/test_sample.py::test_alpha",
            "tests/test_sample.py::test_delta[1]",
            "tests/test_sample.py::test_delta[2]",
        ]
        assert new_tests["failed"] == ["tests/test_sample.py::test_beta"]
        assert new_tests["count"] == 4
        assert regression["passed"] == [
            "tests/test_acceptance.py::test_console_smoke",
            "tests/test_legacy.py::test_zeta",
        ]
        assert regression["failed"] == ["tests/test_legacy.py::test_epsilon"]
        assert regression["count"] == 3

    def test_serial_recording_still_classifies_identically(self, serial_output):
        # Zero-regression anchor: the serial format keeps its exact behaviour.
        new_tests, regression = _normalized_classification(serial_output)
        assert new_tests["count"] == 4
        assert regression["count"] == 3


class TestCriticalGateEquivalence:
    def _detect(self, stdout: str) -> tuple[list[str], list[str]]:
        ran_ids = [tid for tid, _passed in _parse_test_ids(stdout)]
        skipped_ids = _parse_skipped_test_ids(stdout)
        return _detect_critical_failures(ran_ids, skipped_ids, CRITICAL_PATTERNS)

    def test_xdist_matches_serial(self, xdist_output, serial_output):
        assert self._detect(xdist_output) == self._detect(serial_output)

    def test_gate_stays_live_under_xdist(self, xdist_output):
        critical_skipped, critical_missing = self._detect(xdist_output)
        # Skipped critical test is caught (skip != pass) ...
        assert critical_skipped == [
            "tests/test_acceptance.py::test_render_paradigm_in_headless_browser",
        ]
        # ... a pattern nothing collected is reported missing ...
        assert critical_missing == ["test_never_written"]
        # ... and a critical test that actually ran is left alone.
        assert "test_console_smoke" not in critical_missing

    def test_missing_detection_is_not_silently_disabled(self, xdist_output):
        # The guard only reports ``missing`` when the run produced parseable
        # per-test results; under xdist it must therefore parse something.
        assert _parse_test_ids(xdist_output)
        assert _parse_skipped_test_ids(xdist_output)


# ---------------------------------------------------------------------------
# zero regression on the other supported formats
# ---------------------------------------------------------------------------

class TestOtherFormatsUnchanged:
    def test_serial_pytest(self, serial_output):
        ids = _parse_test_ids(serial_output)
        assert sorted(tid for tid, ok in ids if ok) == EXPECTED_PASSED
        assert sorted(tid for tid, ok in ids if not ok) == EXPECTED_FAILED
        assert _parse_skipped_test_ids(serial_output) == EXPECTED_SKIPPED
        assert _parse_test_summary_counts(serial_output) == (5, 2)

    def test_jest(self):
        stdout = "  ✓ renders a button\n  ✕ explodes\n"
        assert _parse_test_ids(stdout) == [
            ("renders a button", True),
            ("explodes", False),
        ]

    def test_go_test(self):
        stdout = "--- PASS: TestFoo\n--- FAIL: TestBar\n"
        assert _parse_test_ids(stdout) == [("TestFoo", True), ("TestBar", False)]

    def test_cargo_summary_counts(self):
        stdout = "test result: ok. 5 passed; 0 failed; 0 ignored\n"
        assert _parse_test_summary_counts(stdout) == (5, 0)
