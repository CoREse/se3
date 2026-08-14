"""i18n routing for the UI-framework text in src/tianluo/engine/display.py.

The section labels / usage-column headers / default block titles rendered by
display.py are UI-framework text and must go through the ``tianluo.i18n`` catalog;
the LLM content threaded into those blocks (summary bodies, decision text) is
data and must pass through verbatim. These tests assert both: the framework
labels flip with the language, and the content stays untranslated.
"""

from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console

from tianluo import i18n
from tianluo.engine import display
from tianluo.engine.display import render_proposal, render_usage_block, set_console
from tianluo.engine.token_usage import UsageTotals


@pytest.fixture(autouse=True)
def _isolate_console():
    saved = display._console
    yield
    display._console = saved


def _plain_console() -> tuple[Console, StringIO]:
    buf = StringIO()
    return Console(file=buf, width=100, force_terminal=True, highlight=False), buf


def _usage() -> UsageTotals:
    return UsageTotals(
        input_tokens=12345,
        output_tokens=6789,
        cache_read_input_tokens=1000,
        cache_creation_input_tokens=0,
        total_cost_usd=0.0123,
    )


class TestUsageBlockLabels:
    def test_default_title_and_labels_en_us(self):
        i18n.set_language("en-US")
        console, buf = _plain_console()
        set_console(console)
        render_usage_block(self._usage_or_skip())
        out = buf.getvalue()
        assert " ## Token Usage " in out
        assert "Input tokens" in out
        assert "Cache read" in out
        assert "Cost" in out

    def test_default_title_and_labels_zh_cn(self):
        i18n.set_language("zh-CN")
        console, buf = _plain_console()
        set_console(console)
        render_usage_block(self._usage_or_skip())
        out = buf.getvalue()
        assert " ## Token 用量 " in out
        assert "输入 tokens" in out
        assert "缓存读取" in out
        assert "费用" in out
        # No English label bled through.
        assert "Input tokens" not in out

    def test_explicit_title_preserved(self):
        # An explicit title passed by a caller is used verbatim, not overridden.
        i18n.set_language("zh-CN")
        console, buf = _plain_console()
        set_console(console)
        render_usage_block(self._usage_or_skip(), title="Session Token Usage")
        assert " ## Session Token Usage " in buf.getvalue()

    def _usage_or_skip(self) -> UsageTotals:
        return _usage()


class TestHistoryUsageModelPlaceholder:
    """The per-call Model cell localizes the unresolved-model placeholder."""

    @staticmethod
    def _wide_console() -> tuple[Console, StringIO]:
        # The usage table has 12 columns; a narrow console ellipsizes cells
        # and would hide the very placeholder this test asserts.
        buf = StringIO()
        return Console(file=buf, width=300, force_terminal=True, highlight=False), buf

    def test_unresolved_model_placeholder_localized(self):
        from tianluo.engine.display import build_history_usage_renderables

        payload = {
            "calls": [
                {
                    "schema_version": 2,
                    "call_id": "c1",
                    "attempt": 0,
                    "usage_status": "unavailable",
                    "resolved_model": "unknown",
                }
            ],
        }
        for lang, placeholder in (("en-US", "unknown"), ("zh-CN", "未知")):
            i18n.set_language(lang)
            console, buf = self._wide_console()
            set_console(console)
            for renderable in build_history_usage_renderables(payload):
                console.print(renderable)
            out = buf.getvalue()
            assert placeholder in out

    def test_resolved_model_shown_verbatim(self):
        from tianluo.engine.display import build_history_usage_renderables

        payload = {
            "calls": [
                {
                    "schema_version": 2,
                    "call_id": "c1",
                    "attempt": 0,
                    "usage_status": "available",
                    "resolved_model": "claude-opus-5",
                }
            ],
        }
        i18n.set_language("zh-CN")
        console, buf = self._wide_console()
        set_console(console)
        for renderable in build_history_usage_renderables(payload):
            console.print(renderable)
        assert "claude-opus-5" in buf.getvalue()

    def test_flow_totals_box_carries_flow_level_title(self):
        from tianluo.engine.display import build_history_usage_renderables

        payload = {
            "calls": [
                {
                    "schema_version": 2,
                    "call_id": "c1",
                    "attempt": 0,
                    "usage_status": "available",
                    "resolved_model": "claude-opus-5",
                }
            ],
            "summary": {
                "actual_cost_usd": None,
                "estimated_cost_usd": None,
                "unknown_call_count": 0,
                "unknown_model_count": 0,
                "unknown_price_count": 0,
                "unknown_cache_ttl_count": 0,
                "partial": False,
                "diagnostics": [],
                "totals": {
                    "schema_version": 2,
                    "call_id": "summary",
                    "attempt": 0,
                    "usage_status": "unavailable",
                    "resolved_model": "unknown",
                },
            },
        }
        for lang, flow_title, session_title in (
            ("en-US", "Flow Usage Totals", "Session Token Usage"),
            ("zh-CN", "本次流程用量合计", "本次会话 Token 用量"),
        ):
            i18n.set_language(lang)
            console, buf = self._wide_console()
            set_console(console)
            for renderable in build_history_usage_renderables(payload):
                console.print(renderable)
            out = buf.getvalue()
            assert flow_title in out
            # The per-session title must not leak into the flow-totals box.
            assert session_title not in out


