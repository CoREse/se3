"""The WebUI language registry is served from the locale files on disk.

Adding a UI language must be a pure data change: dropping a new
``static/i18n/<code>.json`` makes it selectable in the console without editing
app.js. The frontend boots that registry from ``GET /i18n/index.json``, which
this module pins: it is unauthenticated (the language is chosen before sign-in),
it is derived from the locale directory, and it carries each language's endonym.
"""

from __future__ import annotations

import json

import pytest

from _authsrv import authed_app
from tianluo.server import app as app_mod


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    app, _key = authed_app()
    with TestClient(app) as c:
        yield c


def test_manifest_lists_the_shipped_languages_unauthenticated(client):
    # No login(): the switcher paints before the operator signs in.
    resp = client.get("/i18n/index.json")
    assert resp.status_code == 200
    codes = [item["code"] for item in resp.json()["languages"]]
    assert "en-US" in codes and "zh-CN" in codes
    labels = {item["code"]: item["label"] for item in resp.json()["languages"]}
    # Endonyms come from each dictionary's own ``lang.<code>`` entry.
    assert labels["en-US"] == "English"
    assert labels["zh-CN"] == "中文"


def test_a_new_locale_file_is_discovered_without_a_code_change(monkeypatch, tmp_path):
    locales = tmp_path / "i18n"
    locales.mkdir()
    (locales / "en-US.json").write_text(
        json.dumps({"lang.en-US": "English"}), encoding="utf-8"
    )
    (locales / "fr-FR.json").write_text(
        json.dumps({"lang.fr-FR": "Français"}), encoding="utf-8"
    )
    monkeypatch.setattr(app_mod, "UI_LOCALES_DIR", locales)

    langs = app_mod._discover_ui_languages()
    assert langs == [
        {"code": "en-US", "label": "English"},
        {"code": "fr-FR", "label": "Français"},
    ]


def test_a_malformed_locale_file_is_skipped_not_fatal(monkeypatch, tmp_path):
    locales = tmp_path / "i18n"
    locales.mkdir()
    (locales / "en-US.json").write_text(
        json.dumps({"lang.en-US": "English"}), encoding="utf-8"
    )
    (locales / "broken.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(app_mod, "UI_LOCALES_DIR", locales)

    assert app_mod._discover_ui_languages() == [{"code": "en-US", "label": "English"}]


def test_a_locale_without_its_own_endonym_labels_itself(monkeypatch, tmp_path):
    locales = tmp_path / "i18n"
    locales.mkdir()
    (locales / "ja-JP.json").write_text(json.dumps({"nav.history": "履歴"}), encoding="utf-8")
    monkeypatch.setattr(app_mod, "UI_LOCALES_DIR", locales)

    assert app_mod._discover_ui_languages() == [{"code": "ja-JP", "label": "ja-JP"}]
