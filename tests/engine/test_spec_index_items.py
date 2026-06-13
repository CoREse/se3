"""Tests for item-level spec index (SpecIndex v2).

Covers: build, load, save, incremental rebuild, invalidation, cross-spec
disambiguation, and 1-hop reference resolution.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from se3.engine.spec_index import INDEX_VERSION, SpecIndex, ItemMeta, load_or_build
from se3.engine.spec_format import SPEC_FORMAT_VERSION_MARKER


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """Create a minimal SE3 project with a few specs."""
    specs_dir = tmp_path / "se3" / "specs"
    specs_dir.mkdir(parents=True)
    return tmp_path


def write_spec(project_root: Path, spec_name: str, content: str) -> Path:
    """Write a spec file and return its path."""
    spec_dir = project_root / "se3" / "specs" / spec_name
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec_file = spec_dir / "spec.md"
    spec_file.write_text(content, encoding="utf-8")
    return spec_file


# ---------------------------------------------------------------------------
# Scenario 1: First build
# ---------------------------------------------------------------------------

def test_first_build(tmp_project: Path) -> None:
    """First call to load_or_build triggers a full scan."""
    write_spec(
        tmp_project,
        "alpha",
        f"""{SPEC_FORMAT_VERSION_MARKER}

# Alpha Spec

## Purpose

Test spec for indexing.

### Requirement: First Req

Body of first requirement.

**tags**: foo, bar
**keywords**: auth, login

### Requirement: Second Req

Body of second requirement.

**tags**: baz
""",
    )

    index = load_or_build(tmp_project)

    assert len(index.items) == 2
    assert "alpha::First Req" in index.items
    assert "alpha::Second Req" in index.items

    item1 = index.items["alpha::First Req"]
    assert item1.spec_name == "alpha"
    assert item1.requirement_name == "First Req"
    assert item1.tags == ["foo", "bar"]
    assert item1.keywords == ["auth", "login"]
    assert item1.spec_path == str(tmp_project / "se3" / "specs" / "alpha" / "spec.md")
    assert item1.sha256_prefix != ""
    assert len(item1.sha256_prefix) == 32  # 16 bytes = 32 hex chars

    # Summary is derived from first paragraph
    assert "Body of first requirement" in item1.summary

    # Backwards-compat specs property
    assert "alpha" in index.specs
    assert index.specs["alpha"].name == "alpha"

    # Selector list
    selector = index.list_for_selector()
    assert len(selector) == 2
    names = [s["requirement_name"] for s in selector]
    assert names == ["First Req", "Second Req"]


# ---------------------------------------------------------------------------
# Scenario 2: mtime unchanged → skip rebuild
# ---------------------------------------------------------------------------

def test_mtime_unchanged_skips_rebuild(tmp_project: Path) -> None:
    """When mtime/size are unchanged, load_or_build should NOT re-parse."""
    write_spec(
        tmp_project,
        "beta",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Beta
## Purpose
Test.
### Requirement: R1
Body.
""",
    )

    # First build
    index1 = load_or_build(tmp_project)
    assert len(index1.items) == 1

    # Save index
    index1.save()

    # Second load_or_build should detect no changes
    index2 = load_or_build(tmp_project)
    assert len(index2.items) == 1
    # Items should be identical (not rebuilt)
    assert index2.items["beta::R1"].sha256_prefix == index1.items["beta::R1"].sha256_prefix


# ---------------------------------------------------------------------------
# Scenario 3: mtime changed → rebuild
# ---------------------------------------------------------------------------

def test_mtime_changed_triggers_rebuild(tmp_project: Path) -> None:
    """Changing a spec's mtime (without content change) triggers hash check."""
    spec_file = write_spec(
        tmp_project,
        "gamma",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Gamma
## Purpose
Test.
### Requirement: R1
Body one.
""",
    )

    index = load_or_build(tmp_project)
    index.save()

    # Touch the file to change mtime
    time.sleep(0.05)
    os.utime(spec_file, None)

    # Re-load: should detect mtime change, compute hash, hash same → no rebuild
    # Wait, actually the hash is same so it shouldn't rebuild. But if we change content...

    # Let's change the content instead
    spec_file.write_text(
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Gamma
## Purpose
Test.
### Requirement: R1
Body one changed.
""",
        encoding="utf-8",
    )

    index2 = load_or_build(tmp_project)
    item = index2.items["gamma::R1"]
    assert "changed" in item.summary


