"""Tests for the dual-layer preset prompt registry (src/tianluo/preset_loader.py).

Covers:
- Built-in presets are read at runtime from package data (no init copy).
- Project layer overrides the built-in layer for same-named presets.
- Unknown preset names raise with the available list.
- A declared-but-missing prompt_file raises rather than being swallowed.
- list_presets enumerates both layers.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from tianluo import preset_loader
from tianluo.preset_loader import (
    LAYER_BUILTIN,
    LAYER_PROJECT,
    PresetError,
    PresetNotFoundError,
    list_presets,
    load_registry,
    resolve,
)


@pytest.fixture
def project_root(tmp_path):
    return tmp_path


def _write_project_preset(root: Path, name: str, content: str, *, with_yaml: bool = True):
    prompts_dir = root / "tianluo" / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / f"{name}.md").write_text(content, encoding="utf-8")
    if with_yaml:
        (root / "tianluo.yaml").write_text(
            "presets:\n"
            f"  {name}:\n"
            "    type: feature\n"
            f"    prompt_file: tianluo/prompts/{name}.md\n",
            encoding="utf-8",
        )


class TestBuiltinLayer:
    def test_builtin_doc_sync_present(self, project_root):
        registry = load_registry(project_root)
        assert "doc-sync" in registry
        entry = registry["doc-sync"]
        assert entry.layer == LAYER_BUILTIN
        assert entry.type == "feature"

    def test_builtin_read_at_runtime_from_package(self, project_root):
        # The built-in prompt is read from package data, NOT copied into
        # the project — a clean tmp project still resolves it.
        ptype, text, layer = resolve("doc-sync", project_root)
        assert layer == LAYER_BUILTIN
        assert ptype == "feature"
        assert text.strip()  # non-empty prompt body
        assert "README" in text
        # Nothing was copied into the project tree.
        assert not (project_root / "tianluo" / "prompts").exists()

    def test_builtin_prompts_carry_no_spec_corpus_references(self, project_root):
        # The retired tianluo/specs/ mirror must not survive in packaged
        # prompt bodies: those go verbatim into the LLM prompt, and naming a
        # directory the framework no longer maintains either wastes a tool
        # round or (in a legacy project with a stale tree on disk) re-installs
        # the spec-over-code inversion this refactor removed.
        for name in preset_loader._BUILTIN_PRESET_METADATA:
            _ptype, text, _layer = resolve(name, project_root)
            assert "tianluo/specs" not in text, name
            assert "se3/specs" not in text, name


class TestProjectOverride:
    def test_project_overrides_builtin_same_name(self, project_root):
        _write_project_preset(
            project_root, "doc-sync", "PROJECT-LEVEL doc-sync prompt body"
        )
        ptype, text, layer = resolve("doc-sync", project_root)
        assert layer == LAYER_PROJECT
        assert "PROJECT-LEVEL" in text
        assert ptype == "feature"

    def test_project_preset_without_yaml_uses_default_type(self, project_root):
        # A bare tianluo/prompts/*.md file with no tianluo.yaml metadata still
        # registers, using the default type.
        _write_project_preset(
            project_root, "doc-sync", "bare project prompt", with_yaml=False
        )
        ptype, text, layer = resolve("doc-sync", project_root)
        assert layer == LAYER_PROJECT
        assert "bare project prompt" in text
        assert ptype == preset_loader.DEFAULT_PRESET_TYPE

    def test_yaml_type_override(self, project_root):
        prompts_dir = project_root / "tianluo" / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "thing.md").write_text("thing body", encoding="utf-8")
        (project_root / "tianluo.yaml").write_text(
            "presets:\n  thing:\n    type: bugfix\n", encoding="utf-8"
        )
        ptype, _text, layer = resolve("thing", project_root)
        assert ptype == "bugfix"
        assert layer == LAYER_PROJECT


class TestErrors:
    def test_unknown_preset_lists_available(self, project_root):
        with pytest.raises(PresetNotFoundError) as exc:
            resolve("does-not-exist", project_root)
        msg = str(exc.value)
        assert "does-not-exist" in msg
        # The built-in doc-sync should appear in the available list.
        assert "doc-sync" in msg

    def test_missing_prompt_file_raises(self, project_root):
        # Declared in tianluo.yaml but the file does not exist.
        (project_root / "tianluo.yaml").write_text(
            "presets:\n"
            "  ghost:\n"
            "    type: feature\n"
            "    prompt_file: tianluo/prompts/ghost.md\n",
            encoding="utf-8",
        )
        with pytest.raises(PresetError) as exc:
            resolve("ghost", project_root)
        assert "ghost" in str(exc.value)
        assert "missing" in str(exc.value).lower()


class TestListPresets:
    def test_list_includes_both_layers(self, project_root):
        _write_project_preset(
            project_root, "proj-only", "project preset body"
        )
        presets = list_presets(project_root)
        by_name = {e.name: e for e in presets}
        assert "doc-sync" in by_name
        assert by_name["doc-sync"].layer == LAYER_BUILTIN
        assert "proj-only" in by_name
        assert by_name["proj-only"].layer == LAYER_PROJECT
        # Sorted by name.
        names = [e.name for e in presets]
        assert names == sorted(names)
