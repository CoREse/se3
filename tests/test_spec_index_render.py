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

def test_spec_view_groups_by_name_range_when_flat(tmp_path):
    # A flat spec (every Requirement under one chapter) is subdivided into
    # name-range semantic groups derived from the ### requirement outline — NOT
    # collapsed straight to anonymous pN pages.
    idx = make_index(tmp_path, {"big": _spec_with_items(40)})
    out = r.render_spec_view(idx, "big", threshold=1200)
    assert _byte_len(out) <= 1200
    # Over threshold ⇒ at least one name-range group folds to a semantic handle
    # carrying its sN drill command.
    assert "[group] big/" in out
    assert "se3 spec index big s" in out
    # Navigation handles are address-free (item-identity invariant).
    for line in out.splitlines():
        if line.startswith("[group]") or line.startswith("    → se3 spec index"):
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


# ---------------------------------------------------------------------------
# Domain ↔ spec name collision (issue: a domain literally named after a spec —
# e.g. the `server` spec living in domain `server` — must stay reachable as a
# domain group instead of being shadowed by the spec's item view).
# ---------------------------------------------------------------------------

def _collision_index(tmp_path):
    """An index where domain ``server`` collides with the ``server`` spec name."""
    specs = {}
    for n in ("server", "ws", "state", "app", "auth", "ssl", "tls", "api"):
        specs[n] = {"domain": "server", "locator": "L" * 80, "reqs": [("R", "s", [], [])]}
    for i in range(8):
        specs[f"e{i}"] = {"domain": "engine", "locator": "L" * 80, "reqs": [("R", "s", [], [])]}
    return make_index(tmp_path, specs)


def test_root_domain_handle_uses_sentinel_on_spec_name_collision(tmp_path):
    idx = _collision_index(tmp_path)
    out = r.render_root_view(idx, threshold=700)
    assert _byte_len(out) <= 700
    cmds = [_DRILL_RE.search(l).group(1)
            for l in out.splitlines() if _DRILL_RE.search(l)]
    # The server domain (colliding with the 'server' spec) drills via the
    # sentinel so it is not shadowed by the server spec's item view.
    assert f"{r._ROOT_DOMAIN_SENTINEL} server" in cmds
    # The non-colliding engine domain keeps the bare form.
    assert "engine" in cmds


def test_render_index_sentinel_routes_to_domain_not_colliding_spec(tmp_path):
    idx = _collision_index(tmp_path)
    # Sentinel routing → the server DOMAIN group, listing its specs as entries.
    out = r.render_index(idx, r._ROOT_DOMAIN_SENTINEL, ["server"], threshold=16384)
    assert "- server" in out          # the server spec listed as a domain member
    assert "- ws" in out
    assert "server::R" not in out      # did NOT open the server spec's item view
    # The bare spec name still routes to the spec's items (spec namespace wins).
    spec_out = r.render_index(idx, "server", threshold=16384)
    assert "server::R" in spec_out


def test_sentinel_domain_view_pages_stay_in_domain_namespace(tmp_path):
    # Drilling the colliding domain group and walking its pagination handles must
    # keep routing back to the domain group (sentinel-prefixed), never to the
    # server spec's item view.
    idx = _collision_index(tmp_path)
    out = r.render_index(idx, r._ROOT_DOMAIN_SENTINEL, ["server"], threshold=400)
    assert _byte_len(out) <= 400
    for l in out.splitlines():
        m = _DRILL_RE.search(l)
        if m:
            # Every drill command emitted from a colliding domain view carries
            # the sentinel as its first token.
            assert m.group(1).split()[0] == r._ROOT_DOMAIN_SENTINEL


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


# ---------------------------------------------------------------------------
# Oversized leaf item — drilling to a page must still expose the item address
# (every navigation path ends at a selectable <spec>::<requirement> leaf).
# ---------------------------------------------------------------------------

def test_oversized_leaf_item_keeps_address_on_drill(tmp_path):
    # A single Requirement whose tags/refs/summary alone exceed the threshold.
    big = {
        "reqs": [(
            "Huge Requirement",
            "x" * 2000,
            [f"tag-{i}" for i in range(60)],
            [f"other::Req {i:03d} long name" for i in range(40)],
        )],
    }
    idx = make_index(tmp_path, {"s": big})
    top = r.render_spec_view(idx, "s", threshold=600)
    assert "[page]" in top  # the oversized item is paginated behind a handle
    m = _DRILL_RE.search(top)
    assert m is not None
    parts = m.group(1).split()
    assert parts[0] == "s"
    page = r.render_spec_view(idx, "s", parts[1:], threshold=600)
    # Drilling the page MUST still surface the selectable item address, even
    # though the item is compacted to fit; otherwise analyze can never finish
    # drilling to this leaf.
    assert "s::Huge Requirement" in page


