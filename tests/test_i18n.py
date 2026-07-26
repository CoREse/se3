"""Tests for the tianluo.i18n resource layer and language-resolution chain.

Covers: resource discovery/auto-registration, per-key fallback to en-US,
unknown-language fallback, placeholder fault tolerance, language-code
normalization, the five-level resolution precedence chain, and the lazy
singleton reset seam. Also asserts the wheel ships the locale JSON as package
data.

All tests reset the i18n singleton + catalog caches and isolate ``Path.home``
so a developer's real ~/.se3/config.yaml and shell locale cannot leak in.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import tianluo.i18n as i18n
from tianluo.i18n import loader


@pytest.fixture(autouse=True)
def _reset_i18n(monkeypatch, tmp_path_factory):
    """Reset i18n state and isolate config/locale env before each test.

    The active-language selection and the loaded-catalog cache are process-wide
    singletons; without a reset one test's SE3_LANG or config would bleed into
    the next. Path.home is repointed at an empty dir so LanguageConfig's global
    merge sees no real ~/.se3/config.yaml, and the locale env vars are cleared
    so the host shell's LANG cannot flip resolution assertions.
    """
    i18n.reset_language()
    loader.clear_caches()
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr("tianluo.config.Path.home", lambda: home)
    for var in ("SE3_LANG", "LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(var, raising=False)
    yield
    i18n.reset_language()
    loader.clear_caches()


# --- Catalog loading & discovery ---


class TestCatalogLoading:
    def test_shipped_languages_discovered(self):
        langs = loader.supported_languages()
        assert "en-US" in langs
        assert "zh-CN" in langs

    def test_en_us_holds_seed_keys(self):
        catalog = loader.load_catalog("en-US")
        assert catalog.get("cli.common.done") == "Done"
        assert "cli.run.starting" in catalog

    def test_unknown_code_loads_empty_catalog(self):
        assert loader.load_catalog("xx-YY") == {}

    def test_new_language_file_autoregistered(self, tmp_path, monkeypatch):
        """Dropping a <code>.json into locales/ auto-registers it — no code."""
        fake_locale = tmp_path / "locales"
        fake_locale.mkdir()
        (fake_locale / "en-US.json").write_text(json.dumps({"k": "base"}))
        (fake_locale / "fr-FR.json").write_text(json.dumps({"k": "bonjour"}))

        def _fake_iter():
            for p in sorted(fake_locale.glob("*.json")):
                yield p.stem, p

        monkeypatch.setattr(loader, "_iter_locale_resources", _fake_iter)
        loader.clear_caches()

        assert "fr-FR" in loader.supported_languages()
        assert loader.load_catalog("fr-FR")["k"] == "bonjour"


# --- t() fallback behavior ---


class TestTranslate:
    def test_returns_selected_language_string(self):
        i18n.set_language("zh-CN")
        assert i18n.t("cli.common.done") == "完成"

    def test_missing_key_falls_back_to_en_us(self):
        # cli.run.resume_hint is intentionally absent from zh-CN.
        i18n.set_language("zh-CN")
        assert "cli.run.resume_hint" not in loader.load_catalog("zh-CN")
        assert i18n.t("cli.run.resume_hint") == loader.load_catalog("en-US")[
            "cli.run.resume_hint"
        ]

    def test_missing_everywhere_returns_key(self):
        i18n.set_language("zh-CN")
        assert i18n.t("no.such.key.anywhere") == "no.such.key.anywhere"

    def test_unknown_language_uses_en_us(self):
        i18n.set_language("xx-YY")  # normalizes to None -> BASE_LANGUAGE
        assert i18n.get_language() == "en-US"
        assert i18n.t("cli.common.done") == "Done"

    def test_placeholder_rendered(self):
        i18n.set_language("en-US")
        assert i18n.t("cli.greeting", name="CRE") == "Hello, CRE!"

    def test_missing_placeholder_returns_unformatted_template(self):
        """A missing kwarg must not raise — return the raw template."""
        i18n.set_language("en-US")
        # No 'name' kwarg provided; format would KeyError -> template returned.
        assert i18n.t("cli.greeting") == "Hello, {name}!"

    def test_extra_placeholder_kwargs_ignored(self):
        i18n.set_language("en-US")
        assert i18n.t("cli.common.done", unused="x") == "Done"

    def test_never_raises_on_any_input(self):
        i18n.set_language("zh-CN")
        for key in ("", "cli.common.done", "totally.unknown", "cli.greeting"):
            # Should not raise regardless of missing kwargs / unknown keys.
            i18n.t(key)

    @pytest.mark.parametrize(
        "template", ["{a.b} broken", "{a[0]} broken", "{a!z} broken", "{0} broken"]
    )
    def test_bad_field_access_in_template_returns_template(
        self, monkeypatch, template
    ):
        """Catalog data is translator-editable: a template using attribute or
        subscript field access makes str.format raise AttributeError/TypeError
        (not the KeyError family), and t() must still degrade to the raw
        template rather than crash the flow engine mid-step."""
        i18n.set_language("en-US")
        monkeypatch.setattr(
            "tianluo.i18n.load_catalog", lambda code: {"broken.key": template}
        )
        assert i18n.t("broken.key", a=1) == template


# --- Language-code normalization ---


class TestNormalization:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("zh-CN", "zh-CN"),
            ("zh_CN.UTF-8", "zh-CN"),
            ("ZH-cn", "zh-CN"),
            ("zh", "zh-CN"),
            ("zh_CN", "zh-CN"),
            ("en-US", "en-US"),
            ("en", "en-US"),
            ("en_US.UTF-8", "en-US"),
        ],
    )
    def test_normalizes_to_supported(self, raw, expected):
        assert loader.normalize_language(raw) == expected

    @pytest.mark.parametrize("raw", ["", None, "xx", "de-DE", "C", "POSIX", "  "])
    def test_unknown_returns_none(self, raw):
        assert loader.normalize_language(raw) is None

    @pytest.mark.parametrize("raw", [False, True, 100, 1.5, ["zh-CN"], {"a": 1}])
    def test_non_string_returns_none_without_raising(self, raw):
        """YAML types an unquoted ``language: NO`` as False and a bare numeric
        code as an int; those reach normalize_language straight from config and
        must not explode on .strip()."""
        assert loader.normalize_language(raw) is None

    def test_set_language_with_non_string_selects_base(self):
        assert i18n.set_language(123) == "en-US"
        assert i18n.get_language() == "en-US"


# --- Resolution precedence chain ---


class TestResolveLanguageChain:
    def _write_project(self, root: Path, language):
        val = language if language else "null"
        (root / "tianluo.yaml").write_text(
            f"language:\n  language: {val}\n  spec_language: null\n"
        )

    def _write_global(self, home: Path, language):
        cfg = home / ".se3"
        cfg.mkdir(parents=True, exist_ok=True)
        val = language if language else "null"
        (cfg / "config.yaml").write_text(f"language:\n  language: {val}\n")

    def test_env_wins_over_everything(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SE3_LANG", "zh-CN")
        self._write_project(tmp_path, "en-US")
        monkeypatch.setenv("LANG", "en_US.UTF-8")
        assert i18n.resolve_language(tmp_path) == "zh-CN"

    def test_project_over_global_and_locale(self, tmp_path, monkeypatch):
        home = Path.home()  # patched to isolated dir by the autouse fixture
        self._write_global(home, "en-US")
        self._write_project(tmp_path, "zh-CN")
        monkeypatch.setenv("LANG", "en_US.UTF-8")
        assert i18n.resolve_language(tmp_path) == "zh-CN"

    def test_global_over_locale_when_no_project(self, tmp_path, monkeypatch):
        home = Path.home()
        self._write_global(home, "zh-CN")
        monkeypatch.setenv("LANG", "en_US.UTF-8")
        assert i18n.resolve_language(tmp_path) == "zh-CN"

    def test_locale_used_when_no_config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LANG", "zh_CN.UTF-8")
        assert i18n.resolve_language(tmp_path) == "zh-CN"

    def test_lc_all_beats_lang(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LC_ALL", "zh_CN.UTF-8")
        monkeypatch.setenv("LANG", "en_US.UTF-8")
        assert i18n.resolve_language(tmp_path) == "zh-CN"

    def test_falls_back_to_en_us(self, tmp_path):
        # No env, no config, no locale -> base language.
        assert i18n.resolve_language(tmp_path) == "en-US"

    def test_higher_level_missing_passes_through(self, tmp_path, monkeypatch):
        # SE3_LANG unset, project language null, global unset -> locale wins.
        self._write_project(tmp_path, None)
        monkeypatch.setenv("LANG", "zh_CN.UTF-8")
        assert i18n.resolve_language(tmp_path) == "zh-CN"

    def test_unsupported_env_falls_back_to_base_not_lower_tier(
        self, tmp_path, monkeypatch
    ):
        # A *set* SE3_LANG is an explicit request: an unsupported value resolves
        # to en-US rather than leaking through to a supported project language.
        monkeypatch.setenv("SE3_LANG", "fr-FR")
        self._write_project(tmp_path, "zh-CN")
        assert i18n.resolve_language(tmp_path) == "en-US"

    def test_unsupported_project_falls_back_to_base_not_locale(
        self, tmp_path, monkeypatch
    ):
        # Same rule for the explicit config tier: a set-but-unsupported project
        # language does not fall through to a supported system locale.
        self._write_project(tmp_path, "fr-FR")
        monkeypatch.setenv("LANG", "zh_CN.UTF-8")
        assert i18n.resolve_language(tmp_path) == "en-US"

    def test_non_string_project_language_falls_back_to_base_not_locale(
        self, tmp_path, monkeypatch
    ):
        # `language: NO` (intended Norwegian) is YAML-typed as the boolean False.
        # It is still an *explicit* config value, so it resolves to en-US instead
        # of silently letting the system locale pick the UI language.
        (tmp_path / "tianluo.yaml").write_text("language:\n  language: NO\n")
        monkeypatch.setenv("LANG", "zh_CN.UTF-8")
        assert i18n.resolve_language(tmp_path) == "en-US"

    def test_unsupported_locale_passes_through_to_base(self, tmp_path, monkeypatch):
        # The system locale is an OS hint, not an explicit se3 request, so an
        # unsupported locale falls through to the base language.
        monkeypatch.setenv("LANG", "fr_FR.UTF-8")
        assert i18n.resolve_language(tmp_path) == "en-US"


# --- Lazy singleton behavior ---


class TestLazySingleton:
    def test_import_does_not_read_config(self, tmp_path, monkeypatch):
        """import tianluo.i18n is side-effect free: nothing resolved until first t()."""
        i18n.reset_language()
        # Access the private slot to confirm it's unresolved after reset.
        assert i18n._current_language is None

    def test_get_language_resolves_from_cwd(self, tmp_path, monkeypatch):
        (tmp_path / "tianluo.yaml").write_text("language:\n  language: zh-CN\n")
        monkeypatch.chdir(tmp_path)
        i18n.reset_language()
        assert i18n.get_language() == "zh-CN"

    def test_set_language_overrides_resolution(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SE3_LANG", "en-US")
        i18n.set_language("zh-CN")
        assert i18n.get_language() == "zh-CN"

    def test_reset_forces_reresolution(self, tmp_path, monkeypatch):
        i18n.set_language("zh-CN")
        assert i18n.get_language() == "zh-CN"
        i18n.reset_language()
        monkeypatch.setenv("SE3_LANG", "en-US")
        assert i18n.get_language() == "en-US"


# --- bind_project_root: re-resolution once a command knows its project ---


class TestBindProjectRoot:
    def test_rebinds_language_frozen_at_import_time(self, tmp_path, monkeypatch):
        """A cwd-resolved (import-time) language must not stick to another project.

        Reproduces the Typer help-string freeze: t() renders while commands are
        being defined, caching the cwd's language; a command then targets a
        different project via --project-root and must render in *that* project's
        language.
        """
        monkeypatch.delenv("SE3_LANG", raising=False)
        monkeypatch.setenv("LANG", "en_US.UTF-8")

        cwd = tmp_path / "elsewhere"
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        i18n.reset_language()
        assert i18n.get_language() == "en-US"  # freeze happens here

        project = tmp_path / "zh-project"
        project.mkdir()
        (project / "tianluo.yaml").write_text("language:\n  language: zh-CN\n")

        assert i18n.bind_project_root(project) == "zh-CN"
        assert i18n.get_language() == "zh-CN"

    def test_env_still_outranks_bound_project(self, tmp_path, monkeypatch):
        # bind re-runs the full chain, so SE3_LANG keeps its top precedence.
        monkeypatch.setenv("SE3_LANG", "en-US")
        (tmp_path / "tianluo.yaml").write_text("language:\n  language: zh-CN\n")
        assert i18n.bind_project_root(tmp_path) == "en-US"

    def test_none_root_binds_cwd(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SE3_LANG", raising=False)
        (tmp_path / "tianluo.yaml").write_text("language:\n  language: zh-CN\n")
        monkeypatch.chdir(tmp_path)
        assert i18n.bind_project_root(None) == "zh-CN"


def test_cli_get_project_root_binds_language(tmp_path, monkeypatch):
    """The CLI's project-root discovery is the seam that rebinds the language.

    Commands invoked from a subdirectory of a project must render in the
    *project's* language, not the cwd's.
    """
    from tianluo.commands.code_index_cmd import get_project_root

    monkeypatch.delenv("SE3_LANG", raising=False)
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    (tmp_path / "tianluo.yaml").write_text("language:\n  language: zh-CN\n")
    sub = tmp_path / "src" / "deep"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    i18n.reset_language()

    assert get_project_root() == tmp_path
    assert i18n.get_language() == "zh-CN"


# --- Packaging: wheel ships locale JSON as package data ---


def test_wheel_includes_locales(tmp_path, monkeypatch):
    """Build a wheel and assert the locale catalogs are bundled inside it.

    Guards the packaging contract: the CLI cannot render UI text from an
    installed wheel if the JSON catalogs are dropped during build. Uses the
    PEP 517 build backend directly (hatchling is a build requirement of this
    project, so it is always importable) instead of the optional ``build``
    front-end, so this actually runs rather than perpetually skipping.
    """
    import zipfile

    try:
        from hatchling.build import build_wheel
    except ImportError:  # pragma: no cover - backend must be present to build
        pytest.skip("hatchling build backend unavailable in this env")

    repo_root = Path(__file__).resolve().parent.parent
    out_dir = tmp_path / "wheel"
    out_dir.mkdir()

    # The backend reads pyproject.toml / sources relative to cwd.
    monkeypatch.chdir(repo_root)
    wheel_name = build_wheel(str(out_dir))

    with zipfile.ZipFile(out_dir / wheel_name) as zf:
        names = zf.namelist()
    assert any(n.endswith("tianluo/i18n/locales/en-US.json") for n in names), names
    assert any(n.endswith("tianluo/i18n/locales/zh-CN.json") for n in names), names


# ---------------------------------------------------------------------------
# Engine-layer console chrome (run progress / fix-loop / usage footers)
# ---------------------------------------------------------------------------


def test_engine_run_chrome_keys_exist_in_both_catalogs():
    """Every ``engine.*`` key the run path renders must exist in both catalogs.

    The ``se3 run`` console chrome (test-step progress, fix-loop / adjudication
    banners, token-usage footers) is rendered on the most common run paths, so a
    key present in only one catalog would leak the other language's wording into
    an otherwise localized run.
    """
    from tianluo.i18n.loader import load_catalog

    en = load_catalog("en-US")
    zh = load_catalog("zh-CN")
    engine_keys = {k for k in en if k.startswith("engine.")}
    assert engine_keys, "engine.* chrome keys must be present in the en-US baseline"
    assert engine_keys <= set(zh), sorted(engine_keys - set(zh))


@pytest.mark.parametrize(
    "lang,expected",
    [
        ("en-US", "This round 1,234 in / 567 out · Total 12,345 in / 6,789 out"),
        ("zh-CN", "本轮 1,234 in / 567 out · 累计 12,345 in / 6,789 out"),
    ],
)
def test_round_usage_footer_follows_active_language(lang, expected):
    """The per-round usage footer's label chrome routes through i18n."""
    import tianluo.i18n as i18n
    from tianluo.engine.token_usage import UsageTotals, format_round_usage_footer

    i18n.set_language(lang)
    try:
        footer = format_round_usage_footer(
            UsageTotals(input_tokens=1234, output_tokens=567),
            UsageTotals(input_tokens=12345, output_tokens=6789),
        )
    finally:
        i18n.reset_language()
    assert footer == expected


