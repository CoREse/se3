## MODIFIED Requirements

### Requirement: SE 3.0 Project Structure
The system SHALL define the standard SE 3.0 project file structure.

Standard structure (removed agent-comms/):
```
project/
├── demands.md             # Project requirements (obtained via human calls)
├── progress.md            # Cross-session progress tracking
├── se3.config.yaml        # Framework configuration (optional)
├── README.md              # Project documentation
├── human-calls/           # Async human call queue
├── openspec/
│   ├── specs/
│   ├── changes/
│   └── archive/
└── .claude/
    └── CLAUDE.md          # SE 3.0 framework (project-level)
```

Additionally, the framework SHALL produce a global CLAUDE.md template (`output/CLAUDE.global.md`) for `~/.claude/CLAUDE.md` containing universal conventions applicable to all projects.

#### Scenario: Project initialization
- **WHEN** SE 3.0 is initialized in a directory
- **THEN** the standard file structure is created without agent-comms/ directory

### Requirement: Output Artifacts
The system SHALL produce the following deliverables:
1. Project-level CLAUDE.md template (English)
2. Global CLAUDE.md template for ~/.claude/CLAUDE.md (English)
3. Configuration file template
4. Documentation and best practices guide

#### Scenario: Complete delivery
- **WHEN** SE 3.0 framework design is complete
- **THEN** all deliverables are available for direct use in new projects

