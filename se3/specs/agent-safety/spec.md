<!-- spec-format: v1 -->

# agent-safety Specification

## Purpose

Define runtime safety rules that govern how LLM agents invoke bash tools inside SE3 flow-engine sub-process steps. This spec applies to every flow-engine step whose handler may dispatch an LLM that calls the `Bash` tool — by default this covers `plan`, `implement`, `design`, `propose`, `verify_spec`, `update_spec`, and `self_check`. Other steps that gain bash access in the future inherit the same rules.

**Scope boundary versus `spec-guardrails`.** `spec-guardrails` governs the *integrity of spec files* (forbidding deletion or weakening of Requirements during implementation). `agent-safety`, in contrast, governs the *runtime behavior of the LLM agent itself* while a step is executing — specifically, which bash command shapes are safe for the agent to emit. The two specs are orthogonal: `spec-guardrails` protects artifacts on disk; `agent-safety` protects the running agent and its host process tree.

**Enforcement is advisory.** SE3 ships no runtime bash static scanner, no command-allowlist wrapper, and no kernel-level interceptor for the patterns described below. Compliance depends on the LLM reading this spec when it is listed in the prompt's `## Available Specifications` block and self-policing before emitting destructive commands. The MUST NOT / SHOULD wording below is normative for the agent's *output*, not enforced by the engine; the spec exists so that audits, self-checks, and reviewer LLMs have a shared reference text to cite when a violation is observed.

## Requirements

### Requirement: Prohibited Self-Targeting Process Cleanup

LLM agents running inside an SE3 step MUST NOT use any process-cleanup command whose match pattern can plausibly appear inside the agent's own process-argv. Specifically, agents MUST NOT invoke:

- `pkill -f <pattern>`
- `pgrep -f <pattern>` followed by signalling those PIDs (e.g. `pgrep -f … | xargs kill`, or `pkill --signal` driven by an `-f` match)
- `killall <name>` when `<name>` is a substring likely to occur in the agent's own command line (e.g. `claude`, `python`, `node`, `bash`)

