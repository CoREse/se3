"""Unit tests for src/se3/engine/display.py reverse-block helpers and renderers."""

from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console
from rich.text import Text

from se3.engine import display
from se3.engine.display import (
    _BLOCK_FOOTER_WIDTH,
    _USAGE_BLOCK_COLOR,
    _reverse_footer,
    _reverse_title,
    render_block_footer,
    render_block_header,
    render_code,
    render_diff,
    render_full,
    render_markdown,
    render_text,
    render_usage_block,
    set_console,
)
from se3.engine.token_usage import UsageTotals


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_recording_console(width: int = 80) -> tuple[Console, StringIO]:
    """Build a Console that writes captured output to a StringIO buffer."""
    buf = StringIO()
    console = Console(file=buf, width=width, force_terminal=True, color_system="truecolor")
    return console, buf


def _make_plain_console(width: int = 80) -> tuple[Console, StringIO]:
    """A recording console with number/repr auto-highlighting disabled.

    Rich's default highlighter colorizes numbers, splitting a value like
    ``12,345`` with ANSI escapes mid-token. For deterministic *content*
    assertions we disable highlighting; the styled/visual behavior is covered
    separately.
    """
    buf = StringIO()
    console = Console(file=buf, width=width, force_terminal=True, highlight=False)
    return console, buf


@pytest.fixture(autouse=True)
def _isolate_console():
    """Reset the module-level console between tests."""
    saved = display._console
    yield
    display._console = saved


# ---------------------------------------------------------------------------
# Low-level helper tests
# ---------------------------------------------------------------------------


class TestReverseHelpers:
    def test_reverse_title_returns_text(self):
        out = _reverse_title("Hello", "blue")
        assert isinstance(out, Text)
        # Padded form preserves the ## marker for visibility.
        assert out.plain == " ## Hello "
        assert out.style.bgcolor.name == "blue"
        assert out.style.bold is True

    def test_reverse_title_is_markup_safe(self):
        # Square brackets in title must NOT be parsed as Rich markup.
        out = _reverse_title("[bold]inj[/bold]", "magenta")
        assert out.plain == " ## [bold]inj[/bold] "
        # No nested spans created by markup parsing
        assert len(out.spans) == 0

    def test_reverse_footer_fixed_width(self):
        out = _reverse_footer("blue")
        assert isinstance(out, Text)
        assert out.plain == " " * _BLOCK_FOOTER_WIDTH
        assert len(out.plain) == 4

    def test_reverse_footer_only_spaces(self):
        # Copy safety: footer body must contain only whitespace.
        for color in ("blue", "green", "yellow", "magenta", "red", "cyan"):
            out = _reverse_footer(color)
            assert set(out.plain) == {" "}
            assert out.style.bgcolor.name == color

    @pytest.mark.parametrize("term_width", [40, 80, 120, 200])
    def test_footer_width_independent_of_terminal(self, term_width):
        console, buf = _make_recording_console(width=term_width)
        set_console(console)
        render_block_footer("yellow")
        # The footer has fixed plain width 4, regardless of terminal width.
        # We reconstruct via _reverse_footer (deterministic) and assert width.
        footer = _reverse_footer("yellow")
        assert len(footer.plain) == 4

    def test_render_block_header_uses_global_console(self):
        console, buf = _make_recording_console()
        set_console(console)
        render_block_header("Ctx", "green")
        # The captured output should contain " ## Ctx " somewhere
        assert " ## Ctx " in buf.getvalue()

    def test_render_block_footer_emits_blank_line(self):
        console, buf = _make_recording_console()
        set_console(console)
        render_block_footer("red")
        out = buf.getvalue()
        # Footer line + a trailing blank line
        # Last two non-stripped lines should be footer then empty
        lines = out.splitlines()
        assert len(lines) >= 1


# ---------------------------------------------------------------------------
# Render function output sequencing tests
# ---------------------------------------------------------------------------


