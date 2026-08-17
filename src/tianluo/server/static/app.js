/*
 * tianluo Control Plane — web frontend.
 *
 * Connects to the central server's `/ws/ui` WebSocket for realtime machine /
 * flow state, renders the dashboard, and drives the REST API for flow detail,
 * task publishing, and interjection/call responses.
 *
 * A running flow opens in a full-screen chat view (`#flow-view`): a sidebar
 * carries Overview / Steps / Machine, the conversation is the scrollable main
 * body, every human-intervention point (pending MCP call, interjection, retry
 * decision, CLI confirmation) is surfaced as a prominent intervention item,
 * and a docked reply box is the single, always-present way to respond.
 */
"use strict";

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const state = {
  // ---- Auth / owner identity ----
  // The resolved owner for this browser session, as returned by
  // /api/auth/me|login|breakglass: {owner_id, display_name, is_admin, provider}.
  // null until authenticated; cleared on logout / a 401 from any /api/* call.
  identity: null,
  // Coarse auth state machine value ("unknown" | "login" | "authed"), driven by
  // the pure `nextAuthState` transition. Gates whether the app surface or the
  // login gate is shown.
  authState: "unknown",
  // Daemon keys owned by the current owner (metadata only — the plaintext is
  // shown once at creation and never stored here).
  daemonKeys: [],
  // Manageable owners shown in the admin-only user-management panel (a
  // whitelist of non-sensitive fields; the break-glass subject is filtered
  // server-side and never appears here).
  users: [],
  // Machine whose registered-project dialog is open (null when closed), the
  // registry entries last rendered in it ([{path, exists, active}]), and the
  // path awaiting the second stage of the remove confirmation.
  projectMachineId: null,
  projectEntries: [],
  projectRemoveTarget: null,

  // Attachment rows currently shown under each prompt input, keyed by the strip
  // element's id ("nt-attachments" / "flow-attachments"), plus the monotonic
  // counter behind the placeholder tokens. The rows are a VIEW of what the
  // textarea text already carries — the text is the source of truth (see the
  // upload helper block) — so nothing here is ever consulted at submit time.
  // The counter is global rather than per-strip so two concurrent uploads can
  // never collide on a token even across scopes.
  uploadAttachments: {},
  uploadSeq: 0,
  // The machine+project each strip's rows were uploaded into, same keys as
  // uploadAttachments. Kept because the paths in the text are relative to THAT
  // project root only: if the New Task form is re-pointed at another one, the
  // rows and their text must go with it (see discardAttachments).
  uploadTargets: {},

  machines: [],           // [{machine_id, hostname, online, flows: [...]}]
  selectedMachineId: null,
  selectedFlowId: null,   // flow open in the full-screen flow view
  flowDetail: null,       // last fetched flow object (for the open flow view)
  // Backend usage payload delivered over the WS history_data path for the open
  // flow (full frames only). Fallback badge source when the snapshot's compact
  // `usage_summary` has not arrived yet; reset on flow open/close.
  flowConversationUsage: null,
  flowMachineId: null,    // machine id owning the open flow
  flowConversationRecords: [],   // conversation records shown in the flow view
  // Opaque progress token from the running-flow view's last REST snapshot
  // (`GET /api/history/{flow_id}`). Echoed as `?after=` on a WS-reconnect
  // re-fetch so the server can serve only the delta that accrued during the
  // outage instead of the whole bundle. null = no held progress → the next
  // fetch sends no incremental param and gets a full snapshot. Reset when a
  // different flow opens, the view closes, or an incompatible `mode: full`
  // WS push replaces the cached bundle (the old token no longer pins it).
  // Kept independent from `historyProgress` so the two views never cross-feed.
  flowConversationProgress: null,
  // Bundle content signature (a short version/hash) accompanying the last REST
  // snapshot for the running-flow view. Echoed as `?sig=` alongside the progress
  // token so the periodic self-heal / reconnect pull can be answered with an
  // extra-small `delivery:"not_modified"` reply when the client is provably in
  // sync — the G5 traffic-reduction win that turns "search the whole 17MB bundle
  // every 3s" into "compare a signature". null = none held → the next pull omits
  // `sig` and can only get delta/full. Reset wherever the progress token is.
  flowConversationSignature: null,
  // Monotonic view-local epoch used to invalidate an in-flight REST snapshot
  // when a WS `mode: full` push replaces the authoritative bundle.
  flowConversationEpoch: 0,
  // Persistent "the reader is following the bottom" intent for the open flow —
  // the reliable source the silent progression rebuild uses to decide stickiness
  // instead of the point-in-time frozen-DOM isNearBottom (issue #260). At the
  // discovery→analyze boundary the WS increment stalls: content lands without an
  // auto-scroll (or a large chunk arrives between the measure and the scroll), so
  // the frozen DOM reads scrollHeight-scrollTop-clientHeight>80 and the momentary
  // isNearBottom MISJUDGES a bottom-follower as scrolled-up — the rebuild then
  // takes the anchor branch and pins the old tail, jumping the view up. This flag
  // is driven by real intent signals only — a user scroll of #flow-conversation
  // (set to isNearBottom at that moment) and every programmatic scroll-to-bottom
  // (set true) — and is NOT clobbered by the untrustworthy frozen measurement, so
  // a follower who merely drifted from a stalled append still counts as following
  // and the rebuild sticks to the bottom. Reset true on open (a fresh flow scrolls
  // to bottom). true = following the bottom; false = deliberately scrolled up.
  flowConversationFollowingBottom: true,
  // Progression baseline for the cause-immune fallback refresh (see
  // maybeRefreshConversationOnProgression). Holds the last observed
  // { flowId, currentStep, currentStepIndex, status } so each new
  // /api/flows/{id} snapshot can be compared against it: a changed
  // current_step / current_step_index (step-to-step switch) OR a changed
  // status (e.g. an in-step retry, where the flow flips FAILED/PAUSED→RUNNING
  // while current_step stays the same — see the daemon FlowSnapshot fields)
  // means the flow advanced, which triggers exactly one silent full rebuild of
  // the open conversation. null = no baseline yet (the first snapshot only
  // establishes the baseline and never counts as progression).
  // Bound to a single flowId because the console shows one flow at a time;
  // reset to null on openFlowView / doCloseFlowView so a prior flow's baseline
  // can never misjudge a freshly-opened flow.
  flowProgressionMarker: null,
  // Absolute, monotonic count of WS increments that actually landed new records
  // into the OPEN flow's conversation via applyHistoryData's running-flow branch
  // (append with non-empty `fresh`, or a `mode:full` replacement). It is the
  // authoritative "the WS push path is alive for this flow" signal the grace
  // timer below reads: a real analyze-frame delivered through /ws/ui bumps it.
  // Chosen over comparing flowConversationRecords.length because that length is
  // also moved by non-WS mutations (optimistic local echo, reconcileLocalEchoes,
  // a silent full rebuild) — this counter rises ONLY on a genuine WS landing, so
  // "did the WS deliver an increment during the grace window" is a clean
  // seq0→now delta. Deliberately NOT reset on openFlowView / doCloseFlowView:
  // the grace timer is always cancelled on flow switch/close (cancelProgressionGrace),
  // so a stale prior-flow count can never be compared across flows, and an
  // absolute counter needs no per-lifecycle bookkeeping.
  flowConversationAppendSeq: 0,
  // Pending grace-timer state for the progression fallback. When the open flow
  // is observed to advance, we no longer rebuild immediately; instead we start a
  // grace window and only rebuild if the WS push failed to deliver an increment
  // within it. Unlike the original one-shot safety net, the timer now RE-ARMS
  // itself after each silent rebuild (see armProgressionGrace) and keeps pulling
  // on the grace cadence until a genuine WS increment lands — so a WS that stays
  // dead across a whole step (the #260 discovery→analyze break) still surfaces
  // mid-step content without the reader exiting/re-entering. These hold the
  // pending setTimeout id, the flow it targets, and the flowConversationAppendSeq
  // snapshot frozen when the loop was first armed (compared against the live
  // counter each time a window fires). All three are cleared by
  // cancelProgressionGrace() — on a fresh advance (rescheduled to the newest
  // step), and on openFlowView / doCloseFlowView, which is now the only way the
  // periodic loop is stopped short of a WS recovery.
  progressionGraceTimer: null,
  progressionGraceFlowId: null,
  progressionGraceAppendSeqAtSchedule: 0,
  // Grace-window length (ms) before the fallback silent rebuild fires. 5000ms is
  // far above the healthy push latency (daemon poll_interval=0.4s; an analyze
  // increment lands via /ws/ui in ~1.9s under load) yet far below the threshold
  // at which an operator would perceive a freeze. Held as a configurable state
  // field (not a hardcoded literal) so the DOM-stub tests — which use real
  // setTimeout, no fake timers — can shrink it to a few ms and cover both the
  // healthy and fallback paths quickly and deterministically.
  progressionGraceMs: 5000,
  // Monotonic request-sequence guard for refreshFlowDetail. Each /api/flows/{id}
  // fetch claims the next `flowDetailReqSeq`; a response is applied only when its
  // claimed seq is strictly greater than `flowDetailAppliedSeq` (the highest seq
  // already applied). Concurrent detail fetches (the 3s poll racing a
  // STATUS_UPDATE-triggered refresh) can resolve out of order, so a late older
  // response carrying a stale current_step must NOT overwrite state.flowDetail or
  // move the progression marker backward — without this guard it would re-trigger
  // a redundant silent refresh and let the next genuine snapshot fire yet another.
  flowDetailReqSeq: 0,
  flowDetailAppliedSeq: 0,
  // Flow-view lifecycle generation. Incremented on every openFlowView so each
  // open/close cycle has a distinct id. Each refreshFlowDetail fetch captures
  // the generation in-flight and is dropped on resolution if the view has since
  // been closed/reopened — without this, an in-flight high-seq fetch from a
  // prior lifecycle of the SAME flow would survive the selectedFlowId check
  // (same flowId), apply its stale snapshot, and bump flowDetailAppliedSeq to a
  // high value that suppresses the fresh post-reopen fetches (which restart at
  // seq 1). The seq guard only orders fetches WITHIN one lifecycle; the
  // generation guard scopes freshness ACROSS lifecycles.
  flowDetailViewGen: 0,
  // G3 periodic full-snapshot self-heal bookkeeping. `periodicSnapshotActive` is
  // true while the running-flow view's 3s detailPollTimer is running its
  // conversation self-heal (openFlowView → doCloseFlowView). It is the "the
  // periodic full snapshot now owns correctness" signal that DEMOTES the
  // progression-grace fallback: while it is true a detected advance still updates
  // the marker (activity/stall detection) but does NOT arm the grace loop, so the
  // two paths never issue duplicate full pulls — the 3s poll heals first
  // (3s < the 5s grace window) and is the single self-heal path. The DOM-free
  // progression tests never open a view, so this stays false there and the grace
  // loop still runs, keeping those suites' isolated coverage intact.
  periodicSnapshotActive: false,
  flowInterventions: [],  // intervention entries derived from pending_calls
  flowReplyTargetId: null,// id of the intervention the reply box targets
  flowInterjectRequested: false, // user clicked Interject — synth chip on
  flowInterjectFlowId: null, // flow id the interject opt-in belongs to
  historySessions: [],    // [{flow_id, task_description, status, updated_at, ...}]
  historyIndexLoading: false, // true while /api/history is in flight (refresh)
  historyIndexConfirmed: false, // true once we've *confirmed* real history data:
                                // a WS history_index push (even an empty list) or
                                // a non-empty /api/history return. A 2s empty
                                // /api/history timeout does NOT confirm — it fires
                                // before any daemon pushed its index.
  selectedHistoryId: null,// flow whose records are shown in the history detail
  historyRecords: [],     // records currently rendered in the history detail
  // Opaque progress token for the history-detail view's last REST snapshot —
  // the history-view counterpart of `flowConversationProgress`. Echoed as
  // `?after=` on a WS-reconnect re-fetch and reset on session switch, history
  // close, or an incompatible `mode: full` push. Maintained separately so the
  // history detail and the running-flow view keep independent progress even
  // when both have the same flow open.
  historyProgress: null,
  // Bundle content signature for the history-detail view's last REST snapshot —
  // the history-view counterpart of `flowConversationSignature`. Echoed as
  // `?sig=` on a reconnect re-fetch so an unchanged session can be answered
  // `delivery:"not_modified"` instead of re-sending its whole bundle. Reset
  // wherever `historyProgress` is.
  historySignature: null,
  // Cursor-completeness self-check bookkeeping (shared by both views).
  //   backfillInFlight  — `"<view>|<flowId>" -> true` while a backfill or its
  //     full escalation is awaiting the server, so the 3s poll and a burst of WS
  //     frames collapse to ONE repair request instead of one per signal.
  //   backfillAttempts  — `"<view>|<flowId>" -> { generation, backfills, full,
  //     unkeyableFull }`, the repair budget spent against ONE bundle.
  //   backfillUnfillable — `"<view>|<flowId>" -> { generation, map: { stepId:
  //     [ordinal…] } }`, the numbers the SERVER answered are absent from that
  //     bundle. A number below a file's cursor need not name a record at all (the
  //     cursor counts physical lines, so a blank / unparseable line advances it
  //     without emitting one), so such a number is legitimately unfillable and is
  //     retired from the self-check rather than re-requested on every signal.
  //
  // Both are scoped to a (view, flow, GENERATION) — the generation being the
  // server's lifecycle id for the bundle these facts describe (see
  // `repairBudget`). Never to the bundle SIGNATURE: that is re-minted on every
  // appended record, so a signature-scoped budget would be handed back on each
  // append and a hole the server cannot fill would re-spend it forever (one full
  // re-pull per streamed record). And never to the flow ALONE: a retired number
  // and a spent budget are claims about one bundle, and when the daemon replaces
  // it (a restart rewrites the step file) the very same number can name a real,
  // servable record — carrying the old verdict across would keep that record
  // invisible for the life of the page.
  backfillInFlight: {},
  backfillAttempts: {},
  backfillUnfillable: {},
  // History-detail counterpart of `flowConversationEpoch`.
  historyEpoch: 0,
  // The open history session's backend usage payload (the `usage` field of the
  // /api/history/{flow_id} bundle — {calls, steps, summary, legacy,
  // completeness}). Sent only on complete full snapshots, so it is adopted on
  // every full / delta load that carries it and kept otherwise. The history
  // badge and the usage region render exclusively from this payload.
  historyUsage: null,
  // project_root key of the currently-selected History tab; null lets
  // pickDefaultHistoryProjectRoot pick the most-recently-active project. Reset
  // by closeHistory() so the next openHistory() recomputes the default.
  historySelectedProjectRoot: null,
  connStale: false,       // true while the WS is down — data may be stale
  detailLoaded: false,    // true once the open flow's detail has rendered
  detailFetchFailures: 0, // consecutive /api/flows/{id} failures for the view

  // ---- Send-button settle-after-ws bookkeeping ----
  // After a successful Send POST, the textarea stays enabled but the Send
  // button stays disabled until a ws-pushed flowDetail snapshot proves the
  // backend saw the submission (pending_calls diff or a matching
  // interjection_event). The 8s fallback timer below force-unlocks if no ws
  // update lands in that window — UX degrades to "you can press Send again,
  // but the daemon may already have queued the prior submit".
  pendingSendSettleKey: null,        // identifier of the in-flight Send
  pendingSendTimer: null,            // 8s fallback timer id
  pendingSendBaselineCallIds: null,  // Set of call_ids at send time
  // Synthetic interject chip: held in `pending` visual state from Send press
  // through to the moment the real interjection chip materializes (then it
  // is replaced) or the 8s fallback fires.
  flowSyntheticInterjectPending: false,

  // ---- interjection_event lifecycle tracking ----
  // Per-call_id phase ("pending" | "consumed") learned from ws
  // `interjection_event` messages. Chips read this to apply
  // `.state-pending` / `.state-consumed` visual states. Toasts dedup against
  // `interjectionToastsSeen` so a STATUS_UPDATE that re-emits the same phase
  // (e.g. on reconnect replay) does not spam the user.
  interjectionPhases: {},
  interjectionToastsSeen: {},
  // (call_id, phase) dedup set for `interjection_event` ws messages. Prevents
  // a STATUS_UPDATE replay or duplicate broadcast from double-applying the
  // same phase transition (e.g. consuming a local entry twice).
  interjectionEventSeen: {},
  // Frontend-tracked synthetic interjections: one entry per Send the user
  // pressed for an interject submission, kept around until the matching real
  // `pending_calls` entry materializes (binding the local entry's `callId`)
  // and then is consumed via the `interjection_event` consumed phase. Each
  // entry is `{localId, text, callId, phase, submittedAt}`. `callId` starts
  // `null` and becomes the real call_id once `bindLocalInterjectionToCallId`
  // FIFO-binds it on the next pending event. Multiple submissions can sit
  // here at once so the user can press Interject → Send repeatedly without
  // losing prior drafts.
  localInterjections: [],
  // Consumed afterimages: when an interjection chip transitions to
  // `consumed` it normally also vanishes from `pending_calls` on the same
  // ws cycle, so the user never gets to see the consumed visual state.
  // We keep a tiny `{call_id, prompt, until_ts}` record around for a few
  // seconds so `computeInterventions` can re-inject a faded consumed chip
  // — the user gets a brief pending → consumed transition before the chip
  // disappears for good.
  interjectionConsumedAfterimages: [],
  // Session-level UI preference: tracks which reply-context collapsible
  // prompt bodies the user has manually expanded, keyed by intervention id
  // (e.g. 'call:<callId>'). Survives automatic re-renders (STATUS_UPDATE /
  // ws push → renderInterventions → updateReplyBox) so the user's
  // expand/collapse choice is preserved. Reset on openFlowView /
  // doCloseFlowView so switching or closing a flow returns to the default
  // collapsed state.
  flowReplyPromptExpanded: {},
  // Session-level UI preference parallel to flowReplyPromptExpanded: records
  // the most recent scrollTop of each reply-context collapsible prompt body,
  // keyed by intervention id (e.g. 'call:<callId>'). Lets the docked reply
  // panel's automatic re-renders (STATUS_UPDATE / ws push → renderInterventions
  // → updateReplyBox, which rebuild the whole context block via innerHTML="")
  // restore the user's reading position in a long, expanded 「消息详情」 body
  // instead of snapping it back to the top. Reset on openFlowView /
  // doCloseFlowView alongside flowReplyPromptExpanded.
  flowReplyPromptScroll: {},
  // Cache of on-demand-fetched untruncated pending-call prompts, keyed by
  // call_id. STATUS_UPDATE now clips a flow's own pending_calls prompt to
  // DESC_CLIP (wire economy — a discovery_confirm prompt can embed a whole
  // refined task description), so the reply-context's collapsed body carries
  // only the preview. When the operator expands a clipped body we fetch the
  // full prompt once via GET /api/calls/{id}/detail and cache it here; the 3s
  // poll / ws-push rebuilds then reuse the cached full text instead of the
  // preview (and never re-fetch). A pending call's prompt is immutable, so a
  // call_id key is stable. Reset on openFlowView / doCloseFlowView.
  flowReplyPromptFull: {},

  // ---- Diff-aware render signatures (plan B: skip empty rebuilds) ----
  // Per-region cache of the last rendered data's signature, keyed by region
  // (e.g. 'machines' / 'flows' / 'sidebar' / 'interventions'). A ws push or the
  // 3s detail poll re-runs each full-rebuild render unconditionally; most of
  // those carry unchanged data, and rebuilding a panel that contains the large
  // reply textarea reflows the layout and causes typing jank. Each guarded
  // render computes a `renderSignature(...)` over the field subset that affects
  // its visible output, compares it against the cached value here, and skips the
  // DOM rebuild (touching no DOM) when it matches. Reset at flow-view lifecycle
  // points (open / close / switch) via resetRenderSignatures() so a reused
  // container is never wrongly skipped on its first frame.
  renderSig: {},

  // ---- Issue management ----
  issues: [],                  // [{id, title, description, status, priority, type, tags, source, ...}]
  issuesShowClosed: false,     // include closed/resolved/won't-fix issues
  issuesSourceFilter: "",      // filter: "human" | "system" | ""
  issuesTypeFilter: "",        // filter: issue type or ""
  allIssueTypes: [],           // unfiltered type universe for the dropdown
  allIssueProjectRoots: [],    // unfiltered project_root universe for the dropdown
  issuesProjectFilter: "",     // filter: project_root or "" (全部项目)
  _issuesFetchSeq: 0,          // monotonic counter to discard stale fetchIssues responses
  _issuesFetchInFlight: false, // true while a fetchIssues request is in-flight (coalescing guard)
  _allIssueTypesFetchSeq: 0,   // monotonic counter to discard stale fetchAllIssueTypes responses
  _issuesRefreshPending: false,// true when a refresh was requested while in-flight
  selectedIssueId: null,       // composite key (machine_id::project_root::id) shown in detail pane
  issuesLoading: false,        // true while fetching issues
  // Set of issue composite keys for which a "start flow from issue" request is
  // in-flight.  Prevents duplicate POST /api/flows dispatches and disables the
  // launch button until the server responds (success or error).
  issueLaunchRequests: new Set(),
  // ---- Resume flow tracking ----
  // Set of flow_ids for which a resume request is currently in-flight.
  // Prevents duplicate POST /api/flows/{id}/resume calls and disables the
  // Resume button until the server responds (success or error).
  resumeFlowRequests: new Set(),
  // ---- End-session tracking ----
  // Set of flow_ids for which an end-session request is currently in-flight.
  // Prevents duplicate POST /api/flows/{id}/end calls and disables the
  // End button until the server responds (success or error).
  endSessionRequests: new Set(),
};

// Lifetime of a consumed-state afterimage chip in milliseconds.
const INTERJECTION_CONSUMED_AFTERIMAGE_MS = 3000;

// ---------------------------------------------------------------------------
// Diff-aware render signatures (plan B: skip empty rebuilds)
// ---------------------------------------------------------------------------

// Pure, deterministic serialization of a hand-picked field subset into a
// comparable string. Object keys are sorted recursively so logically-equal
// inputs always hash to the same string regardless of key insertion order, and
// any visible-field change yields a different string. The guarded full-rebuild
// render paths (renderMachines / renderFlows / renderFlowSidebar /
// renderInterventions) feed their region's visible-dependency subset through
// this and compare against `state.renderSig[key]` to decide whether the
// underlying data actually changed before touching the DOM.
function renderSignature(parts) {
  const seen = new Set();
  const norm = (v) => {
    if (v === null || typeof v !== "object") return v;
    if (seen.has(v)) return "[Circular]";
    seen.add(v);
    let out;
    if (Array.isArray(v)) {
      out = v.map(norm);
    } else {
      out = {};
      for (const k of Object.keys(v).sort()) out[k] = norm(v[k]);
    }
    seen.delete(v);
    return out;
  };
  const json = JSON.stringify(norm(parts));
  return json === undefined ? "undefined" : json;
}

// Drop every cached render signature so the next render of each guarded region
// is forced to rebuild its DOM. Called at flow-view lifecycle points (open /
// close / switch): the flow-view containers are reused across flows, so a stale
// signature left over from a prior flow could otherwise make the first frame of
// a freshly-opened (or switched-to) flow wrongly skip its rebuild and show the
// previous flow's panels.
function resetRenderSignatures() {
  state.renderSig = {};
}

// Derive a short, human-readable project name from an absolute project_root
// path: the last path segment (basename) after stripping any trailing
// slashes. Tolerant of both POSIX ('/') and Windows ('\\') separators. A
// non-string, empty, or root-only ('/') input yields '' so callers can treat
// "no readable name" uniformly (the card skips the badge, the sidebar shows a
// placeholder). DOM-free; exported for the pure tests.
function projectBasename(projectRoot) {
  if (typeof projectRoot !== "string") return "";
  // Strip trailing separators so '/a/b/' yields 'b', not ''.
  const trimmed = projectRoot.replace(/[\\/]+$/, "");
  if (!trimmed) return "";
  const parts = trimmed.split(/[\\/]/);
  return parts[parts.length - 1] || "";
}

// Derive the display label shown for a flow's project. Worktree-mode flows
// carry a project_root that points deep inside '{project_root}/se3/worktrees/
// {safe_name}', whose basename is a long, opaque slug that is neither the
// project name nor concise. Detect the 'se3/worktrees/' path segment: when
// present, take the basename of everything BEFORE that segment as the real
// project name and return '<项目名> (worktree)'; otherwise fall back to the
// plain basename. Worktree identification is by full path segment (not a
// substring match) to stay aligned with worktree-management's fixed layout and
// avoid misclassifying an ordinary directory that merely contains "worktrees"
// in its name. Tolerant of both POSIX ('/') and Windows ('\\') separators;
// non-string / empty / root-only input falls back to projectBasename so this
// never throws. DOM-free; exported for the pure tests.
function projectDisplayLabel(projectRoot) {
  if (typeof projectRoot !== "string") return projectBasename(projectRoot);
  // Normalize separators to '/' so segment matching is separator-agnostic.
  const normalized = projectRoot.replace(/\\/g, "/");
  const segments = normalized.replace(/\/+$/, "").split("/");
  // Find the runtime-dir segment ('tianluo', legacy 'se3') immediately
  // followed by 'worktrees'.
  for (let i = 0; i + 1 < segments.length; i++) {
    if ((segments[i] === "tianluo" || segments[i] === "se3") &&
        segments[i + 1] === "worktrees") {
      // The real project root is everything before the runtime-dir segment.
      const prefix = segments.slice(0, i).join("/");
      const projectName = projectBasename(prefix) || projectBasename(projectRoot);
      return `${projectName} (worktree)`;
    }
  }
  return projectBasename(projectRoot);
}

// Pure signature of the machine-list's visible dependencies. renderMachines
// paints one <li> per machine carrying only: the online/offline dot, the name
// (hostname || machine_id), the flow count, and the selected highlight. The
// signature plucks exactly those fields plus the selected id, so an unrelated
// snapshot field changing (internal counters, per-flow detail, etc.) does NOT
// force a list rebuild, while any visible change — a machine added/removed, an
// online flip, a renamed host, a flow-count change, or a selection change —
// yields a different string. DOM-free; exported for the pure tests.
function machinesSignature(machines, selectedId) {
  const list = Array.isArray(machines) ? machines : [];
  return renderSignature(
    list.map((m) => ({
      id: (m && m.machine_id) || null,
      hostname: (m && m.hostname) || "",
      online: !!(m && m.online),
      flows: m && Array.isArray(m.flows) ? m.flows.length : 0,
      selected: !!(m && m.machine_id === selectedId),
    }))
  );
}

// Pure signature of the flow-list's visible dependencies for the selected
// machine. renderFlows paints the heading (host name) and one card per flow;
// each card surfaces status, the waiting-for-lock and pending-call badges, the
// progress bar, the current step / index / total / task_type meta, the task
// description, and a Resume button gated by isFlowResumable + an in-flight
// resume request. The signature plucks exactly those visible inputs so any of
// them changing rebuilds the list, while an unrelated field change skips it.
// `resumeRequests` is the in-flight resume set (a Set, or an array in tests).
// DOM-free; exported for the pure tests.
function flowsSignature(machine, selectedId, resumeRequests) {
  if (!machine || typeof machine !== "object") {
    // No selected machine -> the "select a machine" empty state. Keyed by the
    // selected id so switching selection still differs from a real machine.
    return renderSignature({ machine: null, selected: selectedId || null });
  }
  const resuming = (id) => {
    if (!id) return false;
    if (resumeRequests instanceof Set) return resumeRequests.has(id);
    if (Array.isArray(resumeRequests)) return resumeRequests.indexOf(id) !== -1;
    return false;
  };
  const flows = Array.isArray(machine.flows) ? machine.flows : [];
  return renderSignature({
    machine: machine.machine_id || null,
    hostname: machine.hostname || "",
    selected: !!(machine.machine_id === selectedId),
    flows: flows.map((f) => ({
      id: (f && f.flow_id) || null,
      status: (f && f.status) || "",
      task: (f && f.task_description) || "",
      project_root: (f && f.project_root) || "",
      task_type: (f && f.task_type) || "",
      progress: (f && f.progress) || 0,
      current_step: (f && f.current_step) || "",
      step_index: (f && f.current_step_index) || 0,
      total_steps: (f && f.total_steps) || 0,
      waiting_lock: isWaitingForLock(f),
      pending: hasPendingCall(f),
      resumable: isFlowResumable(f),
      resuming: resuming(f && f.flow_id),
      plan_mode: (f && f.plan_mode) || null,
    })),
  });
}
// Pure signature over everything that affects the docked reply region's
// rendered output — the chip bar (`#flow-interventions`), the reply-context
// panel (`#flow-reply-context`, built by updateReplyBox), and the inline
// Interject button state (syncInterjectButton). renderInterventions feeds the
// freshly-computed `entries` and the reconciled reply state through this and
// compares it against `state.renderSig.interventions`: an empty status_update
// (data unchanged) yields the same string, so the whole region is left
// untouched and the large reply textarea — with the user's in-progress draft,
// focus, scroll position, and auto-grow height — never reflows. A real change
// (a new/withdrawn pending call, an interjection phase flip, a different
// selected target, or a Send going in-flight) yields a different string and
// triggers the rebuild, so real-time feedback is unaffected.
//
// `entries` — the computeInterventions(flow) output; the per-chip visible
//   dependencies are id, kind (which fully determines the chip icon + label
//   via KIND_META and the `kind-<kind>` class), synthetic, prompt, options,
//   callId, phase, and afterimage (an afterimage chip renders disabled).
// `replyState` — the reconciled reply-box dependencies: the selected target
//   id, the in-flight Send gate key (`pendingSendSettleKey`, which disables
//   Send), the Interject opt-in flag, whether the flow is active (drives the
//   placeholder + the Interject button visibility), and whether a real
//   interjection is pending (also drives the Interject button visibility).
//
// The expand/collapse + scroll-position persistent UI state (keyed per
// intervention id) is deliberately NOT part of the signature: on a skip the
// reply-context block is not rebuilt at all, so those states are inherently
// preserved — there is nothing to restore.
function interventionsSignature(entries, replyState) {
  const rs = replyState || {};
  const list = Array.isArray(entries) ? entries : [];
  return renderSignature({
    entries: list.map((e) => ({
      id: e.id,
      kind: e.kind,
      synthetic: !!e.synthetic,
      prompt: e.prompt != null ? String(e.prompt) : "",
      options: Array.isArray(e.options) ? e.options : [],
      callId: e.callId != null ? String(e.callId) : "",
      phase: e.phase != null ? e.phase : null,
      afterimage: !!e.afterimage,
    })),
    targetId: rs.targetId != null ? rs.targetId : null,
    pendingSendSettleKey:
      rs.pendingSendSettleKey != null ? rs.pendingSendSettleKey : null,
    flowInterjectRequested: !!rs.flowInterjectRequested,
    isActiveFlow: !!rs.isActiveFlow,
    hasRealInterjection: !!rs.hasRealInterjection,
  });
}
let ws = null;
let reconnectAttempts = 0;
let detailPollTimer = null;
// Tracks whether the current `#flow-view` has a history entry on top of the
// browser stack (pushed when the view was opened). When true, user-initiated
// closes (✕ button, Escape) delegate to `history.back()` so the back button
// and the close button share one collapse path. The popstate listener clears
// this flag and runs the real cleanup, so we never push-back loop.
let flowViewHistoryPushed = false;

// ---------------------------------------------------------------------------
// DOM helpers
// ---------------------------------------------------------------------------

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------------------
// Auth / owner identity — pure helpers (DOM-free, isomorphically testable)
// ---------------------------------------------------------------------------

// Coarse login state machine. `unknown` is the boot value (before /api/auth/me
// resolves); `login` shows the sign-in gate; `authed` shows the app surface.
const AUTH_STATES = Object.freeze({
  UNKNOWN: "unknown",
  LOGIN: "login",
  AUTHED: "authed",
});

// Pure transition for the auth state machine. Events:
//   "me_ok" | "login_ok" | "breakglass_ok" → AUTHED
//   "me_401" | "unauthorized" | "logout"   → LOGIN
// Any other event leaves the state unchanged (idempotent). The transition is
// deliberately total and side-effect free so it can be unit-tested without a
// DOM or network.
function nextAuthState(current, event) {
  switch (event) {
    case "me_ok":
    case "login_ok":
    case "breakglass_ok":
      return AUTH_STATES.AUTHED;
    case "me_401":
    case "unauthorized":
    case "logout":
      return AUTH_STATES.LOGIN;
    default:
      return current === AUTH_STATES.AUTHED ||
        current === AUTH_STATES.LOGIN ||
        current === AUTH_STATES.UNKNOWN
        ? current
        : AUTH_STATES.UNKNOWN;
  }
}

// Human-readable label for the resolved owner shown in the top bar. Prefers the
// display name, falls back to the internal owner_id, and appends an "(admin)"
// suffix for the operator/break-glass subject. Returns "" for no identity.
function ownerLabel(identity) {
  if (!identity || typeof identity !== "object") return "";
  const name =
    (typeof identity.display_name === "string" && identity.display_name.trim()) ||
    (typeof identity.owner_id === "string" && identity.owner_id) ||
    "unknown";
  return identity.is_admin
    ? tf("topbar.ownerAdmin", `${name} (admin)`, { name })
    : name;
}

// A request is "unauthorized" exactly when the server answered 401. Used to
// drive the global session-expiry interception (kick back to the login gate).
function isUnauthorizedStatus(status) {
  return status === 401;
}

// Owner-scoping policy for the machine list. The backend already filters
// /api/machines by owner, but the frontend re-applies the same narrowing
// defensively so a stale or mixed snapshot can never surface another owner's
// daemon. An admin (operator view) sees every machine; a regular owner sees
// only machines whose `owner_id` matches its own. A machine with no resolved
// owner_id (unbound) is hidden from a regular owner — it is not "theirs".
function canOwnerControlMachine(machine, identity) {
  if (!identity || typeof identity !== "object") return false;
  if (identity.is_admin) return true;
  if (!machine || typeof machine !== "object") return false;
  return Boolean(machine.owner_id) && machine.owner_id === identity.owner_id;
}

// Narrow a machine list to those the current owner may see/control. With no
// identity the list is empty (fail-closed); an admin gets the list verbatim.
function visibleMachinesForOwner(machines, identity) {
  const list = Array.isArray(machines) ? machines : [];
  if (!identity) return [];
  if (identity.is_admin) return list.slice();
  return list.filter((m) => canOwnerControlMachine(m, identity));
}

// View model for one daemon-key row. Normalizes the label/status presentation
// so the renderer (and its tests) share one source of truth.
function daemonKeyRowModel(key) {
  key = key && typeof key === "object" ? key : {};
  const revoked = Boolean(key.revoked || key.revoked_at);
  const trimmedLabel = (typeof key.label === "string" && key.label.trim()) || "";
  return {
    keyId: key.key_id || "",
    // `unlabeled` lets the renderer localize the placeholder via tf() at paint
    // time (the model itself is I18N-free so its pure tests stay deterministic).
    unlabeled: !trimmedLabel,
    label: trimmedLabel || "(unlabeled)",
    revoked,
    statusLabel: revoked ? "Revoked" : "Active",
    statusClass: revoked ? "revoked" : "active",
    createdAt: key.created_at || null,
  };
}

// View model for one row of the per-machine registered-project dialog. `entry`
// is the {path, exists, active} shape the daemon publishes in its STATUS_UPDATE
// snapshot (and that GET /api/machines/{id}/projects mirrors verbatim).
//
// The list is a faithful mirror of the daemon's registry FILE, not of the
// filtered project universe: an entry whose directory has vanished is exactly
// what the operator opened this dialog to clean up, so it is surfaced and
// flagged rather than hidden. `exists` missing altogether (an older daemon that
// predates the field) is deliberately read as "present" — flagging an entry
// stale on absent evidence would invite deleting a live root.
//
// I18N-free by design (same rationale as daemonKeyRowModel): copy is injected
// by the renderer through tf() at paint time, so these projections stay
// deterministic in the pure tests and re-localize on a language switch.
function projectRegistryRowModel(entry) {
  entry = entry && typeof entry === "object" ? entry : {};
  const path = typeof entry.path === "string" ? entry.path.trim() : "";
  return {
    path,
    // Short label shown ahead of the full path; a worktree-shaped root folds
    // back to "<project> (worktree)" like everywhere else in the console.
    name: projectDisplayLabel(path),
    stale: entry.exists === false,
    active: Boolean(entry.active),
    // Removal is offered for any real entry — including an ACTIVE one. The
    // live-flow refusal is the daemon's call (it alone holds the authoritative
    // supervisor view); "active" here only means the daemon is polling the
    // root, which is not the same as a running flow, so pre-judging it here
    // would hide a legitimate action behind a stale mirror.
    canRemove: Boolean(path),
  };
}

// Windows-style absolute prefixes ('C:\…', UNC '\\host\share'). The daemon is
// the authority on what its own filesystem calls absolute (os.path.isabs), so
// this front guard only rejects what is unambiguously relative — it must not
// refuse a legitimate path just because this browser runs on another OS.
const WINDOWS_ABS_RE = /^(?:[A-Za-z]:[\\/]|\\\\)/;

// Build the POST body for a manual project registration from the raw input.
// Returns a discriminated result rather than throwing so the caller can render
// a localized message per rejection reason: {ok:true, body, projectRoot} or
// {ok:false, reason:"empty"|"not_absolute"}. Pure.
function buildAddProjectBody(input) {
  const raw = typeof input === "string" ? input.trim() : "";
  if (!raw) return { ok: false, reason: "empty" };
  if (!raw.startsWith("/") && !WINDOWS_ABS_RE.test(raw)) {
    return { ok: false, reason: "not_absolute" };
  }
  return { ok: true, reason: "", projectRoot: raw, body: { project_root: raw } };
}

// Stable daemon error_code → i18n key map for the project-registry commands.
// The daemon deliberately answers with a machine-readable code instead of
// prose so the user-visible copy can live in the language packs; an unknown
// code (a newer daemon) falls back to the generic key rather than painting an
// untranslated English string from the wire.
const PROJECT_ERROR_KEYS = {
  invalid_path: "projects.errInvalidPath",
  not_found: "projects.errNotFound",
  not_a_directory: "projects.errNotADirectory",
  live_flow: "projects.errLiveFlow",
  not_registered: "projects.errNotRegistered",
  registry_error: "projects.errRegistryError",
  invalid_operation: "projects.errInvalidOperation",
  unsupported: "projects.errUnsupported",
};

function projectErrorKey(errorCode) {
  const code = typeof errorCode === "string" ? errorCode.trim() : "";
  return PROJECT_ERROR_KEYS[code] || "projects.errGeneric";
}

// Project-list transforms applied to the local entry array right after a write
// the daemon has ACKed.
//
// WHY these exist instead of simply re-fetching: GET /projects is answered from
// the server's STATUS_UPDATE mirror, and the daemon sends its PROJECT_RESULT
// ack BEFORE the fast push that refreshes that mirror. A re-fetch issued on the
// ack therefore normally repaints the pre-write registry — a just-added project
// missing, a just-removed one back — next to a success toast. Projecting the
// daemon's own echoed (normalized) root locally keeps the list truthful until
// the fast push lands and repaints authoritatively.
//
// Both are pure and match the daemon's ordering (sorted by path) so the
// optimistic paint and the snapshot that replaces it agree.
function applyProjectAdded(entries, projectRoot) {
  const path = typeof projectRoot === "string" ? projectRoot.trim() : "";
  const rows = Array.isArray(entries) ? entries.slice() : [];
  if (!path) return rows;
  const at = rows.findIndex((e) => e && e.path === path);
  // The daemon validated the directory and added it to its polled set, so both
  // flags are known-true; an existing row is refreshed rather than duplicated
  // (re-adding a stale entry is how an operator "revives" it).
  const row = { path, exists: true, active: true };
  if (at >= 0) rows[at] = row;
  else rows.push(row);
  rows.sort((a, b) => String((a && a.path) || "").localeCompare(String((b && b.path) || "")));
  return rows;
}

function applyProjectRemoved(entries, projectRoot) {
  const path = typeof projectRoot === "string" ? projectRoot.trim() : "";
  const rows = Array.isArray(entries) ? entries.slice() : [];
  if (!path) return rows;
  return rows.filter((e) => !e || e.path !== path);
}

// -- file attachments: DOM-free upload helpers -------------------------------
//
// The whole feature rests on one rule: the textarea's text IS the prompt. A
// pasted file inserts a placeholder token at the caret, and the upload's answer
// replaces that token in place with the project-relative path — which then
// stays put as ordinary, editable, deletable text. Nothing is substituted at
// submit time, so there is no text→attachment mapping that a user edit can
// corrupt. Every helper below is therefore a plain string/object transform,
// callable straight from Node in tests/frontend/file_upload.test.mjs.

// WHY this literal is duplicated from Python: the browser cannot import
// protocol.py, and this pre-flight check exists precisely so an over-sized file
// never leaves the machine. It MUST equal protocol.MAX_UPLOAD_BYTES — the
// server and the daemon each re-check the same bound independently — and the
// static guard test tests/test_frontend_file_upload.py pins the two literals
// together so they cannot drift apart silently.
const MAX_UPLOAD_BYTES = 20 * 1024 * 1024;

// Pre-flight gate run before a single byte is uploaded. Returns a discriminated
// {ok, code} rather than throwing, and the `code` is deliberately drawn from
// the SAME vocabulary the daemon uses (protocol.UPLOAD_ERROR_CODES) so the
// browser-side rejection and the wire-side rejection localize through one path
// (uploadErrorKey). Pure.
function validateUploadFile(file) {
  if (!file || typeof file !== "object") return { ok: false, code: "invalid_payload" };
  const name = typeof file.name === "string" ? file.name.trim() : "";
  if (!name) return { ok: false, code: "invalid_filename" };
  const size = Number(file.size);
  if (!Number.isFinite(size) || size < 0) return { ok: false, code: "invalid_payload" };
  if (size > MAX_UPLOAD_BYTES) return { ok: false, code: "too_large" };
  return { ok: true, code: "" };
}

// The visible "this is uploading" marker parked at the caret until the path
// arrives. `seq` is a per-input monotonic counter, not a display detail: two
// pastes of the SAME file name must yield two distinct tokens, or the first
// answer would replace the second paste's marker. Pure apart from the language
// lookup (re-resolved per call, so a language switch mid-upload is harmless).
function uploadPlaceholderToken(name, seq) {
  const label = String(name == null ? "" : name).trim() || "file";
  const n = Number(seq);
  const ordinal = Number.isFinite(n) ? n : 0;
  return tf("upload.placeholder", `[uploading ${label} #${ordinal}]`, {
    name: label,
    seq: ordinal,
  });
}

// Literal (never regex) first-occurrence replace / remove.
//
// WHY literal: both the token and the path embed a user-supplied file name,
// which may legally contain regex metacharacters (`a+b(1).png`); compiling
// either into a pattern would match the wrong span or throw. First occurrence
// only, because the user may well have copy-pasted the same path elsewhere on
// purpose — this operation owns exactly the one it put there. A miss (the user
// deleted the token, or edited the path) returns the text untouched: the text
// is the source of truth and is never "repaired" behind the user's back. Pure.
function replaceTokenOnce(text, token, replacement) {
  const src = String(text == null ? "" : text);
  const needle = String(token == null ? "" : token);
  if (!needle) return src;
  const at = src.indexOf(needle);
  if (at < 0) return src;
  return src.slice(0, at) + String(replacement == null ? "" : replacement)
    + src.slice(at + needle.length);
}

function removePathOnce(text, path) {
  return replaceTokenOnce(text, path, "");
}

// Insert `text` at the caret of a textarea/input, replacing any selection, and
// leave the caret just past what was inserted so the user can keep typing.
// Touches only value/selectionStart/selectionEnd — no document, no events — so
// a plain object literal stands in for the element under Node. An element with
// no usable selection (a never-focused field reports null) appends at the end
// rather than silently prepending at 0. Returns the new value.
function insertAtCaret(el, text) {
  if (!el || typeof el !== "object") return "";
  const insert = String(text == null ? "" : text);
  const value = String(el.value == null ? "" : el.value);
  const rawStart = Number(el.selectionStart);
  const start = Number.isFinite(rawStart)
    ? Math.max(0, Math.min(rawStart, value.length))
    : value.length;
  const rawEnd = Number(el.selectionEnd);
  const end = Number.isFinite(rawEnd)
    ? Math.max(start, Math.min(rawEnd, value.length))
    : start;
  const next = value.slice(0, start) + insert + value.slice(end);
  el.value = next;
  const caret = start + insert.length;
  el.selectionStart = caret;
  el.selectionEnd = caret;
  return next;
}

// Human file size. Binary units with one decimal (dropped when it is a bare
// .0), capped at MB because the channel's ceiling is 20 MiB — a GB branch would
// be unreachable. The unit words come from the language pack. Pure apart from
// the lookup.
function formatFileSize(bytes) {
  const raw = Number(bytes);
  const size = Number.isFinite(raw) && raw > 0 ? raw : 0;
  const trim = (n) => String(Math.round(n * 10) / 10);
  if (size < 1024) {
    const n = Math.round(size);
    return tf("common.size.bytes", `${n} B`, { n });
  }
  if (size < 1024 * 1024) {
    const n = trim(size / 1024);
    return tf("common.size.kb", `${n} KB`, { n });
  }
  const n = trim(size / (1024 * 1024));
  return tf("common.size.mb", `${n} MB`, { n });
}

// Image extensions used ONLY when the MIME type is absent — some file managers
// and drag sources hand over a blank `type`. The answer decides whether the
// strip shows a thumbnail or a generic icon, nothing else: a wrong guess costs
// a broken preview, never a failed upload, so guessing is worth it.
const IMAGE_EXT_RE = /\.(png|jpe?g|gif|webp|bmp|svg|avif|heic|heif|ico|tiff?)$/i;

function isImageFile(file) {
  if (!file || typeof file !== "object") return false;
  const type = typeof file.type === "string" ? file.type.trim().toLowerCase() : "";
  if (type) return type.startsWith("image/");
  const name = typeof file.name === "string" ? file.name : "";
  return IMAGE_EXT_RE.test(name);
}

// View model for one attachment-strip row. `entry` is the in-memory upload
// record {id, name, size, type, status, path, previewUrl, code}.
//
// The strip is a mirror of what the text already says, so `canRemove` is gated
// on a landed path: an in-flight or failed entry has no path in the text to
// remove, and removal is defined purely as "delete that path from the text" —
// it never touches the file the daemon already wrote to disk. `canCancel` is
// the in-flight counterpart: a row that carries a placeholder instead of a path
// is dismissed by giving up on the request, not by editing a path out. Pure
// apart from the size/status lookups.
//
// WHY `storedName` / `titleText` exist at all: the row's visible name is the
// BROWSER-side name, and a clipboard paste is always called "image.png". Paste
// three screenshots and the strip shows three identical rows, while the prompt
// text carries three distinct content-hash-prefixed paths — nothing on screen
// says which row owns which path. `storedName` is the identifying head of the
// stored basename (its content-hash prefix) and nothing else: the hash is what
// tells the rows apart, while the basename's tail is the original filename the
// row's first line already shows, so repeating it only widened every row for no
// added information. The full relative path is what the agent will actually
// read, so it is what the row's tooltip must say.
function attachmentRowModel(entry) {
  entry = entry && typeof entry === "object" ? entry : {};
  const raw = String(entry.status || "");
  const status = ["uploading", "done", "error"].includes(raw) ? raw : "uploading";
  const size = Number(entry.size);
  const path = typeof entry.path === "string" ? entry.path : "";
  // Only a landed path names a real file: an in-flight row has none yet, and a
  // failed one never will, so both stay blank rather than showing a name for a
  // file that is not on the project machine.
  const stored = status === "done" && path ? path : "";
  // Paths on the wire are project-relative and always posix-separated; a string
  // with no separator degrades to itself.
  const basename = stored ? stored.slice(stored.lastIndexOf("/") + 1) : "";
  // The identifying head is everything before the FIRST "_": the stored name is
  // "<content hash>_<original filename>" and the original filename may itself
  // contain "_" (my_photo.png), so taking the last one would swallow part of it.
  // A basename with no "_", or one starting with it, does not follow that
  // convention (legacy/odd naming) and yields no trustworthy identifier — show
  // the whole basename then rather than an empty tag, and let the column's
  // ellipsis + hover scroll deal with the length.
  const sep = basename.indexOf("_");
  const storedName = sep > 0 ? basename.slice(0, sep) : basename;
  return {
    id: String(entry.id == null ? "" : entry.id),
    name: typeof entry.name === "string" ? entry.name : "",
    size: Number.isFinite(size) && size > 0 ? size : 0,
    sizeText: formatFileSize(entry.size),
    status,
    // Secondary line while the outcome is still unknown; "" once the size
    // itself is the whole story.
    statusText: status === "uploading" ? tf("upload.uploading", "Uploading…") : "",
    isImage: isImageFile(entry),
    // A revoked/absent object URL must not render an empty <img>; the caller
    // drops the preview when it recycles the URL.
    previewUrl: typeof entry.previewUrl === "string" ? entry.previewUrl : "",
    path,
    storedName,
    titleText: stored,
    canRemove: status === "done" && Boolean(path),
    canCancel: status === "uploading",
    errorKey: status === "error" ? uploadErrorKey(entry.code) : "",
  };
}

// Stable upload error_code → i18n key map, covering all three sources of a
// failure code: the daemon's own protocol.UPLOAD_ERROR_CODES, the codes the
// server mints before dispatch (unsupported_daemon / not_connected / timeout /
// no_target), and the browser-local "network" for a fetch that never landed.
// Same contract as PROJECT_ERROR_KEYS: prose lives in the language packs, the
// wire carries only codes, and an unrecognised code (a newer daemon) falls back
// to the generic message rather than painting a raw token.
//
// `invalid_path` folds into the unregistered-project message on purpose: both
// mean the daemon refused the project root this browser named, and the
// operator's remedy is identical — re-add the project — so a second string
// would only be a distinction without a difference.
const UPLOAD_ERROR_KEYS = {
  too_large: "upload.errTooLarge",
  not_registered: "upload.errUnregisteredProject",
  invalid_path: "upload.errUnregisteredProject",
  invalid_filename: "upload.errInvalidFilename",
  invalid_payload: "upload.errFailed",
  write_failed: "upload.errWriteFailed",
  unsupported: "upload.errUnsupportedDaemon",
  unsupported_daemon: "upload.errUnsupportedDaemon",
  not_connected: "upload.errNotConnected",
  timeout: "upload.errTimeout",
  no_target: "upload.errNoTarget",
  // Distinct from no_target: the flow view has no machine/project pickers, so
  // "choose a machine and a project" would name a remedy that does not exist
  // on that screen.
  no_flow: "upload.errNoFlow",
  network: "upload.errNetwork",
};

function uploadErrorKey(code) {
  const c = typeof code === "string" ? code.trim() : "";
  return UPLOAD_ERROR_KEYS[c] || "upload.errFailed";
}

// Machine-readable ``reason`` → i18n key map for POST /api/flows refusals —
// the same contract as UPLOAD_ERROR_KEYS: prose lives in the language packs,
// the wire carries only codes. An unrecognised code falls back to the
// backend's own ``detail`` rather than painting a raw token.
const FLOW_LAUNCH_ERROR_KEYS = {
  unsupported_daemon: "newTask.errUnsupportedDaemonPlanMode",
};

function flowLaunchErrorMessage(status, detail) {
  if (detail && typeof detail === "object" && typeof detail.reason === "string") {
    const key = FLOW_LAUNCH_ERROR_KEYS[detail.reason];
    if (key) return tf(key, "Server refused the request.");
  }
  if (detail && typeof detail === "object" && typeof detail.detail === "string") {
    return detail.detail;
  }
  return tf("error.serverReturned", `Server returned ${status}.`, { status });
}

// Names of the rows still in flight, in paste order.
//
// WHY submitting must wait on this: the placeholder token is not prompt prose.
// Sending while it is still in the text ships "[uploading shot.png #3]" to the
// agent, and — because every submit path blanks its input — the 201 that lands
// a moment later finds no token to replace, so the project-relative path is
// dropped on the floor and the file that DID reach the disk is named by no
// prompt at all. Returning the names rather than a bare count lets the refusal
// say which file it is waiting on. Pure.
function pendingUploadNames(entries) {
  const rows = Array.isArray(entries) ? entries : [];
  return rows
    .filter((e) => e && String(e.status || "") === "uploading")
    .map((e) => (typeof e.name === "string" && e.name.trim()) || "file");
}

// -- file attachments: upload orchestration and the attachment strip ---------
//
// The interaction layer over the pure helpers above. There are three prompt
// inputs but only TWO DOM scopes: respond and interject share the one docked
// textarea, so a single set of bindings serves both — a second copy would only
// be dead DOM competing for the same element.
//
// Each scope owns four ids: the textarea that receives the paste/drop, the
// strip that mirrors it, and the (hidden) file input plus the button that
// opens it. `autoGrow` marks the docked box, whose height tracks its content —
// every programmatic edit of that text has to re-run the same measurement a
// keystroke would.
const UPLOAD_SCOPES = {
  newTask: {
    inputId: "nt-task",
    stripId: "nt-attachments",
    fileInputId: "nt-file-input",
    attachBtnId: "nt-attach-btn",
    autoGrow: false,
  },
  flow: {
    inputId: "flow-reply-input",
    stripId: "flow-attachments",
    fileInputId: "flow-file-input",
    attachBtnId: "flow-attach-btn",
    autoGrow: true,
  },
};

function uploadScope(scope) {
  return UPLOAD_SCOPES[String(scope || "")] || null;
}

// Reverse lookup so the strip's own handlers (remove / clear) can find the
// textarea they must edit without the caller threading the scope through.
function uploadScopeForStrip(stripId) {
  const id = String(stripId || "");
  for (const name of Object.keys(UPLOAD_SCOPES)) {
    if (UPLOAD_SCOPES[name].stripId === id) return UPLOAD_SCOPES[name];
  }
  return null;
}

function attachmentEntries(stripId) {
  if (!state.uploadAttachments || typeof state.uploadAttachments !== "object") {
    state.uploadAttachments = {};
  }
  const id = String(stripId || "");
  if (!Array.isArray(state.uploadAttachments[id])) state.uploadAttachments[id] = [];
  return state.uploadAttachments[id];
}

// Submit gate for one strip: "" when the text is settled and safe to send,
// otherwise the localized refusal to paint. Every prompt-submitting path asks
// this before it reads the textarea — see pendingUploadNames for why an
// in-flight row makes the text unsendable.
function pendingUploadRefusal(stripId) {
  const names = pendingUploadNames(attachmentEntries(stripId));
  if (!names.length) return "";
  const list = names.join(", ");
  return tf(
    "upload.errPending",
    `Still uploading ${list} — wait for it to finish before sending.`,
    { names: list, count: names.length },
  );
}

// Re-measure the docked textarea after a programmatic edit. The auto-grow
// height is normally driven by the `input` event, which a value assignment does
// NOT fire — without this, replacing a placeholder with a long path would leave
// the box clipped at its pre-paste height.
function syncUploadInput(cfg) {
  if (cfg && cfg.autoGrow) autoGrowReplyTextarea();
}

// Resolve where a file pasted into `scope` should be stored.
//
// The two scopes name their target differently because they know different
// things: a running flow already carries its machine and project root (the
// server re-derives both from the flow snapshot, so the browser sends only the
// id), whereas the New Task form has no flow yet and must name the pair
// outright. Returns {ok:true, ...} or {ok:false, code, errorKey}; failures are
// data, not exceptions, so the caller can toast and skip the gesture.
function resolveUploadTarget(scope) {
  const fail = (code) => ({ ok: false, code, errorKey: uploadErrorKey(code) });
  if (scope === "flow") {
    const flowId = typeof state.selectedFlowId === "string" ? state.selectedFlowId.trim() : "";
    if (!flowId) return fail("no_flow");
    return { ok: true, kind: "flow", flowId };
  }
  const machineSel = $("nt-machine");
  const projectSel = $("nt-project");
  const machineId = machineSel ? String(machineSel.value || "").trim() : "";
  const projectRoot = projectSel ? String(projectSel.value || "").trim() : "";
  if (!machineId || !projectRoot) return fail("no_target");
  // WHY a hand-typed path cannot receive an upload: the daemon writes only into
  // roots it has actually registered (that check is what stops a compromised
  // server from dropping bytes anywhere on the machine), and the "Other path…"
  // entry exists precisely for directories the daemon has NOT registered yet —
  // it may not even be a project until the spawn runs `luo init` there. So the
  // upload is refused up front with the "re-add the project" remedy rather than
  // sent out to earn a `not_registered` from the far side.
  if (projectRoot === PROJECT_MANUAL_SENTINEL) return fail("not_registered");
  return { ok: true, kind: "project", machineId, projectRoot };
}

// Build the POST url. The metadata rides in the query string because the body
// is the raw file bytes — the server deliberately does not parse multipart.
function uploadRequestUrl(target, filename) {
  const parts = ["filename=" + encodeURIComponent(String(filename || ""))];
  if (target && target.kind === "flow") {
    parts.push("flow_id=" + encodeURIComponent(String(target.flowId || "")));
  } else if (target) {
    parts.push("machine_id=" + encodeURIComponent(String(target.machineId || "")));
    parts.push("project_root=" + encodeURIComponent(String(target.projectRoot || "")));
  }
  return "/api/uploads?" + parts.join("&");
}

// Project-relative attachment paths as they appear inside message text.
//
// Both layout prefixes are recognised: the runtime directory was renamed
// se3/ → tianluo/ in 12.0.0, but a conversation recorded before that rename is
// still replayed from history, and its prompts carry the old prefix forever.
//
// The character class is what ends a path, and it is deliberately generous
// about what a filename may contain (spaces are the only hard stop) while
// refusing the delimiters that realistically WRAP a path in prose — quotes,
// brackets and CJK punctuation. The ASCII comma is deliberately NOT in that
// set: sanitize_upload_filename keeps a comma verbatim, so `v1,2.png` is a
// real stored name, and ending the run there would truncate it mid-name.
// Comma-separated paths still split correctly — the tempered lookahead stops
// the run at the next prefix, and the trailing rule peels the comma off.
// A tail of sentence punctuation is peeled off separately, because those
// characters ('.' above all) are legal inside a name and can only be judged
// at the very end of the run.
//
// WHY the run is additionally tempered by a lookahead for the next prefix:
// uploading two files in one paste inserts their tokens back-to-back with no
// separator, so the message text really does read `…_a.pngtianluo/uploads/…_b.png`.
// Without the lookahead the greedy run swallows both into one string that still
// ends in .png — a single bogus path the daemon then refuses, hiding BOTH
// thumbnails in exactly the multi-paste case this feature exists for.
const UPLOAD_PATH_RE =
  /(?:tianluo|se3)\/uploads\/(?:(?!(?:tianluo|se3)\/uploads\/)[^\s"'`<>()[\]{}，。、；：！？“”‘’《》【】])+/g;
const UPLOAD_PATH_TRAILING_RE = /[.,;:!?]+$/;
// What may NOT sit immediately before a path for the run to be one: a longer
// path's tail (`/home/me/tianluo/uploads/a.png`) is not a project-relative
// attachment, and neither is `xtianluo/uploads/a.png`.
const UPLOAD_PATH_LEAD_RE = /[\w./-]/;

// Every distinct image attachment path named by `text`, in first-appearance
// order. Pure. The image test is the shared IMAGE_EXT_RE (via a name-only
// pseudo-file) so the strip's thumbnail rule and the conversation's inline
// rule can never disagree about what an image is.
function extractUploadImagePaths(text) {
  const src = typeof text === "string" ? text : "";
  if (!src) return [];
  const seen = new Set();
  const out = [];
  UPLOAD_PATH_RE.lastIndex = 0;
  // End of the previous run, whether or not it qualified: a run that starts
  // exactly where another ended is the back-to-back multi-paste shape, and the
  // preceding character there is a filename character by construction — the
  // one place the lead-character rule must not apply.
  let prevEnd = -1;
  let m = UPLOAD_PATH_RE.exec(src);
  while (m) {
    const start = m.index;
    const okStart =
      start === 0 || start === prevEnd || !UPLOAD_PATH_LEAD_RE.test(src[start - 1]);
    prevEnd = start + m[0].length;
    const path = okStart ? m[0].replace(UPLOAD_PATH_TRAILING_RE, "") : "";
    // A repeated path is one file: the message may well name it twice (the
    // user's prompt and the agent quoting it back), and two identical
    // thumbnails would read as two attachments.
    if (path && !seen.has(path) && isImageFile({ name: path })) {
      seen.add(path);
      out.push(path);
    }
    m = UPLOAD_PATH_RE.exec(src);
  }
  return out;
}

// Build the read-back GET url for one stored attachment. Same target shapes and
// the same encoding as uploadRequestUrl — the two legs address a file the same
// way, and a divergence here would be a 404 nothing reports.
function uploadFetchUrl(path, target) {
  const parts = ["path=" + encodeURIComponent(String(path || ""))];
  if (target && target.kind === "flow") {
    parts.push("flow_id=" + encodeURIComponent(String(target.flowId || "")));
  } else if (target) {
    parts.push("machine_id=" + encodeURIComponent(String(target.machineId || "")));
    parts.push("project_root=" + encodeURIComponent(String(target.projectRoot || "")));
  }
  return "/api/uploads/file?" + parts.join("&");
}

// Which machine/project the conversation currently on screen belongs to.
//
// WHY machine + root is preferred over the flow id whenever it is known: the
// server resolves a flow id against the LIVE snapshot, and the history view's
// whole purpose is to reopen flows that ended — often days ago, on a daemon
// that has long since forgotten them. Naming the machine and the root directly
// keeps an old conversation's thumbnails working; the flow id is only the
// fallback for a flow this browser has not yet seen in any listing.
//
// Returns null when nothing is open, which is the caller's "render nothing".
function resolveInlineImageTarget() {
  const liveId = typeof state.selectedFlowId === "string" ? state.selectedFlowId.trim() : "";
  const historyId =
    typeof state.selectedHistoryId === "string" ? state.selectedHistoryId.trim() : "";
  const flowId = liveId || historyId;
  if (!flowId) return null;

  const found = findFlow(flowId);
  if (found) {
    const machineId = String((found.machine && found.machine.machine_id) || "");
    const projectRoot = String((found.flow && found.flow.project_root) || "");
    if (machineId && projectRoot) return { kind: "project", machineId, projectRoot };
  }
  const session = (state.historySessions || []).find((s) => s && s.flow_id === flowId);
  if (session) {
    const machineId = String(session.machine_id || "");
    const projectRoot = String(session.project_root || "");
    if (machineId && projectRoot) return { kind: "project", machineId, projectRoot };
  }
  return { kind: "flow", flowId };
}

// Inline thumbnails for every image attachment a message names, or null when
// there is nothing to show (no image path, or no open flow to resolve it
// against).
//
// WHY the path text is left in place rather than replaced by the image: that
// string IS the prompt — it is what the agent read, and what removeAttachment
// edits. Swapping it for a picture would make the rendered conversation and the
// conversation the model saw two different things, which is exactly the split
// the whole attachment feature is built to avoid. The thumbnail is an addition.
//
// Every failure mode of the read-back leg (offline daemon, deleted file,
// pre-revision-6 daemon, another owner's flow) lands on the same `error` event,
// and all of them mean the same thing here: fall back to the plain path text
// the message already shows. A broken-image glyph would be strictly worse than
// the text it decorates.
function renderInlineUploadImages(content) {
  const paths = extractUploadImagePaths(content);
  if (!paths.length) return null;
  const target = resolveInlineImageTarget();
  if (!target) return null;

  const wrap = el("div", "inline-uploads");
  let alive = paths.length;
  for (const path of paths) {
    const url = uploadFetchUrl(path, target);
    const link = el("a", "inline-upload-link");
    link.href = url;
    // A new tab, not a navigation: the console is a long-lived view holding
    // live websocket state, and leaving it to look at a screenshot would drop
    // the flow the reader is watching.
    link.target = "_blank";
    link.rel = "noopener";
    const img = el("img", "inline-upload-img");
    img.src = url;
    img.alt = path.slice(path.lastIndexOf("/") + 1);
    img.title = path;
    img.loading = "lazy";
    let failed = false;
    img.addEventListener("error", () => {
      if (failed) return;
      failed = true;
      link.classList.add("hidden");
      alive -= 1;
      // The container carries margin of its own, so an all-failed message would
      // otherwise keep a gap where the images are not.
      if (alive <= 0) wrap.classList.add("hidden");
    });
    link.appendChild(img);
    wrap.appendChild(link);
  }
  return wrap;
}

// Built-in English for the parameterless upload messages, mirroring the en-US
// pack. tf()'s fallback is a plain literal (it is NOT interpolated), so each
// key needs one here — and without them a dictionary-less environment (the boot
// fetch failed, or the Node test harness) would paint the raw dotted key.
const UPLOAD_ERROR_FALLBACKS = {
  "upload.errNoTarget": "Choose a machine and a project before attaching files.",
  "upload.errNoFlow": "Open a flow before attaching files to a reply.",
  "upload.errUnregisteredProject": "That project is not available on the target machine"
    + " — re-add it under Projects and try again.",
  "upload.errUnsupportedDaemon": "That machine runs an older daemon that cannot receive"
    + " files. Upgrade it and try again.",
  "upload.errNotConnected": "That machine is offline right now — reconnect it and try again.",
  "upload.errTimeout": "The machine did not answer in time. Try again in a moment.",
  "upload.errWriteFailed": "The machine could not save the file to disk.",
  "upload.errInvalidFilename": "That file name cannot be used — rename the file and try again.",
  "upload.errNetwork": "The connection to the server dropped before the file arrived.",
};

// Localized copy for one rejected/failed file. `prose` is the server's English
// detail, used only as a last-resort fallback — the wire carries codes precisely
// so the user never reads untranslated backend text.
function uploadFailureText(name, code, prose) {
  const label = String(name == null ? "" : name);
  const key = uploadErrorKey(code);
  const limit = formatFileSize(MAX_UPLOAD_BYTES);
  if (key === "upload.errTooLarge") {
    return tf(key, `“${label}” is larger than the ${limit} limit.`, { name: label, limit });
  }
  if (key === "upload.errFailed") {
    const message = prose || code || "";
    return tf(key, `Could not attach “${label}”: ${message}`, { name: label, message });
  }
  return tf(key, UPLOAD_ERROR_FALLBACKS[key] || prose || key, { name: label, limit });
}

// Object-URL lifecycle for image previews. Both ends are guarded because the
// DOM-stub test environment has no Blob-backed URL factory — a missing preview
// costs a thumbnail, never an upload.
function createPreviewUrl(file) {
  try {
    if (typeof URL !== "undefined" && typeof URL.createObjectURL === "function") {
      return URL.createObjectURL(file);
    }
  } catch (_) {
    /* noop */
  }
  return "";
}

function revokePreviewUrl(entry) {
  const url = entry && typeof entry.previewUrl === "string" ? entry.previewUrl : "";
  if (!url) return;
  entry.previewUrl = "";
  try {
    if (typeof URL !== "undefined" && typeof URL.revokeObjectURL === "function") {
      URL.revokeObjectURL(url);
    }
  } catch (_) {
    /* noop */
  }
}

// Literal first-occurrence replace applied to a live field, keeping the caret
// anchored to the text the user is actually editing. Returns whether the span
// was still there: a false answer means the user deleted the placeholder while
// the request was in flight, and the caller must NOT re-insert anything — the
// deletion is a deliberate "cancel this one", and appending the path elsewhere
// would put text somewhere the user never asked for.
function replaceInInputOnce(inputEl, token, replacement) {
  if (!inputEl || typeof inputEl !== "object") return false;
  const before = String(inputEl.value == null ? "" : inputEl.value);
  const needle = String(token == null ? "" : token);
  if (!needle) return false;
  const at = before.indexOf(needle);
  if (at < 0) return false;
  const text = String(replacement == null ? "" : replacement);
  inputEl.value = before.slice(0, at) + text + before.slice(at + needle.length);
  const delta = text.length - needle.length;
  const shift = (pos) => {
    const p = Number(pos);
    if (!Number.isFinite(p)) return p;
    if (p >= at + needle.length) return p + delta;
    // A caret parked inside the span being rewritten has nowhere faithful to
    // land; the end of the replacement is the least surprising place.
    if (p > at) return at + text.length;
    return p;
  };
  inputEl.selectionStart = shift(inputEl.selectionStart);
  inputEl.selectionEnd = shift(inputEl.selectionEnd);
  return true;
}

// Hard ceiling on one upload request, from the click to the daemon's answer.
//
// WHY a bound is mandatory rather than a nicety: an in-flight row is what the
// submit gate (pendingUploadRefusal) refuses to send past, so a request that
// neither resolves nor rejects — a connection that dies mid-POST, which the
// browser may sit on for minutes before it gives up — would hold the operator's
// whole drafted prompt hostage, including an answer a flow is blocked waiting
// for. The timeout guarantees the row always settles to `error` on its own; the
// strip's cancel button is the fast escape for anyone unwilling to wait it out.
// Generous because the ceiling has to clear a 20 MiB body on a slow uplink plus
// the server's own 10s wait for the daemon ack — it exists to stop a hang, not
// to police a slow link.
const UPLOAD_REQUEST_TIMEOUT_MS = 180000;

// Upload one file: park a placeholder at the caret, POST the bytes, then swap
// the placeholder for the project-relative path the daemon reports. Everything
// the prompt will carry is written into the text HERE; submit-time code never
// rewrites that text (it only refuses to send while a placeholder is still
// outstanding), so a failure can always be undone by simply taking the
// placeholder back out.
async function performUpload(inputEl, file, target, stripId) {
  if (!inputEl || !file || !target || !target.ok) return null;
  const cfg = uploadScopeForStrip(stripId);
  state.uploadSeq = (Number(state.uploadSeq) || 0) + 1;
  const seq = state.uploadSeq;
  const name = (typeof file.name === "string" && file.name.trim()) || "file";
  const token = uploadPlaceholderToken(name, seq);

  const controller = typeof AbortController === "function" ? new AbortController() : null;
  const entry = {
    id: "upload-" + seq,
    name,
    size: Number(file.size) || 0,
    type: typeof file.type === "string" ? file.type : "",
    status: "uploading",
    path: "",
    code: "",
    previewUrl: isImageFile(file) ? createPreviewUrl(file) : "",
    token,
    // The handle abortUploadEntry pulls to hang up a request whose row is being
    // abandoned, and the flag that keeps the answer of an already-abandoned
    // request from repainting a row that is no longer on screen. The flag is
    // the load-bearing half: a fetch that has already resolved cannot be
    // aborted, so `canceled` is what makes the race safe either way.
    controller,
    canceled: false,
  };
  insertAtCaret(inputEl, token);
  attachmentEntries(stripId).push(entry);
  // Recorded at insert time, not on completion: an in-flight row is just as
  // bound to this target as a settled one, and a target change while it is in
  // flight must retire it too.
  rememberUploadTarget(stripId, target);
  renderAttachmentStrip(stripId);
  syncUploadInput(cfg);

  // Whoever gets here first owns the row's outcome. Three parties race for it —
  // the response, the timeout, and a cancel click — and the two that lose must
  // do nothing at all, or a late arrival would re-toast an outcome the user has
  // already seen (or repaint a row they dismissed).
  let settled = false;
  const claim = () => {
    if (settled || entry.canceled) return false;
    settled = true;
    clearTimeout(timer);
    return true;
  };

  // Both failure exits do the same two things: take the placeholder back out of
  // the text (a stranded token would otherwise ship to the agent as prompt
  // prose) and leave the row behind in its error state so the user can see
  // WHICH file failed after the toast is gone. An error row is also, unlike an
  // in-flight one, dismissable and no longer blocks the submit gate.
  const fail = (code, prose) => {
    if (!claim()) return null;
    replaceInInputOnce(inputEl, token, "");
    entry.status = "error";
    entry.code = code || "";
    entry.controller = null;
    revokePreviewUrl(entry);
    renderAttachmentStrip(stripId);
    syncUploadInput(cfg);
    showToast("error", uploadFailureText(name, code, prose));
    return null;
  };

  // Settling the row here rather than merely aborting matters when the runtime
  // has no AbortController: the request may keep running, but the row — and
  // with it the submit gate — no longer waits on it.
  const timer = setTimeout(() => {
    if (controller && typeof controller.abort === "function") {
      try {
        controller.abort();
      } catch (_) { /* nothing left to hang up — fail() still settles the row */ }
    }
    fail("timeout", "");
  }, UPLOAD_REQUEST_TIMEOUT_MS);

  let resp;
  try {
    resp = await authedFetch(uploadRequestUrl(target, name), {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream" },
      body: file,
      signal: controller ? controller.signal : undefined,
    });
  } catch (_) {
    // An abort lands here too, and both aborters have already settled the row:
    // the timeout painted its error, and a cancel took the whole row away. Only
    // a genuinely dropped connection still has an outcome to report.
    return fail("network", "");
  }

  if (!resp || resp.status !== 201) {
    const detail = (resp && (await resp.json().catch(() => ({})))) || {};
    const code = (detail && typeof detail.error_code === "string" && detail.error_code) || "";
    const prose = (detail && typeof detail.detail === "string" && detail.detail) || "";
    return fail(code, prose);
  }

  const body = (await resp.json().catch(() => ({}))) || {};
  const path = (typeof body.path === "string" && body.path.trim()) || "";
  if (!path) return fail("", "");

  // Too late to matter: the row was given up on (timed out, or cancelled) and
  // its placeholder is already out of the text. The bytes did land on the
  // project machine — content-addressed, so a re-paste finds them again — but
  // planting a path now would resurrect an attachment the user watched leave.
  if (!claim()) return null;

  // A miss here is the "user deleted the placeholder mid-flight" case: the file
  // IS stored (the daemon already wrote it), so the row still settles to done —
  // it just has no path in the text to point at, which attachmentRowModel reads
  // as "nothing to remove".
  const landed = replaceInInputOnce(inputEl, token, path);
  entry.status = "done";
  entry.code = "";
  entry.path = landed ? path : "";
  entry.controller = null;
  renderAttachmentStrip(stripId);
  syncUploadInput(cfg);
  return entry;
}

// Render one strip from its entries. Called after every lifecycle transition;
// cheap enough to rebuild wholesale because a strip holds a handful of rows.
function renderAttachmentStrip(stripId, entries) {
  const strip = $(stripId);
  if (!strip) return;
  const rows = Array.isArray(entries) ? entries : attachmentEntries(stripId);
  strip.innerHTML = "";
  if (!rows.length) {
    strip.classList.add("hidden");
    return;
  }
  strip.classList.remove("hidden");
  for (const entry of rows) {
    const model = attachmentRowModel(entry);
    const item = el("div", "attachment-item " + model.status);
    if (model.isImage && model.previewUrl) {
      const img = el("img", "attachment-thumb");
      img.src = model.previewUrl;
      img.alt = model.name;
      item.appendChild(img);
    } else {
      item.appendChild(el("span", "attachment-icon", "📄"));
    }
    const meta = el("div", "attachment-meta");
    // The secondary line carries the stored file's identifying hash next to the
    // size once the file has landed — see attachmentRowModel for why the
    // browser-side name alone cannot identify a row, and why only the hash head
    // of the stored name is worth the width. Two nested spans because the
    // clipping (ellipsis) and the hover scroll have to live on different
    // elements: the outer cell owns the width, the inner text is what slides
    // inside it.
    const sizeCell = el("span", "attachment-size");
    sizeCell.appendChild(el(
      "span",
      "attachment-size-text",
      model.storedName
        ? model.sizeText + " · " + model.storedName
        : model.statusText || model.sizeText,
    ));
    meta.append(el("span", "attachment-name", model.name), sizeCell);
    item.appendChild(meta);
    // The whole row is the tooltip target for the stored path: it is the exact
    // string sitting in the prompt, so a user matching a row against the text
    // can copy/compare it without opening anything.
    if (model.titleText) item.title = model.titleText;
    // The failure prose lives in the tooltip, not in the row: the strip is one
    // scrolling line and a full sentence would push the rest of it off-screen.
    // The toast already said it out loud once.
    if (model.errorKey) item.title = uploadFailureText(model.name, entry && entry.code, "");
    // EVERY row is dismissable, whatever its state. For a failed row the × is
    // the only way to clear it, since its placeholder is already gone. For a row
    // still in flight it is the escape hatch from the submit gate: that gate
    // refuses to send while anything is uploading, so without a control here a
    // request whose answer never comes would hold the drafted prompt hostage
    // until the request timeout — including a reply a flow is blocked on. The
    // two do different things, which is why the labels differ: one edits a path
    // out of the text, the other gives up on a request still running.
    const btn = el("button", "attachment-remove", "×");
    btn.type = "button";
    if (model.canCancel) {
      btn.title = tf("upload.cancelTitle", "Cancel this upload and drop it from the text");
      btn.addEventListener("click", () => cancelAttachment(stripId, model.id));
    } else {
      btn.title = tf("upload.removeTitle", "Remove this attachment from the text");
      btn.addEventListener("click", () => removeAttachment(stripId, model.id));
    }
    item.appendChild(btn);
    strip.appendChild(item);
  }
}

// Hang up an in-flight row's request and mark it abandoned.
//
// The flag, not the abort, is what makes this safe: a response already on its
// way (or one from a runtime with no AbortController) cannot be recalled, and
// `canceled` is what tells performUpload that the outcome it is holding belongs
// to a row nobody is looking at any more.
function abortUploadEntry(entry) {
  if (!entry || typeof entry !== "object") return;
  entry.canceled = true;
  const controller = entry.controller;
  entry.controller = null;
  if (controller && typeof controller.abort === "function") {
    try {
      controller.abort();
    } catch (_) { /* already finished — nothing left to hang up */ }
  }
}

// Give up on a row that is still uploading: stop the request, take its
// placeholder back out of the text, drop the row.
//
// WHY this exists as a distinct gesture: an uploading row blocks every send
// from its prompt box (pendingUploadRefusal), and the request that would clear
// it can stall for as long as the browser is willing to wait on a dead socket.
// Without a way out, the only escape would be closing the view — which blanks
// the drafted prompt along with the strip. Cancelling costs the attachment and
// keeps the words.
function cancelAttachment(stripId, id) {
  const rows = attachmentEntries(stripId);
  const key = String(id == null ? "" : id);
  const idx = rows.findIndex((e) => e && String(e.id) === key);
  if (idx < 0) return;
  const entry = rows[idx];
  const cfg = uploadScopeForStrip(stripId);
  const inputEl = cfg ? $(cfg.inputId) : null;
  abortUploadEntry(entry);
  // Pulling the token is both the cleanup and the second safety net: a late
  // answer plants its path only where the token still is, so removing it turns
  // any reply that still arrives into a no-op on the text.
  if (inputEl && entry && entry.token) {
    replaceInInputOnce(inputEl, entry.token, "");
    syncUploadInput(cfg);
  }
  revokePreviewUrl(entry);
  rows.splice(idx, 1);
  renderAttachmentStrip(stripId);
}

function removeAttachment(stripId, id) {
  const rows = attachmentEntries(stripId);
  const key = String(id == null ? "" : id);
  const idx = rows.findIndex((e) => e && String(e.id) === key);
  if (idx < 0) return;
  const entry = rows[idx];
  const model = attachmentRowModel(entry);
  const cfg = uploadScopeForStrip(stripId);
  const inputEl = cfg ? $(cfg.inputId) : null;
  // WHY nothing is deleted on the project machine: the bytes already landed in
  // the project's (gitignored) uploads directory, content-addressed and shared
  // by every prompt that named the same file — an earlier, already-submitted
  // prompt may still be pointing at this exact path, and a running agent may be
  // about to read it. So removal here is strictly a TEXT edit, "I no longer
  // want to mention this path", and it drops exactly one occurrence plus the
  // row mirroring it. Reaping stored bytes is the project's own housekeeping,
  // never a click in a browser that could silently break another prompt.
  if (inputEl && model.canRemove) {
    inputEl.value = removePathOnce(String(inputEl.value == null ? "" : inputEl.value), model.path);
    syncUploadInput(cfg);
  }
  revokePreviewUrl(entry);
  rows.splice(idx, 1);
  renderAttachmentStrip(stripId);
}

// Drop every row of a strip. Called when the text those rows mirror is itself
// gone (the New Task modal opens/submits, a reply is sent) — same boundary as
// removeAttachment: UI only, the stored files stay where the daemon put them.
function clearAttachments(stripId) {
  const rows = attachmentEntries(stripId);
  for (const entry of rows) {
    // An in-flight row goes too, so its request is hung up and its answer
    // disarmed — otherwise it would keep the connection busy on behalf of a
    // strip that no longer exists, and repaint rows nobody can see.
    abortUploadEntry(entry);
    revokePreviewUrl(entry);
  }
  rows.length = 0;
  if (state.uploadTargets && typeof state.uploadTargets === "object") {
    delete state.uploadTargets[String(stripId || "")];
  }
  renderAttachmentStrip(stripId);
}

// Drop every row AND take back the text those rows planted. This is the harsher
// sibling of clearAttachments, for the case where the text is NOT already gone:
// the destination the paths were resolved against has stopped being the one the
// prompt will run in, so every path in the box is now unresolvable. Leaving them
// would ship a prompt naming files that do not exist under the new project root
// — a silent failure the agent discovers and the user never sees. Returns how
// many rows were discarded so the caller can decide whether to say so out loud.
function discardAttachments(stripId) {
  const rows = attachmentEntries(stripId);
  if (!rows.length) return 0;
  const count = rows.length;
  const cfg = uploadScopeForStrip(stripId);
  const inputEl = cfg ? $(cfg.inputId) : null;
  if (inputEl) {
    for (const entry of rows) {
      const model = attachmentRowModel(entry);
      if (model.canRemove && model.path) {
        inputEl.value = removePathOnce(String(inputEl.value == null ? "" : inputEl.value), model.path);
      } else if (model.status === "uploading" && entry && entry.token) {
        // Still in flight: the placeholder is what the text carries, and pulling
        // it out now is also what disarms the late answer — performUpload plants
        // its path only where the token still is, so a missing token turns the
        // arriving reply into a no-op instead of a path for the wrong project.
        replaceInInputOnce(inputEl, entry.token, "");
      }
    }
    syncUploadInput(cfg);
  }
  clearAttachments(stripId);
  return count;
}

// Remember which machine+project a strip's rows were uploaded into, so a later
// target change can be recognised as one. Keyed by strip rather than per row
// because a change discards the whole strip: every row in it shares the target.
function rememberUploadTarget(stripId, target) {
  if (!state.uploadTargets || typeof state.uploadTargets !== "object") state.uploadTargets = {};
  if (!target || target.kind !== "project") return;
  state.uploadTargets[String(stripId || "")] = uploadTargetKey(target.machineId, target.projectRoot);
}

function uploadTargetKey(machineId, projectRoot) {
  return String(machineId == null ? "" : machineId) + " " + String(projectRoot == null ? "" : projectRoot);
}

// Normalize a FileList (or the array a test hands in) to a plain array.
function filesFromList(list) {
  if (!list) return [];
  if (Array.isArray(list)) return list.filter(Boolean);
  const out = [];
  const n = Number(list.length) || 0;
  for (let i = 0; i < n; i += 1) {
    if (list[i]) out.push(list[i]);
  }
  return out;
}

// Files carried by a paste. `clipboardData.files` is the modern spelling;
// `items` is the fallback still needed for pasted screenshots in some browsers,
// where the image arrives as an item of kind "file" with no `files` entry.
function filesFromClipboard(clipboardData) {
  if (!clipboardData) return [];
  const direct = filesFromList(clipboardData.files);
  if (direct.length) return direct;
  const items = clipboardData.items;
  if (!items) return [];
  const out = [];
  const n = Number(items.length) || 0;
  for (let i = 0; i < n; i += 1) {
    const item = items[i];
    if (!item || item.kind !== "file" || typeof item.getAsFile !== "function") continue;
    const file = item.getAsFile();
    if (file) out.push(file);
  }
  return out;
}

// True when a drag is carrying files rather than text. Dragging a selection
// around inside the textarea is a normal editing gesture and must keep working,
// so the drop handling only claims the event when files are actually involved.
function dragCarriesFiles(event) {
  const dt = event && event.dataTransfer;
  if (!dt) return false;
  if (filesFromList(dt.files).length) return true;
  const types = dt.types;
  if (!types) return false;
  const list = Array.isArray(types) ? types : Array.prototype.slice.call(types);
  return list.some((t) => String(t) === "Files");
}

function setDropActive(scope, active) {
  const cfg = uploadScope(scope);
  const input = cfg ? $(cfg.inputId) : null;
  if (input && input.classList) input.classList.toggle("drop-active", Boolean(active));
}

// Common entry point for all three gestures. Returns the started uploads so a
// caller (and the tests) can await the batch.
function startUploads(scope, files) {
  const cfg = uploadScope(scope);
  const input = cfg ? $(cfg.inputId) : null;
  const list = filesFromList(files);
  const started = [];
  if (!cfg || !input || !list.length) return started;

  // Resolved ONCE per gesture: every file of one paste goes to the same place,
  // and a per-file toast for the same missing target would bury the input.
  const target = resolveUploadTarget(scope);
  if (!target.ok) {
    showToast("error", uploadFailureText("", target.code, ""));
    return started;
  }
  for (const file of list) {
    const verdict = validateUploadFile(file);
    if (!verdict.ok) {
      // Rejected before a single byte leaves the machine — this is the whole
      // point of the browser-side bound; the server and daemon re-check it.
      showToast("error", uploadFailureText(file && file.name, verdict.code, ""));
      continue;
    }
    started.push(performUpload(input, file, target, cfg.stripId));
  }
  return started;
}

function handleInputPaste(event, scope) {
  const files = filesFromClipboard(event && event.clipboardData);
  // No files → an ordinary text paste. Leave the event completely alone so the
  // browser's own insertion (and its undo entry) behaves exactly as before.
  if (!files.length) return [];
  if (event && typeof event.preventDefault === "function") event.preventDefault();
  return startUploads(scope, files);
}

function handleInputDragOver(event, scope) {
  if (!dragCarriesFiles(event)) return false;
  // Without this the browser navigates away to the dropped file.
  if (event && typeof event.preventDefault === "function") event.preventDefault();
  setDropActive(scope, true);
  return true;
}

function handleInputDrop(event, scope) {
  setDropActive(scope, false);
  const files = filesFromList(event && event.dataTransfer && event.dataTransfer.files);
  if (!files.length) return [];
  if (event && typeof event.preventDefault === "function") event.preventDefault();
  return startUploads(scope, files);
}

// Bind one scope's four controls. Tolerates missing nodes so the flow-view
// bindings do not have to know whether that markup is present.
function bindUploadScope(scope) {
  const cfg = uploadScope(scope);
  if (!cfg) return;
  const input = $(cfg.inputId);
  if (input && typeof input.addEventListener === "function") {
    input.addEventListener("paste", (e) => handleInputPaste(e, scope));
    input.addEventListener("dragover", (e) => handleInputDragOver(e, scope));
    input.addEventListener("dragleave", () => setDropActive(scope, false));
    input.addEventListener("drop", (e) => handleInputDrop(e, scope));
  }
  const picker = $(cfg.fileInputId);
  const button = $(cfg.attachBtnId);
  if (button && picker && typeof button.addEventListener === "function") {
    button.addEventListener("click", () => picker.click());
  }
  if (picker && typeof picker.addEventListener === "function") {
    picker.addEventListener("change", () => {
      const files = filesFromList(picker.files);
      // Cleared BEFORE the uploads start: `change` fires only when the value
      // actually changes, so leaving the last selection in place would make
      // picking the same file twice in a row silently do nothing.
      picker.value = "";
      startUploads(scope, files);
    });
  }
}

// View model for one user-management row. Normalizes the label / provider /
// admin presentation and — mirroring the server-side guards — decides which
// per-row actions are offered. This is purely a UX projection; every action is
// independently re-enforced by the backend (`require_owner` + admin + self /
// last-admin / break-glass / local-only checks), so a tampered frontend can
// never bypass a rule. `currentOwnerId` is the signed-in admin's own owner_id.
//
// Rules encoded here (kept in lockstep with app.py's /api/users guards):
//   * is_self  → cannot delete or toggle-admin yourself (self-lockout guard).
//   * provider must be local to reset a password (the backend serializes
//     `can_reset_password`; we also fall back to a `provider === "local"`
//     check so the model is robust if that flag is absent).
//   * the break-glass subject is already filtered out server-side and never
//     appears in the list, so no per-row break-glass handling is needed here.
function userRowModel(user, currentOwnerId) {
  user = user && typeof user === "object" ? user : {};
  const ownerId = typeof user.owner_id === "string" ? user.owner_id : "";
  const isSelf = Boolean(ownerId) && ownerId === currentOwnerId;
  const isAdmin = Boolean(user.is_admin);
  const provider = typeof user.provider === "string" ? user.provider : "";
  const isLocal = provider === "local";
  // Prefer the backend's explicit flag; fall back to the provider check so the
  // model degrades gracefully when an older payload omits `can_reset_password`.
  const canResetPassword =
    user.can_reset_password != null ? Boolean(user.can_reset_password) : isLocal;
  const label =
    (typeof user.display_name === "string" && user.display_name.trim()) ||
    ownerId ||
    "(unknown)";
  return {
    ownerId,
    label,
    provider: provider || "—",
    isSelf,
    isAdmin,
    isLocal,
    adminLabel: isAdmin ? "admin" : "user",
    adminClass: isAdmin ? "admin" : "user",
    // You may delete anyone except yourself; last-admin / break-glass are
    // additionally caught server-side (and surfaced as a toast on failure).
    canDelete: Boolean(ownerId) && !isSelf,
    // Password reset is local-only; never offered for yourself either (self
    // password change is out of scope for this admin panel).
    canResetPassword: Boolean(ownerId) && canResetPassword,
    // You may flip anyone's admin flag except your own (no self-demotion).
    canToggleAdmin: Boolean(ownerId) && !isSelf,
    // The label/intent of the admin toggle action for this row.
    toggleAdminTo: !isAdmin,
    toggleAdminLabel: isAdmin ? "Remove admin" : "Set as admin",
  };
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

// ---------------------------------------------------------------------------
// I18N — client-side UI-language subsystem
// ---------------------------------------------------------------------------
//
// WebUI interface language is a per-user client preference (localStorage), NOT
// tied to any project's se3.yaml — this is a multi-tenant control plane, so a
// single project's language must never dictate the operator's chrome. en-US is
// the baseline dictionary and the per-key fallback; the selected language is
// layered over it in t(). A missing key falls back to en-US, then to the key
// itself, and a failed fetch degrades to an empty dict — so a network hiccup or
// an untranslated key never blanks the UI: index.html keeps English text as the
// in-markup default until a dictionary paints over it. Adding a language is a
// pure data change: drop a new static/i18n/<code>.json and the server's
// /i18n/index.json manifest (derived from the locale files on disk) advertises
// it — SUPPORTED below is only the offline bootstrap registry, used until the
// manifest lands (or if it cannot be fetched, e.g. a plain static host).

const I18N = {
  SUPPORTED: ["en-US", "zh-CN"],
  // Endonyms (each language's own name) for the switcher options, seeded for the
  // bootstrap registry and replaced by the manifest's labels once it loads.
  LABELS: { "en-US": "English", "zh-CN": "中文" },
  FALLBACK: "en-US",
  STORAGE_KEY: "se3_ui_lang",
  lang: "en-US",
  // Loaded flat key→string dictionaries, keyed by language code. en-US is the
  // always-present baseline; the active language is layered over it in t().
  dicts: { "en-US": {}, "zh-CN": {} },
  // Optional re-render hook invoked after a language switch so dynamic UI can
  // repaint. Wired in init(); a no-op / undefined in the require-loaded module.
  onLangChange: null,

  // Pure: choose the initial language from a stored preference, the browser's
  // navigator.language, and the supported-language list. Precedence:
  // localStorage > navigator.language > en-US.
  // WHY: the two layers match differently on purpose. A stored preference is an
  // explicit user choice, so it is honored only when it names a supported
  // language exactly (case-insensitively) — an unsupported stored code lands on
  // en-US rather than being silently re-pointed at a same-primary-subtag
  // neighbour ("zh-TW" must NOT become "zh-CN") and never falls through to the
  // lower-priority browser locale. navigator.language, by contrast, is a mere
  // hint about the environment, so guessing by primary subtag ("zh" / "zh-TW"
  // → "zh-CN") is the desired auto-detection there.
  resolveInitialLang(stored, navLang, supported) {
    const list = Array.isArray(supported) && supported.length
      ? supported : ["en-US"];
    const base = list.includes("en-US") ? "en-US" : list[0];
    const exact = (code) => {
      const c = String(code || "").toLowerCase();
      if (!c) return null;
      for (const item of list) {
        if (item.toLowerCase() === c) return item;
      }
      return null;
    };
    if (stored) return exact(stored) || base;
    const c = String(navLang || "");
    const hit = exact(c);
    if (hit) return hit;
    const prim = c.split("-")[0].toLowerCase();
    if (prim) {
      for (const item of list) {
        if (item.split("-")[0].toLowerCase() === prim) return item;
      }
    }
    return base;
  },

  // Pure: resolve `key` against the active dict, then the baseline dict, then
  // the key itself; interpolate {name} placeholders from `params`. Never throws
  // — a malformed template returns the un-interpolated string.
  lookup(key, params, primary, fallback) {
    let tmpl = (primary && primary[key] != null) ? primary[key]
      : (fallback && fallback[key] != null) ? fallback[key] : key;
    tmpl = String(tmpl);
    if (params && typeof params === "object") {
      try {
        tmpl = tmpl.replace(/\{(\w+)\}/g, (m, k) =>
          (params[k] != null ? String(params[k]) : m));
      } catch (_) { /* fail-safe: return the un-interpolated template */ }
    }
    return tmpl;
  },

  t(key, params) {
    return I18N.lookup(
      key, params, I18N.dicts[I18N.lang], I18N.dicts[I18N.FALLBACK]);
  },

  // Resolve `key` to a translation, or null when it is absent from BOTH the
  // active and baseline dictionaries. applyStaticTranslations uses this (rather
  // than t()) so a total miss — e.g. a boot-time fetch failure that leaves the
  // dicts empty — leaves the node's in-markup English fallback untouched instead
  // of painting the raw dotted key over it. t() still returns the key itself for
  // dynamic (JS-generated) text, where a visible key is the fixable fallback.
  resolve(key, params) {
    const p = I18N.dicts[I18N.lang];
    const f = I18N.dicts[I18N.FALLBACK];
    if (p && p[key] != null) return I18N.lookup(key, params, p, f);
    if (f && f[key] != null) return I18N.lookup(key, params, f, f);
    return null;
  },

  // Pure: normalize a language manifest into [{code, label}]. Accepts either the
  // server's {languages: [{code, label}]} shape or a bare ["en-US", ...] array,
  // and returns [] for anything unusable so the caller keeps its bootstrap
  // registry. en-US is force-included: it is the per-key fallback dictionary, so
  // it must always be loadable even if a deployment's manifest omits it.
  parseManifest(data) {
    const raw = Array.isArray(data) ? data
      : (data && Array.isArray(data.languages)) ? data.languages : [];
    const out = [];
    const seen = new Set();
    for (const item of raw) {
      const code = typeof item === "string" ? item
        : (item && typeof item.code === "string") ? item.code : null;
      if (!code || seen.has(code)) continue;
      seen.add(code);
      const label = (item && typeof item.label === "string" && item.label)
        ? item.label : code;
      out.push({ code, label });
    }
    if (out.length && !seen.has(I18N.FALLBACK)) {
      out.unshift({ code: I18N.FALLBACK, label: "English" });
    }
    return out;
  },

  // Fetch the server-side language registry and adopt it as SUPPORTED/LABELS, so
  // a newly dropped locale JSON becomes selectable with no frontend edit. Never
  // rejects: a failed/empty manifest leaves the bootstrap registry in place.
  async loadManifest() {
    try {
      const resp = await fetch("/i18n/index.json");
      if (!resp.ok) throw new Error(`i18n manifest ${resp.status}`);
      const langs = I18N.parseManifest(await resp.json());
      if (langs.length) {
        I18N.SUPPORTED = langs.map((l) => l.code);
        I18N.LABELS = {};
        for (const l of langs) I18N.LABELS[l.code] = l.label;
      }
    } catch (_) { /* keep the built-in bootstrap registry */ }
    return I18N.SUPPORTED;
  },

  // Fetch one language's dictionary JSON, caching it on `dicts`. A failed fetch
  // resolves to an empty dict (never rejects) so callers degrade to the
  // baseline / in-markup English rather than throwing on a boot-time network
  // error.
  async load(code) {
    if (I18N.dicts[code] && Object.keys(I18N.dicts[code]).length) {
      return I18N.dicts[code];
    }
    try {
      // The frontend is served from the root static mount (same origin as
      // app.js / style.css), so locale files load from /i18n/, not /static/.
      const resp = await fetch(`/i18n/${code}.json`);
      if (!resp.ok) throw new Error(`i18n ${code} ${resp.status}`);
      I18N.dicts[code] = await resp.json();
    } catch (_) {
      I18N.dicts[code] = I18N.dicts[code] || {};
    }
    return I18N.dicts[code];
  },

  // Apply data-i18n / -placeholder / -title attributes across a DOM scope
  // (defaults to the whole document). No-op where querySelectorAll is absent
  // (the require-loaded module in the pure tests).
  applyStaticTranslations(root) {
    const scope = root
      || (typeof document !== "undefined" ? document : null);
    if (!scope || typeof scope.querySelectorAll !== "function") return;
    const sels = [
      "[data-i18n]", "[data-i18n-html]", "[data-i18n-placeholder]",
      "[data-i18n-title]", "[data-i18n-aria-label]",
    ];
    for (const sel of sels) {
      for (const node of scope.querySelectorAll(sel)) {
        // resolve() (not t()) so a missing key leaves the in-markup fallback.
        applyNodeTranslations(node, I18N.resolve);
      }
    }
  },

  // Switch the active language: persist the choice, ensure both the baseline
  // and target dictionaries are loaded, then repaint static + dynamic UI.
  async setLang(code) {
    const next = I18N.SUPPORTED.includes(code) ? code : I18N.FALLBACK;
    I18N.lang = next;
    try {
      if (typeof localStorage !== "undefined") {
        localStorage.setItem(I18N.STORAGE_KEY, next);
      }
    } catch (_) { /* localStorage may throw in restricted contexts */ }
    await Promise.all([I18N.load(I18N.FALLBACK), I18N.load(next)]);
    I18N.applyStaticTranslations();
    if (typeof I18N.onLangChange === "function") I18N.onLangChange();
  },
};

// Pure per-node attribute application (exported for the DOM-stub tests): reads
// data-i18n (→ textContent), data-i18n-html (→ innerHTML), data-i18n-placeholder
// (→ placeholder), and data-i18n-title (→ title) and writes the translated
// string via `tfn`. Each attribute is independent so a single node can localize
// several surfaces.
function applyNodeTranslations(node, tfn) {
  if (!node || typeof node.getAttribute !== "function") return;
  // A null/undefined result means "no translation" — leave the in-markup
  // fallback (text / placeholder / title) untouched rather than blanking it.
  const textKey = node.getAttribute("data-i18n");
  if (textKey) {
    const v = tfn(textKey);
    if (v != null) node.textContent = v;
  }
  // data-i18n-html is the escape hatch for copy that carries inline markup
  // (<strong>/<code> emphasis inside a hint paragraph): textContent would
  // destroy the child elements, so those nodes opt into an innerHTML write and
  // their catalog values carry the same inline tags. The values are our own
  // static locale JSON (never user/LLM content), so this is not an injection
  // surface — never point data-i18n-html at dynamic data.
  const htmlKey = node.getAttribute("data-i18n-html");
  if (htmlKey) {
    const v = tfn(htmlKey);
    if (v != null) node.innerHTML = v;
  }
  const phKey = node.getAttribute("data-i18n-placeholder");
  if (phKey) {
    const v = tfn(phKey);
    if (v != null) {
      node.placeholder = v;
      if (typeof node.setAttribute === "function") {
        node.setAttribute("placeholder", v);
      }
    }
  }
  const titleKey = node.getAttribute("data-i18n-title");
  if (titleKey) {
    const v = tfn(titleKey);
    if (v != null) {
      node.title = v;
      if (typeof node.setAttribute === "function") {
        node.setAttribute("title", v);
      }
    }
  }
  // Screen-reader labels: aria-label is invisible chrome, so it needs the same
  // localization path as visible copy or a zh-CN user hears English labels.
  const ariaKey = node.getAttribute("data-i18n-aria-label");
  if (ariaKey) {
    const v = tfn(ariaKey);
    if (v != null && typeof node.setAttribute === "function") {
      node.setAttribute("aria-label", v);
    }
  }
}

// Localize a DYNAMIC (JS-rendered) UI string, with the in-code literal as the
// built-in fallback. Uses I18N.resolve (not t()): when a dictionary is loaded
// the translation wins; a total miss — a boot-time fetch failure or the
// document-less unit-test environment where the dicts stay empty — returns the
// original literal instead of painting a raw dotted key. This mirrors the
// data-i18n static-fallback contract for JS-generated chrome, and keeps every
// render-time string re-resolved on a language switch (never cached at module
// load). `params` interpolates {name} placeholders in the dict template.
function tf(key, fallback, params) {
  const v = I18N.resolve(key, params);
  return v != null ? v : fallback;
}

function statusClass(status) {
  const s = String(status || "unknown").toLowerCase();
  if (["running", "completed", "failed", "paused", "init"].includes(s)) return s;
  return "unknown";
}

// Localized badge/label text for a raw flow status token. Unknown tokens (a
// status a newer daemon emits) pass through verbatim rather than resolving to a
// raw dotted key, so the UI degrades to the server's own word. Pure.
function flowStatusText(status) {
  const raw = String(status == null ? "" : status).trim();
  if (!raw) return tf("status.flow.unknown", "unknown");
  return tf("status.flow." + raw.toLowerCase(), raw);
}

// Whether a flow has started but is blocked acquiring the project's
// main-worktree mutex before its first code-touching step. The flow stays
// RUNNING — this is purely a running sub-state so a queued flow reads as
// running·等待锁 instead of appearing to silently stall on "已发布". Pure.
function isWaitingForLock(flow) {
  if (!flow || !flow.waiting_for_lock) return false;
  // Defensive: only treat it as waiting while the flow is still running, so a
  // stale flag on a since-terminal snapshot never mislabels the status.
  return String(flow.status || "").toLowerCase() === "running";
}

// Human-facing status label that folds the running waiting-for-lock sub-state
// into the displayed text as "running · waiting for lock". The worktree merge
// is no longer a wrapper sub-state on a COMPLETED body: it now runs as the
// flow's own merge_integrate / version_reconcile steps, which render through
// the normal step lifecycle, so there is no merging override here. Pure.
function flowStatusLabel(flow) {
  const base = flowStatusText(flow && flow.status);
  return isWaitingForLock(flow)
    ? `${base} · ${tf("flow.statusWaitingLock", "waiting for lock")}`
    : base;
}

// A flow is "active" while it can still consume a human interaction — it is
// either making progress (running/init/recovering) or parked awaiting one
// (paused). Completed/failed flows are terminal and accept no further input.
function isActiveFlow(flow) {
  const s = String((flow && flow.status) || "").toLowerCase();
  return ["running", "paused", "init", "recovering"].includes(s);
}

// A flow is "terminal" once it has completed or failed: it will never produce
// another step or another mid-step record, so any fallback loop watching it for
// new content can stop. Deliberately narrower than `!isActiveFlow` — a blank /
// "unknown" status is treated as transient (still loading), NOT terminal, so a
// momentarily-unknown snapshot cannot prematurely kill a live fallback loop.
function isTerminalFlow(flow) {
  const s = String((flow && flow.status) || "").toLowerCase();
  return s === "completed" || s === "failed";
}

// A flow is "resumable" when the daemon can pick it back up via
// `se3 run --resume --flow-id <id>`.  The authoritative signal is the daemon's
// `resumable` flag, computed from the flow's semantic state and surfaced even
// for a per-flow snapshot that was superseded in engine.json (such a snapshot
// may carry source `history`/`resumable` and a raw status that still reads
// `running`).  When the flag is present and true we short-circuit to true.
// Otherwise we fall back — for an older daemon that omits the flag — to the
// legacy heuristic: only FAILED/PAUSED qualify, and archived/history-only flows
// are excluded (they lack a live engine.json).  The backend
// `POST /api/flows/{id}/resume` performs the authoritative check; this pure
// function is a UI gate that hides the button when it would certainly fail.
const RESUMABLE_STATUSES = ["failed", "paused"];

function isFlowResumable(flow) {
  if (!flow || typeof flow !== "object") return false;
  if (!flow.flow_id) return false;
  // A completed flow is terminal-and-done and is never resumable, even if a
  // stale snapshot mistakenly carries resumable=true: the daemon resume
  // validator rejects a COMPLETED flow, so the completed guard takes
  // precedence over the flag (mirrors ServerState.is_flow_resumable).
  if (String(flow.status || "").toLowerCase() === "completed") return false;
  // Primary signal: the daemon's authoritative resumable flag.
  if (flow.resumable === true) return true;
  // Backward-compatible fallback for daemons that don't supply the flag.
  // Archived/history-only sessions cannot be resumed — they lack a live
  // engine.json and the server would return 404.
  const src = String(flow.source || "").toLowerCase();
  if (src === "archived" || src === "history") return false;
  return RESUMABLE_STATUSES.includes(
    String(flow.status || "").toLowerCase()
  );
}

function findFlow(flowId) {
  for (const m of state.machines) {
    for (const f of m.flows || []) {
      if (f.flow_id === flowId) return { machine: m, flow: f };
    }
  }
  return null;
}

// Pending calls for the open flow. Backend daemon aggregator filters by
// flow_id when constructing snapshots (see DaemonAggregator), but a legacy /
// older daemon may not — so re-filter here as a defensive fallback: keep a
// call when its embedded context.flow_id matches the current flow, OR when
// the context carries no flow_id at all (treated as belonging to the active
// flow rather than dropped).
function pendingCalls(flow) {
  if (!flow || !Array.isArray(flow.pending_calls)) return [];
  const currentFlowId = flow.flow_id || "";
  // Match the backend `_filter_calls_for_flow` strict semantics: when the
  // flow has a known id, only keep calls whose `context.flow_id` matches.
  // Unattributed calls (e.g. legacy `merge_*` / `sync_conflicts_*` artifacts
  // left behind by other flows in the same project root) are dropped so
  // they cannot leak into this flow's reply chip-bar.
  if (!currentFlowId) {
    return flow.pending_calls.slice();
  }
  return flow.pending_calls.filter((c) => {
    const ctx = c && c.context;
    const cfid = (ctx && typeof ctx === "object" && ctx.flow_id) || "";
    return cfid && cfid === currentFlowId;
  });
}

function hasPendingCall(flow) {
  return pendingCalls(flow).length > 0;
}

// ---------------------------------------------------------------------------
// Toast notifications
// ---------------------------------------------------------------------------
//
// Lightweight, dependency-free transient feedback. `kind` is one of
// "success" / "error" / "info"; the toast auto-dismisses after a few seconds.

function showToast(kind, message) {
  const container = $("toast-container");
  if (!container) return;
  const k = ["success", "error", "info"].includes(kind) ? kind : "info";
  const toast = el("div", "toast toast-" + k, String(message || ""));
  container.appendChild(toast);
  // Force a layout frame so the entry transition runs.
  requestAnimationFrame(() => toast.classList.add("toast-show"));
  // Errors linger a little longer than success/info messages.
  const ttl = k === "error" ? 6000 : 4000;
  setTimeout(() => {
    toast.classList.remove("toast-show");
    setTimeout(() => toast.remove(), 300);
  }, ttl);
}

// ---------------------------------------------------------------------------
// WebSocket client (with exponential-backoff reconnect)
// ---------------------------------------------------------------------------

// The badge shows LIVE connection state, so its copy cannot come from a
// data-i18n attribute: applyStaticTranslations() repaints those on every
// language switch and would rewrite a "connected" badge back to "connecting…".
// Remembering the state as an i18n KEY (not a resolved string) lets
// repaintConnStatus() re-render the CURRENT status in the new language after a
// switch, and lets the boot-time dictionary load localize whatever the WS
// lifecycle has already painted.
let connStatus = {
  kind: "connecting",
  key: "conn.connecting",
  fallback: "connecting…",
};

function setConnStatus(kind, key, fallback) {
  connStatus = { kind, key, fallback };
  repaintConnStatus();
}

// Re-render the badge from the remembered state (used after a language switch
// and once the boot-time dictionaries land).
function repaintConnStatus() {
  const node = $("conn-status");
  if (!node) return;
  node.className = "conn conn-" + connStatus.kind;
  node.textContent = tf(connStatus.key, connStatus.fallback);
}

// The browser tab title is user-visible copy, but <title> lives outside the
// body scope applyStaticTranslations walks, so it is localized explicitly (on
// boot and after every language switch) from the same key as the in-page h1.
function applyDocumentTitle() {
  if (typeof document === "undefined") return;
  document.title = tf("topbar.title", "tianluo Control Plane");
}

// Toggle the "data may be stale" banners shown over the history view and the
// running-flow view while the WebSocket connection is down.
function setStale(stale) {
  state.connStale = !!stale;
  for (const id of ["history-stale", "flow-view-stale", "issues-stale"]) {
    const node = $(id);
    if (node) node.classList.toggle("hidden", !stale);
  }
}

// fetch() wrapper that intercepts session-expiry. Any /api/* call that comes
// back 401 means the cookie session is gone (logout elsewhere / expiry); we
// kick the SPA back to the login gate so the user can re-authenticate instead
// of staring at a silently-empty dashboard. Same-origin cookies ride along
// automatically, so no Authorization header is needed.
async function authedFetch(input, init) {
  const resp = await fetch(input, init);
  if (isUnauthorizedStatus(resp.status)) {
    handleUnauthorized();
  }
  return resp;
}

// Drop back to the login gate after a 401 / explicit logout. Idempotent: a
// burst of concurrent 401s collapses to a single transition + WS teardown.
function handleUnauthorized() {
  if (state.authState === AUTH_STATES.LOGIN) return;
  state.authState = nextAuthState(state.authState, "unauthorized");
  state.identity = null;
  teardownWs();
  applyAuthState();
}

// Close the live /ws/ui socket (used on logout / session loss so a stale,
// now-unauthorized socket is not left dangling reconnecting).
function teardownWs() {
  reconnectAttempts = 0;
  if (ws) {
    try {
      ws.onclose = null;
      ws.onerror = null;
      ws.close();
    } catch (_) {
      /* noop */
    }
    ws = null;
  }
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${location.host}/ws/ui`;
  setConnStatus(
    "connecting",
    reconnectAttempts ? "conn.reconnecting" : "conn.connecting",
    reconnectAttempts ? "reconnecting…" : "connecting…",
  );

  ws = new WebSocket(url);

  ws.onopen = () => {
    // A reconnect (rather than the first connect) means the views may be
    // showing stale data — clear the banners and refresh what's open.
    const wasReconnect = reconnectAttempts > 0 || state.connStale;
    reconnectAttempts = 0;
    setConnStatus("connected", "conn.connected", "connected");
    setStale(false);
    if (wasReconnect) {
      if (state.selectedFlowId) {
        refreshFlowDetail();
        // Re-pull the conversation incrementally so records emitted during the
        // outage (whose `history_data` append deltas were never delivered) are
        // backfilled without wiping and re-rendering the whole conversation —
        // the held progress token is echoed so the server returns only the
        // delta. Mirrors the history view's incremental re-fetch below.
        loadFlowConversation(state.selectedFlowId, { incremental: true });
      }
      if (isHistoryOpen()) {
        fetchHistoryIndex();
        if (state.selectedHistoryId) {
          openHistorySession(state.selectedHistoryId, { incremental: true });
        }
      }
      if (isIssuesOpen()) {
        fetchIssues();
        fetchAllIssueTypes();
      }
    }
  };

  ws.onmessage = (event) => {
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch (_) {
      return;
    }
    if (!msg || typeof msg !== "object") return;
    // Both "snapshot" (on connect) and "status_update" carry the full list.
    if (Array.isArray(msg.machines)) {
      applyMachines(msg.machines);
    } else if (msg.type === "history_index" && Array.isArray(msg.sessions)) {
      applyHistoryIndex(msg.sessions);
    } else if (
      msg.type === "history_index_delta" &&
      (Array.isArray(msg.upserts) || Array.isArray(msg.removed))
    ) {
      // G5 differential index: only the SessionMeta rows that changed, merged
      // by flow_id into the local aggregated index instead of re-fanning the
      // whole index on any active flow's updated_at tick.
      applyHistoryIndexDelta(msg.upserts, msg.removed);
    } else if (msg.type === "history_data" && msg.flow_id) {
      applyHistoryData(msg);
    } else if (msg.type === "history_cursor" && msg.flow_id) {
      // A records-less bundle-state advisory: the server applied a frame it
      // deliberately does not relay (a cache-miss full pull, a rejected
      // truncating full). Nothing to render, but the cursor it carries is
      // precisely how a console learns the bundle holds records it never got.
      applyHistoryCursor(msg);
    } else if (msg.type === "interjection_event" && msg.call_id && msg.phase) {
      applyInterjectionEvent(msg);
    } else if (msg.type === "spawn_failed") {
      applySpawnFailed(msg);
    }
  };

  ws.onclose = () => {
    setConnStatus("disconnected", "conn.disconnected", "disconnected");
    setStale(true);
    scheduleReconnect();
  };

  ws.onerror = () => {
    // onclose will follow and trigger the reconnect.
    try { ws.close(); } catch (_) { /* noop */ }
  };
}

function scheduleReconnect() {
  // Exponential backoff: 1s, 2s, 4s … capped at 30s — mirrors the daemon
  // client's reconnect policy.
  const delay = Math.min(30000, 1000 * Math.pow(2, reconnectAttempts));
  reconnectAttempts += 1;
  setTimeout(connect, delay);
}

// ---------------------------------------------------------------------------
// State application
// ---------------------------------------------------------------------------

// FIFO-bind the oldest unbound local interjection entry to a real call_id.
// Used by `applyInterjectionEvent` on the `pending` phase: when the daemon
// reports a new interjection call file, we assume the oldest unbound local
// entry corresponds to it (a Send press created the file). Idempotent: if
// the `callId` is already bound to some local entry the call is a no-op,
// so a duplicate event or a STATUS_UPDATE replay never re-binds.
function bindLocalInterjectionToCallId(callId) {
  if (!callId) return;
  const list = state.localInterjections || [];
  if (list.some((e) => e.callId === callId)) return;
  const target = list.find((e) => !e.callId);
  if (target) target.callId = callId;
}

// Drop the local interjection entry that was bound to `callId`. Used by
// `applyInterjectionEvent` on the `consumed` phase: once the run loop has
// drained the interjection there is nothing left to display, so the local
// entry is removed and its synthetic chip disappears on the next render.
function consumeLocalInterjectionByCallId(callId) {
  if (!callId) return;
  state.localInterjections = (state.localInterjections || []).filter(
    (e) => e.callId !== callId,
  );
}

// Handle a ws-pushed `interjection_event`. Two phases are emitted by the
// server: `pending` when an interjection call file first appears in a flow's
// pending_calls; `consumed` when it disappears (run loop drained it).
// We record the phase per call_id so chip rendering can show pending /
// consumed visual states, dedup toasts against `interjectionToastsSeen`,
// and settle a pending Send if it was waiting on this very call_id.
function applyInterjectionEvent(msg) {
  const callId = String(msg.call_id || "");
  const phase = String(msg.phase || "");
  if (!callId || !phase) return;
  const isOpenFlow = !!(msg.flow_id && state.selectedFlowId === msg.flow_id);
  // Phase recording, local-entry binding/consumption, and toasts are all
  // scoped to the open flow — an `interjection_event` for some other flow
  // is irrelevant to this tab's intervention bar and must not silently
  // mutate the open flow's local bookkeeping.
  if (!isOpenFlow) return;
  // (call_id, phase) dedup: a STATUS_UPDATE replay on reconnect or a
  // duplicate broadcast would otherwise re-apply the consumed phase to
  // already-removed local entries, or re-bind an already-bound call_id.
  const seenKey = callId + ":" + phase;
  if (state.interjectionEventSeen[seenKey]) return;
  state.interjectionEventSeen[seenKey] = true;

  state.interjectionPhases[callId] = phase;

  if (phase === "pending") {
    bindLocalInterjectionToCallId(callId);
  } else if (phase === "consumed") {
    consumeLocalInterjectionByCallId(callId);
  }

  // Dedup: one toast per (call_id, phase) — phase transitions only fire
  // once each, but a STATUS_UPDATE replay on reconnect would otherwise
  // double-toast.
  const toastKey = callId + ":" + phase;
  if (isOpenFlow && !state.interjectionToastsSeen[toastKey]) {
    state.interjectionToastsSeen[toastKey] = true;
    if (phase === "pending") {
      showToast("info", tf("toast.interjectionDelivered", "Interjection delivered — waiting for the flow to consume it"));
    } else if (phase === "consumed") {
      showToast("success", tf("toast.interjectionConsumed", "Interjection consumed"));
    }
  }

  // On `consumed`, register a brief afterimage so the chip visually
  // transitions through `state-consumed` before vanishing. The chip is
  // about to drop out of `pending_calls` on the next STATUS_UPDATE; without
  // this the user would see the chip flicker away with no transition.
  if (phase === "consumed" && isOpenFlow) {
    state.interjectionConsumedAfterimages = (
      state.interjectionConsumedAfterimages || []
    ).filter((a) => a.callId !== callId);
    state.interjectionConsumedAfterimages.push({
      callId: callId,
      prompt: String(msg.text || ""),
      untilTs: Date.now() + INTERJECTION_CONSUMED_AFTERIMAGE_MS,
    });
    // Schedule a re-render after the afterimage expires so the chip is
    // removed even if no further ws message arrives in the interim.
    setTimeout(() => {
      if (state.selectedFlowId !== msg.flow_id) return;
      // Drop expired afterimages.
      const now = Date.now();
      state.interjectionConsumedAfterimages = (
        state.interjectionConsumedAfterimages || []
      ).filter((a) => a.untilTs > now);
      if (state.flowDetail) renderInterventions(state.flowDetail);
    }, INTERJECTION_CONSUMED_AFTERIMAGE_MS + 100);
  }

  // Either phase confirms the daemon side observed our work — settle a
  // matching pending Send. We match on call_id (a real call's id), and we
  // also settle synthetic interject submissions as soon as a pending event
  // fires for any interjection in this flow (the synthetic "id" never
  // matches a real call_id, so use that broader trigger).
  if (isOpenFlow) {
    if (state.pendingSendSettleKey) {
      if (
        state.pendingSendSettleKey === callId ||
        (state.pendingSendSettleKey === "synthetic-interject" &&
          phase === "pending")
      ) {
        settlePendingSend();
      }
    }
    // The real chip has materialized via this `pending` event — drop the
    // synthetic placeholder so the user sees the real one in its place.
    if (phase === "pending" && state.flowSyntheticInterjectPending) {
      state.flowSyntheticInterjectPending = false;
      state.flowInterjectRequested = false;
      if (state.flowReplyTargetId === "interjection:new") {
        state.flowReplyTargetId = null;
      }
    }
  }

  // Re-render so the chip picks up the new state-pending / state-consumed
  // class. `state.flowDetail` may be null right after open/reset; in that
  // case the next refreshFlowDetail will rebuild from scratch.
  if (isOpenFlow && state.flowDetail) {
    renderInterventions(state.flowDetail);
  }
}

// Handle a ws-pushed `spawn_failed`. The daemon reports that a task we just
// published (`POST /api/flows` answered 202 "dispatched") could not actually
// be launched — the project init failed, the `se3 run` subprocess could not
// start, or a resume failed. Without this the task would stay stuck on the
// optimistic "published" state forever. We surface the real reason as a
// lingering error toast scoped to the project root so the user knows the
// publish did not take effect and can retry.
function applySpawnFailed(msg) {
  if (!msg || typeof msg !== "object") return;
  const projectRoot = String(msg.project_root || "");
  const reason = String(msg.error || tf("toast.unknownError", "unknown error"));
  const where = projectRoot ? ` (${projectRoot})` : "";
  showToast("error", tf("toast.taskLaunchFailed", `Failed to launch task${where}: ${reason}`, { where, reason }));
}

// Clear pending-Send bookkeeping and re-enable the Send button via a
// renderInterventions pass. Safe to call when no send is pending (no-op).
function settlePendingSend() {
  if (!state.pendingSendSettleKey && !state.pendingSendTimer) return;
  state.pendingSendSettleKey = null;
  state.pendingSendBaselineCallIds = null;
  if (state.pendingSendTimer) {
    clearTimeout(state.pendingSendTimer);
    state.pendingSendTimer = null;
  }
  if (state.flowDetail) renderInterventions(state.flowDetail);
}

// Called on every ws-driven refresh of `state.flowDetail`. Compares the
// fresh pending_calls call_id set against the baseline captured at Send
// time; any diff means the backend has observed the submission and we can
// release the Send button.
function maybeSettleViaPendingCallsDiff(freshFlow) {
  if (!state.pendingSendSettleKey || !state.pendingSendBaselineCallIds) return;
  const fresh = new Set(
    (freshFlow && freshFlow.pending_calls ? freshFlow.pending_calls : [])
      .map((c) => c && c.call_id)
      .filter(Boolean),
  );
  const baseline = state.pendingSendBaselineCallIds;
  if (fresh.size !== baseline.size) {
    settlePendingSend();
    return;
  }
  for (const id of fresh) {
    if (!baseline.has(id)) {
      settlePendingSend();
      return;
    }
  }
}

function applyMachines(machines) {
  // Defense in depth: the server already scopes /ws/ui pushes to this owner,
  // but re-apply the owner narrowing on the client so a mixed/stale snapshot
  // can never render another owner's daemon (or expose New Task / respond /
  // interject entry points on a machine that is not this owner's).
  state.machines = visibleMachinesForOwner(machines, state.identity);

  // Keep selection valid; default to the first machine.
  if (!state.machines.some((m) => m.machine_id === state.selectedMachineId)) {
    state.selectedMachineId = state.machines.length
      ? state.machines[0].machine_id
      : null;
  }

  renderMachines();
  renderFlows();

  // Keep an open registered-project dialog in step with the snapshot: the
  // daemon fires a fast push right after every registry write, so mirroring it
  // here is what makes an add/remove land in the list promptly.
  if (isModalOpen("project-modal")) syncProjectsFromSnapshot();

  // Refresh the issues list if the issues view is open — re-fetch from the
  // REST API so that daemon-side changes (new/closed/reopened issues) are
  // reflected promptly without waiting for a manual filter toggle.
  // Also refresh the type and project dropdown universes so that newly
  // appearing issue types/projects are reflected in the filter dropdowns
  // without requiring the user to close and reopen the panel.
  if (isIssuesOpen()) {
    fetchIssues();
    fetchAllIssueTypes();
  }

  // Refresh the open flow view if its flow is still around.
  if (state.selectedFlowId) {
    if (findFlow(state.selectedFlowId)) {
      refreshFlowDetail();
    } else {
      closeFlowView();
    }
  }
}

// ---------------------------------------------------------------------------
// Main-list panel switch (mobile-portrait): Machines <-> Flows
// ---------------------------------------------------------------------------
//
// On a phone the desktop two-column grid is collapsed to a single visible
// panel: the machine list shows by default, and selecting a machine switches
// to the Flows panel (with a back button to return). The state lives as an
// `active-flows` class on `#main-layout`. On desktop that class has no matching
// styles (both columns always render), so these flips are inert and the desktop
// layout is unchanged.
//
// listPanelState is the DOM-free transition helper (exported for the pure
// tests): given the current panel and an action it returns the next panel.

function listPanelState(current, action) {
  switch (action) {
    case "select-machine":
      return "flows";
    case "back":
    case "reset":
      return "machines";
    default:
      return current === "flows" ? "flows" : "machines";
  }
}

function currentListPanel() {
  const layout = $("main-layout");
  return layout && layout.classList.contains("active-flows") ? "flows" : "machines";
}

function setListPanel(panel) {
  const layout = $("main-layout");
  if (layout) layout.classList.toggle("active-flows", panel === "flows");
}

function applyListPanelAction(action) {
  setListPanel(listPanelState(currentListPanel(), action));
}

// History view (G5) — same single-view panel switch, mirroring the main list
// above. On a phone the History session list and the session detail share one
// grid cell and only one is visible at a time: the list is the default, and
// selecting a session reveals the detail (with a back button to return). The
// state lives as an `active-detail` class on `#history-view`. On desktop that
// class has no matching styles (both panes always render), so these flips are
// inert and the desktop History layout is unchanged.
//
// historyPanelState is the DOM-free transition helper (exported for the pure
// tests): given the current panel and an action it returns the next panel.

function historyPanelState(current, action) {
  switch (action) {
    case "select-session":
      return "detail";
    case "back":
    case "reset":
      return "list";
    default:
      return current === "detail" ? "detail" : "list";
  }
}

function currentHistoryPanel() {
  const view = $("history-view");
  return view && view.classList.contains("active-detail") ? "detail" : "list";
}

function setHistoryPanel(panel) {
  const view = $("history-view");
  if (view) view.classList.toggle("active-detail", panel === "detail");
}

function applyHistoryPanelAction(action) {
  setHistoryPanel(historyPanelState(currentHistoryPanel(), action));
}

// ---------------------------------------------------------------------------
// Issue management — pure helpers
// ---------------------------------------------------------------------------

// Derive a display title from an issue object. Prefers the explicit `title`
// field; falls back to the first non-empty line of `description`; final
// fallback is the localized "untitled" placeholder (the in-code literal is the
// built-in fallback, so the pure unit tests — which load no dictionary — still
// see "untitled"). Issue text itself is data and is never translated.
function issueDisplayTitle(issue) {
  const untitled = () => tf("issue.untitled", "untitled");
  if (!issue || typeof issue !== "object") return untitled();
  if (issue.title && typeof issue.title === "string" && issue.title.trim()) {
    return issue.title.trim();
  }
  if (issue.description && typeof issue.description === "string") {
    const first = issue.description.split(/\r?\n/).find((l) => l.trim());
    if (first) return first.trim().slice(0, 80);
  }
  return untitled();
}

// Derive a filesystem-style slug from an issue's display title. Lowercased,
// non-alphanumeric runs collapsed to hyphens, leading/trailing hyphens
// stripped. Returns "untitled" when the input produces an empty slug.
// Pure — no DOM, no state.
function issueSlug(title) {
  if (!title || typeof title !== "string") return "untitled";
  const slug = title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  return slug || "untitled";
}

// Filter an issue list by the current UI filters (showClosed, source, type).
// Pure — no DOM, no state.
function filterIssues(issues, { showClosed, sourceFilter, typeFilter }) {
  if (!Array.isArray(issues)) return [];
  const closedStatuses = new Set(["resolved", "won't-fix", "closed"]);
  return issues.filter((iss) => {
    if (!iss || typeof iss !== "object") return false;
    if (!showClosed && closedStatuses.has(iss.status)) return false;
    if (sourceFilter && iss.source !== sourceFilter) return false;
    if (typeFilter && iss.type !== typeFilter) return false;
    return true;
  });
}

// Collect unique types from an issue list for the filter dropdown. Returns a
// sorted array of non-empty type strings. Pure.
function issueTypes(issues) {
  if (!Array.isArray(issues)) return [];
  const s = new Set();
  for (const iss of issues) {
    if (iss && typeof iss.type === "string" && iss.type.trim()) s.add(iss.type.trim());
  }
  return [...s].sort();
}

// Collect unique, non-empty project_root strings from an issue list. Returns a
// stably-sorted array of distinct project_root values. Issues with missing or
// falsy project_root are skipped. Pure: no DOM, no state.
// This is the project-options derivation for the issue project dropdown, parallel
// to issueTypes for the type dropdown and groupHistorySessionsByProjectRoot for
// the history project dropdown.
function issueProjectRoots(issues) {
  if (!Array.isArray(issues)) return [];
  const seen = new Set();
  const result = [];
  for (const iss of issues) {
    if (!iss || typeof iss !== "object") continue;
    const pr = iss.project_root;
    if (typeof pr === "string" && pr.trim()) {
      const trimmed = pr.trim();
      if (!seen.has(trimmed)) {
        seen.add(trimmed);
        result.push(trimmed);
      }
    }
  }
  // Stable sort: preserve insertion order (first-seen order) since the
  // input already comes from deduplicated sources; alphabetical sort
  // is unnecessary and would break the "most recently seen first"
  // natural ordering from STATUS_UPDATE.
  return result;
}

// Pick the default project_root the Issues view should select in the project
// dropdown. Pure: no DOM, no state.
//
// * If currentSelected is still present in allProjectRoots, keep it (preserves
//   the user's in-session selection across refreshes).
// * If currentSelected has disappeared (e.g. the project's last issue was
//   closed and no longer appears), fall back to "" (全部项目).
// * If currentSelected is null/undefined/empty (first load or reset), default
//   to "" (全部项目).
//
// This mirrors pickDefaultHistoryProjectRoot but defaults to the "全部项目"
// sentinel (empty string) rather than buckets[0], because "全部项目" is the
// most common desired starting state for issue browsing.
function pickDefaultIssueProjectRoot(allProjectRoots, currentSelected) {
  if (!Array.isArray(allProjectRoots)) return "";
  // Preserve the current selection if it still exists in the options.
  if (currentSelected && allProjectRoots.includes(currentSelected)) {
    return currentSelected;
  }
  // Default: "全部项目" (empty string sentinel).
  return "";
}

// Issues panel state helper (mirrors historyPanelState): manages the
// single-view panel switch on narrow screens (list ↔ detail).
function issuesPanelState(current, action) {
  switch (action) {
    case "select-issue":
      return "detail";
    case "back":
    case "reset":
      return "list";
    default:
      return current === "detail" ? "detail" : "list";
  }
}

function currentIssuesPanel() {
  const view = $("issues-view");
  return view && view.classList.contains("active-detail") ? "detail" : "list";
}

function setIssuesPanel(panel) {
  const view = $("issues-view");
  if (view) view.classList.toggle("active-detail", panel === "detail");
}

function applyIssuesPanelAction(action) {
  setIssuesPanel(issuesPanelState(currentIssuesPanel(), action));
}

// CSS class for issue status badges. Pure.
function issueStatusClass(status) {
  switch (status) {
    case "open":         return "badge-open";
    case "in-progress":  return "badge-in-progress";
    case "resolved":     return "badge-resolved";
    case "won't-fix":    return "badge-wontfix";
    case "closed":       return "badge-closed";
    default:             return "badge-open";
  }
}

// Catalog keys for the issue-status badge text. The raw status tokens carry
// characters that make poor dotted keys ("won't-fix"), so the mapping is
// explicit rather than derived from the token. Pure.
const ISSUE_STATUS_KEYS = {
  "open": "status.issue.open",
  "in-progress": "status.issue.inProgress",
  "resolved": "status.issue.resolved",
  "won't-fix": "status.issue.wontFix",
  "closed": "status.issue.closed",
};

// Localized badge text for a raw issue status. An unknown token (a status this
// frontend does not know yet) falls back to the token itself, so a newer
// backend still reads as *something* rather than blanking. Pure.
function issueStatusText(status) {
  const raw = String(status == null ? "" : status).trim();
  if (!raw) return tf("status.issue.open", "open");
  const key = ISSUE_STATUS_KEYS[raw.toLowerCase()];
  return key ? tf(key, raw) : raw;
}

// CSS class for issue priority badges. Pure.
function issuePriorityClass(priority) {
  switch (priority) {
    case "critical": return "priority-critical";
    case "high":     return "priority-high";
    case "medium":   return "priority-medium";
    case "low":      return "priority-low";
    default:         return "priority-none";
  }
}

// Localized text for the enum-like issue tokens (type / priority / source). The
// catalogs carry only the tokens this frontend knows; anything else (a type a
// project invents, a source a newer backend emits) passes through verbatim so it
// still reads as *something* rather than as a raw catalog key. Pure.
function issueTypeText(type) {
  const raw = String(type == null ? "" : type).trim();
  if (!raw) return "";
  return KNOWN_ISSUE_TYPES.includes(raw.toLowerCase())
    ? tf("issueType." + raw.toLowerCase(), raw)
    : raw;
}

function issuePriorityText(priority) {
  const raw = String(priority == null ? "" : priority).trim();
  if (!raw) return "";
  return KNOWN_ISSUE_PRIORITIES.includes(raw.toLowerCase())
    ? tf("issuePriority." + raw.toLowerCase(), raw)
    : raw;
}

function issueSourceText(source) {
  const raw = String(source == null ? "" : source).trim() || "system";
  return KNOWN_ISSUE_SOURCES.includes(raw.toLowerCase())
    ? tf("issueSource." + raw.toLowerCase(), raw)
    : raw;
}

// Known issue types for the create/edit form dropdown.
const KNOWN_ISSUE_TYPES = ["bug", "feature", "enhancement", "idea", "task"];
const KNOWN_ISSUE_PRIORITIES = ["critical", "high", "medium", "low"];
const KNOWN_ISSUE_SOURCES = ["human", "system"];

// Resolve the owning machine_id from an issue object returned by GET /api/issues.
// The REST API attaches the key as ``machine_id`` (state.py get_issues); older
// code used ``_machine_id`` (set by the now-dead collectAllIssues).  Prefer the
// canonical key, fall back to the legacy one.  Pure — no DOM, no state.
function issueMachineId(iss) {
  if (!iss || typeof iss !== "object") return "";
  return (iss.machine_id || iss._machine_id || "").toString();
}

// Composite key for disambiguating issues across machines/projects.
// Issue IDs are per-project monotonic counters, so two projects can produce
// the same numeric id.  The composite key prevents selection/detail-lookup
// collisions when the aggregated issue list contains multiple projects.
function issueCompositeKey(iss) {
  if (!iss || typeof iss !== "object") return "";
  const mid = (iss.machine_id || iss._machine_id || "").toString();
  const pr = (iss.project_root || "").toString();
  const id = (iss.id || "").toString();
  return mid + "::" + pr + "::" + id;
}

// Build the POST body for ``POST /api/issues`` (create).  Pure.
function buildIssueCreateBody(description, machineId, projectRoot, title, type, priority, tags) {
  const body = { description, machine_id: machineId, project_root: projectRoot };
  if (title) body.title = title;
  if (type) body.type = type;
  if (priority) body.priority = priority;
  if (tags && tags.length) body.tags = tags;
  return body;
}

// Build the PATCH body for ``PATCH /api/issues/{id}`` (edit).  Only includes
// fields the user actually modified (tracked via a dirty set).  Pure.
//
// ``description`` is gated on the dirty set just like the other fields: the
// STATUS_UPDATE snapshot carries only a DESC_CLIP-truncated preview, so
// unconditionally PATCHing the form's textarea value back would overwrite the
// issue's stored full body with the 200-char preview whenever the user edits
// only (say) the priority. The server's PATCH leaves description untouched when
// the key is absent, so omitting it is lossless.
function buildIssueEditBody(description, machineId, projectRoot, dirtyFields, formValues) {
  const body = {};
  if (machineId) body.machine_id = machineId;
  if (projectRoot) body.project_root = projectRoot;
  if (dirtyFields.has("issue-description")) body.description = description;
  if (dirtyFields.has("issue-title"))   body.title = formValues.title || "";
  if (dirtyFields.has("issue-type"))    body.type = formValues.type || "";
  if (dirtyFields.has("issue-priority")) body.priority = formValues.priority || "";
  if (dirtyFields.has("issue-tags"))    body.tags = formValues.tags || [];
  return body;
}

// Build the POST body for ``POST /api/issues/{id}/close`` or ``reopen``.  Pure.
function buildIssueActionBody(machineId, projectRoot, reason) {
  const body = {};
  if (machineId) body.machine_id = machineId;
  if (projectRoot) body.project_root = projectRoot;
  if (reason) body.reason = reason;
  return body;
}

// Human-readable disable reasons for the "从此 issue 启动 flow" entry, keyed by
// the non-open statuses an issue can carry.  Used by issueLaunchModel.  Pure.
const ISSUE_LAUNCH_DISABLED_REASONS = {
  "in-progress": "The issue is in progress; a flow cannot be launched again.",
  "resolved": "The issue is resolved; no flow needs to be launched.",
  "won't-fix": "The issue is marked won't-fix.",
  "closed": "The issue is closed.",
};

// i18n keys parallel to ISSUE_LAUNCH_DISABLED_REASONS; resolved at render time
// (the map literal is the offline fallback). Keyed identically.
const ISSUE_LAUNCH_DISABLED_REASON_KEYS = {
  "in-progress": "issueLaunch.reason.inProgress",
  "resolved": "issueLaunch.reason.resolved",
  "won't-fix": "issueLaunch.reason.wontFix",
  "closed": "issueLaunch.reason.closed",
};

// Decide whether a flow may be started from an issue.  Only `open` issues are
// launchable from the UI; every other status is disabled with a human-readable
// reason (the daemon still performs the final in-progress race check).  Pure.
function issueLaunchModel(iss) {
  if (!iss || typeof iss !== "object") {
    return {
      canLaunch: false,
      reason: "Invalid issue.",
      reasonKey: "issueLaunch.reason.invalid",
    };
  }
  const status = (iss.status == null ? "open" : String(iss.status))
    .trim()
    .toLowerCase() || "open";
  if (status === "open") {
    return { canLaunch: true, reason: "", reasonKey: "" };
  }
  const known = ISSUE_LAUNCH_DISABLED_REASONS[status];
  return {
    canLaunch: false,
    reason: known || `The issue status is ${status}; a flow cannot be launched.`,
    reasonKey: ISSUE_LAUNCH_DISABLED_REASON_KEYS[status]
      || "issueLaunch.reason.unknownStatus",
    reasonParams: known ? undefined : { status },
  };
}

// Resolve an issueLaunchModel's disable reason via I18N at render time, falling
// back to the model's built-in reason literal (offline / test env).
function issueLaunchReasonText(model) {
  if (!model || !model.reasonKey) return model ? model.reason : "";
  return tf(model.reasonKey, model.reason, model.reasonParams);
}

// Build the ``POST /api/flows`` body for starting a flow from an issue.  The
// issue's machine/project are passed so the server can reject a target
// mismatch; the server re-resolves them owner-scoped and ignores the task
// content (the issue description becomes the task).  ``planMode`` carries the
// explicit PLAN decomposition doctrine / group granularity; an empty value
// (project default) is OMITTED so the daemon resolves the project
// configuration / default — an explicit empty string would read as a request
// to override.  Pure.
function buildIssueFlowBody(iss, discover, worktree, planMode) {
  const id = iss && iss.id != null ? String(iss.id) : "";
  const body = {
    from_issue_id: id,
    machine_id: issueMachineId(iss),
    project_root: iss && iss.project_root ? String(iss.project_root) : "",
    task: "",
    discover: Boolean(discover),
    worktree: Boolean(worktree),
  };
  applyPlanModeFields(body, planMode);
  return body;
}

// Read the two plan-mode selects into a {decomposition, granularity} pair.
// A missing element reads as "project default" (empty), so a build that drops
// the controls degrades to the project configuration rather than throwing.
function readPlanModeInputs(decompositionId, granularityId) {
  const read = (id) => {
    const node = $(id);
    return (node && node.value && node.value.trim()) || "";
  };
  return { decomposition: read(decompositionId), granularity: read(granularityId) };
}

// Copy the non-empty plan-mode selections onto a ``POST /api/flows`` body.
// Shared by both builders so the omit-when-empty rule cannot drift between the
// New Task form and the Issue Launch modal.  Pure.
function applyPlanModeFields(body, planMode) {
  const decomposition = (planMode && planMode.decomposition) || "";
  const granularity = (planMode && planMode.granularity) || "";
  if (decomposition) body.plan_decomposition = String(decomposition);
  if (granularity) body.plan_granularity = String(granularity);
  return body;
}

// Build the ``POST /api/flows`` body for the New Task form.  ``discover``
// starts the flow from the discovery step; ``worktree`` runs the flow in an
// isolated worktree that auto-merges back on success (equivalent to the CLI
// ``luo run --worktree``).  ``planMode`` follows the same omit-when-empty rule
// as buildIssueFlowBody.  Pure.
function buildNewFlowBody({ machineId, task, taskType, discover, worktree, projectRoot, planMode }) {
  const body = {
    machine_id: machineId,
    task: task,
    task_type: taskType,
    discover: Boolean(discover),
    worktree: Boolean(worktree),
    project_root: projectRoot,
  };
  applyPlanModeFields(body, planMode);
  return body;
}

// ---------------------------------------------------------------------------
// Render: machine list
// ---------------------------------------------------------------------------

function renderMachines() {
  // Diff-aware skip: a ws status push re-runs this unconditionally, but most
  // pushes carry unchanged machine data. Rebuilding the list reflows the page
  // (and, with the flow view open, contributes to reply-textarea typing jank),
  // so when the visible-dependency signature is unchanged we touch no DOM.
  const sig = machinesSignature(state.machines, state.selectedMachineId);
  if (state.renderSig.machines === sig) return;
  state.renderSig.machines = sig;

  const list = $("machine-list");
  list.innerHTML = "";

  if (!state.machines.length) {
    list.appendChild(el("li", "empty", tf("machines.empty", "No machines connected.")));
    return;
  }

  for (const m of state.machines) {
    const li = el("li", "machine-item");
    if (m.machine_id === state.selectedMachineId) li.classList.add("selected");

    const dot = el("span", "dot " + (m.online ? "online" : "offline"));
    const name = el("span", "machine-name", m.hostname || m.machine_id);
    name.title = m.machine_id;
    const flowN = (m.flows || []).length;
    const count = el("span", "machine-count",
      flowN === 1
        ? tf("machines.flowCount", `${flowN} flow`, { n: flowN })
        : tf("machines.flowCountPlural", `${flowN} flows`, { n: flowN }));

    // Registered-project dialog entry point. The registry is a per-daemon
    // concept, so the machine row is its only honest anchor in this UI. The
    // button's sole visible dependency is machine_id, which machinesSignature
    // already carries — so the diff-aware skip above can never drop it: any
    // rebuild emits it, and a skipped rebuild means the row (button included)
    // is still the one that was painted.
    const manage = el("button", "icon-btn machine-projects-btn", "🗂");
    manage.type = "button";
    manage.title = tf("projects.manage", "Manage registered projects");
    manage.addEventListener("click", (e) => {
      // The row itself selects the machine; the button must not do that too.
      if (e && typeof e.stopPropagation === "function") e.stopPropagation();
      openProjects(m.machine_id);
    });

    li.append(dot, name, count, manage);
    li.addEventListener("click", () => {
      state.selectedMachineId = m.machine_id;
      // Narrow screens switch to the Flows panel; inert on desktop.
      applyListPanelAction("select-machine");
      renderMachines();
      renderFlows();
    });
    list.appendChild(li);
  }
}

// ---------------------------------------------------------------------------
// Render: flow list
// ---------------------------------------------------------------------------

function renderFlows() {
  const machine = state.machines.find((m) => m.machine_id === state.selectedMachineId);

  // Diff-aware skip (same rationale as renderMachines): when the selected
  // machine's flow-list visible-dependency signature is unchanged, rebuild
  // nothing so an unchanged ws push reflows neither this panel nor (via the
  // shared layout) the docked reply textarea.
  const sig = flowsSignature(machine, state.selectedMachineId, state.resumeFlowRequests);
  if (state.renderSig.flows === sig) return;
  state.renderSig.flows = sig;

  const panel = $("flow-list");
  const heading = $("flows-heading");
  panel.innerHTML = "";

  if (!machine) {
    heading.textContent = tf("flows.title", "Flows");
    panel.appendChild(el("p", "empty", tf("flows.empty", "Select a machine to view its flows.")));
    return;
  }

  heading.textContent = tf("flows.titleWith", `Flows — ${machine.hostname || machine.machine_id}`, { name: machine.hostname || machine.machine_id });
  // Defense-in-depth against the empty ``(untitled flow)`` card: skip any flow
  // lacking a flow_id so it neither renders a card nor blocks the empty state.
  // The root cause is fixed in DaemonAggregator._snapshot_for_root (an archived
  // root no longer fabricates a flowless snapshot), but a flowless entry from a
  // stale/legacy source must still never reach renderFlowCard.
  const flows = (machine.flows || []).filter((f) => f && f.flow_id);
  if (!flows.length) {
    panel.appendChild(el("p", "empty", tf("flows.emptyMachine", "No flows on this machine.")));
    return;
  }

  for (const flow of flows) {
    panel.appendChild(renderFlowCard(flow));
  }
}

function renderFlowCard(flow) {
  const card = el("div", "flow-card");

  const head = el("div", "flow-card-head");
  const task = el("span", "flow-task",
    flow.task_description || flow.flow_id || tf("flow.untitled", "(untitled flow)"));
  task.title = flow.task_description || "";
  // The badge is the raw flow status; isWaitingForLock keeps its own separate ⏳
  // badge below (it layers on running). The worktree merge no longer has a
  // completed-body badge override — it renders as the flow's own merge steps.
  const sc = statusClass(flow.status);
  const badge = el("span", "badge badge-" + sc, flowStatusText(flow.status));
  head.append(task, badge);

  // Annotate which project this running flow belongs to so flows from
  // different project roots are distinguishable at a glance. Show the
  // worktree-aware label ('<项目名> (worktree)' for worktree flows, else the
  // basename) as the readable text; the full project_root is the hover title.
  // Skip the badge entirely when project_root is missing to avoid empty-label
  // noise.
  const projectName = projectDisplayLabel(flow.project_root);
  if (projectName) {
    const project = el("span", "flow-card-project", projectName);
    project.title = flow.project_root;
    head.appendChild(project);
  }

  if (isWaitingForLock(flow)) {
    // Surface the running·waiting-for-lock sub-state so a queued flow reads as
    // running rather than appearing stalled.
    head.appendChild(el("span", "badge badge-waiting-lock",
      tf("flow.badge.waitingLock", "⏳ waiting for lock")));
  }

  if (hasPendingCall(flow)) {
    // The badge is purely an indicator — opening the flow view (below) is the
    // single entry point; there is no separate context-less call modal.
    head.appendChild(el("span", "badge badge-call",
      tf("flow.badge.needsResponse", "⚠ needs response")));
  }

  const resumeBtn = makeResumeButton(flow);
  if (resumeBtn) head.appendChild(resumeBtn);

  const endBtn = makeEndButton(flow);
  if (endBtn) head.appendChild(endBtn);

  const bar = el("div", "progress");
  const inner = el("div", "progress-bar");
  inner.style.width = Math.round((flow.progress || 0) * 100) + "%";
  bar.appendChild(inner);

  const meta = el("div", "flow-meta");
  meta.append(
    el("span", null, flow.current_step
      ? tf("flow.card.currentStep", `step: ${flow.current_step}`,
        { step: flow.current_step })
      : (flow.task_type || "")),
    el("span", null, `${flow.current_step_index || 0}/${flow.total_steps || 0}`),
  );
  // PLAN decomposition mode as a low-key chip (backend projection; absent on
  // snapshots from daemons that predate it). A flow created under the retired
  // strategy axis has no doctrine of its own, so it shows what it recorded.
  const planMode = flow.plan_mode;
  if (planMode && planMode.decomposition) {
    meta.appendChild(el("span", "flow-plan-chip plan-" + planMode.decomposition,
      planDecompositionLabel(planMode.decomposition)));
  } else if (planMode && planMode.legacy_strategy) {
    meta.appendChild(el("span", "flow-plan-chip plan-legacy",
      legacyStrategyLabel(planMode.legacy_strategy)));
  }

  card.append(head, bar, meta);
  card.addEventListener("click", () => openFlowView(flow.flow_id));
  return card;
}

// ---------------------------------------------------------------------------
// Running-flow chat view
// ---------------------------------------------------------------------------
//
// A full-screen view (parity with the history view): the sidebar carries
// Overview / Steps / Machine, the conversation is the scrollable main body,
// intervention items are pinned above a docked reply box. There is no narrow
// drawer and no context-less call modal — every interaction lives here.

const STEP_ICONS = {
  completed: "✓", failed: "✗", running: "⟳",
  paused: "⏸", pending: "⏸", partial: "◐", retrying: "⟳",
};

function isFlowViewOpen() {
  return !$("flow-view").classList.contains("hidden");
}

function openFlowView(flowId) {
  state.selectedFlowId = flowId;
  state.flowDetail = null;
  state.flowConversationUsage = null;
  state.flowMachineId = null;
  state.flowConversationRecords = [];
  // A different flow is opening: drop any progress token held for the prior
  // flow so its delta cursor can never be echoed against this flow's bundle.
  state.flowConversationProgress = null;
  // ...and the bundle signature it was paired with, so the first self-heal for
  // this flow can never send a prior flow's signature.
  state.flowConversationSignature = null;
  // A freshly-opened flow forces a scroll to the bottom, so it starts as a
  // bottom-follower; a stale "scrolled up" intent from the prior flow must not
  // make this flow's first silent rebuild anchor an old tail (issue #260).
  state.flowConversationFollowingBottom = true;
  // Reset the progression baseline so this flow's first detail snapshot only
  // establishes a baseline (the full first-open load already shows everything);
  // a prior flow's current_step/status must never trigger a refresh here.
  state.flowProgressionMarker = null;
  // Cancel any grace timer left pending from the prior flow so its fallback
  // rebuild can never fire against this freshly-opened flow.
  cancelProgressionGrace();
  // Reset the detail request-sequence guard so this flow's fetches start fresh.
  // A still-in-flight fetch from a PRIOR lifecycle — including a prior open of
  // this same flow — is dropped on resolution by the flowDetailViewGen check
  // below, so resetting the seq counters here cannot let a stale high-seq
  // response apply or suppress this lifecycle's fresh low-seq responses.
  state.flowDetailReqSeq = 0;
  state.flowDetailAppliedSeq = 0;
  // Bump the lifecycle generation so any fetch claimed by a previous open/close
  // cycle resolves into a mismatched generation and is discarded.
  state.flowDetailViewGen += 1;
  state.flowInterventions = [];
  state.flowReplyTargetId = null;
  state.flowInterjectRequested = false;
  state.flowInterjectFlowId = flowId;
  state.flowSyntheticInterjectPending = false;
  if (state.pendingSendTimer) {
    clearTimeout(state.pendingSendTimer);
    state.pendingSendTimer = null;
  }
  state.pendingSendSettleKey = null;
  state.pendingSendBaselineCallIds = null;
  state.interjectionPhases = {};
  state.interjectionToastsSeen = {};
  state.flowReplyPromptExpanded = {};
  state.flowReplyPromptScroll = {};
  state.flowReplyPromptFull = {};
  // Force the next frame of every diff-aware render region to rebuild: this is
  // both first-open and the flow-switch path (the containers are reused), so a
  // signature cached against the prior flow must not skip this flow's rebuild.
  resetRenderSignatures();
  state.detailLoaded = false;
  state.detailFetchFailures = 0;

  // Push a history entry so the browser back button collapses the flow view
  // instead of leaving the site. The popstate listener takes care of the
  // real cleanup; the ✕ button just calls history.back() on top of this.
  try {
    history.pushState(
      { se3FlowView: flowId },
      "",
      "#flow/" + encodeURIComponent(flowId)
    );
    flowViewHistoryPushed = true;
  } catch (e) {
    flowViewHistoryPushed = false;
  }

  $("flow-view").classList.remove("hidden");
  // Always open with the mobile sidebar drawer collapsed; inert on desktop.
  closeFlowSidebar();
  $("flow-view-title").textContent = tf("flow.title", "Flow");
  renderSidebarPlaceholder(tf("flow.sidebarLoading", "Loading flow details…"));
  $("flow-interventions").innerHTML = "";
  // Reset the session-usage badge for the freshly-opened flow; it re-appears
  // once this flow's first usage-bearing step event is rendered.
  updateFlowUsageBadge([]);
  resetReplyBox();

  // The periodic self-heal becomes the conversation's correctness source while
  // this view is open (see maybeRefreshConversationOnProgression); it runs every
  // tick until the view closes, regardless of terminal status.
  state.periodicSnapshotActive = true;

  refreshFlowDetail();
  // Fetch the flow's conversation snapshot; WS history_data deltas append live.
  loadFlowConversation(flowId);
  // Poll the REST endpoint while the view is open. pollFlowView refreshes the
  // left-side detail AND re-pulls the whole conversation on the same 3s cadence:
  // the periodic full snapshot is the right side's correctness source, so a WS
  // increment the push path dropped self-heals at the next tick (WS deltas stay
  // as a pure low-latency optimization).
  if (detailPollTimer) clearInterval(detailPollTimer);
  detailPollTimer = setInterval(pollFlowView, 3000);
}

// Cleanup-only close: clears state and hides the view, but never touches
// history. The single source of truth for closing a flow view is the
// popstate handler — both the ✕ button and the browser back button funnel
// through it, so there is no risk of push-back loops or double-pop drift.
function doCloseFlowView() {
  state.flowConversationEpoch += 1;
  state.selectedFlowId = null;
  state.flowDetail = null;
  state.flowConversationUsage = null;
  state.flowMachineId = null;
  state.flowConversationRecords = [];
  state.flowConversationProgress = null;
  state.flowConversationSignature = null;
  // Clear the progression baseline so a later openFlowView starts fresh.
  state.flowProgressionMarker = null;
  // Cancel any pending grace timer so a closed flow never fires a fallback
  // rebuild against a view that is no longer open.
  cancelProgressionGrace();
  // Reset the detail request-sequence guard alongside the marker.
  state.flowDetailReqSeq = 0;
  state.flowDetailAppliedSeq = 0;
  state.flowInterventions = [];
  state.flowReplyTargetId = null;
  state.flowInterjectRequested = false;
  state.flowInterjectFlowId = null;
  // Reset all Send-lifecycle bookkeeping so the next opened flow starts
  // fresh — a stale pendingSendSettleKey could otherwise leave Send
  // disabled the moment a chip appears in the next flow.
  state.flowSyntheticInterjectPending = false;
  if (state.pendingSendTimer) {
    clearTimeout(state.pendingSendTimer);
    state.pendingSendTimer = null;
  }
  state.pendingSendSettleKey = null;
  state.pendingSendBaselineCallIds = null;
  state.interjectionPhases = {};
  state.interjectionToastsSeen = {};
  state.flowReplyPromptExpanded = {};
  state.flowReplyPromptScroll = {};
  state.flowReplyPromptFull = {};
  // Clear the diff-aware render-signature cache so a later openFlowView starts
  // with no stale signatures pinning a closed flow's panels.
  resetRenderSignatures();
  // Reset the mobile sidebar drawer so the next opened flow starts collapsed.
  closeFlowSidebar();
  $("flow-view").classList.add("hidden");
  // The periodic self-heal stops with the view: clear the "poll owns correctness"
  // flag so a later detached progression observation (or a DOM-free test) is free
  // to arm the grace fallback again.
  state.periodicSnapshotActive = false;
  if (detailPollTimer) {
    clearInterval(detailPollTimer);
    detailPollTimer = null;
  }
}

// User-initiated close (✕ button, Escape, etc). When we pushed a history
// entry on open, defer to history.back() so the browser stack stays in sync
// and the popstate listener performs the real cleanup. Otherwise (history
// API unavailable or push failed), close directly.
function closeFlowView() {
  if (flowViewHistoryPushed) {
    flowViewHistoryPushed = false;
    history.back();
    return;
  }
  doCloseFlowView();
}

// ---------------------------------------------------------------------------
// Flow-view sidebar drawer (mobile, G4)
// ---------------------------------------------------------------------------
//
// On a narrow screen the Overview / Steps / Machine sidebar is an off-canvas
// drawer: hidden by default, slid in when the user taps the head toggle, and
// dismissed by tapping the backdrop. The drawer state is a single
// `sidebar-open` class on `#flow-view`; on desktop that class has no matching
// styles (the sidebar always renders in the grid and the toggle/backdrop are
// hidden), so these flips are inert and the desktop layout is unchanged.
//
// flowSidebarNextState is the DOM-free transition helper (exported for the pure
// tests, mirroring navMenuNextState): given the drawer's current open flag it
// returns the next one.

function flowSidebarNextState(open) {
  return !open;
}

function isFlowSidebarOpen() {
  const view = $("flow-view");
  return Boolean(view && view.classList.contains("sidebar-open"));
}

function setFlowSidebarOpen(open) {
  const view = $("flow-view");
  const toggle = $("flow-sidebar-toggle");
  if (view) view.classList.toggle("sidebar-open", open);
  if (toggle) toggle.setAttribute("aria-expanded", open ? "true" : "false");
}

function toggleFlowSidebar() {
  setFlowSidebarOpen(flowSidebarNextState(isFlowSidebarOpen()));
}

function closeFlowSidebar() {
  setFlowSidebarOpen(false);
}

// ---------------------------------------------------------------------------
// WeChat-style auto-grow reply textarea (mobile portrait only)
// ---------------------------------------------------------------------------
//
// replyTextareaHeight is the DOM-free clamp behind the auto-grow textarea
// (exported for the pure tests, mirroring navMenuNextState /
// flowSidebarNextState). Given the textarea's measured `scrollHeight` and the
// [minPx, maxPx] pixel bounds it returns the height to apply: the content
// height clamped into [minPx, maxPx]. Inputs are floored to whole pixels;
// non-finite / non-positive / out-of-order values degrade deterministically
// (a bad measurement falls back to the minimum and never yields NaN), in the
// same defensive style as the other mobile state helpers.
function replyTextareaHeight(scrollHeight, minPx, maxPx) {
  const min = Number.isFinite(minPx) && minPx > 0 ? Math.floor(minPx) : 0;
  let max = Number.isFinite(maxPx) && maxPx > 0 ? Math.floor(maxPx) : min;
  if (max < min) max = min;
  const sh = Number.isFinite(scrollHeight) ? Math.floor(scrollHeight) : min;
  return Math.max(min, Math.min(sh, max));
}

// Single-line floor for the auto-grow textarea, kept in sync with the mobile
// `.flow-reply-row textarea { min-height: 40px }` rule in style.css.
const REPLY_TEXTAREA_MIN_PX = 40;

// matchMedia gate: the auto-grow behavior only runs on the phone-portrait
// breakpoint. On desktop the textarea keeps its 6-row default height and manual
// `resize: vertical`, so the grower must be a no-op there.
function isMobilePortrait() {
  try {
    return (
      typeof window !== "undefined" &&
      typeof window.matchMedia === "function" &&
      window.matchMedia("(max-width: 600px)").matches
    );
  } catch (_) {
    return false;
  }
}

// WeChat-style auto-grow: on mobile portrait, size the reply textarea to its
// content between the single-line minimum and ~35vh, then scroll internally
// past the cap. Content shrinking / clearing lets it fall back to a single
// line. On desktop (or absent matchMedia) this clears any JS-applied inline
// height so the stylesheet default governs, and otherwise returns immediately.
function autoGrowReplyTextarea() {
  const input = $("flow-reply-input");
  if (!input) return;
  if (!isMobilePortrait()) {
    // Desktop: only undo an inline height that THIS auto-grow logic applied
    // (i.e. when transitioning back out of mobile portrait). A blanket clear on
    // every input event would discard the user's manual `resize: vertical` drag
    // — which is itself recorded as an inline `style.height` — and snap the box
    // back to the CSS min-height on the next keystroke. The flag distinguishes
    // a JS-applied height from a user-dragged one, so manual resizes survive
    // typing on desktop.
    if (input.style && input.__autoGrowApplied) {
      input.style.height = "";
      input.style.overflowY = "";
      input.__autoGrowApplied = false;
    }
    return;
  }
  const vh = typeof window !== "undefined" && window.innerHeight
    ? window.innerHeight
    : 0;
  const maxPx = Math.floor(vh * 0.35);
  // Collapse to 0 before measuring so `scrollHeight` reflects the TRUE content
  // height. Resetting to "auto" (the previous approach) let the textarea fall
  // back to its `rows="6"` intrinsic height, so an empty / default field still
  // measured ~6 rows and never shrank to a single line. With the height pinned
  // to 0, `scrollHeight` is the content's own height (one line + padding when
  // empty), which `replyTextareaHeight` then clamps up to the single-line
  // minimum — so the default/empty state truly collapses to one row.
  input.style.height = "0px";
  const target = replyTextareaHeight(
    input.scrollHeight,
    REPLY_TEXTAREA_MIN_PX,
    maxPx,
  );
  input.style.height = target + "px";
  // Only show the internal scrollbar once the content overflows the cap.
  input.style.overflowY = input.scrollHeight > target ? "auto" : "hidden";
  // Mark that the auto-grow applied an inline height so a later desktop pass
  // (e.g. on viewport widening) knows it is safe to clear.
  input.__autoGrowApplied = true;
}

// Fetch the initial conversation snapshot for the open flow. Mirrors the
// history view: a one-shot `/api/history/{flow_id}` pull, after which the WS
// `history_data` push keeps an active flow's conversation up to date.
// Load (or incrementally refresh) the running-flow conversation.
//
//   opts.incremental === false (default): the FIRST open of a flow. Show the
//     loading placeholder, clear the container and its reconciliation state,
//     send no `after` token (so the server replies `delivery: "full"`), and
//     paint the result with a full rebuild. First-open behaviour is unchanged.
//
//   opts.incremental === true: a WS-reconnect refresh of the already-open flow
//     (`ws.onopen`). The container is NOT cleared and `__convState` is NOT
//     reset, so existing bubbles, their fold/raw state, and the reader's scroll
//     position survive. The held progress token is echoed via `?after=`; the
//     server returns only the delta records emitted during the outage, which
//     are deduped and appended through the same merge/reconcile path the live
//     `history_data` push uses. The server may still answer `delivery: "full"`
//     (token stale / cache replaced / cache miss) — that falls back to a full
//     authoritative rebuild. A failed request leaves the existing conversation
//     untouched (no error placeholder, no clear).
//
//   opts.silent === true: a SILENT full rebuild — the bottom-line "step
//     progressed, recover the main conversation" workaround (see
//     maybeRefreshConversationOnProgression). It does the same non-incremental,
//     no-`after`-token full `/api/history` pull and `delivery: "full"` whole-tree
//     rebuild as a first open (so the rendered result equals what an exit/re-enter
//     would produce), but WITHOUT the destructive pre-clear: it does NOT empty the
//     container or show a "Loading conversation…" placeholder before the fetch, so
//     no blank flash is visible — the DOM is replaced in one synchronous render
//     only AFTER the data has arrived. Scroll anchoring is relaxed from the
//     first-open's forced stick-to-bottom to `isNearBottom(container)`, so a user
//     scrolled up reading history is not yanked to the bottom. Like the reconnect
//     refresh it never wipes the conversation on a transient failure. This path
//     touches ONLY the conversation area (#flow-conversation) and its state — it
//     never reads or writes the reply region (#flow-interventions /
//     #flow-reply-context), so a draft, focus, or textarea height in flight is
//     untouched. silent and incremental are mutually exclusive; silent always
//     forces the full (non-`after`) pull.
async function loadFlowConversation(flowId, opts) {
  const incremental = !!(opts && opts.incremental);
  const silent = !!(opts && opts.silent);
  const container = $("flow-conversation");
  // Every new request supersedes every older request for this view. This is
  // required on reconnect too: unstable connectivity can start overlapping
  // refreshes, and a late full fallback from an older cache generation must
  // not overwrite the newer result or regress its progress token.
  //
  // EXCEPTION — a silent progression refresh defers its epoch bump until it
  // actually holds replacement data (just before committing, below). Bumping
  // up-front would invalidate an in-flight first-open full load; if the silent
  // fetch then fails transiently it returns early without rendering, and the
  // already-superseded first-open response is discarded too, freezing the view
  // on the Loading/empty DOM until the user re-enters. Deferring the bump lets
  // the first-open complete normally whenever the silent refresh fails, and the
  // silent path still claims the epoch once it can paint fresh data.
  if (!silent) {
    state.flowConversationEpoch += 1;
  }
  if (!incremental && !silent) {
    container.innerHTML = "";
    // Drop any reconciliation state left by a previously-open flow so a stray
    // append for this flow can't merge into the prior flow's detached sections.
    container.__convState = null;
    container.appendChild(el("p", "empty", tf("flow.loadingConversation", "Loading conversation…")));
  } else if (silent) {
    // SILENT signature-check refresh (G5): no destructive pre-clear and no
    // placeholder, so the user sees no blank flash — the existing DOM stays put
    // until the response is folded in below. Unlike the old behaviour this path
    // NO LONGER drops the held progress token / signature: it now ECHOES them
    // (see the request below) so an unchanged bundle is answered with an
    // extra-small `not_modified` (the common idle case) or a `delta` tail
    // instead of re-shipping the whole 17MB bundle every 3s. A `delivery:"full"`
    // is served only on a real divergence (token/signature stale), which then
    // rebuilds the whole tree exactly as an exit/re-enter would — the #209
    // freeze defence is preserved, only the per-poll cost is cut from "search
    // the whole bundle" to "compare a signature". Nothing is reset here.
  }
  let requestEpoch = state.flowConversationEpoch;
  // Capture the records that belong to the snapshot generation represented by
  // the outgoing progress token. If the server falls back to a full response,
  // only records added after this point are proven live appends worth carrying
  // across the authoritative replacement.
  const requestRecords = state.flowConversationRecords;
  try {
    // Echo the held progress token ONLY when we still hold the records it was
    // issued against. A token whose backing records were dropped (the held
    // array is empty) must NOT be echoed: the server would answer with just the
    // delta tail, which the view would then render as the WHOLE conversation —
    // a silently truncated history. An empty held set therefore forces a full
    // reload even on a reconnect, so a stale offset can never be applied across
    // a cleared/replaced bundle.
    const heldProgress = state.flowConversationRecords.length
      ? state.flowConversationProgress : null;
    // The signature only travels with a live token (see historySnapshotUrl); an
    // empty held set forced heldProgress to null above, so it is dropped too.
    const heldSignature = heldProgress ? state.flowConversationSignature : null;
    // A reconnect (incremental) AND the periodic self-heal (silent) both echo the
    // held token + signature so the server can answer not_modified/delta instead
    // of a full bundle. Only a first-open (neither flag) sends the bare no-token
    // URL that forces a `delivery:"full"`.
    const url = (incremental || silent)
      ? historySnapshotUrl(flowId, heldProgress, heldSignature)
      : `/api/history/${encodeURIComponent(flowId)}`;
    const resp = await authedFetch(url);
    // The user may have opened another flow while this was in flight.
    if (
      state.selectedFlowId !== flowId ||
      state.flowConversationEpoch !== requestEpoch
    ) return;
    if (!resp.ok) {
      // On a reconnect refresh OR a silent progression refresh keep the existing
      // conversation rather than wiping it for a transient failure; first-open
      // still surfaces the error.
      if (incremental || silent) return;
      container.innerHTML = "";
      container.appendChild(el("p", "empty",
        tf("flow.loadError", `Could not load conversation for this flow (${resp.status}).`, { status: resp.status })));
      return;
    }
    const data = await resp.json();
    if (
      state.selectedFlowId !== flowId ||
      state.flowConversationEpoch !== requestEpoch
    ) return;
    if (silent) {
      // We now hold replacement data, so it is finally safe to supersede any
      // older in-flight first-open / reconnect load (which the deferred bump
      // left untouched). Claim the epoch and adopt it; the rest of the commit
      // path is synchronous, so no other request can interleave before render.
      state.flowConversationEpoch += 1;
      requestEpoch = state.flowConversationEpoch;
    }
    // Measure stickiness BEFORE the render mutates scrollHeight. A first-open
    // always scrolls to bottom; a reconnect follows only if already near it.
    //
    // A SILENT progression rebuild instead reads the persistent
    // flowConversationFollowingBottom intent (issue #260): the frozen-DOM
    // isNearBottom is unreliable at the discovery→analyze boundary — a stalled
    // increment leaves a bottom-follower drifted off the bottom, so the momentary
    // measurement misjudges them as scrolled-up and the anchor branch pins the old
    // tail, jumping the view up. The intent flag, driven only by real scroll /
    // scroll-to-bottom signals, still reports "following", so the rebuild sticks.
    // It is OR-combined with the frozen measurement so a reader who genuinely sits
    // near the bottom (flag not yet set true this lifecycle) still follows.
    const stick = silent
      ? (state.flowConversationFollowingBottom || isNearBottom(container))
      : incremental ? isNearBottom(container) : true;
    // A silent refresh does a from-scratch `append=false` rebuild that clears
    // `container.innerHTML` and re-lays-out the same records — possibly at
    // DIFFERENT heights (markdown reflow, a step header appearing, a partial
    // bubble finalizing). When the reader is NOT stuck to the bottom, anchor on
    // the actual bubble they are looking at (captured BEFORE the merge mutates
    // the records array and BEFORE the rebuild clears the DOM) so a height change
    // above it does not scroll the conversation — the issue #209 jump. The
    // captured pre-rebuild scrollTop is retained as the absolute fallback for
    // when no usable geometry exists (DOM-free tests / empty conversation).
    const scrollAnchor = (silent && !stick)
      ? captureScrollAnchor(container, state.flowConversationRecords) : null;
    const preserveScrollTop = (silent && !stick) ? container.scrollTop : null;
    // Fold the response in through the shared decision helper, which picks
    // delta-append vs full-replace from the server's `delivery` tag and keeps
    // live appends that arrived during the await. Record the fresh progress
    // token for the next reconnect.
    const result = mergeHistoryResponse(
      data,
      state.flowConversationRecords,
      requestRecords,
    );
    // `preserveTokens` marks a frame the merge REJECTED wholesale (the #287
    // empty-full guard): nothing about the held generation changed, so the held
    // token/signature must stand — adopting the rejected frame's (null) pair
    // would force the next poll into a needless full re-pull.
    if (!result.preserveTokens) {
      state.flowConversationProgress = result.progress;
      // Refresh the held bundle signature so the next self-heal poll echoes the
      // current generation. Guarded on a present value so a legacy / test response
      // that omits `signature` does not clobber a good held signature to null.
      if (result.signature != null) {
        state.flowConversationSignature = result.signature;
      }
    }
    if (result.resync) {
      // The signed cursor we echoed no longer bound the server's bundle (stale /
      // rotated after a daemon reconnect); the reply is a recoverable full whose
      // authoritative token we just adopted. Shed the repair state keyed to the
      // dead generation — see resetRepairStateForResync. Bounded: the next poll
      // echoes the fresh cursor and resyncs no more.
      resetRepairStateForResync("flow", flowId);
    }
    if (result.render === "noop") {
      // Nothing to REPAINT: a `not_modified` reply (the server has nothing more
      // to send) or an incremental delivery that, after dedup, added nothing new
      // (the WS append for the same batch beat this fetch in). It is NOT proof
      // the view is complete — the token only records what the server sent — so
      // the cursor self-check still runs; on the healthy path it is one set
      // comparison and no request.
      await reconcileCursorCompleteness("flow", flowId, result.cursor, result.generation, result.pending);
      return;
    }
    // Reconcile after the merge: if the response already holds the daemon's
    // authoritative copy of a reply, the still-pending local echo (a live
    // append with a different recordKey) would otherwise survive and duplicate
    // it. A mid-list removal shifts indices, so the cheap incremental-append
    // render can no longer be trusted — force a full rebuild in that case.
    const reconciled = reconcileLocalEchoes(result.records);
    const echoRemoved = reconciled !== result.records;
    // G3: a silent periodic self-heal that changed nothing must not repaint. On
    // the healthy path the 3s full snapshot equals what WS already delivered, so
    // the reconciled records match those held — skip the from-scratch rebuild AND
    // the scroll adjustment entirely, keeping the poll cheap and jank-free; only a
    // real divergence (a dropped/rewritten increment) falls through to rebuild and
    // self-heal. Compared BEFORE state.flowConversationRecords is reassigned, so it
    // still holds the pre-merge array. Only the silent path opts in — a first-open
    // / reconnect must always render its authoritative result.
    if (silent && sameRenderedConversation(reconciled, state.flowConversationRecords)) {
      await reconcileCursorCompleteness("flow", flowId, result.cursor, result.generation, result.pending);
      return;
    }
    state.flowConversationRecords = reconciled;
    // Delta delivery → incremental append render (preserves DOM/fold state);
    // full fallback, or any echo removal, → authoritative full rebuild.
    const appendRender = result.render === "delta" && !echoRemoved;
    if (silent && !appendRender) {
      // A silent FULL rebuild (real divergence): drop any incremental
      // reconciliation state so renderConversation repaints the whole tree in
      // one synchronous pass (no per-record flash) and rebuilds `__convState`
      // from zero. A silent DELTA (now possible since the self-heal echoes its
      // token) instead folds the tail into the existing DOM — keep `__convState`
      // so that cheap append path works, exactly like a reconnect delta.
      container.__convState = null;
    }
    renderConversation(container, state.flowConversationRecords, appendRender);
    refreshFlowStickyHeader();
    updateFlowUsageBadge(state.flowConversationRecords);
    if (stick) {
      scrollFlowConversationToBottom();
    } else if (silent && !appendRender) {
      // A silent FULL rebuild re-lays-out every bubble at possibly different
      // heights, so re-anchor the reader's viewport to the same bubble (matched
      // by recordKey across the rebuilt records) so a height change above it does
      // not shift the view (#209). A silent DELTA appended below the viewport
      // moves nothing the reader can see, so it needs no re-anchor. Falls back to
      // the absolute pre-rebuild offset (preserveScrollTop, clamped) when the
      // anchor is unusable — preserving the prior behaviour for DOM-free cases.
      restoreScrollAnchor(
        container, state.flowConversationRecords, scrollAnchor, preserveScrollTop);
    }
    // The reply is rendered; now verify it is COMPLETE. A delta/full can itself
    // arrive with the head still absent (the server only ever sends what its
    // receipt says is outstanding), so the cursor check runs on every delivery,
    // not just the no-op ones.
    await reconcileCursorCompleteness("flow", flowId, result.cursor, result.generation, result.pending);
  } catch (_) {
    if (
      state.selectedFlowId !== flowId ||
      state.flowConversationEpoch !== requestEpoch
    ) return;
    if (incremental || silent) return;  // keep the existing conversation
    container.innerHTML = "";
    container.appendChild(el("p", "empty", tf("flow.networkError", "Network error loading conversation.")));
  }
}

function scrollFlowConversationToBottom() {
  const c = $("flow-conversation");
  c.scrollTop = c.scrollHeight;
  // Landing at the bottom (re)establishes the follow-the-bottom intent, so a
  // subsequent silent rebuild sticks rather than anchoring the old tail (#260).
  state.flowConversationFollowingBottom = true;
}

// --- Element-anchored scroll preservation for the silent full rebuild --------
//
// The progression-triggered silent refresh (issue #209's cause-immune
// workaround) rebuilds `#flow-conversation` from scratch via
// `renderConversation(append=false)`. Re-laying-out the same records can give
// the content ABOVE the reader's viewport a different total height, so the old
// remedy of restoring an absolute pixel `scrollTop` made the conversation
// visibly jump up a large stretch — the very bug this fixes. Instead of pinning
// a pixel value we anchor on the CONTENT the reader is looking at: the topmost
// bubble visible at the viewport top, identified by its record's `recordKey`
// (stable across the old/new arrays even when `reconcileLocalEchoes` drops a
// mid-list echo and shifts every index). After the rebuild we move scrollTop so
// that same bubble returns to the same viewport offset; any height change above
// it is absorbed and the reader's view does not move.

// captureScrollAnchor — read-only. Returns `{ recordKey, viewportOffset }` for
// the first bubble whose bottom edge is still below the container's viewport
// top (the topmost bubble the reader can currently see), or null when there is
// no usable geometry (no visible bubble / all-zero rects, as in the DOM-free
// tests) so the caller falls back to the absolute-scrollTop restore. Reads DOM
// geometry and the passed-in (old) records array only; mutates nothing.
function captureScrollAnchor(container, records) {
  if (!container || !records || !records.length) return null;
  const containerTop = container.getBoundingClientRect().top;
  for (const child of container.children) {
    // Skip `.history-step-header` separators (no `__convIdx`); a bubble's
    // `__convIdx` is its index into the records array it was rendered from.
    if (child.__convIdx === undefined) continue;
    const rec = records[child.__convIdx];
    if (rec === undefined) continue;
    const rect = child.getBoundingClientRect();
    // First bubble still (partially) below the viewport top. An all-zero rect
    // (no layout geometry) never satisfies `bottom > top`, so a DOM-free
    // container yields null and the caller falls back to preserveScrollTop.
    if (rect.bottom > containerTop) {
      return { recordKey: recordKey(rec), viewportOffset: rect.top - containerTop };
    }
  }
  return null;
}

// restoreScrollAnchor — re-finds the captured record in the NEW records array by
// recordKey (NOT by absolute index, which a mid-list echo removal would shift),
// locates its rebuilt bubble, and sets `container.scrollTop` so the bubble sits
// back at `anchor.viewportOffset`. Reading the rects forces a synchronous
// layout, so the measured offset is accurate without a requestAnimationFrame
// deferral. Falls back to `fallbackScrollTop` (the original preserveScrollTop
// behaviour, clamped to the new content height) when the anchor is unusable:
// no anchor, the record vanished, its bubble is missing, or it has no geometry.
function restoreScrollAnchor(container, records, anchor, fallbackScrollTop) {
  const applyFallback = () => {
    if (container && fallbackScrollTop != null) {
      container.scrollTop = Math.min(fallbackScrollTop, container.scrollHeight);
    }
  };
  if (!container || !anchor || !records) { applyFallback(); return; }
  let newIndex = -1;
  for (let i = 0; i < records.length; i++) {
    if (recordKey(records[i]) === anchor.recordKey) { newIndex = i; break; }
  }
  if (newIndex < 0) { applyFallback(); return; }
  let bubble = null;
  for (const child of container.children) {
    if (child.__convIdx === newIndex) { bubble = child; break; }
  }
  if (!bubble) { applyFallback(); return; }
  const rect = bubble.getBoundingClientRect();
  // A degenerate (zero-height) rect means no usable layout geometry — fall back
  // rather than computing a bogus offset against an all-zero rect.
  if (rect.bottom === rect.top) { applyFallback(); return; }
  const containerTop = container.getBoundingClientRect().top;
  const currentOffset = rect.top - containerTop;
  // Increasing scrollTop pulls content up (offset shrinks), so shifting by
  // (currentOffset - target) lands the bubble back at the captured offset.
  container.scrollTop = container.scrollTop + (currentOffset - anchor.viewportOffset);
}

// Render a single-message placeholder into the flow view's sidebar.
function renderSidebarPlaceholder(message) {
  const body = $("flow-sidebar-body");
  body.innerHTML = "";
  body.appendChild(el("p", "empty", message));
}

// Record a failed detail fetch. While the flow has never loaded, surface an
// explicit error in the sidebar once retries keep failing — rather than
// leaving a permanently blank panel. A previously-rendered sidebar is left
// intact on a transient blip; the 3s poll will refresh it.
function noteDetailFetchFailure(message) {
  state.detailFetchFailures += 1;
  if (state.detailLoaded) return;
  if (state.detailFetchFailures >= 2) {
    renderSidebarPlaceholder(tf("flow.detailRetrying", `${message} Retrying…`, { message }));
  }
}

// Cancel a pending progression grace timer and clear its bookkeeping. Idempotent
// (a no-op when nothing is pending), so it is safe to call unconditionally from
// a fresh-advance reschedule and from openFlowView / doCloseFlowView. Because the
// grace timer now RE-ARMS itself on every silent-fallback firing (see
// armProgressionGrace), this is the sole way the periodic retry loop is stopped
// on a flow switch / close — it must clear the pending timer so no further pull
// can fire against a flow that is no longer open.
function cancelProgressionGrace() {
  if (state.progressionGraceTimer != null) {
    clearTimeout(state.progressionGraceTimer);
  }
  state.progressionGraceTimer = null;
  state.progressionGraceFlowId = null;
  state.progressionGraceAppendSeqAtSchedule = 0;
}

// Arm (or re-arm) the progression grace timer for `flowId`, gating on the WS
// append counter frozen at `seqAtSchedule` (the value at the moment the advance
// was first detected — NOT re-snapshotted per cycle). When the window elapses
// with the WS still silent (append counter has not moved past the frozen
// snapshot) it fires one silent self-heal AND re-arms itself on the same
// cadence, so a WS that never recovers still keeps pulling freshly-written
// mid-step content into the open view — the reader never has to exit and
// re-enter. Since G5 that silent self-heal is a SIGNATURE-CHECK pull (it echoes
// the held token + signature): it costs a not_modified reply when nothing
// changed, folds in a delta tail when a little did, and only rebuilds the whole
// tree on a real divergence — NOT an unconditional full re-ship every window.
// It STOPS (does not re-arm) the instant a genuine WS increment lands
// (appendSeq moves past the frozen snapshot ⇒ the healthy push path recovered),
// the open flow changes, OR the flow reaches a terminal status (completed /
// failed) — a terminal flow yields no further content, and its final append can
// land before the arming snapshot so the counter never moves past it, which
// would otherwise wedge the loop into full-rebuilding the DOM every window
// forever. The frozen snapshot is the sole "WS recovered" gate:
// a silent rebuild deliberately does NOT bump flowConversationAppendSeq (only a
// real /ws/ui landing does), so comparing against the frozen value — rather than
// re-reading it each cycle — is what makes "keep retrying until the WS itself
// delivers something" terminate correctly.
function armProgressionGrace(flowId, seqAtSchedule) {
  state.progressionGraceFlowId = flowId;
  state.progressionGraceAppendSeqAtSchedule = seqAtSchedule;
  state.progressionGraceTimer = setTimeout(() => {
    // The pending timer just fired: drop the reference so cancelProgressionGrace
    // is a no-op and a future advance can re-arm cleanly.
    state.progressionGraceTimer = null;
    state.progressionGraceFlowId = null;
    state.progressionGraceAppendSeqAtSchedule = 0;
    // Stop the loop when the flow is no longer open, or when the WS push path
    // delivered a genuine increment for it during the window (append counter
    // moved past the frozen snapshot) — the live append is the update, so the
    // healthy path stays zero-rebuild and the loop terminates the moment WS
    // recovers.
    if (state.selectedFlowId !== flowId) return;
    if (state.flowConversationAppendSeq > seqAtSchedule) return;
    // WS stayed silent through this window: run the silent signature-check
    // self-heal so mid-step content the broken WS never pushed still appears
    // (a cheap not_modified/delta when little/nothing changed; a full rebuild
    // only on a real divergence).
    loadFlowConversation(flowId, { silent: true });
    // Terminal-status stop condition. A completed / failed flow can produce no
    // further mid-step content, so this one catch-up pull (which surfaces any
    // final record a broken WS never delivered) is the LAST work needed — do NOT
    // re-arm. Without this guard a terminal flow whose final WS append landed
    // before the arming snapshot would leave the append counter permanently at
    // the frozen value (no future append can ever bump it), so the "WS recovered"
    // gate could never fire and the loop would silently full-rebuild the DOM
    // every window forever while the view stays open. Only a still-live flow
    // re-arms, to keep pulling until a genuine WS increment resumes.
    const found = findFlow(flowId);
    if (found && isTerminalFlow(found.flow)) return;
    armProgressionGrace(flowId, seqAtSchedule);
  }, state.progressionGraceMs);
}

// Fallback-when-the-WS-goes-quiet: detect that the open flow advanced and, if
// the WS push path fails to deliver the increment on its own, silently rebuild
// the conversation. Issue #209's root cause — the daemon _push_loop starved
// under a heavy root so incremental history_data never went out — is FIXED by
// #243/#244 (the push side now reads engine headers off the event loop), so on
// a step switch / retry the WS delta now arrives on its own within ~2s. This is
// therefore no longer a trigger-on-every-advance workaround but a FAILURE
// SAFETY NET: on a detected advance we start a grace window and only fire the
// silent rebuild if no WS increment landed for this flow before it elapses.
//
// Compares `flow`'s current_step / current_step_index / status against the held
// baseline (`state.flowProgressionMarker`). The first observation of a flow
// (no baseline, or a baseline bound to a different flowId) only establishes the
// baseline and never triggers. Afterwards the flow is judged to have advanced
// when ANY of these reliably-observable, real-payload signals changes:
//   * current_step      — a step-to-step switch (discovery→analyze, …);
//   * current_step_index — the same switch viewed positionally (belt-and-braces
//                          for a step whose type label happens to repeat);
//   * status            — an in-step retry/resume, where current_step / index
//                          stay the same (the engine reuses the step_id) but the
//                          flow flips FAILED/PAUSED→RUNNING when the operator
//                          chooses Retry and the step re-runs (see run.py's
//                          resume path). Only this forward-motion transition (a
//                          dead/paused flow coming back to RUNNING) counts —
//                          NOT every status change. A RUNNING→FAILED or
//                          RUNNING→PAUSED transition is the flow stopping, not
//                          advancing, so it must NOT trigger a refresh; otherwise
//                          a step failure on the open flow would fire a spurious
//                          full /api/history reload on a non-progression snapshot.
// NOTE: the daemon's FlowSnapshot.to_dict() never emits a `step_history` field
// (the server back-fills it to an empty list), so a step_history-length signal
// is permanently dead and is deliberately NOT used here; the constrained
// retry/resume status transition is the real signal that captures the same-step
// retry case.
// If the advanced flow is the one currently open (`state.selectedFlowId`
// matches), we snapshot flowConversationAppendSeq and arm a self-re-arming grace
// timer (armProgressionGrace) instead of rebuilding immediately. On a fresh
// advance any earlier pending loop is cancelled and re-armed against the newest
// step, so a real multi-step burst observes the latest step and a duplicate
// snapshot (marker already updated ⇒ not advanced) neither re-arms nor re-fires.
// When a window elapses the timer rebuilds ONLY if the flow is still open and the
// WS append counter has not moved past the snapshot (the push path stayed
// silent), and then re-arms for the next window — so under a WS that never
// recovers it keeps pulling freshly-written mid-step content on the grace cadence
// (the reader need not exit/re-enter) and stops the instant a genuine WS
// increment lands. On the healthy path the very first window sees the counter
// already moved and never rebuilds — zero rebuilds. The baseline marker is
// updated on every call regardless, and a steady-state flow with no advance arms
// nothing. Only the conversation region and its state are touched — the reply
// region (draft / focus / textarea height) is never read or written here or in
// the callback.
function maybeRefreshConversationOnProgression(flow) {
  if (!flow || typeof flow !== "object") return;
  const flowId = flow.flow_id;
  if (!flowId) return;
  const currentStep = flow.current_step != null ? flow.current_step : null;
  const currentStepIndex = Number.isFinite(flow.current_step_index)
    ? flow.current_step_index : null;
  const status = flow.status != null ? flow.status : null;
  const marker = state.flowProgressionMarker;
  // First observation of this flow: only establish the baseline, never trigger
  // (the first-open full load already shows the whole conversation).
  if (!marker || marker.flowId !== flowId) {
    state.flowProgressionMarker = { flowId, currentStep, currentStepIndex, status };
    return;
  }
  // A status change only counts as advancement when it is a forward-motion
  // retry/resume transition — a previously FAILED/PAUSED flow flipping back to
  // RUNNING. A RUNNING→FAILED / RUNNING→PAUSED transition is the flow stopping,
  // not advancing, so it must NOT fire a refresh.
  //
  // The comparison MUST be case-insensitive: production flow.status arrives
  // LOWERCASE (FlowStatus enum values are "running"/"failed"/"paused",
  // serialized via .value and passed through the server unchanged), so an
  // uppercase-literal compare would make resumedFromHalt permanently false and
  // the same-step retry case (the ONLY trigger for in-step retry) dead. Mirror
  // the rest of app.js (e.g. `String(flow.status||"").toLowerCase()`).
  const statusUpper = String(status || "").toUpperCase();
  const markerStatusUpper = String(marker.status || "").toUpperCase();
  const resumedFromHalt =
    statusUpper === "RUNNING" &&
    (markerStatusUpper === "FAILED" || markerStatusUpper === "PAUSED");
  const advanced =
    currentStep !== marker.currentStep ||
    currentStepIndex !== marker.currentStepIndex ||
    resumedFromHalt;
  // Always refresh the baseline so a duplicate snapshot of the same advance
  // (e.g. the 3s poll re-delivering what the WS push already carried) triggers
  // at most once.
  state.flowProgressionMarker = { flowId, currentStep, currentStepIndex, status };
  if (advanced && state.selectedFlowId === flowId) {
    // Re-arm from a clean slate: cancel any retry loop still pending from an
    // earlier advance so a real burst of steps observes only the newest one and
    // cannot stack multiple concurrent fallback loops.
    cancelProgressionGrace();
    // G3 convergence: while the view's 3s periodic full-snapshot self-heal is
    // running it is the primary self-heal path (it re-pulls the whole
    // conversation every 3s and idempotently reconciles, healing any dropped
    // increment before the 5s grace window would even elapse). Arming the grace
    // loop too would issue a duplicate full pull on nearly the same cadence, so
    // demote the fallback to marker-only here — the advance is still recorded
    // above for activity/stall detection, but self-heal is deferred to the poll.
    // Only when the poll is NOT active (a view without it, or the DOM-free
    // progression tests) does the grace loop remain the self-heal path.
    if (state.periodicSnapshotActive) return;
    // Freeze the append counter at this advance and hand it to the self-re-arming
    // grace timer, which keeps pulling on the grace cadence until a genuine WS
    // increment lands past this snapshot (or the flow closes) — so a WS that
    // never recovers still surfaces mid-step content without an exit/re-enter.
    armProgressionGrace(flowId, state.flowConversationAppendSeq);
  }
}

// The running-flow view's 3s detailPollTimer callback (G3). It refreshes the
// left-side detail (sidebar / interventions) AND runs the right-side periodic
// self-heal on the SAME cadence — reusing the left side's already proven 3s
// rhythm and its epoch/seq race guards rather than adding a second timer with
// duplicated lifecycle management. refreshFlowDetail is fire-and-forget async;
// selfHealFlowConversation likewise. Kept as its own function (not an inline
// arrow) so it is exportable for the DOM-stub tests.
function pollFlowView() {
  refreshFlowDetail();
  selfHealFlowConversation();
}

// Periodic signature-check self-heal for the open running-flow conversation (G3,
// slimmed by G5). Every tick loadFlowConversation's silent path echoes the held
// progress token + bundle signature: the server answers not_modified when
// nothing changed (the cheap idle case — no bundle re-ship, no repaint), a delta
// tail when a little did, and only a full rebuild on a real divergence. So any WS
// increment the push path dropped, or any front-end/daemon/server misjudgement
// that stalled the delta chain, is still corrected at the next 3s tick — the #209
// freeze defence is intact — but the per-poll WIRE cost is now a signature
// comparison, not a full 17MB pull. The idempotent reconcile (G2) plus
// sameRenderedConversation still guard against any repaint when a full reply
// happens to match what is held.
//
// Terminal STATUS is deliberately NOT a stop condition. An earlier version
// latched the self-heal off after one post-terminal catch-up pull, but the
// daemon/server can flip a flow to completed/failed BEFORE the history cache
// holds the final commit / code-index result — so that single catch-up pull can
// capture a stale snapshot, and if the WS append carrying the commit result was
// also dropped, the right side would freeze without ever showing the commit
// result. No purely front-end signal can reliably tell "content is temporarily
// static because the cache is still catching up" from "content is final", so we
// do not try to guess: matching the left-side detail poll (refreshFlowDetail,
// which likewise runs every tick for the whole open view regardless of status),
// the self-heal keeps pulling on every tick while the view is open. The pull
// stops only when the view closes (endFlowDetailPolling clears the timer).
// loadFlowConversation's silent path is a cheap no-op — it skips the DOM rebuild
// whenever the pulled snapshot matches what is already held (see
// sameRenderedConversation) — so re-pulling a genuinely static terminal
// conversation costs one fetch per tick and never repaints, while a late commit
// result (cache catches up after the status flip) is picked up at the next tick
// and rendered. Correctness is thus the periodic full snapshot, never a timing
// guess about when the history became final.
function selfHealFlowConversation() {
  const flowId = state.selectedFlowId;
  if (!flowId) return;
  loadFlowConversation(flowId, { silent: true });
}

async function refreshFlowDetail() {
  const flowId = state.selectedFlowId;
  if (!flowId) return;
  // Claim a monotonic sequence number for this fetch. Detail fetches can run
  // concurrently (3s poll vs STATUS_UPDATE refresh) and resolve out of order, so
  // every applied result must be the freshest one observed — a late older
  // response is dropped below rather than allowed to regress state/marker.
  const reqSeq = ++state.flowDetailReqSeq;
  // Snapshot the lifecycle generation this fetch belongs to. If the view is
  // closed/reopened while the fetch is in flight, the generation advances and
  // this response is discarded on resolution — even when the same flow is
  // reopened (where the selectedFlowId check alone would let it through).
  const reqGen = state.flowDetailViewGen;
  try {
    const resp = await authedFetch(`/api/flows/${encodeURIComponent(flowId)}`);
    if (state.selectedFlowId !== flowId || state.flowDetailViewGen !== reqGen) return;
    if (!resp.ok) {
      noteDetailFetchFailure(tf("flow.detailLoadError", `Could not load flow details (${resp.status}).`, { status: resp.status }));
      return;
    }
    const data = await resp.json();
    if (state.selectedFlowId !== flowId || state.flowDetailViewGen !== reqGen) return;
    if (!data || !data.flow) {
      noteDetailFetchFailure(tf("flow.detailNotAvailable", "This flow is not available on the server yet."));
      return;
    }
    // Drop a stale response that lost the race to a newer detail fetch already
    // applied. Applying it would overwrite the fresher snapshot and rewind the
    // progression marker, spuriously re-triggering the silent refresh.
    if (reqSeq <= state.flowDetailAppliedSeq) return;
    state.flowDetailAppliedSeq = reqSeq;
    state.detailFetchFailures = 0;
    state.detailLoaded = true;
    state.flowDetail = data.flow;
    state.flowMachineId = data.machine_id || null;
    // Cause-immune fallback trigger: refreshFlowDetail is the single converging
    // point for every flow-detail update (3s poll, STATUS_UPDATE→applyMachines,
    // openFlowView, ws reconnect), and each call carries the authoritative
    // /api/flows/{id} snapshot — so checking for progression here covers all
    // those paths from one site. On a detected advance of the currently-open
    // flow it silently rebuilds the conversation, working around the recurring
    // "step-switch/retry freezes the main conversation" bug without touching the
    // incremental-push / dedupe / progress-token machinery.
    maybeRefreshConversationOnProgression(data.flow);
    // Settle a Send waiting on ws confirmation BEFORE rendering, so the
    // chip-bar rebuild reflects the unlocked state in one pass.
    maybeSettleViaPendingCallsDiff(data.flow);
    renderFlowSidebar(data.flow, data.machine_id);
    renderInterventions(data.flow);
    // The backend usage summary rides this snapshot; refresh the badge so a
    // mid-run usage change is reflected even when no new record landed.
    updateFlowUsageBadge(state.flowConversationRecords);
  } catch (_) {
    if (state.selectedFlowId !== flowId || state.flowDetailViewGen !== reqGen) return;
    noteDetailFetchFailure(tf("flow.detailNetworkError", "Network error loading flow details."));
  }
}

// Pure, DOM-free extraction of the sidebar's visible-dependency field subset
// into a value the diff-aware `renderSignature` can serialize. Every field that
// affects the rendered Overview / Steps / Machine / Resume output is included
// so any real change forces a rebuild, while an unchanged 3s poll / ws push
// produces an identical signature and skips the DOM. The step history is
// reduced to just the per-row visible bits (step_type + status + the duration
// the row prints), and the resume affordance is captured via both the static
// resumability predicate and the in-flight `resumeInProgress` flag (so the
// button's pending state toggle still rebuilds the sidebar).
function flowSidebarSignature(flow, machineId, resumeInProgress) {
  const f = flow && typeof flow === "object" ? flow : {};
  const steps = Array.isArray(f.step_history) ? f.step_history : [];
  return renderSignature({
    task_description: f.task_description ?? null,
    flow_id: f.flow_id ?? null,
    project_root: f.project_root ?? null,
    status: f.status ?? null,
    // Overview Status is flowStatusLabel(flow), whose · waiting-for-lock suffix
    // is fully determined by isWaitingForLock (status + the flag), both already
    // captured here — so the Status re-renders when the lock is acquired.
    waiting_lock: isWaitingForLock(f),
    task_type: f.task_type ?? null,
    current_step_index: f.current_step_index ?? null,
    total_steps: f.total_steps ?? null,
    progress: f.progress ?? null,
    current_step: f.current_step ?? null,
    updated_at: f.updated_at ?? null,
    steps: steps.map((s) => ({
      step_type: s.step_type ?? s.step_id ?? null,
      status: s.status ?? null,
      duration: s.duration != null ? s.duration : (s.elapsed ?? null),
    })),
    machineId: machineId ?? null,
    resumable: isFlowResumable(f),
    resumeInProgress: Boolean(resumeInProgress),
    // The plan-mode/scope/usage projections the sidebar renders: any change to
    // them must rebuild the sidebar like any other visible field.
    plan_mode: f.plan_mode ?? null,
    review_scope: f.review_scope ?? null,
    usage_summary: f.usage_summary ?? null,
  });
}

// Render the sidebar: Overview, Steps, and Machine. Rebuilt wholesale on each
// 3s poll — the panel is small, so a full rebuild does not visibly flicker.
// Guarded by a diff-aware signature: an unchanged poll / ws push computes the
// same `flowSidebarSignature` as last time and returns without touching the
// DOM, so the reply textarea's layout is never reflowed by an empty rebuild.
function renderFlowSidebar(flow, machineId) {
  const sig = flowSidebarSignature(
    flow, machineId, isResumeInProgress(flow && flow.flow_id),
  );
  if (state.renderSig.sidebar === sig) return;
  state.renderSig.sidebar = sig;

  $("flow-view-title").textContent =
    flow.task_description || flow.flow_id || "Flow";

  const body = $("flow-sidebar-body");
  body.innerHTML = "";

  const kv = (k, v, title) => {
    const row = el("div", "kv");
    const valEl = el("span", "v", String(v));
    if (title) valEl.title = title;
    row.append(el("span", "k", k), valEl);
    return row;
  };
  const sc = statusClass(flow.status);

  // -- overview --
  const overview = el("div", "detail-section");
  overview.appendChild(el("h4", null, tf("flowSidebar.overview", "Overview")));
  overview.appendChild(kv(tf("flowSidebar.status", "Status"), flowStatusLabel(flow)));
  overview.appendChild(kv(tf("flowSidebar.type", "Type"), flow.task_type || "-"));
  overview.appendChild(kv(
    tf("flowSidebar.project", "Project"), projectDisplayLabel(flow.project_root) || "-", flow.project_root || "",
  ));
  overview.appendChild(kv(
    tf("flowSidebar.progress", "Progress"),
    `${flow.current_step_index || 0}/${flow.total_steps || 0} ` +
    `(${Math.round((flow.progress || 0) * 100)}%)`,
  ));
  if (flow.current_step) overview.appendChild(kv(tf("flowSidebar.currentStep", "Current step"), flow.current_step));
  if (flow.updated_at) overview.appendChild(kv(tf("flowSidebar.updated", "Updated"), formatTime(flow.updated_at)));
  body.appendChild(overview);

  // -- plan decomposition mode / review scope / usage --
  // All three come from the shared backend projections the daemon relays in
  // the flow snapshot; the sidebar only labels them (see the rendering section
  // above). Absent projections add nothing, so an older daemon's snapshot
  // looks exactly as it did before.
  appendPlanModeSection(body, flow);
  const scopeRows = buildScopeRows(flow.review_scope);
  if (scopeRows) {
    const scopeSec = el("div", "detail-section");
    scopeSec.appendChild(el("h4", null, tf("scope.label", "Review scope")));
    scopeSec.appendChild(scopeRows);
    body.appendChild(scopeSec);
  }
  const compactUsage = usagePayloadSummary(flow.usage_summary);
  if (compactUsage) {
    const usageSec = el("div", "detail-section");
    usageSec.appendChild(el("h4", null, tf("usage.title", "Usage / cost")));
    renderCompactUsageSummary(usageSec, compactUsage);
    body.appendChild(usageSec);
  }

  // -- steps --
  const steps = Array.isArray(flow.step_history) ? flow.step_history : [];
  const stepSec = el("div", "detail-section");
  stepSec.appendChild(el("h4", null, tf("flowSidebar.steps", "Steps")));
  if (steps.length) {
    for (const step of steps) {
      const ss = String(step.status || "pending").toLowerCase();
      const row = el("div", "step-row");
      row.append(
        el("span", "step-icon " + ss, STEP_ICONS[ss] || "•"),
        el("span", "step-name", step.step_type || step.step_id || "step"),
      );
      const dur = step.duration != null ? step.duration : step.elapsed;
      if (dur != null) {
        row.appendChild(el("span", "step-dur", `${Math.round(Number(dur))}s`));
      }
      stepSec.appendChild(row);
    }
  } else if (flow.current_step) {
    const row = el("div", "step-row");
    const cs = sc === "running" ? "running" : (sc === "failed" ? "failed" : "pending");
    row.append(
      el("span", "step-icon " + cs, STEP_ICONS[cs] || "•"),
      el("span", "step-name", flow.current_step),
    );
    stepSec.appendChild(row);
  } else {
    stepSec.appendChild(el("p", "empty", tf("flow.noStepHistory", "No step history reported.")));
  }
  body.appendChild(stepSec);

  // -- machine --
  const machineSec = el("div", "detail-section");
  machineSec.appendChild(el("h4", null, tf("flowSidebar.machineSection", "Machine")));
  machineSec.appendChild(kv(tf("flowSidebar.machine", "Machine"), machineId || "-"));
  if (flow.flow_id) machineSec.appendChild(kv(tf("flowSidebar.flowId", "Flow id"), flow.flow_id));
  body.appendChild(machineSec);

  // -- resume / end --
  const resumeBtn = makeResumeButton(flow);
  const endBtn = makeEndButton(flow);
  if (resumeBtn || endBtn) {
    const actionSec = el("div", "detail-section");
    if (resumeBtn) actionSec.appendChild(resumeBtn);
    if (endBtn) actionSec.appendChild(endBtn);
    body.appendChild(actionSec);
  }
}

// ---------------------------------------------------------------------------
// Resume flow
// ---------------------------------------------------------------------------
//
// Dispatches a resume request to the backend for a FAILED or PAUSED flow.
// The backend validates the flow's engine state and spawns `se3 run --resume`.
// The button is disabled while the request is in-flight (tracked via
// `state.resumeFlowRequests`) to prevent duplicate dispatches.

// Pure helper: is a resume request currently in-flight for *flowId*?
function isResumeInProgress(flowId) {
  return state.resumeFlowRequests.has(flowId);
}

// Pure helper: pick the toast text for a failed resume dispatch.
//
// WHY: a 404 has two very different causes and the generic "not found" wording
// misleads on the second one. On a shared filesystem (HPC-style clusters) the
// same flow is reported by whichever node currently runs the job; once the
// owning machine goes offline the backend answers 404 with
// "machine '<id>' owning flow '<id>' is not connected", which is actionable
// (the session moved nodes) rather than "this flow does not exist".
//
// The offline case is recognised by substring-matching the backend's ENGLISH
// detail — a deliberately weak contract (app.py builds that string with an
// f-string, so a reword there silently breaks the match). The degradation is
// benign and explicit: an unmatched non-empty detail is shown verbatim, so the
// user still sees the backend's own reason; only the localized phrasing is
// lost. A backend detail change should therefore be mirrored here, but never
// leaves the UI without a message.
//
// The 409 "held by another machine" case is the one place the backend hands us
// a MACHINE-READABLE field (`holder_machine`) instead of relying on its own
// prose: the refusal names a host the operator must go to, and that sentence
// has to render in the console's own language. When the field is present the
// localized key wins over the backend detail (which stays the fallback for a
// backend that has not yet been upgraded to send it).
//
// Other non-404 statuses are intentionally passed straight through (detail
// verbatim, "" when absent) so those branches keep their own fallback wording.
function resumeErrorText(status, detail, holderMachine, reason) {
  const text = typeof detail === "string" ? detail.trim() : "";
  const machine = typeof holderMachine === "string" ? holderMachine.trim() : "";
  if (status === 409 && machine) {
    return tf(
      "toast.resumeHeldByMachine",
      text || `This flow is running on machine ${machine} and cannot be resumed from here.`,
      { machine },
    );
  }
  // The backend refuses without naming a host when several daemons report the
  // same shared-filesystem flow and none can be singled out as its holder — it
  // deliberately says nothing machine-specific rather than blame an observer.
  // The `reason` code carries that case so the console still renders in its own
  // language instead of echoing the backend's Chinese detail.
  if (status === 409 && reason === "still_running") {
    return tf(
      "toast.resumeStillRunning",
      text || "This flow is still running and cannot be resumed",
    );
  }
  if (status !== 404) return text;
  if (/is not connected/.test(text)) {
    return tf("toast.resumeMachineOffline", text);
  }
  if (text) return text;
  return tf("toast.resumeNotFound", "Flow not found or not resumable.");
}

async function resumeFlow(flowId) {
  if (!flowId) return;
  if (state.resumeFlowRequests.has(flowId)) return; // debounce
  state.resumeFlowRequests.add(flowId);
  // Re-render affected surfaces so the button shows a disabled/pending state.
  renderFlows();
  renderHistoryList();
  if (state.flowDetail && state.flowDetail.flow_id === flowId) {
    renderFlowSidebar(state.flowDetail, state.flowMachineId);
  }
  try {
    const resp = await authedFetch(
      `/api/flows/${encodeURIComponent(flowId)}/resume`,
      { method: "POST" },
    );
    if (resp.ok) {
      showToast("success", tf("toast.resumeDispatched", `Resume dispatched for ${flowId.slice(0, 8)}…`, { id: flowId.slice(0, 8) }));
    } else if (resp.status === 404) {
      // Either the flow is genuinely unknown or its owning machine went
      // offline (shared-FS node switch) — resumeErrorText tells them apart.
      let detail = "";
      try { detail = (await resp.json()).detail || ""; } catch (_) {}
      showToast("error", resumeErrorText(404, detail));
    } else if (resp.status === 409) {
      // The flow exists but is not resumable right now — typically it is still
      // running (a live process holds it). When the backend names the holding
      // machine, resumeErrorText renders it from the local language pack;
      // otherwise the backend's own rejection detail is surfaced rather than a
      // misleading "dispatched" success.
      let detail = "";
      let holderMachine = "";
      let reason = "";
      try {
        const body = await resp.json();
        detail = body.detail || "";
        holderMachine = body.holder_machine || "";
        reason = body.reason || "";
      } catch (_) {}
      showToast(
        "error",
        resumeErrorText(409, detail, holderMachine, reason)
          || tf("toast.resumeStillRunning", "This flow is still running and cannot be resumed"),
      );
    } else {
      let detail = "";
      try { detail = (await resp.json()).detail || ""; } catch (_) {}
      showToast("error", detail || tf("toast.resumeFailed", `Resume failed (${resp.status}).`, { status: resp.status }));
    }
  } catch (_) {
    showToast("error", tf("toast.resumeNetworkError", "Network error — could not dispatch resume."));
  } finally {
    state.resumeFlowRequests.delete(flowId);
    renderFlows();
    renderHistoryList();
    if (state.flowDetail && state.flowDetail.flow_id === flowId) {
      renderFlowSidebar(state.flowDetail, state.flowMachineId);
    }
  }
}

// Create a Resume button for a resumable flow.  Returns null when the flow
// is not resumable, so callers can unconditionally append the result.
function makeResumeButton(flow) {
  if (!isFlowResumable(flow)) return null;
  const flowId = flow.flow_id;
  const pending = isResumeInProgress(flowId);
  const btn = el("button", "btn-resume", pending ? tf("flow.resuming", "Resuming…") : tf("flow.resume", "Resume"));
  btn.type = "button";
  btn.disabled = pending;
  btn.title = tf("flow.resumeTitle", "Resume this flow");
  btn.addEventListener("click", (e) => {
    e.stopPropagation(); // don't bubble to the card's click handler
    resumeFlow(flowId);
  });
  return btn;
}

// ---------------------------------------------------------------------------
// End session
// ---------------------------------------------------------------------------
//
// Ends (and, for worktree sessions, archives) a non-completed flow. A
// synchronous `se3 run` on the main branch only needs the process terminated,
// but a `--worktree` flow otherwise leaves an orphan worktree behind forever;
// the backend `POST /api/flows/{id}/end` dispatches `MSG_END_SESSION` to the
// owning daemon which terminates the process and archives the worktree just
// like a normally-completed session. The control is gated by the pure
// `isFlowEndable` predicate (a UI gate mirroring the server's
// `ServerState.is_flow_endable`) and debounced via `state.endSessionRequests`.

// Pure helper: is *projectRoot* a `--worktree` isolation directory
// (`<main>/tianluo/worktrees/<name>`, legacy `<main>/se3/worktrees/<name>`)?  This is the structural check the server's
// `_is_worktree_session_path` mirrors — a live (possibly dangling) worktree run
// is reported with its worktree sandbox as `project_root`.
function isWorktreeSessionPath(projectRoot) {
  if (!projectRoot) return false;
  const parts = String(projectRoot)
    .replace(/\\/g, "/")
    .split("/")
    .filter(Boolean);
  if (parts.length < 3) return false;
  const runtimeSeg = parts[parts.length - 3];
  return parts[parts.length - 2] === "worktrees" &&
    (runtimeSeg === "tianluo" || runtimeSeg === "se3");
}

// Pure UI gate: may *flow* be ended from the console?  A flow is endable when
// it carries a flow_id and is not an archived/history-only snapshot (those have
// no live process / worktree to clean up). Every active or recoverable state
// (running / paused / failed / recovering / init) is endable, because a
// dangling worktree may be left by any of them. A COMPLETED flow is normally
// NOT endable (it was cleaned up the ordinary way) — EXCEPT a completed
// worktree session whose `project_root` still points inside
// `<main>/se3/worktrees/<name>`: that is a `se3 run --worktree` flow whose
// follow-up merge/cleanup failed, leaving an orphan worktree on disk that this
// feature exists to archive, so it stays endable. This mirrors the server's
// `ServerState.is_flow_endable` pre-check.
function isFlowEndable(flow) {
  if (!flow || typeof flow !== "object") return false;
  if (!flow.flow_id) return false;
  const src = String(flow.source || "").toLowerCase();
  if (src === "archived" || src === "history") return false;
  if (String(flow.status || "").toLowerCase() === "completed") {
    return isWorktreeSessionPath(flow.project_root);
  }
  return true;
}

// Pure helper: is an end-session request currently in-flight for *flowId*?
function isEndInProgress(flowId) {
  return state.endSessionRequests.has(flowId);
}

let _endSessionPending = false;

function openEndSessionModal(flow) {
  if (!flow || !flow.flow_id) return;
  const modal = $("end-session-modal");
  if (!modal) return;
  const errBox = $("end-session-error");
  if (errBox) errBox.classList.add("hidden");
  const msgNode = $("end-session-message");
  if (msgNode) {
    msgNode.textContent = tf("endSession.confirmMessage",
      "Confirm ending and archiving this session (" + flow.flow_id.slice(0, 8) + "…)? " +
      "A worktree session will be cleaned up and archived, and uncommitted work will not be merged into the main branch.",
      { id: flow.flow_id.slice(0, 8) });
  }
  modal.dataset.flowId = flow.flow_id;
  modal.classList.remove("hidden");
}

function closeEndSessionModal() {
  const modal = $("end-session-modal");
  if (modal) modal.classList.add("hidden");
  _endSessionPending = false;
}

async function confirmEndSession() {
  if (_endSessionPending) return;
  _endSessionPending = true;
  const modal = $("end-session-modal");
  const flowId = modal ? modal.dataset.flowId : "";
  const confirmBtn = $("end-session-confirm");
  if (confirmBtn) confirmBtn.disabled = true;
  try {
    await endFlow(flowId);
    closeEndSessionModal();
  } finally {
    if (confirmBtn) confirmBtn.disabled = false;
    _endSessionPending = false;
  }
}

async function endFlow(flowId) {
  if (!flowId) return;
  if (state.endSessionRequests.has(flowId)) return; // debounce
  state.endSessionRequests.add(flowId);
  // Re-render affected surfaces so the button shows a disabled/pending state.
  renderFlows();
  renderHistoryList();
  if (state.flowDetail && state.flowDetail.flow_id === flowId) {
    renderFlowSidebar(state.flowDetail, state.flowMachineId);
  }
  try {
    const resp = await authedFetch(
      `/api/flows/${encodeURIComponent(flowId)}/end`,
      { method: "POST" },
    );
    if (resp.ok || resp.status === 202) {
      showToast("success", tf("toast.endDispatched", `End dispatched for ${flowId.slice(0, 8)}…`, { id: flowId.slice(0, 8) }));
    } else if (resp.status === 404) {
      showToast("error", tf("toast.flowNotFound", "Flow not found."));
    } else if (resp.status === 409) {
      let detail = "";
      try { detail = (await resp.json()).detail || ""; } catch (_) {}
      showToast("error", detail || tf("toast.endAlreadyEnded", "This session has already ended and cannot be ended again."));
    } else if (resp.status === 503) {
      showToast("error", tf("toast.endMachineOffline", "Machine not connected — could not dispatch the end command."));
    } else {
      let detail = "";
      try { detail = (await resp.json()).detail || ""; } catch (_) {}
      showToast("error", detail || tf("toast.endFailed", `End failed (${resp.status}).`, { status: resp.status }));
    }
  } catch (_) {
    showToast("error", tf("toast.endNetworkError", "Network error — could not dispatch end."));
  } finally {
    state.endSessionRequests.delete(flowId);
    renderFlows();
    renderHistoryList();
    if (state.flowDetail && state.flowDetail.flow_id === flowId) {
      renderFlowSidebar(state.flowDetail, state.flowMachineId);
    }
  }
}

// Create an End button for an endable flow.  Returns null when the flow is not
// endable, so callers can unconditionally append the result.  Clicking opens
// the confirmation modal rather than ending immediately (a destructive op).
function makeEndButton(flow) {
  if (!isFlowEndable(flow)) return null;
  const flowId = flow.flow_id;
  const pending = isEndInProgress(flowId);
  const btn = el("button", "btn-end", pending ? tf("flow.ending", "Ending…") : tf("flow.end", "End"));
  btn.type = "button";
  btn.disabled = pending;
  btn.title = tf("flow.endTitle", "End (and archive) this session");
  btn.addEventListener("click", (e) => {
    e.stopPropagation(); // don't bubble to the card's click handler
    openEndSessionModal(flow);
  });
  return btn;
}

// ---------------------------------------------------------------------------
// Intervention items
// ---------------------------------------------------------------------------
//
// Every point at which a running flow needs a human is collapsed onto the same
// carrier — a `pending_calls` entry tagged with a `kind`. The four kinds plus
// a frontend-initiated interjection are rendered as prominent, default-
// expanded intervention items that never blend into ordinary conversation
// bubbles. The docked reply box targets exactly one of them at a time.

// User-facing labels use neutral wording. Implementation details (MCP, call
// ids, etc.) are intentionally absent from any string the user can see — they
// only appear on the underlying `data-call-id` attribute and `title` tooltips
// so operators can still cross-reference call files when debugging.
const KIND_META = {
  call: {
    label: "Awaiting reply",
    labelKey: "intervention.call.label",
    hint: "The running flow is waiting for your reply.",
    hintKey: "intervention.call.hint",
    icon: "⚙",
  },
  interjection: {
    label: "Interject",
    labelKey: "intervention.interjection.label",
    hint: "Add an extra instruction to the running flow.",
    hintKey: "intervention.interjection.hint",
    icon: "✎",
  },
  retry_decision: {
    label: "Decision needed",
    labelKey: "intervention.retryDecision.label",
    hint: "A step failed; choose how to continue (e.g. retry / skip / abort).",
    hintKey: "intervention.retryDecision.hint",
    icon: "↻",
  },
  cli_confirm: {
    label: "Confirmation needed",
    labelKey: "intervention.cliConfirm.label",
    hint: "A subprocess is waiting for a confirmation.",
    hintKey: "intervention.cliConfirm.hint",
    icon: "⌨",
  },
  discovery_confirm: {
    label: "Confirm task description",
    labelKey: "intervention.discoveryConfirm.label",
    hint: "Discovery has produced a refined task description. Enter 1 to confirm and continue, or reply with anything else to keep refining.",
    hintKey: "intervention.discoveryConfirm.hint",
    icon: "✓",
  },
  // A CONFIRM approval gate (plan 确认 / adjudicate 裁决审批 / per-step review).
  // Unlike discovery_confirm's "type 1 to continue", this renders explicit
  // 批准/打回 buttons that POST a structured {approved, feedback} decision, so
  // an operator can never silently mis-approve/mis-reject via free text.
  confirm: {
    label: "Approval needed",
    labelKey: "intervention.confirm.label",
    hint: "The running flow is waiting for your approval. Click Approve to pass, or Reject and explain what needs changing.",
    hintKey: "intervention.confirm.hint",
    icon: "✓",
  },
};

// Canonicalize a raw `kind` field; unknown kinds degrade to a plain "call".
function normalizeKind(kind) {
  const k = String(kind || "call").toLowerCase();
  return KIND_META[k] ? k : "call";
}

// The `step_type` of an optimistic local echo, e.g. "reply_call".
// WHY: `step_type` is an IDENTIFIER, never display text — it is used as a DOM
// class suffix (`step-type-<type>`), as a grouping key, and as the step-header
// fallback. Putting a rendered i18n label in it made the field's DOM validity
// depend on the language pack: zh-CN's "待回复 回复" contains a space, and
// `classList.add()` then threw InvalidCharacterError on every render of the
// echo, freezing the whole chat view. The token is therefore derived from the
// (already canonical, ASCII) kind and never passes through I18N; the human
// label is resolved separately at render time by stepHeaderLabel.
const REPLY_STEP_TYPE_PREFIX = "reply_";
function replyStepType(kind) {
  return REPLY_STEP_TYPE_PREFIX + normalizeKind(kind);
}

// Derive the ordered list of intervention entries for a flow. Each pending
// call becomes one entry. The reply box is enabled ONLY when an entry exists,
// so during a normal step with nothing waiting on input the box stays
// disabled — matching CLI parity. A synthetic "interject now" entry is
// appended only when the user has explicitly opted in via the Interject
// button (state.flowInterjectRequested) AND the flow is still active AND
// there is no real interjection already pending. Pure: depends only on
// `flow` and `state.flowInterjectRequested`.
function computeInterventions(flow) {
  // Local synthetic chips come first: each `localInterjections` entry the
  // user has Send-submitted gets one chip. Once bound to a real call_id via
  // `bindLocalInterjectionToCallId`, the real `pending_calls` entry with
  // the same id is suppressed below so we never render the same
  // interjection twice (the local chip continues to represent it through
  // the consumed transition).
  const localList = state.localInterjections || [];
  const localEntries = localList.map((e) => ({
    id: e.callId ? "local-call:" + e.callId : "local:" + e.localId,
    kind: "interjection",
    callId: e.callId || "",
    prompt: String(e.text || ""),
    context: null,
    options: [],
    synthetic: true,
    localId: e.localId,
    phase: e.phase || "pending",
  }));
  const localBoundCallIds = new Set(
    localList.map((e) => e.callId).filter(Boolean),
  );

  const realEntries = pendingCalls(flow)
    .filter((c) => !localBoundCallIds.has(String(c.call_id || "")))
    .map((c, i) => {
      const kind = normalizeKind(c.kind);
      const callId = String(c.call_id || "");
      // The interjection lifecycle phase comes from ws `interjection_event`
      // messages tracked in `state.interjectionPhases`. Default phase: a
      // freshly-aggregated pending_calls entry is implicitly `pending` so the
      // chip picks up the pending visual state even before the matching
      // event arrives (e.g. on a slow server).
      let phase = null;
      if (kind === "interjection") {
        phase = state.interjectionPhases[callId] || "pending";
      }
      return {
        id: "call:" + (callId || ("idx" + i)),
        kind: kind,
        callId: callId,
        // STATUS_UPDATE clips this prompt to DESC_CLIP; the reply-context
        // lazy-loads the untruncated body on expand via GET /api/calls/{id}/
        // detail (needs the owning project root to disambiguate a local id).
        prompt: String(c.prompt || c.message || ""),
        projectRoot: c.project_root != null ? String(c.project_root) : "",
        // Pin the on-demand full-prompt pull to the flow's owning daemon.
        // project_root alone is ambiguous when one owner has two daemons on the
        // same absolute path with a colliding local call_id; the open flow's
        // machine_id disambiguates so the server returns THIS call's full body.
        machineId:
          c.machine_id != null
            ? String(c.machine_id)
            : state.flowMachineId
              ? String(state.flowMachineId)
              : "",
        context: c.context != null ? c.context : null,
        options: Array.isArray(c.options) ? c.options : [],
        synthetic: false,
        phase: phase,
      };
    });

  const entries = [...localEntries, ...realEntries];
  // Only a REAL pending interjection in `pending_calls` should suppress the
  // standby "interjection:new" chip — local synthetic entries represent
  // already-submitted drafts and must coexist with the standby chip so the
  // user can immediately draft another while previous ones are in flight.
  const hasInterjection = realEntries.some((e) => e.kind === "interjection");
  // Render the synthetic interjection chip when:
  //   - the user has opted into interject mode (clicked the ✎ button), OR
  //   - a synthetic chip is held in pending state after a Send press while
  //     we wait for the real interjection chip to materialize via ws.
  // Either way it is suppressed once a real interjection already exists in
  // pending_calls (the real chip displaces it).
  const wantSynthetic =
    (state.flowInterjectRequested || state.flowSyntheticInterjectPending) &&
    isActiveFlow(flow) &&
    !hasInterjection;
  if (wantSynthetic) {
    entries.push({
      id: "interjection:new",
      kind: "interjection",
      callId: "",
      prompt: "",
      context: null,
      options: [],
      synthetic: true,
      localId: null,
      phase: state.flowSyntheticInterjectPending ? "pending" : null,
    });
  }
  // Re-inject brief afterimage chips for interjections that were just
  // consumed and dropped from pending_calls. They render with
  // `.state-consumed` so the user sees a pending → consumed transition.
  // The afterimage is skipped when the same call_id is somehow still in
  // pending_calls (defensive) so we never duplicate.
  const existingCallIds = new Set(
    entries.map((e) => e.callId).filter(Boolean),
  );
  const now = Date.now();
  for (const a of state.interjectionConsumedAfterimages || []) {
    if (a.untilTs <= now) continue;
    if (existingCallIds.has(a.callId)) continue;
    entries.push({
      id: "call:" + a.callId,
      kind: "interjection",
      callId: a.callId,
      prompt: a.prompt || "",
      context: null,
      options: [],
      synthetic: false,
      phase: "consumed",
      afterimage: true,
    });
  }
  return entries;
}

// Decide which intervention the reply box should target after the chip bar is
// rebuilt from the latest `pending_calls`. Pure: depends only on its args.
//
//   - Keep the current selection when its chip still exists, so a live
//     pending_calls refresh that left the selected call in place does not
//     yank the reply box out from under the user mid-reply.
//   - Otherwise (the selected call was answered/withdrawn and dropped by the
//     backend aggregator, so its chip is gone) re-home onto the first real
//     pending call, falling back to the first entry — or `null` when the bar
//     is now empty, which resets the reply box to its disabled/idle state.
function reconcileReplyTarget(entries, currentTargetId) {
  if (!Array.isArray(entries) || !entries.length) return null;
  if (entries.some((e) => e.id === currentTargetId)) return currentTargetId;
  // Afterimage chips (consumed-state transitional placeholders) are not
  // targetable — they exist purely for the visual transition.
  const firstCall = entries.find((e) => !e.synthetic && !e.afterimage);
  const firstActive = entries.find((e) => !e.afterimage);
  const fallback = firstCall || firstActive;
  return fallback ? fallback.id : null;
}

// Rebuild the intervention chip-bar (sits inside the docked reply form, above
// the reply-context panel) and re-sync the reply box. Called from the 3s
// detail poll AND from every `refreshFlowDetail` (status_update / WS) so the
// chip bar tracks the latest `pending_calls`: a call the backend no longer
// reports loses its chip immediately (no stale "待回复" hanging through the
// run), and a newly-appeared call gains its chip on the next refresh.
// Selection (`flowReplyTargetId`) and the typed-but-unsent reply text are
// deliberately preserved across rebuilds when the selected chip survives;
// when it does not, `reconcileReplyTarget` re-homes (or clears) the target so
// the reply box never points at a vanished chip. Chips do NOT render the
// intervention's prompt/context/options — that lives in `updateReplyBox`'s
// reply-context panel for the currently selected chip only.
function renderInterventions(flow) {
  const entries = computeInterventions(flow);
  const targetId = reconcileReplyTarget(entries, state.flowReplyTargetId);
  const hasRealInterjection = entries.some(
    (e) => e.kind === "interjection" && !e.synthetic,
  );
  const sig = interventionsSignature(entries, {
    targetId,
    pendingSendSettleKey: state.pendingSendSettleKey,
    flowInterjectRequested: state.flowInterjectRequested,
    isActiveFlow: isActiveFlow(flow),
    hasRealInterjection,
  });

  // Always sync the pure state the rest of the app reads (the entries list and
  // the reconciled reply target), so the skip path leaves no stale data behind
  // even though it touches no DOM. The expand/scroll persistent UI state is
  // intentionally left alone — on a skip the reply-context block is not rebuilt,
  // so the user's collapse/scroll choices survive automatically.
  state.flowInterventions = entries;
  state.flowReplyTargetId = targetId;

  // Diff-aware skip (plan B): when the visible-dependency signature is
  // unchanged since the last render, produce ZERO DOM mutations. This is the
  // core of the textarea-jank fix — an empty status_update (the common case)
  // must not innerHTML="" the chip bar nor rebuild #flow-reply-context, so the
  // large reply textarea never reflows mid-typing.
  if (state.renderSig.interventions === sig) return;
  state.renderSig.interventions = sig;

  const region = $("flow-interventions");
  region.innerHTML = "";
  for (const entry of entries) {
    region.appendChild(renderInterventionChip(entry));
  }

  syncInterjectButton(flow);
  updateReplyBox(flow);
}

// Click handler for the inline Interject icon button. Toggles the opt-in
// flag and selects (or unselects) the synthetic interjection chip so the
// reply box's Send target updates without any other interaction.
function onInterjectButtonClick(e) {
  e.preventDefault();
  const flow = state.flowDetail;
  if (!isActiveFlow(flow)) return;
  if (state.flowInterjectRequested) {
    // Exit interject mode: drop opt-in and clear target (real call selection
    // takes priority on the next rebuild via the firstCall preference).
    state.flowInterjectRequested = false;
    if (state.flowReplyTargetId === "interjection:new") {
      state.flowReplyTargetId = null;
    }
  } else {
    state.flowInterjectRequested = true;
    state.flowInterjectFlowId = flow && flow.flow_id ? flow.flow_id : null;
    state.flowReplyTargetId = "interjection:new";
  }
  renderInterventions(flow);
  if (state.flowInterjectRequested) $("flow-reply-input").focus();
}

// Show / hide / toggle the active state of the inline Interject icon button
// next to the textarea. The button materializes the synthetic interjection
// chip (opt-in) without taking a chip-bar row of its own.
function syncInterjectButton(flow) {
  const btn = $("flow-interject-btn");
  if (!btn) return;
  const entries = state.flowInterventions || [];
  const hasRealInterjection = entries.some(
    (e) => e.kind === "interjection" && !e.synthetic,
  );
  if (!isActiveFlow(flow) || hasRealInterjection) {
    btn.classList.add("hidden");
    btn.classList.remove("active");
    return;
  }
  btn.classList.remove("hidden");
  const active = !!state.flowInterjectRequested;
  btn.classList.toggle("active", active);
  btn.title = active
    ? tf("interject.cancelTitle", "Cancel interjection")
    : tf("interject.title", "Add an extra instruction to the running flow.");
  btn.textContent = active ? "✕" : "✎";
}

// Render one intervention entry as a compact chip — kind icon + label +
// optional short call_id. Clicking the chip selects it as the reply box's
// target (the full prompt / context / options then materialize in the
// reply-context panel above the textarea). The chip itself never expands.
function renderInterventionChip(entry) {
  const meta = KIND_META[entry.kind] || KIND_META.call;
  const chip = el("button", "intervention-chip kind-" + entry.kind);
  chip.type = "button";
  if (entry.id === state.flowReplyTargetId) chip.classList.add("selected");
  // Interjection chips carry a lifecycle phase (`pending` or `consumed`)
  // learned from ws `interjection_event` messages — and a synthetic chip
  // that is currently mid-send is also marked pending. The class is purely
  // visual; selection, click, and reply targeting all still work normally.
  if (entry.phase === "pending") chip.classList.add("state-pending");
  else if (entry.phase === "consumed") chip.classList.add("state-consumed");

  chip.append(
    el("span", "intervention-chip-icon", meta.icon),
    el("span", "intervention-chip-label", tf(meta.labelKey, meta.label)),
  );
  if (entry.callId) {
    // The internal call id stays on the chip for debugging — surfaced only
    // through `data-call-id` and the hover tooltip — but never rendered as
    // visible text so the user never sees implementation jargon.
    chip.dataset.callId = entry.callId;
    chip.title = entry.callId;
  }

  // Afterimage chips (consumed-state visual transition placeholders) are
  // not interactive — they are about to vanish on their own.
  if (entry.afterimage) {
    chip.disabled = true;
  } else {
    chip.addEventListener("click", (e) => {
      e.preventDefault();
      if (state.flowReplyTargetId === entry.id) return;
      state.flowReplyTargetId = entry.id;
      if (state.flowDetail) renderInterventions(state.flowDetail);
      $("flow-reply-input").focus();
    });
  }

  return chip;
}

// An option may be a plain string or `{label, value}`; resolve both shapes.
function optionLabel(opt) {
  if (opt && typeof opt === "object") {
    return String(opt.label != null ? opt.label : (opt.value != null ? opt.value : ""));
  }
  return String(opt);
}
function optionText(opt) {
  if (opt && typeof opt === "object") {
    return String(opt.value != null ? opt.value : (opt.label != null ? opt.label : ""));
  }
  return String(opt);
}

function safeStringify(value) {
  try { return JSON.stringify(value, null, 2); }
  catch (_) { return String(value); }
}

// Build the docked reply panel's prompt body as a default-collapsed,
// expand-on-demand container. Returns a wrapper holding a "展开/收起消息详情"
// trigger button plus a `.flow-reply-prompt` body (Markdown rendered, never
// truncated) that starts hidden. Clicking the trigger toggles the body and,
// on expand only, scrolls it into view (requestAnimationFrame-wrapped, matching
// the foldable behavior elsewhere in this view); collapsing does not scroll.
// The body stays mounted while hidden so the existing reply-box tests (and the
// CSS height cap that only applies in the expanded state) keep working. Kept as
// a small pure function so the DOM-stub tests can assemble it directly. Scope:
// only the #flow-view docked reply box (updateReplyBox) consumes this.
//
// opts — optional second argument (backward-compatible: callers that omit it
//   get the original default-collapsed, no-persist behaviour):
//   opts.expanded  — initial expanded state (default false);
//   opts.onToggle  — callback invoked on every user click with the new
//                    expanded boolean, so the caller can persist the choice.
//   opts.scrollTop — when the block is built in the expanded state, the
//                    scrollTop to restore on the body (via requestAnimationFrame,
//                    after layout), so an automatic re-render that rebuilds the
//                    block does not snap the user's reading position back to the
//                    top. Ignored when collapsed (a hidden body cannot scroll)
//                    and on the first user-click expand (that path keeps the
//                    original scrollIntoView behaviour).
//   opts.onScroll  — callback invoked with the body's current scrollTop whenever
//                    the user scrolls the expanded body, so the caller can record
//                    the latest reading position for the next rebuild.
//   opts.loadFullText — optional async function returning the untruncated body
//                    (or null). Invoked at most once, the first time the block is
//                    shown expanded (either mounted already-expanded from a
//                    persisted state, or on the user's expand click). Its resolved
//                    text replaces the mounted (clipped preview) body in place, so
//                    STATUS_UPDATE can carry only the DESC_CLIP preview while the
//                    full prompt is fetched on demand. Failures are swallowed
//                    (the preview stays); it is never called for a body that is
//                    never expanded.
function buildCollapsiblePrompt(promptText, opts) {
  const initialExpanded = opts && opts.expanded;
  const onToggle = opts && opts.onToggle;
  const onScroll = opts && opts.onScroll;
  const restoreScrollTop = opts && opts.scrollTop;
  const loadFullText = opts && opts.loadFullText;
  const wrap = el("div", "flow-reply-prompt-wrap");
  const collapsedLabel = tf("prompt.expandDetail", "▸ Expand message detail");
  const expandedLabel = tf("prompt.collapseDetail", "▾ Collapse message detail");
  const btn = el("button", "flow-reply-prompt-toggle",
    initialExpanded ? expandedLabel : collapsedLabel);
  btn.type = "button";
  const body = el("div", "flow-reply-prompt" + (initialExpanded ? "" : " hidden"));
  body.appendChild(renderMarkdown(promptText));
  // Fetch-and-swap the untruncated body the first time it is shown expanded.
  // Guarded so the network call fires once per mounted block and only when the
  // caller actually wants a full-text upgrade for a clipped preview.
  let fullRequested = false;
  const requestFull = () => {
    if (fullRequested || typeof loadFullText !== "function") return;
    fullRequested = true;
    Promise.resolve()
      .then(loadFullText)
      .then((full) => {
        if (typeof full === "string" && full && full !== promptText) {
          body.innerHTML = "";
          body.appendChild(renderMarkdown(full));
        }
      })
      .catch(() => {});
  };
  let expanded = !!initialExpanded;
  if (initialExpanded) {
    wrap.classList.add("expanded");
    requestFull();
  }
  // Record the live reading position on every scroll so the captured scrollTop
  // is decoupled from when (polling vs ws push) the next rebuild fires.
  if (onScroll) {
    body.addEventListener("scroll", () => { onScroll(body.scrollTop); });
  }
  // Rebuild restore: only meaningful when the block is mounted already-expanded
  // (a hidden body has no usable scroll position). Deferred to a rAF so the new
  // node has been laid out before we set scrollTop. This is independent of the
  // click-to-expand path below, which keeps using scrollIntoView.
  if (initialExpanded && restoreScrollTop) {
    requestAnimationFrame(() => { body.scrollTop = restoreScrollTop; });
  }
  btn.addEventListener("click", (e) => {
    e.preventDefault();
    expanded = !expanded;
    body.classList.toggle("hidden", !expanded);
    wrap.classList.toggle("expanded", expanded);
    btn.textContent = expanded ? expandedLabel : collapsedLabel;
    if (onToggle) onToggle(expanded);
    if (expanded) {
      requestFull();
      requestAnimationFrame(() => body.scrollIntoView({ block: "nearest" }));
    }
  });
  wrap.append(btn, body);
  return wrap;
}

// Render the ADJUDICATE approval-review block for a confirm target, or null.
//
// A CONFIRM gate that reviews an ADJUDICATE ruling is un-actionable without the
// ruling in view — the operator has no way to judge 批 vs 打回. The backend
// (build_adjudicate_review_context) injects the ruling's `adjudication_rationale`,
// the post-ruling `adjudicated_description`, and the pre-ruling `baseline` into
// `target.context`; here we surface the rationale panel plus a
// baseline→adjudicated_description before/after diff (reusing the shared
// unified-diff renderer `renderDiffPanel`).
//
// Every field is best-effort: an older call file (or a ruling that changed no
// description) may omit any subset, so each sub-block is gated independently and
// the whole thing degrades to "only what's available" rather than throwing. A
// non-adjudicate target (context missing, or step_to_review_type !== 'adjudicate')
// returns null so the block never renders for a plain call / plan confirm.
function renderAdjudicateReview(target) {
  const ctx = target && target.context;
  if (!ctx || String(ctx.step_to_review_type || "") !== "adjudicate") return null;

  const rationale = String(ctx.adjudication_rationale || "").trim();
  const baseline = String(ctx.baseline == null ? "" : ctx.baseline);
  const adjudicated = String(
    ctx.adjudicated_description == null ? "" : ctx.adjudicated_description,
  );

  // Nothing worth showing — no rationale and no description on either side.
  // Degrade to rendering nothing so a bare adjudicate confirm still shows only
  // its 批准/打回 buttons rather than an empty framed panel.
  if (!rationale && !adjudicated && !baseline) return null;

  const wrap = el("div", "flow-reply-adjudicate");
  wrap.appendChild(el("div", "flow-reply-adjudicate-title", tf("adjudicate.title", "Adjudication approval")));

  if (rationale) {
    const panel = el("div", "flow-reply-adjudicate-rationale");
    panel.appendChild(el("div", "flow-reply-adjudicate-label", tf("adjudicate.rationaleLabel", "Adjudication rationale")));
    panel.appendChild(
      el("div", "flow-reply-adjudicate-rationale-body", rationale),
    );
    wrap.appendChild(panel);
  }

  // Description before/after. Only diff when the ruling actually rewrote the
  // description (adjudicated non-empty) AND it differs from the baseline — an
  // empty/null rewrite means "description unchanged", so a full-delete diff would
  // misrepresent the ruling; render an explicit note instead. A missing baseline
  // still produces a sensible all-added diff via _toolUnifiedDiff.
  const diffWrap = el("div", "flow-reply-adjudicate-diff");
  diffWrap.appendChild(el("div", "flow-reply-adjudicate-label", tf("adjudicate.taskDescLabel", "Task description")));
  if (!adjudicated) {
    diffWrap.appendChild(
      el("p", "flow-reply-adjudicate-note", tf("adjudicate.noChange", "This adjudication did not modify the task description.")),
    );
  } else if (adjudicated === baseline) {
    diffWrap.appendChild(
      el("p", "flow-reply-adjudicate-note", tf("adjudicate.sameAsBaseline", "The task description matches the baseline (no change).")),
    );
  } else {
    const diff = _toolUnifiedDiff(baseline, adjudicated, "adjudicated_description");
    diffWrap.appendChild(
      renderDiffPanel({
        kind: "edit_diff",
        diff: diff,
        old_start_line: 1,
        new_start_line: 1,
        truncated: false,
      }),
    );
  }
  wrap.appendChild(diffWrap);

  // Boundary-clause coverage. A ruling may sweep sibling surfaces into ONE
  // boundary clause, so the clause governs more than the surface that triggered
  // it — and an over-broad sweep writes a wrong constraint into the contract.
  // This gate is the only place a human can catch that, hence每个 surface 与其
  // by-construction 论证 are shown side by side. Absent/empty/non-array degrades
  // to omitting the section: "swept in nothing" is the conservative (宁漏勿错)
  // outcome the prompt asks for, not an anomaly worth framing. Entries missing a
  // surface or a justification are dropped rather than shown half-blank —
  // the backend already rejects those, so seeing one here means malformed input.
  const surfaces = [];
  if (Array.isArray(ctx.covered_surfaces)) {
    for (const entry of ctx.covered_surfaces) {
      if (!entry || typeof entry !== "object") continue;
      const surface = String(entry.surface == null ? "" : entry.surface).trim();
      const justification = String(entry.justification == null ? "" : entry.justification).trim();
      if (!surface || !justification) continue;
      surfaces.push({ surface, justification });
    }
  }
  if (surfaces.length) {
    const sec = el("div", "flow-reply-adjudicate-surfaces");
    sec.appendChild(
      el(
        "div",
        "flow-reply-adjudicate-label",
        tf("adjudicate.coveredSurfacesLabel", "Surfaces covered by the boundary clause"),
      ),
    );
    for (const item of surfaces) {
      const row = el("div", "flow-reply-adjudicate-surface");
      row.appendChild(el("div", "flow-reply-adjudicate-surface-name", item.surface));
      const just = el("div", "flow-reply-adjudicate-surface-why");
      just.appendChild(
        el(
          "span",
          "flow-reply-adjudicate-surface-why-label",
          tf("adjudicate.coveredSurfaceJustification", "Why"),
        ),
      );
      just.appendChild(el("span", "flow-reply-adjudicate-surface-why-body", item.justification));
      row.appendChild(just);
      sec.appendChild(row);
    }
    wrap.appendChild(sec);
  }
  return wrap;
}

// Sync the docked reply box to the current intervention selection. When at
// least one chip exists, the textarea + submit are enabled and the reply-
// context panel above them materializes the selected chip's full content:
// kind header, prompt (Markdown, NOT truncated), optional context (`<pre>`,
// not capped) and any options buttons (one-click reply). When no chip exists,
// the textarea + submit are disabled and the panel shows a hint line.
function updateReplyBox(flow) {
  const entries = state.flowInterventions || [];
  const input = $("flow-reply-input");
  const submit = $("flow-reply-submit");
  const ctx = $("flow-reply-context");

  if (!entries.length) {
    // Textarea stays enabled so the user can always draft text — Send is the
    // gate. The placeholder reminds them they need a target to send.
    input.disabled = false;
    // No target means Send is unconditionally disabled regardless of the
    // pending-Send bookkeeping; an in-flight submission would have kept its
    // target around in `entries` while it was still pending.
    submit.disabled = true;
    // The shortened idle placeholder is mobile-only so the desktop wording
    // stays byte-for-byte unchanged (desktop zero-change hard constraint);
    // reuse the same `isMobilePortrait()` matchMedia('(max-width: 600px)')
    // gate the auto-grow textarea logic already relies on (it also guards
    // against a missing `window` in the DOM-stub test environment).
    input.placeholder = isActiveFlow(flow)
      ? (isMobilePortrait()
          ? tf("reply.empty.canDraftMobile", "You can draft a reply, or tap ✎ to interject…")
          : tf("reply.empty.canDraft", "No pending items — you can draft a reply first, or click ✎ to interject…"))
      : tf("flow.replyPlaceholder", "No pending items…");
    ctx.className = "flow-reply-context";
    ctx.innerHTML = "";
    ctx.appendChild(el("p", "flow-reply-empty",
      isActiveFlow(flow)
        ? tf("reply.empty.active", "There are no pending interaction items right now — nothing to reply to.")
        : tf("reply.empty.ended", "This flow has ended — no further interaction is possible.")));
    return;
  }

  let target = entries.find((e) => e.id === state.flowReplyTargetId);
  if (!target) {
    target = entries[0];
    state.flowReplyTargetId = target.id;
  }
  const meta = KIND_META[target.kind] || KIND_META.call;

  input.disabled = false;
  // Send is gated by both target presence and the ws-settle bookkeeping —
  // a Send already in flight stays locked here even though the chip remains
  // selected. `settlePendingSend()` (or the 8s fallback) clears the gate.
  submit.disabled = !!state.pendingSendSettleKey;
  input.placeholder =
    target.kind === "interjection"
      ? tf("reply.placeholder.interject", "Enter the instruction to interject into the running flow…")
      : target.kind === "confirm"
        // Free-text fallback for the CONFIRM gate. List the words that are
        // recognized as an outright approval/rejection so the operator knows
        // what maps to which; anything else prompts a "will be treated as a
        // revision request" second-guess before it is sent.
        ? tf("reply.placeholder.confirm", "approve/pass/approve passes directly, reject/decline/reject rejects directly; any other text is treated as a revision request…")
        : tf("reply.placeholder.reply", "Enter your reply…");

  ctx.className = "flow-reply-context active kind-" + target.kind;
  ctx.innerHTML = "";

  // Header row: neutral "回复中 · <kind label>" wording. The internal call id
  // is intentionally absent from any visible text — it remains on the head's
  // `data-call-id` and `title` tooltip so operators can still cross-reference
  // calls when debugging, but is never surfaced to the user as jargon.
  const head = el("div", "flow-reply-head");
  head.append(
    el("span", "flow-reply-to", tf("reply.replyingTo", "Replying to")),
    el("span", "flow-reply-sep", "·"),
    el("span", "flow-reply-kind kind-" + target.kind, tf(meta.labelKey, meta.label)),
  );
  if (target.callId) {
    head.dataset.callId = target.callId;
    head.title = target.callId;
  }
  ctx.appendChild(head);

  // Prompt body — default collapsed behind an expand/collapse trigger. The
  // prompt is redundant with the conversation chat-stream above (the refined
  // task description, the agent turn text, the step_failed card, etc. are
  // already tiled there for every kind), so the docked reply panel keeps only
  // the bounded-height functional controls (header / options / textarea /
  // Send) on screen by default and surfaces the full prompt on demand. When
  // expanded the body is height-capped + scrollable (see `.flow-reply-prompt`
  // in style.css), so even a very long prompt — e.g. a `discovery_confirm`
  // whose prompt embeds an entire refined task description — can never push the
  // textarea / options / Send out of view.
  const cachedFull =
    target.callId && state.flowReplyPromptFull
      ? state.flowReplyPromptFull[target.callId]
      : null;
  // Once fetched, reuse the cached untruncated body so the 3s poll / ws-push
  // rebuild does not fall back to the DESC_CLIP preview (and never re-fetches).
  const promptText =
    typeof cachedFull === "string" && cachedFull ? cachedFull : target.prompt;
  // A real (non-synthetic) call's prompt whose preview is DESC_CLIP-clipped can
  // be upgraded on demand; synthetic / local interjection prompts are the user's
  // own verbatim draft and are never clipped, so they need no fetch.
  const needsFull =
    !!target.callId &&
    !target.synthetic &&
    cachedFull == null &&
    descriptionLikelyTruncated(target.prompt);
  if (promptText) {
    ctx.appendChild(buildCollapsiblePrompt(promptText, {
      expanded: !!state.flowReplyPromptExpanded[target.id],
      // Restore the last reading position so the 3s detail poll / ws push
      // rebuild of this context block (ctx.innerHTML="") does not snap an
      // expanded long 「消息详情」 body back to the top while the user reads.
      scrollTop: state.flowReplyPromptScroll[target.id],
      loadFullText: needsFull
        ? () =>
            fetchCallFullPrompt(target.callId, {
              machineId: target.machineId,
              projectRoot: target.projectRoot,
            }).then((full) => {
              // Cache so subsequent rebuilds mount the full body directly.
              if (typeof full === "string" && full) {
                state.flowReplyPromptFull[target.callId] = full;
              }
              return full;
            })
        : null,
      onToggle(v) {
        state.flowReplyPromptExpanded[target.id] = v;
        // Collapsing discards the saved position so a later re-expand starts
        // from the top; harmless for the expand case (overwritten on scroll).
        if (!v) state.flowReplyPromptScroll[target.id] = 0;
      },
      onScroll(top) { state.flowReplyPromptScroll[target.id] = top; },
    }));
  } else {
    ctx.appendChild(el("p", "flow-reply-hint", tf(meta.hintKey, meta.hint)));
  }

  // Context block is intentionally NOT rendered for any kind. Every pending
  // intervention's prompt already carries the human-meaningful text the user
  // needs to act (discovery_confirm embeds the refined task description; the
  // other kinds — call / interjection / cli_confirm / retry_decision — carry
  // their full prompt), and the backend mirrors the same payload into the
  // prompt, so a separate context block only duplicated content below the
  // prompt. Suppressing it for all kinds keeps the reply panel free of
  // redundant context, matching the prior discovery_confirm-only behavior.

  // ADJUDICATE approval gate: an adjudicate confirm cannot be judged without the
  // ruling in view, so surface the rationale + baseline→adjudicated_description
  // diff above the decision buttons. Guarded internally on
  // context.step_to_review_type, so this is a no-op (null) for any other target —
  // including a plain call / plan confirm — and degrades gracefully when the
  // context omits some fields.
  const adjudicateReview = renderAdjudicateReview(target);
  if (adjudicateReview) ctx.appendChild(adjudicateReview);

  // CONFIRM approval gate: render an explicit 批准/打回 pair plus an optional
  // note textarea so a decision is one click and always lands as a structured
  // {approved, feedback} payload — never a free-text guess. Legacy (kind-less)
  // confirm calls normalize to "call" and never reach this branch, so they keep
  // the plain free-text box (the required降级). The docked #flow-reply-input
  // stays available underneath as the word-list-mirrored free-text fallback.
  if (target.kind === "confirm") {
    const decide = el("div", "flow-reply-confirm");
    const note = el("textarea", "flow-reply-confirm-note");
    note.placeholder = tf("reply.confirmNotePlaceholder", "Note (optional; explain what needs changing when rejecting)…");
    const btnRow = el("div", "flow-reply-confirm-actions");
    const approveBtn = el(
      "button",
      "flow-reply-option flow-reply-option-primary flow-reply-confirm-approve",
      tf("reply.approve", "Approve"),
    );
    approveBtn.type = "button";
    approveBtn.addEventListener("click", (e) => {
      e.preventDefault();
      // A blank note is normalized to null inside sendConfirmDecision.
      sendConfirmDecision(state.selectedFlowId, target, true, note.value);
    });
    const rejectBtn = el(
      "button",
      "flow-reply-option flow-reply-confirm-reject",
      tf("reply.reject", "Reject"),
    );
    rejectBtn.type = "button";
    rejectBtn.addEventListener("click", (e) => {
      e.preventDefault();
      sendConfirmDecision(state.selectedFlowId, target, false, note.value);
    });
    // Mirror the docked Send button: while a decision is in flight the gate key
    // is set, so render the pair disabled to match sendConfirmDecision's
    // re-entry guard — the buttons look locked instead of clickable-but-inert.
    if (state.pendingSendSettleKey) {
      approveBtn.disabled = true;
      rejectBtn.disabled = true;
    }
    btnRow.append(approveBtn, rejectBtn);
    decide.append(note, btnRow);
    ctx.appendChild(decide);
  }

  // Optional options — render as one-click reply buttons. Clicking sends the
  // option text directly via sendReply, same path the inline option click on
  // the previous card layout used.
  //
  // Discovery confirmation: guarantee a GUI confirm button (sends the literal
  // "1" the programmatic gate expects) even if a backend call file omitted the
  // option, so the button + the "输入 1 确认" textual prompt always coexist.
  let options = target.options || [];
  if (target.kind === "discovery_confirm" && !options.length) {
    options = [{ label: tf("reply.confirmContinue", "Confirm and continue (enter 1)"), value: "1" }];
  }
  if (options.length) {
    const opts = el("div", "flow-reply-options");
    for (const opt of options) {
      const optText = optionText(opt);
      const isConfirm = target.kind === "discovery_confirm";
      const btn = el(
        "button",
        "flow-reply-option" + (isConfirm ? " flow-reply-option-primary" : ""),
        optionLabel(opt),
      );
      btn.type = "button";
      btn.addEventListener("click", (e) => {
        e.preventDefault();
        sendReply(state.selectedFlowId, target, optText);
      });
      opts.appendChild(btn);
    }
    ctx.appendChild(opts);
  }
}

function resetReplyBox() {
  const input = $("flow-reply-input");
  input.value = "";
  // The strip only ever mirrors paths present in this text, so blanking the
  // text must retire the rows with it — otherwise opening another flow shows
  // rows for paths that live in the previous flow's project, whose × button
  // then has nothing to remove, and leaks their preview object URLs.
  clearAttachments("flow-attachments");
  // Reset the auto-grow height back to a single line on a fresh flow / chip
  // switch (mobile portrait only; a no-op on desktop).
  autoGrowReplyTextarea();
  // Textarea stays enabled so the user can begin drafting immediately;
  // Send remains disabled until a target chip is available.
  input.disabled = false;
  $("flow-reply-submit").disabled = true;
  const btn = $("flow-interject-btn");
  if (btn) {
    btn.classList.add("hidden");
    btn.classList.remove("active");
  }
  const ctx = $("flow-reply-context");
  ctx.className = "flow-reply-context";
  ctx.innerHTML = "";
  ctx.appendChild(el("p", "flow-reply-empty",
    tf("flow.replyContext", "No pending interaction right now.")));
}

function truncate(text, max) {
  const s = String(text || "").replace(/\s+/g, " ").trim();
  return s.length > max ? s.slice(0, max - 1) + "…" : s;
}

// ---------------------------------------------------------------------------
// CONFIRM free-text interpretation (client-side mirror of the backend)
// ---------------------------------------------------------------------------
//
// A CONFIRM gate's primary UI is the 批准/打回 buttons, but the retained
// free-text box must never silently mis-classify an operator's answer. These
// token sets MIRROR run.py:_interpret_confirm_answer (see the NOTE there) so
// the frontend can locally decide, BEFORE sending, whether a typed reply is an
// approval, a rejection, or an unrecognized note that should trigger a "this
// will be treated as a revision request — sure?" second-guess. Keep the two
// lists in sync when editing either — a divergence would let "同意"/"批准" fall
// into the confirm-dialog branch here even though the backend would approve it.
const CONFIRM_APPROVE_TOKENS = new Set([
  "approve", "approved", "yes", "y", "ok", "okay", "lgtm",
  "accept", "accepted", "continue", "proceed", "pass", "skip",
  "同意", "通过", "批准", "确认", "允许", "接受",
]);
const CONFIRM_REJECT_TOKENS = new Set([
  "no", "n", "reject", "rejected", "deny", "denied",
  "request changes", "changes", "revise", "revision",
  "驳回", "拒绝", "打回", "否决", "不通过", "重做", "重拟",
]);

// Classify a free-text CONFIRM answer as "approve" | "reject" | "unknown".
// Matches whole-string OR first-word against the token sets — the same
// semantics as the backend — so "approve, looks good" still reads as approval
// while "request changes" only matches as a whole string. Anything unmatched
// is "unknown": the caller second-guesses it rather than silently rejecting.
function interpretConfirmAnswer(text) {
  const stripped = String(text == null ? "" : text).trim();
  if (!stripped) return "unknown";
  const lowered = stripped.toLowerCase();
  const firstWord = lowered.split(/\s+/)[0] || lowered;
  if (CONFIRM_APPROVE_TOKENS.has(lowered) || CONFIRM_APPROVE_TOKENS.has(firstWord)) {
    return "approve";
  }
  if (CONFIRM_REJECT_TOKENS.has(lowered) || CONFIRM_REJECT_TOKENS.has(firstWord)) {
    return "reject";
  }
  return "unknown";
}

// Second-guess an unrecognized CONFIRM free-text answer before it is sent as a
// revision request. window.confirm is absent in the DOM-stub test env; default
// to proceeding there (the pure logic is still asserted by stubbing
// globalThis.confirm), while the browser shows the real dialog.
function confirmRevisionIntent() {
  const c =
    typeof globalThis !== "undefined" && typeof globalThis.confirm === "function"
      ? globalThis.confirm
      : null;
  if (!c) return true;
  return !!c(tf("reply.revisionConfirm", "Your reply will be treated as a revision request. Continue?"));
}

// ---------------------------------------------------------------------------
// Reply submission
// ---------------------------------------------------------------------------
//
// One docked box, two destinations. A reply to a call / retry_decision /
// cli_confirm entry POSTs to `/respond` keyed by `call_id`; an interjection
// POSTs to `/interject`. On success the reply is spliced into the conversation
// in place so the operator sees it without waiting for a refresh.

function submitReply(event) {
  event.preventDefault();
  const entries = state.flowInterventions || [];
  const target = entries.find((e) => e.id === state.flowReplyTargetId);
  if (!state.selectedFlowId || !target) {
    showToast("error", tf("toast.noInteractionSelected", "No interaction is selected to respond to."));
    return;
  }
  const input = $("flow-reply-input");
  const text = input.value.trim();
  if (!text) {
    showToast("error", tf("toast.responseEmpty", "Response must not be empty."));
    return;
  }
  // An upload still in flight makes this text unsendable: it carries a
  // placeholder token instead of a path, and sending clears the box the answer
  // needs to write into. Refusing costs the operator the second or two the
  // upload still needs; sending costs them the file.
  const pendingUploads = pendingUploadRefusal("flow-attachments");
  if (pendingUploads) {
    showToast("error", pendingUploads);
    return;
  }
  // CONFIRM gates route the free-text box through the structured decision
  // payload so a typed "同意"/"批准" is a real approval and a "1"/unknown note
  // is only ever sent as a revision AFTER an explicit second-guess — never
  // silently. Legacy (kind-less) confirm chips normalize to "call" and keep the
  // plain free-text path below.
  if (target.kind === "confirm") {
    const verdict = interpretConfirmAnswer(text);
    if (verdict === "approve") {
      sendConfirmDecision(state.selectedFlowId, target, true, null);
    } else if (verdict === "reject") {
      sendConfirmDecision(state.selectedFlowId, target, false, text);
    } else if (confirmRevisionIntent()) {
      sendConfirmDecision(state.selectedFlowId, target, false, text);
    }
    // A cancelled second-guess sends nothing — the operator can re-edit.
    return;
  }
  sendReply(state.selectedFlowId, target, text);
}

// Arm the pending-Send settle gate shared by `sendReply` and
// `sendConfirmDecision`: snapshot the call_id baseline, set the settle key, and
// start the 8s fallback that force-unlocks Send if the daemon's ws delta never
// arrives. Extracted so the structured confirm-decision send reuses the EXACT
// same settlement bookkeeping as a free-text reply (no divergence in how a
// pending Send re-enables), while keeping `submit.disabled = true` at the two
// call sites where each also touches its own input state.
function armPendingSend(target) {
  const flow = state.flowDetail;
  state.pendingSendBaselineCallIds = new Set(
    (flow && flow.pending_calls ? flow.pending_calls : [])
      .map((c) => c && c.call_id)
      .filter(Boolean),
  );
  state.pendingSendSettleKey =
    target.kind === "interjection" && target.synthetic
      ? "synthetic-interject"
      : String(target.callId || target.id || "send");

  if (state.pendingSendTimer) clearTimeout(state.pendingSendTimer);
  state.pendingSendTimer = setTimeout(() => {
    // ws delayed past 8s — force-unlock and tell the user the next press is
    // possible but the daemon may already have queued the first one.
    if (state.pendingSendSettleKey) {
      showToast("info", tf("toast.wsDelayed", "ws delayed, retry possible"));
    }
    // Clear synthetic-pending visual state too — without ws confirmation
    // we cannot tell if the real chip will ever arrive, so let the user
    // start a fresh interject if they want.
    if (state.flowSyntheticInterjectPending) {
      state.flowSyntheticInterjectPending = false;
      state.flowInterjectRequested = false;
      if (state.flowReplyTargetId === "interjection:new") {
        state.flowReplyTargetId = null;
      }
    }
    settlePendingSend();
  }, 8000);
}

// Send a structured CONFIRM approval decision: POST /respond with
// {response: {approved, feedback}, call_id}. The daemon writes the inner dict
// through untouched (see run.py "Unwrap the daemon envelope"), so the reviewed
// step gets a real approve/reject rather than a free-text guess. Mirrors
// sendReply's success/failure/settle handling so the pending-Send gate and the
// optimistic echo behave identically for a button click and a typed reply.
async function sendConfirmDecision(flowId, target, approved, feedback) {
  if (!flowId || !target) return;
  // Re-entry guard: the 批准/打回 buttons live in the context panel, separate
  // from #flow-reply-submit, so disabling Send does NOT stop a second click on
  // them. Without this, a double-click — or 批准 then 打回 before the daemon
  // consumes the call — would POST several structured responses for the SAME
  // call_id; the daemon overwrites the one <call_id>.response.json each time, so
  // the persisted decision becomes whichever request lands last rather than the
  // operator's first explicit choice. Ignore any send while one is already in
  // flight (pendingSendSettleKey stays set until the ws delta or 8s fallback
  // settles it), so exactly one structured decision lands per click.
  if (state.pendingSendSettleKey) return;
  const submit = $("flow-reply-submit");
  submit.disabled = true;
  armPendingSend(target);

  // Normalize the note to null when blank so an approval carries no spurious
  // feedback (matching _interpret_confirm_answer's approve → (True, None)).
  const note =
    feedback == null || String(feedback).trim() === ""
      ? null
      : String(feedback).trim();
  const decision = { approved: !!approved, feedback: note };

  try {
    const resp = await authedFetch(
      `/api/flows/${encodeURIComponent(flowId)}/respond`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ response: decision, call_id: target.callId }),
      },
    );
    if (resp.ok) {
      // Success decided solely by resp.ok — clear the free-text box (the note
      // textarea lives in the rebuilt context panel and is discarded on the
      // finally re-render) and toast before the best-effort echo, same as
      // sendReply (issue #193 boundary).
      if (state.selectedFlowId === flowId) {
        $("flow-reply-input").value = "";
        autoGrowReplyTextarea();
        clearAttachments("flow-attachments");
      }
      showToast("success", approved ? tf("toast.approved", "Approved.") : tf("toast.rejected", "Rejected."));
      // Optimistic echo as a human-readable user bubble so the decision shows
      // immediately without waiting for the next history_data push.
      const echo = approved
        ? note
          ? tf("reply.echo.approveNote", `Approved: ${note}`, { note })
          : tf("reply.approve", "Approve")
        : note
          ? tf("reply.echo.rejectNote", `Rejected: ${note}`, { note })
          : tf("reply.reject", "Reject");
      appendLocalReply(flowId, target, echo);
    } else {
      const detail = await resp.json().catch(() => ({}));
      const message = detail.detail || tf("error.serverReturned", `Server returned ${resp.status}.`, { status: resp.status });
      showToast("error", tf("toast.couldNotSend", `Could not send: ${message}`, { message }));
      settlePendingSend();
    }
  } catch (_) {
    showToast("error", tf("toast.sendNetworkError", "Could not send — network error reaching the server."));
    settlePendingSend();
  } finally {
    if (state.selectedFlowId === flowId && state.flowDetail) {
      renderInterventions(state.flowDetail);
    }
  }
}

async function sendReply(flowId, target, text) {
  if (!flowId || !target || !text) return;
  const submit = $("flow-reply-submit");
  const input = $("flow-reply-input");
  // Only the Send button locks — the textarea stays enabled at all times so
  // the user can keep editing / drafting a follow-up while the request is in
  // flight. Locking the textarea would block the docked-chat UX the spec
  // calls for. Repeated Send clicks are debounced by the disabled button.
  submit.disabled = true;

  // The Send button re-enables only after `maybeSettleViaPendingCallsDiff` sees
  // a delta on a subsequent ws-driven refresh (or an `interjection_event`
  // matches), proving the daemon has observed the submission; the 8s fallback
  // catches a stuck ws so the UI does not stall forever.
  armPendingSend(target);

  try {
    let resp;
    if (target.kind === "interjection") {
      resp = await authedFetch(`/api/flows/${encodeURIComponent(flowId)}/interject`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: text }),
      });
    } else {
      resp = await authedFetch(`/api/flows/${encodeURIComponent(flowId)}/respond`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ response: text, call_id: target.callId }),
      });
    }
    if (resp.ok) {
      // Success is decided SOLELY by `resp.ok`. Everything that must happen on
      // a successful send — clearing the input, the synthetic-chip visual
      // state, and the success toast — runs here, BEFORE the optimistic echo
      // is spliced in, so a fault in the (best-effort) conversation rendering
      // can never demote a delivered reply back into the network-error catch
      // below. This is the root-cause fix for the discovery-confirm regression
      // (issue #193, same class as #191): a render exception inside the old
      // wide try-block surfaced as "Could not send — network error" even
      // though the daemon had already received and acted on the "1".
      if (state.selectedFlowId === flowId) {
        $("flow-reply-input").value = "";
        // Cleared content collapses the auto-grow textarea back to one line.
        autoGrowReplyTextarea();
        // The paths travelled with the text that was just sent; the rows now
        // mirror nothing. UI only — the files stay on the project machine,
        // where the flow is about to read them.
        clearAttachments("flow-attachments");
      }
      // Keep the synthetic interject chip visible in `pending` visual state
      // until the real interjection chip materializes (via ws push) — that
      // gives the user immediate feedback that the submission is in flight
      // without re-pinning the now-empty textarea to a fresh synthetic chip.
      // The chip is replaced when `pending_calls` gains the real entry or
      // when an `interjection_event` consumed event arrives; the 8s fallback
      // above releases it if neither lands.
      if (target.kind === "interjection" && target.synthetic) {
        state.flowSyntheticInterjectPending = true;
      }
      showToast("success", target.kind === "interjection"
        ? tf("toast.interjectionSent", "Interjection sent.")
        : tf("toast.responseSent", "Response sent."));
      // Optimistic echo. `appendLocalReply` is best-effort: it writes the echo
      // record into `state.flowConversationRecords` first, then renders behind
      // its own try/catch, so it never throws back into this success path.
      appendLocalReply(flowId, target, text);
    } else {
      const detail = await resp.json().catch(() => ({}));
      const message = detail.detail || tf("error.serverReturned", `Server returned ${resp.status}.`, { status: resp.status });
      showToast("error", tf("toast.couldNotSend", `Could not send: ${message}`, { message }));
      // Error path — settle immediately so the user can retry without
      // waiting on a ws update that will never come for this failed POST.
      settlePendingSend();
    }
  } catch (_) {
    showToast("error", tf("toast.sendNetworkError", "Could not send — network error reaching the server."));
    settlePendingSend();
  } finally {
    // Re-render so the chip-bar reflects the freshly-set
    // `flowSyntheticInterjectPending` pending visual state; the Send button
    // stays locked until `settlePendingSend()` clears `pendingSendSettleKey`.
    if (state.selectedFlowId === flowId && state.flowDetail) {
      renderInterventions(state.flowDetail);
    }
  }
}

// Splice a just-sent reply into the conversation as its own record so it is
// visible immediately, without waiting for the next `history_data` push.
function appendLocalReply(flowId, target, text) {
  if (state.selectedFlowId !== flowId) return;
  // Snapshot this reply's *rank* among all copies of the same text — i.e. how
  // many prior copies already exist, counting BOTH authoritative (non-echo)
  // user records AND still-pending optimistic echoes of the same text. This
  // gives every echo a stable, distinct rank (0, 1, 2, …) even when several
  // identical replies ("yes" / "continue", repeated interjections) are sent in
  // rapid succession before any daemon record returns. `reconcileLocalEchoes`
  // removes an echo only once the authoritative count for the text grows past
  // its rank — i.e. once THIS reply's own daemon record lands — so each pending
  // echo stays visible continuously until its own copy arrives, and a single
  // daemon arrival never sweeps away more than one pending echo.
  // Rank computation walks every existing record through normalizeRecord /
  // comparableUserText. A pathological record could make those throw; that must
  // NOT bubble back into sendReply's success path (issue #193) nor prevent the
  // echo from being recorded. Guard it: on failure fall back to rank 0 (the echo
  // still lands and `reconcileLocalEchoes` will pair it with its daemon copy).
  let priorCopies = 0;
  try {
    const echoText = comparableUserText(text);
    for (const r of state.flowConversationRecords) {
      if (r && r.__localEcho) {
        const t = comparableUserText(
          r.__localEchoText != null ? r.__localEchoText : normalizeRecord(r).content);
        if (t === echoText) priorCopies += 1;
        continue;
      }
      const n = normalizeRecord(r);
      if (n.role === "user" && comparableUserText(n.content) === echoText) priorCopies += 1;
    }
  } catch (err) {
    console.error("appendLocalReply: best-effort rank computation failed", err);
    priorCopies = 0;
  }
  const record = {
    step_id: "interaction",
    // Mark this as the optimistic echo and keep the original literal text so
    // `reconcileLocalEchoes` can drop it once the daemon pushes the authoritative
    // copy of the same reply — otherwise the echo and the daemon record (which
    // carry different step_id / timestamp, so different recordKey) both render
    // and the desktop user reply shows twice.
    __localEcho: true,
    __localEchoText: text,
    // The reply's rank among all copies of this text (prior authoritative
    // records + pending echoes). Field name kept for backward compatibility.
    __localEchoPriorAuth: priorCopies,
    message: {
      role: "user",
      content: text,
      timestamp: Date.now(),
      // A machine-safe token (see replyStepType) — NOT the display label. The
      // localized header text is resolved from it at render time.
      step_type: replyStepType(target && target.kind),
    },
  };
  // State is the source of truth: append the echo to the conversation records
  // FIRST, before any DOM rendering, so a "1" confirm (or any reply) is in the
  // message list even if a render helper throws. If rendering fails the next
  // ws `history_data` push re-renders and the echo (or its reconciled
  // authoritative copy) is shown anyway.
  state.flowConversationRecords = state.flowConversationRecords.concat([record]);
  // Rendering is best-effort: a fault here must NOT bubble back to sendReply's
  // success path and masquerade as a network error (issue #193). Swallow and
  // log so the defect stays observable without breaking the delivered reply.
  try {
    renderConversation($("flow-conversation"), state.flowConversationRecords, true);
    refreshFlowStickyHeader();
    updateFlowUsageBadge(state.flowConversationRecords);
    scrollFlowConversationToBottom();
  } catch (err) {
    console.error("appendLocalReply: best-effort render failed", err);
  }
}

// ---------------------------------------------------------------------------
// History view
// ---------------------------------------------------------------------------
//
// The server is a pure in-memory relay: `/api/history` returns the aggregated
// session index daemons push, and `/api/history/{flow_id}` returns a flow's
// step-by-step conversation records (pulled on demand for historical flows).
// Active flows additionally stream incremental `history_data` deltas over
// `/ws/ui`, which we append live and scroll into view.

function isHistoryOpen() {
  return !$("history-view").classList.contains("hidden");
}

function openHistory() {
  $("history-view").classList.remove("hidden");
  // Start on the session list panel (inert on desktop).
  applyHistoryPanelAction("reset");
  renderHistoryList();
  fetchHistoryIndex();
}

function closeHistory() {
  state.historyEpoch += 1;
  $("history-view").classList.add("hidden");
  // Reset the narrow-screen panel back to the session list (inert on desktop).
  applyHistoryPanelAction("reset");
  state.selectedHistoryId = null;
  state.historyRecords = [];
  state.historyProgress = null;
  state.historySignature = null;
  state.historySelectedProjectRoot = null;
  // Clear the per-session header so a stale flow_id / usage total can't bleed
  // into the next opened session.
  $("history-detail-flow-id").textContent = "";
  updateHistoryUsageBadge([]);
}

async function fetchHistoryIndex() {
  // Enter the loading state so renderHistoryList can show "正在刷新历史…"
  // instead of a blank page while the server forces a fresh index refresh
  // (broadcast_index_refresh → daemon force_index). The underlying live-pull
  // mechanism is unchanged; this only surfaces the in-flight feedback.
  state.historyIndexLoading = true;
  renderHistoryList();
  try {
    const resp = await authedFetch("/api/history");
    if (!resp.ok) return;
    const data = await resp.json();
    if (Array.isArray(data.sessions)) {
      state.historySessions = data.sessions;
      // Only a *non-empty* result confirms real history. An empty array here is
      // usually the 2s HISTORY_INDEX_REFRESH_TIMEOUT firing before any daemon
      // pushed its index, so it MUST NOT flip us into the confirmed state — that
      // would wrongly fall back to the empty-state during the ~1min a daemon
      // takes to push its history_index (the authoritative confirmation arrives
      // via applyHistoryIndex below).
      if (data.sessions.length) state.historyIndexConfirmed = true;
    }
  } catch (_) {
    /* transient — a WS history_index push will refresh it */
  } finally {
    // Clear the loading state on every exit path (success / non-ok / error)
    // and re-render the final list.
    state.historyIndexLoading = false;
    renderHistoryList();
  }
}

// Push handler: the daemon's full session index, rebroadcast by the server.
// The full push is the authoritative BASELINE the differential deltas below
// merge onto (it establishes the complete view on connect / reconnect / a
// HISTORY_INDEX_REQUEST); a delta only ever mutates the rows that changed.
function applyHistoryIndex(sessions) {
  state.historySessions = sessions;
  // Any history_index push — even an empty list — is an authoritative report
  // that the daemon has accounted for its history, so an empty result can now
  // settle into the confirmed-empty state instead of "still connecting".
  state.historyIndexConfirmed = true;
  if (isHistoryOpen()) renderHistoryList();
}

// Push handler: a DIFFERENTIAL session-index update (G5). Rather than re-fanning
// the whole aggregated index whenever a single active flow's `updated_at` ticks
// (which scales with the *total* flow count), the server relays only the changed
// SessionMeta rows. Merge them into the local aggregated index by `flow_id`:
// each upsert replaces-or-inserts its row, each removed id drops its row. The
// full `applyHistoryIndex` push remains the baseline; a delta arriving before
// any baseline simply seeds a partial view that the next full `GET /api/history`
// / `history_index` push corrects — matching the server's backward-compat note.
function applyHistoryIndexDelta(upserts, removed) {
  // Merge into a flow_id → row map seeded from the current index so an upsert
  // for an existing flow replaces it in place and a new flow is appended, then
  // re-materialize the array. A Map preserves insertion order, so the sort below
  // is what actually re-establishes the server's ordering.
  const byId = new Map();
  for (const s of state.historySessions || []) {
    if (s && s.flow_id != null) byId.set(String(s.flow_id), s);
  }
  for (const up of upserts || []) {
    if (up && typeof up === "object" && up.flow_id != null) {
      byId.set(String(up.flow_id), up);
    }
  }
  for (const rid of removed || []) {
    byId.delete(String(rid));
  }
  // Re-sort by updated_at DESC (entries lacking it last) so an updated active
  // flow rises to the top exactly as a fresh full index would order it — the
  // server sorts the full index the same way (reverse string sort on updated_at)
  // and the per-project bucketing keeps within-bucket input order.
  state.historySessions = Array.from(byId.values()).sort((a, b) => {
    const ta = String((a && a.updated_at) || "");
    const tb = String((b && b.updated_at) || "");
    if (ta < tb) return 1;
    if (ta > tb) return -1;
    return 0;
  });
  // A delta implies the daemon reported real history, so — like a full push —
  // it settles the confirmed state (never leaves the view stuck "connecting").
  state.historyIndexConfirmed = true;
  if (isHistoryOpen()) renderHistoryList();
}

// Push handler: incremental (or full) records for one flow. The same flow may
// be open in both the history view and the running-flow view; each keeps its
// own record array so they update independently without double-appending.
// HOP-5 DEBUG (frontend applyHistoryData): opt-in console tracing of the last
// hop — whether a broadcast history_data frame reaches the open flow view and
// grows flowConversationAppendSeq (the counter the progression-grace fallback
// watches). Off by default so the normal console stays clean; enable in a live
// browser session with `localStorage.SE3_WS_DEBUG = "1"` (or set
// `window.SE3_WS_DEBUG = true`) to confirm whether the discovery→analyze
// increments arrive at the last hop or die upstream.
function wsHistDebug() {
  try {
    if (typeof window !== "undefined" && window.SE3_WS_DEBUG) return true;
    if (typeof localStorage !== "undefined" && localStorage.getItem("SE3_WS_DEBUG")) {
      return true;
    }
  } catch (_) { /* localStorage may throw in restricted contexts */ }
  return false;
}

// WHY (#287): the same empty-full defence mergeHistoryResponse applies to the
// REST path, for the WS `mode:"full"` push — a zero-record full frame must not
// wipe a view that already holds records. It replaces the whole bundle, so
// adopting an empty one blanks the chat pane (the worktree self-heal's
// pseudo-empty snapshot). A view holding nothing yet still takes the empty frame
// (a genuinely empty flow must render its empty state); only a regression to
// zero is refused.
//
// WHY the browser stops at zero and does NOT enforce the full add-only floor
// ("never fewer records than we hold"): a full frame that is legitimately
// SHORTER is indistinguishable here from a truncated one. A replacement cache
// generation — the bundle re-established by a different machine, or re-pulled
// after the old one was dropped — is authoritative even when it carries fewer
// records, and refusing it would pin the view to a stale generation forever. The
// count floor therefore lives where the machine identity and the cached record
// count ARE known: `ServerState.apply_history_frame` (same-machine fulls may
// only grow) and the history endpoint's wire floor. A same-machine full the
// cache rejects for shrinking is also never relayed over the WS at all (the
// fan-out suppresses it), so no truncated-but-nonempty full reaches this guard
// from that path. This guard is only the last-ditch backstop against the one
// frame that can never be legitimate: an empty one.
function rejectsEmptyFullPush(records, held) {
  return records.length === 0 && Array.isArray(held) && held.length > 0;
}

// Apply a pushed history frame, then self-check what the view now holds against
// the frame's authoritative cursor. WHY the check runs even when the frame was
// discarded as an all-duplicate or empty-full re-delivery (hence the `finally`):
// the hole this heals is one the frame itself does NOT carry — a console that
// missed the head only ever sees tail appends, and every one of them is its cue
// to notice the head is absent and pull it by number.
function applyHistoryData(msg) {
  try {
    applyHistoryFrameRecords(msg);
  } finally {
    applyHistoryCursor(msg);
  }
}

function applyHistoryFrameRecords(msg) {
  const records = Array.isArray(msg.records) ? msg.records : [];
  const append = msg.mode === "append";
  if (wsHistDebug()) {
    // eslint-disable-next-line no-console
    console.debug(
      "hist-diag applyHistoryData flow=%s mode=%s records=%d selectedFlow=%s appendSeq=%d",
      msg.flow_id, msg.mode, records.length, state.selectedFlowId,
      state.flowConversationAppendSeq);
  }

  // A full frame may carry the backend usage payload (`usage`) — the same
  // schema the REST bundle delivers. Adopt it for whichever view is open so a
  // WS-only update path never leaves the badge / region behind the backend.
  if (msg.usage && typeof msg.usage === "object") {
    if (isHistoryOpen() && state.selectedHistoryId === msg.flow_id) {
      state.historyUsage = msg.usage;
    }
    if (state.selectedFlowId === msg.flow_id) {
      state.flowConversationUsage = msg.usage;
    }
  }

  // -- history view consumer --
  if (isHistoryOpen() && state.selectedHistoryId === msg.flow_id) {
    // Capture the reader's position BEFORE re-rendering: an append grows
    // scrollHeight, so "near bottom" must be measured against the old layout.
    const stick = !append || isNearBottom(historyScrollContainer());
    if (append) {
      const rec = reconcileAppendRecords(state.historyRecords, records);
      if (rec.changed) {
        state.historyRecords = rec.records;
        // A mid-list in-place update (a retry rewrote an already-rendered line)
        // cannot be repainted by the tail-only incremental render — force a full
        // rebuild in that case; a pure tail append keeps the cheap append render.
        renderHistoryRecords(msg.flow_id, state.historyRecords, append && !rec.updatedInPlace);
        refreshHistoryStickyHeader();
        refreshHistoryMetaAndUsage(msg.flow_id);
        updateHistoryUsageBadge(state.historyRecords);
        if (stick) scrollHistoryToBottom();
      }
      // else: nothing changed (all no-op re-deliveries) — skip render entirely
    } else if (rejectsEmptyFullPush(records, state.historyRecords)) {
      // Empty full push against a populated detail view — discard it (#287),
      // keeping the rendered records and the token/signature that pin them.
    } else {
      state.historyEpoch += 1;
      state.historyRecords = records;
      // A full push replaces the cached bundle server-side (a new generation),
      // so any progress token / signature we held no longer pins it — drop them
      // so the next reconnect re-fetch falls back to a full load rather than
      // echoing a stale delta cursor.
      state.historyProgress = null;
      state.historySignature = null;
      renderHistoryRecords(msg.flow_id, state.historyRecords, append);
      refreshHistoryStickyHeader();
      refreshHistoryMetaAndUsage(msg.flow_id);
      updateHistoryUsageBadge(state.historyRecords);
      if (stick) scrollHistoryToBottom();
    }
  }

  // -- running-flow view consumer --
  if (state.selectedFlowId === msg.flow_id) {
    const stick = !append || isNearBottom($("flow-conversation"));
    let merged;
    // Whether a tail-only incremental render is still safe. Any in-place rewrite
    // of an already-held line touches the middle of the array, so it forces a
    // full rebuild; a pure tail append can use the cheap incremental path.
    let appendSafe = true;
    if (append) {
      const rec = reconcileAppendRecords(state.flowConversationRecords, records);
      // `changed` is false only when every incoming record is a byte-identical
      // re-delivery of a line already held (the REST∩WS overlap) — no new tail
      // record and no rewritten line. The idempotent reconcile makes this the
      // ONLY drop case, so a real post-respond append or a retry rewrite always
      // registers as changed and keeps streaming. The history consumer above has
      // already run for this frame, so returning here short-circuits only this
      // running-flow consumer.
      if (!rec.changed) {
        if (wsHistDebug()) {
          // eslint-disable-next-line no-console
          console.debug(
            "hist-diag applyHistoryData ALL-DUPLICATES flow=%s (appendSeq unchanged=%d)",
            msg.flow_id, state.flowConversationAppendSeq);
        }
        return;                             // all no-op re-deliveries — skip entirely
      }
      merged = rec.records;
      // A real WS increment landed for the open flow — mark the push path alive
      // so a pending progression grace timer skips its fallback rebuild. Only
      // this genuine-change path counts (the no-op case returned above).
      state.flowConversationAppendSeq += 1;
      appendSafe = !rec.updatedInPlace;
      if (wsHistDebug()) {
        // eslint-disable-next-line no-console
        console.debug(
          "hist-diag applyHistoryData APPEND-APPLIED flow=%s fresh=%d inPlace=%s appendSeq=%d",
          msg.flow_id, rec.fresh.length, rec.updatedInPlace,
          state.flowConversationAppendSeq);
      }
    } else if (rejectsEmptyFullPush(records, state.flowConversationRecords)) {
      // Empty full push against an already-rendered conversation — discard the
      // frame (#287). The history consumer above has already run, so returning
      // here short-circuits only this running-flow consumer, leaving the DOM,
      // the records, the epoch and the held token/signature exactly as they were.
      return;
    } else {
      state.flowConversationEpoch += 1;
      merged = records;
      // Full push = new server bundle generation; the held delta cursor and the
      // signature it was paired with are now stale, so invalidate both (mirrors
      // the history-view branch above).
      state.flowConversationProgress = null;
      state.flowConversationSignature = null;
      // A mode:full WS push also delivered fresh authoritative records for the
      // open flow, so it likewise counts as the push path being alive.
      state.flowConversationAppendSeq += 1;
    }
    // When the daemon's authoritative user record lands, drop the matching
    // optimistic local echo so the reply is shown once. A mid-list removal
    // shifts indices, so the incremental-append render (which only re-reads the
    // tail) can no longer be trusted — force a full rebuild in that case.
    const reconciled = reconcileLocalEchoes(merged);
    const echoRemoved = reconciled !== merged;
    state.flowConversationRecords = reconciled;
    renderConversation(
      $("flow-conversation"), state.flowConversationRecords,
      append && !echoRemoved && appendSafe);
    refreshFlowStickyHeader();
    updateFlowUsageBadge(state.flowConversationRecords);
    if (stick) scrollFlowConversationToBottom();
  }
}

// True when the reader is at (or within a small threshold of) the bottom of a
// scroll container. Used to decide whether an incremental append should keep
// the viewport pinned to the latest record, or leave it where the reader
// scrolled to so they can finish reading an earlier turn.
function isNearBottom(c) {
  if (!c) return true;
  return c.scrollHeight - c.scrollTop - c.clientHeight <= 80;
}

function formatTime(ts) {
  if (ts == null || ts === "") return "";
  let d;
  if (typeof ts === "number") {
    // Epoch seconds (server uses time.time()) vs milliseconds.
    d = new Date(ts < 1e12 ? ts * 1000 : ts);
  } else {
    d = new Date(ts);
  }
  return isNaN(d.getTime()) ? String(ts) : d.toLocaleString();
}

// True when at least one daemon is currently connected (any machine online).
// The history empty-state logic uses this to tell "still connecting / waiting
// for history" apart from "confirmed no history".
function daemonConnected() {
  return (state.machines || []).some((m) => m && m.online);
}

// Classify the history list's empty/loading semantics into one of four states so
// renderHistoryList can show a distinct, user-readable hint for each instead of
// collapsing "still connecting" into the confirmed-empty state. Pure (all inputs
// passed in) so it can be unit-tested without a DOM.
//   has-sessions    : there are sessions to render
//   loading-refresh : empty but an /api/history round-trip is in flight
//   loading-connect : empty and not yet confirmed (no daemon connected, or no
//                     history_index pushed) — keep showing a waiting hint, never
//                     fall back to the empty-state on the 2s /api/history timeout
//   empty-confirmed : empty AND confirmed (daemon connected and zero sessions)
function historyListEmptyState({ sessions, loading, daemonConnected, indexConfirmed }) {
  if (Array.isArray(sessions) && sessions.length) return "has-sessions";
  if (loading) return "loading-refresh";
  if (!daemonConnected || !indexConfirmed) return "loading-connect";
  return "empty-confirmed";
}

// Sentinel key for the bucket of sessions that carry no `project_root` field
// (legacy archived sessions written before the field existed). Chosen so it
// cannot collide with any real absolute path; the visible label rendered for
// this bucket is "未知项目".
const UNKNOWN_PROJECT_ROOT = "__se3_unknown_project__";
const UNKNOWN_PROJECT_ROOT_LABEL = "Unknown project";

// Group history sessions by their `project_root` field. Pure: no DOM, no
// state. Returns an array of `{ project_root, label, sessions, latestTs }`
// buckets where:
//   * sessions are deduplicated by project_root (real absolute paths kept as-is;
//     empty / null / undefined / non-string falsy values fold into the
//     UNKNOWN_PROJECT_ROOT bucket)
//   * sessions inside a bucket keep their input relative order
//   * buckets are sorted by `latestTs` (the max of each session's
//     `updated_at || created_at`) in descending order
//   * the UNKNOWN bucket, when it exists, is always pinned at the tail
function groupHistorySessionsByProjectRoot(sessions) {
  if (!Array.isArray(sessions) || sessions.length === 0) return [];
  const byKey = new Map();
  for (const s of sessions) {
    if (!s || typeof s !== "object") continue;
    const raw = s.project_root;
    const key = typeof raw === "string" && raw ? raw : UNKNOWN_PROJECT_ROOT;
    let bucket = byKey.get(key);
    if (!bucket) {
      bucket = {
        project_root: key,
        label: key === UNKNOWN_PROJECT_ROOT ? UNKNOWN_PROJECT_ROOT_LABEL : key,
        sessions: [],
        latestTs: 0,
      };
      byKey.set(key, bucket);
    }
    bucket.sessions.push(s);
    const ts = tsValue(s.updated_at != null ? s.updated_at : s.created_at);
    if (typeof ts === "number" && ts > bucket.latestTs) bucket.latestTs = ts;
  }
  const buckets = Array.from(byKey.values());
  buckets.sort((a, b) => {
    const aUnknown = a.project_root === UNKNOWN_PROJECT_ROOT;
    const bUnknown = b.project_root === UNKNOWN_PROJECT_ROOT;
    if (aUnknown !== bUnknown) return aUnknown ? 1 : -1;
    return (b.latestTs || 0) - (a.latestTs || 0);
  });
  return buckets;
}

// Pick the project_root key the History view should select by default. Pure.
// Fallback chain:
//   1. `currentSelected` if it still corresponds to a bucket — preserves the
//      user's manual tab choice across re-renders
//   2. otherwise the first bucket's key (which, after the sort in
//      groupHistorySessionsByProjectRoot, is the most-recently-active project)
//   3. otherwise null (no buckets at all)
function pickDefaultHistoryProjectRoot(buckets, currentSelected) {
  if (!Array.isArray(buckets) || buckets.length === 0) return null;
  if (currentSelected != null) {
    for (const b of buckets) {
      if (b && b.project_root === currentSelected) return currentSelected;
    }
  }
  return buckets[0].project_root;
}

// Build the project select dropdown shown above the history items. One option
// per bucket; change events update `state.historySelectedProjectRoot` and
// re-render the list. Caller decides whether to render at all (callers skip
// when buckets.length < 2 to avoid a visually-redundant single-option control).
function renderHistoryProjectSelect(buckets, selected, onSelect) {
  const row = el("label", "history-project-select-row");
  row.append(el("span", "history-project-select-label", tf("history.projectLabel", "Project")));
  const select = el("select", "history-project-select");
  for (const b of buckets) {
    const label = b.project_root === UNKNOWN_PROJECT_ROOT
      ? tf("history.unknownProject", UNKNOWN_PROJECT_ROOT_LABEL)
      : b.label;
    const opt = el("option", null, label);
    opt.value = b.project_root;
    opt.title = label;
    select.appendChild(opt);
  }
  select.value = selected;
  select.addEventListener("change", () => onSelect(select.value));
  row.appendChild(select);
  return row;
}

function renderHistoryList() {
  const list = $("history-list");
  list.innerHTML = "";
  const sessions = state.historySessions || [];
  const emptyState = historyListEmptyState({
    sessions,
    loading: state.historyIndexLoading,
    daemonConnected: daemonConnected(),
    indexConfirmed: state.historyIndexConfirmed,
  });
  if (emptyState !== "has-sessions") {
    // Distinguish "still refreshing" / "still connecting / waiting for data"
    // from "confirmed no history" so the user is never left staring at a bare
    // empty-state while the daemon is still connecting or pushing its index.
    // Each state carries its own modifier class so it is DOM-distinguishable.
    if (emptyState === "loading-refresh") {
      list.appendChild(el("p", "empty empty-loading-refresh", tf("history.refreshing", "Refreshing history…")));
    } else if (emptyState === "loading-connect") {
      list.appendChild(
        el("p", "empty empty-loading-connect", tf("history.connecting", "Connecting / waiting for history data…")));
    } else {
      list.appendChild(
        el("p", "empty empty-confirmed", tf("history.emptyConfirmed", "No history sessions reported.")));
    }
    return;
  }

  // Group sessions by project_root and resolve the active tab. The default
  // selection is recomputed on every render so a project that disappears from
  // the index (its sessions were archived elsewhere) does not leave a dead key
  // in `state.historySelectedProjectRoot` that would filter out everything.
  const buckets = groupHistorySessionsByProjectRoot(sessions);
  const selected = pickDefaultHistoryProjectRoot(
    buckets, state.historySelectedProjectRoot);
  state.historySelectedProjectRoot = selected;

  // >= 2 buckets: show the select dropdown. A single-option control would add
  // visual weight without offering a real choice, so we suppress it in that case.
  if (buckets.length >= 2) {
    list.appendChild(renderHistoryProjectSelect(buckets, selected, (root) => {
      if (state.historySelectedProjectRoot === root) return;
      state.historySelectedProjectRoot = root;
      renderHistoryList();
    }));
  }

  // Have sessions: when a refresh is in flight, prepend a lightweight bar so
  // the user knows the list is being updated without hiding existing entries.
  if (state.historyIndexLoading) {
    list.appendChild(el("p", "history-refreshing", tf("history.refreshing", "Refreshing history…")));
  }

  // Render only the cards belonging to the selected bucket. An out-of-band
  // selected key (no matching bucket) collapses to an empty session list
  // rather than leaking unrelated cards.
  const visibleBucket = buckets.find((b) => b.project_root === selected);
  const visibleSessions = visibleBucket ? visibleBucket.sessions : [];
  for (const s of visibleSessions) {
    const card = el("div", "history-item");
    if (s.flow_id === state.selectedHistoryId) card.classList.add("selected");

    const head = el("div", "history-item-head");
    const task = el("span", "history-task",
      s.task_description || s.flow_id || tf("history.untitledSession", "(untitled session)"));
    task.title = s.task_description || s.flow_id || "";
    // Badge is the raw session status. A worktree session's merge back now runs
    // as its own merge_integrate / version_reconcile steps (rendered in the
    // transcript), so there is no completed-body "合并中" fold on this surface.
    const sc = statusClass(s.status);
    head.append(
      task,
      el("span", "badge badge-" + sc, flowStatusText(s.status)));
    if (s.active) head.appendChild(el("span", "badge badge-live",
      tf("history.badge.live", "● live")));
    const resumeBtn = makeResumeButton(s);
    if (resumeBtn) head.appendChild(resumeBtn);
    card.appendChild(head);

    const meta = el("div", "history-item-meta");
    // The session's flow_id is its se3 identity (this history record's own ID).
    // Surface it in the meta row, truncating with ellipsis but keeping the full
    // value in `title` so it stays readable/copyable when it overflows.
    const flowIdSpan = el("span", "history-item-flow-id", s.flow_id || "");
    flowIdSpan.title = s.flow_id || "";
    meta.append(
      el("span", null, s.machine_id || ""),
      flowIdSpan,
      el("span", null, formatTime(s.updated_at || s.created_at)),
    );
    card.appendChild(meta);

    card.addEventListener("click", () => openHistorySession(s.flow_id));
    list.appendChild(card);
  }
}

function historyTitle(flowId) {
  const s = (state.historySessions || []).find((x) => x.flow_id === flowId);
  return (s && s.task_description) || flowId || "Session";
}

// Open (or incrementally refresh) a history-detail session.
//
//   opts.incremental === false (default): the user picked a (possibly new)
//     session. Reset the held records/progress, clear the detail DOM and its
//     reconciliation state, send no `after` token (full load), and full-rebuild
//     the result — first-selection behaviour is unchanged.
//
//   opts.incremental === true: a WS-reconnect refresh of the SAME open session
//     (`ws.onopen`). Records, progress, the detail DOM and `__convState` are
//     all preserved; the held progress token is echoed via `?after=` so the
//     server returns only the delta emitted during the outage, which is deduped
//     and appended through the shared merge path. A `delivery: "full"` answer
//     (stale token / cache miss / replacement) falls back to an authoritative
//     full rebuild matching the current full-load result. A failed request
//     leaves the existing detail untouched.
async function openHistorySession(flowId, opts) {
  const incremental = !!(opts && opts.incremental);
  state.selectedHistoryId = flowId;
  // As in loadFlowConversation, starting any request invalidates older
  // reconnect refreshes so only the newest response may update records and
  // progress.
  state.historyEpoch += 1;
  if (!incremental) {
    state.historyRecords = [];
    // Selecting a (possibly different) session resets the held progress so this
    // session's first fetch is a full load, never a delta against another's
    // bundle. A reconnect re-fetch of the *same* session repopulates it.
    state.historyProgress = null;
    state.historySignature = null;
    // A different session must never show the previous session's usage payload
    // (the badge + usage region reset with it until this load lands).
    state.historyUsage = null;
  }
  // Narrow screens switch to the detail panel; inert on desktop. Idempotent on
  // a reconnect refresh where the detail panel is already shown.
  applyHistoryPanelAction("select-session");
  renderHistoryList();
  $("history-detail-title").textContent = historyTitle(flowId);
  // Show the complete flow_id on its own line, always — independent of the
  // title's task_description→flow_id fallback, so the se3 flow_id stays
  // identifiable even when a task_description is present.
  const flowIdEl = $("history-detail-flow-id");
  flowIdEl.textContent = flowId || "";
  // Reset/seed the session-usage badge: a fresh (non-incremental) open has just
  // cleared historyRecords so the badge hides until the load lands; a reconnect
  // refresh keeps the prior total visible.
  updateHistoryUsageBadge(state.historyRecords);
  // Clear the strategy/scope meta + usage region on a fresh open so a session
  // switch can never briefly show the previous session's blocks; a reconnect
  // refresh keeps the existing blocks until the load settles.
  if (!incremental) {
    const meta = $("history-meta");
    if (meta) meta.innerHTML = "";
    const usageRegion = $("history-usage-region");
    if (usageRegion) {
      usageRegion.innerHTML = "";
      usageRegion.classList.add("hidden");
    }
  }

  // Inject a Resume button into the detail header when the session is resumable.
  // Anchor the resume-bar to the flow_id line (a stable static element) rather
  // than the title's nextElementSibling: the static flow_id / usage-badge
  // elements now sit between the title and any resume-bar, so a sibling-of-title
  // probe would no longer find a prior resume-bar to clean up.
  const priorBar = flowIdEl.nextElementSibling;
  if (priorBar && priorBar.classList.contains("history-resume-bar")) {
    priorBar.remove(); // clean up previous
  }
  const session = (state.historySessions || []).find((x) => x.flow_id === flowId);
  const resumeBtn = makeResumeButton(session);
  if (resumeBtn) {
    const resumeBar = el("div", "history-resume-bar");
    resumeBar.appendChild(resumeBtn);
    flowIdEl.after(resumeBar);
  }

  const detail = $("history-detail");
  if (!incremental) {
    detail.innerHTML = "";
    // Drop reconciliation state from the previously-selected session.
    detail.__convState = null;
    detail.appendChild(el("p", "empty", tf("history.loadingRecords", "Loading records…")));
  }
  const requestEpoch = state.historyEpoch;

  // See loadFlowConversation: this baseline separates records from the
  // generation being refreshed from live appends that arrive during the await.
  const requestRecords = state.historyRecords;
  try {
    // See loadFlowConversation: only echo the held token when its backing
    // records are still held, so a cleared/replaced bundle can never have a
    // stale delta offset applied (which would render only the tail = a
    // truncated session). An empty held set forces a full reload.
    const heldProgress = state.historyRecords.length
      ? state.historyProgress : null;
    const heldSignature = heldProgress ? state.historySignature : null;
    const url = incremental
      ? historySnapshotUrl(flowId, heldProgress, heldSignature)
      : `/api/history/${encodeURIComponent(flowId)}`;
    const resp = await authedFetch(url);
    // The user may have clicked another session while this was in flight.
    if (
      state.selectedHistoryId !== flowId ||
      state.historyEpoch !== requestEpoch
    ) return;
    if (!resp.ok) {
      // Keep the existing detail on a reconnect refresh failure.
      if (incremental) return;
      detail.innerHTML = "";
      detail.appendChild(el("p", "empty",
        tf("history.loadError", `Could not load history for this session (${resp.status}).`, { status: resp.status })));
      return;
    }
    const data = await resp.json();
    if (
      state.selectedHistoryId !== flowId ||
      state.historyEpoch !== requestEpoch
    ) return;
    // Adopt the backend usage payload when the bundle carries one (complete
    // full snapshots only); an incremental/delta reply without it keeps the
    // previously-adopted payload — the summary never flickers to empty on a
    // reconnect refresh.
    if (data && data.usage && typeof data.usage === "object") {
      state.historyUsage = data.usage;
    }
    // Measure stickiness before the render mutates layout.
    const stick = incremental ? isNearBottom(historyScrollContainer()) : true;
    // Same shared full/delta decision as the running-flow loader. On first open
    // no `after` is sent so the server replies `delivery: "full"` and the live
    // appends that arrived during the await are preserved; on reconnect the
    // progress token is echoed for a delta.
    const result = mergeHistoryResponse(
      data,
      state.historyRecords,
      requestRecords,
    );
    // See loadFlowConversation: a merge-rejected frame (the #287 empty-full
    // guard) leaves the held generation — and therefore its token/signature —
    // untouched.
    if (!result.preserveTokens) {
      state.historyProgress = result.progress;
      if (result.signature != null) {
        state.historySignature = result.signature;
      }
    }
    if (result.resync) {
      // Symmetric with loadFlowConversation: a stale/rotated signed cursor drew
      // a recoverable full; adopt its authoritative token (above) and void the
      // dead generation's repair state (see resetRepairStateForResync).
      resetRepairStateForResync("history", flowId);
    }
    if (result.render === "noop") {
      // A `not_modified` reply, or a delta that added nothing new after dedup —
      // nothing to repaint. As in loadFlowConversation this is not evidence of
      // completeness, so the cursor self-check still runs.
      await reconcileCursorCompleteness("history", flowId, result.cursor, result.generation, result.pending);
      return;
    }
    state.historyRecords = result.records;
    // Delta delivery → incremental append render; full fallback → full rebuild.
    renderHistoryRecords(flowId, state.historyRecords, result.render === "delta");
    refreshHistoryStickyHeader();
    // Strategy/scope meta and the backend usage region ride the same records /
    // payload the conversation just rendered, so they refresh in lockstep.
    refreshHistoryMetaAndUsage(flowId);
    updateHistoryUsageBadge(state.historyRecords);
    if (stick) scrollHistoryToBottom();
    await reconcileCursorCompleteness("history", flowId, result.cursor, result.generation, result.pending);
  } catch (_) {
    if (
      state.selectedHistoryId !== flowId ||
      state.historyEpoch !== requestEpoch
    ) return;
    if (incremental) return;            // keep the existing detail
    detail.innerHTML = "";
    detail.appendChild(el("p", "empty", tf("history.networkError", "Network error loading session history.")));
  }
}

// ---------------------------------------------------------------------------
// Issue management view
// ---------------------------------------------------------------------------
//
// The issues view mirrors the history view layout: a full-screen overlay with
// a list pane (filters + cards) and a detail pane. Issues are fetched from
// the REST API (`/api/issues`) and write operations dispatch MSG_ISSUE_COMMAND
// to the daemon. The list refreshes on every STATUS_UPDATE (via applyMachines).

function isIssuesOpen() {
  return !$("issues-view").classList.contains("hidden");
}

function openIssues() {
  $("issues-view").classList.remove("hidden");
  applyIssuesPanelAction("reset");
  renderIssuesList();
  fetchIssues();
  // Populate the type dropdown universe from an unfiltered fetch so that
  // switching types works even when a type filter is already active.
  fetchAllIssueTypes();
}

function closeIssues() {
  $("issues-view").classList.add("hidden");
  applyIssuesPanelAction("reset");
  state.selectedIssueId = null;
  // Reset the project filter so the next open recomputes the default
  // (mirrors closeHistory resetting historySelectedProjectRoot).
  state.issuesProjectFilter = "";
}

// Collect all issues from the machine snapshot. Issues are already in-memory
// from the latest STATUS_UPDATE; this just flattens them.
function collectAllIssues() {
  const all = [];
  for (const m of (state.machines || [])) {
    if (!m || !Array.isArray(m.issues)) continue;
    for (const iss of m.issues) {
      if (iss && typeof iss === "object") {
        all.push({ ...iss, _machine_id: m.machine_id });
      }
    }
  }
  return all;
}

// fetchIssuesCoalesceDecision — pure helper for the fetchIssues request-coalescing
// entry decision.  Extracted for regression testing (the previous starvation bug
// was caused by bumping the seq counter on every STATUS_UPDATE, discarding all
// in-flight responses as "stale").
//
// Returns { action: "defer", refreshPending: true } when a request is already
// in-flight (the caller should NOT start a new fetch; the in-flight request will
// re-dispatch on completion if refreshPending is set in the finally block).
// Returns { action: "proceed", seq: nextSeq } when no request is in-flight (the
// caller should bump the seq counter and start the fetch).
function fetchIssuesCoalesceDecision({ inFlight, seq }) {
  if (inFlight) {
    return { action: "defer", refreshPending: true };
  }
  return { action: "proceed", seq: seq + 1 };
}

// fetchIssuesFinallyDecision — pure helper for the fetchIssues finally-block
// ordering.  The critical invariant is: render the current data BEFORE checking
// refreshPending and re-dispatching.  Reversing the order (re-dispatch then render)
// bumps seq before render reads it, causing the freshly-rendered data to be
// immediately stale.
//
// Returns { applyResponse, reDispatch }:
//   applyResponse is true when completedSeq === fetchSeq (no newer request has
//   been started since this one began).
//   reDispatch is true when refreshPending was set (a STATUS_UPDATE arrived
//   while this request was in-flight).
function fetchIssuesFinallyDecision(completedSeq, { fetchSeq, refreshPending }) {
  return {
    applyResponse: completedSeq === fetchSeq,
    reDispatch: refreshPending,
  };
}

async function fetchIssues() {
  // Coalesce rapid repeated calls (e.g. STATUS_UPDATE arriving every 2s while
  // the /api/issues response takes >2s).  If a request is already in-flight,
  // mark that a refresh is needed and return — the in-flight request will
  // re-trigger fetchIssues() on completion if _issuesRefreshPending is set.
  // This prevents the old pattern where every STATUS_UPDATE bumped
  // _issuesFetchSeq, causing every in-flight response to be discarded as
  // "stale" and the issue list to never receive data.
  const entryDecision = fetchIssuesCoalesceDecision({
    inFlight: state._issuesFetchInFlight,
    seq: state._issuesFetchSeq,
  });
  if (entryDecision.action === "defer") {
    state._issuesRefreshPending = entryDecision.refreshPending;
    return;
  }

  const seq = entryDecision.seq;
  state._issuesFetchSeq = seq;
  state._issuesFetchInFlight = true;
  state.issuesLoading = true;
  renderIssuesList();
  try {
    const params = new URLSearchParams();
    if (state.issuesShowClosed) params.set("include_closed", "true");
    if (state.issuesSourceFilter) params.set("source", state.issuesSourceFilter);
    if (state.issuesTypeFilter) params.set("type", state.issuesTypeFilter);
    if (state.issuesProjectFilter) params.set("project_root", state.issuesProjectFilter);
    const qs = params.toString();
    const resp = await authedFetch("/api/issues" + (qs ? "?" + qs : ""));
    if (!resp.ok) return;
    const data = await resp.json().catch(() => ({ issues: [] }));
    // Only apply results if no newer fetch has been started since we began.
    if (seq !== state._issuesFetchSeq) return;
    if (Array.isArray(data.issues)) {
      state.issues = data.issues;
    }
  } catch (_) {
    /* transient — next STATUS_UPDATE will refresh */
  } finally {
    state._issuesFetchInFlight = false;
    // Always clear the loading flag — the critical invariant is that
    // issuesLoading is false whenever NO request is in-flight.
    state.issuesLoading = false;
    // Use the pure decision helper to determine whether to apply this
    // response and whether to re-dispatch a pending refresh.
    const finallyDecision = fetchIssuesFinallyDecision(seq, {
      fetchSeq: state._issuesFetchSeq,
      refreshPending: state._issuesRefreshPending,
    });
    // Only apply data/render updates for the most recent request.
    if (finallyDecision.applyResponse) {
      renderIssuesList();
      refreshIssueTypeFilter();
      // Re-render the detail pane if one is selected so status badges and
      // action buttons reflect the latest data (e.g. after close/reopen).
      if (state.selectedIssueId) {
        const stillExists = (state.issues || []).some(
          (i) => i && issueCompositeKey(i) === state.selectedIssueId,
        );
        if (stillExists) {
          renderIssueDetail(state.selectedIssueId);
        } else {
          // Issue dropped out of the current filter (e.g. closed with open-only
          // view).  Clear the stale detail pane.
          state.selectedIssueId = null;
          applyIssuesPanelAction("reset");
        }
      }
    }
    // If a refresh was requested while this request was in-flight, kick off
    // a new fetch *after* rendering the current data.  The new fetch will
    // re-set issuesLoading = true and re-render with fresher data once it
    // completes.  This ensures the issue list is never stuck in a perpetual
    // loading state when STATUS_UPDATEs arrive faster than the API responds.
    if (finallyDecision.reDispatch) {
      state._issuesRefreshPending = false;
      fetchIssues();
    }
  }
}

// allIssueTypesApplyDecision — pure helper deciding whether a completed
// fetchAllIssueTypes response is still the latest in-flight request and may
// update the dropdown universes.  Extracted for DOM-free regression testing of
// the stale-response guard: frequent STATUS_UPDATEs start overlapping requests,
// and without this sequence check a slower older response could complete last
// and overwrite a newer project-root universe (removing a just-added project and
// resetting the user's selected project to "全部项目").  Returns true only when
// completedSeq matches the latest seq.
function allIssueTypesApplyDecision(completedSeq, currentSeq) {
  return completedSeq === currentSeq;
}

// Fetch all issue types once (unfiltered) so the dropdown stays complete even
// when a type filter is active.  Populates state.allIssueTypes and
// state.allIssueProjectRoots (both come from the same unfiltered response).
async function fetchAllIssueTypes() {
  // Sequence guard: bump a monotonic counter per request so that when several
  // requests overlap (STATUS_UPDATE arrives faster than /api/issues responds),
  // only the most recently started one is allowed to update the dropdown
  // universes.  An older response completing after a newer one must NOT
  // overwrite the project-root universe (which would drop a just-added project
  // and reset the selected project).
  const seq = state._allIssueTypesFetchSeq + 1;
  state._allIssueTypesFetchSeq = seq;
  try {
    const params = new URLSearchParams();
    if (state.issuesShowClosed) params.set("include_closed", "true");
    if (state.issuesSourceFilter) params.set("source", state.issuesSourceFilter);
    // Deliberately omit 'type' and 'project_root' params to get the full
    // universe for both dropdowns.
    const qs = params.toString();
    const resp = await authedFetch("/api/issues" + (qs ? "?" + qs : ""));
    if (!resp.ok) return;
    const data = await resp.json().catch(() => ({ issues: [] }));
    // Discard a stale response: a newer fetchAllIssueTypes has started since
    // this one began, so applying these (older) results would clobber the
    // newer universe and selection.
    if (!allIssueTypesApplyDecision(seq, state._allIssueTypesFetchSeq)) return;
    if (Array.isArray(data.issues)) {
      state.allIssueTypes = issueTypes(data.issues);
      state.allIssueProjectRoots = issueProjectRoots(data.issues);
    }
  } catch (_) {
    /* best-effort */
  }
  // Only the latest request re-renders the dropdowns; a stale response must not
  // re-derive (and reset) the selection from an outdated universe.
  if (!allIssueTypesApplyDecision(seq, state._allIssueTypesFetchSeq)) return;
  refreshIssueTypeFilter();
  refreshIssueProjectFilter();
}

// selectTypeDropdownOptions is the pure selection logic for the type-filter
// dropdown (exported for the DOM-free tests in
// tests/frontend/issue_management.test.mjs).  It prefers the cached
// unfiltered type universe so the dropdown stays complete when a type filter
// is active; it falls back to deriving types from the current (possibly
// filtered) issues list when the cache is empty (e.g. first load or
// fetchAllIssueTypes has not yet returned).
function selectTypeDropdownOptions(allIssueTypes, issues) {
  return allIssueTypes && allIssueTypes.length
    ? allIssueTypes
    : issueTypes(issues);
}

function refreshIssueTypeFilter() {
  const sel = $("issues-type-filter");
  if (!sel) return;
  const current = sel.value;
  const types = selectTypeDropdownOptions(state.allIssueTypes, state.issues);
  sel.innerHTML = "";
  sel.appendChild(new Option(tf("issues.typeAll", "All types"), ""));
  for (const t of types) {
    const opt = document.createElement("option");
    opt.value = t;
    opt.textContent = issueTypeText(t);
    sel.appendChild(opt);
  }
  sel.value = types.includes(current) ? current : "";
}

// Rebuild the project-root filter dropdown from the unfiltered project universe
// (state.allIssueProjectRoots). Mirrors refreshIssueTypeFilter's pattern: the
// dropdown stays complete even when a project filter is active, because the
// options come from the separate unfiltered universe rather than from the
// already-narrowed state.issues list.
function refreshIssueProjectFilter() {
  const sel = $("issues-project-filter");
  if (!sel) return;
  // Resolve "current" from state, NOT from the DOM <select>. The DOM value can
  // be stale after closeIssues() resets state.issuesProjectFilter to "" without
  // touching the (now-hidden) <select>; reading sel.value here would re-select
  // the previous project and silently re-narrow the list on the next fetch.
  // State is the single source of truth, so close-reset mirrors closeHistory.
  const current = state.issuesProjectFilter;
  const roots = state.allIssueProjectRoots;
  sel.innerHTML = "";
  sel.appendChild(new Option(tf("issues.projectAll", "All projects"), ""));
  for (const pr of roots) {
    const opt = document.createElement("option");
    opt.value = pr;
    // Show the project_root as-is (it's already an absolute path); truncate
    // very long paths for readability using the shared truncate helper.
    opt.textContent = pr.length > 60 ? truncate(pr, 60) : pr;
    opt.title = pr; // full path on hover
    sel.appendChild(opt);
  }
  // Resolve the default: keep the current selection if it still exists,
  // otherwise fall back to "全部项目" (empty string).
  const resolved = pickDefaultIssueProjectRoot(roots, current);
  sel.value = resolved;
  // Sync the state with the resolved selection so fetchIssues sends the
  // correct parameter on the next request.
  state.issuesProjectFilter = resolved;
}

function renderIssuesList() {
  const list = $("issues-list");
  if (!list) return;
  list.innerHTML = "";

  const filtered = filterIssues(state.issues, {
    showClosed: state.issuesShowClosed,
    sourceFilter: state.issuesSourceFilter,
    typeFilter: state.issuesTypeFilter,
  });

  if (state.issuesLoading && !filtered.length) {
    list.appendChild(el("p", "empty", tf("issues.loading", "Loading issues…")));
    return;
  }
  if (!filtered.length) {
    list.appendChild(el("p", "empty", tf("issues.empty", "No issues yet.")));
    return;
  }

  // Sort by id descending (newest first).
  filtered.sort((a, b) => String(b.id || "").localeCompare(String(a.id || "")));

  for (const iss of filtered) {
    const card = el("div", "issue-item");
    if (issueCompositeKey(iss) === state.selectedIssueId) card.classList.add("selected");
    card.classList.add("issue-source-" + (iss.source || "system"));

    const head = el("div", "issue-item-head");
    const title = el("span", "issue-title-text", issueDisplayTitle(iss));
    title.title = issueDisplayTitle(iss);
    const idLabel = el("span", "issue-id-label", "#" + (iss.id || "?"));
    head.append(title, idLabel);

    const sc = issueStatusClass(iss.status);
    head.appendChild(el("span", "badge " + sc, issueStatusText(iss.status)));
    card.appendChild(head);

    const meta = el("div", "issue-item-meta");
    if (iss.type) meta.appendChild(el("span", null, issueTypeText(iss.type)));
    if (iss.priority) {
      meta.appendChild(
        el("span", issuePriorityClass(iss.priority), issuePriorityText(iss.priority)),
      );
    }
    meta.appendChild(el("span", null, issueSourceText(iss.source)));
    if (iss.created_at) {
      meta.appendChild(el("span", null, formatTime(iss.created_at)));
    }
    card.appendChild(meta);

    const rowActions = el("div", "issue-item-actions");
    rowActions.appendChild(makeIssueLaunchButton(iss, "issue-item-launch"));
    card.appendChild(rowActions);

    card.addEventListener("click", () => openIssueDetail(issueCompositeKey(iss)));
    list.appendChild(card);
  }
}

// The daemon clips issue descriptions / call prompts to this many characters
// (with a trailing "...") before inlining them in a status snapshot — mirrors
// se3.daemon.history._DESC_CLIP. A preview longer than this was therefore
// clipped and has a full body worth pulling on demand.
const DESC_CLIP = 200;

// A preview text was truncated iff it exceeds the clip length: the clipper only
// ever shortens (to exactly DESC_CLIP + "..."), so length > DESC_CLIP uniquely
// identifies a clipped body. Pure.
function descriptionLikelyTruncated(text) {
  return typeof text === "string" && text.length > DESC_CLIP;
}

// Fetch one issue's untruncated description from the owning daemon (via the
// server's on-demand `/api/issues/{id}/detail` downlink) and swap it into the
// already-rendered detail pane. On success the truncated preview node is
// replaced in place; on failure the preview is kept and the pre-created hint
// `note` is revealed. A late response is dropped if the user has since selected
// another issue (guarded on `selectedIssueId`), so a stale full body can never
// overwrite a different issue's pane.
// Fetch one issue's untruncated description from the owning daemon (via the
// server's on-demand `/api/issues/{id}/detail` downlink). Returns the full
// description string, or null when it is unavailable. Shared by the detail
// pane's lazy upgrade and the edit modal's textarea pre-fill, so an edit starts
// from the full body rather than the DESC_CLIP preview.
async function fetchIssueFullDescription(iss) {
  const params = new URLSearchParams();
  const mid = issueMachineId(iss);
  if (mid) params.set("machine_id", mid);
  if (iss.project_root) params.set("project_root", String(iss.project_root));
  const qs = params.toString();
  const url = `/api/issues/${encodeURIComponent(String(iss.id))}/detail`
    + (qs ? `?${qs}` : "");
  const resp = await authedFetch(url);
  if (!resp.ok) throw new Error(`status ${resp.status}`);
  const data = await resp.json();
  return data && data.issue && typeof data.issue.description === "string"
    ? data.issue.description : null;
}

async function loadIssueFullDescription(iss, descNode, note) {
  const key = issueCompositeKey(iss);
  try {
    const full = await fetchIssueFullDescription(iss);
    if (state.selectedIssueId !== key) return;   // user moved on — drop it
    if (full != null && descNode) {
      descNode.textContent = full;
      if (note) note.classList.add("hidden");
    } else if (note) {
      // The daemon returned no description (e.g. the issue vanished) — keep the
      // preview and surface the hint rather than blanking the field.
      note.classList.remove("hidden");
    }
  } catch (_) {
    if (state.selectedIssueId !== key) return;
    if (note) note.classList.remove("hidden");
  }
}

// Fetch one pending call's untruncated prompt from the owning daemon (via the
// server's on-demand `/api/calls/{id}/detail` downlink). Returns the full prompt
// string, or null when it is unavailable. Both STATUS_UPDATE pending_calls
// surfaces (the machine-wide aggregate AND a flow's own list) now clip the prompt
// to DESC_CLIP for wire economy, so the reply-context's collapsed body calls this
// to upgrade the preview to the full text when the operator expands it; kept
// alongside the issue loader so both on-demand detail pulls live in one place.
async function fetchCallFullPrompt(callId, { machineId, projectRoot } = {}) {
  const params = new URLSearchParams();
  if (machineId) params.set("machine_id", String(machineId));
  if (projectRoot) params.set("project_root", String(projectRoot));
  const qs = params.toString();
  const url = `/api/calls/${encodeURIComponent(String(callId))}/detail`
    + (qs ? `?${qs}` : "");
  try {
    const resp = await authedFetch(url);
    if (!resp.ok) return null;
    const data = await resp.json();
    return data && data.call && typeof data.call.prompt === "string"
      ? data.call.prompt : null;
  } catch (_) {
    return null;
  }
}

function openIssueDetail(issueId) {
  state.selectedIssueId = issueId;
  applyIssuesPanelAction("select-issue");
  renderIssueDetail(issueId);
  // Re-render list to update selection highlight.
  renderIssuesList();
}

function renderIssueDetail(issueId) {
  const detail = $("issues-detail");
  const titleNode = $("issues-detail-title");
  if (!detail) return;
  detail.innerHTML = "";

  const iss = (state.issues || []).find((i) => i && issueCompositeKey(i) === issueId);
  if (!iss) {
    detail.appendChild(el("p", "empty", tf("issueDetail.notFound", "Issue not found.")));
    if (titleNode) titleNode.textContent = tf("issues.detailTitle", "Issue");
    return;
  }

  const displayTitle = issueDisplayTitle(iss);
  if (titleNode) titleNode.textContent = displayTitle;

  // Header: title + badges
  const header = el("div", "issue-detail-header");
  const titleEl = el("div", "issue-detail-title", displayTitle);
  const badges = el("div", "issue-detail-badges");
  badges.appendChild(el("span", "badge " + issueStatusClass(iss.status),
    issueStatusText(iss.status)));
  if (iss.source) {
    badges.appendChild(
      el("span", "badge issue-source-" + iss.source, issueSourceText(iss.source)),
    );
  }
  header.append(titleEl, badges);
  detail.appendChild(header);

  // Fields
  const fields = [
    ["ID", "#" + (iss.id || "?")],
    [tf("issueDetail.field.type", "Type"), issueTypeText(iss.type) || "-"],
    [tf("issueDetail.field.priority", "Priority"), issuePriorityText(iss.priority) || "-"],
    [tf("issueDetail.field.source", "Source"), issueSourceText(iss.source)],
    [tf("issueDetail.field.createdAt", "Created"), formatTime(iss.created_at)],
    [tf("issueDetail.field.updatedAt", "Updated"), formatTime(iss.updated_at)],
  ];
  if (iss.tags && iss.tags.length) {
    fields.push([tf("issueDetail.field.tags", "Tags"), iss.tags.join(", ")]);
  }
  if (iss.project_root) {
    fields.push([tf("issueDetail.field.project", "Project"), iss.project_root]);
  }

  for (const [label, value] of fields) {
    const row = el("div", "issue-detail-field");
    row.append(
      el("span", "issue-detail-label", label),
      el("span", "issue-detail-value", String(value)),
    );
    detail.appendChild(row);
  }

  // Description. The STATUS_UPDATE / REST issue mirror carries only a truncated
  // preview (clipped to DESC_CLIP so the ~470 KB of full descriptions never
  // inflate the snapshot); when the preview was clipped, pull the untruncated
  // body on demand and swap it in place. A pre-created (hidden) note is toggled
  // visible if the pull fails, so the truncated preview always survives.
  if (iss.description) {
    const descNode = el("div", "issue-detail-desc", iss.description);
    detail.appendChild(descNode);
    if (descriptionLikelyTruncated(iss.description)) {
      const note = el(
        "div", "issue-detail-desc-note hidden",
        tf("issueDetail.descLoadFailed", "Could not load the full description; reopen to retry."));
      detail.appendChild(note);
      loadIssueFullDescription(iss, descNode, note);
    }
  }

  // Action buttons
  const actions = el("div", "issue-detail-actions");
  actions.appendChild(makeIssueLaunchButton(iss, "issue-detail-launch"));
  const editBtn = el("button", "ghost-btn", tf("issueDetail.edit", "Edit"));
  editBtn.type = "button";
  editBtn.addEventListener("click", () => openIssueEditModal(iss));
  actions.appendChild(editBtn);

  const closedStatuses = new Set(["resolved", "won't-fix", "closed"]);
  if (closedStatuses.has(iss.status)) {
    const reopenBtn = el("button", "ghost-btn", tf("issueDetail.reopen", "Reopen"));
    reopenBtn.type = "button";
    reopenBtn.addEventListener("click", () => openIssueActionModal("reopen", iss));
    actions.appendChild(reopenBtn);
  } else {
    const closeBtn = el("button", "ghost-btn", tf("issueDetail.close", "Close"));
    closeBtn.type = "button";
    closeBtn.addEventListener("click", () => openIssueActionModal("close", iss));
    actions.appendChild(closeBtn);
  }
  detail.appendChild(actions);
}

// ---------------------------------------------------------------------------
// Issue create / edit modal
// ---------------------------------------------------------------------------

// Parse a comma-separated tags string into a trimmed, non-empty array.
// Pure.
function parseTagsFromString(raw) {
  if (!raw || typeof raw !== "string") return [];
  return raw.split(",").map((s) => s.trim()).filter(Boolean);
}

// Format a tags array into a comma-separated display string.
// Pure.
function formatTagsForInput(tags) {
  if (!Array.isArray(tags) || !tags.length) return "";
  return tags.join(", ");
}

// Track which form fields the user has modified so that the edit PATCH
// body can distinguish "user cleared a field" (send empty string) from
// "user didn't touch a field" (don't include in the body).
let _issueFormDirty = new Set();
function _initIssueFormDirtyTracking() {
  const form = $("issue-form");
  if (form) {
    form.addEventListener("input", (e) => {
      if (e.target && e.target.id) _issueFormDirty.add(e.target.id);
    });
  }
}

function _populateIssueMachineSelect() {
  const sel = $("issue-machine");
  if (!sel) return;
  sel.innerHTML = "";
  const online = (state.machines || []).filter((m) => m && m.online);
  if (!online.length) {
    sel.appendChild(new Option(tf("issueModal.noMachines", "(no machines connected)"), ""));
    sel.disabled = true;
    return;
  }
  sel.disabled = false;
  for (const m of online) {
    sel.appendChild(new Option(m.hostname || m.machine_id, m.machine_id));
  }
}

function _refreshIssueProjectOptions() {
  const sel = $("issue-project");
  if (!sel) return;
  const machineId = $("issue-machine") ? $("issue-machine").value.trim() : "";
  const roots = machineProjectRoots(machineId);
  sel.innerHTML = "";
  // Unlike the New Task form, issue commands have no ensure_se3_project
  // fallback — the daemon rejects unregistered paths.  Do not offer an
  // "Other path…" manual entry; restrict to known project roots.
  const manualInput = $("issue-project-manual");
  if (manualInput) manualInput.classList.add("hidden");
  if (!roots.length) {
    sel.appendChild(new Option(tf("issueModal.noProjects", "(this machine has no registered projects)"), ""));
    sel.disabled = true;
    return;
  }
  sel.disabled = false;
  if (roots.length === 1) {
    sel.appendChild(new Option(roots[0], roots[0]));
    sel.value = roots[0];
  } else {
    const ph = new Option(tf("issueModal.selectProject", "(select a project…)"), "");
    ph.disabled = true;
    ph.selected = true;
    sel.appendChild(ph);
    for (const r of roots) {
      sel.appendChild(new Option(r, r));
    }
  }
}

function _updateIssueProjectManualVisibility() {
  const sel = $("issue-project");
  const manualInput = $("issue-project-manual");
  if (!sel || !manualInput) return;
  manualInput.classList.toggle("hidden", sel.value !== PROJECT_MANUAL_SENTINEL);
  if (sel.value === PROJECT_MANUAL_SENTINEL) manualInput.focus();
}

// Lock/unlock the edit modal's description textarea while its full body is being
// fetched. The STATUS_UPDATE mirror carries only the DESC_CLIP preview, so until
// the untruncated body arrives the textarea would hold "preview...". Letting the
// user edit that and save would PATCH the truncated text back over the stored
// full description. Disabling the field while loading — and keeping it disabled
// with a failure hint if the fetch never resolves — makes that truncation
// impossible: a disabled field never enters the dirty set, so buildIssueEditBody
// omits `description` and the stored body is left untouched. Other fields stay
// editable. `phase` is "clear" (enable, hide hint), "loading", or "failed".
function _setIssueDescriptionLock(phase, message) {
  const ta = $("issue-description");
  const hint = $("issue-description-hint");
  if (phase === "clear") {
    if (ta) ta.disabled = false;
    if (hint) { hint.classList.add("hidden"); hint.textContent = ""; }
    return;
  }
  if (ta) ta.disabled = true;
  if (hint) {
    hint.textContent = message || "";
    hint.classList.remove("hidden");
  }
}

function openIssueCreateModal() {
  $("issue-modal-title").textContent = tf("issueModal.title", "New Issue");
  _setIssueDescriptionLock("clear");
  $("issue-description").value = "";
  $("issue-title").value = "";
  $("issue-type").value = "";
  $("issue-priority").value = "";
  $("issue-tags").value = "";
  $("issue-form-submit").textContent = tf("issueModal.submit", "Create");
  $("issue-form-error").classList.add("hidden");
  _issueFormDirty = new Set();
  // Store the editing context: null means create mode.
  $("issue-form").dataset.mode = "create";
  $("issue-form").dataset.issueId = "";
  $("issue-form").dataset.machineId = "";
  $("issue-form").dataset.projectRoot = "";
  // Populate machine/project selectors for create mode.
  $("issue-machine-row").classList.remove("hidden");
  _populateIssueMachineSelect();
  _refreshIssueProjectOptions();
  $("issue-modal").classList.remove("hidden");
  $("issue-description").focus();
}

function openIssueEditModal(iss) {
  if (!iss) return;
  $("issue-modal-title").textContent = tf("issueModal.editTitle", "Edit Issue #" + (iss.id || "?"), { id: iss.id || "?" });
  _setIssueDescriptionLock("clear");
  $("issue-description").value = iss.description || "";
  $("issue-title").value = iss.title || "";
  $("issue-type").value = iss.type || "";
  $("issue-priority").value = iss.priority || "";
  $("issue-tags").value = formatTagsForInput(iss.tags);
  $("issue-form-submit").textContent = tf("issueModal.save", "Save");
  $("issue-form-error").classList.add("hidden");
  _issueFormDirty = new Set();
  $("issue-form").dataset.mode = "edit";
  $("issue-form").dataset.issueId = iss.id || "";
  $("issue-form").dataset.machineId = issueMachineId(iss);
  $("issue-form").dataset.projectRoot = iss.project_root || "";
  // Machine/project context is already known for existing issues — hide the
  // selector row so the user is not confused by an irrelevant dropdown.
  $("issue-machine-row").classList.add("hidden");
  $("issue-modal").classList.remove("hidden");
  $("issue-description").focus();

  // The snapshot description is a DESC_CLIP-truncated preview. Upgrade the
  // textarea to the untruncated body on demand so an edit starts from — and
  // saves back — the full description, not the 200-char preview. While the fetch
  // is in flight the field is DISABLED (see _setIssueDescriptionLock): editing
  // the visible "preview..." and saving would overwrite the stored full body
  // with the truncated text. On success we swap in the full body and re-enable;
  // on failure (or a null body) the field stays disabled with a hint, so the
  // description can never be silently truncated by an edit — other fields remain
  // editable and the PATCH omits description (it never became dirty).
  if (descriptionLikelyTruncated(iss.description)) {
    // Issue identity is composite (id + machine_id + project_root): the same
    // numeric id can name a different issue on another machine/project. Pin all
    // three so a stale in-flight fetch cannot populate the modal after the user
    // reopened it on a same-id issue from a different machine/project.
    const issueId = String(iss.id || "");
    const machineId = String(issueMachineId(iss) || "");
    const projectRoot = String(iss.project_root || "");
    _setIssueDescriptionLock("loading", tf("issueModal.descLoading", "Loading full description…"));
    const _stillEditingThisIssue = () => {
      const form = $("issue-form");
      return form.dataset.mode === "edit"
        && form.dataset.issueId === issueId
        && form.dataset.machineId === machineId
        && form.dataset.projectRoot === projectRoot;
    };
    fetchIssueFullDescription(iss).then((full) => {
      if (!_stillEditingThisIssue()) return;
      if (full == null) {
        _setIssueDescriptionLock(
          "failed",
          tf("issueModal.descLoadFailed", "Could not load the full description; the description is temporarily uneditable (close and retry); other fields remain editable."));
        return;
      }
      if (!_issueFormDirty.has("issue-description")) {
        $("issue-description").value = full;
      }
      _setIssueDescriptionLock("clear");
    }).catch(() => {
      if (!_stillEditingThisIssue()) return;
      _setIssueDescriptionLock(
        "failed",
        tf("issueModal.descLoadFailed", "Could not load the full description; the description is temporarily uneditable (close and retry); other fields remain editable."));
    });
  }
}

function closeIssueModal() {
  $("issue-modal").classList.add("hidden");
}

async function submitIssueForm(event) {
  event.preventDefault();
  const errBox = $("issue-form-error");
  errBox.classList.add("hidden");
  const form = $("issue-form");
  const mode = form.dataset.mode || "create";
  const description = $("issue-description").value.trim();
  if (!description) {
    showFormError(errBox, tf("issueModal.errDescRequired", "Description must not be empty."));
    return;
  }

  const submit = $("issue-form-submit");
  submit.disabled = true;

  try {
    let resp;
    if (mode === "create") {
      // Resolve machine_id and project_root from the form's dropdowns.
      const machineId = $("issue-machine") ? $("issue-machine").value.trim() : "";
      if (!machineId) {
        showFormError(errBox, tf("issueModal.errNoMachine", "No online machine is available."));
        submit.disabled = false;
        return;
      }
      let projectRoot = $("issue-project") ? $("issue-project").value.trim() : "";
      if (projectRoot === PROJECT_MANUAL_SENTINEL) {
        projectRoot = $("issue-project-manual")
          ? $("issue-project-manual").value.trim()
          : "";
      }
      if (!projectRoot || !isValidAbsolutePath(projectRoot)) {
        showFormError(errBox, tf("issueModal.errInvalidPath", "Select or enter a valid project path."));
        submit.disabled = false;
        return;
      }
      const body = buildIssueCreateBody(
        description, machineId, projectRoot,
        $("issue-title").value.trim(),
        $("issue-type").value,
        $("issue-priority").value,
        parseTagsFromString($("issue-tags").value),
      );
      resp = await authedFetch("/api/issues", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
    } else {
      const issueId = form.dataset.issueId;
      const body = buildIssueEditBody(
        description,
        form.dataset.machineId,
        form.dataset.projectRoot,
        _issueFormDirty,
        {
          title: $("issue-title").value.trim(),
          type: $("issue-type").value,
          priority: $("issue-priority").value,
          tags: parseTagsFromString($("issue-tags").value),
        },
      );
      resp = await authedFetch("/api/issues/" + encodeURIComponent(issueId), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
    }

    if (resp.ok || resp.status === 202) {
      closeIssueModal();
      showToast("success", mode === "create" ? tf("toast.issueCreated", "Issue created.") : tf("toast.issueUpdated", "Issue updated."));
      fetchIssues();
    } else {
      const detail = await resp.json().catch(() => ({}));
      showFormError(errBox, detail.detail || tf("error.serverReturned", `Server returned ${resp.status}.`, { status: resp.status }));
    }
  } catch (_) {
    showFormError(errBox, tf("error.networkReach", "Network error — could not reach the server."));
  } finally {
    submit.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// Issue close / reopen action modal
// ---------------------------------------------------------------------------

let _issueActionPending = false;

function openIssueActionModal(action, iss) {
  if (!iss) return;
  const titleNode = $("issue-action-title");
  const msgNode = $("issue-action-message");
  const reasonLabel = $("issue-action-reason-label");
  const reasonInput = $("issue-action-reason");
  const errBox = $("issue-action-error");
  errBox.classList.add("hidden");
  reasonInput.value = "";

  if (action === "close") {
    titleNode.textContent = tf("issueAction.closeTitle", "Close Issue");
    msgNode.textContent = tf("issueAction.closeMessage", "Confirm closing Issue #" + iss.id + "?", { id: iss.id });
    reasonLabel.classList.remove("hidden");
    reasonInput.classList.remove("hidden");
  } else {
    titleNode.textContent = tf("issueAction.reopenTitle", "Reopen Issue");
    msgNode.textContent = tf("issueAction.reopenMessage", "Confirm reopening Issue #" + iss.id + "?", { id: iss.id });
    reasonLabel.classList.add("hidden");
    reasonInput.classList.add("hidden");
  }

  const modal = $("issue-action-modal");
  modal.dataset.action = action;
  modal.dataset.issueId = iss.id || "";
  modal.dataset.machineId = issueMachineId(iss);
  modal.dataset.projectRoot = iss.project_root || "";
  modal.classList.remove("hidden");
}

function closeIssueActionModal() {
  $("issue-action-modal").classList.add("hidden");
  _issueActionPending = false;
}

async function confirmIssueAction() {
  if (_issueActionPending) return;
  _issueActionPending = true;
  const modal = $("issue-action-modal");
  const action = modal.dataset.action;
  const issueId = modal.dataset.issueId;
  const errBox = $("issue-action-error");
  errBox.classList.add("hidden");

  const confirmBtn = $("issue-action-confirm");
  confirmBtn.disabled = true;

  try {
    let resp;
    if (action === "close") {
      const reason = $("issue-action-reason").value.trim();
      const body = buildIssueActionBody(
        modal.dataset.machineId, modal.dataset.projectRoot, reason,
      );
      resp = await authedFetch(
        "/api/issues/" + encodeURIComponent(issueId) + "/close",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
      );
    } else {
      const body = buildIssueActionBody(
        modal.dataset.machineId, modal.dataset.projectRoot,
      );
      resp = await authedFetch(
        "/api/issues/" + encodeURIComponent(issueId) + "/reopen",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
      );
    }

    if (resp.ok || resp.status === 202) {
      closeIssueActionModal();
      showToast("success", action === "close" ? tf("toast.issueClosed", "Issue closed.") : tf("toast.issueReopened", "Issue reopened."));
      fetchIssues();
    } else {
      const detail = await resp.json().catch(() => ({}));
      showFormError(errBox, detail.detail || tf("error.serverReturned", `Server returned ${resp.status}.`, { status: resp.status }));
    }
  } catch (_) {
    showFormError(errBox, tf("error.networkReach", "Network error — could not reach the server."));
  } finally {
    confirmBtn.disabled = false;
    _issueActionPending = false;
  }
}

// ---------------------------------------------------------------------------
// Start a flow from an issue
// ---------------------------------------------------------------------------
//
// Only `open` issues can be launched from the UI (issueLaunchModel). The
// button is always rendered — for a non-open issue it stays visible but
// disabled with the reason as its tooltip, matching the "可见但置灰" contract.
// Clicking opens a small modal carrying a discovery checkbox (reusing the
// New Task start-from-discovery interaction), then dispatches POST /api/flows
// with the issue's machine/project and from_issue_id.

function isIssueLaunchInProgress(key) {
  return state.issueLaunchRequests.has(key);
}

// Build the "启动 flow" button for an issue (list row or detail pane).  When
// the issue is not open the button is rendered disabled with the reason as its
// title, so the entry is visible but greyed out.
function makeIssueLaunchButton(iss, extraClass) {
  const model = issueLaunchModel(iss);
  const btn = el("button", "ghost-btn issue-launch-btn" + (extraClass ? " " + extraClass : ""), tf("issueLaunch.button", "Launch flow"));
  btn.type = "button";
  if (!model.canLaunch) {
    btn.disabled = true;
    btn.classList.add("disabled");
    btn.title = issueLaunchReasonText(model);
  } else if (isIssueLaunchInProgress(issueCompositeKey(iss))) {
    btn.disabled = true;
    btn.title = tf("issueLaunch.dispatching", "Dispatching…");
  } else {
    btn.title = tf("issueLaunch.buttonTitle", "Launch a new flow from this issue");
  }
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (btn.disabled) return;
    openIssueLaunchModal(iss);
  });
  return btn;
}

function openIssueLaunchModal(iss) {
  if (!iss) return;
  const model = issueLaunchModel(iss);
  if (!model.canLaunch) {
    showToast("error", issueLaunchReasonText(model));
    return;
  }
  const modal = $("issue-launch-modal");
  if (!modal) return;
  const titleNode = $("issue-launch-title");
  const msgNode = $("issue-launch-message");
  const discoverInput = $("issue-launch-discover");
  const worktreeInput = $("issue-launch-worktree");
  const decompositionInput = $("issue-launch-decomposition");
  const granularityInput = $("issue-launch-granularity");
  const errBox = $("issue-launch-error");
  if (titleNode) titleNode.textContent = tf("issueLaunch.title", "Launch Flow from Issue");
  if (msgNode) {
    msgNode.textContent = tf("issueLaunch.message",
      "A new flow will be launched from Issue #" + (iss.id || "?") + " (" + issueDisplayTitle(iss) + ").",
      { id: iss.id || "?", title: issueDisplayTitle(iss) });
  }
  if (discoverInput) discoverInput.checked = false;
  if (worktreeInput) worktreeInput.checked = false;
  // Same reset-to-project-default rule as the New Task form.
  if (decompositionInput) decompositionInput.value = "";
  if (granularityInput) granularityInput.value = "";
  if (errBox) errBox.classList.add("hidden");
  modal.dataset.issueKey = issueCompositeKey(iss);
  modal.dataset.machineId = issueMachineId(iss);
  modal.dataset.projectRoot = iss.project_root || "";
  modal.dataset.issueId = iss.id || "";
  modal.classList.remove("hidden");
}

function closeIssueLaunchModal() {
  const modal = $("issue-launch-modal");
  if (modal) modal.classList.add("hidden");
}

async function confirmIssueLaunch() {
  const modal = $("issue-launch-modal");
  if (!modal) return;
  const key = modal.dataset.issueKey || "";
  if (key && isIssueLaunchInProgress(key)) return; // debounce
  const errBox = $("issue-launch-error");
  if (errBox) errBox.classList.add("hidden");

  // Find the live issue object so we send its current machine/project.
  const iss =
    (state.issues || []).find((i) => i && issueCompositeKey(i) === key) || {
      id: modal.dataset.issueId,
      machine_id: modal.dataset.machineId,
      project_root: modal.dataset.projectRoot,
    };
  const discover = Boolean($("issue-launch-discover") && $("issue-launch-discover").checked);
  const worktree = Boolean($("issue-launch-worktree") && $("issue-launch-worktree").checked);
  const planMode = readPlanModeInputs("issue-launch-decomposition", "issue-launch-granularity");
  const body = buildIssueFlowBody(iss, discover, worktree, planMode);

  const confirmBtn = $("issue-launch-confirm");
  if (confirmBtn) confirmBtn.disabled = true;
  if (key) state.issueLaunchRequests.add(key);
  renderIssuesList();

  try {
    const resp = await authedFetch("/api/flows", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (resp.status === 202) {
      closeIssueLaunchModal();
      showToast("success", tf("toast.issueFlowDispatched", "Flow dispatched from the issue."));
    } else {
      const detail = await resp.json().catch(() => ({}));
      const message = flowLaunchErrorMessage(resp.status, detail);
      if (errBox) showFormError(errBox, message);
      showToast("error", tf("toast.flowLaunchFailed", `Failed to launch flow: ${message}`, { message }));
    }
  } catch (_) {
    if (errBox) showFormError(errBox, tf("error.networkReach", "Network error — could not reach the server."));
    showToast("error", tf("toast.flowLaunchNetworkError", "Failed to launch flow — network error."));
  } finally {
    if (key) state.issueLaunchRequests.delete(key);
    if (confirmBtn) confirmBtn.disabled = false;
    renderIssuesList();
  }
}

// ---------------------------------------------------------------------------
// Record normalization
// ---------------------------------------------------------------------------
//
// Both `/api/history/{flow_id}` and the WS `history_data` push wrap every
// record as `{step_id, message: {role, content, raw_json, timestamp,
// step_type, attempt}}`. Reading `rec.role` / `rec.content` straight off the
// outer object yields `undefined` — the historical root cause of every role
// being misclassified and the body falling back to a raw-JSON dump.
//
// `normalizeRecord` is the single entry point all render paths go through: it
// unwraps the `message` envelope (tolerating a flat record with no envelope),
// and — when the textual `content` is missing — recovers a readable body from
// the assistant's final text block in `raw_json`.

// Best-effort assistant-text recovery from a parsed NDJSON stream
// (`raw_json` is `list[dict]`, one dict per NDJSON line).
//
// The streaming layer emits several legal shapes — full assistant messages,
// streaming `content_block_delta` / `message_delta` chunks, bare
// `{role:"assistant", content:"..."}` envelopes, plus tool-use blocks that the
// frontend re-renders via `renderToolMarkers`. We walk every line and collect
// any text we can find, in stream order, so the assistant bubble ALWAYS has
// something to feed the Markdown + tool-marker renderer rather than degrading
// to a raw `<pre>` of the source NDJSON.
//
// Unparsable / unknown blocks fall back to a short JSON stringify so they
// degrade gracefully into the rendered text instead of being silently dropped.
function extractAssistantText(rawJson) {
  if (!Array.isArray(rawJson) || !rawJson.length) return "";
  const parts = [];
  const pushTool = (name, input) => {
    let detail = "";
    if (input != null) {
      try { detail = JSON.stringify(input); } catch (_) { detail = String(input); }
    }
    parts.push("\n[" + (name || "Tool") + (detail ? ": " + detail : "") + "]\n");
  };
  // tool_result blocks ride the user-message channel in the NDJSON. Their
  // bracket text is *not* injected back into the assistant content string —
  // they are paired by tool_use_id with the matching tool_use marker by the
  // chip state machine (`extractAssistantChipEvents`) and only the merged
  // chip is rendered. Emitting a second bracket marker here would produce the
  // pre-G3 "zombie second chip" the running-flow console used to render.
  const extractBlocks = (blocks) => {
    if (!Array.isArray(blocks)) return;
    for (const block of blocks) {
      if (block == null) continue;
      if (typeof block === "string") { parts.push(block); continue; }
      if (typeof block !== "object") continue;
      const bt = String(block.type || "").toLowerCase();
      if (bt === "text" && typeof block.text === "string") {
        parts.push(block.text);
      } else if (bt === "tool_use") {
        pushTool(block.name, block.input);
      } else if (bt === "tool_result") {
        // paired with the matching tool_use by extractAssistantChipEvents
      } else if (typeof block.text === "string") {
        parts.push(block.text);
      }
    }
  };

  for (const line of rawJson) {
    if (line == null) continue;
    if (typeof line === "string") { parts.push(line); continue; }
    if (typeof line !== "object") { parts.push(String(line)); continue; }

    const type = String(line.type || "").toLowerCase();

    // Full assistant / message envelope: `{type:"assistant", message:{...}}`
    // or a bare `{role:"assistant", content:[...]}` / `{content:"..."}`.
    if (type === "assistant" || type === "message" ||
        (line.message && typeof line.message === "object")) {
      const msg = (line.message && typeof line.message === "object")
        ? line.message : line;
      if (Array.isArray(msg.content)) {
        extractBlocks(msg.content);
      } else if (typeof msg.content === "string") {
        parts.push(msg.content);
      }
      continue;
    }

    // Streaming deltas (Anthropic-style event-stream shapes).
    if (type === "content_block_delta" || type === "message_delta") {
      const delta = (line.delta && typeof line.delta === "object")
        ? line.delta : {};
      if (typeof delta.text === "string") parts.push(delta.text);
      else if (typeof delta.partial_json === "string") parts.push(delta.partial_json);
      continue;
    }
    if (type === "content_block_start") {
      const block = line.content_block;
      if (block && typeof block === "object") extractBlocks([block]);
      continue;
    }
    if (type === "tool_use") {
      pushTool(line.name, line.input);
      continue;
    }

    // Plain text envelopes — `{text:"..."}` / `{content:"..."}` / direct
    // `role:"assistant"` bubble.
    if (typeof line.text === "string") { parts.push(line.text); continue; }
    if (typeof line.content === "string") { parts.push(line.content); continue; }
    if (Array.isArray(line.content)) { extractBlocks(line.content); continue; }

    // Structured but unrecognized — keep a best-effort short summary rather
    // than dropping the line silently. This prevents the assistant bubble
    // from falling back to a raw NDJSON dump when an unfamiliar block type
    // shows up in the middle of an otherwise normal stream.
    if (type && type !== "message_start" && type !== "message_stop" &&
        type !== "content_block_stop" && type !== "ping") {
      try {
        const summary = JSON.stringify(line);
        if (summary && summary.length <= 400) parts.push(summary);
      } catch (_) { /* swallow */ }
    }
  }
  return parts.join("");
}

// Unwrap `{step_id, message:{...}}` into a flat, render-ready object. Falls
// back to the flat shape when no `message` envelope is present.
function normalizeRecord(rec) {
  if (!rec || typeof rec !== "object") {
    return {
      role: "log", content: rec == null ? "" : String(rec),
      timestamp: null, stepType: "", stepId: "", raw: null, attempt: null,
      agentName: null, modelName: null, ordinal: null,
    };
  }
  const msg = (rec.message && typeof rec.message === "object") ? rec.message : rec;
  const pick = (key) => (msg[key] != null ? msg[key] : rec[key]);
  // `ordinal` is the record's 0-based physical line position in its step .jsonl
  // file, injected at the envelope level by the daemon history reader
  // (daemon/history.py). It is written at append time and preserved across a
  // retry's in-place rewrite, so it is the record's STABLE identity — the basis
  // for `recordKey`'s `stepId#ordinal` key and the idempotent reconcile. Surfaced
  // here so every render/dedup path sees the same value the envelope carries.
  const ordinal = recordOrdinal(rec);

  // step_type resolution intentionally diverges from `pick` (which is
  // message-first). The daemon injects an authoritative `step_type` at the
  // record *envelope* (`rec.step_type`), parsed deterministically from the
  // jsonl file-name convention `NN_<type>_<hash>(_Gk)` — real daemon `message`
  // payloads carry no `step_type` at all. So the envelope value is preferred;
  // an inner `message.step_type` is only consulted as a backward-compatible
  // fallback for older daemons (or hand-crafted records) that lack the
  // envelope field; then empty. Using `pick` here would wrongly let a stray
  // inner field shadow the authoritative envelope value.
  const pickStepType = () => {
    if (rec.step_type != null && rec.step_type !== "") return rec.step_type;
    if (msg.step_type != null && msg.step_type !== "") return msg.step_type;
    return "";
  };

  // Step-completion events from the engine's structured event stream. They
  // ride the same conversation channel as chat history but carry the step's
  // structured outputs rather than turn text. We surface them as a non-chat
  // record so renderConversationRecord can produce the raw event chip plus a
  // default-expanded report card driven from `stepReport`.
  //
  // `step_output` events are emitted for non-terminal steps (PAUSED /
  // REVISION_NEEDED / RETRYING) that consumed tokens but have not reached a
  // terminal status. They carry the step data including `token_usage` so the
  // web console can show a per-step usage footnote. Their `kind` is
  // `"step_output"` and they render as a usage-only chip (no full report
  // card, since the step hasn't completed). A `step_completed`/`step_failed`
  // record for the same `step_id` supersedes the chip for that step.
  const eventType = String(pick("type") || "").toLowerCase();
  if (eventType === "step_completed" || eventType === "step_failed" || eventType === "step_output") {
    const data = pick("data") && typeof pick("data") === "object" ? pick("data") : {};
    const innerStep = (data.step && typeof data.step === "object")
      ? data.step
      : (msg.step && typeof msg.step === "object") ? msg.step : null;
    const stepReport = {
      step_type: pickStepType() || (innerStep && innerStep.step_type) || "",
      step_id: pick("step_id") || (innerStep && innerStep.step_id) || "",
      status: (innerStep && innerStep.status)
        || data.status
        || pick("status")
        || (eventType === "step_failed" ? "failed" : eventType === "step_output" ? "non_terminal" : "completed"),
      outputs: (innerStep && innerStep.outputs)
        || data.outputs
        || pick("outputs")
        || {},
      error_message: (innerStep && innerStep.error_message)
        || data.error_message
        || pick("error_message")
        || "",
    };
    return {
      role: "step-event",
      kind: eventType,
      content: "",
      timestamp: pick("timestamp") != null ? pick("timestamp") : pick("time"),
      stepType: stepReport.step_type,
      stepId: stepReport.step_id || pick("step_id") || "",
      stepReport: stepReport,
      raw: { raw_json: [msg], raw_ndjson: null },
      attempt: null,
      agentName: null, modelName: null, ordinal: ordinal,
    };
  }

  // `step_started` lifecycle anchor (persisted by chat_history.record_step_started
  // the moment a step enters RUNNING — including non-LLM steps TEST / COMMIT /
  // SPEC_GATE that produce no conversation records). It rides the same channel
  // as step_completed but carries NO `data.step` outputs (the step has not
  // produced anything yet), so it normalizes to a lightweight "进行中" anchor
  // with `stepReport: null`: renderStepStartedRecord shows only a status row,
  // never a report card. Its `stepId` matches the step's later chat /
  // step_output / step_completed / step_failed records, so all of them share one
  // `stepKey` and collapse into a SINGLE visual step region (no duplicate
  // same-named region is created by the terminal/intermediate events).
  //
  // `step_status` (persisted by chat_history.record_step_status) is the same
  // affordance-free anchor but for a non-terminal SETTLED state — `paused` /
  // `retrying`. It shares the step's `stepId` so it groups into the same
  // region as the `step_started` running anchor; addConversationRecords then
  // supersedes the earlier anchor so the region shows only its CURRENT state
  // (进行中 → 已暂停) instead of stacking status rows.
  //
  // `waiting_for_lock` (persisted by chat_history.record_waiting_for_lock) is
  // the same affordance-free lifecycle anchor, emitted the moment a queued
  // synchronous run begins blocking to acquire the main-worktree lock before
  // its first code-touching step. It carries a human-readable `message` but no
  // `role` / `content` / `raw_json`, so without this branch it would fall
  // through to the generic path and render as an empty "(no readable content)"
  // bubble. Treating it as a "等待锁" status anchor surfaces the flow as
  // running-and-waiting-for-lock, and — sharing the step's `stepId` — it is
  // superseded in place by the later `step_started` running anchor and the
  // terminal report once the lock is acquired and the step proceeds.
  //
  // The worktree merge no longer emits its own bypass anchor going forward: it
  // runs as the flow's merge_integrate / version_reconcile steps, which emit the
  // ordinary step_started / step_status / step_completed lifecycle anchors
  // handled here. But pre-change archived worktree flows (real old flows exist in
  // se3/history) still carry a bare legacy ``{"type":"merging"}`` row. The CLI
  // reader (chat_history.get_step_history) skips it; this daemon→webui path must
  // likewise recognise it, else it falls through to the generic role path and
  // renders as a stray empty "(no readable content)" bubble. Fold it into the
  // same affordance-free lifecycle-anchor family (a "正在 merge" status row) it
  // was rendered as before the bypass retirement.
  if (
    eventType === "step_started"
    || eventType === "step_status"
    || eventType === "waiting_for_lock"
    || eventType === "merging"
  ) {
    return {
      role: "step-event",
      kind: eventType,
      content: typeof pick("message") === "string" ? pick("message") : "",
      timestamp: pick("timestamp") != null ? pick("timestamp") : pick("time"),
      stepType: pickStepType(),
      stepId: pick("step_id") || "",
      status: String(
        pick("status")
          || (eventType === "step_started"
            ? "running"
            : eventType === "waiting_for_lock"
              ? "waiting_for_lock"
              : eventType === "merging"
                ? "merging"
                : "paused"),
      ).toLowerCase(),
      stepReport: null,
      raw: { raw_json: [msg], raw_ndjson: null },
      attempt: null,
      agentName: null, modelName: null, ordinal: ordinal,
    };
  }

  // Per-group DAG status records (written by chat_history.record_group_status
  // from the implement step's DAGScheduler lifecycle hooks). They ride the
  // conversation channel as a lightweight, time-ordered status marker — NOT a
  // chat turn and NOT a partial stream fragment — so the web console can show
  // "G3 正在 worktree 实施中" / "G1 已完成" while the parallel implement step is
  // still running, before the full per-group histories are salvaged back at
  // step end. Recognized here before the generic role path so the dedicated
  // marker renderer (renderGroupStatusRecord) can pick it up.
  const recType = String(pick("type") || "").toLowerCase();
  if (recType === "group_status") {
    return {
      role: "group-status",
      kind: "group_status",
      groupId: pick("group_id") != null ? String(pick("group_id")) : "",
      status: pick("status") != null ? String(pick("status")) : "",
      content: "",
      timestamp: pick("timestamp") != null ? pick("timestamp") : pick("time"),
      // The implement step always tags these `implement`; honor an explicit
      // envelope/inner step_type when present, else default to implement so
      // the marker groups under the IMPLEMENT step header.
      stepType: pickStepType() || "implement",
      stepId: pick("step_id") || "",
      raw: { raw_json: [msg], raw_ndjson: pick("raw_ndjson") },
      attempt: null,
      // Agent/model metadata: record_group_status (chat_history.py) attaches an
      // optional `agent_name` (the configured runner name the group's LLMCaller
      // selected for the current attempt) and, once parsed from the NDJSON
      // init/system metadata, `model_name` (the actual model). Extracted the
      // same way as the chat-record branch below so the marker can show the
      // group's live agent · model. Null for legacy records lacking the fields
      // (backward-compatible) — only displayed when present, no placeholder.
      agentName: typeof pick("agent_name") === "string" && pick("agent_name") ? pick("agent_name") : null,
      modelName: typeof pick("model_name") === "string" && pick("model_name") ? pick("model_name") : null,
      ordinal: ordinal,
    };
  }

  // Code-index update-progress records (written by chat_history.record_index_progress
  // from the commit step's code-index rebuild callback). The commit step
  // re-summarises every touched source node before staging; that rebuild is
  // otherwise invisible, so each file/dir node emits one self-contained NDJSON
  // line carrying its `path` / node `kind` (file|dir), the running `done`/`total`
  // counts and the `phase`. Like group_status they ride the conversation channel
  // as a lightweight, time-ordered status marker — NOT a chat turn — and are
  // recognized here before the generic role path so the dedicated renderer
  // (renderIndexProgressRecord) can pick them up and update ONE progress line in
  // place as the counts advance.
  if (recType === "index_progress") {
    const totalRaw = Number(pick("total"));
    const doneRaw = Number(pick("done"));
    return {
      role: "index-progress",
      kind: "index_progress",
      path: pick("path") != null ? String(pick("path")) : "",
      // The record's own `kind` field is the node kind (file|dir); keep it under
      // a distinct name so it never collides with the normalized `kind` marker
      // discriminator above.
      indexKind: pick("kind") != null ? String(pick("kind")) : "",
      done: Number.isFinite(doneRaw) ? doneRaw : 0,
      total: Number.isFinite(totalRaw) ? totalRaw : 0,
      phase: pick("phase") != null ? String(pick("phase")) : "",
      content: "",
      timestamp: pick("timestamp") != null ? pick("timestamp") : pick("time"),
      // record_index_progress always tags these `commit` (the only step that
      // rebuilds the index with a flow context); honor an explicit envelope /
      // inner step_type when present, else default to commit so the marker groups
      // under the COMMIT step header.
      stepType: pickStepType() || "commit",
      stepId: pick("step_id") || "",
      raw: { raw_json: [msg], raw_ndjson: null },
      attempt: null,
      // Index-progress markers have no LLM turn of their own, so they carry no
      // agent/model badge (each per-node summary runs its own throwaway caller).
      agentName: null,
      modelName: null,
      ordinal: ordinal,
    };
  }

  // Stream-progress records (written by record_stream_progress, daemon-read
  // and pushed BEFORE the turn's final result) are in-progress process output.
  // They carry `type:'stream_progress'` and/or `partial:true` and are always
  // assistant-role; mark them so the renderer can show them live, line by line,
  // and fold them away once the turn's final (non-partial) assistant result
  // for the same (stepId, attempt) arrives.
  const isPartial =
    pick("partial") === true ||
    String(pick("type") || "").toLowerCase() === "stream_progress";

  let role = String(pick("role") || msg.type || "log").toLowerCase();
  if (!["user", "assistant", "system"].includes(role)) {
    role = role === "human" ? "user" : (role || "log");
  }
  if (isPartial) role = "assistant";

  const rawJson = pick("raw_json");
  const rawNdjson = pick("raw_ndjson");

  let content = pick("content");
  if (typeof content !== "string" || content === "") {
    const recovered = extractAssistantText(rawJson);
    if (recovered) {
      content = recovered;
    } else if (content != null && typeof content !== "string") {
      try { content = JSON.stringify(content, null, 2); } catch (_) { content = String(content); }
    } else if (content == null) {
      content = typeof pick("text") === "string" ? pick("text") : "";
    }
  }

  // Tool chip state fields written by record_stream_progress on stream_progress
  // records: tool_use carries `tool_use_id` with `tool_detail=null` / `is_error`
  // absent (in-flight); tool_result carries the same id plus `is_error` and a
  // structured `tool_detail` payload (terminal). Expose them so the live chip
  // state machine can find/upgrade chips by id without re-parsing the bracket
  // marker text.
  const toolUseIdRaw = pick("tool_use_id");
  const toolUseId =
    typeof toolUseIdRaw === "string" && toolUseIdRaw ? toolUseIdRaw : null;
  const isErrorRaw = pick("is_error");
  const isError = isErrorRaw === true || isErrorRaw === false ? isErrorRaw : null;
  const toolDetailRaw = pick("tool_detail");
  const toolDetail =
    toolDetailRaw && typeof toolDetailRaw === "object" ? toolDetailRaw : null;

  // Per-call token usage (G5): record_response (chat_history.py) attaches a
  // `token_usage` dict — the increment for THIS LLM call, parsed from the
  // stream's `type=='result'` line — to the assistant ChatMessage. The daemon
  // forwards it inside the `message` envelope verbatim, so a final (non-partial)
  // assistant record carries the round's usage here. Partial stream fragments
  // and non-LLM records have none → null, so the per-round footnote (which
  // gates on a non-empty round usage) renders only when this turn actually
  // called the LLM. Shape mirrors `UsageTotals.to_dict()`.
  const tokenUsageRaw = pick("token_usage");
  const tokenUsage =
    tokenUsageRaw && typeof tokenUsageRaw === "object" ? tokenUsageRaw : null;

  // Agent/model metadata: record_prompt / record_response attach optional
  // `agent_name` (the configured runner name, e.g. "dclaude") and
  // `model_name` (the actual model parsed from NDJSON init/system metadata,
  // e.g. "claude-opus-4-8"). The daemon forwards them inside the `message`
  // envelope verbatim. Null for records predating these fields (backward-
  // compatible). Only displayed when present — no placeholder for missing data.
  const agentName = typeof pick("agent_name") === "string" && pick("agent_name") ? pick("agent_name") : null;
  const modelName = typeof pick("model_name") === "string" && pick("model_name") ? pick("model_name") : null;

  return {
    role: role,
    content: content,
    timestamp: pick("timestamp") != null ? pick("timestamp") : pick("time"),
    stepType: pickStepType(),
    stepId: pick("step_id") || "",
    tokenUsage: tokenUsage,
    agentName: agentName,
    modelName: modelName,
    // `envelope` carries the record's original .jsonl envelope ({step_id,
    // step_type, message} — the JSON envelope of the standardized persistence
    // layer). It is the stable data source for the user side's Layer-3 "查看原始"
    // (see `makeUserRawToggle`): a user record carries raw_json=[] and no
    // raw_ndjson, so without the envelope its Layer 3 would have nothing to
    // show. Only this generic branch sets it; the step-event / group_status /
    // partial branches construct their own `raw` and are intentionally left
    // unchanged.
    raw: { raw_json: rawJson, raw_ndjson: rawNdjson, envelope: rec },
    attempt: pick("attempt"),
    partial: isPartial,
    toolUseId: toolUseId,
    isError: isError,
    toolDetail: toolDetail,
    ordinal: ordinal,
  };
}

// The record's stable per-step line ordinal, read from the .jsonl envelope the
// daemon history reader tags (daemon/history.py). Envelope-first (that is where
// the reader injects it); a `message.ordinal` is only a defensive fallback for
// an already-unwrapped shape. Returns null for any record without a finite
// numeric ordinal — optimistic local echoes and pre-ordinal daemons — so the
// caller falls back to the legacy content key.
function recordOrdinal(rec) {
  if (!rec || typeof rec !== "object") return null;
  let o = rec.ordinal;
  if (o == null && rec.message && typeof rec.message === "object") {
    o = rec.message.ordinal;
  }
  return typeof o === "number" && Number.isFinite(o) ? o : null;
}

// The legacy coarse identity: stepId + role/kind/status/second-timestamp/attempt
// + content length + content[:96]. Used both as the fallback key for records
// with no ordinal AND, in the idempotent reconcile, as a cheap content signature
// to tell an unchanged re-delivery of a stable line apart from a retry rewrite
// of the same line (see `sameStableRecordContent`).
//
// `status` disambiguates two status anchors of the SAME step region — e.g. a
// `paused` step_status followed by a resumed `running` step_started — that share
// stepId / role (step-event) / attempt (null) / empty content and, on a
// same-second daemon resume, the same second timestamp. `kind` does the same for
// the terminal `step_completed` / `step_failed` reports vs the non-terminal
// `step_output` usage record, which all normalize to role `step-event` with no
// top-level `status` and empty content. Generic chat records carry neither field
// → a constant "undefined", so their identity is unchanged and backward-compat.
function legacyKeyFromNorm(n) {
  const content = typeof n.content === "string" ? n.content : "";
  return [
    n.stepId, n.role, String(n.kind), String(n.status), String(n.timestamp),
    String(n.attempt), content.length, content.slice(0, 96),
  ].join("");
}

// Stable identity key for a raw record. When the daemon-injected `ordinal` is
// present it is the record's TRUE stable identity — `stepId#ordinal` — written
// at append time and preserved across a retry's in-place rewrite, so the same
// logical .jsonl line always hashes identically regardless of its content or
// timestamp. This is what lets the idempotent reconcile update line N in place
// (a retry rewrote it) instead of dropping or duplicating it, and what keeps
// content-empty marker records (discovery/commit/index_progress) distinct by
// their line position rather than colliding on their shared empty content.
//
// Records without an ordinal (optimistic local echoes, pre-ordinal daemons)
// fall back to the legacy coarse content key so their behaviour is unchanged.
// The `#` separator and short length make an ordinal key un-collidable with any
// legacy key.
//
// WHY the raw `stepId` is used verbatim (never re-folded): a worktree flow's
// discovery is surfaced from several physical .jsonl files (the worktree
// primary plus a ``.from-<branch>`` merge-back sidecar), whose per-file line
// ordinals both restart at 0. The daemon reader (G1) gives each physical file a
// DISTINCT step_id, so consuming it as-is keeps `stepId#ordinal` globally unique
// across sources — the 2nd+ discovery round no longer collides with round 1 and
// so is neither dropped nor overwritten by the idempotent reconcile.
function recordKey(rec) {
  const n = normalizeRecord(rec);
  const ord = recordOrdinal(rec);
  if (ord != null && n.stepId) return n.stepId + "#" + ord;
  return legacyKeyFromNorm(n);
}

// True when two records sharing a stable `stepId#ordinal` identity carry the
// SAME rendered content — i.e. this is a plain re-delivery (REST∩WS overlap),
// not a retry that rewrote line N with new output. Compared via the legacy
// coarse signature (role/kind/status/ts/len/content[:96]), which captures a
// content or status change while treating a byte-identical re-delivery as equal.
function sameStableRecordContent(a, b) {
  return legacyKeyFromNorm(normalizeRecord(a)) === legacyKeyFromNorm(normalizeRecord(b));
}

// True when two record arrays would render identically — same length and, at
// every position, the same VISIBLE signature (legacyKeyFromNorm captures role /
// kind / status / timestamp / length / content[:96], so a content or status
// change registers as different even when the stable stepId#ordinal identity is
// unchanged by a retry rewrite). Used by the G3 periodic silent self-heal to
// skip a from-scratch DOM rebuild when the authoritative snapshot matches what is
// already held, so the 3s poll is a cheap no-op on the healthy path and repaints
// only a genuine divergence. Deliberately position-wise (not set-based): a
// reordering IS a visible change and must repaint.
function sameRenderedConversation(a, b) {
  if (a === b) return true;
  if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) {
    if (legacyKeyFromNorm(normalizeRecord(a[i])) !==
        legacyKeyFromNorm(normalizeRecord(b[i]))) {
      return false;
    }
  }
  return true;
}

// Merge a freshly-fetched snapshot with the records that arrived OR changed live
// during the fetch. The snapshot is the authoritative generation and MUST remain
// the correctness source (the whole point of the periodic full re-pull): but the
// server may have built it before a live record reached the cache, so a live
// record can legitimately carry content NEWER than the snapshot's copy of the
// same line. This folds the live records in through the idempotent ordinal
// reconcile (reconcileAppendRecords) so a genuinely-new line appends and a
// byte-identical re-delivery is a no-op — but a live record sharing a snapshot
// line's stable `stepId#ordinal` may overwrite it ONLY when it is provably newer
// (strictly later timestamp) than the snapshot's copy.
//
// The strict-newer guard is the correctness pivot. Without it the merge would
// blindly replace the snapshot line with the live copy, which regresses the view
// in the inverse race: baseline holds line 1=A, a WS append advances the held
// view to 1=B, then the server cache advances to 1=C and the REST full snapshot
// correctly carries C — but the WS frame carrying C was dropped. The live-held
// copy is still the older B; letting B overwrite the authoritative C would roll
// the conversation BACKWARD until a later poll happened to repair it. Comparing
// timestamps keeps the intended FORWARD in-place update (a live retry that raced
// ahead of a stale snapshot: B newer than A wins) while refusing the BACKWARD one
// (B older than C is dropped, snapshot stays authoritative). Ties resolve to the
// snapshot — it is the correctness source, and any truly-missed forward rewrite
// self-heals at the next full pull.
function mergeSnapshotWithLiveAppends(snapshot, liveAppends) {
  if (!liveAppends.length) return snapshot;
  const snap = Array.isArray(snapshot) ? snapshot : [];
  // Stable key -> the snapshot line's sortable timestamp, so a live rewrite of an
  // already-present ordinal can be gated on being strictly newer.
  const snapTsByKey = new Map();
  for (const r of snap) {
    if (recordOrdinal(r) == null) continue;
    const n = normalizeRecord(r);
    if (!n.stepId) continue;
    snapTsByKey.set(n.stepId + "#" + recordOrdinal(r), recordSortTs(r));
  }
  const applicable = liveAppends.filter((r) => {
    // Legacy / local-echo records (no ordinal) never collide with a snapshot's
    // stable identity — they take the ordinary append-if-absent path untouched.
    if (recordOrdinal(r) == null) return true;
    const n = normalizeRecord(r);
    if (!n.stepId) return true;
    const k = n.stepId + "#" + recordOrdinal(r);
    if (!snapTsByKey.has(k)) return true;          // a new line the snapshot lacks
    return recordSortTs(r) > snapTsByKey.get(k);   // only a strictly-newer rewrite wins
  });
  if (!applicable.length) return snapshot;
  return reconcileAppendRecords(snapshot, applicable).records;
}

// Base records that changed IN PLACE during the fetch: a stable `stepId#ordinal`
// already present in the request baseline whose rendered content advanced (a
// retry rewrote that .jsonl line). `dedupeAppendRecords` only surfaces records
// with a NEW key, so it omits an in-place rewrite entirely — and a stale full
// snapshot that still carries the pre-rewrite content would then REGRESS the
// view backward once the merge preserves nothing for that line. Surfacing the
// rewritten records here lets the idempotent snapshot merge update the matching
// snapshot line to the live content instead of dropping it. Only ordinal-keyed
// records qualify: a legacy (no-ordinal) record encodes its content in its
// recordKey, so a legacy content change already reads as a NEW key that
// `dedupeAppendRecords` picks up on the ordinary append path.
function stableInPlaceRewrites(baseline, base) {
  if (!base.length || !baseline.length) return [];
  const sigByKey = new Map();
  for (const r of baseline) {
    const n = normalizeRecord(r);
    if (recordOrdinal(r) == null || !n.stepId) continue;
    sigByKey.set(n.stepId + "#" + recordOrdinal(r), legacyKeyFromNorm(n));
  }
  if (!sigByKey.size) return [];
  const out = [];
  for (const r of base) {
    const n = normalizeRecord(r);
    if (recordOrdinal(r) == null || !n.stepId) continue;
    const k = n.stepId + "#" + recordOrdinal(r);
    if (sigByKey.has(k) && sigByKey.get(k) !== legacyKeyFromNorm(n)) out.push(r);
  }
  return out;
}

// Filter incoming append records against an existing array, returning only
// those whose recordKey is not already present. This covers the symmetric
// race direction that mergeSnapshotWithLiveAppends cannot: the HTTP snapshot
// lands AFTER the server cache write but BEFORE the WS broadcast, so the
// snapshot already contains the batch; when the same batch arrives as a
// `history_data` append moments later, blindly concating would duplicate
// every record. Deduping here prevents that. Partial / stream_progress
// records naturally have a different recordKey as their content accumulates,
// so they are never incorrectly filtered out.
//
// Invariant — a TRUE duplicate can only overlap at the TAIL. The snapshot/WS
// race and the reconnect-delta overlap both re-deliver the *most recent* batch;
// a record that arrives now can never duplicate one the operator saw long ago.
// So the `seen` set is built only from a bounded recent tail window of
// `existing`, NOT the whole array. recordKey is deliberately coarse
// (stepId+role+second-timestamp+attempt+len+content[:96]), so a genuinely-new
// record can coincidentally collide with a FAR-BACK old record — e.g. a discovery
// continuation reuses its step_id, and a repeated short reply ("1" / "按1确定")
// at the same wall-clock second hashes identically to an earlier one. Comparing
// against the whole array let that distant collision permanently suppress every
// later append sharing the key — the observed "live render stalls after respond,
// nothing shows until you leave and re-enter the view" regression. Bounding to
// the tail keeps full coverage of the real overlap while ensuring a distant old
// record can never压制 a new one. The window spans the incoming batch plus a
// safety baseline; when `existing` is shorter than the window the whole array is
// compared, identical to the prior behavior.
const DEDUPE_TAIL_BASELINE = 64;
function dedupeAppendRecords(existing, incoming) {
  if (!incoming.length || !existing.length) return incoming;
  const windowLen = Math.max(incoming.length, DEDUPE_TAIL_BASELINE) + incoming.length;
  const start = existing.length > windowLen ? existing.length - windowLen : 0;
  const seen = new Set();
  for (let i = start; i < existing.length; i++) seen.add(recordKey(existing[i]));
  return incoming.filter((r) => !seen.has(recordKey(r)));
}

// Idempotent reconcile of an append batch into the records a view already holds.
// This is the正确性 core of the right-side "chat that stops advancing" fix: an
// increment can now arrive any number of times and always converges to the same
// result, because a record's identity is its stable `stepId#ordinal` (not a
// content+timestamp key that a marker record or a retry rewrite would break).
//
// Three outcomes per incoming record, keyed by `recordKey`:
//   * stable identity already held → the SAME logical .jsonl line. A retry can
//     rewrite line N with new content, so update it IN PLACE (converge to the
//     newest); a byte-identical re-delivery is a no-op. Never a second bubble —
//     this is what unfreezes the discovery region whose step_output line is
//     rewritten each PAUSE→resume round, and keeps the commit `index_progress`
//     card updating in place through the whole rebuild.
//   * new identity → appended at the tail.
//   * legacy (no-ordinal) record → the pre-ordinal bounded-tail dedup is kept
//     verbatim: a key matching only a FAR-BACK record is a coincidental
//     collision that must still append (the "stall after respond" fix), a key
//     matching within the recent tail is a true duplicate and is dropped. Legacy
//     records (optimistic local echoes) never take the in-place path.
//
// Returns { records, fresh, rebased, updatedInPlace, changed }:
//   * records        — the reconciled array to adopt (same ref when unchanged).
//   * fresh          — the genuinely-new records, in incoming order (the caller
//                      may re-order these for a timestamp-correct delta render).
//   * rebased        — `existing` with in-place updates applied but WITHOUT the
//                      fresh tail (so a caller can merge `fresh` by timestamp).
//   * updatedInPlace — an already-held bubble's content changed, so a tail-only
//                      incremental render is unsafe and the caller must rebuild.
//   * changed        — records !== existing (an in-place update and/or an append).
function reconcileAppendRecords(existing, incoming) {
  const base = Array.isArray(existing) ? existing : [];
  const inc = Array.isArray(incoming) ? incoming : [];
  if (!inc.length) {
    return { records: base, fresh: [], rebased: base, updatedInPlace: false, changed: false };
  }
  const idxByKey = new Map();
  // Last occurrence wins, so an in-place update targets the most recent copy of
  // a key (matches the daemon's own last-write-wins for a rewritten line).
  for (let i = 0; i < base.length; i++) idxByKey.set(recordKey(base[i]), i);

  // Legacy bounded-tail window: only a duplicate within this recent slice is a
  // true re-delivery; a match further back is a coincidental coarse-key
  // collision and must still append (preserves the pre-ordinal behaviour).
  const windowLen = Math.max(inc.length, DEDUPE_TAIL_BASELINE) + inc.length;
  const tailStart = base.length > windowLen ? base.length - windowLen : 0;

  let out = null;                       // lazily cloned base on first in-place edit
  let updatedInPlace = false;
  const fresh = [];
  const freshIdxByKey = new Map();
  for (const rec of inc) {
    const k = recordKey(rec);
    const stable = recordOrdinal(rec) != null;
    if (idxByKey.has(k)) {
      const at = idxByKey.get(k);
      if (stable) {
        // Same logical line — converge to newest content; skip a no-op re-deliver.
        if (!sameStableRecordContent(base[at], rec)) {
          if (!out) out = base.slice();
          out[at] = rec;
          updatedInPlace = true;
        }
        continue;
      }
      if (at >= tailStart) continue;    // legacy true tail duplicate — drop
      // legacy far-back collision — fall through to append
    }
    if (freshIdxByKey.has(k)) {
      // The same key appears twice within this one batch: converge a stable
      // identity to its newest copy, drop a legacy duplicate.
      if (stable) fresh[freshIdxByKey.get(k)] = rec;
      continue;
    }
    freshIdxByKey.set(k, fresh.length);
    fresh.push(rec);
  }
  const rebased = out || base;
  const records = fresh.length ? rebased.concat(fresh) : rebased;
  return {
    records,
    fresh,
    rebased,
    updatedInPlace,
    changed: updatedInPlace || fresh.length > 0,
  };
}

// Defensive guard against a merged history snapshot that carries the SAME
// discovery step's records twice. The worktree-mode read path can surface a
// flow whose history is split across the main-repo root (where discovery ran
// before the worktree fork) and the worktree root (the later steps plus its own
// copy of discovery). The daemon merges the two roots and de-dups at the
// physical step-file layer (see daemon/history.py), so in the normal case no
// duplicate ever reaches the client. This is a belt-and-suspenders frontend
// backstop: if a duplicate discovery record nonetheless arrives in a `mode:
// full` snapshot — which the `mergeHistoryResponse` full path adopts wholesale
// and which `dedupeAppendRecords` (append-only) does NOT cover — it would
// render as a doubled discovery bubble. This pass drops a discovery record
// whose `recordKey` was already seen EARLIER in the same snapshot ONLY when the
// earlier record carried byte-identical content — a genuine clone. It keeps the
// first occurrence and preserves order. It is scoped strictly to discovery
// records: every non-discovery record passes through untouched (so the later
// analyze/plan/implement steps and the recordKey identity / incremental cursor
// of the rest of the conversation are unchanged), and when nothing is dropped
// the original array reference is returned so the common path is a no-op.
//
// WHY the content compare (not a bare `recordKey` de-dup): a worktree flow's
// discovery is split across physical .jsonl files (the worktree primary plus a
// ``.from-<branch>`` merge-back sidecar). G1's daemon reader gives each file a
// distinct step_id so `stepId#ordinal` stays globally unique — but this guard
// must not depend on that holding. If two discovery records ever collide on
// `recordKey` yet carry DIFFERENT content they are NOT clones (the pathological
// ordinal reuse that erased the 2nd+ discovery round pre-G1); dropping the
// later one would silently lose a legitimate round. Comparing content first
// keeps a true clone collapsed to one bubble while never deleting a distinct
// record that merely reuses a physical ordinal.
function dedupeSnapshotDiscovery(records) {
  if (!Array.isArray(records) || records.length < 2) return records;
  // recordKey -> the set of content signatures already kept for that key.
  const seen = new Map();
  let dropped = false;
  const out = [];
  for (const rec of records) {
    let norm = null;
    try { norm = normalizeRecord(rec); } catch (_) { norm = null; }
    const isDiscovery =
      !!norm && String(norm.stepType || "").toLowerCase() === "discovery";
    if (isDiscovery) {
      const key = recordKey(rec);
      const sig = legacyKeyFromNorm(norm);
      const sigs = seen.get(key);
      if (sigs) {
        if (sigs.has(sig)) { dropped = true; continue; }  // byte-identical clone
        sigs.add(sig);
      } else {
        seen.set(key, new Set([sig]));
      }
    }
    out.push(rec);
  }
  return dropped ? out : records;
}

// --- cursor completeness self-check (numbered backfill) ---------------------
//
// WHY this layer exists at all: the progress token's offset is the SERVER's
// self-signed receipt of what it SENT, and nothing ever reconciles it against
// what the client KEPT. A client that joined the stream late (the `/api/auth/me`
// 401 login gate holds the WebSocket shut, so it sees no push at all until it
// authenticates) or that lost a single frame holds a TAIL of the bundle while
// the receipt already reads "fully delivered" — every later poll is answered
// `not_modified`, and the hole is welded in permanently (the live head-loss
// defect: cursor said 2 records, the console held 1, and no code path could ever
// notice). The bundle's `cursor` — its own per-step-file record COUNTS — is the
// only signal that describes the bundle's CONTENT rather than the server's
// belief about the client, so completeness is decided against the cursor, by
// checking that every `stepId#ordinal` in 0..n-1 is actually held.

//: Must match the server's MISSING_MAX_ORDINALS (src/se3/server/app.py) — a
//: longer list is rejected there, so encoding one would waste a round trip.
const MISSING_MAX_ORDINALS = 200;
//: Per (view, flow, bundle generation): how many numbered backfills to try
//: before escalating to one full re-pull, and then giving up. WHY a cap: when
//: the server bundle ITSELF lacks the number (a record the daemon never
//: reported), the self-check can never be satisfied and an uncapped reconcile
//: would fire one request per poll, forever.
const MAX_BACKFILL_ATTEMPTS = 2;

// Map a cursor key (a physical history filename) to the step id the records
// carry. Mirrors the daemon's `_display_step_id`: strip at `.jsonl`, but KEEP a
// `.from-<branch>` merge-back sidecar marker, because the daemon gives each
// physical file a distinct step id so their per-file ordinals cannot collide.
function stepIdFromCursorKey(filename) {
  if (typeof filename !== "string") return "";
  const idx = filename.indexOf(".jsonl");
  if (idx < 0) return filename;
  const stem = filename.slice(0, idx);
  const suffix = filename.slice(idx + ".jsonl".length);
  const m = /^\.from-(.+)$/.exec(suffix);
  return m ? `${stem}.from-${m[1]}` : stem;
}

// Compare the records a view HOLDS against the counts the bundle's `cursor`
// declares, and report exactly which record numbers are absent.
//
// `unfillable` (optional) is `{ stepId: [ordinal…] }` — numbers the SERVER has
// already answered are absent from its bundle. They are excluded from `missing`:
// the cursor counts PHYSICAL LINES (a blank / unparseable line advances it
// without producing a record, and a read resumed at a non-zero base never emits
// the lines below it), so a number under the cursor need not name a record at
// all. Re-asking for one on every signal — and re-pulling the whole bundle when
// the ask fails — is exactly the request storm the numbering was meant to avoid.
//
// `pending` (optional) is `{ stepId: [ordinal…] }` — numbers the SERVER's cursor
// DECLARES but has not yet received from the daemon (still streaming; the live
// backlog crossing many short-lived daemon↔server windows). They are excluded
// from `missing` for the OPPOSITE reason to `unfillable`: not because the bundle
// will never hold them (permanent), but because it does not hold them YET
// (transient). Asking for a pending number yields nothing — it is not in the
// bundle to slice — and re-asking every signal is the same request storm, so a
// pending gap must neither drive a backfill nor tip the self-check into its
// giving-up terminal state. It is reported via `pendingGap` so the caller can
// tell "waiting on the daemon → keep the panel rendering, stay armed" apart from
// "fully in sync". When the daemon delivers the pending window the cursor
// advances and the next self-check settles, with no user intervention.
//
// Returns `{ missing, surplus, unkeyable, pendingGap }`:
//   * `missing`   — `{ stepId: [ordinal…] }`, the numbers in 0..n-1 the client
//                   does not hold and the server has declared neither unfillable
//                   NOR pending (a step file it holds NOTHING of yields the whole
//                   0..n-1 range, minus any pending/unfillable numbers).
//   * `surplus`   — the client holds MORE records for a step than the cursor
//                   claims exist (or one numbered at/after n). The numbering no
//                   longer describes the same bundle, so a numbered backfill
//                   would be meaningless — the caller must re-pull in full.
//   * `unkeyable` — a step covered by the cursor holds a record with no ordinal
//                   (a pre-ordinal daemon). Such a record is not addressable by
//                   number, so the numbered self-check is not a sound
//                   completeness test here and the caller must simply SKIP it —
//                   falling back to the token-only behaviour. It must NOT force a
//                   full re-pull: the condition holds for every frame the flow
//                   ever pushes, which would trade one cheap delta poll for one
//                   whole-bundle download per streamed record.
//   * `pendingGap`— at least one cursor gap fell in the server-declared `pending`
//                   window (still streaming from the daemon). Excluded from
//                   `missing`, so a pending-only gap yields an empty `missing`;
//                   the caller uses this flag to keep rendering + stay armed
//                   rather than treat the empty `missing` as "fully complete".
// Pure / DOM-free. Optimistic local echoes are excluded from the held set (they
// belong to no server bundle, carry no ordinal, and would otherwise read as
// either surplus or un-numbered records), as are records whose step is absent
// from the cursor.
function findMissingOrdinals(records, cursor, unfillable, pending) {
  const missing = {};
  let surplus = false;
  let unkeyable = false;
  let pendingGap = false;
  if (!cursor || typeof cursor !== "object") {
    return { missing, surplus, unkeyable, pendingGap };
  }
  const held = new Map();     // stepId -> { ords: Set, count, legacy: bool }
  for (const rec of (Array.isArray(records) ? records : [])) {
    if (rec && rec.__localEcho) continue;
    let stepId = "";
    try { stepId = normalizeRecord(rec).stepId || ""; } catch (_) { stepId = ""; }
    if (!stepId) continue;
    let entry = held.get(stepId);
    if (!entry) {
      entry = { ords: new Set(), count: 0, legacy: false };
      held.set(stepId, entry);
    }
    entry.count += 1;
    const ord = recordOrdinal(rec);
    if (ord == null) entry.legacy = true;
    else entry.ords.add(ord);
  }
  const known = (unfillable && typeof unfillable === "object") ? unfillable : {};
  const waiting = (pending && typeof pending === "object") ? pending : {};
  for (const key of Object.keys(cursor)) {
    const total = cursor[key];
    if (typeof total !== "number" || !Number.isInteger(total) || total < 0) continue;
    const stepId = stepIdFromCursorKey(key);
    if (!stepId) continue;
    const entry = held.get(stepId) || { ords: new Set(), count: 0, legacy: false };
    if (entry.legacy) unkeyable = true;
    const retired = new Set(Array.isArray(known[stepId]) ? known[stepId] : []);
    // Numbers the server says are still in flight from the daemon: excluded
    // from `missing` (asking for them serves nothing) but flagged so the caller
    // keeps the panel live and the self-check armed instead of giving up.
    const declaredPending = new Set(Array.isArray(waiting[stepId]) ? waiting[stepId] : []);
    if (entry.count > total) surplus = true;
    const gaps = [];
    for (let i = 0; i < total; i++) {
      if (entry.ords.has(i) || retired.has(i)) continue;
      if (declaredPending.has(i)) { pendingGap = true; continue; }
      gaps.push(i);
    }
    for (const ord of entry.ords) {
      if (ord >= total) surplus = true;
    }
    if (gaps.length) missing[stepId] = gaps;
  }
  return { missing, surplus, unkeyable, pendingGap };
}

// Encode a `{ stepId: [ordinal…] }` map into the `missing=` wire form the
// history endpoint parses: `stepId:ord,ord;stepId:ord`. Returns null when there
// is nothing to ask for, or when the list exceeds the server's ordinal cap — the
// caller then re-pulls in full rather than sending a request the server would
// reject.
function encodeMissingParam(missing) {
  if (!missing || typeof missing !== "object") return null;
  const groups = [];
  let total = 0;
  for (const stepId of Object.keys(missing)) {
    const ords = missing[stepId];
    if (!Array.isArray(ords) || !ords.length) continue;
    total += ords.length;
    if (total > MISSING_MAX_ORDINALS) return null;
    groups.push(`${stepId}:${ords.join(",")}`);
  }
  return groups.length ? groups.join(";") : null;
}

// Build the `GET /api/history/{flow_id}` URL, appending the opaque progress
// token as the `after` query parameter when one is held so the server can
// serve an incremental delta. With no progress (first open / after an
// invalidation) the bare URL is returned, so the request is an unconditional
// full snapshot — the first-open behaviour is unchanged. The token is encoded
// via URLSearchParams so a base64url token (`-` / `_` / `=`) is transmitted
// safely. Shared by both the running-flow and history-detail reconnect loaders.
//
// `missing` (optional) is the encoded record-number list a cursor self-check
// found absent; the server answers it with `delivery:"backfill"` — exactly those
// records, taken from the SAME bundle the held token pins. It therefore only
// travels alongside a live token, for the same reason `sig` does: the numbers
// are only meaningful within the generation the token binds.
function historySnapshotUrl(flowId, progress, signature, missing) {
  const base = `/api/history/${encodeURIComponent(flowId)}`;
  if (!progress) return base;
  const params = new URLSearchParams();
  params.set("after", progress);
  // The signature rides ALONGSIDE the token (never alone): the server only
  // answers `not_modified` when BOTH the token is in-sync AND the signature
  // matches, so a `sig` without an `after` offset is meaningless. Omitted when
  // none is held so a token-only reconnect still gets a delta/full.
  if (signature) params.set("sig", signature);
  if (missing) params.set("missing", missing);
  return `${base}?${params.toString()}`;
}

// The sortable epoch-ms timestamp of a record, mirroring the `__convTs` key the
// conversation renderer assigns (`tsValue(normalizeRecord(rec).timestamp)`).
// Used by the delta merge to detect when freshly-arrived gap records are older
// than records already held in the array's tail. DOM-free; null/unparseable
// timestamps degrade to 0 (the same floor `tsValue` uses).
function recordSortTs(rec) {
  let norm = null;
  try { norm = normalizeRecord(rec); } catch (_) { norm = null; }
  return tsValue(norm && norm.timestamp);
}

// Stable merge of two record arrays into one ordered by `(timestamp, source,
// index)`. The timestamp is the primary key the conversation renderer orders
// bubbles by. The equal-timestamp tie-break splits the `held` records into two
// classes using `requestBaseline` (the array held when the request started):
//
//   * src=0 — held records ALREADY in `requestBaseline`. These were delivered
//     before the request's progress offset, so the server bundle places them
//     authoritatively BEFORE any `fresh` delta record (the delta is exactly the
//     records after that offset). They MUST keep their earlier position on a
//     tie — e.g. a baseline record A at ts=3 stays before a later delta record
//     B at ts=3, matching the server order A then B.
//   * src=1 — the REST delta (`fresh`) records: the authoritative outage-window
//     gap, after the baseline but before any record that arrived during the
//     request.
//   * src=2 — held records that arrived DURING the request (a WS append not in
//     `requestBaseline`). On a tie these sort AFTER the delta, so a turn's
//     earlier REST partial precedes its final pushed over WS at the same
//     timestamp, and the position-based partial-segment grouping pairs them in
//     one segment instead of stranding a stale streaming bubble.
//
// When `requestBaseline` is omitted every held record is treated as a live
// append (src=2), so the delta still wins ties against held records — the
// pre-baseline-aware behaviour. Each class keeps its own internal order via its
// own index tiebreak. DOM-free; used when delta gap records must be interleaved
// with held tail records rather than appended after them.
function stableMergeByTimestamp(held, fresh, requestBaseline) {
  const baselineSet = new Set(
    Array.isArray(requestBaseline) ? requestBaseline : [],
  );
  const tagged = held
    .map((rec, idx) => ({
      rec,
      src: baselineSet.has(rec) ? 0 : 2,
      idx,
      ts: recordSortTs(rec),
    }))
    .concat(
      fresh.map((rec, idx) => ({ rec, src: 1, idx, ts: recordSortTs(rec) })),
    );
  return tagged
    .sort((a, b) => (a.ts - b.ts) || (a.src - b.src) || (a.idx - b.idx))
    .map((d) => d.rec);
}

// Fold a `GET /api/history/{flow_id}` response into the records a view already
// holds, picking the merge strategy from the server's `delivery` tag. This is
// the single shared decision point for both the running-flow view and the
// history-detail view; it is DOM-free and side-effect-free so it can be unit
// tested directly.
//
// `existing` is the array currently held by the view after the fetch await.
// `requestBaseline` is the array held when the request started. The difference
// between them is the only client-side data proven to have arrived during the
// request, and therefore the only data safe to preserve across a full fallback
// that invalidates the previous cache generation.
//
// Returns `{ records, progress, signature, cursor, generation, render }` where:
//   * `generation`— the lifecycle id of the bundle `cursor` describes (or null
//                  when the reply carried none / its cursor was withheld). The
//                  self-check scopes its per-bundle repair state to it.
//   * `records`  — the merged array the caller should adopt.
//   * `progress` — the response's fresh progress token (a string, or null when
//                  the response carried none) for the caller to store and echo
//                  on its next reconnect.
//   * `signature`— the response's bundle content signature (a string, or null),
//                  for the caller to store and echo as `?sig=` so an unchanged
//                  bundle can be answered `not_modified` next time.
//   * `cursor`   — the response's authoritative per-step-file record counts (or
//                  null when it carried none), returned on EVERY branch so the
//                  caller can run its completeness self-check. WHY it must
//                  travel even on `not_modified`: the progress token proves only
//                  what the server SENT, never what the client KEPT, so
//                  `not_modified` is no longer evidence of being in sync — it
//                  merely means the caller has nothing to REPAINT. Completeness
//                  is decided by the caller against this cursor
//                  (`reconcileCursorCompleteness`), and a hole found there is
//                  healed by a numbered backfill.
//   * `render`   — how the caller should paint the result:
//       - "delta": an incremental delivery whose new records were appended
//         (after `dedupeAppendRecords` filtered out anything already held);
//         the caller MAY render incrementally (append-only).
//       - "noop":  nothing to repaint — either a `not_modified` reply (the
//         client was provably in sync) or a delta that, after dedup, added
//         nothing. The held array is returned unchanged (same reference) so the
//         caller can skip both the state swap and the render.
//       - "full":  a full delivery (or any non-"delta" tag — the safe default);
//         the server records are the new authority, with only live appends that
//         arrived during the fetch preserved, and the caller MUST rebuild the
//         conversation.
// A rejected empty-full frame (see the #287 guard below) additionally sets
// `preserveTokens: true`, meaning the caller must KEEP the token/signature it
// already holds rather than adopting this (null) pair — the frame is discarded
// wholesale, so nothing about the held generation changed.
function mergeHistoryResponse(response, existing, requestBaseline) {
  const merged = mergeHistoryDelivery(response, existing, requestBaseline);
  // The bundle generation travels WITH the cursor, never apart from it: the two
  // are one statement ("this bundle holds these counts"), and the self-check
  // scopes its repair budget and its retired numbers to it. A frame whose cursor
  // was withheld (the #287 empty-full rejection) therefore carries no generation
  // either — there is nothing to key against.
  merged.generation = (merged.cursor && response
    && Number.isInteger(response.generation)) ? response.generation : null;
  // The bundle's server-declared pending window (cursor DECLARES these ordinals
  // but its records have not caught up — still streaming from the daemon). Rides
  // on EVERY delivery so the caller's completeness self-check can tell a cursor
  // gap that is "still coming" apart from a real hole; null when the response
  // carried none (a legacy server / a rejected frame). Kept even on `noop`, for
  // the same reason `cursor` is — the self-check runs against it there too.
  merged.pending = (response && response.pending && typeof response.pending === "object")
    ? response.pending : null;
  // The server could not bind the signed cursor we presented to the current
  // bundle (expired / rotated after a daemon reconnect) and fell back to a
  // recoverable full; its progress/signature/generation are authoritative. A
  // frame the merge REJECTED wholesale (#287 empty-full) is exempt — its
  // tokens are null and must not be adopted, so a resync off it would strand the
  // held cursor at null and force a needless full next poll.
  merged.resync = !!(response && response.resync) && !merged.preserveTokens;
  return merged;
}

function mergeHistoryDelivery(response, existing, requestBaseline) {
  const base = Array.isArray(existing) ? existing : [];
  const baseline = Array.isArray(requestBaseline) ? requestBaseline : base;
  const records = (response && Array.isArray(response.records))
    ? response.records : [];
  const progress = (response && typeof response.progress === "string")
    ? response.progress : null;
  const signature = (response && typeof response.signature === "string")
    ? response.signature : null;
  const cursor = (response && response.cursor && typeof response.cursor === "object")
    ? response.cursor : null;
  if (response && response.delivery === "not_modified") {
    // The server has nothing further to SEND (the echoed token's offset equals
    // its record count and the signature matched), so there is nothing to
    // repaint — the extra-small idle-poll reply that keeps the periodic
    // self-heal from re-shipping the whole bundle. WHY this is no longer taken
    // as proof the client is in SYNC: the token's offset is the server's own
    // receipt of what it sent, so a client that never received (or never kept) a
    // record is answered `not_modified` forever and its hole never heals — the
    // live head-loss defect. The caller must still self-check the returned
    // `cursor` against the records it holds; `noop` now means only "nothing to
    // render".
    return { records: base, progress, signature, cursor, render: "noop" };
  }
  if (response && response.delivery === "backfill") {
    // The numbered records this client's cursor self-check found it lacked,
    // taken from the SAME bundle its token pins (plus that token's tail). They
    // are by definition NOT tail records — the head/middle of the conversation —
    // so an append render cannot place them: fold them in by timestamp (the
    // reconcile de-dups by `stepId#ordinal`, making a repeated backfill of the
    // same number idempotent) and rebuild in full.
    const rec = reconcileAppendRecords(base, records);
    if (!rec.changed) {
      return { records: base, progress, signature, cursor, render: "noop" };
    }
    return {
      records: rec.fresh.length
        ? stableMergeByTimestamp(rec.rebased, rec.fresh, requestBaseline)
        : rec.rebased,
      progress,
      signature,
      cursor,
      render: "full",
    };
  }
  if (response && response.delivery === "delta") {
    // Idempotent reconcile: `fresh` are the genuinely-new lines to place by
    // timestamp; an existing `stepId#ordinal` that a retry rewrote is updated in
    // place inside `rebased`. `dedupeAppendRecords` (still used for legacy
    // no-ordinal records inside the reconcile) alone would DROP such a rewrite,
    // stalling the delta path until a full re-pull; the reconcile applies it.
    const rec = reconcileAppendRecords(base, records);
    if (!rec.changed) {
      // Every delta record is a byte-identical re-delivery of a line already
      // held (e.g. the WS append for the same batch beat the snapshot in).
      // Nothing to render.
      return { records: base, progress, signature, cursor, render: "noop" };
    }
    const fresh = rec.fresh;
    if (rec.updatedInPlace) {
      // A held line's content changed under an unchanged ordinal — the tail-only
      // delta render cannot repaint a mid-list bubble, so fold any fresh records
      // into the rebased array by timestamp and force a full rebuild.
      return {
        records: fresh.length
          ? stableMergeByTimestamp(rec.rebased, fresh, requestBaseline)
          : rec.rebased,
        progress,
        signature,
        cursor,
        render: "full",
      };
    }
    // The delta carries the outage-window gap records. While this fetch was in
    // flight a live WS `history_data` append (which does NOT bump the fetch
    // epoch) can have landed newer records into the held array's tail — e.g. a
    // still-streaming turn's final result. Blindly tail-appending these older
    // gap records would put them AFTER those newer records, inverting array
    // order. insertBubbleSorted tolerates that (it is timestamp-keyed), but the
    // strictly position-based partial-segment grouping (partialSegments) and
    // supersede logic (markSupersededProgress) do not: a partial fragment that
    // sits after its turn's final in the array is grouped into a phantom later
    // segment and never superseded, leaving a stale accumulating streaming
    // bubble next to the turn's already-rendered final result (the turn shown
    // twice), and accumulateRoundUsageByStep's positional cumulative usage is
    // computed against the inverted order. So when any fresh record is older
    // than the held tail, merge by (timestamp, index) and force a full rebuild
    // instead of a tail append, so the records array preserves stream order and
    // the position-based grouping agrees with the real turn structure.
    //
    // The tail comparison is STRICT (`>`): a fresh record whose timestamp
    // merely *equals* the held tail is NOT append-safe. The held tail can be a
    // WS final that arrived later during this request, while the equal-timestamp
    // fresh record is its earlier REST partial; tail-appending it would place
    // the partial after the final (the same inversion as a strictly-older
    // record). Only records strictly newer than the held tail can be safely
    // appended; an equal-timestamp record falls through to the stable merge,
    // which orders the REST delta before the held WS record on a tie.
    const tailTs = base.length
      ? recordSortTs(base[base.length - 1]) : -Infinity;
    const inOrder = fresh.every((r) => recordSortTs(r) > tailTs);
    if (inOrder) {
      return { records: base.concat(fresh), progress, signature, cursor, render: "delta" };
    }
    return {
      // Pass the RAW `requestBaseline` (not the `base`-defaulted `baseline`) so
      // the merge can keep equal-timestamp records that predate the request in
      // their authoritative early position, while ordering only records that
      // arrived during the request after the REST delta. When the caller omits
      // it, every held record is treated as a live append (delta wins ties).
      records: stableMergeByTimestamp(base, fresh, requestBaseline),
      progress,
      signature,
      cursor,
      render: "full",
    };
  }
  // WHY (#287): a `delivery:"full"` frame carrying ZERO records must never be
  // allowed to erase an already-rendered conversation. The worktree self-heal
  // re-pull can hand the server an empty full snapshot when the daemon fails to
  // resolve the flow's history directory ("no dir" surfacing as "no records"),
  // and adopting it wholesale blanked the whole chat pane — including the first
  // discovery round that had rendered correctly. The daemon (G3) and the server
  // cache (G2) each refuse to produce such a frame; this is the last layer of
  // that defence, so no single failure upstream can reach the reader as an empty
  // chat. Treated as a no-op: the held records, DOM, progress token and
  // signature all stand (`preserveTokens` tells the caller not to adopt this
  // frame's cursor, which pins an empty bundle). A genuinely empty flow (nothing
  // held yet) still renders its empty state — only a REGRESSION to zero is
  // rejected, never a first paint.
  if (!records.length && base.length) {
    return {
      records: base,
      progress: null,
      signature: null,
      // The frame is discarded wholesale, so its cursor — which describes the
      // rejected EMPTY bundle — must not reach the completeness self-check
      // either: checking against it would read every held record as surplus and
      // trigger a pointless full re-pull of the very bundle we just refused.
      cursor: null,
      render: "noop",
      preserveTokens: true,
    };
  }
  // Full (or unrecognised) delivery invalidates the previous generation.
  // Discard every baseline record, preserving only records that appeared while
  // this request was in flight and are not already in the new snapshot.
  const liveAppends = dedupeAppendRecords(baseline, base);
  // ...plus records the WS path rewrote IN PLACE during the fetch (a retry
  // advancing an already-held `stepId#ordinal`). `dedupeAppendRecords` filters
  // these out — their key already exists in the baseline — so without them a
  // stale full snapshot that still holds the pre-rewrite content would regress
  // the view backward. The idempotent snapshot merge below updates the matching
  // snapshot line to this live content in place ONLY when the live copy is
  // strictly newer than the snapshot's (so the reverse race — a dropped WS frame
  // whose newer content the snapshot already carries — keeps the authoritative
  // snapshot line); they never double-count with `liveAppends` (disjoint:
  // in-baseline vs new key).
  const liveRewrites = stableInPlaceRewrites(baseline, base);
  // Pending optimistic local echoes (`__localEcho`) are client-only UI state,
  // NOT part of any server cache generation, so they must survive a full
  // (generation-replacing) fallback. They were spliced into the array before
  // this request started, so they live in the baseline and are therefore
  // filtered out of `liveAppends` above; without re-adding them a user's
  // just-sent reply would visibly disappear until the daemon writes its
  // authoritative record at the next step boundary. Re-include any echo still
  // held in the current array that isn't already among the live appends. The
  // caller's downstream `reconcileLocalEchoes` then removes each echo once the
  // new snapshot carries its own authoritative copy — exactly matching the
  // old full-reload behaviour.
  const pendingEchoes = base.filter(
    (r) => r && r.__localEcho && liveAppends.indexOf(r) === -1,
  );
  // In-place rewrites go FIRST: the reconcile applies them as mid-list updates
  // (no tail effect), so the appended tail order stays [liveAppends, echoes] —
  // matching the prior behaviour.
  const preserved = liveRewrites.concat(liveAppends, pendingEchoes);
  return {
    // Collapse any duplicate discovery record the merged worktree+main snapshot
    // may carry (defensive backstop — the daemon already de-dups at the file
    // layer) before folding in preserved live appends, so a split-root flow
    // never shows a doubled discovery bubble on the full-replace path.
    records: mergeSnapshotWithLiveAppends(dedupeSnapshotDiscovery(records), preserved),
    progress,
    signature,
    cursor,
    render: "full",
  };
}

// --- the completeness self-check entry point --------------------------------
//
// Run after EVERY history signal — each REST reply folded by
// `mergeHistoryResponse` (including a `not_modified`, whose whole point used to
// be "do nothing") and each WS `history_data` / `history_cursor` frame, both of
// which now carry the post-frame cursor. It compares the records the view holds
// against the bundle's own per-file counts and repairs any hole by asking for
// exactly the numbers it lacks.
//
// WHY the repair is a NUMBERED backfill rather than a full re-pull: the full
// bundle of a long flow is megabytes, the server throttles full pulls, and the
// hole is usually one record — so a full re-pull is the wrong instrument for the
// common case. It stays as the fallback for the cases where the NUMBERING itself
// cannot be trusted (surplus / legacy un-numbered records / no token to pin a
// generation / the backfill budget spent).

function viewIsCurrent(view, flowId) {
  return view === "flow"
    ? state.selectedFlowId === flowId
    : state.selectedHistoryId === flowId;
}

function heldHistoryRecords(view) {
  return view === "flow" ? state.flowConversationRecords : state.historyRecords;
}

// Adopt a repair reply's records into `view` and repaint from scratch. A
// backfill lands head/middle records, so the cheap append render never applies.
function commitBackfillResult(view, flowId, result) {
  if (!result.preserveTokens) {
    if (view === "flow") {
      state.flowConversationProgress = result.progress;
      if (result.signature != null) state.flowConversationSignature = result.signature;
    } else {
      state.historyProgress = result.progress;
      if (result.signature != null) state.historySignature = result.signature;
    }
  }
  if (result.render === "noop") return;
  if (view === "flow") {
    const container = $("flow-conversation");
    const stick = isNearBottom(container);
    state.flowConversationRecords = reconcileLocalEchoes(result.records);
    container.__convState = null;
    renderConversation(container, state.flowConversationRecords, false);
    refreshFlowStickyHeader();
    updateFlowUsageBadge(state.flowConversationRecords);
    if (stick) scrollFlowConversationToBottom();
  } else {
    const stick = isNearBottom(historyScrollContainer());
    state.historyRecords = result.records;
    renderHistoryRecords(flowId, state.historyRecords, false);
    refreshHistoryStickyHeader();
    updateHistoryUsageBadge(state.historyRecords);
    if (stick) scrollHistoryToBottom();
  }
}

// Drop the held token/signature and re-pull the whole bundle. Used only when the
// numbering is unusable, so it must be able to cross a generation boundary: the
// held token is discarded (not echoed), which makes the loader send the bare
// no-token URL the server answers with a complete `delivery:"full"`.
async function forceFullHistoryReload(view, flowId) {
  if (view === "flow") {
    state.flowConversationProgress = null;
    state.flowConversationSignature = null;
    await loadFlowConversation(flowId, { silent: true });
  } else {
    state.historyProgress = null;
    state.historySignature = null;
    await openHistorySession(flowId, { incremental: true });
  }
}

// The repair budget spent against ONE bundle. Scoped to (view, flow, generation):
// every fact it carries — how much of the budget is gone, whether the un-numbered
// escalation has already been tried — is a claim about the bundle currently
// cached server-side, and is void the moment the daemon replaces it. Re-keying on
// a generation change hands the new bundle a fresh budget, which is exactly right:
// a hole in a NEW bundle is a new hole and deserves its own repair attempts.
//
// `generation` is null when the server (or a test stub) sends none; every signal
// then keys the same null bucket, which degrades to the flow-scoped budget rather
// than to an unbounded one.
function repairBudget(key, generation) {
  const gen = Number.isInteger(generation) ? generation : null;
  let spent = state.backfillAttempts[key];
  if (!spent || spent.generation !== gen) {
    spent = { generation: gen, backfills: 0, full: 0, unkeyableFull: false };
    state.backfillAttempts[key] = spent;
  }
  return spent;
}

// The numbers the SERVER has declared its bundle holds no record for, for the
// bundle *generation* currently held. A verdict from a superseded bundle is
// dropped rather than carried over — see `repairBudget`.
function retiredOrdinals(key, generation) {
  const gen = Number.isInteger(generation) ? generation : null;
  const known = state.backfillUnfillable[key];
  if (!known || known.generation !== gen) return null;
  return known.map;
}

// Fold a backfill reply's `unfillable` list into the generation's retired-number
// set, so a number the server has told us its bundle does not hold is never asked
// for again while that bundle stands. Returns true when something was retired.
function retireUnfillableOrdinals(key, generation, unfillable) {
  if (!unfillable || typeof unfillable !== "object") return false;
  const gen = Number.isInteger(generation) ? generation : null;
  let entry = state.backfillUnfillable[key];
  if (!entry || entry.generation !== gen) {
    entry = { generation: gen, map: {} };
  }
  let retired = false;
  for (const stepId of Object.keys(unfillable)) {
    const ords = unfillable[stepId];
    if (!Array.isArray(ords) || !ords.length) continue;
    const merged = new Set(entry.map[stepId] || []);
    for (const ord of ords) {
      if (Number.isInteger(ord)) { merged.add(ord); retired = true; }
    }
    entry.map[stepId] = Array.from(merged);
  }
  if (retired) state.backfillUnfillable[key] = entry;
  return retired;
}

// Adopt a server `resync` reply's authoritative cursor state and shed the repair
// bookkeeping bound to the bundle it just superseded.
//
// WHY this path exists at all: a signed progress cursor the client echoes can
// stop binding the server's bundle when the daemon reconnects and its bundle
// rotates (a new generation / machine), or when the cursor simply expires. The
// server CANNOT answer that with a 401 — `require_owner` is cookie-only and
// resolves BEFORE the cursor is ever decoded, so an owner polling its own flow
// is authenticated no matter how stale the cursor is (this was the forensic
// finding behind the field's spurious 401↔reconnect correlation). Instead it
// falls back to a recoverable `delivery:"full"` tagged `resync:true`, whose
// progress/signature/generation describe the CURRENT bundle. The caller adopts
// that token (the normal full-delivery token swap already did so above); this
// helper additionally voids the repair budget and retired-unfillable set keyed
// to the now-dead generation, so the fresh bundle's self-check starts clean
// rather than inheriting verdicts about a bundle that no longer exists.
//
// Bounded by construction — no bare-retry loop, no resync storm: the adopted
// token binds the current generation, so the very next poll echoes a cursor the
// server CAN bind and gets an ordinary in-sync delta (`resync:false`). The
// in-flight guard (`backfillInFlight`) is deliberately left alone: a repair
// already awaiting the server read some prior generation and will settle or
// no-op itself; clearing it here could let a duplicate request fire.
function resetRepairStateForResync(view, flowId) {
  const flightKey = `${view}|${flowId}`;
  delete state.backfillAttempts[flightKey];
  delete state.backfillUnfillable[flightKey];
  // eslint-disable-next-line no-console
  console.debug(
    "history cursor resync: stale signed cursor rejected, adopted authoritative bundle for flow=%s (view=%s)",
    flowId, view);
}

async function reconcileCursorCompleteness(view, flowId, cursor, generation, pending) {
  if (!flowId || !cursor || typeof cursor !== "object") return;
  if (!viewIsCurrent(view, flowId)) return;
  const flightKey = `${view}|${flowId}`;
  if (state.backfillInFlight[flightKey]) return;

  const held = heldHistoryRecords(view);
  // `pending` (the server-declared still-streaming window, carried by every REST
  // reply and WS frame) is subtracted from the gap set alongside the retired
  // numbers: a pending gap is a record the daemon has not yet pushed (the
  // delivery-livelock backlog crossing many short connection windows), not a
  // hole, so it must not provoke a backfill nor a giving-up.
  const probe = findMissingOrdinals(
    held, cursor, retiredOrdinals(flightKey, generation), pending);
  const spent = repairBudget(flightKey, generation);
  const progress = view === "flow"
    ? state.flowConversationProgress : state.historyProgress;
  const signature = view === "flow"
    ? state.flowConversationSignature : state.historySignature;

  if (probe.unkeyable) {
    // The view holds a record no number can name (a pre-ordinal daemon), so the
    // numbered check cannot decide whether anything is missing — and a numbered
    // backfill could not repair it if it were. Escalate ONCE per generation to a
    // token-less full re-pull, which serves every record the bundle holds
    // regardless of numbering and so heals a genuine hole here in a single
    // request. If the view is STILL unkeyable after that full, the un-numbered
    // records are a property of the daemon, not a transient hole: retire the
    // numbered self-check for this bundle (falling back to the token-only
    // behaviour, zero extra requests) rather than re-pull once per streamed
    // record. A new generation re-arms it — the replacing daemon may well number.
    if (spent.unkeyableFull) return;
    spent.unkeyableFull = true;
    state.backfillInFlight[flightKey] = true;
    try {
      await forceFullHistoryReload(view, flowId);
    } catch (_) {
      // Transient: the retirement stands for this generation either way — a
      // failed full is not evidence the numbering became usable.
    } finally {
      delete state.backfillInFlight[flightKey];
    }
    return;
  }

  const encoded = probe.surplus ? null : encodeMissingParam(probe.missing);

  if (!probe.surplus && !Object.keys(probe.missing).length) {
    // No REAL hole to repair. Either the held set covers every number the bundle
    // declares (bar the ones the server declared unfillable), OR the only gaps
    // left are `pending` — numbers the server says are still streaming from the
    // daemon (the delivery-livelock backlog crossing many short-lived connection
    // windows). Both are handled the SAME way here, and deliberately so: no
    // backfill (a pending number is not in the bundle to slice, and asking every
    // poll is the request storm the numbering avoids), no giving-up terminal
    // state, no wedge — the already-rendered records stand. The self-check stays
    // armed: when the daemon delivers the pending window the cursor advances,
    // this runs again on the next signal, and any residual real hole is repaired
    // then, with zero user intervention. The repair budget is released either
    // way so a LATER hole in this flow is still repairable. (`pendingGap` is not
    // branched on — a pending-only gap must behave exactly like being in sync,
    // which is the point of not wedging.)
    delete state.backfillAttempts[flightKey];
    return;
  }

  // A numbered backfill is only servable against a live token: the numbers name
  // positions in the bundle that token pins. With no token (or with a surplus,
  // which means the numbering no longer describes this bundle) the only sound
  // repair is a full re-pull.
  const canBackfill = !!(encoded && progress && spent.backfills < MAX_BACKFILL_ATTEMPTS);
  if (!canBackfill && spent.full >= 1) {
    // The budget is spent and a REAL hole is still open — reaching here means
    // `probe.missing` is non-empty (or surplus), and `missing` already EXCLUDES
    // every server-declared `pending` number, so this only ever fires for a
    // genuine void (or a numbering that no longer describes the bundle), never
    // for records still streaming from the daemon. A pending-only gap took the
    // healthy branch above and can NEVER reach this giving-up log. Stop:
    // repeating the request every poll would turn a server-side gap into a
    // client-driven request storm. The budget is handed back by a clean
    // self-check (the flow actually recovered) or by a new bundle generation,
    // never by a mere new record arriving.
    // eslint-disable-next-line no-console
    console.debug(
      "history cursor self-check: gap persists after backfill+full for flow=%s (view=%s) — giving up",
      flowId, view);
    return;
  }

  state.backfillInFlight[flightKey] = true;
  try {
    if (!canBackfill) {
      spent.full += 1;
      await forceFullHistoryReload(view, flowId);
      return;
    }
    spent.backfills += 1;
    const requestRecords = held;
    const resp = await authedFetch(
      historySnapshotUrl(flowId, progress, signature, encoded));
    if (!resp.ok || !viewIsCurrent(view, flowId)) return;
    const data = await resp.json();
    if (!viewIsCurrent(view, flowId)) return;
    // Numbers the bundle holds no record for are retired BEFORE the merge, so
    // the self-check that runs on the next signal no longer counts them missing
    // and the repair converges instead of re-firing. Retired against the REPLY's
    // generation — the bundle whose verdict this is — which may already differ
    // from the one the probe ran against.
    if (data && Number.isInteger(data.generation)) {
      retireUnfillableOrdinals(flightKey, data.generation, data.unfillable);
    } else {
      retireUnfillableOrdinals(flightKey, generation, data && data.unfillable);
    }
    commitBackfillResult(
      view, flowId,
      mergeHistoryResponse(data, heldHistoryRecords(view), requestRecords));
  } catch (_) {
    // A transient failure changes nothing: the next poll re-runs the self-check
    // and, since the hole is still there, retries within the same budget.
  } finally {
    delete state.backfillInFlight[flightKey];
  }
}

// Run the self-check for whichever view(s) a WS frame concerns. Both
// `history_data` (records + post-frame cursor) and the records-less
// `history_cursor` advisory (the frame that REPAIRS a bundle the cache refused to
// relay) land here, so the push path is self-checkable exactly like the poll.
// The frame carries the same `pending` window the REST snapshot would (see
// get_history_bundle_meta), so a push-driven self-check draws the pending line
// identically to a poll-driven one.
function applyHistoryCursor(msg) {
  if (!msg || !msg.flow_id || !msg.cursor) return;
  if (isHistoryOpen() && state.selectedHistoryId === msg.flow_id) {
    void reconcileCursorCompleteness(
      "history", msg.flow_id, msg.cursor, msg.generation, msg.pending);
  }
  if (state.selectedFlowId === msg.flow_id) {
    void reconcileCursorCompleteness(
      "flow", msg.flow_id, msg.cursor, msg.generation, msg.pending);
  }
}

// Reduce a user record's text to its literal, marker-stripped, trimmed form so
// an optimistic local echo and the daemon's authoritative record compare equal
// even when the daemon wrapped the reply in the prompt-marker envelope (e.g. a
// discovery continuation). With no markers it is just the trimmed content.
function comparableUserText(content) {
  if (typeof content !== "string") return "";
  const split = splitUserPromptByMarker(content);
  if (split && split.content) return split.content.trim();
  return content.trim();
}

// Drop optimistic local-echo user records (the instant-feedback copies appended
// by `appendLocalReply`) once the daemon's authoritative record for the same
// reply has arrived, so a submitted reply is shown exactly once. The echo and
// the daemon record carry different step_id / timestamp — hence different
// `recordKey` — so `mergeSnapshotWithLiveAppends`' identity dedup cannot pair
// them; this content-based pass does.
//
// Safety invariants (match the spec's no-loss / no-reorder rules):
//   - an echo is removed ONLY once THIS reply's own authoritative copy has
//     landed — detected by the per-text authoritative count growing past the
//     echo's stable rank (`__localEchoPriorAuth`, the count of prior copies —
//     authoritative records + pending echoes — at creation). A reply whose text
//     duplicates an earlier, already-recorded reply (repeated "yes" /
//     "continue", identical interjections) therefore stays visible continuously
//     until its OWN daemon record arrives, never flickering out when a reconcile
//     pass finds the earlier duplicate, and a single daemon arrival never sweeps
//     away more than one pending echo even when several identical replies are
//     pending at once;
//   - the authoritative daemon record is never removed — it keeps its real
//     timestamp and so lands in the correct chronological slot;
//   - the same array reference is returned when nothing changed, letting the
//     caller keep the cheap incremental-append render path.
function reconcileLocalEchoes(records) {
  if (!Array.isArray(records) || records.length < 2) return records;
  // Count authoritative (non-echo) user records per comparable reply text.
  const authCount = new Map();
  // Gather echo indices per text, in array (chronological) order.
  const echoesByText = new Map();
  let echoCount = 0;
  for (let i = 0; i < records.length; i++) {
    const r = records[i];
    if (r && r.__localEcho) {
      echoCount += 1;
      const t = comparableUserText(
        r.__localEchoText != null ? r.__localEchoText : normalizeRecord(r).content);
      if (!t) continue;
      if (!echoesByText.has(t)) echoesByText.set(t, []);
      echoesByText.get(t).push(i);
      continue;
    }
    const n = normalizeRecord(r);
    if (n.role === "user") {
      const t = comparableUserText(n.content);
      if (t) authCount.set(t, (authCount.get(t) || 0) + 1);
    }
  }
  if (!echoCount || !authCount.size) return records;
  // For each text, an echo is the daemon copy's twin once the authoritative
  // count has grown past that echo's rank (`__localEchoPriorAuth` = the number
  // of prior copies — authoritative records + pending echoes — at creation).
  // Each echo carries a distinct rank (0, 1, 2, …), so removing every echo
  // whose rank < auth removes exactly the `auth` earliest pending echoes and
  // is stable across passes: a record removed in an earlier pass no longer
  // affects the survivors' ranks, so a later daemon arrival clears only its own
  // echo and never sweeps away a later still-pending one. Legacy echoes without
  // the field default to rank 0, preserving the original single-reply behavior.
  const removeIdx = new Set();
  for (const [t, idxs] of echoesByText) {
    const auth = authCount.get(t) || 0;
    if (!auth) continue;
    for (const i of idxs) {
      const p = records[i].__localEchoPriorAuth;
      const rank = typeof p === "number" && p >= 0 ? p : 0;
      if (rank < auth) removeIdx.add(i);
    }
  }
  if (!removeIdx.size) return records;
  return records.filter((_, i) => !removeIdx.has(i));
}

// Comparable epoch-ms value for a timestamp of unknown shape, for sorting.
function tsValue(ts) {
  if (ts == null || ts === "") return 0;
  if (typeof ts === "number") return ts < 1e12 ? ts * 1000 : ts;
  const d = new Date(ts);
  return isNaN(d.getTime()) ? 0 : d.getTime();
}

// ---------------------------------------------------------------------------
// Record classification (pure, role-based)
// ---------------------------------------------------------------------------
//
// Folding is decided *only* from the structured `role` field — never by
// guessing from text — so the call is deterministic and never misfires.
// `user` / `system` are template-style prompt messages: they default to a
// collapsed one-line chip. `assistant` (the real product) and anything else
// default to an expanded bubble. `human` is already folded into `user` by
// `normalizeRecord`, so it lands in the collapsible set too.

const COLLAPSIBLE_ROLES = ["user", "system"];

// True when a record's role marks it as a template-style prompt message that
// should default to a collapsed chip rather than an expanded bubble.
function isCollapsibleRole(role) {
  return COLLAPSIBLE_ROLES.includes(String(role || "").toLowerCase());
}

// One-line label for a collapsed chip, e.g. "system prompt · discovery". The
// role and step context stay raw (they are protocol identifiers, not copy); only
// the surrounding template is translated.
function chipLabel(norm) {
  const role = String((norm && norm.role) || "message");
  const ctx = (norm && (norm.stepType || norm.stepId)) || "";
  return ctx
    ? tf("prompt.chipLabel", `${role} prompt · ${ctx}`, { role, ctx })
    : tf("prompt.chipLabelNoCtx", `${role} prompt`, { role });
}

// ---------------------------------------------------------------------------
// User prompt marker split
// ---------------------------------------------------------------------------
//
// Step prompts wrap their template prefix (role definition, agent-safety
// boilerplate, generic step instructions) with a sentinel marker pair that
// the engine injects at prompt-build time
// (see src/se3/engine/prompt_markers.py). The frontend looks for these
// literals — never pattern-guesses prompt text — and splits the user
// message into a default-collapsed chip (the boilerplate) and a default-
// expanded bubble (the actual task / context). Records that lack the
// markers (legacy / non-step prompts) fall back to the whole-chip path.

const TEMPLATE_PREFIX_END = "<!--SE3:TEMPLATE_END-->";
const USER_CONTENT_BEGIN = "<!--SE3:USER_CONTENT-->";
const USER_CONTENT_END = "<!--SE3:USER_CONTENT_END-->";

// Strip leading CR/LF from a marker-bounded segment so the segment text
// starts on its first real character. The engine joins markers with `\n`
// on both sides, and the frontend should not display that join glue.
function stripLeadingNewlines(s) {
  let i = 0;
  while (i < s.length && (s[i] === "\n" || s[i] === "\r")) i++;
  return s.slice(i);
}
function stripTrailingNewlines(s) {
  let i = s.length;
  while (i > 0 && (s[i - 1] === "\n" || s[i - 1] === "\r")) i--;
  return s.slice(0, i);
}

// Split a user-role message body at its template / user-content / suffix
// boundaries. Returns one of three shapes:
//
//   - Three-segment (engine emitted TEMPLATE_PREFIX_END, USER_CONTENT_BEGIN,
//     USER_CONTENT_END in order):
//       `{prefix, content, suffix}` — `prefix` is the system-template
//       boilerplate before TEMPLATE_PREFIX_END, `content` is the user's
//       literal input between USER_CONTENT_BEGIN and USER_CONTENT_END, and
//       `suffix` is the framework-injected tail (Available Specs, runtime
//       env, READ-ONLY constraint, language directive, …).
//   - Two-segment (engine emitted only TEMPLATE_PREFIX_END +
//     USER_CONTENT_BEGIN, no USER_CONTENT_END): `{prefix, content:"", suffix}`
//     with `suffix` = everything after USER_CONTENT_BEGIN. This is the legacy
//     two-marker layout, used by every non-discovery step prompt module
//     (analyze / plan / plan_tasks / implement / self_check / verify_spec /
//     update_spec / summarize / version_analyze). The post-BEGIN tail there
//     is framework-injected text (task-description heading, project context,
//     spec_content, runtime-env, READ-ONLY constraint, language directive,
//     …) — NOT a user literal — so we route it into the collapsed system-
//     prompt chip as a `suffix` subsection and emit no expanded user-content
//     bubble. This makes the two-segment layout degrade to the same on-
//     screen behavior as a no-marker legacy message (a single collapsed
//     chip), instead of regressing to displaying framework text in an
//     expanded bubble.
//   - `null`: the markers are missing, malformed, or out of order — the
//     caller should fall back to the whole-message chip behavior.
function splitUserPromptByMarker(content) {
  if (typeof content !== "string" || !content) return null;
  const tpe = content.indexOf(TEMPLATE_PREFIX_END);
  if (tpe < 0) return null;
  const ucb = content.indexOf(USER_CONTENT_BEGIN, tpe + TEMPLATE_PREFIX_END.length);
  if (ucb < 0) return null;
  // Three-segment: USER_CONTENT_END must come after USER_CONTENT_BEGIN.
  const uce = content.indexOf(USER_CONTENT_END, ucb + USER_CONTENT_BEGIN.length);
  const prefix = content.slice(0, tpe);
  if (uce >= 0) {
    const middle = content.slice(ucb + USER_CONTENT_BEGIN.length, uce);
    const tail = content.slice(uce + USER_CONTENT_END.length);
    return {
      prefix: prefix,
      content: stripTrailingNewlines(stripLeadingNewlines(middle)),
      suffix: stripLeadingNewlines(tail),
    };
  }
  // Two-segment legacy: BEGIN with no END. The remainder is framework-
  // injected text, not a user literal, so we route it into the chip as the
  // suffix subsection and emit no user-content bubble.
  const rest = content.slice(ucb + USER_CONTENT_BEGIN.length);
  return {
    prefix: prefix,
    content: "",
    suffix: stripLeadingNewlines(rest),
  };
}

// ---------------------------------------------------------------------------
// History records rendering
// ---------------------------------------------------------------------------
//
// Records carry a step identifier; group them so each step's conversation is
// shown under its own heading, ordered within the group by timestamp.
function stepKey(norm) {
  return String(norm.stepId || norm.stepType || "step");
}

// Coerce an arbitrary step-type string into a legal DOM token (the suffix of a
// `step-type-<token>` class): lower-case, whitespace folded to "-", anything
// outside [a-z0-9_-] dropped, runs of "-" collapsed and trimmed. Returns "" when
// nothing legal survives (e.g. a pure-CJK label), which callers read as "skip".
// WHY: `classList.add()` throws InvalidCharacterError on a token containing
// whitespace, and a record's step_type is untrusted input — it can come from a
// version-skewed daemon or from an older frontend's echo left in state. The
// renderer must never be the thing that a single bad field takes down.
function sanitizeDomToken(raw) {
  return String(raw == null ? "" : raw)
    .trim()
    .toLowerCase()
    .replace(/\s+/g, "-")
    .replace(/[^a-z0-9_-]/g, "")
    .replace(/-{2,}/g, "-")
    .replace(/^[-]+|[-]+$/g, "");
}

// Tag a conversation bubble with a stable, lower-cased `step-type-<type>` DOM
// class so a later group can paint each step region with a low-saturation,
// distinguishable per-step grouping style. Applied uniformly to every bubble
// (chat turns, step-event rows, step_started anchors) in addConversationRecords
// so the whole step region — start status, conversation, and final report —
// shares one step-type class. No-op when the step type is empty (legacy
// records) or sanitizes to nothing, so nothing dangling — and nothing illegal —
// is added. Never throws, whatever it is handed.
function tagStepType(bubble, stepType) {
  const key = sanitizeDomToken(stepType);
  if (key && bubble && bubble.classList) {
    bubble.classList.add("step-type-" + key);
  }
}

// Friendly, upper-case step-section headings matching the message paradigm
// (DISCOVERY / ANALYZE / PLAN / IMPLEMENT / TEST / SELF CHECK / UPDATE SPEC /
// VERSION ANALYZE / COMMIT / SUMMARY …). Keyed by lower-case `step_type`. These
// are intentionally distinct from `STEP_REPORT_TITLES` (the title-case report-
// card labels such as "Analysis" / "Work Summary"): the conversation step
// headers follow the paradigm's exact wording, e.g. ANALYZE (not "Analysis")
// and SUMMARY (not "Summarize").
const STEP_HEADER_TITLES = {
  discovery: "DISCOVERY",
  analyze: "ANALYZE",
  investigate: "INVESTIGATE",
  project_summary: "PROJECT SUMMARY",
  propose: "PROPOSE",
  design: "DESIGN",
  plan: "PLAN",
  plan_tasks: "PLAN TASKS",
  confirm: "CONFIRM",
  implement: "IMPLEMENT",
  test: "TEST",
  self_check: "SELF CHECK",
  verify_spec: "VERIFY SPEC",
  update_spec: "UPDATE SPEC",
  spec_gate: "SPEC GATE",
  version_analyze: "VERSION ANALYZE",
  commit: "COMMIT",
  // The worktree merge is now the flow's own steps (they replaced the retired
  // "合并中" bypass indicator), so they get first-class step headings.
  merge_integrate: "MERGE",
  version_reconcile: "VERSION RECONCILE",
  summarize: "SUMMARY",
};

// Per-group DAG status → human-readable text. Keyed by lower-case status as
// written by chat_history.record_group_status; mirrors the lifecycle the
// DAGScheduler emits (queued → running → completed | failed | skipped).
// The literals are the offline fallback (dicts unloaded) and are therefore
// English, matching index.html's built-in English fallback contract.
const GROUP_STATUS_TEXT = {
  queued: "Queued",
  running: "Implementing in worktree",
  completed: "Completed",
  failed: "Failed",
  skipped: "Skipped",
};

// Per-group DAG status → leading status icon, giving running / completed /
// failed an at-a-glance visual distinction in the marker (the rest of the
// distinction comes from the `.status-<status>` CSS class).
const GROUP_STATUS_ICON = {
  queued: "◷",
  running: "◐",
  completed: "✓",
  failed: "✗",
  skipped: "⊘",
};

// Step lifecycle status → {icon, text} for the lightweight per-step status
// rows. Status is conveyed by BOTH an explicit text label and an icon (never
// color alone), so running / retrying / paused / completed / failed / partial
// stay legible and accessible regardless of the status background tint a later
// group applies. Keyed by the lower-case StepStatus value the engine persists.
const STEP_STATUS_DISPLAY = {
  running: { icon: "◐", text: "In progress" },
  retrying: { icon: "↻", text: "Retrying" },
  paused: { icon: "⏸", text: "Paused" },
  completed: { icon: "✓", text: "Completed" },
  failed: { icon: "✗", text: "Failed" },
  partial: { icon: "◑", text: "Partially complete" },
  waiting_for_lock: { icon: "⏳", text: "Waiting for lock" },
};

// Pure: resolve a step's status display ({icon, text}); unknown statuses fall
// back to the raw token (text) with a neutral dot icon so nothing is silently
// dropped. Exposed for unit testing.
function stepStatusDisplay(status) {
  const key = String(status == null ? "" : status).toLowerCase();
  const base = STEP_STATUS_DISPLAY[key];
  if (base) {
    // Resolve the label via I18N at RENDER time (the map is a module-load const
    // evaluated before the dicts load, so it can't call t() in its initializer).
    // resolve() returns null on a total miss (e.g. the document-less unit-test
    // environment where the dicts stay empty) → we keep the map's built-in
    // label as the offline fallback; when the dicts are loaded it localizes.
    const tr = I18N.resolve(`status.step.${key}`);
    return { icon: base.icon, text: tr != null ? tr : base.text };
  }
  return { icon: "•", text: key || "running" };
}

// Pure: render a per-group DAG status marker label, e.g.
// groupStatusLabel("G3", "running") → "G3 正在 worktree 实施中". An unknown
// status falls back to its raw token so nothing is silently dropped, and a
// missing group id degrades to "?" rather than producing a dangling label.
// Exposed for unit testing.
function groupStatusLabel(groupId, status) {
  const gid = String(groupId == null ? "" : groupId).trim() || "?";
  const key = String(status == null ? "" : status).toLowerCase();
  // Localize known statuses via I18N.resolve at render time, keeping the map's
  // built-in label as the offline fallback (null resolve = empty test dicts);
  // an unknown status keeps its raw token so nothing is silently dropped.
  const known = GROUP_STATUS_TEXT[key];
  const tr = known ? I18N.resolve(`status.group.${key}`) : null;
  const text = (tr != null ? tr : known) || String(status == null ? "" : status);
  return text ? `${gid} ${text}` : gid;
}

// Code-index update-progress → leading status icon. A node that has just
// finished a running wave shows the "working" spinner glyph; once done reaches
// total the marker flips to a completed check. The rest of the visual
// distinction comes from the `.status-<state>` CSS class.
const INDEX_PROGRESS_ICON = {
  running: "◐",
  completed: "✓",
};

// Pure: the lifecycle state of a code-index progress marker — "completed" once
// every node is summarised (done >= total, total > 0), else "running". Exposed
// for unit testing and reused by the renderer + the supersede reconciliation so
// the terminal state is decided in exactly one place.
function indexProgressState(done, total) {
  const d = Number(done);
  const t = Number(total);
  if (Number.isFinite(t) && t > 0 && Number.isFinite(d) && d >= t) {
    return "completed";
  }
  return "running";
}

// Pure: render a code-index update-progress marker label, e.g.
// indexProgressLabel("src/se3/cli.py", 3, 12) →
// "更新 code-index：src/se3/cli.py (3/12)". A missing path degrades to "…"
// rather than a dangling label, and the (done/total) suffix is dropped when no
// total is known so a bare running marker never shows "(0/0)". Exposed for
// unit testing.
function indexProgressLabel(path, done, total) {
  const p = String(path == null ? "" : path).trim() || "…";
  const t = Number(total);
  if (Number.isFinite(t) && t > 0) {
    const d = Number(done);
    const shown = Number.isFinite(d) ? d : 0;
    // Localize at render time; the built-in template is the offline fallback.
    const tr = I18N.resolve("indexProgress.withTotal", { path: p, done: shown, total: t });
    return tr != null ? tr : `Updating code-index: ${p} (${shown}/${t})`;
  }
  const tr = I18N.resolve("indexProgress.noTotal", { path: p });
  return tr != null ? tr : `Updating code-index: ${p}`;
}

// Resolve the conversation step-header label for a step type. Known step types
// map to their paradigm heading; unknown ones fall back to the original step
// key/label so the strict time order and separator rebuild are never broken.
function stepHeaderLabel(stepType, fallback) {
  const key = String(stepType || "").toLowerCase();
  if (key && STEP_HEADER_TITLES[key]) {
    const tr = I18N.resolve("stepHeader." + key);
    return tr != null ? tr : STEP_HEADER_TITLES[key];
  }
  // Optimistic local echoes carry a `reply_<kind>` token rather than a rendered
  // label (see replyStepType): compose the human heading here, from the SAME
  // i18n keys the intervention chip uses, so the echo's header stays localized
  // without the label ever leaking back into the identifier field.
  if (key.startsWith(REPLY_STEP_TYPE_PREFIX)) {
    const meta = KIND_META[key.slice(REPLY_STEP_TYPE_PREFIX.length)];
    if (meta) {
      const kindLabel = tf(meta.labelKey, meta.label);
      return tf("flow.stepType.response", kindLabel + " response",
        { label: kindLabel });
    }
  }
  return fallback || stepType || "step";
}

// Render a flat list of raw records into `container` as a CLI-style chat
// stream. Shared verbatim by the history view and the running-flow view so
// both present identical bubbles / folding.
//
// **Strict chronological order:** records are ordered globally by
// `(__convTs, __convIdx)` across step boundaries — the same physical order
// as the underlying jsonl files. A lightweight `.history-step-header` row is
// inserted between two adjacent bubbles whenever they cross a step boundary,
// so the user still sees per-step visual grouping; it is purely a separator
// and never changes the physical order of bubbles. A discovery → user reply
// → discovery sequence renders in jsonl-order rather than re-bucketed into
// independent per-step sections.
//
// Incremental updates: an active flow streams `history_data` appends every
// LLM turn. A full rebuild on each append would recreate every
// `makeFoldable` / `makeRawToggle` / chip in its default collapsed state,
// collapsing a record the reader had just expanded. So when `append` is set,
// only the new tail records are built and inserted into the existing DOM —
// bubbles already on screen (and any folds / chips / raw panels the reader
// opened) are untouched. After each batch, step-header separators are
// rebuilt from the now-correctly-ordered bubble list, which keeps the
// stateful bubbles intact while the stateless headers re-shift to wherever
// the latest insertion changed a step boundary.
//
// Per-container reconciliation state lives on `container.__convState`:
//   { count: number of raw records already rendered }
function renderConversation(container, records, append) {
  const st = container.__convState;
  // Fast incremental path: only when we have a live reconciliation state AND
  // the incoming array is a strict superset of what is already rendered
  // (`records.length >= st.count`). A shorter array means the snapshot was
  // replaced out from under us (full re-fetch, reconnect) — fall through to a
  // clean rebuild instead of silently doing nothing, which would otherwise
  // look like the conversation "froze".
  if (append && st && st.count > 0 && records.length >= st.count) {
    if (records.length > st.count) {
      addConversationRecords(container, st, records, st.count);
    }
    return;
  }
  container.innerHTML = "";
  const fresh = { count: 0 };
  container.__convState = fresh;
  if (!records.length) {
    container.appendChild(
      el("p", "empty", tf("flow.noConversationRecords", "No conversation records for this session.")));
    return;
  }
  addConversationRecords(container, fresh, records, 0);
}

// Build records `records[startIndex..]` and merge them into `container`,
// keeping the whole conversation strictly ordered by `(__convTs, __convIdx)`.
// Each bubble carries its step key so a single linear sweep can rebuild the
// `.history-step-header` separators after all bubbles for this batch have
// been placed.
//
// `st.count` is advanced here (not by the caller) and ALWAYS reaches
// `records.length`, even if an individual record fails to render: a single
// malformed record must never stall the append cursor, or every subsequent
// delta would re-attempt the same broken slot and the stream would freeze.
// Each record is built behind a try/catch and degrades to a minimal
// placeholder bubble (carrying the same ordering metadata) so the rest of the
// batch — and all future appends — keep flowing.
function addConversationRecords(container, st, records, startIndex) {
  // Map every record to a stable segment key over the FULL ordered array (not
  // by inspecting the DOM): same-segment partials (one turn's run of fragments)
  // share a key and merge into one accumulating bubble, while a later round's
  // partials (separated by an intervening final) get a distinct key and a
  // separate bubble. Computing this purely keeps the incremental (append) and
  // full-rebuild paths identical, and is immune to removeSupersededProgress
  // running only at the end of the loop (a just-closed segment's stale bubble
  // still lingers mid-loop, so a DOM probe would mis-merge the next round).
  const segments = partialSegments(records);
  // Per-record cumulative round usage (G5), grouped by step_id over the FULL
  // ordered array so the cumulative is stable regardless of how the batch was
  // sliced. Attached to each norm below so the interactive assistant renderers
  // can show『本轮 … · 累计 …』without re-walking the records.
  const roundCumulative = accumulateRoundUsageByStep(records);
  for (let i = startIndex; i < records.length; i++) {
    const segKey = segments[i];

    // Partial fragment: fold it into its segment's live accumulating bubble,
    // creating that bubble on the segment's first fragment. A malformed partial
    // must not stall the stream, so on failure we fall through to the generic
    // per-record path below for the same index.
    if (segKey != null) {
      let handled = false;
      try {
        const norm = normalizeRecord(records[i]);
        const live = findLivePartialBubble(container, segKey);
        if (live) {
          appendPartialFragment(live, norm);
          // Track the LATEST fragment for ordering + supersede: the bubble's
          // (ts, idx) become the newest fragment's, so a later final sorts just
          // after it and `superseded.has(child.__convIdx)` (the latest member
          // index is always superseded once the turn finalizes) still drops it.
          live.__convTs = tsValue(norm.timestamp);
          live.__convIdx = i;
        } else {
          const bubble = buildPartialBubble(norm);
          bubble.__convTs = tsValue(norm.timestamp);
          bubble.__convIdx = i;
          bubble.__convStepKey = stepKey(norm);
          bubble.__convStepType = norm.stepType || "";
          tagStepType(bubble, bubble.__convStepType);
          bubble.__convStepLabel = norm.stepType || norm.stepId || "step";
          bubble.__convPartial = true;
          bubble.__convSegmentKey = segKey;
          bubble.__convTurnKey = progressTurnKey(norm);
          insertBubbleSorted(container, bubble);
        }
        handled = true;
      } catch (err) {
        try { console.warn("partial fragment render failed", i, err); }
        catch (_) { /* console may be absent */ }
      }
      if (handled) continue;
    }

    let norm = null;
    let bubble;
    try {
      norm = normalizeRecord(records[i]);
      // Per-step running cumulative for the per-round usage footnote (G5).
      if (norm) norm.cumulativeUsage = roundCumulative[i];
      bubble = renderConversationRecord(norm);
    } catch (err) {
      try { console.warn("conversation record render failed", i, err); }
      catch (_) { /* console may be absent */ }
      bubble = el("div", "history-record conv-record role-error");
      bubble.appendChild(
        el("p", "md-p conv-empty",
          tf("conv.recordUnrenderable", "(this record could not be rendered)")));
    }
    // WHY: one dirty record must never break the whole conversation render.
    // Everything below (ordering metadata, DOM-class tagging, the supersede
    // tags, the insert) touches untrusted record fields, and this loop sits on
    // the ws.onmessage → applyHistoryData → renderConversation path where an
    // escaping throw freezes the entire chat view — not just the offending
    // bubble — until the reader exits and re-enters the session (that is exactly
    // how a single echo with a space in its step_type froze live chat). Isolate
    // per record: log, drop this one bubble, keep the batch flowing.
    try {
      bubble.__convTs = tsValue(norm && norm.timestamp);
      bubble.__convIdx = i;
      bubble.__convStepKey = stepKey(norm || {});
      bubble.__convStepType = (norm && norm.stepType) || "";
      tagStepType(bubble, bubble.__convStepType);
      bubble.__convStepLabel = (norm && (norm.stepType || norm.stepId)) || "step";
      // Tag partial (stream-progress) bubbles so they can be folded away once the
      // turn's final result arrives (see removeSupersededProgress).
      bubble.__convPartial = !!(norm && norm.partial);
      if (bubble.__convPartial) {
        bubble.classList.add("conv-partial");
        bubble.__convTurnKey = progressTurnKey(norm);
      }
      // Tag the step lifecycle status anchors (step_started / step_status /
      // waiting_for_lock) so a later, more-current anchor for the SAME step
      // region supersedes the earlier one — the region then shows only its
      // current state (等待锁 → 进行中 → 已暂停) rather than stacking redundant
      // status rows.
      bubble.__convStatusRow = !!(
        norm
        && (norm.kind === "step_started"
          || norm.kind === "step_status"
          || norm.kind === "waiting_for_lock"));
      // Tag the terminal report rows (step_completed / step_failed). Once a step
      // region has a terminal report, ITS non-terminal status anchors (进行中 /
      // 已暂停 / 重试中) are stale — the report card itself conveys the final
      // completed/failed state — so removeSupersededStatusRows drops them. This
      // is what stops a finished region from simultaneously showing 进行中 (or a
      // stale 已暂停) alongside its completed report.
      bubble.__convTerminalRow = !!(
        norm && (norm.kind === "step_completed" || norm.kind === "step_failed"));
      // Tag per-group DAG status markers with a (step_id, group_id) composite
      // identity. A group emits several `group_status` records over its lifetime
      // (running w/o model → running w/ agent → running w/ agent·model →
      // completed/failed), all sharing one (step_id, group_id); they must
      // converge to a SINGLE card that updates in place. removeSupersededStatusRows
      // can't do this — it keys on step_id alone, and many groups of one implement
      // step share a step_id, so it would wrongly fold distinct groups together.
      // removeSupersededGroupStatusRows reconciles per composite key instead, so
      // each group keeps its own card while successive records for the SAME group
      // (which carry the accumulated agent/model) supersede the older one in place.
      if (norm && norm.kind === "group_status") {
        const gStatus = String(norm.status || "").toLowerCase();
        bubble.__convGroupStatusRow = true;
        bubble.__convGroupId = norm.groupId || "";
        bubble.__convGroupStatusKey =
          String(norm.stepId || stepKey(norm)) + "#" + (norm.groupId || "");
        bubble.__convGroupStatusTerminal =
          ["completed", "failed", "skipped"].includes(gStatus);
      }
      // Tag code-index update-progress markers so a step's successive markers
      // (one per file/dir node, each with a higher `done`) converge to a SINGLE
      // progress line that updates in place. Unlike group_status these key on the
      // step_id ALONE: one commit step rebuilds ONE index, so every marker of the
      // step is the same climbing line. removeSupersededIndexProgressRows keeps the
      // latest (terminal-preferred) marker per step so the count advances and the
      // icon flips to ✓ instead of stacking one row per file.
      if (norm && norm.kind === "index_progress") {
        bubble.__convIndexProgressRow = true;
        bubble.__convIndexProgressKey = String(norm.stepId || stepKey(norm));
        bubble.__convIndexProgressTerminal =
          indexProgressState(norm.done, norm.total) === "completed";
      }
      insertBubbleSorted(container, bubble);
    } catch (err) {
      try { console.error("conversation record post-render failed", i, err); }
      catch (_) { /* console may be absent */ }
    }
  }
  // Advance the cursor before the (stateless) header rebuild so the count is
  // correct even if header rebuilding ever throws. The cursor counts processed
  // records, NOT rendered bubbles, so removing superseded partial bubbles below
  // never desyncs it.
  st.count = records.length;
  // Once a turn produces its final (non-partial) assistant result, drop the
  // in-progress bubbles that streamed before it — the structured result bubble
  // is the turn's terminal form per the message paradigm. Stateful affordances
  // (folds / raw toggles / chips) on surviving bubbles are untouched.
  removeSupersededProgress(container, records);
  removeSupersededStatusRows(container);
  removeSupersededGroupStatusRows(container);
  removeSupersededIndexProgressRows(container);
  rebuildStepHeaders(container);
}

// Reconcile code-index update-progress markers so each step (keyed by step_id,
// `__convIndexProgressKey`) keeps exactly ONE progress line that updates in
// place as the rebuild advances. A commit step emits one marker per summarised
// node (done = 1, 2, 3, … N); keeping only the LATEST (or the terminal
// done>=total) marker is visually equivalent to a single line whose count
// climbs and whose icon flips to ✓ at completion — the stacking of one row per
// file disappears.
//
// Keyed by step_id ALONE (not a composite): one commit step rebuilds exactly
// one index, so all its markers ARE the same climbing line — unlike
// group_status where many groups share a step_id and must stay independent.
// Within a bucket the LATEST record by (__convTs, __convIdx) wins, with NO
// terminal preference — deliberately unlike removeSupersededGroupStatusRows.
// A group runs once, so its terminal card is final; but a commit step can
// REBUILD the index more than once under the same step_id (a step retry after
// source files changed between attempts), appending a fresh done=1..M run after
// the first run's done=N,total=N terminal. Preferring the terminal would freeze
// the line on the earlier rebuild's "✓ (N/N)" and hide the second rebuild's
// live progress until it too completes. Latest-wins is also correct within a
// single rebuild: the terminal marker is the last node summarised, so it has
// the highest (ts, idx) and naturally wins. Records are read from the append-
// ordered NDJSON in file order, so __convIdx reflects emission order and there
// is no out-of-order case to guard against. Only index_progress markers are
// touched — every other record is left exactly as-is. Markers are affordance-
// free, so removing them never disturbs surrounding fold / raw / chip state.
function removeSupersededIndexProgressRows(container) {
  const markers = Array.from(container.children).filter(
    (c) => c.__convIndexProgressRow === true);
  const byKey = new Map();
  for (const c of markers) {
    let arr = byKey.get(c.__convIndexProgressKey);
    if (!arr) { arr = []; byKey.set(c.__convIndexProgressKey, arr); }
    arr.push(c);
  }
  const newer = (a, b) =>
    a.__convTs > b.__convTs
    || (a.__convTs === b.__convTs && a.__convIdx > b.__convIdx);
  const toRemove = [];
  for (const arr of byKey.values()) {
    if (arr.length < 2) continue;
    let keep = null;
    for (const c of arr) {
      if (!keep || newer(c, keep)) keep = c;
    }
    for (const c of arr) {
      if (c !== keep) toRemove.push(c);
    }
  }
  for (const c of toRemove) container.removeChild(c);
}

// Reconcile per-group DAG status markers so each group (uniquely identified by
// its (step_id, group_id) composite key, `__convGroupStatusKey`) keeps exactly
// ONE card that updates in place, while distinct groups stay independent.
//
// A group emits several `group_status` records as it advances (running w/o model
// → running w/ agent → running w/ agent·model → completed/failed). Because each
// later record carries the accumulated agent/model identity, keeping only the
// LATEST record's card is visually equivalent to "upgrading the badge in place
// from agent → agent · model" — the original stacking bug (a model-less running
// card + an agent card + an agent·model card all piled up) disappears.
//
// This is deliberately a SEPARATE pass from removeSupersededStatusRows: that one
// keys on step_id (`__convStepKey`) alone, but many groups of one implement step
// share a single step_id, so folding by step_id would collapse different groups
// into one card. Here we bucket by the (step_id, group_id) composite key so
// different group_ids — and the same group_id under different step_ids — never
// fold together. Within a bucket the terminal card (completed/failed/skipped) is
// preferred (so a terminal report replaces the group's earlier running card even
// if records arrive out of order); otherwise the latest by (__convTs, __convIdx)
// wins. Only group_status markers are touched — every other record, step status
// row and report card is left exactly as-is. Markers are affordance-free, so
// removing them never disturbs surrounding fold / raw / chip state.
function removeSupersededGroupStatusRows(container) {
  const markers = Array.from(container.children).filter(
    (c) => c.__convGroupStatusRow === true);
  const byKey = new Map();
  for (const c of markers) {
    let arr = byKey.get(c.__convGroupStatusKey);
    if (!arr) { arr = []; byKey.set(c.__convGroupStatusKey, arr); }
    arr.push(c);
  }
  const newer = (a, b) =>
    a.__convTs > b.__convTs
    || (a.__convTs === b.__convTs && a.__convIdx > b.__convIdx);
  const toRemove = [];
  for (const arr of byKey.values()) {
    if (arr.length < 2) continue;
    let keep = null;
    for (const c of arr) {
      if (!keep) { keep = c; continue; }
      const keepTerminal = !!keep.__convGroupStatusTerminal;
      const cTerminal = !!c.__convGroupStatusTerminal;
      // Prefer a terminal card; among equals (both terminal or both not), keep
      // the latest by (ts, idx).
      if (cTerminal && !keepTerminal) keep = c;
      else if (cTerminal === keepTerminal && newer(c, keep)) keep = c;
    }
    for (const c of arr) {
      if (c !== keep) toRemove.push(c);
    }
  }
  for (const c of toRemove) container.removeChild(c);
}

// For each step region (keyed by `__convStepKey`), reconcile the lifecycle
// status anchors (`step_started` / `step_status`) against the region's current
// state so the region shows ONE truthful status — never two stacked anchors,
// and never a stale non-terminal anchor next to a terminal report:
//
//   * If the region has a TERMINAL report (`step_completed` / `step_failed`),
//     ALL of its non-terminal status anchors are dropped — the report card is
//     the region's final state. This is what stops a completed step from
//     simultaneously showing "进行中" (running → completed) or a stale "已暂停"
//     (running → paused → running → completed) alongside its completed report.
//   * Otherwise only the LATEST anchor by (__convTs, __convIdx) survives, so a
//     "进行中" step_started updates in place to "已暂停" (a later paused
//     step_status), and a resumed step re-arms "进行中" (a later running anchor
//     supersedes the earlier paused one) — without ever stacking two rows.
//
// Status anchors are affordance-free (no fold / raw / chip state), so removing
// them is as safe as the stateless header rebuild and never disturbs the
// conversation bubbles or the report card itself.
function removeSupersededStatusRows(container) {
  // Reconcile PER step_id (`__convStepKey`) across the WHOLE container — NOT per
  // maximal contiguous DOM run. Another step's record (or a late-arriving
  // record) can split one execution's lifecycle anchors and its terminal report
  // into separate contiguous runs (e.g. step_started(A) → record(B) →
  // step_completed(A)); a contiguous-run reconciliation would then leave A's
  // first "进行中" row stranded next to A's completed report. Grouping by key
  // globally lets the terminal supersede every preceding non-terminal anchor of
  // the SAME execution regardless of what splits them in the DOM.
  //
  // Within a key, anchors are segmented by terminal rows so multiple executions
  // reusing one step_id are reconciled independently: a terminal supersedes only
  // the non-terminal anchors of ITS OWN execution (those preceding it since the
  // last terminal), while anchors started AFTER an earlier terminal (a fresh
  // execution) are preserved. The trailing (still-open) execution — anchors
  // after the last terminal, or all anchors when there is no terminal — keeps
  // only its LATEST anchor (进行中 → 已暂停, or a resumed 进行中 superseding the
  // earlier paused row).
  //
  // Bubbles already sit in strict (__convTs, __convIdx) order, so filtering by
  // key preserves each execution's chronological order. `.history-step-header`
  // separators (no `__convStepKey`) are skipped.
  const bubbles = Array.from(container.children).filter(
    (c) => c.__convStepKey !== undefined);
  const byKey = new Map();
  for (const c of bubbles) {
    let arr = byKey.get(c.__convStepKey);
    if (!arr) { arr = []; byKey.set(c.__convStepKey, arr); }
    arr.push(c);
  }
  const toRemove = [];
  const flushOpenSegment = (segStatusRows, segLatest) => {
    // No terminal closed this segment — keep only the latest anchor.
    for (const sr of segStatusRows) {
      if (sr !== segLatest) toRemove.push(sr);
    }
  };
  for (const arr of byKey.values()) {
    let segStatusRows = [];     // non-terminal anchors in the current execution
    let segLatest = null;       // latest anchor in the current execution
    for (const c of arr) {
      if (c.__convTerminalRow) {
        // A terminal closes this execution: drop ALL of its non-terminal
        // anchors (the report card is the execution's final state), then start
        // a fresh execution segment.
        for (const sr of segStatusRows) toRemove.push(sr);
        segStatusRows = [];
        segLatest = null;
        continue;
      }
      if (c.__convStatusRow) {
        segStatusRows.push(c);
        if (!segLatest
            || c.__convTs > segLatest.__convTs
            || (c.__convTs === segLatest.__convTs && c.__convIdx > segLatest.__convIdx)) {
          segLatest = c;
        }
      }
    }
    flushOpenSegment(segStatusRows, segLatest);
  }
  for (const c of toRemove) container.removeChild(c);
}

// Stable per-turn key grouping a streamed turn's partial progress lines with
// the final assistant result that supersedes them. record_stream_progress and
// record_response both stamp the same (step_id, attempt), so a partial and its
// terminal result share this key.
function progressTurnKey(norm) {
  const stepId = (norm && norm.stepId) || "";
  const attempt = norm && norm.attempt != null ? norm.attempt : "";
  return stepId + "#" + attempt + "#assistant";
}

// Pure: given the full ordered records array, return the Set of indices of
// `partial` records whose OWN turn has reached a final (non-partial) assistant
// result — i.e. a partial is superseded only by a non-partial assistant record
// of the same (step_id, attempt) key that appears AFTER it in the stream. A
// turn with only partials (no final yet) supersedes nothing — its progress
// stays visible live.
//
// Order matters because `_reset_retry_counter_for_new_call` resets retry_count
// to 0 for each new discovery round / fix iteration / revision while those
// re-runs reuse the same step_id and per-step jsonl file. So an earlier round's
// final result and a later round's freshly-streaming partials share the same
// (step_id, attempt=0) key; a positional check keeps the later round's live
// progress visible until ITS OWN final lands. Exposed for unit testing.
function markSupersededProgress(records) {
  const norms = [];
  for (let i = 0; i < records.length; i++) {
    let norm = null;
    try { norm = normalizeRecord(records[i]); } catch (_) { norm = null; }
    norms.push(norm);
  }
  // Walk backward, tracking keys finalized strictly after the current index.
  // A partial is superseded iff a later final shares its key.
  const finalizedAfter = new Set();
  const superseded = new Set();
  for (let i = norms.length - 1; i >= 0; i--) {
    const norm = norms[i];
    if (!norm || norm.role !== "assistant") continue;
    if (norm.partial) {
      if (finalizedAfter.has(progressTurnKey(norm))) {
        superseded.add(i);
      }
    } else {
      finalizedAfter.add(progressTurnKey(norm));
    }
  }
  return superseded;
}

// Pure: given the full ordered records array, return an array (same length as
// `records`) of stable per-record *segment keys*. A segment key groups exactly
// the run of `partial` fragments that belong to ONE turn — the same batch the
// turn's final result later collapses into a single assistant bubble — so the
// live (in-progress) view can accumulate those fragments into one bubble and
// stay consistent with the final collapsed form.
//
// A record's segment key is:
//   * null               — for non-assistant or non-partial records.
//   * progressTurnKey(norm) + '#seg' + N
//                        — for a partial, where N is the count of FINAL
//                          (non-partial) assistant results sharing the same
//                          progressTurnKey that appear strictly before this
//                          record.
//
// The `#segN` suffix is what keeps multi-round turns apart: discovery
// continue / fix-loop re-runs reuse the same (step_id, attempt=0) — hence the
// same progressTurnKey — but a later round's partials follow the earlier
// round's final, so they land at a higher final-count N and form a distinct
// segment. This mirrors `markSupersededProgress`'s positional supersede rule
// (a final supersedes only the partials before it that share its key), so the
// two never disagree about which fragments belong together. O(n), DOM-free,
// exposed for unit testing.
function partialSegments(records) {
  const segments = new Array(records.length).fill(null);
  // progressTurnKey -> number of finals seen so far for that key.
  const finalCounts = Object.create(null);
  for (let i = 0; i < records.length; i++) {
    let norm = null;
    try { norm = normalizeRecord(records[i]); } catch (_) { norm = null; }
    if (!norm || norm.role !== "assistant") continue;
    const turnKey = progressTurnKey(norm);
    if (norm.partial) {
      const seen = finalCounts[turnKey] || 0;
      segments[i] = turnKey + "#seg" + seen;
    } else {
      finalCounts[turnKey] = (finalCounts[turnKey] || 0) + 1;
    }
  }
  return segments;
}

// Remove already-rendered partial bubbles whose turn has been finalized. Driven
// by `markSupersededProgress` so the render path and the unit test share one
// discriminator. Only `.conv-partial` bubbles are ever removed.
function removeSupersededProgress(container, records) {
  const superseded = markSupersededProgress(records);
  if (!superseded.size) return;
  for (const child of Array.from(container.children)) {
    if (child.__convPartial && superseded.has(child.__convIdx)) {
      container.removeChild(child);
    }
  }
}

// --- live partial accumulation --------------------------------------------
//
// While a turn streams its partial (stream_progress) fragments, all fragments
// of one segment (one `partialSegments` key) are folded into ONE accumulating
// assistant bubble rather than each spawning its own bubble. The bubble lays
// its content out exactly like a final no-result assistant turn
// (`renderAssistantProcessInline`), and its head/timestamp track the latest
// fragment so it reads as a single live assistant message — matching the form
// it collapses into once the turn's final result lands and
// `removeSupersededProgress` drops it.

// Build a fresh accumulating bubble for a partial fragment. Mirrors the
// assistant row that `renderConversationRecord` builds for a no-result turn
// (head + `.conv-bubble` + `.assistant-process-inline`), plus the `.conv-partial`
// marker class so `removeSupersededProgress` can drop it. References to the head
// and inline-process container are stashed on the row so `appendPartialFragment`
// can extend them without `querySelector` / `replaceChild` (absent from the test
// DOM stub).
function buildPartialBubble(norm) {
  const row = el("div", "history-record conv-record role-assistant conv-partial");
  const head = renderRecordHead(norm);
  const bubble = el("div", "conv-bubble");
  const inline = el("div", "assistant-process-inline");
  bubble.appendChild(inline);
  row.appendChild(head);
  row.appendChild(bubble);
  row.__partialHead = head;
  row.__partialBubble = bubble;
  row.__partialInline = inline;
  // Most-complete agent/model seen so far for this accumulating bubble. Updated
  // to the latest non-empty value as each fragment arrives so the live badge
  // tracks the real agent from the first fragment and upgrades to agent·model
  // once a later fragment carries the parsed model name.
  row.__partialAgentName = null;
  row.__partialModelName = null;
  // Per-bubble chip registry: keyed by tool_use_id, so the terminal
  // (tool_result) fragment for an id upgrades the SAME in-flight chip rather
  // than appending a new one.
  row.__chipRegistry = new Map();
  refreshPartialAgentBadge(row, norm);
  applyFragmentToBubble(row, norm);
  return row;
}

// Prepend / refresh the agent·model badge on an accumulating partial bubble.
// The badge is shown ONLY once a fragment carries a non-empty agentName; it is
// inserted above the inline-process container (mirroring the final assistant
// bubble, where the badge is the bubble's first child). On later fragments the
// existing badge is updated in place — no new bubble, no DOM churn beyond the
// badge text — so an agent-only badge upgrades to "agent · model" once the
// model name is parsed mid-stream. Records that never carry an agentName render
// no badge and no placeholder, staying byte-compatible with legacy streams.
function refreshPartialAgentBadge(row, norm) {
  if (norm && typeof norm.agentName === "string" && norm.agentName) {
    // A changed agent (e.g. a retry/rotation whose fragments reuse this
    // accumulating bubble) invalidates the model cached for the OLD agent —
    // drop it so the badge does not show "newAgent · oldModel". The new
    // agent's own model (if any) is re-applied below from this same fragment
    // or a later one; until then the badge shows the new agent name alone.
    if (row.__partialAgentName && row.__partialAgentName !== norm.agentName) {
      row.__partialModelName = null;
    }
    row.__partialAgentName = norm.agentName;
  }
  if (norm && typeof norm.modelName === "string" && norm.modelName) {
    row.__partialModelName = norm.modelName;
  }
  const text = formatAgentBadgeText(row.__partialAgentName, row.__partialModelName);
  if (!text) return;
  const bubble = row.__partialBubble;
  if (!bubble) return;
  let badge = row.__partialBadge;
  if (badge) {
    badge.textContent = text;
    return;
  }
  badge = el("span", "agent-badge");
  badge.textContent = text;
  // Insert above the inline-process container (the bubble's first child) so the
  // badge sits at the top of the bubble, consistent with the final form.
  bubble.insertBefore(badge, bubble.childNodes[0] || null);
  row.__partialBadge = badge;
}

// Apply one stream_progress fragment to a bubble: either upgrade an existing
// chip via tool_use_id (state-machine path) or append text via the legacy
// renderToolMarkers fallback (text / thinking deltas, or records that lack
// the structured tool_use_id field).
function applyFragmentToBubble(row, norm) {
  const inline = row.__partialInline;
  if (!inline) return;
  if (norm && typeof norm.toolUseId === "string" && norm.toolUseId) {
    const reg = row.__chipRegistry || (row.__chipRegistry = new Map());
    const content = typeof norm.content === "string" ? norm.content : "";
    // Parse the bracket marker for name + header. Fragment text is exactly
    // `[<Name>...]` so the first known-name match drives the chip identity;
    // fall back to "Tool" if no name matches.
    const nameMatch = TOOL_MARKER_RE.exec(content);
    TOOL_MARKER_RE.lastIndex = 0;
    const name = nameMatch ? nameMatch[1] : "Tool";
    const parsed = nameMatch ? parseToolBracket(name, nameMatch[0])
      : { name: name, header: "", status: "in-flight" };
    const existing = reg.get(norm.toolUseId);
    if (norm.isError === true) {
      const chip = existing || (() => {
        const c = createInFlightChip(parsed.name, parsed.header);
        if (c.dataset) c.dataset.toolUseId = norm.toolUseId;
        reg.set(norm.toolUseId, c);
        inline.appendChild(c);
        return c;
      })();
      upgradeChipToFailure(chip, parsed.header, norm.toolDetail);
    } else if (norm.isError === false) {
      const chip = existing || (() => {
        const c = createInFlightChip(parsed.name, parsed.header);
        if (c.dataset) c.dataset.toolUseId = norm.toolUseId;
        reg.set(norm.toolUseId, c);
        inline.appendChild(c);
        return c;
      })();
      upgradeChipToSuccess(chip, parsed.header, norm.toolDetail);
    } else {
      // in-flight (tool_use). Skip if we already have a chip for this id —
      // the daemon emits exactly one in-flight per id, but a duplicate
      // mid-stream must not produce a second chip.
      if (!existing) {
        const chip = createInFlightChip(parsed.name, parsed.header);
        if (chip.dataset) chip.dataset.toolUseId = norm.toolUseId;
        reg.set(norm.toolUseId, chip);
        inline.appendChild(chip);
      }
    }
    return;
  }
  // No structured tool_use_id — append content via the legacy bracket-marker
  // parser. This is the path for text / thinking fragments and for any
  // pre-G3 jsonl whose stream_progress records lack the id field.
  const content = typeof norm.content === "string" ? norm.content : "";
  for (const node of renderToolMarkers(content)) inline.appendChild(node);
}

// Extend an existing accumulating bubble with a newly-arrived fragment of the
// same segment: append the fragment's rendered nodes into the inline-process
// container, and refresh the head (rebuilt via `renderRecordHead`) so the bubble
// shows the LATEST fragment's timestamp — the same head structure the final
// state uses, so the displayed time is byte-identical. The head swap uses
// insertBefore + removeChild (no `replaceChild`, which the test DOM stub lacks).
function appendPartialFragment(row, norm) {
  refreshPartialAgentBadge(row, norm);
  applyFragmentToBubble(row, norm);
  const oldHead = row.__partialHead;
  const newHead = renderRecordHead(norm);
  if (oldHead && oldHead.parentNode === row) {
    row.insertBefore(newHead, oldHead);
    row.removeChild(oldHead);
  } else {
    row.insertBefore(newHead, row.childNodes[0] || null);
  }
  row.__partialHead = newHead;
}

// Find the live accumulating bubble for `segKey` already present in `container`,
// or null. Matches on the `__convSegmentKey` tag stamped by
// `addConversationRecords`; `.history-step-header` separators carry no such tag
// and are skipped.
function findLivePartialBubble(container, segKey) {
  for (const child of container.children) {
    if (child.__convPartial && child.__convSegmentKey === segKey) return child;
  }
  return null;
}

// Insert `bubble` into `container` keeping all bubbles ordered globally by
// (__convTs, __convIdx). Existing `.history-step-header` rows are skipped
// during the scan — they are stateless separators that get rebuilt by
// `rebuildStepHeaders` after the new bubble has settled into its slot.
function insertBubbleSorted(container, bubble) {
  let ref = null;
  for (const child of container.children) {
    if (child.__convIdx === undefined) continue;
    if (child.__convTs > bubble.__convTs ||
        (child.__convTs === bubble.__convTs &&
         child.__convIdx > bubble.__convIdx)) {
      ref = child;
      break;
    }
  }
  container.insertBefore(bubble, ref);
}

// Recompute `.history-step-header` separator rows from the current bubble
// order. Headers are stateless — they get fully removed and re-inserted so
// boundaries move with any new in-between bubbles. Stateful bubbles (folds,
// raw toggles, chips) are NEVER touched.
function rebuildStepHeaders(container) {
  const existing = Array.from(container.children).filter(
    (c) => c.classList && c.classList.contains("history-step-header"),
  );
  for (const h of existing) container.removeChild(h);

  // A header is inserted at the start of every CONTIGUOUS run of a step key —
  // i.e. whenever the key differs from the immediately-previous bubble. Under
  // strict chronological ordering one step_id can be split by another step's
  // records (e.g. SELF_CHECK(A) → IMPLEMENT(B) → SELF_CHECK(A) on a
  // retry/revision loop). A re-appearing step's records physically sit AFTER
  // the interleaving step, so they MUST get their own boundary header: without
  // one the second A segment would render beneath B's header and sticky
  // navigation would mis-attribute that content to B (its viewport-top step
  // would resolve to IMPLEMENT, not SELF_CHECK). Emitting a header per
  // contiguous run keeps each physical segment correctly attributed to its own
  // step. Adjacent same-key bubbles still collapse into one header (no header
  // mid-run) because `lastKey` only changes at a real boundary.
  let lastKey = null;
  const children = Array.from(container.children);
  for (const child of children) {
    if (child.__convStepKey === undefined) continue;
    const key = child.__convStepKey;
    if (key !== lastKey) {
      const header = el("div", "history-step-header");
      // Use the paradigm step name (DISCOVERY / ANALYZE / …); unknown step
      // types fall back to the record's original label / step key.
      const title = stepHeaderLabel(
        child.__convStepType,
        child.__convStepLabel || child.__convStepKey,
      );
      header.appendChild(el("h5", "history-step-title", title));
      container.insertBefore(header, child);
    }
    lastKey = key;
  }
}

function renderHistoryRecords(flowId, records, append) {
  renderConversation($("history-detail"), records, append);
}

// ---------------------------------------------------------------------------
// Viewport-driven sticky floating step header (G5)
// ---------------------------------------------------------------------------
//
// As the reader scrolls a conversation, a floating banner pinned to the top of
// the scroll area shows the title of whichever step's content currently sits at
// the viewport top. It is the JS-driven counterpart of the `.history-step-header`
// separator rows: the SAME paradigm step label, surfaced when the original
// header has scrolled out of view, and hidden the instant the original header is
// itself at the top (mutual exclusion). Clicking it smooth-scrolls that step's
// original header back to the very top. The logic is identical for the
// running-flow view (`#flow-conversation`, its own scroller) and the history
// view (`#history-detail`, whose scroller is the enclosing
// `.history-detail-pane`), so both share `computeStickyStep` + the helpers below.
//
// A small tolerance absorbs fractional scroll positions so a header sitting
// exactly at the top reliably counts as "visible" (→ hide the float).
const STICKY_EPS = 1;

// Pure, DOM-free: given each step header's content-relative offset
// (`headerOffsets[i].top`, sorted ascending) and the scroller's current
// `scrollTop`, return the step whose content the viewport top falls within —
// `{ index, label, key }` — or `null` when the floating header must be hidden.
//
// The float is hidden (returns null) when the viewport top is ABOVE the first
// header, or when a header sits essentially AT the viewport top (its original
// header is visible, so the float would be a redundant duplicate). Otherwise it
// reflects the LAST header that has scrolled strictly above the viewport top —
// so scrolling down advances to the next step the moment its header crosses the
// top, and scrolling up falls back to the previous step the instant its content
// re-enters the top. The returned step reflects ONLY the viewport-top position,
// never the flow's currently-executing step.
// `revealPx` is the mutual-exclusion band below the viewport top: a header
// whose offset is within `revealPx` of the top is treated as "visible in the
// viewport" and hides the float, so the floating banner is never displayed at
// the same time as a visible original header. The DOM path passes the VISIBLE
// SCROLL VIEWPORT HEIGHT (see `stickyRevealPx`), so the float hides as soon as
// any original header enters the visible viewport — not merely once it reaches
// the narrow band of the float banner's own height. The float therefore shows
// only while scrolled into a step region taller than the viewport (its next
// header off-screen below). It defaults to the bare `STICKY_EPS` tolerance;
// pure callers that omit it keep the 1px-only behavior, and callers that pass a
// specific band (e.g. the float banner height) get exactly that band.
function computeStickyStep(headerOffsets, scrollTop, revealPx) {
  if (!Array.isArray(headerOffsets) || !headerOffsets.length) return null;
  const reveal = Number.isFinite(revealPx) && revealPx > STICKY_EPS
    ? revealPx : STICKY_EPS;
  const y = Number.isFinite(scrollTop) ? Math.max(0, scrollTop) : 0;
  let active = null;
  for (let i = 0; i < headerOffsets.length; i++) {
    const h = headerOffsets[i];
    const top = h && Number(h.top);
    if (!Number.isFinite(top)) continue;
    if (top <= y - STICKY_EPS) {
      // Header strictly above the viewport top → its step owns the top edge.
      active = { index: i, label: h.label, key: h.key };
    } else if (top <= y + reveal) {
      // Header at — or just below, within the reveal band — the viewport top.
      // Its original header is (about to be) visible at the top, so the float
      // must stay hidden (mutual exclusion), regardless of any earlier
      // candidate.
      return null;
    } else {
      // Header below the viewport top; offsets are ascending so we can stop.
      break;
    }
  }
  return active;
}

// Pure: the scrollTop that places step `index`'s original header exactly at the
// top of the scroll area (the click-to-locate target). Returns null for an
// out-of-range index so a stale click is a no-op rather than a NaN scroll.
function stickyScrollTarget(headerOffsets, index) {
  if (!Array.isArray(headerOffsets)) return null;
  const h = headerOffsets[index];
  const top = h && Number(h.top);
  return Number.isFinite(top) ? Math.max(0, top) : null;
}

// Read the title text out of a `.history-step-header` separator row (its inner
// `.history-step-title`), falling back to the row's own text.
function stickyHeaderTitle(headerEl) {
  for (const k of (headerEl.children || [])) {
    if (k.classList && k.classList.contains("history-step-title")) {
      return k.textContent;
    }
  }
  return headerEl.textContent || "";
}

// Measure the content-relative top offset of every `.history-step-header` that
// is a direct child of `content`, relative to `scroller`'s scroll-content
// origin (so the value is directly comparable with `scroller.scrollTop`).
// Uses getBoundingClientRect so it is correct whether `content` IS the scroller
// (flow view) or is nested inside it (history view → the detail pane scrolls).
function measureStepHeaderOffsets(scroller, content) {
  const out = [];
  if (!scroller || !content) return out;
  const sRect = scroller.getBoundingClientRect();
  const scrollTop = scroller.scrollTop || 0;
  let idx = 0;
  for (const child of content.children) {
    if (!child.classList || !child.classList.contains("history-step-header")) {
      continue;
    }
    const r = child.getBoundingClientRect();
    out.push({
      index: idx,
      top: r.top - sRect.top + scrollTop,
      label: stickyHeaderTitle(child),
    });
    idx += 1;
  }
  return out;
}

// Smooth-scroll `scroller` so its content offset `top` reaches the top, falling
// back to an instant jump where the smooth-options form is unsupported.
function smoothScrollTo(scroller, top) {
  if (!scroller) return;
  const dest = Math.max(0, Number(top) || 0);
  if (typeof scroller.scrollTo === "function") {
    try { scroller.scrollTo({ top: dest, behavior: "smooth" }); return; }
    catch (_) { /* options form unsupported — fall through to instant */ }
  }
  scroller.scrollTop = dest;
}

function hideStickyHeader(floatEl) {
  if (!floatEl) return;
  floatEl.classList.add("hidden");
  floatEl.__index = -1;
}

// The reveal band passed to computeStickyStep — the height of the VISIBLE
// scroll viewport. The float must hide as soon as ANY original step header is
// visible in the scroll viewport (mutual exclusion), not merely once that
// header has scrolled to within the float banner's own height of the top:
// otherwise the floating banner sits on screen at the same time as a fully
// visible original header just below it. Using the scroller's on-screen height
// makes "within the reveal band" equivalent to "visible in the viewport", so
// the float only shows while scrolled into a step region taller than the
// viewport (its next header off-screen below) and hides the instant a boundary
// header emerges into view. Falls back to the float banner's height, then to
// the bare STICKY_EPS, when the viewport height is unavailable (e.g. before
// layout).
function stickyRevealPx(scroller, floatEl) {
  if (scroller && typeof scroller.getBoundingClientRect === "function") {
    const h = Number(scroller.getBoundingClientRect().height);
    if (Number.isFinite(h) && h > STICKY_EPS) return h;
  }
  const inner = floatEl && floatEl.__inner;
  if (inner && typeof inner.getBoundingClientRect === "function") {
    const h = Number(inner.getBoundingClientRect().height);
    if (Number.isFinite(h) && h > STICKY_EPS) return h;
  }
  return STICKY_EPS;
}

// Recompute which step the viewport top is in and reflect it in the float.
//
// The header offsets are RE-MEASURED from the live DOM on every update (not
// read from a stale mount-time cache): expanding a folded message, a font /
// image reflow, or a window resize / rotation shifts the headers below it, and
// a cached offset would then switch the float early and scroll a click to the
// wrong place. measureStepHeaderOffsets is scroll-invariant (it adds back
// scrollTop), so re-measuring at any scroll position yields the same
// content-space offsets — only their REAL post-reflow positions change. The
// fresh offsets are also stored back on the scroller so the click handler
// locates against current layout too.
function updateStickyHeader(scroller) {
  if (!scroller) return;
  const floatEl = scroller.__convStickyFloat;
  if (!floatEl) return;
  const content = scroller.__convStickyContent;
  const offsets = content
    ? measureStepHeaderOffsets(scroller, content)
    : (scroller.__convStickyOffsets || []);
  scroller.__convStickyOffsets = offsets;
  const active = computeStickyStep(
    offsets, scroller.scrollTop || 0, stickyRevealPx(scroller, floatEl));
  if (!active) { hideStickyHeader(floatEl); return; }
  floatEl.__index = active.index;
  floatEl.__title.textContent = active.label;
  floatEl.classList.remove("hidden");
}

// Build the floating-header element (lazily, once per scroller). It is a
// zero-height `position: sticky` anchor whose absolutely-positioned inner button
// paints the banner over the content top, so it pins to the viewport top without
// consuming layout space or shifting the conversation. Clicking it locates the
// active step's original header back to the top.
function buildStickyHeaderEl(scroller) {
  const floatEl = el("div", "conv-sticky-header hidden");
  const inner = el("button", "conv-sticky-header__inner");
  inner.type = "button";
  const titleEl = el("span", "conv-sticky-header__title");
  inner.appendChild(titleEl);
  floatEl.appendChild(inner);
  floatEl.__inner = inner;
  floatEl.__title = titleEl;
  floatEl.__index = -1;
  inner.addEventListener("click", () => {
    // Re-measure against current layout before locating: a fold-expand / reflow
    // since the last scroll may have moved the target header, and a stale cached
    // offset would scroll to the wrong place.
    const content = scroller.__convStickyContent;
    if (content) {
      scroller.__convStickyOffsets = measureStepHeaderOffsets(scroller, content);
    }
    const idx = floatEl.__index;
    const target = stickyScrollTarget(scroller.__convStickyOffsets || [], idx);
    if (target == null) return;
    // Pure navigation: scroll only — never mutate step or flow state. Hide
    // eagerly for snappy feedback; the scroll-driven update keeps it hidden
    // once the original header settles at the top.
    smoothScrollTo(scroller, target);
    hideStickyHeader(floatEl);
  });
  return floatEl;
}

// Ensure the floating step header is mounted on `scroller` (as its first child,
// so the sticky anchor pins to the scroll viewport top), wire the scroll
// listener once, refresh the cached header offsets from the freshly-rendered
// `content`, and update the float. Idempotent — safe to call after every render
// (a full rebuild wipes the float via innerHTML="", so re-inserting the same
// detached node re-mounts it without disturbing fold/raw/chip state on bubbles).
function ensureStickyHeaderMounted(scroller, content) {
  if (!scroller || !content) return;
  let floatEl = scroller.__convStickyFloat;
  if (!floatEl) {
    floatEl = buildStickyHeaderEl(scroller);
    scroller.__convStickyFloat = floatEl;
    const onScroll = () => {
      updateStickyHeader(scroller);
      // A real user scroll of the running-flow conversation is the authoritative
      // signal for the follow-the-bottom intent the silent rebuild consults
      // (#260): scrolling up drops it, scrolling back to the bottom re-arms it.
      // Programmatic appends grow scrollHeight without firing a scroll event, so
      // this only ever captures deliberate reader motion. Gated to
      // #flow-conversation — the history view has no silent progression rebuild.
      if (scroller.id === "flow-conversation") {
        state.flowConversationFollowingBottom = isNearBottom(scroller);
      }
    };
    scroller.addEventListener("scroll", onScroll);
    scroller.__convStickyOnScroll = onScroll;
    // A window resize / orientation change reflows the conversation and shifts
    // every header. Re-measure + re-evaluate the float on resize so the sticky
    // header and click-to-locate keep tracking the real layout. Guarded for
    // headless / non-window environments (the test DOM stub has no window).
    if (typeof window !== "undefined"
        && typeof window.addEventListener === "function") {
      const onResize = () => updateStickyHeader(scroller);
      window.addEventListener("resize", onResize);
      scroller.__convStickyOnResize = onResize;
    }
  }
  scroller.__convStickyContent = content;
  if (scroller.firstChild !== floatEl) {
    scroller.insertBefore(floatEl, scroller.firstChild || null);
  }
  scroller.__convStickyOffsets = measureStepHeaderOffsets(scroller, content);
  updateStickyHeader(scroller);
}

// View-specific mounts: the running-flow view's `#flow-conversation` is its own
// scroller; the history view's `#history-detail` is nested inside the scrolling
// `.history-detail-pane`. Both funnel through the shared logic above.
function refreshFlowStickyHeader() {
  const conv = $("flow-conversation");
  if (conv) ensureStickyHeaderMounted(conv, conv);
}

function refreshHistoryStickyHeader() {
  const detail = $("history-detail");
  const scroller = historyScrollContainer();
  if (detail && scroller) ensureStickyHeaderMounted(scroller, detail);
}

// ---------------------------------------------------------------------------
// Conversation rendering engine
// ---------------------------------------------------------------------------
//
// A self-contained renderer shared by the history view and the running flow
// view. Three layers:
//   renderMarkdown(text)      — lightweight Markdown → DOM (no dependencies)
//   renderToolMarkers(text)   — split inline [Tool: …] markers into own blocks
//   renderConversationRecord  — a role-tagged bubble around the above
//
// Everything is built with createElement / textContent / createTextNode, so
// arbitrary assistant text can never inject HTML.

// --- inline span rendering -------------------------------------------------

// Render `**bold**` / `__bold__` and `*italic*` / `_italic_` within one line.
function renderEmphasisLine(parent, text) {
  const RE = /(\*\*|__)(.+?)\1|(\*|_)([^*_]+?)\3/g;
  let last = 0;
  let m;
  while ((m = RE.exec(text)) !== null) {
    if (m.index > last) {
      parent.appendChild(document.createTextNode(text.slice(last, m.index)));
    }
    if (m[1]) {
      parent.appendChild(el("strong", null, m[2]));
    } else {
      parent.appendChild(el("em", null, m[4]));
    }
    last = m.index + m[0].length;
  }
  if (last < text.length) {
    parent.appendChild(document.createTextNode(text.slice(last)));
  }
}

// Render emphasis across a multi-line block, turning `\n` into <br>.
function renderEmphasis(parent, text) {
  const lines = String(text == null ? "" : text).split("\n");
  lines.forEach((line, idx) => {
    if (idx > 0) parent.appendChild(document.createElement("br"));
    renderEmphasisLine(parent, line);
  });
}

// Render inline content: pull out `code spans` first (literal inside), then
// apply emphasis to the remaining text.
function renderInline(parent, text) {
  const parts = String(text == null ? "" : text).split(/(`[^`]+`)/);
  for (const part of parts) {
    if (!part) continue;
    if (part.length >= 2 && part[0] === "`" && part[part.length - 1] === "`") {
      parent.appendChild(el("code", "md-inline-code", part.slice(1, -1)));
    } else {
      renderEmphasis(parent, part);
    }
  }
}

// --- block rendering -------------------------------------------------------

const MD_LIST_RE = /^\s*([-*+]|\d+[.)])\s+/;
const MD_HEADING_RE = /^(#{1,6})\s+(.*)$/;
const MD_FENCE_RE = /^\s*(```+|~~~+)(.*)$/;
const MD_QUOTE_RE = /^\s*>\s?/;

// Lightweight Markdown renderer: headings, fenced/inline code, ordered &
// unordered lists, blockquotes, bold/italic, paragraphs. Returns a
// DocumentFragmentNode ready to append.
function renderMarkdown(text) {
  const frag = document.createDocumentFragment();
  const lines = String(text == null ? "" : text).split("\n");
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    // fenced code block
    const fence = line.match(MD_FENCE_RE);
    if (fence) {
      const marker = fence[1][0];
      const lang = fence[2].trim();
      const closeRe = new RegExp("^\\s*\\" + marker + "{3,}\\s*$");
      const buf = [];
      i += 1;
      while (i < lines.length && !closeRe.test(lines[i])) {
        buf.push(lines[i]);
        i += 1;
      }
      i += 1; // consume the closing fence (or run off the end)
      const pre = el("pre", "md-code");
      const code = el("code", null, buf.join("\n"));
      if (lang) code.dataset.lang = lang;
      pre.appendChild(code);
      frag.appendChild(pre);
      continue;
    }

    // blank line — paragraph separator
    if (!line.trim()) { i += 1; continue; }

    // heading
    const h = line.match(MD_HEADING_RE);
    if (h) {
      const level = h[1].length;
      const node = el("h" + level, "md-h md-h" + level);
      renderInline(node, h[2]);
      frag.appendChild(node);
      i += 1;
      continue;
    }

    // list — consume consecutive items of the same family
    if (MD_LIST_RE.test(line)) {
      const ordered = /^\s*\d+[.)]\s+/.test(line);
      const list = el(ordered ? "ol" : "ul", "md-list");
      while (i < lines.length && MD_LIST_RE.test(lines[i])) {
        const li = el("li");
        renderInline(li, lines[i].replace(MD_LIST_RE, ""));
        list.appendChild(li);
        i += 1;
      }
      frag.appendChild(list);
      continue;
    }

    // blockquote
    if (MD_QUOTE_RE.test(line)) {
      const buf = [];
      while (i < lines.length && MD_QUOTE_RE.test(lines[i])) {
        buf.push(lines[i].replace(MD_QUOTE_RE, ""));
        i += 1;
      }
      const bq = el("blockquote", "md-quote");
      renderInline(bq, buf.join("\n"));
      frag.appendChild(bq);
      continue;
    }

    // paragraph — gather until a blank line or a block-level marker
    const buf = [];
    while (i < lines.length && lines[i].trim() &&
           !MD_FENCE_RE.test(lines[i]) &&
           !MD_HEADING_RE.test(lines[i]) &&
           !MD_LIST_RE.test(lines[i]) &&
           !MD_QUOTE_RE.test(lines[i])) {
      buf.push(lines[i]);
      i += 1;
    }
    const p = el("p", "md-p");
    renderInline(p, buf.join("\n"));
    frag.appendChild(p);
  }
  return frag;
}

// --- inline tool-call markers ---------------------------------------------

// Tool names that the streaming/history layers embed inline as `[Name: …]`.
const TOOL_MARKER_NAMES = [
  "Tool", "Read", "Bash", "Edit", "Write", "Grep", "Glob", "Task",
  "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch", "LS", "TodoWrite",
  "Search", "Fetch",
];
const TOOL_MARKER_RE = new RegExp(
  "\\[(" + TOOL_MARKER_NAMES.join("|") + ")\\b[^\\]\\n]*\\]", "g");

// Parse `[<Name> [: ] [✓|✗ ] <header>]` into `{name, header, status}`.
//
// The pre-G3 path used `inner.indexOf(":")` to slice off the header; that broke
// on the success / failure bracket grammar `[Read ✓ path · 87 lines]` (no
// colon → empty header → an empty "zombie" chip rendered next to the in-flight
// one). The new parser strips the leading name, then the optional `:` or
// `✓` / `✗` status glyph, returning a clean header plus the status glyph it
// observed.
function parseToolBracket(name, raw) {
  let inner = String(raw == null ? "" : raw);
  inner = inner.replace(/^\[/, "").replace(/\]$/, "");
  // Strip the leading "<name>" prefix.
  if (inner.slice(0, name.length) === name) {
    inner = inner.slice(name.length);
  }
  // Trim leading whitespace, then peel off the status glyph or colon.
  inner = inner.replace(/^\s+/, "");
  let status = "in-flight";
  if (inner.charAt(0) === "✓") {            // ✓
    status = "success"; inner = inner.slice(1);
  } else if (inner.charAt(0) === "✗") {     // ✗
    status = "failure"; inner = inner.slice(1);
  } else if (inner.charAt(0) === ":") {
    status = "in-flight"; inner = inner.slice(1);
  }
  return { name: name, header: inner.replace(/^\s+/, "").trim(), status: status };
}

// --- Chip state machine ----------------------------------------------------
//
// A `tool-marker` chip has three visual states keyed by `tool_use_id`:
//   * in-flight — dashed border, no glyph (tool_use seen, result pending)
//   * success — solid border + ✓ (tool_result with is_error=false)
//   * failure — solid border + ✗ (tool_result with is_error=true)
//
// `createInFlightChip` builds an in-flight chip; `upgradeChipToSuccess` /
// `upgradeChipToFailure` mutate the SAME node (preserving DOM identity so the
// bubble's child order is stable), swap the chip's status class, refresh the
// header text in place, and attach a default-folded (success) / default-open
// (failure) detail panel built by `renderToolDetailPanel(detail)`.

function setChipHeader(chip, name, header, status) {
  // Build header inside `chip` from scratch — keeps detail panel children
  // attached as siblings, only the head row is regenerated.
  while (chip.firstChild) {
    // Stop once we hit the right-aligned toggle or the appended detail
    // panel; the head is always the initial set of `<span>` children, and
    // the toggle / panel are siblings appended later by attachChipDetail.
    const child = chip.firstChild;
    if (child.classList && (
      child.classList.contains("tool-marker-toggle") ||
      child.classList.contains("tool-marker-details")
    )) {
      break;
    }
    chip.removeChild(child);
  }
  const glyph = status === "success" ? "✓"
    : status === "failure" ? "✗" : "";
  // Insert head as the first children, ahead of any toggle / details panel.
  // Head row order is: name → detail → glyph → (toggle, panel).
  const refNode = chip.firstChild;
  const n = el("span", "tool-marker-name", name);
  chip.insertBefore(n, refNode);
  if (header) {
    const d = el("span", "tool-marker-detail", header);
    chip.insertBefore(d, refNode);
  }
  if (glyph) {
    const g = el("span", "tool-marker-glyph", glyph);
    chip.insertBefore(g, refNode);
  }
}

function createInFlightChip(name, header) {
  const chip = el("div", "tool-marker in-flight");
  chip.__toolName = name;
  chip.__toolStatus = "in-flight";
  setChipHeader(chip, name, header || "", "in-flight");
  return chip;
}

function upgradeChipToSuccess(chip, header, detail) {
  if (!chip) return;
  chip.classList.remove("in-flight", "failure");
  chip.classList.add("success");
  chip.__toolStatus = "success";
  setChipHeader(chip, chip.__toolName || "Tool", header || "", "success");
  attachChipDetail(chip, detail, /*expanded=*/false);
}

function upgradeChipToFailure(chip, header, detail) {
  if (!chip) return;
  chip.classList.remove("in-flight", "success");
  chip.classList.add("failure");
  chip.__toolStatus = "failure";
  setChipHeader(chip, chip.__toolName || "Tool", header || "", "failure");
  attachChipDetail(chip, detail, /*expanded=*/true);
}

function attachChipDetail(chip, detail, expanded) {
  // Remove any prior toggle + details panel so an upgrade replaces them
  // rather than duplicating (the chip head row is rebuilt by setChipHeader
  // which already leaves any later siblings — the toggle / panel — alone).
  const old = Array.from(chip.children).filter(
    (c) => c.classList && (
      c.classList.contains("tool-marker-details") ||
      c.classList.contains("tool-marker-toggle")
    ));
  for (const o of old) chip.removeChild(o);
  if (!detail) return;
  const panel = el("div", "tool-marker-details" + (expanded ? " expanded" : " folded"));
  const toggle = el("button", "tool-marker-toggle",
    expanded
      ? tf("tool.detail.toggleHide", "hide details")
      : tf("tool.detail.toggleShow", "details"));
  toggle.type = "button";
  const body = el("div", "tool-marker-details-body");
  try {
    body.appendChild(renderToolDetailPanel(detail));
  } catch (err) {
    try { console.warn("tool detail render failed", err); }
    catch (_) { /* console may be absent */ }
    body.appendChild(el("pre", "tool-marker-details-fallback",
      _safeJsonStringify(detail)));
  }
  toggle.addEventListener("click", () => {
    const open = panel.classList.contains("expanded");
    panel.classList.toggle("expanded", !open);
    panel.classList.toggle("folded", open);
    toggle.textContent = !open
      ? tf("tool.detail.toggleHide", "hide details")
      : tf("tool.detail.toggleShow", "details");
    if (!open) {
      try { requestAnimationFrame(() => panel.scrollIntoView({ block: "nearest" })); }
      catch (_) { /* RAF / DOM optional in test env */ }
    }
  });
  panel.appendChild(body);
  // toggle sits as a direct child of the chip head row (right-aligned via
  // .tool-marker-toggle's `margin-left:auto`); the details panel follows it
  // and wraps to its own row via flex-basis:100%.
  chip.appendChild(toggle);
  chip.appendChild(panel);
}

function _safeJsonStringify(obj) {
  try { return JSON.stringify(obj, null, 2); }
  catch (_) { return String(obj); }
}

// --- Detail panel renderers (per detail.kind) ------------------------------
//
// `build_tool_detail_payload` (tool_formatters.py) emits a JSON-safe dict with
// a `kind` discriminator. Each sub-renderer maps one kind to DOM, producing
// the CLI-equivalent visual: line-numbered diffs for Edit / Write-overwrite,
// the full file content for Write-create, line-numbered text for Read,
// command + stdout / stderr for Bash, match lists for Grep / Glob, and a
// generic text fallback for unknown kinds.
const TOOL_DETAIL_RENDERERS = {};

function registerToolDetailRenderer(kind, fn) {
  if (kind && typeof fn === "function") TOOL_DETAIL_RENDERERS[kind] = fn;
}

function renderToolDetailPanel(detail) {
  if (!detail || typeof detail !== "object") {
    const frag = document.createDocumentFragment();
    frag.appendChild(el("p", "tool-detail-empty", tf("tool.detail.empty", "(no details available)")));
    return frag;
  }
  const fn = TOOL_DETAIL_RENDERERS[detail.kind] || TOOL_DETAIL_RENDERERS.text;
  return fn(detail);
}

// Render a unified diff with a dim line-number gutter and add/del/hunk/ctx
// coloring matching the CLI `display.render_diff` palette. Hunk `@@` lines
// reset the gutter to the diff's encoded new-side / old-side start.
function renderDiffPanel(detail) {
  const wrap = el("div", "tool-marker-diff");
  if (detail.file_path) {
    wrap.appendChild(el("div", "tool-marker-diff-path", detail.file_path));
  }
  const lines = String(detail.diff || "").split("\n");
  let oldLine = detail.old_start_line || 1;
  let newLine = detail.new_start_line || 1;
  for (const ln of lines) {
    if (ln.startsWith("---") || ln.startsWith("+++")) {
      // header lines — skip; we already show file path above
      continue;
    }
    let cls = "diff-ctx";
    let gutter = "";
    if (ln.startsWith("@@")) {
      cls = "diff-hunk";
      const m = /@@\s*-(\d+)(?:,\d+)?\s*\+(\d+)(?:,\d+)?\s*@@/.exec(ln);
      if (m) {
        oldLine = parseInt(m[1], 10) || 1;
        newLine = parseInt(m[2], 10) || 1;
      }
    } else if (ln.startsWith("+")) {
      cls = "diff-add"; gutter = String(newLine); newLine++;
    } else if (ln.startsWith("-")) {
      cls = "diff-del"; gutter = String(oldLine); oldLine++;
    } else if (ln.length) {
      cls = "diff-ctx"; gutter = String(newLine); newLine++; oldLine++;
    }
    const row = el("div", "diff-line " + cls);
    row.appendChild(el("span", "diff-gutter", gutter));
    row.appendChild(el("span", "diff-content", ln));
    wrap.appendChild(row);
  }
  if (detail.truncated) {
    wrap.appendChild(el("div", "diff-truncated", tf("tool.detail.truncated", "… (output truncated)")));
  }
  return wrap;
}

function renderTextWithLineNumbers(text, startLine) {
  const wrap = el("div", "tool-marker-text");
  const lines = String(text || "").split("\n");
  let n = startLine || 1;
  for (const line of lines) {
    const row = el("div", "text-line");
    row.appendChild(el("span", "text-gutter", String(n)));
    row.appendChild(el("span", "text-content", line));
    wrap.appendChild(row);
    n++;
  }
  return wrap;
}

registerToolDetailRenderer("edit_diff", renderDiffPanel);
registerToolDetailRenderer("write_diff", renderDiffPanel);

registerToolDetailRenderer("write_full", (detail) => {
  const frag = document.createDocumentFragment();
  if (detail.file_path) {
    frag.appendChild(el("div", "tool-marker-diff-path", detail.file_path));
  }
  frag.appendChild(renderTextWithLineNumbers(detail.content, detail.start_line));
  if (detail.truncated) {
    frag.appendChild(el("div", "diff-truncated", tf("tool.detail.truncated", "… (output truncated)")));
  }
  return frag;
});

// A file changed but its text was never reported (codex `file_change` items).
// The panel shows the path and says so, rather than rendering a diff or an
// empty file body that would misstate what happened to the file.
registerToolDetailRenderer("file_path_only", (detail) => {
  const frag = document.createDocumentFragment();
  if (detail.file_path) {
    frag.appendChild(el("div", "tool-marker-diff-path", detail.file_path));
  }
  frag.appendChild(
    el("p", "tool-detail-empty", tf("tool.detail.noContent", "(no file content reported)")),
  );
  return frag;
});

registerToolDetailRenderer("read_text", (detail) => {
  const frag = document.createDocumentFragment();
  if (detail.file_path) {
    frag.appendChild(el("div", "tool-marker-diff-path", detail.file_path));
  }
  frag.appendChild(renderTextWithLineNumbers(detail.text, detail.start_line));
  if (detail.truncated) {
    frag.appendChild(el("div", "diff-truncated", tf("tool.detail.truncated", "… (output truncated)")));
  }
  return frag;
});

registerToolDetailRenderer("bash_output", (detail) => {
  const frag = document.createDocumentFragment();
  if (detail.command) {
    const cmd = el("div", "tool-marker-bash-cmd");
    cmd.appendChild(el("span", "tool-marker-bash-label", "$"));
    cmd.appendChild(el("span", "tool-marker-bash-text", String(detail.command)));
    frag.appendChild(cmd);
  }
  if (detail.stdout) {
    frag.appendChild(el("pre", "tool-marker-bash-stdout", String(detail.stdout)));
  }
  if (detail.stderr) {
    const sw = el("div", "tool-marker-bash-stderr-wrap");
    sw.appendChild(el("div", "tool-marker-bash-stderr-label", tf("tool.detail.stderr", "stderr")));
    sw.appendChild(el("pre", "tool-marker-bash-stderr", String(detail.stderr)));
    frag.appendChild(sw);
  }
  if (detail.truncated) {
    frag.appendChild(el("div", "diff-truncated", tf("tool.detail.truncated", "… (output truncated)")));
  }
  return frag;
});

function renderMatchList(items, emptyText) {
  const wrap = el("div", "tool-marker-matches");
  if (!items || !items.length) {
    wrap.appendChild(el("p", "tool-detail-empty", emptyText || tf("tool.detail.noMatches", "(no matches)")));
    return wrap;
  }
  for (const item of items) {
    wrap.appendChild(el("div", "tool-marker-match", String(item)));
  }
  return wrap;
}

// The pattern/path values are the tool call's own arguments — passed through as
// data; only the surrounding labels are translated.
function patternPathHead(detail) {
  const pattern = detail.pattern || "";
  const path = detail.path || "";
  const head = el("div", "tool-marker-diff-path");
  head.textContent = tf(
    "tool.detail.patternPath",
    `pattern=${pattern} path=${path}`,
    { pattern, path },
  );
  return head;
}

registerToolDetailRenderer("grep_matches", (detail) => {
  const frag = document.createDocumentFragment();
  if (detail.pattern || detail.path) {
    frag.appendChild(patternPathHead(detail));
  }
  frag.appendChild(renderMatchList(detail.matches, tf("tool.detail.noMatches", "(no matches)")));
  if (detail.truncated) {
    frag.appendChild(el("div", "diff-truncated", tf("tool.detail.truncated", "… (output truncated)")));
  }
  return frag;
});

registerToolDetailRenderer("glob_matches", (detail) => {
  const frag = document.createDocumentFragment();
  if (detail.pattern || detail.path) {
    frag.appendChild(patternPathHead(detail));
  }
  frag.appendChild(renderMatchList(detail.files, tf("tool.detail.noFiles", "(no files)")));
  if (detail.truncated) {
    frag.appendChild(el("div", "diff-truncated", tf("tool.detail.truncated", "… (output truncated)")));
  }
  return frag;
});

registerToolDetailRenderer("text", (detail) => {
  const wrap = el("div", "tool-marker-text-plain");
  wrap.appendChild(el("pre", "tool-marker-text-pre", String(detail.text || "")));
  if (detail.truncated) {
    wrap.appendChild(el("div", "diff-truncated", tf("tool.detail.truncated", "… (output truncated)")));
  }
  return wrap;
});

// --- Per-tool header / detail formatters (JS counterparts) -----------------
//
// JS mirror of `src/se3/engine/tool_formatters.py` (`format_tool_chip_header`
// + `build_tool_detail_payload`). The live path receives these pre-computed
// from the daemon via `stream_progress.tool_detail`, but the final non-partial
// assistant record (raw_json) carries only the raw tool_use / tool_result
// blocks — so the frontend MUST re-format the input/result here, otherwise
// the moment the live partial bubble is superseded by the final one the
// chip's header collapses to `JSON.stringify(input)` and the detail panel
// shows the literal result string (the regression that motivated this).
//
// The header strings returned here are the BODY only (e.g.
// `src/foo.py:0-200 · 87 lines`); the chip frame adds the name span and the
// ✓ / ✗ glyph from CSS, so we don't repeat the tool name in the body. This
// matches `_success_combined_*` / `_failure_use_body` on the Python side.

function _toolExtractText(data) {
  if (data == null) return "";
  if (typeof data === "string") return data;
  if (Array.isArray(data)) {
    const out = [];
    for (const it of data) {
      if (it && typeof it === "object" && typeof it.text === "string") out.push(it.text);
      else if (typeof it === "string") out.push(it);
    }
    return out.join("\n");
  }
  if (typeof data === "object") {
    if (typeof data.text === "string") return data.text;
    if (typeof data.content === "string") return data.content;
    if (Array.isArray(data.content)) return _toolExtractText(data.content);
  }
  return "";
}

function _toolTruncatePreview(text, max) {
  if (!text) return "";
  const s = String(text).replace(/\n/g, " ");
  const limit = max || 60;
  if (s.length <= limit) return s;
  return s.slice(0, Math.max(1, limit - 3)) + "...";
}

// JS mirror of `truncate_path` in tool_formatters.py: shorten the middle,
// never the tail. WHY: an unregistered file tool (codex's `Delete`) carries a
// file_path as its only key, so the generic key=value fallback below would cut
// the string mid-path at 30 chars and drop the very filename the chip exists to
// name. Relativization against the project root is Python-side only — the
// frontend has no project root on the raw_json path — so a path that fits is
// passed through whole.
function _toolTruncatePath(path, max) {
  if (!path) return "";
  const s = String(path);
  const limit = max || 160;
  if (s.length <= limit) return s;
  const parts = s.replace(/\\/g, "/").split("/");
  if (parts.length <= 1) return s;
  return `${parts[0]}/.../${parts[parts.length - 1]}`;
}

function _toolHasKey(input, key) {
  return !!input && typeof input === "object" &&
    Object.prototype.hasOwnProperty.call(input, key);
}

// Body-only header for in-flight (tool_use without result yet). Mirrors
// `_format_*_use` in tool_formatters.py, stripping the leading `<name>: `
// prefix since the chip frame adds the name span separately.
function _toolInFlightBody(toolName, input) {
  input = input || {};
  if (toolName === "Read") {
    const p = String(input.file_path || "?");
    const o = input.offset, l = input.limit;
    if (o != null && l != null) return `${p}:${o}-${Number(o) + Number(l)}`;
    if (o != null) return `${p}:${o}-`;
    if (l != null) return `${p} (${l} lines)`;
    return p;
  }
  if (toolName === "Edit") {
    const p = String(input.file_path || "?");
    // Key absence ≠ empty value (mirrors `_format_edit_use`): an upstream that
    // reports only *that* a file changed (codex file_change) omits the keys, so
    // there is no line count to show; a present-but-empty value keeps its count.
    if (!_toolHasKey(input, "old_string") && !_toolHasKey(input, "new_string")) return p;
    const oldS = String(input.old_string || "");
    const newS = String(input.new_string || "");
    const ol = oldS ? oldS.split("\n").length : 0;
    const nl = newS ? newS.split("\n").length : 0;
    if (ol || nl) return `${p} (${ol} lines → ${nl} lines)`;
    return p;
  }
  if (toolName === "Write") {
    const p = String(input.file_path || "?");
    if (!_toolHasKey(input, "content")) return p;
    const c = String(input.content || "");
    const n = c ? c.split("\n").length : 0;
    if (n) return `${p} (${n} lines)`;
    return `${p} (empty)`;
  }
  if (toolName === "Bash") {
    return _toolTruncatePreview(input.command || "", 50);
  }
  if (toolName === "Grep") {
    const pat = _toolTruncatePreview(input.pattern || "?", 30);
    const path = String(input.path || ".");
    return `/${pat}/ in ${path}`;
  }
  if (toolName === "Glob") {
    const pat = _toolTruncatePreview(input.pattern || "?", 30);
    const path = String(input.path || ".");
    return `${pat} in ${path}`;
  }
  // Generic fallback: short key=value pairs, up to 3
  const parts = [];
  let i = 0;
  for (const k of Object.keys(input)) {
    if (i++ >= 3) { parts.push("..."); break; }
    const v = input[k];
    let vs;
    if (typeof v === "string") {
      vs = k === "file_path" ? _toolTruncatePath(v) : _toolTruncatePreview(v, 30);
    } else if (typeof v === "number" || typeof v === "boolean") {
      vs = String(v);
    } else {
      try { vs = _toolTruncatePreview(JSON.stringify(v), 30); }
      catch (_) { vs = _toolTruncatePreview(String(v), 30); }
    }
    parts.push(`${k}=${vs}`);
  }
  return parts.join(", ");
}

// Body-only header for the success terminal state. Mirrors the
// `_SUCCESS_COMBINED` dispatch table in tool_formatters.py.
function _toolSuccessBody(toolName, input, resultData) {
  input = input || {};
  const text = _toolExtractText(resultData);
  const nLines = text ? text.split("\n").length : 0;
  if (toolName === "Read") {
    const p = String(input.file_path || "?");
    const o = input.offset, l = input.limit;
    let range = "";
    if (o != null && l != null) range = `:${o}-${Number(o) + Number(l)}`;
    else if (o != null) range = `:${o}-`;
    return `${p}${range} · ${nLines} lines`;
  }
  if (toolName === "Edit") {
    const p = String(input.file_path || "?");
    // Mirrors `_success_combined_edit`: keys absent = no text was reported, so
    // "0 lines → 0 lines" would assert a count nobody measured.
    if (!_toolHasKey(input, "old_string") && !_toolHasKey(input, "new_string")) return p;
    const ol = String(input.old_string || "").split("\n").length;
    const nl = String(input.new_string || "").split("\n").length;
    return `${p} (${ol} lines → ${nl} lines)`;
  }
  if (toolName === "Write") {
    const p = String(input.file_path || "?");
    if (!_toolHasKey(input, "content")) return p;
    const n = String(input.content || "").split("\n").length;
    return `${p} (${n} lines)`;
  }
  if (toolName === "Bash") {
    return `${_toolTruncatePreview(input.command || "", 50)} · ${nLines} lines output`;
  }
  if (toolName === "Grep") {
    const pat = _toolTruncatePreview(input.pattern || "?", 30);
    const path = String(input.path || ".");
    return `/${pat}/ in ${path} · ${nLines} matches`;
  }
  if (toolName === "Glob") {
    const pat = _toolTruncatePreview(input.pattern || "?", 30);
    const path = String(input.path || ".");
    return `${pat} in ${path} · ${nLines} files`;
  }
  // Unregistered tool — use input body + result preview
  const useBody = _toolInFlightBody(toolName, input);
  const resPreview = _toolTruncatePreview(text, 60);
  if (useBody && resPreview) return `${useBody} · ${resPreview}`;
  return useBody || resPreview;
}

// Body-only header for the failure terminal state. Mirrors
// `format_tool_chip_header` on the failure path.
function _toolFailureBody(toolName, input, resultData) {
  const useBody = _toolInFlightBody(toolName, input || {});
  const err = _toolTruncatePreview(_toolExtractText(resultData), 80);
  if (useBody && err) return `${useBody} · ${err}`;
  return useBody || err || "";
}

// Compute a minimal unified diff between two strings, mirroring
// `generate_edit_diff` (difflib.unified_diff with n=3). Used by Edit/Write
// detail rendering on the final raw_json path — the live path has it
// pre-computed by the Python backend.
function _toolUnifiedDiff(oldStr, newStr, filePath) {
  if (oldStr === newStr) return "";
  const a = oldStr.split("\n");
  const b = newStr.split("\n");
  // LCS table
  const n = a.length, m = b.length;
  const dp = [];
  for (let i = 0; i <= n; i++) dp.push(new Array(m + 1).fill(0));
  for (let i = 1; i <= n; i++) {
    for (let j = 1; j <= m; j++) {
      if (a[i - 1] === b[j - 1]) dp[i][j] = dp[i - 1][j - 1] + 1;
      else dp[i][j] = dp[i - 1][j] >= dp[i][j - 1] ? dp[i - 1][j] : dp[i][j - 1];
    }
  }
  // Walk back to ops
  const ops = [];
  let i = n, j = m;
  while (i > 0 && j > 0) {
    if (a[i - 1] === b[j - 1]) { ops.unshift({ op: "eq", line: a[i - 1] }); i--; j--; }
    else if (dp[i - 1][j] >= dp[i][j - 1]) { ops.unshift({ op: "del", line: a[i - 1] }); i--; }
    else { ops.unshift({ op: "add", line: b[j - 1] }); j--; }
  }
  while (i > 0) { ops.unshift({ op: "del", line: a[--i] }); }
  while (j > 0) { ops.unshift({ op: "add", line: b[--j] }); }
  // Emit a single hunk covering the full file (sufficient for the chip view).
  const lines = [
    `--- a/${filePath}`,
    `+++ b/${filePath}`,
    `@@ -1,${n} +1,${m} @@`,
  ];
  for (const o of ops) {
    if (o.op === "eq") lines.push(" " + o.line);
    else if (o.op === "add") lines.push("+" + o.line);
    else lines.push("-" + o.line);
  }
  return lines.join("\n");
}

// Per-tool structured detail payload. JS mirror of
// `build_tool_detail_payload`. The browser never sees the pre-Write file
// content, so Write always falls through the "new file" / full-content branch
// — the live path's `write_diff` is only reachable when the daemon precomputed
// it. That's an accepted asymmetry for the offline final view.
function _toolDetailPayload(toolName, input, resultData) {
  input = input || {};
  if (toolName === "Edit") {
    const fp = String(input.file_path || "?");
    // Keys absent = the upstream reported no text at all (codex file_change).
    // Synthesising a diff from "" would render the file as emptied, so the
    // panel says "path, no content information" instead — mirrors
    // `_build_edit_detail`.
    if (!_toolHasKey(input, "old_string") && !_toolHasKey(input, "new_string")) {
      return { kind: "file_path_only", file_path: fp, truncated: false };
    }
    const oldS = String(input.old_string || "");
    const newS = String(input.new_string || "");
    const diff = _toolUnifiedDiff(oldS, newS, fp);
    return {
      kind: "edit_diff",
      file_path: fp,
      diff: diff,
      old_start_line: 1,
      new_start_line: 1,
      truncated: false,
    };
  }
  if (toolName === "Write") {
    const fp = String(input.file_path || "?");
    if (!_toolHasKey(input, "content")) {
      return { kind: "file_path_only", file_path: fp, truncated: false };
    }
    const content = String(input.content || "");
    return {
      kind: "write_full",
      file_path: fp,
      content: content,
      start_line: 1,
      truncated: false,
    };
  }
  if (toolName === "Read") {
    const text = _toolExtractText(resultData);
    const offset = Number(input.offset || 0) || 0;
    return {
      kind: "read_text",
      file_path: String(input.file_path || "?"),
      text: text,
      start_line: offset + 1,
      truncated: false,
    };
  }
  if (toolName === "Bash") {
    return {
      kind: "bash_output",
      command: String(input.command || ""),
      stdout: _toolExtractText(resultData),
      stderr: "",
      truncated: false,
    };
  }
  if (toolName === "Grep") {
    const text = _toolExtractText(resultData);
    return {
      kind: "grep_matches",
      pattern: String(input.pattern || ""),
      path: String(input.path || "."),
      matches: text ? text.split("\n") : [],
      truncated: false,
    };
  }
  if (toolName === "Glob") {
    const text = _toolExtractText(resultData);
    return {
      kind: "glob_matches",
      pattern: String(input.pattern || ""),
      path: String(input.path || "."),
      files: text ? text.split("\n") : [],
      truncated: false,
    };
  }
  return { kind: "text", text: _toolExtractText(resultData), truncated: false };
}

// --- Chip event extraction (raw_json → ordered text/chip events) -----------
//
// Pairs `tool_use` and `tool_result` blocks by `tool_use_id` in a single pass,
// preserving stream order. Output: `[{kind:'text', text} | {kind:'chip',
// toolUseId, name, status, header, detail}]`. A `tool_result` without a
// preceding `tool_use` (legacy / mis-streamed) becomes its own chip in the
// terminal state; a `tool_use` without a `tool_result` stays in-flight.
//
// Headers and detail payloads are computed by the per-tool `_tool*Body` /
// `_toolDetailPayload` helpers above so the final-view chip matches the live
// chip byte-for-byte (the live chip is driven by `format_tool_chip_header` /
// `build_tool_detail_payload` on the daemon side; these JS helpers are the
// frontend mirror).
function extractAssistantChipEvents(rawJson) {
  if (!Array.isArray(rawJson)) return null;
  const events = [];
  const byId = new Map();
  const pushText = (text) => {
    if (!text) return;
    const last = events[events.length - 1];
    if (last && last.kind === "text") last.text += text;
    else events.push({ kind: "text", text: text });
  };
  const handleToolUse = (name, input, id) => {
    const toolName = name || "Tool";
    const header = _toolInFlightBody(toolName, input);
    const evt = {
      kind: "chip",
      toolUseId: id || null,
      name: toolName,
      status: "in-flight",
      header: header,
      detail: null,
      _input: input || {},
    };
    events.push(evt);
    if (id) byId.set(id, evt);
  };
  const handleToolResult = (id, content, isError) => {
    const existing = id ? byId.get(id) : null;
    if (existing) {
      const toolName = existing.name || "Tool";
      const input = existing._input || {};
      existing.status = isError ? "failure" : "success";
      existing.header = isError
        ? _toolFailureBody(toolName, input, content)
        : _toolSuccessBody(toolName, input, content);
      existing.detail = _toolDetailPayload(toolName, input, content);
    } else {
      // Orphan result with no preceding tool_use — we have no input data, so
      // fall back to a text-kind detail and a plain truncated header.
      const text = _toolExtractText(content);
      events.push({
        kind: "chip",
        toolUseId: id || null,
        name: "Tool",
        status: isError ? "failure" : "success",
        header: _toolTruncatePreview(text, 60),
        detail: { kind: "text", text: text, truncated: false },
      });
    }
  };
  const walkBlocks = (blocks) => {
    if (!Array.isArray(blocks)) return;
    for (const block of blocks) {
      if (block == null || typeof block !== "object") continue;
      const bt = String(block.type || "").toLowerCase();
      if (bt === "text" && typeof block.text === "string") {
        pushText(block.text);
      } else if (bt === "tool_use") {
        handleToolUse(block.name, block.input, block.id);
      } else if (bt === "tool_result") {
        handleToolResult(
          block.tool_use_id || block.toolUseId,
          block.content,
          block.is_error === true || block.isError === true,
        );
      }
    }
  };

  for (const line of rawJson) {
    if (line == null || typeof line !== "object") continue;
    const type = String(line.type || "").toLowerCase();
    if (type === "assistant" || type === "user" || type === "message" ||
        (line.message && typeof line.message === "object")) {
      const msg = (line.message && typeof line.message === "object")
        ? line.message : line;
      if (Array.isArray(msg.content)) walkBlocks(msg.content);
      else if (typeof msg.content === "string") pushText(msg.content);
      continue;
    }
    if (type === "tool_use") {
      handleToolUse(line.name, line.input, line.id);
      continue;
    }
    if (type === "tool_result") {
      handleToolResult(
        line.tool_use_id || line.toolUseId,
        line.content || line.result,
        line.is_error === true || line.isError === true,
      );
      continue;
    }
  }
  return events;
}

// Render an ordered list of chip/text events into DOM nodes. Used by both the
// final assistant bubble (events derived from raw_json) and by tests that
// drive the chip state machine directly.
function renderChipEvents(events) {
  const nodes = [];
  if (!Array.isArray(events)) return nodes;
  for (const evt of events) {
    if (!evt) continue;
    if (evt.kind === "text") {
      const text = evt.text || "";
      if (text.trim()) {
        for (const node of renderToolMarkers(text)) nodes.push(node);
      }
    } else if (evt.kind === "chip") {
      const chip = createInFlightChip(evt.name, evt.header);
      if (evt.toolUseId) chip.dataset && (chip.dataset.toolUseId = evt.toolUseId);
      if (evt.status === "success") upgradeChipToSuccess(chip, evt.header, evt.detail);
      else if (evt.status === "failure") upgradeChipToFailure(chip, evt.header, evt.detail);
      nodes.push(chip);
    }
  }
  return nodes;
}

// Render one `[Name: detail]` marker as a standalone, visually distinct block.
//
// This path is the LEGACY plain-string fallback used by `renderToolMarkers`
// when no structured chip-event list is available (e.g. older jsonl records
// with no `tool_use_id` field, or a hand-crafted assistant string). It MUST
// recognize the success / failure bracket grammar (`[Read ✓ ...]` /
// `[Read ✗ ...]`) so the chip class reflects the true status — pre-G3 this
// path defaulted to a plain in-flight chip even on terminal markers, and the
// fragile `indexOf(":")` split dropped the header when no colon was present.
function renderToolBlock(name, raw) {
  const parsed = parseToolBracket(name, raw);
  const chip = createInFlightChip(parsed.name, parsed.header);
  if (parsed.status === "success") {
    chip.classList.remove("in-flight");
    chip.classList.add("success");
    chip.__toolStatus = "success";
    setChipHeader(chip, parsed.name, parsed.header, "success");
  } else if (parsed.status === "failure") {
    chip.classList.remove("in-flight");
    chip.classList.add("failure");
    chip.__toolStatus = "failure";
    setChipHeader(chip, parsed.name, parsed.header, "failure");
  }
  return chip;
}

// Split `text` on inline tool markers: marker spans become standalone tool
// blocks, the surrounding prose still flows through the Markdown renderer.
// Returns an array of Nodes.
function renderToolMarkers(text) {
  const src = String(text == null ? "" : text);
  const nodes = [];
  let last = 0;
  let m;
  TOOL_MARKER_RE.lastIndex = 0;
  while ((m = TOOL_MARKER_RE.exec(src)) !== null) {
    if (m.index > last) {
      const chunk = src.slice(last, m.index);
      if (chunk.trim()) nodes.push(renderMarkdown(chunk));
    }
    nodes.push(renderToolBlock(m[1], m[0]));
    last = m.index + m[0].length;
  }
  if (last < src.length) {
    const chunk = src.slice(last);
    if (chunk.trim()) nodes.push(renderMarkdown(chunk));
  }
  if (!nodes.length) nodes.push(renderMarkdown(src));
  return nodes;
}

// --- per-step assistant renderers ------------------------------------------
//
// Some step types (DISCOVERY today; ANALYZE / PLAN / PLAN_TASKS in the future)
// emit assistant messages that are essentially a structured JSON document
// embedded in narrative text — the CLI sink parses them with
// `parse_json_response` and renders each field individually (e.g. the
// DISCOVERY Panel renders `content` as markdown, `refined_description` as a
// nested cyan block, and `questions` as a numbered list). Mirroring that
// behavior on the web lets a human read a discovery turn at a glance instead
// of staring at a raw ```json``` fence.
//
// The registry is a simple `{stepType: renderer}` lookup. A renderer takes
// `(content: string, norm: object)` and returns a Node, Fragment, or null.
// Returning null (or throwing) makes the caller fall back to the default
// `renderToolMarkers` + Markdown path — failure must never break the wider
// conversation view.
const STEP_ASSISTANT_RENDERERS = {};
function registerAssistantRenderer(stepType, renderer) {
  if (!stepType || typeof renderer !== "function") return;
  STEP_ASSISTANT_RENDERERS[String(stepType).toLowerCase()] = renderer;
}

// --- structured JSON extraction ----------------------------------------------
//
// The frontend has its own multi-region JSON extractor (`collectJsonRegions`
// + `extractResultJson`) that intentionally exceeds the CLI's
// `json_parser.py` in capability: it collects ALL top-level JSON regions in
// one assistant turn, lets a per-step predicate pick the real result among
// possibly several tool-call JSON segments, and excises every region from
// the narrative so intermediate tool-call JSON never leaks into the Layer-1
// view. The extractor is a JSON-string-aware brace/bracket-balanced scanner,
// NOT a fence regex — fence boundaries are literal-character-level while
// JSON string boundaries are lexical, and the two cannot be reconciled in a
// regex. See `collectJsonRegions` below for the algorithm.
//
// `tryParseJsonLenient` is the per-slice parser: strict JSON first, then a
// repair pass that strips a single trailing comma before `}` / `]` (the
// most common LLM quirk that strict `JSON.parse` rejects). If both passes
// fail the helper returns undefined; the scanner advances one character and
// continues, so a stray `{` in prose never derails extraction. The legacy
// single-shot helpers `extractFencedJson` / `extractTrailingBareJson` /
// `extractStructuredJson` remain exported as compatibility surface but are
// no longer consumed internally by `collectJsonRegions`.

// Try parsing `text` as JSON, returning the parsed value or `undefined` on
// failure. A small repair pass strips a single trailing comma before `}` /
// `]` which is the most common LLM quirk that strict `JSON.parse` rejects.
function tryParseJsonLenient(text) {
  if (typeof text !== "string") return undefined;
  const trimmed = text.trim();
  if (!trimmed) return undefined;
  try { return JSON.parse(trimmed); } catch (_) { /* fall through */ }
  try {
    const repaired = trimmed.replace(/,(\s*[}\]])/g, "$1");
    return JSON.parse(repaired);
  } catch (_) { /* fall through */ }
  return undefined;
}

// Extract the first fenced ```json … ``` (or bare ``` … ```) block whose
// body parses as JSON. Returns `{value, startIndex, endIndex}` (slice
// indices into the original text, INCLUSIVE of the fences so the caller can
// remove them when computing the narrative prefix) or null.
function extractFencedJson(text) {
  const re = /```(?:json)?\s*\n?([\s\S]*?)\n?```/gi;
  let m;
  while ((m = re.exec(text)) !== null) {
    const body = m[1];
    const value = tryParseJsonLenient(body);
    if (value !== undefined) {
      return { value: value, startIndex: m.index, endIndex: m.index + m[0].length };
    }
  }
  return null;
}

// Extract a trailing bare JSON object that runs to the end of the text.
// Walks back from the last `}` / `]` looking for a matching `{` / `[` whose
// enclosed body parses as JSON. Returns `{value, startIndex}` (slice index
// into the original text where the JSON begins; the body runs to the end)
// or null.
function extractTrailingBareJson(text) {
  if (typeof text !== "string") return null;
  const trimmedEnd = text.replace(/\s+$/, "");
  if (!trimmedEnd) return null;
  const last = trimmedEnd[trimmedEnd.length - 1];
  if (last !== "}" && last !== "]") return null;
  const open = last === "}" ? "{" : "[";
  for (let i = 0; i < trimmedEnd.length; i++) {
    if (trimmedEnd[i] !== open) continue;
    const candidate = trimmedEnd.slice(i);
    const value = tryParseJsonLenient(candidate);
    if (value !== undefined) {
      return { value: value, startIndex: i };
    }
  }
  return null;
}

// Pull the first parseable JSON value out of `text`, preferring a fenced
// block, then a trailing bare object/array. Returns `{value, narrative}`
// where `narrative` is `text` with the JSON region removed (trimmed); or
// null if no JSON was recovered.
function extractStructuredJson(text) {
  if (typeof text !== "string" || !text) return null;
  const fenced = extractFencedJson(text);
  if (fenced) {
    const narrative = (text.slice(0, fenced.startIndex) +
                       text.slice(fenced.endIndex)).trim();
    return { value: fenced.value, narrative: narrative };
  }
  const bare = extractTrailingBareJson(text);
  if (bare) {
    const narrative = text.slice(0, bare.startIndex).trim();
    return { value: bare.value, narrative: narrative };
  }
  return null;
}

// --- result-identification: in-progress vs. final assistant turn -----------
//
// An assistant turn folds its thinking process behind "展开全部" ONLY when it
// has produced a *final result* JSON for the step. The signal for "final
// result" is NOT "the body parsed as some JSON" — an intermediate tool call
// (Bash/Edit/Grep/… arguments), including a turn carrying two or more such
// tool-call JSON segments, also parses as JSON. The signal is "the parsed JSON
// carries at least one field belonging to this step's result set" — the same
// fields the step's report renderer / CLI Panel reads from `step.outputs`.
//
// `STEP_RESULT_FIELDS` enumerates those result keys per step type. A turn whose
// only JSON is a tool call has none of them, so the renderer returns null and
// the caller keeps the thinking process inline (never collapsing it into an
// empty "展开全部" toggle).
const STEP_RESULT_FIELDS = {
  analyze: ["task_type", "complexity", "scope", "reasoning",
    "relevant_specs", "selected_items"],
  plan: ["plan", "task_groups"],
  plan_tasks: ["task_groups", "plan"],
  implement: ["completion_status", "files_changed", "tests_added",
    "implemented_groups", "summary", "incomplete_tasks",
    "restricted_edits_applied", "restricted_edits_failed"],
  test: ["test_results"],
  self_check: ["issues", "actionable_count", "self_check_result"],
  verify_spec: ["verified", "summary", "issues", "recommendations",
    "verification_result", "fix_needed"],
  update_spec: ["updated_specs", "specs_updated", "new_capabilities"],
  spec_gate: ["gate_passed", "gate_route", "gate_skipped", "fix_needed",
    "test_results"],
  commit: ["committed", "commit_hash", "commit_message"],
  version_analyze: ["current_version", "suggested_version", "bump_type",
    "confidence", "reasoning"],
  summarize: ["summary"],
  investigate: ["root_cause", "evidence", "files_involved",
    "suggested_fix_direction", "confidence", "conclusive"],
  discovery: ["content", "refined_description", "questions"],
  charter_freshness: ["charter_update_needed", "charter_auto_updated",
    "charter_diff", "suggested_update", "touched_classes"],
};

// True when `value` is a dict carrying at least one of `stepType`'s result
// fields with a non-null value. Presence (not non-emptiness) is the test, so a
// genuine-but-empty result such as `{committed: false}` or `{actionable_count:
// 0}` still counts; a tool-call JSON (no result key) does not.
function isStepResultDict(stepType, value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const fields = STEP_RESULT_FIELDS[stepType];
  if (!fields) return false;
  for (const f of fields) {
    if (Object.prototype.hasOwnProperty.call(value, f) && value[f] != null) {
      return true;
    }
  }
  return false;
}

// Walk forward from a `{` or `[` at `start` looking for the matching closer
// while honoring JSON string state (so `{`, `}`, `[`, `]`, and backticks that
// happen to sit inside a JSON string field value never affect the depth count
// or terminate the region). Returns the index of the matching closer, or -1
// if the input runs out before a match is found. Standalone helper, used only
// by `collectJsonRegions`; not exported.
function findBalancedJsonEnd(text, start) {
  const open = text[start];
  const close = open === "{" ? "}" : "]";
  if (open !== "{" && open !== "[") return -1;
  let depth = 0;
  let inString = false;
  let escape = false;
  for (let i = start; i < text.length; i++) {
    const ch = text[i];
    if (inString) {
      if (escape) {
        escape = false;
      } else if (ch === "\\") {
        escape = true;
      } else if (ch === '"') {
        inString = false;
      }
    } else if (ch === '"') {
      inString = true;
    } else if (ch === "{" || ch === "[") {
      depth++;
    } else if (ch === "}" || ch === "]") {
      depth--;
      if (depth === 0) {
        return ch === close ? i : -1;
      }
    }
  }
  return -1;
}

// Collect every TOP-LEVEL parseable JSON region in `text`. Implemented as a
// JSON-string-aware brace/bracket-balanced scanner: walk character by
// character; whenever we hit an unenclosed `{` or `[`, try to find the
// matching closer (string-state aware, so JSON string contents — including
// ` ``` `, `{`, `}`, `[`, `]` — never bleed into depth counting or terminate
// the region) and run the slice through `tryParseJsonLenient`. A successful
// parse registers a region and the scanner resumes immediately past the
// closer (so nested objects/arrays are NOT registered as independent
// regions); a parse failure simply advances the cursor by one and the scan
// continues.
//
// This intentionally replaces the older fence-regex + `lastFenceEnd` guard
// approach: fence regexes cannot be made compatible with JSON string state
// (fence boundaries are literal-character-level, JSON string boundaries are
// lexical), and any fix that returns to fence regexes only moves the bug —
// embedded ` ``` ` inside a discovery `content` field, prose-only ` ``` `
// fences interleaved with bare JSON, and other structural shapes all reduce
// to the same root cause.
//
// Adjacent ```json / ``` fence markers are opportunistically absorbed into a
// region's `startIndex` / `endIndex` so the caller's narrative excision does
// not retain stray fence fragments. The absorption is tight: only an
// immediately-adjacent opening fence (allowing a small amount of whitespace
// / a single newline between fence and `{` / `[`) is pulled into
// `startIndex`, and only an immediately-adjacent closing fence into
// `endIndex`. `extractTrailingBareJson` and `extractFencedJson` are no
// longer consulted by this function; they remain exported as compatibility
// helpers.
function collectJsonRegions(text) {
  const regions = [];
  if (typeof text !== "string" || !text) return regions;
  let i = 0;
  while (i < text.length) {
    const ch = text[i];
    if (ch === "{" || ch === "[") {
      const end = findBalancedJsonEnd(text, i);
      if (end !== -1) {
        const slice = text.slice(i, end + 1);
        const value = tryParseJsonLenient(slice);
        if (value !== undefined) {
          // Detect tight delimitation: an immediately-adjacent ```json /
          // ``` fence on either side, OR the JSON sitting at the trailing
          // end of the text (only whitespace after it). A region must be
          // delimited at least one way to be registered, so stray `[0]` /
          // `{}` snippets embedded in prose are never mistaken for JSON
          // payloads. Allow horizontal whitespace + at most one newline +
          // horizontal whitespace between fence and JSON open/close, so a
          // fence from far earlier in the text never gets pulled in.
          const beforeMatch = text
            .slice(0, i)
            .match(/```(?:json)?[ \t]*\n?[ \t]*$/i);
          const afterMatch = text
            .slice(end + 1)
            .match(/^[ \t]*\n?[ \t]*```/);
          const isTrailing = text.slice(end + 1).trim() === "";
          // Historical blind spot: a BARE JSON object (no fence) followed by
          // further non-whitespace text — a trailing prose tail, a second
          // narrative paragraph, or another trailing payload block — matches
          // none of beforeMatch / afterMatch / isTrailing, so the region was
          // dropped, extractResultJson returned null, and the discovery (and
          // every other STEP_RESULT_FIELDS) renderer lost content /
          // refined_description / questions, falling back to thinking-only.
          // Register such a region when the parsed value is a *substantive
          // object* — a non-array object carrying at least one key — that
          // also stands as its own BLOCK (the `{` is preceded only by
          // horizontal whitespace back to a line break or the start of the
          // text). A real step result is always a keyed object
          // ({content, ...}) emitted on its own block, so this captures the
          // new bare shapes while leaving unregistered: stray prose fragments
          // (`[0]`, a bare array, or an empty `{}`), and — crucially — JSON
          // embedded INLINE inside a tool marker such as
          // `[Read: {"file_path": "…"}]`, whose `{` is preceded by `[Read: `
          // mid-line. Registering an inline tool-marker object would excise
          // it from the narrative and gut the marker's detail. The block-start
          // guard is what keeps tool markers intact. The result predicate
          // downstream still decides which registered region is the result.
          const beforeBare = text.slice(0, i).replace(/[ \t]*$/, "");
          const atBlockStart = beforeBare === "" || beforeBare.endsWith("\n");
          const isSubstantiveObject =
            value && typeof value === "object" && !Array.isArray(value) &&
            Object.keys(value).length > 0;
          if (beforeMatch || afterMatch || isTrailing ||
              (isSubstantiveObject && atBlockStart)) {
            let startIndex = i;
            let endIndex = end + 1;
            if (beforeMatch) startIndex -= beforeMatch[0].length;
            if (afterMatch) endIndex += afterMatch[0].length;
            regions.push({
              value: value,
              startIndex: startIndex,
              endIndex: endIndex,
            });
            // Resume past the JSON closer (NOT past the absorbed trailing
            // fence) so a stray subsequent ``` cannot be misread as
            // opening a new region. The regions list stays sorted by
            // startIndex by construction because i increases monotonically.
            i = end + 1;
            continue;
          }
        }
      }
    }
    i++;
  }
  return regions;
}

// Pick the JSON region from `text` whose value satisfies `predicate` — i.e. the
// step's real result among possibly several tool-call JSON segments. The LAST
// matching region wins, because a result conventionally follows the tool calls
// that produced it. Returns `{value, narrative}` with the chosen region removed
// from the narrative (trimmed), or null when no region matches (→ the caller
// keeps the thinking process inline). A turn with 2+ JSON segments none of
// which is a result therefore yields null, exactly as required.
function extractResultJson(text, predicate) {
  if (typeof text !== "string" || !text) return null;
  const regions = collectJsonRegions(text);
  let chosen = null;
  for (const r of regions) {
    if (predicate(r.value)) chosen = r;
  }
  if (!chosen) return null;
  // Narrative = the prose with EVERY JSON region removed (not just the chosen
  // one), so intermediate tool-call JSON segments do not leak into the clean
  // Layer-1 view; the full original content (incl. all JSON) remains available
  // in the Layer-2 "展开全部" process area, which re-renders `content` verbatim.
  // Splice from the tail so earlier indices stay valid.
  let narrative = text;
  const sorted = regions.slice().sort((a, b) => b.startIndex - a.startIndex);
  for (const r of sorted) {
    narrative = narrative.slice(0, r.startIndex) + narrative.slice(r.endIndex);
  }
  return { value: chosen.value, narrative: narrative.trim() };
}

// --- discovery assistant renderer ------------------------------------------
//
// Mirror of the CLI `_display_discovery_message` /
// `_extract_narrative_from_raw` pipeline:
//   1. Pull the structured JSON out of the raw text. On failure return null
//      and let the caller fall back to the generic renderer.
//   2. Render any narrative (text outside the JSON region) at the top via
//      `renderToolMarkers`, so inline `[Read: …]` and friends still surface
//      as standalone blocks.
//   3. Render the JSON's `content` field as markdown.
//   4. Render `refined_description` (when present) inside a dedicated
//      "Proposed Task Description" card so the human can see the proposed
//      task description at a glance — the visual counterpart of the CLI
//      cyan reverse-color block.
//   5. Render `questions` as an ordered list.
// Returns a DocumentFragment ready to append into the assistant bubble, or
// null when no JSON was found (caller falls back to the default path).
// A discovery RESULT carries a non-empty `content`, `refined_description`, or
// `questions` field. A turn whose JSON is just a tool call (or whose result
// fields are all empty) is not a final result — it is thinking process.
function isDiscoveryResultDict(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  if (typeof value.content === "string" && value.content.trim()) return true;
  if (typeof value.refined_description === "string" &&
      value.refined_description.trim()) return true;
  if (Array.isArray(value.questions) && value.questions.length) return true;
  return false;
}

function renderDiscoveryAssistant(content, norm) {
  // Result identification: only surface a structured result (Layer 1) when this
  // turn parsed a real discovery result (content / refined_description /
  // questions). A turn whose only JSON is a tool call — including 2+ such
  // segments — matches no result region, so we return null and the caller keeps
  // the thinking process shown inline in full (never folded). When a result IS
  // present, the narrative is tiled above it as the visible first layer, and the
  // caller adds the single "查看原始" entry for the original record.
  const extracted = extractResultJson(content, isDiscoveryResultDict);
  if (!extracted) return null;
  const value = extracted.value;

  // Render the result fields first; the narrative is surfaced only alongside a
  // real result so a narrative-only (no-result) turn never masquerades as a
  // final result.
  const resultFrag = document.createDocumentFragment();

  // content as markdown
  const jsonContent = value.content;
  if (typeof jsonContent === "string" && jsonContent.trim()) {
    const contentWrap = el("div", "discovery-content");
    contentWrap.appendChild(renderMarkdown(jsonContent));
    resultFrag.appendChild(contentWrap);
  }

  // refined_description as a Proposed Task Description card
  const refined = value.refined_description;
  if (typeof refined === "string" && refined.trim()) {
    const card = el(
      "div",
      "step-report step-report--proposed-task kind-discovery-refined",
    );
    const head = el("div", "step-report__head");
    head.appendChild(
      el("span", "step-report__title",
        tf("discovery.proposedTaskDescription", "Proposed Task Description")),
    );
    card.appendChild(head);
    const body = el("div", "step-report__body");
    const md = el("div", "step-report__markdown");
    md.appendChild(renderMarkdown(refined));
    body.appendChild(md);
    card.appendChild(body);
    resultFrag.appendChild(card);
  }

  // questions as a numbered list
  const questions = value.questions;
  if (Array.isArray(questions) && questions.length) {
    const qWrap = el("div", "discovery-questions");
    qWrap.appendChild(el("h6", "discovery-questions__title",
      tf("discovery.questions", "Questions")));
    const ol = el("ol", "discovery-questions__list");
    for (const q of questions) {
      const li = el("li");
      if (q && typeof q === "object") {
        // CLI sometimes nests `{question: "...", options: [...]}` etc.;
        // fall back to JSON.stringify so nothing is silently lost.
        const qText = typeof q.question === "string" ? q.question : safeStringify(q);
        li.textContent = qText;
      } else {
        li.textContent = String(q);
      }
      ol.appendChild(li);
    }
    qWrap.appendChild(ol);
    resultFrag.appendChild(qWrap);
  }

  // Predicate guaranteed at least one of the above rendered; if somehow none
  // did, fall back to the inline thinking path rather than fold an empty card.
  if (!resultFrag.childNodes.length) return null;

  const frag = document.createDocumentFragment();
  // narrative prefix (only with a real result present) — routed through the
  // shared `renderNarrativeNodes` helper so when raw_json is available the
  // narrative's tool calls render as the same rich chips (✓/✗ + details) the
  // thinking-only assistant path produces, instead of bare bracket chips.
  if (extracted.narrative) {
    const narWrap = el("div", "assistant-narrative");
    for (const node of renderNarrativeNodes(extracted.narrative, norm)) {
      narWrap.appendChild(node);
    }
    if (narWrap.childNodes.length) frag.appendChild(narWrap);
  }
  frag.appendChild(resultFrag);
  // Per-round usage footnote at the bubble tail (G5) — only when this round
  // actually called the LLM (non-empty round usage).
  appendRoundUsageFootnote(frag, norm);
  return frag;
}
registerAssistantRenderer("discovery", renderDiscoveryAssistant);

// --- generic structured assistant renderer (all non-discovery steps) -------
//
// Beyond discovery, most step types emit an assistant message that is a JSON
// object — optionally wrapped in a ```json``` fence and/or preceded by
// narrative — carrying the same structured fields the CLI end-of-step Panel
// reads from `step.outputs`. Rather than re-implement a bespoke field layout
// per step, we reuse the existing STEP_REPORT_RENDERERS (the web counterparts
// of `step_renderers.py`) so an assistant turn's default view shows exactly the
// same structured fields a reader sees on that step's report card, and web/CLI
// stay in field parity.
//
// `reportRendererFor` resolves the report renderer at call time (not capture
// time) so registration order does not matter, and lets `plan_tasks` borrow the
// `plan` renderer (both consume `task_groups`).
function reportRendererFor(stepType) {
  if (stepType === "plan_tasks") return STEP_REPORT_RENDERERS.plan;
  return STEP_REPORT_RENDERERS[stepType] || null;
}

// Build an assistant renderer for `stepType` that parses the JSON body and
// renders its fields via the shared report renderer. Returns a renderer
// `(content, norm) -> Node | null`:
//   1. Extract structured JSON + narrative (extractStructuredJson). When no
//      JSON is recoverable — or the top level is not a dict — return null so
//      renderConversationRecord falls back to the renderToolMarkers + markdown
//      path and nothing is lost.
//   2. Render any narrative (text outside the JSON) at the top via
//      renderToolMarkers, preserving inline [Tool: …] markers.
//   3. Delegate field rendering to the step's report renderer, passing the
//      parsed JSON as the synthetic `step.outputs`. Any throw inside the report
//      renderer → return null (full fallback) so a partial structured render
//      never strands the assistant text.
function makeStructuredAssistantRenderer(stepType) {
  return function (content, norm) {
    const reportRenderer = reportRendererFor(stepType);
    if (!reportRenderer) return null;
    // Result identification: pick the JSON region carrying a real result field
    // for this step. A turn whose only JSON is a tool call (or 2+ tool calls)
    // matches no result region → null → the caller keeps the thinking process
    // inline (never folded into an empty "展开全部"). The CLI parser is dict-only,
    // and isStepResultDict already rejects arrays / scalars.
    const extracted = extractResultJson(
      content, (v) => isStepResultDict(stepType, v));
    if (!extracted) return null;
    const value = extracted.value;

    // structured fields via the shared report renderer
    let body;
    try {
      const synthStep = {
        step_type: stepType,
        status: typeof value.status === "string" ? value.status : "completed",
        outputs: value,
        error_message: "",
      };
      body = reportRenderer(synthStep, value);
    } catch (err) {
      // A report renderer fault must never strand the assistant text — bail to
      // the generic fallback, which re-renders the whole body verbatim.
      try { console.warn("assistant report renderer failed", stepType, err); }
      catch (_) { /* console may be absent */ }
      return null;
    }
    const bodyHasContent = !!(body && body.childNodes && body.childNodes.length);
    // Both conditions must hold to fold: a real result field is present (by
    // construction of extractResultJson) AND the report renderer produced
    // content. If the body is empty, keep the thinking inline so nothing is
    // lost rather than fold behind an empty toggle.
    if (!bodyHasContent) return null;

    const frag = document.createDocumentFragment();
    // narrative prefix (tool markers preserved) — surfaced only alongside the
    // real result, so a narrative-only turn never folds. Routed through the
    // shared `renderNarrativeNodes` helper so raw_json-backed turns get rich
    // chips (✓/✗ + details) instead of bare bracket chips.
    if (extracted.narrative) {
      const narWrap = el("div", "assistant-narrative");
      for (const node of renderNarrativeNodes(extracted.narrative, norm)) {
        narWrap.appendChild(node);
      }
      if (narWrap.childNodes.length) frag.appendChild(narWrap);
    }

    const wrap = el("div", "assistant-structured kind-" + stepType);
    const bodyWrap = el("div", "step-report__body");
    bodyWrap.appendChild(body);
    wrap.appendChild(bodyWrap);
    frag.appendChild(wrap);
    // Per-round usage footnote at the bubble tail (G5) — covers confirm and
    // every other structured interactive step that actually called the LLM.
    appendRoundUsageFootnote(frag, norm);
    return frag;
  };
}

// Register the generic structured renderer for every step type that has a
// report renderer (discovery keeps its dedicated card-style renderer above).
// The registry stays open for new step types — adding one is a one-line
// registration here plus its report renderer.
for (const stepType of [
  "analyze", "plan", "plan_tasks", "implement", "test", "self_check",
  "verify_spec", "update_spec", "commit", "version_analyze", "summarize",
]) {
  registerAssistantRenderer(stepType, makeStructuredAssistantRenderer(stepType));
}

// --- long-content folding --------------------------------------------------

// Records longer than this (characters) are folded by default — a `user` step
// prompt can run to 130KB+, which would both bury the conversation structure
// and bloat the DOM if rendered eagerly.
const FOLD_THRESHOLD = 1600;
// How much of the head is shown as the collapsed-state summary.
const FOLD_SUMMARY_CHARS = 700;

// Human-readable size for a character count.
function formatSize(n) {
  if (n < 1024) return tf("common.size.chars", `${n} chars`, { n });
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / (1024 * 1024)).toFixed(1) + " MB";
}

// Wrap rendered content so long records fold by default. `renderFull` is a
// zero-arg factory returning a Node/Fragment with the complete content; it is
// invoked lazily — only on first expand — so a 130KB record never builds its
// full DOM until the reader asks for it. Short records (≤ FOLD_THRESHOLD) are
// returned rendered eagerly with no fold controls.
function makeFoldable(renderFull, fullText) {
  const text = String(fullText == null ? "" : fullText);
  if (text.length <= FOLD_THRESHOLD) return renderFull();

  const wrap = el("div", "foldable folded");
  const summary = el("pre", "fold-summary");
  summary.textContent = text.slice(0, FOLD_SUMMARY_CHARS).replace(/\s+$/, "") + " …";
  const full = el("div", "fold-full");

  const collapsedLabel = tf("fold.expandAll", `▸ Expand all (${formatSize(text.length)})`, { size: formatSize(text.length) });
  const btn = el("button", "fold-toggle", collapsedLabel);
  let expanded = false;
  let built = false;
  btn.addEventListener("click", () => {
    expanded = !expanded;
    if (expanded && !built) {
      full.appendChild(renderFull());
      built = true;
    }
    wrap.classList.toggle("folded", !expanded);
    wrap.classList.toggle("expanded", expanded);
    btn.textContent = expanded ? tf("fold.collapse", "▾ Collapse") : collapsedLabel;
    if (expanded) {
      requestAnimationFrame(() => full.scrollIntoView({ block: "nearest" }));
    }
  });

  wrap.append(summary, full, btn);
  return wrap;
}

// --- raw-data toggle -------------------------------------------------------

// Pretty-print a raw payload: `raw_json` is `list[dict]`, `raw_ndjson` is a
// string of NDJSON lines — format each line on its own when possible.
function formatRaw(payload) {
  if (typeof payload === "string") {
    const lines = payload.split("\n").filter((l) => l.trim());
    return lines
      .map((l) => {
        try { return JSON.stringify(JSON.parse(l), null, 2); }
        catch (_) { return l; }
      })
      .join("\n");
  }
  try { return JSON.stringify(payload, null, 2); }
  catch (_) { return String(payload); }
}

// Resolve the raw payload + kind for a record, or `{payload:null}` when the
// record carries nothing inspectable. Shared by `hasRawPayload` (a cheap
// predicate the user-marker path uses to decide whether a "展开全部" toggle is
// worth offering for raw access alone) and `makeRawToggle` (which builds the
// control).
function resolveRawPayload(norm) {
  const raw = (norm && norm.raw) || {};
  if (raw.raw_json != null &&
      !(Array.isArray(raw.raw_json) && raw.raw_json.length === 0)) {
    return { payload: raw.raw_json, kind: "raw_json" };
  }
  if (raw.raw_ndjson != null && raw.raw_ndjson !== "") {
    return { payload: raw.raw_ndjson, kind: "raw_ndjson" };
  }
  return { payload: null, kind: "" };
}

// True when a record has a raw_json / raw_ndjson payload reachable through a
// "查看原始" toggle.
function hasRawPayload(norm) {
  return resolveRawPayload(norm).payload != null;
}

// Build a "view raw" control for one record: a small button plus a hidden
// formatted-JSON block showing the pre-normalization raw_json / raw_ndjson.
// Hidden by default — the default view stays human-readable. Returns null
// when the record carries no raw payload. This shared helper backs the USER
// side: per the message paradigm it is only ever appended *inside* a "展开全部"
// expand area (user `makeUserPromptToggle`) or a collapsed chip's detail —
// never at the always-visible row level. Its "no raw → null" contract is relied
// on by `makeUserPromptToggle` and the collapsed-chip path, so it MUST stay
// intact. (The assistant side uses its own `makeAssistantRawToggle`, which
// instead falls back to the unrendered content literal when no raw exists.)
function makeRawToggle(norm) {
  const { payload, kind } = resolveRawPayload(norm);
  if (payload == null) return null;

  const wrap = el("div", "raw-toggle-wrap");
  const btn = el("button", "raw-toggle", tf("raw.view", "View raw"));
  const pre = el("pre", "raw-json hidden");
  let rendered = false;
  let shown = false;
  btn.addEventListener("click", () => {
    shown = !shown;
    if (shown && !rendered) {
      pre.textContent = formatRaw(payload);
      rendered = true;
    }
    pre.classList.toggle("hidden", !shown);
    btn.classList.toggle("active", shown);
    btn.textContent = shown ? tf("raw.hide", `Hide raw (${kind})`, { kind }) : tf("raw.view", "View raw");
    if (shown) {
      requestAnimationFrame(() => pre.scrollIntoView({ block: "nearest" }));
    }
  });
  wrap.append(btn, pre);
  return wrap;
}

// --- assistant single-layer raw disclosure --------------------------------

// "查看原始" — the assistant turn's single fold layer (Layer 2). Unlike the user
// side's three layers, an assistant turn whose result JSON is rendered keeps
// just two: Layer 1 is the visible narrative + structured result, and this
// single "查看原始" entry is the only fold — there is no "展开全部" wrapper. The
// button is always visible; the body (this turn's original record — raw NDJSON /
// tool-call JSON / unrendered result-JSON literal) is folded by default.
//
// This is a dedicated assistant entry, NOT the shared `makeRawToggle`: that
// helper returns null when no raw payload exists (a contract the user
// `makeUserPromptToggle` and the collapsed chip depend on). Here we instead
// fall back to the unrendered `content` text literal when raw_json / raw_ndjson
// is absent, so the original turn record is always reachable. Expanding scrolls
// the freshly shown block into view; collapsing leaves the reader's position
// untouched.
function makeAssistantRawToggle(content, norm) {
  const { payload, kind } = resolveRawPayload(norm);
  const hasRaw = payload != null;
  const wrap = el("div", "raw-toggle-wrap assistant-raw-toggle-wrap");
  const btn = el("button", "raw-toggle", tf("raw.view", "View raw"));
  btn.type = "button";
  const pre = el("pre", "raw-json hidden");
  let rendered = false;
  let shown = false;
  btn.addEventListener("click", () => {
    shown = !shown;
    if (shown && !rendered) {
      // Prefer the structured raw payload; fall back to the unrendered content
      // literal so the original record is never unreachable.
      pre.textContent = hasRaw
        ? formatRaw(payload)
        : String(content == null ? "" : content);
      rendered = true;
    }
    pre.classList.toggle("hidden", !shown);
    btn.classList.toggle("active", shown);
    btn.textContent = shown
      ? tf("raw.hide", `Hide raw (${hasRaw ? kind : "content"})`, { kind: hasRaw ? kind : "content" })
      : tf("raw.view", "View raw");
    if (shown) {
      requestAnimationFrame(() => pre.scrollIntoView({ block: "nearest" }));
    }
  });
  wrap.append(btn, pre);
  return wrap;
}

// --- user single-layer raw disclosure (Layer 3) ---------------------------

// "查看原始" — the user turn's Layer 3 raw disclosure. The running-flow-console
// spec requires a user turn's Layer 3 to STABLY present the message's original
// NDJSON record (the .jsonl envelope), regardless of whether a second-layer raw
// payload exists. The shared `makeRawToggle` returns null when no raw_json /
// raw_ndjson payload is present — a contract relied on elsewhere and which we
// must NOT weaken — so the user side gets this dedicated path instead: it
// prefers the raw payload (resolveRawPayload) when present, and otherwise falls
// back to the record's original .jsonl envelope (`norm.raw.envelope`, the
// {step_id, step_type, message} JSON envelope). It ALWAYS returns a toggle,
// never null, so Layer 3 is always reachable. The interaction / naming (查看原始 /
// 隐藏原始, scroll-into-view only on expand) matches the other raw toggles.
function makeUserRawToggle(norm) {
  const { payload, kind } = resolveRawPayload(norm);
  const hasRaw = payload != null;
  const envelope = (norm && norm.raw && norm.raw.envelope != null)
    ? norm.raw.envelope
    : null;
  const wrap = el("div", "raw-toggle-wrap user-raw-toggle-wrap");
  const btn = el("button", "raw-toggle", tf("raw.view", "View raw"));
  btn.type = "button";
  const pre = el("pre", "raw-json hidden");
  let rendered = false;
  let shown = false;
  btn.addEventListener("click", () => {
    shown = !shown;
    if (shown && !rendered) {
      // Prefer the second-layer raw payload; fall back to the original .jsonl
      // envelope record so Layer 3 is never empty for a user turn.
      pre.textContent = hasRaw ? formatRaw(payload) : formatRaw(envelope);
      rendered = true;
    }
    pre.classList.toggle("hidden", !shown);
    btn.classList.toggle("active", shown);
    btn.textContent = shown
      ? tf("raw.hide", `Hide raw (${hasRaw ? kind : "envelope"})`, { kind: hasRaw ? kind : "envelope" })
      : tf("raw.view", "View raw");
    if (shown) {
      requestAnimationFrame(() => pre.scrollIntoView({ block: "nearest" }));
    }
  });
  wrap.append(btn, pre);
  return wrap;
}

// --- user-prompt three-layer progressive disclosure -----------------------

// Append the "模板前缀" (template prefix) and "框架后缀" (framework suffix)
// subsections of a split user prompt into `target`. Each subsection carries a
// labeled heading so a developer can tell what the engine injected before vs.
// after the user's literal input. Empty segments are skipped — legacy
// two-segment records carry no suffix, and a prefix that starts at index 0 is
// empty — so a subsection only appears when it has content.
function appendPromptSubsections(target, split) {
  const hasPrefix = typeof split.prefix === "string" && split.prefix.length > 0;
  const hasSuffix = typeof split.suffix === "string" && split.suffix.length > 0;
  if (hasPrefix) {
    const sec = el("div", "user-prompt-chip__section");
    sec.appendChild(el("h6", "user-prompt-chip__section-title", tf("prompt.templatePrefix", "Template prefix")));
    sec.appendChild(el("pre", "conv-plain", split.prefix));
    target.appendChild(sec);
  }
  if (hasSuffix) {
    const sec = el("div", "user-prompt-chip__section");
    sec.appendChild(el("h6", "user-prompt-chip__section-title", tf("prompt.frameworkSuffix", "Framework suffix")));
    sec.appendChild(el("pre", "conv-plain", split.suffix));
    target.appendChild(sec);
  }
}

// "展开全部" — Layer 2 of the user turn's three-layer disclosure. (The user side
// keeps all three layers; the assistant side was simplified to two — narrative +
// result, then a single "查看原始" — so there is no longer an assistant-side
// process toggle to mirror.) Collapsed by default; on first expand it lazily
// renders the full prompt the LLM actually saw as the two labeled subsections
// (模板前缀 / 框架后缀), then nests Layer 3 ("查看原始", the raw NDJSON) at the end of
// the expanded area, so the default view (the user-content bubble above it)
// stays limited to the user's literal input and the raw toggle never shows until
// "展开全部" is opened. Control naming ("▸ 展开全部" / "▾ 收起全部") and the expand-only
// scroll-into-view behavior follow the same conventions used elsewhere in the
// view.
function makeUserPromptToggle(split, norm) {
  const wrap = el("div", "process-toggle-wrap user-prompt-toggle-wrap folded");
  const btn = el("button", "process-toggle", tf("fold.expandAllSimple", "▸ Expand all"));
  btn.type = "button";
  const full = el("div", "process-full hidden");
  let built = false;
  let expanded = false;
  btn.addEventListener("click", () => {
    expanded = !expanded;
    if (expanded && !built) {
      appendPromptSubsections(full, split);
      // Layer 3 nests inside the expanded Layer-2 area, after the prefix /
      // suffix subsections — never visible in the default Layer-1 bubble. Uses
      // the user-side toggle, which always presents the original record (the
      // .jsonl envelope when no second-layer raw payload exists), so Layer 3 is
      // stably reachable per the running-flow-console spec.
      full.appendChild(makeUserRawToggle(norm));
      built = true;
    }
    full.classList.toggle("hidden", !expanded);
    wrap.classList.toggle("folded", !expanded);
    wrap.classList.toggle("expanded", expanded);
    btn.textContent = expanded ? tf("fold.collapseAll", "▾ Collapse all") : tf("fold.expandAllSimple", "▸ Expand all");
    if (expanded) {
      requestAnimationFrame(() => full.scrollIntoView({ block: "nearest" }));
    }
  });
  wrap.append(btn, full);
  return wrap;
}

// Build the inner content of an assistant bubble using the assistant's
// two-layer progressive disclosure model (the user side keeps three layers;
// the assistant side is deliberately simpler):
//   Layer 1 (default, visible): the narrative + the clean structured result,
//                       via STEP_ASSISTANT_RENDERERS — the narrative (already
//                       stripped of every JSON region) tiled above the rendered
//                       fields, no raw JSON blob.
//   Layer 2 (single fold, "查看原始"): this turn's original record — raw NDJSON /
//                       tool-call JSON / unrendered result-JSON literal — folded
//                       by default via `makeAssistantRawToggle`. There is no
//                       "展开全部" wrapper: the single "查看原始" entry is the only
//                       fold for the assistant side.
// This mirrors the message paradigm's two assistant cases:
//   * a turn that produced a result JSON shows the narrative + rendered result
//     by default, with the original record reachable behind a single "查看原始";
//   * a turn with NO result JSON keeps its thinking process shown in full,
//     never collapsed/contracted — so the process is rendered inline via
//     `renderToolMarkers` (not folded), and nothing is ever hidden.
// Build the inline thinking-process view of an assistant turn: the narrative +
// tool markers rendered via `renderToolMarkers`, wrapped in the
// `assistant-process-inline` container. This is the exact structure the
// no-result assistant branch of `renderAssistantBubble` produces; it is shared
// with `buildPartialBubble` so the live accumulating (partial) bubble lays its
// content out identically to the final no-result assistant turn it collapses
// into — partials never carry result JSON, so the final form is always this
// inline-process shape.
// Render a narrative text region's tool-call markers using the richest path
// available. When `norm.raw.raw_json` carries tool_use / tool_result blocks for
// the turn, pull the rich chip events (paired by tool_use_id, with ✓/✗ glyph
// and collapsible detail panel) out of raw_json and interleave them in order
// with the bracket markers found in `text`: each bracket position is replaced
// by the next rich chip, prose between markers renders as markdown. Drops the
// text events `extractAssistantChipEvents` emits — those carry the raw text
// content blocks from raw_json, which in production hold the FULL assistant
// body (narrative + the trailing ```json fenced result literal). Rendering them
// through `renderToolMarkers` would re-emit the JSON code block as markdown
// even though the caller already stripped it from `text` and is rendering the
// parsed result as structured fields below — the user would see the JSON
// twice. Using `text` as the prose source (which the structured renderers pass
// pre-stripped) keeps the narrative clean.
//
// When raw_json is unavailable (legacy records, no `raw` field, non-array) OR
// when raw_json carries no chip events at all (e.g. a single text block, the
// dominant production shape for a no-tool turn), fall back to
// `renderToolMarkers(text)`: that path parses the bracketed `[Name: …]`
// markers in `text` and produces a bare in-flight chip per marker. Returns
// Node[], matching `renderToolMarkers`'s shape so callers can
// `for (const node of …) wrap.appendChild(node)` without branching.
//
// Excess chips (more chips in raw_json than bracket markers in `text`) are
// appended after the prose so an assistant turn whose displayed content is
// empty — the final-raw_json test shape, `content: ""` plus tool blocks in
// raw_json — still renders one chip per tool call. The duplication this whole
// rewrite prevents is the *text* events from raw_json being re-rendered as
// markdown; chip events themselves carry only header + detail payload, never
// the JSON literal, so tail-appending excess chips is safe.
function renderNarrativeNodes(text, norm) {
  const rawJson = norm && norm.raw && norm.raw.raw_json;
  const chipEvents = Array.isArray(rawJson)
    ? (extractAssistantChipEvents(rawJson) || []).filter(
        (e) => e && e.kind === "chip")
    : [];
  if (!chipEvents.length) return renderToolMarkers(text);

  const buildChip = (evt) => {
    const chip = createInFlightChip(evt.name, evt.header);
    if (evt.toolUseId) chip.dataset && (chip.dataset.toolUseId = evt.toolUseId);
    if (evt.status === "success") upgradeChipToSuccess(chip, evt.header, evt.detail);
    else if (evt.status === "failure") upgradeChipToFailure(chip, evt.header, evt.detail);
    return chip;
  };

  const src = String(text == null ? "" : text);
  const nodes = [];
  let last = 0;
  let chipIdx = 0;
  let m;
  TOOL_MARKER_RE.lastIndex = 0;
  while ((m = TOOL_MARKER_RE.exec(src)) !== null) {
    if (m.index > last) {
      const chunk = src.slice(last, m.index);
      if (chunk.trim()) nodes.push(renderMarkdown(chunk));
    }
    if (chipIdx < chipEvents.length) {
      nodes.push(buildChip(chipEvents[chipIdx++]));
    } else {
      nodes.push(renderToolBlock(m[1], m[0]));
    }
    last = m.index + m[0].length;
  }
  if (last < src.length) {
    const chunk = src.slice(last);
    if (chunk.trim()) nodes.push(renderMarkdown(chunk));
  }
  while (chipIdx < chipEvents.length) {
    nodes.push(buildChip(chipEvents[chipIdx++]));
  }
  if (!nodes.length) nodes.push(renderMarkdown(src));
  return nodes;
}

function renderAssistantProcessInline(content, norm) {
  const inline = el("div", "assistant-process-inline");
  for (const node of renderNarrativeNodes(content, norm)) inline.appendChild(node);
  return inline;
}

// True for any non-empty plain object (dict). Used by the unregistered-step
// fallback below to detect a `step.outputs` payload the LLM pasted as a JSON
// region in the assistant body — for confirm / project_summary and any other
// step type not registered in `STEP_ASSISTANT_RENDERERS`, the body has no
// per-step field schema we could match, so the only signal is "looks like an
// outputs dict". Arrays and scalars are rejected so a top-level tool-call
// arg array does not get folded into a kv card.
function isPlainOutputsDict(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  for (const _ in value) return true;
  return false;
}

// --- Agent/model badge --------------------------------------------------------
//
// Renders a small badge at the top of an assistant bubble (or step report card)
// showing the agent name and, when available, the model name. The badge is
// shown ONLY when `norm.agentName` is a non-empty string — old records or
// records without the field render nothing, no placeholder. When both
// `agentName` and `modelName` are present, the badge shows
// "agentName · modelName"; otherwise just "agentName".
function renderAgentBadge(norm) {
  if (!norm || !norm.agentName) return null;
  const badge = el("span", "agent-badge");
  badge.textContent = norm.modelName
    ? `${norm.agentName} · ${norm.modelName}`
    : norm.agentName;
  return badge;
}

// Pure helper to compute the badge text — exported for testing.
function formatAgentBadgeText(agentName, modelName) {
  if (!agentName) return null;
  return modelName ? `${agentName} · ${modelName}` : agentName;
}

function renderAssistantBubble(content, norm) {
  const frag = document.createDocumentFragment();
  const stepType = String(norm.stepType || "").toLowerCase();
  const renderer = stepType && STEP_ASSISTANT_RENDERERS[stepType];
  let structured = null;
  if (renderer) {
    try {
      structured = renderer(content, norm);
    } catch (err) {
      // A registry renderer must never break the wider conversation — log once
      // and fall back to the inline process view.
      try { console.warn("assistant renderer failed", stepType, err); }
      catch (_) { /* console may be absent */ }
      structured = null;
    }
  }

  if (structured) {
    // Result-JSON turn: Layer 1 is the narrative + clean rendered result (the
    // renderer already prepends the narrative, which has every JSON region
    // stripped — so the narrative never duplicates the structured fields).
    // The single "查看原始" entry (Layer 2) holds the original record.
    const resultWrap = el("div", "assistant-result");
    resultWrap.appendChild(structured);
    frag.appendChild(resultWrap);
    frag.appendChild(makeAssistantRawToggle(content, norm));
    return frag;
  }

  // Generic fallback for step types NOT registered in STEP_ASSISTANT_RENDERERS
  // (confirm / project_summary / etc.). Without this branch the body — which
  // for these steps is a structured `step.outputs` dict the LLM emitted as a
  // ```json``` fence — falls through to renderAssistantProcessInline →
  // renderMarkdown and surfaces as a raw JSON code block, burying field names
  // inside JSON syntax. By scoping the fallback to !renderer we preserve the
  // existing "registered step + structured null = inline thinking" behavior
  // (a turn whose only JSON is tool calls must not be folded into an empty
  // toggle), so the fix does not overlap with structured rendering.
  if (!renderer) {
    const extracted = extractResultJson(content, isPlainOutputsDict);
    if (extracted) {
      const body = renderGenericOutputs(extracted.value);
      if (body && body.childNodes && body.childNodes.length) {
        const resultWrap = el("div", "assistant-result assistant-result--generic");
        // Narrative prose (every JSON region already excised) goes above the
        // rendered fields; tool-marker chips are preserved via the shared
        // narrative helper.
        if (extracted.narrative) {
          const narWrap = el("div", "assistant-narrative");
          for (const node of renderNarrativeNodes(extracted.narrative, norm)) {
            narWrap.appendChild(node);
          }
          if (narWrap.childNodes.length) resultWrap.appendChild(narWrap);
        }
        resultWrap.appendChild(body);
        // Per-round usage footnote (G5) for unregistered structured steps
        // (confirm / project_summary / …) that called the LLM.
        appendRoundUsageFootnote(resultWrap, norm);
        frag.appendChild(resultWrap);
        // Single "查看原始" entry holds this turn's original record.
        frag.appendChild(makeAssistantRawToggle(content, norm));
        return frag;
      }
    }
  }

  // No result JSON this turn (or the body could not be structured): keep the
  // full thinking process shown inline, never folded or contracted to empty.
  // Per the unified "every conversation message can view raw" principle, also
  // append the always-present "查看原始" entry below the inline process — folded
  // by default, button always visible. The inline thinking stays fully shown;
  // makeAssistantRawToggle shows the raw_json/raw_ndjson payload when present
  // and otherwise falls back to the unrendered content literal, so the original
  // record is always reachable without contracting the inline process to empty.
  frag.appendChild(renderAssistantProcessInline(content, norm));
  // Per-round usage footnote (G5): a no-result turn that nevertheless called the
  // LLM (e.g. an assistant turn carrying token_usage but no structured result)
  // still reports its round / cumulative usage at the tail.
  appendRoundUsageFootnote(frag, norm);
  frag.appendChild(makeAssistantRawToggle(content, norm));
  return frag;
}

// --- record bubble ---------------------------------------------------------

// Render a single normalized record as a role-tagged conversation bubble.
//
// Classification is strictly role-based (see `isCollapsibleRole`): `user` /
// `system` template-style prompts default to a collapsed one-line chip — their
// content is not deleted, just not shown until clicked — while `assistant` (and
// anything else) renders as an expanded bubble. assistant bodies flow through
// tool-marker + Markdown rendering; user/system bodies stay literal,
// whitespace-preserving text. Long bodies still fold by default.
function renderConversationRecord(norm) {
  // Engine step events — render the raw event chip + the default-expanded
  // step report card. Not a chat turn; bypasses the role-based bubble path.
  if (norm.kind === "step_completed" || norm.kind === "step_failed") {
    return renderStepEventRecord(norm);
  }

  // Step started / step status — a lightweight status row (text + icon). No
  // report card, no fold / raw / chip: it is just the step region's lifecycle
  // anchor. `step_started` marks RUNNING ("进行中"); `step_status` marks a
  // non-terminal SETTLED state ("已暂停" / "重试中"). Both group under the same
  // stepKey as the step's other records, so the region appears the instant the
  // step starts (vital for non-LLM steps) and updates in place as it pauses.
  if (
    norm.kind === "step_started"
    || norm.kind === "step_status"
    || norm.kind === "waiting_for_lock"
    || norm.kind === "merging"
  ) {
    return renderStepStartedRecord(norm);
  }

  // Non-terminal step usage events — render a lightweight usage-only chip
  // (not a full report card, since the step hasn't completed). Shows the
  // step's token usage so the user sees self_check / verify_spec / test
  // usage even when they return REVISION_NEEDED and are abandoned in the
  // fix loop. The chip is collapsed by default; expand to see usage detail.
  if (norm.kind === "step_output") {
    return renderStepOutputUsageRecord(norm);
  }

  // Per-group DAG status — a lightweight, self-contained marker (not a chat
  // turn, no fold/raw affordances) inserted into the implement step's time
  // line so the user watches G1–G5 progress while the parallel step runs.
  if (norm.kind === "group_status") {
    return renderGroupStatusRecord(norm);
  }

  // Code-index update-progress — a lightweight, self-contained progress line
  // (not a chat turn, no fold/raw affordances) inserted into the commit step's
  // time line so the user watches the pre-commit code-index rebuild advance
  // file by file.
  if (norm.kind === "index_progress") {
    return renderIndexProgressRecord(norm);
  }

  const known = ["user", "assistant", "system"].includes(norm.role);
  const role = known ? norm.role : "other";
  const content = typeof norm.content === "string" ? norm.content : "";

  // user role with a template/user-content marker: a default-collapsed chip
  // for the boilerplate prefix + a default-expanded bubble for the actual
  // task content. Falls through to the legacy whole-chip path when there is
  // no marker (older / non-step prompts).
  if (norm.role === "user" && content) {
    const split = splitUserPromptByMarker(content);
    if (split) return renderUserMarkerRecord(norm, split);
  }

  const row = el("div", "history-record conv-record role-" + role);

  // Build the inner bubble lazily so a collapsed chip pays nothing until the
  // reader expands it.
  const buildBubble = () => {
    const bubble = el("div", "conv-bubble");
    // Agent/model badge: shown only on assistant bubbles when agentName is
    // present. Prepend it at the top so it appears above the bubble content.
    // No placeholder when the field is absent (backward-compatible).
    if (role === "assistant") {
      const badge = renderAgentBadge(norm);
      if (badge) bubble.appendChild(badge);
    }
    if (!content) {
      bubble.appendChild(
        el("p", "md-p conv-empty",
          tf("conv.recordEmpty", "(no readable content for this record)")));
    } else if (role === "assistant") {
      // assistant: two-layer progressive disclosure. The default view is the
      // narrative + clean structured result (via STEP_ASSISTANT_RENDERERS); the
      // turn's original record is reachable behind a single "查看原始" entry
      // (makeAssistantRawToggle) — there is no "展开全部" wrapper on the assistant
      // side. When no result JSON is present — or no structured renderer can
      // parse the body — the thinking process is rendered inline in full via
      // renderToolMarkers (never folded), so nothing is hidden, per the message
      // paradigm and the Three-Tier Progressive Disclosure requirement.
      bubble.appendChild(renderAssistantBubble(content, norm));
    } else {
      // user / system / other: literal text — these are large structured
      // prompts whose exact whitespace matters; do not Markdown-mangle them.
      const buildFull = () => el("pre", "conv-plain", content);
      bubble.appendChild(makeFoldable(buildFull, content));
    }
    // Inline thumbnails for any attachment path the turn names. Built HERE
    // rather than at row level because buildBubble is the one construction
    // point both the expanded and the collapsed-chip paths run through, so
    // every role gets them from a single call — and the path text above stays
    // exactly as written (see renderInlineUploadImages).
    const inlineImages = renderInlineUploadImages(content);
    if (inlineImages) bubble.appendChild(inlineImages);
    return bubble;
  };

  if (isCollapsibleRole(norm.role)) {
    // Template-style prompt: collapse to a one-line chip by default. The chip
    // header carries the role/step label; clicking expands the full record
    // (head + bubble + raw toggle) and clicking again collapses it. Content is
    // never removed — only hidden.
    const wrap = el("div", "msg-chip-wrap collapsed");
    const label = chipLabel(norm);
    const chip = el("button", "msg-chip", "▸ " + label);
    chip.type = "button";
    const detail = el("div", "msg-chip-detail");
    let built = false;
    let expanded = false;
    chip.addEventListener("click", () => {
      expanded = !expanded;
      if (expanded && !built) {
        detail.appendChild(renderRecordHead(norm));
        detail.appendChild(buildBubble());
        // Unified "every conversation message can view raw": dispatch by role to
        // an always-non-null raw toggle instead of the nullable makeRawToggle, so
        // a system / user chip ALWAYS exposes "查看原始" (no longer disappears when
        // no raw payload is present). user keeps its envelope fallback semantics
        // (makeUserRawToggle, per 3870fd8e); system falls back to the content
        // literal (makeAssistantRawToggle). makeRawToggle's null contract is left
        // intact for non-conversation paths — it is simply no longer called here.
        if (norm.role === "user") {
          detail.appendChild(makeUserRawToggle(norm));
        } else {
          detail.appendChild(makeAssistantRawToggle(content, norm));
        }
        built = true;
      }
      wrap.classList.toggle("collapsed", !expanded);
      chip.textContent = (expanded ? "▾ " : "▸ ") + label;
      if (expanded) {
        requestAnimationFrame(() => detail.scrollIntoView({ block: "nearest" }));
      }
    });
    wrap.append(chip, detail);
    row.appendChild(wrap);
    return row;
  }

  // assistant / other: expanded by default. For an assistant turn WITH content
  // the "查看原始" raw toggle is NOT appended at the row level — it is the fold
  // built inside `renderAssistantBubble` → `makeAssistantRawToggle` (below the
  // rendered result or the inline process), so the default Layer-1 view stays
  // clean. But an assistant turn with empty/whitespace content takes the
  // "(no readable content)" branch in buildBubble and never invokes
  // renderAssistantBubble, so it would otherwise get NO toggle at all even when
  // its raw_json / raw_ndjson carries a payload (e.g. a pure tool-call turn whose
  // text was stored empty). For `other` (non-collapsible non-assistant) roles
  // there is likewise no internal fold. So per the unified "every conversation
  // message can view raw" principle append an always-present "查看原始" here for
  // every non-assistant role AND for an empty-content assistant turn, while still
  // avoiding a duplicate toggle on the assistant-with-content path.
  row.appendChild(renderRecordHead(norm));
  row.appendChild(buildBubble());
  if (role !== "assistant" || !content) {
    row.appendChild(makeAssistantRawToggle(content, norm));
  }
  return row;
}

// ---------------------------------------------------------------------------
// Per-group DAG status markers (group_status)
// ---------------------------------------------------------------------------

// Build the conversation-row form of a per-group DAG status record. Each
// record is its own marker placed in strict (timestamp, index) order by
// addConversationRecords, so a group's successive states (queued → running →
// completed) appear as the parallel implement step advances. The marker is
// intentionally affordance-free: no fold, no raw toggle, no chip — it never
// disturbs the fold / raw state of the surrounding chat bubbles, and a
// step-header rebuild around it leaves it untouched.
function renderGroupStatusRecord(norm) {
  const status = String(norm.status || "").toLowerCase();
  const row = el(
    "div",
    "history-record conv-record role-group-status group-status-marker status-" +
      (status || "unknown"),
  );
  const icon = GROUP_STATUS_ICON[status] || "•";
  row.appendChild(el("span", "group-status-icon", icon));
  row.appendChild(
    el("span", "group-status-text", groupStatusLabel(norm.groupId, norm.status)),
  );
  // Agent/model badge: show the agent the group's LLMCaller is currently using
  // (and, once parsed, the actual model) so the "正在 worktree 实施中" marker
  // shows the same `agent` / `agent · model` text as other LLM steps. Reuses
  // formatAgentBadgeText so the format never drifts from the chat-bubble badge,
  // and renders nothing for legacy records lacking these fields (no placeholder).
  // Each group_status record renders its own marker; addConversationRecords'
  // removeSupersededGroupStatusRows pass then folds all markers sharing one
  // (step_id, group_id) composite key down to a single card — keeping the
  // latest (terminal-preferred) one. Because later records carry the
  // accumulated agent/model, that surviving card shows the upgraded
  // agent → agent · model badge, achieving an in-place update across
  // retries/rotations without stacking duplicate cards or reordering.
  const badgeText = formatAgentBadgeText(norm.agentName, norm.modelName);
  if (badgeText) {
    const badge = el("span", "agent-badge group-status-agent", badgeText);
    row.appendChild(badge);
  }
  return row;
}

// ---------------------------------------------------------------------------
// Code-index update-progress markers (index_progress)
// ---------------------------------------------------------------------------

// Build the conversation-row form of a code-index update-progress record. The
// commit step rebuilds se3/code-index.md before staging, re-summarising every
// touched node; each node emits one of these markers so the web console shows a
// live "更新 code-index：<path> (i/N)" line as the rebuild advances. Like the
// group_status marker it is intentionally affordance-free (no fold, no raw
// toggle, no chip) so it never disturbs the surrounding chat bubbles, and
// addConversationRecords' removeSupersededIndexProgressRows pass folds all
// markers of one step down to a SINGLE line that updates in place — the count
// climbs and the icon flips to ✓ when done reaches total, rather than stacking
// one row per file.
function renderIndexProgressRecord(norm) {
  const state = indexProgressState(norm.done, norm.total);
  const row = el(
    "div",
    "history-record conv-record role-index-progress index-progress-marker status-" +
      state,
  );
  row.appendChild(
    el("span", "index-progress-icon", INDEX_PROGRESS_ICON[state] || "•"),
  );
  row.appendChild(
    el(
      "span",
      "index-progress-text",
      indexProgressLabel(norm.path, norm.done, norm.total),
    ),
  );
  return row;
}

// ---------------------------------------------------------------------------
// Step event records (step_completed / step_failed)
// ---------------------------------------------------------------------------

// Build the conversation-row form of a `step_started` event — a lightweight
// "进行中" status row marking that the step has entered RUNNING. It is
// intentionally affordance-free: a single status line carrying BOTH an icon and
// the explicit "进行中" text (never color alone), with no report card, no fold,
// no raw toggle, no chip. This makes a step's region appear the instant it
// starts — most importantly for non-LLM steps (TEST / COMMIT / SPEC_GATE) that
// emit no conversation records and would otherwise stay blank until their
// terminal step_completed lands. Because it shares the step's `stepKey`
// (= step_id) with the step's later chat / step_output / step_completed /
// step_failed records, all of them group into ONE visual step region; the
// terminal/intermediate events never spawn a second same-named region. The DOM
// carries a `step-type-<type>` class (added by addConversationRecords) plus a
// `step-status-running` class so a later group can apply low-saturation
// per-step grouping styles.
function renderStepStartedRecord(norm) {
  const stepLabel = norm.stepType
    ? (resolveStepReportTitle(norm.stepType) || norm.stepType)
    : "step";
  const status = String(norm.status || "running").toLowerCase();
  const display = stepStatusDisplay(status);
  const kind = norm.kind === "step_status"
    ? "step_status"
    : norm.kind === "waiting_for_lock"
      ? "waiting_for_lock"
      : "step_started";
  const row = el(
    "div",
    "history-record conv-record role-step-event kind-" + kind + " "
      + "step-status-row step-status-" + status,
  );
  row.appendChild(el("span", "step-status-icon", display.icon));
  row.appendChild(
    el("span", "step-status-text", stepLabel + " · " + display.text));
  return row;
}

// Build the conversation-row form of a step_completed / step_failed event:
// the raw event surfaces as a default-collapsed chip (preserving the original
// raw payload for inspection), and the default-expanded step-report card sits
// right below it. Both live under the same `.history-step` container as the
// step's chat messages, so they group naturally with that step's discussion.
function renderStepEventRecord(norm) {
  const isFailed = norm.kind === "step_failed";
  const row = el(
    "div",
    "history-record conv-record role-step-event kind-" + norm.kind,
  );

  const verb = isFailed
    ? tf("stepReport.chip.failed", "Step failed")
    : tf("stepReport.chip.completed", "Step completed");
  const icon = isFailed ? "✗" : "✓";
  const stepLabel = norm.stepType
    ? (resolveStepReportTitle(norm.stepType) || norm.stepType)
    : "step";
  const label = `${icon} ${verb} · ${stepLabel}`;

  // Raw event chip — collapsed by default; expand to inspect the source JSON.
  const chipWrap = el("div", "msg-chip-wrap collapsed step-event-chip kind-" + norm.kind);
  const chip = el("button", "msg-chip step-event-chip-button", "▸ " + label);
  chip.type = "button";
  const detail = el("div", "msg-chip-detail");
  let chipBuilt = false;
  let chipExpanded = false;
  chip.addEventListener("click", () => {
    chipExpanded = !chipExpanded;
    if (chipExpanded && !chipBuilt) {
      detail.appendChild(
        el("pre", "raw-json", formatRaw(norm.raw && norm.raw.raw_json)),
      );
      chipBuilt = true;
    }
    chipWrap.classList.toggle("collapsed", !chipExpanded);
    chip.textContent = (chipExpanded ? "▾ " : "▸ ") + label;
    if (chipExpanded) {
      requestAnimationFrame(() => detail.scrollIntoView({ block: "nearest" }));
    }
  });
  chipWrap.append(chip, detail);
  row.appendChild(chipWrap);

  // Default-expanded report card (still collapsible by the reader).
  const card = norm.stepReport ? renderStepReport({
    step_type: norm.stepReport.step_type,
    step_id: norm.stepReport.step_id,
    status: norm.stepReport.status,
    outputs: norm.stepReport.outputs || {},
    error_message: norm.stepReport.error_message || "",
  }) : null;
  if (card) row.appendChild(card);

  return row;
}

// ---------------------------------------------------------------------------
// Non-terminal step usage (step_output)
// ---------------------------------------------------------------------------
//
// A `step_output` event is emitted for steps that consumed tokens but have
// not reached a terminal status (PAUSED / REVISION_NEEDED / RETRYING).
// Unlike `step_completed` / `step_failed`, these render a lightweight usage-
// only chip — no full report card (the step hasn't completed). The chip shows
// the step type label and its `token_usage` as a compact footnote; expand to
// inspect the raw JSON payload.

function renderStepOutputUsageRecord(norm) {
  const row = el(
    "div",
    "history-record conv-record role-step-event kind-step_output",
  );

  const stepLabel = norm.stepType
    ? (resolveStepReportTitle(norm.stepType) || norm.stepType)
    : "step";

  // Usage footnote — the primary visible content. Only rendered when the
  // step actually consumed tokens; an empty/absent usage produces no row.
  const stepOutputs = (norm.stepReport && norm.stepReport.outputs) || {};
  const usage = stepOutputs.token_usage;
  const usageSummary = usagePayloadSummary(stepOutputs.usage_summary);
  if (isTokenUsageEmpty(usage)) {
    // No tokens consumed — still return the row with a collapsed chip only,
    // so the record's existence is preserved for the session badge logic.
    const chipWrap = el("div", "msg-chip-wrap collapsed step-event-chip kind-step_output");
    const inProgress = () => tf(
      "stepOutput.inProgress", stepLabel + " (in progress)", { label: stepLabel });
    const chip = el("button", "msg-chip step-event-chip-button",
      "▸ " + inProgress());
    chip.type = "button";
    const detail = el("div", "msg-chip-detail");
    let chipBuilt = false;
    chip.addEventListener("click", () => {
      if (!chipBuilt) {
        detail.appendChild(
          el("pre", "raw-json", formatRaw(norm.raw && norm.raw.raw_json)),
        );
        chipBuilt = true;
      }
      chipWrap.classList.toggle("collapsed");
      chip.textContent = (chipWrap.classList.contains("collapsed") ? "▸ " : "▾ ")
        + inProgress();
      if (!chipWrap.classList.contains("collapsed")) {
        requestAnimationFrame(() => detail.scrollIntoView({ block: "nearest" }));
      }
    });
    chipWrap.append(chip, detail);
    row.appendChild(chipWrap);
    return row;
  }

  // With usage: render the usage footnote as the primary display.
  const label = stepLabel + " · " + formatTokenUsage(usage, usageSummary);
  const chipWrap = el("div", "msg-chip-wrap collapsed step-event-chip kind-step_output");
  const chip = el("button", "msg-chip step-event-chip-button", "▸ " + label);
  chip.type = "button";
  const detail = el("div", "msg-chip-detail");
  let chipBuilt = false;
  chip.addEventListener("click", () => {
    if (!chipBuilt) {
      detail.appendChild(
        el("pre", "raw-json", formatRaw(norm.raw && norm.raw.raw_json)),
      );
      chipBuilt = true;
    }
    chipWrap.classList.toggle("collapsed");
    chip.textContent = chipWrap.classList.contains("collapsed")
      ? "▸ " + label
      : "▾ " + label;
    if (!chipWrap.classList.contains("collapsed")) {
      requestAnimationFrame(() => detail.scrollIntoView({ block: "nearest" }));
    }
  });
  chipWrap.append(chip, detail);
  row.appendChild(chipWrap);

  // Usage footnote in expanded form (always visible alongside the chip).
  const footnote = buildStepUsageFootnote(usage, usageSummary);
  if (footnote) row.appendChild(footnote);

  return row;
}

// ---------------------------------------------------------------------------
// User-prompt marker record (template prefix chip + actual content bubble)
// ---------------------------------------------------------------------------

// Build the row for a `user` message whose body has the sentinel markers,
// using the user side's three-layer progressive disclosure (the assistant side
// is now two layers — narrative + result, then a single "查看原始"):
//   Layer 1 (default): the user's literal input (the middle USER_CONTENT
//                      section) as a default-expanded bubble — only what the
//                      human typed, never the framework boilerplate.
//   Layer 2 ("展开全部"): the full prompt the LLM saw, as the 模板前缀 /
//                      框架后缀 subsections, collapsed by default via
//                      `makeUserPromptToggle`.
//   Layer 3 ("查看原始"): the raw NDJSON, nested at the end of the Layer-2
//                      expand area (never a row-level always-visible control).
//
// When the content section is empty (legacy two-segment record, or a step
// whose template wrapped an empty user_content), there is no user literal to
// surface as Layer 1: the record degrades to a single default-collapsed
// system-prompt chip combining the prefix + suffix (no user bubble), matching
// the no-marker whole-chip fallback. The raw payload toggle stays available in
// both shapes — nested inside the chip's expand detail or the Layer-2 area.
function renderUserMarkerRecord(norm, split) {
  const row = el("div", "history-record conv-record role-user user-prompt-marker");

  const ctx = norm.stepType || norm.stepId || "step";
  const hasContent = typeof split.content === "string" && split.content.length > 0;

  if (hasContent) {
    // Layer 1 — default-expanded bubble carrying ONLY the user's real input.
    // Literal text is preserved so the exact body the LLM saw is reproduced.
    const bubble = el("div", "conv-bubble user-content-bubble");
    bubble.appendChild(el("pre", "conv-plain", split.content));
    row.appendChild(bubble);

    // The marker split is the OTHER path a user turn can take (this function is
    // an early return out of renderConversationRecord, so it never reaches
    // buildBubble) — and it is where uploaded paths overwhelmingly land, since
    // the user's own typed content is the half the split isolates. Only that
    // half is scanned: the framework boilerplate around it names no attachments.
    const inlineImages = renderInlineUploadImages(split.content);
    if (inlineImages) row.appendChild(inlineImages);

    // Layer 2 — "展开全部" toggle revealing the 模板前缀 / 框架后缀 subsections, with
    // Layer 3 ("查看原始") nested at the end of its expand area. Collapsed by
    // default so neither the framework boilerplate nor the raw toggle shows in
    // the default view. ALWAYS offered so Layer 3 is stably reachable per the
    // running-flow-console spec: even with no boilerplate and no second-layer
    // raw payload, makeUserRawToggle falls back to the original .jsonl envelope
    // record, so the user turn's Layer 3 is always present (no longer gated on
    // the presence of boilerplate or a raw payload).
    row.appendChild(makeUserPromptToggle(split, norm));
  } else {
    // Empty user-content (legacy two-segment / prefix+suffix sandwich):
    // degrade to a single default-collapsed system-prompt chip combining the
    // prefix and suffix subsections (and the nested "查看原始" raw toggle), with
    // no user bubble.
    // Re-resolved on every paint (not cached in a const): a language switch
    // re-renders the console, and the toggle handler below must not restore a
    // label captured under the previous language.
    const label = () => chipLabel({ role: "system", stepType: ctx });
    const chipWrap = el("div", "msg-chip-wrap collapsed user-prompt-chip");
    const chip = el("button", "msg-chip", "▸ " + label());
    chip.type = "button";
    const chipDetail = el("div", "msg-chip-detail");
    let chipBuilt = false;
    let chipExpanded = false;
    chip.addEventListener("click", () => {
      chipExpanded = !chipExpanded;
      if (chipExpanded && !chipBuilt) {
        appendPromptSubsections(chipDetail, split);
        // Layer 3 — nested inside the chip's expand detail, never row-level.
        // Uses the user-side toggle so the original .jsonl envelope record is
        // always reachable even when no second-layer raw payload exists.
        chipDetail.appendChild(makeUserRawToggle(norm));
        chipBuilt = true;
      }
      chipWrap.classList.toggle("collapsed", !chipExpanded);
      chip.textContent = (chipExpanded ? "▾ " : "▸ ") + label();
      if (chipExpanded) {
        requestAnimationFrame(() => chipDetail.scrollIntoView({ block: "nearest" }));
      }
    });
    chipWrap.append(chip, chipDetail);
    row.appendChild(chipWrap);
  }

  return row;
}

// ---------------------------------------------------------------------------
// Step report cards
// ---------------------------------------------------------------------------
//
// Each completed step emits a `step_completed` event whose data carries a
// snapshot of the step's structured outputs. We mirror the CLI-side
// `src/se3/engine/step_renderers.py` registry with a per-step renderer that
// turns those outputs into a human-readable HTML card. Cards are default-
// expanded (but still collapsible) and always sit alongside the raw event
// chip — never replace it.

const STEP_REPORT_TITLES = {
  discovery: "Discovery",
  analyze: "Analysis",
  investigate: "Root-Cause Investigation",
  project_summary: "Project Summary",
  propose: "Proposal",
  design: "Design",
  plan: "Planning",
  plan_tasks: "Task Planning",
  confirm: "Confirmation",
  implement: "Implementation",
  test: "Testing",
  self_check: "Self Check",
  verify_spec: "Spec Verification",
  update_spec: "Spec Update",
  spec_gate: "Spec Gate",
  version_analyze: "Version Analysis",
  charter_freshness: "Charter Freshness",
  commit: "Commit",
  // The two worktree-merge steps that replaced the retired "合并中" bypass.
  merge_integrate: "Merge",
  version_reconcile: "Version Reconcile",
  summarize: "Work Summary",
};

// Per-step report-card title suffix (G3): the final report card of a step must
// read as that step's *result / summary*, never as a bare step name that a
// reader could mistake for a brand-new step heading. So instead of titling the
// card with the bare step label (e.g. "Implementation" — easily read as the
// start of a fresh IMPLEMENT step), every card is suffixed with an explicit
// `结果` / `总结` semantic word. `summarize` (which already IS a summary step)
// reads "总结"; every other step's final card reads "结果". The suffix is kept
// as data (not a literal at the call site) so the title is unit-testable and
// stays in parity across every registered STEP_REPORT_RENDERERS step.
const STEP_REPORT_TITLE_SUFFIX = {
  summarize: "Summary",
};
const STEP_REPORT_TITLE_SUFFIX_DEFAULT = "Result";

// Pure: build a report-card title for `stepType` as `<步骤> · 结果/总结`. The
// base label comes from STEP_REPORT_TITLES (title-case, intentionally distinct
// from the uppercase `.history-step-header` step labels in STEP_HEADER_TITLES),
// and the `· 结果` / `· 总结` suffix makes the card unmistakably the step's
// result rather than a new step. Unknown step types degrade to the raw key (or
// "Step") plus the default suffix so nothing is silently dropped or thrown.
function reportCardTitle(stepType) {
  const key = String(stepType || "").toLowerCase();
  const base = resolveStepReportTitle(stepType) || key || "Step";
  // Resolve the semantic suffix (结果 / 总结) at render time; the map value is the
  // offline fallback (null resolve = empty test dicts).
  const hasSuffixKey = Object.prototype.hasOwnProperty.call(
    STEP_REPORT_TITLE_SUFFIX, key);
  const suffixFallback = STEP_REPORT_TITLE_SUFFIX[key]
    || STEP_REPORT_TITLE_SUFFIX_DEFAULT;
  const trSuffix = I18N.resolve(
    hasSuffixKey ? "stepReportSuffix." + key : "stepReportSuffix.default");
  const suffix = trSuffix != null ? trSuffix : suffixFallback;
  return base + " · " + suffix;
}

// Pure-ish: resolve a step type's title-case report label via I18N at render
// time, falling back to the STEP_REPORT_TITLES map literal (offline / test env).
// Returns null for an unknown step type so callers can degrade to the raw key.
function resolveStepReportTitle(stepType) {
  const key = String(stepType || "").toLowerCase();
  const known = STEP_REPORT_TITLES[key];
  if (!known) return null;
  const tr = I18N.resolve("stepReport." + key);
  return tr != null ? tr : known;
}

// ---------------------------------------------------------------------------
// Token-usage display (G4)
// ---------------------------------------------------------------------------
//
// The engine attaches a per-step `token_usage` dict to `step.outputs` and folds
// the running session total into `flow.state.session_token_usage` (G2). The web
// console surfaces both: a low-key footnote on each completed step's report
// card, and a session-total badge in the flow-view corner accumulated by the
// client over the step events it has received. The dict shape mirrors the
// engine's `UsageTotals.to_dict()` (token_usage.py):
//   { input_tokens, output_tokens, cache_creation_input_tokens,
//     cache_read_input_tokens, total_cost_usd }
// The two helpers below (formatTokenUsage / accumulateSessionUsage) are pure /
// DOM-free so they can be unit-tested headlessly.

const TOKEN_USAGE_TOKEN_FIELDS = [
  "input_tokens",
  "output_tokens",
  "cache_creation_input_tokens",
  "cache_read_input_tokens",
];

// Coerce a usage field to a finite number; missing / null / NaN → 0, mirroring
// the backend `_coerce_int` / `_coerce_float` tolerance so a partial or
// malformed payload never throws or renders NaN.
function usageNum(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

// True when every token field is zero AND the cost is (near) zero — matches
// `UsageTotals.is_empty()`. The display layer suppresses the footnote / badge
// for empty usage so steps that made no LLM call show nothing extra.
function isTokenUsageEmpty(usage) {
  if (!usage || typeof usage !== "object") return true;
  const tokens = TOKEN_USAGE_TOKEN_FIELDS.reduce(
    (sum, k) => sum + usageNum(usage[k]), 0);
  return tokens === 0 && usageNum(usage.total_cost_usd) === 0;
}

function formatTokenCount(n) {
  return usageNum(n).toLocaleString("en-US");
}

// Render a USD cost as `$0.0123` (4 dp), matching the Python `format_cost`
// precision so sub-cent LLM costs stay legible.
function formatCostUsd(v) {
  return "$" + usageNum(v).toFixed(4);
}

// Render a token_usage dict as a compact, labelled small string:
//   "in 12,345 · out 6,789 · cache r/w 1,000/200 · $0.0123"
// Safe for missing / null / partial input (each missing field → 0). The labels
// are chrome, so they come from the language dictionary (`usage.valueLine`);
// resolve() (not t()) is used so a boot-time dict miss degrades to the English
// baseline below rather than painting a raw key into a usage badge.
function formatTokenUsage(usage, summary) {
  const u = usage && typeof usage === "object" ? usage : {};
  // When the backend's shared UsageSummary is available it owns the cost
  // column: the legacy five-field projection collapses "no provider cost
  // reported" to 0.0, and rendering that as "$0.0000" fabricates a billing
  // figure the flow summary itself reports as unknown.
  const s = summary && typeof summary === "object" ? summary : null;
  const params = {
    in: formatTokenCount(u.input_tokens),
    out: formatTokenCount(u.output_tokens),
    cacheRead: formatTokenCount(u.cache_read_input_tokens),
    cacheWrite: formatTokenCount(u.cache_creation_input_tokens),
    cost: s ? formatCostOrUnknown(s.actual_cost_usd) : formatCostUsd(u.total_cost_usd),
  };
  const line = I18N.resolve("usage.valueLine", params);
  if (line != null) return line;
  return (
    "in " + params.in +
    " · out " + params.out +
    " · cache r/w " + params.cacheRead +
    "/" + params.cacheWrite +
    " · " + params.cost
  );
}

// Sum the per-step `token_usage` carried on the conversation records into one
// session total. De-duplicates by full record identity (`recordKey`) — NOT by
// step_id. RETAINED FOR TESTS AND DIAGNOSTICS ONLY: since G10 no rendered
// badge consumes this helper — the backend UsageSummary payload is the sole
// authoritative total, and a missing payload renders an explicit
// "unavailable" state (see applyUsageBadge) instead of a client-side sum that
// could double-count cumulative session costs or misclassify cached tokens.
//
// The match relies on the engine surfacing every token-consuming run in an
// emitted record. Both terminal (COMPLETED/PARTIAL/FAILED) and non-terminal
// (PAUSED/REVISION_NEEDED) runs now publish `token_usage` in `step.outputs`
// (G2: the data-layer fix that makes render_step_usage / buildStepUsageFootnote
// able to display usage regardless of the step's status). A non-terminal run
// also carries its combined (carried + current) total forward in
// `carried_token_usage` so the next run's `token_usage` includes all prior
// rounds. The single terminal record for a multi-round step therefore reflects
// the SUM of all its rounds.
//
// **De-duplication across `step_output` and `step_completed`**: When both a
// `step_output` (non-terminal intermediate) and a `step_completed` /
// `step_failed` (terminal) record exist for the SAME `step_id`, the terminal
// record's `token_usage` already includes all prior rounds (via
// `carried_token_usage`), so only the terminal record is counted. A `step_id`
// that has only `step_output` records (e.g. self_check REVISION_NEEDED in a
// fix loop where the step is abandoned) is counted from the LAST `step_output`
// record, whose `token_usage` carries the combined total including all prior
// non-terminal rounds of that step. This prevents double-counting for steps
// like discovery (PAUSED → COMPLETED) while still surfacing abandoned steps'
// usage.
//
// `carried_token_usage` is an engine-internal carry field, not a display
// source: renderers read ONLY `outputs.token_usage`, never
// `carried_token_usage`.
//
// A step_id is reused across fix-loop re-runs (test / self_check / verify_spec
// re-execute on the same Step object), each emitting a distinct terminal record
// with its own per-run token_usage; those carry distinct timestamps/content and
// so distinct `recordKey`s and are counted separately, just as the engine counts
// each run (and the carry is empty across terminal-to-terminal runs, so there is
// no overlap to double-count). A genuinely re-delivered identical record
// (snapshot re-fetch, reconnect, incremental re-render) shares its `recordKey`
// and is still counted exactly once; order-independent. Returns a usage dict
// with the same key set as a single step's token_usage (all zeros when nothing
// was found), so the caller can format / empty-check it uniformly.
function accumulateSessionUsage(records) {
  const totals = {
    input_tokens: 0,
    output_tokens: 0,
    cache_creation_input_tokens: 0,
    cache_read_input_tokens: 0,
    total_cost_usd: 0,
  };
  if (!Array.isArray(records)) return totals;

  // Phase 1: Identify step_ids that have terminal records (step_completed /
  // step_failed). For these step_ids, step_output records are NOT counted —
  // the terminal record's token_usage already includes all prior rounds via
  // carried_token_usage. For step_ids with only step_output records (e.g.
  // self_check REVISION_NEEDED abandoned in a fix loop), only the LAST
  // step_output record is counted, whose token_usage carries the combined
  // total including all prior rounds.
  const terminalStepIds = new Set();
  const stepOutputByStepId = new Map();  // step_id → last step_output record
  const seen = new Set();  // recordKey → boolean (dedup across both phases)

  for (const rec of records) {
    let norm;
    try {
      norm = normalizeRecord(rec);
    } catch (_) {
      continue;
    }
    if (!norm || norm.role !== "step-event" || !norm.stepReport) continue;
    const sid = norm.stepId;
    if (norm.kind === "step_completed" || norm.kind === "step_failed") {
      terminalStepIds.add(sid);
    } else if (norm.kind === "step_output") {
      // Track the LAST step_output record per step_id (overwrites earlier).
      stepOutputByStepId.set(sid, rec);
    }
  }

  // Phase 2a: Accumulate all terminal records, de-duped by recordKey.
  // Multiple terminal records for the same step_id (fix-loop re-runs) are
  // counted separately because they carry distinct per-run usage.
  for (const rec of records) {
    let norm;
    let key;
    try {
      norm = normalizeRecord(rec);
      key = recordKey(rec);
    } catch (_) {
      continue;
    }
    if (!norm || norm.role !== "step-event" || !norm.stepReport) continue;
    const usage = norm.stepReport.outputs && norm.stepReport.outputs.token_usage;
    if (isTokenUsageEmpty(usage)) continue;
    if (norm.kind !== "step_completed" && norm.kind !== "step_failed") continue;
    if (seen.has(key)) continue;
    seen.add(key);
    totals.input_tokens += usageNum(usage.input_tokens);
    totals.output_tokens += usageNum(usage.output_tokens);
    totals.cache_creation_input_tokens +=
      usageNum(usage.cache_creation_input_tokens);
    totals.cache_read_input_tokens += usageNum(usage.cache_read_input_tokens);
    totals.total_cost_usd += usageNum(usage.total_cost_usd);
  }

  // Phase 2b: Accumulate step_output records for step_ids that have NO
  // terminal record. Only the LAST step_output is used, whose token_usage
  // carries the combined total (carried + current round) so there is no
  // undercount for multi-round non-terminal steps.
  for (const [sid, rec] of stepOutputByStepId) {
    // Skip step_output records for step_ids that already have terminal records
    // — the terminal record's token_usage includes all prior rounds.
    if (terminalStepIds.has(sid)) continue;
    const key = recordKey(rec);
    if (seen.has(key)) continue;
    seen.add(key);
    let norm;
    try { norm = normalizeRecord(rec); } catch (_) { continue; }
    const usage = norm.stepReport.outputs && norm.stepReport.outputs.token_usage;
    if (isTokenUsageEmpty(usage)) continue;
    totals.input_tokens += usageNum(usage.input_tokens);
    totals.output_tokens += usageNum(usage.output_tokens);
    totals.cache_creation_input_tokens +=
      usageNum(usage.cache_creation_input_tokens);
    totals.cache_read_input_tokens += usageNum(usage.cache_read_input_tokens);
    totals.total_cost_usd += usageNum(usage.total_cost_usd);
  }

  return totals;
}

// Build the per-step report-card token-usage footnote, or null when the step
// consumed no tokens (so the card structure is unchanged for usage-less steps).
// Reads ONLY `outputs.token_usage`, never the internal `carried_token_usage`
// field — per the G2 convention that both CLI (render_step_usage) and WebUI
// (this function) share a single, consistent display source.
function buildStepUsageFootnote(usage, summary) {
  if (isTokenUsageEmpty(usage)) return null;
  const foot = el("div", "step-report__usage");
  foot.append(
    el("span", "step-report__usage-label", tf("stepReport.usageLabel", "tokens")),
    el("span", "step-report__usage-value",
      formatTokenUsage(usage, usagePayloadSummary(summary))),
  );
  return foot;
}

// ---------------------------------------------------------------------------
// Per-round token-usage footnote (G5)
// ---------------------------------------------------------------------------
//
// Unlike the per-step report card (driven by step_completed `outputs.token_usage`,
// the engine's per-STEP total), the per-round footnote is driven by the per-CALL
// `token_usage` that record_response attaches to each assistant ChatMessage
// (exposed as `norm.tokenUsage`). Every time the console shows content to the
// user — each discovery round, each confirm review — the footnote reports both
// this round's increment AND the running cumulative for the same interactive
// step. The cumulative is derived client-side by summing the per-round usages
// grouped by `step_id` (an interactive step keeps one step_id across its rounds),
// mirroring the CLI footer's `carried + current` arithmetic.

const ROUND_USAGE_FIELDS = [
  "input_tokens",
  "output_tokens",
  "cache_creation_input_tokens",
  "cache_read_input_tokens",
  "total_cost_usd",
];

function emptyUsageTotals() {
  const t = {};
  for (const k of ROUND_USAGE_FIELDS) t[k] = 0;
  return t;
}

function addUsageInto(totals, usage) {
  for (const k of ROUND_USAGE_FIELDS) totals[k] += usageNum(usage[k]);
}

// Pure: given the full ordered records array, return an array (same length as
// `records`) where element [i] is the cumulative token usage for record i's
// `step_id` — the running sum of every per-round usage seen at or before i that
// shares its step_id — or null when record i carries no (non-empty) round usage.
//
// De-dups by full record identity (`recordKey`, the same key accumulateSessionUsage
// uses): a record re-delivered across snapshots / reconnects must NOT advance the
// running sum, but it still snapshots the current cumulative so the duplicate
// renders the same footnote. Grouping by step_id keeps each interactive step's
// cumulative independent. O(n), DOM-free, exposed for unit testing.
function accumulateRoundUsageByStep(records) {
  const n = Array.isArray(records) ? records.length : 0;
  const result = new Array(n).fill(null);
  if (!n) return result;
  const perStep = Object.create(null); // step_id -> running totals
  const seen = new Set();
  for (let i = 0; i < n; i++) {
    let norm;
    let key;
    try {
      norm = normalizeRecord(records[i]);
      key = recordKey(records[i]);
    } catch (_) {
      continue; // a malformed record must never break the running sum
    }
    const usage = norm && norm.tokenUsage;
    if (isTokenUsageEmpty(usage)) continue;
    const stepId = String((norm && norm.stepId) || "");
    let totals = perStep[stepId];
    if (!totals) {
      totals = emptyUsageTotals();
      perStep[stepId] = totals;
    }
    // Advance the running sum once per distinct record; a re-delivered identical
    // record shares its key and is not double-counted.
    if (!seen.has(key)) {
      seen.add(key);
      addUsageInto(totals, usage);
    }
    result[i] = Object.assign({}, totals);
  }
  return result;
}

// Build the compact, low-key per-round usage footnote, or null when this round
// consumed no tokens (so a round that made no LLM call — empty redraw, resume
// re-display — shows nothing extra). Wording: 『本轮 X in / Y out · 累计 X in / Y out』.
// `cumulativeUsage` falls back to the round itself when missing/empty, so a
// single-round step or a direct (test) call without a precomputed cumulative
// still reads cleanly. Numbers reuse formatTokenCount for project-wide parity.
function buildRoundUsageFootnote(roundUsage, cumulativeUsage) {
  if (isTokenUsageEmpty(roundUsage)) return null;
  const cum = isTokenUsageEmpty(cumulativeUsage) ? roundUsage : cumulativeUsage;
  const text = tf("usage.roundFootnote",
    "This round " + formatTokenCount(roundUsage.input_tokens) + " in / " +
    formatTokenCount(roundUsage.output_tokens) + " out · Total " +
    formatTokenCount(cum.input_tokens) + " in / " +
    formatTokenCount(cum.output_tokens) + " out",
    {
      roundIn: formatTokenCount(roundUsage.input_tokens),
      roundOut: formatTokenCount(roundUsage.output_tokens),
      cumIn: formatTokenCount(cum.input_tokens),
      cumOut: formatTokenCount(cum.output_tokens),
    });
  const foot = el("div", "round-usage");
  foot.appendChild(el("span", "round-usage__text", text));
  return foot;
}

// Append the per-round usage footnote to `container` from a normalized record's
// `tokenUsage` (this round) + `cumulativeUsage` (running per-step total, set by
// the render loop). No-op when the round consumed no tokens. Shared by every
// interactive assistant render path so the footnote placement never drifts.
function appendRoundUsageFootnote(container, norm) {
  if (!container || !norm) return;
  const foot = buildRoundUsageFootnote(norm.tokenUsage, norm.cumulativeUsage);
  if (foot) container.appendChild(foot);
}

// Render the flow-view session-usage badge from the conversation records the
// client has received so far. Hidden (and emptied) when nothing has been
// consumed yet, so it never shows a bare "0" placeholder. Best-effort —
// a render fault here must never disturb the conversation.
// The single applyUsageBadge is defined in the G10 backend-summary section
// below (payload-first, explicit-unavailable otherwise) —
// updateFlowUsageBadge / updateHistoryUsageBadge delegate to it with their
// own payload sources.
function updateFlowUsageBadge(records) {
  // The open flow's compact backend summary (from the /api/flows/{id} snapshot)
  // is the authoritative badge source; the WS-delivered full payload is the
  // next fallback, and a pre-payload daemon leaves both null so the badge
  // shows the explicit unavailable state inside applyUsageBadge.
  const payload = (state.flowDetail && state.flowDetail.usage_summary)
    || state.flowConversationUsage;
  applyUsageBadge($("flow-usage-badge"), records, payload);
}

// History-detail counterpart of updateFlowUsageBadge: same total over the open
// session's records, rendered into the history view's header badge.
function updateHistoryUsageBadge(records) {
  applyUsageBadge($("history-usage-badge"), records, state.historyUsage);
}

// ---------------------------------------------------------------------------
// Backend usage-summary rendering (G10)
// ---------------------------------------------------------------------------
//
// The backend (engine → daemon → server) computes ONE usage/cost payload
// through tianluo.usage.build_usage_payload / UsageSummary — including
// provider-session de-duplication, cached-token semantics, model resolution
// and pricing. The frontend only RENDERS that payload; it never re-sums
// provider session costs, cached tokens or model pricing (the legacy
// client-side accumulation below stays solely as the pre-payload fallback for
// old daemons). Two payload shapes arrive, both consumed by the same
// renderers so history and live flows share one schema:
//
//   * full   — the history bundle's `usage`: {calls, steps, summary, legacy,
//              completeness};
//   * compact — FlowSnapshot / SessionMeta `usage_summary`: the records-free
//              UsageSummary dict ({totals, actual_cost_usd, estimated_cost_usd,
//              unknown_*, partial, diagnostics, completeness}).
//
// `usage_status` distinguishes explicit zeros (available + zero fields — a
// real "this call cost nothing" report) from missing data (unavailable) and
// legacy five-field adaptations (legacy_ambiguous); a non-available status is
// rendered as its own label, never as a misleading 0.

// Localized one-word label for a UsageStatus value; "" for `available` so the
// normal row carries no extra mark.
function usageStatusMark(status) {
  const s = String(status || "");
  if (!s || s === "available") return "";
  const text = I18N.resolve("usage.status." + s);
  if (text != null) return text;
  return s;
}

function usageTotalsFields(totals) {
  const t = totals && typeof totals === "object" ? totals : {};
  return {
    input: usageNum(t.logical_input_tokens),
    output: usageNum(t.output_tokens),
    cacheRead: usageNum(t.cache_read_input_tokens),
    cacheCreate: usageNum(t.cache_creation_input_tokens)
      + usageNum(t.cache_creation_5m_input_tokens)
      + usageNum(t.cache_creation_1h_input_tokens),
    status: String(t.usage_status || "available"),
  };
}

// Render the totals of a UsageSummary (its `totals` UsageRecord-shaped dict) as
// one compact labelled string. A non-available status renders its own label
// instead of pretending the zeros mean anything.
function formatUsageTotals(totals) {
  const f = usageTotalsFields(totals);
  const params = {
    in: formatTokenCount(f.input),
    out: formatTokenCount(f.output),
    cacheRead: formatTokenCount(f.cacheRead),
    cacheCreate: formatTokenCount(f.cacheCreate),
  };
  const mark = usageStatusMark(f.status);
  const base = I18N.resolve("usage.totalsLine", params);
  const line = base != null
    ? base
    : ("in " + params.in + " · out " + params.out +
       " · cache r/w " + params.cacheRead + "/" + params.cacheCreate);
  return mark ? line + " · " + mark : line;
}

// Optional USD: "$0.0123", or the localized "unknown" label for null/absent —
// an absent actual cost is missing data, never a fabricated $0.
function formatCostOrUnknown(v) {
  if (v == null || v === "") return tf("usage.unknown", "unknown");
  return formatCostUsd(v);
}

// "—" for a cost the backend never computed (per-call estimates do not exist
// in the payload — only flow/step summaries carry them), distinct from the
// "unknown" label used for a summary cost that is genuinely missing.
function formatCostOrDash(v) {
  if (v == null || v === "") return "—";
  return formatCostUsd(v);
}

// Extract the records-free summary dict from either payload shape (compact
// summary passthrough; full payload's `summary`), or null when neither holds
// one. Every renderer below goes through this single shape normalizer.
function usagePayloadSummary(payload) {
  if (!payload || typeof payload !== "object") return null;
  if (payload.summary && typeof payload.summary === "object") return payload.summary;
  if (payload.totals && typeof payload.totals === "object") return payload;
  return null;
}

// Append the actual/estimated/unknown counters + completeness badge for one
// summary dict. Actual and estimated stay separate lines (the backend never
// combines them, so the UI must not either); unknown counters only appear
// when non-zero.
function appendUsageCostLines(container, summary) {
  const s = summary && typeof summary === "object" ? summary : {};
  const actual = s.actual_cost_usd;
  const estimated = s.estimated_cost_usd;
  const row = el("div", "usage-cost-row");
  row.append(
    el("span", "usage-cost-item",
      tf("usage.actual", "Actual") + " " + formatCostOrUnknown(actual)),
  );
  if (estimated != null) {
    row.append(
      el("span", "usage-cost-item usage-cost-item--estimate",
        tf("usage.estimated", "Estimated") + " " + formatCostUsd(estimated)),
    );
  }
  container.appendChild(row);
  const unknowns = [
    ["unknownCalls", usageNum(s.unknown_call_count)],
    ["unknownModel", usageNum(s.unknown_model_count)],
    ["unknownPrice", usageNum(s.unknown_price_count)],
    ["unknownCacheTtl", usageNum(s.unknown_cache_ttl_count)],
  ];
  const parts = [];
  for (const [key, count] of unknowns) {
    if (count > 0) parts.push(tf("usage." + key, key) + " " + count);
  }
  if (parts.length) {
    container.appendChild(el("div", "usage-unknown-line", parts.join(" · ")));
  }
  if (s.completeness) {
    const complete = s.completeness === "complete";
    container.appendChild(el(
      "span",
      "usage-completeness " + (complete ? "ok" : "warn"),
      tf("usage.completeness", "Completeness") + ": " +
      tf("usage.completeness." + s.completeness, s.completeness),
    ));
  }
  if (s.partial) {
    container.appendChild(el("div", "usage-note", tf(
      "usage.partialNote", "Usage incomplete — see per-call status.")));
  }
}

// Render a compact UsageSummary (flow snapshot / session meta shape) into a
// container: totals line + cost/unknown/completeness lines.
function renderCompactUsageSummary(container, summary) {
  const s = usagePayloadSummary(summary);
  if (!s || !container) return;
  container.appendChild(el("div", "usage-totals-line", formatUsageTotals(s.totals)));
  appendUsageCostLines(container, s);
}

// Render the full build_usage_payload shape ({calls, steps, summary, legacy,
// completeness}) into a container: flow totals, per-call table, per-step
// table. Only ever fed backend-computed payloads.
function renderUsagePayloadRegion(container, payload) {
  if (!container || !payload || typeof payload !== "object") return;
  const summary = usagePayloadSummary(payload);
  if (!summary) {
    if (payload.completeness === "none") {
      container.appendChild(el("p", "empty", tf("usage.noUsage", "No usage data recorded.")));
    }
    return;
  }
  container.appendChild(el("h4", "usage-region__title", tf("usage.flowHeader", "Flow totals")));
  renderCompactUsageSummary(container, summary);
  if (payload.legacy) {
    container.appendChild(el("div", "usage-note", tf(
      "usage.legacyNote", "Usage recovered from legacy records — may be incomplete.")));
  }

  const calls = Array.isArray(payload.calls) ? payload.calls : [];
  if (calls.length) {
    container.appendChild(el("h4", "usage-region__title", tf("usage.callsHeader", "LLM calls / attempts")));
    const table = el("table", "usage-table");
    const thead = el("thead");
    const cols = [
      ["call", tf("usage.col.call", "Call"), ""],
      ["agent", tf("usage.col.agent", "Agent"), ""],
      ["runner", tf("usage.col.runner", "Runner"), ""],
      ["provider", tf("usage.col.provider", "Provider"), ""],
      ["model", tf("usage.col.model", "Model"), ""],
      ["status", tf("usage.col.status", "Status"), ""],
      ["input", tf("usage.col.input", "Input"), "num"],
      ["output", tf("usage.col.output", "Output"), "num"],
      ["cacheRead", tf("usage.col.cacheRead", "Cache read"), "num"],
      ["cacheCreate", tf("usage.col.cacheCreate", "Cache create"), "num"],
      ["actual", tf("usage.col.actual", "Actual"), "num"],
      ["estimate", tf("usage.col.estimate", "Estimate"), "num"],
    ];
    const headRow = el("tr");
    for (const [, label, cls] of cols) {
      headRow.appendChild(el("th", cls || null, label));
    }
    thead.appendChild(headRow);
    table.appendChild(thead);
    const tbody = el("tbody");
    for (const record of calls) {
      const f = usageTotalsFields(record);
      const row = el("tr");
      const attempt = record && record.attempt ? "#" + record.attempt : "";
      row.appendChild(el("td", null, String((record && record.call_id) || "") + attempt));
      row.appendChild(el("td", null, (record && record.agent_name) || "-"));
      row.appendChild(el("td", null, (record && record.runner_type) || "-"));
      row.appendChild(el("td", null, (record && record.provider) || "-"));
      // The backend's internal "unknown" sentinel is user-visible text here,
      // so it must follow the UI language like the CLI's
      // cli.display.usage.model_unknown counterpart does.
      const rawModel = (record && record.resolved_model) || "";
      const model = rawModel && rawModel !== "unknown"
        ? rawModel
        : tf("usage.unknown", "unknown");
      const modelCell = el("td", null, model);
      // The raw provider value stays visible as the hover title for diagnosis;
      // unresolved model names never enter pricing anyway (the backend marks
      // them unknown).
      if (record && record.reported_model && record.reported_model !== model) {
        modelCell.title = String(record.reported_model);
      }
      row.appendChild(modelCell);
      row.appendChild(el("td", null,
        usageStatusMark(record && record.usage_status) || tf("usage.status.available", "available")));
      row.appendChild(el("td", "num", formatTokenCount(f.input)));
      row.appendChild(el("td", "num", formatTokenCount(f.output)));
      row.appendChild(el("td", "num", formatTokenCount(f.cacheRead)));
      row.appendChild(el("td", "num", formatTokenCount(f.cacheCreate)));
      row.appendChild(el("td", "num",
        formatCostOrUnknown(record && record.actual_cost_usd)));
      // Per-call estimates are computed by the shared backend payload
      // (build_usage_payload), the same result the CLI terminal table
      // renders; a dash reads as "not estimable", never as a missing
      // computation.
      row.appendChild(el("td", "num",
        formatCostOrDash(record && record.estimated_cost_usd)));
      tbody.appendChild(row);
    }
    table.appendChild(tbody);
    container.appendChild(table);
  }

  const steps = payload.steps && typeof payload.steps === "object" ? payload.steps : {};
  const stepKeys = Object.keys(steps);
  if (stepKeys.length) {
    container.appendChild(el("h4", "usage-region__title", tf("usage.stepsHeader", "Per-step usage")));
    const table = el("table", "usage-table");
    const thead = el("thead");
    const headRow = el("tr");
    for (const label of [
      tf("usage.col.step", "Step"),
      tf("usage.col.calls", "Calls"),
      tf("usage.col.input", "Input"),
      tf("usage.col.output", "Output"),
      tf("usage.col.cacheRead", "Cache read"),
      tf("usage.col.cacheCreate", "Cache create"),
      tf("usage.col.actual", "Actual"),
      tf("usage.col.estimate", "Estimate"),
      tf("usage.col.completeness", "Completeness"),
    ]) {
      headRow.appendChild(el("th", null, label));
    }
    thead.appendChild(headRow);
    table.appendChild(thead);
    const tbody = el("tbody");
    for (const stepKey of stepKeys) {
      const entry = steps[stepKey];
      const entrySummary = entry && entry.summary;
      const f = usageTotalsFields(entrySummary && entrySummary.totals);
      const row = el("tr");
      row.appendChild(el("td", null, stepKey || "-"));
      row.appendChild(el("td", null, String(usageNum(entry && entry.record_count))));
      row.appendChild(el("td", "num", formatTokenCount(f.input)));
      row.appendChild(el("td", "num", formatTokenCount(f.output)));
      row.appendChild(el("td", "num", formatTokenCount(f.cacheRead)));
      row.appendChild(el("td", "num", formatTokenCount(f.cacheCreate)));
      row.appendChild(el("td", "num",
        formatCostOrUnknown(entrySummary && entrySummary.actual_cost_usd)));
      row.appendChild(el("td", "num",
        formatCostOrUnknown(entrySummary && entrySummary.estimated_cost_usd)));
      const completeness = entrySummary && entrySummary.completeness;
      row.appendChild(el("td", null, completeness
        ? tf("usage.completeness." + completeness, completeness)
        : "-"));
      tbody.appendChild(row);
    }
    table.appendChild(tbody);
    container.appendChild(table);
  }
}

// True when any record carries a non-empty legacy token_usage — a signal that
// usage exists while the backend supplied no authoritative summary. Pure
// existence probe only: it never computes a total, because the frontend must
// not apply a second accounting formula.
function hasLegacyTokenUsage(records) {
  if (!Array.isArray(records)) return false;
  for (const rec of records) {
    let norm;
    try { norm = normalizeRecord(rec); } catch (_) { continue; }
    const usage = norm && norm.stepReport
      && norm.stepReport.outputs && norm.stepReport.outputs.token_usage;
    if (!isTokenUsageEmpty(usage)) return true;
  }
  return false;
}

// Shared renderer for a session-total usage badge. `payload` (full history
// payload or compact flow/session summary) is the backend authority. A
// pre-payload (legacy) daemon yields no summary: the badge then shows an
// explicit "unavailable" state instead of recomputing totals client-side —
// blind client-side summation could double-count cumulative session costs or
// misclassify cached tokens, diverging from the backend rules. Hidden (and
// emptied) when neither holds anything, so it never shows a bare "0".
function applyUsageBadge(badge, records, payload) {
  if (!badge) return;
  try {
    const summary = usagePayloadSummary(payload);
    if (summary) {
      badge.innerHTML = "";
      badge.append(
        el("span", "flow-usage-badge__label", tf("usage.sessionLabel", "Session")),
        el("span", "flow-usage-badge__value", formatUsageTotals(summary.totals)),
      );
      const actual = summary.actual_cost_usd;
      const estimated = summary.estimated_cost_usd;
      if (actual != null || estimated != null) {
        const costParts = [];
        if (actual != null) {
          costParts.push(tf("usage.actual", "Actual") + " " + formatCostUsd(actual));
        }
        if (estimated != null) {
          costParts.push(tf("usage.estimated", "Estimated") + " " + formatCostUsd(estimated));
        }
        badge.append(el("span", "flow-usage-badge__cost", costParts.join(" · ")));
      }
      badge.classList.remove("hidden");
      return;
    }
    if (!hasLegacyTokenUsage(records)) {
      badge.innerHTML = "";
      badge.classList.add("hidden");
      return;
    }
    badge.innerHTML = "";
    badge.append(
      el("span", "flow-usage-badge__label", tf("usage.sessionLabel", "Session")),
      el("span", "flow-usage-badge__value",
        tf("usage.unavailableNote", "usage unavailable — legacy data")),
    );
    badge.classList.remove("hidden");
  } catch (_) {
    badge.innerHTML = "";
    badge.classList.add("hidden");
  }
}

// ---------------------------------------------------------------------------
// Plan-decomposition + review-scope display
// ---------------------------------------------------------------------------
//
// The plan-mode projection ({decomposition, granularity, group_count, reason,
// reason_key, legacy_strategy, inferred}) and the scope audit
// ({active_round, last_round, last_clean_full_round_id,
// completed_full_rounds}) are computed by the shared backends
// (strategy_view.py / review_scope) and relayed verbatim; the UI only labels
// them. A flow created before the plan-decomposition model carries no doctrine
// of its own, so it is shown as the retired path it actually recorded, marked
// as such — never as a new-model value it never had, and never written back.

function planDecompositionLabel(value) {
  return catalogLabel("plan.decomposition.", value);
}

function planGranularityLabel(value) {
  return catalogLabel("plan.granularity.", value);
}

function legacyStrategyLabel(value) {
  return catalogLabel("plan.legacy.", value);
}

// Resolve one enum value through the i18n catalog, degrading to the raw value
// (and to a localized "unknown" for an empty one) so an unrecognized backend
// value is still displayed rather than swallowed.
function catalogLabel(prefix, value) {
  const s = String(value || "");
  if (!s) return tf("plan.unknown", "unknown");
  const text = I18N.resolve(prefix + s);
  if (text != null) return text;
  return s;
}

// The plan-mode reason text. A ``reason_key`` marks a sentence the backend
// projection itself authored (legacy inference / legacy strategy record)
// rather than persisted flow data, so it is rendered through this catalog
// instead of verbatim English.
function planModeReasonText(planMode) {
  const key = String((planMode && planMode.reason_key) || "");
  if (key) {
    const text = I18N.resolve("plan.reason." + key);
    if (text != null) return text;
  }
  return planMode && planMode.reason ? String(planMode.reason) : "";
}

// Build the plan-mode rows (doctrine + granularity + group count + reason) for
// a plan-mode projection dict, or null when nothing is recoverable.
function buildPlanModeRows(planMode) {
  if (!planMode || typeof planMode !== "object") return null;
  const frag = document.createDocumentFragment();
  const kv = (k, v, title) => {
    const row = el("div", "kv");
    const valEl = el("span", "v", String(v));
    if (title) valEl.title = title;
    row.append(el("span", "k", k), valEl);
    return row;
  };
  if (planMode.decomposition) {
    let value = planDecompositionLabel(planMode.decomposition);
    if (planMode.granularity) {
      value += " / " + planGranularityLabel(planMode.granularity);
    }
    frag.appendChild(kv(tf("plan.label", "Plan decomposition"), value));
    if (typeof planMode.group_count === "number") {
      frag.appendChild(kv(
        tf("plan.groupsLabel", "Task groups"), String(planMode.group_count),
      ));
    }
  } else if (planMode.legacy_strategy) {
    // "not applicable" names the absence of a PLAN -> IMPLEMENT segment, which
    // reads the same in both models; both notes below describe legacy
    // provenance, so attaching them would date a current small/review/survey
    // flow to a model it never ran under. Only a real recorded path
    // (direct/planned) carries them.
    let value = legacyStrategyLabel(planMode.legacy_strategy);
    if (planMode.legacy_strategy !== "not_applicable") {
      value += " · " + tf("plan.legacyNote", "retired implementation strategy");
      if (planMode.inferred) {
        value += " · " + tf("plan.inferredNote", "inferred from legacy records");
      }
    }
    frag.appendChild(kv(tf("plan.label", "Plan decomposition"), value));
  } else {
    return null;
  }
  const reasonText = planModeReasonText(planMode);
  if (reasonText) {
    frag.appendChild(kv(tf("plan.reasonLabel", "Plan mode reason"), reasonText));
  }
  return frag;
}

// Render the plan-mode section into the flow sidebar body. Appended directly
// (not as its own titled section) so a flow without plan-mode info keeps the
// exact sidebar it had before the projection existed.
function appendPlanModeSection(body, flow) {
  const rows = buildPlanModeRows(flow && flow.plan_mode);
  if (!rows) return;
  const section = el("div", "detail-section");
  section.appendChild(el("h4", null, tf("plan.label", "Plan decomposition")));
  section.appendChild(rows);
  body.appendChild(section);
}

// Build the scope-audit rows from a review_scope projection, or null when the
// flow recorded none. `changedPathCount` is optional extra context from the
// last self_check outputs.
function buildScopeRows(reviewScope, changedPathCount) {
  if (!reviewScope || typeof reviewScope !== "object") return null;
  const round = (reviewScope.last_round && typeof reviewScope.last_round === "object")
    ? reviewScope.last_round
    : (reviewScope.active_round && typeof reviewScope.active_round === "object")
      ? reviewScope.active_round
      : null;
  const parts = [];
  if (round) {
    const mode = String(round.scope_mode || "");
    const modeLabel = I18N.resolve("scope.mode." + mode);
    const modeDisplay = modeLabel != null ? modeLabel : (mode || "-");
    const passDisplay = round.pass_index != null ? String(round.pass_index) : "-";
    const fixDisplay = round.fix_iteration != null ? String(round.fix_iteration) : "-";
    // The fallback literal carries the interpolated values already — tf() only
    // interpolates the *dictionary* template, so the no-dict fallback must be
    // pre-built (same pattern as formatTokenUsage's English baseline).
    parts.push(tf("scope.round.line",
      `${modeDisplay} pass ${passDisplay} · fix ${fixDisplay}`,
      { mode: modeDisplay, pass: passDisplay, fix: fixDisplay }));
    if (round.baseline_id) {
      const id = String(round.baseline_id).slice(0, 12);
      parts.push(tf("scope.baseline", `baseline ${id}`, { id }));
    }
  }
  if (changedPathCount != null && changedPathCount > 0) {
    parts.push(tf("scope.changedPaths", `${changedPathCount} changed path(s)`,
      { count: changedPathCount }));
  }
  const fullRounds = usageNum(reviewScope.completed_full_rounds);
  if (fullRounds > 0) {
    parts.push(tf("scope.fullRounds", `${fullRounds} full round(s) clean`,
      { count: fullRounds }));
  }
  if (!parts.length) return null;
  const row = el("div", "kv");
  row.append(
    el("span", "k", tf("scope.label", "Review scope")),
    el("span", "v", parts.join(" · ")),
  );
  return row;
}

// Collect the most recent self_check scope audit facts from conversation
// records (the engine persists scope_mode / baseline_id / scope_changed_paths
// / fix_iteration / pass_index into step outputs). Used by the history detail,
// whose session meta does not carry review_scope. Records without scope fields
// contribute nothing.
function collectScopeAuditFromRecords(records) {
  let found = null;
  if (!Array.isArray(records)) return found;
  for (const rec of records) {
    let norm;
    try { norm = normalizeRecord(rec); } catch (_) { continue; }
    if (!norm || norm.role !== "step-event" || !norm.stepReport) continue;
    const outputs = norm.stepReport.outputs;
    if (!outputs || typeof outputs !== "object") continue;
    if (outputs.scope_mode == null && outputs.baseline_id == null) continue;
    found = {
      scope_mode: outputs.scope_mode || null,
      baseline_id: outputs.baseline_id || "",
      fix_iteration: outputs.fix_iteration != null ? outputs.fix_iteration : 0,
      pass_index: outputs.self_check_pass_index != null
        ? outputs.self_check_pass_index
        : (outputs.pass_index != null ? outputs.pass_index : null),
      changed_paths: Array.isArray(outputs.scope_changed_paths)
        ? outputs.scope_changed_paths : [],
    };
  }
  return found;
}

// Render the history detail's plan-mode + scope meta block (the session-meta
// plan-mode projection and the scope audit collected from the records) plus the
// backend usage region. Every consumer below shares these renderers, so the
// live-flow sidebar, the history detail and the badges never diverge.
function renderHistoryStrategyScope(container, session, records) {
  if (!container) return;
  const frag = document.createDocumentFragment();
  const planRows = buildPlanModeRows(session && session.plan_mode);
  if (planRows) {
    const planSec = el("div", "history-meta-block");
    planSec.appendChild(el(
      "span", "history-meta-label", tf("plan.label", "Plan decomposition") + ":"));
    planSec.appendChild(planRows);
    frag.appendChild(planSec);
  }
  const audit = collectScopeAuditFromRecords(records);
  if (audit) {
    const roundMode = audit.scope_mode || "";
    const modeLabel = I18N.resolve("scope.mode." + roundMode);
    const modeDisplay = modeLabel != null ? modeLabel : (roundMode || "-");
    const passDisplay = audit.pass_index != null ? String(audit.pass_index) : "-";
    const fixDisplay = String(audit.fix_iteration || 0);
    const parts = [
      tf("scope.round.line",
        `${modeDisplay} pass ${passDisplay} · fix ${fixDisplay}`,
        { mode: modeDisplay, pass: passDisplay, fix: fixDisplay }),
    ];
    if (audit.baseline_id) {
      const id = String(audit.baseline_id).slice(0, 12);
      parts.push(tf("scope.baseline", `baseline ${id}`, { id }));
    }
    if (audit.changed_paths.length) {
      const count = audit.changed_paths.length;
      parts.push(tf("scope.changedPaths", `${count} changed path(s)`, { count }));
    }
    const scopeSec = el("div", "history-meta-block");
    scopeSec.appendChild(el(
      "span", "history-meta-label", tf("scope.label", "Review scope") + ":"));
    scopeSec.appendChild(el("span", "history-meta-value", parts.join(" · ")));
    frag.appendChild(scopeSec);
  }
  if (frag.childNodes.length) {
    container.innerHTML = "";
    container.appendChild(frag);
  }
}

// Render the open history session's backend usage payload into
// `#history-usage-region`. Hidden (and emptied) when the session carries no
// payload — history and live flows share the exact same renderers, so there is
// one schema and one set of formulas on the wire and on screen.
function renderHistoryUsageRegion() {
  const container = $("history-usage-region");
  if (!container) return;
  container.innerHTML = "";
  const summary = usagePayloadSummary(state.historyUsage);
  if (!summary) {
    container.classList.add("hidden");
    return;
  }
  renderUsagePayloadRegion(container, state.historyUsage);
  container.classList.remove("hidden");
}

// Refresh the open history session's strategy/scope meta and the backend usage
// region in one pass — the shared post-render step for the REST load and the
// WS history_data consumers alike.
function refreshHistoryMetaAndUsage(flowId) {
  const session = (state.historySessions || []).find(
    (x) => x && x.flow_id === flowId);
  renderHistoryStrategyScope($("history-meta"), session, state.historyRecords);
  renderHistoryUsageRegion();
}

// Build a default-open collapsible report card. `buildBody()` is invoked
// immediately so the body is rendered out of the gate; on a later toggle
// it is preserved (not rebuilt) so the reader's expand-state on inner
// foldables is not lost.
function makeReportCard(stepType, titleText, buildBody) {
  const card = el("div", "step-report kind-" + stepType);
  const head = el("div", "step-report__head");
  const toggle = el("button", "step-report__toggle", "▾");
  toggle.type = "button";
  head.append(toggle, el("span", "step-report__title", titleText));
  card.appendChild(head);

  const body = el("div", "step-report__body");
  body.appendChild(buildBody());
  card.appendChild(body);

  let open = true;
  toggle.addEventListener("click", () => {
    open = !open;
    body.classList.toggle("hidden", !open);
    toggle.textContent = open ? "▾" : "▸";
    if (open) {
      requestAnimationFrame(() => body.scrollIntoView({ block: "nearest" }));
    }
  });
  // Header text is also clickable for a wider hit target.
  head.addEventListener("click", (e) => {
    if (e.target === toggle) return;
    toggle.click();
  });
  return card;
}

// Public dispatch entry point: returns a default-expanded report card for the
// step, dispatching by `step_type` and falling back to a generic renderer for
// unregistered types (parity with `step_renderers.py:_default_render`).
function renderStepReport(step) {
  if (!step || typeof step !== "object") return null;
  const stepType = String(step.step_type || "").toLowerCase();
  const renderer = STEP_REPORT_RENDERERS[stepType] || renderDefaultReport;
  const title = reportCardTitle(stepType);
  return makeReportCard(stepType || "unknown", title, () => {
    const body = renderer(step, step.outputs || {});
    const frag = document.createDocumentFragment();
    if (body instanceof Node) frag.appendChild(body);
    if (step.error_message) {
      frag.appendChild(el("div", "step-report__error",
        tf("stepReport.errorPrefix", "Error: ") + String(step.error_message)));
    }
    // Token-usage footnote (G4): a low-key one-liner at the card's bottom,
    // shown only when this step actually consumed tokens. Steps with no
    // `token_usage` get no extra row, so their card structure is unchanged.
    const usageFoot = buildStepUsageFootnote(
      (step.outputs || {}).token_usage, (step.outputs || {}).usage_summary);
    if (usageFoot) frag.appendChild(usageFoot);
    return frag;
  });
}

// -- shared report-card building blocks --

function reportEmpty(text) {
  return el("p", "step-report__empty", text || tf("stepReport.empty", "(no report fields)"));
}

function reportStatusBar(parts) {
  const bar = el("div", "step-report__status-bar");
  parts.forEach((p, idx) => {
    if (idx > 0) bar.appendChild(el("span", "step-report__sep", "│"));
    if (p instanceof Node) bar.appendChild(p);
    else bar.appendChild(el("span", "step-report__stat", String(p)));
  });
  return bar;
}

function reportSection(title, body) {
  const sec = el("div", "step-report__section");
  if (title) sec.appendChild(el("h6", "step-report__section-title", title));
  if (body instanceof Node) sec.appendChild(body);
  else if (typeof body === "string") sec.appendChild(
    el("p", "step-report__text", body));
  return sec;
}

function reportList(items, formatItem) {
  const ul = el("ul", "step-report__list");
  let index = 0;
  for (const item of items) {
    const li = el("li");
    const out = formatItem ? formatItem(item, index) : null;
    if (out instanceof Node) li.appendChild(out);
    else if (typeof out === "string") li.textContent = out;
    else if (typeof item === "string") li.textContent = item;
    else li.textContent = safeStringify(item);
    ul.appendChild(li);
    index += 1;
  }
  return ul;
}

// -- generic outputs renderer (parity with step_renderers.py:_default_render) -
//
// Render an arbitrary `step.outputs` dict as field-by-field key/value rows:
//   * string > 300 chars → preview(first 200, newlines→spaces) + " (N chars)"
//     suffix (matches the CLI _default_render behavior so the user sees the
//     same head-of-line truncation across CLI and web).
//   * nested dict → recursively rendered indented one level via a
//     `.step-report__kv-nested` wrapper, so the user can read the shape
//     instead of staring at a single multi-line JSON literal.
//   * everything else → safeStringify.
// Returns a DocumentFragment carrying either a `.step-report__kv` block or
// nothing when `outputs` is empty / not a plain object — the caller decides
// how to surface "empty" so the same helper can back both `renderDefaultReport`
// (which prefers a "(step produced no outputs)" hint) and the assistant-bubble
// fallback (which never falls in here without a non-empty dict).
function renderGenericOutputs(outputs) {
  const frag = document.createDocumentFragment();
  if (!outputs || typeof outputs !== "object" || Array.isArray(outputs)) return frag;
  const entries = Object.entries(outputs);
  if (!entries.length) return frag;
  const kv = el("div", "step-report__kv");
  for (const [k, v] of entries) {
    kv.appendChild(renderGenericKvRow(k, v));
  }
  frag.appendChild(kv);
  return frag;
}

function renderGenericKvRow(k, v) {
  const r = el("div", "step-report__kv-row");
  r.append(el("span", "step-report__kv-k", k));
  if (v && typeof v === "object" && !Array.isArray(v)) {
    // Nested dict — render one indented level of key/value rows so the user
    // can read field names instead of a flat JSON dump.
    const nested = el("div", "step-report__kv-nested");
    const subEntries = Object.entries(v);
    if (subEntries.length) {
      for (const [nk, nv] of subEntries) {
        nested.appendChild(renderGenericKvRow(nk, nv));
      }
    }
    r.appendChild(nested);
  } else {
    const valEl = el("span", "step-report__kv-v");
    if (typeof v === "string" && v.length > 300) {
      // Char count (not formatSize's KB/MB scaling) — keeps CLI parity with
      // step_renderers.py:_default_render, which always reports characters.
      const size = tf("common.size.chars", `${v.length} chars`, { n: v.length });
      valEl.textContent = v.slice(0, 200).replace(/\n/g, " ") + `… (${size})`;
      valEl.title = size;
    } else if (typeof v === "string") {
      valEl.textContent = v;
    } else {
      valEl.textContent = safeStringify(v);
    }
    r.appendChild(valEl);
  }
  return r;
}

// -- default fallback (parity with step_renderers.py:_default_render) -------

function renderDefaultReport(step, outputs) {
  const frag = document.createDocumentFragment();
  const hasFields = outputs && typeof outputs === "object" &&
    !Array.isArray(outputs) && Object.keys(outputs).length > 0;
  if (!hasFields) {
    frag.appendChild(reportEmpty(
      tf("stepReport.empty.noOutputs", "(step produced no outputs)")));
  } else {
    frag.appendChild(renderGenericOutputs(outputs));
  }
  const status = step && step.status && String(step.status).toLowerCase();
  if (status && status !== "completed" && status !== "running") {
    frag.appendChild(el("div", "step-report__muted",
      tf("stepReport.status", "Status: " + status, { status })));
  }
  return frag;
}

// -- analyze (parity with step_renderers.py:_render_analyze) ----------------

function renderAnalyzeReport(step, outputs) {
  const frag = document.createDocumentFragment();
  frag.appendChild(reportStatusBar([
    `${tf("stepReport.label.task", "task")}: ${outputs.task_type || "N/A"}`,
    `${tf("stepReport.label.complexity", "complexity")}: ${outputs.complexity || "N/A"}`,
    `${tf("stepReport.label.scope", "scope")}: ${outputs.scope || "N/A"}`,
  ]));
  if (outputs.reasoning) {
    frag.appendChild(reportSection(tf("stepReport.section.reasoning", "Reasoning"), String(outputs.reasoning)));
  }
  const items = (Array.isArray(outputs.selected_items) && outputs.selected_items.length)
    ? outputs.selected_items
    : (Array.isArray(outputs.relevant_specs) ? outputs.relevant_specs : []);
  if (items.length) {
    frag.appendChild(reportSection(tf("stepReport.count.relevantSpecItems",
      `Relevant Spec Items (${items.length})`, { n: items.length }),
      reportList(items, (it) => {
        if (it && typeof it === "object") {
          const spec = it.spec || it.spec_name || "";
          const name = it.requirement_name || it.name || "";
          const label = (spec && name)
            ? `${spec}:${name}`
            : (spec || name || safeStringify(it));
          return document.createTextNode(label);
        }
        return document.createTextNode(String(it));
      })));
  }
  return frag;
}

// -- plan (parity with step_renderers.py:_render_plan) ----------------------

// Field-by-field proposal renderer. Mirrors the CLI display.render_proposal
// field order — summary / files_to_modify / files_to_create / rationale —
// and expands per-item dicts (path/reason for modify, path/purpose for create)
// into readable bullets instead of a single JSON pre block. Any leftover keys
// fall through renderGenericOutputs so nothing is dropped.
function renderProposalFields(proposal) {
  const frag = document.createDocumentFragment();
  if (!proposal || typeof proposal !== "object" || Array.isArray(proposal)) {
    return frag;
  }
  const known = new Set();

  const summary = proposal.summary;
  if (typeof summary === "string" && summary) {
    frag.appendChild(reportSection(tf("stepReport.section.summary", "Summary"), summary));
    known.add("summary");
  }

  const filesToModify = proposal.files_to_modify;
  if (Array.isArray(filesToModify) && filesToModify.length) {
    frag.appendChild(reportSection(
      tf("stepReport.count.filesToModify",
        `Files to Modify (${filesToModify.length})`, { n: filesToModify.length }),
      reportList(filesToModify, (f) => renderProposalFileItem(f, "reason")),
    ));
    known.add("files_to_modify");
  }

  const filesToCreate = proposal.files_to_create;
  if (Array.isArray(filesToCreate) && filesToCreate.length) {
    frag.appendChild(reportSection(
      tf("stepReport.count.filesToCreate",
        `Files to Create (${filesToCreate.length})`, { n: filesToCreate.length }),
      reportList(filesToCreate, (f) => renderProposalFileItem(f, "purpose")),
    ));
    known.add("files_to_create");
  }

  const rationale = proposal.rationale;
  if (typeof rationale === "string" && rationale) {
    frag.appendChild(reportSection(tf("stepReport.section.rationale", "Rationale"), rationale));
    known.add("rationale");
  }

  const rest = {};
  for (const [k, v] of Object.entries(proposal)) {
    if (!known.has(k) && v !== null && v !== undefined && v !== "") {
      rest[k] = v;
    }
  }
  if (Object.keys(rest).length) {
    frag.appendChild(reportSection(tf("stepReport.section.otherFields", "Other Fields"), renderGenericOutputs(rest)));
  }
  return frag;
}

function renderProposalFileItem(f, descKey) {
  if (f && typeof f === "object" && !Array.isArray(f)) {
    const path = f.path || "";
    const desc = f[descKey] || "";
    const wrap = el("span", "step-report__file-row");
    wrap.appendChild(el("span", "step-report__file-path", String(path)));
    if (desc) {
      wrap.appendChild(el("span", "step-report__muted", ` — ${desc}`));
    }
    return wrap;
  }
  return document.createTextNode(String(f));
}

// Field-by-field design renderer. Mirrors the CLI display.render_design
// field order — overview / components / interfaces / decisions — expanding
// list-of-dicts into name+description / name+signature+description /
// decision+reason rows.
function renderDesignFields(design) {
  const frag = document.createDocumentFragment();
  if (!design || typeof design !== "object" || Array.isArray(design)) {
    return frag;
  }
  const known = new Set();

  const overview = design.overview;
  if (typeof overview === "string" && overview) {
    frag.appendChild(reportSection(tf("stepReport.section.overview", "Overview"), overview));
    known.add("overview");
  }

  const components = design.components;
  if (Array.isArray(components) && components.length) {
    frag.appendChild(reportSection(
      tf("stepReport.count.components",
        `Components (${components.length})`, { n: components.length }),
      reportList(components, renderDesignComponentItem),
    ));
    known.add("components");
  }

  const interfaces = design.interfaces;
  if (Array.isArray(interfaces) && interfaces.length) {
    frag.appendChild(reportSection(
      tf("stepReport.count.interfaces",
        `Interfaces (${interfaces.length})`, { n: interfaces.length }),
      reportList(interfaces, renderDesignInterfaceItem),
    ));
    known.add("interfaces");
  }

  const decisions = design.decisions;
  if (Array.isArray(decisions) && decisions.length) {
    frag.appendChild(reportSection(
      tf("stepReport.count.keyDecisions",
        `Key Decisions (${decisions.length})`, { n: decisions.length }),
      reportList(decisions, renderDesignDecisionItem),
    ));
    known.add("decisions");
  }

  const rest = {};
  for (const [k, v] of Object.entries(design)) {
    if (!known.has(k) && v !== null && v !== undefined && v !== "") {
      rest[k] = v;
    }
  }
  if (Object.keys(rest).length) {
    frag.appendChild(reportSection(tf("stepReport.section.otherFields", "Other Fields"), renderGenericOutputs(rest)));
  }
  return frag;
}

function renderDesignComponentItem(c) {
  if (c && typeof c === "object" && !Array.isArray(c)) {
    const wrap = el("span", "step-report__design-item");
    const name = c.name || "";
    if (name) wrap.appendChild(el("span", "step-report__design-name", String(name)));
    const desc = c.description || c.responsibilities || "";
    if (desc) {
      wrap.appendChild(el("span", "step-report__muted",
        (name ? " — " : "") + String(desc)));
    }
    return wrap;
  }
  return document.createTextNode(String(c));
}

function renderDesignInterfaceItem(i) {
  if (i && typeof i === "object" && !Array.isArray(i)) {
    const wrap = el("span", "step-report__design-item");
    const name = i.name || "";
    if (name) wrap.appendChild(el("span", "step-report__design-name", String(name)));
    const sig = i.signature || "";
    if (sig) {
      wrap.appendChild(el("span", "step-report__design-sig", ` ${sig}`));
    }
    const desc = i.description || "";
    if (desc) {
      wrap.appendChild(el("span", "step-report__muted",
        ((name || sig) ? " — " : "") + String(desc)));
    }
    return wrap;
  }
  return document.createTextNode(String(i));
}

function renderDesignDecisionItem(d) {
  if (d && typeof d === "object" && !Array.isArray(d)) {
    const wrap = el("span", "step-report__design-item");
    const decision = d.decision || "";
    if (decision) wrap.appendChild(el("span", "step-report__design-name", String(decision)));
    const reason = d.reason || "";
    if (reason) {
      wrap.appendChild(el("span", "step-report__muted",
        (decision ? " — " : tf("stepReport.design.reasonPrefix", "Reason: ")) + String(reason)));
    }
    return wrap;
  }
  return document.createTextNode(String(d));
}

function renderPlanReport(step, outputs) {
  const frag = document.createDocumentFragment();
  const plan = outputs.plan && typeof outputs.plan === "object"
    ? outputs.plan : {};
  const proposal = plan.proposal;
  const design = plan.design;
  const groups = Array.isArray(outputs.task_groups) ? outputs.task_groups : [];

  if (proposal && typeof proposal === "object" && !Array.isArray(proposal)) {
    const body = renderProposalFields(proposal);
    if (body.childNodes.length) {
      frag.appendChild(reportSection(tf("stepReport.section.proposal", "Proposal"), body));
    } else {
      // Empty-object proposal: keep the section header so the user still sees
      // it was present, but show the generic empty hint instead of a blank.
      frag.appendChild(reportSection(tf("stepReport.section.proposal", "Proposal"), renderStructured(proposal)));
    }
  } else if (typeof proposal === "string" && proposal) {
    frag.appendChild(reportSection(tf("stepReport.section.proposal", "Proposal"), proposal));
  }
  if (design && typeof design === "object" && !Array.isArray(design)) {
    const body = renderDesignFields(design);
    if (body.childNodes.length) {
      frag.appendChild(reportSection(tf("stepReport.section.design", "Design"), body));
    } else {
      frag.appendChild(reportSection(tf("stepReport.section.design", "Design"), renderStructured(design)));
    }
  } else if (typeof design === "string" && design) {
    frag.appendChild(reportSection(tf("stepReport.section.design", "Design"), design));
  }
  if (groups.length) {
    frag.appendChild(reportSection(
      tf("stepReport.count.taskGroups",
        `Task Groups (${groups.length})`, { n: groups.length }),
      reportList(groups, (g) => {
        const tasks = Array.isArray(g && g.tasks) ? g.tasks : [];
        const totalLoc = tasks.reduce(
          (s, t) => s + (Number(t && t.estimated_loc) || 0), 0);
        const deps = Array.isArray(g && g.depends_on) && g.depends_on.length
          ? g.depends_on.join(", ") : tf("stepReport.plan.dependsNone", "none");
        const row = el("span", "step-report__group-row");
        row.append(
          el("span", "step-report__group-id", String(g.group_id || "?")),
          el("span", "step-report__group-name", " " + String(g.name || "")),
          el("span", "step-report__muted",
            "  · " + tf("stepReport.plan.groupMeta",
              `${tasks.length} tasks · ~${totalLoc} LOC · depends: ${deps}`,
              { tasks: tasks.length, loc: totalLoc, deps })),
        );
        return row;
      })));
  }
  if (!frag.childNodes.length) {
    return renderDefaultReport(step, outputs);
  }
  return frag;
}

// Render arbitrary nested data as a pre-formatted JSON block (no height cap).
function renderStructured(data) {
  const pre = el("pre", "step-report__json");
  pre.textContent = safeStringify(data);
  return pre;
}

// -- implement (parity with step_renderers.py:_render_implement) ------------

function renderImplementReport(step, outputs) {
  const frag = document.createDocumentFragment();
  const status = String(outputs.completion_status || "unknown");
  const filesChanged = Array.isArray(outputs.files_changed) ? outputs.files_changed : [];
  const testsAdded = Array.isArray(outputs.tests_added) ? outputs.tests_added : [];
  const implGroups = Array.isArray(outputs.implemented_groups) ? outputs.implemented_groups : [];
  const summary = outputs.summary || "";
  const incomplete = Array.isArray(outputs.incomplete_tasks) ? outputs.incomplete_tasks : [];
  const restrictedApplied = Array.isArray(outputs.restricted_edits_applied) ? outputs.restricted_edits_applied : [];
  const restrictedFailed = Array.isArray(outputs.restricted_edits_failed) ? outputs.restricted_edits_failed : [];

  const icons = { complete: "✓", partial: "◐", failed: "✗" };
  const classes = { complete: "ok", partial: "warn", failed: "fail" };
  const cls = classes[status] || "muted";

  const bar = el("div", "step-report__status-bar");
  bar.append(
    el("span", "step-report__icon " + cls, icons[status] || "●"),
    el("span", "step-report__label " + cls,
      tf("stepReport.implement.status." + status, status)),
  );
  if (implGroups.length) {
    bar.append(el("span", "step-report__sep", "│"),
      el("span", null, tf("stepReport.implement.groups",
        `${implGroups.length} groups`, { n: implGroups.length })));
  }
  bar.append(
    el("span", "step-report__sep", "│"),
    el("span", null, tf("stepReport.implement.files",
      `${filesChanged.length} files`, { n: filesChanged.length })),
  );
  if (testsAdded.length) {
    bar.append(el("span", "step-report__sep", "│"),
      el("span", null, tf("stepReport.implement.tests",
        `${testsAdded.length} tests`, { n: testsAdded.length })));
  }
  frag.appendChild(bar);

  if (summary) {
    const parts = String(summary).split(";").map((s) => s.trim()).filter(Boolean);
    if (parts.length <= 1) {
      frag.appendChild(reportSection(tf("stepReport.section.summary", "Summary"), parts[0] || String(summary)));
    } else {
      frag.appendChild(reportSection(tf("stepReport.section.summary", "Summary"),
        reportList(parts, (p, i) => {
          const span = el("span");
          span.appendChild(el("span", "step-report__group-id",
            (implGroups.length ? `G${i + 1}` : String(i + 1)) + "."));
          span.appendChild(document.createTextNode(" " + p));
          return span;
        })));
    }
  }

  if (filesChanged.length) {
    const grouped = {};
    for (const fp of filesChanged) {
      const norm = String(fp).replace(/\\/g, "/");
      const parts = norm.split("/");
      const dir = parts.length > 1 ? parts[0] + "/" : "./";
      (grouped[dir] = grouped[dir] || []).push(norm);
    }
    const sortedDirs = Object.keys(grouped).sort((a, b) => {
      if (a === b) return 0;
      if (a === "./") return 1;
      if (b === "./") return -1;
      return a.localeCompare(b);
    });
    const wrap = el("div", "step-report__files");
    for (const dir of sortedDirs) {
      const list = grouped[dir].slice().sort();
      const dirEl = el("div", "step-report__file-group");
      dirEl.appendChild(el("div", "step-report__file-dir",
        `${dir} (${list.length})`));
      for (const f of list) {
        dirEl.appendChild(el("div", "step-report__file-row", f));
      }
      wrap.appendChild(dirEl);
    }
    frag.appendChild(reportSection(
      tf("stepReport.count.filesChanged",
        `Files Changed (${filesChanged.length})`, { n: filesChanged.length }), wrap));
  }

  if (testsAdded.length) {
    frag.appendChild(reportSection(tf("stepReport.count.testsAdded",
      `Tests Added (${testsAdded.length})`, { n: testsAdded.length }),
      reportList(testsAdded, (t) => document.createTextNode("+ " + String(t)))));
  }

  if (incomplete.length) {
    frag.appendChild(reportSection(tf("stepReport.count.incompleteTasks",
      `Incomplete Tasks (${incomplete.length})`, { n: incomplete.length }),
      reportList(incomplete, (t) => {
        if (t && typeof t === "object") {
          const tid = t.task_id || t.id || "?";
          const reason = t.reason || t.error || "";
          const row = el("span");
          row.appendChild(el("span", "step-report__group-id", String(tid)));
          if (reason) row.appendChild(document.createTextNode(": " + reason));
          return row;
        }
        return document.createTextNode(String(t));
      })));
  }

  if (restrictedApplied.length || restrictedFailed.length) {
    const body = el("div");
    if (restrictedApplied.length) {
      body.appendChild(el("div", "step-report__muted",
        tf("stepReport.restricted.applied",
          `Restricted edits applied: ${restrictedApplied.length}`,
          { n: restrictedApplied.length })));
    }
    if (restrictedFailed.length) {
      body.appendChild(el("div", "step-report__warn",
        tf("stepReport.restricted.failed",
          `Restricted edits failed: ${restrictedFailed.length}`,
          { n: restrictedFailed.length })));
      body.appendChild(reportList(restrictedFailed, (e) => {
        if (e && typeof e === "object") {
          return document.createTextNode(String(
            e.file || e.file_path || e.path || safeStringify(e)));
        }
        return document.createTextNode(String(e));
      }));
    }
    frag.appendChild(reportSection(tf("stepReport.section.restrictedEdits", "Restricted Edits"), body));
  }
  return frag;
}

// -- test (parity with step_renderers.py:_render_test) ----------------------

function renderTestReport(step, outputs) {
  const results = outputs.test_results;
  if (!results || typeof results !== "object") {
    return renderDefaultReport(step, outputs);
  }
  const frag = document.createDocumentFragment();
  const overall = results.overall_passed != null
    ? results.overall_passed : results.passed;

  const bar = el("div", "step-report__status-bar");
  bar.appendChild(el("span", "step-report__label " + (overall ? "ok" : "fail"),
    overall ? tf("stepReport.status.passed", "PASSED")
            : tf("stepReport.status.failed", "FAILED")));
  const phases = Array.isArray(results.phases) ? results.phases : [];
  if (phases.length) {
    const passed = phases.filter((p) => p && p.passed).length;
    bar.append(el("span", "step-report__sep", "│"),
      el("span", null, tf("stepReport.test.phaseCount",
        `${passed} / ${phases.length} phases`,
        { passed, total: phases.length })));
  }
  frag.appendChild(bar);

  if (phases.length) {
    frag.appendChild(reportSection(tf("stepReport.section.phases", "Phases"), reportList(phases, (p) => {
      const ok = !!(p && p.passed);
      const row = el("span");
      row.appendChild(el("span", "step-report__icon " + (ok ? "ok" : "fail"),
        ok ? "✓" : "✗"));
      row.appendChild(document.createTextNode(" " + (p && p.name || "?")));
      return row;
    })));
  }
  if (results.command) {
    frag.appendChild(reportSection(tf("stepReport.section.command", "Command"), String(results.command)));
  }
  return frag;
}

// -- self_check (parity with step_renderers.py:_render_self_check) ----------

function renderSelfCheckReport(step, outputs) {
  const frag = document.createDocumentFragment();
  const issues = Array.isArray(outputs.issues) ? outputs.issues : [];
  // `actionable_count` exists only on a real step.outputs — self_check_handler
  // computes it after `_validate_and_filter_issues`. The synthetic outputs that
  // makeStructuredAssistantRenderer builds from an assistant message carry the
  // LLM's raw JSON, which has no such key; falling back to 0 there rendered a
  // green "✓ PASSED" above a list of issues. Derive from issues.length instead,
  // and keep the wording neutral on that path — those issues are unvalidated,
  // so we cannot assert they are actionable.
  const hasCount = outputs.actionable_count != null;
  const actionable = hasCount ? Number(outputs.actionable_count) : issues.length;
  const status = String(step.status || "").toLowerCase();

  const bar = el("div", "step-report__status-bar");
  if (status === "failed") {
    bar.appendChild(el("span", "step-report__label fail",
      "✗ " + tf("stepReport.status.failed", "FAILED")));
  } else if (actionable === 0) {
    bar.appendChild(el("span", "step-report__label ok",
      "✓ " + tf("stepReport.status.passed", "PASSED")));
  } else {
    bar.appendChild(el("span", "step-report__label fail", hasCount
      ? tf("stepReport.selfCheck.actionableIssues",
        `✗ ${actionable} actionable issue(s)`, { n: actionable })
      : tf("stepReport.selfCheck.issues",
        `✗ ${actionable} issue(s)`, { n: actionable })));
  }
  frag.appendChild(bar);

  const result = outputs.self_check_result;
  const summary = result && typeof result === "object" ? result.summary : "";
  if (summary) frag.appendChild(reportSection(tf("stepReport.section.summary", "Summary"), String(summary)));

  if (issues.length) {
    const bySev = { critical: [], high: [], medium: [], low: [] };
    for (const i of issues) {
      if (!i || typeof i !== "object") continue;
      const sev = String(i.severity || "medium").toLowerCase();
      (bySev[sev] || bySev.medium).push(i);
    }
    for (const sev of ["critical", "high", "medium", "low"]) {
      const grp = bySev[sev];
      if (!grp || !grp.length) continue;
      const sevLabel = tf("stepReport.severity." + sev, sev);
      frag.appendChild(reportSection(
        tf("stepReport.groupHeader",
          `${sevLabel} (${grp.length})`, { label: sevLabel, n: grp.length }),
        reportList(grp, (i) => {
          const desc = i.description || i.message || safeStringify(i);
          const loc = i.location || "";
          const row = el("span");
          row.appendChild(document.createTextNode(String(desc)));
          if (loc) row.appendChild(
            el("span", "step-report__muted", " @ " + loc));
          return row;
        })));
    }
  }
  if (outputs.warning) {
    frag.appendChild(el("div", "step-report__warn", "⚠ " + outputs.warning));
  }
  return frag;
}

// -- verify_spec (parity with step_renderers.py:_render_verify_spec) --------

function renderVerifySpecReport(step, outputs) {
  const frag = document.createDocumentFragment();
  const verified = outputs.verified != null
    ? !!outputs.verified
    : (outputs.fix_needed != null ? !outputs.fix_needed : null);

  const bar = el("div", "step-report__status-bar");
  if (verified === true) {
    bar.appendChild(el("span", "step-report__label ok",
      "✓ " + tf("stepReport.status.passed", "PASSED")));
  } else if (verified === false) {
    bar.appendChild(el("span", "step-report__label fail",
      "✗ " + tf("stepReport.status.failed", "FAILED")));
  } else {
    bar.appendChild(el("span", "step-report__label muted",
      tf("stepReport.status.unknown", "?")));
  }
  frag.appendChild(bar);

  const vRes = outputs.verification_result;
  const summary = outputs.summary
    || (vRes && typeof vRes === "object" ? vRes.summary : "");
  if (summary) frag.appendChild(reportSection(tf("stepReport.section.summary", "Summary"), String(summary)));

  const issues = Array.isArray(outputs.issues) ? outputs.issues : [];
  if (issues.length) {
    const byScope = { in_scope: [], out_of_scope: [] };
    for (const i of issues) {
      if (!i || typeof i !== "object") {
        byScope.in_scope.push({ message: String(i), priority: "medium" });
        continue;
      }
      const sc = String(i.scope || "in_scope");
      (byScope[sc] = byScope[sc] || []).push(i);
    }
    const scopes = [
      ["stepReport.scope.inScope", "In-scope", "in_scope"],
      ["stepReport.scope.outOfScope", "Out-of-scope", "out_of_scope"],
    ];
    for (const [labelKey, labelFallback, key] of scopes) {
      const grp = byScope[key];
      if (!grp || !grp.length) continue;
      const label = tf(labelKey, labelFallback);
      frag.appendChild(reportSection(
        tf("stepReport.groupHeader",
          `${label} (${grp.length})`, { label, n: grp.length }),
        reportList(grp, (i) => {
          const msg = i.message || safeStringify(i);
          const prio = String(i.priority || "medium").toLowerCase();
          const row = el("span");
          row.appendChild(el("span", "step-report__prio " + prio,
            "[" + prio + "]"));
          row.appendChild(document.createTextNode(" " + msg));
          if (i.suggestion) {
            row.appendChild(el("div", "step-report__muted",
              "→ " + i.suggestion));
          }
          return row;
        })));
    }
  }

  const recs = (Array.isArray(outputs.recommendations) && outputs.recommendations.length)
    ? outputs.recommendations
    : (vRes && typeof vRes === "object" && Array.isArray(vRes.recommendations)
      ? vRes.recommendations : []);
  if (recs.length) {
    frag.appendChild(reportSection(tf("stepReport.section.recommendations", "Recommendations"),
      reportList(recs, (r) => document.createTextNode(String(r)))));
  }
  return frag;
}

// -- update_spec (parity with step_renderers.py:_render_update_spec) --------

function renderUpdateSpecReport(step, outputs) {
  const frag = document.createDocumentFragment();
  const specs = outputs.updated_specs || outputs.specs_updated || [];
  const caps = outputs.new_capabilities || [];
  if (!Array.isArray(specs)) {
    return renderDefaultReport(step, outputs);
  }
  if (!specs.length && !(Array.isArray(caps) && caps.length)) {
    frag.appendChild(reportEmpty(
      tf("stepReport.empty.noSpecUpdates", "No spec updates needed")));
    return frag;
  }
  if (specs.length) {
    frag.appendChild(reportSection(
      tf("stepReport.count.updatedSpecs",
        `Updated Specs (${specs.length})`, { n: specs.length }),
      reportList(specs, (s) => {
        if (s && typeof s === "object") {
          const name = s.spec_name || s.name || "unknown";
          const desc = s.change_description || s.description || "";
          const row = el("span");
          row.appendChild(el("span", "step-report__icon ok", "✓ "));
          row.appendChild(el("strong", null, String(name)));
          if (desc) row.appendChild(document.createTextNode(": " + desc));
          return row;
        }
        return document.createTextNode("✓ " + String(s));
      })));
  }
  if (Array.isArray(caps) && caps.length) {
    frag.appendChild(reportSection(tf("stepReport.section.newCapabilities", "New Capabilities"),
      reportList(caps, (c) => document.createTextNode(String(c)))));
  }
  return frag;
}

// -- commit (parity with step_renderers.py:_render_commit) ------------------

function renderCommitReport(step, outputs) {
  const frag = document.createDocumentFragment();
  if (!outputs.committed) {
    frag.appendChild(reportEmpty(tf("stepReport.empty.noChangesToCommit", "No changes to commit")));
    return frag;
  }
  const hash = String(outputs.commit_hash || "");
  const shortHash = hash.length > 7 ? hash.slice(0, 7) : (hash || "N/A");
  const bar = el("div", "step-report__status-bar");
  bar.appendChild(el("span", "step-report__label ok", shortHash));
  if (outputs.version_bumped && outputs.version) {
    bar.append(
      el("span", "step-report__sep", "│"),
      el("span", "step-report__label highlight", "v" + outputs.version),
    );
  }
  frag.appendChild(bar);
  if (outputs.commit_message) {
    frag.appendChild(reportSection(tf("stepReport.section.commitMessage", "Commit Message"),
      String(outputs.commit_message)));
  }
  return frag;
}

// -- version_analyze (parity with step_renderers.py:_render_version_analyze)

function renderVersionAnalyzeReport(step, outputs) {
  const frag = document.createDocumentFragment();
  const bar = el("div", "step-report__status-bar");
  bar.append(
    el("span", null, String(outputs.current_version || "N/A")),
    el("span", "step-report__sep", "→"),
    el("span", "step-report__label highlight",
      String(outputs.suggested_version || "N/A")),
  );
  frag.appendChild(bar);
  const bumpType = String(outputs.bump_type || "?");
  const confidence = String(outputs.confidence || "?");
  frag.appendChild(el("div", "step-report__muted",
    tf("stepReport.versionAnalyze.subline",
      `${bumpType} bump  │  confidence: ${confidence}`,
      { bumpType, confidence })));
  if (outputs.reasoning) {
    frag.appendChild(reportSection(tf("stepReport.section.reasoning", "Reasoning"), String(outputs.reasoning)));
  }
  return frag;
}

// -- summarize (parity with step_renderers.py:_render_summarize) ------------

function renderSummarizeReport(step, outputs) {
  const frag = document.createDocumentFragment();
  const summary = outputs.summary || "";
  if (summary) {
    const wrap = el("div", "step-report__markdown");
    wrap.appendChild(renderMarkdown(String(summary)));
    frag.appendChild(wrap);
  } else {
    frag.appendChild(reportEmpty(tf("stepReport.empty.noSummary", "(no summary)")));
  }
  return frag;
}

// -- discovery (parity with CLI default; outputs vary by mode) --------------

function renderDiscoveryReport(step, outputs) {
  const frag = document.createDocumentFragment();
  if (outputs.refined_description) {
    frag.appendChild(reportSection(tf("stepReport.section.refinedDescription", "Refined Description"),
      String(outputs.refined_description)));
  }
  const dState = outputs.discovery_state;
  const mode = (dState && typeof dState === "object" && dState.mode)
    || outputs.mode;
  if (mode) frag.appendChild(reportSection(tf("stepReport.section.mode", "Mode"), String(mode)));
  const round = (dState && typeof dState === "object" && dState.round != null)
    ? dState.round : null;
  if (round != null) {
    frag.appendChild(el("div", "step-report__muted",
      "Round: " + String(round)));
  }
  const conv = outputs.conversation_history;
  if (Array.isArray(conv) && conv.length) {
    frag.appendChild(reportSection(`Conversation (${conv.length} turns)`,
      reportList(conv, (turn) => {
        if (turn && typeof turn === "object") {
          const role = turn.role || "?";
          const text = turn.content || turn.text || safeStringify(turn);
          const row = el("div", "step-report__conv-turn");
          row.appendChild(el("span", "step-report__kv-k", role + ":"));
          row.appendChild(document.createTextNode(" " + text));
          return row;
        }
        return document.createTextNode(String(turn));
      })));
  }
  if (!frag.childNodes.length) {
    return renderDefaultReport(step, outputs);
  }
  return frag;
}

// -- spec_gate (parity with step_renderers.py:_render_spec_gate) ------------
//
// Renders the gate conclusion (PASSED / FAILED, the fallback route to
// update_spec / implement, the no-op skip) and, when the phase-2 re-test ran,
// reuses renderTestReport's summary-only rendering — it MUST NOT dump the raw
// pytest stdout/stderr the way the generic fallback would.
function renderSpecGateReport(step, outputs) {
  const frag = document.createDocumentFragment();
  const gatePassed = !!outputs.gate_passed;
  const gateRoute = String(outputs.gate_route || "");
  const gateSkipped = !!outputs.gate_skipped;

  const bar = el("div", "step-report__status-bar");
  if (gateSkipped) {
    bar.appendChild(el("span", "step-report__label ok",
      "✓ " + tf("stepReport.status.passed", "PASSED")));
    bar.append(el("span", "step-report__sep", "│"),
      el("span", "step-report__muted",
        tf("stepReport.specGate.skipped", "no spec change — gate skipped (no-op)")));
  } else if (gatePassed) {
    bar.appendChild(el("span", "step-report__label ok",
      "✓ " + tf("stepReport.status.passed", "PASSED")));
  } else {
    bar.appendChild(el("span", "step-report__label fail",
      "✗ " + tf("stepReport.status.failed", "FAILED")));
  }
  frag.appendChild(bar);

  if (gateRoute === "update_spec") {
    frag.appendChild(el("div", "step-report__muted",
      tf("stepReport.specGate.routeUpdateSpec",
        "Route: back to update_spec (invalid spec artifact)")));
  } else if (gateRoute === "implement") {
    frag.appendChild(el("div", "step-report__muted",
      tf("stepReport.specGate.routeImplement",
        "Route: to implement (spec edit broke a test)")));
  }

  if (!gatePassed && outputs.fix_instructions) {
    frag.appendChild(reportSection(tf("stepReport.section.fixInstructions", "Fix Instructions"),
      String(outputs.fix_instructions)));
  }

  // Phase-2 re-test summary — reuse the test report's summary-only renderer so
  // the raw stdout/stderr never leaks into the DOM.
  const results = outputs.test_results;
  if (results && typeof results === "object") {
    frag.appendChild(reportSection(tf("stepReport.section.reTest", "Re-test"),
      renderTestReport(step, { test_results: results })));
  }
  return frag;
}

// -- charter_freshness ------------------------------------------------------
//
// The step sits after invariant_check and may auto-write se3/charter.md itself
// (a descriptive update reflecting the already-reviewed change) via its
// propose->gate->apply closed loop, or degrade to an advisory suggestion. The
// card renders three shapes: auto-updated (note + the old→new unified diff in a
// monospace block), update advised but not applied (suggested_update + the
// degraded reason), and fresh / untouched (a one-line pass note).
function renderCharterFreshnessReport(step, outputs) {
  const frag = document.createDocumentFragment();
  const updateNeeded = !!outputs.charter_update_needed;
  const autoUpdated = !!outputs.charter_auto_updated;

  const bar = el("div", "step-report__status-bar");
  if (autoUpdated) {
    bar.appendChild(el("span", "step-report__label ok",
      "✓ " + tf("stepReport.charter.autoUpdated", "charter auto-updated")));
  } else if (updateNeeded) {
    bar.appendChild(el("span", "step-report__label warn",
      tf("stepReport.charter.updateAdvised", "charter update advised")));
  } else {
    bar.appendChild(el("span", "step-report__label ok",
      "✓ " + tf("stepReport.charter.fresh", "charter fresh")));
  }
  const touched = outputs.touched_classes;
  if (Array.isArray(touched) && touched.length) {
    const list = touched.join(", ");
    bar.append(el("span", "step-report__sep", "│"),
      el("span", "step-report__muted",
        tf("stepReport.charter.touched", "touched: " + list, { list })));
  }
  frag.appendChild(bar);

  if (autoUpdated) {
    frag.appendChild(el("div", "step-report__muted",
      tf("stepReport.charter.autoUpdatedNote",
        "se3/charter.md was updated to describe the already-reviewed change.")));
    const diff = outputs.charter_diff;
    if (diff) {
      const pre = el("pre", "step-report__diff");
      pre.textContent = String(diff);
      frag.appendChild(reportSection(tf("stepReport.section.charterDiff", "Charter diff (old → new)"), pre));
    }
  } else if (updateNeeded) {
    if (outputs.suggested_update) {
      frag.appendChild(reportSection(tf("stepReport.section.suggestedUpdate", "Suggested update"),
        String(outputs.suggested_update)));
    }
    if (outputs.degraded_reason) {
      frag.appendChild(el("div", "step-report__muted",
        tf("stepReport.section.notAutoApplied",
          "Not auto-applied: " + String(outputs.degraded_reason),
          { reason: String(outputs.degraded_reason) })));
    }
  }

  if (outputs.reason) {
    frag.appendChild(el("div", "step-report__muted", String(outputs.reason)));
  }

  // Human-amendment monitoring light: when the diff itself edited the charter.
  if (outputs.admission_warning) {
    frag.appendChild(reportSection(tf("stepReport.section.charterAdmission", "Charter admission"),
      String(outputs.admission_warning)));
  }
  return frag;
}

const STEP_REPORT_RENDERERS = {
  analyze: renderAnalyzeReport,
  plan: renderPlanReport,
  implement: renderImplementReport,
  test: renderTestReport,
  self_check: renderSelfCheckReport,
  verify_spec: renderVerifySpecReport,
  update_spec: renderUpdateSpecReport,
  commit: renderCommitReport,
  version_analyze: renderVersionAnalyzeReport,
  summarize: renderSummarizeReport,
  discovery: renderDiscoveryReport,
  spec_gate: renderSpecGateReport,
  charter_freshness: renderCharterFreshnessReport,
};

// Build the role / attempt / timestamp header line for one record.
function renderRecordHead(norm) {
  const head = el("div", "history-record-head");
  head.appendChild(el("span", "record-role", norm.role));
  const right = el("div", "record-head-right");
  if (norm.attempt != null && norm.attempt !== "" && Number(norm.attempt) > 1) {
    right.appendChild(el("span", "record-attempt",
      tf("record.attempt", "attempt " + norm.attempt, { n: norm.attempt })));
  }
  if (norm.timestamp != null) {
    right.appendChild(el("span", "record-time", formatTime(norm.timestamp)));
  }
  head.appendChild(right);
  return head;
}

// #history-detail is a flex column with no overflow — it never scrolls.
// Its real scroll container is the enclosing .history-detail-pane, which
// declares overflow-y:auto. Scroll helpers must target that element so
// appends to an active session follow into view and isNearBottom() can
// detect whether the reader scrolled away from the bottom.
function historyScrollContainer() {
  const detail = $("history-detail");
  return (detail && detail.closest(".history-detail-pane")) || detail;
}

function scrollHistoryToBottom() {
  const pane = historyScrollContainer();
  if (pane) pane.scrollTop = pane.scrollHeight;
}

// ---------------------------------------------------------------------------
// New task form
// ---------------------------------------------------------------------------

// Sentinel option value for the "Other path…" entry appended to the Project
// dropdown. Selecting it reveals a free-form text input for an absolute
// project path, so users can target directories the daemon has not yet
// registered (e.g. a brand-new empty directory the daemon will auto-init).
const PROJECT_MANUAL_SENTINEL = "__manual__";

function openNewTask() {
  const select = $("nt-machine");
  select.innerHTML = "";
  const online = state.machines.filter((m) => m.online);
  if (!online.length) {
    select.appendChild(new Option(tf("issueModal.noMachines", "(no machines connected)"), ""));
  } else {
    for (const m of online) {
      select.appendChild(new Option(m.hostname || m.machine_id, m.machine_id));
    }
  }
  $("nt-task").value = "";
  $("nt-discover").checked = false;
  $("nt-worktree").checked = false;
  // Reset the plan-mode controls to "project default" so a previously chosen
  // explicit doctrine/granularity never silently leaks into the next task.
  const ntDecomposition = $("nt-decomposition");
  if (ntDecomposition) ntDecomposition.value = "";
  const ntGranularity = $("nt-granularity");
  if (ntGranularity) ntGranularity.value = "";
  $("nt-error").classList.add("hidden");
  $("nt-submit").disabled = false;
  const manualInput = $("nt-project-manual");
  if (manualInput) manualInput.value = "";
  // The strip mirrors the task text, which was just blanked — leaving last
  // session's rows would point at paths no longer in the box.
  clearAttachments("nt-attachments");
  $("new-task-modal").classList.remove("hidden");
  refreshProjectOptions();
}

// Look up the project_roots reported by a machine snapshot (the daemon
// includes them in STATUS_UPDATE; older daemons emit an empty list).
function machineProjectRoots(machineId) {
  if (!machineId) return [];
  const m = state.machines.find((x) => x.machine_id === machineId);
  const roots = m && Array.isArray(m.project_roots) ? m.project_roots : [];
  return roots.filter((r) => typeof r === "string" && r);
}

// Rebuild the Project select to reflect the currently chosen machine.
// Wired to the machine select's change event and called once when the modal
// opens. Disables the submit button when there is nothing the user could pick.
function refreshProjectOptions() {
  const machineId = $("nt-machine").value.trim();
  populateProjectSelect($("nt-project"), machineProjectRoots(machineId), {
    emptyHint: $("nt-project-empty"),
    submit: $("nt-submit"),
  });
  updateManualPathVisibility();
}

// Re-point of the New Task target. The paths already sitting in #nt-task are
// relative to the project they were uploaded into, and nothing rewrites the
// task text at submit time — so once the form names a different machine or
// project, those paths would travel into a flow that runs somewhere the files
// were never written, and the agent's only symptom is a missing file. Both
// halves therefore go: the text is un-planted and the strip retired, and the
// user is told once so the disappearance is not a mystery.
//
// Called after the two selects have settled (a machine change repopulates the
// project list first), and driven off the recorded target rather than the raw
// change event: repopulating can leave the same machine+project selected, and
// discarding a still-valid attachment would be its own small betrayal.
function syncNewTaskUploadTarget() {
  const stripId = "nt-attachments";
  const machineSel = $("nt-machine");
  const projectSel = $("nt-project");
  const key = uploadTargetKey(
    machineSel ? String(machineSel.value || "").trim() : "",
    projectSel ? String(projectSel.value || "").trim() : "",
  );
  const targets = state.uploadTargets && typeof state.uploadTargets === "object" ? state.uploadTargets : {};
  const previous = targets[stripId];
  if (typeof previous !== "string" || previous === key) return 0;
  const discarded = discardAttachments(stripId);
  if (discarded) {
    showToast(
      "info",
      tf(
        "upload.targetChanged",
        "Attachments were removed because the task now targets a different project.",
      ),
    );
  }
  return discarded;
}

// Pure DOM helper: rebuild `select` from a project_roots list. Empties the
// select, hides/shows the optional empty-hint, and toggles `submit.disabled`
// when there is nothing to publish against.
//
// Every populated list also gets an "Other path…" sentinel entry appended
// so a user can always target a directory the daemon has not yet
// registered — the daemon will run `se3 init` there before spawning.
//
// * 0 known roots → still shows the manual sentinel; submit is re-enabled
//   so the user can supply an absolute path by hand.
// * 1 known root  → auto-selects it; manual sentinel still available.
// * 2+ known roots → renders each as an option with a leading "(select…)"
//   placeholder so `required` forces an explicit choice from the user.
function populateProjectSelect(select, roots, opts) {
  opts = opts || {};
  const hint = opts.emptyHint || null;
  const submit = opts.submit || null;
  select.innerHTML = "";
  const list = Array.isArray(roots) ? roots.filter((r) => r) : [];
  const manualOption = new Option(tf("newTask.otherPath", "Other path…"), PROJECT_MANUAL_SENTINEL);

  if (!list.length) {
    // No known roots — show the empty hint, but keep the manual entry
    // available so users can still publish by typing an absolute path.
    select.disabled = false;
    if (hint) hint.classList.remove("hidden");
    select.appendChild(manualOption);
    select.value = PROJECT_MANUAL_SENTINEL;
    if (submit) submit.disabled = false;
    return PROJECT_MANUAL_SENTINEL;
  }

  select.disabled = false;
  if (hint) hint.classList.add("hidden");
  if (submit) submit.disabled = false;
  if (list.length === 1) {
    select.appendChild(new Option(list[0], list[0]));
    select.appendChild(manualOption);
    select.value = list[0];
    return list[0];
  }
  const placeholder = new Option(tf("issueModal.selectProject", "(select a project…)"), "");
  placeholder.disabled = true;
  placeholder.selected = true;
  select.appendChild(placeholder);
  for (const root of list) {
    select.appendChild(new Option(root, root));
  }
  select.appendChild(manualOption);
  return null;
}

// Show or hide the manual absolute-path input based on the Project select's
// current value. Visible only when the user chose the "Other path…" sentinel.
function updateManualPathVisibility() {
  const projectSelect = $("nt-project");
  const manualInput = $("nt-project-manual");
  const manualHint = $("nt-project-manual-hint");
  if (!projectSelect || !manualInput) return;
  const manual = projectSelect.value === PROJECT_MANUAL_SENTINEL;
  manualInput.classList.toggle("hidden", !manual);
  if (manualHint) manualHint.classList.toggle("hidden", !manual);
  if (manual) {
    manualInput.focus();
  }
}

// Validate a user-supplied project_root string for the New Task form. Mirrors
// the server's checks: non-empty and an absolute Unix-style path. Windows
// drive-letter paths are intentionally not supported here.
function isValidAbsolutePath(value) {
  if (typeof value !== "string") return false;
  const trimmed = value.trim();
  return trimmed.length > 0 && trimmed.startsWith("/");
}

function closeNewTask() {
  $("new-task-modal").classList.add("hidden");
}

async function submitNewTask(event) {
  event.preventDefault();
  const errBox = $("nt-error");
  errBox.classList.add("hidden");

  const machineId = $("nt-machine").value.trim();
  const task = $("nt-task").value.trim();
  const taskType = $("nt-type").value;
  const discover = $("nt-discover").checked;
  const worktree = $("nt-worktree").checked;
  const planMode = readPlanModeInputs("nt-decomposition", "nt-granularity");
  const projectSelectValue = $("nt-project").value.trim();

  if (!machineId) return showFormError(errBox, tf("newTask.errSelectMachine", "Select a target machine."));
  if (!task) return showFormError(errBox, tf("newTask.errTaskEmpty", "Task description must not be empty."));
  // Same rule as the reply box: a task text still holding a placeholder token
  // would reach the agent as prose, and closing the modal on submit destroys
  // the input the arriving path must be written into.
  const pendingUploads = pendingUploadRefusal("nt-attachments");
  if (pendingUploads) {
    showToast("error", pendingUploads);
    return showFormError(errBox, pendingUploads);
  }
  if (!projectSelectValue) {
    return showFormError(errBox, tf("newTask.errSelectProject", "Select a project root for this task."));
  }
  let projectRoot;
  if (projectSelectValue === PROJECT_MANUAL_SENTINEL) {
    const manualInput = $("nt-project-manual");
    projectRoot = (manualInput && manualInput.value.trim()) || "";
    if (!projectRoot) {
      return showFormError(errBox, tf("newTask.errEnterPath", "Enter an absolute path for the project."));
    }
    if (!isValidAbsolutePath(projectRoot)) {
      return showFormError(
        errBox,
        tf("newTask.errPathAbsolute", "Project path must be absolute (start with '/')."),
      );
    }
  } else {
    projectRoot = projectSelectValue;
  }

  const submit = $("nt-submit");
  submit.disabled = true;
  try {
    const resp = await authedFetch("/api/flows", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(
        buildNewFlowBody({
          machineId,
          task,
          taskType,
          discover,
          worktree,
          projectRoot,
          planMode,
        }),
      ),
    });
    if (resp.status === 202) {
      closeNewTask();
      // The paths went out inside `task` itself; the rows have nothing left to
      // mirror. Only the strip is cleared — the uploaded files stay on the
      // project machine, which is exactly where the flow is about to read them.
      clearAttachments("nt-attachments");
      showToast("success", tf("toast.taskPublished", "Task published."));
    } else {
      const detail = await resp.json().catch(() => ({}));
      const message = flowLaunchErrorMessage(resp.status, detail);
      showFormError(errBox, message);
      showToast("error", tf("toast.taskPublishFailed", `Could not publish task: ${message}`, { message }));
      submit.disabled = false;
    }
  } catch (err) {
    showFormError(errBox, tf("error.networkReach", "Network error — could not reach the server."));
    showToast("error", tf("toast.taskPublishNetworkError", "Could not publish task — network error."));
    submit.disabled = false;
  }
}

function showFormError(node, message) {
  node.textContent = message;
  node.classList.remove("hidden");
}

// ---------------------------------------------------------------------------
// Topbar overflow menu (mobile-portrait)
// ---------------------------------------------------------------------------
//
// On a narrow screen the auth/admin top-bar actions are tucked behind a
// hamburger toggle; on desktop the #nav-menu wrapper is `display: contents`
// and the toggle is hidden, so the buttons stay inline and these class flips
// are inert (no matching styles) — the desktop top bar is unchanged.
//
// navMenuNextState is the DOM-free state-transition helper (exported for the
// pure tests): given the menu's current open flag it returns the next one.

function navMenuNextState(open) {
  return !open;
}

function isNavMenuOpen() {
  const menu = $("nav-menu");
  return Boolean(menu && menu.classList.contains("open"));
}

function setNavMenuOpen(open) {
  const menu = $("nav-menu");
  const toggle = $("nav-menu-toggle");
  if (menu) menu.classList.toggle("open", open);
  if (toggle) toggle.setAttribute("aria-expanded", open ? "true" : "false");
}

function toggleNavMenu() {
  setNavMenuOpen(navMenuNextState(isNavMenuOpen()));
}

function closeNavMenu() {
  setNavMenuOpen(false);
}

// ---------------------------------------------------------------------------
// Auth flow — login gate, session bootstrap, logout
// ---------------------------------------------------------------------------

// Reflect the current auth state into the DOM: show the login gate or the app
// surface, toggle the auth-only top-bar controls, and render the owner label.
// Driven entirely by `state.authState` / `state.identity` so it stays a pure
// projection of the state machine.
function applyAuthState() {
  const authed = state.authState === AUTH_STATES.AUTHED;
  const loginView = $("login-view");
  const mainLayout = $("main-layout");
  if (loginView) loginView.classList.toggle("hidden", authed);
  if (mainLayout) mainLayout.classList.toggle("hidden", !authed);
  for (const node of document.querySelectorAll(".auth-only")) {
    node.classList.toggle("hidden", !authed);
  }
  // Admin-only controls (e.g. the user-management entry) are a *further*
  // narrowing on top of auth-only: shown only when the resolved owner is an
  // admin. A non-admin owner — even fully authenticated — never sees them.
  // This is UX gating only; every /api/users route re-checks admin server-side.
  const isAdmin = authed && Boolean(state.identity && state.identity.is_admin);
  for (const node of document.querySelectorAll(".admin-only")) {
    node.classList.toggle("hidden", !isAdmin);
  }
  const label = $("owner-label");
  if (label) label.textContent = authed ? ownerLabel(state.identity) : "";
}

// Resolve the existing session (if any) on page load. A 200 means an owner is
// already signed in (cookie still valid) → straight to the app; a 401 means no
// session → show the login gate. Network failures degrade to the login gate.
async function bootstrapAuth() {
  let resp;
  try {
    resp = await fetch("/api/auth/me");
  } catch (_) {
    state.authState = nextAuthState(state.authState, "me_401");
    applyAuthState();
    return;
  }
  if (resp.ok) {
    const identity = await resp.json().catch(() => null);
    onAuthenticated(identity, "me_ok");
  } else {
    state.identity = null;
    state.authState = nextAuthState(state.authState, "me_401");
    applyAuthState();
  }
}

// Shared post-authentication entry point (used by bootstrap / login /
// break-glass): record the owner, advance the state machine, paint the app,
// and open the realtime socket.
function onAuthenticated(identity, event) {
  state.identity = identity || null;
  state.authState = nextAuthState(state.authState, event);
  applyAuthState();
  connect();
}

async function handleLogin(event) {
  event.preventDefault();
  const errBox = $("login-error");
  errBox.classList.add("hidden");
  const username = $("login-username").value.trim();
  const password = $("login-password").value;
  if (!username) return showFormError(errBox, tf("login.errUsername", "Enter your username."));
  if (!password) return showFormError(errBox, tf("login.errPassword", "Enter your password."));

  const submit = $("login-submit");
  submit.disabled = true;
  try {
    const resp = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    if (resp.ok) {
      const identity = await resp.json().catch(() => null);
      $("login-password").value = "";
      onAuthenticated(identity, "login_ok");
    } else if (resp.status === 429) {
      showFormError(errBox, tf("login.errTooMany", "Too many attempts — wait a moment and retry."));
    } else if (resp.status === 503) {
      showFormError(errBox, tf("login.errNotEnabled", "Password login is not enabled on this server."));
    } else {
      showFormError(errBox, tf("login.errInvalid", "Invalid username or password."));
    }
  } catch (_) {
    showFormError(errBox, tf("error.networkReach", "Network error — could not reach the server."));
  } finally {
    submit.disabled = false;
  }
}

async function handleBreakglass(event) {
  event.preventDefault();
  const errBox = $("breakglass-error");
  errBox.classList.add("hidden");
  const token = $("breakglass-token").value.trim();
  if (!token) return showFormError(errBox, tf("login.errPasteToken", "Paste the break-glass token."));

  const submit = $("breakglass-submit");
  submit.disabled = true;
  try {
    const resp = await fetch("/api/auth/breakglass", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    });
    if (resp.ok) {
      const identity = await resp.json().catch(() => null);
      $("breakglass-token").value = "";
      onAuthenticated(identity, "breakglass_ok");
    } else {
      showFormError(errBox, tf("login.errInvalidToken", "Invalid or expired break-glass token."));
    }
  } catch (_) {
    showFormError(errBox, tf("error.networkReach", "Network error — could not reach the server."));
  } finally {
    submit.disabled = false;
  }
}

function toggleBreakglass() {
  const form = $("breakglass-form");
  if (form) form.classList.toggle("hidden");
}

async function handleLogout() {
  try {
    await fetch("/api/auth/logout", { method: "POST" });
  } catch (_) {
    /* logout is best-effort — clear client state regardless */
  }
  state.identity = null;
  state.machines = [];
  state.selectedMachineId = null;
  state.issues = [];
  state.selectedIssueId = null;
  state.allIssueProjectRoots = [];
  state.issuesProjectFilter = "";
  // Reset the mobile panel switch so a fresh login lands on the machine list.
  applyListPanelAction("reset");
  state.authState = nextAuthState(state.authState, "logout");
  teardownWs();
  // Wipe any rendered owner data so a different owner signing in next sees a
  // clean surface rather than the previous owner's machines.
  applyMachines([]);
  applyAuthState();
}

// ---------------------------------------------------------------------------
// Daemon-key management panel
// ---------------------------------------------------------------------------

function openKeys() {
  $("keys-error").classList.add("hidden");
  $("keys-reveal").classList.add("hidden");
  $("keys-label").value = "";
  $("keys-modal").classList.remove("hidden");
  loadDaemonKeys();
}

function closeKeys() {
  $("keys-modal").classList.add("hidden");
}

async function loadDaemonKeys() {
  const listBox = $("keys-list");
  listBox.innerHTML = "";
  listBox.appendChild(el("p", "empty", tf("keys.loading", "Loading keys…")));
  try {
    const resp = await authedFetch("/api/daemon-keys");
    if (!resp.ok) {
      state.daemonKeys = [];
      renderDaemonKeys();
      return;
    }
    const data = await resp.json().catch(() => ({ keys: [] }));
    state.daemonKeys = Array.isArray(data.keys) ? data.keys : [];
  } catch (_) {
    state.daemonKeys = [];
  }
  renderDaemonKeys();
}

function renderDaemonKeys() {
  const listBox = $("keys-list");
  listBox.innerHTML = "";
  if (!state.daemonKeys.length) {
    listBox.appendChild(el("p", "empty", tf("keys.empty", "No daemon keys yet.")));
    return;
  }
  for (const key of state.daemonKeys) {
    const model = daemonKeyRowModel(key);
    const row = el("div", "keys-row" + (model.revoked ? " revoked" : ""));
    row.append(
      el("span", "keys-row-label",
        model.unlabeled ? tf("keys.unlabeled", "(unlabeled)") : model.label),
      el("span", "keys-row-status keys-status-" + model.statusClass,
        tf(model.statusClass === "revoked" ? "keys.statusRevoked" : "keys.statusActive", model.statusLabel)),
    );
    if (!model.revoked && model.keyId) {
      const btn = el("button", "ghost-btn keys-revoke-btn", tf("keys.revoke", "Revoke"));
      btn.type = "button";
      btn.addEventListener("click", () => revokeDaemonKey(model.keyId));
      row.appendChild(btn);
    }
    listBox.appendChild(row);
  }
}

async function createDaemonKey(event) {
  event.preventDefault();
  const errBox = $("keys-error");
  errBox.classList.add("hidden");
  const submit = $("keys-create-submit");
  submit.disabled = true;
  try {
    const resp = await authedFetch("/api/daemon-keys", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label: $("keys-label").value.trim() }),
    });
    if (resp.status === 201) {
      const data = await resp.json().catch(() => ({}));
      // The plaintext is shown exactly once — surface it prominently, then
      // refresh the (metadata-only) list.
      $("keys-reveal-value").textContent = data.key || "";
      $("keys-reveal").classList.remove("hidden");
      $("keys-label").value = "";
      showToast("success", tf("toast.keyCreated", "Daemon key created — copy it now."));
      loadDaemonKeys();
    } else {
      const detail = await resp.json().catch(() => ({}));
      showFormError(errBox, detail.detail || tf("error.serverReturned", `Server returned ${resp.status}.`, { status: resp.status }));
    }
  } catch (_) {
    showFormError(errBox, tf("keys.errCreateNetwork", "Network error — could not create the key."));
  } finally {
    submit.disabled = false;
  }
}

async function revokeDaemonKey(keyId) {
  if (!keyId) return;
  const errBox = $("keys-error");
  errBox.classList.add("hidden");
  try {
    const resp = await authedFetch(
      `/api/daemon-keys/${encodeURIComponent(keyId)}`,
      { method: "DELETE" },
    );
    if (resp.ok) {
      showToast("success", tf("toast.keyRevoked", "Daemon key revoked."));
      loadDaemonKeys();
    } else {
      const detail = await resp.json().catch(() => ({}));
      showFormError(errBox, detail.detail || tf("error.serverReturned", `Server returned ${resp.status}.`, { status: resp.status }));
    }
  } catch (_) {
    showFormError(errBox, tf("keys.errRevokeNetwork", "Network error — could not revoke the key."));
  }
}

// ---------------------------------------------------------------------------
// Registered-project management panel (per machine)
// ---------------------------------------------------------------------------
//
// Reads and writes take deliberately different routes. The LIST is served from
// the server's STATUS_UPDATE mirror, so opening the dialog costs no daemon
// round-trip and works while the daemon is offline (showing the last known
// registry). The WRITES go down to the daemon as commands and are answered with
// a stable error_code, which projectErrorKey() turns into a localized message.
// Freshness after a write comes from the fast push the daemon fires once the
// registry file is rewritten — applyMachines repaints an open dialog from it.

function projectsUrl(machineId, query) {
  const base = "/api/machines/" + encodeURIComponent(machineId) + "/projects";
  return query ? base + "?" + query : base;
}

function openProjects(machineId) {
  if (!machineId) return;
  state.projectMachineId = machineId;
  state.projectEntries = [];
  state.projectRemoveTarget = null;
  $("project-error").classList.add("hidden");
  $("project-add-path").value = "";
  $("project-modal").classList.remove("hidden");
  loadProjects();
}

function closeProjects() {
  $("project-modal").classList.add("hidden");
  state.projectMachineId = null;
  state.projectEntries = [];
}

async function loadProjects() {
  const machineId = state.projectMachineId;
  const listBox = $("project-list");
  listBox.innerHTML = "";
  listBox.appendChild(el("p", "empty", tf("projects.loading", "Loading projects…")));
  if (!machineId) {
    state.projectEntries = [];
    renderProjects();
    return;
  }
  try {
    const resp = await authedFetch(projectsUrl(machineId));
    if (!resp.ok) {
      state.projectEntries = [];
      showFormError($("project-error"),
        tf("projects.errLoad", "Could not load the project list."));
      renderProjects();
      return;
    }
    const data = await resp.json().catch(() => ({ projects: [] }));
    state.projectEntries = Array.isArray(data.projects) ? data.projects : [];
  } catch (_) {
    state.projectEntries = [];
    showFormError($("project-error"),
      tf("projects.errLoad", "Could not load the project list."));
  }
  renderProjects();
}

function renderProjects() {
  const listBox = $("project-list");
  listBox.innerHTML = "";
  if (!state.projectEntries.length) {
    listBox.appendChild(el("p", "empty", tf("projects.empty", "No registered projects.")));
    return;
  }
  for (const entry of state.projectEntries) {
    const model = projectRegistryRowModel(entry);
    if (!model.path) continue;
    const row = el("div", "project-row" + (model.stale ? " stale" : ""));
    const main = el("div", "project-row-main");
    main.append(
      el("span", "project-row-name", model.name || model.path),
      el("span", "project-row-path", model.path),
    );
    row.appendChild(main);
    if (model.active) {
      row.appendChild(el("span", "project-row-badge project-badge-active",
        tf("projects.active", "active")));
    }
    if (model.stale) {
      row.appendChild(el("span", "project-row-badge project-badge-stale",
        tf("projects.stale", "missing")));
    }
    if (model.canRemove) {
      const btn = el("button", "ghost-btn project-remove-btn", tf("projects.remove", "Remove"));
      btn.type = "button";
      // First click only opens the confirmation — no request leaves here.
      btn.addEventListener("click", () => confirmRemoveProject(model.path));
      row.appendChild(btn);
    }
    listBox.appendChild(row);
  }
}

// Repaint an open dialog from the freshly-arrived STATUS_UPDATE snapshot. This
// is what makes an add/remove (and any daemon-side registration) show up
// without a reload: the daemon fast-pushes right after every registry write.
function syncProjectsFromSnapshot() {
  const machineId = state.projectMachineId;
  if (!machineId) return;
  const machine = state.machines.find((m) => m.machine_id === machineId);
  const projects = machine && Array.isArray(machine.registered_projects)
    ? machine.registered_projects
    : [];
  state.projectEntries = projects;
  renderProjects();
}

// Turn a failed project-command response into localized copy. The server keeps
// `error_code` at the top level of the body precisely so the UI never has to
// display the daemon's untranslated English; that prose is used only as the
// per-key fallback (and for a response carrying no code at all).
async function projectFailureMessage(resp) {
  const detail = await resp.json().catch(() => ({}));
  const prose = (detail && typeof detail.detail === "string" && detail.detail) || "";
  const code = (detail && typeof detail.error_code === "string" && detail.error_code) || "";
  if (code) {
    return tf(projectErrorKey(code), prose || code);
  }
  return prose || tf("error.serverReturned", `Server returned ${resp.status}.`, { status: resp.status });
}

async function addProject(event) {
  if (event && typeof event.preventDefault === "function") event.preventDefault();
  const errBox = $("project-error");
  errBox.classList.add("hidden");
  const machineId = state.projectMachineId;
  if (!machineId) return;
  const built = buildAddProjectBody($("project-add-path").value);
  if (!built.ok) {
    showFormError(errBox, built.reason === "not_absolute"
      ? tf("projects.errNotAbsolute", "Enter an absolute path.")
      : tf("projects.errEmptyPath", "Enter a project path."));
    return;
  }
  const submit = $("project-add-submit");
  submit.disabled = true;
  try {
    const resp = await authedFetch(projectsUrl(machineId), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(built.body),
    });
    if (resp.status === 201) {
      $("project-add-path").value = "";
      showToast("success", tf("toast.projectAdded", "Project registered."));
      // Paint the daemon's echoed normalized root locally; re-fetching here
      // would read the not-yet-refreshed mirror (see applyProjectAdded).
      const body = await resp.json().catch(() => ({}));
      const stored = (body && typeof body.project_root === "string" && body.project_root)
        || built.projectRoot;
      state.projectEntries = applyProjectAdded(state.projectEntries, stored);
      renderProjects();
    } else {
      showFormError(errBox, await projectFailureMessage(resp));
    }
  } catch (_) {
    showFormError(errBox,
      tf("projects.errAddNetwork", "Network error — could not register the project."));
  } finally {
    submit.disabled = false;
  }
}

function confirmRemoveProject(projectRoot) {
  if (!projectRoot) return;
  state.projectRemoveTarget = projectRoot;
  $("project-remove-error").classList.add("hidden");
  $("project-remove-path").textContent = projectRoot;
  $("project-remove-modal").classList.remove("hidden");
}

function closeRemoveProject() {
  $("project-remove-modal").classList.add("hidden");
  state.projectRemoveTarget = null;
}

async function removeProject() {
  const machineId = state.projectMachineId;
  const projectRoot = state.projectRemoveTarget;
  const errBox = $("project-remove-error");
  errBox.classList.add("hidden");
  if (!machineId || !projectRoot) return;
  const confirmBtn = $("project-remove-confirm");
  confirmBtn.disabled = true;
  try {
    const resp = await authedFetch(
      projectsUrl(machineId, "project_root=" + encodeURIComponent(projectRoot)),
      { method: "DELETE" },
    );
    if (resp.ok) {
      closeRemoveProject();
      showToast("success", tf("toast.projectRemoved", "Project deregistered."));
      // Drop the row locally instead of re-fetching the still-stale mirror
      // (see applyProjectRemoved). Both the clicked spelling and the daemon's
      // echoed normalized root are dropped — a worktree spelling deregisters
      // its owning main root, so the two can differ.
      const body = await resp.json().catch(() => ({}));
      const stored = (body && typeof body.project_root === "string" && body.project_root) || "";
      let rows = applyProjectRemoved(state.projectEntries, projectRoot);
      if (stored && stored !== projectRoot) rows = applyProjectRemoved(rows, stored);
      state.projectEntries = rows;
      renderProjects();
    } else {
      showFormError(errBox, await projectFailureMessage(resp));
    }
  } catch (_) {
    showFormError(errBox,
      tf("projects.errRemoveNetwork", "Network error — could not remove the project."));
  } finally {
    confirmBtn.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// User-management panel (admin only)
// ---------------------------------------------------------------------------

function openUsers() {
  $("users-error").classList.add("hidden");
  $("users-username").value = "";
  $("users-password").value = "";
  $("users-display-name").value = "";
  $("users-is-admin").checked = false;
  $("users-modal").classList.remove("hidden");
  loadUsers();
}

function closeUsers() {
  $("users-modal").classList.add("hidden");
}

async function loadUsers() {
  const listBox = $("users-list");
  listBox.innerHTML = "";
  listBox.appendChild(el("p", "empty", tf("users.loading", "Loading users…")));
  try {
    const resp = await authedFetch("/api/users");
    if (!resp.ok) {
      state.users = [];
      renderUsers();
      return;
    }
    const data = await resp.json().catch(() => ({ users: [] }));
    state.users = Array.isArray(data.users) ? data.users : [];
  } catch (_) {
    state.users = [];
  }
  renderUsers();
}

function renderUsers() {
  const listBox = $("users-list");
  listBox.innerHTML = "";
  if (!state.users.length) {
    listBox.appendChild(el("p", "empty", tf("users.empty", "No manageable users.")));
    return;
  }
  const currentOwnerId = state.identity && state.identity.owner_id;
  for (const user of state.users) {
    const model = userRowModel(user, currentOwnerId);
    const row = el("div", "users-row" + (model.isSelf ? " is-self" : ""));

    const main = el("div", "users-row-main");
    main.append(
      el("span", "users-row-label", model.label),
      el("span", "users-row-provider", model.provider),
      el("span", "users-row-admin users-admin-" + model.adminClass,
        tf(model.adminClass === "admin" ? "users.roleAdmin" : "users.roleUser", model.adminLabel)),
    );
    if (model.isSelf) main.appendChild(el("span", "users-row-self", tf("users.self", "(You)")));
    row.appendChild(main);

    const actions = el("div", "users-row-actions");
    if (model.canToggleAdmin) {
      const btn = el("button", "ghost-btn users-admin-btn",
        tf(model.toggleAdminTo ? "users.setAdmin" : "users.unsetAdmin", model.toggleAdminLabel));
      btn.type = "button";
      btn.addEventListener("click", () =>
        toggleAdmin(model.ownerId, model.toggleAdminTo));
      actions.appendChild(btn);
    }
    if (model.canResetPassword) {
      const btn = el("button", "ghost-btn users-password-btn", tf("users.resetPassword", "Reset password"));
      btn.type = "button";
      btn.addEventListener("click", () => resetPassword(model.ownerId, model.label));
      actions.appendChild(btn);
    }
    if (model.canDelete) {
      const btn = el("button", "ghost-btn users-delete-btn", tf("users.delete", "Delete"));
      btn.type = "button";
      btn.addEventListener("click", () => deleteUser(model.ownerId, model.label));
      actions.appendChild(btn);
    }
    row.appendChild(actions);
    listBox.appendChild(row);
  }
}

async function createUser(event) {
  event.preventDefault();
  const errBox = $("users-error");
  errBox.classList.add("hidden");
  const username = $("users-username").value.trim();
  const password = $("users-password").value;
  if (!username) return showFormError(errBox, tf("users.errUsername", "Username must not be empty."));
  if (!password) return showFormError(errBox, tf("users.errPassword", "Password must not be empty."));

  const submit = $("users-create-submit");
  submit.disabled = true;
  try {
    const resp = await authedFetch("/api/users", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username,
        password,
        display_name: $("users-display-name").value.trim(),
        is_admin: $("users-is-admin").checked,
      }),
    });
    if (resp.ok) {
      $("users-username").value = "";
      $("users-password").value = "";
      $("users-display-name").value = "";
      $("users-is-admin").checked = false;
      showToast("success", tf("toast.userCreated", "User created."));
      loadUsers();
    } else {
      const detail = await resp.json().catch(() => ({}));
      showFormError(errBox, detail.detail || tf("error.serverReturned", `Server returned ${resp.status}.`, { status: resp.status }));
    }
  } catch (_) {
    showFormError(errBox, tf("users.errCreateNetwork", "Network error — could not create the user."));
  } finally {
    submit.disabled = false;
  }
}

async function deleteUser(ownerId, label) {
  if (!ownerId) return;
  const errBox = $("users-error");
  errBox.classList.add("hidden");
  try {
    const resp = await authedFetch(
      `/api/users/${encodeURIComponent(ownerId)}`,
      { method: "DELETE" },
    );
    if (resp.ok) {
      showToast("success", tf("toast.userDeleted", `Deleted user ${label || ownerId}.`, { name: label || ownerId }));
      loadUsers();
    } else {
      const detail = await resp.json().catch(() => ({}));
      showFormError(errBox, detail.detail || tf("error.serverReturned", `Server returned ${resp.status}.`, { status: resp.status }));
    }
  } catch (_) {
    showFormError(errBox, tf("users.errDeleteNetwork", "Network error — could not delete the user."));
  }
}

async function resetPassword(ownerId, label) {
  if (!ownerId) return;
  const errBox = $("users-error");
  errBox.classList.add("hidden");
  const password = typeof prompt === "function"
    ? prompt(tf("users.resetPrompt", `Set a new password for ${label || ownerId}:`, { name: label || ownerId }))
    : null;
  // A null result means the admin dismissed the prompt; an empty string would
  // be rejected by the backend (422), so guard both here.
  if (password == null) return;
  if (!password) return showFormError(errBox, tf("users.errNewPassword", "New password must not be empty."));
  try {
    const resp = await authedFetch(
      `/api/users/${encodeURIComponent(ownerId)}/password`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
      },
    );
    if (resp.ok) {
      showToast("success", tf("toast.passwordReset", `Reset password for ${label || ownerId}.`, { name: label || ownerId }));
    } else {
      const detail = await resp.json().catch(() => ({}));
      showFormError(errBox, detail.detail || tf("error.serverReturned", `Server returned ${resp.status}.`, { status: resp.status }));
    }
  } catch (_) {
    showFormError(errBox, tf("users.errResetNetwork", "Network error — could not reset the password."));
  }
}

async function toggleAdmin(ownerId, isAdmin) {
  if (!ownerId) return;
  const errBox = $("users-error");
  errBox.classList.add("hidden");
  try {
    const resp = await authedFetch(
      `/api/users/${encodeURIComponent(ownerId)}/admin`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ is_admin: Boolean(isAdmin) }),
      },
    );
    if (resp.ok) {
      showToast("success", isAdmin ? tf("toast.adminGranted", "Set as admin.") : tf("toast.adminRevoked", "Removed admin."));
      loadUsers();
    } else {
      const detail = await resp.json().catch(() => ({}));
      showFormError(errBox, detail.detail || tf("error.serverReturned", `Server returned ${resp.status}.`, { status: resp.status }));
    }
  } catch (_) {
    showFormError(errBox, tf("users.errAdminNetwork", "Network error — could not change admin status."));
  }
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

// Whether a modal shell (keys / users / issue-modal / …) is currently open.
function isModalOpen(id) {
  const m = typeof document !== "undefined" ? $(id) : null;
  return !!(m && m.classList && !m.classList.contains("hidden"));
}

// Repaint the dynamic (JS-rendered) UI after a language switch. The static
// data-i18n nodes are handled by applyStaticTranslations; the localized chrome
// those JS renderers emit (via tf()) only reflects the new language when the
// renderer is re-run, so here we rebuild every currently-visible dynamic
// surface — not just the machine/flow lists. Each renderer resolves its text
// through tf() at call time, so re-invoking it is all that a language switch
// needs. Guarded and best-effort: a missing renderer or a mid-switch throw must
// never break the language change.
function rerenderDynamic() {
  const call = (fn, ...args) => {
    try { if (typeof fn === "function") fn(...args); } catch (_) { /* per-surface */ }
  };
  try {
    // Clearing the diff-aware signatures forces the guarded list renderers to
    // rebuild instead of skipping as "unchanged".
    if (typeof resetRenderSignatures === "function") resetRenderSignatures();
  } catch (_) { /* best-effort */ }
  // Live-state chrome the static pass cannot own: the connection badge (its
  // text is the current WS state, not a fixed string) and the browser tab
  // title (outside the data-i18n DOM scope).
  call(repaintConnStatus);
  call(applyDocumentTitle);
  call(renderMachines);
  call(renderFlows);
  // Open flow-detail view: sidebar (Overview/Steps/Machine), conversation, and
  // reply-box chrome. refreshFlowDetail re-fetches then re-renders all three.
  if (typeof isFlowViewOpen === "function" && isFlowViewOpen() && state.selectedFlowId) {
    call(refreshFlowDetail);
  }
  // Open issues view: filter option labels, list, and the open detail pane.
  if (typeof isIssuesOpen === "function" && isIssuesOpen()) {
    call(refreshIssueTypeFilter);
    call(refreshIssueProjectFilter);
    call(renderIssuesList);
    if (state.selectedIssueId) call(renderIssueDetail, state.selectedIssueId);
  }
  // Open history view: session list, and the open session detail (re-fetch as
  // an incremental refresh so its records re-render in the new language).
  if (typeof isHistoryOpen === "function" && isHistoryOpen()) {
    call(renderHistoryList);
    if (state.selectedHistoryId) {
      call(openHistorySession, state.selectedHistoryId, { incremental: true });
    }
  }
  // Open daemon-key / user-management modals: both render from cached state.
  if (isModalOpen("keys-modal")) call(renderDaemonKeys);
  if (isModalOpen("users-modal")) call(renderUsers);
  // Open registered-project dialog: rows render from cached snapshot entries.
  if (isModalOpen("project-modal")) call(renderProjects);
  // Confirmation/edit modals carry JS-set copy that reflects their current mode
  // (edit-vs-create title, flow-specific end message, action/launch text). The
  // static-translation pass in setLang has just repainted their data-i18n nodes
  // back to the generic defaults, so restore the mode-specific copy here.
  call(repaintOpenModals);
}

// Repaint the copy of any open confirmation/edit modal in its current mode so a
// mid-session language switch keeps its flow/issue-specific text (not the static
// data-i18n default). Each modal stashes what it needs on its own dataset, so
// the copy is recoverable without re-opening it. Best-effort and guarded.
function repaintOpenModals() {
  // Issue create/edit modal: the title + submit label diverge by mode, and the
  // static pass reset the title node to "New Issue" / "Create".
  if (isModalOpen("issue-modal")) {
    const form = $("issue-form");
    const titleNode = $("issue-modal-title");
    const submitNode = $("issue-form-submit");
    const mode = form ? form.dataset.mode : "";
    if (mode === "edit") {
      const id = (form && form.dataset.issueId) || "?";
      if (titleNode) titleNode.textContent =
        tf("issueModal.editTitle", "Edit Issue #" + id, { id });
      if (submitNode) submitNode.textContent = tf("issueModal.save", "Save");
    } else if (titleNode || submitNode) {
      if (titleNode) titleNode.textContent = tf("issueModal.title", "New Issue");
      if (submitNode) submitNode.textContent = tf("issueModal.submit", "Create");
    }
  }

  // Issue close/reopen confirmation: title + message vary by action + issue id.
  // The message node has no data-i18n, so it would otherwise stay in the old
  // language until the modal is reopened.
  if (isModalOpen("issue-action-modal")) {
    const modal = $("issue-action-modal");
    const action = modal ? modal.dataset.action : "";
    const id = (modal && modal.dataset.issueId) || "?";
    const titleNode = $("issue-action-title");
    const msgNode = $("issue-action-message");
    if (action === "close") {
      if (titleNode) titleNode.textContent = tf("issueAction.closeTitle", "Close Issue");
      if (msgNode) msgNode.textContent =
        tf("issueAction.closeMessage", "Confirm closing Issue #" + id + "?", { id });
    } else {
      if (titleNode) titleNode.textContent = tf("issueAction.reopenTitle", "Reopen Issue");
      if (msgNode) msgNode.textContent =
        tf("issueAction.reopenMessage", "Confirm reopening Issue #" + id + "?", { id });
    }
  }

  // Launch-from-issue confirmation: the message embeds the issue title, which is
  // recovered by looking the issue up in cached state via its composite key.
  if (isModalOpen("issue-launch-modal")) {
    const modal = $("issue-launch-modal");
    const key = modal ? modal.dataset.issueKey : "";
    const id = (modal && modal.dataset.issueId) || "?";
    const iss = (state.issues || []).find(
      (i) => i && issueCompositeKey(i) === key);
    const title = iss ? issueDisplayTitle(iss) : "";
    const titleNode = $("issue-launch-title");
    const msgNode = $("issue-launch-message");
    if (titleNode) titleNode.textContent = tf("issueLaunch.title", "Launch Flow from Issue");
    if (msgNode) msgNode.textContent = tf("issueLaunch.message",
      "A new flow will be launched from Issue #" + id + " (" + title + ").",
      { id, title });
  }

  // End-session confirmation: the message embeds the flow id short-hash.
  if (isModalOpen("end-session-modal")) {
    const modal = $("end-session-modal");
    const flowId = (modal && modal.dataset.flowId) || "";
    const msgNode = $("end-session-message");
    if (msgNode && flowId) {
      const shortId = flowId.slice(0, 8);
      msgNode.textContent = tf("endSession.confirmMessage",
        "Confirm ending and archiving this session (" + shortId + "…)? " +
        "A worktree session will be cleaned up and archived, and uncommitted work will not be merged into the main branch.",
        { id: shortId });
    }
  }
}

// Adopt the server's language registry, resolve the initial UI language
// (localStorage > navigator > en-US), wire the top-bar switch control, then load
// the baseline + selected dictionaries and paint the static text. Idempotent-safe:
// called once from init(). The manifest is awaited BEFORE the language is
// resolved so a stored / browser preference for a language shipped after this
// build (a new locale JSON) still wins instead of silently falling back to en-US.
async function initI18n() {
  await I18N.loadManifest();

  let stored = null;
  try {
    if (typeof localStorage !== "undefined") {
      stored = localStorage.getItem(I18N.STORAGE_KEY);
    }
  } catch (_) { /* localStorage may throw in restricted contexts */ }
  const navLang =
    (typeof navigator !== "undefined" && navigator.language) || "";
  I18N.lang = I18N.resolveInitialLang(stored, navLang, I18N.SUPPORTED);
  I18N.onLangChange = rerenderDynamic;

  const sel = $("lang-select");
  if (sel) {
    sel.innerHTML = "";
    for (const code of I18N.SUPPORTED) {
      const opt = document.createElement("option");
      opt.value = code;
      // Endonyms (native language names) — identical across every dictionary,
      // so the label reads correctly whichever language is active. The manifest
      // label is the fallback, so a language with no `lang.<code>` entry in the
      // dictionaries still shows its own name rather than the raw dotted key.
      opt.textContent = tf(`lang.${code}`, I18N.LABELS[code] || code);
      sel.appendChild(opt);
    }
    sel.value = I18N.lang;
    sel.addEventListener("change", (e) => {
      const s = $("lang-select");
      I18N.setLang(e.target.value).then(() => {
        if (s) s.value = I18N.lang;
      });
    });
  }

  // Load baseline + selected dicts, then paint. Fetch failure degrades to the
  // in-markup English (I18N.load never rejects).
  await Promise.all([I18N.load(I18N.FALLBACK), I18N.load(I18N.lang)]);
  if (sel) {
    for (const opt of sel.children) {
      opt.textContent = tf(`lang.${opt.value}`, I18N.LABELS[opt.value] || opt.value);
    }
    sel.value = I18N.lang;
  }
  I18N.applyStaticTranslations();
  // Bootstrapping does not await initI18n: auth, the WebSocket and the first
  // data renders all race the manifest + dictionary fetches, so any dynamic
  // surface painted during that window carries the pre-load default language.
  // Repaint every dynamic surface (this also covers the connection badge and the
  // document title, which the static pass cannot own) once the dicts land, the
  // same way a manual language switch does.
  rerenderDynamic();
}

function init() {
  initI18n();
  $("new-task-btn").addEventListener("click", openNewTask);
  $("new-task-close").addEventListener("click", closeNewTask);
  $("new-task-form").addEventListener("submit", submitNewTask);
  // Both target selects run the attachment re-check after their own handler,
  // because either one moves where the task will run — and the paths already in
  // the box only resolve under the project they were uploaded into.
  $("nt-machine").addEventListener("change", () => {
    refreshProjectOptions();
    syncNewTaskUploadTarget();
  });
  $("nt-project").addEventListener("change", () => {
    updateManualPathVisibility();
    syncNewTaskUploadTarget();
  });

  $("flow-view-close").addEventListener("click", closeFlowView);

  // Narrow-screen flow-view sidebar drawer (G4): the head toggle opens it, the
  // backdrop dismisses it. On desktop both controls are hidden and the
  // `sidebar-open` class has no styles, so these bindings are harmless no-ops.
  const sidebarToggle = $("flow-sidebar-toggle");
  if (sidebarToggle) {
    sidebarToggle.addEventListener("click", toggleFlowSidebar);
  }
  const sidebarBackdrop = $("flow-sidebar-backdrop");
  if (sidebarBackdrop) {
    sidebarBackdrop.addEventListener("click", closeFlowSidebar);
  }

  // Narrow-screen main-list panel switch: return from Flows to the machine
  // list. Inert on desktop (the back button is hidden and both panes render).
  const flowsBack = $("flows-back-btn");
  if (flowsBack) {
    flowsBack.addEventListener("click", () => applyListPanelAction("back"));
  }

  $("history-btn").addEventListener("click", openHistory);
  $("history-close").addEventListener("click", closeHistory);

  // Issues view.
  $("issues-btn").addEventListener("click", openIssues);
  $("issues-close").addEventListener("click", closeIssues);
  $("issues-show-closed").addEventListener("change", (e) => {
    state.issuesShowClosed = e.target.checked;
    fetchIssues();
    fetchAllIssueTypes();
  });
  $("issues-source-filter").addEventListener("change", (e) => {
    state.issuesSourceFilter = e.target.value;
    fetchIssues();
    fetchAllIssueTypes();
  });
  $("issues-type-filter").addEventListener("change", (e) => {
    state.issuesTypeFilter = e.target.value;
    fetchIssues();
  });
  $("issues-project-filter").addEventListener("change", (e) => {
    state.issuesProjectFilter = e.target.value;
    fetchIssues();
  });
  $("issues-create-btn").addEventListener("click", openIssueCreateModal);

  // Narrow-screen issues panel switch: return from detail to list.
  const issuesBack = $("issues-back-btn");
  if (issuesBack) {
    issuesBack.addEventListener("click", () => applyIssuesPanelAction("back"));
  }

  // Issue create/edit modal.
  $("issue-form").addEventListener("submit", submitIssueForm);
  _initIssueFormDirtyTracking();
  const issMachineSel = $("issue-machine");
  if (issMachineSel) {
    issMachineSel.addEventListener("change", _refreshIssueProjectOptions);
  }
  const issProjectSel = $("issue-project");
  if (issProjectSel) {
    issProjectSel.addEventListener("change", _updateIssueProjectManualVisibility);
  }
  $("issue-modal-close").addEventListener("click", closeIssueModal);

  // Issue action (close/reopen) modal.
  $("issue-action-confirm").addEventListener("click", confirmIssueAction);
  $("issue-action-cancel").addEventListener("click", closeIssueActionModal);
  $("issue-action-close").addEventListener("click", closeIssueActionModal);

  // End-session confirmation modal.
  const endConfirm = $("end-session-confirm");
  if (endConfirm) endConfirm.addEventListener("click", confirmEndSession);
  const endCancel = $("end-session-cancel");
  if (endCancel) endCancel.addEventListener("click", closeEndSessionModal);
  const endClose = $("end-session-close");
  if (endClose) endClose.addEventListener("click", closeEndSessionModal);

  // Issue launch (start flow from issue) modal.
  const issueLaunchConfirm = $("issue-launch-confirm");
  if (issueLaunchConfirm) {
    issueLaunchConfirm.addEventListener("click", confirmIssueLaunch);
  }
  const issueLaunchCancel = $("issue-launch-cancel");
  if (issueLaunchCancel) {
    issueLaunchCancel.addEventListener("click", closeIssueLaunchModal);
  }
  const issueLaunchClose = $("issue-launch-close");
  if (issueLaunchClose) {
    issueLaunchClose.addEventListener("click", closeIssueLaunchModal);
  }

  // Narrow-screen History panel switch: return from the session detail to the
  // session list. Inert on desktop (the back button is hidden and both panes
  // render).
  const historyBack = $("history-back-btn");
  if (historyBack) {
    historyBack.addEventListener("click", () => applyHistoryPanelAction("back"));
  }

  // Topbar overflow menu (mobile): the hamburger opens it, activating any menu
  // item or clicking outside closes it. On desktop the toggle is hidden and the
  // menu wrapper is `display: contents`, so these bindings are harmless no-ops.
  const navToggle = $("nav-menu-toggle");
  if (navToggle) {
    navToggle.addEventListener("click", toggleNavMenu);
  }
  const navMenu = $("nav-menu");
  if (navMenu) {
    // Activating any control inside the menu collapses it.
    navMenu.addEventListener("click", (e) => {
      if (e.target.closest("button")) closeNavMenu();
    });
  }
  // A click anywhere outside the menu / toggle dismisses an open menu.
  document.addEventListener("click", (e) => {
    if (!isNavMenuOpen()) return;
    if (e.target.closest("#nav-menu") || e.target.closest("#nav-menu-toggle")) return;
    closeNavMenu();
  });

  // Auth gate: login / break-glass / logout.
  $("login-form").addEventListener("submit", handleLogin);
  $("breakglass-form").addEventListener("submit", handleBreakglass);
  $("breakglass-toggle").addEventListener("click", toggleBreakglass);
  $("logout-btn").addEventListener("click", handleLogout);

  // Daemon-key management panel.
  $("keys-btn").addEventListener("click", openKeys);
  $("keys-close").addEventListener("click", closeKeys);
  $("keys-create-form").addEventListener("submit", createDaemonKey);

  // Registered-project management panel (opened from a machine row).
  $("project-close").addEventListener("click", closeProjects);
  $("project-add-form").addEventListener("submit", addProject);
  $("project-remove-close").addEventListener("click", closeRemoveProject);
  $("project-remove-cancel").addEventListener("click", closeRemoveProject);
  $("project-remove-confirm").addEventListener("click", removeProject);

  // User-management panel (admin only).
  $("users-btn").addEventListener("click", openUsers);
  $("users-close").addEventListener("click", closeUsers);
  $("users-create-form").addEventListener("submit", createUser);

  $("flow-reply-form").addEventListener("submit", submitReply);
  $("flow-interject-btn").addEventListener("click", onInterjectButtonClick);
  // Ctrl/Cmd+Enter submits the reply box without leaving the textarea.
  $("flow-reply-input").addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      $("flow-reply-form").requestSubmit();
    }
  });
  // WeChat-style auto-grow: resize the textarea to its content as the user
  // types (mobile portrait only; a no-op on desktop).
  $("flow-reply-input").addEventListener("input", autoGrowReplyTextarea);

  // File attachments on both prompt inputs (paste / drag-drop / file picker).
  // Two scopes, three inputs: respond and interject share the docked textarea.
  bindUploadScope("newTask");
  bindUploadScope("flow");

  // Browser back collapses the flow view back to the flow list, instead of
  // leaving the site. openFlowView pushed an entry on top of the stack; a
  // popstate fired while the flow view is open means the user (or our own
  // closeFlowView via history.back) popped it off.
  window.addEventListener("popstate", () => {
    if (isFlowViewOpen()) {
      flowViewHistoryPushed = false;
      doCloseFlowView();
    }
  });

  // Topbar version label — sourced from the same `__version__` as the CLI.
  fetch("/api/version")
    .then((r) => r.json())
    .then(({ version }) => {
      const el = $("se3-version");
      if (el && version) el.textContent = "v" + version;
    })
    .catch(() => {});

  // Resolve any existing session, then either open the realtime socket
  // (authenticated) or show the login gate. `connect()` is now reached only
  // through the auth flow, never unconditionally on boot.
  bootstrapAuth();
}

if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", init);
}

// Expose the pure, DOM-free helpers for lightweight Node assertion tests.
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    isCollapsibleRole,
    chipLabel,
    normalizeKind,
    replyStepType,
    sanitizeDomToken,
    computeInterventions,
    normalizeRecord,
    isActiveFlow,
    truncate,
    optionLabel,
    optionText,
    pendingCalls,
    populateProjectSelect,
    isValidAbsolutePath,
    PROJECT_MANUAL_SENTINEL,
    splitUserPromptByMarker,
    STEP_REPORT_TITLES,
    reportCardTitle,
    STEP_HEADER_TITLES,
    stepHeaderLabel,
    groupStatusLabel,
    GROUP_STATUS_TEXT,
    renderGroupStatusRecord,
    // Code-index update-progress marker (G3) — exposed for the DOM-free +
    // DOM-stub tests in tests/frontend/index_progress.test.mjs.
    indexProgressLabel,
    indexProgressState,
    renderIndexProgressRecord,
    STEP_STATUS_DISPLAY,
    stepStatusDisplay,
    renderStepStartedRecord,
    tagStepType,
    hasRawPayload,
    makeRawToggle,
    makeUserRawToggle,
    STEP_REPORT_RENDERERS,
    reportList,
    // Token-usage display (G4) — exposed for the DOM-free tests in
    // tests/frontend/token_usage.test.mjs.
    formatTokenUsage,
    accumulateSessionUsage,
    isTokenUsageEmpty,
    formatCostUsd,
    buildStepUsageFootnote,
    updateFlowUsageBadge,
    // History-detail session-usage badge — exposed for the DOM-stub tests in
    // tests/frontend/history_usage.test.mjs.
    applyUsageBadge,
    updateHistoryUsageBadge,
    // Backend usage-summary rendering (G10) — exposed for the DOM-free /
    // DOM-stub tests in tests/frontend/strategy_usage_summary.test.mjs.
    usageStatusMark,
    formatUsageTotals,
    formatCostOrUnknown,
    formatCostOrDash,
    usagePayloadSummary,
    appendUsageCostLines,
    renderCompactUsageSummary,
    renderUsagePayloadRegion,
    renderHistoryUsageRegion,
    // Plan-decomposition + review-scope display — exposed for the DOM-free /
    // DOM-stub tests in tests/frontend/strategy_usage_summary.test.mjs.
    planDecompositionLabel,
    planGranularityLabel,
    legacyStrategyLabel,
    planModeReasonText,
    buildPlanModeRows,
    appendPlanModeSection,
    buildScopeRows,
    collectScopeAuditFromRecords,
    renderHistoryStrategyScope,
    // Per-round usage footnote (G5) — exposed for the DOM-free tests in
    // tests/frontend/round_usage.test.mjs.
    buildRoundUsageFootnote,
    accumulateRoundUsageByStep,
    renderProposalFields,
    renderDesignFields,
    renderPlanReport,
    renderStepReport,
    renderStepOutputUsageRecord,
    STEP_ASSISTANT_RENDERERS,
    registerAssistantRenderer,
    renderDiscoveryAssistant,
    makeStructuredAssistantRenderer,
    renderAssistantBubble,
    renderGenericOutputs,
    renderDefaultReport,
    isPlainOutputsDict,
    extractStructuredJson,
    extractResultJson,
    collectJsonRegions,
    isStepResultDict,
    isDiscoveryResultDict,
    STEP_RESULT_FIELDS,
    TEMPLATE_PREFIX_END,
    USER_CONTENT_BEGIN,
    USER_CONTENT_END,
    KIND_META,
    extractAssistantText,
    // Tool chip state machine (exposed for the DOM-stub tests in
    // tests/frontend/tool_chip_state.test.mjs).
    parseToolBracket,
    createInFlightChip,
    upgradeChipToSuccess,
    upgradeChipToFailure,
    renderToolDetailPanel,
    extractAssistantChipEvents,
    renderChipEvents,
    renderNarrativeNodes,
    applyFragmentToBubble,
    refreshPartialAgentBadge,
    buildPartialBubble,
    appendPartialFragment,
    // Incremental conversation reconciliation + chip refresh (exposed for the
    // DOM-stub tests in tests/frontend/test_app_pure.mjs).
    renderConversation,
    addConversationRecords,
    insertBubbleSorted,
    rebuildStepHeaders,
    // Viewport-driven sticky floating step header (G5) — exposed for the
    // DOM-free + DOM-stub tests in tests/frontend/sticky_step_header.test.mjs.
    computeStickyStep,
    stickyScrollTarget,
    measureStepHeaderOffsets,
    updateStickyHeader,
    ensureStickyHeaderMounted,
    smoothScrollTo,
    markSupersededProgress,
    partialSegments,
    progressTurnKey,
    renderConversationRecord,
    renderInterventions,
    buildCollapsiblePrompt,
    reconcileReplyTarget,
    tsValue,
    stepKey,
    recordKey,
    recordOrdinal,
    reconcileAppendRecords,
    mergeSnapshotWithLiveAppends,
    dedupeAppendRecords,
    dedupeSnapshotDiscovery,
    recordSortTs,
    stableMergeByTimestamp,
    historySnapshotUrl,
    mergeHistoryResponse,
    // Cursor completeness self-check + numbered backfill (head-loss repair) —
    // exposed for tests/frontend/history_cursor_backfill.test.mjs.
    stepIdFromCursorKey,
    findMissingOrdinals,
    encodeMissingParam,
    reconcileCursorCompleteness,
    applyHistoryCursor,
    reconcileLocalEchoes,
    comparableUserText,
    // Optimistic reply echo + send path (G1/G2) — exposed for the DOM-stub
    // tests in tests/frontend/reply_send_error_handling.test.mjs so the
    // integrated success-branch-vs-network-error-catch behavior (issue #193)
    // can be asserted, not just the appendLocalReply helper in isolation.
    appendLocalReply,
    sendReply,
    // CONFIRM approval gate (G3): structured decision send + free-text mirror,
    // exposed for tests/frontend/confirm_chip.test.mjs.
    sendConfirmDecision,
    // ADJUDICATE approval-review block (G4): rationale panel + baseline→
    // adjudicated_description diff, exposed for tests/frontend/adjudicate_review.test.mjs.
    renderAdjudicateReview,
    submitReply,
    // Exposed alongside submitReply so the upload suite can drive BOTH prompt
    // submitters against the in-flight-attachment gate.
    submitNewTask,
    updateReplyBox,
    interpretConfirmAnswer,
    CONFIRM_APPROVE_TOKENS,
    CONFIRM_REJECT_TOKENS,
    showToast,
    settlePendingSend,
    // Reconnect incremental load paths (G4) — exposed for the DOM-stub load
    // path tests in tests/frontend/test_app_pure.mjs.
    loadFlowConversation,
    // Element-anchored scroll preservation for the silent rebuild (issue #209
    // jump fix) — exposed for the DOM-stub tests in
    // tests/frontend/issue217_scroll_anchor.test.mjs.
    captureScrollAnchor,
    restoreScrollAnchor,
    // Cause-immune progression-refresh fallback (G2) — exposed for the DOM-stub
    // tests in tests/frontend/progression_refresh.test.mjs.
    maybeRefreshConversationOnProgression,
    cancelProgressionGrace,
    refreshFlowDetail,
    // G3 periodic full-snapshot self-heal — exposed for the DOM-stub tests in
    // tests/frontend/test_app_pure.mjs (the 3s poll callback, its conversation
    // self-heal, and the visible-equality no-op guard).
    pollFlowView,
    selfHealFlowConversation,
    sameRenderedConversation,
    openHistorySession,
    closeHistory,
    applyHistoryData,
    // G5 differential history-index merge + detail lazy-load — exposed for the
    // DOM-stub tests in tests/frontend/test_app_pure.mjs.
    applyHistoryIndex,
    applyHistoryIndexDelta,
    descriptionLikelyTruncated,
    loadIssueFullDescription,
    fetchIssueFullDescription,
    fetchCallFullPrompt,
    renderIssueDetail,
    // History list rendering + shared mutable state (exposed for the DOM-stub
    // tests in tests/frontend/test_app_pure.mjs).
    renderHistoryList,
    historyListEmptyState,
    daemonConnected,
    UNKNOWN_PROJECT_ROOT,
    UNKNOWN_PROJECT_ROOT_LABEL,
    groupHistorySessionsByProjectRoot,
    pickDefaultHistoryProjectRoot,
    // Resume-flow pure helpers (G6) — exposed for the DOM-free tests in
    // tests/frontend/flow_resume.test.mjs.
    isFlowResumable,
    RESUMABLE_STATUSES,
    isResumeInProgress,
    resumeErrorText,
    // End-session pure helpers (G4) — exposed for the DOM-free tests in
    // tests/frontend/end_session.test.mjs.
    isFlowEndable,
    isWorktreeSessionPath,
    isEndInProgress,
    makeEndButton,
    // Waiting-for-lock running sub-state (G2) — exposed for the DOM-free tests
    // in tests/frontend/waiting_for_lock.test.mjs.
    isWaitingForLock,
    flowStatusLabel,
    // Localized status-badge text — exposed for the DOM-free tests in
    // tests/frontend/i18n_render_switch.test.mjs.
    flowStatusText,
    issueStatusText,
    issueTypeText,
    issuePriorityText,
    issueSourceText,
    // Local interjection lifecycle helpers (G4) — exposed for the DOM-free
    // tests in tests/frontend/test_app_pure.mjs.
    bindLocalInterjectionToCallId,
    consumeLocalInterjectionByCallId,
    applyInterjectionEvent,
    // Auth / owner identity pure helpers (G9) — exposed for the DOM-free
    // tests in tests/test_server_authz_frontend.mjs.
    AUTH_STATES,
    nextAuthState,
    ownerLabel,
    isUnauthorizedStatus,
    canOwnerControlMachine,
    visibleMachinesForOwner,
    daemonKeyRowModel,
    // Registered-project management (G4) — pure helpers plus the DOM-stub
    // renderer, exposed for tests/frontend/project_registry.test.mjs.
    projectRegistryRowModel,
    buildAddProjectBody,
    projectErrorKey,
    applyProjectAdded,
    applyProjectRemoved,
    renderProjects,
    openProjects,
    closeProjects,
    loadProjects,
    addProject,
    confirmRemoveProject,
    closeRemoveProject,
    removeProject,
    syncProjectsFromSnapshot,
    // File-attachment upload helpers (G4) — DOM-free, exposed for the tests in
    // tests/frontend/file_upload.test.mjs.
    MAX_UPLOAD_BYTES,
    validateUploadFile,
    uploadPlaceholderToken,
    replaceTokenOnce,
    removePathOnce,
    insertAtCaret,
    formatFileSize,
    isImageFile,
    attachmentRowModel,
    uploadErrorKey,
    UPLOAD_ERROR_KEYS,
    pendingUploadNames,
    pendingUploadRefusal,
    // Upload orchestration + attachment strip (G5) — the DOM-stub tests in
    // tests/frontend/file_upload.test.mjs drive these directly.
    UPLOAD_SCOPES,
    resolveUploadTarget,
    uploadRequestUrl,
    // Inline conversation thumbnails for stored attachments (G5) — exposed for
    // tests/frontend/inline_upload_images.test.mjs.
    extractUploadImagePaths,
    uploadFetchUrl,
    resolveInlineImageTarget,
    renderInlineUploadImages,
    uploadFailureText,
    replaceInInputOnce,
    attachmentEntries,
    performUpload,
    UPLOAD_REQUEST_TIMEOUT_MS,
    renderAttachmentStrip,
    removeAttachment,
    cancelAttachment,
    abortUploadEntry,
    clearAttachments,
    discardAttachments,
    uploadTargetKey,
    syncNewTaskUploadTarget,
    startUploads,
    handleInputPaste,
    handleInputDragOver,
    handleInputDrop,
    bindUploadScope,
    filesFromClipboard,
    filesFromList,
    dragCarriesFiles,
    // User-management row model (G3) — exposed for the DOM-free tests in
    // tests/frontend/user_mgmt.test.mjs.
    userRowModel,
    // Issue management pure helpers (G7) — exposed for the DOM-free tests in
    // tests/frontend/issue_management.test.mjs.
    issueDisplayTitle,
    issueSlug,
    filterIssues,
    issueTypes,
    issuesPanelState,
    issueStatusClass,
    issuePriorityClass,
    KNOWN_ISSUE_TYPES,
    issueMachineId,
    issueCompositeKey,
    buildIssueCreateBody,
    buildIssueEditBody,
    buildIssueActionBody,
    // Start-flow-from-issue pure helpers (G4) — exposed for the DOM-free tests
    // in tests/frontend/issue_management.test.mjs.
    issueLaunchModel,
    buildIssueFlowBody,
    buildNewFlowBody,
    ISSUE_LAUNCH_DISABLED_REASONS,
    parseTagsFromString,
    formatTagsForInput,
    selectTypeDropdownOptions,
    // Issue project-filter pure helpers (G3) — exposed for the DOM-free tests in
    // tests/frontend/issue_management.test.mjs.
    issueProjectRoots,
    pickDefaultIssueProjectRoot,
    // fetchIssues request-coalescing pure helpers (G7) — exposed for the
    // DOM-free regression tests in tests/frontend/issue_management.test.mjs.
    fetchIssuesCoalesceDecision,
    fetchIssuesFinallyDecision,
    // fetchAllIssueTypes stale-response guard (G1) — exposed for the DOM-free
    // regression tests in tests/frontend/issue_management.test.mjs.
    allIssueTypesApplyDecision,
    // Topbar overflow-menu state helper (G2) — exposed for the DOM-free tests
    // in tests/frontend/mobile_responsive.test.mjs.
    navMenuNextState,
    // Main-list panel-switch state helper (G3) — exposed for the DOM-free tests
    // in tests/frontend/mobile_responsive.test.mjs.
    listPanelState,
    // History panel-switch state helper (G5) — exposed for the DOM-free tests
    // in tests/frontend/mobile_responsive.test.mjs.
    historyPanelState,
    // Flow-view sidebar-drawer state helper (G4) — exposed for the DOM-free
    // tests in tests/frontend/mobile_responsive.test.mjs.
    flowSidebarNextState,
    // WeChat-style auto-grow reply textarea clamp — exposed for the DOM-free
    // tests in tests/frontend/mobile_responsive.test.mjs.
    replyTextareaHeight,
    // Agent/model badge (G1) — exposed for the DOM-free tests in
    // tests/frontend/test_app_pure.mjs.
    formatAgentBadgeText,
    renderAgentBadge,
    // Diff-aware render-signature infrastructure (G1) — exposed for the
    // DOM-free tests in tests/frontend/test_app_pure.mjs.
    renderSignature,
    resetRenderSignatures,
    // Project-root basename label for running flows (exposed for the DOM-free
    // tests in tests/frontend/test_app_pure.mjs).
    projectBasename,
    projectDisplayLabel,
    // Machine/flow list signatures (G2) — exposed for the DOM-free tests.
    machinesSignature,
    flowsSignature,
    renderMachines,
    renderFlows,
    // Sidebar diff-aware signature (G3) — exposed for DOM-free tests.
    flowSidebarSignature,
    // Reply-panel diff-aware equality (G4) — exposed for the DOM-free tests in
    // tests/frontend/test_app_pure.mjs.
    interventionsSignature,
    // WebUI i18n subsystem (G6) — exposed for the DOM-free + DOM-stub tests in
    // tests/frontend/i18n_render_switch.test.mjs.
    I18N,
    applyNodeTranslations,
    repaintOpenModals,
    // Element/label helpers shared by the DOM-stub test modules (G10) so the
    // strategy/usage-summary suites build and inspect the same nodes the app
    // renders, without re-implementing el()/tf() locally.
    el,
    tf,
    $,
    usageNum,
    // Live-state chrome that a language switch must repaint itself (the badge
    // and the tab title carry no data-i18n attribute) — exposed for the
    // DOM-stub tests in tests/frontend/i18n_render_switch.test.mjs.
    setConnStatus,
    repaintConnStatus,
    applyDocumentTitle,
    state,
  };
}
