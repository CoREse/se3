/*
 * step_type token-safety + render-isolation tests (Bug A).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub is
 * installed. Exposes `registerStepTypeTokenSafetyTests({app, check, findOne,
 * findAll})`.
 *
 * Context: `appendLocalReply` used to stamp the optimistic echo's `step_type`
 * with a RENDERED i18n label — zh-CN's "待回复 回复", which contains a space.
 * `step_type` is an identifier (DOM class suffix / grouping key / header
 * fallback), so `tagStepType`'s `classList.add("step-type-" + key)` threw
 * InvalidCharacterError on it. The echo lived on in
 * `state.flowConversationRecords`, so EVERY later render (applyHistoryData →
 * renderConversation → addConversationRecords, on the un-guarded ws.onmessage
 * path) threw again: the whole chat view froze until the reader exited and
 * re-entered the session.
 *
 * Two independent invariants are pinned here:
 *   (A) the echo's step_type is a machine-safe token derived from the kind, not
 *       an i18n label — while its rendered step header stays localized (zh-CN
 *       must NOT regress to English or to a bare token);
 *   (B) the renderer is defensive regardless of who wrote the record: a dirty
 *       step_type is sanitized (or skipped) rather than thrown on, and a record
 *       that still manages to throw only loses ITS OWN bubble — the rest of the
 *       batch renders.
 *
 * The harness's FakeNode.classList is a permissive Set, so a browser-faithful
 * `classList.add` (which throws on whitespace / empty tokens) is installed for
 * the duration of each check — without it these tests could not observe the
 * original defect at all.
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const I18N_DIR = path.join(here, "..", "..", "src", "se3", "server", "static", "i18n");

const DOM_TOKEN_RE = /^[a-z0-9_-]+$/;

export function registerStepTypeTokenSafetyTests(ctx) {
  const { app, check, findOne, findAll } = ctx;
  const { I18N } = app;

  // Browser-faithful classList.add: whitespace or an empty token is an
  // InvalidCharacterError, exactly as DOMTokenList specifies.
  function withStrictDomTokens(fn) {
    const origCreate = document.createElement;
    document.createElement = (tag) => {
      const node = origCreate(tag);
      const add = node.classList.add;
      node.classList.add = (...tokens) => {
        for (const tok of tokens) {
          const s = String(tok == null ? "" : tok);
          if (s === "" || /\s/.test(s)) {
            const err = new Error(
              `InvalidCharacterError: token '${s}' is not a valid DOM token`);
            err.name = "InvalidCharacterError";
            throw err;
          }
        }
        return add(...tokens);
      };
      return node;
    };
    try {
      return fn();
    } finally {
      document.createElement = origCreate;
    }
  }

  // Run `fn` with the real shipped dictionaries loaded and the UI in zh-CN,
  // restoring the harness's default (empty dicts, en-US) afterwards so the
  // other suites keep their offline-fallback expectations.
  function withZhCN(fn) {
    const prevLang = I18N.lang;
    const prevDicts = I18N.dicts;
    I18N.dicts = {
      "en-US": JSON.parse(fs.readFileSync(path.join(I18N_DIR, "en-US.json"), "utf8")),
      "zh-CN": JSON.parse(fs.readFileSync(path.join(I18N_DIR, "zh-CN.json"), "utf8")),
    };
    I18N.lang = "zh-CN";
    try {
      return fn();
    } finally {
      I18N.lang = prevLang;
      I18N.dicts = prevDicts;
    }
  }

  const chatRecord = (stepId, stepType, role, content, ts) => ({
    step_id: stepId, step_type: stepType,
    message: { role, content, timestamp: ts },
  });

  const bubbleTexts = (container) =>
    findAll(container, "conv-record").map((b) => b.textContent);

  const headerTitles = (container) =>
    findAll(container, "history-step-header")
      .map((h) => { const t = findOne(h, "history-step-title"); return t ? t.textContent : ""; });

  // ---- (A) sanitizeDomToken / tagStepType never produce an illegal token ----
  check("sanitizeDomToken folds whitespace and drops illegal characters", () => {
    assert.equal(app.sanitizeDomToken("  Self Check  "), "self-check");
    assert.equal(app.sanitizeDomToken("implement"), "implement");
    assert.equal(app.sanitizeDomToken("self_check"), "self_check");
    assert.equal(app.sanitizeDomToken("a/b:c"), "abc");
    // A pure-CJK label sanitizes to nothing → callers must skip, not add "".
    assert.equal(app.sanitizeDomToken("待回复 回复"), "");
    assert.equal(app.sanitizeDomToken(""), "");
    assert.equal(app.sanitizeDomToken(null), "");
    assert.equal(app.sanitizeDomToken(undefined), "");
  });

  check("tagStepType never throws and never adds an illegal class", () => {
    withStrictDomTokens(() => {
      for (const dirty of ["待回复 回复", "Awaiting reply response", "  ", "", null,
        undefined, "step type/with:junk", 42]) {
        const bubble = document.createElement("div");
        app.tagStepType(bubble, dirty);
        for (const cls of String(bubble.className).split(/\s+/).filter(Boolean)) {
          assert.ok(cls.startsWith("step-type-"), `unexpected class ${cls}`);
          assert.ok(DOM_TOKEN_RE.test(cls.slice("step-type-".length)),
            `class suffix must be a legal DOM token, got ${cls}`);
        }
      }
    });
  });

  // ---- (B) a dirty record must not break the whole conversation render -----
  check("a space-bearing step_type record renders and does not stall the batch", () => {
    withStrictDomTokens(() => {
      const container = document.createElement("div");
      app.renderConversation(container, [
        chatRecord("interaction", "待回复 回复", "user", "1", 1),
        chatRecord("01_discovery_abc", "discovery", "assistant", "healthy answer", 2),
      ], false);
      const texts = bubbleTexts(container);
      assert.equal(texts.length, 2,
        "the dirty record must not stop the healthy one from rendering");
      assert.ok(texts.some((t) => t.includes("healthy answer")),
        "the record AFTER the dirty one must still produce a bubble");
    });
  });

  check("a record whose step_type throws on coercion loses only its own bubble", () => {
    withStrictDomTokens(() => {
      const container = document.createElement("div");
      const poison = {
        step_id: "interaction",
        step_type: { toString() { throw new Error("poison step_type"); } },
        message: { role: "user", content: "poison", timestamp: 1 },
      };
      app.renderConversation(container, [
        poison,
        chatRecord("01_discovery_abc", "discovery", "assistant", "healthy answer", 2),
      ], false);
      const texts = bubbleTexts(container);
      assert.ok(texts.some((t) => t.includes("healthy answer")),
        "an unrenderable record must not abort the rest of the batch");
    });
  });

  check("an incremental append past a dirty record keeps flowing", () => {
    withStrictDomTokens(() => {
      const container = document.createElement("div");
      const records = [chatRecord("interaction", "待回复 回复", "user", "1", 1)];
      app.renderConversation(container, records, false);
      records.push(chatRecord("01_discovery_abc", "discovery", "assistant", "later turn", 2));
      // The append path is the ws.onmessage path: it must not throw either.
      app.renderConversation(container, records, true);
      assert.ok(bubbleTexts(container).some((t) => t.includes("later turn")),
        "a later WS append must still render past a dirty record already in state");
    });
  });

  // ---- (C) the echo's step_type is a token; its header stays localized -----
  check("zh-CN appendLocalReply writes a legal DOM token, not an i18n label", () => {
    withZhCN(() => withStrictDomTokens(() => {
      const flowId = "20260714-093536_a4af4b75";
      app.state.selectedFlowId = flowId;
      app.state.flowConversationRecords = [];
      app.appendLocalReply(flowId, { id: "call:c1", kind: "call" }, "不行，聊天记录里还是什么都没有");

      const echo = app.state.flowConversationRecords[0];
      assert.ok(echo && echo.__localEcho, "the echo must land in state");
      const stepType = echo.message.step_type;
      assert.ok(DOM_TOKEN_RE.test(stepType),
        `echo step_type must be a legal DOM token, got ${JSON.stringify(stepType)}`);
      assert.equal(stepType, "reply_call");
      // …and it must NOT vary with the UI language (it is an identifier).
      assert.equal(app.replyStepType("call"), "reply_call");
      assert.equal(app.replyStepType("interjection"), "reply_interjection");
      assert.equal(app.replyStepType("nonsense-kind"), "reply_call");

      // Display must not regress: the step header still shows the zh-CN label.
      assert.equal(app.stepHeaderLabel(stepType, "step"), "待回复 回复");
      const container = document.createElement("div");
      app.renderConversation(container, app.state.flowConversationRecords, false);
      assert.deepEqual(headerTitles(container), ["待回复 回复"],
        "the echo's step header must stay localized (no English, no bare token)");
    }));
  });

  check("the echo is still reconciled away by its authoritative record", () => {
    withZhCN(() => {
      const flowId = "20260714-093536_a4af4b75";
      app.state.selectedFlowId = flowId;
      app.state.flowConversationRecords = [];
      app.appendLocalReply(flowId, { id: "call:c1", kind: "call" }, "继续");
      assert.equal(app.state.flowConversationRecords.length, 1);

      // The daemon's authoritative copy of the same reply arrives.
      const merged = app.state.flowConversationRecords.concat([
        chatRecord("01_discovery_abc", "discovery", "user", "继续", 9),
      ]);
      const reconciled = app.reconcileLocalEchoes(merged);
      assert.equal(reconciled.length, 1, "the echo must be dropped, not duplicated");
      assert.ok(!reconciled[0].__localEcho, "the surviving record is the authoritative one");
    });
  });
}
