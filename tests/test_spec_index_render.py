"""Tests for the size-bounded deterministic spec index renderer (G3).

Covers: determinism (same input → byte-identical output), threshold compliance
(every feasible view ≤ threshold), mixed view, root-view domain grouping with
the ``(未分类)`` bucket, recursive group drill-down, deterministic ``pN``
pagination fallback, self-describing command help, the item-vs-handle rendering
distinction (item identity invariant), and the hard guarantee that rendering
never invokes the LLM.
"""

from __future__ import annotations

import re
from unittest import mock

import pytest

from se3.engine.spec_index import ItemMeta, SpecIndex, SpecMeta
from se3.engine import spec_index_render as r
from se3.engine.spec_governance import UNCLASSIFIED_GROUP


# ---------------------------------------------------------------------------
# Synthetic index builder (no disk, fully in-memory, deterministic)
# ---------------------------------------------------------------------------

def make_index(tmp_path, specs):
    """Build an in-memory ``SpecIndex`` from a compact description.

    ``specs`` maps spec_name -> dict with optional ``domain`` / ``locator`` and
    a ``reqs`` list of ``(name, summary, tags, refs)`` tuples.
    """
    idx = SpecIndex(tmp_path)
    for sname, info in specs.items():
        reqs = info.get("reqs", [])
        idx.spec_metas[sname] = SpecMeta(
            spec_name=sname,
            domain=info.get("domain"),
            locator=info.get("locator", ""),
            item_count=len(reqs),
        )
        for i, (rname, summary, tags, refs) in enumerate(reqs):
            item = ItemMeta(
                spec_name=sname,
                requirement_name=rname,
                spec_path=str(tmp_path / sname / "spec.md"),
                mtime=0.0,
                size=0,
                sha256_prefix="",
                tags=list(tags),
                keywords=[],
                refs=list(refs),
                summary=summary,
                line_start=i + 1,
                line_end=i + 1,
            )
            idx.items[item.item_id] = item
    return idx


def _byte_len(text: str) -> int:
    return len(text.encode("utf-8"))


_DRILL_RE = re.compile(r"→ se3 spec index (.+)$")


def _spec_with_items(n, summary_len=120):
    summary = "x" * summary_len
    return {"reqs": [(f"Req {i:03d}", summary, ["t"], []) for i in range(n)]}


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_root_view_is_deterministic(tmp_path):
    specs = {
        "alpha": {"domain": "core", "locator": "alpha loc", "reqs": [("A", "sa", [], [])]},
        "beta": {"domain": "engine/x", "locator": "beta loc", "reqs": [("B", "sb", [], [])]},
        "gamma": {"locator": "gamma loc", "reqs": [("C", "sc", [], [])]},
    }
    idx = make_index(tmp_path, specs)
    out1 = r.render_root_view(idx, threshold=400)
    out2 = r.render_root_view(idx, threshold=400)
    assert out1 == out2


def test_spec_view_is_deterministic(tmp_path):
    idx = make_index(tmp_path, {"big": _spec_with_items(40)})
    a = r.render_spec_view(idx, "big", threshold=1200)
    b = r.render_spec_view(idx, "big", threshold=1200)
    assert a == b


def test_pagination_packing_is_deterministic_across_thresholds(tmp_path):
    idx = make_index(tmp_path, {"big": _spec_with_items(60)})
    # Same threshold ⇒ identical pages; the page structure is a pure function
    # of (index, threshold).
    for th in (1000, 1500, 3000):
        assert r.render_spec_view(idx, "big", threshold=th) == \
            r.render_spec_view(idx, "big", threshold=th)


# ---------------------------------------------------------------------------
# Threshold compliance (feasible thresholds)
# ---------------------------------------------------------------------------

def _walk_all_views(idx, spec, threshold):
    """Drill every page handle reachable from a spec view; return max byte size."""
    seen = set()
    max_bytes = 0

    def rec(groups):
        nonlocal max_bytes
        out = r.render_spec_view(idx, spec, groups, threshold=threshold)
        max_bytes = max(max_bytes, _byte_len(out))
        for line in out.splitlines():
            m = _DRILL_RE.search(line)
            if m:
                parts = m.group(1).split()
                g = tuple(parts[1:])  # parts[0] == spec
                if g and g not in seen:
                    seen.add(g)
                    rec(list(g))

    rec([])
    return max_bytes


