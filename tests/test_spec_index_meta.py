"""G2 tests: spec index navigation-layer upgrade.

Covers the additions made by group G2 to ``se3.engine.spec_index``:

- ``ItemMeta`` physical line interval (``line_start`` / ``line_end``) and its
  JSON round-trip.
- Spec-level ``SpecMeta`` (``domain`` / ``locator`` / ``item_count``) parsed
  mechanically from the spec file, including the missing-domain (None) case.
- Index ``version`` bumped to 2, with an old v1 cache self-invalidating and
  triggering an automatic full rebuild rather than being mis-read.
- ``resolve_item_location`` returning a body consistent with the physical line
  interval, and rejecting non-item / unknown addresses.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from se3.engine.spec_index import (
    INDEX_VERSION,
    ItemMeta,
    SpecMeta,
    SpecIndex,
    load_or_build,
)
from se3.engine.spec_format import SPEC_FORMAT_VERSION_MARKER


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def write_spec(project_root: Path, spec_name: str, content: str) -> Path:
    spec_dir = project_root / "se3" / "specs" / spec_name
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec_file = spec_dir / "spec.md"
    spec_file.write_text(content, encoding="utf-8")
    return spec_file


ALPHA_SPEC = f"""{SPEC_FORMAT_VERSION_MARKER}
<!-- domain: engine/steps -->

# Alpha Spec

## Purpose

Alpha governs the example subsystem in one sentence. More detail follows here.

## Definitions

- Thing: a thing.

### Requirement: First Req

First requirement opening summary sentence.

More body for first requirement.

### Requirement: Second Req

Second requirement opening summary sentence.

Trailing body for the second requirement.
"""


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    (tmp_path / "se3" / "specs").mkdir(parents=True)
    return tmp_path


# ---------------------------------------------------------------------------
# Physical line interval
# ---------------------------------------------------------------------------

def test_line_interval_matches_file(tmp_project: Path) -> None:
    """line_start/line_end bracket each Requirement's actual lines in the file."""
    spec_file = write_spec(tmp_project, "alpha", ALPHA_SPEC)
    index = SpecIndex(tmp_project).build()

    file_lines = spec_file.read_text(encoding="utf-8").splitlines()

    first = index.get_item("alpha", "First Req")
    second = index.get_item("alpha", "Second Req")
    assert first is not None and second is not None

    # line_start points exactly at the Requirement heading line.
    assert file_lines[first.line_start - 1] == "### Requirement: First Req"
    assert file_lines[second.line_start - 1] == "### Requirement: Second Req"

    # Intervals are 1-based, inclusive, ordered, and non-overlapping.
    assert first.line_start >= 1
    assert first.line_end >= first.line_start
    assert first.line_end < second.line_start
    assert second.line_start >= 1
    assert second.line_end >= second.line_start

    # The last Requirement extends to EOF.
    assert second.line_end == len(file_lines)


def test_line_interval_round_trip(tmp_project: Path) -> None:
    """line_start/line_end survive save -> load unchanged."""
    write_spec(tmp_project, "alpha", ALPHA_SPEC)
    index = SpecIndex(tmp_project).build()
    index.save()

    reloaded = SpecIndex(tmp_project)
    assert reloaded.load() is True

    for key, item in index.items.items():
        assert key in reloaded.items
        assert reloaded.items[key].line_start == item.line_start
        assert reloaded.items[key].line_end == item.line_end


def test_itemmeta_from_dict_defaults_lines_to_zero() -> None:
    """A legacy ItemMeta payload without line fields defaults them to 0."""
    legacy = {
        "spec_name": "x",
        "requirement_name": "Y",
        "spec_path": "/tmp/x/spec.md",
        "mtime": 1.0,
        "size": 10,
        "sha256_prefix": "ab" * 16,
    }
    item = ItemMeta.from_dict(legacy)
    assert item.line_start == 0
    assert item.line_end == 0


# ---------------------------------------------------------------------------
# Spec-level metadata: domain / locator / item_count
# ---------------------------------------------------------------------------

def test_spec_meta_extraction(tmp_project: Path) -> None:
    """domain / locator / item_count are derived mechanically from the file."""
    write_spec(tmp_project, "alpha", ALPHA_SPEC)
    index = SpecIndex(tmp_project).build()

    meta = index.get_spec_meta("alpha")
    assert meta is not None
    assert meta.domain == "engine/steps"
    assert meta.locator.startswith("Alpha governs the example subsystem")
    # Locator is just the first Purpose paragraph (no Definitions leakage).
    assert "Definitions" not in meta.locator
    assert "Thing" not in meta.locator
    assert meta.item_count == 2


def test_spec_meta_missing_domain_is_none(tmp_project: Path) -> None:
    """A spec with no domain marker records domain=None without error."""
    content = f"""{SPEC_FORMAT_VERSION_MARKER}

# Beta Spec

## Purpose

Beta does the beta thing.

### Requirement: Only Req

Body.
"""
    write_spec(tmp_project, "beta", content)
    index = SpecIndex(tmp_project).build()

    meta = index.get_spec_meta("beta")
    assert meta is not None
    assert meta.domain is None
    assert meta.locator.startswith("Beta does the beta thing")
    assert meta.item_count == 1