**Mechanism of self-kill.** When SE3's flow engine spawns the LLM (typically `claude -p <large-prompt>`), the entire prompt text is laid out on the child process's argv. The Linux kernel exposes this argv to `/proc/<pid>/cmdline`, which is exactly what `pkill -f` / `pgrep -f` scan with their regex. If the prompt happens to mention the very string the agent is now trying to kill (because the task description, prior step output, or this spec's own examples reference it), the match pattern hits the agent's own claude CLI process. The kernel delivers SIGTERM/SIGKILL to the agent, the agent exits with status -9 / -15, the flow engine sees an abnormal subprocess exit, optionally re-spawns it, and the new instance — with the same prompt — re-issues the same cleanup command, producing a self-sustaining SIGKILL oscillation that is hard to diagnose from logs alone (the agent simply "vanishes" mid-step).

`killall` is included in the prohibition because it matches by *command name*, and when the SE3 host process tree contains `python`, `bash`, or `node` ancestors (which it always does), an unguarded `killall python` can take down the parent flow engine, the agent, the test runner, and any sibling LLM all at once.

#### Scenario: pkill -f matches the agent's own prompt
- **GIVEN** an LLM agent running inside an `implement` step whose prompt text already includes the literal substring `python -m pytest` (e.g. because a prior task description, a test plan, or this spec was injected into the prompt)
- **WHEN** the agent emits the bash command `pkill -f 'python -m pytest'` intending to terminate a hung test runner it spawned earlier
- **THEN** the kernel matches the regex against `/proc/<claude-pid>/cmdline`, which contains the full prompt and therefore contains the substring `python -m pytest`
- **AND** the claude CLI process itself receives SIGTERM (or SIGKILL with `-9`), exits with status -15 / -9, and the SE3 step terminates abnormally
- **AND** the agent has violated this Requirement; the correct response is to use one of the safe alternatives in Requirement: Safe Alternatives for Process Cleanup

#### Scenario: killall on a shared interpreter name
- **GIVEN** an LLM agent whose host process tree contains `python` ancestors (the SE3 engine, pytest, or the project's own scripts)
- **WHEN** the agent emits `killall python` to clean up a runaway helper script
- **THEN** every `python` process in the same PID namespace receives the signal, including the SE3 flow-engine parent
- **AND** the agent has violated this Requirement

### Requirement: Safe Alternatives for Process Cleanup

When an LLM agent needs to terminate a subprocess, it SHOULD prefer the alternatives below, in the listed order of preference. These alternatives carry advisory weight only — see Purpose for the enforcement caveat — but every audit or self-check that observes a `pkill -f` should propose one of these replacements.

**Priority order: (d) > (a) > (c) > (b).**

#### (d) RECOMMENDED FIRST — Track and kill by recorded PID

Whenever the agent itself spawns the process it later wants to kill, it SHOULD record the PID at spawn time and signal that exact PID directly. This avoids any pattern-matching attack surface.

Concrete forms:
- In Python: `proc = subprocess.Popen([...]); ...; proc.terminate()` (or `os.kill(proc.pid, signal.SIGTERM)`).
- In shell: `cmd &; PID=$!; ...; kill "$PID"`.

##### Scenario: agent spawns and kills its own helper
- **GIVEN** the agent needs to start a background HTTP server during an `implement` step and tear it down before the step ends
- **WHEN** the agent uses `python -m http.server 8000 & SERVER_PID=$!` to spawn the server
- **THEN** at teardown it SHOULD invoke `kill "$SERVER_PID"` (and not `pkill -f http.server`)
- **AND** the cleanup is immune to the self-kill failure mode in the previous Requirement

#### (a) Match by exact command name

When the agent did not spawn the process itself but knows its `comm` (the kernel-level short command name, ≤16 bytes), it SHOULD use `pgrep -x` / `pkill -x` which match `comm` exactly, not `cmdline`. Because `comm` is the basename of the executable (typically `python`, `claude`, `node`), this never matches against the full prompt argv.

Concrete forms:
- `pgrep -x my-helper` — exact match on the executable name
- `pkill -x my-helper` — signal processes whose `comm` is exactly `my-helper`

##### Scenario: agent terminates a uniquely-named helper binary
- **GIVEN** the agent wants to stop a helper binary installed as `/usr/local/bin/se3-runner`
- **WHEN** the agent emits `pkill -x se3-runner`
- **THEN** only processes whose `comm` is exactly `se3-runner` receive the signal
- **AND** the prompt's argv text is irrelevant to the match, so no self-kill is possible

#### (c) If `-f` is unavoidable, anchor the pattern and pre-check

If the target process can only be distinguished by its full command line (e.g. several `python` workers differing only in script arguments), the agent MAY use `-f` provided that BOTH conditions hold:

1. The match pattern is anchored to a complete absolute path or a string token guaranteed not to appear anywhere else in the agent's prompt (e.g. a random nonce embedded in the worker's argv at spawn time, or a fully-qualified path like `/opt/myapp/workers/ingest_worker.py` that prompts never quote verbatim).
2. The agent first runs `pgrep -af <pattern>` (note: `-a` prints the full command line of each match) and inspects the returned PID set. If any PID in the set equals `$$`, equals `$PPID`, or appears anywhere in the agent's ancestor chain (see (b)), the cleanup MUST be aborted.

##### Scenario: agent pre-checks an anchored -f pattern
- **GIVEN** the agent must kill workers spawned with `/opt/myapp/workers/ingest_worker.py --shard=3` and only `-f` can distinguish them from other `python` processes
- **WHEN** the agent runs `pgrep -af '/opt/myapp/workers/ingest_worker.py --shard=3'`
- **AND** the returned PID list contains no entry equal to `$$`, `$PPID`, or any ancestor PID
- **THEN** the agent MAY proceed to `pkill -f '/opt/myapp/workers/ingest_worker.py --shard=3'`
- **AND** if the pre-check returns the agent's own PID or any ancestor, the agent MUST abort and fall back to alternative (a) or (d)

#### (b) Explicitly exclude $$ / $PPID / ancestor chain

Whenever the agent uses any pattern-based selector (even after (a) or (c)), it SHOULD compute an exclusion set containing the current shell PID (`$$`), its parent (`$PPID`), and the transitive ancestor chain, then filter those PIDs out of the kill target list before signalling.

A minimal ancestor-walk in shell:
```sh
exclude="$$ $PPID"
pid="$PPID"
while [ "$pid" -ne 1 ] && [ -n "$pid" ]; do
  pid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')
  [ -z "$pid" ] && break
  exclude="$exclude $pid"
done
# then: for each candidate PID, skip if it appears in $exclude
```

##### Scenario: agent excludes its own ancestors before killing
- **GIVEN** the agent has produced a candidate PID list via `pgrep -x` or an anchored `pgrep -af`
- **WHEN** the agent walks `ps -o ppid= -p <pid>` upward from `$PPID` until reaching PID 1, collecting every PID into an exclusion set together with `$$` and `$PPID`
- **THEN** the agent removes any candidate PID present in the exclusion set before issuing `kill`
- **AND** even if the pattern unexpectedly matches an ancestor, the signal is never delivered
