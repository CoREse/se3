/*
 * Local input-draft persistence tests (Group G1).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub
 * (`globalThis.document` / `FakeNode`) is installed. Exposes
 * `registerInputDraftTests({app, check, checkAsync, findOne, findAll})` so the
 * parent harness drives the same check() reporter against the same `app`
 * module export.
 *
 * What is under test: the four prompt boxes (#flow-reply-input, #nt-task,
 * #issue-description, #issue-title) keep whatever was typed and not sent, in
 * localStorage only. The contract these checks pin down:
 *
 *   - a keystroke saves behind a debounce, and re-opening the same place
 *     refills the box (with the docked textarea's auto-grow re-measured, or a
 *     multi-line draft comes back clipped to one line);
 *   - drafts are isolated by position — one per flow for the reply box, one
 *     fixed slot each for the other three — so no box ever shows another's
 *     text;
 *   - EVERY existing clear path drops the draft with the text: a plain reply,
 *     an interject, a structured approve/reject, a published task, a created
 *     issue. Missing one leaves a stale draft to reappear on the next open,
 *     which is exactly the failure the coverage here exists to prevent;
 *   - ...and no clear path reaches past the text it delivered. Every box stays
 *     editable while its request is in flight, so each of them is checked
 *     against a rewrite typed mid-round-trip — including one typed back to the
 *     exact characters that were sent, which only the edit counter can tell
 *     apart from the untouched box;
 *   - the store is bounded (entry cap + TTL) so long use cannot grow it
 *     without limit;
 *   - and every localStorage touch is guarded, because it throws outright in
 *     some privacy modes: a storage failure has to read as "there is no draft"
 *     and must never make an input box unusable or block a submit.
 *
 * Parallel safety: the checks share the harness's one node process and its
 * id-keyed element cache, so each one installs its OWN fake localStorage,
 * resets the elements it touches, and restores every global it borrows
 * (window / fetch / setTimeout) before returning. Nothing here depends on
 * another module's ordering or leaves mutable state behind.
 */
import assert from "node:assert/strict";

