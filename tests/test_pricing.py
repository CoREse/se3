"""Tests for the versioned pricing catalog and project price overrides.

Covers the built-in table structure (USD per million tokens across the six
token categories, per-entry provenance), alias normalization, config-level
``pricing.models`` overrides, and the estimate contract: unknown model or a
missing price for a nonzero category is *unknown*, never zero.
"""

from __future__ import annotations

import logging

import pytest

from tianluo.pricing import (
    PRICING_CATALOG_VERSION,
    CostEstimate,
    ModelPrice,
    PricingCatalog,
    PricingOverrideError,
    TokenCategory,
    canonicalize_pricing_model,
    estimate_cost,
)
from tianluo.config import PricingConfig, load_pricing_catalog


# ---------------------------------------------------------------------------
# Built-in table structure
# ---------------------------------------------------------------------------


class TestBuiltinCatalog:
    def test_anthropic_entries_price_all_six_categories(self):
        catalog = PricingCatalog.builtin()
        for model in ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"):
            entry = catalog.get(model)
            assert entry is not None
            for category in TokenCategory:
                assert entry.price_for(category) is not None, (
                    f"{model} lacks a price for {category.value}"
                )

    def test_anthropic_cache_prices_are_input_multipliers(self):
        # Anthropic publishes cache prices as fixed multiples of input.
        entry = PricingCatalog.builtin().get("claude-opus-5")
        assert entry.cache_read == pytest.approx(entry.uncached_input * 0.10)
        assert entry.cache_creation == pytest.approx(
            entry.uncached_input * 1.25
        )
        assert entry.cache_creation_5m == pytest.approx(
            entry.uncached_input * 1.25
        )
        assert entry.cache_creation_1h == pytest.approx(
            entry.uncached_input * 2.0
        )

    def test_openai_entries_leave_cache_creation_unknown(self):
        # OpenAI reports cached input (a subset) but no cache creation classes.
        entry = PricingCatalog.builtin().get("gpt-5")
        assert entry.uncached_input is not None
        assert entry.cache_read is not None
        assert entry.cache_creation is None
        assert entry.cache_creation_5m is None
        assert entry.cache_creation_1h is None

    def test_every_entry_records_version_source_and_date(self):
        catalog = PricingCatalog.builtin()
        for model, entry in catalog.entries.items():
            assert entry.catalog_version == PRICING_CATALOG_VERSION
            assert entry.source, f"{model} has no source"
            assert entry.effective_date, f"{model} has no effective date"

    def test_codex_has_no_per_token_price(self):
        # Codex bills by subscription: no entry, and therefore unknown cost.
        assert PricingCatalog.builtin().get("codex") is None


# ---------------------------------------------------------------------------
# Alias normalization
# ---------------------------------------------------------------------------


class TestModelAliases:
    @pytest.mark.parametrize(
        ("name", "canonical"),
        [
            ("claude-opus-5", "claude-opus-5"),
            ("Opus-5", "claude-opus-5"),
            ("CLAUDE-OPUS-5", "claude-opus-5"),
            ("  claude-sonnet-5 ", "claude-sonnet-5"),
            ("gpt-5-mini", "gpt-5-mini"),
        ],
    )
    def test_alias_normalizes_to_canonical(self, name, canonical):
        assert canonicalize_pricing_model(name) == canonical

    @pytest.mark.parametrize(
        "name",
        [
            "",
            None,
            "$ANTHROPIC_MODEL",
            "${MODEL}",
        ],
    )
    def test_unmappable_names_stay_unknown(self, name):
        assert canonicalize_pricing_model(name) is None

    def test_unknown_model_has_no_builtin_entry(self):
        # A well-formed but uncatalogued name normalizes (so it can be used
        # as an override key) but resolves to no price — unknown cost.
        assert canonicalize_pricing_model("claude-opus-6") == "claude-opus-6"
        assert PricingCatalog.builtin().get("claude-opus-6") is None

    @pytest.mark.parametrize(
        ("dated", "canonical"),
        [
            # Real Claude Code / interactive stream result events report dated
            # snapshot ids; their base model owns the price row.
            ("claude-opus-4-1-20250805", "claude-opus-4-1"),
            ("claude-sonnet-4-5-20250929", "claude-sonnet-4-6"),
            ("claude-opus-4-8-1-20250805", "claude-opus-4-8"),
            ("claude-3-5-sonnet-20241022", "claude-sonnet-4"),
            ("gpt-4o-2024-08-06", "gpt-4o"),
            ("GPT-4O-2024-08-06", "gpt-4o"),
        ],
    )
    def test_dated_snapshot_id_normalizes_to_base_model(self, dated, canonical):
        assert canonicalize_pricing_model(dated) == canonical

    def test_dated_snapshot_id_resolves_to_catalog_price(self):
        # The two dated ids the usage fixtures emit must price against their
        # base rows — not inflate unknown_model_count for a known model.
        catalog = PricingCatalog.builtin()
        opus = catalog.get("claude-opus-4-1-20250805")
        assert opus is not None and opus.model == "claude-opus-4-1"
        assert (opus.uncached_input, opus.output) == (15.0, 75.0)
        sonnet = catalog.get("claude-sonnet-4-5-20250929")
        assert sonnet is not None and sonnet.model == "claude-sonnet-4-6"

    def test_dated_unknown_base_stays_unknown(self):
        # A dated id whose base name the catalog does not own normalizes to
        # the base (usable as an override key) but resolves to no price.
        assert canonicalize_pricing_model("mystery-model-20250805") == "mystery-model"
        assert PricingCatalog.builtin().get("mystery-model-20250805") is None

    def test_pure_date_suffix_is_unmappable(self):
        assert canonicalize_pricing_model("-20250805") is None


