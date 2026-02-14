# Requirement Capture Mechanism

## Why

Currently, SE 3.0 lacks a structured way to handle new requirements that emerge from different sources. Whether Claude discovers a missing capability, asks a human for input, or a human spontaneously proposes a new feature, these all need to flow through a unified process to ensure:
- No requirements are lost
- Clear traceability of where requirements came from
- Consistent handling regardless of origin

## What Changes

1. **Requirement Intake Spec**: Define three-source intake mechanism (autonomous, mcp-call, human-initiated)
2. **Source Markers**: Proposal markers `[Source: ...]` for traceability
3. **Context Preservation**: Handle human-initiated requirements with optional interruption context
4. **Unified Workflow**: All three sources flow into same openspec change process

## Capabilities

### New Capabilities
- `requirement-intake`: Structured three-source requirement capture mechanism

### Modified Capabilities
- None (this is purely additive to existing SDD workflow)

## Impact

- New spec in `openspec/specs/requirement-intake/`
- Updates to session protocol documentation to reference new intake mechanism
- No breaking changes to existing changes or specs
