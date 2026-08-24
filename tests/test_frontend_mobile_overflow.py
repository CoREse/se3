"""Real-Chromium geometry guard for the mobile running-console overflow fix.

The static-source guards in ``tests/test_frontend_mobile.py`` lock that the
hardening *rules exist inside the 600px breakpoint*; only a real browser can
answer the question the user actually reported — "流程界面可以左右滑动" — because
it depends on layout facts no source scan can compute:

  * ``.flow-conversation`` declares ``overflow-y: auto``, which per CSS forces
    its computed ``overflow-x`` to ``auto`` as well. The conversation is
    therefore its OWN scroll container, which is why the mobile
    ``html, body { overflow-x: hidden }`` backstop never saw the overflow
    (``.flow-view`` is ``position: fixed; inset: 0``, so the document never
    grew) and the console could be swiped sideways.
  * whether a construct overflows at all is a min-content-width question:
    ``overflow-wrap: anywhere`` reduces the min-content contribution and fixes
    it, while ``overflow-wrap: break-word`` does not.

The page is built from the production ``static/index.html`` over ``file://`` and
driven through the production ``renderConversation`` — no daemon, no server, no
network — so this stays a fast, self-contained frontend test. It SKIPS (never
fails) when Playwright or the Chromium system libraries are unavailable, since
the static guards plus the Node harness already cover the same fix on hosts
without a browser.

The sweep deliberately does not enumerate report constructs by hand: it renders
real archived-shape step outputs, then plants an unbreakable 120-character token
into *every* rendered text node. That exercises every ``.step-report__*``
construct, markdown node, chip label and anonymous inline box the renderer can
produce, including ones today's data happens not to blow out.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = REPO_ROOT / "src" / "tianluo" / "server" / "static"
INDEX_HTML = STATIC_DIR / "index.html"

# A real no-space run taken from this repo's own archived analyze outputs
# (``outputs.scope``), padded to the width a phone column cannot absorb. At the
# 12px status-bar font anything past ~48 characters exceeds the 346px line box
# of a 390px phone, so this is comfortably over the edge.
LONG_TOKEN = (
    "loadFlowConversation()/__convState/flowConversationProgress"
    "/flowConversationRecords/extraSegmentsToPushPastTheColumn"
)

# The identifiers behind the two constructs that override the inherited
# wrapping. Both shapes are real: a configured runner name in `tianluo.yaml` is
# free-form (worktree/role suffixes are common) and model ids carry bracketed
# context-window suffixes, while codex synthesizes MCP tool names as
# `mcp__<server>__<tool>`.
LONG_AGENT_NAME = "oclaude-worktree-implementer-with-a-very-long-configured-name"
LONG_MODEL_NAME = "claude-opus-5-20260514-extended-thinking[1m]"
LONG_TOOL_NAME = "mcp__tianluo_flow_control_plane__inspect_running_flow_conversation"

# Step outputs shaped like the real archived ones, chosen to reach the widest
# spread of report renderers (status bar, sections, lists, kv rows + nested kv,
# markdown, file groups, warnings, errors) rather than to be exhaustive by
# themselves — the text-node stress pass below is what makes the sweep complete.
STEP_OUTPUTS: dict[str, dict] = {
    "analyze": {
        "task_type": "bugfix",
        "complexity": "medium",
        # The row the user reported as overflowing the right edge.
        "scope": (
            "Frontend only: src/tianluo/server/static (app.js) — the running-flow "
            f"conversation surface. Touches {LONG_TOKEN} (silent non-incremental "
            "/api/history rebuild) and isNearBottom() scroll preservation."
        ),
        "reasoning": f"The renderer path {LONG_TOKEN} rebuilds the whole list.",
        "selected_items": [f"charter:{LONG_TOKEN}", "charter:Coding Conventions"],
    },
    "implement": {
        "files_changed": [
            f"src/tianluo/server/static/{LONG_TOKEN}.js",
            "src/tianluo/server/static/style.css",
        ],
        "tests_added": [f"tests/frontend/{LONG_TOKEN}.test.mjs"],
        "summary": f"Hardened {LONG_TOKEN} against horizontal overflow.",
    },
    "test": {
        "overall_status": "FAILED",
        "failed_tests": [f"tests/test_x.py::{LONG_TOKEN}"],
        "stdout": f"E   AssertionError: {LONG_TOKEN}\n",
        "warning": f"⚠ {LONG_TOKEN}",
    },
    "self_check": {
        "findings": [
            {"severity": "high", "description": LONG_TOKEN, "location": LONG_TOKEN},
        ],
    },
    "summarize": {
        # `summary` is the key renderSummarizeReport wraps in
        # `.step-report__markdown` (headings / paragraphs / lists / code fences).
        "summary": (
            f"# {LONG_TOKEN}\n\nParagraph referencing `{LONG_TOKEN}`.\n\n"
            f"- {LONG_TOKEN}\n\n```\n{LONG_TOKEN}\n```\n"
        ),
    },
    "discovery": {
        "refined_description": LONG_TOKEN,
        "mode": "interactive",
        # Drives `.step-report__conv-turn` (whose turn text is a class-less text
        # node — it can only be reached by inherited wrapping).
        "conversation_history": [
            {"role": "user", "content": LONG_TOKEN},
            {"role": "assistant", "content": f"Understood: {LONG_TOKEN}"},
        ],
    },
    # Unknown step type → the generic kv renderer, including a nested dict
    # (`.step-report__kv-nested` / `__kv-k` / `__kv-v`).
    "generic_probe": {
        LONG_TOKEN: LONG_TOKEN,
        "nested": {LONG_TOKEN: {"deeper": LONG_TOKEN}},
        "empty_list": [],
    },
}

# Built once per browser page: reveal the target view, render the records, and
# (in stress mode) plant the unbreakable token into every rendered text node.
BUILD_JS = r"""
(args) => {
  const {outputs, view, token, stress, agentName, modelName, toolName} = args;
  document.querySelectorAll('.view').forEach((n) => n.classList.add('hidden'));
  let container;
  if (view === 'flow') {
    document.getElementById('flow-view').classList.remove('hidden');
    container = document.getElementById('flow-conversation');
  } else {
    const hv = document.getElementById('history-view');
    hv.classList.remove('hidden');
    hv.classList.add('active-detail');
    container = document.getElementById('history-detail');
  }
  container.innerHTML = '';
  const records = [];
  let i = 0;
  for (const [type, out] of Object.entries(outputs)) {
    i += 1;
    const sid = String(i).padStart(2, '0') + '_' + type + '_xx';
    records.push({step_id: sid, step_type: type, message: {
      role: 'assistant', timestamp: 1000 + i,
      content: 'Touching ' + token + ' in src/tianluo/server/static/app.js.'}});
    records.push({step_id: sid, step_type: type, message: {
      type: 'step_completed', timestamp: 1000 + i,
      data: {step_type: type, status: 'completed', outputs: out}}});
  }
  // An assistant turn carrying the two constructs that opt OUT of the inherited
  // wrapping and therefore need their own mobile release: the agent/model badge
  // (`white-space: nowrap` on desktop) and a structured tool chip whose name is
  // a `mcp__<server>__<tool>` token (`flex-shrink: 0` on desktop, plus uppercase
  // + letter-spacing). The chip is delivered through `raw_json` so it takes the
  // rich `extractAssistantChipEvents` path, which — unlike the legacy bracket
  // parser — renders ANY tool name, not just whitelisted ones.
  records.push({step_id: '90_implement_xx', step_type: 'implement', message: {
    role: 'assistant', timestamp: 2000,
    agent_name: agentName, model_name: modelName,
    content: 'Calling [Tool: ' + toolName + '] on the workspace.',
    raw_json: [
      {type: 'text', text: 'Calling [Tool: ' + toolName + '] on the workspace.'},
      {type: 'tool_use', id: 'tu_long', name: toolName,
       input: {[token]: token, path: 'src/tianluo/server/static/' + token}},
      {type: 'tool_result', tool_use_id: 'tu_long',
       content: [{type: 'text', text: token}]},
    ],
  }});
  renderConversation(container, records, false);
  // The chip's detail panel is folded by default, so its `.tool-marker-input-key`
  // column would never be measured; expand every panel so the sweep sees it.
  container.querySelectorAll('.tool-marker-details.folded')
    .forEach((n) => n.classList.remove('folded'));
  const seen = new Set();
  const ew = document.createTreeWalker(container, NodeFilter.SHOW_ELEMENT);
  let e;
  while ((e = ew.nextNode())) for (const c of e.classList) seen.add(c);
  if (stress) {
    const tw = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
    const nodes = [];
    let t;
    while ((t = tw.nextNode())) if (t.nodeValue && t.nodeValue.trim()) nodes.push(t);
    for (const nd of nodes) nd.nodeValue = nd.nodeValue + ' ' + token;
  }
  return {records: records.length, classes: [...seen].sort()};
}
"""

# Measure the conversation container plus every descendant box AND inline text
# rect, discounting anything an overflow!=visible ancestor legitimately clips
# (`.step-report__diff`'s internal scroller, `.tool-marker-detail`'s ellipsis).
MEASURE_JS = r"""
(sel) => {
  const root = document.querySelector(sel);
  const rootLimit = root.getBoundingClientRect().left + root.clientWidth;
  // Constructs that own a CAPTIVE viewport by design: a diff / argument dump
  // scrolls inside its own box, and the mobile tool-chip summary truncates to
  // one line with an ellipsis (the full text is one tap away in the details
  // panel). Their content is contained by construction and cannot widen the
  // column, so anything inside them is out of scope for this sweep — measuring
  // it would just re-report the design as a defect.
  const CAPTIVE = '.step-report__diff, .tool-marker-detail, .tool-marker-input-pre';
  // Returns the x limit `node` must respect, or null when a captive ancestor
  // legitimately contains it.
  function clipLimit(node) {
    let lim = rootLimit;
    let p = node.parentElement;
    while (p && p !== root.parentElement) {
      const cs = getComputedStyle(p);
      if (cs.overflowX !== 'visible') {
        if (p.matches(CAPTIVE)) return null;
        lim = Math.min(lim, p.getBoundingClientRect().left + p.clientWidth);
      }
      p = p.parentElement;
    }
    return lim;
  }
  const label = (n) => n.tagName + (typeof n.className === 'string' && n.className.trim()
    ? '.' + n.className.trim().split(/\s+/).join('.') : '');
  const hits = [];
  const walker = document.createTreeWalker(
    root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
  let n;
  while ((n = walker.nextNode())) {
    if (n.nodeType === 1) {
      const cs = getComputedStyle(n);
      if (cs.display === 'none' || cs.visibility === 'hidden') continue;
      const r = n.getBoundingClientRect();
      if (!r.width && !r.height) continue;
      const lim = clipLimit(n);
      if (lim !== null && r.right > lim + 0.5) {
        hits.push({what: label(n), over: Math.round(r.right - lim), kind: 'box'});
      }
    } else {
      if (!n.nodeValue || !n.nodeValue.trim()) continue;
      const rng = document.createRange();
      rng.selectNodeContents(n);
      const lim = clipLimit(n);
      if (lim === null) continue;
      for (const r of rng.getClientRects()) {
        if (!r.width) continue;
        if (r.right > lim + 0.5) {
          hits.push({what: label(n.parentElement) + ' (text)',
                     over: Math.round(r.right - lim), kind: 'text'});
          break;
        }
      }
    }
  }
  const byWhat = new Map();
  for (const h of hits) {
    const prev = byWhat.get(h.what);
    if (!prev || h.over > prev.over) byWhat.set(h.what, h);
  }
  const before = root.scrollLeft;
  root.scrollLeft = 99999;
  const maxScrollLeft = root.scrollLeft;
  root.scrollLeft = before;
  return {
    clientWidth: root.clientWidth,
    scrollWidth: root.scrollWidth,
    overflowX: getComputedStyle(root).overflowX,
    maxScrollLeft,
    docMaxScroll: document.documentElement.scrollWidth
      - document.documentElement.clientWidth,
    hits: [...byWhat.values()].sort((a, b) => b.over - a.over).slice(0, 40),
  };
}
"""


def _browser_page(width: int):
    """Yield a Playwright page at ``width`` with the console loaded, or skip."""
    playwright = pytest.importorskip(
        "playwright.sync_api",
        reason="playwright is not installed (pip install 'tianluo[browser]')",
    )
    ctx = playwright.sync_playwright().start()
    try:
        browser = ctx.chromium.launch()
    except Exception as exc:  # noqa: BLE001 - any launch failure means "no browser here"
        ctx.stop()
        pytest.skip(
            "headless Chromium could not be launched "
            f"(run scripts/install_browser_test_libs.sh): {exc}"
        )
    page = browser.new_page(viewport={"width": width, "height": 844})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(INDEX_HTML.as_uri(), wait_until="domcontentloaded")
    page.wait_for_function("typeof window.renderConversation === 'function'",
                           timeout=15000)
    return ctx, browser, page, errors


def _sweep(view: str, width: int, stress: bool) -> dict:
    ctx, browser, page, errors = _browser_page(width)
    try:
        built = page.evaluate(BUILD_JS, {
            "outputs": STEP_OUTPUTS, "view": view,
            "token": LONG_TOKEN, "stress": stress,
            "agentName": LONG_AGENT_NAME, "modelName": LONG_MODEL_NAME,
            "toolName": LONG_TOOL_NAME,
        })
        assert not errors, f"app.js raised in-page: {errors[:3]}"
        assert built["records"] > 0
        sel = "#flow-conversation" if view == "flow" else "#history-detail"
        result = page.evaluate(MEASURE_JS, sel)
        result["classes"] = built["classes"]
        return result
    finally:
        browser.close()
        ctx.stop()


def _fmt(hits: list[dict]) -> str:
    return "\n".join(f"  +{h['over']}px {h['kind']:<4} {h['what']}" for h in hits)


@pytest.mark.parametrize("width", [390, 320])
def test_flow_console_has_no_horizontal_scroll_on_mobile(width):
    """The reported defect: the running console must not be swipeable sideways.

    ``scrollWidth == clientWidth`` and ``maxScrollLeft == 0`` on
    ``#flow-conversation`` is the exact geometric statement of "the user cannot
    slide the flow view left/right".
    """
    r = _sweep("flow", width, stress=True)
    assert r["scrollWidth"] == r["clientWidth"], (
        f"#flow-conversation is {r['scrollWidth']}px wide inside a "
        f"{r['clientWidth']}px column at {width}px — the console scrolls "
        f"horizontally. Overflowing constructs:\n{_fmt(r['hits'])}"
    )
    assert r["maxScrollLeft"] == 0, (
        f"the conversation can be swiped {r['maxScrollLeft']}px sideways at {width}px"
    )
    # The document itself never scrolls either (the global backstop stays intact).
    assert r["docMaxScroll"] <= 0


@pytest.mark.parametrize("view", ["flow", "history"])
def test_no_conversation_construct_overflows_the_column(view):
    """Comprehensive sweep: no rendered construct paints past the column edge.

    Every text node carries an unbreakable 120-char token, so this covers every
    ``.step-report__*`` construct, markdown node, chip label and anonymous inline
    box the renderer can emit — including the History view, which shares the
    renderers and was previously *clipping* the same content rather than
    wrapping it.
    """
    r = _sweep(view, 390, stress=True)
    # Sanity: the sweep actually rendered report cards, otherwise it is vacuous.
    assert "step-report" in r["classes"], "no report card was rendered"
    assert any(c.startswith("step-report__") for c in r["classes"])
    assert not r["hits"], (
        f"{len(r['hits'])} construct(s) overflow the {view} conversation column "
        f"at 390px:\n{_fmt(r['hits'])}"
    )


def test_analyze_scope_row_wraps_inside_the_column():
    """The user-visible symptom: the ANALYZE card's 『范围』 row must wrap.

    Rendered from real (non-stressed) analyze outputs, the scope stat must fit
    the status-bar line box and wrap onto several lines instead of extending
    past the right edge.
    """
    ctx, browser, page, errors = _browser_page(390)
    try:
        page.evaluate(BUILD_JS, {
            "outputs": {"analyze": STEP_OUTPUTS["analyze"]}, "view": "flow",
            "token": LONG_TOKEN, "stress": False,
            "agentName": LONG_AGENT_NAME, "modelName": LONG_MODEL_NAME,
            "toolName": LONG_TOOL_NAME,
        })
        assert not errors, f"app.js raised in-page: {errors[:3]}"
        stat = page.evaluate(
            """() => {
              const spans = [...document.querySelectorAll('.step-report__stat')];
              const scope = spans.find((s) => s.textContent.includes('scope'));
              if (!scope) return null;
              const bar = scope.closest('.step-report__status-bar');
              const cont = document.getElementById('flow-conversation');
              const r = scope.getBoundingClientRect();
              const cs = getComputedStyle(scope);
              const line = parseFloat(getComputedStyle(bar).fontSize) * 1.4;
              return {
                width: r.width, right: r.right, height: r.height, lineApprox: line,
                overflowWrap: cs.overflowWrap, wordBreak: cs.wordBreak,
                barClient: bar.clientWidth, barScroll: bar.scrollWidth,
                contRight: cont.getBoundingClientRect().left + cont.clientWidth,
              };
            }"""
        )
        assert stat is not None, "the analyze card rendered no scope stat"
        assert stat["overflowWrap"] == "anywhere", (
            "the scope stat must inherit overflow-wrap: anywhere — break-word "
            "alone does not reduce its min-content width, so the flex item stays "
            "wider than the column"
        )
        assert stat["right"] <= stat["contRight"] + 0.5, (
            f"the scope row's right edge ({stat['right']}) is past the "
            f"conversation's ({stat['contRight']})"
        )
        assert stat["barScroll"] <= stat["barClient"] + 0.5, (
            "the status bar still overflows its own line box"
        )
        # Wrapped, not truncated: the long scope string occupies several lines.
        assert stat["height"] > stat["lineApprox"] * 2, (
            f"the scope text did not wrap (height {stat['height']}px is about one "
            f"line) — it must break onto multiple lines, not be clipped"
        )
    finally:
        browser.close()
        ctx.stop()


@pytest.mark.parametrize("view", ["flow", "history"])
@pytest.mark.parametrize("width", [390, 320])
def test_agent_badge_and_tool_name_wrap_instead_of_being_clipped(view, width):
    """The two constructs that override the inherited wrapping must wrap too.

    ``.agent-badge`` pins ``white-space: nowrap`` and ``.tool-marker-name`` /
    ``.tool-marker-input-key`` are ``flex-shrink: 0`` at the top level, so the
    conversation-wide ``overflow-wrap: anywhere`` cannot reach them: a long
    configured runner/model id or a structured ``mcp__<server>__<tool>`` name
    stays one unbreakable box wider than the column. Containment alone is not an
    acceptable answer here — it would merely hide the excess at the Flow view's
    right edge and clip it at the History pane boundary — so assert the boxes
    actually fit AND that they wrap onto more than one line rather than being
    truncated.
    """
    ctx, browser, page, errors = _browser_page(width)
    try:
        page.evaluate(BUILD_JS, {
            "outputs": {"implement": STEP_OUTPUTS["implement"]}, "view": view,
            "token": LONG_TOKEN, "stress": False,
            "agentName": LONG_AGENT_NAME, "modelName": LONG_MODEL_NAME,
            "toolName": LONG_TOOL_NAME,
        })
        assert not errors, f"app.js raised in-page: {errors[:3]}"
        sel = "#flow-conversation" if view == "flow" else "#history-detail"
        probe = page.evaluate(
            """(sel) => {
              const root = document.querySelector(sel);
              const limit = root.getBoundingClientRect().left + root.clientWidth;
              const pick = (cls) => {
                const n = root.querySelector('.' + cls);
                if (!n) return null;
                const r = n.getBoundingClientRect();
                const cs = getComputedStyle(n);
                return {
                  over: Math.round(r.right - limit), width: Math.round(r.width),
                  height: Math.round(r.height),
                  lineHeight: Math.round(parseFloat(cs.fontSize) * 1.2),
                  text: (n.textContent || '').trim(),
                };
              };
              return {
                badge: pick('agent-badge'),
                toolName: pick('tool-marker-name'),
                inputKey: pick('tool-marker-input-key'),
                scrollWidth: root.scrollWidth, clientWidth: root.clientWidth,
              };
            }""",
            sel,
        )
        for name in ("badge", "toolName", "inputKey"):
            got = probe[name]
            assert got is not None, (
                f"the fixture rendered no .{name} construct — this guard would "
                f"pass vacuously"
            )
            assert got["over"] <= 0, (
                f"{name} paints {got['over']}px past the {view} column at "
                f"{width}px: {got['text'][:80]!r}"
            )
            # Wrapped, not clipped: the identifier is far wider than the column,
            # so it must occupy more than one line.
            assert got["height"] > got["lineHeight"] * 1.5, (
                f"{name} is {got['height']}px tall (~one {got['lineHeight']}px "
                f"line) — the long identifier was truncated, not wrapped"
            )
        assert probe["scrollWidth"] == probe["clientWidth"], (
            f"the {view} conversation still has {probe['scrollWidth']}px of "
            f"content in a {probe['clientWidth']}px column"
        )
    finally:
        browser.close()
        ctx.stop()


# `renderGenericKvRow` recurses over `step.outputs` with no depth limit, so the
# nesting ladder is driven entirely by the data. 14 levels is past the ~12 at
# which the unbounded desktop indent exhausted a 320px column; 30 proves the cap
# is a constant rather than merely a bigger constant.
def _nested_outputs(levels: int) -> dict:
    value: dict = {"leaf": LONG_TOKEN}
    for i in range(levels, 0, -1):
        value = {f"level_{i}": value}
    return {"deep": value, "flat": LONG_TOKEN}


NESTED_PROBE_JS = r"""
(sel) => {
  const root = document.querySelector(sel);
  const limit = root.getBoundingClientRect().left + root.clientWidth;
  const wrappers = [...root.querySelectorAll('.step-report__kv-nested')];
  const rows = wrappers.map((n) => {
    let depth = 0;
    for (let p = n.parentElement; p; p = p.parentElement) {
      if (p.classList.contains('step-report__kv-nested')) depth += 1;
    }
    const r = n.getBoundingClientRect();
    return {depth: depth + 1, left: r.left, width: r.width};
  }).sort((a, b) => a.depth - b.depth);
  return {
    rows,
    count: wrappers.length,
    limit,
    clientWidth: root.clientWidth,
    scrollWidth: root.scrollWidth,
  };
}
"""


@pytest.mark.parametrize("view", ["flow", "history"])
@pytest.mark.parametrize("levels", [14, 30])
def test_deeply_nested_generic_outputs_stay_inside_the_column(view, levels):
    """Deep generic outputs must not indent themselves out of the phone column.

    `.step-report__kv-nested` is a `flex-basis: 100%` flex item, so its indent is
    SUBTRACTIVE: on the desktop ladder every level costs the usable column 25px.
    With an unbounded recursion that ladder eats a 320px column outright — the
    innermost row is left with no width, its key/value paint outside the card,
    and the overflow backstop clips them rather than wrapping them. Wrapping
    cannot rescue a column that has no width left, so the fix has to bound the
    ladder itself; this asserts the bound holds at any depth.
    """
    width = 320
    ctx, browser, page, errors = _browser_page(width)
    try:
        page.evaluate(BUILD_JS, {
            "outputs": {"generic_probe": _nested_outputs(levels)}, "view": view,
            "token": LONG_TOKEN, "stress": False,
            "agentName": LONG_AGENT_NAME, "modelName": LONG_MODEL_NAME,
            "toolName": LONG_TOOL_NAME,
        })
        assert not errors, f"app.js raised in-page: {errors[:3]}"
        sel = "#flow-conversation" if view == "flow" else "#history-detail"
        probe = page.evaluate(NESTED_PROBE_JS, sel)
        assert probe["count"] == levels + 1, (
            f"the fixture rendered {probe['count']} nested wrappers for {levels} "
            f"levels — this guard would not be measuring the ladder"
        )

        # The ladder saturates: total indentation is a constant, not a function
        # of depth. Compare against the SAME bound at both depths.
        rows = probe["rows"]
        indent = rows[-1]["left"] - rows[0]["left"]
        assert indent <= 80, (
            f"nested indentation reached {indent:.0f}px at depth {levels} on a "
            f"{width}px screen — the ladder must stop accruing, not merely "
            f"start narrower"
        )
        # And the innermost level keeps a genuinely usable value column.
        assert rows[-1]["width"] >= 0.6 * rows[0]["width"], (
            f"the innermost level is {rows[-1]['width']:.0f}px wide against "
            f"{rows[0]['width']:.0f}px at the top — deep outputs are being "
            f"squeezed out of the column"
        )
        for row in rows:
            assert row["left"] + row["width"] <= probe["limit"] + 0.5, (
                f"the level-{row['depth']} nested block paints past the column "
                f"edge"
            )

        # Nothing paints outside the column, and the console still cannot be
        # swiped sideways.
        measured = page.evaluate(MEASURE_JS, sel)
        assert not measured["hits"], (
            f"{len(measured['hits'])} construct(s) overflow the {view} column at "
            f"{width}px with {levels} nested levels:\n{_fmt(measured['hits'])}"
        )
        assert measured["scrollWidth"] == measured["clientWidth"]
        assert measured["maxScrollLeft"] == 0
    finally:
        browser.close()
        ctx.stop()


def test_internal_scroll_and_truncation_designs_are_preserved():
    """The hardening must not flatten constructs that scroll/clip on purpose.

    ``.step-report__diff`` owns a captive ``overflow: auto`` viewport (capped at
    320px tall) and the mobile ``.tool-marker-detail`` truncates to one line with
    an ellipsis. Neither responds to ``overflow-wrap``, and neither may be
    replaced by page-level wrapping.
    """
    ctx, browser, page, errors = _browser_page(390)
    try:
        probe = page.evaluate(
            """() => {
              document.getElementById('flow-view').classList.remove('hidden');
              const c = document.getElementById('flow-conversation');
              c.innerHTML = '';
              const mk = (cls) => {
                const d = document.createElement('div');
                d.className = cls; c.appendChild(d); return getComputedStyle(d);
              };
              const diff = mk('step-report__diff');
              const detail = mk('tool-marker-detail');
              return {
                diffOverflow: diff.overflow, diffMaxHeight: diff.maxHeight,
                detailWhiteSpace: detail.whiteSpace,
                detailTextOverflow: detail.textOverflow,
                convOverflowX: getComputedStyle(c).overflowX,
              };
            }"""
        )
        assert not errors, f"app.js raised in-page: {errors[:3]}"
        assert probe["diffOverflow"] == "auto", (
            "the charter diff block must keep its own internal scroller"
        )
        assert probe["diffMaxHeight"] == "320px"
        assert probe["detailWhiteSpace"] == "nowrap", (
            "the tool-call chip detail must keep its single-line truncation"
        )
        assert probe["detailTextOverflow"] == "ellipsis"
        assert probe["convOverflowX"] == "hidden", (
            "the conversation scroll container must pin overflow-x: hidden on mobile"
        )
    finally:
        browser.close()
        ctx.stop()


def test_desktop_conversation_is_untouched_by_the_mobile_overlay():
    """Hard constraint: none of the hardening may reach a desktop viewport.

    At 1280px the breakpoint does not match, so the conversation keeps its
    natural ``overflow-x: auto`` and the report constructs keep the default
    ``overflow-wrap: normal`` — proving the fix is a breakpoint-local overlay.
    """
    ctx, browser, page, errors = _browser_page(1280)
    try:
        page.evaluate(BUILD_JS, {
            "outputs": {"analyze": STEP_OUTPUTS["analyze"]}, "view": "flow",
            "token": LONG_TOKEN, "stress": False,
            "agentName": LONG_AGENT_NAME, "modelName": LONG_MODEL_NAME,
            "toolName": LONG_TOOL_NAME,
        })
        assert not errors, f"app.js raised in-page: {errors[:3]}"
        desktop = page.evaluate(
            """() => {
              const c = document.getElementById('flow-conversation');
              const stat = document.querySelector('.step-report__stat');
              const kvk = document.createElement('span');
              kvk.className = 'step-report__kv-k';
              c.appendChild(kvk);
              const mk = (cls) => {
                const d = document.createElement('span');
                d.className = cls; c.appendChild(d); return getComputedStyle(d);
              };
              return {
                convOverflowX: getComputedStyle(c).overflowX,
                statOverflowWrap: getComputedStyle(stat).overflowWrap,
                kvkMinWidth: getComputedStyle(kvk).minWidth,
                badgeWhiteSpace: mk('agent-badge').whiteSpace,
                toolNameShrink: mk('tool-marker-name').flexShrink,
                inputKeyShrink: mk('tool-marker-input-key').flexShrink,
              };
            }"""
        )
        assert desktop["badgeWhiteSpace"] == "nowrap", (
            "desktop must keep the agent/model badge on one line"
        )
        assert desktop["toolNameShrink"] == "0" and desktop["inputKeyShrink"] == "0", (
            "desktop must keep the tool name / input key columns non-shrinking"
        )
        assert desktop["convOverflowX"] == "auto", (
            "desktop must keep the conversation's natural overflow behaviour"
        )
        assert desktop["statOverflowWrap"] == "normal", (
            "the mobile wrapping must not leak into the desktop cascade"
        )
        assert desktop["kvkMinWidth"] == "100px", (
            "the desktop kv key column must keep its 100px alignment floor"
        )
    finally:
        browser.close()
        ctx.stop()


def test_step_outputs_fixture_reaches_the_report_renderers():
    """Guard the guard: the fixture must exercise the real report constructs.

    If a renderer is renamed or the fixture drifts so that no report card is
    produced, the sweeps above would pass vacuously.
    """
    r = _sweep("flow", 390, stress=False)
    classes = set(r["classes"])
    for construct in (
        "step-report",
        "step-report__status-bar",
        "step-report__stat",
        "step-report__section",
        "step-report__kv-row",
        "step-report__kv-nested",
        "step-report__list",
        "step-report__markdown",
        "step-report__conv-turn",
        "msg-chip",
        "history-step-title",
    ):
        assert construct in classes, (
            f"the fixture no longer renders .{construct}; the mobile overflow "
            f"sweep would silently stop covering it. Rendered: "
            f"{json.dumps(sorted(classes))}"
        )
