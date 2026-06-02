"""Tests for the server / auth configuration (G1 seam).

Covers:
- ServerConfig / AuthConfig defaults (no ``server:`` section)
- providers parsing (defaults, unknown filtering, dedup, fallback)
- session cookie security attributes + validation
- local-auth lockout / rate-limit parsing + validation
- oidc / proxy_header disabled-by-default seams
- db_path default + resolution (~ expansion)
- se3.yaml / global config override + local-override precedence
"""

from __future__ import annotations

from pathlib import Path

import yaml

from se3.config import (
    AuthConfig,
    LocalAuthConfig,
    OidcConfig,
    ProxyHeaderConfig,
    ServerConfig,
    SessionConfig,
    load_server_config,
)


def _write_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


class TestServerConfigDefaults:
    def test_empty_dict_defaults(self):
        cfg = ServerConfig.from_dict({})
        assert cfg.db_path == "~/.se3/server.db"
        assert cfg.auth.providers == ["local"]

    def test_non_mapping_defaults(self):
        cfg = ServerConfig.from_dict(["not", "a", "mapping"])
        assert cfg.db_path == "~/.se3/server.db"
        assert cfg.auth.providers == ["local"]

    def test_auth_subconfigs_are_defaults(self):
        cfg = ServerConfig.from_dict({})
        assert isinstance(cfg.auth, AuthConfig)
        assert isinstance(cfg.auth.session, SessionConfig)
        assert isinstance(cfg.auth.local, LocalAuthConfig)
        assert isinstance(cfg.auth.oidc, OidcConfig)
        assert isinstance(cfg.auth.proxy_header, ProxyHeaderConfig)

    def test_session_security_defaults_are_failsafe(self):
        s = ServerConfig.from_dict({}).auth.session
        assert s.cookie_secure is True
        assert s.cookie_httponly is True
        assert s.cookie_samesite == "lax"
        assert s.cookie_name == "se3_session"
        assert s.max_age_seconds == 86400

    def test_local_defaults(self):
        local = ServerConfig.from_dict({}).auth.local
        assert local.max_failed_attempts == 5
        assert local.lockout_seconds == 300
        assert local.ratelimit_window_seconds == 60
        assert local.ratelimit_max_attempts == 10

    def test_oidc_and_proxy_disabled_by_default(self):
        auth = ServerConfig.from_dict({}).auth
        assert auth.oidc.enabled is False
        assert auth.oidc.scopes == ["openid", "email", "profile"]
        assert auth.proxy_header.enabled is False
        assert auth.proxy_header.header == "X-Forwarded-Email"

    def test_resolved_db_path_expands_user(self):
        cfg = ServerConfig.from_dict({})
        resolved = cfg.resolved_db_path()
        assert isinstance(resolved, Path)
        assert "~" not in str(resolved)
        assert str(resolved).endswith(".se3/server.db")


class TestAuthProvidersParsing:
    def test_explicit_providers(self):
        cfg = ServerConfig.from_dict(
            {"auth": {"providers": ["local", "oidc"]}}
        )
        assert cfg.auth.providers == ["local", "oidc"]

    def test_unknown_providers_filtered(self):
        cfg = ServerConfig.from_dict(
            {"auth": {"providers": ["local", "bogus"]}}
        )
        assert cfg.auth.providers == ["local"]

    def test_all_unknown_falls_back_to_local(self):
        cfg = ServerConfig.from_dict({"auth": {"providers": ["nope"]}})
        assert cfg.auth.providers == ["local"]

    def test_non_list_falls_back_to_local(self):
        cfg = ServerConfig.from_dict({"auth": {"providers": "local"}})
        assert cfg.auth.providers == ["local"]

    def test_dedup_and_case_insensitive(self):
        cfg = ServerConfig.from_dict(
            {"auth": {"providers": ["LOCAL", "local", "Oidc"]}}
        )
        assert cfg.auth.providers == ["local", "oidc"]

    def test_blank_entries_skipped(self):
        cfg = ServerConfig.from_dict(
            {"auth": {"providers": ["", "  ", "proxy_header"]}}
        )
        assert cfg.auth.providers == ["proxy_header"]


class TestSessionValidation:
    def test_invalid_samesite_falls_back(self):
        s = SessionConfig.from_dict({"cookie_samesite": "bogus"})
        assert s.cookie_samesite == "lax"

    def test_valid_samesite_strict(self):
        s = SessionConfig.from_dict({"cookie_samesite": "Strict"})
        assert s.cookie_samesite == "strict"

    def test_blank_cookie_name_falls_back(self):
        s = SessionConfig.from_dict({"cookie_name": "   "})
        assert s.cookie_name == "se3_session"

    def test_secure_can_be_disabled(self):
        s = SessionConfig.from_dict({"cookie_secure": False})
        assert s.cookie_secure is False

    def test_invalid_max_age_falls_back(self):
        s = SessionConfig.from_dict({"max_age_seconds": -5})
        assert s.max_age_seconds == 86400

    def test_non_int_max_age_falls_back(self):
        s = SessionConfig.from_dict({"max_age_seconds": "abc"})
        assert s.max_age_seconds == 86400