def test_test_step_progress_line_follows_active_language():
    """The ``Running tests:`` progress line (every run with a test step) is i18n."""
    import tianluo.i18n as i18n

    i18n.set_language("zh-CN")
    try:
        rendered = i18n.t("engine.test.running", command="pytest -q")
    finally:
        i18n.reset_language()
    assert "pytest -q" in rendered
    assert "Running tests" not in rendered


@pytest.mark.parametrize(
    "lang,expected",
    [
        ("en-US", "in 12,345 · out 6,789 · cache(r/w) 1,000/200 · $0.0123"),
        ("zh-CN", "输入 12,345 · 输出 6,789 · 缓存(读/写) 1,000/200 · $0.0123"),
    ],
)
def test_usage_line_labels_follow_active_language(lang, expected):
    """The compact usage line is embedded in already-localized wrappers (e.g.
    the discovery cumulative footer), so its labels must localize too — a
    hardcoded 'in/out/cache(r/w)' would render a mixed-language line."""
    from tianluo.engine.token_usage import UsageTotals, format_usage_line

    i18n.set_language(lang)
    try:
        line = format_usage_line(
            UsageTotals(
                input_tokens=12345,
                output_tokens=6789,
                cache_creation_input_tokens=200,
                cache_read_input_tokens=1000,
                total_cost_usd=0.0123,
            )
        )
    finally:
        i18n.reset_language()
    assert line == expected