class TestProposalLabelsVsContent:
    def test_labels_localized_content_verbatim(self):
        i18n.set_language("zh-CN")
        console, buf = _plain_console()
        set_console(console)
        # The summary body is LLM content and must survive untranslated.
        render_proposal({"summary": "Add a widget", "rationale": "because"})
        out = buf.getvalue()
        assert " ## 方案 " in out
        assert "摘要：" in out
        assert "理由：" in out
        # Content passed through verbatim (not translated / not dropped).
        assert "Add a widget" in out
        assert "because" in out
        # English framework labels are gone.
        assert "Summary:" not in out
        assert "Rationale:" not in out


class TestUsageStatusColumn:
    """The per-call Status column must render the localized ``usage.status.*``
    label, not the Python enum repr.

    ``UsageStatus`` is a ``(str, Enum)`` mixin, so ``str(member)`` is
    ``'UsageStatus.AVAILABLE'`` — feeding that back into the constructor used
    to raise and fall through to printing the repr verbatim.
    """

    def test_enum_member_renders_localized_label(self):
        from tianluo.engine.display import _usage_status_label
        from tianluo.usage import UsageStatus

        i18n.set_language("en-US")
        assert _usage_status_label(UsageStatus.AVAILABLE) == "available"
        i18n.set_language("zh-CN")
        assert _usage_status_label(UsageStatus.AVAILABLE) == "可用"
        assert _usage_status_label(UsageStatus.LEGACY_AMBIGUOUS) == "旧记录"

    def test_wire_string_and_unknown_value_still_supported(self):
        from tianluo.engine.display import _usage_status_label

        i18n.set_language("en-US")
        assert _usage_status_label("partial") == "partial"
        assert _usage_status_label("nonsense") == "nonsense"

    def test_calls_table_has_no_enum_repr(self):
        from tianluo.engine.display import build_history_usage_renderables

        payload = {
            "calls": [
                {
                    "schema_version": 2,
                    "call_id": "c1",
                    "attempt": 0,
                    "usage_status": "available",
                    "resolved_model": "claude-opus-5",
                }
            ],
        }
        i18n.set_language("en-US")
        buf = StringIO()
        console = Console(file=buf, width=240, force_terminal=False, highlight=False)
        set_console(console)
        for renderable in build_history_usage_renderables(payload):
            console.print(renderable)
        out = buf.getvalue()
        assert "UsageStatus." not in out
        assert "available" in out