# ---------------------------------------------------------------------------
# Estimation contract
# ---------------------------------------------------------------------------


class TestEstimateCost:
    def test_one_million_tokens_costs_the_unit_price(self):
        entry = PricingCatalog.builtin().get("claude-opus-5")
        estimate = estimate_cost(
            entry,
            {
                TokenCategory.UNCACHED_INPUT: 1_000_000,
                TokenCategory.OUTPUT: 1_000_000,
            },
        )
        assert estimate.estimated_cost_usd == pytest.approx(5.0 + 25.0)
        assert estimate.is_estimated

    def test_cached_and_uncached_input_are_priced_separately(self):
        entry = PricingCatalog.builtin().get("claude-opus-5")
        estimate = estimate_cost(
            entry,
            {
                TokenCategory.UNCACHED_INPUT: 1_000_000,
                TokenCategory.CACHE_READ: 1_000_000,
                TokenCategory.CACHE_CREATION_5M: 1_000_000,
            },
        )
        assert estimate.estimated_cost_usd == pytest.approx(5.0 + 0.5 + 6.25)

    def test_no_tokens_is_not_a_zero_cost(self):
        estimate = estimate_cost(
            PricingCatalog.builtin().get("claude-opus-5"), {}
        )
        assert estimate.estimated_cost_usd is None
        assert estimate.reason == "no token usage"

    def test_unknown_model_is_unknown_not_zero(self):
        estimate = estimate_cost(
            None, {TokenCategory.OUTPUT: 100}
        )
        assert estimate.estimated_cost_usd is None
        assert estimate.reason == "unknown model"

    def test_missing_cache_ttl_price_is_unknown_not_partial(self):
        # A model priced for base categories but not 5-minute cache creation:
        # nonzero 5m tokens must not fall back to zero or a partial sum.
        partial_entry = ModelPrice(
            model="partial", uncached_input=1.0, output=2.0, cache_read=0.1
        )
        estimate = estimate_cost(
            partial_entry,
            {
                TokenCategory.UNCACHED_INPUT: 1000,
                TokenCategory.CACHE_CREATION_5M: 500,
            },
        )
        assert estimate.estimated_cost_usd is None
        assert estimate.unknown_categories == (TokenCategory.CACHE_CREATION_5M,)

    def test_zero_token_unknown_category_does_not_block(self):
        partial_entry = ModelPrice(
            model="partial", uncached_input=1.0, output=2.0
        )
        estimate = estimate_cost(
            partial_entry,
            {
                TokenCategory.UNCACHED_INPUT: 1000,
                TokenCategory.CACHE_CREATION_5M: 0,
            },
        )
        assert estimate.estimated_cost_usd == pytest.approx(0.001)

    def test_estimate_never_prices_logical_input(self):
        # Only the mutually exclusive sub-categories carry prices; a caller
        # asking for the logical total must get nothing rather than a guess.
        estimate = estimate_cost(
            PricingCatalog.builtin().get("claude-opus-5"),
            {"logical_input": 100},  # type: ignore[dict-item]
        )
        assert estimate.estimated_cost_usd is None


# ---------------------------------------------------------------------------
# Override validation and merging
# ---------------------------------------------------------------------------


