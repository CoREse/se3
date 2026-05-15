<!-- spec-format: v1 -->

# spec-guardrails Specification

## Purpose

Define the guardrails that protect spec integrity during implementation. These rules ensure that requirements are not inappropriately weakened, deleted, or modified by agents implementing against them.

## Requirements

### Requirement: Prohibited Actions

Agents MUST NOT perform the following actions without explicit human approval:

1. **MUST NOT delete** an existing spec requirement without explicit human approval (via human call)
2. **MUST NOT weaken** a requirement (e.g., changing "SHALL validate all inputs" to "SHOULD validate inputs")
3. **MUST NOT modify** the description or scenarios of a requirement they are implementing — the implementer does not get to change the spec they're building against

#### Scenario: Attempt to delete requirement
- **WHEN** an agent removes a SHALL requirement from a spec during implementation
- **THEN** the system blocks the change and reports a guardrail violation

#### Scenario: Attempt to weaken requirement
- **WHEN** an agent changes "SHALL" to "SHOULD" or "MUST" to "SHOULD"
- **THEN** the system blocks the change and reports a guardrail violation

#### Scenario: Attempt to modify implementing spec
- **WHEN** an agent modifies scenarios in a spec they are currently implementing
- **THEN** the system blocks the change and reports a guardrail violation

### Requirement: Permitted Actions

Agents CAN perform the following actions:

1. **CAN ADD** new requirements
2. **CAN MODIFY** requirements they are not currently implementing (with a change proposal)
3. **CAN MARK requirements as deprecated** with a human-approved reason and migration path

#### Scenario: Add new requirement
- **WHEN** an agent discovers a missing requirement during implementation
- **THEN** they CAN add the new requirement to the spec
- **AND** they SHOULD create a separate change to track the new requirement

#### Scenario: Modify non-implementing spec
- **WHEN** an agent identifies an issue in a spec they are NOT implementing
- **THEN** they CAN propose modifications through the normal change process

#### Scenario: Mark requirement as deprecated
- **WHEN** a requirement is no longer needed with human approval
- **THEN** the agent CAN mark it as deprecated with a reason and migration path

### Requirement: Guardrail Enforcement

The system SHALL enforce guardrails through automated checks.

**Enforcement points:**
1. Before committing, review the git diff of `se3/specs/`
2. If spec drift is detected, revert and investigate
3. Use `se3 guardrails` command to check spec files for violations

**Violation detection methods:**
1. Compare original and modified spec content
2. Check for deleted scenarios (missing WHEN clauses)
3. Check for weakened language (SHALL → SHOULD, MUST → SHOULD)
4. Check for weakened quantifiers (all → some, every → some)
5. For corner-case branches that infer weakening from the combination of "a strong line is missing" AND "a weak-only line exists in the new file", the detector SHALL prove the two lines are a replacement pair before reporting a violation (see "Pairing-Based Corner-Case Detection")

#### Scenario: Automated guardrail check
- **WHEN** `se3 guardrails <spec-file>` is run
- **THEN** it compares the spec against the git HEAD version
- **AND** reports any violations of the guardrails

#### Scenario: Mandatory guardrails after every `se3 merge` commit
- **GIVEN** a `se3 merge <branch>` invocation produces a merge commit that touches one or more `se3/specs/**/spec.md` files (whether or not those files had textual conflicts)
- **WHEN** the merge product is evaluated
- **THEN** `se3 guardrails` is run against each touched spec file before the merge is considered complete
- **AND** the check is enforced in all three strategy tiers — `default`, `strict`, AND `fast` — so that the `fast` tier's relaxation for ordinary text conflicts does NOT extend to spec files
- **AND** any violation (deleted requirement, weakened language SHALL→SHOULD, weakened quantifier all→some, deleted scenarios) causes the merge commit to be rolled back and escalated to a human MCP call file under `se3/calls/`

### Requirement: Guardrails CLI Command Interface

The `se3 guardrails` command SHALL accept a spec file path as its primary positional argument and SHALL support comparison against either the git HEAD version or a user-supplied original file.

**Command flags:**
1. The command SHALL support an optional `--original` / `-o` flag that takes a path to an "original" version of the spec file to compare against.
2. When `--original` is provided, the command SHALL compare the current spec file against the supplied original file instead of the git HEAD version.
3. When `--original` is NOT provided, the command SHALL fall back to comparing against the git HEAD version of the spec file (the default behavior).

This allows callers (e.g., merge tooling, test harnesses, or human reviewers) to invoke the same guardrail checks against an arbitrary baseline — not only the git HEAD — which is useful when the "original" content lives outside the working tree (for example, when comparing the merge product against the pre-merge state of a branch, or against a snapshot stored in a call file).

#### Scenario: Compare against git HEAD (default)
- **WHEN** `se3 guardrails <spec-file>` is invoked without an `--original` flag
- **THEN** the command resolves the spec's git HEAD version
- **AND** compares the working-tree spec against that HEAD version
- **AND** reports any guardrail violations found between the two

#### Scenario: Compare against a user-supplied original file
- **GIVEN** a caller has a baseline copy of the spec at a path outside the working tree (e.g., `/tmp/spec-before-merge.md`)
- **WHEN** `se3 guardrails <spec-file> --original /tmp/spec-before-merge.md` is invoked
- **THEN** the command compares `<spec-file>` against the supplied original file rather than the git HEAD version
- **AND** reports any guardrail violations found between the two
- **AND** the `-o` short form is accepted as an alias for `--original`

### Requirement: Guardrail Violation Reporting

When a guardrail violation is detected, the system SHALL provide clear reporting.

**Report format:**
- Violation type (delete/weaken/modify-implementing)
- File path and line number
- Original text (if applicable)
- Modified text (if applicable)
- Rule that was violated