def test_spec_view_outputs_are_threshold_bounded(tmp_path):
    idx = make_index(tmp_path, {"big": _spec_with_items(80)})
    for th in (1200, 2000, 4000, 16384):
        max_bytes = _walk_all_views(idx, "big", th)
        assert max_bytes <= th, f"view exceeded threshold {th}: {max_bytes}"


def test_under_threshold_renders_everything_inline(tmp_path):
    idx = make_index(tmp_path, {"small": _spec_with_items(3)})
    out = r.render_spec_view(idx, "small", threshold=16384)
    # No pagination handles when it fits (header mentions [page] in its legend,
    # so check that no body line *is* a page handle).
    assert not any(l.startswith("[page]") for l in out.splitlines())
    assert "small::Req 000" in out
    assert "small::Req 002" in out


# ---------------------------------------------------------------------------
# Root view: domain grouping + (未分类)
# ---------------------------------------------------------------------------

def test_root_view_lists_all_specs_with_locator_and_count(tmp_path):
    specs = {
        "alpha": {"locator": "alpha purpose line", "reqs": [("A", "s", [], [])]},
        "beta": {"locator": "beta purpose line",
                 "reqs": [("B", "s", [], []), ("C", "s", [], [])]},
    }
    idx = make_index(tmp_path, specs)
    out = r.render_root_view(idx, threshold=16384)
    assert "- alpha  (1 items)" in out
    assert "- beta  (2 items)" in out
    assert "alpha purpose line" in out
    assert "beta purpose line" in out


def test_root_view_over_threshold_folds_by_domain(tmp_path):
    specs = {
        f"eng{i}": {"domain": "engine", "locator": "L" * 80,
                    "reqs": [("R", "s", [], [])]}
        for i in range(6)
    }
    specs.update({
        f"srv{i}": {"domain": "server", "locator": "L" * 80,
                    "reqs": [("R", "s", [], [])]}
        for i in range(6)
    })
    idx = make_index(tmp_path, specs)
    out = r.render_root_view(idx, threshold=600)
    assert _byte_len(out) <= 600
    # Folded into domain group handles, no '::' in handles.
    assert "[group] engine" in out
    assert "[group] server" in out
    assert "se3 spec index engine" in out


def test_unclassified_group_for_specs_without_domain(tmp_path):
    specs = {
        f"x{i}": {"locator": "L" * 80, "reqs": [("R", "s", [], [])]}
        for i in range(10)
    }
    idx = make_index(tmp_path, specs)
    out = r.render_root_view(idx, threshold=500)
    assert _byte_len(out) <= 500
    assert UNCLASSIFIED_GROUP in out
    assert f"[group] {UNCLASSIFIED_GROUP}" in out


def test_root_mixed_view_some_folded_some_inline(tmp_path):
    # One large domain group (folds first) + a couple of tiny standalone specs.
    specs = {
        f"big{i}": {"domain": "bulk", "locator": "L" * 120,
                    "reqs": [("R", "s", [], [])]}
        for i in range(8)
    }
    specs["solo"] = {"domain": "alone", "locator": "tiny", "reqs": [("R", "s", [], [])]}
    idx = make_index(tmp_path, specs)
    # Threshold chosen so folding the big group suffices, leaving solo inline.
    out = r.render_root_view(idx, threshold=900)
    assert _byte_len(out) <= 900
    assert "[group] bulk" in out          # large group folded
    assert "- solo" in out                # small spec still listed inline


# ---------------------------------------------------------------------------
# Recursive group drill-down (domain levels deeper)
# ---------------------------------------------------------------------------

