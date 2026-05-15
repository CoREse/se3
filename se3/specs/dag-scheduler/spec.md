<!-- spec-format: v1 -->
# dag-scheduler Specification

## Purpose

The dag-scheduler subsystem provides the event-driven parallel scheduler used by the implement step's DAG execution strategy. It transforms a list of task groups (with `depends_on` relationships) into a validated directed acyclic graph, plans relay-based worktree reuse via `classify_chains()`, and executes groups concurrently through a `ThreadPoolExecutor` so that each group starts as soon as its dependencies clear. It also exposes a transitive-reduction utility that strips redundant `depends_on` edges to minimize false serialization in the planned DAG.

## Requirements

### Requirement: Relay Plan Classification

`classify_chains(groups)` shall analyze the dependency topology and produce a `RelayPlan` describing, for each group, whether it relays from a predecessor's worktree, forks a new worktree from a predecessor's branch, or starts from a fresh worktree as a root. The plan also records leaf nodes (no downstream) and convergence points (multiple predecessors).

#### Scenario: Empty group list
- **WHEN** `classify_chains([])` is called
- **THEN** an empty `RelayPlan` is returned with empty `relay_map`, `fork_from`, `convergence_points`, `leaf_nodes`, and `root_nodes`

#### Scenario: Root nodes have no predecessor
- **WHEN** a group has no entries in `depends_on`
- **THEN** it is added to `root_nodes`
- **AND** `relay_map[gid]` is set to `None` (signaling a new worktree must be created)

#### Scenario: Leaf nodes have no downstream
- **WHEN** a group is not referenced as a dependency by any other group
- **THEN** it is added to `leaf_nodes` so the executor knows to merge it back to the original branch

