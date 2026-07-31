/*
 * File-attachment upload tests (G5 — interaction wiring; G6 — pure helpers).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub is
 * installed. Exposes `registerFileUploadTests({app, check, checkAsync, findOne,
 * findAll, objectUrls})` so the parent harness drives the same reporter, the
 * same `app` module export and the same object-URL recorder.
 *
 * The invariant every case below circles is the feature's one rule: THE
 * TEXTAREA TEXT IS THE PROMPT. A paste parks a placeholder at the caret, the
 * answer swaps it for the project-relative path in place, and nothing is
 * substituted at submit time. So the assertions are mostly about the exact
 * string left in `input.value` — that string, not the attachment strip, is what
 * the agent will read.
 *
 * Coverage:
 *   (0) the DOM-free helpers on their own — the size bound, the size/type
 *       formatters, the literal first-occurrence edits, the caret insert and
 *       the code→i18n-key map, each pinned at its boundary.
 *   (a) resolveUploadTarget — the two scopes and their three refusals.
 *   (b) uploadRequestUrl — flow vs machine+root query shapes, encoding.
 *   (c) performUpload — success (in-place swap), failure (clean rollback), the
 *       user-deleted-the-placeholder race, and concurrent pastes.
 *   (d) startUploads — the browser-side size bound and the unresolved-target
 *       refusal, both asserted as "no request was made".
 *   (e) renderAttachmentStrip — thumbnail vs icon rows, hidden-when-empty.
 *   (f) removeAttachment / clearAttachments — text-only removal, and the
 *       hard boundary that nothing is ever deleted on the project machine.
 *   (g) paste / drop / picker handlers — plain text left untouched, drop
 *       claimed only for files, picker value reset.
 *
 * The server and daemon halves are covered independently by
 * tests/server/test_upload_api.py and tests/daemon/test_uploads.py; nothing
 * here is a security control (the browser is not trusted by either).
 */
import assert from "node:assert/strict";

