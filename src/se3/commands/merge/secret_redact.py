"""Secret redaction for merge logs and traces.

Detects and masks common secret patterns in text strings before they
are written to log files or trace files.  Patterns include:

  * API keys (``sk-...``, ``ak-...``)
  * Bearer tokens (``Bearer <token>``)
  * GitHub personal-access tokens (``ghp_...``)
  * PyPI / npm tokens (``pypi-...``, ``npm_...``)
  * Password fields in TOML / JSON / YAML
  * Generic ``token=`` / ``api_key=`` query parameters

An allowlist supports exempting specific keys or patterns from
redaction (e.g. test fixtures that intentionally contain fake secrets).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Default patterns
# ---------------------------------------------------------------------------

_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # --- Structured password fields (apply before generic patterns) ---
    # TOML password field
    (
        re.compile(r"(^\s*password\s*=\s*['\"])[^'\"]{1,500}(['\"])", re.MULTILINE),
        r"\1***\2",
    ),
    # JSON "password": "..."
    (
        re.compile(r'("password"\s*:\s*")[^"]{1,500}(")'),
        r'\1***\2',
    ),
    # --- API keys and tokens ---
    # OpenAI / Anthropic API keys
    (
        re.compile(r"\b(sk-[a-zA-Z0-9_-]{10,100})\b"),
        "sk-***",
    ),
    # Generic API key prefixes
    (
        re.compile(r"\b(ak-[a-zA-Z0-9_-]{10,100})\b"),
        "ak-***",
    ),
    # GitHub personal access tokens
    (
        re.compile(r"\b(gh[pousr]_[A-Za-z0-9_]{36,})\b"),
        "gh***",
    ),
    # PyPI API tokens
    (
        re.compile(r"\b(pypi-[A-Za-z0-9_-]{10,200})\b"),
        "pypi-***",
    ),
    # npm access tokens
    (
        re.compile(r"\b(npm_[A-Za-z0-9]{36,})\b"),
        "npm_***",
    ),
    # Bearer token header value.
    #
    # The character class includes the URL-safe base64 alphabet plus
    # the standard base64 padding/extra characters (``+``, ``/``, ``=``)
    # so legacy OAuth2 access tokens and PASETO tokens with traditional
    # base64 padding are also redacted, not just JWT-style URL-safe
    # tokens.  Without ``+/=`` a base64 token like
    # ``Bearer abc+def/ghi=`` would leak past the ``+``/``/``/``=``
    # boundary into log output.
    #
    # G3: also accept ``;`` and ``,`` so cookie-style or multi-token
    # Bearer values (e.g. ``Bearer abc;path=/`` or
    # ``Bearer t1, Bearer t2`` with comma-separated tokens) are
    # redacted in their entirety. The greedy ``+`` length match
    # combined with the {10,500} bound prevents over-redaction past
    # the line boundary.
    (
        re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.+/=;,]{10,500}", re.IGNORECASE),
        r"\1***",
    ),
    # Generic token=... query param or form field
    #
    # Trade-off: the value-length lower bound is intentionally {8,500}
    # rather than the {1,500} used by the TOML/JSON-quoted patterns
    # above.  The unquoted form has no quote-delimiter to bound the
    # match, so a {1,500} lower bound would over-redact (e.g. it would
    # eat ``api_key = 'sk-abc...'`` before the dedicated ``sk-`` pattern
    # gets a chance to match).  The cost is that very short
    # human-chosen passwords like ``password=secret`` (6 chars) leak
    # verbatim into logs.  Mitigation: the structured TOML/JSON
    # patterns above redact ``password = 'secret'`` and
    # ``"password": "secret"`` regardless of value length, so
    # short-password leaks only occur when the source format is
    # unquoted ``key=value`` text — which is itself rare in our
    # logs/traces.  Tighten or lengthen the regex if usage patterns
    # shift.
    (
        re.compile(r"((?:token|api_key|apikey|secret|password|passwd)\s*=\s*)['\"]?[A-Za-z0-9_\-\.]{8,500}['\"]?", re.IGNORECASE),
        r"\1***",
    ),
    # Authorization header (any scheme).
    #
    # G3: extend the character class to include ``;`` and ``,`` so
    # cookie-style multi-token Authorization values (rare but
    # RFC-permitted) and comma-separated credentials are redacted as
    # a single span. Without ``;,`` a value like
    # ``Authorization: Basic abc=; path=/`` would leak the cookie
    # attributes after the ``;``.
    (
        re.compile(r"(Authorization\s*:\s*\S+\s+)[A-Za-z0-9_\-\.=/+;,]{10,500}", re.IGNORECASE),
        r"\1***",
    ),
]


@dataclass
class RedactConfig:
    """Configuration for secret redaction.

    Attributes:
        allowlist: Exact string values that are exempt from redaction.
            Useful for test fixtures with intentionally fake secrets.
        extra_patterns: Additional (compiled regex, replacement) pairs
            applied after the built-in patterns.
        preview_length: Maximum length of a redacted preview before
            truncation.
    """

    allowlist: set[str] = field(default_factory=set)
    extra_patterns: list[tuple[re.Pattern[str], str]] = field(default_factory=list)
    preview_length: int = 80


class SecretRedactor:
    """Stateful redactor with configurable allowlist and extra patterns.

    Thread-safe (no mutable state after construction).
    """

    def __init__(self, config: Optional[RedactConfig] = None) -> None:
        self.config = config or RedactConfig()
        self._patterns = _SECRET_PATTERNS + self.config.extra_patterns

    def redact(self, text: str) -> str:
        """Return a copy of *text* with secrets replaced."""
        if not text:
            return text
        result = text
        for pattern, replacement in self._patterns:
            result = pattern.sub(replacement, result)
        # Allowlist restoration: if any allowlisted string was
        # accidentally masked, put it back.  This is a best-effort
        # reverse — exact match only.
        for allowed in self.config.allowlist:
            # Only restore if the allowed value is a known fake.
            masked = self._mask_fake(allowed)
            if masked != allowed:
                result = result.replace(masked, allowed)
        return result

    @staticmethod
    def _mask_fake(value: str) -> str:
        """Apply the same masking logic to a single known-fake value."""
        result = value
        for pattern, replacement in _SECRET_PATTERNS:
            result = pattern.sub(replacement, result)
        return result

    def redact_diff(self, diff_text: str) -> str:
        """Redact secrets inside a git diff.

        Operates line-by-line so that diff structure (``+``, ``-``,
        ``@@`` markers) is preserved.
        """
        if not diff_text:
            return diff_text
        lines = diff_text.splitlines(keepends=True)
        return "".join(self.redact(line) for line in lines)


# ---------------------------------------------------------------------------
# Module-level convenience functions (default config)
# ---------------------------------------------------------------------------

_default_redactor: Optional[SecretRedactor] = None


def _get_default() -> SecretRedactor:
    global _default_redactor
    if _default_redactor is None:
        _default_redactor = SecretRedactor()
    return _default_redactor


def redact_text(text: str) -> str:
    """Redact secrets from *text* using the default configuration."""
    return _get_default().redact(text)


def redact_diff(diff_text: str) -> str:
    """Redact secrets from a git diff using the default configuration."""
    return _get_default().redact_diff(diff_text)
