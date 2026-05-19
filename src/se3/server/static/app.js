/*
 * SE3 Control Plane — web frontend.
 *
 * Connects to the central server's `/ws/ui` WebSocket for realtime machine /
 * flow state, renders the dashboard, and drives the REST API for flow detail,
 * task publishing, and interjection/call responses.
 */
"use strict";

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const state = {
  machines: [],           // [{machine_id, hostname, online, flows: [...]}]
  selectedMachineId: null,
  selectedFlowId: null,   // flow open in the detail drawer
  historySessions: [],    // [{flow_id, task_description, status, updated_at, ...}]
  selectedHistoryId: null,// flow whose records are shown in the history detail
  historyRecords: [],     // records currently rendered in the history detail
  drawerConversationRecords: [], // conversation records shown in the flow drawer
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

function findFlow(flowId) {
  for (const m of state.machines) {
    for (const f of m.flows || []) {
      if (f.flow_id === flowId) return { machine: m, flow: f };
    }
  }
  return null;
}

function hasPendingCall(flow) {
  return Array.isArray(flow.pending_calls) &&
    flow.pending_calls.some((c) => (c.kind || "call") === "call");
}

// ---------------------------------------------------------------------------
// WebSocket client (with exponential-backoff reconnect)
// ---------------------------------------------------------------------------

function setConnStatus(kind, label) {
  const node = $("conn-status");
  node.className = "conn conn-" + kind;
  node.textContent = label;
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${location.host}/ws/ui`;
  setConnStatus("connecting", reconnectAttempts ? "reconnecting…" : "connecting…");

  ws = new WebSocket(url);

  ws.onopen = () => {
    reconnectAttempts = 0;
    setConnStatus("connected", "connected");
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

  // Refresh the detail drawer if its flow is still around.
  if (state.selectedFlowId) {
    if (findFlow(state.selectedFlowId)) {
      refreshFlowDetail();
    } else {
      closeDrawer();
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
    const callBadge = el("span", "badge badge-call", "⚠ needs response");
    callBadge.addEventListener("click", (e) => {
      e.stopPropagation();
      openCallModal(flow);
    });
    head.appendChild(callBadge);
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
  card.addEventListener("click", () => openDrawer(flow.flow_id));
  return card;
}

// ---------------------------------------------------------------------------
// Render: flow detail drawer
// ---------------------------------------------------------------------------

const STEP_ICONS = {
  completed: "✓", failed: "✗", running: "⟳",
  paused: "⏸", pending: "⏸", partial: "◐", retrying: "⟳",
};

function openDrawer(flowId) {
  state.selectedFlowId = flowId;
  state.drawerConversationRecords = [];
  $("flow-detail").classList.remove("hidden");
  refreshFlowDetail();
  // Fetch the flow's conversation snapshot; WS history_data deltas append live.
  loadDrawerConversation(flowId);
  // Poll the REST endpoint while the drawer is open (WS updates also refresh).
  if (detailPollTimer) clearInterval(detailPollTimer);
  detailPollTimer = setInterval(refreshFlowDetail, 3000);
}

function closeDrawer() {
  state.selectedFlowId = null;
  state.drawerConversationRecords = [];
  $("flow-detail").classList.add("hidden");
  if (detailPollTimer) {
    clearInterval(detailPollTimer);
    detailPollTimer = null;
  }
}

// Fetch the initial conversation snapshot for a flow drawer. Mirrors the
// history view: a one-shot `/api/history/{flow_id}` pull, after which the WS
// `history_data` push keeps an active flow's conversation up to date.
async function loadDrawerConversation(flowId) {
  const container = $("detail-conversation");
  container.innerHTML = "";
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
    state.drawerConversationRecords = Array.isArray(data.records)
      ? data.records : [];
    renderConversation(container, state.drawerConversationRecords);
    scrollDrawerConversationToBottom();
  } catch (_) {
    if (state.selectedFlowId !== flowId) return;
    container.innerHTML = "";
    container.appendChild(el("p", "empty", "Network error loading conversation."));
  }
}

function scrollDrawerConversationToBottom() {
  const c = $("detail-conversation");
  c.scrollTop = c.scrollHeight;
}

async function refreshFlowDetail() {
  const flowId = state.selectedFlowId;
  if (!flowId) return;
  try {
    const resp = await fetch(`/api/flows/${encodeURIComponent(flowId)}`);
    if (!resp.ok) return;
    const data = await resp.json();
    renderFlowDetail(data.flow, data.machine_id);
  } catch (_) {
    /* transient — the poll will retry */
  }
}

function renderFlowDetail(flow, machineId) {
  $("detail-title").textContent =
    flow.task_description || flow.flow_id || "Flow";

  const body = $("detail-body");
  body.innerHTML = "";

  // -- overview --
  const overview = el("div", "detail-section");
  overview.appendChild(el("h4", null, "Overview"));
  const kv = (k, v) => {
    const row = el("div", "kv");
    row.append(el("span", "k", k), el("span", "v", String(v)));
    return row;
  };
  const sc = statusClass(flow.status);
  overview.appendChild(kv("Status", flow.status || "unknown"));
  overview.appendChild(kv("Machine", machineId || "-"));
  overview.appendChild(kv("Type", flow.task_type || "-"));
  overview.appendChild(kv(
    "Progress",
    `${flow.current_step_index || 0}/${flow.total_steps || 0} ` +
    `(${Math.round((flow.progress || 0) * 100)}%)`,
  ));
  if (flow.current_step) overview.appendChild(kv("Current step", flow.current_step));
  if (flow.updated_at) overview.appendChild(kv("Updated", flow.updated_at));
  body.appendChild(overview);

  // -- pending call --
  if (hasPendingCall(flow)) {
    const callSec = el("div", "detail-section");
    callSec.appendChild(el("h4", null, "Action required"));
    const respondBtn = el("button", null, "Respond to pending call");
    respondBtn.addEventListener("click", () => openCallModal(flow));
    callSec.appendChild(respondBtn);
    body.appendChild(callSec);
  }

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

  // The flow's turn-by-turn conversation is rendered into the dedicated
  // `#detail-conversation` region (a sibling of `#detail-body`), not here —
  // that region survives the 3s detail poll and is fed by loadDrawerConversation
  // plus live `history_data` WS deltas, replacing the old single summary box.
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
// be open in both the history view and the drawer; each keeps its own record
// array so they update independently without double-appending each other.
function applyHistoryData(msg) {
  const records = Array.isArray(msg.records) ? msg.records : [];
  const append = msg.mode === "append";

  // -- history view consumer --
  if (isHistoryOpen() && state.selectedHistoryId === msg.flow_id) {
    state.historyRecords = append
      ? state.historyRecords.concat(records)
      : records;
    renderHistoryRecords(msg.flow_id, state.historyRecords);
    scrollHistoryToBottom();
  }

  // -- running-flow drawer consumer --
  if (state.selectedFlowId === msg.flow_id) {
    state.drawerConversationRecords = append
      ? state.drawerConversationRecords.concat(records)
      : records;
    renderConversation($("detail-conversation"), state.drawerConversationRecords);
    scrollDrawerConversationToBottom();
  }
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
    state.historyRecords = Array.isArray(data.records) ? data.records : [];
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

// Comparable epoch-ms value for a timestamp of unknown shape, for sorting.
function tsValue(ts) {
  if (ts == null || ts === "") return 0;
  if (typeof ts === "number") return ts < 1e12 ? ts * 1000 : ts;
  const d = new Date(ts);
  return isNaN(d.getTime()) ? 0 : d.getTime();
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
// flow drawer so both present identical grouping / bubbles / folding.
function renderConversation(container, records) {
  container.innerHTML = "";
  if (!records.length) {
    container.appendChild(
      el("p", "empty", "No conversation records for this session."));
    return;
  }
  const order = [];
  const byStep = new Map();
  records.forEach((rec, index) => {
    const norm = normalizeRecord(rec);
    const key = stepKey(norm);
    if (!byStep.has(key)) {
      byStep.set(key, { label: norm.stepType || norm.stepId || "step", records: [] });
      order.push(key);
    }
    // `index` is the stable tiebreaker so equal timestamps keep arrival order.
    byStep.get(key).records.push({ norm, index });
  });
  for (const key of order) {
    const group = byStep.get(key);
    group.records.sort((a, b) => {
      const d = tsValue(a.norm.timestamp) - tsValue(b.norm.timestamp);
      return d !== 0 ? d : a.index - b.index;
    });
    container.appendChild(renderStepGroup(group));
  }
}

function renderHistoryRecords(flowId, records) {
  renderConversation($("history-detail"), records);
}

function renderStepGroup(group) {
  const sec = el("div", "history-step");
  sec.appendChild(el("h5", "history-step-title", group.label));
  for (const item of group.records) {
    sec.appendChild(renderConversationRecord(item.norm));
  }
  return sec;
}

// ---------------------------------------------------------------------------
// Conversation rendering engine
// ---------------------------------------------------------------------------
//
// A self-contained renderer shared by the history view and (G3) the running
// flow drawer. Three layers:
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
// assistant bodies flow through tool-marker + Markdown rendering; user/system
// bodies are shown as literal whitespace-preserving text. Long bodies fold by
// default, and each record gets a "view raw" toggle.
function renderConversationRecord(norm) {
  const known = ["user", "assistant", "system"].includes(norm.role);
  const role = known ? norm.role : "other";
  const row = el("div", "history-record conv-record role-" + role);

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
  row.appendChild(head);

  const bubble = el("div", "conv-bubble");
  const content = typeof norm.content === "string" ? norm.content : "";
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
    // user / system: literal text — these are large structured prompts whose
    // exact whitespace matters; do not Markdown-mangle them.
    const buildFull = () => el("pre", "conv-plain", content);
    bubble.appendChild(makeFoldable(buildFull, content));
  }
  row.appendChild(bubble);

  const rawToggle = makeRawToggle(norm);
  if (rawToggle) row.appendChild(rawToggle);

  return row;
}

function scrollHistoryToBottom() {
  const detail = $("history-detail");
  detail.scrollTop = detail.scrollHeight;
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
    } else {
      const detail = await resp.json().catch(() => ({}));
      showFormError(errBox, detail.detail || `Server returned ${resp.status}.`);
      submit.disabled = false;
    }
  } catch (err) {
    showFormError(errBox, "Network error — could not reach the server.");
    submit.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// Interjection / call response
// ---------------------------------------------------------------------------

let activeCall = null;  // { flowId, callId }

function openCallModal(flow) {
  const pending = (flow.pending_calls || []).filter(
    (c) => (c.kind || "call") === "call",
  );
  const call = pending[0] || {};
  activeCall = { flowId: flow.flow_id, callId: call.call_id || "" };

  const info = $("call-info");
  info.innerHTML = "";
  info.append(
    el("div", null, `Flow: ${flow.task_description || flow.flow_id}`),
    el("div", null, `Call: ${call.call_id || "(unnamed)"}`),
    el("div", null, `Type: ${call.kind || "call"} — human confirmation needed`),
  );

  $("call-response").value = "";
  $("call-error").classList.add("hidden");
  $("call-submit").disabled = false;
  $("call-modal").classList.remove("hidden");
}

function closeCallModal() {
  activeCall = null;
  $("call-modal").classList.add("hidden");
}

async function submitCall(event) {
  event.preventDefault();
  if (!activeCall) return;
  const errBox = $("call-error");
  errBox.classList.add("hidden");

  const response = $("call-response").value.trim();
  if (!response) return showFormError(errBox, "Response must not be empty.");

  const submit = $("call-submit");
  submit.disabled = true;
  try {
    const resp = await fetch(
      `/api/flows/${encodeURIComponent(activeCall.flowId)}/respond`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          response: response,
          call_id: activeCall.callId,
        }),
      },
    );
    if (resp.ok) {
      closeCallModal();
    } else {
      const detail = await resp.json().catch(() => ({}));
      showFormError(errBox, detail.detail || `Server returned ${resp.status}.`);
      submit.disabled = false;
    }
  } catch (err) {
    showFormError(errBox, "Network error — could not reach the server.");
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

  $("detail-close").addEventListener("click", closeDrawer);

  $("history-btn").addEventListener("click", openHistory);
  $("history-close").addEventListener("click", closeHistory);

  $("call-close").addEventListener("click", closeCallModal);
  $("call-form").addEventListener("submit", submitCall);

  // Click the modal backdrop to dismiss.
  for (const id of ["new-task-modal", "call-modal"]) {
    $(id).addEventListener("click", (e) => {
      if (e.target.id === id) $(id).classList.add("hidden");
    });
  }

  connect();
}

document.addEventListener("DOMContentLoaded", init);