# ---------------------------------------------------------------------------
# Scenario 4: hash consistent skips rebuild despite mtime change
# ---------------------------------------------------------------------------

def test_hash_consistent_skips_rebuild(tmp_project: Path) -> None:
    """If mtime changes but hash is identical, no rebuild needed."""
    spec_file = write_spec(
        tmp_project,
        "delta",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Delta
## Purpose
Test.
### Requirement: R1
Body.
""",
    )

    index = load_or_build(tmp_project)
    old_hash = index.items["delta::R1"].sha256_prefix
    index.save()

    # Only touch mtime, do NOT change content
    time.sleep(0.05)
    atime = spec_file.stat().st_atime
    mtime = time.time()
    os.utime(spec_file, (atime, mtime))

    # needs_rebuild should check hash and return False
    assert not index.needs_rebuild("delta")

    # load_or_build should also skip rebuild
    index2 = load_or_build(tmp_project)
    assert index2.items["delta::R1"].sha256_prefix == old_hash


# ---------------------------------------------------------------------------
# Scenario 5: Cross-spec same requirement name does not collide
# ---------------------------------------------------------------------------

def test_cross_spec_name_disambiguation(tmp_project: Path) -> None:
    """Two specs with the same Requirement name get distinct item keys."""
    write_spec(
        tmp_project,
        "spec-a",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Spec A
## Purpose
Test A.
### Requirement: Config
Config for A.
""",
    )
    write_spec(
        tmp_project,
        "spec-b",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Spec B
## Purpose
Test B.
### Requirement: Config
Config for B.
""",
    )

    index = load_or_build(tmp_project)
    assert len(index.items) == 2

    item_a = index.get_item("spec-a", "Config")
    item_b = index.get_item("spec-b", "Config")
    assert item_a is not None
    assert item_b is not None
    assert item_a.spec_name == "spec-a"
    assert item_b.spec_name == "spec-b"
    assert item_a.item_id == "spec-a::Config"
    assert item_b.item_id == "spec-b::Config"


# ---------------------------------------------------------------------------
# Scenario 6: refs 1-hop resolution
# ---------------------------------------------------------------------------

def test_resolve_refs_one_hop(tmp_project: Path) -> None:
    """resolve_refs expands exactly 1 hop, no chaining."""
    write_spec(
        tmp_project,
        "ref-spec",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Ref Spec
## Purpose
Test refs.
### Requirement: Alpha
Alpha body.

### Requirement: Beta
Beta body refers to Requirement: Alpha.

### Requirement: Gamma
Gamma body refers to Requirement: Beta.

### Requirement: Delta
Delta refers to ref-spec::Alpha.
""",
    )

    index = load_or_build(tmp_project)

    # Beta refers to Alpha (intra-spec)
    extra = index.resolve_refs("ref-spec::Beta", max_hops=1)
    assert extra == {"ref-spec::Alpha"}

    # Gamma refers to Beta → 1-hop only, so Beta is returned, not Alpha
    extra = index.resolve_refs("ref-spec::Gamma", max_hops=1)
    assert extra == {"ref-spec::Beta"}

    # Delta refers to Alpha via inter-spec ref
    extra = index.resolve_refs("ref-spec::Delta", max_hops=1)
    assert extra == {"ref-spec::Alpha"}

    # max_hops=0 returns empty
    extra = index.resolve_refs("ref-spec::Beta", max_hops=0)
    assert extra == set()