def test_oversized_leaf_address_survives_below_floor(tmp_path):
    # Even at a tiny threshold the address line is preserved (navigability wins
    # over the byte cap in this degenerate sub-floor case).
    big = {"reqs": [("Huge Requirement", "x" * 2000,
                     [f"tag-{i}" for i in range(60)], [])]}
    idx = make_index(tmp_path, {"s": big})
    top = r.render_spec_view(idx, "s", threshold=400)
    m = _DRILL_RE.search(top)
    assert m is not None
    page = r.render_spec_view(idx, "s", m.group(1).split()[1:], threshold=400)
    assert "s::Huge Requirement" in page


# ---------------------------------------------------------------------------
# Domain components named like page handles stay navigable (the domain
# namespace is authoritative over the renderer's pagination namespace).
# ---------------------------------------------------------------------------

def test_root_domain_named_like_page_token_is_navigable(tmp_path):
    idx = make_index(tmp_path, {
        "alpha": {"domain": "p1", "locator": "aloc", "reqs": [("A", "sa", [], [])]},
        "beta": {"domain": "p1", "locator": "bloc", "reqs": [("B", "sb", [], [])]},
        "gamma": {"domain": "core", "locator": "gloc", "reqs": [("G", "sg", [], [])]},
    })
    out = r.render_index(idx, "p1", threshold=16384)
    assert "- alpha" in out and "- beta" in out
    assert "- gamma" not in out  # the 'core' domain is not part of the p1 group


def test_nested_domain_component_named_like_page_token(tmp_path):
    idx = make_index(tmp_path, {
        "fmt": {"domain": "engine/p1", "locator": "L" * 40, "reqs": [("R", "s", [], [])]},
        "grd": {"domain": "engine/p1", "locator": "L" * 40, "reqs": [("R", "s", [], [])]},
        "llm": {"domain": "engine/llm", "locator": "L" * 40, "reqs": [("R", "s", [], [])]},
    })
    out = r.render_index(idx, "engine", ["p1"], threshold=16384)
    assert "- fmt" in out and "- grd" in out
    assert "- llm" not in out
    assert "no such group" not in out


def test_root_page_handle_not_shadowed_by_sibling_domain(tmp_path):
    # Domain ``engine`` has a sibling subgroup ``engine/p1`` AND enough direct
    # specs (domain exactly ``engine``) to force pagination of those direct
    # specs. The generated ``pN`` page command must open the *page*, NOT the
    # colliding ``engine/p1`` domain subgroup. Without the page sentinel the
    # command ``se3 spec index engine p1`` would be parsed as the domain path
    # ``engine/p1`` (the domain namespace is authoritative), shadowing the page.
    specs = {}
    for i in range(18):
        specs[f"eng{i:02d}"] = {
            "domain": "engine", "locator": "L" * 80, "reqs": [("R", "s", [], [])],
        }
    for n in ("fmt", "grd"):
        specs[n] = {
            "domain": "engine/p1", "locator": "L" * 80, "reqs": [("R", "s", [], [])],
        }
    idx = make_index(tmp_path, specs)
    out = r.render_index(idx, "engine", threshold=700)
    assert _byte_len(out) <= 700
    # The engine view paginated its direct specs behind ``[page]`` handles.
    assert any(l.startswith("[page]") for l in out.splitlines())
    drill_cmds = [_DRILL_RE.search(l).group(1)
                  for l in out.splitlines() if _DRILL_RE.search(l)]
    page_cmds = [c for c in drill_cmds if r._ROOT_PAGE_SENTINEL in c.split()]
    assert page_cmds, "expected page commands carrying the page sentinel"
    # Follow the first page command: it must resolve to a page (the engine/p1
    # subgroup stays folded behind its [group] handle), NOT expand the
    # engine/p1 domain (which would list fmt/grd as direct member specs).
    parts = page_cmds[0].split()
    page = r.render_index(idx, parts[0], parts[1:], threshold=700)
    assert "no such group" not in page
    assert "- fmt" not in page and "- grd" not in page


