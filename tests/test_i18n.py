"""Tests for the se3.i18n resource layer and language-resolution chain.

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

import se3.i18n as i18n
from se3.i18n import loader


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
    monkeypatch.setattr("se3.config.Path.home", lambda: home)
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


# --- Resolution precedence chain ---


class TestResolveLanguageChain:
    def _write_project(self, root: Path, language):
        val = language if language else "null"
        (root / "se3.yaml").write_text(
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


# --- Lazy singleton behavior ---


class TestLazySingleton:
    def test_import_does_not_read_config(self, tmp_path, monkeypatch):
        """import se3.i18n is side-effect free: nothing resolved until first t()."""
        i18n.reset_language()
        # Access the private slot to confirm it's unresolved after reset.
        assert i18n._current_language is None

    def test_get_language_resolves_from_cwd(self, tmp_path, monkeypatch):
        (tmp_path / "se3.yaml").write_text("language:\n  language: zh-CN\n")
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
    assert any(n.endswith("se3/i18n/locales/en-US.json") for n in names), names
    assert any(n.endswith("se3/i18n/locales/zh-CN.json") for n in names), names