class TestLocalAuthValidation:
    def test_custom_values(self):
        local = LocalAuthConfig.from_dict(
            {
                "max_failed_attempts": 3,
                "lockout_seconds": 600,
                "ratelimit_window_seconds": 30,
                "ratelimit_max_attempts": 20,
            }
        )
        assert local.max_failed_attempts == 3
        assert local.lockout_seconds == 600
        assert local.ratelimit_window_seconds == 30
        assert local.ratelimit_max_attempts == 20

    def test_non_positive_falls_back(self):
        local = LocalAuthConfig.from_dict(
            {"max_failed_attempts": 0, "lockout_seconds": -1}
        )
        assert local.max_failed_attempts == 5
        assert local.lockout_seconds == 300

    def test_bool_rejected(self):
        # YAML true should not be coerced to 1.
        local = LocalAuthConfig.from_dict({"max_failed_attempts": True})
        assert local.max_failed_attempts == 5


class TestOidcAndProxySeams:
    def test_oidc_enable_and_fields(self):
        oidc = OidcConfig.from_dict(
            {
                "enabled": True,
                "issuer": "https://idp.example.com",
                "client_id": "abc",
                "client_secret": "shh",
                "redirect_url": "https://app/cb",
                "scopes": ["openid", "email"],
            }
        )
        assert oidc.enabled is True
        assert oidc.issuer == "https://idp.example.com"
        assert oidc.client_id == "abc"
        assert oidc.scopes == ["openid", "email"]

    def test_oidc_invalid_scopes_fall_back(self):
        oidc = OidcConfig.from_dict({"scopes": "openid"})
        assert oidc.scopes == ["openid", "email", "profile"]

    def test_oidc_blank_fields_become_none(self):
        oidc = OidcConfig.from_dict({"issuer": "  "})
        assert oidc.issuer is None

    def test_proxy_header_custom(self):
        ph = ProxyHeaderConfig.from_dict(
            {"enabled": True, "trust_proxy": True, "header": "X-Auth-Email"}
        )
        assert ph.enabled is True
        assert ph.trust_proxy is True
        assert ph.header == "X-Auth-Email"

    def test_proxy_header_trust_proxy_defaults_false(self):
        ph = ProxyHeaderConfig.from_dict({"enabled": True})
        assert ph.trust_proxy is False

    def test_proxy_header_blank_falls_back(self):
        ph = ProxyHeaderConfig.from_dict({"header": ""})
        assert ph.header == "X-Forwarded-Email"
        assert ph.trust_proxy is False


class TestDbPath:
    def test_custom_db_path(self):
        cfg = ServerConfig.from_dict({"db_path": "/var/lib/se3/db.sqlite"})
        assert cfg.db_path == "/var/lib/se3/db.sqlite"
        assert cfg.resolved_db_path() == Path("/var/lib/se3/db.sqlite")

    def test_blank_db_path_falls_back(self):
        cfg = ServerConfig.from_dict({"db_path": "   "})
        assert cfg.db_path == "~/.se3/server.db"


class TestLoadServerConfig:
    def test_missing_yaml_returns_defaults(self, tmp_path):
        cfg = load_server_config(tmp_path)
        assert cfg.db_path == "~/.se3/server.db"
        assert cfg.auth.providers == ["local"]

    def test_yaml_without_server_section(self, tmp_path):
        _write_yaml(tmp_path / "se3.yaml", {"version": {"enabled": True}})
        cfg = load_server_config(tmp_path)
        assert cfg.auth.providers == ["local"]

    def test_yaml_with_server_section(self, tmp_path):
        _write_yaml(
            tmp_path / "se3.yaml",
            {
                "server": {
                    "db_path": "/data/se3.db",
                    "auth": {
                        "providers": ["local", "proxy_header"],
                        "session": {"cookie_secure": False},
                        "local": {"max_failed_attempts": 7},
                    },
                }
            },
        )
        cfg = load_server_config(tmp_path)
        assert cfg.db_path == "/data/se3.db"
        assert cfg.auth.providers == ["local", "proxy_header"]
        assert cfg.auth.session.cookie_secure is False
        assert cfg.auth.local.max_failed_attempts == 7

    def test_local_yaml_shadows_yaml(self, tmp_path):
        _write_yaml(
            tmp_path / "se3.yaml",
            {"server": {"db_path": "/from-yaml.db"}},
        )
        _write_yaml(
            tmp_path / "se3.local.yaml",
            {"server": {"db_path": "/from-local.db"}},
        )
        cfg = load_server_config(tmp_path)
        assert cfg.db_path == "/from-local.db"

    def test_existing_config_loading_not_broken(self, tmp_path):
        # A se3.yaml carrying both a server: section and unrelated sections
        # still loads other config without error.
        from se3.config import load_version_config

        _write_yaml(
            tmp_path / "se3.yaml",
            {
                "server": {"auth": {"providers": ["local"]}},
                "version": {"enabled": False},
            },
        )
        assert load_server_config(tmp_path).auth.providers == ["local"]
        assert load_version_config(tmp_path).enabled is False
