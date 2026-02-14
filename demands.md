# Demands

## D1: Long-Horizon Agent Development Framework

Based on Anthropic's "Effective Harnesses for Long-Running Agents", design a development framework for Claude Code.

### D1.1: Cross-Session Knowledge Transfer
- Progressive context loading: read progress.md + git log to locate state, load other files on demand
- Use git commit messages, progress files, openspec specs as knowledge transfer channels
- Session startup protocol uses progressive loading, not a fixed file checklist

### D1.2: Incremental Development Flow
- Each session focuses on a bounded scope of features/changes
- Code MUST be in a mergeable state at session end
- Use openspec changes to define increment boundaries

### D1.3: Git-Based Checkpointing
- Meaningful git commit after each feature/change completion
- Commit messages include context valuable for the next session
- Support rollback via git history

## D2: SDD Integration with Long-Horizon Agents

### D2.1: OpenSpec as Core Feature Manager
- Specs as single source of truth for project capabilities
- Changes as incremental development units
- Archive mechanism tracks completed change history

### D2.2: Task Decomposition Strategy
- Max 5 logically related tasks per change group
- Task granularity suitable for a single context window
- Clear inter-task dependencies

### D2.3: Verification Loop
- Verify implementation against spec after each change
- Feed verification results back to specs
- Record issues for follow-up changes

## D3: Native Agent Team Support

### D3.1: Multi-Agent Coordination via Task Tool
- Use Claude Code's native Task tool for multi-agent work
- Parent agents spawn sub-agents, each assigned to different openspec changes
- Results return directly through Task tool — no file-based communication layer

### D3.2: Agent Role Differentiation
- Roles (architect, implementer, reviewer) expressed through Task tool prompts
- No separate configuration files for roles
- Single-agent mode as default; multi-agent only when changes can be parallelized

## D4: Human-as-MCP

### D4.1: Unified Human Input Channel
- All human input (including project intent) obtained via human calls on demand
- Sync mode (human present → ask directly) + async mode (human absent → write file)
- Async calls persisted to human-calls/ directory

### D4.2: Non-Blocking Execution
- Human calls MUST NOT block unrelated tasks
- Dependent tasks marked as waiting-human and paused
- Other tasks continue normally
- Paused tasks resume when response arrives

### D4.3: Human Call Scenarios
- Project intent (first-time bootstrap human call)
- Decisions requiring human judgment
- Operations requiring human execution
- Information requiring human domain knowledge

## D5: System Implementation

### D5.1: Implementation Vehicle
- CLAUDE.md as primary vehicle, encoding core protocols and conventions
- Two-tier: global CLAUDE.md (~/.claude/) for universal conventions, project CLAUDE.md for SE 3.0 specifics
- English language for token efficiency and instruction adherence

### D5.2: Configuration
- Optional se3.config.yaml for behavior customization
- All settings have sensible defaults
- No config file needed for basic usage

### D5.3: Deliverables
- Project-level CLAUDE.md template (English)
- Global CLAUDE.md template (English)
- Configuration file template
- Documentation and best practices guide
- This project serves as reference implementation
