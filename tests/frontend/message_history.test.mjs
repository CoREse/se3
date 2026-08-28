/*
 * Owner-scoped message history + arrow-key recall tests (Group G2).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub
 * (`globalThis.document` / `FakeNode`) is installed. Exposes
 * `registerMessageHistoryTests({app, check, checkAsync})` so the parent harness
 * drives the same check() reporter against the same `app` module export.
 *
 * What is under test: the two prompt boxes that recall SENT text —
 * #flow-reply-input (respond + interject, one textarea, one channel) and
 * #nt-task. The contract these checks pin down:
 *
 *   - the navigation semantics copy the CLI's (prompt_toolkit, multiline): ↑
 *     reaches history only with the caret on the first line, ↓ only on the
 *     last; anywhere else the arrow stays caret movement;
 *   - stepping into history stashes what was being edited, and walking back
 *     down past the newest entry restores it;
 *   - the two channels never mix, and replacing the box's content re-measures
 *     the auto-grow textarea (a value assignment fires no `input` event);
 *   - only DELIVERED text is recorded — all four success paths, none of the
 *     failure ones — with blanks and immediate repeats dropped;
 *   - a delivered message always reaches the server, which owns the ordering:
 *     the browser's cached list may be stale, so it never vetoes the POST;
 *   - identity is the server-assigned entry id and NEVER the text: an answer
 *     that raced this session's own sends collapses because the ids say the
 *     rows are those sends, a stale answer never swallows a send made after the
 *     request left, and a local entry the server has not named survives on
 *     local send order however familiar it reads;
 *   - a list that changes under a traversal in progress (a late load, a send
 *     landing while the operator browses) re-aims the cursor at the entry the
 *     box is actually showing, so every arrow still moves one adjacent step;
 *   - and the one that decides whether this is safe to ship: an unreachable /
 *     401 / hung / broken history endpoint degrades to this session's in-memory
 *     list — recalled at once, never queued behind the request — and never
 *     blocks typing or submitting, while the existing Ctrl/Cmd+Enter submit
 *     binding on the same textarea keeps working. Arrows taken while the first
 *     load is still in flight are queued and replayed one entry per key.
 *
 * Parallel safety: the checks share the harness's one node process and its
 * id-keyed element cache, so each one resets the elements it touches, clears
 * the module-level history cache, and restores every global it borrows
 * (fetch / setTimeout / window) before returning. Nothing here depends on
 * another module's ordering or leaves mutable state behind.
 */
import assert from "node:assert/strict";