def test_oversized_requirement_name_stays_bounded_with_nonselectable_placeholder(tmp_path):
    # A requirement whose NAME alone makes the address line exceed the threshold.
    # The bounded-context invariant wins: every individual index response MUST stay
    # within the configured byte threshold. The drilled leaf therefore does NOT
    # force-emit the over-long literal address (which would overrun the bound and,
    # if byte-severed, read as a different non-existent item). It ALSO does NOT
    # substitute the broader ``<spec>::*`` whole-spec selector — that would
    # silently change an exact single-item selection into selecting every
    # Requirement in the spec (different, broader semantics). Instead it emits a
    # bounded, NON-selectable placeholder, since no threshold-bounded view can
    # expose the over-long exact address. A name this long is itself a
    # writing-discipline violation surfaced by guardrails.
    long_name = "Extremely " + "Long " * 200 + "Requirement Name"
    idx = make_index(tmp_path, {"s": {"reqs": [(long_name, "x" * 50, [], [])]}})
    threshold = 300
    top = r.render_spec_view(idx, "s", threshold=threshold)
    # The top view (a folded page handle, name-span capped) stays bounded.
    assert _byte_len(top) <= threshold
    drill = next((_DRILL_RE.search(l).group(1)
                  for l in top.splitlines() if _DRILL_RE.search(l)), None)
    assert drill is not None, "the oversized item is paginated behind a handle"
    parts = drill.split()
    page = r.render_spec_view(idx, "s", parts[1:], threshold=threshold)
    # The drilled page itself stays within the byte threshold (no exception).
    assert _byte_len(page) <= threshold
    # The over-long literal address is NOT emitted (it would overrun the bound).
    assert f"s::{long_name}" not in page
    # The broader whole-spec selector is NOT offered as a SELECTABLE leaf — a
    # ``- s::*`` line would be validated as the whole-spec wildcard and broaden
    # the selection. The placeholder is non-selectable (no ``- `` leaf line).
    assert "- s::*" not in page
    for line in page.splitlines():
        assert not (line.startswith("- ") and "::" in line), (
            "the oversized leaf must not surface any selectable item/wildcard line"
        )
    # A bounded, non-selectable placeholder names the spec and the resolution.
    assert "unselectable" in page
    assert "split this Requirement" in page or "index_render_threshold" in page


# ---------------------------------------------------------------------------
# Chapter (## section) grouping in the spec view — semantic structure before
# anonymous pagination (issue: spec views must group by the stored chapter
# outline, not flatten straight to pN pages).
# ---------------------------------------------------------------------------

def _make_sectioned_index(tmp_path, spec_name, sections):
    """Build an index whose items carry an enclosing ``## `` chapter section.

    ``sections`` is an ordered list of ``(section_name, [(req_name, summary)])``.
    """
    idx = SpecIndex(tmp_path)
    reqs = sum(len(items) for _s, items in sections)
    idx.spec_metas[spec_name] = SpecMeta(spec_name=spec_name, item_count=reqs)
    line = 1
    for sec, items in sections:
        line += 1  # the `## <sec>` heading occupies a line
        for rname, summary in items:
            item = ItemMeta(
                spec_name=spec_name,
                requirement_name=rname,
                spec_path=str(tmp_path / spec_name / "spec.md"),
                mtime=0.0,
                size=0,
                sha256_prefix="",
                summary=summary,
                line_start=line,
                line_end=line,
                section=sec,
            )
            idx.items[item.item_id] = item
            line += 1
    return idx


def _make_subsectioned_index(tmp_path, spec_name, section, subsections):
    """Build an index for one ``## `` chapter split into ``#### `` subsections.

    ``subsections`` is an ordered list of ``(subsection_name, [(req, summary)])``.
    Every item carries the same enclosing ``section`` and its run's ``subsection``.
    """
    idx = SpecIndex(tmp_path)
    reqs = sum(len(items) for _s, items in subsections)
    idx.spec_metas[spec_name] = SpecMeta(spec_name=spec_name, item_count=reqs)
    line = 2  # the `## <section>` heading occupies a line
    for sub, items in subsections:
        line += 1  # the `#### <sub>` divider occupies a line
        for rname, summary in items:
            item = ItemMeta(
                spec_name=spec_name,
                requirement_name=rname,
                spec_path=str(tmp_path / spec_name / "spec.md"),
                mtime=0.0,
                size=0,
                sha256_prefix="",
                summary=summary,
                line_start=line,
                line_end=line,
                section=section,
                subsection=sub,
            )
            idx.items[item.item_id] = item
            line += 1
    return idx


