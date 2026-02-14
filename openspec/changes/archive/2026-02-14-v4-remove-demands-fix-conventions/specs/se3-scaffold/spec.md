## ADDED Requirements

### Requirement: Self-Iterate Flow
The system SHALL define a self-iterate behavior that drives the project from human intent to working implementation.

Flow:
1. Obtain direction via human call → create openspec change (proposal captures the intent)
2. Implement the change (specs → design → tasks → code)
3. Verify implementation against specs
4. Check if specs fully cover the project goals — if gaps exist, go to 1
5. Update project documentation

#### Scenario: Self-iterate execution
- **WHEN** agent is instructed to self-iterate
- **THEN** agent executes the flow without stopping until step 5, using human calls only when blocked

## MODIFIED Requirements

### Requirement: SE 3.0 Project Structure
The system SHALL define the standard SE 3.0 project file structure.

Standard structure (demands.md removed):
```
project/
├── progress.md            # Cross-session progress tracking
├── se3.config.yaml        # Framework configuration (optional)
├── README.md              # Project documentation
├── human-calls/           # Async human call queue
├── openspec/
│   ├── specs/             # Source of truth for requirements
│   ├── changes/
│   └── archive/
└── .claude/
    └── CLAUDE.md          # SE 3.0 framework (project-level)
```

OpenSpec specs serve as the single source of truth for project requirements. No separate demands/requirements file is needed.

#### Scenario: Project initialization
- **WHEN** SE 3.0 is initialized in a directory
- **THEN** the standard file structure is created without demands.md

### Requirement: Output Artifacts
The system SHALL produce the following deliverables:
1. Project-level CLAUDE.md template (English)
2. Global CLAUDE.md template for ~/.claude/CLAUDE.md (English)
3. Configuration file template
4. Documentation and best practices guide

#### Scenario: Complete delivery
- **WHEN** SE 3.0 framework design is complete
- **THEN** all deliverables are available for direct use in new projects