def test_domain_path_drilldown_descends_levels(tmp_path):
    specs = {
        "fmt": {"domain": "engine/format", "locator": "L" * 80, "reqs": [("R", "s", [], [])]},
        "grd": {"domain": "engine/format", "locator": "L" * 80, "reqs": [("R", "s", [], [])]},
        "llm": {"domain": "engine/llm", "locator": "L" * 80, "reqs": [("R", "s", [], [])]},
    }
    idx = make_index(tmp_path, specs)
    # Root over threshold → single "engine" group handle (folds one level).
    root = r.render_root_view(idx, threshold=600)
    assert "[group] engine" in root
    # Drill "engine" → next domain level reveals the engine/format subgroup.
    eng = r.render_root_view(idx, ["engine"], threshold=600)
    assert "engine/format" in eng
    # Drill "engine/format" with a big threshold → the two specs listed.
    fmt = r.render_root_view(idx, ["engine", "format"], threshold=16384)
    assert "- fmt" in fmt
    assert "- grd" in fmt


def test_render_group_view_dispatches(tmp_path):
    idx = make_index(tmp_path, {
        "s": {"domain": "d", "locator": "loc", "reqs": [("R", "sum", [], [])]},
    })
    root = r.render_group_view(idx, None, [], threshold=16384)
    assert "- s" in root
    spec = r.render_group_view(idx, "s", [], threshold=16384)
    assert "s::R" in spec


# ---------------------------------------------------------------------------
# Pagination fallback (no deeper structure)
# ---------------------------------------------------------------------------

def test_spec_view_paginates_when_over_threshold(tmp_path):
    idx = make_index(tmp_path, {"big": _spec_with_items(40)})
    out = r.render_spec_view(idx, "big", threshold=1200)
    assert "[page] big/p1" in out
    # Page handles are navigation-only: no item address.
    for line in out.splitlines():
        if line.startswith("[page]") or line.startswith("    → se3 spec index"):
            assert "::" not in line


def test_drilling_a_page_yields_items(tmp_path):
    idx = make_index(tmp_path, {"big": _spec_with_items(40)})
    top = r.render_spec_view(idx, "big", threshold=1200)
    # Grab the first page drill command and follow it.
    first = next(_DRILL_RE.search(l).group(1)
                 for l in top.splitlines() if _DRILL_RE.search(l))
    parts = first.split()
    assert parts[0] == "big"
    page = r.render_spec_view(idx, "big", parts[1:], threshold=1200)
    # Eventually the drilled view shows real item addresses (drill once more if
    # the first page is itself a super-page).
    depth = 0
    groups = parts[1:]
    while "big::" not in page and depth < 6:
        nxt = next((_DRILL_RE.search(l).group(1)
                    for l in page.splitlines() if _DRILL_RE.search(l)), None)
        if nxt is None:
            break
        groups = nxt.split()[1:]
        page = r.render_spec_view(idx, "big", groups, threshold=1200)
        depth += 1
    assert "big::Req" in page


def test_paginated_drilled_views_stay_bounded(tmp_path):
    idx = make_index(tmp_path, {"big": _spec_with_items(120)})
    max_bytes = _walk_all_views(idx, "big", 2000)
    assert max_bytes <= 2000


def test_recursive_super_pagination_when_many_pages(tmp_path):
    # Many small items at a modest threshold ⇒ many pages whose handle list
    # itself must be folded into super-pages (recursion to bounded output).
    idx = make_index(tmp_path, {"big": _spec_with_items(200, summary_len=10)})
    out = r.render_spec_view(idx, "big", threshold=1500)
    assert _byte_len(out) <= 1500
    # Top level shows page handles, and they collectively reference fewer than
    # the full item count (i.e. real folding happened).
    assert "[page] big/p1" in out


# ---------------------------------------------------------------------------
# Self-describing output
# ---------------------------------------------------------------------------

def test_output_is_self_describing(tmp_path):
    idx = make_index(tmp_path, {"big": _spec_with_items(40)})
    out = r.render_spec_view(idx, "big", threshold=1200)
    assert "se3 spec show <spec>::<requirement>" in out
    assert "se3 spec index <spec> [<group>...]" in out


def test_root_output_is_self_describing(tmp_path):
    idx = make_index(tmp_path, {"s": {"locator": "loc", "reqs": [("R", "s", [], [])]}})
    out = r.render_root_view(idx, threshold=16384)
    assert "se3 spec show" in out
    assert "se3 spec index" in out