def test_resolve_refs_missing_ignored(tmp_project: Path) -> None:
    """Refs pointing to non-existent items are silently ignored."""
    write_spec(
        tmp_project,
        "missing-ref",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Missing Ref
## Purpose
Test.
### Requirement: A
A refers to Requirement: NonExistent.
""",
    )

    index = load_or_build(tmp_project)
    extra = index.resolve_refs("missing-ref::A", max_hops=1)
    assert extra == set()


# ---------------------------------------------------------------------------
# Additional coverage
# ---------------------------------------------------------------------------

def test_list_for_selector_sorted_and_stable(tmp_project: Path) -> None:
    """list_for_selector returns a stably sorted list."""
    write_spec(
        tmp_project,
        "z",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Z
## Purpose
Test.
### Requirement: B
Body B.
### Requirement: A
Body A.
""",
    )
    write_spec(
        tmp_project,
        "a",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# A
## Purpose
Test.
### Requirement: C
Body C.
""",
    )

    index = load_or_build(tmp_project)
    selector = index.list_for_selector()
    keys = [(s["spec"], s["requirement_name"]) for s in selector]
    assert keys == [("a", "C"), ("z", "A"), ("z", "B")]


def test_rebuild_for_single_spec(tmp_project: Path) -> None:
    """rebuild_for updates only one spec's items."""
    write_spec(
        tmp_project,
        "s1",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# S1
## Purpose
Test.
### Requirement: R1
Body.
""",
    )
    write_spec(
        tmp_project,
        "s2",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# S2
## Purpose
Test.
### Requirement: R2
Body.
""",
    )

    index = load_or_build(tmp_project)
    assert len(index.items) == 2

    # Update s1
    write_spec(
        tmp_project,
        "s1",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# S1
## Purpose
Test.
### Requirement: R1
Body updated.
### Requirement: R1b
New req.
""",
    )
    index.rebuild_for("s1")

    assert len(index.items) == 3
    assert "s1::R1" in index.items
    assert "s1::R1b" in index.items
    assert "s2::R2" in index.items
    assert "updated" in index.items["s1::R1"].summary


def test_load_or_build_reloads_under_lock_before_save(tmp_project, monkeypatch) -> None:
    """``load_or_build`` reloads the on-disk index AFTER acquiring the rebuild
    lock, so a concurrent writer's update to a spec NOT in this process's
    rebuild set is not clobbered by this process's stale in-memory snapshot.

    Race re-created: this process computes its rebuild set as ``{y}`` while
    ``x`` still looks fresh. *Then* a concurrent writer updates ``x`` and saves
    the index. The pre-fix code only re-checked its own ``{y}`` subset and would
    save its stale ``x``, losing the concurrent update. The fix reloads the
    on-disk index (and re-derives the full rebuild set) under the lock, so the
    concurrent ``x`` update survives.
    """
    import se3.engine.spec_index as si

    write_spec(
        tmp_project, "x",
        f"{SPEC_FORMAT_VERSION_MARKER}\n# X\n## Purpose\nP.\n### Requirement: A\nX original body.\n",
    )
    write_spec(
        tmp_project, "y",
        f"{SPEC_FORMAT_VERSION_MARKER}\n# Y\n## Purpose\nP.\n### Requirement: B\nY original.\n",
    )
    # Build the initial on-disk index (x and y both fresh).
    load_or_build(tmp_project)

    # Make only ``y`` stale, so this process's rebuild set is exactly ``{y}``
    # and ``x`` is NOT in it.
    time.sleep(0.01)
    write_spec(
        tmp_project, "y",
        f"{SPEC_FORMAT_VERSION_MARKER}\n# Y\n## Purpose\nP.\n### Requirement: B\nY changed.\n",
    )

    # A concurrent writer updates ``x`` and saves the whole index — fired EXACTLY
    # once, right after this process computed its initial rebuild set (the first
    # ``_compute_specs_to_rebuild`` return), i.e. inside the lock-acquisition
    # window the fix must close.
    real_compute = si._compute_specs_to_rebuild
    state = {"fired": False}

    def _compute_with_race(index):
        result = real_compute(index)
        if not state["fired"]:
            state["fired"] = True
            time.sleep(0.01)
            write_spec(
                tmp_project, "x",
                f"{SPEC_FORMAT_VERSION_MARKER}\n# X\n## Purpose\nP.\n"
                f"### Requirement: A\nX CONCURRENTLY changed.\n",
            )
            other = si.SpecIndex(tmp_project)
            other.build()
            other.save()
        return result

    monkeypatch.setattr(si, "_compute_specs_to_rebuild", _compute_with_race)
    index = load_or_build(tmp_project)

    # The concurrent update to ``x`` survived — not clobbered by this process's
    # stale in-memory copy.
    assert "CONCURRENTLY" in index.items["x::A"].summary
    # ``y`` is consistent too.
    assert "y::B" in index.items
    # The persisted file on disk also retains the concurrent ``x`` update.
    reloaded = si.SpecIndex(tmp_project)
    assert reloaded.load() is True
    assert "CONCURRENTLY" in reloaded.items["x::A"].summary


def test_save_and_load_roundtrip(tmp_project: Path) -> None:
    """Index survives save/load roundtrip intact."""
    write_spec(
        tmp_project,
        "round",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Round
## Purpose
Test.
### Requirement: R
Body.
**tags**: x, y
""",
    )

    index1 = load_or_build(tmp_project)
    index1.save()

    index2 = SpecIndex(tmp_project)
    assert index2.load()
    assert len(index2.items) == 1
    item = index2.items["round::R"]
    assert item.tags == ["x", "y"]
    assert item.spec_name == "round"


def test_no_requirements_spec_gets_sentinel(tmp_project: Path) -> None:
    """A spec with no Requirements gets a sentinel entry for change detection."""
    write_spec(
        tmp_project,
        "empty",
        """# Empty

## Purpose

No requirements here.
""",
    )

    index = load_or_build(tmp_project)
    assert len(index.items) == 1
    assert "empty::__no_requirements__" in index.items
    # sentinel should not appear in selector
    assert index.list_for_selector() == []


def test_deleted_spec_detected_on_reload(tmp_project: Path) -> None:
    """If a spec is deleted, load_or_build removes its items."""
    write_spec(
        tmp_project,
        "gone",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Gone
## Purpose
Test.
### Requirement: R
Body.
""",
    )

    index = load_or_build(tmp_project)
    index.save()
    assert "gone::R" in index.items

    # Delete the spec
    import shutil
    shutil.rmtree(tmp_project / "se3" / "specs" / "gone")

    index2 = load_or_build(tmp_project)
    assert "gone::R" not in index2.items


# ---------------------------------------------------------------------------
# Integration: build against real project specs
# ---------------------------------------------------------------------------

def test_build_against_real_specs() -> None:
    """Verify the index can be built from the actual se3/specs/ directory."""
    project_root = Path(__file__).resolve().parents[2]
    index = load_or_build(project_root)

    # We should have at least base + several specs with requirements
    assert len(index.items) >= 10

    # base spec should be present
    base_items = [item for item in index.items.values() if item.spec_name == "base"]
    assert len(base_items) >= 1

    # Selector should be non-empty and sorted
    selector = index.list_for_selector()
    assert len(selector) >= 10
    # Verify sorted order
    keys = [(s["spec"], s["requirement_name"]) for s in selector]
    assert keys == sorted(keys)

    # Verify JSON serializability of the saved index
    index_file = project_root / "se3" / "cache" / "spec-index.json"
    assert index_file.exists()
    with open(index_file, encoding="utf-8") as f:
        data = json.load(f)
    assert "version" in data
    assert "items" in data
    # Every item key should be spec::requirement format
    for key in data["items"]:
        assert "::" in key


def test_real_specs_index_file_is_valid_json() -> None:
    """The produced index file must be valid JSON and parseable by jq."""
    project_root = Path(__file__).resolve().parents[2]
    index_file = project_root / "se3" / "cache" / "spec-index.json"

    # Ensure index exists
    index = load_or_build(project_root)
    index.save()

    with open(index_file, encoding="utf-8") as f:
        data = json.load(f)

    assert data["version"] == INDEX_VERSION
    assert isinstance(data["items"], dict)

    for key, item_data in data["items"].items():
        assert "spec_name" in item_data
        assert "requirement_name" in item_data
        assert "mtime" in item_data
        assert "size" in item_data
        assert "sha256_prefix" in item_data
        assert len(item_data["sha256_prefix"]) == 32  # 16 bytes hex
        assert "tags" in item_data
        assert "keywords" in item_data
        assert "refs" in item_data
        assert "summary" in item_data


def test_same_length_content_change_with_restored_mtime_rebuilds(tmp_project: Path) -> None:
    """A same-size edit whose mtime is restored must still be detected.

    Trusting mtime+size alone would serve a stale index (wrong item names,
    summaries, line locations) indefinitely; the content hash is the
    authoritative currency proof and is validated on every needs_rebuild call.
    """
    spec_file = write_spec(
        tmp_project,
        "epsilon",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Epsilon
## Purpose
P.
### Requirement: AAAA
body one
""",
    )
    index = load_or_build(tmp_project)
    index.save()
    assert "epsilon::AAAA" in index.items
    st = spec_file.stat()

    # Rename the requirement AAAA -> BBBB (identical byte length), then restore
    # the original mtime so both mtime AND size match the cached entry.
    spec_file.write_text(
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Epsilon
## Purpose
P.
### Requirement: BBBB
body one
""",
        encoding="utf-8",
    )
    os.utime(spec_file, (st.st_atime, st.st_mtime))
    assert spec_file.stat().st_size == st.st_size
    assert spec_file.stat().st_mtime == st.st_mtime

    assert index.needs_rebuild("epsilon") is True
    index2 = load_or_build(tmp_project)
    assert "epsilon::BBBB" in index2.items
    assert "epsilon::AAAA" not in index2.items


def test_item_records_enclosing_chapter_section(tmp_project: Path) -> None:
    """Each item records its nearest preceding ``## `` chapter heading."""
    write_spec(
        tmp_project,
        "chapters",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Chapters
## Purpose
P.
## Core
### Requirement: One
First.
## Advanced
### Requirement: Two
Second.
""",
    )
    index = load_or_build(tmp_project)
    assert index.items["chapters::One"].section == "Core"
    assert index.items["chapters::Two"].section == "Advanced"


def test_item_records_enclosing_subsection_divider(tmp_project: Path) -> None:
    """A ``#### `` divider (one whose next non-blank line is a ``### Requirement:``
    heading) opens a sub-section every following Requirement belongs to until the
    next divider or a new ``## `` chapter. A ``#### `` heading inside a Requirement
    body (followed by prose, not a Requirement) is NOT a divider and does not leak
    its label onto the next Requirement."""
    write_spec(
        tmp_project,
        "subs",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Subs
## Purpose
P.
## Requirements

#### Core
### Requirement: One
First.

### Requirement: Two
Second.

#### Advanced
### Requirement: Three
Third.

### Requirement: Four
Fourth.

#### Inline Note
Prose under an inline note (not a divider — prose follows, not a Requirement).

### Requirement: Five
Fifth.
""",
    )
    index = load_or_build(tmp_project)
    # The two real dividers group the requirements that follow them.
    assert index.items["subs::One"].subsection == "Core"
    assert index.items["subs::Two"].subsection == "Core"
    assert index.items["subs::Three"].subsection == "Advanced"
    assert index.items["subs::Four"].subsection == "Advanced"
    # "Inline Note" is followed by prose, not a Requirement, so it is NOT a
    # divider — Five keeps the most recent real divider (Advanced), not the note.
    assert index.items["subs::Five"].subsection == "Advanced"


def test_subsection_does_not_leak_across_chapter_boundary(tmp_project: Path) -> None:
    """A ``#### `` divider's sub-section is scoped to its ``## `` chapter; a later
    chapter's Requirements without their own divider carry an empty subsection."""
    write_spec(
        tmp_project,
        "scoped",
        f"""{SPEC_FORMAT_VERSION_MARKER}
# Scoped
## Purpose
P.
## Chapter A

#### Group X
### Requirement: A1
Body.

## Chapter B
### Requirement: B1
Body.
""",
    )
    index = load_or_build(tmp_project)
    assert index.items["scoped::A1"].subsection == "Group X"
    # B1 is in a different chapter with no divider of its own — no leak.
    assert index.items["scoped::B1"].subsection == ""
