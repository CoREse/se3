## MODIFIED Requirements

### Requirement: SE 3.0 Init Skill
The system SHALL NOT provide a separate init skill. Project initialization is handled by the CLAUDE.md startup protocol (detect empty project → human call → create demands.md + progress.md).

#### Scenario: Project initialization without skill
- **WHEN** agent enters an empty project with SE 3.0 CLAUDE.md configured
- **THEN** the startup protocol handles initialization directly, no separate skill needed