class TestOverrides:
    def test_override_merges_by_canonical_model(self):
        warnings: list[str] = []
        catalog = PricingCatalog.builtin().with_overrides(
            {"Opus-5": {"input": 3.0, "output": 20.0}},
            warn=warnings.append,
        )
        entry = catalog.get("claude-opus-5")
        assert entry.uncached_input == 3.0
        assert entry.output == 20.0
        # Untouched categories keep the built-in values.
        assert entry.cache_read == pytest.approx(0.5)
        assert entry.cache_creation_5m == pytest.approx(6.25)
        assert "override" in entry.source
        assert warnings == []

    def test_override_accepts_friendly_category_keys(self):
        catalog = PricingCatalog.builtin().with_overrides(
            {"claude-opus-5": {"input": 1.0, "cache_write": 2.0}}
        )
        entry = catalog.get("claude-opus-5")
        assert entry.uncached_input == 1.0
        assert entry.cache_creation == 2.0

    @pytest.mark.parametrize("bad", [-1.0, -0.5, True, "cheap"])
    def test_non_negative_validation_rejects_bad_values(self, bad):
        with pytest.raises(PricingOverrideError):
            PricingCatalog.builtin().with_overrides(
                {"gpt-5": {"input": bad}}
            )

    def test_non_mapping_model_entry_raises(self):
        with pytest.raises(PricingOverrideError):
            PricingCatalog.builtin().with_overrides({"gpt-5": 3.0})  # type: ignore[dict-item]

    def test_unknown_model_key_defines_a_new_entry(self):
        # An override key that is neither an alias nor a built-in entry
        # defines a brand-new canonical price row (e.g. a model the built-in
        # table does not know yet); it must not be silently dropped.
        warnings: list[str] = []
        catalog = PricingCatalog.builtin().with_overrides(
            {"mystery-model": {"input": 1.0}}, warn=warnings.append
        )
        entry = catalog.get("mystery-model")
        assert entry is not None and entry.uncached_input == 1.0
        assert warnings == []

    def test_unknown_category_key_warns_and_is_skipped(self):
        warnings: list[str] = []
        PricingCatalog.builtin().with_overrides(
            {"gpt-5": {"tokens": 1.0}}, warn=warnings.append
        )
        assert warnings and "tokens" in warnings[0]

    def test_new_model_entry_created_from_override(self):
        catalog = PricingCatalog.builtin().with_overrides(
            {"claude-opus-6": {"input": 4.0, "output": 16.0}}
        )
        entry = catalog.get("claude-opus-6")
        assert entry is not None
        assert entry.uncached_input == 4.0
        assert entry.output == 16.0
        # Categories the override did not touch stay unknown, not guessed.
        assert entry.cache_read is None

    def test_catalog_dict_round_trip(self):
        catalog = PricingCatalog.builtin()
        restored = PricingCatalog.from_dict(catalog.to_dict())
        assert restored.version == catalog.version
        assert set(restored.entries) == set(catalog.entries)
        assert restored.get("claude-opus-5") == catalog.get("claude-opus-5")


# ---------------------------------------------------------------------------
# Config-level loading (tianluo.yaml pricing: section)
# ---------------------------------------------------------------------------


class TestPricingConfig:
    def _write_yaml(self, tmp_path, content: str):
        config_file = tmp_path / "tianluo.yaml"
        config_file.write_text(content)
        return tmp_path

    def test_load_without_pricing_section_returns_builtin(self, tmp_path):
        self._write_yaml(tmp_path, "workflow: {}\n")
        catalog = load_pricing_catalog(tmp_path)
        assert catalog.get("claude-opus-5").uncached_input == 5.0

    def test_load_project_override(self, tmp_path):
        self._write_yaml(
            tmp_path,
            "pricing:\n"
            "  models:\n"
            "    claude-opus-5:\n"
            "      input: 2.5\n"
            "      output: 12.0\n",
        )
        catalog = load_pricing_catalog(tmp_path)
        entry = catalog.get("claude-opus-5")
        assert entry.uncached_input == 2.5
        assert entry.output == 12.0

    def test_invalid_override_drops_only_that_entry(self, tmp_path, caplog):
        self._write_yaml(
            tmp_path,
            "pricing:\n"
            "  models:\n"
            "    claude-opus-5:\n"
            "      input: -1\n"
            "    claude-haiku-4-5:\n"
            "      input: 0.75\n",
        )
        with caplog.at_level(logging.WARNING):
            catalog = load_pricing_catalog(tmp_path)
        # The bad entry is dropped; the valid sibling override still applies.
        assert catalog.get("claude-opus-5").uncached_input == 5.0
        assert catalog.get("claude-haiku-4-5").uncached_input == 0.75
        assert any("claude-opus-5" in record.message for record in caplog.records)

    def test_non_mapping_section_warns_and_returns_builtin(
        self, tmp_path, caplog
    ):
        self._write_yaml(tmp_path, "pricing: [1, 2]\n")
        with caplog.at_level(logging.WARNING):
            catalog = load_pricing_catalog(tmp_path)
        assert catalog.get("claude-opus-5") is not None
        assert any("pricing" in record.message for record in caplog.records)

    def test_pricing_config_dataclass_empty_default(self):
        config = PricingConfig()
        assert config.overrides == {}
        assert config.build_catalog().version == PRICING_CATALOG_VERSION
