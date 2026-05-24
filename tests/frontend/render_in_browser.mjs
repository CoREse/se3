/*
 * render_in_browser.mjs — SINGLE SOURCE of the web-console rendering-paradigm
 * assertions, shared by both ends so the judgement can never drift:
 *
 *   - the node + DOM-stub harness (`render_real_records.mjs`) imports this file
 *     for its side effect and calls `globalThis.__se3Paradigm.paradigmAssertions`;
 *   - the real-Chromium acceptance test
 *     (`tests/test_console_real_daemon_e2e.py::test_render_paradigm_in_headless_browser`)
 *     injects this exact file as a classic `<script>` (Playwright
 *     `add_script_tag`) so the same `paradigmAssertions` runs in-page, against
 *     the production `app.js` `renderConversation` output on real
 *     `GET /api/history/{flow_id}` records.
 *
 * The judgements operate on a rendered conversation `container` plus the raw
 * `records`, using ONLY `childNodes` / `classList` / `textContent` — the small
 * slice of the DOM API that BOTH the minimal `FakeNode` stub and the real
 * browser DOM implement — so one walker serves both ends.
 *
 * Deliberately written WITHOUT `import` / `export` so the identical source
 * loads as:
 *   - an ESM side-effect import in node (`import "./render_in_browser.mjs"`),
 *     which populates `globalThis.__se3Paradigm`; and
 *   - a classic injected browser script, which populates `window.__se3Paradigm`.
 *
 * `paradigmAssertions` never throws, never touches stdout/stdin, and returns a
 * plain (structured-clone-safe, DOM-node-free) result object so a Playwright
 * `page.evaluate` can return it straight to Python.
 */