# ---------------------------------------------------------------------------
# Item identity invariant: item entries carry ::, handles never do
# ---------------------------------------------------------------------------

def test_item_entries_carry_address_handles_do_not(tmp_path):
    idx = make_index(tmp_path, {
        "big": {"reqs": [(f"Req {i}", "s" * 60, ["a"], []) for i in range(30)]},
    })
    out = r.render_spec_view(idx, "big", threshold=1100)
    handle_lines = [l for l in out.splitlines()
                    if l.startswith("[page]") or l.startswith("[group]")]
    assert handle_lines  # folding happened
    for l in handle_lines:
        assert "::" not in l
    # When fully expanded, item lines carry the :: address.
    full = r.render_spec_view(idx, "big", threshold=16384)
    item_lines = [l for l in full.splitlines() if l.startswith("- big::")]
    assert len(item_lines) == 30


def test_item_entry_includes_tags_and_refs(tmp_path):
    idx = make_index(tmp_path, {
        "s": {"reqs": [("R", "summary text", ["auth", "sec"], ["other::Thing"])]},
    })
    out = r.render_spec_view(idx, "s", threshold=16384)
    assert "- s::R" in out
    assert "summary text" in out
    assert "tags: auth, sec" in out
    assert "refs: other::Thing" in out


# ---------------------------------------------------------------------------
# No LLM ever invoked during rendering
# ---------------------------------------------------------------------------

def test_rendering_never_invokes_llm(tmp_path):
    idx = make_index(tmp_path, {
        "a": {"domain": "engine", "locator": "L" * 80, "reqs": [("R", "s", [], [])]},
        "big": _spec_with_items(60),
    })
    with mock.patch("se3.engine.llm_caller.LLMCaller.call") as call:
        # Exercise every rendering entry point and several drill levels.
        r.render_index(idx, threshold=600)
        r.render_root_view(idx, threshold=600)
        r.render_root_view(idx, ["engine"], threshold=600)
        r.render_spec_view(idx, "big", threshold=1200)
        r.render_spec_view(idx, "big", ["p1"], threshold=1200)
        r.render_group_view(idx, "big", ["p1"], threshold=1200)
        r.render_index(idx, "big", ["p1"], threshold=1200)
        assert call.call_count == 0


# ---------------------------------------------------------------------------
# Unified entry disambiguation + edge cases
# ---------------------------------------------------------------------------

def test_render_index_routes_known_spec_to_spec_view(tmp_path):
    idx = make_index(tmp_path, {
        "engine": {"domain": "d", "locator": "loc", "reqs": [("R", "sum", [], [])]},
    })
    # First positional is a known spec name ⇒ spec view (items), not domain nav.
    out = r.render_index(idx, "engine", threshold=16384)
    assert "engine::R" in out


def test_render_index_routes_unknown_first_arg_to_domain_nav(tmp_path):
    idx = make_index(tmp_path, {
        "fmt": {"domain": "engine/format", "locator": "L" * 40, "reqs": [("R", "s", [], [])]},
    })
    out = r.render_index(idx, "engine", ["format"], threshold=16384)
    assert "- fmt" in out


def test_unknown_spec_reports_gracefully(tmp_path):
    idx = make_index(tmp_path, {"s": {"reqs": [("R", "s", [], [])]}})
    out = r.render_spec_view(idx, "does-not-exist", threshold=16384)
    assert "no such spec" in out


def test_empty_index_root_view(tmp_path):
    idx = make_index(tmp_path, {})
    out = r.render_root_view(idx, threshold=16384)
    assert "se3 spec index" in out  # header still present
    assert "(no entries)" in out


def test_invalid_threshold_falls_back_to_default(tmp_path):
    idx = make_index(tmp_path, {"s": {"reqs": [("R", "s", [], [])]}})
    a = r.render_root_view(idx, threshold=0)
    b = r.render_root_view(idx, threshold=None)
    c = r.render_root_view(idx, threshold=r.DEFAULT_INDEX_RENDER_THRESHOLD)
    assert a == b == c