def test_discovery_confirm_metadata_follows_active_language():
    """The discovery programmatic-confirmation gate's prompt/option is framework
    UI copy: it renders in the active language (the refined description — LLM
    output — always passes through verbatim)."""
    from tianluo.engine.steps.discovery import discovery_confirm_metadata

    i18n.set_language("en-US")
    try:
        prompt, options = discovery_confirm_metadata("Add a /health endpoint")
    finally:
        i18n.reset_language()
    assert "Type 1 to confirm" in prompt
    assert "Proposed task description:" in prompt
    assert "Add a /health endpoint" in prompt
    assert options[0]["value"] == "1"
    assert "Confirm and continue" in options[0]["label"]

    i18n.set_language("zh-CN")
    try:
        prompt, options = discovery_confirm_metadata("Add a /health endpoint")
    finally:
        i18n.reset_language()
    assert "输入 1 确认" in prompt
    assert "Proposed task description:" not in prompt
    assert "Add a /health endpoint" in prompt  # LLM output is never translated
    assert options[0]["value"] == "1"
    assert "确认并继续" in options[0]["label"]


def test_state_machine_banners_have_no_hardcoded_english():
    """The revision-requested / adjudicate-confirmation banners are framework
    console chrome: they must render through i18n keys, not raw English, or a
    zh-CN run shows English banners inside an otherwise Chinese UI."""
    from pathlib import Path as _Path

    import tianluo.engine.state_machine as sm

    source = _Path(sm.__file__).read_text(encoding="utf-8")
    for literal in (
        "🔁 REVISION REQUESTED",
        "🔎 ADJUDICATE CONFIRMATION REQUESTED",
        'f"Reviewer: ',
        'f"Iteration: {iteration}"',
    ):
        assert literal not in source, literal

    i18n.set_language("zh-CN")
    try:
        assert i18n.t("engine.revision.banner_title", step="IMPLEMENT") == (
            "🔁 要求修订：IMPLEMENT"
        )
        # Reviewer feedback is user/LLM payload — passed through verbatim.
        assert "keep the guard" in i18n.t(
            "engine.revision.feedback", feedback="keep the guard"
        )
        assert "REQUESTED" not in i18n.t("engine.adjudicate.confirm_title")
        assert "human" in i18n.t("engine.adjudicate.reviewer", reviewer="human")
    finally:
        i18n.reset_language()