export async function registerMessageHistoryTests(ctx) {
  const { app } = ctx;
  const check = ctx.check;
  const checkAsync = ctx.checkAsync;
  const REPLY = app.MSG_HISTORY.REPLY;
  const NEW_TASK = app.MSG_HISTORY.NEW_TASK;

  // ---- globals -----------------------------------------------------------

  // A minimal stand-in for the real endpoint: rows with server-assigned ids,
  // the same adjacent-repeat fold the store applies, and the id the append
  // landed on reported back. A stub that answered `{}` would not exercise the
  // contract at all — that id is the only thing the browser may merge on.
  function fakeHistoryServer(initial) {
    const srv = {
      rows: [],
      next: 1,
      // When set, a GET answers with THIS list instead of the live rows — how
      // a check pins a stale snapshot that a later POST overtakes.
      snapshot: null,
    };
    for (const text of initial || []) srv.rows.push({ id: srv.next++, text });
    srv.read = () => (srv.snapshot || srv.rows).map((r) => ({ id: r.id, text: r.text }));
    srv.append = (text) => {
      const body = typeof text === "string" ? text : "";
      if (!body.trim()) return { status: "skipped", appended: false, entry_id: null };
      const last = srv.rows[srv.rows.length - 1];
      if (last && last.text === body) {
        return { status: "skipped", appended: false, entry_id: last.id };
      }
      const row = { id: srv.next++, text: body };
      srv.rows.push(row);
      return { status: "appended", appended: true, entry_id: row.id };
    };
    return srv;
  }

  // Record every request and answer it out of a fake server built from
  // `historyEntries`. `fail` makes every history call reject (network down /
  // blocked), and `status` models a 401 from an expired session.
  function installFetch({ historyEntries = [], fail = false, status = 200, server = null } = {}) {
    const srv = server || fakeHistoryServer(historyEntries);
    const calls = [];
    const saved = globalThis.fetch;
    globalThis.fetch = async (url, init) => {
      const u = String(url);
      const opts = init || {};
      calls.push({ url: u, init: opts, body: opts.body ? JSON.parse(opts.body) : null });
      if (u.includes("/api/message-history/")) {
        if (fail) throw new Error("network down");
        const body = opts.method === "POST"
          ? srv.append(JSON.parse(opts.body || "{}").text)
          : { entries: srv.read() };
        return {
          ok: status >= 200 && status < 300,
          status,
          json: async () => body,
        };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    };
    return {
      calls,
      server: srv,
      history: () => calls.filter((c) => c.url.includes("/api/message-history/")),
      restore: () => { globalThis.fetch = saved; },
    };
  }

  function withNoTimers(fn) {
    const saved = globalThis.setTimeout;
    globalThis.setTimeout = () => 0;
    try {
      return fn();
    } finally {
      globalThis.setTimeout = saved;
    }
  }

  async function flush() {
    for (let i = 0; i < 8; i += 1) await Promise.resolve();
  }

  // ---- element helpers ---------------------------------------------------

  // The harness caches elements by id, so a check that binds a listener would
  // otherwise stack another one on every run.
  function freshInput(id) {
    const node = document.getElementById(id);
    node._listeners = {};
    node.value = "";
    node.selectionStart = 0;
    node.selectionEnd = 0;
    if (node.style) { node.style.height = ""; node.style.overflowY = ""; }
    node.__autoGrowApplied = false;
    return node;
  }

  // Put the caret where a browser would after a click / a programmatic edit.
  function caretAt(node, pos) {
    node.selectionStart = pos;
    node.selectionEnd = pos;
  }

  // One arrow keystroke; returns whether the app consumed it (preventDefault),
  // which is exactly the "history took this key" signal.
  function press(node, key, extra) {
    let prevented = false;
    node.dispatch(
      "keydown",
      Object.assign({ key, preventDefault() { prevented = true; } }, extra || {}),
    );
    return prevented;
  }

  // Seed a channel's list without going near the network, and mark it loaded so
  // the lazy fetch does not fire behind the check. Plain strings become entries
  // carrying ids from `startId`, i.e. rows a server has already named; the
  // default base is far from any id the fake server hands out, so a check that
  // seeds AND posts cannot collide by accident.
  function seedHistory(channel, entries, startId) {
    const base = Number.isFinite(startId) ? startId : 9001;
    const st = app.historyChannelState(channel);
    st.entries = entries.map((e, i) =>
      typeof e === "string"
        ? app.normalizeHistoryEntry({ text: e, serverId: base + i })
        : app.normalizeHistoryEntry(e));
    st.loaded = true;
    st.loading = null;
    return st;
  }

  // Seed the client with exactly the rows a fake server holds, ids and all —
  // the state a browser is in after a load that already landed.
  function seedFromServer(channel, f) {
    return seedHistory(channel, f.server.rows.map((r) => ({ id: r.id, text: r.text })));
  }

  // Just the texts of a channel's list; most checks are about ordering.
  function texts(channel) {
    return app.historyChannelState(channel).entries.map((e) => e.text);
  }

  // Wipe the module-level cache and the per-input cursors so no check inherits
  // another's list.
  function resetHistory() {
    app.clearMessageHistoryCache();
    // Recalling an entry (and restoring the stash) is a content change like any
    // keystroke, so it queues a debounced draft save. Drop those rather than
    // let a timer from one check fire into the next one's storage.
    for (const key of Object.keys(app.DRAFTS.timers)) app.cancelDraftSave(key);
  }

  // Make the docked textarea report a content-dependent scrollHeight, the one
  // signal autoGrowReplyTextarea() reads (mirrors the draft suite's helper).
  function withMeasuredReplyBox(fn) {
    const input = document.getElementById("flow-reply-input");
    const savedWindow = Object.prototype.hasOwnProperty.call(globalThis, "window")
      ? globalThis.window : undefined;
    const hadWindow = savedWindow !== undefined;
    globalThis.window = {
      innerHeight: 1000,
      matchMedia: () => ({ matches: true }),
      addEventListener: () => {},
    };
    Object.defineProperty(input, "scrollHeight", {
      configurable: true,
      get() { return 20 * String(this.value || "").split("\n").length + 20; },
    });
    try {
      return fn(input);
    } finally {
      Object.defineProperty(input, "scrollHeight", {
        configurable: true, writable: true, value: 0,
      });
      if (hadWindow) globalThis.window = savedWindow;
      else delete globalThis.window;
    }
  }

  function armFlow(flowId, callId, kind) {
    app.state.selectedFlowId = flowId;
    app.state.flowConversationRecords = [];
    app.state.flowDetail = {
      flow_id: flowId,
      status: "running",
      pending_calls: [{ call_id: callId, kind, prompt: "?" }],
    };
    app.state.pendingSendTimer = null;
    app.state.pendingSendSettleKey = null;
    app.state.flowInterventions = [];
    app.state.flowReplyTargetId = null;
    app.resetRenderSignatures();
  }

  // ---- pure helpers ------------------------------------------------------

  check("G2 caretAtFirstLine / caretAtLastLine mark the two edges of a multi-line box", () => {
    const text = "one\ntwo\nthree";
    // Offsets: 0-3 line 1, 4-7 line 2, 8-13 line 3.
    assert.equal(app.caretAtFirstLine(text, 0), true);
    assert.equal(app.caretAtFirstLine(text, 3), true, "end of the first line is still the first line");
    assert.equal(app.caretAtFirstLine(text, 4), false, "start of line 2 is not the first line");
    assert.equal(app.caretAtLastLine(text, 13), true);
    assert.equal(app.caretAtLastLine(text, 9), true, "inside the last line");
    assert.equal(app.caretAtLastLine(text, 7), false, "end of line 2 still has a line below");
    // A single-line box is both edges at once, which is what makes the plain
    // one-line prompt navigable at all.
    assert.equal(app.caretAtFirstLine("hello", 2), true);
    assert.equal(app.caretAtLastLine("hello", 2), true);
    // No / out-of-range caret reads as end-of-text, where a fresh focus sits.
    assert.equal(app.caretAtFirstLine(text, undefined), false);
    assert.equal(app.caretAtLastLine(text, undefined), true);
    assert.equal(app.caretAtFirstLine("", 0), true);
    // A box whose first line is empty: offset 0 is still ON that first line, so
    // the arrow must reach history rather than deadlock against the browser's
    // no-op default (lastIndexOf clamps a negative fromIndex and used to see
    // the leading newline itself here).
    assert.equal(app.caretAtFirstLine("\nhello", 0), true);
    assert.equal(app.caretAtFirstLine("\nhello", 1), false, "start of line 2 is not the first line");
    assert.equal(app.caretAtFirstLine("\n", 0), true);
    assert.equal(app.caretAtLastLine("hello\n", 6), true, "an empty last line is still the last line");
  });

  check("G2 orderHistoryPush: blanks dropped, an immediate repeat dropped, cap holds", () => {
    const entries = [];
    const first = app.orderHistoryPush(entries, "first");
    assert.ok(first, "an entry record comes back");
    assert.equal(app.orderHistoryPush(entries, "first"), null, "a repeat of the newest is not a new entry");
    assert.equal(app.orderHistoryPush(entries, "   \n "), null, "whitespace is not a message");
    assert.equal(app.orderHistoryPush(entries, ""), null);
    assert.ok(app.orderHistoryPush(entries, "second"));
    // Non-adjacent repeats DO enter — only the immediate one is suppressed —
    // and the newcomer gets its OWN identity, so nothing downstream can fold
    // the two "first"s back together on the strength of their text.
    const again = app.orderHistoryPush(entries, "first");
    assert.ok(again);
    assert.notEqual(again.id, first.id);
    assert.equal(again.serverId, null, "unnamed until the POST answers");
    assert.deepEqual(entries.map((e) => e.text), ["first", "second", "first"]);
    // Ordering never truncates: the cap is installHistoryEntries()' job, and it
    // drops the oldest, matching the server's own truncation.
    const savedMax = app.MSG_HISTORY.MAX_ENTRIES;
    try {
      app.MSG_HISTORY.MAX_ENTRIES = 5;
      const st = { entries: [], dropped: [] };
      for (let i = 0; i < 8; i += 1) {
        const ordered = app.historyOrdered(st);
        app.orderHistoryPush(ordered, "m" + i);
        app.installHistoryEntries(st, ordered);
      }
      assert.deepEqual(st.entries.map((e) => e.text), ["m3", "m4", "m5", "m6", "m7"]);
      assert.deepEqual(st.dropped, [], "nothing was pending, so the eviction is final");
    } finally {
      app.MSG_HISTORY.MAX_ENTRIES = savedMax;
    }
    // The default cap is the CLI's 500.
    assert.equal(app.MSG_HISTORY.MAX_ENTRIES, 500);
  });

  check("G2 entry identity is the server id, never the text", () => {
    const a = app.normalizeHistoryEntry({ id: 7, text: "same words" });
    const b = app.normalizeHistoryEntry({ id: 7, text: "same words" });
    const c = app.normalizeHistoryEntry({ id: 8, text: "same words" });
    const unnamed = app.normalizeHistoryEntry("same words");
    const unnamed2 = app.normalizeHistoryEntry("same words");
    assert.equal(a.serverId, 7);
    assert.equal(unnamed.serverId, null, "text this session sent carries no server id yet");
    assert.equal(app.sameHistoryEntry(a, b), true, "one row, however often it is read");
    assert.equal(app.sameHistoryEntry(a, c), false, "equal text, two appends");
    assert.equal(app.sameHistoryEntry(a, unnamed), false, "an unnamed entry is nobody else");
    assert.equal(app.sameHistoryEntry(unnamed, unnamed2), false);
    assert.equal(app.sameHistoryEntry(unnamed, unnamed), true, "...but it is itself");
    // A canonical record passes through untouched, so a reference held by a
    // POST still in flight keeps pointing at the entry that stays in the list.
    assert.equal(app.normalizeHistoryEntry(unnamed), unnamed);
  });

  // ---- navigation semantics ---------------------------------------------

  check("G2 ↑ recalls only from the first line; mid-text it stays caret movement", () => {
    resetHistory();
    seedHistory(REPLY, ["oldest", "middle", "newest"]);
    const box = freshInput("flow-reply-input");
    app.bindMessageHistory("flow-reply-input");

    // Caret parked on line 2 of a multi-line draft: the arrow is the browser's.
    box.value = "line one\nline two";
    caretAt(box, 12);
    assert.equal(press(box, "ArrowUp"), false, "not consumed — the caret has a line above it");
    assert.equal(box.value, "line one\nline two", "the box is untouched");

    // Same box, caret on the first line: history takes over.
    caretAt(box, 3);
    assert.equal(press(box, "ArrowUp"), true);
    assert.equal(box.value, "newest");
    assert.equal(press(box, "ArrowUp"), true);
    assert.equal(box.value, "middle");
    assert.equal(press(box, "ArrowUp"), true);
    assert.equal(box.value, "oldest");
    // Past the oldest there is nothing to recall, so the key is left alone.
    assert.equal(press(box, "ArrowUp"), false);
    assert.equal(box.value, "oldest");
    resetHistory();
  });

  check("G2 ↓ walks forward only from the last line, and restores the stashed edit", () => {
    resetHistory();
    seedHistory(REPLY, ["older", "newer"]);
    const box = freshInput("flow-reply-input");
    app.bindMessageHistory("flow-reply-input");

    box.value = "half written answer";
    caretAt(box, 19);
    // ↓ while still editing is not history — there is nothing forward of it.
    assert.equal(press(box, "ArrowDown"), false);
    assert.equal(box.value, "half written answer");

    press(box, "ArrowUp");
    assert.equal(box.value, "newer");
    press(box, "ArrowUp");
    assert.equal(box.value, "older");
    // A recalled entry leaves the caret at the end, so ↓ is at the last line.
    assert.equal(box.selectionStart, "older".length);
    assert.equal(press(box, "ArrowDown"), true);
    assert.equal(box.value, "newer");
    // One more ↓ walks past the newest entry and hands back the stash.
    assert.equal(press(box, "ArrowDown"), true);
    assert.equal(box.value, "half written answer", "the unsent edit came back");
    // And we are out of history again: another ↓ is the browser's.
    assert.equal(press(box, "ArrowDown"), false);

    // ↓ mid-text (a line below the caret) is caret movement, not history.
    box.value = "line one\nline two";
    caretAt(box, 2);
    assert.equal(press(box, "ArrowDown"), false);
    assert.equal(box.value, "line one\nline two");
    resetHistory();
  });

  check("G2 the two channels never cross", () => {
    resetHistory();
    seedHistory(REPLY, ["answer to the flow"]);
    seedHistory(NEW_TASK, ["build the thing"]);
    const reply = freshInput("flow-reply-input");
    const task = freshInput("nt-task");
    app.bindMessageHistory("flow-reply-input");
    app.bindMessageHistory("nt-task");

    press(reply, "ArrowUp");
    press(task, "ArrowUp");
    assert.equal(reply.value, "answer to the flow");
    assert.equal(task.value, "build the thing");
    // Their cursors are independent too: walking one back does not move the
    // other's position in its own list.
    assert.equal(app.historyNavState("flow-reply-input").cursor, 1);
    assert.equal(app.historyNavState("nt-task").cursor, 1);
    assert.equal(app.historyChannelForInput("issue-description"), "", "the issue modal has no channel");
    assert.equal(app.historyChannelForInput("issue-title"), "");
    resetHistory();
  });

  check("G2 a recalled entry re-measures the auto-grow textarea", () => {
    resetHistory();
    seedHistory(REPLY, ["a\nb\nc\nd"]);
    withMeasuredReplyBox((input) => {
      input._listeners = {};
      input.value = "";
      input.style.height = "";
      caretAt(input, 0);
      app.bindMessageHistory("flow-reply-input");
      assert.equal(press(input, "ArrowUp"), true);
      // 4 lines * 20 + 20 = 100px; an un-measured box would still sit at the
      // single-line floor it was reset to.
      assert.equal(input.style.height, "100px", "height tracks the recalled entry");
      // Walking back down to the (empty) stash collapses it again.
      caretAt(input, input.value.length);
      assert.equal(press(input, "ArrowDown"), true);
      assert.equal(input.value, "");
      assert.equal(input.style.height, "40px");
    });
    resetHistory();
  });

  check("G2 Ctrl/Cmd+Enter still submits and a modified arrow is left to the browser", () => {
    resetHistory();
    seedHistory(REPLY, ["recallable"]);
    const box = freshInput("flow-reply-input");
    // The binding init() installs BEFORE the history one — the check is that
    // adding history navigation leaves it intact and firing.
    let submits = 0;
    box.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
        e.preventDefault();
        submits += 1;
      }
    });
    app.bindMessageHistory("flow-reply-input");
    assert.equal(box._listeners.keydown.length, 2, "history is a SECOND listener, not a replacement");

    box.value = "typed";
    caretAt(box, 5);
    assert.equal(press(box, "Enter", { ctrlKey: true }), true);
    assert.equal(press(box, "Enter", { metaKey: true }), true);
    assert.equal(submits, 2, "both chords still submit");
    assert.equal(box.value, "typed", "and neither one recalled anything");

    // Modified arrows (selection / word jumps) stay the browser's.
    for (const mod of ["shiftKey", "ctrlKey", "metaKey", "altKey"]) {
      const extra = {};
      extra[mod] = true;
      assert.equal(press(box, "ArrowUp", extra), false, `${mod}+ArrowUp is not history`);
      assert.equal(box.value, "typed");
    }
    // The unmodified one still is.
    assert.equal(press(box, "ArrowUp"), true);
    assert.equal(box.value, "recallable");
    resetHistory();
  });

  // ---- lazy load + degradation ------------------------------------------

  await checkAsync("G2 history is lazy: nothing is fetched until the box is touched", async () => {
    resetHistory();
    const f = installFetch({ historyEntries: ["from the server"] });
    try {
      const box = freshInput("flow-reply-input");
      app.bindMessageHistory("flow-reply-input");
      // Binding alone must not cost a request — the flow view and the New Task
      // modal paint without waiting on history.
      assert.equal(f.history().length, 0, "binding fetches nothing");
      box.dispatch("focus", {});
      await flush();
      assert.equal(f.history().length, 1, "the first focus loads it");
      assert.equal(f.history()[0].url, "/api/message-history/flow-reply");
      // A second focus does not re-fetch: one attempt per session.
      box.dispatch("focus", {});
      await flush();
      assert.equal(f.history().length, 1);
      caretAt(box, 0);
      assert.equal(press(box, "ArrowUp"), true);
      assert.equal(box.value, "from the server");
    } finally {
      f.restore();
      resetHistory();
    }
  });

  await checkAsync("G2 an unreachable history endpoint degrades to this session's own list", async () => {
    resetHistory();
    const f = installFetch({ fail: true });
    try {
      const box = freshInput("flow-reply-input");
      app.bindMessageHistory("flow-reply-input");
      // The rejected load must not throw out of the app, and must not leave the
      // channel wedged in "loading" forever.
      await app.ensureMessageHistoryLoaded(REPLY);
      assert.equal(app.historyChannelState(REPLY).loaded, true);

      // The box is fully usable: text goes in, ↑ finds nothing (nothing was
      // ever sent this session), and the keystroke is left to the browser.
      box.value = "still typing";
      caretAt(box, 0);
      assert.equal(press(box, "ArrowUp"), false);
      assert.equal(box.value, "still typing");

      // Whatever this session delivers is recalled from memory even though the
      // POST cannot land either.
      app.recordMessageHistory(REPLY, "sent while offline");
      await flush();
      // No POST could land, so the entry stays unnamed — and stays in the list.
      assert.equal(app.historyChannelState(REPLY).entries[0].serverId, null);
      box.value = "";
      caretAt(box, 0);
      assert.equal(press(box, "ArrowUp"), true);
      assert.equal(box.value, "sent while offline");
    } finally {
      f.restore();
      resetHistory();
    }
  });

  await checkAsync("G2 a 401 from the history endpoint is not an auth transition", async () => {
    resetHistory();
    const f = installFetch({ status: 401 });
    const savedAuth = app.state.authState;
    try {
      const box = freshInput("nt-task");
      app.bindMessageHistory("nt-task");
      box.dispatch("focus", {});
      await flush();
      // Best-effort background bookkeeping must not drag the whole console back
      // to the login gate — the endpoints that deliver text still do that.
      assert.equal(app.state.authState, savedAuth, "no auth-state transition from a history 401");
      assert.equal(app.historyChannelState(NEW_TASK).entries.length, 0);
      box.value = "a brand new task";
      caretAt(box, 0);
      assert.equal(press(box, "ArrowUp"), false, "nothing to recall, nothing consumed");
      assert.equal(box.value, "a brand new task", "the box still takes text");
    } finally {
      f.restore();
      app.state.authState = savedAuth;
      resetHistory();
    }
  });

  await checkAsync("G2 the remote list merges under this session's sends without duplicating", async () => {
    resetHistory();
    // The POST races the GET, so the server's answer may or may not already
    // contain what this session just sent. Neither outcome may duplicate it —
    // and in both the id, not the text, is what decides.
    //
    // Here the send is an adjacent repeat of the server's newest row, so the
    // server folds it and names that row: the local entry IS that row.
    const f = installFetch({ historyEntries: ["older remote", "sent just now"] });
    try {
      app.recordMessageHistory(REPLY, "sent just now");
      await flush();
      await app.ensureMessageHistoryLoaded(REPLY);
      await flush();
      assert.deepEqual(texts(REPLY), ["older remote", "sent just now"]);
      assert.equal(f.server.rows.length, 2, "the fold created no row");
    } finally {
      f.restore();
      resetHistory();
    }
    // ...and here the POST creates a row the (stale) answer cannot contain, so
    // the entry is appended once rather than folded into the older list.
    const g = installFetch({ historyEntries: ["older remote"] });
    try {
      g.server.snapshot = g.server.rows.map((r) => ({ id: r.id, text: r.text }));
      app.recordMessageHistory(REPLY, "sent just now");
      await flush();
      await app.ensureMessageHistoryLoaded(REPLY);
      await flush();
      assert.deepEqual(texts(REPLY), ["older remote", "sent just now"]);
    } finally {
      g.restore();
      resetHistory();
    }
  });

  check("G2 orderHistoryMerge folds on entry id and never on text", () => {
    const remote = (pairs) => pairs.map(([id, text]) => ({ id, text }));
    const mine = (pairs) => pairs.map(([serverId, text]) =>
      app.normalizeHistoryEntry({ text, serverId }));
    const of = (list) => list.map((e) => e.text);

    // The POST/GET race, in its plain form: this session sent C and D, the
    // answer already carries both rows, and their ids say so.
    assert.deepEqual(
      of(app.orderHistoryMerge(remote([[1, "A"], [2, "C"], [3, "D"]]), mine([[2, "C"], [3, "D"]]))),
      ["A", "C", "D"],
    );
    // A partial overlap: the answer caught C but not D.
    assert.deepEqual(
      of(app.orderHistoryMerge(remote([[1, "A"], [2, "C"]]), mine([[2, "C"], [3, "D"]]))),
      ["A", "C", "D"],
    );
    // Another device appended after this session's entries landed.
    assert.deepEqual(
      of(app.orderHistoryMerge(remote([[1, "C"], [2, "D"], [3, "E"]]), mine([[1, "C"], [2, "D"]]))),
      ["C", "D", "E"],
    );
    // THE case a text alignment gets wrong. A stale answer [C, D] arrives while
    // this session has just sent a THIRD entry that also reads "C". Its id is 3
    // and the answer holds 1 and 2, so it is a different append and survives —
    // a non-adjacent repeat is a real entry, and dropping it would make the
    // newest delivered message unrecallable.
    assert.deepEqual(
      of(app.orderHistoryMerge(remote([[1, "C"], [2, "D"]]), mine([[3, "C"]]))),
      ["C", "D", "C"],
    );
    // ...and the same shape where the id DOES match is one append, shown once.
    assert.deepEqual(
      of(app.orderHistoryMerge(remote([[1, "C"], [2, "D"]]), mine([[1, "C"]]))),
      ["C", "D"],
    );
    // Rule 4: an entry the server never named — its POST failed, or has not
    // answered yet — is kept on local send order, however familiar it reads.
    assert.deepEqual(
      of(app.orderHistoryMerge(remote([[1, "C"], [2, "D"]]), mine([[null, "C"]]))),
      ["C", "D", "C"],
    );
    // A server that predates entry ids leaves every row unnamed; nothing can
    // collapse then, which is the safe direction (a duplicate shown beats a
    // delivered message swallowed).
    assert.deepEqual(
      of(app.orderHistoryMerge(["C", "D"], mine([[null, "C"]]))),
      ["C", "D", "C"],
    );
    // Degenerate ends, and non-list input.
    assert.deepEqual(of(app.orderHistoryMerge([], ["C", "D"])), ["C", "D"]);
    assert.deepEqual(of(app.orderHistoryMerge(["A", "B"], [])), ["A", "B"]);
    assert.deepEqual(app.orderHistoryMerge(null, null), []);
    // Blanks are still not messages.
    assert.deepEqual(of(app.orderHistoryMerge(["A"], ["  ", "B"])), ["A", "B"]);
    // The fold only orders; the cap is applied to its result, from the old end.
    const savedMax = app.MSG_HISTORY.MAX_ENTRIES;
    try {
      app.MSG_HISTORY.MAX_ENTRIES = 2;
      const st = { entries: [], dropped: [] };
      app.installHistoryEntries(st, app.orderHistoryMerge(["A", "B", "C"], ["D"]));
      assert.deepEqual(of(st.entries), ["C", "D"]);
    } finally {
      app.MSG_HISTORY.MAX_ENTRIES = savedMax;
    }
    // A row read twice in one answer is still one row.
    assert.deepEqual(
      of(app.orderHistoryMerge(remote([[1, "C"], [1, "C"]]), [])),
      ["C"],
    );
  });

  check("G2 orderHistoryMerge anchors an unnamed send after the history it was sent into", () => {
    const remote = (pairs) => pairs.map(([id, text]) => ({ id, text }));
    const mine = (pairs) => pairs.map(([serverId, text]) =>
      app.normalizeHistoryEntry({ text, serverId }));
    const of = (list) => list.map((e) => e.text);

    // "A" is a row this session already had named, so it pins where the local
    // list and the answer line up. "L" was sent into a channel that ended at
    // "A"; "R" reached the server afterwards, from somewhere else. Ordering L
    // behind R would re-date this session's own send on nothing but the moment
    // the answer happened to arrive.
    assert.deepEqual(
      of(app.orderHistoryMerge(remote([[1, "A"], [2, "R"]]), mine([[1, "A"], [null, "L"]]))),
      ["A", "L", "R"],
    );
    // Several unnamed sends, each anchored to the pin it followed, and in send
    // order among themselves.
    assert.deepEqual(
      of(app.orderHistoryMerge(
        remote([[1, "A"], [2, "B"], [3, "R"]]),
        mine([[1, "A"], [null, "L1"], [null, "L2"], [2, "B"], [null, "L3"]]),
      )),
      ["A", "L1", "L2", "B", "L3", "R"],
    );
    // Sent BEFORE a row the answer names: it belongs in front of that row, not
    // shuffled behind it.
    assert.deepEqual(
      of(app.orderHistoryMerge(remote([[1, "A"], [2, "R"]]), mine([[null, "L"], [1, "A"]]))),
      ["L", "A", "R"],
    );
    // No pin anywhere in the local list — nothing says where the send sat, and
    // the only thing known about it is that this session sent it, so it stays
    // the newest entry.
    assert.deepEqual(
      of(app.orderHistoryMerge(remote([[1, "A"], [2, "R"]]), mine([[null, "L"]]))),
      ["A", "R", "L"],
    );
  });

  await checkAsync("G2 a two-entry overlap between the GET and this session is recalled once", async () => {
    resetHistory();
    const f = installFetch({ historyEntries: ["older remote"] });
    try {
      // Both POSTs landed before the delayed GET answered, so the response
      // already carries them — appending blindly would recall each twice.
      app.recordMessageHistory(REPLY, "first sent");
      app.recordMessageHistory(REPLY, "second sent");
      await flush();
      await app.ensureMessageHistoryLoaded(REPLY);
      await flush();
      assert.deepEqual(texts(REPLY), ["older remote", "first sent", "second sent"]);
    } finally {
      f.restore();
      resetHistory();
    }
  });

  await checkAsync("G2 the FIRST ↑ recalls once the pending load lands", async () => {
    resetHistory();
    const saved = globalThis.fetch;
    let release = () => {};
    globalThis.fetch = () =>
      new Promise((resolve) => {
        release = () =>
          resolve({ ok: true, status: 200, json: async () => ({ entries: ["from the server"] }) });
      });
    try {
      const box = freshInput("flow-reply-input");
      app.bindMessageHistory("flow-reply-input");
      caretAt(box, 0);
      // Pressing ↑ before the GET returns IS the normal first interaction with
      // the box — the same touch that triggers the lazy load. The key has to
      // survive the round trip instead of silently recalling nothing.
      assert.equal(press(box, "ArrowUp"), true, "the key is taken, not left to the browser");
      assert.equal(box.value, "", "nothing can be recalled yet");
      release();
      await flush();
      assert.equal(box.value, "from the server", "the step is replayed when the list lands");
      assert.equal(app.historyNavState("flow-reply-input").cursor, 1);
    } finally {
      globalThis.fetch = saved;
      resetHistory();
    }
  });

  await checkAsync("G2 a replay that lost its race does not overwrite newer text", async () => {
    resetHistory();
    const saved = globalThis.fetch;
    let release = () => {};
    globalThis.fetch = () =>
      new Promise((resolve) => {
        release = () =>
          resolve({ ok: true, status: 200, json: async () => ({ entries: ["stale recall"] }) });
      });
    try {
      const box = freshInput("nt-task");
      app.bindMessageHistory("nt-task");
      caretAt(box, 0);
      press(box, "ArrowUp");
      // The operator went on typing while the list was still in flight.
      box.value = "a task I am writing";
      release();
      await flush();
      assert.equal(box.value, "a task I am writing", "a late arrival never clobbers newer text");
    } finally {
      globalThis.fetch = saved;
      resetHistory();
    }
  });

  await checkAsync("G2 a stale answer does not swallow a message sent after the request left", async () => {
    resetHistory();
    const saved = globalThis.fetch;
    let release = () => {};
    globalThis.fetch = (url, init) => {
      if (init && init.method === "POST") {
        // The server's newest row is "D", so this "C" is a genuine third entry
        // and is given its own id — the fact that makes it survive the merge.
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => ({ status: "appended", appended: true, entry_id: 3 }),
        });
      }
      return new Promise((resolve) => {
        release = () =>
          resolve({
            ok: true,
            status: 200,
            json: async () => ({ entries: [{ id: 1, text: "C" }, { id: 2, text: "D" }] }),
          });
      });
    };
    try {
      const pending = app.ensureMessageHistoryLoaded(REPLY);
      // Sent AFTER the GET left, so the answer below cannot be carrying it —
      // its "C" is an older, non-adjacent occurrence, not this session's send.
      app.recordMessageHistory(REPLY, "C");
      release();
      await pending;
      await flush();
      assert.deepEqual(texts(REPLY), ["C", "D", "C"], "the newest send stays recallable");
    } finally {
      globalThis.fetch = saved;
      resetHistory();
    }
  });

  await checkAsync("G2 a delivered message is posted even when the cache already ends with it", async () => {
    resetHistory();
    const f = installFetch({ historyEntries: ["same words"] });
    try {
      seedFromServer(REPLY, f);
      // The cached list is a snapshot; another device may have appended since
      // it was taken, so only the server can decide this is a repeat of the
      // *actual* previous entry. A local veto here loses the entry for good.
      app.recordMessageHistory(REPLY, "same words");
      await flush();
      const posts = f.history().filter((c) => c.init.method === "POST");
      assert.equal(posts.length, 1, "the server still hears about it");
      assert.deepEqual(posts[0].body, { text: "same words" });
      // The server folded it onto the row the cache already held, and said so
      // by naming that row — so the list is unchanged and shows one entry.
      assert.deepEqual(texts(REPLY), ["same words"]);
      assert.equal(f.server.rows.length, 1);
      // Blank text is still not a message on either side.
      app.recordMessageHistory(REPLY, "   ");
      await flush();
      assert.equal(f.history().filter((c) => c.init.method === "POST").length, 1);
    } finally {
      f.restore();
      resetHistory();
    }
  });

  await checkAsync("G2 a hung history GET never swallows a recall this session can serve", async () => {
    resetHistory();
    const saved = globalThis.fetch;
    // A request that never answers at all — the failure a plain rejection does
    // not model, and the one an arrow key must not be held behind.
    globalThis.fetch = (url, init) => {
      if (init && init.method === "POST") {
        return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
      }
      return new Promise(() => {});
    };
    try {
      withNoTimers(() => {
        const box = freshInput("flow-reply-input");
        app.bindMessageHistory("flow-reply-input");
        box.dispatch("focus", {});
        app.recordMessageHistory(REPLY, "sent while the GET hangs");
        caretAt(box, 0);
        assert.equal(press(box, "ArrowUp"), true, "the key is taken...");
        assert.equal(
          box.value,
          "sent while the GET hangs",
          "...and the in-memory entry is recalled at once, not queued forever",
        );
      });
    } finally {
      globalThis.fetch = saved;
      resetHistory();
    }
  });

  await checkAsync("G2 two ↑ pressed before the load lands walk two entries, not one", async () => {
    resetHistory();
    const saved = globalThis.fetch;
    let release = () => {};
    globalThis.fetch = () =>
      new Promise((resolve) => {
        release = () =>
          resolve({ ok: true, status: 200, json: async () => ({ entries: ["older", "newer"] }) });
      });
    try {
      const box = freshInput("flow-reply-input");
      app.bindMessageHistory("flow-reply-input");
      caretAt(box, 0);
      assert.equal(press(box, "ArrowUp"), true);
      assert.equal(press(box, "ArrowUp"), true, "the second key is taken too");
      release();
      await flush();
      // Both keys were consumed, so both have to count: one ↑ per entry, or
      // the operator silently loses a step they already paid for.
      assert.equal(box.value, "older");
      assert.equal(app.historyNavState("flow-reply-input").cursor, 2);
    } finally {
      globalThis.fetch = saved;
      resetHistory();
    }
  });

  await checkAsync("G2 a ↓ cancels a ↑ still queued behind the load", async () => {
    resetHistory();
    const saved = globalThis.fetch;
    let release = () => {};
    globalThis.fetch = () =>
      new Promise((resolve) => {
        release = () =>
          resolve({ ok: true, status: 200, json: async () => ({ entries: ["taken back"] }) });
      });
    try {
      const box = freshInput("nt-task");
      app.bindMessageHistory("nt-task");
      caretAt(box, 0);
      assert.equal(press(box, "ArrowUp"), true);
      assert.equal(press(box, "ArrowDown"), true, "the ↓ takes the queued ↑ back");
      release();
      await flush();
      assert.equal(box.value, "", "the pair nets out to the editing state");
      assert.equal(app.historyNavState("nt-task").cursor, 0);
    } finally {
      globalThis.fetch = saved;
      resetHistory();
    }
  });

  await checkAsync("G2 a POST answered after a sign-out does not seed the next owner", async () => {
    resetHistory();
    const f = installFetch();
    try {
      app.recordMessageHistory(REPLY, "the previous owner's words");
      // The answer is still in flight when the session ends.
      app.clearMessageHistoryCache();
      await flush();
      assert.deepEqual(texts(REPLY), [], "the next owner starts empty and stays empty");
    } finally {
      f.restore();
      resetHistory();
    }
  });

  await checkAsync("G2 a GET answered after a sign-out does not touch the next owner", async () => {
    resetHistory();
    const saved = globalThis.fetch;
    let release = () => {};
    globalThis.fetch = () =>
      new Promise((resolve) => {
        release = () =>
          resolve({
            ok: true,
            status: 200,
            // Deliberately SHORTER than the next owner's list: a rebase run
            // against it would clamp a cursor of 3 into a list of 1.
            json: async () => ({ entries: [{ id: 41, text: "the previous owner's only line" }] }),
          });
      });
    try {
      // Owner A focuses the box; the lazy read is still in flight when the
      // session ends.
      const pending = app.ensureMessageHistoryLoaded(REPLY);
      app.clearMessageHistoryCache();

      // Owner B signs in on the same browser, loads their own history and
      // walks it to the oldest entry.
      seedHistory(REPLY, ["B one", "B two", "B three"], 7001);
      const box = freshInput("flow-reply-input");
      app.bindMessageHistory("flow-reply-input");
      caretAt(box, 0);
      press(box, "ArrowUp");
      press(box, "ArrowUp");
      press(box, "ArrowUp");
      assert.equal(box.value, "B one", "B is showing their oldest entry");

      release();
      await pending;
      await flush();

      assert.deepEqual(texts(REPLY), ["B one", "B two", "B three"], "A's rows never reach B's list");
      assert.equal(
        app.historyNavState("flow-reply-input").cursor, 3,
        "B's cursor still names the entry B's box is showing",
      );
      // The proof that matters at the keyboard: ↓ moves to the chronologically
      // adjacent entry rather than to whatever a clamped cursor pointed at.
      assert.equal(press(box, "ArrowDown"), true);
      assert.equal(box.value, "B two");
    } finally {
      globalThis.fetch = saved;
      resetHistory();
    }
  });

  check("G2 signing out drops the cached history", () => {
    seedHistory(REPLY, ["one owner's words"]);
    app.historyNavState("flow-reply-input").cursor = 1;
    app.clearMessageHistoryCache();
    assert.deepEqual(app.historyChannelState(REPLY).entries, [], "the next owner starts empty");
    assert.equal(app.historyNavState("flow-reply-input").cursor, 0);
    resetHistory();
  });

  // ---- the four success paths -------------------------------------------

  await checkAsync("G2 push path 1/4 — a delivered reply enters the reply channel", async () => {
    resetHistory();
    const f = installFetch();
    try {
      await withNoTimers(async () => {
        armFlow("F-hist-1", "c1", "call");
        await app.sendReply("F-hist-1", { id: "call:c1", kind: "call", callId: "c1" }, "the answer");
        await flush();
      });
      assert.deepEqual(texts(REPLY), ["the answer"]);
      const posts = f.history().filter((c) => c.init.method === "POST");
      assert.equal(posts.length, 1);
      assert.equal(posts[0].url, "/api/message-history/flow-reply");
      assert.deepEqual(posts[0].body, { text: "the answer" });
    } finally {
      f.restore();
      app.settlePendingSend();
      resetHistory();
    }
  });

  await checkAsync("G2 push path 2/4 — a delivered interject shares that channel", async () => {
    resetHistory();
    const f = installFetch();
    try {
      await withNoTimers(async () => {
        armFlow("F-hist-2", "i1", "interjection");
        await app.sendReply(
          "F-hist-2",
          { id: "interjection:new", kind: "interjection", synthetic: true, callId: null },
          "stop and look at this",
        );
        await flush();
      });
      // One textarea, one conversation: recall walks respond and interject
      // together rather than splitting them into two lists.
      assert.deepEqual(texts(REPLY), ["stop and look at this"]);
      assert.deepEqual(texts(NEW_TASK), []);
    } finally {
      f.restore();
      app.state.flowSyntheticInterjectPending = false;
      app.state.flowInterjectRequested = false;
      app.settlePendingSend();
      resetHistory();
    }
  });

  await checkAsync("G2 push path 3/4 — a rejection note enters history, a bare approval does not", async () => {
    resetHistory();
    const f = installFetch();
    try {
      await withNoTimers(async () => {
        armFlow("F-hist-3", "cf1", "confirm");
        const target = { id: "call:cf1", kind: "confirm", callId: "cf1" };
        await app.sendConfirmDecision("F-hist-3", target, false, "  基线方向反了  ");
        await flush();
        app.settlePendingSend();
        await app.sendConfirmDecision("F-hist-3", target, true, null);
        await flush();
        app.settlePendingSend();
        await app.sendConfirmDecision("F-hist-3", target, true, "   ");
        await flush();
      });
      // The note is trimmed exactly as it was sent, and a decision carrying no
      // text adds nothing to recall.
      assert.deepEqual(texts(REPLY), ["基线方向反了"]);
    } finally {
      f.restore();
      app.settlePendingSend();
      resetHistory();
    }
  });

  await checkAsync("G2 push path 4/4 — a published task enters the new-task channel", async () => {
    resetHistory();
    const f = installFetch();
    const savedMachines = app.state.machines;
    try {
      document.getElementById("nt-machine").value = "m1";
      document.getElementById("nt-project").value = "/srv/proj";
      document.getElementById("nt-type").value = "feature";
      document.getElementById("nt-task").value = "build the thing";
      app.attachmentEntries("nt-attachments").length = 0;
      // /api/flows answers 202 (published); the history POST rides after it.
      const saved = globalThis.fetch;
      globalThis.fetch = async (url, init) => {
        const u = String(url);
        f.calls.push({ url: u, init: init || {}, body: init && init.body ? JSON.parse(init.body) : null });
        if (u.includes("/api/message-history/")) {
          const opts = init || {};
          const body = opts.method === "POST"
            ? f.server.append(JSON.parse(opts.body || "{}").text)
            : { entries: f.server.read() };
          return { ok: true, status: 200, json: async () => body };
        }
        return { ok: true, status: 202, json: async () => ({}) };
      };
      await app.submitNewTask({ preventDefault: () => {} });
      await flush();
      globalThis.fetch = saved;
      assert.deepEqual(texts(NEW_TASK), ["build the thing"]);
      assert.deepEqual(texts(REPLY), [], "not the reply channel");
      const posts = f.history().filter((c) => c.init.method === "POST");
      assert.equal(posts.length, 1);
      assert.equal(posts[0].url, "/api/message-history/new-task");
    } finally {
      f.restore();
      app.state.machines = savedMachines;
      resetHistory();
    }
  });

  await checkAsync("G2 a FAILED send records nothing — undelivered text is not history", async () => {
    resetHistory();
    const saved = globalThis.fetch;
    const seen = [];
    globalThis.fetch = async (url, init) => {
      seen.push(String(url));
      return { ok: false, status: 500, json: async () => ({ detail: "boom" }) };
    };
    try {
      await withNoTimers(async () => {
        armFlow("F-hist-fail", "c9", "call");
        await app.sendReply("F-hist-fail", { id: "call:c9", kind: "call", callId: "c9" }, "never landed");
        await flush();
      });
      assert.deepEqual(texts(REPLY), []);
      assert.equal(seen.some((u) => u.includes("/api/message-history/")), false, "no history write either");
    } finally {
      globalThis.fetch = saved;
      app.settlePendingSend();
      resetHistory();
    }
  });

  await checkAsync("G2 a send resets the cursor so the next ↑ starts at the newest entry", async () => {
    resetHistory();
    const f = installFetch();
    try {
      seedHistory(REPLY, ["much older"]);
      const box = freshInput("flow-reply-input");
      app.bindMessageHistory("flow-reply-input");
      // Walk into history first, so the cursor is NOT at the editing state.
      caretAt(box, 0);
      press(box, "ArrowUp");
      assert.equal(app.historyNavState("flow-reply-input").cursor, 1);

      await withNoTimers(async () => {
        armFlow("F-hist-cursor", "c7", "call");
        await app.sendReply("F-hist-cursor", { id: "call:c7", kind: "call", callId: "c7" }, "just sent");
        await flush();
      });
      const nav = app.historyNavState("flow-reply-input");
      assert.equal(nav.cursor, 0, "back to the editing state");
      assert.equal(nav.stash, "");
      caretAt(box, 0);
      press(box, "ArrowUp");
      assert.equal(box.value, "just sent", "the first ↑ finds what was just sent");
    } finally {
      f.restore();
      app.settlePendingSend();
      resetHistory();
    }
  });

  // ---- identity across the POST/GET race --------------------------------

  await checkAsync("G2 a fold's id collapses this session's entry onto the server's row", async () => {
    resetHistory();
    const f = installFetch({ historyEntries: ["C", "D"] });
    try {
      // The send repeats the server's newest row, so the server folds it and
      // names that row. The id — not the matching text — is what says the local
      // entry and row 2 are one and the same append.
      app.recordMessageHistory(REPLY, "D");
      await flush();
      assert.deepEqual(texts(REPLY), ["D"], "this session's own entry, so far alone");
      assert.equal(
        app.historyChannelState(REPLY).entries[0].serverId, 2,
        "bound to the row the fold named",
      );
      await app.ensureMessageHistoryLoaded(REPLY);
      await flush();
      assert.deepEqual(texts(REPLY), ["C", "D"], "one append, shown once");
      assert.equal(f.server.rows.length, 2, "and no row was created for it");
    } finally {
      f.restore();
      resetHistory();
    }
  });

  await checkAsync("G2 the server's verdict wins when the cached list vetoed a repeat", async () => {
    resetHistory();
    // The cache ends with "same words" so the local repeat rule refuses the
    // entry — but another device has appended since that snapshot was taken,
    // so the server's real newest row is something else and it accepts. The
    // side that performs the append judges it, and here that is the server.
    const f = installFetch({ historyEntries: ["same words", "from another device"] });
    try {
      seedHistory(REPLY, ["same words"], 1);
      app.recordMessageHistory(REPLY, "same words");
      await flush();
      assert.deepEqual(texts(REPLY), ["same words", "same words"]);
      assert.equal(
        app.historyChannelState(REPLY).entries[1].serverId, 3,
        "added with the id the server gave it, so a later read folds it once",
      );
    } finally {
      f.restore();
      resetHistory();
    }
  });

  await checkAsync("G2 a send awaiting its id stays recallable, and folds only once the POST fails", async () => {
    resetHistory();
    const saved = globalThis.fetch;
    let settle = null;
    const posted = [];
    globalThis.fetch = (url, init) => {
      if (init && init.method === "POST") {
        posted.push(JSON.parse(init.body || "{}"));
        // Committed on the server, but the answer never comes back — the
        // failure a rejection does not model.
        return new Promise((resolve, reject) => { settle = { resolve, reject }; });
      }
      return Promise.resolve({ ok: true, status: 200, json: async () => ({ entries: [] }) });
    };
    try {
      // The cached list ends with "A", but the server's real newest entry may
      // be something else entirely, so this send may well be a legitimate
      // non-adjacent repeat. Only the side that performs the append judges it,
      // and while the POST is in flight that side is the server.
      seedHistory(REPLY, ["A"], 1);
      const box = freshInput("flow-reply-input");
      app.bindMessageHistory("flow-reply-input");
      app.recordMessageHistory(REPLY, "A");
      await flush();
      assert.deepEqual(posted, [{ text: "A" }], "the server hears about it");
      assert.deepEqual(texts(REPLY), ["A", "A"], "the pending append keeps its place in send order");
      caretAt(box, 0);
      assert.equal(press(box, "ArrowUp"), true);
      assert.equal(box.value, "A", "and is recallable while the answer is outstanding");
      // The request fails: NOW the append is the client's own to judge, and its
      // adjacent-repeat rule — the degraded, client-only rule — folds it away.
      settle.reject(new Error("network down"));
      await flush();
      assert.deepEqual(texts(REPLY), ["A"], "the local rule applies only after the degradation");
    } finally {
      globalThis.fetch = saved;
      resetHistory();
    }
  });

  await checkAsync("G2 a failed POST is judged against what the append followed, not a merged neighbour", async () => {
    resetHistory();
    const saved = globalThis.fetch;
    let rejectPost = () => {};
    let releaseGet = () => {};
    globalThis.fetch = (url, init) => {
      if (init && init.method === "POST") {
        return new Promise((_resolve, reject) => {
          rejectPost = () => reject(new Error("network down"));
        });
      }
      return new Promise((resolve) => {
        releaseGet = () => resolve({
          ok: true,
          status: 200,
          json: async () => ({ entries: [{ id: 1, text: "A" }, { id: 2, text: "B" }] }),
        });
      });
    };
    try {
      freshInput("flow-reply-input");
      app.bindMessageHistory("flow-reply-input");
      // The session's list ends at "A", so sending "B" is no repeat at all.
      seedHistory(REPLY, ["A"], 1);
      app.historyChannelState(REPLY).loaded = false;
      app.recordMessageHistory(REPLY, "B");
      await flush();
      // The load lands while the POST is still out, carrying another device's
      // own "B" — a row this append shares no id with.
      const load = app.ensureMessageHistoryLoaded(REPLY);
      releaseGet();
      await load;
      await flush();
      assert.deepEqual(
        texts(REPLY), ["A", "B", "B"],
        "the send stays anchored after the entry it was appended to",
      );
      // Only now is the append the client's to judge — against "A", what it
      // actually followed. A text match with the row that merely landed beside
      // it is exactly the inference the merge rules forbid.
      rejectPost();
      await flush();
      assert.deepEqual(texts(REPLY), ["A", "B", "B"]);
    } finally {
      globalThis.fetch = saved;
      resetHistory();
    }
  });

  await checkAsync("G2 a failed POST beside an identical server row keeps the send", async () => {
    resetHistory();
    const saved = globalThis.fetch;
    let rejectPost = () => {};
    globalThis.fetch = (url, init) => {
      if (init && init.method === "POST") {
        return new Promise((_resolve, reject) => {
          rejectPost = () => reject(new Error("network down"));
        });
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({ entries: [{ id: 1, text: "B" }] }),
      });
    };
    try {
      freshInput("flow-reply-input");
      app.bindMessageHistory("flow-reply-input");
      // Nothing was known when "B" went out, so it followed nothing and can be
      // no repeat of anything. The answer then puts an unrelated "B" in front
      // of it — a different append, as its id says.
      app.recordMessageHistory(REPLY, "B");
      await flush();
      await app.ensureMessageHistoryLoaded(REPLY);
      await flush();
      assert.deepEqual(texts(REPLY), ["B", "B"]);
      rejectPost();
      await flush();
      assert.deepEqual(
        texts(REPLY), ["B", "B"],
        "rule 4: an entry the server never named is kept, however familiar its neighbour reads",
      );
    } finally {
      globalThis.fetch = saved;
      resetHistory();
    }
  });

  await checkAsync("G2 a delayed fold keeps the traversal on the row it folded onto", async () => {
    resetHistory();
    const saved = globalThis.fetch;
    let releasePost = () => {};
    globalThis.fetch = (url, init) => {
      if (init && init.method === "POST") {
        // Held open until the GET has already delivered the server's own copy
        // of this very send — the race the entry id exists to settle.
        return new Promise((resolve) => {
          releasePost = () =>
            resolve({
              ok: true,
              status: 200,
              json: async () => ({ status: "skipped", appended: false, entry_id: 2 }),
            });
        });
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          entries: [
            { id: 1, text: "older" },
            { id: 2, text: "L" },
            { id: 3, text: "newer" },
          ],
        }),
      });
    };
    try {
      const box = freshInput("flow-reply-input");
      app.bindMessageHistory("flow-reply-input");
      app.recordMessageHistory(REPLY, "L");
      await app.ensureMessageHistoryLoaded(REPLY);
      await flush();
      // Nothing has named the local entry yet, and text may never fold one —
      // so the session's own "L" sits after the answer's rows (rule 4).
      assert.deepEqual(texts(REPLY), ["older", "L", "newer", "L"]);
      caretAt(box, 0);
      assert.equal(press(box, "ArrowUp"), true);
      assert.equal(box.value, "L", "the operator is standing on their own send");
      releasePost();
      await flush();
      assert.deepEqual(texts(REPLY), ["older", "L", "newer"], "one append, shown once");
      // The box still shows L, so the cursor has to have followed the append
      // onto the row it collapsed into — not stayed at the index that now
      // holds "newer".
      caretAt(box, 0);
      assert.equal(press(box, "ArrowUp"), true);
      assert.equal(box.value, "older", "↑ moves to the entry before the one on screen");
      caretAt(box, box.value.length);
      assert.equal(press(box, "ArrowDown"), true);
      assert.equal(box.value, "L");
      caretAt(box, box.value.length);
      assert.equal(press(box, "ArrowDown"), true);
      assert.equal(box.value, "newer", "and forward one adjacent step, nothing skipped");
    } finally {
      globalThis.fetch = saved;
      resetHistory();
    }
  });

  await checkAsync("G2 an entry evicted by the cap under a live traversal skips nothing", async () => {
    resetHistory();
    const f = installFetch();
    const savedMax = app.MSG_HISTORY.MAX_ENTRIES;
    try {
      // A three-entry cap stands in for the real 500 so the eviction is
      // reachable without seeding a full list.
      app.MSG_HISTORY.MAX_ENTRIES = 3;
      // The default seed base is deliberately far from the ids the fake server
      // hands out: this check both seeds AND posts, and a seeded row sharing an
      // id with the append would read as the row the server folded it onto.
      seedHistory(REPLY, ["E1", "E2", "E3"]);
      const box = freshInput("flow-reply-input");
      app.bindMessageHistory("flow-reply-input");
      for (const expected of ["E3", "E2", "E1"]) {
        caretAt(box, 0);
        assert.equal(press(box, "ArrowUp"), true);
        assert.equal(box.value, expected);
      }
      // A send lands while the operator is standing on the oldest entry, and
      // the box is deliberately left alone (a recalled entry is being read).
      // The cap evicts exactly the entry on screen.
      app.recordMessageHistory(REPLY, "E4", { retired: false });
      await flush();
      assert.deepEqual(texts(REPLY), ["E2", "E3", "E4"], "the displayed entry fell off the old end");
      assert.equal(box.value, "E1", "the box still shows it");
      // Nothing older survives, so ↑ has nowhere to go and the key belongs to
      // the browser...
      caretAt(box, 0);
      assert.equal(press(box, "ArrowUp"), false, "no surviving entry is older");
      // ...while ↓ must recall the entry that was chronologically next after
      // the evicted one, not the one after THAT.
      caretAt(box, box.value.length);
      assert.equal(press(box, "ArrowDown"), true);
      assert.equal(box.value, "E2", "the oldest survivor, not E3");
      caretAt(box, box.value.length);
      assert.equal(press(box, "ArrowDown"), true);
      assert.equal(box.value, "E3");
      caretAt(box, box.value.length);
      assert.equal(press(box, "ArrowDown"), true);
      assert.equal(box.value, "E4", "up to the send that caused the eviction");
    } finally {
      app.MSG_HISTORY.MAX_ENTRIES = savedMax;
      f.restore();
      resetHistory();
    }
  });

  await checkAsync("G2 a pending append that folds gives back the entry the cap displaced", async () => {
    resetHistory();
    const savedMax = app.MSG_HISTORY.MAX_ENTRIES;
    const saved = globalThis.fetch;
    let releasePost = () => {};
    globalThis.fetch = (url, init) => {
      if (init && init.method === "POST") {
        // Held open: while it is, nobody knows yet whether this append is a
        // new row or a fold onto the one the list already ends with.
        return new Promise((resolve) => {
          releasePost = () =>
            resolve({
              ok: true,
              status: 200,
              json: async () => ({ status: "skipped", appended: false, entry_id: 9003 }),
            });
        });
      }
      return Promise.resolve({ ok: true, status: 200, json: async () => ({ entries: [] }) });
    };
    try {
      // A three-entry cap stands in for the real 500, and the window is full.
      app.MSG_HISTORY.MAX_ENTRIES = 3;
      seedHistory(REPLY, ["X", "Y", "A"]);
      app.recordMessageHistory(REPLY, "A");
      await flush();
      assert.deepEqual(
        texts(REPLY), ["Y", "A", "A"],
        "the pending append sits in send order and provisionally pushes X out",
      );
      releasePost();
      await flush();
      // The server folded it onto the row the cache already ended with, so it
      // was never a separate entry — it displaced nothing, and the window is
      // exactly the one the server still holds. Ordering is decided first and
      // the cap applied to the result, not the other way round.
      assert.deepEqual(texts(REPLY), ["X", "Y", "A"], "the displaced entry comes back");
      assert.deepEqual(
        app.historyChannelState(REPLY).dropped, [],
        "and nothing is held behind the window once the verdict is in",
      );
    } finally {
      app.MSG_HISTORY.MAX_ENTRIES = savedMax;
      globalThis.fetch = saved;
      resetHistory();
    }
  });

  await checkAsync("G2 an append displaced by later sends is never resurrected as newest", async () => {
    resetHistory();
    const savedMax = app.MSG_HISTORY.MAX_ENTRIES;
    const saved = globalThis.fetch;
    let releaseFirst = () => {};
    let nextId = 100;
    globalThis.fetch = (url, init) => {
      if (init && init.method === "POST") {
        const sent = JSON.parse(init.body || "{}").text;
        const answer = {
          ok: true,
          status: 200,
          json: async () => ({ status: "appended", appended: true, entry_id: nextId++ }),
        };
        // Only E4's answer is delayed; the sends that overtake it are named at
        // once, so the cap carries E4 off the old end while it is still in
        // flight.
        if (sent === "E4") return new Promise((resolve) => { releaseFirst = () => resolve(answer); });
        return Promise.resolve(answer);
      }
      return Promise.resolve({ ok: true, status: 200, json: async () => ({ entries: [] }) });
    };
    try {
      app.MSG_HISTORY.MAX_ENTRIES = 3;
      for (const sent of ["E4", "E5", "E6", "E7"]) {
        app.recordMessageHistory(REPLY, sent);
        await flush();
      }
      assert.deepEqual(texts(REPLY), ["E5", "E6", "E7"], "three later sends displaced E4");
      // Enough further sends that E4 is past even the overflow the pending
      // append is held in — the answer now comes back to a sequence that has
      // no trace of it left at all.
      for (const sent of ["E8", "E9", "E10"]) {
        app.recordMessageHistory(REPLY, sent);
        await flush();
      }
      assert.deepEqual(texts(REPLY), ["E8", "E9", "E10"]);
      releaseFirst();
      await flush();
      // The answer names an append; it does not move one. Re-adding E4 here
      // would put an old send at the newest end and evict E9, which really is
      // newer — the capped window keeps local send order and the newest three.
      assert.deepEqual(texts(REPLY), ["E8", "E9", "E10"], "the late answer changes nothing");
    } finally {
      app.MSG_HISTORY.MAX_ENTRIES = savedMax;
      globalThis.fetch = saved;
      resetHistory();
    }
  });

  // ---- a list that changes under a live traversal ------------------------

  await checkAsync("G2 a late load re-aims a traversal already in progress", async () => {
    resetHistory();
    const saved = globalThis.fetch;
    let release = () => {};
    globalThis.fetch = (url, init) => {
      if (init && init.method === "POST") {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => ({ status: "appended", appended: true, entry_id: 2 }),
        });
      }
      return new Promise((resolve) => {
        release = () =>
          resolve({
            ok: true,
            status: 200,
            json: async () => ({
              entries: [
                { id: 1, text: "older" },
                { id: 2, text: "L" },
                { id: 3, text: "newer" },
              ],
            }),
          });
      });
    };
    try {
      const box = freshInput("flow-reply-input");
      app.bindMessageHistory("flow-reply-input");
      // This session sent L. The load that will place entries on BOTH sides of
      // it is still in flight when the operator walks back onto it.
      app.recordMessageHistory(REPLY, "L");
      await flush();
      box.value = "half written";
      caretAt(box, 0);
      assert.equal(press(box, "ArrowUp"), true);
      assert.equal(box.value, "L");
      release();
      await flush();
      assert.deepEqual(texts(REPLY), ["older", "L", "newer"]);
      // The box still shows L, so the cursor must still NAME L — not whatever
      // entry its old index now points at. Each arrow from here has to move one
      // chronologically adjacent step.
      caretAt(box, 0);
      assert.equal(press(box, "ArrowUp"), true);
      assert.equal(box.value, "older", "↑ reaches the entry before the one on screen");
      caretAt(box, box.value.length);
      assert.equal(press(box, "ArrowDown"), true);
      assert.equal(box.value, "L");
      caretAt(box, box.value.length);
      assert.equal(press(box, "ArrowDown"), true);
      assert.equal(box.value, "newer", "and forward to the entry after it — not skipped");
      caretAt(box, box.value.length);
      assert.equal(press(box, "ArrowDown"), true);
      assert.equal(box.value, "half written", "the stash comes last, once history runs out");
    } finally {
      globalThis.fetch = saved;
      resetHistory();
    }
  });

  await checkAsync("G2 a send that leaves the box alone keeps its traversal and stash", async () => {
    resetHistory();
    const f = installFetch();
    try {
      seedHistory(REPLY, ["X"]);
      const box = freshInput("flow-reply-input");
      app.bindMessageHistory("flow-reply-input");
      box.value = "follow-up B";
      caretAt(box, 0);
      assert.equal(press(box, "ArrowUp"), true);
      assert.equal(box.value, "X");
      // A send that left before the operator went browsing now succeeds, and
      // the epoch gate kept the box (it holds a recalled entry, and the stash
      // holds the never-sent follow-up). The traversal is still live, so it has
      // to absorb the newly appended entry rather than be thrown away.
      app.recordMessageHistory(REPLY, "A", { retired: false });
      await flush();
      assert.deepEqual(texts(REPLY), ["X", "A"]);
      const nav = app.historyNavState("flow-reply-input");
      assert.equal(nav.cursor, 2, "the cursor still names X, which moved one step back");
      assert.equal(nav.stash, "follow-up B", "the stashed edit is not discarded");
      assert.equal(box.value, "X", "and the box was left exactly as it was");
      caretAt(box, box.value.length);
      assert.equal(press(box, "ArrowDown"), true);
      assert.equal(box.value, "A", "↓ moves to the entry after X — the one just sent");
      caretAt(box, box.value.length);
      assert.equal(press(box, "ArrowDown"), true);
      assert.equal(box.value, "follow-up B", "and one more hands the stash back");
    } finally {
      f.restore();
      resetHistory();
    }
  });

  await checkAsync("G2 walking back to an unsent draft mid-send keeps it from being erased", async () => {
    resetHistory();
    const savedFetch = globalThis.fetch;
    const savedTimeout = globalThis.setTimeout;
    let release = () => {};
    // The delivery POST is held open; the history endpoint answers at once.
    globalThis.setTimeout = () => 0;
    globalThis.fetch = (url, init) => {
      if (String(url).includes("/api/message-history/")) {
        const opts = init || {};
        const body = opts.method === "POST"
          ? { status: "appended", appended: true, entry_id: 99 }
          : { entries: [] };
        return Promise.resolve({ ok: true, status: 200, json: async () => body });
      }
      return new Promise((resolve) => {
        release = () => resolve({ ok: true, status: 200, json: async () => ({}) });
      });
    };
    try {
      armFlow("F-hist-recall", "cr1", "call");
      seedHistory(REPLY, ["X"]);
      const box = freshInput("flow-reply-input");
      app.bindMessageHistory("flow-reply-input");
      app.bindDraftInput("flow-reply-input");
      // An unsent draft in the box...
      box.value = "draft B";
      box.dispatch("input", {});
      // ...set aside to recall X, which is what gets sent.
      caretAt(box, 0);
      assert.equal(press(box, "ArrowUp"), true);
      assert.equal(box.value, "X");
      const sending = app.sendReply(
        "F-hist-recall", { id: "call:cr1", kind: "call", callId: "cr1" }, "X");
      // Before the answer lands, the operator walks back down to their draft.
      caretAt(box, box.value.length);
      assert.equal(press(box, "ArrowDown"), true);
      assert.equal(box.value, "draft B");
      release();
      await sending;
      await flush();
      // "draft B" was never delivered, so the success of its neighbour may not
      // take it — nor the localStorage copy behind it.
      assert.equal(box.value, "draft B", "text that was never sent survives the send");
      assert.equal(app.effectiveDraftText("flow:F-hist-recall"), "draft B");
    } finally {
      globalThis.fetch = savedFetch;
      globalThis.setTimeout = savedTimeout;
      app.settlePendingSend();
      app.clearDraft("flow:F-hist-recall");
      resetHistory();
    }
  });

  check("G2 opening a flow / the New Task modal leaves no stale cursor behind", () => {
    resetHistory();
    seedHistory(REPLY, ["earlier answer"]);
    seedHistory(NEW_TASK, ["earlier task"]);
    const replyNav = app.historyNavState("flow-reply-input");
    replyNav.cursor = 1;
    replyNav.stash = "another flow's half-written words";
    const taskNav = app.historyNavState("nt-task");
    taskNav.cursor = 1;
    taskNav.stash = "last time's words";

    app.resetHistoryNav("flow-reply-input");
    app.resetHistoryNav("nt-task");
    assert.equal(app.historyNavState("flow-reply-input").cursor, 0);
    assert.equal(app.historyNavState("flow-reply-input").stash, "");
    assert.equal(app.historyNavState("nt-task").cursor, 0);
    assert.equal(app.historyNavState("nt-task").stash, "");
    resetHistory();
  });

  // ---- the stash and the upload lifecycle ---------------------------------
  //
  // Stepping into history parks the operator's editing buffer off screen while
  // its uploads keep running. A placeholder token is only safe prompt text
  // while the row behind it keeps pendingUploadRefusal() refusing to send, so a
  // buffer that comes back on ↓ must carry the landed path, or cleaned text —
  // never a marker whose row has settled or left.

  // Reset just the upload-side state these checks borrow.
  function resetUploads() {
    app.state.uploadAttachments = {};
    app.state.uploadTargets = {};
    app.state.uploadSeq = 0;
    for (const id of ["flow-attachments", "nt-attachments"]) {
      const strip = document.getElementById(id);
      strip.innerHTML = "";
      strip.classList.add("hidden");
    }
    document.getElementById("toast-container").innerHTML = "";
  }

  // A non-image stand-in: app.js reads only name/size/type, and a blank type
  // keeps the preview-URL path (which needs a URL host) out of these checks.
  const textFile = (name) => ({ name, size: 4, type: "" });

  // Answer one upload POST, running `duringFlight` first — the request is where
  // "the operator pressed ↑ while the bytes were on the wire" happens.
  function installUploadFetch(duringFlight, spec) {
    const saved = globalThis.fetch;
    globalThis.fetch = async () => {
      if (duringFlight) duringFlight();
      const s = spec || { status: 201, body: { status: "stored", path: "tianluo/uploads/aaaa_notes.txt", size: 4 } };
      return {
        ok: s.status >= 200 && s.status < 300,
        status: s.status,
        json: async () => s.body || {},
      };
    };
    return () => { globalThis.fetch = saved; };
  }

  await checkAsync("G2 an upload that lands during a traversal returns the path, not the marker", async () => {
    resetHistory();
    resetUploads();
    seedHistory(REPLY, ["earlier answer"]);
    const box = freshInput("flow-reply-input");
    app.bindMessageHistory("flow-reply-input");
    app.state.selectedFlowId = "F-stash-land";
    box.value = "look at ";
    caretAt(box, box.value.length);

    const restore = installUploadFetch(() => {
      // Mid-flight: the buffer holding the placeholder goes off screen.
      assert.equal(box.value.includes("uploading"), true, "placeholder is parked at the caret");
      press(box, "ArrowUp");
      assert.equal(box.value, "earlier answer");
    });
    try {
      await app.performUpload(
        box,
        textFile("notes.txt"),
        { ok: true, kind: "flow", flowId: "F-stash-land" },
        "flow-attachments",
      );
      // The recalled entry is still on screen; the answer went into the buffer.
      assert.equal(box.value, "earlier answer");
      caretAt(box, box.value.length);
      assert.equal(press(box, "ArrowDown"), true);
      assert.equal(
        box.value,
        "look at tianluo/uploads/aaaa_notes.txt",
        "the buffer came back naming the uploaded file",
      );
      assert.equal(box.value.includes("uploading"), false, "no internal marker survived");
      assert.equal(app.pendingUploadRefusal("flow-attachments"), "", "nothing is in flight any more");
    } finally {
      restore();
      app.clearAttachments("flow-attachments");
      app.clearDraft(app.flowDraftKey("F-stash-land"));
      app.state.selectedFlowId = null;
      resetUploads();
      resetHistory();
    }
  });

  await checkAsync("G2 an upload that FAILS during a traversal returns cleaned text", async () => {
    resetHistory();
    resetUploads();
    seedHistory(REPLY, ["earlier answer"]);
    const box = freshInput("flow-reply-input");
    app.bindMessageHistory("flow-reply-input");
    app.state.selectedFlowId = "F-stash-fail";
    box.value = "look at ";
    caretAt(box, box.value.length);

    const restore = installUploadFetch(
      () => { press(box, "ArrowUp"); },
      { status: 409, body: { detail: "nope", error_code: "not_registered" } },
    );
    try {
      await app.performUpload(
        box,
        textFile("notes.txt"),
        { ok: true, kind: "flow", flowId: "F-stash-fail" },
        "flow-attachments",
      );
      caretAt(box, box.value.length);
      assert.equal(press(box, "ArrowDown"), true);
      assert.equal(box.value, "look at ", "the words are back, byte for byte");
      assert.equal(box.value.includes("uploading"), false);
    } finally {
      restore();
      app.clearAttachments("flow-attachments");
      app.clearDraft(app.flowDraftKey("F-stash-fail"));
      app.state.selectedFlowId = null;
      resetUploads();
      resetHistory();
    }
  });

  await checkAsync("G2 cancelling an upload during a traversal takes its marker out of the stash", async () => {
    resetHistory();
    resetUploads();
    seedHistory(REPLY, ["earlier answer"]);
    const box = freshInput("flow-reply-input");
    app.bindMessageHistory("flow-reply-input");
    app.state.selectedFlowId = "F-stash-cancel";
    box.value = "look at ";
    caretAt(box, box.value.length);

    // A request that has not answered yet is exactly the case the cancel button
    // exists for; the upload stays in flight for the whole check and is only
    // let go (as an abort would) in the teardown.
    const savedFetch = globalThis.fetch;
    const savedTimeout = globalThis.setTimeout;
    // The upload's own 180s watchdog would outlive the node run; the check
    // settles the request itself, so the timer has nothing left to guard.
    globalThis.setTimeout = () => 0;
    let abortRequest = null;
    globalThis.fetch = () => new Promise((_resolve, reject) => { abortRequest = reject; });
    const pending = app.performUpload(
      box,
      textFile("notes.txt"),
      { ok: true, kind: "flow", flowId: "F-stash-cancel" },
      "flow-attachments",
    );
    try {
      press(box, "ArrowUp");
      assert.equal(box.value, "earlier answer");
      // While it is genuinely in flight the marker is still honest: it stays in
      // the stash, and the gate is up.
      assert.notEqual(app.pendingUploadRefusal("flow-attachments"), "");
      assert.equal(
        app.historyNavState("flow-reply-input").stash.includes("uploading"),
        true,
      );
      const rowId = app.attachmentEntries("flow-attachments")[0].id;
      app.cancelAttachment("flow-attachments", rowId);
      caretAt(box, box.value.length);
      assert.equal(press(box, "ArrowDown"), true);
      assert.equal(box.value, "look at ");
      assert.equal(app.pendingUploadRefusal("flow-attachments"), "");
    } finally {
      if (abortRequest) abortRequest(new Error("aborted"));
      await pending;
      globalThis.fetch = savedFetch;
      globalThis.setTimeout = savedTimeout;
      app.clearAttachments("flow-attachments");
      app.clearDraft(app.flowDraftKey("F-stash-cancel"));
      app.state.selectedFlowId = null;
      resetUploads();
      resetHistory();
    }
  });

  check("G2 a stashed marker whose row left by any other route is not handed back", () => {
    resetHistory();
    resetUploads();
    seedHistory(REPLY, ["earlier answer"]);
    const box = freshInput("flow-reply-input");
    app.bindMessageHistory("flow-reply-input");
    app.state.selectedFlowId = "F-stash-orphan";
    const token = app.uploadPlaceholderToken("notes.txt", 1);
    app.attachmentEntries("flow-attachments").push({
      id: "upload-1", name: "notes.txt", size: 4, type: "",
      status: "uploading", path: "", code: "", previewUrl: "", token,
      controller: null, canceled: false,
    });
    box.value = "look at " + token;
    caretAt(box, box.value.length);
    press(box, "ArrowUp");
    assert.equal(box.value, "earlier answer");
    // The strip is dropped without touching the parked buffer — what every
    // "the text those rows mirror is itself gone" path does.
    app.clearAttachments("flow-attachments");
    caretAt(box, box.value.length);
    assert.equal(press(box, "ArrowDown"), true);
    assert.equal(box.value, "look at ", "the orphaned marker was settled on restore");
    app.clearDraft(app.flowDraftKey("F-stash-orphan"));
    app.state.selectedFlowId = null;
    resetUploads();
    resetHistory();
  });


  // A marker the operator duplicated by hand is NOT covered by the rewrite that
  // the upload lifecycle performs — that one deliberately owns only the copy it
  // planted. Every other copy is just as unbacked once the row settles, and the
  // stash is off screen, so nothing but the restore-time sweep can take it out.

  await checkAsync("G2 a duplicated marker does not survive a landing in the stash", async () => {
    resetHistory();
    resetUploads();
    seedHistory(REPLY, ["earlier answer"]);
    const box = freshInput("flow-reply-input");
    app.bindMessageHistory("flow-reply-input");
    app.state.selectedFlowId = "F-stash-dup-land";
    box.value = "compare ";
    caretAt(box, box.value.length);

    const restore = installUploadFetch(() => {
      // The operator copied the visible marker before going looking: the parked
      // buffer carries it twice.
      const token = app.attachmentEntries("flow-attachments")[0].token;
      box.value = "compare " + token + " with " + token;
      caretAt(box, box.value.length);
      press(box, "ArrowUp");
      assert.equal(box.value, "earlier answer");
    });
    try {
      await app.performUpload(
        box,
        textFile("notes.txt"),
        { ok: true, kind: "flow", flowId: "F-stash-dup-land" },
        "flow-attachments",
      );
      caretAt(box, box.value.length);
      assert.equal(press(box, "ArrowDown"), true);
      assert.equal(
        box.value,
        "compare tianluo/uploads/aaaa_notes.txt with ",
        "the planted copy became the path; the hand-made one was swept",
      );
      assert.equal(box.value.includes("uploading"), false, "no internal marker survived");
      assert.equal(app.pendingUploadRefusal("flow-attachments"), "", "and the gate is down");
    } finally {
      restore();
      app.clearAttachments("flow-attachments");
      app.clearDraft(app.flowDraftKey("F-stash-dup-land"));
      app.state.selectedFlowId = null;
      resetUploads();
      resetHistory();
    }
  });

  await checkAsync("G2 a duplicated marker does not survive a FAILED upload in the stash", async () => {
    resetHistory();
    resetUploads();
    seedHistory(REPLY, ["earlier answer"]);
    const box = freshInput("flow-reply-input");
    app.bindMessageHistory("flow-reply-input");
    app.state.selectedFlowId = "F-stash-dup-fail";
    box.value = "compare ";
    caretAt(box, box.value.length);

    const restore = installUploadFetch(
      () => {
        const token = app.attachmentEntries("flow-attachments")[0].token;
        box.value = "compare " + token + " with " + token;
        caretAt(box, box.value.length);
        press(box, "ArrowUp");
      },
      { status: 409, body: { detail: "nope", error_code: "not_registered" } },
    );
    try {
      await app.performUpload(
        box,
        textFile("notes.txt"),
        { ok: true, kind: "flow", flowId: "F-stash-dup-fail" },
        "flow-attachments",
      );
      caretAt(box, box.value.length);
      assert.equal(press(box, "ArrowDown"), true);
      assert.equal(box.value, "compare  with ", "both copies are gone, the words are not");
      assert.equal(box.value.includes("uploading"), false);
      assert.equal(app.pendingUploadRefusal("flow-attachments"), "");
    } finally {
      restore();
      app.clearAttachments("flow-attachments");
      app.clearDraft(app.flowDraftKey("F-stash-dup-fail"));
      app.state.selectedFlowId = null;
      resetUploads();
      resetHistory();
    }
  });

  // A marker whose row is STILL in flight is honest text and must stay — every
  // copy of it — or ↓ would silently edit words the gate is still protecting.
  await checkAsync("G2 duplicated markers stay while the row is genuinely in flight", async () => {
    resetHistory();
    resetUploads();
    seedHistory(REPLY, ["earlier answer"]);
    const box = freshInput("flow-reply-input");
    app.bindMessageHistory("flow-reply-input");
    app.state.selectedFlowId = "F-stash-dup-live";
    const token = app.uploadPlaceholderToken("notes.txt", 1);
    app.attachmentEntries("flow-attachments").push({
      id: "upload-1", name: "notes.txt", size: 4, type: "",
      status: "uploading", path: "", code: "", previewUrl: "", token,
      controller: null, canceled: false,
    });
    box.value = "compare " + token + " with " + token;
    caretAt(box, box.value.length);
    press(box, "ArrowUp");
    assert.equal(box.value, "earlier answer");
    caretAt(box, box.value.length);
    assert.equal(press(box, "ArrowDown"), true);
    assert.equal(
      box.value,
      "compare " + token + " with " + token,
      "the buffer comes back byte for byte while the gate is still up",
    );
    assert.notEqual(app.pendingUploadRefusal("flow-attachments"), "");
    app.clearAttachments("flow-attachments");
    app.clearDraft(app.flowDraftKey("F-stash-dup-live"));
    app.state.selectedFlowId = null;
    resetUploads();
    resetHistory();
  });

  // Leave no borrowed state behind for the checks that follow.
  resetHistory();
  resetUploads();
  freshInput("flow-reply-input");
  freshInput("nt-task");
  app.state.selectedFlowId = null;
  app.state.flowDetail = null;
  app.state.flowConversationRecords = [];
  app.state.flowInterventions = [];
  app.state.flowReplyTargetId = null;
  app.resetRenderSignatures();
}
