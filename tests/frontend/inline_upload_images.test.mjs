/*
 * Inline conversation thumbnails for stored attachments (G5).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub is
 * installed. Exposes `registerInlineUploadImagesTests({app, check, findOne,
 * findAll})` so the parent harness drives the same reporter and the same `app`
 * module export.
 *
 * The rule these cases circle is the mirror image of the upload suite's: THE
 * PATH TEXT IS THE PROMPT, so a thumbnail may only ever be ADDED beside it.
 * Every assertion that a picture appeared is therefore paired with one that the
 * path string is still on screen — an implementation that swapped the text for
 * the image would satisfy half of each case and none of them whole.
 *
 * Coverage:
 *   (a) extractUploadImagePaths — both layout prefixes (the runtime directory
 *       was renamed se3/ → tianluo/, and history replays the old one forever),
 *       the non-image reject, dedup, the prose delimiters that must not be
 *       swallowed into a filename, and the back-to-back multi-paste shape that
 *       must split into one path per file.
 *   (b) uploadFetchUrl — the two target shapes and their encoding, pinned
 *       against the GET endpoint the server actually serves.
 *   (c) resolveInlineImageTarget — live flow, reopened history session, an
 *       unknown flow's fallback, and the nothing-open null.
 *   (d) renderInlineUploadImages — the anchor/img structure, the new-tab
 *       affordance, and the self-hiding load failure that is the whole
 *       degradation story (offline daemon / deleted file / legacy daemon).
 *   (e) the two render seams — buildBubble (assistant + collapsed chip) and
 *       renderUserMarkerRecord (the user's own prompt half).
 *
 * The server leg is covered independently by tests/server/test_upload_fetch_api.py;
 * nothing here is a security control.
 */
import assert from "node:assert/strict";