def test_spec_meta_round_trip(tmp_project: Path) -> None:
    """SpecMeta survives save -> load and is persisted under the specs section."""
    write_spec(tmp_project, "alpha", ALPHA_SPEC)
    index = SpecIndex(tmp_project).build()
    index.save()

    index_file = tmp_project / "se3" / "cache" / "spec-index.json"
    data = json.loads(index_file.read_text(encoding="utf-8"))
    assert data["version"] == INDEX_VERSION
    assert "specs" in data
    assert data["specs"]["alpha"]["domain"] == "engine/steps"
    assert data["specs"]["alpha"]["item_count"] == 2

    reloaded = SpecIndex(tmp_project)
    assert reloaded.load() is True
    meta = reloaded.get_spec_meta("alpha")
    assert meta == SpecMeta(
        spec_name="alpha",
        domain="engine/steps",
        locator=index.get_spec_meta("alpha").locator,
        item_count=2,
    )


def test_spec_meta_updated_by_rebuild_for(tmp_project: Path) -> None:
    """rebuild_for incrementally refreshes the spec's metadata."""
    spec_file = write_spec(tmp_project, "alpha", ALPHA_SPEC)
    index = SpecIndex(tmp_project).build()
    assert index.get_spec_meta("alpha").item_count == 2

    # Rewrite with a different domain and an added Requirement.
    spec_file.write_text(
        f"""{SPEC_FORMAT_VERSION_MARKER}
<!-- domain: engine/merge -->

# Alpha Spec

## Purpose

Alpha changed its positioning sentence.

### Requirement: First Req

Body.

### Requirement: Second Req

Body.

### Requirement: Third Req

Body.
""",
        encoding="utf-8",
    )
    index.rebuild_for("alpha")

    meta = index.get_spec_meta("alpha")
    assert meta.domain == "engine/merge"
    assert meta.item_count == 3
    assert meta.locator.startswith("Alpha changed its positioning")


# ---------------------------------------------------------------------------
# Version self-invalidation
# ---------------------------------------------------------------------------

def test_old_version_cache_self_invalidates(tmp_project: Path) -> None:
    """A stale v1 cache fails to load and triggers a full v2 rebuild."""
    write_spec(tmp_project, "alpha", ALPHA_SPEC)

    cache_dir = tmp_project / "se3" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    index_file = cache_dir / "spec-index.json"
    # Hand-write a v1-shaped cache without line fields or the specs section.
    index_file.write_text(
        json.dumps(
            {
                "version": 1,
                "items": {
                    "alpha::First Req": {
                        "spec_name": "alpha",
                        "requirement_name": "First Req",
                        "spec_path": str(
                            tmp_project / "se3" / "specs" / "alpha" / "spec.md"
                        ),
                        "mtime": 0.0,
                        "size": 1,
                        "sha256_prefix": "00" * 16,
                        "tags": [],
                        "keywords": [],
                        "refs": [],
                        "summary": "stale",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    # A direct load must reject the stale version.
    stale = SpecIndex(tmp_project)
    assert stale.load() is False

    # load_or_build must rebuild from scratch into v2 with the new fields.
    index = load_or_build(tmp_project)
    item = index.get_item("alpha", "First Req")
    assert item is not None
    assert item.line_start >= 1
    assert item.line_end >= item.line_start
    assert index.get_spec_meta("alpha").domain == "engine/steps"

    # The persisted file is now v2.
    data = json.loads(index_file.read_text(encoding="utf-8"))
    assert data["version"] == INDEX_VERSION
    assert "specs" in data


# ---------------------------------------------------------------------------
# resolve_item_location
# ---------------------------------------------------------------------------

def test_resolve_item_location_body_matches_interval(tmp_project: Path) -> None:
    """The returned body equals exactly the file text of [line_start, line_end]."""
    spec_file = write_spec(tmp_project, "alpha", ALPHA_SPEC)
    index = SpecIndex(tmp_project).build()

    resolved = index.resolve_item_location("alpha", "First Req")
    assert resolved is not None
    path, start, end, body = resolved

    assert path == str(spec_file)
    file_lines = spec_file.read_text(encoding="utf-8").splitlines()
    expected = "\n".join(file_lines[start - 1 : end])
    assert body == expected
    # The body begins at the Requirement heading.
    assert body.startswith("### Requirement: First Req")
    # And it contains the requirement's own content but not the next heading.
    assert "First requirement opening summary sentence." in body
    assert "### Requirement: Second Req" not in body


def test_resolve_item_location_last_requirement_to_eof(tmp_project: Path) -> None:
    write_spec(tmp_project, "alpha", ALPHA_SPEC)
    index = SpecIndex(tmp_project).build()

    resolved = index.resolve_item_location("alpha", "Second Req")
    assert resolved is not None
    _, _, _, body = resolved
    assert body.startswith("### Requirement: Second Req")
    assert "Trailing body for the second requirement." in body


def test_resolve_item_location_unknown_returns_none(tmp_project: Path) -> None:
    write_spec(tmp_project, "alpha", ALPHA_SPEC)
    index = SpecIndex(tmp_project).build()

    assert index.resolve_item_location("alpha", "Nope") is None
    assert index.resolve_item_location("nonexistent", "First Req") is None


def test_resolve_item_location_rejects_no_requirements_sentinel(
    tmp_project: Path,
) -> None:
    """A spec with zero Requirements yields no resolvable item address."""
    content = f"""{SPEC_FORMAT_VERSION_MARKER}

# Empty Spec

## Purpose

Empty has only a purpose.
"""
    write_spec(tmp_project, "empty", content)
    index = SpecIndex(tmp_project).build()

    # The sentinel item exists internally but is not an addressable item.
    assert index.resolve_item_location("empty", "__no_requirements__") is None
    # Spec meta still records zero items.
    meta = index.get_spec_meta("empty")
    assert meta is not None
    assert meta.item_count == 0