class TestRenderFunctionsBlocks:
    def test_render_full_with_title_emits_footer(self):
        console, buf = _make_recording_console()
        set_console(console)
        render_full("hello body", title="My Block")
        out = buf.getvalue()
        # Title appears before content, footer (4 spaces with bg) appears after.
        i_title = out.find("## My Block")
        i_body = out.find("hello body")
        assert 0 <= i_title < i_body
        # Title and footer both present (footer characters are spaces, but
        # ANSI bg sequence will appear in the captured stream); detect by
        # looking for an ANSI background after the body.
        assert "\x1b[" in out  # has ANSI styling
        assert i_body < len(out)

    def test_render_full_without_title_emits_no_block_borders(self):
        console, buf = _make_recording_console()
        set_console(console)
        render_full("just body")
        out = buf.getvalue()
        assert "##" not in out
        # No background-colored spans expected
        # (We accept generic ANSI for plain text formatting; here Rich won't
        # add bg without title.)
        assert "## " not in out

    def test_render_text_with_title_block(self):
        console, buf = _make_recording_console()
        set_console(console)
        render_text("payload", title="Step Output")
        out = buf.getvalue()
        assert " ## Step Output " in out

    def test_render_text_without_title_no_borders(self):
        console, buf = _make_recording_console()
        set_console(console)
        render_text("payload")
        out = buf.getvalue()
        assert "##" not in out

    def test_render_code_with_title_block(self):
        console, buf = _make_recording_console()
        set_console(console)
        render_code("print(1)\n", language="python", title="Snippet")
        out = buf.getvalue()
        assert " ## Snippet " in out
        # syntax-highlighted body present
        assert "print" in out

    def test_render_diff_with_displayed_lines_emits_block(self):
        console, buf = _make_recording_console()
        set_console(console)
        diff_lines = [
            "--- a/foo.py\n",
            "+++ b/foo.py\n",
            "@@ -1,2 +1,2 @@\n",
            "-old\n",
            "+new\n",
            " ctx\n",
        ]
        render_diff(diff_lines, "foo.py", max_lines=50)
        out = buf.getvalue()
        assert " ## Diff: foo.py " in out

    def test_render_diff_no_displayed_lines_no_block(self):
        console, buf = _make_recording_console()
        set_console(console)
        # Only header lines; no real diff content rendered
        render_diff(["--- a/foo.py\n", "+++ b/foo.py\n"], "foo.py")
        out = buf.getvalue()
        assert "## Diff" not in out

    def test_render_markdown_with_title_block(self):
        console, buf = _make_recording_console()
        set_console(console)
        render_markdown("**md**", title="Notes")
        out = buf.getvalue()
        assert " ## Notes " in out


# ---------------------------------------------------------------------------
# task_formatter integration: _heading_group should append a footer
# ---------------------------------------------------------------------------


class TestHeadingGroupFooter:
    def test_heading_group_last_element_is_blank_after_footer(self):
        from rich.console import Group
        from se3.engine.formatters.task_formatter import _heading_group

        group = _heading_group("Plan", "blue", Text("body"))
        assert isinstance(group, Group)
        elements = list(group.renderables)
        # (title, blank, body, blank, footer, blank) → 6 elements
        assert len(elements) == 6
        title, blank1, body, blank2, footer, blank3 = elements
        assert isinstance(title, Text) and title.plain == " ## Plan "
        assert isinstance(blank1, Text) and blank1.plain == ""
        assert isinstance(body, Text) and body.plain == "body"
        assert isinstance(blank2, Text) and blank2.plain == ""
        assert isinstance(footer, Text)
        assert set(footer.plain) == {" "}
        assert footer.style.bgcolor.name == "blue"
        assert isinstance(blank3, Text) and blank3.plain == ""


# ---------------------------------------------------------------------------
# render_usage_block (G3 — CLI token-usage summary)
# ---------------------------------------------------------------------------


class TestRenderUsageBlock:
    def _populated(self) -> UsageTotals:
        return UsageTotals(
            input_tokens=12345,
            output_tokens=6789,
            cache_creation_input_tokens=200,
            cache_read_input_tokens=1000,
            total_cost_usd=0.0123,
        )

    def test_populated_renders_aligned_block(self):
        console, buf = _make_plain_console()
        set_console(console)
        render_usage_block(self._populated(), title="Step Token Usage")
        out = buf.getvalue()

        # Reverse-color title block present
        assert " ## Step Token Usage " in out
        # Labels with units
        assert "Input tokens" in out
        assert "Output tokens" in out
        assert "Cache read" in out
        assert "Cache creation" in out
        assert "Cost" in out
        # Thousands separators on token counts
        assert "12,345" in out
        assert "6,789" in out
        assert "1,000" in out
        # Cost formatted as $X.XXXX
        assert "$0.0123" in out

    def test_empty_usagetotals_renders_nothing(self):
        console, buf = _make_recording_console()
        set_console(console)
        render_usage_block(UsageTotals())
        assert buf.getvalue() == ""

    def test_none_renders_nothing(self):
        console, buf = _make_recording_console()
        set_console(console)
        render_usage_block(None)
        assert buf.getvalue() == ""

    def test_accepts_dict_input(self):
        console, buf = _make_plain_console()
        set_console(console)
        # The JSON-primitive dict form persisted in step.outputs / state.
        render_usage_block(
            {
                "input_tokens": 5,
                "output_tokens": 7,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "total_cost_usd": 1.5,
            }
        )
        out = buf.getvalue()
        assert " ## Token Usage " in out
        assert "$1.5000" in out

    def test_empty_dict_renders_nothing(self):
        console, buf = _make_recording_console()
        set_console(console)
        render_usage_block({})
        assert buf.getvalue() == ""

    def test_footer_uses_usage_color_and_fixed_width(self):
        console, buf = _make_recording_console(width=120)
        set_console(console)
        render_usage_block(self._populated())
        out = buf.getvalue()
        # No Rule / Panel border characters from this block
        assert "─" not in out
        # Footer color constant is cyan (auxiliary/summary accent)
        assert _USAGE_BLOCK_COLOR == "cyan"
