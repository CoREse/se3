"""Versioned model pricing catalog shared by usage estimation and display.

Intentionally depends only on the Python standard library (like
:mod:`tianluo.usage`) so the daemon and history readers can estimate costs
without importing the engine or PyYAML. Project overrides live in
``tianluo.yaml`` under ``pricing.models.<canonical_model>`` and are validated
and merged in by :mod:`tianluo.config` before being passed here.

Prices are expressed in **USD per million tokens**. A ``None`` price means the
category is genuinely unknown for that model — callers must treat it as
"unknown cost", never as zero, because a fabricated ``$0`` would masquerade as
an exact bill while actually reflecting missing data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import (
    Any,
    Callable,
    Dict,
    Mapping,
    Optional,
    Tuple,
)

# Version of the built-in price table. Bumped whenever entries are added,
# corrected, or retired. Each entry also records its own source URL and
# effective date, so a stale or disputed price is traceable to one line.
PRICING_CATALOG_VERSION = "2026-08-13"


class TokenCategory(str, Enum):
    """The six mutually exclusive token classes a price entry covers.

    These deliberately mirror the ``UsageRecord`` token fields: cache read and
    cache creation are *subsets* of logical input, so pricing them separately
    (and never pricing ``logical_input`` itself) is what keeps a token from
    being billed twice.
    """

    UNCACHED_INPUT = "uncached_input"
    OUTPUT = "output"
    CACHE_READ = "cache_read"
    CACHE_CREATION = "cache_creation"
    CACHE_CREATION_5M = "cache_creation_5m"
    CACHE_CREATION_1H = "cache_creation_1h"


@dataclass(frozen=True)
class ModelPrice:
    """Per-model USD-per-million price across the six token categories.

    ``None`` marks an unknown category; ``effective_date`` and ``source`` keep
    every built-in entry auditable, and ``catalog_version`` records which table
    version the entry came from.
    """

    model: str
    uncached_input: Optional[float] = None
    output: Optional[float] = None
    cache_read: Optional[float] = None
    cache_creation: Optional[float] = None
    cache_creation_5m: Optional[float] = None
    cache_creation_1h: Optional[float] = None
    catalog_version: str = PRICING_CATALOG_VERSION
    source: str = ""
    effective_date: str = ""

    def price_for(self, category: TokenCategory) -> Optional[float]:
        """USD per million tokens for ``category``, or ``None`` when unknown."""
        return {
            TokenCategory.UNCACHED_INPUT: self.uncached_input,
            TokenCategory.OUTPUT: self.output,
            TokenCategory.CACHE_READ: self.cache_read,
            TokenCategory.CACHE_CREATION: self.cache_creation,
            TokenCategory.CACHE_CREATION_5M: self.cache_creation_5m,
            TokenCategory.CACHE_CREATION_1H: self.cache_creation_1h,
        }[category]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model": self.model,
            "uncached_input": self.uncached_input,
            "output": self.output,
            "cache_read": self.cache_read,
            "cache_creation": self.cache_creation,
            "cache_creation_5m": self.cache_creation_5m,
            "cache_creation_1h": self.cache_creation_1h,
            "catalog_version": self.catalog_version,
            "source": self.source,
            "effective_date": self.effective_date,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ModelPrice":
        return cls(
            model=str(data.get("model") or ""),
            uncached_input=data.get("uncached_input"),
            output=data.get("output"),
            cache_read=data.get("cache_read"),
            cache_creation=data.get("cache_creation"),
            cache_creation_5m=data.get("cache_creation_5m"),
            cache_creation_1h=data.get("cache_creation_1h"),
            catalog_version=str(
                data.get("catalog_version") or PRICING_CATALOG_VERSION
            ),
            source=str(data.get("source") or ""),
            effective_date=str(data.get("effective_date") or ""),
        )


@dataclass(frozen=True)
class CostEstimate:
    """Outcome of estimating a unit's cost from tokens against a price entry.

    ``estimated_cost_usd`` is ``None`` whenever the estimate is not fully
    computable (no tokens, unknown model, or a nonzero category without a
    price) — callers must surface "unknown", not treat ``None`` as zero.
    ``unknown_categories`` lists which nonzero token classes lacked a price,
    and ``reason`` is a short human-readable cause for display.
    """

    estimated_cost_usd: Optional[float]
    unknown_categories: Tuple[TokenCategory, ...] = ()
    priced: Tuple[Tuple[TokenCategory, float], ...] = ()
    reason: str = ""

    @property
    def is_estimated(self) -> bool:
        return self.estimated_cost_usd is not None


def estimate_cost(
    price: Optional[ModelPrice],
    tokens: Mapping[TokenCategory, int],
) -> CostEstimate:
    """Estimate USD cost from six-category token counts and a price entry.

    Returns "unknown" (``estimated_cost_usd=None``) rather than a partial sum
    whenever any *nonzero* category lacks a price: a partial total would
    understate the real cost while looking exact. Only categories that actually
    carry tokens matter — a zero-token category with no price does not block
    the estimate.
    """
    nonzero: Dict[TokenCategory, int] = {}
    for category, n in tokens.items():
        if not isinstance(category, TokenCategory):
            try:
                category = TokenCategory(str(category))
            except ValueError:
                continue  # Unknown key (e.g. "logical_input") is never priced.
        if n > 0:
            nonzero[category] = n
    if not nonzero:
        return CostEstimate(None, reason="no token usage")
    if price is None:
        return CostEstimate(None, reason="unknown model")
    unknown = [
        category
        for category, n in sorted(nonzero.items(), key=lambda item: item[0].value)
        if price.price_for(category) is None
    ]
    if unknown:
        return CostEstimate(
            None,
            unknown_categories=tuple(unknown),
            reason="missing price for " + ", ".join(c.value for c in unknown),
        )
    priced = tuple(
        (
            category,
            n * float(price.price_for(category)) / 1_000_000.0,  # type: ignore[arg-type]
        )
        for category, n in sorted(nonzero.items(), key=lambda item: item[0].value)
    )
    return CostEstimate(
        estimated_cost_usd=sum(cost for _, cost in priced),
        priced=priced,
        reason="estimated",
    )


# Canonical model name -> aliases accepted on input. Aliases are normalized
# BEFORE pricing so a CLI-reported alias or a dated model id maps onto the same
# price row as its canonical name; unmappable names stay unmappable (unknown
# cost) instead of guessing a price for an alias we do not own.
_MODEL_ALIASES: Dict[str, Tuple[str, ...]] = {
    "claude-opus-5": ("opus-5", "claude-5-opus", "claude-opus-5-1"),
    "claude-sonnet-5": ("sonnet-5", "claude-5-sonnet", "claude-sonnet-5-1"),
    "claude-haiku-4-5": ("haiku-4-5", "claude-5-haiku", "claude-haiku-4-5-1"),
    "claude-fable-5": ("fable-5", "claude-5-fable", "claude-fable-5-1"),
    "claude-opus-4-8": ("claude-opus-4-8-1", "claude-opus-4-7", "claude-opus-4-6"),
    "claude-sonnet-4-6": ("claude-sonnet-4-6-1", "claude-sonnet-4-5"),
    "claude-opus-4-1": ("claude-opus-4", "claude-3-opus", "claude-opus-4-0"),
    "claude-sonnet-4": ("claude-3-5-sonnet", "claude-3-7-sonnet"),
    "gpt-5": ("gpt-5-1", "chatgpt-5"),
    "gpt-5-mini": ("gpt-5-mini-1", "chatgpt-5-mini"),
    "gpt-5-nano": ("gpt-5-nano-1", "chatgpt-5-nano"),
    "gpt-4o": ("gpt-4o-1", "chatgpt-4o-latest"),
    "gpt-4o-mini": ("gpt-4o-mini-1", "chatgpt-4o-mini"),
}

# Reverse lookup built once: alias -> canonical.
_ALIAS_TO_CANONICAL: Dict[str, str] = {
    alias: canonical
    for canonical, aliases in _MODEL_ALIASES.items()
    for alias in aliases
}

# A provider snapshot id appends a date to its base model name in one of two
# forms — compact Anthropic style (claude-opus-4-1-20250805) or dashed
# snapshot style (gpt-4o-2024-08-06). Either maps onto the base model's price
# row, so the suffix is stripped before the alias lookup below.
_DATED_MODEL_SUFFIX_RE = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2})$")


def canonicalize_pricing_model(model: Optional[str]) -> Optional[str]:
    """Normalize a reported model name to its canonical price-table key.

    A CLI-reported alias or a dated provider snapshot id whose base name the
    table owns (e.g. ``claude-opus-4-1-20250805``) maps onto the same price
    row as its canonical base model. Returns ``None`` when the name is empty,
    carries an unexpanded ``$VAR`` reference, or cannot be mapped — callers
    then report "unknown cost" rather than pricing a guess.
    """
    if not model or not isinstance(model, str):
        return None
    name = model.strip().lower()
    if not name or "$" in name or "{" in name:
        return None
    if name in _ALIAS_TO_CANONICAL:
        return _ALIAS_TO_CANONICAL[name]
    # Provider streams report dated snapshot ids (claude-opus-4-1-20250805);
    # strip the trailing date so the base name resolves through the alias
    # table / catalog entries instead of reading as an uncatalogued model.
    base = name
    while _DATED_MODEL_SUFFIX_RE.search(base):
        base = _DATED_MODEL_SUFFIX_RE.sub("", base)
    if not base:
        return None  # The name was nothing but a date suffix.
    if base != name:
        if base in _ALIAS_TO_CANONICAL:
            return _ALIAS_TO_CANONICAL[base]
        return base
    return name


def _anthropic(
    model: str,
    input_price: float,
    output_price: float,
    *,
    source: str,
    effective_date: str,
) -> ModelPrice:
    """Build an Anthropic entry from its base input/output rates.

    Anthropic publishes cache prices as fixed multiples of the base input rate
    (read 0.10x, 5-minute write 1.25x, 1-hour write 2.0x), so deriving them
    keeps the table consistent and the provenance on one line.
    """
    return ModelPrice(
        model=model,
        uncached_input=input_price,
        output=output_price,
        cache_read=round(input_price * 0.10, 4),
        cache_creation=round(input_price * 1.25, 4),
        cache_creation_5m=round(input_price * 1.25, 4),
        cache_creation_1h=round(input_price * 2.0, 4),
        source=source,
        effective_date=effective_date,
    )


def _openai(
    model: str,
    input_price: float,
    output_price: float,
    *,
    cached_input: Optional[float],
    source: str,
    effective_date: str,
) -> ModelPrice:
    """Build an OpenAI entry; cached input is a published subset rate, and
    cache *creation* categories stay ``None`` (not reported by OpenAI)."""
    return ModelPrice(
        model=model,
        uncached_input=input_price,
        output=output_price,
        cache_read=cached_input,
        cache_creation=None,
        cache_creation_5m=None,
        cache_creation_1h=None,
        source=source,
        effective_date=effective_date,
    )


def _builtin_entries() -> Dict[str, ModelPrice]:
    """The versioned built-in price table.

    Values are USD per million tokens as published by the provider pricing
    pages; each entry records its source and the date the price took effect.
    Projects override or extend this via ``pricing.models`` in tianluo.yaml.
    Codex models are intentionally absent — they bill by subscription, so no
    per-token price exists and costs display as unknown rather than a fake 0.
    """
    anthropic_source = "https://www.anthropic.com/pricing"
    tracker_source = "https://benchlm.ai/anthropic/api-pricing"
    openai_source = "https://openai.com/api/pricing/"
    entries: Dict[str, ModelPrice] = {}
    for entry in (
        _anthropic(
            "claude-opus-5", 5.0, 25.0,
            source=tracker_source, effective_date="2026-05-01",
        ),
        # Sonnet 5 intro price (through 2026-08-31); standard rate is 3/15.
        _anthropic(
            "claude-sonnet-5", 2.0, 10.0,
            source=tracker_source, effective_date="2026-05-01",
        ),
        _anthropic(
            "claude-haiku-4-5", 1.0, 5.0,
            source=anthropic_source, effective_date="2025-10-01",
        ),
        _anthropic(
            "claude-fable-5", 10.0, 50.0,
            source=tracker_source, effective_date="2026-05-01",
        ),
        _anthropic(
            "claude-opus-4-8", 5.0, 25.0,
            source=anthropic_source, effective_date="2025-11-01",
        ),
        _anthropic(
            "claude-sonnet-4-6", 3.0, 15.0,
            source=anthropic_source, effective_date="2025-09-01",
        ),
        _anthropic(
            "claude-opus-4-1", 15.0, 75.0,
            source=anthropic_source, effective_date="2024-11-01",
        ),
        _anthropic(
            "claude-sonnet-4", 3.0, 15.0,
            source=anthropic_source, effective_date="2024-10-01",
        ),
        _openai(
            "gpt-5", 1.25, 10.0, cached_input=0.125,
            source=openai_source, effective_date="2025-08-01",
        ),
        _openai(
            "gpt-5-mini", 0.25, 2.0, cached_input=0.025,
            source=openai_source, effective_date="2025-08-01",
        ),
        _openai(
            "gpt-5-nano", 0.05, 0.40, cached_input=0.005,
            source=openai_source, effective_date="2025-08-01",
        ),
        _openai(
            "gpt-4o", 2.50, 10.0, cached_input=1.25,
            source=openai_source, effective_date="2024-05-13",
        ),
        _openai(
            "gpt-4o-mini", 0.15, 0.60, cached_input=0.075,
            source=openai_source, effective_date="2024-07-18",
        ),
    ):
        entries[entry.model] = entry
    return entries


def _validate_price_value(value: Any) -> Tuple[Optional[float], Optional[str]]:
    """Return (price, error); a price must be a non-negative finite number.

    Bools are rejected explicitly — YAML ``true`` is not the number 1, and
    silently accepting it would turn a config typo into a wrong price.
    """
    if isinstance(value, bool):
        return None, "boolean is not a price"
    if isinstance(value, (int, float)):
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            return None, "not finite"
        if number < 0:
            return None, "negative"
        return number, None
    return None, f"not a number ({type(value).__name__})"


# Friendly config keys accepted for each token category alongside the
# canonical enum values.
_CATEGORY_KEYS: Dict[str, TokenCategory] = {
    "uncached_input": TokenCategory.UNCACHED_INPUT,
    "input": TokenCategory.UNCACHED_INPUT,
    "output": TokenCategory.OUTPUT,
    "cache_read": TokenCategory.CACHE_READ,
    "cache_creation": TokenCategory.CACHE_CREATION,
    "cache_write": TokenCategory.CACHE_CREATION,
    "cache_creation_5m": TokenCategory.CACHE_CREATION_5M,
    "cache_creation_1h": TokenCategory.CACHE_CREATION_1H,
}


class PricingOverrideError(ValueError):
    """A project pricing override is structurally invalid."""


@dataclass(frozen=True)
class PricingCatalog:
    """Versioned model price table plus validated project overrides."""

    version: str = PRICING_CATALOG_VERSION
    entries: Mapping[str, ModelPrice] = field(
        default_factory=lambda: _builtin_entries()
    )

    @classmethod
    def builtin(cls) -> "PricingCatalog":
        """The built-in versioned price table with no project overrides."""
        return cls(entries=_builtin_entries())

    def get(self, model: Optional[str]) -> Optional[ModelPrice]:
        """Resolve a (possibly aliased) model name to its price entry.

        Returns ``None`` for unknown / unexpandable names — the caller then
        reports unknown cost instead of guessing.
        """
        canonical = canonicalize_pricing_model(model)
        if canonical is None:
            return None
        return self.entries.get(canonical)

    def with_overrides(
        self,
        overrides: Mapping[str, Mapping[str, Any]],
        *,
        source: str = "project",
        warn: Callable[[str], None] = lambda message: None,
    ) -> "PricingCatalog":
        """Validate and merge ``pricing.models`` overrides onto this catalog.

        Each override key is alias-normalized to a canonical model; categories
        must be non-negative finite numbers. Invalid values raise
        :class:`PricingOverrideError`; unknown model keys are reported through
        ``warn`` and skipped (they cannot be silently priced anyway).
        """
        merged = dict(self.entries)
        for raw_model, raw_categories in overrides.items():
            canonical = canonicalize_pricing_model(raw_model)
            if canonical is None:
                warn(f"pricing.models key {raw_model!r} cannot be mapped; ignored")
                continue
            if not isinstance(raw_categories, Mapping):
                raise PricingOverrideError(
                    f"pricing.models.{canonical} must be a mapping of "
                    f"category -> USD per million tokens, got "
                    f"{type(raw_categories).__name__}"
                )
            patch: Dict[str, Optional[float]] = {}
            for raw_key, raw_value in raw_categories.items():
                category = _CATEGORY_KEYS.get(str(raw_key).strip().lower())
                if category is None:
                    warn(
                        f"pricing.models.{canonical}.{raw_key!r} is not a known "
                        f"price category; ignored"
                    )
                    continue
                value, error = _validate_price_value(raw_value)
                if error is not None:
                    raise PricingOverrideError(
                        f"pricing.models.{canonical}.{raw_key}={raw_value!r} "
                        f"is invalid: {error}"
                    )
                patch[category.value] = value
            if not patch:
                continue
            base = merged.get(canonical) or ModelPrice(model=canonical)
            merged[canonical] = replace(
                base,
                **{
                    key: patch.get(key, getattr(base, key))
                    for key in (
                        "uncached_input",
                        "output",
                        "cache_read",
                        "cache_creation",
                        "cache_creation_5m",
                        "cache_creation_1h",
                    )
                },
                catalog_version=self.version,
                source=f"{source} override of {base.source or 'new entry'}".strip(),
                effective_date=base.effective_date,
            )
        return PricingCatalog(version=self.version, entries=merged)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "entries": {m: p.to_dict() for m, p in sorted(self.entries.items())},
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PricingCatalog":
        entries = {
            model: ModelPrice.from_dict(entry)
            for model, entry in (data.get("entries") or {}).items()
            if isinstance(entry, Mapping)
        }
        return cls(
            version=str(data.get("version") or PRICING_CATALOG_VERSION),
            entries=entries,
        )
