# tianluo Configuration Reference

This is the **full, authoritative reference** for every configuration block
tianluo reads. It is written against `src/tianluo/config.py` — every key below
corresponds to a real dataclass field, and every default is the value of the
corresponding `DEFAULT_*` constant (or the dataclass default) in that module.

`tianluo.example.yaml` is a *getting-started sample*, not a schema: it shows a
small, opinionated subset and it also carries a few historical blocks the engine
no longer reads. When the two disagree, this document (and the code it mirrors)
wins.

## Table of Contents

1. [Configuration files and resolution rules](#configuration-files-and-resolution-rules)
   - [Which file is read: the four-tier lookup](#which-file-is-read-the-four-tier-lookup)
   - [Pitfall 1 — `tianluo.local.yaml` replaces the whole file](#pitfall-1--tianluolocalyaml-replaces-the-whole-file)
   - [The global layer (`~/.se3/config.yaml`)](#the-global-layer-se3configyaml)
   - [Legacy `se3.yaml` / `se3.local.yaml`](#legacy-se3yaml--se3localyaml)
   - [How to read this document](#how-to-read-this-document)
2. [Blocks](#blocks)
   - [`agents`](#agents)
   - [`llm_caller`](#llm_caller)
   - [`confirmation`](#confirmation)
   - [`language`](#language)
   - [`workflow`](#workflow)
     - [PLAN decomposition doctrine and granularity](#plan-decomposition-doctrine-and-granularity)
     - [Retired: `workflow.implementation_strategy`](#retired-workflowimplementation_strategy)
     - [Self-check review scope (full-incremental-closure)](#self-check-review-scope-full-incremental-closure)
   - [`pricing`](#pricing)
     - [Usage and cost visibility in history and the web console](#usage-and-cost-visibility-in-history-and-the-web-console)
   - [`investigation`](#investigation)
   - [`test`](#test)
   - [`e2e`](#e2e)
     - [Prerequisite: a container runtime you can run without sudo](#prerequisite-a-container-runtime-you-can-run-without-sudo)
     - [Runtime selection](#runtime-selection)
     - [The other half: `tianluo/e2e/` content configuration](#the-other-half-tianluoe2e-content-configuration)
     - [Where the E2E step sits, and how failures route](#where-the-e2e-step-sits-and-how-failures-route)
   - [`implement`](#implement)
   - [`steps`](#steps)
   - [`version`](#version)
   - [`documentation`](#documentation)
   - [`code_index`](#code_index)
   - [`merge`](#merge)
   - [`conflict_resolver`](#conflict_resolver)
   - [`claude_subprocess`](#claude_subprocess)
   - [`server`](#server)
   - [`presets`](#presets)
3. [Legacy / historical configuration](#legacy--historical-configuration)
   - [Blocks the engine no longer reads](#blocks-the-engine-no-longer-reads)
4. [Troubleshooting: "I changed the config and nothing happened"](#troubleshooting-i-changed-the-config-and-nothing-happened)

---

## Configuration files and resolution rules

### Which file is read: the four-tier lookup

Project configuration lives in a single YAML file at the project root. Exactly
**one** file is selected — `get_project_config_path()` probes candidates in
order and stops at the first one that is a regular file:

| # | Candidate | Notes |
|---|-----------|-------|
| 1 | `<worktree>/tianluo.local.yaml` | Highest priority. Gitignored local override. |
| 2 | `<main_repo>/tianluo.local.yaml` | Only probed when the project root is a linked git worktree. |
| 3 | `<worktree>/tianluo.yaml` | The committed project config. |
| 4 | `<main_repo>/tianluo.yaml` | Lowest priority. |

Tiers 2 and 4 exist only for **git worktrees**. `luo run --worktree` executes in
a linked worktree, and a gitignored `tianluo.local.yaml` does not travel there —
so the main repository's local override is consulted before the worktree's
committed `tianluo.yaml`. For a plain (non-worktree) checkout the lookup
collapses to the classic two tiers: `tianluo.local.yaml` > `tianluo.yaml`.

If none exists, `<project_root>/tianluo.yaml` is returned as the notional
target, and every loader falls back to built-in defaults.

Two details worth knowing:

- The probe uses `is_file()`, which **follows symlinks**. A layout such as
  `tianluo.local.yaml -> ../shared-overrides.yaml` is picked up as the active
  override (intentional — it is how users share overrides between clones). A
  stray *directory* or dangling symlink at that path is not a regular file and
  therefore does not shadow `tianluo.yaml`.
- Relative paths inside the selected config (e.g. `version.version_file`,
  `test.command`, `test.phases[].cwd`) are resolved by downstream callers
  against the **project root of the running process**, not against the directory
  containing the config file. A relative path written into a main-repo
  `tianluo.local.yaml` is interpreted relative to the worktree that reads it.

### Pitfall 1 — `tianluo.local.yaml` replaces the whole file

**The local override is whole-file select-one, not per-key merge.** Once
`tianluo.local.yaml` exists, `tianluo.yaml` is *never opened*. Anything defined
only in `tianluo.yaml` silently reverts to its built-in default.

```yaml
# tianluo.yaml (committed)
workflow:
  max_fix_iterations: 100
agents:
  primary: { type: claude-code, cmd: claude }
llm_caller:
  defaults: [primary]
```

```yaml
# tianluo.local.yaml — "I just want to tweak one knob"
workflow:
  self_check_passes_required: 2
```

Result: `max_fix_iterations` falls back to the default `100` (coincidentally the
same here), **and the project-level `agents` registry and `llm_caller.defaults`
are gone** — the project side of those blocks is now empty. What the run
actually uses then depends on the global layer, in this order: the `agents`
entries and `llm_caller.defaults` declared in `~/.se3/config.yaml`, then a chain
implied by a legacy `claude_commands:` block, and only if none of those supply a
chain does it reach the built-in PATH-probed one. So on a machine whose
`~/.se3/config.yaml` configures agents, the run silently switches from the
project's chain to the *global* chain — not to the built-in probe. To keep a
value, repeat it in the local file. This is why `tianluo.example.yaml`
annotates `workflow.max_fix_iterations` with "intentionally identical to
`tianluo.local.yaml` so the local override does not silently shadow a different
value".

Rule of thumb: **`tianluo.local.yaml` is a full replacement config, not a
patch.** Start it as a copy of `tianluo.yaml` and edit from there.

A related failure mode: a `tianluo.local.yaml` that is *unparsable* (YAML syntax
error, or a top level that is not a mapping) is treated as empty — it still
shadows `tianluo.yaml`, and every loader falls back to built-in defaults. The
loader emits a one-shot warning naming the offending file when this happens; if
your project config "stopped working", check the log for it.

### The global layer (`~/.se3/config.yaml`)

A per-user global config is read from `~/.se3/config.yaml`. The path comes from
`_GLOBAL_CONFIG_PATH_SUFFIX = (".se3", "config.yaml")` and did **not** change
with the se3 → tianluo rename — the directory is still `~/.se3/`, alongside
`~/.se3/server.db`, `~/.se3/daemon.pid`, and the rest of the per-user runtime
state. It is a normal YAML mapping using the same block names as the project
file.

**Only some blocks participate in a project↔global merge.** The rest are read
from the project file only:

| Block | Global layer? | Merge granularity |
|-------|---------------|-------------------|
| `agents` | Yes | **Entry-level.** Project entries override global entries with the same name; non-conflicting entries from both sides coexist. |
| `llm_caller.defaults` | Yes | **Whole-list.** Project `defaults` wholly replaces global `defaults`; global is used only when the project omits the key. |
| `llm_caller.steps.<step>` | Yes | **Per-step whole-value.** A step declared in the project replaces the global declaration *for that step*; steps declared only globally still apply. |
| `confirmation.steps` | Yes | **Entry-level**, same rule as `agents`. |
| `language` | Yes | **Field-level.** Each of `language` / `spec_language` independently takes the project value when set, otherwise the global one. |
| `server` | Yes | **Whole-block.** A project `server:` section wholly replaces the global one (no deep merge). |
| everything else | No | Project file only (`workflow`, `test`, `implement`, `steps`, `version`, `documentation`, `code_index`, `merge`, `conflict_resolver`, `claude_subprocess`, `investigation`, `presets`, `pricing`, …). |

### Legacy `se3.yaml` / `se3.local.yaml`

The pre-rename filenames are still honoured at **every** tier of the lookup, with
the canonical `tianluo.*` name always winning over the `se3.*` one at the same
tier. The effective order is therefore:

```
worktree/tianluo.local.yaml → worktree/se3.local.yaml
  → main/tianluo.local.yaml → main/se3.local.yaml
  → worktree/tianluo.yaml   → worktree/se3.yaml
  → main/tianluo.yaml       → main/se3.yaml
```

The `se3.*` fallback keeps working through 12.x and is **removed in 13.0.0**.
The ancient `se3.config.yaml` is recognised only as a project-root *marker* (for
the CLI's parent-directory walk), never loaded as config.

### How to read this document

- **Default** columns quote the literal value the code uses. Where the code
  names a constant, the constant is the source of truth
  (`DEFAULT_MAX_FIX_ITERATIONS = 100`, `DEFAULT_CODE_INDEX_CHUNK_BYTES = 16 * 1024`,
  …); the numbers here are those constants' current values.
- **Type** is the YAML type accepted, not the Python annotation.
- Unless a key's entry says otherwise, loading is **clamp-and-warn**: an illegal
  value logs a warning and falls back to the default rather than aborting the
  run. The exceptions that *fail fast* are called out explicitly
  (`ConfigError` / `ValueError`), because they abort the flow at load time.
- A key documented as *inert* is parsed by `config.py` but has no consumer in
  the engine. It is listed for completeness (so you do not go hunting for a
  behaviour it never had), not as something worth setting.

---

## Blocks

### `agents`

The top-level identity layer. Every `llm_caller.*` entry and every
`confirmation.steps.<step>.reviewer` is a **name reference resolved against this
registry**. It is a mapping keyed by agent name.

```yaml
agents:
  primary: { type: claude-code, cmd: claude }
  cheap:   { type: claude-code, cmd: hclaude }
  gpt:     { type: codex,       cmd: codex }
  tty:     { type: claude-interactive, cmd: claude }
```

Each entry corresponds to an `AgentDef`:

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| *(map key)* | string | — | The agent's `name`. Must be a non-empty string; other keys are skipped with a warning. |
| `type` | string | `claude-code` | Which `AgentRunner` adapter drives this agent. See the table below. |
| `cmd` | string | — | The CLI command to invoke. **Required** — an entry with no usable `cmd` is skipped with a warning. |
| `provider` | string | `""` | Declared LLM provider for this agent (e.g. `anthropic`, `openai`). Used as the **declared fallback** for usage accounting when the runner's stream reports no provider; it never overrides a provider-reported value. |
| `model` | string | `""` | Declared model for this agent. After environment-variable expansion it feeds the canonical `resolved_model` fallback chain (provider report → expanded agent model → runner startup metadata → `unknown`); a literal like `$ANTHROPIC_MODEL` never enters `resolved_model` — the raw value stays in `reported_model` for diagnosis. |
| `priority` | int | `0` | **Deprecated and ignored.** Rotation order is the *written order* of the name lists in `llm_caller`, not this number. Setting it emits a one-shot deprecation warning per source. |

A shorthand form is accepted: `primary: claude` is equivalent to
`primary: { type: claude-code, cmd: claude, priority: 0 }`.

Valid `type` values (dispatched in `LLMCaller`; an unrecognised type raises
`ValueError: Unknown agent type: …` at the first call, not at config load):

| `type` | Adapter | Notes |
|--------|---------|-------|
| `claude-code` | `claude_runner.ClaudeRunner` | One-shot `claude -p` subprocess. The default. |
| `codex` | `codex_runner` | OpenAI Codex CLI. |
| `claude-interactive` | `claude_interactive_runner` | pexpect-driven interactive PTY session. Opt-in only — it needs a terminal and `pexpect`, so it is never auto-selected. |

**Unknown *names* fail fast.** Referencing an agent that is not in the merged
registry raises a `ValueError` at config-load time (listing the registered
names), so a typo in `llm_caller.defaults` or a reviewer name aborts the run
before any LLM call is made.

**Project + global merge is entry-level**: `~/.se3/config.yaml` can hold your
machine-wide agent inventory and a project only needs to add or override the
entries it cares about.

**When `agents` is omitted entirely** the default chain is probed from `PATH`,
in this order: `claude` (type `claude-code`), then `codex` (type `codex`). All
available candidates form the chain; if none is on `PATH`, loading raises
`ValueError`. `claude-interactive` is deliberately excluded from this
auto-probe.

The removed list form (`agents: [ … ]`) and the legacy `claude_commands:` key
are still detected: the list form is warned about and ignored wholesale;
`claude_commands` is auto-migrated into a synthesized registry plus an implicit
`llm_caller.defaults` (with a one-shot warning that prints the equivalent
new-schema YAML). Setting both `agents` and `claude_commands` in the same source
ignores `claude_commands`.

### `llm_caller`

Which agent chain runs which step.

```yaml
llm_caller:
  defaults: [primary, cheap]
  steps:
    implement: [primary]
    self_check: [[primary], [gpt]]
```

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `defaults` | list of agent names | built-in PATH probe | The default rotation chain for any step without an override. Written order **is** rotation order. |
| `steps` | mapping `<step> → list of agent names` | `{}` | Per-step hard overrides. Keys must be `StepType` values; unknown keys log a one-shot "likely a typo" warning and are ignored. |

Rotation semantics: within a chain, the `LLMCaller` rotates to the next agent on
*infrastructure* errors. Rotation happens **strictly inside the selected list** —
a single runner never rotates on its own, and a step override never spills over
into `defaults`.

#### Pitfall 2 — `llm_caller.steps.<step>` is a hard override with no fallback

Declaring a step here **replaces** the chain for that step. There is no implicit
append of, or fallback to, `defaults`. If you want the default agents as a tail,
you must list them.

```yaml
llm_caller:
  defaults: [primary, cheap, backup]

  steps:
    # WRONG if you meant "try opus first, then the usual chain".
    # implement now has exactly ONE agent; on infra failure there is
    # nothing to rotate to.
    implement: [opus]

    # RIGHT — the default tail is spelled out.
    plan: [opus, primary, cheap, backup]
```

Precedence between sources is also whole-value: if a step is declared in the
project config, the global `~/.se3/config.yaml` declaration for that same step is
ignored entirely (no concatenation, no dedup).

Degenerate declarations are treated as *no override* (warn, then fall back to
`defaults`): a non-list value, an empty list, a list whose entries are all
invalid, and the removed inline-dict form (`- cmd: claude-opus`). Only an
unknown agent *name* is fatal.

#### `self_check`: nested per-pass chains

`llm_caller.steps.self_check` additionally accepts a **list of lists** — one
chain per self_check pass:

```yaml
llm_caller:
  steps:
    self_check:
      - [primary]        # pass 1
      - [gpt]            # pass 2 — a different vendor reviews the same diff
      - [primary, cheap] # pass 3
```

- **Flat form** (`self_check: [a, b]`) — one chain reused for every pass. Fully
  back-compatible.
- **Nested form** — chain *i* drives pass *i* (1-based). Passes beyond the
  number of declared chains reuse the **last** chain.
- **Mixed form** (bare names *and* sub-lists in the same list) is a config error:
  it warns and falls back to `llm_caller.defaults`.

The nested form also determines the pass count. When
`workflow.self_check_passes_required` is *not* set explicitly, the effective
number of passes is the number of declared chains — the chain list alone
expresses the intent. When both are set, the explicit count wins (and
over/under-shooting reuses or skips chains as described above).

### `confirmation`

Which steps are gated by a review before the flow moves on.

```yaml
confirmation:
  steps:
    plan:
      max_iterations: 3
    adjudicate:
      reviewer: human
```

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `steps` | mapping `<step> → entry` | `{}` | A step is confirmed **iff** it appears as a key here. No step is exempt from this rule. |
| `steps.<step>.reviewer` | string or null | `null` | `human` → routes to the `tianluo/calls/` MCP call file + interactive approval. An **agent name** → single-agent LLM review with that agent. Omitted/`null` → LLM review via `llm_caller.defaults`. |
| `steps.<step>.max_iterations` | positive int | `3` (`_CONFIRM_DEFAULT_MAX_ITERATIONS`) | Cap on the review→modify→re-review cycle. Non-integer or `<= 0` warns and falls back to the default. |

There is **no global on/off switch**. Only the keys present in
`confirmation.steps` are confirmed. Unknown fields inside a step entry (anything
other than `reviewer` / `max_iterations`) are warned about once and ignored.

An unknown `reviewer` agent name raises `ValueError` at load time — and the check
walks **every** entry, not just the step about to run, so typos under a step that
happens not to be in this flow's sequence still surface at startup.

**plan-confirm is opt-in, like every other step.** It used to be always-on —
a `CONFIRM` step was inserted after every `plan` step regardless of this block.
It no longer is: with no `confirmation.steps.plan` entry, PLAN is followed by
no gate at all. The reason for the downgrade is that `self_check` is already
task-description-authoritative and verifies requirement→code coverage against
the real code, while under the default coarse-grained
[`capability` doctrine](#plan-decomposition-doctrine-and-granularity) a
requirement→task coverage review has lost its discriminating power — a group
that says "implement the export capability" cannot be checked line-by-line
against a requirement list.

To keep a gate, declare it like any other step:

```yaml
confirmation:
  steps:
    plan:
      reviewer: human      # manual grouping gate
```

What the review asks depends on the doctrine the flow was planned under:

- **`capability`** — reviews the *grouping*: whether the group count equals the
  number of independent tasks (and whether every further split names a
  capability-edge reason that holds), whether any group was cut along a
  forbidden artifact type or code layer, and whether the `depends_on`
  declarations hold. It explicitly does not ask for a per-requirement task
  decomposition.
- **`granular`** — keeps the legacy requirement→task coverage review verbatim.

The doctrine is read from the persisted flow context, so a resumed flow is
reviewed under the doctrine it was planned with. The granularity travels with
it, because it pins the group *count*: under `single` the count is a configured
guarantee PLAN may not deviate from, so the review puts it out of scope
entirely; under `conservative` the deliberate over-splitting that setting orders
is not a defect. Only under `auto` is the count PLAN's own judgement, and only
there may the gate ask for it to be regrouped — otherwise the reviewer would
demand a revision PLAN is forbidden to produce, and the loop would just spin to
`max_iterations`.

By contrast `adjudicate` is **unconfirmed by default**: with no entry here a
ruling auto-passes with no gate — including a ruling that rewrites the *task
description*. If an unattended run must not silently rewrite its own task, opt
in with `adjudicate: { reviewer: human }`.

Deprecated keys, detected and ignored with a one-shot warning per source:
`confirmation.enabled`, top-level `confirmation.reviewer`,
`confirmation.llm_reviewer`, and the list form of `confirmation.steps`.

### `language`

Two independent language settings, merged **field by field** with
`~/.se3/config.yaml`.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `language` | string or null | `null` | The unified **human language**. Drives both the CLI/console UI copy (via the i18n catalogs) and the language injected into human-facing LLM step outputs (summarize / discovery / confirmed steps). e.g. `zh-CN`, `en-US`. `null` = no restriction on LLM output (UI copy still resolves through the chain below). |
| `spec_language` | string or null | `null` | The **knowledge-asset language** — the writing language of `tianluo/charter.md` and the code-index, injected into the `charter_freshness` and code-index summary prompts. `null` = no restriction. |

CLI UI-text resolution order: `SE3_LANG` env > this key (project, then
global) > system locale (`LC_ALL` / `LC_MESSAGES` / `LANG`) > `en-US`. `en-US`
is the baseline catalog holding the full key set; a missing key or unsupported
language code falls back to it.

Changing either setting affects only content generated *after* the change; it
does not retroactively translate existing knowledge assets.

The central WebUI console's interface language is a per-user browser /
localStorage preference and does **not** follow this project setting.

### `workflow`

Fix-loop and self_check behaviour. Loaded into `WorkflowConfig`.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `max_fix_iterations` | int `>= 0` | `100` | Cap on the test→verify→fix loop. **`0` (or `null`) means unlimited.** Negative → `ConfigError`. A float (even `0.0`) or bool warns and falls back to `100` — the unlimited sentinel must be the literal int `0` or `null`. |
| `self_check_passes_required` | int `>= 1` | `1` | How many self_check passes must run. `< 1` → `ConfigError`. bool/float/non-integer warns and falls back to `1`. See the nested-chain interaction in [`llm_caller`](#self_check-nested-per-pass-chains). |
| `baseline_fix_max_attempts` | int `>= 0` | `3` | Per-flow cap on looping *inherited* (pre-implement baseline) test failures. Deliberately independent of `max_fix_iterations`, which may be the unlimited sentinel — inherited failures must stay bounded on their own. **`0` disables baseline looping entirely** (inherited failures are surfaced, never looped). Negative → `ConfigError`. |
| `self_check_defer_fix_threshold` | int `>= 0` | `0` | For nested self_check chains: when a non-final pass finds *fewer* than this many issues and none is critical/high severity, its fix is deferred so the remaining passes run first; their findings are then deduped into one consolidated fix loop. **`0` (or `null`) disables deferral** — every issue-finding pass fixes immediately (the historical behaviour). Negative → `ConfigError`. |
| `adjudicate_period` | int `>= 0` | `10` | Period, in fix iterations, of the adjudicate step's catch-all safety net: every N fix iterations one adjudicate run is forced even when no structural oscillation signal fired. **`0` (or `null`) disables the periodic net** (adjudicate then runs only on the structural triggers: candidate oscillation / contradiction / recurrence). Unlike its siblings this key is **fail-fast on a bad type**: a bool, a float, or a non-numeric string raises `ConfigError` rather than defaulting, because silently defaulting would enable an interval the user never asked for. A cleanly integer-valued string (`"7"`) still coerces. Negative → `ConfigError`. |
| `plan_decomposition` | `capability` \| `granular` | `capability` | Which decomposition doctrine PLAN follows (see below). **Fail-fast on any other value** — a typo would otherwise silently pick a different doctrine, invisible until the groups come out the wrong size. |
| `plan_granularity` | `auto` \| `single` \| `conservative` | `auto` | Group-count pressure applied **under `capability` only**; meaningless and ignored under `granular`. `auto` = group count equals the number of independent tasks, more groups only at the capability edge; `single` = pin exactly one group; `conservative` = lower the splitting threshold, err toward more groups. **Fail-fast on any other value.** |

The retired `workflow.self_check_convergence_enabled` key is accepted only for
configuration compatibility. It is always normalized to `false`, and the
deprecation warning is emitted **once per process** (a long-lived daemon /
server that reloads the config repeatedly logs it only on the first load);
validated findings always enter the fix loop.

#### PLAN decomposition doctrine and granularity

**There is one path, not two.** `feature`, `bugfix` and discovery flows always
run ANALYZE → PLAN → IMPLEMENT; no configuration key removes PLAN from a
sequence any more. What varies is *what PLAN emits* and *how many groups* it
emits — and, wherever the granularity left the group count to PLAN, the
execution shape of the PLAN → IMPLEMENT segment is read off that count, not
off a routing flag:

- **one group** → IMPLEMENT runs the whole task as a single autonomous
  implement call (what the retired `direct` path used to select explicitly);
- **two or more groups** → the existing DAG scheduling applies: groups with no
  dependency between them run in parallel in isolated worktrees and are merged
  back, groups with declared dependencies run in order.

The one exception is `plan_granularity: single` (described below), which pins
the one-call shape as a configured guarantee: however many groups PLAN emits,
the whole task is delivered by a single autonomous call.

Task type and decomposition remain different layers: the task type
(`feature` / `bugfix` / `small` / `review` / `survey` / discovery) decides the
whole flow's step composition and quality gates, while these two keys only
shape the PLAN → IMPLEMENT segment. Sequences that never had a PLAN step
(`small`, `review`, `survey`) are unaffected.

**`plan_decomposition: capability`** (default) — PLAN cuts the work into coarse
groups whose unit is the **task**: one coherent piece of work the user would
regard as a single thing. The *only* reason to split a task any further is
"can a single autonomous implement call safely carry this?":

1. one task that one call can complete → **one group** (the group's content is
   simply "carry out that task");
2. one task one call cannot carry → **two or more groups**;
3. two or more mutually unrelated, independent tasks → **one group each**, so
   they can be executed in parallel in isolated worktrees. Aspects of one
   coherent task are not independent tasks and stay together in its group;
4. default to aggregation — **one task, one group**. Split a task only at the
   *capability edge*: PLAN positively judges that a single autonomous
   implement call cannot complete it, or that forcing it into one call would
   substantially degrade the quality of the execution — and records that
   reason in the group's description. PLAN never pre-cuts a single task along
   implementation phases, implementation paths, or artifact types: how a task
   breaks down internally is decided inside the implement call, not by PLAN.

A hard guardrail forbids cutting groups along artifact types or code layers:
no separate test group, docs group or config group, and no group defined by a
file set, module boundary or layer. Testing and verification are part of what
each group itself delivers. (A group whose *task* happens to concern the test
or docs system — "fix the flaky retry in the test runner" — is of course
legitimate; what is forbidden is carving one task's tests out into a group of
their own.)

Under `capability`, PLAN emits **no per-task listing**. A group carries only
the scheduling fields `group_id` / `name` / `description` / `group_order` /
`depends_on`; the in-group breakdown is left to the implement runner's own
planning / sub-agent system, which decomposes against the real code at
execution time. Both runners support headless subagents — `claude -p` exposes
the Agent (subagent) tool, and codex ships subagents enabled by default under
non-interactive `codex exec` — but codex only spawns them when explicitly
instructed, so the implement prompts state that permission explicitly. PLAN
produces no proposal and no design document either: its whole output is the
scheduling data (the groups plus `total_complexity` / `estimated_effort`), and
that is what a human gate reviews. Project conventions reach the implement call
through the charter and the code-index instead.

**`plan_granularity`** applies only under `capability`:

- **`auto`** (default) — the group count is the number of mutually independent
  tasks in the requirement, normally one group per task. Groups beyond that
  count are added only where a single task reaches the capability edge and
  genuinely needs more than one autonomous implement call.
- **`single`** — exactly one group, whatever the task's size. This is the
  configuration-forced equivalent of the retired `direct` path, and it is a
  guarantee rather than a request: PLAN is instructed to emit one group, but if
  it emits several anyway, IMPLEMENT still runs the whole task through the one
  autonomous call and passes the groups in only as an outline. Under `single`
  a flow therefore never fans out into a parallel worktree DAG.
- **`conservative`** — lower the splitting threshold: whenever there is *any*
  doubt that one call can carry a task, split it. Prefers one group per
  sub-task even where the default sizing would have kept the task whole, and
  errs toward more groups, on the reasoning that an under-sized group costs
  little while a group that overflows the call it was sized for costs a failed
  implementation.

**`plan_decomposition: granular`** is the retained legacy doctrine: the
fine-grained per-task listing (`tasks` arrays with `complexity` /
`estimated_loc` / `files`), the LOC-driven merge and DAG thresholds, and the
requirement→task coverage review at the plan gate — all unchanged from before
this model existed. `plan_granularity` is ignored in this mode.

Priority for a new flow: explicit CLI (`--plan-decomposition` /
`--plan-granularity`) / web request → the two config keys → `capability` +
`auto`. **The decision is made once, at flow creation, and persisted** in the
flow context together with its reason; a resumed flow keeps executing the
doctrine it already entered, whatever the configuration says later, and the
groups PLAN already emitted are never re-judged mid-flow. Flows created before
this model existed carry no new keys: they are *described* read-only by the
retired strategy value they recorded, and their on-disk state is never
rewritten.

#### Retired: `workflow.implementation_strategy`

The `implementation_strategy` routing axis (`auto` / `direct` / `planned`) is
retired. Both `workflow.implementation_strategy` and the
`--implementation-strategy` CLI option are still accepted for **one version**
and mapped onto the new keys, with a deprecation warning emitted once per
process. **They will be removed in the next major version.**

| Legacy value | Maps to | Because it meant |
|--------------|---------|------------------|
| `direct` | `plan_granularity: single` | "do the whole thing in one call" = force a single group |
| `planned` | `plan_decomposition: granular` | "I want fine-grained task groups + DAG scheduling" = keep the legacy doctrine |
| `auto` | the new defaults (`capability` + `auto`) | "let the flow decide" |

The mapping happens at the config-object boundary (and, for the CLI, through
the same table), so nothing downstream carries two dialects. A new key you
actually wrote always wins: the legacy key is a fallback for un-migrated
projects, never an override. An out-of-range legacy value is still fail-fast.

#### Self-check review scope (full-incremental-closure)

SELF_CHECK is judged against the **effective task description** (the original
task or the discovery refined description, user interjections, and the
adjudicated description) plus the charter and `WHY:`/`INVARIANT:` project
constraints. PLAN, `task_groups`, the implementation summary and the legacy
`adjudicated_plan` are scheduling / history hints only — they can never
narrow, widen or override the requirements.

Rounds are scoped by a persisted, recoverable **baseline** (stored in runtime
state, never in git; the workspace diff is rebuilt from it).

**Both round modes are diff-scoped.** `full` and `incremental` are the
persisted `scope_mode` values, and they name the *diff baseline* — not the
presence or absence of a diff. A `full` round is not an unscoped read of the
whole tree: it reviews the diff from this flow's **implementation baseline**
(everything the flow changed). An `incremental` round reviews the diff from the
latest **fix baseline** (that fix's own delta). Every round prompt carries a
scope manifest — the changed paths, their `+`/`-` counts and the exact line
ranges a citation may reference — and the full diff text, either inlined or
pulled on demand with the read-only `luo review-scope diff` command. Rebuilding
the range by hand with `git diff` is wrong and the prompt forbids it: a
baseline is a workspace *content* snapshot, not a commit, and HEAD advances
inside a flow.

1. Before the first IMPLEMENT, an **implementation baseline** is captured so
   the first review covers everything this flow changed and excludes the
   user's pre-flow working-tree modifications. Commits the flow makes itself
   (for example the per-group merges of a planned multi-group IMPLEMENT) stay
   inside the scope — the baseline is content-keyed, so it survives HEAD
   moving forward; only history that no longer contains the baseline commit
   (rebase, amend, a backwards reset) is undecidable. The first SELF_CHECK
   round is `full` — the complete requirements against the diff from that
   implementation baseline. A clean full round proceeds directly to the next
   gate.
2. Before every FIX, a **fix baseline** is captured; the post-fix round is
   `incremental` — diff-scoped from that fix baseline, with attention focused
   on the fix's exact diff, its changed files and the still-open findings.
   Checkers may still read the whole repository, the charter, the code-index,
   test results and unmodified code, and must trace impact along call chains,
   shared state, protocols, data formats, configuration, concurrency and
   invariants: the scope controls attention, never tool permissions.
3. A clean incremental round is followed by a **full closure round**; only a
   clean closure round reaches INVARIANT_CHECK. A closure finding re-enters
   FIX and the incremental → closure cycle repeats.
4. Any change to the effective task description (adjudication, interjection)
   or an undecidable baseline **forces a full round** — i.e. the round falls
   back to the implementation baseline; an empty diff is never used to fake an
   incremental review.
5. **TEST always runs the project's full configured test commands** — SELF_CHECK
   scope never selects or shrinks tests.

All validated findings — deferred, repeated, or adjudicated — always enter the
fix loop; there is no convergence / severity / divergence path that passes a
round with unresolved findings.

Each SELF_CHECK step persists its `scope_mode`, baseline id, changed paths, fix
iteration and pass index for the CLI history view and the web console's scope
audit. Both surfaces render the mode as *full diff* / *incremental diff* and
state which baseline it diffs from, so the persisted value is never read as
"non-diff review"; the persisted values themselves stay `full` / `incremental`.

`WorkflowConfig` also carries a field named `self_check_passes_required_explicit`.
It is **not a config key** — it is derived at load time (it records whether
`self_check_passes_required` appeared in the YAML) and feeds the nested-chain
pass-count resolution. Writing it in YAML has no effect.

Note the shared sentinel convention across this project: for every *iteration
cap*, `0` and `null` mean "unlimited / disabled" and a negative value is a
fail-fast error. That rule holds for `workflow.max_fix_iterations`,
`workflow.self_check_defer_fix_threshold`, `workflow.adjudicate_period` and
[`investigation.max_iterations`](#investigation) alike.

At load time the resolved `max_fix_iterations` and its winning source file are
logged once per config path (`workflow config: max_fix_iterations=… (effective
source: …)`), specifically so a `tianluo.local.yaml` shadowing the committed
value is visible rather than mysterious.

### `pricing`

Model pricing overrides for the session usage/cost summary. Loaded into
`PricingConfig` and merged onto the built-in versioned price table (every
built-in entry records its catalog version, source URL and effective date).

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `pricing.models.<canonical_model>.<category>` | number `>= 0` | built-in table | USD **per million tokens** for one price category of one model. Categories: `input` / `uncached_input`, `output`, `cache_read`, `cache_creation` / `cache_write`, `cache_creation_5m`, `cache_creation_1h`. |

Behavioural notes:

- Model keys are **alias-normalized** before merging (`Opus-5` → `claude-opus-5`).
  A key that maps to neither an alias nor a built-in entry **defines a new
  entry** for that exact name.
- A non-numeric, boolean, or negative value drops the *offending model entry*
  (with a warning) while valid sibling overrides still apply; pricing is
  display accounting, so a typo must not fail the flow but must also never
  silently become a wrong price.
- Provider-reported actual cost is always authoritative. Estimates cover only
  calls that reported tokens but no actual cost, and only when the model and
  every nonzero token category have a price — **unknown model, unknown price,
  or a missing cache-TTL price renders as "unknown cost", never as `$0`**.
- Uncatalogued providers (e.g. Codex, which bills by subscription) simply show
  tokens with unknown cost.

#### Usage and cost visibility in history and the web console

Every LLM call/attempt is recorded as a unified usage record (`usage_status`,
agent, runner, provider, provider session, reported / resolved model, token
categories, provider actual cost). The record is the single accounting unit;
the flow session summary, `luo history show`, the daemon history payload and
the web console all render the **same backend aggregation** — the frontend
never re-sums provider sessions, cached tokens or pricing itself.

- Provider-reported **actual cost** is de-duplicated by billing session / call
  semantics and stays the authoritative *actual* column. **Estimated cost**
  covers only billing units that reported tokens but no actual cost, using
  the merged price table. Actual and estimated are always shown as separate
  columns — never added into a fabricated "total".
- Missing usage, missing model, missing price, or an unknown cache TTL is
  displayed as **unknown / partial**, never as a misleading `$0`. Explicit
  zero-usage reports stay distinguishable from missing data, and legacy
  five-field records are flagged `legacy` rather than guessed at.
- `luo history show <flow_id>` prints the per-call, per-step and flow totals
  (with `--json` emitting the same structured payload), and the web console's
  history view and live-flow sidebar show the same figures plus the plan mode
  (decomposition / granularity / group count) and the self-check scope audit.
  Active, archived and
  history-only flows are all recoverable; flows predating the unified records
  are marked incomplete instead of being rewritten.

### `investigation`

The `investigate` step's own bounded loop. `investigate` handles
"symptom known, cause unknown" work: it may add temporary logging or
verification patches, but must restore everything before the step ends (net-zero
diff, snapshot-verified by the engine).

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `max_iterations` | int `>= 0` | `3` | How many investigation rounds may run when a round ends inconclusive (`conclusive=false`). **`0` (or `null`) = unlimited.** Negative → `ConfigError`. bool/float warns and falls back to `3`. |

Exhausting the budget does **not** fail the flow: it proceeds to `plan` carrying
the best hypothesis so far, flagged low-confidence.

This loop is deliberately separate from the fix loop
(`workflow.max_fix_iterations`): an investigation round is an *exploration*
budget, not a repair attempt, and sharing one counter would let a long repair
history starve investigation (or the reverse).

### `test`

The test step's command, timeouts, extra phases, and the skip-is-not-a-pass
gate. Loaded into `TestConfig`. All eight fields:

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `command` | string or null | `null` | The primary test command, split with `shlex`. `null` → auto-detect from the project layout: `pytest.ini` or `pyproject.toml` → `<python> -m pytest -v`; `package.json` → `npm test`; `Cargo.toml` → `cargo test`; `go.mod` → `go test ./...`; otherwise `<python> -m pytest -v`. |
| `timeout` | int (seconds) | `1800` | Fallback timeout for the primary command, and the default timeout for any phase that does not set its own. Unlike its siblings this key is **not** individually clamp-and-warn — see the note below. |
| `phases` | list of maps | `[]` | Additional commands run **after** the primary one. Entry schema below. |
| `timeout_multiplier` | float `>= 1.0` | `2.0` | Multiplier applied to the LLM's estimated test duration when computing the dynamic timeout. Clamped up to `1.0` with a warning, so a typo like `0` or `0.1` cannot silently disable the feature. Non-numeric warns and falls back to `2.0`. |
| `min_dynamic_timeout` | int `>= 1` (seconds) | `30` | Floor for the computed dynamic timeout. Clamped up to `1` with a warning. |
| `max_dynamic_timeout` | int (seconds) | `14400` (4 h) | Ceiling for the computed dynamic timeout, so repeated fix-loop timeouts cannot compound the estimate without bound and mask a hung test as "just slow". **The effective default is `max(14400, timeout)`** — a project that deliberately sets a larger `test.timeout` is not capped below its own explicit intent. If the configured value is below `min_dynamic_timeout` it is raised to match, with a warning. |
| `critical_tests` | list of strings | `[]` | Acceptance tests that MUST genuinely run. See below. |
| `parallel` | `"auto"` or int `>= 1` | `null` | Run the **primary** command under [pytest-xdist](https://pytest-xdist.readthedocs.io/). `null` (the default) = serial, exactly as before this key existed. Anything else — `0`, a negative, a float, `true`, an unknown string — warns and falls back to serial. See below. |

> **Gotcha — a bad `timeout` discards the whole block.** `TestConfig.load()`
> wraps its parsing in a blanket `try/except` that falls back to an
> **all-defaults `TestConfig`**. Most keys are validated individually and clamp
> and warn, but `timeout` is coerced with a bare `int(...)`, so a value like
> `timeout: "30m"` raises inside that block and silently discards your
> `command`, `phases`, and `critical_tests` along with it. The only symptom is
> one log line: `Failed to load TestConfig from …, using defaults: …`. Write
> plain integer seconds.

`phases[]` entry keys (read directly by the test step):

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `name` | string | — | Phase label, used in results and logs. Required in practice. |
| `command` | string | — | The command, split with `shlex`. Required. |
| `cwd` | string or null | project root | Working directory, resolved against the project root. |
| `timeout` | int (seconds) | `test.timeout` | Per-phase timeout. |
| `required` | bool | `true` | When `true`, a failing phase makes the whole run fail. `false` = informational. |
| `in_fix_loop` | bool | `true` | When `false`, the phase is skipped during fix iterations (`get_phases_for_run(is_fix_iteration=True)` filters on it) — useful for a slow smoke suite you only want on the first pass. |

Both the primary command and each required phase get one in-place retry on
timeout before being treated as a failure, so a transient slowdown does not push
the flow into the fix loop.

**`critical_tests` — the "skip must not count as a pass" guard.** pytest exits
`0` for a SKIPPED test, and a test that has been renamed or silently
un-collected (import error, typo'd pattern) simply vanishes from the output
while the run still exits `0`. Either case would otherwise let `tests_passed`
(and downstream `verified`) go false-green. Each entry is matched as a
**substring of pytest's per-test id** (`path/to/file.py::test_name`), so a
fully-qualified id is the precise anchor and a bare file path matches every test
in it. For each pattern:

- matches one or more **skipped** tests → those ids are reported as
  `critical_skipped` and the run is **not verified**;
- else matches one or more tests that actually ran (passed *or* failed) → the
  pattern is considered genuinely exercised (a real failure is surfaced through
  the normal failure path);
- else → reported as `critical_missing` and the run is **not verified** — but
  only when the run produced parseable per-test results at all.

That last caveat matters: under a non-verbose test command nothing is parseable,
so missing-detection is skipped (with a warning) to avoid flagging every pattern
as missing. **`critical_tests` requires a verbose per-test-output command** such
as `python -m pytest -v`.

The list is empty by default — this is an explicit opt-in, so ordinary
platform / optional-dependency skips are never penalised. A non-list value warns
and disables the gate rather than raising.

**`parallel` — opt-in pytest-xdist parallelism.** `auto` gives one worker per
CPU, an integer pins the worker count. The value is appended to the primary
command as `-n auto` / `-n N`, **together with `--dist loadgroup`**:

```yaml
test:
  parallel: auto     # -> <primary command> -n auto --dist loadgroup
```

Three boundaries are worth knowing:

- **`--dist loadgroup` rides along.** It is the only scheduling mode under which
  pytest's `xdist_group` marker means anything — tests sharing a group name run
  on the same worker, and therefore sequentially. That is how a suite keeps its
  genuinely order-dependent tests (a shared git worktree, shared mutable global
  state) from colliding, so the switch would be unsafe without it. If your
  command **already** specifies a `--dist` mode, your choice wins and the
  `xdist_group` serial-group guarantee is lost with it. The same applies to
  `-n` / `--numprocesses`: an explicit worker count in the command is never
  overridden.
- **Primary command only.** `phases[]` entries are commands you wrote, and they
  are executed verbatim — this switch never rewrites them. A non-pytest primary
  command (`npm test`, `cargo test`, …) is likewise left alone, with a debug log
  line saying so.
- **A missing plugin is an error, not a silent fallback.** If pytest-xdist is
  not installed in the environment that runs the tests, the step ends as
  `FAILED` with an install instruction and goes to you — it never degrades to a
  serial run (which would report success for something you did not get) and
  never enters the fix loop (no code change can install a package). For a
  `<python> -m pytest …` command this is detected *before* anything runs, by
  probing that same interpreter; for a bare `pytest` command — whose interpreter
  cannot be known up front — it is recognised from pytest's
  `unrecognized arguments: -n …` output afterwards.

Parallelism is only safe for a suite whose tests do not depend on execution
order or share mutable global state, which is why it is opt-in.

### `e2e`

End-to-end testing: build a real, isolated environment (one container network,
one or more services), drive the project inside it, and assert on what actually
happened. Loaded into `E2EConfig`. **Off by default** — with `e2e.enabled` false
or the block absent, the state machine never inserts the `E2E` step and the flow
behaves exactly as it did before the subsystem existed.

This block carries **only runtime settings**. The *content* of e2e — services,
build steps, scenarios, baseline images — lives in a separate
[`tianluo/e2e/` directory](#the-other-half-tianluoe2e-content-configuration).
The split is deliberate: `enabled` is the **user's** promise (that a container
runtime is installed and that the fix loop may spend time on scenarios), and the
flow never flips it — at most it prints a suggestion that the project looks like
a good e2e candidate. Content, by contrast, is authored and evolved by the flow
just like test code. Because the two live in different files, "the flow never
writes `tianluo.yaml`" is checkable by looking at which paths were touched.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `enabled` | bool | `false` | Master switch. When false the `E2E` step is never inserted into any step sequence. Accepts the usual boolean spellings; an unrecognised value warns and stays `false`. |
| `runtime` | `auto` \| `docker` \| `podman` | `auto` | Which container runtime to use. See [Runtime selection](#runtime-selection). An invalid value warns and falls back to `auto`. |
| `oci_runtime` | string or null | `null` | Passed through to the runtime's `--runtime` flag. Point it at a VM-grade OCI runtime (Kata Containers and friends) to get VM-boundary isolation **by configuration alone**, with no separate backend. `null` = the container runtime's own default. |
| `build_timeout` | int `>= 1` (seconds) | `1800` | Budget for building a service image. Separate from `scenario_timeout` because image builds are the slow half (dependency installs on a cold layer cache) and scenarios the fast half. |
| `scenario_timeout` | int `>= 1` (seconds) | `300` | Default per-scenario budget. A scenario may override it with its own `timeout:`. |
| `estimated_e2e_duration` | int `>= 1` (seconds) or null | `null` | The e2e counterpart of [`test.estimated_test_duration`](#test)'s role: lets a supervising runner tell "still running" apart from "hung". |
| `scenarios` | list of strings | `[]` | Scenario selection by name. **Empty means "run everything"**, not "run nothing". Narrow it so a fix loop need not replay the full suite on every iteration — the same precedent as `test.critical_tests`. |
| `critical_scenarios` | list of strings | `[]` | Scenarios that must genuinely run for the result to count. Every name listed here is **added to whatever the selection resolved to** — narrowing `scenarios` for speed, or passing `--scenario` while debugging, cannot quietly drop one — and a critical scenario that produced no passing result makes the run fail, the same "a skip is not a pass" guard [`test.critical_tests`](#test) applies. A name that no declared scenario answers to is a configuration error: the guarantee could never be met. |
| `keep_environment` | bool | `false` | Leave containers and the network alive after the run so you can attach and look around. Debugging aid; the run prints the exact `rm -f` / `network rm` commands to clean up. |

Every field follows the clamp-and-warn policy used by [`test`](#test): a
malformed value is logged and replaced by its default rather than raising, so a
typo in one knob never makes the project unloadable.

#### Prerequisite: a container runtime you can run without sudo

e2e needs Docker or Podman, and **you install it yourself** — exactly as you
installed the `claude` / `codex` CLIs. pip cannot provide a container runtime.

tianluo and its e2e subsystem run entirely as your normal user. **No code path
calls `sudo` or requires root.** The prerequisite is therefore precisely: *the
current user can run `docker` or `podman` without sudo*. Any one of these
satisfies it:

- your user is a member of the `docker` group;
- **rootless Docker** (`dockerd-rootless-setuptool.sh install` ships with Docker);
- **Podman**, which is rootless natively via user namespaces and works out of the
  box for an unprivileged user.

Under a rootless runtime the bind-mounted source tree is UID-mapped (Podman's
`--userns=keep-id`) so that files a container writes into your source directory
end up owned by **you** — never as root-owned residue you cannot clean up.

Check the host before enabling:

```bash
luo e2e doctor
```

The tier-2 (baseline screenshot diff) image comparison needs one third-party
Python package, isolated behind an optional extra:

```bash
pip install 'tianluo[e2e]'
```

The framework code and the Dockerfile templates ship with **every** install —
the extra isolates a *dependency*, not tianluo's own code. A core-only install
stays importable; a project that enables e2e without the extra and reaches a
tier-2 assertion gets an actionable "install `tianluo[e2e]`" message rather than
a `ModuleNotFoundError`.

#### Runtime selection

`auto` probes by **executing** `docker info`, then `podman info`, and takes the
first that succeeds. This is deliberately not a `PATH` lookup: the most common
failure is a runtime that is *installed but unusable by the current user* — no
`docker` group membership, daemon not running — and a `PATH` check would happily
select it. Running `info` verifies in one shot that the binary exists, the
daemon/environment is healthy, and the current user has permission. The same
code is the preflight check, so probing and preflight can never disagree.

- **Both usable → `docker`**, deterministically. BuildKit/buildx is the more
  mature ecosystem, and on a machine with both installed Docker is usually the
  one the user deliberately installed for daily use. Prefer podman? Say so
  explicitly.
- **Naming a runtime explicitly disables fallback.** With `runtime: docker`, an
  unavailable Docker is an error with remediation — tianluo will *not* quietly
  use podman instead. A silent switch changes the image cache, the storage
  location and UID-mapping behaviour behind your back, producing exactly the
  kind of "it worked yesterday" fault that is miserable to diagnose.
- **The probe result is fixed for the session.** Every container operation in
  one run uses the same runtime; there is no mid-run switching.

A failed probe is reported as an **environment** problem with step-by-step
remediation (join the `docker` group / install podman / set up rootless Docker).
It is never treated as a code defect — see the failure routing below.

#### The other half: `tianluo/e2e/` content configuration

Content configuration lives in its own directory next to `charter.md`,
`code-index.md` and `issues/`, and **is committed to git**:

```
tianluo/e2e/
├── environment.yaml     # services topology: base images, build steps, readiness probes
├── scenarios/
│   ├── cli-smoke.yaml   # one scenario per file: driver + actions + assertions
│   └── api-smoke.yaml
└── baselines/           # git-tracked baseline screenshots for tier-2 diffs
```

Images are **not** committed — they are a regenerable cache, rebuildable from
zero out of this configuration. The project's source tree is *bind-mounted* into
the containers rather than `COPY`-ed into the images, so a fix-loop iteration
only restarts containers; images rebuild only when the build steps themselves
change.

The flow generates this directory on first use (when `enabled` is true but the
directory does not yet exist) and evolves it incrementally thereafter — it
adds and revises, it does not overwrite your hand edits. Evolution runs once per
flow, in the `E2E` step and *before* the scenarios execute, so a scenario added
for the behaviour this task introduced is exercised by the same run; fix-loop
iterations re-run the suite but never re-evolve it (a scenario is never rewritten
to make a failure go away). You can also drive it manually:

```bash
luo e2e bootstrap          # generate / evolve the content directory
luo e2e list               # list declared scenarios
luo e2e run                # run them all
luo e2e run -s api-smoke   # run one
luo e2e run --keep         # leave the environment up for inspection
```

`luo e2e run` and the in-flow `E2E` step share one execution path, so manual
debugging and the flow behave identically. It exits `1` on a scenario failure,
`3` on an environment problem and `4` on missing/inadmissible configuration.

A minimal single-service example — `tianluo/e2e/environment.yaml`:

```yaml
network: tianluo-e2e
services:
  - name: app
    image: python:3.12-slim
    base_kind: base            # base | playwright | gui-xvfb
    build:
      - pip install --no-cache-dir -e .
    readiness:
      kind: command            # command | http | tcp | log
      command: ["python", "-c", "import myapp"]
      timeout: 60
```

**Where a readiness probe runs matters.** `command` probes execute *inside* the
container, so they see the shared network and can address peers by service name
(`curl http://app:8000/health`, `pg_isready -h db`). `http` and `tcp` probes are
dialled by tianluo itself, i.e. **from the host**, so they can only reach a port
the service publishes — `ports: ["18000:8000"]` plus
`url: http://127.0.0.1:18000/health`. Pointing an `http` probe at `app:8000` or
at an unpublished `localhost:8000` is rejected at validation time rather than
left to time out. An `http` probe accepts any 2xx/3xx answer by default; add
`status: 401` when the service's healthy answer is a specific other code.

…and `tianluo/e2e/scenarios/cli-smoke.yaml`:

```yaml
name: cli-smoke
driver: app                    # must name a service declared above
actions:
  - action: exec
    command: ["python", "-m", "myapp", "--version"]
assertions:
  - kind: exit_code
    equals: 0
  - kind: stdout
    contains: "myapp "
```

**Action order, and one rule about `browser`.** Actions execute in declaration
order, with a single exception the validator makes impossible to hit by accident:
`browser` operations are collected into **one** Playwright program that runs
after the rest of the sequence (page state cannot survive a one-shot `exec`, so
batching is what keeps one browser session alive across the whole scenario).
Declaring a non-`browser` action *after* a `browser` one is therefore a
validation error — it would have run first, silently inverting the order you
wrote. Put every `exec` / `http` / `wait` ahead of the browser sequence, and take
in-page screenshots with a browser `op: screenshot` rather than the `screenshot`
action (which captures a virtual X display and belongs to a `gui-xvfb` driver).

An action that *fails* does not abort the sequence, but it is not free either. A
non-zero `exec` is recorded as a note and left to the assertions to judge — its
exit code and streams are exactly what `exit_code` / `stdout` / `stderr` exist
for, and a command that fails is frequently the thing under test. Everything else
(an unreachable `http` action, a `wait.until` that never came true, a coordinate
click that missed, a browser op that threw) has no assertion counterpart, so it
is reported as an action failure and the scenario **cannot** be reported as
passed — otherwise a broken UI whose assertions happen to look elsewhere would
come back green.

`base_kind` picks which bundled Dockerfile template is layered over `image`:
`base` (plain CLI / web / API), `playwright` (a browser driver — the official
image pins browsers, system libraries *and fonts*, which is what makes a tier-2
baseline reproducible), or `gui-xvfb` (Xvfb + a light window manager + scrot +
xdotool, so a desktop application can run and be screenshotted with no physical
display).

A scenario's `driver` must be able to perform what the scenario declares, and
that is checked at validation time: a `browser` action or a `dom` assertion needs
a browser, so the driver has to be a `playwright` service. Pointing such a
scenario at a `base` driver is a configuration error reported before anything is
built — otherwise the mismatch would only surface after every image was built and
every readiness probe awaited, aborting the run and discarding the results of the
scenarios that already passed in the same round.

**The assertion ladder is enforced by the schema, not merely recommended.**
Tier 1 is deterministic (`exit_code`, `stdout`, `stderr`, `http_status`,
`http_body`, `file_exists`, `file_content`, `dom`) and is the default, needing no
declaration. Tier 2 is `screenshot_diff` against a committed baseline and
requires `visual_regression: true` on the assertion. Tier 3 is
`visual_semantic` — an LLM looking at an image — and requires both
`semantic_visual: true` and `require_evidence: true`, because an LLM verdict is
admissible only alongside a reviewable description of what it saw. Escalating
past a tier that could have done the job is a **validation error**: a screenshot
comparison where a DOM query would do turns deterministic verification into
probabilistic verification. The same rule governs *driving* — clicking screen
coordinates (`visual_click`) is reserved for GUIs with no programmatic entry
point.

**Getting the first tier-2 baseline.** A baseline can only be produced inside the
image that renders the comparison shot — fonts and rasterisation live in the
image — so a new `screenshot_diff` starts with no file under `baselines/`. Run
`luo e2e run --write-baselines` once: it captures the missing images, reports the
scenario as **not passed** (nobody has looked at the rendering yet), and leaves it
to you to review the file and commit it. Without that flag a declared-but-absent
baseline is a configuration error naming the file, not a scenario failure — no
comparison happened, so blaming the code would be a lie.

A `baseline:` value must be a plain relative name **inside** `baselines/` — no
absolute path, no `..`. Anything else is rejected at validation time: a path that
escapes the directory would be compared against (and, under
`--write-baselines`, written to) a file outside version control, quietly voiding
the "the baseline is a committed asset" contract.

#### Where the E2E step sits, and how failures route

When `enabled` is true, the `E2E` step is inserted **immediately after `test`**
and therefore before `self_check`: e2e is the coarse-grained counterpart of the
unit suite, so it runs on code that already passes the fine-grained one, and the
review layer then reads a diff whose behaviour has been exercised. Sequences with
no `test` step (`review`, `survey`) are left untouched — they produce no code
change for a scenario to exercise.

Failure routing is deliberately split in two:

- **A failing scenario is a code defect.** It returns `REVISION_NEEDED` and
  enters the ordinary fix loop under
  [`workflow.max_fix_iterations`](#workflow), exactly like a failing unit test;
  once the budget is exhausted the run files an issue through the same
  fix-loop-exhaustion path. There is no discard, waiver, or severity-graded
  pass-through.
- **An environment problem is not.** An unusable runtime, missing permissions, a
  failed preflight — these fail the step with remediation text and **do not**
  consume a fix iteration. Sending an LLM to "fix" a host with no Docker
  installed would burn the entire fix budget for nothing.

### `implement`

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `group_loc_threshold` | int | `300` | **[`plan_decomposition: granular` only]** Total estimated LOC at or below which a multi-group task plan is merged into a **single** LLM call instead of being farmed out per group. Also feeds the sequential-vs-DAG-parallel decision above the threshold. **Fail-fast, not clamp-and-warn** (see below). |
| `use_worktree` | bool | `true` | Whether implement groups run in isolated git worktrees. Accepts the usual boolean spellings; an unrecognised string falls back to the default rather than flipping behaviour. **Overridable at runtime by the `SE3_IMPLEMENT_USE_WORKTREE` environment variable**, which wins over the YAML value. |

**`group_loc_threshold` applies to `granular` plans only.** Under the default
[`capability` doctrine](#plan-decomposition-doctrine-and-granularity) the
threshold takes no part in either decision: a coarse capability group carries
no per-task `estimated_loc`, so the total would always be `0` — and `0` reads
as *both* "small enough to merge" and "not worth parallelising", which would
silently serialize groups the plan declared independent. Capability mode
dispatches on group count and topology instead: one group → the whole-task
implement call, two or more → the DAG path (`plan_granularity: single` pins
the whole-task call regardless of the count), still subject to the existing
`use_worktree`, linear-dependency-chain and no-commits short-circuits. The
threshold is skipped because a capability group carries no LOC estimate to
judge — not because every group is large: a capability group is one coherent
task, aggregated by default and split only at the capability edge, so it is
*bounded* by what a single autonomous call can carry but is not sized at that
ceiling (two mutually unrelated 20-LOC tasks legitimately produce two small
groups). Under `granular` the threshold behaves exactly as it always has.

`group_loc_threshold` is one of the explicit exceptions to the clamp-and-warn
rule: `ImplementConfig.from_dict` calls bare `int(...)` on the value with no
`try`/`except`, and its caller in the implement step does not guard it either.
A value that `int()` cannot parse — e.g. `group_loc_threshold: "300 LOC"` —
raises an uncaught `ValueError` out of `ImplementConfig.load` and **fails the
implement step** rather than falling back to `300`. A float or bool is accepted
but silently truncated by `int()` (`300.9` → `300`, `true` → `1`).

### `steps`

| YAML key | Dataclass field | Type | Default | Meaning |
|----------|-----------------|------|---------|---------|
| `steps.append` | `StepConfig.append_steps` | list of strings | `[]` | Step-type names appended to the end of the default step sequence. |

**Note the name mismatch**: the YAML key is `append`, the dataclass field is
`append_steps`. Writing `steps.append_steps:` in YAML does nothing.

```yaml
steps:
  append:
    - summarize
```

Entries are validated against the `StepType` enum: an unknown name is **silently
ignored** (no warning), and a step already present in the sequence is not
duplicated. Appending is the only supported mutation — there is no `steps.remove`
or `steps.replace`.

### `version`

Version-bump behaviour for the `commit` step. Loaded into
`config.VersionConfig`.

> **Naming trap.** The YAML key for an explicit version-file path is
> **`version_file`**, not `file_path`. `VersionConfig.from_dict` reads
> `version_data.get("version_file")`; `.file_path` exists only as a read-only
> *property alias* on the dataclass (for duck-type compatibility with
> `version_bumper.VersionConfig`) and is never read from YAML. A
> `version: { file_path: … }` in your config — including the one in
> `tianluo.example.yaml` — is silently ignored.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `enabled` | bool | `true` | Master switch for automatic version bumping in the commit step. |
| `version_file` | string or null | `null` | Explicit path to the version file. `null` = auto-detect (`pyproject.toml`, `package.json`, …). |
| `include_in_commit_message` | bool | `true` | Whether the commit message carries a `Version: X.Y.Z` trailer. |
| `script_path` | string or null | `null` | Path to a project version script. `null` = the default `tianluo/scripts/version.py`. |
| `auto_generate_script` | bool | `true` | Generate that script via LLM when it is not found. |
| `auto_bump` | bool | `true` | *Inert.* No consumer in the engine — the bump is driven by `version_analyze`'s `suggested_version` and gated by `confirmation`, not by this flag. |
| `confidence_threshold` | string or null | `null` | *Inert.* Historically `"medium"` / `"high"` selected which confidence levels required human confirmation. |
| `prerelease_prefix` | string | `""` | *Inert.* |
| `prerelease_number` | int | `0` | *Inert.* |
| `templates` | map | `{readme_badge: …, versions_entry: …}` | *Inert / legacy.* Superseded by the [`documentation`](#documentation) block, which is what `DocumentationUpdater` actually reads. Kept so old configs still parse. |
| `readme_enabled` | bool | `true` | *Inert.* |
| `readme_marker` | string | `"<!-- SE3-VERSION -->"` | *Inert.* |
| `versions_enabled` | bool | `true` | *Inert.* |
| `versions_file` | string | `"VERSIONS.md"` | *Inert.* |
| `versions_header` | string | `"# Version History\n\n"` | *Inert.* |

Deprecated keys, **accepted but ignored with a warning**: `bump_rules` and
`smart_version_analysis`. Both were removed when the version decision model
collapsed to a single authoritative `suggested_version` produced by
`version_analyze`. To customize the bump rules, write natural-language rules
into `tianluo/version-rules.md` instead — that file is injected into the
`version_analyze` prompt; when absent, default SemVer 2.0.0 applies.

Note the split for worktree flows: a worktree session's commit is **not** its
release point, so it does not write the version file; the merge-side
`version_reconcile` step does.

### `documentation`

Templates used by `DocumentationUpdater` when the commit step mechanically
updates `README.md` and `VERSIONS.md`. This is the block that actually drives
the updater, superseding the legacy `version.templates`.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `readme_badge_template` | string | `![Version](https://img.shields.io/badge/version-{{version}}-blue)` | Markdown written in place of the README version badge. |
| `versions_entry_template` | string | first `##` block of the packaged `versions_md.md` template, else `## {{version}} - {{date}}\n\n{{changes}}\n` | The VERSIONS.md entry prepended for each release. An explicit config value is taken verbatim and **not** validated — one missing `{{changes}}` silently yields release entries with no changelog body. The `{{version}}` + `{{changes}}` requirement applies only to the packaged-file fallback: the first `##` block of `versions_md.md` is accepted as a template only when it carries both, otherwise the built-in default is used. |
| `readme_header_template` | string | *(unset)* | Optional. When present, a version header line in the README is replaced too. Unset means the header pass is skipped entirely. |

Non-string values (and a non-mapping `documentation:` section) are dropped, so
the updater keeps its built-in defaults.

**Placeholders** are `{{double-brace}}` and are substituted by a plain string
replace. The context the commit step supplies is:

| Placeholder | Value |
|-------------|-------|
| `{{version}}` | The new version string. |
| `{{date}}` | `YYYY-MM-DD` at render time. |
| `{{year}}` | Four-digit year at render time. |
| `{{changes}}` | *(VERSIONS entry only)* The rendered changelog bullets. |

**Badge matching and insertion.** `update_readme` looks for an existing badge
using, in order: a static shields-style `![Version](…version-X-…)` link, any
`![version](…)` link (case-insensitive), then an `<img …version…>` tag. The
**first** pattern that matches is replaced (`count=1`) with the rendered
template. If **none** matches, the rendered badge is *inserted* after the title
heading. The file is only written when the content actually changed.

**The idempotent-no-op trick (used by this repository).** Because the rendered
template replaces the matched badge verbatim, a template that contains **no
`{{version}}` placeholder** renders to a constant string. Once the README
already holds that exact string, the second pattern matches it, `re.sub`
substitutes it with itself, `content == original_content`, and nothing is
written — the badge rewrite becomes a byte-for-byte no-op, run after run.

This repository's root [`tianluo.yaml`](../tianluo.yaml) uses exactly that to
pin a shields.io *dynamic* badge that reads the version straight from
`pyproject.toml`, so the README carries no hardcoded version number and the
commit step never reverts it:

```yaml
documentation:
  readme_badge_template: "![Version](https://img.shields.io/badge/dynamic/toml?url=https%3A%2F%2Fraw.githubusercontent.com%2FCoREse%2Ftianluo%2Fmaster%2Fpyproject.toml&query=%24.project.version&label=version&color=blue)"
```

Two details make this work and are worth preserving if you copy it: the alt text
must stay `![Version]` (so the `![version](…)` pattern still finds it — otherwise
the updater finds *no* badge and **inserts a second one**), and the template must
not contain `{{version}}` (otherwise every release renders a different string
and the file is rewritten).

### `code_index`

Knobs for building and rendering `tianluo/code-index.md`. All eight fields:

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `degrade_trigger_lines` | positive int | `2000` | A structure-less, non-binary text file with at least this many lines becomes eligible for the line/byte chunk **degrade** mode (AST/structure boundaries are the normal granularity; chunking is the last resort). One of two size triggers — first to hit wins. |
| `degrade_trigger_bytes` | positive int | `262144` (256 KiB) | Byte counterpart of `degrade_trigger_lines`. |
| `chunk_lines` | positive int | `200` | When a file degrades to chunking, each chunk spans at most this many lines. |
| `chunk_bytes` | positive int | `16384` (16 KiB) | Byte counterpart of `chunk_lines` — the first limit to hit cuts the chunk. |
| `exclude` | list of strings | `[]` | Project-relative path patterns excluded from enumeration. Backstops the gitignore-based walk for tracked-but-unwanted noise git cannot filter (vendored blobs, huge generated files). Non-string / blank entries are dropped with a warning; a non-list value warns and yields `[]`. |
| `view_budget_bytes` | positive int | `8192` (8 KiB) | Byte budget for the adaptive **root-view map** injected into every flow step. Small on purpose: it bounds the always-injected orientation map and naturally stops expanding at directory granularity, which is the right altitude — function-level detail is pulled on demand via `luo code-index show`. |
| `primary_roots` | list of strings | `[]` | Explicit top-level directory names whose subtree the adaptive root view drills into; the rest stay collapsed. `[]` = auto-detect the code-bearing top-level directories. Entries may be written with or without a trailing slash (`src` or `src/`); they are normalised to a trailing slash. |
| `max_concurrency` | positive int | `4` | How many per-file LLM summarisation calls run concurrently during a (re)build. Conservative by default because the ceiling is LLM quota / rate-limit bound (the calls are I/O-bound, not CPU-bound); raise it to match your backend's limits. |

Every integer field is clamp-and-warn: a bool, a float, a non-integer, or a
non-positive value logs a warning and falls back to the default, so a malformed
`code_index:` never breaks an index rebuild.

### `merge`

Behaviour of the `luo merge` orchestrator.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `strategy` | `fast` \| `safe` \| `strict` | `fast` | Conflict-resolution tier. `fast`: the LLM resolves conflicts; on failure the merge exits without invoking a human and never falls back to take-theirs (it inherits the old robust strategy's dirty-worktree stash behaviour). `safe`: the LLM resolves, and escalates to a human MCP call if it cannot converge. `strict`: every conflict goes straight to a human call, no LLM. |
| `delete_merged_default` | bool | `true` | Whether `luo merge` deletes merged branches (and archives their worktrees) by default. Pass `--no-delete-merged` to opt out for one invocation. |
| `strict_runtime_sync` | bool | `false` | Stricter reconciliation of runtime state during the merge. |
| `max_conflict_resolve_iterations` | int `>= 1` | `10` | Cap on LLM conflict-resolution rounds. `< 1` → `ConfigError`; a non-integer warns and falls back to `10`. |

The removed strategy names `default` and `robust` are **not silently aliased** —
they raise a `ConfigError` carrying a migration hint, so a stale config cannot
quietly change merge semantics. Migrate `default` → `safe`, `robust` → `fast`.

### `conflict_resolver`

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `strategy` | `human` \| `llm` | `human` | Conflict handling for **loop-branch** merges. `human`: preserve the conflict state, write a call file, wait for a human. `llm`: attempt per-file LLM resolution, falling back to human on failure. |

An unrecognised value is silently coerced to `human` (fail-safe, no error).

This block is distinct from [`merge`](#merge): `merge.strategy` governs the
`luo merge` orchestrator, `conflict_resolver.strategy` governs the loop-branch
merge path.

### `claude_subprocess`

Settings for the Claude CLI subprocesses tianluo spawns as workers.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `setting_sources` | non-empty list of `user` \| `project` \| `local` | `["user"]` | Which Claude settings files the spawned CLI loads, passed through as `--setting-sources <csv>`. |

The `["user"]` default **isolates tianluo's workers from the target project's
`.claude/settings.json`**, so `permissions.deny` rules aimed at that project's
own sub-LLMs cannot lock out tianluo's plan / implement / review children. Opt
back in explicitly with `["user", "project"]`.

This key is **fail-fast**: a non-list, an empty list, a non-string element, or
any value outside `{user, project, local}` raises `ValueError` at load time
rather than warning and defaulting. A non-mapping `claude_subprocess:` section
raises too.

> **Pitfall — duplicate `--settings` is last-wins.** The Claude CLI's
> `--settings` flag does *not* accumulate: a second occurrence anywhere later in
> argv wholly overrides the first, including the `model` it selects. This bit
> this project once, when a guard appended its own `--settings` after an agent
> wrapper's — the wrapper's model was silently discarded and the run used the
> user settings' model instead. The engine therefore never appends a
> `--settings` of its own. If your own `agents.<name>.cmd` is a wrapper script
> that passes `--settings`, keep it the only one in the final argv. Note that
> `--setting-sources` (this config key) is a different flag with different
> semantics and is not affected.

### `server`

> Applies only to the optional central control plane (`pip install
> 'tianluo[server]'`, run via the `tianluo-server` entry point). A core-only
> install never reads it at runtime. For deployment, TLS reverse proxying,
> bootstrapping the first admin, and daemon key issuance, see
> [docs/daemon-and-server.md](daemon-and-server.md).

The `server:` block is a normal top-level key: a project `server:` section
**wholly replaces** the global one (no deep merge). Every field defaults, so a
server with no `server:` section still comes up usable.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `db_path` | string | `~/.se3/server.db` | Path of the embedded sqlite store backing identity / auth. `~` is expanded at use. The `tianluo-server --db-path` flag overrides it for a single launch. |
| `auth` | map | *(all defaults)* | See below. |

#### `server.auth`

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `providers` | list of `local` \| `oidc` \| `proxy_header` | `["local"]` | Ordered chain of auth providers to assemble. Unknown / blank / non-string entries are dropped with a warning; if nothing valid remains (or the value is not a list) it falls back to `["local"]`, so the server never comes up with an empty provider chain. Duplicates are collapsed. |

#### `server.auth.session`

UI session cookie attributes. Defaults are fail-safe for a public deployment
behind a TLS-terminating reverse proxy.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `cookie_name` | non-empty string | `se3_session` | Session cookie name. |
| `cookie_secure` | bool | `true` | Set the `Secure` attribute. Leave on unless you are serving plain HTTP on localhost. |
| `cookie_httponly` | bool | `true` | Set the `HttpOnly` attribute. |
| `cookie_samesite` | `lax` \| `strict` \| `none` | `lax` | `SameSite` attribute (compared lower-cased). An invalid value warns and falls back. |
| `max_age_seconds` | positive int | `86400` (24 h) | Session lifetime. |

#### `server.auth.local`

Brute-force guards for the built-in username+password provider. Two independent
mechanisms: consecutive-failure lockout, and a sliding rate limit.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `max_failed_attempts` | positive int | `5` | Consecutive failures that lock the account. |
| `lockout_seconds` | positive int | `300` (5 min) | How long the lock lasts. |
| `ratelimit_window_seconds` | positive int | `60` | Sliding rate-limit window. |
| `ratelimit_max_attempts` | positive int | `10` | Login attempts accepted per window. |

Each of these is clamp-and-warn on a bool, non-integer, or non-positive value —
a typo cannot silently disable a lockout window.

#### `server.auth.oidc`

A config **seam** for a future OIDC social-login provider; disabled by default
and not implemented in v1. When `enabled` is false the remaining fields are
inert.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `enabled` | bool | `false` | Turn the provider on. |
| `issuer` | string or null | `null` | OIDC issuer URL. |
| `client_id` | string or null | `null` | |
| `client_secret` | string or null | `null` | |
| `redirect_url` | string or null | `null` | |
| `scopes` | non-empty list of strings | `["openid", "email", "profile"]` | An empty or malformed list warns and falls back to the default. |

#### `server.auth.proxy_header`

A config **seam** for trusting a reverse-proxy-injected identity header;
disabled by default, v1 ships the seam only.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `enabled` | bool | `false` | Turn the provider on. |
| `trust_proxy` | bool | `false` | Trust the upstream proxy's identity assertion. |
| `header` | non-empty string | `X-Forwarded-Email` | Header carrying the identity. |

> **Security precondition when enabling this**: the reverse proxy MUST strip any
> client-supplied copy of `header`, and the server MUST NOT be reachable while
> bypassing the proxy. Otherwise the injected identity is forgeable and this is
> an authorization hole.

### `presets`

Named prompt templates for recurring tasks, run with `luo run --preset <name>`
(list them with `luo run --preset list`). Read by `preset_loader`, not by
`config.py`.

```yaml
presets:
  doc-sync:
    type: feature
    prompt_file: tianluo/prompts/doc-sync.md
```

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| *(map key)* | string | — | Preset name. |
| `type` | task type | the built-in default, or the existing entry's type | Task type the preset runs as (`feature`, `bugfix`, `small`, `review`, `survey`). |
| `prompt_file` | string | `tianluo/prompts/<name>.md` | Path to the prompt Markdown, relative to the project root. |

Resolution is two-layered. Built-in presets ship inside the package and are read
at runtime (never copied by `luo init`), so upgrading tianluo picks up the
latest ones automatically. Project presets override built-ins by name, and are
themselves built from two sources: first a zero-config scan of
`tianluo/prompts/*.md` (file stem = preset name, default type), then this
`presets:` block, which overlays metadata onto that scan and may redirect a
preset to an arbitrary `prompt_file`. Declaring a preset here with neither a
`prompt_file` nor a matching `tianluo/prompts/<name>.md` resolves to the
conventional location and errors if it genuinely does not exist.

---

## Legacy / historical configuration

Some block names in older `tianluo.yaml` files date from the retired
**spec-mirror** era, when `tianluo/specs/**` was a governed mirror of the code.
That mirror has been retired — the code is the single source of truth, exposed
through the code-index, `tianluo/charter.md`, and colocated why-comments. The
`spec_governance`, `spec_loading` and `spec_write_protection` blocks (and
`merge.guardrail_repair`) no longer have a loader at all: an existing block is
simply ignored, and keeping it in your config is harmless but pointless.

### Blocks the engine no longer reads

`tianluo.example.yaml` still ships the two blocks below. **The engine does not
read either of them** — verified by grepping `src/` for every key name and for
`get("human_call")` / a top-level `get("session")`; the only `session` hits are
the *nested* `server.auth.session` block and an unrelated JSON field in
`engine/chat_history.py`. They are retained in the sample for historical
continuity; configuring them has no effect.

| Block | Keys | Status |
|-------|------|--------|
| `human_call` | `timeout_days`, `directory` | No reader. Human-call files are written to `tianluo/calls/` at a path the runtime layout fixes, not this key. |
| `session` | `progress_file`, `max_progress_entries` | No reader. |

> An `e2e` block used to sit in this table too, carrying `baseline_dir`,
> `diff_threshold`, `default_viewport` and `test_paths` — four keys nothing ever
> read. The name has since been reclaimed by a real subsystem with an entirely
> different schema: see [`e2e`](#e2e). Those four legacy keys are not recognised
> by it and are ignored.

---

## Troubleshooting: "I changed the config and nothing happened"

**Suspect #1 is always [Pitfall 1](#pitfall-1--tianluolocalyaml-replaces-the-whole-file)** —
a `tianluo.local.yaml` shadowing the file you edited. Check which file is
actually active:

```bash
python -c "
from pathlib import Path
from tianluo.config import get_project_config_path
print(get_project_config_path(Path('.')))
"
```

If that prints a path you did not edit (note it may point into the **main repo**
when you are inside a worktree), you found it.

Then print the resolved value of the block you care about — every loader is a
plain function you can call:

```bash
python -c "
from pathlib import Path
from tianluo.config import (
    load_workflow_config, load_investigation_config, load_docs_config,
    load_code_index_config, load_merge_config, load_agents,
    load_confirmation_config, load_language_config,
)
p = Path('.')
print(load_workflow_config(p))
print(load_investigation_config(p))
print(load_docs_config(p))
print(load_code_index_config(p))
print(load_merge_config(p))
print(load_agents(p))
print(load_confirmation_config(p))
print(load_language_config(p))
"
```

Other loaders follow the same shape: `TestConfig.load(p)`,
`ImplementConfig.load(p)`, `StepConfig.load(p)`, `load_version_config(p)`,
`load_server_config(p)`, `load_claude_subprocess_config(p)`,
`load_conflict_resolver_config(p)`,
`load_step_agents(p, "implement")`, `load_self_check_resolution(p)`.

A checklist for the remaining cases:

1. **A YAML syntax error in `tianluo.local.yaml`.** It still shadows
   `tianluo.yaml`, and everything falls back to built-in defaults. The loader
   logs a one-shot warning naming the file — run with logging visible, or just
   `python -c "import yaml; yaml.safe_load(open('tianluo.local.yaml'))"`.
2. **A per-step chain that swallowed your `defaults` edit.** See
   [Pitfall 2](#pitfall-2--llm_callerstepsstep-is-a-hard-override-with-no-fallback):
   `llm_caller.steps.<step>` never falls back. Confirm with
   `load_step_agents(p, "<step>")`.
3. **The key name is wrong.** The mismatches this codebase actually has:
   `steps.append` (not `append_steps`), and `version.version_file` (not
   `file_path`). Unknown keys are generally ignored in silence.
4. **The block is inert or legacy.** Cross-check against
   [Legacy / historical configuration](#legacy--historical-configuration) and
   the *inert* markers in the [`version`](#version) table.
5. **The value was clamped or rejected.** Most fields warn and fall back rather
   than raising; the resolved value printed by the loader is the ground truth.
   `workflow` additionally logs its effective source once per config path:
   `workflow config: max_fix_iterations=… (effective source: …)`.
6. **An environment variable is winning.** `SE3_IMPLEMENT_USE_WORKTREE`
   overrides `implement.use_worktree`; `SE3_LANG` outranks `language.language`
   for CLI UI text.
7. **You edited `tianluo.example.yaml`.** It is a shipped sample and is never
   read. Copy it to `tianluo.yaml` first.