def test_oversized_chapter_prefers_subsection_dividers_over_pagination(tmp_path):
    # An oversized single ``## `` chapter organised with two meaningful ``#### ``
    # subsections must be split along those semantic boundaries — NOT chunked
    # every _SECTION_SUBDIVIDE_SIZE items by raw document order. Each subsection
    # run here is below the cap, so deeper structure fully replaces pagination.
    idx = _make_subsectioned_index(tmp_path, "flat", "Requirements", [
        ("Core Behaviour", [(f"Core {i}", "x" * 80) for i in range(8)]),
        ("Advanced Behaviour", [(f"Adv {i}", "x" * 80) for i in range(8)]),
    ])
    units = r._spec_section_units(idx, "flat")
    # Exactly two units, one per ``#### `` divider — not 16/12 ≈ 2 blind chunks
    # that would mix Core and Advanced requirements at the 12-item boundary.
    labels = [label for _tok, label, _items in units]
    assert len(units) == 2
    assert any("Core Behaviour" in l for l in labels)
    assert any("Advanced Behaviour" in l for l in labels)
    # The first unit holds only Core items, the second only Advanced — the
    # subsection boundary is respected, not the 12-item document-order cut.
    u0_names = {i.requirement_name for i in units[0][2]}
    u1_names = {i.requirement_name for i in units[1][2]}
    assert all(n.startswith("Core ") for n in u0_names)
    assert all(n.startswith("Adv ") for n in u1_names)

    # Rendered over threshold: semantic group handles, no anonymous pages.
    out = r.render_spec_view(idx, "flat", threshold=900)
    assert _byte_len(out) <= 900
    assert "[group] flat/" in out
    assert "[page] flat/" not in out


def test_oversized_subsection_run_falls_back_to_pagination_within_it(tmp_path):
    # When a single ``#### `` subsection run is itself larger than the cap, the
    # deeper structure is exhausted first (the run is its own group), then
    # deterministic name-range chunking applies WITHIN that run.
    idx = _make_subsectioned_index(tmp_path, "flat", "Requirements", [
        ("Small", [(f"S {i}", "x" * 60) for i in range(3)]),
        ("Huge", [(f"H {i:02d}", "x" * 60) for i in range(30)]),
    ])
    units = r._spec_section_units(idx, "flat")
    labels = [label for _tok, label, _items in units]
    # The small run stays one unit; the huge run is chunked into several units,
    # all still labelled with their subsection so the boundary is visible.
    assert sum(1 for l in labels if "Small" in l) == 1
    huge_units = [u for u in units if "Huge" in u[1]]
    assert len(huge_units) >= 2
    for _tok, _label, items in huge_units:
        assert all(i.requirement_name.startswith("H ") for i in items)


def test_spec_view_groups_by_chapter_when_multi_section(tmp_path):
    idx = _make_sectioned_index(tmp_path, "multi", [
        ("Chapter Alpha", [("A1", "x" * 120), ("A2", "x" * 120)]),
        ("Chapter Beta", [("B1", "x" * 120), ("B2", "x" * 120)]),
    ])
    out = r.render_spec_view(idx, "multi", threshold=700)
    # Over threshold ⇒ at least one chapter folds to a semantic group handle
    # (NOT an anonymous pN page) carrying its sN drill command.
    assert "[group] multi/Chapter" in out
    assert "se3 spec index multi s" in out
    assert _byte_len(out) <= 700


def test_spec_view_chapter_drilldown_yields_only_that_chapters_items(tmp_path):
    idx = _make_sectioned_index(tmp_path, "multi", [
        ("Chapter Alpha", [("A1", "x" * 120), ("A2", "x" * 120)]),
        ("Chapter Beta", [("B1", "x" * 120), ("B2", "x" * 120)]),
    ])
    s1 = r.render_spec_view(idx, "multi", ["s1"], threshold=900)
    assert "multi::A1" in s1 and "multi::A2" in s1
    assert "multi::B1" not in s1 and "multi::B2" not in s1


def test_spec_view_chapter_drilldown_is_deterministic_and_bounded(tmp_path):
    idx = _make_sectioned_index(tmp_path, "multi", [
        ("Alpha", [(f"A{i}", "x" * 80) for i in range(8)]),
        ("Beta", [(f"B{i}", "x" * 80) for i in range(8)]),
    ])
    for th in (700, 900, 1500):
        assert r.render_spec_view(idx, "multi", ["s1"], threshold=th) == \
            r.render_spec_view(idx, "multi", ["s1"], threshold=th)
    # Bad chapter token reports gracefully (it is a handle, not an item).
    bad = r.render_spec_view(idx, "multi", ["s99"], threshold=900)
    assert "no such group" in bad


