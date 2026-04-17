# History Spec Folding: Code-Fence Awareness and Blank-Line Tolerance

**Date:** 2026-04-17

## Summary

Refined the `se3 history show --detailed` spec folding rules in `se3-commands` so that `### name` headings inside fenced/indented code blocks are not mistakenly folded as spec subsections, and so that specs with unusual blank-line spacing between `### spec-name` and `# Title` are still recognized.

## Changes

### Updated: se3-commands

**Location:** `se3/specs/se3-commands/spec.md` — "Show flow details with LLM call details" scenario

- Added clause: `### name` headings inside fenced code blocks (``` or ~~~) or indented code blocks are NOT treated as spec subsections. Only `### name` headings outside any code context qualify for folding in the known-title and unknown-title fallback paths.
- Added clause: spec recognition via the primary path allows arbitrary blank lines between `### spec-name` and the following `# Title` heading, so malformed or unusually formatted spec blocks are still folded rather than rendered raw.

## Motivation

Before this fix, prompt segments that happened to contain fenced markdown examples with `### some-name` inside them were folded as if they were real specs, producing misleading `[spec] @some-name` annotations. The fallback paths also did not pass strict starts to the subsection folder, so fenced `###` headings could be folded alongside real ones. The tolerance change ensures that formatting variability in embedded spec bodies does not prevent the primary recognition path from engaging.