export async function registerInputDraftTests(ctx) {
  const { app } = ctx;
  const check = ctx.check;
  const checkAsync = ctx.checkAsync;

  // ---- fake localStorage -------------------------------------------------
  //
  // Node has no Web Storage by default, so every check installs one of these
  // and removes it again. `opts.throwOnGet` / `throwOnSet` model the privacy
  // modes and the quota wall the app has to survive.
  function makeStorage(opts = {}) {
    const data = new Map();
    return {
      data,
      getItem(k) {
        if (opts.throwOnGet) throw new Error("SecurityError: storage disabled");
        return data.has(k) ? data.get(k) : null;
      },
      setItem(k, v) {
        if (opts.throwOnSet) {
          const e = new Error("QuotaExceededError");
          e.name = "QuotaExceededError";
          throw e;
        }
        data.set(k, String(v));
      },
      removeItem(k) { data.delete(k); },
    };
  }

  // Install a fake storage (or `localStorage` outright undefined / poisoned)
  // and hand back the undo. Split from the two wrappers below because the async
  // one must not restore until its promise settles — restoring at the first
  // await would run the interesting half of a send with no storage at all, and
  // "the draft is gone" would pass for the wrong reason.
  function installStorage(store) {
    const had = Object.prototype.hasOwnProperty.call(globalThis, "localStorage");
    const saved = had ? Object.getOwnPropertyDescriptor(globalThis, "localStorage") : null;
    if (store === "throwing-access") {
      Object.defineProperty(globalThis, "localStorage", {
        configurable: true,
        get() { throw new Error("SecurityError: access denied"); },
      });
    } else if (store === null) {
      delete globalThis.localStorage;
    } else {
      Object.defineProperty(globalThis, "localStorage", {
        configurable: true,
        writable: true,
        value: store,
      });
    }
    return () => {
      delete globalThis.localStorage;
      if (saved) Object.defineProperty(globalThis, "localStorage", saved);
    };
  }

  function withStorage(store, fn) {
    const undo = installStorage(store);
    try {
      return fn();
    } finally {
      undo();
    }
  }

  async function withStorageAsync(store, fn) {
    const undo = installStorage(store);
    try {
      return await fn();
    } finally {
      undo();
    }
  }

  // Raw stored payload, so a check can assert the SHAPE (only text+ts survive —
  // attachment rows never reach the store) and not just the round-trip.
  function storedPayload(store) {
    const raw = store.data.get(app.DRAFTS.STORAGE_KEY);
    return raw ? JSON.parse(raw) : null;
  }

  // ---- element helpers ---------------------------------------------------

  // The harness caches elements by id, so a check that binds a listener would
  // otherwise stack another one on every run. Drop the input listeners and the
  // value before each use.
  function freshInput(id) {
    const node = document.getElementById(id);
    node._listeners = {};
    node.value = "";
    if (node.style) { node.style.height = ""; node.style.overflowY = ""; }
    node.__autoGrowApplied = false;
    return node;
  }

  // Make the docked textarea report a content-dependent scrollHeight, the one
  // signal autoGrowReplyTextarea() reads. Without it every draft would measure
  // the same and "auto-grow re-ran after the refill" would be unobservable.
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

  // One keystroke: set the value and fire the `input` event the browser would.
  // Checks then call app.flushDraftSaves() to land the debounced write, so none
  // of them has to sleep on a real timer.
  function typeInto(node, text) {
    node.value = text;
    node.dispatch("input", {});
  }

  // ---- pure store helpers ------------------------------------------------

  check("G1 pruneDraftEntries drops expired entries and caps at the newest N", () => {
    const now = 1_000_000;
    const entries = {
      fresh: { text: "a", ts: now - 1000 },
      mid: { text: "b", ts: now - 2000 },
      old: { text: "c", ts: now - 3000 },
      expired: { text: "d", ts: now - 10 * 24 * 3600 * 1000 },
      blank: { text: "", ts: now },
    };
    const kept = app.pruneDraftEntries(entries, now, 2, 5 * 24 * 3600 * 1000);
    assert.deepEqual(Object.keys(kept).sort(), ["fresh", "mid"]);
    // Expired and blank entries are gone regardless of the cap.
    const uncapped = app.pruneDraftEntries(entries, now, 99, 5 * 24 * 3600 * 1000);
    assert.deepEqual(Object.keys(uncapped).sort(), ["fresh", "mid", "old"]);
  });

  check("G1 the store is bounded: MAX_ENTRIES survives, the oldest fall out", () => {
    const store = makeStorage();
    withStorage(store, () => {
      const cap = app.DRAFTS.MAX_ENTRIES;
      for (let i = 0; i < cap + 7; i += 1) {
        assert.equal(app.saveDraft("flow:f" + i, "draft " + i), true);
      }
      const payload = storedPayload(store);
      const keys = Object.keys(payload.entries).sort();
      assert.equal(keys.length, cap, "entry count is capped");
      // Eviction is by recency, and the whole burst lands inside one
      // millisecond — so this also pins the total ordering that makes "oldest"
      // decidable at all (a plain Date.now() stamp ties, and a tie evicts by
      // insertion order, which would drop the NEWEST draft instead).
      const expected = [];
      for (let i = 7; i < cap + 7; i += 1) expected.push("flow:f" + i);
      assert.deepEqual(keys, expected.sort());
      assert.equal(app.loadDraft("flow:f0"), "");
      assert.equal(app.loadDraft("flow:f" + (cap + 6)), "draft " + (cap + 6));
    });
  });

  check("G1 the draft being typed right now is never the one evicted", () => {
    const store = makeStorage();
    withStorage(store, () => {
      for (let i = 0; i < app.DRAFTS.MAX_ENTRIES; i += 1) app.saveDraft("flow:filler" + i, "x");
      // One more slot's worth of writes into the SAME key: it must survive every
      // one of them, whatever the clock did.
      for (let i = 0; i < 5; i += 1) {
        app.saveDraft("flow:live", "revision " + i);
        assert.equal(app.loadDraft("flow:live"), "revision " + i);
      }
      assert.equal(Object.keys(storedPayload(store).entries).length, app.DRAFTS.MAX_ENTRIES);
    });
  });

  check("G1 an entry older than the TTL reads as no draft", () => {
    const store = makeStorage();
    withStorage(store, () => {
      const stale = Date.now() - app.DRAFTS.TTL_MS - 1000;
      store.data.set(
        app.DRAFTS.STORAGE_KEY,
        JSON.stringify({ v: 1, entries: { "flow:aged": { text: "old words", ts: stale } } }),
      );
      assert.equal(app.loadDraft("flow:aged"), "");
    });
  });

  check("G1 only text is persisted — the stored entry carries nothing else", () => {
    const store = makeStorage();
    withStorage(store, () => {
      // Attachment rows exist for this strip; none of it may reach the store.
      app.state.uploadAttachments = { "flow-attachments": [{ name: "a.png", path: "x/a.png" }] };
      app.saveDraft("flow:shape", "see x/a.png");
      const payload = storedPayload(store);
      assert.deepEqual(Object.keys(payload.entries["flow:shape"]).sort(), ["text", "ts"]);
      assert.equal(payload.entries["flow:shape"].text, "see x/a.png");
      assert.equal(JSON.stringify(payload).includes("a.png\",\"path"), false);
      app.state.uploadAttachments = {};
    });
  });

  check("G1 whitespace-only text clears the slot instead of storing a blank draft", () => {
    const store = makeStorage();
    withStorage(store, () => {
      app.saveDraft("flow:blank", "something");
      assert.equal(app.loadDraft("flow:blank"), "something");
      app.saveDraft("flow:blank", "   \n  ");
      assert.equal(app.loadDraft("flow:blank"), "");
    });
  });

  check("G1 a corrupt / foreign payload reads as no drafts rather than throwing", () => {
    const store = makeStorage();
    withStorage(store, () => {
      store.data.set(app.DRAFTS.STORAGE_KEY, "{not json");
      assert.deepEqual(app.readDraftEntries(), {});
      store.data.set(app.DRAFTS.STORAGE_KEY, JSON.stringify({ v: 9, other: 1 }));
      assert.deepEqual(app.readDraftEntries(), {});
      assert.equal(app.loadDraft("flow:x"), "");
    });
  });

  // ---- draft keys are isolated by input position -------------------------

  check("G1 draftKeyForInput gives each of the four boxes its own slot", () => {
    app.state.selectedFlowId = "F-alpha";
    document.getElementById("issue-form").dataset.mode = "create";
    const keys = [
      app.draftKeyForInput("flow-reply-input"),
      app.draftKeyForInput("nt-task"),
      app.draftKeyForInput("issue-description"),
      app.draftKeyForInput("issue-title"),
    ];
    assert.equal(new Set(keys).size, 4, "four distinct draft slots");
    assert.equal(keys[0], "flow:F-alpha");
    assert.equal(keys[1], app.DRAFTS.NEW_TASK);
    // Switching flows switches the reply box's slot; an unknown flow has none.
    app.state.selectedFlowId = "F-beta";
    assert.equal(app.draftKeyForInput("flow-reply-input"), "flow:F-beta");
    app.state.selectedFlowId = null;
    assert.equal(app.draftKeyForInput("flow-reply-input"), "");
    assert.equal(app.draftKeyForInput("unknown-box"), "");
  });

  check("G1 the issue EDIT form is not drafted — it holds a stored body, not unsent text", () => {
    const store = makeStorage();
    withStorage(store, () => {
      document.getElementById("issue-form").dataset.mode = "create";
      app.saveDraft(app.DRAFTS.ISSUE_DESCRIPTION, "my new issue");
      document.getElementById("issue-form").dataset.mode = "edit";
      assert.equal(app.draftKeyForInput("issue-description"), "");
      assert.equal(app.draftKeyForInput("issue-title"), "");
      const box = freshInput("issue-description");
      app.bindDraftInput("issue-description");
      typeInto(box, "editing someone else's issue");
      app.flushDraftSaves();
      // The create-mode draft is untouched by the edit form.
      assert.equal(app.loadDraft(app.DRAFTS.ISSUE_DESCRIPTION), "my new issue");
      document.getElementById("issue-form").dataset.mode = "create";
    });
  });

  // ---- debounced write + refill -----------------------------------------

  check("G1 typing saves behind a debounce: one write, the latest text", () => {
    const store = makeStorage();
    withStorage(store, () => {
      app.state.selectedFlowId = "F-debounce";
      const box = freshInput("flow-reply-input");
      app.bindDraftInput("flow-reply-input");
      typeInto(box, "h");
      typeInto(box, "he");
      typeInto(box, "hel");
      // Nothing has landed yet — the window has not elapsed.
      assert.equal(store.data.has(app.DRAFTS.STORAGE_KEY), false, "no write mid-debounce");
      app.flushDraftSaves();
      const payload = storedPayload(store);
      assert.deepEqual(Object.keys(payload.entries), ["flow:F-debounce"]);
      assert.equal(app.loadDraft("flow:F-debounce"), "hel");
    });
  });

  check("G1 reply drafts are per-flow — one flow's words never surface in another", () => {
    const store = makeStorage();
    withStorage(store, () => {
      const box = freshInput("flow-reply-input");
      app.bindDraftInput("flow-reply-input");

      app.state.selectedFlowId = "F-one";
      typeInto(box, "answer for one");
      app.flushDraftSaves();
      app.state.selectedFlowId = "F-two";
      typeInto(box, "answer for two");
      app.flushDraftSaves();

      assert.equal(app.loadDraft("flow:F-one"), "answer for one");
      assert.equal(app.loadDraft("flow:F-two"), "answer for two");

      // Re-opening flow one refills ITS text, not the last one typed.
      box.value = "";
      app.state.selectedFlowId = "F-one";
      assert.equal(app.restoreDraftInto("flow-reply-input"), "answer for one");
      assert.equal(box.value, "answer for one");
    });
  });

  check("G1 restoring a multi-line draft re-measures the auto-grow textarea", () => {
    const store = makeStorage();
    withStorage(store, () => {
      withMeasuredReplyBox((input) => {
        app.state.selectedFlowId = "F-grow";
        input.value = "";
        input.style.height = "";
        app.saveDraft("flow:F-grow", "line1\nline2\nline3\nline4");
        assert.equal(app.restoreDraftInto("flow-reply-input"), "line1\nline2\nline3\nline4");
        // 4 lines * 20 + 20 = 100px; a box left un-measured would still read
        // the single-line floor (40px) it was reset to.
        assert.equal(input.style.height, "100px", "height tracks the restored content");
      });
    });
  });

  check("G1 opening a flow (resetReplyBox) refills that flow's draft and re-grows", () => {
    const store = makeStorage();
    withStorage(store, () => {
      withMeasuredReplyBox((input) => {
        app.state.selectedFlowId = "F-open";
        app.saveDraft("flow:F-open", "half written\nsecond line");
        input.value = "leftover from the previous flow";
        app.resetReplyBox();
        assert.equal(input.value, "half written\nsecond line");
        assert.equal(input.style.height, "60px");

        // A flow with no draft opens empty and collapsed — the blanking still
        // wins where there is nothing to restore.
        app.state.selectedFlowId = "F-nodraft";
        app.resetReplyBox();
        assert.equal(input.value, "");
        assert.equal(input.style.height, "40px");
      });
    });
  });

  check("G1 a programmatic upload edit is drafted too (no input event fires)", () => {
    const store = makeStorage();
    withStorage(store, () => {
      app.state.selectedFlowId = "F-upload";
      const input = freshInput("flow-reply-input");
      input.value = "look at uploads/pic.png";
      // syncUploadInput is the single hook every programmatic text edit in the
      // upload path goes through; drive it via the exported scope config.
      app.scheduleDraftSave(app.draftKeyForInput("flow-reply-input"), input.value);
      app.flushDraftSaves();
      assert.equal(app.loadDraft("flow:F-upload"), "look at uploads/pic.png");
    });
  });

  check("G1 an in-flight upload placeholder is never persisted as a draft", () => {
    const store = makeStorage();
    withStorage(store, () => {
      app.state.selectedFlowId = "F-pending-upload";
      app.state.uploadAttachments = {};
      const input = freshInput("flow-reply-input");
      app.bindDraftInput("flow-reply-input");
      const token = app.uploadPlaceholderToken("shot.png", 1);
      // The row performUpload pushes before it starts the request: the token in
      // the text is only safe while THIS row keeps the submit gate up, and the
      // strip is deliberately not persisted.
      app.attachmentEntries("flow-attachments").push({
        id: "upload-1", name: "shot.png", size: 4, type: "",
        status: "uploading", path: "", code: "", previewUrl: "", token,
        controller: null, canceled: false,
      });
      typeInto(input, "have a look at " + token);
      app.flushDraftSaves();
      const stored = app.loadDraft("flow:F-pending-upload");
      assert.equal(stored, "have a look at ", "only the words were kept");
      assert.equal(stored.includes("uploading"), false, "no internal marker reached storage");

      // Once the upload lands the path is ordinary text, and drafts like any
      // other — a reload has to bring the file's name back with the prompt.
      const rows = app.attachmentEntries("flow-attachments");
      rows[0].status = "done";
      rows[0].path = "tianluo/uploads/aaaa_shot.png";
      typeInto(input, "have a look at tianluo/uploads/aaaa_shot.png");
      app.flushDraftSaves();
      assert.equal(
        app.loadDraft("flow:F-pending-upload"),
        "have a look at tianluo/uploads/aaaa_shot.png",
      );

      app.clearDraft("flow:F-pending-upload");
      app.state.uploadAttachments = {};
    });
  });

  check("G1 a marker the operator copied is dropped from the draft wherever it appears", () => {
    const store = makeStorage();
    withStorage(store, () => {
      app.state.selectedFlowId = "F-dup-upload";
      app.state.uploadAttachments = {};
      const input = freshInput("flow-reply-input");
      app.bindDraftInput("flow-reply-input");
      const token = app.uploadPlaceholderToken("shot.png", 1);
      app.attachmentEntries("flow-attachments").push({
        id: "upload-dup", name: "shot.png", size: 4, type: "",
        status: "uploading", path: "", code: "", previewUrl: "", token,
        controller: null, canceled: false,
      });
      // One row, but its visible marker copied so the text carries it twice.
      // The strip is not persisted, so after a reload NEITHER copy has a row
      // behind it — keeping one would ship an internal marker as prompt prose
      // with the uploaded file named nowhere.
      typeInto(input, "compare " + token + " with " + token);
      app.flushDraftSaves();
      const stored = app.loadDraft("flow:F-dup-upload");
      assert.equal(stored, "compare  with ");
      assert.equal(stored.includes("uploading"), false, "no copy reached storage");

      app.state.uploadAttachments = {};
      input.value = "";
      assert.equal(app.restoreDraftInto("flow-reply-input"), "compare  with ");
      assert.equal(
        app.pendingUploadRefusal("flow-attachments"), "",
        "the gate is down after a reload — which is why no copy may come back",
      );
      app.clearDraft("flow:F-dup-upload");
      app.state.uploadAttachments = {};
    });
  });

  check("G1 a draft restored while nothing is uploading carries no orphaned marker", () => {
    const store = makeStorage();
    withStorage(store, () => {
      app.state.selectedFlowId = "F-reload-upload";
      app.state.uploadAttachments = {};
      const input = freshInput("flow-reply-input");
      app.bindDraftInput("flow-reply-input");
      const token = app.uploadPlaceholderToken("shot.png", 2);
      app.attachmentEntries("flow-attachments").push({
        id: "upload-2", name: "shot.png", size: 4, type: "",
        status: "uploading", path: "", code: "", previewUrl: "", token,
        controller: null, canceled: false,
      });
      typeInto(input, "answer " + token);
      app.flushDraftSaves();

      // A reload: the strip is gone (it is live state, never persisted) and the
      // draft is refilled into a box whose submit gate is now down.
      app.state.uploadAttachments = {};
      input.value = "";
      const restored = app.restoreDraftInto("flow-reply-input");
      assert.equal(restored, "answer ");
      assert.equal(input.value, "answer ");
      assert.equal(
        app.pendingUploadRefusal("flow-attachments"),
        "",
        "the gate is down after a reload — which is exactly why the marker may not come back",
      );
      app.clearDraft("flow:F-reload-upload");
    });
  });

  // ---- every clear path ---------------------------------------------------

  // Drive a send with fetch stubbed and scheduling neutered, so neither the 8s
  // settle gate nor the toast TTL outlives the check.
  async function withStubbedSend(fn, { ok = true, status = 200 } = {}) {
    const savedFetch = globalThis.fetch;
    const savedSetTimeout = globalThis.setTimeout;
    globalThis.setTimeout = () => 0;
    globalThis.fetch = () =>
      Promise.resolve({ ok, status, json: () => Promise.resolve({ issues: [] }) });
    try {
      return await fn();
    } finally {
      globalThis.fetch = savedFetch;
      globalThis.setTimeout = savedSetTimeout;
      app.settlePendingSend();
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

  await checkAsync("G1 clear path 1/4 — a delivered reply drops its flow draft", async () => {
    const store = makeStorage();
    await withStorageAsync(store, async () => {
      armFlow("F-send", "c1", "call");
      app.saveDraft("flow:F-send", "the answer");
      assert.equal(app.loadDraft("flow:F-send"), "the answer");
      await withStubbedSend(() =>
        app.sendReply("F-send", { id: "call:c1", kind: "call", callId: "c1" }, "the answer"));
      assert.equal(app.loadDraft("flow:F-send"), "", "sent text is no longer a draft");
    });
  });

  await checkAsync("G1 clear path 2/4 — a delivered interject drops the same slot", async () => {
    const store = makeStorage();
    await withStorageAsync(store, async () => {
      armFlow("F-inter", "i1", "interjection");
      app.saveDraft("flow:F-inter", "stop and look at this");
      await withStubbedSend(() =>
        app.sendReply(
          "F-inter",
          { id: "interjection:new", kind: "interjection", synthetic: true, callId: null },
          "stop and look at this",
        ));
      assert.equal(app.loadDraft("flow:F-inter"), "");
      app.state.flowSyntheticInterjectPending = false;
      app.state.flowInterjectRequested = false;
    });
  });

  await checkAsync("G1 clear path 3/4 — a structured approve/reject drops the flow draft", async () => {
    const store = makeStorage();
    await withStorageAsync(store, async () => {
      armFlow("F-confirm", "k1", "confirm");
      app.saveDraft("flow:F-confirm", "needs another pass");
      await withStubbedSend(() =>
        app.sendConfirmDecision(
          "F-confirm",
          { id: "call:k1", kind: "confirm", callId: "k1" },
          false,
          "needs another pass",
        ));
      assert.equal(app.loadDraft("flow:F-confirm"), "");

      // ...and the approve branch, which clears the box by the same route.
      armFlow("F-confirm2", "k2", "confirm");
      app.saveDraft("flow:F-confirm2", "looks good");
      await withStubbedSend(() =>
        app.sendConfirmDecision(
          "F-confirm2", { id: "call:k2", kind: "confirm", callId: "k2" }, true, null));
      assert.equal(app.loadDraft("flow:F-confirm2"), "");
    });
  });

  await checkAsync("G1 a FAILED send keeps the draft — nothing was delivered", async () => {
    const store = makeStorage();
    await withStorageAsync(store, async () => {
      armFlow("F-fail", "c9", "call");
      app.saveDraft("flow:F-fail", "unsent words");
      await withStubbedSend(
        () => app.sendReply("F-fail", { id: "call:c9", kind: "call", callId: "c9" }, "unsent words"),
        { ok: false, status: 500 },
      );
      assert.equal(app.loadDraft("flow:F-fail"), "unsent words");
    });
  });

  await checkAsync("G1 clear path 4/4 — a published task drops the new-task draft", async () => {
    const store = makeStorage();
    await withStorageAsync(store, async () => {
      app.saveDraft(app.DRAFTS.NEW_TASK, "build the thing");
      const machine = document.getElementById("nt-machine");
      machine.value = "M1";
      document.getElementById("nt-project").value = "/srv/proj";
      document.getElementById("nt-type").value = "feature";
      document.getElementById("nt-task").value = "build the thing";
      const savedFetch = globalThis.fetch;
      globalThis.fetch = () =>
        Promise.resolve({ ok: true, status: 202, json: () => Promise.resolve({}) });
      try {
        await app.submitNewTask({ preventDefault() {} });
      } finally {
        globalThis.fetch = savedFetch;
      }
      assert.equal(app.loadDraft(app.DRAFTS.NEW_TASK), "");
    });
  });

  check("G1 the New Task panel refills its draft after its own open-clears-everything reset", () => {
    const store = makeStorage();
    withStorage(store, () => {
      app.saveDraft(app.DRAFTS.NEW_TASK, "a task I started describing");
      app.state.machines = [{ machine_id: "M1", hostname: "host-1", online: true, project_roots: ["/srv/p"] }];
      app.openNewTask();
      assert.equal(document.getElementById("nt-task").value, "a task I started describing");
      app.state.machines = [];
    });
  });

  await checkAsync("G1 a created issue drops both issue drafts; an edit leaves them alone", async () => {
    const store = makeStorage();
    await withStorageAsync(store, async () => {
      const form = document.getElementById("issue-form");
      app.saveDraft(app.DRAFTS.ISSUE_DESCRIPTION, "something is broken");
      app.saveDraft(app.DRAFTS.ISSUE_TITLE, "broken thing");

      // An EDIT that succeeds must not consume the pending new-issue draft.
      form.dataset.mode = "edit";
      form.dataset.issueId = "7";
      form.dataset.machineId = "M1";
      form.dataset.projectRoot = "/srv/p";
      document.getElementById("issue-description").value = "edited body";
      const savedFetch = globalThis.fetch;
      globalThis.fetch = () =>
        Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ issues: [] }) });
      try {
        await app.submitIssueForm({ preventDefault() {} });
        assert.equal(app.loadDraft(app.DRAFTS.ISSUE_DESCRIPTION), "something is broken");

        // The create path does consume it.
        form.dataset.mode = "create";
        document.getElementById("issue-description").value = "something is broken";
        document.getElementById("issue-title").value = "broken thing";
        document.getElementById("issue-machine").value = "M1";
        document.getElementById("issue-project").value = "/srv/p";
        document.getElementById("issue-type").value = "";
        document.getElementById("issue-priority").value = "";
        document.getElementById("issue-tags").value = "";
        await app.submitIssueForm({ preventDefault() {} });
        assert.equal(app.loadDraft(app.DRAFTS.ISSUE_DESCRIPTION), "");
        assert.equal(app.loadDraft(app.DRAFTS.ISSUE_TITLE), "");
      } finally {
        globalThis.fetch = savedFetch;
      }
    });
  });

  check("G1 the New Issue modal refills both boxes after its open-time blanking", () => {
    const store = makeStorage();
    withStorage(store, () => {
      app.saveDraft(app.DRAFTS.ISSUE_DESCRIPTION, "half-typed report");
      app.saveDraft(app.DRAFTS.ISSUE_TITLE, "half-typed title");
      app.state.machines = [{ machine_id: "M1", hostname: "host-1", online: true, project_roots: ["/srv/p"] }];
      app.openIssueCreateModal();
      assert.equal(document.getElementById("issue-description").value, "half-typed report");
      assert.equal(document.getElementById("issue-title").value, "half-typed title");
      app.state.machines = [];
    });
  });

  // ---- storage failures degrade to "no draft", never to a dead input -----

  check("G1 storage that throws on every access still leaves the boxes usable", () => {
    withStorage("throwing-access", () => {
      app.state.selectedFlowId = "F-privacy";
      const box = freshInput("flow-reply-input");
      app.bindDraftInput("flow-reply-input");
      assert.equal(app.draftStorage(), null);
      assert.equal(app.loadDraft("flow:F-privacy"), "");
      assert.deepEqual(app.readDraftEntries(), {});
      assert.equal(app.saveDraft("flow:F-privacy", "typed"), false);
      // Typing, flushing and restoring are all no-ops that never throw, and the
      // box keeps exactly what the user put in it.
      typeInto(box, "still typing");
      app.flushDraftSaves();
      assert.equal(box.value, "still typing");
      assert.equal(app.restoreDraftInto("flow-reply-input"), "");
      assert.equal(box.value, "still typing");
    });
  });

  check("G1 no localStorage at all behaves the same way", () => {
    withStorage(null, () => {
      app.state.selectedFlowId = "F-none";
      const box = freshInput("nt-task");
      app.bindDraftInput("nt-task");
      typeInto(box, "task text");
      app.flushDraftSaves();
      assert.equal(box.value, "task text");
      assert.equal(app.loadDraft(app.DRAFTS.NEW_TASK), "");
    });
  });

  check("G1 a QuotaExceededError on write is swallowed, and reads still work", () => {
    const store = makeStorage({ throwOnSet: true });
    withStorage(store, () => {
      assert.equal(app.saveDraft("flow:F-quota", "too big"), false);
      assert.equal(app.loadDraft("flow:F-quota"), "");
      // And a send whose draft-clear hits the same wall must not throw.
      assert.equal(app.clearDraft("flow:F-quota"), false);
    });
  });

  await checkAsync("G1 a send still succeeds when storage is dead", async () => {
    await withStorageAsync("throwing-access", async () => {
      armFlow("F-deadstore", "c5", "call");
      await withStubbedSend(() =>
        app.sendReply("F-deadstore", { id: "call:c5", kind: "call", callId: "c5" }, "hello"));
      // The success path ran to completion: the box was cleared by it.
      assert.equal(document.getElementById("flow-reply-input").value, "");
    });
  });

  // ---- the in-flight window ----------------------------------------------
  //
  // The reply textarea deliberately stays enabled for the whole round trip, so
  // "success" and "the box still holds what was sent" are two different facts.
  // These checks pin the difference down: a follow-up written while the request
  // was on the wire was never delivered and must survive the response.

  // Drive a send whose response is held open until the check releases it, so a
  // follow-up can be typed into the still-editable box mid-flight.
  async function withHeldSend(fn) {
    const savedFetch = globalThis.fetch;
    const savedSetTimeout = globalThis.setTimeout;
    let release = () => {};
    globalThis.setTimeout = () => 0;
    globalThis.fetch = (url) => {
      // Owner-scoped history bookkeeping (G2) rides along on its own endpoint
      // and is answered straight away — only the delivery POST is held.
      if (String(url).includes("/api/message-history/")) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ entries: [] }) });
      }
      return new Promise((resolve) => {
        release = () => resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
      });
    };
    try {
      return await fn(() => release());
    } finally {
      globalThis.fetch = savedFetch;
      globalThis.setTimeout = savedSetTimeout;
      app.settlePendingSend();
    }
  }

  await checkAsync("G1 a follow-up typed during a slow send is not erased by its success", async () => {
    const store = makeStorage();
    await withStorageAsync(store, async () => {
      armFlow("F-slow", "c7", "call");
      const box = freshInput("flow-reply-input");
      app.bindDraftInput("flow-reply-input");
      box.value = "text A";
      app.saveDraft("flow:F-slow", "text A");

      await withHeldSend(async (release) => {
        const sending = app.sendReply(
          "F-slow", { id: "call:c7", kind: "call", callId: "c7" }, "text A");
        typeInto(box, "follow-up B");
        release();
        await sending;
      });
      assert.equal(box.value, "follow-up B", "the never-sent follow-up stays visible");
      app.flushDraftSaves();
      assert.equal(app.loadDraft("flow:F-slow"), "follow-up B", "...and stays the draft");
      app.clearDraft("flow:F-slow");
    });
  });

  await checkAsync("G1 a structured decision leaves a mid-flight follow-up alone too", async () => {
    const store = makeStorage();
    await withStorageAsync(store, async () => {
      armFlow("F-slowk", "k7", "confirm");
      const box = freshInput("flow-reply-input");
      app.bindDraftInput("flow-reply-input");
      box.value = "needs work";
      app.saveDraft("flow:F-slowk", "needs work");

      await withHeldSend(async (release) => {
        const sending = app.sendConfirmDecision(
          "F-slowk", { id: "call:k7", kind: "confirm", callId: "k7" }, false, "needs work");
        typeInto(box, "second thought");
        release();
        await sending;
      });
      assert.equal(box.value, "second thought");
      app.flushDraftSaves();
      assert.equal(app.loadDraft("flow:F-slowk"), "second thought");
      app.clearDraft("flow:F-slowk");
    });
  });

  await checkAsync("G1 a follow-up typed back to the sent characters is still not erased", async () => {
    const store = makeStorage();
    await withStorageAsync(store, async () => {
      armFlow("F-same", "c8", "call");
      const box = freshInput("flow-reply-input");
      app.bindDraftInput("flow-reply-input");
      box.value = "text A";
      app.saveDraft("flow:F-same", "text A");

      await withHeldSend(async (release) => {
        const sending = app.sendReply(
          "F-same", { id: "call:c8", kind: "call", callId: "c8" }, "text A");
        // Edited away and then back again. The characters now match what was
        // delivered, but this is a NEW paragraph the operator is partway
        // through and has never sent — a text comparison cannot tell the two
        // apart, and clearing it would destroy unrecoverable writing.
        typeInto(box, "text A and more");
        typeInto(box, "text A");
        release();
        await sending;
      });
      assert.equal(box.value, "text A", "the re-typed follow-up survives the success");
      app.flushDraftSaves();
      assert.equal(app.loadDraft("flow:F-same"), "text A", "...and is still a draft");
      app.clearDraft("flow:F-same");
    });
  });

  // The two modals never lock their textareas either, so the same in-flight
  // window exists there — and there the cost is worse: the panel is reopened
  // from the draft, so a wrongly-cleared slot shows a blank form.

  // Hold the delivery request open while answering the history endpoint at
  // once, so a rewrite can happen mid-flight. `status` is what the delivery
  // POST finally answers (202 for a launched flow, 200 for a created issue).
  async function withHeldSubmit(status, fn) {
    const savedFetch = globalThis.fetch;
    const savedSetTimeout = globalThis.setTimeout;
    let release = () => {};
    globalThis.setTimeout = () => 0;
    globalThis.fetch = (url) => {
      if (String(url).includes("/api/message-history/")) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ entries: [] }) });
      }
      return new Promise((resolve) => {
        release = () => resolve({ ok: true, status, json: () => Promise.resolve({}) });
      });
    };
    try {
      return await fn(() => release());
    } finally {
      globalThis.fetch = savedFetch;
      globalThis.setTimeout = savedSetTimeout;
      app.clearMessageHistoryCache();
    }
  }

  await checkAsync("G1 a task description rewritten during a slow launch is not erased", async () => {
    const store = makeStorage();
    await withStorageAsync(store, async () => {
      app.state.machines = [
        { machine_id: "M1", hostname: "host-1", online: true, project_roots: ["/srv/proj"] },
      ];
      try {
        app.saveDraft(app.DRAFTS.NEW_TASK, "build the thing");
        document.getElementById("nt-machine").value = "M1";
        document.getElementById("nt-project").value = "/srv/proj";
        document.getElementById("nt-type").value = "feature";
        const box = freshInput("nt-task");
        app.bindDraftInput("nt-task");
        box.value = "build the thing";

        await withHeldSubmit(202, async (release) => {
          const sending = app.submitNewTask({ preventDefault() {} });
          typeInto(box, "the NEXT thing to build");
          release();
          await sending;
        });
        app.flushDraftSaves();
        assert.equal(
          app.loadDraft(app.DRAFTS.NEW_TASK),
          "the NEXT thing to build",
          "a description that was never published stays a draft",
        );
        // ...and the reopened panel hands it back rather than a blank form.
        app.openNewTask();
        assert.equal(
          document.getElementById("nt-task").value,
          "the NEXT thing to build",
        );
      } finally {
        app.state.machines = [];
        app.clearDraft(app.DRAFTS.NEW_TASK);
      }
    });
  });

  await checkAsync("G1 an issue rewritten during a slow create is not erased", async () => {
    const store = makeStorage();
    await withStorageAsync(store, async () => {
      const form = document.getElementById("issue-form");
      app.state.machines = [
        { machine_id: "M1", hostname: "host-1", online: true, project_roots: ["/srv/p"] },
      ];
      try {
        form.dataset.mode = "create";
        app.saveDraft(app.DRAFTS.ISSUE_DESCRIPTION, "something is broken");
        app.saveDraft(app.DRAFTS.ISSUE_TITLE, "broken thing");
        const desc = freshInput("issue-description");
        const title = freshInput("issue-title");
        app.bindDraftInput("issue-description");
        app.bindDraftInput("issue-title");
        desc.value = "something is broken";
        title.value = "broken thing";
        document.getElementById("issue-machine").value = "M1";
        document.getElementById("issue-project").value = "/srv/p";
        document.getElementById("issue-type").value = "";
        document.getElementById("issue-priority").value = "";
        document.getElementById("issue-tags").value = "";

        await withHeldSubmit(200, async (release) => {
          const sending = app.submitIssueForm({ preventDefault() {} });
          // Only the description is rewritten: each box is retired on its own
          // evidence, so the untouched title is still consumed.
          typeInto(desc, "a different thing is broken");
          release();
          await sending;
        });
        app.flushDraftSaves();
        assert.equal(
          app.loadDraft(app.DRAFTS.ISSUE_DESCRIPTION),
          "a different thing is broken",
          "the report that was never filed stays a draft",
        );
        assert.equal(
          app.loadDraft(app.DRAFTS.ISSUE_TITLE), "", "the untouched title was filed",
        );
        app.openIssueCreateModal();
        assert.equal(
          document.getElementById("issue-description").value,
          "a different thing is broken",
        );
      } finally {
        app.state.machines = [];
        app.clearDraft(app.DRAFTS.ISSUE_DESCRIPTION);
        app.clearDraft(app.DRAFTS.ISSUE_TITLE);
        form.dataset.mode = "create";
      }
    });
  });

  await checkAsync("G1 a create that lands after the modal moved on leaves the new form alone", async () => {
    const store = makeStorage();
    await withStorageAsync(store, async () => {
      const form = document.getElementById("issue-form");
      const modal = document.getElementById("issue-modal");
      app.state.machines = [
        { machine_id: "M1", hostname: "host-1", online: true, project_roots: ["/srv/p"] },
      ];
      try {
        app.saveDraft(app.DRAFTS.ISSUE_DESCRIPTION, "something is broken");
        app.saveDraft(app.DRAFTS.ISSUE_TITLE, "broken thing");
        app.openIssueCreateModal();
        const desc = document.getElementById("issue-description");
        const title = document.getElementById("issue-title");
        document.getElementById("issue-machine").value = "M1";
        document.getElementById("issue-project").value = "/srv/p";
        document.getElementById("issue-type").value = "";
        document.getElementById("issue-priority").value = "";
        document.getElementById("issue-tags").value = "";

        await withHeldSubmit(200, async (release) => {
          const sending = app.submitIssueForm({ preventDefault() {} });
          // The operator dismisses the slow create and opens an EXISTING issue
          // to edit. Edit mode writes no draft (see draftKeyForInput), so
          // neither create epoch moves — the epochs alone cannot tell that the
          // two boxes now belong to somewhere else entirely.
          app.openIssueEditModal({
            id: 7,
            machine_id: "M1",
            project_root: "/srv/p",
            description: "the stored body",
            title: "stored title",
            tags: [],
          });
          desc.value = "unsaved edit to issue 7";
          title.value = "unsaved title for issue 7";
          release();
          await sending;
        });
        assert.equal(
          desc.value, "unsaved edit to issue 7",
          "the create's success does not blank a form it never owned",
        );
        assert.equal(title.value, "unsaved title for issue 7");
        assert.equal(
          modal.classList.contains("hidden"), false,
          "nor close the modal out from under the operator",
        );
        // The report that WAS filed is still consumed: its draft was never
        // touched again, and the next New Issue starts from a blank form.
        assert.equal(app.loadDraft(app.DRAFTS.ISSUE_DESCRIPTION), "");
        assert.equal(app.loadDraft(app.DRAFTS.ISSUE_TITLE), "");
      } finally {
        app.state.machines = [];
        app.clearDraft(app.DRAFTS.ISSUE_DESCRIPTION);
        app.clearDraft(app.DRAFTS.ISSUE_TITLE);
        form.dataset.mode = "create";
        modal.classList.add("hidden");
      }
    });
  });

  // ---- reopening inside the debounce window -------------------------------

  check("G1 reopening the New Task panel inside the debounce window restores the queued text", () => {
    const store = makeStorage();
    withStorage(store, () => {
      const savedSetTimeout = globalThis.setTimeout;
      // The debounce never fires: the draft exists only as a queued save.
      globalThis.setTimeout = () => 0;
      try {
        app.state.machines = [
          { machine_id: "M1", hostname: "host-1", online: true, project_roots: ["/srv/p"] },
        ];
        const box = freshInput("nt-task");
        app.bindDraftInput("nt-task");
        typeInto(box, "queued but not yet written");
        assert.equal(app.loadDraft(app.DRAFTS.NEW_TASK), "", "nothing is in storage yet");
        // Dismiss and reopen — openNewTask blanks the box, then restores.
        app.openNewTask();
        assert.equal(
          document.getElementById("nt-task").value,
          "queued but not yet written",
          "the queued save is what comes back, not a blank box",
        );
      } finally {
        globalThis.setTimeout = savedSetTimeout;
        app.clearDraft(app.DRAFTS.NEW_TASK);
        app.state.machines = [];
      }
    });
  });

  check("G1 re-entering a flow inside the debounce window restores the queued reply", () => {
    const store = makeStorage();
    withStorage(store, () => {
      const savedSetTimeout = globalThis.setTimeout;
      globalThis.setTimeout = () => 0;
      try {
        app.state.selectedFlowId = "F-debounce";
        const box = freshInput("flow-reply-input");
        app.bindDraftInput("flow-reply-input");
        typeInto(box, "half an answer");
        assert.equal(app.loadDraft("flow:F-debounce"), "");
        app.resetReplyBox();
        assert.equal(box.value, "half an answer");
      } finally {
        globalThis.setTimeout = savedSetTimeout;
        app.clearDraft("flow:F-debounce");
      }
    });
  });

  // Leave no borrowed state behind for the checks that follow.
  app.state.selectedFlowId = null;
  app.state.flowDetail = null;
  app.state.flowConversationRecords = [];
  app.state.machines = [];
  document.getElementById("issue-form").dataset.mode = "create";
  app.resetRenderSignatures();
}
