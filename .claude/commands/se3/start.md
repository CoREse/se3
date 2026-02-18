---
name: "SE3: Start Session"
description: Initialize an SE3 work session — environment, context, baseline
---

**Usage**: `/se3:start`

Start a new SE3 work session. This skill runs the full startup protocol and guides you through any required setup.

**Steps**

1. **Run `se3 start --format json`** to compute session state
   ```bash
   se3 start --format json
   ```
   Parse the JSON output to get:
   - `first_time`: Whether this is a new project
   - `env_setup`: Whether init.sh needs to run
   - `openspec`: Whether openspec is available/initialized
   - `git`: Branch, uncommitted changes, recent commits
   - `active_changes`: List of active openspec changes
   - `pending_human_calls`: Human responses waiting to be processed
   - `actions`: Array of actions to execute

2. **Execute each action in the `actions` array** in order:

   - `ask_user`: Use AskUserQuestion tool with the provided question
   - `run_script`: Execute the command (e.g., `bash init.sh`)
   - `init_openspec`: Run `openspec init --tools claude`
   - `run_tests`: Run tests to establish baseline
   - `process_human_call`: Read the specified file in `human-calls/` and act on the response
   - `create_progress`: Create `progress.md` file
   - `create_human_calls_dir`: Create `human-calls/` directory

3. **Load relevant specifications** (OpenSpec as source of truth):
   - Run `openspec list --specs` to see available specifications
   - If there are active changes: Read the related spec files in `openspec/specs/<area>/spec.md`
   - These specs are the **single source of truth** for project requirements
   - Agent MUST NOT deviate from spec requirements without explicit human approval

4. **Report session summary** to the user:
   - Current branch and git status
   - Active changes (if any)
   - Relevant specs that will govern the work
   - Pending human calls (if any)
   - What was set up or checked

5. **Transition to work mode**:
   - If there are active changes: Ask if they want to continue one
   - If no active changes: Ask what they want to work on

**Input Classification & Stage Routing (SE3 1.x)**

The `se3 start` command includes input classification to determine workflow routing:

| Intent Type | Description | Stage Entry |
|-------------|-------------|-------------|
| `directive` | Explicit self-iterate, "implement X", "start feature Y" | Full SDD workflow |
| `bug-report` | Error description, stack trace, broken behavior | Bug fix workflow |
| `feature-request` | New capability, enhancement idea | Feature proposal workflow |
| `question` | How does X work? Why Y? | Knowledge query |
| `review` | "Check this", "What do you think", "Is this correct" | Review workflow |
| `clarification` | Follow-up on previous topic | Resume/continue workflow |
| `meta` | About the project/process itself | Meta workflow |
| `off-topic` | Not related to project | Answer without modifying project files |

**Classification Indicators**:
- Bug: "error", "bug", "broken", "fail", "crash", "exception", "stack trace", "not working"
- Review: "review", "check this", "look at", "what do you think", "is this correct"
- Feature: "add ", "implement", "create ", "build ", "support ", "feature", "new capability"
- Question: "how ", "why ", "what is", "explain", "?"
- Directive: "self-iterate", "continue", "proceed", "start ", "fix ", "update ", "refactor "

**Stage Decision Matrix**:

```
Input + Current State → Stage Decision

IF intent == bug-report:
  IF openspec/changes/ has active change AND it relates to bug:
    → Continue active change (add bug fix task)
  ELSE:
    → Create new change: "bugfix/{description}"
    → Stage: Analyze → Fix → Verify

IF intent == feature-request:
  IF complexity == small AND no spec change needed:
    → Direct implementation (Small workflow)
  ELSE:
    → Create new change: "feature/{description}"
    → Stage: Proposal → Specs → Design → Tasks → Code → Verify

IF intent == question:
  IF answer requires code investigation:
    → Quick exploration (no change created)
  ELSE:
    → Direct answer from existing knowledge

IF intent == review:
  → Review workflow: Check → Report → Optional fix

IF intent == clarification:
  → Continue previous context
  OR if new context: treat as new input
```

**MUST NOT**: Handle input outside of SE3 workflow
**MUST**: Create appropriate change record for any code modification

**First-time Bootstrap (SE3 1.x)**

If `progress.md` does not exist and no git history:
1. Ask the human (sync human call): "What should this project do?"
2. Create an openspec change from their response
3. Create `progress.md`
4. Create `human-calls/` directory

**Core Principles (SE3 1.x)**

1. **Human-as-MCP**: All human input obtained on-demand via human calls. No pre-written requirement files.
2. **Progressive Loading**: Start with `progress.md` + `git log`. Load deeper only when the task needs it.
3. **Specs as Truth**: OpenSpec specs are the source of truth for **requirements**. Agents MUST NOT weaken or delete existing requirements without explicit human approval.
4. **Verify Before Done**: Never mark a feature complete without running tests. Spec scenarios are acceptance criteria, not documentation.
5. **Tool-Assisted Enforcement**: Use CLI tools (`se3 lint`, `se3 verify`, `se3 status`) to validate specs, verify coverage, and diagnose issues. Tools make rules enforceable, not just documented.
6. **Incremental Development**: Work in openspec changes. Each session stays within a bounded scope.

**Guardrails**

- If tests fail during `run_tests`, report the failure and pause — do not proceed until fixed
- If `init.sh` fails, diagnose and report before proceeding
- Always complete all actions before starting work
- Do NOT skip the startup protocol — it ensures the environment is ready
- **Specs are authoritative**: OpenSpec specs are the single source of truth — agent MUST NOT modify requirements they are implementing against
- **On-demand spec loading**: Load spec files when work scope is determined, not all upfront (progressive loading)
- **NEVER modify spec files you are implementing against**
