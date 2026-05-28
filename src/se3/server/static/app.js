/*
 * SE3 Control Plane — web frontend.
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
  machines: [],           // [{machine_id, hostname, online, flows: [...]}]
  selectedMachineId: null,
  selectedFlowId: null,   // flow open in the full-screen flow view
  flowDetail: null,       // last fetched flow object (for the open flow view)
  flowMachineId: null,    // machine id owning the open flow
  flowConversationRecords: [],   // conversation records shown in the flow view
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
  // project_root key of the currently-selected History tab; null lets
  // pickDefaultHistoryProjectRoot pick the most-recently-active project. Reset
  // by closeHistory() so the next openHistory() recomputes the default.
  historySelectedProjectRoot: null,
  connStale: false,       // true while the WS is down — data may be stale
  detailLoaded: false,    // true once the open flow's detail has rendered
  detailFetchFailures: 0, // consecutive /api/flows/{id} failures for the view
};

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

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

function statusClass(status) {
  const s = String(status || "unknown").toLowerCase();
  if (["running", "completed", "failed", "paused", "init"].includes(s)) return s;
  return "unknown";
}

// A flow is "active" while it can still consume a human interaction — it is
// either making progress (running/init/recovering) or parked awaiting one
// (paused). Completed/failed flows are terminal and accept no further input.
function isActiveFlow(flow) {
  const s = String((flow && flow.status) || "").toLowerCase();
  return ["running", "paused", "init", "recovering"].includes(s);
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

function setConnStatus(kind, label) {
  const node = $("conn-status");
  node.className = "conn conn-" + kind;
  node.textContent = label;
}

// Toggle the "data may be stale" banners shown over the history view and the
// running-flow view while the WebSocket connection is down.
function setStale(stale) {
  state.connStale = !!stale;
  for (const id of ["history-stale", "flow-view-stale"]) {
    const node = $(id);
    if (node) node.classList.toggle("hidden", !stale);
  }
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${location.host}/ws/ui`;
  setConnStatus("connecting", reconnectAttempts ? "reconnecting…" : "connecting…");

  ws = new WebSocket(url);

  ws.onopen = () => {
    // A reconnect (rather than the first connect) means the views may be
    // showing stale data — clear the banners and refresh what's open.
    const wasReconnect = reconnectAttempts > 0 || state.connStale;
    reconnectAttempts = 0;
    setConnStatus("connected", "connected");
    setStale(false);
    if (wasReconnect) {
      if (state.selectedFlowId) {
        refreshFlowDetail();
        // Re-pull the conversation snapshot so records emitted during the
        // outage (whose `history_data` append deltas were never delivered)
        // are backfilled — mirroring the history view's re-fetch below.
        loadFlowConversation(state.selectedFlowId);
      }
      if (isHistoryOpen()) {
        fetchHistoryIndex();
        if (state.selectedHistoryId) openHistorySession(state.selectedHistoryId);
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
    } else if (msg.type === "history_data" && msg.flow_id) {
      applyHistoryData(msg);
    }
  };

  ws.onclose = () => {
    setConnStatus("disconnected", "disconnected");
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

function applyMachines(machines) {
  state.machines = machines;

  // Keep selection valid; default to the first machine.
  if (!state.machines.some((m) => m.machine_id === state.selectedMachineId)) {
    state.selectedMachineId = state.machines.length
      ? state.machines[0].machine_id
      : null;
  }

  renderMachines();
  renderFlows();

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
// Render: machine list
// ---------------------------------------------------------------------------

function renderMachines() {
  const list = $("machine-list");
  list.innerHTML = "";

  if (!state.machines.length) {
    list.appendChild(el("li", "empty", "No machines connected."));
    return;
  }

  for (const m of state.machines) {
    const li = el("li", "machine-item");
    if (m.machine_id === state.selectedMachineId) li.classList.add("selected");

    const dot = el("span", "dot " + (m.online ? "online" : "offline"));
    const name = el("span", "machine-name", m.hostname || m.machine_id);
    name.title = m.machine_id;
    const count = el("span", "machine-count",
      `${(m.flows || []).length} flow${(m.flows || []).length === 1 ? "" : "s"}`);

    li.append(dot, name, count);
    li.addEventListener("click", () => {
      state.selectedMachineId = m.machine_id;
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
  const panel = $("flow-list");
  const heading = $("flows-heading");
  panel.innerHTML = "";

  const machine = state.machines.find((m) => m.machine_id === state.selectedMachineId);
  if (!machine) {
    heading.textContent = "Flows";
    panel.appendChild(el("p", "empty", "Select a machine to view its flows."));
    return;
  }

  heading.textContent = `Flows — ${machine.hostname || machine.machine_id}`;
  const flows = machine.flows || [];
  if (!flows.length) {
    panel.appendChild(el("p", "empty", "No flows on this machine."));
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
    flow.task_description || flow.flow_id || "(untitled flow)");
  task.title = flow.task_description || "";
  const sc = statusClass(flow.status);
  const badge = el("span", "badge badge-" + sc, flow.status || "unknown");
  head.append(task, badge);

  if (hasPendingCall(flow)) {
    // The badge is purely an indicator — opening the flow view (below) is the
    // single entry point; there is no separate context-less call modal.
    head.appendChild(el("span", "badge badge-call", "⚠ needs response"));
  }

  const bar = el("div", "progress");
  const inner = el("div", "progress-bar");
  inner.style.width = Math.round((flow.progress || 0) * 100) + "%";
  bar.appendChild(inner);

  const meta = el("div", "flow-meta");
  meta.append(
    el("span", null, flow.current_step
      ? `step: ${flow.current_step}`
      : (flow.task_type || "")),
    el("span", null, `${flow.current_step_index || 0}/${flow.total_steps || 0}`),
  );

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
  state.flowMachineId = null;
  state.flowConversationRecords = [];
  state.flowInterventions = [];
  state.flowReplyTargetId = null;
  state.flowInterjectRequested = false;
  state.flowInterjectFlowId = flowId;
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
  $("flow-view-title").textContent = "Flow";
  renderSidebarPlaceholder("Loading flow details…");
  $("flow-interventions").innerHTML = "";
  resetReplyBox();

  refreshFlowDetail();
  // Fetch the flow's conversation snapshot; WS history_data deltas append live.
  loadFlowConversation(flowId);
  // Poll the REST endpoint while the view is open (WS updates also refresh).
  if (detailPollTimer) clearInterval(detailPollTimer);
  detailPollTimer = setInterval(refreshFlowDetail, 3000);
}

// Cleanup-only close: clears state and hides the view, but never touches
// history. The single source of truth for closing a flow view is the
// popstate handler — both the ✕ button and the browser back button funnel
// through it, so there is no risk of push-back loops or double-pop drift.
function doCloseFlowView() {
  state.selectedFlowId = null;
  state.flowDetail = null;
  state.flowMachineId = null;
  state.flowConversationRecords = [];
  state.flowInterventions = [];
  state.flowReplyTargetId = null;
  state.flowInterjectRequested = false;
  state.flowInterjectFlowId = null;
  $("flow-view").classList.add("hidden");
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

// Fetch the initial conversation snapshot for the open flow. Mirrors the
// history view: a one-shot `/api/history/{flow_id}` pull, after which the WS
// `history_data` push keeps an active flow's conversation up to date.
async function loadFlowConversation(flowId) {
  const container = $("flow-conversation");
  container.innerHTML = "";
  // Drop any reconciliation state left by a previously-open flow so a stray
  // append for this flow can't merge into the prior flow's detached sections.
  container.__convState = null;
  container.appendChild(el("p", "empty", "Loading conversation…"));
  try {
    const resp = await fetch(`/api/history/${encodeURIComponent(flowId)}`);
    // The user may have opened another flow while this was in flight.
    if (state.selectedFlowId !== flowId) return;
    if (!resp.ok) {
      container.innerHTML = "";
      container.appendChild(el("p", "empty",
        `Could not load conversation for this flow (${resp.status}).`));
      return;
    }
    const data = await resp.json();
    if (state.selectedFlowId !== flowId) return;
    // Merge the snapshot with whatever is already in the array: `history_data`
    // appends that arrived during the await, plus any locally-spliced replies.
    const snapshot = Array.isArray(data.records) ? data.records : [];
    state.flowConversationRecords = mergeSnapshotWithLiveAppends(
      snapshot, state.flowConversationRecords);
    renderConversation(container, state.flowConversationRecords);
    scrollFlowConversationToBottom();
  } catch (_) {
    if (state.selectedFlowId !== flowId) return;
    container.innerHTML = "";
    container.appendChild(el("p", "empty", "Network error loading conversation."));
  }
}

function scrollFlowConversationToBottom() {
  const c = $("flow-conversation");
  c.scrollTop = c.scrollHeight;
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
    renderSidebarPlaceholder(message + " Retrying…");
  }
}

async function refreshFlowDetail() {
  const flowId = state.selectedFlowId;
  if (!flowId) return;
  try {
    const resp = await fetch(`/api/flows/${encodeURIComponent(flowId)}`);
    if (state.selectedFlowId !== flowId) return;
    if (!resp.ok) {
      noteDetailFetchFailure(`Could not load flow details (${resp.status}).`);
      return;
    }
    const data = await resp.json();
    if (state.selectedFlowId !== flowId) return;
    if (!data || !data.flow) {
      noteDetailFetchFailure("This flow is not available on the server yet.");
      return;
    }
    state.detailFetchFailures = 0;
    state.detailLoaded = true;
    state.flowDetail = data.flow;
    state.flowMachineId = data.machine_id || null;
    renderFlowSidebar(data.flow, data.machine_id);
    renderInterventions(data.flow);
  } catch (_) {
    if (state.selectedFlowId !== flowId) return;
    noteDetailFetchFailure("Network error loading flow details.");
  }
}

// Render the sidebar: Overview, Steps, and Machine. Rebuilt wholesale on each
// 3s poll — the panel is small, so a full rebuild does not visibly flicker.
function renderFlowSidebar(flow, machineId) {
  $("flow-view-title").textContent =
    flow.task_description || flow.flow_id || "Flow";

  const body = $("flow-sidebar-body");
  body.innerHTML = "";

  const kv = (k, v) => {
    const row = el("div", "kv");
    row.append(el("span", "k", k), el("span", "v", String(v)));
    return row;
  };
  const sc = statusClass(flow.status);

  // -- overview --
  const overview = el("div", "detail-section");
  overview.appendChild(el("h4", null, "Overview"));
  overview.appendChild(kv("Status", flow.status || "unknown"));
  overview.appendChild(kv("Type", flow.task_type || "-"));
  overview.appendChild(kv(
    "Progress",
    `${flow.current_step_index || 0}/${flow.total_steps || 0} ` +
    `(${Math.round((flow.progress || 0) * 100)}%)`,
  ));
  if (flow.current_step) overview.appendChild(kv("Current step", flow.current_step));
  if (flow.updated_at) overview.appendChild(kv("Updated", formatTime(flow.updated_at)));
  body.appendChild(overview);

  // -- steps --
  const steps = Array.isArray(flow.step_history) ? flow.step_history : [];
  const stepSec = el("div", "detail-section");
  stepSec.appendChild(el("h4", null, "Steps"));
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
    stepSec.appendChild(el("p", "empty", "No step history reported."));
  }
  body.appendChild(stepSec);

  // -- machine --
  const machineSec = el("div", "detail-section");
  machineSec.appendChild(el("h4", null, "Machine"));
  machineSec.appendChild(kv("Machine", machineId || "-"));
  if (flow.flow_id) machineSec.appendChild(kv("Flow id", flow.flow_id));
  body.appendChild(machineSec);
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
    label: "待回复",
    hint: "运行流程正在等待你的回复。",
    icon: "⚙",
  },
  interjection: {
    label: "插话",
    hint: "向正在运行的流程补一条额外指令。",
    icon: "✎",
  },
  retry_decision: {
    label: "需要决策",
    hint: "某一步骤失败,请选择如何继续(例如 重试 / 跳过 / 终止)。",
    icon: "↻",
  },
  cli_confirm: {
    label: "需要确认",
    hint: "子进程正在等待一次确认。",
    icon: "⌨",
  },
  discovery_confirm: {
    label: "确认任务描述",
    hint: "Discovery 已生成精炼后的任务描述。输入 1 确认并继续,或回复其它内容继续完善需求。",
    icon: "✓",
  },
};

// Canonicalize a raw `kind` field; unknown kinds degrade to a plain "call".
function normalizeKind(kind) {
  const k = String(kind || "call").toLowerCase();
  return KIND_META[k] ? k : "call";
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
  const entries = pendingCalls(flow).map((c, i) => {
    const kind = normalizeKind(c.kind);
    return {
      id: "call:" + (c.call_id || ("idx" + i)),
      kind: kind,
      callId: String(c.call_id || ""),
      prompt: String(c.prompt || c.message || ""),
      context: c.context != null ? c.context : null,
      options: Array.isArray(c.options) ? c.options : [],
      synthetic: false,
    };
  });
  const hasInterjection = entries.some((e) => e.kind === "interjection");
  if (
    state.flowInterjectRequested &&
    isActiveFlow(flow) &&
    !hasInterjection
  ) {
    entries.push({
      id: "interjection:new",
      kind: "interjection",
      callId: "",
      prompt: "",
      context: null,
      options: [],
      synthetic: true,
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
  const firstCall = entries.find((e) => !e.synthetic);
  return (firstCall || entries[0]).id || null;
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
  const region = $("flow-interventions");
  region.innerHTML = "";
  const entries = computeInterventions(flow);
  state.flowInterventions = entries;

  state.flowReplyTargetId = reconcileReplyTarget(entries, state.flowReplyTargetId);

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
    ? "取消插话"
    : "向运行中的流程补一条额外指令。";
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

  chip.append(
    el("span", "intervention-chip-icon", meta.icon),
    el("span", "intervention-chip-label", meta.label),
  );
  if (entry.callId) {
    // The internal call id stays on the chip for debugging — surfaced only
    // through `data-call-id` and the hover tooltip — but never rendered as
    // visible text so the user never sees implementation jargon.
    chip.dataset.callId = entry.callId;
    chip.title = entry.callId;
  }

  chip.addEventListener("click", (e) => {
    e.preventDefault();
    if (state.flowReplyTargetId === entry.id) return;
    state.flowReplyTargetId = entry.id;
    if (state.flowDetail) renderInterventions(state.flowDetail);
    $("flow-reply-input").focus();
  });

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
    submit.disabled = true;
    input.placeholder = isActiveFlow(flow)
      ? "暂无待处理项 — 你可以先草拟回复,或点击 ✎ 插话…"
      : "暂无待处理项…";
    ctx.className = "flow-reply-context";
    ctx.innerHTML = "";
    ctx.appendChild(el("p", "flow-reply-empty",
      isActiveFlow(flow)
        ? "目前没有待处理的交互项 — 没有可以回复的对象。"
        : "该流程已结束 — 无法再继续交互。"));
    return;
  }

  let target = entries.find((e) => e.id === state.flowReplyTargetId);
  if (!target) {
    target = entries[0];
    state.flowReplyTargetId = target.id;
  }
  const meta = KIND_META[target.kind] || KIND_META.call;

  input.disabled = false;
  submit.disabled = false;
  input.placeholder = target.kind === "interjection"
    ? "输入要插入运行流程的指令…"
    : "输入你的回复…";

  ctx.className = "flow-reply-context active kind-" + target.kind;
  ctx.innerHTML = "";

  // Header row: neutral "回复中 · <kind label>" wording. The internal call id
  // is intentionally absent from any visible text — it remains on the head's
  // `data-call-id` and `title` tooltip so operators can still cross-reference
  // calls when debugging, but is never surfaced to the user as jargon.
  const head = el("div", "flow-reply-head");
  head.append(
    el("span", "flow-reply-to", "回复中"),
    el("span", "flow-reply-sep", "·"),
    el("span", "flow-reply-kind kind-" + target.kind, meta.label),
  );
  if (target.callId) {
    head.dataset.callId = target.callId;
    head.title = target.callId;
  }
  ctx.appendChild(head);

  // Full prompt — rendered as Markdown, never truncated.
  if (target.prompt) {
    const prompt = el("div", "flow-reply-prompt");
    prompt.appendChild(renderMarkdown(target.prompt));
    ctx.appendChild(prompt);
  } else {
    ctx.appendChild(el("p", "flow-reply-hint", meta.hint));
  }

  // Context block is intentionally NOT rendered for any kind. Every pending
  // intervention's prompt already carries the human-meaningful text the user
  // needs to act (discovery_confirm embeds the refined task description; the
  // other kinds — call / interjection / cli_confirm / retry_decision — carry
  // their full prompt), and the backend mirrors the same payload into the
  // prompt, so a separate context block only duplicated content below the
  // prompt. Suppressing it for all kinds keeps the reply panel free of
  // redundant context, matching the prior discovery_confirm-only behavior.

  // Optional options — render as one-click reply buttons. Clicking sends the
  // option text directly via sendReply, same path the inline option click on
  // the previous card layout used.
  //
  // Discovery confirmation: guarantee a GUI confirm button (sends the literal
  // "1" the programmatic gate expects) even if a backend call file omitted the
  // option, so the button + the "输入 1 确认" textual prompt always coexist.
  let options = target.options || [];
  if (target.kind === "discovery_confirm" && !options.length) {
    options = [{ label: "确认并继续 (输入 1)", value: "1" }];
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
    "No pending interaction right now."));
}

function truncate(text, max) {
  const s = String(text || "").replace(/\s+/g, " ").trim();
  return s.length > max ? s.slice(0, max - 1) + "…" : s;
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
    showToast("error", "No interaction is selected to respond to.");
    return;
  }
  const input = $("flow-reply-input");
  const text = input.value.trim();
  if (!text) {
    showToast("error", "Response must not be empty.");
    return;
  }
  sendReply(state.selectedFlowId, target, text);
}

async function sendReply(flowId, target, text) {
  if (!flowId || !target || !text) return;
  const submit = $("flow-reply-submit");
  const input = $("flow-reply-input");
  submit.disabled = true;
  // Lock the textarea while the request is in flight so the user does not
  // edit a draft that is mid-send. Restored in the finally block.
  input.disabled = true;
  try {
    let resp;
    if (target.kind === "interjection") {
      resp = await fetch(`/api/flows/${encodeURIComponent(flowId)}/interject`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: text }),
      });
    } else {
      resp = await fetch(`/api/flows/${encodeURIComponent(flowId)}/respond`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ response: text, call_id: target.callId }),
      });
    }
    if (resp.ok) {
      if (state.selectedFlowId === flowId) $("flow-reply-input").value = "";
      // The interject opt-in is consumed by a successful send; the reply box
      // should disarm until the user opts in again (or a new pending call
      // arrives), matching the CLI parity contract.
      if (target.kind === "interjection" && target.synthetic) {
        state.flowInterjectRequested = false;
        state.flowReplyTargetId = null;
      }
      appendLocalReply(flowId, target, text);
      showToast("success", target.kind === "interjection"
        ? "Interjection sent."
        : "Response sent.");
    } else {
      const detail = await resp.json().catch(() => ({}));
      const message = detail.detail || `Server returned ${resp.status}.`;
      showToast("error", `Could not send: ${message}`);
    }
  } catch (_) {
    showToast("error", "Could not send — network error reaching the server.");
  } finally {
    // Re-sync the reply box: textarea returns to enabled (drafts always allowed),
    // submit reflects whether a target still exists, and the inline Interject
    // button refreshes its visibility/active state.
    if (state.selectedFlowId === flowId && state.flowDetail) {
      renderInterventions(state.flowDetail);
    } else {
      // No flow detail to re-derive a target from (different flow selected, or
      // detail not yet loaded). Drafts are always allowed, but Send must stay
      // disabled because we cannot prove a sendable target exists.
      input.disabled = false;
      submit.disabled = true;
    }
  }
}

// Splice a just-sent reply into the conversation as its own record so it is
// visible immediately, without waiting for the next `history_data` push.
function appendLocalReply(flowId, target, text) {
  if (state.selectedFlowId !== flowId) return;
  const meta = KIND_META[target.kind] || KIND_META.call;
  const record = {
    step_id: "interaction",
    message: {
      role: "user",
      content: text,
      timestamp: Date.now(),
      step_type: meta.label + " response",
    },
  };
  state.flowConversationRecords = state.flowConversationRecords.concat([record]);
  renderConversation($("flow-conversation"), state.flowConversationRecords, true);
  scrollFlowConversationToBottom();
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
  renderHistoryList();
  fetchHistoryIndex();
}

function closeHistory() {
  $("history-view").classList.add("hidden");
  state.selectedHistoryId = null;
  state.historyRecords = [];
  state.historySelectedProjectRoot = null;
}

async function fetchHistoryIndex() {
  // Enter the loading state so renderHistoryList can show "正在刷新历史…"
  // instead of a blank page while the server forces a fresh index refresh
  // (broadcast_index_refresh → daemon force_index). The underlying live-pull
  // mechanism is unchanged; this only surfaces the in-flight feedback.
  state.historyIndexLoading = true;
  renderHistoryList();
  try {
    const resp = await fetch("/api/history");
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
function applyHistoryIndex(sessions) {
  state.historySessions = sessions;
  // Any history_index push — even an empty list — is an authoritative report
  // that the daemon has accounted for its history, so an empty result can now
  // settle into the confirmed-empty state instead of "still connecting".
  state.historyIndexConfirmed = true;
  if (isHistoryOpen()) renderHistoryList();
}

// Push handler: incremental (or full) records for one flow. The same flow may
// be open in both the history view and the running-flow view; each keeps its
// own record array so they update independently without double-appending.
function applyHistoryData(msg) {
  const records = Array.isArray(msg.records) ? msg.records : [];
  const append = msg.mode === "append";

  // -- history view consumer --
  if (isHistoryOpen() && state.selectedHistoryId === msg.flow_id) {
    // Capture the reader's position BEFORE re-rendering: an append grows
    // scrollHeight, so "near bottom" must be measured against the old layout.
    const stick = !append || isNearBottom(historyScrollContainer());
    state.historyRecords = append
      ? state.historyRecords.concat(records)
      : records;
    renderHistoryRecords(msg.flow_id, state.historyRecords, append);
    if (stick) scrollHistoryToBottom();
  }

  // -- running-flow view consumer --
  if (state.selectedFlowId === msg.flow_id) {
    const stick = !append || isNearBottom($("flow-conversation"));
    state.flowConversationRecords = append
      ? state.flowConversationRecords.concat(records)
      : records;
    renderConversation(
      $("flow-conversation"), state.flowConversationRecords, append);
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
const UNKNOWN_PROJECT_ROOT_LABEL = "未知项目";

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
  row.append(el("span", "history-project-select-label", "项目"));
  const select = el("select", "history-project-select");
  for (const b of buckets) {
    const opt = el("option", null, b.label);
    opt.value = b.project_root;
    opt.title = b.label;
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
      list.appendChild(el("p", "empty empty-loading-refresh", "正在刷新历史…"));
    } else if (emptyState === "loading-connect") {
      list.appendChild(
        el("p", "empty empty-loading-connect", "正在连接 / 正在等待历史数据…"));
    } else {
      list.appendChild(
        el("p", "empty empty-confirmed", "No history sessions reported."));
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
    list.appendChild(el("p", "history-refreshing", "正在刷新历史…"));
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
      s.task_description || s.flow_id || "(untitled session)");
    task.title = s.task_description || s.flow_id || "";
    const sc = statusClass(s.status);
    head.append(task, el("span", "badge badge-" + sc, s.status || "unknown"));
    if (s.active) head.appendChild(el("span", "badge badge-live", "● live"));
    card.appendChild(head);

    const meta = el("div", "history-item-meta");
    meta.append(
      el("span", null, s.machine_id || ""),
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

async function openHistorySession(flowId) {
  state.selectedHistoryId = flowId;
  state.historyRecords = [];
  renderHistoryList();
  $("history-detail-title").textContent = historyTitle(flowId);

  const detail = $("history-detail");
  detail.innerHTML = "";
  // Drop reconciliation state from the previously-selected session.
  detail.__convState = null;
  detail.appendChild(el("p", "empty", "Loading records…"));

  try {
    const resp = await fetch(`/api/history/${encodeURIComponent(flowId)}`);
    // The user may have clicked another session while this was in flight.
    if (state.selectedHistoryId !== flowId) return;
    if (!resp.ok) {
      detail.innerHTML = "";
      detail.appendChild(el("p", "empty",
        `Could not load history for this session (${resp.status}).`));
      return;
    }
    const data = await resp.json();
    if (state.selectedHistoryId !== flowId) return;
    // Preserve any `history_data` appends that arrived during the await (the
    // array was reset to [] beforehand, so its current contents are exactly
    // those appends) instead of discarding them with the snapshot assignment.
    const snapshot = Array.isArray(data.records) ? data.records : [];
    state.historyRecords = mergeSnapshotWithLiveAppends(
      snapshot, state.historyRecords);
    renderHistoryRecords(flowId, state.historyRecords);
    scrollHistoryToBottom();
  } catch (_) {
    if (state.selectedHistoryId !== flowId) return;
    detail.innerHTML = "";
    detail.appendChild(el("p", "empty", "Network error loading session history."));
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
    };
  }
  const msg = (rec.message && typeof rec.message === "object") ? rec.message : rec;
  const pick = (key) => (msg[key] != null ? msg[key] : rec[key]);

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
  const eventType = String(pick("type") || "").toLowerCase();
  if (eventType === "step_completed" || eventType === "step_failed") {
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
        || (eventType === "step_failed" ? "failed" : "completed"),
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

  return {
    role: role,
    content: content,
    timestamp: pick("timestamp") != null ? pick("timestamp") : pick("time"),
    stepType: pickStepType(),
    stepId: pick("step_id") || "",
    raw: { raw_json: rawJson, raw_ndjson: rawNdjson },
    attempt: pick("attempt"),
    partial: isPartial,
    toolUseId: toolUseId,
    isError: isError,
    toolDetail: toolDetail,
  };
}

// Stable identity key for a raw record, used to dedup a REST snapshot against
// `history_data` append deltas that arrived while the snapshot was in flight.
function recordKey(rec) {
  const n = normalizeRecord(rec);
  const content = typeof n.content === "string" ? n.content : "";
  return [
    n.stepId, n.role, String(n.timestamp), String(n.attempt),
    content.length, content.slice(0, 96),
  ].join("");
}

// Merge a freshly-fetched snapshot with append records that arrived during the
// fetch. The server's snapshot may have been built before a live-appended
// record was cached, so any live append not already present in the snapshot is
// preserved by appending it; appends already in the snapshot are dropped.
function mergeSnapshotWithLiveAppends(snapshot, liveAppends) {
  if (!liveAppends.length) return snapshot;
  const seen = new Set(snapshot.map(recordKey));
  const extra = liveAppends.filter((r) => !seen.has(recordKey(r)));
  return extra.length ? snapshot.concat(extra) : snapshot;
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

// One-line label for a collapsed chip, e.g. "system prompt · discovery".
function chipLabel(norm) {
  const role = String((norm && norm.role) || "message");
  const ctx = (norm && (norm.stepType || norm.stepId)) || "";
  return ctx ? `${role} prompt · ${ctx}` : `${role} prompt`;
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
  version_analyze: "VERSION ANALYZE",
  commit: "COMMIT",
  summarize: "SUMMARY",
};

// Resolve the conversation step-header label for a step type. Known step types
// map to their paradigm heading; unknown ones fall back to the original step
// key/label so the strict time order and separator rebuild are never broken.
function stepHeaderLabel(stepType, fallback) {
  const key = String(stepType || "").toLowerCase();
  if (key && STEP_HEADER_TITLES[key]) return STEP_HEADER_TITLES[key];
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
      el("p", "empty", "No conversation records for this session."));
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
      bubble = renderConversationRecord(norm);
    } catch (err) {
      try { console.warn("conversation record render failed", i, err); }
      catch (_) { /* console may be absent */ }
      bubble = el("div", "history-record conv-record role-error");
      bubble.appendChild(
        el("p", "md-p conv-empty", "(this record could not be rendered)"));
    }
    bubble.__convTs = tsValue(norm && norm.timestamp);
    bubble.__convIdx = i;
    bubble.__convStepKey = stepKey(norm || {});
    bubble.__convStepType = (norm && norm.stepType) || "";
    bubble.__convStepLabel = (norm && (norm.stepType || norm.stepId)) || "step";
    // Tag partial (stream-progress) bubbles so they can be folded away once the
    // turn's final result arrives (see removeSupersededProgress).
    bubble.__convPartial = !!(norm && norm.partial);
    if (bubble.__convPartial) {
      bubble.classList.add("conv-partial");
      bubble.__convTurnKey = progressTurnKey(norm);
    }
    insertBubbleSorted(container, bubble);
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
  rebuildStepHeaders(container);
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
  row.__partialInline = inline;
  // Per-bubble chip registry: keyed by tool_use_id, so the terminal
  // (tool_result) fragment for an id upgrades the SAME in-flight chip rather
  // than appending a new one.
  row.__chipRegistry = new Map();
  applyFragmentToBubble(row, norm);
  return row;
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

  let lastKey = null;
  const children = Array.from(container.children);
  for (const child of children) {
    if (child.__convStepKey === undefined) continue;
    if (child.__convStepKey !== lastKey) {
      const header = el("div", "history-step-header");
      // Use the paradigm step name (DISCOVERY / ANALYZE / …); unknown step
      // types fall back to the record's original label / step key.
      const title = stepHeaderLabel(
        child.__convStepType,
        child.__convStepLabel || child.__convStepKey,
      );
      header.appendChild(el("h5", "history-step-title", title));
      container.insertBefore(header, child);
      lastKey = child.__convStepKey;
    }
  }
}

