/*
 * WebUI interjection-dialog tests.
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub is
 * installed. Exposes `registerInterjectionDialogTests({app, check, findOne,
 * findAll})` so the parent harness drives the same reporter and `app` export.
 *
 * Coverage:
 *   (a) a `kind: "dialog"` chat record normalizes with its kind intact and
 *       renders as its own labelled, always-expanded turn — a user turn here
 *       is something the operator TYPED, so the collapse-by-default rule for
 *       generated prompts must not hide it;
 *   (b) the `dialog` call kind is a first-class intervention (it has KIND_META
 *       and does not degrade to a plain "call");
 *   (c) the reply panel shows the transcript, and — once a decision is
 *       proposed — every decision field as an EDITABLE control plus a confirm
 *       button, because a decision executes only after the operator confirms
 *       it and they must be able to change any field first.
 */
import assert from "node:assert/strict";

export function registerInterjectionDialogTests(ctx) {
  const { app, check, findOne, findAll } = ctx;

  const dialogRecord = (role, content) => ({
    step_id: "03_implement_ab",
    step_type: "implement",
    message: {
      role,
      kind: "dialog",
      content,
      raw_json: [],
      timestamp: "2026-09-02T10:00:00",
      attempt: 0,
    },
  });

  // ---- (a) the chat record ------------------------------------------------
  check("dialog record keeps its kind through normalizeRecord", () => {
    const norm = app.normalizeRecord(dialogRecord("user", "why SQLite?"));
    assert.equal(norm.kind, "dialog");
    assert.equal(norm.role, "user");
    assert.equal(norm.content, "why SQLite?");
    assert.equal(norm.stepId, "03_implement_ab");
  });

  check("an ordinary chat record still reports an empty kind", () => {
    const norm = app.normalizeRecord({
      message: { role: "assistant", content: "done" },
    });
    assert.equal(norm.kind, "");
  });

  check("a dialog turn renders as its own labelled record", () => {
    const norm = app.normalizeRecord(dialogRecord("user", "switch to Postgres"));
    const node = app.renderConversationRecord(norm);
    assert.ok(String(node.className).includes("kind-dialog"));
    assert.ok(findOne(node, "dialog-turn-badge"));
    const body = findOne(node, "dialog-turn-body");
    assert.equal(body.textContent, "switch to Postgres");
  });

  check("an agent dialog turn is labelled distinctly from the user's", () => {
    const user = app.renderConversationRecord(
      app.normalizeRecord(dialogRecord("user", "u")));
    const agent = app.renderConversationRecord(
      app.normalizeRecord(dialogRecord("assistant", "a")));
    assert.ok(String(user.className).includes("role-user"));
    assert.ok(String(agent.className).includes("role-assistant"));
    assert.notEqual(
      findOne(user, "dialog-turn-badge").textContent,
      findOne(agent, "dialog-turn-badge").textContent,
    );
  });

  // ---- (b) the call kind --------------------------------------------------
  check("dialog is a first-class intervention kind", () => {
    assert.ok(app.KIND_META.dialog, "KIND_META must carry the dialog kind");
    assert.equal(app.normalizeKind("dialog"), "dialog");
    // Regression guard: an unknown kind still degrades to a plain call.
    assert.equal(app.normalizeKind("not-a-kind"), "call");
  });

  // ---- (c) the reply panel ------------------------------------------------
  const target = (context) => ({
    id: "dialog_03_implement_ab",
    callId: "dialog_03_implement_ab",
    kind: "dialog",
    prompt: "…",
    options: [],
    context,
  });

  check("the panel renders the transcript so far", () => {
    const panel = app.renderDialogPanel(target({
      transcript: [
        { role: "user", content: "why SQLite?" },
        { role: "assistant", content: "It was the smallest change." },
      ],
      decision: null,
      rewind_targets: [],
    }));
    const turns = findAll(panel, "flow-reply-dialog-turn");
    assert.equal(turns.length, 2);
    const texts = findAll(panel, "flow-reply-dialog-text").map((n) => n.textContent);
    assert.deepEqual(texts, ["why SQLite?", "It was the smallest change."]);
  });

  check("no decision yet means no decision form", () => {
    const panel = app.renderDialogPanel(target({
      transcript: [{ role: "assistant", content: "which one?" }],
      decision: null,
      rewind_targets: [],
    }));
    assert.equal(findAll(panel, "flow-reply-dialog-decision").length, 0);
    assert.equal(findAll(panel, "flow-reply-dialog-confirm").length, 0);
  });

  check("a proposed decision renders every field as an editable control", () => {
    const panel = app.renderDialogPanel(target({
      transcript: [{ role: "assistant", content: "restarting" }],
      decision: {
        action: "restart",
        restart_step_id: "01_plan_aa",
        workspace: "reset",
        instruction: "be careful",
        revised_description: "the corrected task",
      },
      rewind_targets: [
        { step_id: "01_plan_aa", step_type: "plan" },
        { step_id: "03_implement_ab", step_type: "implement" },
      ],
    }));
    const fields = findAll(panel, "flow-reply-dialog-field");
    // action + restart target + workspace + instruction + revised description
    assert.equal(fields.length, 5);
    const [action, restart, workspace, instruction, revised] = fields;
    assert.equal(action.value, "restart");
    assert.equal(restart.value, "01_plan_aa");
    assert.equal(workspace.value, "reset");
    assert.equal(instruction.value, "be careful");
    assert.equal(revised.value, "the corrected task");
    assert.ok(findOne(panel, "flow-reply-dialog-confirm"));
  });

  check("the restart selector offers every rewind target plus the current step", () => {
    const panel = app.renderDialogPanel(target({
      transcript: [],
      decision: { action: "restart" },
      rewind_targets: [
        { step_id: "01_plan_aa", step_type: "plan" },
        { step_id: "02_implement_bb", step_type: "implement" },
      ],
    }));
    const restart = findAll(panel, "flow-reply-dialog-field")[1];
    const values = (restart.children || []).map((o) => o.value);
    assert.deepEqual(values, ["", "01_plan_aa", "02_implement_bb"]);
  });

  check("a same-session dialog says which agent it is talking to", () => {
    const panel = app.renderDialogPanel(target({
      transcript: [],
      decision: null,
      rewind_targets: [],
      same_session: true,
      agent_name: "dclaude",
    }));
    const hints = findAll(panel, "flow-reply-hint").map((n) => n.textContent);
    assert.ok(hints.some((h) => h.includes("dclaude")), hints.join("|"));
  });

  check("a restart+reset proposal shows what it would discard first", () => {
    const panel = app.renderDialogPanel(target({
      transcript: [],
      decision: { action: "restart", workspace: "reset" },
      rewind_targets: [],
      reset_preview: {
        status_summary: " M src/app.py\n?? notes.md",
        flow_commits: ["abc1234 wip"],
        has_dirty_snapshot: true,
        snapshot_warning: false,
      },
    }));
    const box = findOne(panel, "flow-reply-dialog-reset-preview");
    assert.ok(box, "the reset preview must be rendered before the confirm");
    const bodies = findAll(panel, "flow-reply-dialog-reset-body")
      .map((n) => n.textContent).join("\n");
    assert.ok(bodies.includes("src/app.py"), bodies);
    assert.ok(bodies.includes("abc1234 wip"), bodies);
    assert.equal(findAll(panel, "flow-reply-dialog-reset-warning").length, 0);
  });

  check("a reset with no pre-flow snapshot warns in the preview", () => {
    const panel = app.renderDialogPanel(target({
      transcript: [],
      decision: { action: "restart", workspace: "reset" },
      rewind_targets: [],
      reset_preview: {
        status_summary: "",
        flow_commits: [],
        has_dirty_snapshot: false,
        snapshot_warning: true,
      },
    }));
    assert.equal(findAll(panel, "flow-reply-dialog-reset-warning").length, 1);
  });

  check("a keep-workspace proposal shows no reset preview", () => {
    const panel = app.renderDialogPanel(target({
      transcript: [],
      decision: { action: "restart", workspace: "keep" },
      rewind_targets: [],
      reset_preview: null,
    }));
    assert.equal(findAll(panel, "flow-reply-dialog-reset-preview").length, 0);
  });

  check("an unavailable preview is shown as such, never as a clean tree", () => {
    const panel = app.renderDialogPanel(target({
      transcript: [],
      decision: { action: "restart", workspace: "reset" },
      rewind_targets: [],
      reset_preview: { ok: false, error: "git status failed" },
    }));
    // No preview box (an empty status panel reads as "nothing to lose") and a
    // visible warning instead.
    assert.equal(findAll(panel, "flow-reply-dialog-reset-preview").length, 0);
    assert.equal(findAll(panel, "flow-reply-dialog-reset-warning").length, 1);
  });

  check("an apply failure is shown in the panel, not only in the prompt", () => {
    // Regression: the reason lived only in the (collapsed) call prompt, so a
    // rejected Apply republished a byte-identical panel and read as "nothing
    // happened" — inviting the same click again.
    const panel = app.renderDialogPanel(target({
      transcript: [{ role: "user", content: "restart at plan" }],
      decision: { action: "restart", workspace: "keep", restart_step_id: "" },
      apply_error: "engine.rewind.no_entry_snapshot",
    }));
    const banner = findOne(panel, "flow-reply-dialog-apply-error");
    assert.ok(banner, "the apply error must be rendered in the panel");
    assert.ok(String(banner.textContent).includes("engine.rewind.no_entry_snapshot"));
  });

  check("no apply-error banner when the round carries none", () => {
    const panel = app.renderDialogPanel(target({
      transcript: [],
      decision: { action: "continue", workspace: "keep" },
    }));
    assert.equal(findAll(panel, "flow-reply-dialog-apply-error").length, 0);
  });

  check("a restart shows the DAG group work it will delete, keep included", () => {
    // `workspace: keep` only ever meant the flow's own tree; the group
    // worktrees and leaf branches go either way, and neither the main tree's
    // status nor baseline..HEAD shows any of it.
    const panel = app.renderDialogPanel(target({
      transcript: [],
      decision: { action: "restart", workspace: "keep", restart_step_id: "" },
      group_work: [{
        branch: "impl/f1/G1",
        worktree_path: "/tmp/wt/G1",
        commits: ["abc1234 feat: G1"],
        status_summary: " M src/x.py",
      }],
    }));
    const boxes = findAll(panel, "flow-reply-dialog-reset-preview");
    assert.equal(boxes.length, 1);
    const body = findOne(boxes[0], "flow-reply-dialog-reset-body");
    assert.ok(String(body.textContent).includes("impl/f1/G1"));
    assert.ok(String(body.textContent).includes("abc1234 feat: G1"));
    assert.ok(String(body.textContent).includes("src/x.py"));
  });

  check("no group-work box when the restart deletes no groups", () => {
    const panel = app.renderDialogPanel(target({
      transcript: [],
      decision: { action: "restart", workspace: "keep" },
      group_work: [],
    }));
    assert.equal(findAll(panel, "flow-reply-dialog-reset-preview").length, 0);
  });

  check("a blank reply leaves no dangling optimistic echo", () => {
    // "Resume unchanged" sends "". The engine writes no history record for it
    // and reconcileLocalEchoes skips blank echoes, so an echo here would sit in
    // the conversation forever matching nothing.
    app.state.selectedFlowId = "f-echo";
    app.state.flowConversationRecords = [];
    app.appendLocalReply("f-echo", { kind: "dialog", callId: "c1" }, "");
    assert.equal(app.state.flowConversationRecords.length, 0);
    app.appendLocalReply("f-echo", { kind: "dialog", callId: "c1" }, "   ");
    assert.equal(app.state.flowConversationRecords.length, 0);
    // A real reply still echoes.
    app.appendLocalReply("f-echo", { kind: "dialog", callId: "c1" }, "go on");
    assert.equal(app.state.flowConversationRecords.length, 1);
    app.state.flowConversationRecords = [];
  });

  check("a malformed context degrades instead of throwing", () => {
    const panel = app.renderDialogPanel({ kind: "dialog" });
    assert.ok(panel);
    assert.equal(findAll(panel, "flow-reply-dialog-turn").length, 0);
  });
}
