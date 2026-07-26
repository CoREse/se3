"""Tests for SecretRedactor and module-level convenience functions."""

from __future__ import annotations

import pytest

from tianluo.commands.merge.secret_redact import (
    RedactConfig,
    SecretRedactor,
    redact_diff,
    redact_text,
)


class TestRedactText:
    """Module-level redact_text with default config."""

    def test_openai_api_key(self) -> None:
        text = "The key is sk-abc123def456ghi789jkl012mno345pqr"
        result = redact_text(text)
        assert "sk-***" in result
        assert "sk-abc123" not in result

    def test_github_token(self) -> None:
        text = "Authorization: token ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        result = redact_text(text)
        assert "gh***" in result
        assert "ghp_" not in result

    def test_bearer_token(self) -> None:
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        result = redact_text(text)
        assert "Bearer ***" in result

    def test_toml_password(self) -> None:
        text = 'password = "supersecret123"\n'
        result = redact_text(text)
        assert 'password = "***"' in result

    def test_json_password(self) -> None:
        text = '{"password": "supersecret123"}'
        result = redact_text(text)
        assert '"password": "***"' in result

    def test_generic_token_param(self) -> None:
        text = "curl https://api.example.com?token=abc123def456"
        result = redact_text(text)
        assert "token=***" in result

    def test_no_false_positive_short_token(self) -> None:
        # Short values should not be redacted (minimum length is 8).
        text = "token=abc"
        result = redact_text(text)
        assert result == "token=abc"

    def test_no_secret_no_change(self) -> None:
        text = "This is a normal sentence without any secrets."
        result = redact_text(text)
        assert result == text

    def test_empty_string(self) -> None:
        assert redact_text("") == ""

    def test_multiple_secrets(self) -> None:
        text = (
            "API key: sk-abc123def456ghi789\n"
            "GitHub: ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n"
            "Bearer: Bearer eyJhbGciOiJIUzI1NiJ9\n"
        )
        result = redact_text(text)
        assert "sk-***" in result
        assert "gh***" in result
        assert "Bearer ***" in result


class TestRedactDiff:
    """Git diff redaction preserves line structure."""

    def test_diff_line_prefixes_preserved(self) -> None:
        diff = (
            "diff --git a/config.py b/config.py\n"
            "--- a/config.py\n"
            "+++ b/config.py\n"
            "@@ -1,3 +1,3 @@\n"
            "-api_key = 'old_key'\n"
            "+api_key = 'sk-abc123def456'\n"
        )
        result = redact_diff(diff)
        assert "diff --git" in result
        assert "--- a/config.py" in result
        assert "+++ b/config.py" in result
        assert "@@ -1,3 +1,3 @@" in result
        assert "-api_key" in result
        assert "+api_key" in result
        assert "sk-***" in result
        assert "sk-abc123" not in result

    def test_diff_empty(self) -> None:
        assert redact_diff("") == ""


class TestSecretRedactorAllowlist:
    """Allowlist restores known-fake values."""

    def test_allowlist_restores_fake_secret(self) -> None:
        config = RedactConfig(allowlist={"sk-fake-test-key-12345"})
        redactor = SecretRedactor(config)
        text = "Key: sk-fake-test-key-12345"
        result = redactor.redact(text)
        # The fake key is restored because it matches the allowlist.
        assert "sk-fake-test-key-12345" in result
        assert "sk-***" not in result

    def test_allowlist_no_effect_on_real_secrets(self) -> None:
        config = RedactConfig(allowlist={"sk-fake"})
        redactor = SecretRedactor(config)
        text = "Key: sk-real-secret-key-12345"
        result = redactor.redact(text)
        assert "sk-***" in result
        assert "sk-real-secret-key-12345" not in result


class TestSecretRedactorExtraPatterns:
    """Custom patterns via RedactConfig."""

    def test_extra_pattern(self) -> None:
        import re

        config = RedactConfig(
            extra_patterns=[
                (re.compile(r"\b(mycustom-[a-z0-9]{10,})\b"), "mycustom-***")
            ]
        )
        redactor = SecretRedactor(config)
        text = "Token: mycustom-abc123def456"
        result = redactor.redact(text)
        assert "mycustom-***" in result
        assert "mycustom-abc123" not in result


class TestRedactConfig:
    """Dataclass defaults."""

    def test_defaults(self) -> None:
        config = RedactConfig()
        assert config.allowlist == set()
        assert config.extra_patterns == []
        assert config.preview_length == 80
