# Discovery Step Markdown Rendering and Confirmation Content Display

**Date:** 2026-04-13

## Summary

Added specification for the discovery step's message display rendering. LLM content fields (`content` and `refined_description`) are now rendered as markdown via `rich.markdown.Markdown` instead of plain text. The confirmation phase now displays the full LLM analysis content alongside the refined description.

## Changes

### 1. Updated: flow-engine

**Location:** `se3/specs/flow-engine/spec.md`

**Discovery Message Display Rendering (under "Discovery Workflow" requirement):**
- Added "Discovery Message Display Rendering" subsection specifying that `_display_discovery_message()` uses `rich.markdown.Markdown` for LLM content fields and `rich.text.Text` for structural UI elements, combined via `rich.console.Group` into a `Panel`
- Added rendering rules table mapping each display mode (confirmation, synthesis+questions, synthesis, question, general) to its rendering strategy
- Added specification that the confirmation phase displays the full LLM `content` field (analysis, reasoning, summaries) before the `refined_description`
- Added scenario: "Discovery message renders LLM content as markdown"
- Added scenario: "Confirmation phase shows full LLM analysis content"