export function registerInlineUploadImagesTests(ctx) {
  const { app, check, findAll, findOne } = ctx;
  const state = app.state;

  const IMG = "tianluo/uploads/e155b5b05cf8_image.png";
  const IMG2 = "tianluo/uploads/77bb0c1d4a92_shot.jpeg";
  const LEGACY = "se3/uploads/aabbccddeeff_old.png";
  const DOC = "tianluo/uploads/0123456789ab_notes.txt";

  // A live flow reachable through findFlow, which is the richest target: it
  // names machine + root outright, so the server never has to resolve a flow.
  function openLiveFlow() {
    state.machines = [{
      machine_id: "m1",
      hostname: "box",
      flows: [{ flow_id: "f1", project_root: "/srv/proj" }],
    }];
    state.historySessions = [];
    state.selectedFlowId = "f1";
    state.selectedHistoryId = null;
  }

  function closeEverything() {
    state.machines = [];
    state.historySessions = [];
    state.selectedFlowId = null;
    state.selectedHistoryId = null;
  }

  // Normalized record in the real daemon shape (envelope step_type, inner
  // message carrying only chat fields), matching the parent harness's asstNorm.
  const norm = (role, content) => app.normalizeRecord({
    step_id: "implement",
    step_type: "implement",
    message: { role, content, timestamp: 1 },
  });

  // ==========================================================================
  // (a) extractUploadImagePaths
  // ==========================================================================
  check("G5 extractUploadImagePaths: both layout prefixes are recognised", () => {
    assert.deepEqual(
      app.extractUploadImagePaths(`看这个 ${IMG} 和这个 ${LEGACY}`),
      [IMG, LEGACY],
      "a conversation recorded before the se3 → tianluo rename replays forever",
    );
  });

  check("G5 extractUploadImagePaths: a non-image attachment is not a thumbnail", () => {
    assert.deepEqual(app.extractUploadImagePaths(`attached ${DOC}`), []);
    assert.deepEqual(
      app.extractUploadImagePaths(`${DOC} and ${IMG}`),
      [IMG],
      "the image is still found next to a file that is not one",
    );
  });

  check("G5 extractUploadImagePaths: a path named twice is still one file", () => {
    assert.deepEqual(
      app.extractUploadImagePaths(`I read ${IMG} — as requested, ${IMG} shows the bug.`),
      [IMG],
      "the agent quoting the path back must not double the picture",
    );
    assert.deepEqual(app.extractUploadImagePaths(`${IMG}\n${IMG2}`), [IMG, IMG2]);
  });

  check("G5 extractUploadImagePaths: back-to-back paths are two files", () => {
    // The multi-paste shape, verbatim: startUploads calls performUpload per
    // file and each insertAtCaret adds no separator of its own, so two
    // screenshots dropped in one gesture leave their two tokens — and then
    // their two paths — adjacent with nothing between them. A run that ate
    // both would still end in .png, so isImageFile could not catch the
    // mis-parse; only the daemon would, by refusing the concatenation, and
    // NEITHER screenshot would show.
    assert.deepEqual(app.extractUploadImagePaths(`${IMG}${IMG2}`), [IMG, IMG2]);
    assert.deepEqual(
      app.extractUploadImagePaths(`照着 ${IMG}${IMG2}${LEGACY} 改`),
      [IMG, IMG2, LEGACY],
      "three in one paste chain the same way",
    );
    assert.deepEqual(
      app.extractUploadImagePaths(`look ${IMG},${IMG2} ok`),
      [IMG, IMG2],
      "an ASCII comma separates paths as its CJK twin already did",
    );
    assert.deepEqual(
      app.extractUploadImagePaths(`${DOC}${IMG}`),
      [IMG],
      "a non-image first attachment must not swallow the image after it",
    );
  });

  check("G5 extractUploadImagePaths: a comma inside a stored name survives", () => {
    // sanitize_upload_filename folds only /\:*?"<>|\0, control chars and
    // whitespace — a comma reaches disk verbatim, so `v1,2.png` is a name the
    // daemon would serve. Ending the run at the comma would truncate it to a
    // extension-less string and silently drop the thumbnail.
    const COMMA = "tianluo/uploads/a1b2c3d4e5f6_v1,2.png";
    assert.deepEqual(app.extractUploadImagePaths(`see ${COMMA} ok`), [COMMA]);
    assert.deepEqual(
      app.extractUploadImagePaths(`${COMMA},${IMG2}`),
      [COMMA, IMG2],
      "and it still splits from a following path across the same comma",
    );
  });

  check("G5 extractUploadImagePaths: a longer path's tail is not an attachment", () => {
    // The lead-character rule the adjacency exception must not undo: an
    // absolute path merely CONTAINS the same suffix, and its project-relative
    // tail addresses nothing the server would serve.
    assert.deepEqual(app.extractUploadImagePaths(`/srv/proj/${IMG}`), []);
    assert.deepEqual(app.extractUploadImagePaths(`myse3/uploads/aa_x.png`), []);
    assert.deepEqual(
      app.extractUploadImagePaths(`/srv/proj/${IMG} but ${IMG2}`),
      [IMG2],
      "the absolute one is skipped, the relative one still found",
    );
  });

  check("G5 extractUploadImagePaths: wrapping punctuation is not part of the name", () => {
    for (const wrapped of [
      `"${IMG}"`,
      `'${IMG}'`,
      `(${IMG})`,
      `[${IMG}]`,
      `请看 ${IMG}。`,
      `请看 ${IMG}，谢谢`,
      `see ${IMG}.`,
      `see ${IMG}!`,
      `\`${IMG}\``,
    ]) {
      assert.deepEqual(app.extractUploadImagePaths(wrapped), [IMG], wrapped);
    }
  });

  check("G5 extractUploadImagePaths: only paths under an uploads dir count", () => {
    assert.deepEqual(app.extractUploadImagePaths(""), []);
    assert.deepEqual(app.extractUploadImagePaths(null), []);
    assert.deepEqual(app.extractUploadImagePaths("tianluo/state/engine.png"), []);
    assert.deepEqual(app.extractUploadImagePaths("other/uploads/a.png"), []);
  });

  // ==========================================================================
  // (b) uploadFetchUrl
  // ==========================================================================
  check("G5 uploadFetchUrl: flow target carries path + flow_id", () => {
    const url = app.uploadFetchUrl(IMG, { kind: "flow", flowId: "f 1" });
    assert.equal(
      url,
      "/api/uploads/file?path=" + encodeURIComponent(IMG) + "&flow_id=f%201",
    );
  });

  check("G5 uploadFetchUrl: project target carries machine_id + project_root", () => {
    const url = app.uploadFetchUrl(IMG, {
      kind: "project", machineId: "m1", projectRoot: "/srv/my proj",
    });
    assert.equal(url.startsWith("/api/uploads/file?"), true);
    assert.equal(url.includes("path=" + encodeURIComponent(IMG)), true);
    assert.equal(url.includes("machine_id=m1"), true);
    assert.equal(url.includes("project_root=%2Fsrv%2Fmy%20proj"), true);
    assert.equal(url.includes("flow_id"), false);
  });

  // ==========================================================================
  // (c) resolveInlineImageTarget
  // ==========================================================================
  check("G5 resolveInlineImageTarget: a live flow resolves to machine + root", () => {
    openLiveFlow();
    assert.deepEqual(app.resolveInlineImageTarget(), {
      kind: "project", machineId: "m1", projectRoot: "/srv/proj",
    });
  });

  check("G5 resolveInlineImageTarget: a reopened history session resolves too", () => {
    // The case machine+root exists for: the flow ENDED, so the live snapshot no
    // longer carries it and a flow_id would resolve to nothing server-side —
    // yet looking back at an old conversation is exactly when the thumbnails
    // matter most.
    closeEverything();
    state.historySessions = [
      { flow_id: "old", machine_id: "m2", project_root: "/srv/archived" },
    ];
    state.selectedHistoryId = "old";
    assert.deepEqual(app.resolveInlineImageTarget(), {
      kind: "project", machineId: "m2", projectRoot: "/srv/archived",
    });
  });

  check("G5 resolveInlineImageTarget: an unplaceable flow falls back to its id", () => {
    closeEverything();
    state.selectedFlowId = "ghost";
    assert.deepEqual(app.resolveInlineImageTarget(), { kind: "flow", flowId: "ghost" });
  });

  check("G5 resolveInlineImageTarget: nothing open resolves to nothing", () => {
    closeEverything();
    assert.equal(app.resolveInlineImageTarget(), null);
  });

  // ==========================================================================
  // (d) renderInlineUploadImages
  // ==========================================================================
  check("G5 renderInlineUploadImages: nothing to show renders nothing", () => {
    openLiveFlow();
    assert.equal(app.renderInlineUploadImages("no attachments here"), null);
    assert.equal(app.renderInlineUploadImages(`attached ${DOC}`), null);
    closeEverything();
    assert.equal(app.renderInlineUploadImages(`see ${IMG}`), null,
      "with no flow open there is nothing to resolve the path against");
  });

  check("G5 renderInlineUploadImages: one anchor-wrapped img per path", () => {
    openLiveFlow();
    const wrap = app.renderInlineUploadImages(`before ${IMG} between ${IMG2} after`);
    assert.notEqual(wrap, null);
    assert.equal(wrap.classList.contains("inline-uploads"), true);

    const links = findAll(wrap, "inline-upload-link");
    const imgs = findAll(wrap, "inline-upload-img");
    assert.equal(links.length, 2, "two distinct paths, two pictures");
    assert.equal(imgs.length, 2);

    const expected = app.uploadFetchUrl(IMG, app.resolveInlineImageTarget());
    assert.equal(imgs[0].src, expected);
    assert.equal(imgs[0].src.includes("/api/uploads/file?"), true);
    assert.equal(imgs[0].src.includes("path=" + encodeURIComponent(IMG)), true);
    assert.equal(imgs[1].src.includes("path=" + encodeURIComponent(IMG2)), true);

    // The stored basename is the alt text and the full relative path the
    // tooltip — the same pairing the attachment strip uses, so a reader
    // comparing a thumbnail against the prompt text reads the same two strings
    // in both places.
    assert.equal(imgs[0].alt, "e155b5b05cf8_image.png");
    assert.equal(imgs[0].title, IMG);

    // Clicking opens the original: a new tab, because the console holds live
    // websocket state that navigating away would drop.
    assert.equal(links[0].href, expected);
    assert.equal(links[0].target, "_blank");
    assert.equal(links[0].rel, "noopener");
    assert.equal(imgs[0].parentNode, links[0]);
  });

  check("G5 renderInlineUploadImages: a failed load hides itself, never breaks", () => {
    openLiveFlow();
    const wrap = app.renderInlineUploadImages(`${IMG} and ${IMG2}`);
    const links = findAll(wrap, "inline-upload-link");
    const imgs = findAll(wrap, "inline-upload-img");

    // Offline daemon / deleted file / pre-revision-6 daemon all arrive here.
    imgs[0].dispatch("error");
    assert.equal(links[0].classList.contains("hidden"), true,
      "a broken-image glyph would be strictly worse than the path text");
    assert.equal(links[1].classList.contains("hidden"), false,
      "one unreadable file does not take its neighbour down");
    assert.equal(wrap.classList.contains("hidden"), false);

    imgs[1].dispatch("error");
    assert.equal(wrap.classList.contains("hidden"), true,
      "with nothing left to show the container's own margin must go too");
    // A retry firing `error` twice must not push the counter past zero.
    imgs[1].dispatch("error");
    assert.equal(wrap.classList.contains("hidden"), true);
  });

  // ==========================================================================
  // (e) the render seams
  // ==========================================================================
  check("G5 assistant turn: the thumbnail joins the path text, never replaces it", () => {
    openLiveFlow();
    const row = app.renderConversationRecord(
      norm("assistant", `I looked at ${IMG} and the button is misaligned.`));
    const img = findOne(row, "inline-upload-img");
    assert.notEqual(img, null, "an agent naming an attachment shows it");
    assert.equal(img.src.includes(encodeURIComponent(IMG)), true);
    assert.equal(row.textContent.includes(IMG), true,
      "the path is what the agent read — it stays on screen verbatim");
  });

  check("G5 user prompt (marker split): the user's own half is scanned", () => {
    openLiveFlow();
    const content = [
      "## Task Description",
      "<!--SE3:TEMPLATE_END-->",
      "<!--SE3:USER_CONTENT-->",
      `照着 ${IMG} 改一下布局`,
      "<!--SE3:USER_CONTENT_END-->",
      "## Runtime Context",
    ].join("\n");
    const row = app.renderConversationRecord(norm("user", content));
    assert.equal(row.classList.contains("user-prompt-marker"), true,
      "this case only means something on the marker-split path");
    const img = findOne(row, "inline-upload-img");
    assert.notEqual(img, null, "the pasted screenshot is where uploads land");
    assert.equal(img.src.includes(encodeURIComponent(IMG)), true);
    assert.equal(row.textContent.includes(IMG), true);
  });

  check("G5 collapsed chip: expanding builds the thumbnail with the bubble", () => {
    openLiveFlow();
    // No markers → the legacy whole-message chip, collapsed by default. The
    // images are built inside buildBubble, so they must appear on the first
    // expand rather than never (the bubble does not exist until then).
    const row = app.renderConversationRecord(norm("user", `请看 ${IMG}`));
    assert.equal(findOne(row, "inline-upload-img"), null,
      "a collapsed chip pays nothing — not even a request for the image");
    const chip = findOne(row, "msg-chip");
    assert.notEqual(chip, null);
    chip.dispatch("click");
    const img = findOne(row, "inline-upload-img");
    assert.notEqual(img, null, "expanding the chip reveals the picture too");
    assert.equal(img.src.includes(encodeURIComponent(IMG)), true);
    assert.equal(row.textContent.includes(IMG), true);
  });

  check("G5 a message naming no image renders no img at all", () => {
    openLiveFlow();
    const row = app.renderConversationRecord(
      norm("assistant", `I wrote the answer into ${DOC} as asked.`));
    assert.equal(findOne(row, "inline-uploads"), null);
    assert.equal(findOne(row, "inline-upload-img"), null);
    assert.equal(row.textContent.includes(DOC), true);
  });

  check("G5 no flow open: the conversation degrades to plain path text", () => {
    // The whole-feature fallback: a rendered record with nothing to resolve
    // against is exactly the pre-feature view, with no error anywhere.
    closeEverything();
    const row = app.renderConversationRecord(norm("assistant", `see ${IMG}`));
    assert.equal(findOne(row, "inline-upload-img"), null);
    assert.equal(row.textContent.includes(IMG), true);
  });

  closeEverything();
}