function renderHistoryRecords(flowId, records, append) {
  renderConversation($("history-detail"), records, append);
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
    // Stop once we hit the appended detail panel; the head is always the
    // initial set of `<span>` children.
    const child = chip.firstChild;
    if (child.classList && child.classList.contains("tool-marker-details")) {
      break;
    }
    chip.removeChild(child);
  }
  const glyph = status === "success" ? "✓"
    : status === "failure" ? "✗" : "";
  // Insert head as the first children, ahead of any details panel.
  const refNode = chip.firstChild;
  if (glyph) {
    const g = el("span", "tool-marker-glyph", glyph);
    chip.insertBefore(g, refNode);
  }
  const n = el("span", "tool-marker-name", name);
  chip.insertBefore(n, refNode);
  if (header) {
    const d = el("span", "tool-marker-detail", header);
    chip.insertBefore(d, refNode);
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
  // Remove any prior details panel so an upgrade replaces, not duplicates.
  const old = Array.from(chip.children).filter(
    (c) => c.classList && c.classList.contains("tool-marker-details"));
  for (const o of old) chip.removeChild(o);
  if (!detail) return;
  const panel = el("div", "tool-marker-details" + (expanded ? " expanded" : " folded"));
  const toggle = el("button", "tool-marker-details-toggle",
    expanded ? "hide details" : "details");
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
    toggle.textContent = !open ? "hide details" : "details";
    if (!open) {
      try { requestAnimationFrame(() => panel.scrollIntoView({ block: "nearest" })); }
      catch (_) { /* RAF / DOM optional in test env */ }
    }
  });
  panel.append(toggle, body);
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
    frag.appendChild(el("p", "tool-detail-empty", "(no details available)"));
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
    wrap.appendChild(el("div", "diff-truncated", "… (output truncated)"));
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
    frag.appendChild(el("div", "diff-truncated", "… (output truncated)"));
  }
  return frag;
});

