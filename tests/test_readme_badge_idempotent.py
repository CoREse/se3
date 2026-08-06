"""Regression guard: the README version badge must survive `commit` untouched.

README.md carries a shields.io *dynamic* TOML badge that reads the version out
of ``pyproject.toml`` at render time, so no release version is ever duplicated
into the README prose. The commit step still runs
``DocumentationUpdater.update_readme`` on every release, so that only holds as
long as this repository's ``documentation.readme_badge_template`` renders to
byte-identical markdown (i.e. carries no ``{{version}}`` placeholder).

WHY: this test reads the committed ``tianluo.yaml`` rather than the
maintainer's gitignored ``tianluo.local.yaml`` — the committed file is the
artifact being guarded, and it is the only one that exists on CI or in a fresh
clone. Config loading is whole-file select-one, so a local file shadowing this
block on a maintainer machine is a real hazard the docs call out; it is
deliberately out of scope here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tianluo.engine.docs_updater import DocumentationUpdater

REPO_ROOT = Path(__file__).resolve().parents[1]

# Deliberately absurd so "unchanged" cannot be an accident of the injected
# version happening to equal the one already written in the file.
INJECTED_VERSION = "99.99.99"


def _load_documentation_config() -> dict:
    """Read the ``documentation:`` block out of the committed ``tianluo.yaml``.

    Fails (never skips) when the file or the block is missing: their absence is
    precisely the regression this test exists to catch.
    """
    config_path = REPO_ROOT / "tianluo.yaml"
    assert config_path.is_file(), (
        f"{config_path} is missing — it carries the documentation.readme_badge_template "
        "that keeps the dynamic README badge stable across commits."
    )

    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    documentation = data.get("documentation")
    assert isinstance(documentation, dict) and documentation, (
        "tianluo.yaml has no `documentation:` block — without it the commit step "
        "would rewrite the dynamic README badge back into a hardcoded static one."
    )
    return documentation


def test_readme_badge_update_is_a_no_op(tmp_path: Path) -> None:
    """Running the real updater over the real README changes nothing."""
    readme_source = REPO_ROOT / "README.md"
    assert readme_source.is_file(), f"{readme_source} not found"

    original = readme_source.read_text(encoding="utf-8")

    # Operate on a copy only — the repository's README must never be written.
    readme_copy = tmp_path / "README.md"
    readme_copy.write_text(original, encoding="utf-8")

    updater = DocumentationUpdater(tmp_path, config=_load_documentation_config())
    updater.update_readme(INJECTED_VERSION)

    updated = readme_copy.read_text(encoding="utf-8")

    assert updated == original, (
        "DocumentationUpdater.update_readme rewrote README.md. The configured "
        "readme_badge_template must render byte-identically to the badge line "
        "already in README.md (no {{version}} placeholder)."
    )
    assert updated.count("![Version](") == 1, (
        "README.md must carry exactly one version badge; got "
        f"{updated.count('![Version](')}. A second one means the updater failed to "
        "match the existing badge and inserted a fresh one after the title."
    )
    assert INJECTED_VERSION not in updated, (
        f"The injected version {INJECTED_VERSION} leaked into README.md — the "
        "badge template must not interpolate a version number."
    )


@pytest.mark.parametrize("readme_name", ["README.md", "README.zh.md"])
def test_readme_carries_the_configured_dynamic_badge(readme_name: str) -> None:
    """Both READMEs must carry the exact badge literal configured in tianluo.yaml.

    ``update_readme`` only ever touches README.md, so the Chinese README's badge
    is not protected by the no-op test above; pinning both to the same literal is
    what keeps them from drifting apart.
    """
    configured_badge = _load_documentation_config()["readme_badge_template"]
    assert "{{version}}" not in configured_badge, (
        "readme_badge_template must not contain a {{version}} placeholder — that "
        "would reintroduce a hardcoded release version into README.md."
    )

    content = (REPO_ROOT / readme_name).read_text(encoding="utf-8")
    assert configured_badge in content, (
        f"{readme_name} does not contain the badge literal configured in "
        "tianluo.yaml; the two must stay identical."
    )
    assert content.count("![Version](") == 1