def test_single_chapter_spec_groups_then_paginates(tmp_path):
    # One ## section with many items ⇒ name-range semantic subgroups derived
    # from the ### requirement outline at the top level ...
    idx = _make_sectioned_index(tmp_path, "flat", [
        ("Requirements", [(f"R{i}", "x" * 120) for i in range(40)]),
    ])
    out = r.render_spec_view(idx, "flat", threshold=1200)
    assert _byte_len(out) <= 1200
    assert "[group] flat/" in out
    assert "[page] flat/" not in out  # structure (groups) precedes pagination
    # ... and pagination is the fallback only WITHIN an over-threshold subgroup.
    drill = next(_DRILL_RE.search(l).group(1)
                 for l in out.splitlines() if _DRILL_RE.search(l))
    groups = drill.split()[1:]  # drop the leading spec name
    sub = r.render_spec_view(idx, "flat", groups, threshold=600)
    assert _byte_len(sub) <= 600
    assert "[page] flat/" in sub


# ---------------------------------------------------------------------------
# Truncation integrity — the last-resort cap never severs the header or a
# navigation unit (issue: arbitrary UTF-8 byte chop could cut a drill/show
# command in half, leaving the LLM unable to navigate).
# ---------------------------------------------------------------------------

def test_tiny_threshold_is_byte_bounded(tmp_path):
    idx = make_index(tmp_path, {"big": _spec_with_items(40)})
    # A threshold far below even the compact navigation header. The context-bound
    # invariant wins: the output is hard-truncated to the byte bound (never
    # exceeding the configured positive threshold), on a valid UTF-8 boundary.
    threshold = 50
    out = r.render_spec_view(idx, "big", threshold=threshold)
    assert _byte_len(out) <= threshold
    # Whatever survives is still a prefix of the compact navigation header (the
    # most useful navigation content first), never arbitrary mid-content bytes.
    assert r._COMPACT_NAV_HEADER.startswith(out)


def test_threshold_just_above_compact_header_keeps_both_commands(tmp_path):
    # When the threshold can hold the compact header (but not the full help), the
    # renderer still emits both navigation commands verbatim, within the bound.
    idx = make_index(tmp_path, {"big": _spec_with_items(40)})
    threshold = r._blen(r._COMPACT_NAV_HEADER)
    out = r.render_spec_view(idx, "big", threshold=threshold)
    assert _byte_len(out) <= threshold
    assert "se3 spec show <spec>::<requirement>" in out
    assert "se3 spec index <spec> [<group>...]" in out


def test_pages_carry_requirement_name_span(tmp_path):
    """A ``[page]`` handle is self-describing: it shows the requirement-name span
    (first … last) of the items behind it, derived from the ``###`` outline —
    not an opaque ``pN``. Pages live inside an over-threshold name-range
    subgroup, so we drill one level to reach the pagination layer."""
    idx = make_index(tmp_path, {"big": _spec_with_items(40)})
    top = r.render_spec_view(idx, "big", threshold=1200)
    drill = next(_DRILL_RE.search(l).group(1)
                 for l in top.splitlines() if _DRILL_RE.search(l))
    groups = drill.split()[1:]  # drop the leading spec name
    sub = r.render_spec_view(idx, "big", groups, threshold=1200)
    page_lines = [l for l in sub.splitlines() if l.startswith("[page]")]
    assert page_lines, "expected pagination handles within the drilled subgroup"
    # A page shows a requirement-name span (the … separator).
    assert any("…" in l for l in page_lines)
    # Handles never carry an item ``::`` address (item-identity invariant).
    assert all("::" not in l for l in page_lines)


def test_compact_header_keeps_small_threshold_output_bounded(tmp_path):
    """When the full self-describing header would overflow a small (but above
    the navigation floor) threshold, the renderer falls back to the compact
    navigation header so the whole output stays within the byte threshold."""
    idx = make_index(tmp_path, {"big": _spec_with_items(40)})
    threshold = 220
    out = r.render_spec_view(idx, "big", threshold=threshold)
    assert _byte_len(out) <= threshold
    # The compact header still carries a complete navigation path.
    assert "se3 spec show <spec>::<requirement>" in out
    assert "se3 spec index <spec> [<group>...]" in out


def test_truncation_never_severs_a_group_handles_drill_command(tmp_path):
    # Many small items so the top level renders page/group handles, with a
    # threshold that forces _enforce_threshold to drop trailing units.
    idx = make_index(tmp_path, {"big": _spec_with_items(200, summary_len=10)})
    out = r.render_spec_view(idx, "big", threshold=560)
    lines = out.splitlines()
    # Every retained handle line must be followed by its `→ se3 spec index`
    # drill command — never left dangling by truncation.
    for i, line in enumerate(lines):
        if line.startswith("[page]") or line.startswith("[group]"):
            assert i + 1 < len(lines)
            assert "se3 spec index" in lines[i + 1]
