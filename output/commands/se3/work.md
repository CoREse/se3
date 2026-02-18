---
name: "SE3: Work"
description: Start or continue working on a change (feature, bugfix, review, directive)
---

**Usage**:
- `/se3:work` — List active changes or create new one
- `/se3:work <change-name>` — Continue working on specific change
- `/se3:work <description>` — Create and start working on new change

**Steps**

1. **Determine what to work on**:
   - If a change name was provided: `se3 work <name> --format json`
   - If a description was provided: Infer workflow type, then `se3 work --new <type>/<kebab-name> --format json`
   - If nothing provided: `se3 work --format json` to list active changes, then AskUserQuestion

2. **Parse JSON response** to get:
   - `change`: The change name
   - `workflow`: Workflow type (bugfix/feature/review/directive/small)
   - `current_step`: Current workflow step
   - `steps`: All steps with their status
   - `tasks`: List of tasks with done/not-done status
   - `progress`: Task completion statistics
   - `actions`: Array of actions to execute

3. **Execute the workflow loop**:

   For each action in `actions`:
   - `ask_user`: Ask clarifying questions using AskUserQuestion
   - `create_change`: Run `openspec new change <name>`
   - `write_proposal`: Create `proposal.md` in the change directory
   - `write_spec`: Create/update specs in `openspec/specs/` with WHEN/THEN scenarios
   - `write_design`: Create `design.md` (only if complexity warrants it)
   - `write_tasks`: Create `tasks.md` breaking work into max 5 tasks
   - `analyze_bug`: Reproduce, identify root cause, report findings
   - `inspect_code`: Read and review the code/files in question
   - `report_review`: Present findings categorized as critical/warning/suggestion
   - `implement_task`: Implement the specified task, then mark `- [x]` in tasks.md
   - `implement`: Direct code implementation (for small changes)
   - `run_tests`: Run the test suite, report results. If FAIL → pause and fix
   - `run_lint`: Run `se3 lint` to validate specs
   - `verify_scenarios`: Check that all spec scenarios pass
   - `archive_change`: Run `openspec archive <name>` when complete
   - `skip_step`: Skip a step (e.g., design for simple changes)
   - `advance_step`: Trigger workflow step advancement
   - `complete`: All steps done

4. **After completing actions**, re-run `se3 work <name> --format json` for updated state
   - Continue the loop until `complete` or blocked
   - If blocked: Report blocker and suggest next steps

5. **On completion**, show summary and suggest `/se3:done`

**Workflow Types**

| Type | Steps | When Used |
|------|-------|-----------|
| `bugfix` | analyze → fix → verify | Bug reports |
| `feature` | clarify → propose → spec → design → implement → verify | Feature requests |
| `review` | inspect → report → fix | Code review requests |
| `directive` | plan → implement → verify → check_coverage | "Implement X" commands |
| `small` | implement → verify | Simple changes, no openspec needed |

**Adaptive Formality**

The CLI automatically determines formality based on change contents:
- **Large**: Has proposal + specs + design
- **Medium**: Has proposal + specs (no design)
- **Small**: No proposal/specs, ≤3 tasks

**Spec Guardrails (SE3 1.x)**

What Agents MUST NOT Do:
- **MUST NOT delete** an existing spec requirement without explicit human approval (via human call)
- **MUST NOT weaken** a requirement (e.g., changing "SHALL validate all inputs" to "SHOULD validate inputs")
- **MUST NOT modify** the description or scenarios of a requirement they are implementing — the implementer does not get to change the spec they're building against

What Agents CAN Do:
- **ADD** new requirements
- **MODIFY** requirements they are not currently implementing (with a change proposal)
- **Mark requirements as deprecated** with a human-approved reason and migration path

**Enforcement**: After archiving a change, review the git diff of `openspec/specs/` to confirm no requirements were inappropriately weakened or removed. If spec drift is detected, revert and investigate.

**Session Guard (2.1+)**

`se3 work` checks if session is properly started before proceeding:
- If `.claude/.session.json` does not exist → returns error `SESSION_NOT_STARTED`
- If session status is not "active" → returns error `SESSION_NOT_ACTIVE`
- If session file is corrupted → returns error `SESSION_INVALID`

In all cases, the agent should run `se3 start` first.

**Guardrails**

- NEVER modify spec files you are implementing against
- Run tests after each implementation task
- Mark tasks complete (`- [x]`) immediately after finishing
- If a step is unclear, pause and AskUserQuestion
- If implementation reveals design issues, suggest updating artifacts
- Max 5 tasks per group — if more needed, context clear between groups
- Before archiving: verify all spec scenarios pass