**Structured evidence:** Each violation SHALL additionally carry a structured `evidence` payload that callers (fast-mode abort reports, human MCP call files, log entries) can render directly. Evidence MAY include any of the following fields when relevant to the violation type:
- `strong_line` / `weak_line` — original strong-language text and the candidate weak replacement text
- `strong_line_no` / `weak_line_no` — line numbers within the modified spec file
- `pairing_score` — the similarity score (e.g., token-set Jaccard) that justified treating the two lines as a replacement pair
- `prefix_score` — secondary diagnostic score (e.g., a leading-token prefix similarity) used alongside `pairing_score` when the detector evaluates multiple similarity views of the same candidate pair
- `all_pairings` — when the detector evaluated more than one candidate weak-only line for the same missing strong line, the full list of `{weak_line, weak_line_no, pairing_score, prefix_score, ...}` candidates considered, so reviewers can see which candidates were rejected and why
- `deleted_line` / `deleted_line_no` / `when_clause` / `when_clauses` — for deletion violations, the removed text, its line number in the original spec, and the affected WHEN-clause name(s) (single or list form)
- `branch_name` / `trigger_branch` / `branch_kind` — the merge branch and detector code-path that produced the report (e.g., "primary" vs "corner-case")
- `pre_sha` / `post_sha` / `parent_count` / `min_parents` / `topology_check` — topology evidence for `CHECK_FAILURE` violations (see "Merge Topology Validation")
- `exception_type` / `exception_msg` — for `CHECK_INCOMPLETE` violations, the exception class name and message captured when a file read or git invocation failed

#### Scenario: Report deletion violation
- **WHEN** a scenario is deleted from a spec
- **THEN** the report shows: "[must_not_delete] Deleted scenarios detected: {scenario_names}"

#### Scenario: Report weakening violation
- **WHEN** SHALL is changed to SHOULD
- **THEN** the report shows: "[must_not_weaken] Requirement weakened: SHALL → SHOULD"

#### Scenario: Violation evidence is structured and self-contained
- **GIVEN** a guardrail violation has been detected by any branch (primary or corner-case)
- **WHEN** the violation is serialized for downstream consumers (fast-mode abort summary, human MCP call file, log entry)
- **THEN** the violation carries an `evidence` object containing the strong/weak line text, line numbers, and the detector branch that fired
- **AND** consumers SHALL render the evidence verbatim without having to re-read the spec file or re-run the detector

#### Scenario: Spec file read failure during merge guardrails check
- **GIVEN** a spec file `se3/specs/foo/spec.md` was changed in the merge
- **WHEN** the guardrails check attempts to read it and the read raises `OSError` or `UnicodeDecodeError`
- **THEN** the system emits a `CHECK_INCOMPLETE` violation for that file
- **AND** the evidence includes `exception_type` and `exception_msg`
- **AND** the report's `incomplete` flag is set to `True`
- **AND** the report's `passed` flag is `False`

#### Scenario: Changed-spec-files enumeration fails
- **GIVEN** the git tooling cannot enumerate the spec files changed between `pre_sha` and `post_sha` (e.g., a `git diff` invocation fails)
- **WHEN** `check_merge_result` runs
- **THEN** the system emits a single `CHECK_INCOMPLETE` violation with `file_path="N/A"`
- **AND** the report is returned with `passed=False` and `incomplete=True`
- **AND** the system does NOT silently treat the failure as "no spec files changed"

#### Scenario: Per-file iteration error does not abort the report
- **GIVEN** the guardrails check iterates over multiple changed spec files
- **WHEN** one file raises an unexpected `OSError`, `UnicodeDecodeError`, or `ValueError` during checking
- **THEN** the system records a `CHECK_INCOMPLETE` violation for that file
- **AND** the iteration continues with the remaining files
- **AND** the final report's `incomplete` flag is `True` and `passed` flag is `False`

### Requirement: Typed Evidence Schema

The `evidence` payload attached to a guardrail violation SHALL be a typed record with an enumerated, closed set of field names — NOT a free-form dict. This protects callers (fast-mode abort reports, human MCP call files, log entries) from silent field-name typos and gives downstream consumers a stable schema to read against.

**Schema contract:**

1. The evidence record SHALL be a dataclass-like structure (`EvidenceRecord`) that enumerates every recognised field. Each field SHALL be optional (default `None`) so a single record can carry evidence for any violation type.
2. Constructing an `EvidenceRecord` with an unknown field name SHALL fail fast — the constructor SHALL raise a `TypeError` (or equivalent typed error) at the point of construction. Unknown keys MUST NOT be silently dropped or stored.
3. The record SHALL provide a `to_dict()` serializer that returns a plain `dict` containing **only** the populated fields — fields whose value is `None` SHALL be omitted from the serialized output, so JSON-persisted call files and log entries do not carry `null`-valued noise.
4. The record SHALL provide a `from_dict(data)` deserializer that round-trips with `to_dict()`. `from_dict(None)` and `from_dict({})` SHALL return `None` so callers can transparently handle the no-evidence case. `from_dict(...)` with an unknown key SHALL surface the same fail-fast `TypeError` as direct construction (unknown keys are NEVER silently dropped).
5. The set of recognised field names SHALL be the union of the categories enumerated under "Guardrail Violation Reporting" — pairing evidence, deletion evidence, branch/detector context, topology evidence, and incomplete-check evidence. Adding a new category of evidence to the system SHALL be done by extending the typed schema, not by passing extra dict keys at the call site.

#### Scenario: Unknown evidence field is rejected at construction
- **GIVEN** a caller attempts to attach evidence with a misspelled field name (e.g., `strng_line` instead of `strong_line`)
- **WHEN** an `EvidenceRecord` is constructed (directly or via `from_dict`)
- **THEN** a `TypeError` is raised at the point of construction
- **AND** the violation is NOT created with silently missing evidence

#### Scenario: Serialized evidence omits None-valued fields
- **GIVEN** an `EvidenceRecord` populated only with `strong_line`, `weak_line`, and `pairing_score`
- **WHEN** `to_dict()` is called
- **THEN** the returned dict contains only those three keys
- **AND** no `None`-valued field name appears in the dict
- **AND** the dict is suitable for embedding in JSON call files and log entries without `null` noise

#### Scenario: Evidence round-trips through dict serialization
- **GIVEN** an `EvidenceRecord` with any combination of populated fields
- **WHEN** the record is serialized via `to_dict()` and rebuilt via `EvidenceRecord.from_dict(...)`
- **THEN** the rebuilt record carries the same populated fields with the same values
- **AND** `from_dict(None)` returns `None`
- **AND** `from_dict({})` returns `None`

### Requirement: Stable Violation-Set Hashing

The guardrails module SHALL expose a `violation_set_hash(violations)` function that computes a deterministic, order-independent hash of a set of `GuardrailViolation` objects. Downstream consumers (fast-mode repair tracking, de-duplication of repeated calls, idempotency checks against previously-recorded violation sets) rely on this hash to recognise when two evaluations of a merge product describe the *same* underlying defects — even if the violations are reported in a different order, or even if the human-readable `message` text drifts slightly between runs.