# --- status-value translation (t_status) -------------------------------------


@pytest.mark.parametrize(
    "value,expected_key",
    [
        ("open", "status.open"),
        ("in-progress", "status.in_progress"),
        ("won't-fix", "status.wont_fix"),
        ("revision_needed", "status.revision_needed"),
        ("COMPLETED", "status.completed"),
    ],
)
def test_status_key_normalizes_status_tokens(value, expected_key):
    """Status tokens carry hyphens/apostrophes/case; they must all normalize to
    one snake_case catalog key so a single ``status.*`` namespace can serve the
    issue, flow and step vocabularies."""
    assert i18n.status_key(value) == expected_key


def test_status_key_accepts_enum():
    from tianluo.engine.issue_manager import IssueStatus

    assert i18n.status_key(IssueStatus.IN_PROGRESS) == "status.in_progress"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("open", "待处理"),
        ("in-progress", "进行中"),
        ("won't-fix", "不修复"),
        ("completed", "已完成"),
        ("failed", "已失败"),
        ("revision_needed", "需修订"),
    ],
)
def test_t_status_localizes_known_statuses(value, expected):
    i18n.set_language("zh-CN")
    assert i18n.t_status(value) == expected


def test_t_status_accepts_enum_members():
    from tianluo.engine.issue_manager import IssueStatus
    from tianluo.engine.models import StepStatus

    i18n.set_language("zh-CN")
    assert i18n.t_status(IssueStatus.WONT_FIX) == "不修复"
    assert i18n.t_status(StepStatus.RETRYING) == "重试中"


