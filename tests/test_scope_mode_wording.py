"""Guards for the SELF_CHECK scope-mode wording clarification (group G5).

`scope_mode` keeps its persisted values `full` / `incremental` — they are
state-compatibility identifiers and renaming them would break resume. What this
group changes is only how those two values are *described*: both modes are
diff-scoped, and the mode names the diff baseline (this flow's implementation
baseline for `full`, the earliest not-yet-reviewed fix baseline for
`incremental`). These tests pin
that clarification everywhere it surfaces — the SELF_CHECK prompt, the
`luo history show` labels, the WebUI labels/tooltip and the bilingual
configuration docs — and pin the persisted values as unchanged.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from tianluo import i18n
from tianluo.commands import history_cmd
from tianluo.engine.steps.self_check import _format_review_scope


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_I18N_DIR = REPO_ROOT / "src" / "tianluo" / "i18n" / "locales"
WEB_STATIC = REPO_ROOT / "src" / "tianluo" / "server" / "static"
WEB_I18N_DIR = WEB_STATIC / "i18n"
APP_JS = WEB_STATIC / "app.js"
DOCS_EN = REPO_ROOT / "docs" / "configuration.md"
DOCS_ZH = REPO_ROOT / "docs" / "configuration.zh.md"

LANGS = ("en-US", "zh-CN")


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1. SELF_CHECK prompt purpose wording
# ---------------------------------------------------------------------------
def _scope_inputs(mode: str) -> dict:
    return {
        "scope_mode": mode,
        "baseline_id": "b-123456789012",
        "scope_changed_paths": ["src/a.py"],
        "scope_causal_anchors": {"src/a.py": [[10, 12]]},
        "scope_diff": "@@ -1 +1 @@\n+x\n",
    }


@pytest.mark.parametrize("mode", ["full", "incremental"])
def test_prompt_states_both_modes_are_diff_scoped(mode):
    rendered = _format_review_scope(_scope_inputs(mode))
    assert "diff-scoped" in rendered
    # The persisted token itself is still what the prompt reports.
    assert f"- scope_mode: {mode} (" in rendered
    # ... annotated so `full` cannot be read as "no diff, read everything".
    assert "names the diff baseline" in rendered
    assert "both modes are diff-scoped" in rendered


def test_full_round_purpose_names_the_implementation_baseline():
    rendered = _format_review_scope(_scope_inputs("full"))
    assert "Full round — diff-scoped" in rendered
    assert "implementation baseline" in rendered
    # The misreading this group exists to kill.
    assert "not an unscoped read of the tree" in rendered


def test_incremental_round_purpose_names_the_fix_baseline():
    rendered = _format_review_scope(_scope_inputs("incremental"))
    assert "Incremental round — diff-scoped" in rendered
    assert "fix baseline" in rendered


def test_incremental_wording_does_not_promise_a_single_latest_fix():
    """Several FIX calls can run with no round between them.

    The round is then scoped from the EARLIEST uncovered fix baseline, so its
    diff spans all of them. Telling the checker the diff starts at the latest
    fix understates what it is looking at and invites it to dismiss an earlier
    unreviewed fix's hunks as out of scope.
    """
    rendered = _format_review_scope(_scope_inputs("incremental"))
    assert "latest fix baseline" not in rendered
    assert "that fix's own delta" not in rendered
    assert "earliest fix" in rendered
    # Both the mode annotation and the purpose sentence must say it.
    mode_line = next(
        line for line in rendered.splitlines()
        if line.startswith("- scope_mode: incremental")
    )
    assert "earliest fix not reviewed yet" in mode_line
    purpose_line = next(
        line for line in rendered.splitlines() if line.startswith("- purpose: ")
    )
    assert "earliest fix" in purpose_line
    assert "more than one" in purpose_line


@pytest.mark.parametrize("lang", LANGS)
def test_scope_mode_hint_does_not_say_latest_fix(lang):
    cli = _load(CLI_I18N_DIR / f"{lang}.json")
    web = _load(WEB_I18N_DIR / f"{lang}.json")
    for text in (cli["history.field.scope_mode_hint"], web["scope.modeHint"]):
        assert "latest fix baseline" not in text
        assert "最近一次 fix 基线" not in text


def test_en_docs_describe_the_incremental_baseline_as_the_earliest_unreviewed():
    text = " ".join(DOCS_EN.read_text(encoding="utf-8").split())
    assert "earliest fix the flow has not reviewed yet" in text
    assert "latest **fix baseline** (that fix's own delta)" not in text


def test_zh_docs_describe_the_incremental_baseline_as_the_earliest_unreviewed():
    text = DOCS_ZH.read_text(encoding="utf-8")
    assert "尚未被审查的最早一次 fix" in text


def test_fallback_note_names_the_baseline_it_falls_back_to():
    """The undecidable-fix-baseline fallback used to say only "a full review",
    which reads as "stop diffing" — it must name the baseline instead."""
    inputs = _scope_inputs("full")
    inputs["scope_fallback_from_incremental"] = True
    rendered = _format_review_scope(inputs)
    assert "diff-scoped from the implementation baseline instead" in rendered


def test_fallback_note_blames_only_the_domain_that_failed():
    """Both halves of the incremental evidence domain route here when they
    break, so the note must not accuse a fix baseline that rebuilt cleanly —
    nor promise an implementation-baseline diff that is itself missing."""
    inputs = _scope_inputs("full")
    inputs["scope_fallback_from_incremental"] = True
    inputs["scope_fallback_cause"] = "fix_baseline"
    rendered = _format_review_scope(inputs)
    assert "the fix baseline was not trustworthy" in rendered

    inputs = _scope_inputs("full")
    inputs["scope_fallback_from_incremental"] = True
    inputs["scope_fallback_cause"] = "task_baseline"
    inputs["scope_undecidable"] = True
    inputs["scope_diagnostic"] = "implementation baseline descriptor is corrupt"
    rendered = _format_review_scope(inputs)
    assert "the fix baseline was not trustworthy" not in rendered
    assert "rebuilt cleanly" in rendered
    assert "whole-task (implementation baseline) half" in rendered
    # It must not claim the baseline it fell back to is available.
    assert "diff-scoped from the implementation baseline instead" not in rendered
    assert "itself unavailable here" in rendered


def test_fallback_note_without_a_recorded_cause_stays_generic():
    """Older persisted state carries no cause; the note then says only what is
    certainly true rather than picking a domain to blame."""
    inputs = _scope_inputs("full")
    inputs["scope_fallback_from_incremental"] = True
    rendered = _format_review_scope(inputs)
    assert "the fix baseline was not trustworthy" not in rendered
    assert "combined incremental evidence domain could not be reconstructed" in rendered


# ---------------------------------------------------------------------------
# 2. CLI ↔ WebUI label parity
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("lang", LANGS)
def test_cli_and_webui_scope_mode_labels_are_identical(lang):
    """`_scope_mode_label`'s WebUI-parity contract: the two surfaces must not
    describe the same persisted value differently."""
    cli = _load(CLI_I18N_DIR / f"{lang}.json")
    web = _load(WEB_I18N_DIR / f"{lang}.json")
    for mode in ("full", "incremental"):
        assert cli[f"history.scope.mode.{mode}"] == web[f"scope.mode.{mode}"]


@pytest.mark.parametrize("lang", LANGS)
def test_scope_mode_labels_say_diff_in_both_modes(lang):
    cli = _load(CLI_I18N_DIR / f"{lang}.json")
    web = _load(WEB_I18N_DIR / f"{lang}.json")
    for bundle, prefix in ((cli, "history.scope.mode."), (web, "scope.mode.")):
        for mode in ("full", "incremental"):
            assert "diff" in bundle[f"{prefix}{mode}"].lower(), (
                f"{lang} {prefix}{mode} must read as a diff mode"
            )


@pytest.mark.parametrize("lang", LANGS)
def test_scope_mode_hint_names_both_baselines(lang):
    cli = _load(CLI_I18N_DIR / f"{lang}.json")
    web = _load(WEB_I18N_DIR / f"{lang}.json")
    for text in (cli["history.field.scope_mode_hint"], web["scope.modeHint"]):
        assert "implementation" in text
        assert "fix" in text
        assert "diff" in text.lower()


def test_en_us_holds_the_new_keys_as_the_baseline_language():
    """en-US is the base catalog: a key present only in zh-CN could never fall
    back, so both new keys must exist in en-US first."""
    assert "history.field.scope_mode_hint" in _load(CLI_I18N_DIR / "en-US.json")
    assert "scope.modeHint" in _load(WEB_I18N_DIR / "en-US.json")


def test_zh_cn_translates_the_new_keys_rather_than_copying_en():
    cli_en = _load(CLI_I18N_DIR / "en-US.json")
    cli_zh = _load(CLI_I18N_DIR / "zh-CN.json")
    web_en = _load(WEB_I18N_DIR / "en-US.json")
    web_zh = _load(WEB_I18N_DIR / "zh-CN.json")
    assert cli_zh["history.field.scope_mode_hint"] != cli_en[
        "history.field.scope_mode_hint"]
    assert web_zh["scope.modeHint"] != web_en["scope.modeHint"]


# ---------------------------------------------------------------------------
# 3. `luo history show` rendering
# ---------------------------------------------------------------------------
def _detail(mode: str = "incremental") -> dict:
    return {
        "flow_id": "f1",
        "task_description": "t",
        "task_type": "feature",
        "status": "running",
        "current_step": "self_check",
        "progress": {"completed": 1, "total": 2},
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "chat_sessions": 1,
        "steps": [],
        "review_scope": {
            "active_round": {
                "round_id": "scr-x",
                "scope_mode": mode,
                "pass_index": 1,
                "fix_iteration": 0,
            },
        },
    }


def _history_show(mode: str, lang: str, monkeypatch, tmp_path) -> str:
    monkeypatch.setenv("SE3_LANG", lang)
    i18n.reset_language()
    try:
        runner = CliRunner()
        with patch.object(history_cmd, "get_project_root", return_value=tmp_path), \
             patch.object(history_cmd, "get_flow_detail", return_value=_detail(mode)):
            return runner.invoke(history_cmd.app, ["show", "f1"]).output
    finally:
        # The autouse fixture pins en-US; restore it for whatever runs next in
        # this worker.
        monkeypatch.setenv("SE3_LANG", "en-US")
        i18n.reset_language()


@pytest.mark.parametrize("mode,label", [
    ("full", "full diff"),
    ("incremental", "incremental diff"),
])
def test_history_show_renders_diff_scoped_labels_en(
    mode, label, monkeypatch, tmp_path,
):
    out = _history_show(mode, "en-US", monkeypatch, tmp_path)
    assert label in out


@pytest.mark.parametrize("mode,label", [("full", "全量 diff"),
                                        ("incremental", "增量 diff")])
def test_history_show_renders_diff_scoped_labels_zh(
    mode, label, monkeypatch, tmp_path,
):
    out = _history_show(mode, "zh-CN", monkeypatch, tmp_path)
    assert label in out


def test_history_show_carries_the_baseline_hint(monkeypatch, tmp_path):
    out = _history_show("full", "en-US", monkeypatch, tmp_path)
    # Rich wraps the hint across the table's value column, so assert on the
    # distinctive words rather than the whole sentence.
    assert "implementation baseline" in " ".join(out.split())
    assert "fix baseline" in " ".join(out.split())


def test_scope_mode_label_still_passes_unknown_values_through():
    """INVARIANT-adjacent: the label is presentation only. An unrecognised or
    future persisted value must render raw, never be swallowed."""
    assert history_cmd._scope_mode_label("something-else") == "something-else"
    assert history_cmd._scope_mode_label(None) == "-"


# ---------------------------------------------------------------------------
# 4. WebUI wiring
# ---------------------------------------------------------------------------
def test_app_js_attaches_the_scope_mode_hint_to_both_renderers():
    src = APP_JS.read_text(encoding="utf-8", errors="replace")
    assert src.count('tf("scope.modeHint"') == 2, (
        "both the flow-sidebar and the history-detail scope renderers must "
        "carry the baseline tooltip"
    )
    assert "SCOPE_MODE_HINT_EN" in src


def _app_js_scope_hint_fallback() -> str:
    """The literal `SCOPE_MODE_HINT_EN` is built from, concatenation resolved.

    Scanned literal-by-literal rather than cut at the first `;`: the sentence
    itself contains a semicolon, so a naive statement cut lands mid-string.
    """
    src = APP_JS.read_text(encoding="utf-8", errors="replace")
    pos = src.index("const SCOPE_MODE_HINT_EN =") + len("const SCOPE_MODE_HINT_EN =")
    literal = re.compile(r'\s*\+?\s*"((?:[^"\\]|\\.)*)"')
    parts = []
    while True:
        match = literal.match(src, pos)
        if not match:
            break
        parts.append(json.loads('"%s"' % match.group(1)))
        pos = match.end()
    return "".join(parts)


def test_app_js_fallback_hint_matches_the_localized_string():
    """The no-dictionary path must not serve a stale sentence.

    `tf()` falls back to this literal when the WebUI catalogs fail to load, so
    a fallback drifting from en-US would understate the incremental delta at
    exactly the moment nothing else can correct it.
    """
    assert _app_js_scope_hint_fallback() == _load(
        WEB_I18N_DIR / "en-US.json"
    )["scope.modeHint"]


def test_app_js_fallback_hint_does_not_say_latest_fix():
    fallback = _app_js_scope_hint_fallback()
    assert "latest fix baseline" not in fallback
    assert "earliest fix not reviewed yet" in fallback


def test_app_js_still_resolves_labels_by_persisted_scope_mode_value():
    """The label lookup is keyed by the raw persisted value; the wording change
    must not have introduced a renamed key."""
    src = APP_JS.read_text(encoding="utf-8", errors="replace")
    assert src.count('I18N.resolve("scope.mode." + ') == 2


# ---------------------------------------------------------------------------
# 5. Documentation
# ---------------------------------------------------------------------------
def test_en_docs_clarify_both_modes_are_diff_scoped():
    text = " ".join(DOCS_EN.read_text(encoding="utf-8").split())
    assert "Both round modes are diff-scoped" in text
    assert "name the *diff baseline*" in text
    assert "not an unscoped read of the whole tree" in text


def test_zh_docs_clarify_both_modes_are_diff_scoped():
    text = DOCS_ZH.read_text(encoding="utf-8")
    assert "两种 round 模式都是 diff 审查" in text
    assert "implementation baseline" in text
    assert "fix baseline" in text


@pytest.mark.parametrize("path", [DOCS_EN, DOCS_ZH])
def test_docs_keep_the_persisted_scope_mode_values(path):
    text = path.read_text(encoding="utf-8")
    assert "`scope_mode`" in text
    assert "`full`" in text and "`incremental`" in text
