## Context

SE 3.0 currently has implicit requirement capture through:
- First-time bootstrap (human call asking "what should this project do?")
- Agent self-discovery during implementation
- Human interruption with new ideas

But there's no explicit structure distinguishing these sources or ensuring consistent handling.

## Goals / Non-Goals

**Goals:**
- Explicit three-source taxonomy for requirement origins
- Source markers in proposals for traceability
- Unified workflow regardless of source
- Optional context preservation for human-initiated requirements

**Non-Goals:**
- Automatic detection of requirement source (still requires agent judgment)
- Forcing all requirements through rigid intake (small changes can still bypass)
- Complex interruption recovery mechanisms (MVP: ask user what to do)

## Decisions

**Decision: Three explicit sources vs. N sources**
Chose exactly three (autonomous, mcp-call, human-initiated) to:
- Cover the main cases without overwhelming granularity
- Map clearly to who "owns" the requirement origin (agent vs. human, requested vs. spontaneous)
- Allow human-initiated to cover both "true interruption" and "natural turn boundary" without distinction

**Decision: Proposal markers vs. separate tracking**
Use `[Source: ...]` markers in proposal rather than separate metadata file:
- Simpler implementation, no new file format
- Visible in standard proposal review
- Searchable with standard tools

**Decision: Context preservation as optional**
Don't require complex stack/ checkpoint mechanisms:
- MVP: Ask user "Resume previous work?" with change-id
- Can add automation later if needed
- Most human-initiated requirements are genuinely new work, not sub-interruptions

## Risks / Trade-offs

**Risk: Source misclassification**
- Agent might misclassify the source (e.g., mark human-initiated as autonomous)
- Mitigation: Review during change archive, correct if wrong

**Risk: Overhead for small changes**
- Adding source marker feels heavy for tiny fixes
- Mitigation: Small changes (bug fixes, tweaks) don't need full intake, can bypass openspec change entirely per adaptive formality

**Risk: Human-initiated ambiguity**
- "Is this really human-initiated or just me (agent) not asking clearly?"
- Mitigation: When in doubt, prefer human-initiated (safer to give credit to human)

## Migration Plan

1. Create spec (this change)
2. Update documentation to reference new intake mechanism
3. Demonstrate with example changes using source markers
4. No breaking changes: existing changes without markers are grandfathered
