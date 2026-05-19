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
  historySessions: [],    // [{flow_id, task_description, status, updated_at, ...}]
  selectedHistoryId: null,// flow whose records are shown in the history detail
  historyRecords: [],     // records currently rendered in the history detail
  connStale: false,       // true while the WS is down — data may be stale
  detailLoaded: false,    // true once the open flow's detail has rendered
  detailFetchFailures: 0, // consecutive /api/flows/{id} failures for the view
};

let ws = null;
let reconnectAttempts = 0;
let detailPollTimer = null;

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

function pendingCalls(flow) {
  return flow && Array.isArray(flow.pending_calls) ? flow.pending_calls : [];
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
  state.detailLoaded = false;
  state.detailFetchFailures = 0;

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

function closeFlowView() {
  state.selectedFlowId = null;
  state.flowDetail = null;
  state.flowMachineId = null;
  state.flowConversationRecords = [];
  state.flowInterventions = [];
  state.flowReplyTargetId = null;
  $("flow-view").classList.add("hidden");
  if (detailPollTimer) {
    clearInterval(detailPollTimer);
    detailPollTimer = null;
  }
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

const KIND_META = {
  call: {
    label: "MCP call",
    hint: "A pending MCP call is awaiting your response.",
    icon: "⚙",
  },
  interjection: {
    label: "Interjection",
    hint: "Send an additional instruction into the running flow.",
    icon: "✎",
  },
  retry_decision: {
    label: "Retry decision",
    hint: "A step failed — reply with how to proceed (e.g. retry / skip / abort).",
    icon: "↻",
  },
  cli_confirm: {
    label: "CLI confirmation",
    hint: "The CLI subprocess is waiting for a confirmation.",
    icon: "⌨",
  },
};

// Canonicalize a raw `kind` field; unknown kinds degrade to a plain "call".
function normalizeKind(kind) {
  const k = String(kind || "call").toLowerCase();
  return KIND_META[k] ? k : "call";
}

// Derive the ordered list of intervention entries for a flow. Each pending
// call becomes one entry; for an active flow with no interjection already
// pending, a synthetic interjection entry is always appended so the chat box
// can be used to interject at any time. Pure: depends only on `flow`.
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
  if (isActiveFlow(flow) && !hasInterjection) {
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

// Rebuild the intervention region and re-sync the reply box. Called from the
// 3s detail poll; selection (`flowReplyTargetId`) and the typed-but-unsent
// reply text are deliberately preserved across rebuilds.
function renderInterventions(flow) {
  const region = $("flow-interventions");
  region.innerHTML = "";
  const entries = computeInterventions(flow);
  state.flowInterventions = entries;

  // Keep the prior selection if it still exists; otherwise prefer the first
  // real pending call (a call needing an answer) over the synthetic
  // interjection, falling back to whatever is first.
  if (!entries.some((e) => e.id === state.flowReplyTargetId)) {
    const firstCall = entries.find((e) => !e.synthetic);
    state.flowReplyTargetId = (firstCall || entries[0] || {}).id || null;
  }

  for (const entry of entries) {
    region.appendChild(renderInterventionItem(entry));
  }
  updateReplyBox(flow);
}

// Render one intervention entry as a prominent, default-expanded card. The
// card body shows the kind, prompt, optional context, and any options. The
// whole card is a click target that selects it as the reply box's target.
function renderInterventionItem(entry) {
  const meta = KIND_META[entry.kind] || KIND_META.call;
  const card = el("div", "intervention kind-" + entry.kind);
  if (entry.id === state.flowReplyTargetId) card.classList.add("selected");

  const head = el("div", "intervention-head");
  head.append(
    el("span", "intervention-icon", meta.icon),
    el("span", "intervention-kind", meta.label),
  );
  if (entry.callId) {
    const cid = el("span", "intervention-callid", entry.callId);
    cid.title = "call id: " + entry.callId;
    head.appendChild(cid);
  }
  card.appendChild(head);

  if (entry.prompt) {
    const prompt = el("div", "intervention-prompt");
    prompt.appendChild(renderMarkdown(entry.prompt));
    card.appendChild(prompt);
  } else {
    card.appendChild(el("div", "intervention-hint", meta.hint));
  }

  if (entry.context != null && entry.context !== "") {
    const ctx = el("div", "intervention-context");
    ctx.append(
      el("span", "intervention-context-label", "context"),
      el("pre", "intervention-context-body",
        typeof entry.context === "string"
          ? entry.context
          : safeStringify(entry.context)),
    );
    card.appendChild(ctx);
  }

  if (entry.options.length) {
    const opts = el("div", "intervention-options");
    for (const opt of entry.options) {
      const optText = optionText(opt);
      const btn = el("button", "intervention-option", optionLabel(opt));
      btn.type = "button";
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        // An option is a one-click reply: select this entry and send it.
        state.flowReplyTargetId = entry.id;
        sendReply(state.selectedFlowId, entry, optText);
      });
      opts.appendChild(btn);
    }
    card.appendChild(opts);
  }

  // Clicking anywhere on the card (outside an option button) targets it.
  card.addEventListener("click", () => {
    if (state.flowReplyTargetId === entry.id) return;
    state.flowReplyTargetId = entry.id;
    if (state.flowDetail) renderInterventions(state.flowDetail);
    $("flow-reply-input").focus();
  });

  return card;
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

// Sync the docked reply box to the current intervention selection: enable it
// and label its target when there is a pending interaction, disable it with an
// explanatory line when there is none.
function updateReplyBox(flow) {
  const entries = state.flowInterventions || [];
  const input = $("flow-reply-input");
  const submit = $("flow-reply-submit");
  const ctx = $("flow-reply-context");

  if (!entries.length) {
    input.disabled = true;
    submit.disabled = true;
    input.placeholder = "No pending interaction…";
    ctx.textContent = isActiveFlow(flow)
      ? "No pending interaction right now — nothing to respond to."
      : "This flow has ended — no further interaction is possible.";
    ctx.className = "flow-reply-context";
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
    ? "Type an instruction to interject into this running flow…"
    : "Type your response to this call…";

  ctx.className = "flow-reply-context active";
  ctx.innerHTML = "";
  ctx.append(
    el("span", "flow-reply-to", "Replying to"),
    el("span", "flow-reply-kind kind-" + target.kind, meta.label),
  );
  const where = target.callId
    ? ` · call ${target.callId}`
    : (target.kind === "interjection" ? " · running flow" : "");
  if (where) ctx.appendChild(el("span", "flow-reply-where", where));
  if (target.prompt) {
    ctx.appendChild(el("span", "flow-reply-preview",
      " — " + truncate(target.prompt, 120)));
  }
}

function resetReplyBox() {
  const input = $("flow-reply-input");
  input.value = "";
  input.disabled = true;
  $("flow-reply-submit").disabled = true;
  const ctx = $("flow-reply-context");
  ctx.className = "flow-reply-context";
  ctx.textContent = "No pending interaction right now.";
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
  submit.disabled = true;
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
    // Re-enable per the live intervention state — the box may now have no
    // pending target (e.g. the call was answered) and should stay disabled.
    if (state.selectedFlowId === flowId && state.flowDetail) {
      updateReplyBox(state.flowDetail);
    } else {
      submit.disabled = false;
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
}

async function fetchHistoryIndex() {
  try {
    const resp = await fetch("/api/history");
    if (!resp.ok) return;
    const data = await resp.json();
    if (Array.isArray(data.sessions)) {
      state.historySessions = data.sessions;
      renderHistoryList();
    }
  } catch (_) {
    /* transient — a WS history_index push will refresh it */
  }
}

// Push handler: the daemon's full session index, rebroadcast by the server.
function applyHistoryIndex(sessions) {
  state.historySessions = sessions;
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

function renderHistoryList() {
  const list = $("history-list");
  list.innerHTML = "";
  const sessions = state.historySessions || [];
  if (!sessions.length) {
    list.appendChild(el("p", "empty", "No history sessions reported."));
    return;
  }
  for (const s of sessions) {
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

// Pull the last assistant message's last text block out of a parsed NDJSON
// stream (`raw_json` is `list[dict]`, one dict per NDJSON line).
function extractAssistantText(rawJson) {
  if (!Array.isArray(rawJson)) return "";
  let text = "";
  for (const line of rawJson) {
    if (!line || typeof line !== "object" || line.type !== "assistant") continue;
    const content = line.message && Array.isArray(line.message.content)
      ? line.message.content
      : null;
    if (!content) continue;
    for (const block of content) {
      if (block && block.type === "text" && typeof block.text === "string") {
        text = block.text;
      }
    }
  }
  return text;
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

  let role = String(pick("role") || msg.type || "log").toLowerCase();
  if (!["user", "assistant", "system"].includes(role)) {
    role = role === "human" ? "user" : (role || "log");
  }

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

  return {
    role: role,
    content: content,
    timestamp: pick("timestamp") != null ? pick("timestamp") : pick("time"),
    stepType: pick("step_type") || "",
    stepId: pick("step_id") || "",
    raw: { raw_json: rawJson, raw_ndjson: rawNdjson },
    attempt: pick("attempt"),
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
// History records rendering
// ---------------------------------------------------------------------------
//
// Records carry a step identifier; group them so each step's conversation is
// shown under its own heading, ordered within the group by timestamp.
function stepKey(norm) {
  return String(norm.stepId || norm.stepType || "step");
}

// Render a flat list of raw records into `container` as a CLI-style, step-
// grouped conversation. Shared verbatim by the history view and the running-
// flow view so both present identical grouping / bubbles / folding.
//
// Incremental updates: an active flow streams `history_data` appends every LLM
// turn. A full rebuild on each append would recreate every `makeFoldable` /
// `makeRawToggle` / chip in its default collapsed state, collapsing a record
// the reader had just expanded. So when `append` is set, only the new tail
// records are built and inserted into the existing DOM — bubbles already on
// screen (and any folds / chips / raw panels the reader opened) are untouched.
//
// Per-container reconciliation state lives on `container.__convState`:
//   { count: number of raw records already rendered,
//     sections: Map<stepKey, sectionElement> }
function renderConversation(container, records, append) {
  const st = container.__convState;
  if (append && st && st.count > 0 && records.length >= st.count) {
    // Incremental append: build only the records past what is on screen.
    if (records.length > st.count) {
      addConversationRecords(container, st, records, st.count);
      st.count = records.length;
    }
    return;
  }
  // Full (re)build — initial open, snapshot replace, or a non-append push.
  container.innerHTML = "";
  const fresh = { count: 0, sections: new Map() };
  container.__convState = fresh;
  if (!records.length) {
    container.appendChild(
      el("p", "empty", "No conversation records for this session."));
    return;
  }
  addConversationRecords(container, fresh, records, 0);
  fresh.count = records.length;
}

// Build records `records[startIndex..]` and merge them into `container`,
// grouping by step. New steps get a fresh section appended in arrival order;
// records joining an existing step are inserted in timestamp order so a late
// delta cannot land out of sequence. Existing DOM is never destroyed.
function addConversationRecords(container, st, records, startIndex) {
  for (let i = startIndex; i < records.length; i++) {
    const norm = normalizeRecord(records[i]);
    const key = stepKey(norm);
    let section = st.sections.get(key);
    if (!section) {
      section = el("div", "history-step");
      section.appendChild(el("h5", "history-step-title",
        norm.stepType || norm.stepId || "step"));
      st.sections.set(key, section);
      container.appendChild(section);
    }
    const bubble = renderConversationRecord(norm);
    // `__convTs` / `__convIdx` order bubbles within a step; `__convIdx` (the
    // record's absolute position) is the stable tiebreaker for equal timestamps.
    bubble.__convTs = tsValue(norm.timestamp);
    bubble.__convIdx = i;
    insertBubbleSorted(section, bubble);
  }
}

// Insert `bubble` into `section` keeping bubbles ordered by (__convTs,
// __convIdx). The leading `h5` title has no `__convIdx` and is skipped.
function insertBubbleSorted(section, bubble) {
  let ref = null;
  for (const child of section.children) {
    if (child.__convIdx === undefined) continue;
    if (child.__convTs > bubble.__convTs ||
        (child.__convTs === bubble.__convTs &&
         child.__convIdx > bubble.__convIdx)) {
      ref = child;
      break;
    }
  }
  section.insertBefore(bubble, ref);
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

// Render one `[Name: detail]` marker as a standalone, visually distinct block.
function renderToolBlock(name, raw) {
  const inner = raw.replace(/^\[/, "").replace(/\]$/, "");
  const colon = inner.indexOf(":");
  const detail = colon >= 0 ? inner.slice(colon + 1).trim() : "";
  const block = el("div", "tool-marker");
  block.appendChild(el("span", "tool-marker-name", name));
  if (detail) block.appendChild(el("span", "tool-marker-detail", detail));
  return block;
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

// Build a "view raw" control for one record: a small button plus a hidden
// formatted-JSON block showing the pre-normalization raw_json / raw_ndjson.
// Hidden by default — the default view stays human-readable. Returns null
// when the record carries no raw payload.
function makeRawToggle(norm) {
  const raw = norm.raw || {};
  let payload = null;
  let kind = "";
  if (raw.raw_json != null &&
      !(Array.isArray(raw.raw_json) && raw.raw_json.length === 0)) {
    payload = raw.raw_json;
    kind = "raw_json";
  } else if (raw.raw_ndjson != null && raw.raw_ndjson !== "") {
    payload = raw.raw_ndjson;
    kind = "raw_ndjson";
  }
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
  });
  wrap.append(btn, pre);
  return wrap;
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
  const known = ["user", "assistant", "system"].includes(norm.role);
  const role = known ? norm.role : "other";
  const row = el("div", "history-record conv-record role-" + role);

  const content = typeof norm.content === "string" ? norm.content : "";

  // Build the inner bubble lazily so a collapsed chip pays nothing until the
  // reader expands it.
  const buildBubble = () => {
    const bubble = el("div", "conv-bubble");
    if (!content) {
      bubble.appendChild(
        el("p", "md-p conv-empty", "(no readable content for this record)"));
    } else if (role === "assistant") {
      // assistant: tool-marker split + Markdown, rebuilt lazily on expand.
      const buildFull = () => {
        const frag = document.createDocumentFragment();
        for (const node of renderToolMarkers(content)) frag.appendChild(node);
        return frag;
      };
      bubble.appendChild(makeFoldable(buildFull, content));
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
    });
    wrap.append(chip, detail);
    row.appendChild(wrap);
    return row;
  }

  // assistant / other: expanded by default.
  row.appendChild(renderRecordHead(norm));
  row.appendChild(buildBubble());
  const rawToggle = makeRawToggle(norm);
  if (rawToggle) row.appendChild(rawToggle);
  return row;
}

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
  $("new-task-modal").classList.remove("hidden");
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

  if (!machineId) return showFormError(errBox, "Select a target machine.");
  if (!task) return showFormError(errBox, "Task description must not be empty.");

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

  $("flow-view-close").addEventListener("click", closeFlowView);

  $("history-btn").addEventListener("click", openHistory);
  $("history-close").addEventListener("click", closeHistory);

  $("flow-reply-form").addEventListener("submit", submitReply);
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
  };
}
