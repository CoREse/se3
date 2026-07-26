"""Tests for _extract_failures_section in test.py."""

from __future__ import annotations

import pytest

from tianluo.engine.steps.test import _extract_failures_section


# ---------------------------------------------------------------------------
# Standard pytest output with FAILURES section
# ---------------------------------------------------------------------------

STANDARD_PYTEST_OUTPUT = """\
============================= test session starts ==============================
platform linux -- Python 3.11.0, pytest-7.4.0
collected 5 items

tests/test_cli.py::test_add PASSED
tests/test_cli.py::test_list PASSED
tests/test_cli.py::test_delete FAILED
tests/test_cli.py::test_search FAILED
tests/test_cli.py::test_export PASSED

================================== FAILURES ===================================
_________________________________ test_delete _________________________________

    def test_delete():
        runner = CliRunner()
        result = runner.invoke(cli, ["delete", "1"])
>       assert result.exit_code == 0
E       AssertionError: assert 1 == 0
E        +  where 1 = <Result exit_code=1>.exit_code

tests/test_cli.py:42: AssertionError
_________________________________ test_search _________________________________

    def test_search():
        runner = CliRunner()
        result = runner.invoke(cli, ["search", "foo"])
>       assert "found" in result.output
E       AssertionError: assert 'found' in ''

tests/test_cli.py:55: AssertionError
=========================== short test summary info ============================
FAILED tests/test_cli.py::test_delete - AssertionError: assert 1 == 0
FAILED tests/test_cli.py::test_search - AssertionError: assert 'found' in ''
========================= 2 failed, 3 passed in 0.12s =========================
"""

ERRORS_SECTION_OUTPUT = """\
============================= test session starts ==============================
collected 3 items

================================== ERRORS =====================================
________________________ ERROR collecting test_broken.py _______________________

ModuleNotFoundError: No module named 'missing_dep'

=========================== short test summary info ============================
ERROR test_broken.py - ModuleNotFoundError
========================= 1 error in 0.05s ====================================
"""


class TestExtractFailuresSection:
    def test_empty_string(self):
        assert _extract_failures_section("") == ""

    def test_standard_failures_section(self):
        result = _extract_failures_section(STANDARD_PYTEST_OUTPUT)
        # Should contain the FAILURES header
        assert "FAILURES" in result
        # Should contain the actual assertion errors
        assert "test_delete" in result
        assert "test_search" in result
        assert "AssertionError" in result
        # Should NOT contain the session start or short summary
        # (the short summary is after the FAILURES section end)

    def test_errors_section(self):
        result = _extract_failures_section(ERRORS_SECTION_OUTPUT)
        assert "ERRORS" in result
        assert "ModuleNotFoundError" in result

    def test_no_failures_section_falls_back_to_tail(self):
        plain_output = "line1\nline2\nline3\nall tests passed\n"
        result = _extract_failures_section(plain_output, max_chars=20)
        # Should return the last 20 chars of the output
        assert result == plain_output[-20:]

    def test_respects_max_chars_on_fallback(self):
        long_output = "x" * 5000
        result = _extract_failures_section(long_output, max_chars=1000)
        assert len(result) <= 1000
        assert result == long_output[-1000:]

    def test_full_section_within_limit(self):
        """When FAILURES section is under max_chars, return it entirely."""
        result = _extract_failures_section(STANDARD_PYTEST_OUTPUT, max_chars=5000)
        assert "FAILURES" in result
        assert "test_delete" in result
        assert "test_search" in result

    def test_truncation_of_long_section(self):
        """When FAILURES section exceeds max_chars, truncate per-block."""
        # Build a very long FAILURES section
        blocks = []
        for i in range(10):
            block = f"__ test_func_{i} __\n"
            block += f"    def test_func_{i}():\n"
            block += "        " + "x = 1\n        " * 50  # lots of lines
            block += f">       assert False\n"
            block += f"E       AssertionError\n\n"
            block += f"tests/test_big.py:{i*100}: AssertionError\n"
            blocks.append(block)

        long_failures = (
            "== test session starts ==\n"
            "================================== FAILURES ===================================\n"
            + "".join(blocks)
            + "=========================== short test summary info ============================\n"
        )

        result = _extract_failures_section(long_failures, max_chars=2000)
        assert len(result) <= 2000
        assert "FAILURES" in result

    def test_section_extraction_stops_at_next_separator(self):
        """FAILURES section should not include the short test summary."""
        result = _extract_failures_section(STANDARD_PYTEST_OUTPUT, max_chars=5000)
        # The "short test summary info" line should NOT be in the extracted section
        assert "short test summary info" not in result

    def test_none_like_empty(self):
        """Edge case: whitespace-only strings."""
        result = _extract_failures_section("   \n   \n", max_chars=100)
        # Should just return the tail (whitespace)
        assert result.strip() == ""
