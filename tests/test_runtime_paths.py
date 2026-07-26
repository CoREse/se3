"""Tests for the runtime-directory resolution helper (issue #270).

Canonical ``tianluo/`` preferred, legacy ``se3/`` fallback, canonical
default for fresh roots — plus the relative-path and dual-glob helpers.
"""

from __future__ import annotations

from pathlib import Path

from tianluo.runtime_paths import (
    LEGACY_RUNTIME_DIR_NAME,
    RUNTIME_DIR_NAME,
    dual_runtime_glob,
    runtime_dir,
    runtime_dir_name,
    runtime_relpath,
)


def test_fresh_root_defaults_to_canonical(tmp_path: Path):
    assert runtime_dir_name(tmp_path) == RUNTIME_DIR_NAME == "tianluo"
    assert runtime_dir(tmp_path) == tmp_path / "tianluo"


def test_legacy_root_falls_back_to_se3(tmp_path: Path):
    (tmp_path / "se3").mkdir()
    assert runtime_dir_name(tmp_path) == LEGACY_RUNTIME_DIR_NAME == "se3"
    assert runtime_dir(tmp_path) == tmp_path / "se3"


def test_canonical_wins_when_both_exist(tmp_path: Path):
    (tmp_path / "se3").mkdir()
    (tmp_path / "tianluo").mkdir()
    assert runtime_dir_name(tmp_path) == "tianluo"


def test_resolution_is_uncached_across_migration(tmp_path: Path):
    (tmp_path / "se3").mkdir()
    assert runtime_dir_name(tmp_path) == "se3"
    # simulate `luo migrate run rename-to-tianluo` mid-process
    (tmp_path / "se3").rename(tmp_path / "tianluo")
    assert runtime_dir_name(tmp_path) == "tianluo"


def test_file_named_like_runtime_dir_is_ignored(tmp_path: Path):
    # a stray FILE named tianluo must not hijack resolution
    (tmp_path / "tianluo").write_text("not a dir", encoding="utf-8")
    (tmp_path / "se3").mkdir()
    assert runtime_dir_name(tmp_path) == "se3"


def test_runtime_relpath_follows_layout(tmp_path: Path):
    assert runtime_relpath(tmp_path, "issues", ".next_id").as_posix() == (
        "tianluo/issues/.next_id"
    )
    (tmp_path / "se3").mkdir()
    assert runtime_relpath(tmp_path, "issues", ".next_id").as_posix() == (
        "se3/issues/.next_id"
    )


def test_dual_runtime_glob_sees_both_layouts(tmp_path: Path):
    for wt, name in (("a", "tianluo"), ("b", "se3")):
        d = tmp_path / wt / name / "state"
        d.mkdir(parents=True)
        (d / "engine.json").write_text("{}", encoding="utf-8")
    hits = dual_runtime_glob(tmp_path, "*/", "state/engine.json")
    assert {h.parts[-4] for h in hits} == {"a", "b"}
    # canonical results come first
    assert hits[0].parts[-3] == "tianluo"
