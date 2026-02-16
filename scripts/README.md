# Scripts Directory Documentation

## Overview

The `scripts/` directory contains automation and support scripts for the SE 3.0 framework. These scripts enable git-worktree-based multi-agent collaboration and provide essential utilities for the framework's operation.

## Script Classification

### Framework Development Scripts

These scripts are core to the SE 3.0 framework development and are included in the framework's source code:

| Script | Type | Purpose |
|--------|------|---------|
| `collab-orchestrator.sh` | Bash | **Orchestrator**: Manages multi-agent collaboration sessions, task distribution, health monitoring, and state persistence. |
| `collab-manager-launcher.py` | Python | **Manager Launcher**: Starts the manager agent with activity-based monitoring and command fallback. |
| `collab-worker-launcher.py` | Python | **Worker Launcher**: Starts worker agents in isolated git worktrees with monitoring and timeout management. |
| `collab-manager-prompt.py` | Python | **Prompt Generator**: Generates manager agent prompts with rules, project context, and tasks summary. |
| `collab-worker-prompt.py` | Python | **Prompt Generator**: Generates worker agent prompts with rules, task details, and worktree information. |
| `collab-launch-worker.sh` | Bash | **Legacy Launcher**: Old worker launch script (deprecated in favor of collab-worker-launcher.py). |
| `jq-complete.py` | Python | **JSON Parser**: Pure Python jq replacement with basic filter and assignment support for environments without jq. |
| `mock-claude` | Bash | **Testing Tool**: Mock implementation of Claude CLI for testing collaboration functionality without real API calls. |
| `rules-manager.md` | Markdown | **Rules Documentation**: Manager agent behavior rules and decision-making guidelines. |
| `rules-worker.md` | Markdown | **Rules Documentation**: Worker agent behavior rules and implementation guidelines. |

### Script Usage Scenarios

#### Collaboration Orchestrator (`collab-orchestrator.sh`)
- **Primary Use Case**: Starting and managing automatic collaboration sessions
- **Commands**:
  ```bash
  # Start daemonized collaboration
  se3 collab --daemon "Implement feature X"

  # Manual mode (generate tasks, run manually)
  se3 collab --manual "Implement feature X"

  # Check status
  se3 collab --status

  # Abort and cleanup
  se3 collab --abort
  ```
- **Key Features**:
  - Task planning and distribution
  - Worker health monitoring
  - Manager decision routing
  - Worktree creation/removal
  - Session persistence

#### Manager Agent (`collab-manager-launcher.py`)
- **Triggered By**: Orchestrator for planning, review, and failure handling events
- **Responsibilities**:
  - Analyzing project state
  - Creating task definitions
  - Reviewing completed work
  - Making merge/reject/retry decisions
  - Handling escalations
- **Output Format**: JSON decisions following manager decision schema

#### Worker Agent (`collab-worker-launcher.py`)
- **Triggered By**: Orchestrator for pending tasks
- **Responsibilities**:
  - Implementing tasks in isolated worktrees
  - Running tests to verify implementation
  - Committing changes with proper formatting
  - Handling errors and timeouts
- **Exit Codes**:
  - `0`: Success
  - `1`: Failure
  - `2`: Blocked (needs human input)
  - `124`: Timeout/inactivity

#### JQ Replacement (`jq-complete.py`)
- **Fallback for**: Systems without jq installed
- **Features**:
  - Field access: `.field`, `.field.subfield`
  - Array operations: `.field[]`
  - Assignments: `.field = "value"`, `.count += 1`
  - Pipe operations: `.field = "x" | .status = "pending"`
  - Default values: `.field // "default"`

#### Mock Claude (`mock-claude`)
- **Testing Use**: Simulates Claude CLI responses for integration testing
- **Behavior**:
  - Manager mode: Returns predefined JSON decisions based on prompt content
  - Worker mode: Creates test files in `.collab/` directory
- **Usage**:
  ```bash
  MOCK_MODE=true se3 collab --daemon "Test task"
  ```

## Script Categories

### Runtime Dependencies (Required for se3 collab)
- `collab-orchestrator.sh` — Core orchestration
- `collab-manager-launcher.py` — Manager startup
- `collab-worker-launcher.py` — Worker startup
- `collab-manager-prompt.py` — Prompt generation
- `collab-worker-prompt.py` — Prompt generation
- `jq-complete.py` — JSON parsing fallback
- `rules-manager.md` — Manager behavior rules
- `rules-worker.md` — Worker behavior rules

### Optional/Testing Scripts
- `collab-launch-worker.sh` — Legacy worker launcher (deprecated)
- `mock-claude` — Testing tool

## Project Structure Impact

The scripts create and manage the following directories:

| Directory | Purpose |
|-----------|---------|
| `.collab/` | Session state, task definitions, logs, events |
| `.worktrees/` | Isolated git worktrees for each task |
| `human-calls/` | Async human call queue for escalations |

## Documentation Status

- **Well-documented**: `rules-manager.md`, `rules-worker.md`
- **Minimally documented**: Scripts with basic docstrings
- **Undocumented**: Some helper scripts lack detailed comments

## Usage Guidelines

### For Framework Users
1. Use `se3 collab` commands instead of calling scripts directly
2. Scripts are invoked automatically by the `se3` CLI
3. For debugging, check logs in `.collab/logs/`

### For Framework Developers
1. All changes to collaboration scripts must be tested
2. Use `MOCK_MODE=true` for fast integration testing
3. Maintain compatibility with Python 3.6+
4. Follow existing coding style (docstrings, comments)

## Optimization Opportunities

1. **Script Organization**: Group related scripts into subdirectories
2. **Dependency Management**: Add requirements.txt for Python scripts
3. **Documentation**: Add detailed docstrings to all Python scripts
4. **Deprecation**: Remove collab-launch-worker.sh (legacy script)
5. **Testing**: Add unit tests for jq-complete.py and prompt generators

## Version History

### Changes in Recent Versions
- **1.7.7**: Set max-turns to 0 (unlimited) instead of arbitrary limits
- **1.7.6**: Increase manager max-turns from 3 to 10
- **1.7.5**: Use @file syntax for prompt passing to avoid CLI parsing issues
- **1.7.4**: Use stream-json for real-time output (activity-based timeout)
- **1.7.0**: Activity-based timeout for workers, run_with_monitor() API