registerToolDetailRenderer("read_text", (detail) => {
  const frag = document.createDocumentFragment();
  if (detail.file_path) {
    frag.appendChild(el("div", "tool-marker-diff-path", detail.file_path));
  }
  frag.appendChild(renderTextWithLineNumbers(detail.text, detail.start_line));
  if (detail.truncated) {
    frag.appendChild(el("div", "diff-truncated", "… (output truncated)"));
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
    sw.appendChild(el("div", "tool-marker-bash-stderr-label", "stderr"));
    sw.appendChild(el("pre", "tool-marker-bash-stderr", String(detail.stderr)));
    frag.appendChild(sw);
  }
  if (detail.truncated) {
    frag.appendChild(el("div", "diff-truncated", "… (output truncated)"));
  }
  return frag;
});

function renderMatchList(items, emptyText) {
  const wrap = el("div", "tool-marker-matches");
  if (!items || !items.length) {
    wrap.appendChild(el("p", "tool-detail-empty", emptyText || "(no matches)"));
    return wrap;
  }
  for (const item of items) {
    wrap.appendChild(el("div", "tool-marker-match", String(item)));
  }
  return wrap;
}

registerToolDetailRenderer("grep_matches", (detail) => {
  const frag = document.createDocumentFragment();
  if (detail.pattern || detail.path) {
    const head = el("div", "tool-marker-diff-path");
    head.textContent = `pattern=${detail.pattern || ""} path=${detail.path || ""}`;
    frag.appendChild(head);
  }
  frag.appendChild(renderMatchList(detail.matches, "(no matches)"));
  if (detail.truncated) {
    frag.appendChild(el("div", "diff-truncated", "… (output truncated)"));
  }
  return frag;
});

