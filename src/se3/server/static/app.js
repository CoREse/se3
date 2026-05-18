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
    // Both "snapshot" (on connect) and "status_update" carry the full list.
    if (msg && Array.isArray(msg.machines)) {
      applyMachines(msg.machines);
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
  $("flow-detail").classList.remove("hidden");
  refreshFlowDetail();
  // Poll the REST endpoint while the drawer is open (WS updates also refresh).
  if (detailPollTimer) clearInterval(detailPollTimer);
  detailPollTimer = setInterval(refreshFlowDetail, 3000);
}

function closeDrawer() {
  state.selectedFlowId = null;
  $("flow-detail").classList.add("hidden");
  if (detailPollTimer) {
    clearInterval(detailPollTimer);
    detailPollTimer = null;
  }
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

  // -- latest output / summary --
  const outSec = el("div", "detail-section");
  outSec.appendChild(el("h4", null, "Latest output"));
  const logBox = el("div", "log-box",
    flow.summary || "(no summary or output reported yet)");
  outSec.appendChild(logBox);
  body.appendChild(outSec);
  // Scroll the log box to the bottom so the freshest line is visible.
  logBox.scrollTop = logBox.scrollHeight;
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