**Hash contract:**

1. The hash SHALL be computed from a per-violation `(file_path, violation_type, stable_key)` triple, where `stable_key` is derived by a `_stable_key_from_violation(v)` helper that prefers the violation's structured evidence over its free-form `message`.
2. **Stable-key derivation order** (the helper SHALL consult these evidence sources in order, returning the first that produces a usable key):
   - WEAKENING evidence: the `strong_line` field of the evidence, normalized for comparison (role words like SHALL/SHOULD/MAY/MUST and stop words stripped, tokens sorted, whitespace/punctuation collapsed to a single space) so two reports of the same weakening that differ only in role-word phrasing, whitespace, or punctuation produce the same key.
   - DELETE evidence: the `deleted_line` field, normalized identically.
   - WHEN-clause deletion evidence: the `when_clauses` list — each clause normalized, sorted alphabetically, and joined with `|`, so the key is insensitive to the *count* of WHEN clauses reported in any wrapper message (e.g., "1 WHEN clause(s)" vs "2 WHEN clause(s)") and insensitive to the order in which clauses were enumerated.
   - Fallback: a normalized form of the violation's `message`, used only when no usable evidence is present (preserves backward compatibility with violations created before the evidence schema landed).
3. **Empty-key sentinels:** when an evidence-derived line normalises to the empty string (e.g., the line consisted entirely of role/stop words such as `"SHALL be required"`), the helper SHALL NOT return an empty key. Instead it SHALL return a deterministic sentinel of the form `"sentinel:<kind>:<short_hash>"` where `<kind>` is one of `weakening`, `delete`, `when`, or `msg`, and `<short_hash>` is a stable short hash of the original (un-normalized) source text. This prevents distinct empty-normalised violations from colliding into a single key.
4. **Order independence:** `violation_set_hash` SHALL sort the per-violation tokens before hashing, so two evaluations that produce the same multiset of `(file_path, violation_type, stable_key)` triples in different orders produce the same hash.
5. **Algorithm:** the final hash SHALL be computed by joining the sorted tokens with `\n` and applying SHA-1 (`hashlib.sha1`) to the UTF-8 encoding of the joined payload. SHA-1 is acceptable here because the hash is used for identity/de-duplication, not for cryptographic integrity.
6. The hash MUST stay at the pure string/regex/hashlib layer — no external dependencies, no semantic models — so it remains deterministic across processes, platforms, and Python versions.

#### Scenario: Same violation set in different orders hashes identically
- **GIVEN** two `GuardrailViolation` lists containing the same three violations in different orders
- **WHEN** `violation_set_hash(list_a)` and `violation_set_hash(list_b)` are computed
- **THEN** both hashes are equal
- **AND** consumers can compare hashes to recognise that the same defects were reported

#### Scenario: Message drift does not change the hash
- **GIVEN** two evaluations of the same merge product whose WEAKENING violation evidence carries identical `strong_line` text, but whose human-readable `message` text differs (e.g., one says "Requirement weakened: SHALL → SHOULD" and the other appends a line number)
- **WHEN** `violation_set_hash` is computed for each evaluation
- **THEN** both hashes are equal, because the stable key is derived from the normalized `strong_line`, not the message

#### Scenario: WHEN-clause deletion hash is insensitive to count and order
- **GIVEN** two DELETE violations describing the same set of removed WHEN clauses
- **AND** one violation lists the clauses in one order and the other lists them in a different order
- **AND** the wrapper messages report different total counts (e.g., "1 WHEN clause(s) deleted" vs "2 WHEN clause(s) deleted")
- **WHEN** `violation_set_hash` is computed for each
- **THEN** both hashes are equal, because the stable key joins the normalized, alphabetically sorted clauses

#### Scenario: Empty-normalised lines use deterministic sentinels and do not collide
- **GIVEN** two WEAKENING violations whose `strong_line` evidence consists entirely of role/stop words (e.g., `"SHALL be required"` and `"MUST be required"`) and therefore both normalise to the empty string
- **WHEN** the stable keys are computed
- **THEN** each violation receives a sentinel of the form `"sentinel:weakening:<short_hash>"`
- **AND** the two sentinels differ because they incorporate a short hash of the distinct original texts
- **AND** the two violations therefore produce different `violation_set_hash` outputs rather than colliding into one

#### Scenario: Fallback to normalized message when no evidence is present
- **GIVEN** a legacy violation created without a structured `evidence` payload
- **WHEN** the stable key is computed
- **THEN** the helper falls back to a normalized form of the violation's `message`
- **AND** if the normalized message is itself empty, a `"sentinel:msg:<short_hash>"` key is used so the violation still contributes a unique stable token to the set hash

### Requirement: Pairing-Based Corner-Case Detection

To avoid false positives when a spec is legitimately extended (e.g., a SHALL sentence is rephrased or expanded, while an unrelated SHOULD/MAY line was already present), the corner-case weakening branch of the detector SHALL prove that the missing strong line and a candidate weak line in the modified file are a replacement pair before reporting a violation.

**Pairing rule (applies to both SHALL/MUST weakening and quantifier weakening corner-case branches):**

1. For every "missing strong line" the primary diff identified, the detector SHALL attempt to pair it with a "weak-only line" from the modified file.
2. Pairing is decided by a deterministic content-similarity score (e.g., token-set Jaccard after stripping role words such as SHALL/SHOULD/MAY/MUST and stopwords). The score MUST meet a documented threshold (≥ 0.5 for SHALL/MUST; ≥ 0.65 for in-place mixed-line weakening) for the pair to be accepted.
3. If no candidate weak-only line clears the threshold, the corner-case branch SHALL NOT report a violation — even if both "a strong line went missing somewhere" and "a weak-only line exists somewhere" are independently true.
4. When pairing succeeds, the violation's structured evidence SHALL include the paired strong and weak line texts, both line numbers, and the pairing score.
5. The same pairing rule applies to the quantifier (e.g., `all`/`every` → `some`) corner-case branch.

The similarity computation MUST stay at the pure string/regex layer (no semantic models) so that detection is deterministic, dependency-free, and unit-testable.