registerToolDetailRenderer("glob_matches", (detail) => {
  const frag = document.createDocumentFragment();
  if (detail.pattern || detail.path) {
    const head = el("div", "tool-marker-diff-path");
    head.textContent = `pattern=${detail.pattern || ""} path=${detail.path || ""}`;
    frag.appendChild(head);
  }
  frag.appendChild(renderMatchList(detail.files, "(no files)"));
  if (detail.truncated) {
    frag.appendChild(el("div", "diff-truncated", "… (output truncated)"));
  }
  return frag;
});

registerToolDetailRenderer("text", (detail) => {
  const wrap = el("div", "tool-marker-text-plain");
  wrap.appendChild(el("pre", "tool-marker-text-pre", String(detail.text || "")));
  if (detail.truncated) {
    wrap.appendChild(el("div", "diff-truncated", "… (output truncated)"));
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
    const oldS = String(input.old_string || "");
    const newS = String(input.new_string || "");
    const ol = oldS ? oldS.split("\n").length : 0;
    const nl = newS ? newS.split("\n").length : 0;
    if (ol || nl) return `${p} (${ol} lines → ${nl} lines)`;
    return p;
  }
  if (toolName === "Write") {
    const p = String(input.file_path || "?");
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
    if (typeof v === "string") vs = _toolTruncatePreview(v, 30);
    else if (typeof v === "number" || typeof v === "boolean") vs = String(v);
    else {
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
    const ol = String(input.old_string || "").split("\n").length;
    const nl = String(input.new_string || "").split("\n").length;
    return `${p} (${ol} lines → ${nl} lines)`;
  }
  if (toolName === "Write") {
    const p = String(input.file_path || "?");
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
    const oldS = String(input.old_string || "");
    const newS = String(input.new_string || "");
    const fp = String(input.file_path || "?");
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
    const content = String(input.content || "");
    return {
      kind: "write_full",
      file_path: String(input.file_path || "?"),
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
  commit: ["committed", "commit_hash", "commit_message"],
  version_analyze: ["current_version", "suggested_version", "bump_type",
    "confidence", "reasoning"],
  summarize: ["summary"],
  discovery: ["content", "refined_description", "questions"],
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
          if (beforeMatch || afterMatch || isTrailing) {
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
      el("span", "step-report__title", "Proposed Task Description"),
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
    qWrap.appendChild(el("h6", "discovery-questions__title", "Questions"));
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
  if (n < 1024) return n + " chars";
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

  const collapsedLabel = `▸ 展开全部 (${formatSize(text.length)})`;
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
    btn.textContent = expanded ? "▾ 收起" : collapsedLabel;
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
  const btn = el("button", "raw-toggle", "查看原始");
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
    btn.textContent = shown ? `隐藏原始 (${kind})` : "查看原始";
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
  const btn = el("button", "raw-toggle", "查看原始");
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
      ? `隐藏原始 (${hasRaw ? kind : "content"})`
      : "查看原始";
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
    sec.appendChild(el("h6", "user-prompt-chip__section-title", "模板前缀"));
    sec.appendChild(el("pre", "conv-plain", split.prefix));
    target.appendChild(sec);
  }
  if (hasSuffix) {
    const sec = el("div", "user-prompt-chip__section");
    sec.appendChild(el("h6", "user-prompt-chip__section-title", "框架后缀"));
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
  const btn = el("button", "process-toggle", "▸ 展开全部");
  btn.type = "button";
  const full = el("div", "process-full hidden");
  let built = false;
  let expanded = false;
  btn.addEventListener("click", () => {
    expanded = !expanded;
    if (expanded && !built) {
      appendPromptSubsections(full, split);
      // Layer 3 nests inside the expanded Layer-2 area, after the prefix /
      // suffix subsections — never visible in the default Layer-1 bubble.
      const rawToggle = makeRawToggle(norm);
      if (rawToggle) full.appendChild(rawToggle);
      built = true;
    }
    full.classList.toggle("hidden", !expanded);
    wrap.classList.toggle("folded", !expanded);
    wrap.classList.toggle("expanded", expanded);
    btn.textContent = expanded ? "▾ 收起全部" : "▸ 展开全部";
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
  } else {
    // No result JSON this turn (or the body could not be structured): keep the
    // full thinking process shown inline, never folded or contracted to empty.
    frag.appendChild(renderAssistantProcessInline(content, norm));
  }
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
    if (!content) {
      bubble.appendChild(
        el("p", "md-p conv-empty", "(no readable content for this record)"));
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
        const rawToggle = makeRawToggle(norm);
        if (rawToggle) detail.appendChild(rawToggle);
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

  // assistant / other: expanded by default. The "查看原始" raw toggle is NOT
  // appended at the row level — for an assistant result turn it is the single
  // fold built by `renderAssistantBubble` → `makeAssistantRawToggle` (below the
  // rendered result), so the default Layer-1 view stays clean (per the message
  // paradigm).
  row.appendChild(renderRecordHead(norm));
  row.appendChild(buildBubble());
  return row;
}

// ---------------------------------------------------------------------------
// Step event records (step_completed / step_failed)
// ---------------------------------------------------------------------------

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

  const verb = isFailed ? "Step failed" : "Step completed";
  const icon = isFailed ? "✗" : "✓";
  const stepLabel = norm.stepType
    ? (STEP_REPORT_TITLES[String(norm.stepType).toLowerCase()] || norm.stepType)
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
  const hasPrefix = typeof split.prefix === "string" && split.prefix.length > 0;
  const hasSuffix = typeof split.suffix === "string" && split.suffix.length > 0;

  if (hasContent) {
    // Layer 1 — default-expanded bubble carrying ONLY the user's real input.
    // Literal text is preserved so the exact body the LLM saw is reproduced.
    const bubble = el("div", "conv-bubble user-content-bubble");
    bubble.appendChild(el("pre", "conv-plain", split.content));
    row.appendChild(bubble);

    // Layer 2 — "展开全部" toggle revealing the 模板前缀 / 框架后缀 subsections, with
    // Layer 3 ("查看原始") nested at the end of its expand area. Collapsed by
    // default so neither the framework boilerplate nor the raw toggle shows in
    // the default view. Offered whenever there is boilerplate OR a raw payload
    // to reach — the raw toggle is no longer a row-level always-visible control.
    if (hasPrefix || hasSuffix || hasRawPayload(norm)) {
      row.appendChild(makeUserPromptToggle(split, norm));
    }
  } else {
    // Empty user-content (legacy two-segment / prefix+suffix sandwich):
    // degrade to a single default-collapsed system-prompt chip combining the
    // prefix and suffix subsections (and the nested "查看原始" raw toggle), with
    // no user bubble.
    const label = `system prompt · ${ctx}`;
    const chipWrap = el("div", "msg-chip-wrap collapsed user-prompt-chip");
    const chip = el("button", "msg-chip", "▸ " + label);
    chip.type = "button";
    const chipDetail = el("div", "msg-chip-detail");
    let chipBuilt = false;
    let chipExpanded = false;
    chip.addEventListener("click", () => {
      chipExpanded = !chipExpanded;
      if (chipExpanded && !chipBuilt) {
        appendPromptSubsections(chipDetail, split);
        // Layer 3 — nested inside the chip's expand detail, never row-level.
        const rawToggle = makeRawToggle(norm);
        if (rawToggle) chipDetail.appendChild(rawToggle);
        chipBuilt = true;
      }
      chipWrap.classList.toggle("collapsed", !chipExpanded);
      chip.textContent = (chipExpanded ? "▾ " : "▸ ") + label;
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
  version_analyze: "Version Analysis",
  commit: "Commit",
  summarize: "Work Summary",
};

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
  const title =
    "Report — " + (STEP_REPORT_TITLES[stepType] || stepType || "Step");
  return makeReportCard(stepType || "unknown", title, () => {
    const body = renderer(step, step.outputs || {});
    const frag = document.createDocumentFragment();
    if (body instanceof Node) frag.appendChild(body);
    if (step.error_message) {
      frag.appendChild(el("div", "step-report__error",
        "Error: " + String(step.error_message)));
    }
    return frag;
  });
}

// -- shared report-card building blocks --

function reportEmpty(text) {
  return el("p", "step-report__empty", text || "(no report fields)");
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
  for (const item of items) {
    const li = el("li");
    const out = formatItem ? formatItem(item) : null;
    if (out instanceof Node) li.appendChild(out);
    else if (typeof out === "string") li.textContent = out;
    else if (typeof item === "string") li.textContent = item;
    else li.textContent = safeStringify(item);
    ul.appendChild(li);
  }
  return ul;
}

// -- default fallback (parity with step_renderers.py:_default_render) -------

function renderDefaultReport(step, outputs) {
  const frag = document.createDocumentFragment();
  const entries = outputs && typeof outputs === "object"
    ? Object.entries(outputs) : [];
  if (!entries.length) {
    frag.appendChild(reportEmpty("(step produced no outputs)"));
  } else {
    const kv = el("div", "step-report__kv");
    for (const [k, v] of entries) {
      const r = el("div", "step-report__kv-row");
      r.append(el("span", "step-report__kv-k", k));
      const valEl = el("span", "step-report__kv-v");
      if (typeof v === "string" && v.length > 300) {
        valEl.textContent = v.slice(0, 200).replace(/\n/g, " ") + "…";
        valEl.title = `${v.length} chars`;
      } else if (typeof v === "string") {
        valEl.textContent = v;
      } else {
        valEl.textContent = safeStringify(v);
      }
      r.appendChild(valEl);
      kv.appendChild(r);
    }
    frag.appendChild(kv);
  }
  const status = step.status && String(step.status).toLowerCase();
  if (status && status !== "completed" && status !== "running") {
    frag.appendChild(el("div", "step-report__muted", "Status: " + status));
  }
  return frag;
}

// -- analyze (parity with step_renderers.py:_render_analyze) ----------------

function renderAnalyzeReport(step, outputs) {
  const frag = document.createDocumentFragment();
  frag.appendChild(reportStatusBar([
    `task: ${outputs.task_type || "N/A"}`,
    `complexity: ${outputs.complexity || "N/A"}`,
    `scope: ${outputs.scope || "N/A"}`,
  ]));
  if (outputs.reasoning) {
    frag.appendChild(reportSection("Reasoning", String(outputs.reasoning)));
  }
  const items = (Array.isArray(outputs.selected_items) && outputs.selected_items.length)
    ? outputs.selected_items
    : (Array.isArray(outputs.relevant_specs) ? outputs.relevant_specs : []);
  if (items.length) {
    frag.appendChild(reportSection(`Relevant Spec Items (${items.length})`,
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

function renderPlanReport(step, outputs) {
  const frag = document.createDocumentFragment();
  const plan = outputs.plan && typeof outputs.plan === "object"
    ? outputs.plan : {};
  const proposal = plan.proposal;
  const design = plan.design;
  const groups = Array.isArray(outputs.task_groups) ? outputs.task_groups : [];

  if (proposal && typeof proposal === "object") {
    frag.appendChild(reportSection("Proposal", renderStructured(proposal)));
  } else if (typeof proposal === "string" && proposal) {
    frag.appendChild(reportSection("Proposal", proposal));
  }
  if (design && typeof design === "object") {
    frag.appendChild(reportSection("Design", renderStructured(design)));
  } else if (typeof design === "string" && design) {
    frag.appendChild(reportSection("Design", design));
  }
  if (groups.length) {
    frag.appendChild(reportSection(`Task Groups (${groups.length})`,
      reportList(groups, (g) => {
        const tasks = Array.isArray(g && g.tasks) ? g.tasks : [];
        const totalLoc = tasks.reduce(
          (s, t) => s + (Number(t && t.estimated_loc) || 0), 0);
        const deps = Array.isArray(g && g.depends_on) && g.depends_on.length
          ? g.depends_on.join(", ") : "none";
        const row = el("span", "step-report__group-row");
        row.append(
          el("span", "step-report__group-id", String(g.group_id || "?")),
          el("span", "step-report__group-name", " " + String(g.name || "")),
          el("span", "step-report__muted",
            `  · ${tasks.length} tasks · ~${totalLoc} LOC · depends: ${deps}`),
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
    el("span", "step-report__label " + cls, status),
  );
  if (implGroups.length) {
    bar.append(el("span", "step-report__sep", "│"),
      el("span", null, `${implGroups.length} groups`));
  }
  bar.append(
    el("span", "step-report__sep", "│"),
    el("span", null, `${filesChanged.length} files`),
  );
  if (testsAdded.length) {
    bar.append(el("span", "step-report__sep", "│"),
      el("span", null, `${testsAdded.length} tests`));
  }
  frag.appendChild(bar);

  if (summary) {
    const parts = String(summary).split(";").map((s) => s.trim()).filter(Boolean);
    if (parts.length <= 1) {
      frag.appendChild(reportSection("Summary", parts[0] || String(summary)));
    } else {
      frag.appendChild(reportSection("Summary",
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
      `Files Changed (${filesChanged.length})`, wrap));
  }

  if (testsAdded.length) {
    frag.appendChild(reportSection(`Tests Added (${testsAdded.length})`,
      reportList(testsAdded, (t) => document.createTextNode("+ " + String(t)))));
  }

  if (incomplete.length) {
    frag.appendChild(reportSection(`Incomplete Tasks (${incomplete.length})`,
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
        `Restricted edits applied: ${restrictedApplied.length}`));
    }
    if (restrictedFailed.length) {
      body.appendChild(el("div", "step-report__warn",
        `Restricted edits failed: ${restrictedFailed.length}`));
      body.appendChild(reportList(restrictedFailed, (e) => {
        if (e && typeof e === "object") {
          return document.createTextNode(String(
            e.file || e.file_path || e.path || safeStringify(e)));
        }
        return document.createTextNode(String(e));
      }));
    }
    frag.appendChild(reportSection("Restricted Edits", body));
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
    overall ? "PASSED" : "FAILED"));
  const phases = Array.isArray(results.phases) ? results.phases : [];
  if (phases.length) {
    const passed = phases.filter((p) => p && p.passed).length;
    bar.append(el("span", "step-report__sep", "│"),
      el("span", null, `${passed} / ${phases.length} phases`));
  }
  frag.appendChild(bar);

  if (phases.length) {
    frag.appendChild(reportSection("Phases", reportList(phases, (p) => {
      const ok = !!(p && p.passed);
      const row = el("span");
      row.appendChild(el("span", "step-report__icon " + (ok ? "ok" : "fail"),
        ok ? "✓" : "✗"));
      row.appendChild(document.createTextNode(" " + (p && p.name || "?")));
      return row;
    })));
  }
  if (results.command) {
    frag.appendChild(reportSection("Command", String(results.command)));
  }
  return frag;
}

// -- self_check (parity with step_renderers.py:_render_self_check) ----------

function renderSelfCheckReport(step, outputs) {
  const frag = document.createDocumentFragment();
  const actionable = outputs.actionable_count != null
    ? Number(outputs.actionable_count) : 0;
  const issues = Array.isArray(outputs.issues) ? outputs.issues : [];
  const status = String(step.status || "").toLowerCase();

  const bar = el("div", "step-report__status-bar");
  if (status === "failed") {
    bar.appendChild(el("span", "step-report__label fail", "✗ FAILED"));
  } else if (actionable === 0) {
    bar.appendChild(el("span", "step-report__label ok", "✓ PASSED"));
  } else {
    bar.appendChild(el("span", "step-report__label fail",
      `✗ ${actionable} actionable issue(s)`));
  }
  frag.appendChild(bar);

  const result = outputs.self_check_result;
  const summary = result && typeof result === "object" ? result.summary : "";
  if (summary) frag.appendChild(reportSection("Summary", String(summary)));

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
      frag.appendChild(reportSection(`${sev} (${grp.length})`,
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
    bar.appendChild(el("span", "step-report__label ok", "✓ PASSED"));
  } else if (verified === false) {
    bar.appendChild(el("span", "step-report__label fail", "✗ FAILED"));
  } else {
    bar.appendChild(el("span", "step-report__label muted", "?"));
  }
  frag.appendChild(bar);

  const vRes = outputs.verification_result;
  const summary = outputs.summary
    || (vRes && typeof vRes === "object" ? vRes.summary : "");
  if (summary) frag.appendChild(reportSection("Summary", String(summary)));

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
      ["In-scope", "in_scope"],
      ["Out-of-scope", "out_of_scope"],
    ];
    for (const [label, key] of scopes) {
      const grp = byScope[key];
      if (!grp || !grp.length) continue;
      frag.appendChild(reportSection(`${label} (${grp.length})`,
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
    frag.appendChild(reportSection("Recommendations",
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
    frag.appendChild(reportEmpty("No spec updates needed"));
    return frag;
  }
  if (specs.length) {
    frag.appendChild(reportSection(`Updated Specs (${specs.length})`,
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
    frag.appendChild(reportSection("New Capabilities",
      reportList(caps, (c) => document.createTextNode(String(c)))));
  }
  return frag;
}

// -- commit (parity with step_renderers.py:_render_commit) ------------------

function renderCommitReport(step, outputs) {
  const frag = document.createDocumentFragment();
  if (!outputs.committed) {
    frag.appendChild(reportEmpty("No changes to commit"));
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
    frag.appendChild(reportSection("Commit Message",
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
  frag.appendChild(el("div", "step-report__muted",
    `${outputs.bump_type || "?"} bump  │  confidence: ${outputs.confidence || "?"}`));
  if (outputs.reasoning) {
    frag.appendChild(reportSection("Reasoning", String(outputs.reasoning)));
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
    frag.appendChild(reportEmpty("(no summary)"));
  }
  return frag;
}

// -- discovery (parity with CLI default; outputs vary by mode) --------------

function renderDiscoveryReport(step, outputs) {
  const frag = document.createDocumentFragment();
  if (outputs.refined_description) {
    frag.appendChild(reportSection("Refined Description",
      String(outputs.refined_description)));
  }
  const dState = outputs.discovery_state;
  const mode = (dState && typeof dState === "object" && dState.mode)
    || outputs.mode;
  if (mode) frag.appendChild(reportSection("Mode", String(mode)));
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
};

// Build the role / attempt / timestamp header line for one record.
function renderRecordHead(norm) {
  const head = el("div", "history-record-head");
  head.appendChild(el("span", "record-role", norm.role));
  const right = el("div", "record-head-right");
  if (norm.attempt != null && norm.attempt !== "" && Number(norm.attempt) > 1) {
    right.appendChild(el("span", "record-attempt", "attempt " + norm.attempt));
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
    select.appendChild(new Option("(no machines connected)", ""));
  } else {
    for (const m of online) {
      select.appendChild(new Option(m.hostname || m.machine_id, m.machine_id));
    }
  }
  $("nt-task").value = "";
  $("nt-discover").checked = false;
  $("nt-error").classList.add("hidden");
  $("nt-submit").disabled = false;
  const manualInput = $("nt-project-manual");
  if (manualInput) manualInput.value = "";
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
  const manualOption = new Option("Other path…", PROJECT_MANUAL_SENTINEL);

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
  const placeholder = new Option("(select a project…)", "");
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
  const projectSelectValue = $("nt-project").value.trim();

  if (!machineId) return showFormError(errBox, "Select a target machine.");
  if (!task) return showFormError(errBox, "Task description must not be empty.");
  if (!projectSelectValue) {
    return showFormError(errBox, "Select a project root for this task.");
  }
  let projectRoot;
  if (projectSelectValue === PROJECT_MANUAL_SENTINEL) {
    const manualInput = $("nt-project-manual");
    projectRoot = (manualInput && manualInput.value.trim()) || "";
    if (!projectRoot) {
      return showFormError(errBox, "Enter an absolute path for the project.");
    }
    if (!isValidAbsolutePath(projectRoot)) {
      return showFormError(
        errBox,
        "Project path must be absolute (start with '/').",
      );
    }
  } else {
    projectRoot = projectSelectValue;
  }

  const submit = $("nt-submit");
  submit.disabled = true;
  try {
    const resp = await fetch("/api/flows", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        machine_id: machineId,
        task: task,
        task_type: taskType,
        discover: discover,
        project_root: projectRoot,
      }),
    });
    if (resp.status === 202) {
      closeNewTask();
      showToast("success", "Task published.");
    } else {
      const detail = await resp.json().catch(() => ({}));
      const message = detail.detail || `Server returned ${resp.status}.`;
      showFormError(errBox, message);
      showToast("error", `Could not publish task: ${message}`);
      submit.disabled = false;
    }
  } catch (err) {
    showFormError(errBox, "Network error — could not reach the server.");
    showToast("error", "Could not publish task — network error.");
    submit.disabled = false;
  }
}

function showFormError(node, message) {
  node.textContent = message;
  node.classList.remove("hidden");
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

function init() {
  $("new-task-btn").addEventListener("click", openNewTask);
  $("new-task-close").addEventListener("click", closeNewTask);
  $("new-task-form").addEventListener("submit", submitNewTask);
  $("nt-machine").addEventListener("change", refreshProjectOptions);
  $("nt-project").addEventListener("change", updateManualPathVisibility);

  $("flow-view-close").addEventListener("click", closeFlowView);

  $("history-btn").addEventListener("click", openHistory);
  $("history-close").addEventListener("click", closeHistory);

  $("flow-reply-form").addEventListener("submit", submitReply);
  $("flow-interject-btn").addEventListener("click", onInterjectButtonClick);
  // Ctrl/Cmd+Enter submits the reply box without leaving the textarea.
  $("flow-reply-input").addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      $("flow-reply-form").requestSubmit();
    }
  });

  // Click the modal backdrop to dismiss.
  $("new-task-modal").addEventListener("click", (e) => {
    if (e.target.id === "new-task-modal") closeNewTask();
  });

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

  connect();
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
    STEP_HEADER_TITLES,
    stepHeaderLabel,
    hasRawPayload,
    STEP_REPORT_RENDERERS,
    STEP_ASSISTANT_RENDERERS,
    registerAssistantRenderer,
    renderDiscoveryAssistant,
    makeStructuredAssistantRenderer,
    renderAssistantBubble,
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
    buildPartialBubble,
    appendPartialFragment,
    // Incremental conversation reconciliation + chip refresh (exposed for the
    // DOM-stub tests in tests/frontend/test_app_pure.mjs).
    renderConversation,
    addConversationRecords,
    insertBubbleSorted,
    rebuildStepHeaders,
    markSupersededProgress,
    partialSegments,
    progressTurnKey,
    renderConversationRecord,
    renderInterventions,
    reconcileReplyTarget,
    tsValue,
    stepKey,
    recordKey,
    mergeSnapshotWithLiveAppends,
    // History list rendering + shared mutable state (exposed for the DOM-stub
    // tests in tests/frontend/test_app_pure.mjs).
    renderHistoryList,
    historyListEmptyState,
    daemonConnected,
    UNKNOWN_PROJECT_ROOT,
    UNKNOWN_PROJECT_ROOT_LABEL,
    groupHistorySessionsByProjectRoot,
    pickDefaultHistoryProjectRoot,
    state,
  };
}