(function (root) {
  "use strict";

  // Sentinel markers — mirror src/se3/engine/prompt_markers.py and the app.js
  // constants. Kept here so the user-turn split judgement has a single home.
  const TEMPLATE_PREFIX_END = "<!--SE3:TEMPLATE_END-->";
  const USER_CONTENT_BEGIN = "<!--SE3:USER_CONTENT-->";
  const USER_CONTENT_END = "<!--SE3:USER_CONTENT_END-->";

  // Recursive class finder usable against BOTH the node FakeNode stub and the
  // real browser DOM: each implements childNodes / classList, so one walker
  // keeps the node and browser judgements byte-identical. (querySelectorAll is
  // intentionally avoided because the FakeNode stub does not implement it.)
  function findByClass(node, cls, acc) {
    acc = acc || [];
    if (!node || !node.childNodes) return acc;
    for (const c of node.childNodes) {
      if (
        c &&
        c.classList &&
        typeof c.classList.contains === "function" &&
        c.classList.contains(cls)
      ) {
        acc.push(c);
      }
      findByClass(c, cls, acc);
    }
    return acc;
  }

  function textOf(node) {
    return String((node && node.textContent) || "");
  }

  function stripNl(s, both) {
    if (typeof s !== "string") return "";
    return both ? s.replace(/^\n+|\n+$/g, "") : s.replace(/^\n+/, "");
  }

  // Three-segment user-prompt split, mirroring app.js `splitUserPromptByMarker`,
  // so the assertion can compute the prefix / literal / suffix the renderer is
  // expected to have separated. Returns null when the markers are absent.
  function splitMarkers(content) {
    if (typeof content !== "string" || !content) return null;
    const tpe = content.indexOf(TEMPLATE_PREFIX_END);
    if (tpe < 0) return null;
    const ucb = content.indexOf(
      USER_CONTENT_BEGIN,
      tpe + TEMPLATE_PREFIX_END.length,
    );
    if (ucb < 0) return null;
    const uce = content.indexOf(
      USER_CONTENT_END,
      ucb + USER_CONTENT_BEGIN.length,
    );
    const prefix = content.slice(0, tpe);
    if (uce >= 0) {
      return {
        prefix: prefix,
        content: stripNl(
          content.slice(ucb + USER_CONTENT_BEGIN.length, uce),
          true,
        ),
        suffix: stripNl(content.slice(uce + USER_CONTENT_END.length), false),
      };
    }
    // Legacy two-segment (BEGIN without END): no user literal.
    return {
      prefix: prefix,
      content: "",
      suffix: stripNl(content.slice(ucb + USER_CONTENT_BEGIN.length), false),
    };
  }

  // The single source of the rendering-paradigm judgements. Runs identically
  // against the node DOM stub and the real browser DOM. `records` is the raw
  // `GET /api/history/{flow_id}` records array; `container` is the element
  // `app.js` `renderConversation(container, records, false)` rendered into.
  //
  // Returns a flat result object. `ok` is true only when every paradigm check
  // whose preconditions are present in `records` passes; a check whose feature
  // is absent from `records` is vacuously satisfied (so the same function can
  // grade partial record sets without false failures).
  function paradigmAssertions(records, container) {
    const result = {
      ok: false,
      error: null,
      headers: [],
      expected_headers: [],
      discovery_structured: false,
      discovery_proposed_card: false,
      user_literal_only: false,
      raw_nested: false,
      report_card_present: false,
      record_count: Array.isArray(records) ? records.length : 0,
    };
    const fail = (msg) => {
      result.error = msg;
      return result;
    };
    try {
      if (!Array.isArray(records) || records.length === 0) {
        return fail("no records to render");
      }

      // Envelope-shape guard: real daemon records carry the authoritative
      // step_type at the envelope, NEVER inside the inner message. A leaked
      // inner step_type means the fixture faked the shape (the exact bug this
      // acceptance exists to remove), so the judgement would be meaningless.
      for (const r of records) {
        if (
          r &&
          r.message &&
          Object.prototype.hasOwnProperty.call(r.message, "step_type")
        ) {
          return fail(
            "record.message leaked a step_type — not a real daemon envelope shape",
          );
        }
      }

      // 1. Step-section headers read the paradigm names, never the raw
      //    NN_<type>_<hash> file stem.
      const titles = findByClass(container, "history-step-title").map(textOf);
      result.headers = titles;
      const stems = records
        .map((r) => String((r && r.step_id) || ""))
        .filter((s) => /^\d+_/.test(s));
      for (const t of titles) {
        for (const stem of stems) {
          if (t === stem) {
            return fail(
              "step header showed the raw file stem '" +
                stem +
                "' instead of a paradigm name",
            );
          }
        }
      }
      const envTypes = new Set(
        records.map((r) => String((r && r.step_type) || "").toLowerCase()),
      );
      const expect = [];
      if (envTypes.has("discovery")) expect.push("DISCOVERY");
      if (envTypes.has("implement")) expect.push("IMPLEMENT");
      if (envTypes.has("version_analyze")) expect.push("VERSION ANALYZE");
      result.expected_headers = expect;
      for (const want of expect) {
        if (titles.indexOf(want) < 0) {
          return fail(
            "expected a '" +
              want +
              "' step header; got " +
              JSON.stringify(titles),
          );
        }
      }

      const wholeText = textOf(container);

      // 2. The discovery assistant turn renders structured fields, not a raw
      //    ```json``` blob: the refined_description / content value is present
      //    while the JSON key literal is NOT the visible surface, and a
      //    Proposed Task Description card carries the refined_description.
      const discAsst = records.find(
        (r) =>
          r &&
          String(r.step_type).toLowerCase() === "discovery" &&
          r.message &&
          r.message.role === "assistant",
      );
      if (discAsst) {
        let parsed = null;
        try {
          const m = String(discAsst.message.content || "").match(/\{[\s\S]*\}/);
          parsed = m ? JSON.parse(m[0]) : null;
        } catch (_e) {
          parsed = null;
        }
        if (parsed && (parsed.refined_description || parsed.content)) {
          const needle = String(parsed.refined_description || parsed.content);
          if (wholeText.indexOf(needle) < 0) {
            return fail(
              "discovery refined_description/content was not rendered into the bubble",
            );
          }
          if (wholeText.indexOf('"refined_description":') >= 0) {
            return fail(
              "discovery turn dumped raw JSON (found '\"refined_description\":' literal) instead of structured fields",
            );
          }
          result.discovery_structured = true;
          result.discovery_proposed_card =
            findByClass(container, "step-report--proposed-task").length > 0;
          if (parsed.refined_description && !result.discovery_proposed_card) {
            return fail(
              "discovery refined_description did not render as a Proposed Task Description card",
            );
          }
        }
      }

      // 3. Marker-split user turn — Three-Tier Progressive Disclosure: the
      //    default (Layer 1) view shows ONLY the user's literal input; the
      //    framework template prefix / suffix live behind the collapsed
      //    "展开全部" (Layer 2) toggle, never leaking into the default bubble.
      let userMarker = null;
      for (const r of records) {
        if (!r || !r.message || r.message.role !== "user") continue;
        const split = splitMarkers(String(r.message.content || ""));
        if (split && split.content) {
          userMarker = { rec: r, split: split };
          break;
        }
      }
      if (userMarker) {
        const split = userMarker.split;
        const bubbles = findByClass(container, "user-content-bubble");
        if (!bubbles.length) {
          return fail(
            "no default-expanded user-content bubble for the marker-split user turn",
          );
        }
        const bubbleText = bubbles.map(textOf).join("\n");
        if (bubbleText.indexOf(split.content) < 0) {
          return fail("user bubble did not surface the literal input");
        }
        const suffixNeedle = split.suffix && split.suffix.trim();
        if (suffixNeedle && bubbleText.indexOf(suffixNeedle) >= 0) {
          return fail(
            "user bubble leaked the framework suffix into the default Layer-1 view",
          );
        }
        const prefixNeedle = split.prefix && split.prefix.trim();
        if (prefixNeedle && bubbleText.indexOf(prefixNeedle) >= 0) {
          return fail(
            "user bubble leaked the template prefix into the default Layer-1 view",
          );
        }
        if (!findByClass(container, "user-prompt-toggle-wrap").length) {
          return fail(
            "missing the 展开全部 (Layer 2) toggle for the marker-split user turn",
          );
        }
        result.user_literal_only = true;
      }

      // Layer 3 nesting: the "查看原始" raw toggle is NOT a row-level
      // always-visible control — with nothing expanded, no `.raw-toggle`
      // button is present in the default view (it nests inside "展开全部").
      if (findByClass(container, "raw-toggle").length > 0) {
        return fail(
          "a row-level 查看原始 raw toggle is visible by default (Layer 3 must nest inside 展开全部)",
        );
      }
      result.raw_nested = true;

      // 4. Per-step report card: a step_completed / step_failed event renders a
      //    default-expanded report card (makeReportCard → has a toggle and an
      //    un-hidden body). The card coexists with the raw event chip.
      const hasStepEvent = records.some((r) => {
        const t = r && r.message && String(r.message.type || "").toLowerCase();
        return t === "step_completed" || t === "step_failed";
      });
      if (hasStepEvent) {
        const toggles = findByClass(container, "step-report__toggle");
        if (!toggles.length) {
          return fail("a step_completed event produced no per-step report card");
        }
        const bodies = findByClass(container, "step-report__body");
        for (const b of bodies) {
          if (b.classList && b.classList.contains("hidden")) {
            return fail(
              "a step-report card body was collapsed by default (must be default-expanded)",
            );
          }
        }
        result.report_card_present = true;
      }

      result.ok = true;
      return result;
    } catch (e) {
      return fail(
        "assertion crashed: " + (e && e.stack ? e.stack : String(e)),
      );
    }
  }

  const api = { findByClass, paradigmAssertions, splitMarkers };
  if (root) root.__se3Paradigm = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