#### Scenario: Legitimate SHALL extension does not trigger corner-case false positive
- **GIVEN** the modified spec extends an existing SHALL sentence and adds a new MUST line, with no SHALL→SHOULD/MAY weakening
- **AND** an unrelated SHOULD or MAY line already existed in the file before the change
- **WHEN** the corner-case weakening branch evaluates the diff
- **THEN** the detector tries to pair the missing strong line with each weak-only line by token-set similarity
- **AND** because no weak-only line clears the similarity threshold, no WEAKENING violation is reported
- **AND** the merge proceeds without escalation

#### Scenario: Genuine SHALL → MAY weakening is still caught
- **GIVEN** a SHALL sentence in the original spec is rewritten to a MAY sentence covering the same subject in the modified spec
- **WHEN** the corner-case branch evaluates the diff
- **THEN** the missing strong line and the new MAY line have token-set similarity above the threshold and form a replacement pair
- **AND** the detector reports a WEAKENING violation with structured evidence containing both lines, both line numbers, and the pairing score

#### Scenario: Mixed corner case — one weakening offset by an added SHALL is still flagged
- **GIVEN** the diff both weakens one SHALL into a SHOULD AND adds an unrelated new SHALL elsewhere
- **WHEN** the corner-case branch evaluates the diff
- **THEN** the pairing logic still finds a replacement pair for the weakened line and reports a WEAKENING violation
- **AND** the unrelated added SHALL does not mask the weakening

### Requirement: Mixed-Line Same-Strong-Count Replacement Guard

Token-set Jaccard similarity alone is not sufficient to classify an in-place mixed-line pairing as a genuine weakening when the candidate "weak" line still contains the SAME number of strong-keyword occurrences as the original strong line. In that case, the candidate may simply be an EXTENSION of the original line that happens to mention a weak keyword (e.g., `"SHALL validate inputs."` → `"SHALL validate inputs and SHOULD log."` — same strong-keyword count, similarity above threshold, but NO weakening occurred). Without a structural guard, such extensions would be falsely classified as WEAKENING violations.

The detector SHALL therefore apply a strong-keyword-count guard to every mixed-line pairing before promoting it to a WEAKENING violation, and SHALL emit additional structured evidence so reviewers can audit the decision.

**Guard rule (applies to the mixed-line corner-case branch only — same-count pairings):**

1. For each candidate pairing `(strong_text, weak_text)` that clears the pairing similarity threshold, the detector SHALL count the occurrences of the strong-keyword regex (e.g., `\bSHALL\b|\bMUST\b`) in both lines. Call these `strong_count` and `weak_line_strong_count`.
2. **Fewer-strong path** — if `weak_line_strong_count < strong_count`, the pairing is accepted as a WEAKENING pair without further structural checks. (Strictly fewer strong keywords means at least one was removed.)
3. **Same-count in-place path** — if `weak_line_strong_count == strong_count`, the detector SHALL additionally verify that the weak keyword appears in the SAME structural position as a removed strong keyword. This is established by:
   - Computing `orig_prefix` as the text BEFORE the strong-keyword match in `strong_text`, and `mixed_prefix` as the text BEFORE the weak-keyword match in `weak_text`.
   - Tokenising both prefixes via the same `_tokenize_for_pairing` routine the pairing layer uses.
   - Computing `prefix_score` as the Jaccard similarity of the two prefix token sets.
   - Accepting the pairing as an in-place replacement when **either** (a) BOTH prefix token sets are empty (both lines begin with the keyword itself), OR (b) `prefix_score >= 0.8`.
4. **Greater-count path** — if `weak_line_strong_count > strong_count`, the pairing is rejected (no weakening possible because strong keywords were added, not removed).
5. **Keyword-position swap edge case** — when one prefix is empty and the other is non-empty (e.g., `"SHALL log"` → `"log SHOULD"`), `prefix_score` evaluates to 0 and the same-count path rejects the pairing. This is intentional: keyword-position swaps are treated as structural rewrites rather than in-place weakenings, even though token-set similarity may be high.
6. When the same-count in-place path accepts a pairing, the violation's structured evidence SHALL include a `prefix_score` field (rounded to 3 decimal places) alongside `pairing_score`. The fewer-strong path SHALL NOT include `prefix_score` (it is `None` / omitted).
7. When multiple candidate pairings were evaluated for the same missing strong line, the `all_pairings` evidence array SHALL include each candidate's `pairing_score` and (when applicable) `prefix_score`, so reviewers can see why a same-count candidate was accepted or rejected.

**Diagnostic logging:** When the mixed-line branch finds no pairing that clears either the similarity threshold OR the same-count guard, the detector MAY log the highest sub-threshold pairing score at DEBUG level for near-miss diagnostic tracing. Diagnostic logging MUST NOT promote a sub-threshold or guard-rejected pair to a violation.

The same-count guard MUST stay at the pure string/regex layer (no semantic models) so that detection is deterministic, dependency-free, and unit-testable — identical to the pairing-similarity layer.

#### Scenario: Same-count extension with same strong-keyword count is NOT classified as weakening
- **GIVEN** the original line is `"SHALL validate inputs."` (one SHALL)
- **AND** the modified line is `"SHALL validate inputs and SHOULD log requests."` (still one SHALL, one new SHOULD)
- **WHEN** the mixed-line corner-case branch evaluates the pair
- **THEN** the pairing similarity exceeds the mixed-line threshold
- **AND** `strong_count == weak_line_strong_count == 1` triggers the same-count in-place path
- **AND** the prefixes (`""` vs `""`, since both lines start with the keyword) are both empty — the pairing is accepted ONLY if it ALSO represents an in-place replacement, which here it does not because no strong keyword was removed
- **AND** the detector SHALL NOT emit a WEAKENING violation
- **AND** the candidate is recorded in the sub-threshold diagnostic log if relevant

#### Scenario: Same-count in-place rewrite IS classified as weakening
- **GIVEN** the original line is `"The agent SHALL validate inputs."`
- **AND** the modified line is `"The agent SHOULD validate inputs and SHALL log requests."` (one SHALL replaced by a SHOULD in the original position; a new SHALL was added later)
- **WHEN** the mixed-line corner-case branch evaluates the pair
- **THEN** `strong_count == weak_line_strong_count == 1` triggers the same-count in-place path
- **AND** `orig_prefix` (`"The agent"`) and `mixed_prefix` (`"The agent"`) tokenise to the same set, giving `prefix_score == 1.0` which clears the `0.8` threshold
- **AND** the detector emits a WEAKENING violation
- **AND** the evidence carries both `pairing_score` and `prefix_score` (rounded to 3 decimal places)

