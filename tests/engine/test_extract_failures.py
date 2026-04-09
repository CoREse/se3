"""Tests for _extract_failures_section from test.py.

Covers four scenarios:
1. Standard pytest output with FAILURES section
2. Superlong output requiring per-block truncation
3. No FAILURES section (fallback to tail)
4. Empty input
Plus: ERRORS section extraction
"""

from __future__ import annotations

from se3.engine.steps.test import _extract_failures_section


# ---------------------------------------------------------------------------
# Helpers: realistic pytest output fragments
# ---------------------------------------------------------------------------

STANDARD_PYTEST_OUTPUT = """\
============================= test session starts ==============================
platform linux -- Python 3.11.0, pytest-7.4.0
collected 5 items

tests/test_cli.py::test_add PASSED
tests/test_cli.py::test_list PASSED
tests/test_cli.py::test_delete FAILED
tests/test_cli.py::test_search PASSED
tests/test_cli.py::test_export FAILED

================================== FAILURES ===================================
_________________________________ test_delete _________________________________

    def test_delete():
        result = runner.invoke(app, ["delete", "99"])
>       assert result.exit_code == 0
E       AssertionError: assert 1 == 0

tests/test_cli.py:42: AssertionError
_________________________________ test_export _________________________________

    def test_export():
        result = runner.invoke(app, ["export", "--format", "csv"])
>       assert "task1" in result.output
E       AssertionError: assert 'task1' in ''

tests/test_cli.py:58: AssertionError
=========================== short test summary info ============================
FAILED tests/test_cli.py::test_delete - AssertionError: assert 1 == 0
FAILED tests/test_cli.py::test_export - AssertionError: assert 'task1' in ''
============================== 2 failed, 3 passed ==============================
"""

ERRORS_PYTEST_OUTPUT = """\
============================= test session starts ==============================
collected 3 items

tests/test_cli.py::test_add PASSED

================================== ERRORS =====================================
________________________ ERROR collecting tests/bad.py ________________________

ImportError: cannot import name 'missing' from 'module'

=========================== short test summary info ============================
ERROR tests/bad.py - ImportError: cannot import name 'missing'
============================== 1 passed, 1 error ==============================
"""


class TestExtractFailuresStandard:
    """Standard pytest output with a FAILURES section."""

    def test_extracts_failures_section(self):
        result = _extract_failures_section(STANDARD_PYTEST_OUTPUT)
        # Should contain the FAILURES header
        assert "FAILURES" in result
        # Should contain both test failure blocks
        assert "test_delete" in result
        assert "test_export" in result
        # Should contain assertion details
        assert "AssertionError" in result

    def test_does_not_include_summary_after_failures(self):
        result = _extract_failures_section(STANDARD_PYTEST_OUTPUT)
        # The "short test summary info" section should NOT be included
        # (it comes after the FAILURES section ends)
        assert "short test summary info" not in result


class TestExtractFailuresLongOutput:
    """Superlong output that requires per-block truncation."""

    def test_truncates_long_blocks(self):
        # Build a FAILURES section with many large blocks
        blocks = []
        for i in range(10):
            # Each block is ~500 chars
            block_header = f"{'_' * 30} test_func_{i} {'_' * 30}"
            traceback_lines = "\n".join(
                [f"    line {j}: some code here that is moderately long" for j in range(15)]
            )
            assertion = f">       assert x_{i} == y_{i}\nE       AssertionError: assert {i} == {i + 1}"
            blocks.append(f"{block_header}\n\n{traceback_lines}\n\n{assertion}\n")

        failures_section = "=" * 30 + " FAILURES " + "=" * 30 + "\n" + "\n".join(blocks)
        full_output = "some preamble\n" + failures_section + "\n" + "=" * 30 + " short test summary " + "=" * 30

        # With a tight budget, output should be truncated
        result = _extract_failures_section(full_output, max_chars=1500)
        assert len(result) <= 1500
        assert "FAILURES" in result

    def test_preserves_assertion_in_truncated_blocks(self):
        # Build one very large block
        block_header = "_" * 30 + " test_big " + "_" * 30
        filler = "\n".join([f"    frame {i}: lots of code" for i in range(100)])
        assertion = ">       assert False\nE       AssertionError: custom message here"
        block = f"{block_header}\n\n{filler}\n\n{assertion}\n"

        failures_section = "=" * 30 + " FAILURES " + "=" * 30 + "\n" + block
        full_output = failures_section + "\n" + "=" * 30 + " summary " + "=" * 30

        result = _extract_failures_section(full_output, max_chars=800)
        # The tail (assertion) should be preserved even when truncated
        assert "AssertionError" in result


class TestExtractFailuresNoSection:
    """No FAILURES or ERRORS section — should fall back to tail."""

    def test_fallback_to_tail(self):
        output = "line1\nline2\nline3\nfinal line with useful info"
        result = _extract_failures_section(output, max_chars=100)
        assert "final line with useful info" in result

    def test_fallback_respects_max_chars(self):
        output = "A" * 5000
        result = _extract_failures_section(output, max_chars=1000)
        assert len(result) <= 1000


class TestExtractFailuresEmpty:
    """Empty or None-like input."""

    def test_empty_string(self):
        assert _extract_failures_section("") == ""

    def test_whitespace_only(self):
        # Whitespace is not empty per the truthy check, but has no FAILURES
        result = _extract_failures_section("   \n  \n  ")
        assert isinstance(result, str)


class TestExtractFailuresErrors:
    """ERRORS section extraction (pytest collection errors)."""

    def test_extracts_errors_section(self):
        result = _extract_failures_section(ERRORS_PYTEST_OUTPUT)
        assert "ERRORS" in result
        assert "ImportError" in result
        assert "collecting tests/bad.py" in result

    def test_does_not_include_post_errors_summary(self):
        result = _extract_failures_section(ERRORS_PYTEST_OUTPUT)
        assert "short test summary info" not in result