export async function registerFileUploadTests(ctx) {
  const { app, check, checkAsync, findAll, findOne, objectUrls } = ctx;
  const state = app.state;

  // ---- harness -------------------------------------------------------------
  // Records every request app.js makes so a "must not upload" assertion is a
  // real observation rather than an absence of side effects.
  function installFetch(responder) {
    const calls = [];
    const saved = globalThis.fetch;
    globalThis.fetch = async (url, init) => {
      calls.push({ url: String(url), init: init || {} });
      const spec = responder(String(url), init || {}) || {};
      if (spec.throws) throw new Error("network down");
      const status = spec.status === undefined ? 201 : spec.status;
      return {
        ok: status >= 200 && status < 300,
        status,
        json: async () => (spec.body === undefined ? {} : spec.body),
      };
    };
    return { calls, restore: () => { globalThis.fetch = saved; } };
  }

  const okUpload = (path, extra) => ({
    status: 201,
    body: Object.assign({ status: "stored", path, size: 4 }, extra || {}),
  });

  // A stand-in File: app.js only ever reads name/size/type and hands the object
  // straight to fetch as the body, so this is the whole contract.
  const fakeFile = (name, size, type) => ({
    name,
    size: size === undefined ? 4 : size,
    type: type === undefined ? "" : type,
  });

  function $(id) { return globalThis.document.getElementById(id); }

  function resetScopes() {
    state.uploadAttachments = {};
    state.uploadSeq = 0;
    state.selectedFlowId = null;
    for (const id of ["nt-task", "flow-reply-input"]) {
      const el = $(id);
      el.value = "";
      el.selectionStart = 0;
      el.selectionEnd = 0;
    }
    for (const id of ["nt-attachments", "flow-attachments"]) {
      const strip = $(id);
      strip.innerHTML = "";
      strip.classList.add("hidden");
    }
    $("nt-machine").value = "";
    $("nt-project").value = "";
    $("toast-container").innerHTML = "";
  }

  function toasts() {
    return $("toast-container").children.map((n) => n.textContent);
  }

  // Toast dismissal is the only timer in this path; letting real 6s timers
  // accumulate would just stall the node run at exit.
  const savedSetTimeout = globalThis.setTimeout;
  globalThis.setTimeout = () => 0;

  // Preview object URLs come from the harness-wide recorder, so "every URL this
  // suite minted was handed back" is checkable as created-vs-revoked pairing
  // rather than against a hand-written literal.
  const revoked = objectUrls.revoked;
  function resetObjectUrls() {
    objectUrls.created.length = 0;
    objectUrls.revoked.length = 0;
  }

  try {
    // ========================================================================
    // (0) the DOM-free helpers, on their own
    // ========================================================================
    //
    // Everything below runs on plain object literals: no textarea, no strip, no
    // fetch. These are the transforms the interaction layer is assembled from,
    // so each one is pinned at the boundary where it decides something — the
    // exact byte count that is still allowed, the one occurrence that is
    // rewritten, the caret offset the next keystroke will land at.

    check("G6 MAX_UPLOAD_BYTES is exactly 20 MiB", () => {
      // The same number the server and the daemon each re-check; the static
      // guard in tests/test_frontend_file_upload.py pins it to protocol.py.
      assert.equal(app.MAX_UPLOAD_BYTES, 20 * 1024 * 1024);
    });

    check("G6 validateUploadFile: the limit itself passes, one byte over does not", () => {
      const at = app.validateUploadFile(fakeFile("a.bin", app.MAX_UPLOAD_BYTES));
      assert.deepEqual(at, { ok: true, code: "" });
      const over = app.validateUploadFile(fakeFile("a.bin", app.MAX_UPLOAD_BYTES + 1));
      assert.equal(over.ok, false);
      // The code is drawn from the daemon's vocabulary so the local refusal and
      // the wire refusal localize through the one map.
      assert.equal(over.code, "too_large");
      assert.equal(app.validateUploadFile(fakeFile("a.bin", 0)).ok, true, "an empty file is legal");
    });

    check("G6 validateUploadFile: an unusable name or size is refused before any request", () => {
      assert.equal(app.validateUploadFile(fakeFile("", 4)).code, "invalid_filename");
      assert.equal(app.validateUploadFile(fakeFile("   ", 4)).code, "invalid_filename");
      assert.equal(app.validateUploadFile(fakeFile("a.bin", -1)).code, "invalid_payload");
      assert.equal(app.validateUploadFile(fakeFile("a.bin", Number.NaN)).code, "invalid_payload");
      assert.equal(app.validateUploadFile(null).code, "invalid_payload");
      assert.equal(app.validateUploadFile("a.bin").code, "invalid_payload");
    });

    check("G6 formatFileSize: unit thresholds, and the .0 tail is dropped", () => {
      assert.equal(app.formatFileSize(0), "0 B");
      assert.equal(app.formatFileSize(1023), "1023 B");
      assert.equal(app.formatFileSize(1024), "1 KB", "no bare 1.0");
      assert.equal(app.formatFileSize(1536), "1.5 KB");
      assert.equal(app.formatFileSize(1024 * 1024 - 1), "1024 KB");
      assert.equal(app.formatFileSize(1024 * 1024), "1 MB");
      // The ceiling renders as the round number the error message quotes.
      assert.equal(app.formatFileSize(app.MAX_UPLOAD_BYTES), "20 MB");
      // Junk reads as zero rather than "NaN B" in the strip.
      assert.equal(app.formatFileSize(-5), "0 B");
      assert.equal(app.formatFileSize(undefined), "0 B");
    });

    check("G6 isImageFile: the MIME type wins, the extension is the fallback", () => {
      assert.equal(app.isImageFile(fakeFile("a.png", 1, "image/png")), true);
      assert.equal(app.isImageFile(fakeFile("a.png", 1, "text/plain")), false,
        "a declared type is never second-guessed by the name");
      // Some drag sources hand over a blank type; the name is all that is left.
      assert.equal(app.isImageFile(fakeFile("a.PNG", 1, "")), true);
      assert.equal(app.isImageFile(fakeFile("shot.jpeg", 1, "")), true);
      assert.equal(app.isImageFile(fakeFile("notes.txt", 1, "")), false);
      assert.equal(app.isImageFile(fakeFile("png", 1, "")), false, "a bare word is not an extension");
      assert.equal(app.isImageFile(null), false);
    });

    check("G6 uploadErrorKey: every wire code maps, and an unknown one still reads", () => {
      assert.equal(app.uploadErrorKey("too_large"), "upload.errTooLarge");
      assert.equal(app.uploadErrorKey("not_registered"), "upload.errUnregisteredProject");
      // invalid_path folds into the same message on purpose: both mean the
      // daemon refused the named root, and the remedy is the same one.
      assert.equal(app.uploadErrorKey("invalid_path"), "upload.errUnregisteredProject");
      assert.equal(app.uploadErrorKey("write_failed"), "upload.errWriteFailed");
      assert.equal(app.uploadErrorKey("unsupported_daemon"), "upload.errUnsupportedDaemon");
      assert.equal(app.uploadErrorKey("not_connected"), "upload.errNotConnected");
      assert.equal(app.uploadErrorKey("timeout"), "upload.errTimeout");
      assert.equal(app.uploadErrorKey("network"), "upload.errNetwork");
      assert.equal(app.uploadErrorKey(" timeout "), "upload.errTimeout", "whitespace is trimmed");
      // A code from a newer daemon paints the generic message, never the raw
      // token — the UI must not leak wire vocabulary at the user.
      assert.equal(app.uploadErrorKey("quota_exceeded"), "upload.errFailed");
      assert.equal(app.uploadErrorKey(""), "upload.errFailed");
      assert.equal(app.uploadErrorKey(undefined), "upload.errFailed");
      for (const key of Object.values(app.UPLOAD_ERROR_KEYS)) {
        assert.equal(key.startsWith("upload."), true, `${key} is an i18n key`);
      }
    });

    check("G6 uploadPlaceholderToken: two pastes of one file get two distinct tokens", () => {
      const a = app.uploadPlaceholderToken("shot.png", 1);
      const b = app.uploadPlaceholderToken("shot.png", 2);
      assert.equal(a.includes("shot.png"), true, "the user can tell which file it is");
      // WHY distinct: the first answer replaces the FIRST match, so identical
      // tokens would let paste #1's path land on paste #2's marker.
      assert.notEqual(a, b);
      assert.equal(app.uploadPlaceholderToken("", 0).includes("file"), true,
        "a nameless blob still gets a visible marker");
    });

    check("G6 replaceTokenOnce: literal, first occurrence, miss is a no-op", () => {
      assert.equal(app.replaceTokenOnce("a T b T c", "T", "X"), "a X b T c");
      // The needle embeds a user-supplied file name; compiled as a pattern
      // `a+b(1).png` would match the wrong span or throw outright.
      assert.equal(
        app.replaceTokenOnce("see [up a+b(1).png] here", "[up a+b(1).png]", "p/f.png"),
        "see p/f.png here",
      );
      assert.equal(app.replaceTokenOnce("a+b", ".", "X"), "a+b", "a regex dot matches nothing");
      assert.equal(app.replaceTokenOnce("plain", "[gone]", "X"), "plain");
      assert.equal(app.replaceTokenOnce("plain", "", "X"), "plain", "an empty needle inserts nothing");
      assert.equal(app.replaceTokenOnce(null, "T", "X"), "");
    });

    check("G6 removePathOnce: takes the one occurrence it put there, no more", () => {
      const path = "tianluo/uploads/aaaaaaaaaaaa_a.png";
      assert.equal(app.removePathOnce(`look ${path} and ${path}`, path), `look  and ${path}`);
      // A path the user hand-edited no longer matches, and the text — which IS
      // the prompt — is left exactly as they wrote it.
      assert.equal(app.removePathOnce("look tianluo/uploads/EDITED.png", path),
        "look tianluo/uploads/EDITED.png");
    });

    check("G6 insertAtCaret: lands at the caret and leaves it past the insert", () => {
      const el = { value: "ab", selectionStart: 1, selectionEnd: 1 };
      assert.equal(app.insertAtCaret(el, "XY"), "aXYb");
      assert.equal(el.value, "aXYb");
      assert.equal(el.selectionStart, 3, "typing continues after what was inserted");
      assert.equal(el.selectionEnd, 3);

      const sel = { value: "abcd", selectionStart: 1, selectionEnd: 3 };
      app.insertAtCaret(sel, "X");
      assert.equal(sel.value, "aXd", "a selection is replaced, as a keystroke would");
      assert.equal(sel.selectionStart, 2);

      // A never-focused field reports no selection; appending beats silently
      // prepending at 0, which would bury the marker above the user's text.
      const unfocused = { value: "draft" };
      app.insertAtCaret(unfocused, "!");
      assert.equal(unfocused.value, "draft!");
      assert.equal(unfocused.selectionStart, 6);

      const past = { value: "ab", selectionStart: 99, selectionEnd: 99 };
      app.insertAtCaret(past, "X");
      assert.equal(past.value, "abX", "an out-of-range caret is clamped, not honoured");
    });

    check("G6 attachmentRowModel: removable only once a path is in the text", () => {
      const done = app.attachmentRowModel({
        id: 7, name: "a.png", size: 2048, type: "image/png",
        status: "done", path: "tianluo/uploads/aaa_a.png", previewUrl: "blob:a",
      });
      assert.equal(done.id, "7", "the id is a string for DOM lookup");
      assert.equal(done.sizeText, "2 KB");
      assert.equal(done.isImage, true);
      assert.equal(done.statusText, "", "a finished row says nothing extra");
      assert.equal(done.canRemove, true);
      assert.equal(done.errorKey, "");

      // In flight: removing it would strand the answer with no token to swap.
      const flying = app.attachmentRowModel({ id: "u1", name: "a.bin", size: 10, status: "uploading" });
      assert.equal(flying.canRemove, false);
      assert.equal(flying.statusText, "Uploading…");

      const failed = app.attachmentRowModel({ id: "u2", name: "a.bin", status: "error", code: "write_failed" });
      assert.equal(failed.canRemove, false);
      assert.equal(failed.errorKey, "upload.errWriteFailed");

      // A stored file whose placeholder the user deleted has nothing to point
      // at, so it is not removable either.
      assert.equal(app.attachmentRowModel({ id: "u3", status: "done", path: "" }).canRemove, false);
      // Anything unrecognised is treated as still in flight — the safe side,
      // since it withholds the destructive action rather than offering it.
      assert.equal(app.attachmentRowModel({ id: "u4", status: "weird" }).status, "uploading");
      assert.equal(app.attachmentRowModel(null).name, "");
    });

    // ========================================================================
    // (a) resolveUploadTarget
    // ========================================================================
    check("G5 resolveUploadTarget: flow scope names only the flow id", () => {
      resetScopes();
      state.selectedFlowId = "flow-abc";
      const t = app.resolveUploadTarget("flow");
      assert.equal(t.ok, true);
      assert.equal(t.kind, "flow");
      assert.equal(t.flowId, "flow-abc");
      // The server re-derives machine + root from the flow snapshot; the
      // browser must not be the one asserting where a running flow lives.
      assert.equal(t.projectRoot, undefined);
      assert.equal(t.machineId, undefined);
    });

    check("G5 resolveUploadTarget: no open flow is a no_target refusal", () => {
      resetScopes();
      const t = app.resolveUploadTarget("flow");
      assert.equal(t.ok, false);
      assert.equal(t.code, "no_target");
      assert.equal(t.errorKey, "upload.errNoTarget");
    });

    check("G5 resolveUploadTarget: New Task returns machine + registered root", () => {
      resetScopes();
      $("nt-machine").value = "m1";
      $("nt-project").value = "/home/u/proj";
      const t = app.resolveUploadTarget("newTask");
      assert.deepEqual(t, { ok: true, kind: "project", machineId: "m1", projectRoot: "/home/u/proj" });
    });

    check("G5 resolveUploadTarget: an unpicked machine/project is no_target", () => {
      resetScopes();
      assert.equal(app.resolveUploadTarget("newTask").errorKey, "upload.errNoTarget");
      $("nt-machine").value = "m1";
      assert.equal(app.resolveUploadTarget("newTask").errorKey, "upload.errNoTarget");
      $("nt-machine").value = "";
      $("nt-project").value = "/home/u/proj";
      assert.equal(app.resolveUploadTarget("newTask").errorKey, "upload.errNoTarget");
    });

    check("G5 resolveUploadTarget: the manual-path sentinel is refused up front", () => {
      resetScopes();
      $("nt-machine").value = "m1";
      $("nt-project").value = "__manual__";
      const t = app.resolveUploadTarget("newTask");
      assert.equal(t.ok, false);
      // The daemon writes only into roots it has registered, and "Other path…"
      // is by definition a root it has not — so this is refused locally with
      // the same remedy the daemon would have named.
      assert.equal(t.code, "not_registered");
      assert.equal(t.errorKey, "upload.errUnregisteredProject");
    });

    // ========================================================================
    // (b) uploadRequestUrl
    // ========================================================================
    check("G5 uploadRequestUrl: flow target carries flow_id only", () => {
      const url = app.uploadRequestUrl({ kind: "flow", flowId: "f 1" }, "a b.png");
      assert.equal(url, "/api/uploads?filename=a%20b.png&flow_id=f%201");
      assert.equal(url.includes("project_root"), false);
    });

    check("G5 uploadRequestUrl: project target carries machine_id + project_root", () => {
      const url = app.uploadRequestUrl(
        { kind: "project", machineId: "m1", projectRoot: "/home/u/my proj" },
        "notes.txt",
      );
      assert.equal(
        url,
        "/api/uploads?filename=notes.txt&machine_id=m1&project_root=%2Fhome%2Fu%2Fmy%20proj",
      );
    });

    // ========================================================================
    // (c) performUpload
    // ========================================================================
    await checkAsync("G5 performUpload: the path lands exactly where it was pasted", async () => {
      resetScopes();
      const f = installFetch(() => okUpload("tianluo/uploads/abc123def456_shot.png"));
      try {
        const input = $("nt-task");
        input.value = "before  after";
        input.selectionStart = 7;
        input.selectionEnd = 7;
        const entry = await app.performUpload(
          input,
          fakeFile("shot.png", 12, "image/png"),
          { ok: true, kind: "project", machineId: "m1", projectRoot: "/p" },
          "nt-attachments",
        );
        assert.equal(input.value, "before tianluo/uploads/abc123def456_shot.png after");
        assert.equal(entry.status, "done");
        assert.equal(entry.path, "tianluo/uploads/abc123def456_shot.png");
        assert.equal(f.calls.length, 1, "exactly one POST per file");
        assert.equal(f.calls[0].init.method, "POST");
        assert.equal(f.calls[0].init.headers["Content-Type"], "application/octet-stream");
        assert.equal(f.calls[0].init.body.name, "shot.png", "the File itself is the body");
        assert.equal(f.calls[0].url.includes("machine_id=m1"), true);
      } finally {
        f.restore();
      }
    });

    await checkAsync("G5 performUpload: the placeholder is visible while in flight", async () => {
      resetScopes();
      let seen = null;
      const input = $("nt-task");
      const f = installFetch(() => {
        // Sampled from inside the request: the caret position must already be
        // marked, which is the whole reason a placeholder exists.
        seen = input.value;
        return okUpload("tianluo/uploads/aaaaaaaaaaaa_a.txt");
      });
      try {
        await app.performUpload(
          input,
          fakeFile("a.txt"),
          { ok: true, kind: "flow", flowId: "f1" },
          "nt-attachments",
        );
        assert.equal(seen.includes("a.txt"), true, "placeholder names the file");
        assert.equal(seen.includes("uploading"), true);
        assert.equal(input.value, "tianluo/uploads/aaaaaaaaaaaa_a.txt");
      } finally {
        f.restore();
      }
    });

    await checkAsync("G5 performUpload: a failure leaves no placeholder and no half path", async () => {
      resetScopes();
      const f = installFetch(() => ({
        status: 409,
        body: { detail: "root not registered", error_code: "not_registered" },
      }));
      try {
        const input = $("nt-task");
        input.value = "hello";
        input.selectionStart = 5;
        input.selectionEnd = 5;
        await app.performUpload(
          input,
          fakeFile("x.bin"),
          { ok: true, kind: "flow", flowId: "f1" },
          "nt-attachments",
        );
        assert.equal(input.value, "hello", "text is restored byte-for-byte");
        assert.equal(input.value.includes("uploading"), false);
        assert.equal(input.value.includes("uploads/"), false);
        const rows = app.attachmentEntries("nt-attachments");
        assert.equal(rows.length, 1);
        assert.equal(rows[0].status, "error");
        assert.equal(rows[0].code, "not_registered");
        // Localized from the CODE, never from the daemon's English prose.
        const msg = toasts().join("|");
        assert.equal(msg.includes("re-add it under Projects"), true, msg);
        assert.equal(msg.includes("root not registered"), false, "raw backend prose must not surface");
      } finally {
        f.restore();
      }
    });

    await checkAsync("G5 performUpload: a dropped connection reports the network key", async () => {
      resetScopes();
      const f = installFetch(() => ({ throws: true }));
      try {
        const input = $("flow-reply-input");
        input.value = "draft";
        input.selectionStart = 5;
        input.selectionEnd = 5;
        await app.performUpload(
          input,
          fakeFile("x.bin"),
          { ok: true, kind: "flow", flowId: "f1" },
          "flow-attachments",
        );
        assert.equal(input.value, "draft");
        assert.equal(app.attachmentEntries("flow-attachments")[0].code, "network");
        assert.equal(toasts().join("|").includes("connection to the server dropped"), true);
      } finally {
        f.restore();
      }
    });

    await checkAsync("G5 performUpload: a placeholder deleted mid-flight is not resurrected", async () => {
      resetScopes();
      const input = $("nt-task");
      const f = installFetch(() => {
        // The user gave up on this paste while the bytes were in flight.
        input.value = "just typing";
        return okUpload("tianluo/uploads/bbbbbbbbbbbb_b.txt");
      });
      try {
        const entry = await app.performUpload(
          input,
          fakeFile("b.txt"),
          { ok: true, kind: "flow", flowId: "f1" },
          "nt-attachments",
        );
        assert.equal(input.value, "just typing", "no path is appended anywhere");
        // The file IS stored — the daemon already wrote it — but there is no
        // occurrence in the text, so the row has nothing to point at.
        assert.equal(entry.status, "done");
        assert.equal(entry.path, "");
        assert.equal(app.attachmentRowModel(entry).canRemove, false);
      } finally {
        f.restore();
      }
    });

    await checkAsync("G5 performUpload: concurrent pastes never cross their tokens", async () => {
      resetScopes();
      const input = $("nt-task");
      const resolvers = [];
      const saved = globalThis.fetch;
      const bodies = [];
      globalThis.fetch = (url, init) => {
        bodies.push(init.body.name);
        return new Promise((resolve) => { resolvers.push({ url, resolve }); });
      };
      try {
        const target = { ok: true, kind: "flow", flowId: "f1" };
        const p1 = app.performUpload(input, fakeFile("one.txt"), target, "nt-attachments");
        const p2 = app.performUpload(input, fakeFile("two.txt"), target, "nt-attachments");
        assert.equal(resolvers.length, 2);
        const mid = input.value;
        assert.equal(mid.includes("one.txt"), true);
        assert.equal(mid.includes("two.txt"), true);
        // Answer them out of order — the token, not arrival order, decides
        // which span each path replaces.
        const reply = (path) => ({
          ok: true,
          status: 201,
          json: async () => ({ path }),
        });
        resolvers[1].resolve(reply("tianluo/uploads/222222222222_two.txt"));
        resolvers[0].resolve(reply("tianluo/uploads/111111111111_one.txt"));
        const [e1, e2] = await Promise.all([p1, p2]);
        assert.equal(e1.path, "tianluo/uploads/111111111111_one.txt");
        assert.equal(e2.path, "tianluo/uploads/222222222222_two.txt");
        assert.equal(
          input.value.indexOf("111111111111_one.txt")
            < input.value.indexOf("222222222222_two.txt"),
          true,
          "each path stayed at its own paste position",
        );
        assert.equal(input.value.includes("uploading"), false, "no token survives");
        assert.deepEqual(bodies, ["one.txt", "two.txt"]);
      } finally {
        globalThis.fetch = saved;
      }
    });

    // ========================================================================
    // (d) startUploads — the two pre-flight refusals
    // ========================================================================
    check("G5 startUploads: an over-sized file never leaves the browser", () => {
      resetScopes();
      state.selectedFlowId = "f1";
      const f = installFetch(() => okUpload("x"));
      try {
        const started = app.startUploads("flow", [fakeFile("huge.bin", app.MAX_UPLOAD_BYTES + 1)]);
        assert.equal(started.length, 0);
        assert.equal(f.calls.length, 0, "no request at all");
        assert.equal($("flow-reply-input").value, "", "and no placeholder either");
        assert.equal(toasts().join("|").includes("larger than the 20 MB limit"), true);
      } finally {
        f.restore();
      }
    });

    check("G5 startUploads: exactly the limit is still accepted", () => {
      resetScopes();
      state.selectedFlowId = "f1";
      const f = installFetch(() => okUpload("tianluo/uploads/cccccccccccc_ok.bin"));
      try {
        const started = app.startUploads("flow", [fakeFile("ok.bin", app.MAX_UPLOAD_BYTES)]);
        assert.equal(started.length, 1);
        assert.equal(f.calls.length, 1);
      } finally {
        f.restore();
      }
    });

    check("G5 startUploads: an unresolved target toasts and sends nothing", () => {
      resetScopes();
      const f = installFetch(() => okUpload("x"));
      try {
        const started = app.startUploads("flow", [fakeFile("a.txt")]);
        assert.equal(started.length, 0);
        assert.equal(f.calls.length, 0);
        assert.equal(toasts().join("|").includes("Choose a machine and a project"), true);
      } finally {
        f.restore();
      }
    });

    check("G5 startUploads: the target is resolved once per gesture", () => {
      resetScopes();
      $("nt-machine").value = "m1";
      $("nt-project").value = "__manual__";
      const f = installFetch(() => okUpload("x"));
      try {
        app.startUploads("newTask", [fakeFile("a.txt"), fakeFile("b.txt"), fakeFile("c.txt")]);
        assert.equal(f.calls.length, 0);
        // One refusal for the gesture, not one per file — three identical
        // toasts would bury the input box they are about.
        assert.equal(toasts().length, 1);
      } finally {
        f.restore();
      }
    });

    // ========================================================================
    // (e) renderAttachmentStrip
    // ========================================================================
    check("G5 renderAttachmentStrip: an empty strip is hidden", () => {
      resetScopes();
      app.renderAttachmentStrip("nt-attachments", []);
      const strip = $("nt-attachments");
      assert.equal(strip.classList.contains("hidden"), true);
      assert.equal(strip.children.length, 0);
    });

    check("G5 renderAttachmentStrip: an image row renders a thumbnail", () => {
      resetScopes();
      app.renderAttachmentStrip("nt-attachments", [{
        id: "u1", name: "shot.png", size: 2048, type: "image/png",
        status: "done", path: "tianluo/uploads/aaa_shot.png", previewUrl: "blob:shot",
      }]);
      const strip = $("nt-attachments");
      assert.equal(strip.classList.contains("hidden"), false);
      const thumb = findOne(strip, "attachment-thumb");
      assert.notEqual(thumb, null, "an image entry gets a preview node");
      assert.equal(thumb.src, "blob:shot");
      assert.equal(findOne(strip, "attachment-icon"), null);
      assert.equal(findOne(strip, "attachment-name").textContent, "shot.png");
      assert.equal(findOne(strip, "attachment-size").textContent, "2 KB");
    });

    check("G5 renderAttachmentStrip: a plain file renders icon + name + size", () => {
      resetScopes();
      app.renderAttachmentStrip("nt-attachments", [{
        id: "u1", name: "notes.txt", size: 300, type: "text/plain",
        status: "done", path: "tianluo/uploads/aaa_notes.txt",
      }]);
      const strip = $("nt-attachments");
      assert.notEqual(findOne(strip, "attachment-icon"), null);
      assert.equal(findOne(strip, "attachment-thumb"), null);
      assert.equal(findOne(strip, "attachment-name").textContent, "notes.txt");
      assert.equal(findOne(strip, "attachment-size").textContent, "300 B");
      assert.equal(findAll(strip, "attachment-remove").length, 1);
    });

    check("G5 renderAttachmentStrip: an in-flight row shows status and cannot be removed", () => {
      resetScopes();
      app.renderAttachmentStrip("nt-attachments", [{
        id: "u1", name: "big.bin", size: 4096, status: "uploading",
      }]);
      const strip = $("nt-attachments");
      assert.equal(findOne(strip, "attachment-size").textContent, "Uploading…");
      // Removing it would strand the in-flight response with no token to swap.
      assert.equal(findAll(strip, "attachment-remove").length, 0);
      assert.equal(strip.children[0].classList.contains("uploading"), true);
    });

    check("G5 renderAttachmentStrip: a failed row stays, dismissable, with its reason", () => {
      resetScopes();
      app.renderAttachmentStrip("nt-attachments", [{
        id: "u1", name: "x.bin", size: 10, status: "error", code: "write_failed",
      }]);
      const strip = $("nt-attachments");
      const item = strip.children[0];
      assert.equal(item.classList.contains("error"), true);
      assert.equal(item.title.includes("could not save the file"), true);
      // Its placeholder is already gone from the text, so dismissal is the
      // only way to clear the row.
      assert.equal(findAll(strip, "attachment-remove").length, 1);
    });

    // ========================================================================
    // (f) removeAttachment / clearAttachments
    // ========================================================================
    await checkAsync("G5 removeAttachment: deletes the path from the text and nothing else", async () => {
      resetScopes();
      state.selectedFlowId = "f1";
      const f = installFetch(() => okUpload("tianluo/uploads/dddddddddddd_d.txt"));
      try {
        const input = $("flow-reply-input");
        input.value = "look at ";
        input.selectionStart = 8;
        input.selectionEnd = 8;
        await Promise.all(app.startUploads("flow", [fakeFile("d.txt")]));
        assert.equal(input.value, "look at tianluo/uploads/dddddddddddd_d.txt");
        const id = app.attachmentEntries("flow-attachments")[0].id;
        const before = f.calls.length;
        app.removeAttachment("flow-attachments", id);
        assert.equal(input.value, "look at ", "only the path went away");
        assert.equal(app.attachmentEntries("flow-attachments").length, 0);
        assert.equal($("flow-attachments").classList.contains("hidden"), true);
        // WHY this matters: the stored file is content-addressed and may be
        // referenced by an already-submitted prompt. Removal is a text edit,
        // never a delete on the project machine.
        assert.equal(f.calls.length, before, "no request of any kind is issued");
      } finally {
        f.restore();
      }
    });

    check("G5 removeAttachment: a hand-edited path is left alone", () => {
      resetScopes();
      const input = $("nt-task");
      input.value = "see tianluo/uploads/EDITED_d.txt and keep this";
      app.attachmentEntries("nt-attachments").push({
        id: "u1", name: "d.txt", size: 4, status: "done",
        path: "tianluo/uploads/dddddddddddd_d.txt",
      });
      app.removeAttachment("nt-attachments", "u1");
      // The recorded path is no longer in the text; a fuzzy match here would
      // eat the user's own words.
      assert.equal(input.value, "see tianluo/uploads/EDITED_d.txt and keep this");
      assert.equal(app.attachmentEntries("nt-attachments").length, 0);
    });

    check("G5 removeAttachment: only the first occurrence of a repeated path goes", () => {
      resetScopes();
      const input = $("nt-task");
      input.value = "a P b P c";
      app.attachmentEntries("nt-attachments").push({
        id: "u1", name: "p", size: 1, status: "done", path: "P",
      });
      app.removeAttachment("nt-attachments", "u1");
      // The user may have copied the path elsewhere on purpose; this operation
      // owns exactly the one occurrence it put there.
      assert.equal(input.value, "a  b P c");
    });

    check("G5 clearAttachments: empties the strip and recycles preview URLs", () => {
      resetScopes();
      resetObjectUrls();
      const input = $("nt-task");
      input.value = "tianluo/uploads/aaa_p.png";
      app.attachmentEntries("nt-attachments").push({
        id: "u1", name: "p.png", size: 4, status: "done",
        path: "tianluo/uploads/aaa_p.png", previewUrl: "blob:p.png",
      });
      app.renderAttachmentStrip("nt-attachments");
      app.clearAttachments("nt-attachments");
      assert.equal(app.attachmentEntries("nt-attachments").length, 0);
      assert.equal($("nt-attachments").classList.contains("hidden"), true);
      assert.deepEqual(revoked, ["blob:p.png"]);
      // Clearing follows the text being sent — it never edits the text itself.
      assert.equal(input.value, "tianluo/uploads/aaa_p.png");
    });

    await checkAsync("G5 a sent reply clears the docked strip", async () => {
      resetScopes();
      state.selectedFlowId = "f1";
      resetObjectUrls();
      const f = installFetch(() => okUpload("tianluo/uploads/eeeeeeeeeeee_e.png"));
      try {
        await Promise.all(app.startUploads("flow", [fakeFile("e.png", 8, "image/png")]));
        assert.equal(app.attachmentEntries("flow-attachments").length, 1);
        assert.equal($("flow-attachments").classList.contains("hidden"), false);
        assert.equal(objectUrls.created.length, 1, "the image row got a preview URL");
        app.clearAttachments("flow-attachments");
        assert.equal($("flow-attachments").classList.contains("hidden"), true);
        // Every URL minted for this reply is handed back — an unpaired one is a
        // blob the browser would hold until the tab closes.
        assert.deepEqual(objectUrls.revoked, objectUrls.created);
      } finally {
        f.restore();
      }
    });

    // ========================================================================
    // (g) paste / drop / picker
    // ========================================================================
    check("G5 handleInputPaste: a plain-text paste is left entirely alone", () => {
      resetScopes();
      state.selectedFlowId = "f1";
      const f = installFetch(() => okUpload("x"));
      try {
        let prevented = false;
        const started = app.handleInputPaste(
          { clipboardData: { files: [], items: [] }, preventDefault: () => { prevented = true; } },
          "flow",
        );
        assert.equal(started.length, 0);
        assert.equal(prevented, false, "the browser's own text insertion must still run");
        assert.equal(f.calls.length, 0);
      } finally {
        f.restore();
      }
    });

    check("G5 handleInputPaste: a pasted screenshot is claimed and uploaded", () => {
      resetScopes();
      state.selectedFlowId = "f1";
      const f = installFetch(() => okUpload("tianluo/uploads/ffffffffffff_p.png"));
      try {
        let prevented = false;
        const img = fakeFile("p.png", 9, "image/png");
        const started = app.handleInputPaste({
          // The items-only shape some browsers use for a clipboard image.
          clipboardData: { items: [{ kind: "file", getAsFile: () => img }] },
          preventDefault: () => { prevented = true; },
        }, "flow");
        assert.equal(started.length, 1);
        assert.equal(prevented, true, "the raw bytes must not also be pasted as text");
        assert.equal(f.calls.length, 1);
      } finally {
        f.restore();
      }
    });

    check("G5 handleInputDragOver: only a file drag claims the event", () => {
      resetScopes();
      let prevented = false;
      const ev = (types) => ({
        dataTransfer: { types, files: [] },
        preventDefault: () => { prevented = true; },
      });
      assert.equal(app.handleInputDragOver(ev(["text/plain"]), "flow"), false);
      assert.equal(prevented, false, "an in-textarea text drag stays a normal edit");
      assert.equal($("flow-reply-input").classList.contains("drop-active"), false);

      assert.equal(app.handleInputDragOver(ev(["Files"]), "flow"), true);
      assert.equal(prevented, true, "otherwise the browser navigates to the file");
      assert.equal($("flow-reply-input").classList.contains("drop-active"), true);
    });

    check("G5 handleInputDrop: takes dataTransfer files and clears the highlight", () => {
      resetScopes();
      state.selectedFlowId = "f1";
      const f = installFetch(() => okUpload("tianluo/uploads/aaaaaaaaaaaa_a.txt"));
      try {
        $("flow-reply-input").classList.add("drop-active");
        const started = app.handleInputDrop({
          dataTransfer: { files: [fakeFile("a.txt")] },
          preventDefault: () => {},
        }, "flow");
        assert.equal(started.length, 1);
        assert.equal($("flow-reply-input").classList.contains("drop-active"), false);
      } finally {
        f.restore();
      }
    });

    check("G5 bindUploadScope: the picker resets so the same file can be re-picked", () => {
      resetScopes();
      state.selectedFlowId = "f1";
      const f = installFetch(() => okUpload("tianluo/uploads/aaaaaaaaaaaa_a.txt"));
      const picker = $("flow-file-input");
      const button = $("flow-attach-btn");
      let clicked = 0;
      picker.click = () => { clicked += 1; };
      picker._listeners = {};
      button._listeners = {};
      $("flow-reply-input")._listeners = {};
      try {
        app.bindUploadScope("flow");
        button.dispatch("click");
        assert.equal(clicked, 1, "the button opens the hidden native picker");

        picker.files = [fakeFile("a.txt")];
        picker.value = "C:\\fakepath\\a.txt";
        picker.dispatch("change");
        assert.equal(f.calls.length, 1);
        // `change` only fires on an actual value change, so a stale selection
        // would make picking the same file twice silently do nothing.
        assert.equal(picker.value, "");
      } finally {
        f.restore();
      }
    });

    check("G5 bindUploadScope: both scopes bind all four gestures", () => {
      for (const scope of ["newTask", "flow"]) {
        const cfg = app.UPLOAD_SCOPES[scope];
        const input = $(cfg.inputId);
        input._listeners = {};
        $(cfg.fileInputId)._listeners = {};
        $(cfg.attachBtnId)._listeners = {};
        app.bindUploadScope(scope);
        for (const evt of ["paste", "dragover", "dragleave", "drop"]) {
          assert.equal(
            Array.isArray(input._listeners[evt]) && input._listeners[evt].length === 1,
            true,
            `${scope} binds ${evt}`,
          );
        }
        assert.equal($(cfg.fileInputId)._listeners.change.length, 1);
        assert.equal($(cfg.attachBtnId)._listeners.click.length, 1);
      }
      // Respond and interject share #flow-reply-input, so the two docked modes
      // are covered by this single binding — a third set would be dead DOM.
      assert.equal(app.UPLOAD_SCOPES.flow.inputId, "flow-reply-input");
    });

    await checkAsync("G6 a real paste gesture uploads once and lands at the caret", async () => {
      // The one case driven through the bound listener rather than by calling
      // the handler: a stray second binding, or a handler wired to the wrong
      // element, is only visible from this side of addEventListener.
      resetScopes();
      state.selectedFlowId = "f1";
      // A file name no earlier case used: the sync checks above fire uploads
      // they never await, and after resetScopes rewinds the sequence counter an
      // identical name would mint an identical token for a still-in-flight
      // answer to land on.
      const f = installFetch(() => okUpload("tianluo/uploads/999999999999_gesture.png"));
      const input = $("flow-reply-input");
      input._listeners = {};
      $("flow-file-input")._listeners = {};
      $("flow-attach-btn")._listeners = {};
      try {
        app.bindUploadScope("flow");
        input.value = "look at  please";
        input.setSelectionRange(8, 8);
        let prevented = false;
        input.dispatch("paste", {
          clipboardData: { files: [fakeFile("gesture.png", 9, "image/png")], items: [] },
          preventDefault: () => { prevented = true; },
        });
        assert.equal(prevented, true, "the image must not also be pasted as text");
        assert.equal(f.calls.length, 1, "one gesture, one POST");
        assert.equal(input.value.includes("gesture.png"), true, "the marker is parked at the caret");
        // Let the in-flight upload settle (the suite's setTimeout is a no-op,
        // so the real one is used to yield the macrotask queue).
        await new Promise((resolve) => savedSetTimeout(resolve, 0));
        assert.equal(input.value, "look at tianluo/uploads/999999999999_gesture.png please");
        assert.equal(f.calls.length, 1, "and no retry behind the scenes");
      } finally {
        f.restore();
      }
    });

    check("G5 replaceInInputOnce: the caret follows the text it was anchored to", () => {
      const el = { value: "aa[tok]bb", selectionStart: 9, selectionEnd: 9 };
      assert.equal(app.replaceInInputOnce(el, "[tok]", "path"), true);
      assert.equal(el.value, "aapathbb");
      assert.equal(el.selectionStart, 8, "a caret past the span shifts by the delta");

      const before = { value: "aa[tok]bb", selectionStart: 1, selectionEnd: 1 };
      app.replaceInInputOnce(before, "[tok]", "path");
      assert.equal(before.selectionStart, 1, "a caret before the span stays put");

      const gone = { value: "nothing here", selectionStart: 3, selectionEnd: 3 };
      assert.equal(app.replaceInInputOnce(gone, "[tok]", "path"), false);
      assert.equal(gone.value, "nothing here");
    });

    resetScopes();
    resetObjectUrls();
  } finally {
    globalThis.setTimeout = savedSetTimeout;
  }
}