#### Scenario: Fewer-strong path needs no prefix check
- **GIVEN** the original line contains TWO `SHALL` keywords
- **AND** the modified line contains ONE `SHALL` and one `SHOULD` covering the same subject
- **WHEN** the mixed-line corner-case branch evaluates the pair
- **THEN** `weak_line_strong_count < strong_count` triggers the fewer-strong path
- **AND** the detector accepts the pairing without computing `prefix_score`
- **AND** the evidence carries `pairing_score` but `prefix_score` is omitted from `to_dict()` output

#### Scenario: Keyword-position swap is treated as structural rewrite, not weakening
- **GIVEN** the original line is `"SHALL log requests."` (prefix before SHALL is empty)
- **AND** a candidate weak line is `"log SHOULD requests."` (prefix before SHOULD is `"log"`, non-empty)
- **WHEN** the same-count in-place path runs
- **THEN** one prefix is empty and the other is non-empty, so `prefix_score` evaluates to 0
- **AND** the empty-vs-non-empty case falls through (NOT both-empty, NOT `>= 0.8`)
- **AND** the pairing is rejected — no WEAKENING violation is emitted from this candidate
- **AND** this matches the documented intent that position swaps indicate sentence restructuring rather than in-place weakening

#### Scenario: All candidate pairings are surfaced for reviewer audit
- **GIVEN** the mixed-line branch evaluated more than one candidate weak line for the same missing strong line
- **AND** at least one same-count candidate was accepted by the prefix-score guard
- **WHEN** the WEAKENING violation evidence is serialized
- **THEN** the `all_pairings` array contains an entry for every evaluated candidate
- **AND** each accepted same-count entry carries both `pairing_score` and `prefix_score` (rounded to 3 decimal places)
- **AND** each fewer-strong-path entry carries `pairing_score` only (no `prefix_score` key)
- **AND** reviewers can read the array to understand which candidates passed which guard

### Requirement: Merge Topology Validation

In addition to spec-content checks, the guardrails system SHALL validate the **git topology** of every merge commit it inspects. This catches a class of disasters where the spec content of the merge commit might look fine but the underlying commit graph has been corrupted (e.g. the merge commit was silently dropped by a `git reset --soft HEAD~1` after an amend, or a merge was squashed / fast-forwarded into a single-parent commit).

**Two checks (both apply to every non-no-op merge):**

1. **Ancestry check** — the pre-merge SHA (`ours_before_sha`) MUST be an ancestor of the post-merge SHA (`merge_commit_sha`). If ancestry fails, the merge commit was lost and the parent-count check is skipped because the commit graph is presumed disconnected.
2. **Parent-count check** — the post-merge SHA MUST have at least `min_parents` parents (default `2`). Octopus merges with more than 2 parents are accepted. A single-parent commit at HEAD indicates the merge was squashed, fast-forwarded, or replaced.

**Opt-in fix-up depth tolerance:**

- The check accepts an optional `max_fixup_depth` (default `0` — strict). When `max_fixup_depth > 0`, the detector tolerates a layout where the post-merge SHA is a single-parent commit sitting on top of a real merge commit (e.g. a guardrail-repair fix-up commit pushed after the merge).
- The detector walks back up to `max_fixup_depth` linear ancestors looking for a commit whose parent count satisfies `min_parents`. Every intermediate ancestor in the chain MUST be single-parent; encountering an initial commit (0 parents) or a non-linear shape stops the walk.
- If the walk reaches a merge commit within the allowed depth, the topology check passes. Otherwise a `CHECK_FAILURE` violation is reported including the walked chain summary.
- The default value of `0` keeps the strict behaviour for callers that have not explicitly reasoned about fix-up layouts, so a stray hook commit cannot be silently accepted.

**No-op handling and opt-out:**

- The orchestrator normally filters out the `ours_before_sha == merge_commit_sha` no-op case before invoking the topology check.
- **Equal-SHA contract violation under enforcement.** When the caller nonetheless invokes `check_merge_result` with `enforce_topology=True` AND `ours_before_sha == merge_commit_sha`, the system SHALL NOT silently treat the equal-SHA case as a passing topology (an empty diff between equal SHAs would otherwise yield `passed=True`). Instead it SHALL emit a `CHECK_FAILURE` topology violation with `topology_check="equal_sha"` so that a caller that accidentally passed the pre-merge SHA twice — or otherwise failed to provide a real post-merge SHA — sees the inconsistency rather than receiving a false green light. In this branch the standard ancestry and parent-count sub-checks SHALL be skipped (they are redundant against equal SHAs). The legitimate no-op path is expected to be filtered BEFORE invoking `check_merge_result`; reaching the check with equal SHAs and topology enforcement on is treated as a contract violation by the caller.
- Test harnesses that want to exercise only the spec-diff content checks (without setting up a real merge commit) MAY pass `enforce_topology=False` to `check_merge_result` to skip the topology assertions. Production callers SHOULD leave topology enforcement enabled.

**Violation reporting:**

- Topology violations are reported as `CHECK_FAILURE` violations with `file_path="N/A"` (since they describe the commit graph, not a specific file).
- The violation's structured evidence SHALL include the relevant SHAs, parent count, `min_parents`, `max_fixup_depth` (when applicable), and a `topology_check` discriminator identifying the failing sub-check (`"ancestry"`, `"parent_count"`, `"parent_count_with_fixup"`, or `"equal_sha"`).
- When the git tooling itself fails (e.g., `git merge-base --is-ancestor` or `git rev-list --parents` cannot be executed), the system SHALL emit a `CHECK_FAILURE` violation describing which git invocation failed; it MUST NOT silently treat a tooling failure as a passing topology check. The `CHECK_FAILURE` violation's evidence MAY additionally include `exception_type` and `exception_msg` (the same fields documented under "Guardrail Violation Reporting") so reviewers can see the underlying git tooling exception class and message inline. Note that `CHECK_FAILURE` (used for topology and git-tooling failures) is a distinct violation type from `CHECK_INCOMPLETE` (used for spec-file read failures and changed-file enumeration failures); both may carry `exception_type` / `exception_msg` evidence, but they describe different failure modes and SHALL NOT be conflated by consumers.