def test_t_status_falls_back_to_raw_token_for_unknown_status():
    """A status is a *data* token, not a fixed call site: a value from a newer
    engine has no catalog entry by design. It must render as itself, never as
    t()'s key-echo (``status.brand_new``)."""
    i18n.set_language("zh-CN")
    assert i18n.t_status("brand-new") == "brand-new"
    i18n.set_language("en-US")
    assert i18n.t_status("brand-new") == "brand-new"


def test_t_status_en_us_renders_the_canonical_token():
    i18n.set_language("en-US")
    assert i18n.t_status("in-progress") == "in-progress"
    assert i18n.t_status("completed") == "completed"


# --- Error/success wrapper chrome (engine.output) ---

class TestOutputWrapperChrome:
    """The Error:/Context:/Error/Success chrome around every `se3 run` failure and
    success panel is user-visible, so it must render through i18n rather than the
    English literals it used to hardcode."""

    def test_format_error_prefix_is_translated(self):
        from tianluo.engine.output import format_error

        i18n.set_language("zh-CN")
        rendered = format_error("boom", {"flow": "f1"})
        assert "错误：" in rendered
        assert "上下文：" in rendered
        assert "Error:" not in rendered
        assert "boom" in rendered and "flow: f1" in rendered

    def test_format_error_prefix_in_base_language(self):
        from tianluo.engine.output import format_error

        i18n.set_language("en-US")
        assert "Error:" in format_error("boom")

    def test_panel_titles_are_translated(self, monkeypatch):
        from tianluo.engine import output

        titles: list[str] = []
        monkeypatch.setattr(
            output, "render_full", lambda content, title=None: titles.append(title)
        )
        i18n.set_language("zh-CN")
        output.display_error("boom")
        output.display_success("ok")
        assert titles == ["错误", "成功"]