#### Scenario: Single-predecessor relay
- **WHEN** a group has exactly one predecessor `p` and the group is `p`'s heir (the child of `p` with the smallest `group_order`)
- **THEN** `relay_map[gid] = p` (it reuses `p`'s worktree)
- **AND** the group is not added to `fork_from`

#### Scenario: Single-predecessor fork
- **WHEN** a group has exactly one predecessor `p` but is not `p`'s heir
- **THEN** `relay_map[gid] = None`
- **AND** `fork_from[gid] = p` (a new worktree must be forked from `p`'s branch)

#### Scenario: Convergence point with relayable predecessor
- **WHEN** a group has multiple predecessors and at least one names this group as its heir
- **THEN** the relaying predecessor with the smallest `group_order` is chosen as `primary_predecessor`
- **AND** `relay_map[gid]` is set to that primary
- **AND** all other predecessors become `secondary_predecessors`, sorted by `group_order`
- **AND** an entry is added to `convergence_points`

#### Scenario: Convergence point with no relayable predecessor
- **WHEN** a group has multiple predecessors but is no predecessor's heir
- **THEN** the predecessor with the smallest `group_order` is chosen as primary
- **AND** `relay_map[gid] = None`
- **AND** `fork_from[gid]` is set to that primary
- **AND** the remaining predecessors are recorded as `secondary_predecessors`

### Requirement: Heir Selection By group_order

For each predecessor with one or more children, the scheduler shall designate the child with the smallest `group_order` as its heir (the relay successor). Missing `group_order` defaults to 0.

#### Scenario: Multiple children with distinct order
- **WHEN** a predecessor has children with `group_order` values 2 and 5
- **THEN** the child with order 2 is its heir

#### Scenario: Predecessor with no children
- **WHEN** a node has no entries in the forward adjacency
- **THEN** its heir is `None`

### Requirement: Linear-Chain Detection

A helper `_relay_plan_is_linear(plan)` shall return `True` only when the plan describes a single linear chain (no forks and exactly one root), so callers can short-circuit DAG-parallel execution.

#### Scenario: Linear chain
- **WHEN** `fork_from` is empty and `root_nodes` contains exactly one element
- **THEN** the function returns `True`

#### Scenario: Empty plan
- **WHEN** the plan has zero roots
- **THEN** the function returns `False`

#### Scenario: Branching plan
- **WHEN** `fork_from` is non-empty or there is more than one root
- **THEN** the function returns `False`

### Requirement: DAG Construction and Validation

`DAGScheduler.__init__` shall build forward adjacency, reverse-dependency, and in-degree maps from the input groups and validate the graph by performing a Kahn topological sort.

#### Scenario: Missing group_id
- **WHEN** a group lacks both `group_id` and `name`
- **THEN** construction raises `ValueError` indicating a missing group_id

#### Scenario: Duplicate group_id
- **WHEN** two groups share the same `group_id`
- **THEN** construction raises `ValueError` reporting the duplicate

#### Scenario: Unknown dependency
- **WHEN** a group's `depends_on` references a group_id not present in the input
- **THEN** construction raises `ValueError` naming the dependent and the unknown id

#### Scenario: Cycle detected
- **WHEN** the dependency relationships form a cycle
- **THEN** `_kahn_topo_sort` returns fewer ids than groups
- **AND** construction raises `ValueError` ("Cycle detected in DAG")

#### Scenario: Successful build
- **WHEN** all dependencies resolve and no cycle exists
- **THEN** `_topo_order` contains every group_id exactly once in a valid topological order

### Requirement: Event-Driven Parallel Execution

`DAGScheduler.run(execute_fn)` shall execute groups concurrently in a `ThreadPoolExecutor` (bounded by `max_workers`), starting a group as soon as all its predecessors have completed successfully and producing results in topological order.

#### Scenario: Empty group map
- **WHEN** the scheduler was constructed with no groups
- **THEN** `run()` returns an empty list without creating an executor

#### Scenario: Root groups submitted first
- **WHEN** `run()` begins
- **THEN** every group whose dependencies are already satisfied (initially, those with `in_degree == 0`) is submitted to the executor immediately

#### Scenario: Downstream group unblocks on completion
- **WHEN** a group finishes successfully and one of its downstream groups now has all dependencies in `completed`
- **THEN** that downstream group is submitted from within the completion callback before notifying waiters

#### Scenario: execute_fn signature
- **WHEN** the scheduler submits a group
- **THEN** `execute_fn` is invoked with `(group_dict, deps_results, relay_context)`
- **AND** `deps_results` contains only direct-dependency `GroupResult` entries

#### Scenario: Result ordering
- **WHEN** `run()` returns
- **THEN** the returned list contains one `GroupResult` per group in the topological order recorded at construction
- **AND** if a group is neither completed nor skipped, a defensive `GroupResult.failed(..., "Unknown state")` is emitted in its position

#### Scenario: Main loop synchronization
- **WHEN** any group is still pending or running
- **THEN** the main thread waits on a `threading.Condition` with a 1-second timeout
- **AND** is notified via `condition.notify_all()` from each completion callback

### Requirement: Failure Handling and Propagation

The scheduler shall capture both raised exceptions and `GroupResult.status == "failed"` outcomes, record them in `completed`, and propagate failure to all transitive downstream groups by marking them `skipped`.

#### Scenario: Group raises an exception
- **WHEN** `future.exception()` is non-None for a completed group
- **THEN** a `GroupResult.failed(group_id, "<ExcType>: <message>")` is stored
- **AND** the group is added to the `failed` set
- **AND** failure is propagated downstream

#### Scenario: Group returns failed status
- **WHEN** `execute_fn` returns a `GroupResult` with `status == "failed"`
- **THEN** the group is added to `failed`
- **AND** failure is propagated to all transitive downstream groups

#### Scenario: Downstream skip propagation
- **WHEN** failure propagation visits a downstream group still in `pending`
- **THEN** the group is removed from `pending`
- **AND** a `GroupResult.skipped(downstream)` entry is added to `skipped` (with `status="skipped"`, `completion_status="failed"`, and an "upstream dependency failed" error)
- **AND** propagation continues recursively through that group's adjacency

#### Scenario: Skip is idempotent
- **WHEN** a downstream group is already in `skipped` or `failed`
- **THEN** propagation skips it without re-emitting a result

### Requirement: Relay Context Construction

Before submitting each group, the scheduler shall build a `RelayContext` describing how the group should acquire its worktree, based on the relay plan and already-completed dependency results.

#### Scenario: No relay plan provided
- **WHEN** the scheduler was constructed with `relay_plan=None`
- **THEN** every group receives a default `RelayContext()` with all fields unset

#### Scenario: Relay from predecessor
- **WHEN** `relay_map[gid]` references a completed predecessor
- **THEN** the context's `worktree_path` and `branch_name` are copied from the predecessor's `GroupResult`

#### Scenario: Fork branch
- **WHEN** `gid` appears in `fork_from` and the named predecessor is completed with a branch name
- **THEN** `is_fork=True` and `fork_source_branch` is set to that predecessor's branch
- **AND** `worktree_path` and `branch_name` remain `None`

#### Scenario: Convergence merges
- **WHEN** `gid` is a convergence point
- **THEN** for each secondary predecessor that is completed with a branch name, that branch name is appended (in `group_order` sequence) to `convergence_merges`

### Requirement: Fallback-Leaf Identification

After `run()` completes, `get_fallback_leaves()` shall return the sorted ids of completed groups whose downstream is entirely non-completed and which are not already normal leaves — so the orchestrator can merge their work back to the original branch.

#### Scenario: No results yet
- **WHEN** `get_fallback_leaves()` is called before `run()` has populated `_run_results`
- **THEN** it returns an empty list

#### Scenario: Normal leaf excluded
- **WHEN** a completed group is in `relay_plan.leaf_nodes`
- **THEN** it is excluded from the fallback list (the standard leaf-merge path handles it)

#### Scenario: Completed group with all downstream non-completed
- **WHEN** a completed, non-leaf group has direct downstream groups but none of them are in the completed set
- **THEN** it is included in the fallback list

#### Scenario: Completed group with no downstream and not in leaf_nodes
- **WHEN** the adjacency for a completed group is empty and it is not a normal leaf
- **THEN** it is treated defensively as a fallback leaf

#### Scenario: Output sorted
- **WHEN** multiple fallback leaves exist
- **THEN** the returned list is sorted lexicographically by group_id

### Requirement: Topological Merge Order

`topological_merge_order()` shall expose the full Kahn topological order of group_ids so that callers can merge branches back in dependency-respecting sequence.

#### Scenario: Returns all groups in topo order
- **WHEN** `topological_merge_order()` is called after `run()`
- **THEN** it returns a copy of `_topo_order` containing every group_id (callers filter by completion status themselves)

### Requirement: GroupResult Data Model

`GroupResult` shall represent the outcome of executing a single task group, with class-method constructors for the common `skipped` and `failed` shapes.

#### Scenario: Default fields
- **WHEN** a `GroupResult` is constructed with only `group_id` and `status`
- **THEN** `files_changed`, `tests_added`, `incomplete_tasks`, `restricted_edits` default to empty lists
- **AND** `test_mapping` defaults to an empty dict
- **AND** `completion_status` defaults to `"complete"`
- **AND** `summary` defaults to `""`
- **AND** `branch_name` defaults to `""`
- **AND** `worktree_path` defaults to `None`
- **AND** `estimated_test_duration` defaults to `None`
- **AND** `error` defaults to `None`

#### Scenario: Branch and worktree fields populated by execute_fn
- **WHEN** an `execute_fn` returns a `GroupResult` with `branch_name` and `worktree_path` set
- **THEN** those fields are available to the scheduler for relay-context construction
- **AND** the predecessor's `branch_name` and `worktree_path` propagate to the heir's `RelayContext`

#### Scenario: Skipped factory
- **WHEN** `GroupResult.skipped(group_id)` is called
- **THEN** the result has `status="skipped"`, `completion_status="failed"`, and `error="Skipped: upstream dependency failed"`

#### Scenario: Failed factory
- **WHEN** `GroupResult.failed(group_id, error)` is called
- **THEN** the result has `status="failed"`, `completion_status="failed"`, and `error` set to the supplied message

### Requirement: Transitive Edge Reduction

`transitive_reduce(groups)` shall return a deep copy of the input groups with redundant `depends_on` edges removed. An edge u→v is redundant when there exists a path from u to v of length ≥ 2 in the original graph. The input list must never be mutated.

#### Scenario: Empty input
- **WHEN** `transitive_reduce([])` is called
- **THEN** an empty list is returned

#### Scenario: Group with single or zero dependencies
- **WHEN** a group has `len(depends_on) <= 1`
- **THEN** its `depends_on` is left unchanged (no edge can be redundant against itself)

#### Scenario: Redundant edge removed
- **WHEN** group B depends on A, group C depends on A and B, and the path A→B→C exists
- **THEN** the edge A→C is removed from C's `depends_on`
- **AND** C's `depends_on` still contains B

#### Scenario: Path of length 1 only
- **WHEN** the only path from u to v is the direct edge u→v
- **THEN** `_has_long_path` returns `False` and u is preserved in v's `depends_on`

#### Scenario: Other group keys preserved
- **WHEN** the input groups contain fields beyond `group_id` and `depends_on`
- **THEN** those fields are preserved verbatim in the returned deep copy

#### Scenario: Input not mutated
- **WHEN** `transitive_reduce` is called
- **THEN** the original `groups` list and its dicts remain unchanged (deep copy semantics)

### Requirement: Composite Run-Results State

After `run()` finishes, the scheduler shall persist a single `_run_results` mapping that merges both the `completed` and `skipped` result dictionaries, so post-run queries (notably `get_fallback_leaves`) can inspect every group's terminal status from one place.

#### Scenario: Populated after run
- **WHEN** `run()` returns
- **THEN** `self._run_results` is set to `{**completed, **skipped}`
- **AND** every group that finished or was skipped has an entry keyed by its `group_id`

#### Scenario: Status partitioning preserved
- **WHEN** `_run_results` is inspected
- **THEN** each entry's `status` field distinguishes completed (`"completed"` or `"failed"`) from skipped (`"skipped"`) groups
- **AND** `get_fallback_leaves` derives its `completed_ids` set by filtering on `r.status == "completed"`

#### Scenario: Empty before run
- **WHEN** `run()` has not been invoked (or was invoked with no groups)
- **THEN** `self._run_results` is empty
- **AND** `get_fallback_leaves()` short-circuits to an empty list

### Requirement: Module Organization

The `transitive_reduce` utility shall live in its own module separate from the scheduler implementation, so callers can import it without pulling in the executor and threading machinery of `DAGScheduler`.

#### Scenario: transitive_reduce module location
- **WHEN** the transitive-reduction utility is imported
- **THEN** it is available from `src/se3/engine/transitive_reduction.py` rather than from `dag_scheduler.py`
- **AND** its private helper `_has_long_path` is co-located in the same module

#### Scenario: Helper isolation
- **WHEN** `_has_long_path(source, target, adjacency)` is invoked
- **THEN** it performs a BFS from `source` that explicitly skips the direct `source → target` edge
- **AND** returns `True` only when an alternate path of length ≥ 2 reaches `target`

#### Scenario: Adjacency built from depends_on
- **WHEN** `transitive_reduce` constructs its forward adjacency map
- **THEN** for each group `v` and each `u` in `v["depends_on"]`, an edge `u → v` is recorded
- **AND** every `group_id` present in the input has an adjacency entry (even if it has no outgoing edges)