#### Scenario: Lost merge commit is caught by ancestry check
- **GIVEN** a merge was created and then `git reset --soft HEAD~1` discarded the merge commit, leaving the new HEAD on a commit that is no longer a descendant of `ours_before_sha`
- **WHEN** `check_merge_result(ours_before_sha, current_head_sha)` runs with topology enforcement enabled
- **THEN** `git merge-base --is-ancestor ours_before_sha current_head_sha` returns non-zero
- **AND** the system emits a `CHECK_FAILURE` violation with `topology_check="ancestry"` and the two SHAs in evidence
- **AND** the parent-count check is skipped to avoid noise on a disconnected graph

#### Scenario: Squashed or fast-forwarded merge is caught by parent-count check
- **GIVEN** the post-merge HEAD is a single-parent commit (the merge was squashed or fast-forwarded)
- **WHEN** the topology check runs with `max_fixup_depth=0` (strict, the default)
- **THEN** the system emits a `CHECK_FAILURE` violation with `topology_check="parent_count"`
- **AND** the evidence includes `parent_count=1` and `min_parents=2`
- **AND** the violation message names the post-merge SHA and notes that HEAD is not a merge commit

#### Scenario: Fix-up commit on top of a real merge is accepted when depth allows
- **GIVEN** a merge commit was created and a follow-up single-parent fix-up commit (e.g. a guardrail-repair commit) was pushed on top
- **AND** the caller passes `topology_max_fixup_depth=1` (or greater)
- **WHEN** the topology check runs
- **THEN** the parent-count check walks one linear ancestor, finds the merge commit, and the topology check passes
- **AND** no violation is reported

#### Scenario: Fix-up chain that does not reach a merge is rejected
- **GIVEN** the post-merge HEAD and its `max_fixup_depth` linear ancestors are ALL single-parent commits (no merge commit is reachable within the allowed depth)
- **WHEN** the topology check runs
- **THEN** the system emits a `CHECK_FAILURE` violation with `topology_check="parent_count_with_fixup"`
- **AND** the message includes a chain summary of `(sha parents=N)` entries describing each walked ancestor
- **AND** the evidence includes `parent_count`, `min_parents`, and `max_fixup_depth`

#### Scenario: Topology enforcement can be disabled for content-only tests
- **GIVEN** a test wants to exercise the spec-diff content checks against two arbitrary commits that are not in a real merge relationship
- **WHEN** the test calls `check_merge_result(pre_sha, post_sha, enforce_topology=False)`
- **THEN** the topology checks (ancestry, parent-count, fix-up walk) are skipped entirely
- **AND** only the per-file spec-diff content checks run
- **AND** production callers leave `enforce_topology=True` (the default) so the topology assertions are always evaluated for real merges

#### Scenario: Git tooling failure during topology check is reported, not swallowed
- **GIVEN** a `git merge-base --is-ancestor` or `git rev-list --parents` invocation raises an exception or returns an unexpected non-zero status that is not a clean ancestry/parent-count signal
- **WHEN** the topology check runs
- **THEN** the system emits a `CHECK_FAILURE` violation describing the failing git command and the exception or stderr text
- **AND** the check does NOT report a passing topology — a tooling failure is treated as an inconclusive result, not a success

#### Scenario: Equal pre/post SHAs under topology enforcement are rejected, not silently passed
- **GIVEN** a caller invokes `check_merge_result(ours_before_sha, merge_commit_sha)` with `enforce_topology=True`
- **AND** `ours_before_sha == merge_commit_sha` (the caller accidentally passed the pre-merge SHA twice, or otherwise failed to supply a real post-merge SHA)
- **WHEN** the topology check runs
- **THEN** the system SHALL NOT silently treat the equal-SHA case as a passing topology (an empty diff would otherwise yield `passed=True`)
- **AND** it emits a `CHECK_FAILURE` topology violation with `topology_check="equal_sha"` and `file_path="N/A"`
- **AND** the violation's evidence includes `pre_sha` and `post_sha` (which are equal)
- **AND** the standard ancestry and parent-count sub-checks are skipped for this branch (they are redundant against equal SHAs)
- **AND** the report's `passed` flag is `False`, surfacing the caller-side contract violation rather than masking it

### Requirement: Post-Update Spec Index Rebuild and New-Spec Verification

After the `update_spec` step writes spec changes to disk, the system SHALL rebuild the on-disk spec index for every touched spec AND verify that any spec declared as `new_spec` in the `spec_decisions` output has actually materialised on disk in a structurally valid form. This catches LLM hallucinations (a spec name was declared but no file was written) and partial writes (a file exists but is empty, unparsable, or missing Requirements) before downstream steps consume a corrupt index.

**Rebuild contract:**

1. After `update_spec` produces its outputs, the step SHALL compute the set of touched spec names as the union of `specs_updated[*].spec_name` and `spec_decisions[*].target_spec`.
2. For every touched spec name, the step SHALL call the spec index's `rebuild_for(spec_name)` so the in-memory index reflects the freshly written on-disk content, and SHALL `save()` the index so the next load picks up the changes (not a stale snapshot).
3. The rebuild SHALL hold an exclusive advisory file lock (`flock`) on the spec-index lock file across the entire load → rebuild → verify → save sequence, so two concurrent writers cannot race and overwrite each other's fresher index. The lock SHALL be acquired BEFORE loading the index, not after, to prevent stale-data races.
4. To avoid reentrant deadlock, the locked path SHALL instantiate `SpecIndex` directly and call `load()` / `build()` / `save()` itself, NOT call `load_or_build()` which would try to acquire the same lock again.
5. If the advisory lock cannot be acquired (e.g., `fcntl` unavailable on the platform, or `open(lock_file)` fails with `OSError`), the step SHALL log a warning AND fall back to performing the rebuild without the lock — the rebuild MUST still happen even on platforms without `fcntl`.
6. The lock SHALL be released (`LOCK_UN`) on every exit path from the locked region (success, verification failure, exception), and a failure to release SHALL be swallowed silently (best-effort cleanup) so a stuck lock cannot mask the underlying step result.

**New-spec verification contract (applies to every `spec_decisions` entry with `decision == "new_spec"`):**

1. The step SHALL resolve the target file path as `<specs_dir>/<target_spec>/spec.md` using the same `ContextBuilder.specs_dir` the rest of the system uses, so verification is consistent with reads.
2. **File existence check** — if the target file does not exist, the step SHALL fail with `StepStatus.FAILED` and set `step.error_message` to `"Spec update failed: declared new spec '<target>' but <path> does not exist."`
3. **Parse check** — if the target file exists but raises any exception when parsed by `parse_spec(...)`, the step SHALL fail with `step.error_message` set to `"Spec update failed: declared new spec '<target>' exists but is unparsable."`
4. **Requirements check** — if the parsed spec has zero Requirements (`parsed.requirements` is empty), the step SHALL fail with `step.error_message` set to `"Spec update failed: declared new spec '<target>' has no Requirements."`
5. **Header check** — if the parsed spec has an empty header OR a header whose stripped length is less than 10 characters, the step SHALL fail with `step.error_message` set to `"Spec update failed: declared new spec '<target>' has an empty or very short header."`
6. Each verification failure SHALL log an error describing the specific defect (missing file, parse error, missing Requirements, short header) BEFORE returning `StepStatus.FAILED`, so operators can diagnose which check fired without re-running.
7. Verification failures discovered after the lock is acquired SHALL release the lock before returning `FAILED`.

**Best-effort outer wrapper:**

- The entire rebuild + verification block is wrapped in a top-level `try/except` so that infrastructure-level errors (e.g., index module import failure, unexpected exceptions inside `rebuild_for`) do NOT mask a successful spec update — such errors SHALL be logged with `exc_info=True` and the step SHALL still return `StepStatus.COMPLETED`. This best-effort wrapper applies ONLY to unexpected infrastructure errors; declared-but-missing/unparsable/empty/headerless new specs are NOT best-effort and MUST cause `StepStatus.FAILED` per the verification contract above.

#### Scenario: Index is rebuilt for every touched spec after update_spec
- **GIVEN** `update_spec` reports `specs_updated=[{spec_name: "foo"}]` and `spec_decisions=[{target_spec: "bar", decision: "append"}]`
- **WHEN** the post-update rebuild block runs
- **THEN** the in-memory `SpecIndex` is loaded (or built), `rebuild_for("foo")` and `rebuild_for("bar")` are called, and the index is saved to disk
- **AND** a subsequent load of the index reflects the freshly written spec content, not a stale snapshot

#### Scenario: Declared new spec does not exist on disk
- **GIVEN** `spec_decisions` contains `{decision: "new_spec", target_spec: "nonexistent-spec"}`
- **AND** no file was written at `se3/specs/nonexistent-spec/spec.md`
- **WHEN** the verification block runs
- **THEN** the step logs an error naming the declared spec and the missing path
- **AND** `step.error_message` is set to `"Spec update failed: declared new spec 'nonexistent-spec' but <path> does not exist."`
- **AND** the step returns `StepStatus.FAILED`
- **AND** the advisory lock (if held) is released before returning

#### Scenario: Declared new spec exists but is unparsable
- **GIVEN** `spec_decisions` declares a `new_spec` target
- **AND** the file exists on disk but raises an exception when passed to `parse_spec(...)`
- **WHEN** the verification block runs
- **THEN** the step logs an error containing the parse exception
- **AND** `step.error_message` is set to `"Spec update failed: declared new spec '<target>' exists but is unparsable."`
- **AND** the step returns `StepStatus.FAILED`

#### Scenario: Declared new spec parses but has no Requirements
- **GIVEN** the declared new spec file exists and parses successfully
- **AND** the parsed result has zero `requirements`
- **WHEN** the verification block runs
- **THEN** `step.error_message` is set to `"Spec update failed: declared new spec '<target>' has no Requirements."`
- **AND** the step returns `StepStatus.FAILED`

#### Scenario: Declared new spec has an empty or very short header
- **GIVEN** the declared new spec file exists, parses, and has Requirements
- **AND** `parsed.header_text` is empty or strips to fewer than 10 characters
- **WHEN** the verification block runs
- **THEN** `step.error_message` is set to `"Spec update failed: declared new spec '<target>' has an empty or very short header."`
- **AND** the step returns `StepStatus.FAILED`

#### Scenario: Advisory lock prevents concurrent index races
- **GIVEN** two `update_spec` runs touch the spec index nearly simultaneously
- **WHEN** each run enters the post-update rebuild block
- **THEN** one run acquires the exclusive `flock` on `se3/cache/spec-index.json.lock` before loading the index, and the other run waits
- **AND** the second run only loads, rebuilds, and saves the index after the first run has released the lock
- **AND** neither run overwrites the other run's freshly written index with stale data

#### Scenario: Lock unavailable — rebuild still happens without coordination
- **GIVEN** the advisory lock cannot be acquired (e.g., `fcntl` is unavailable on the platform, or opening the lock file raises `OSError`)
- **WHEN** the post-update rebuild block runs
- **THEN** the system logs a warning that the file lock is not available
- **AND** the rebuild still loads, rebuilds touched specs, and saves the index without coordination
- **AND** the step does NOT return `FAILED` solely because the lock was unavailable

#### Scenario: Infrastructure error during rebuild does not fail a successful spec update
- **GIVEN** `update_spec` successfully wrote spec changes to disk and recorded no `new_spec` decisions
- **WHEN** an unexpected exception (e.g., import error, internal failure inside `rebuild_for`) is raised inside the rebuild block
- **THEN** the outer `try/except` logs the failure with `exc_info=True`
- **AND** the step still returns `StepStatus.COMPLETED`
- **AND** the spec changes already written to disk are NOT rolled back

### Requirement: Deprecated `check()` Backward-Compatibility Wrapper

The `MergeGuardrailsCheck` class SHALL expose a deprecated `check(ours_before_sha, merge_commit_sha)` method that forwards directly to `check_merge_result(ours_before_sha, merge_commit_sha)` with default arguments (topology enforcement enabled, `max_fixup_depth=0`). This wrapper exists solely to preserve backward compatibility with older callers that invoked `check()` before `check_merge_result` was introduced as the canonical entry point.

**Wrapper contract:**

1. The `check()` method SHALL accept the same two positional arguments — `ours_before_sha` and `merge_commit_sha` — as `check_merge_result`'s required positional parameters.
2. The wrapper SHALL return the same `GuardrailReport` that `check_merge_result` would return for the same inputs under default options.
3. The wrapper SHALL be marked deprecated in its docstring; new code SHALL prefer `check_merge_result` directly so it can opt into `enforce_topology` / `topology_max_fixup_depth` controls.
4. The wrapper SHALL NOT duplicate any check logic — it forwards unconditionally to `check_merge_result` so the two entry points cannot drift apart.

#### Scenario: Legacy `check()` call forwards to `check_merge_result`
- **GIVEN** a legacy caller invokes `MergeGuardrailsCheck(project_root).check(pre_sha, post_sha)`
- **WHEN** the wrapper runs
- **THEN** it returns the same `GuardrailReport` that `check_merge_result(pre_sha, post_sha)` would return
- **AND** topology enforcement is applied with the default `max_fixup_depth=0`
- **AND** the wrapper does not implement any independent check logic

### Requirement: New Spec vs Append Criteria

Before adding a new Requirement to an existing spec, the agent SHALL explicitly evaluate whether the content should instead become a new spec. This decision is made during the `update_spec` step and SHALL be recorded in a structured `spec_decisions` output.

**Four evaluation criteria (ALL must be met to append; if ANY fails, create a new spec):**

1. **Conceptual Independence** — The new content shares the same conceptual domain as the existing spec. It is about the same subsystem, mechanism, or abstraction level. If the content introduces a fundamentally different concept (e.g., "how to format JSON" into a spec about "error handling patterns"), it fails this test.

2. **Dependency Direction** — The new content does NOT cause existing Requirements in the spec to depend on it. If adding the Requirement would force older Requirements to reference or assume the new behavior (e.g., an existing "Retry Logic" Requirement now needs to know about a new "Circuit Breaker" Requirement), the dependency direction is wrong and a new spec is needed.

3. **Naming Test** — The new Requirement can be naturally named under the existing spec's title. A reader encountering the Requirement name should not be surprised to find it in this spec. If the name feels like it belongs in a different category, it fails this test.

4. **Cross-Scenario Reusability** — The new content is NOT expected to be referenced by multiple unrelated capabilities. If the content is a cross-cutting concern (e.g., "Authentication", "Configuration Format", "Versioning Rules") that multiple specs will need to cite, it should be its own spec to avoid circular references and provide a single source of truth.

**Decision rule:**
- If ALL four criteria pass → **append** the new Requirement to the existing spec.
- If ANY criterion fails → **create a new spec** at `se3/specs/<new_name>/spec.md` with standard structure (Purpose, Requirements, Scenarios).

**Enforcement:**
- The `update_spec` step prompt SHALL include these four criteria explicitly.
- The LLM SHALL output a `spec_decisions` array where each entry documents the decision for every new Requirement.
- The default spec loading mode for `update_spec` is `full_spec` so that the LLM can see all existing spec names and avoid naming collisions.

**`spec_decisions` entry schema:**

Each entry in the `spec_decisions` array SHALL be a JSON object with the following fields:

1. `requirement_name` (string, required) — The human-readable name of the new Requirement being placed (e.g., `"Step Retry Backoff Strategy"`). This lets downstream consumers correlate the decision back to the actual Requirement heading written into the spec.
2. `decision` (string, required) — One of `"append"` or `"new_spec"`, indicating which branch of the decision rule was taken.
3. `target_spec` (string, required) — The kebab-case spec directory name under `se3/specs/` where the Requirement was placed. For `append`, this is the name of the existing spec; for `new_spec`, this is the name of the newly created spec directory.
4. `reasoning` (string, required) — A brief human-readable justification that references which of the four criteria (Conceptual Independence, Dependency Direction, Naming Test, Cross-Scenario Reusability) passed or failed and why the chosen decision follows. The reasoning text supports auditing the LLM's decision without re-running the step.

The `spec_decisions` array SHALL be present in the `update_spec` JSON output whenever a new Requirement is added. If only existing Requirements were modified, the array SHALL be empty. If no spec updates are needed at all, both `specs_updated` and `spec_decisions` SHALL be empty arrays.

#### Scenario: `spec_decisions` entry carries requirement name and reasoning
- **GIVEN** the `update_spec` step has added a new Requirement to an existing spec
- **WHEN** the step emits its JSON summary
- **THEN** the corresponding `spec_decisions` entry contains `requirement_name`, `decision`, `target_spec`, AND `reasoning` fields
- **AND** `requirement_name` matches the heading text of the new Requirement as written into the spec file
- **AND** `reasoning` explicitly references which of the four criteria passed or failed to justify the chosen `decision`
- **AND** downstream consumers can audit the decision from the JSON alone without re-reading the spec or re-running the LLM

#### Scenario: Typical append — related requirement in same domain
- **GIVEN** the `flow-engine` spec already contains Requirements about step execution and state transitions
- **WHEN** a new Requirement about "step retry backoff strategy" is proposed
- **THEN** all four criteria pass:
  - Conceptual Independence: same domain (flow engine mechanics)
  - Dependency Direction: existing steps do not need to reference backoff
  - Naming Test: "Step Retry Backoff Strategy" fits naturally in flow-engine
  - Cross-Scenario Reusability: only flow-engine references it
- **AND** the decision is **append** to flow-engine

#### Scenario: Typical new spec — conceptually independent subsystem
- **GIVEN** the project has specs for `flow-engine`, `se3-config`, and `spec-guardrails`
- **WHEN** implementing a new "Issue Discovery" subsystem with its own data model, lifecycle, and UI
- **THEN** Criteria 1 fails (different concept from all existing specs)
- **AND** Criteria 4 fails (multiple other specs will reference issue-discovery rules)
- **AND** the decision is **new spec** — create `se3/specs/issue-discovery/spec.md`

#### Scenario: Boundary case — naming test fails but others pass
- **GIVEN** the `se3-config` spec governs YAML configuration file semantics
- **WHEN** a new Requirement about "CLI color theme configuration" is proposed
- **THEN** Criteria 1 passes (both are about configuration)
- **AND** Criteria 2 passes (existing config Requirements do not depend on color themes)
- **AND** Criteria 4 passes (only se3-config consumers care)
- **BUT** Criteria 3 fails — "CLI Color Theme Configuration" is surprising under a spec titled "se3-config" which is about framework configuration, not UI appearance
- **AND** the decision is **new spec** — create `se3/specs/ui-customization/spec.md` (or equivalent)
