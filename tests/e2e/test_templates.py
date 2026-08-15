"""Tests for ``tianluo.e2e.templates`` — the three Dockerfile templates.

Assertions target the instructions that carry meaning (base image, the GUI
stack's four packages, build-step order) rather than whole-file snapshots: a
snapshot would fail on every comment edit while saying nothing about whether the
rendered Dockerfile still builds the environment it is supposed to.
"""

from __future__ import annotations

import pytest

from tianluo.e2e import templates
from tianluo.e2e.errors import E2EConfigError


def render(kind, **context):
    return templates.render_dockerfile(kind, context)


class TestKinds:
    def test_the_three_service_shapes_are_available(self):
        assert set(templates.available_kinds()) == {"base", "playwright", "gui-xvfb"}

    def test_unknown_kind_is_a_config_error(self):
        with pytest.raises(E2EConfigError) as excinfo:
            templates.render_dockerfile("kubernetes", {})

        assert "kubernetes" in str(excinfo.value)

    def test_every_advertised_kind_actually_renders(self):
        for kind in templates.available_kinds():
            assert render(kind, base_image="debian:stable-slim").startswith("# syntax=")


class TestBaseTemplate:
    def test_uses_the_configured_base_image(self):
        rendered = render("base", base_image="python:3.12-slim")

        assert "FROM python:3.12-slim" in rendered

    def test_defaults_the_workdir(self):
        assert "WORKDIR /workspace" in render("base", base_image="node:22-slim")

    def test_honours_an_explicit_workdir(self):
        rendered = render("base", base_image="node:22-slim", workdir="/srv/app")

        assert "WORKDIR /srv/app" in rendered

    def test_never_copies_the_project_source_in(self):
        """The source is bind-mounted so fix-loop iterations reuse the image."""
        rendered = render(
            "base",
            base_image="python:3.12-slim",
            build_steps=["pip install -r requirements.txt"],
        )

        instructions = [
            line.split()[0]
            for line in rendered.splitlines()
            if line and not line.startswith("#") and line[0].isupper()
        ]
        assert "COPY" not in instructions
        assert "ADD" not in instructions

    def test_a_missing_base_image_is_a_config_error(self):
        with pytest.raises(E2EConfigError) as excinfo:
            templates.render_dockerfile("base", {})

        assert "base_image" in str(excinfo.value)


class TestPlaywrightTemplate:
    def test_defaults_to_a_pinned_official_image(self):
        rendered = render("playwright")
        from_line = next(l for l in rendered.splitlines() if l.startswith("FROM "))

        assert from_line.startswith("FROM mcr.microsoft.com/playwright:")
        # A floating tag would move visual baselines out from under the project.
        assert not from_line.endswith(":latest")

    def test_records_why_baselines_must_come_from_this_image(self):
        """The font-rendering invariant is what makes tier-2 diffs meaningful."""
        rendered = render("playwright")

        assert "INVARIANT" in rendered
        assert "font" in rendered.lower()

    def test_accepts_an_explicit_base_image(self):
        rendered = render(
            "playwright", base_image="mcr.microsoft.com/playwright:v1.50.0-noble"
        )

        assert "FROM mcr.microsoft.com/playwright:v1.50.0-noble" in rendered


class TestGuiTemplate:
    @pytest.mark.parametrize("package", ["xvfb", "openbox", "scrot", "xdotool"])
    def test_installs_the_headless_gui_stack(self, package):
        rendered = render("gui-xvfb", base_image="debian:stable-slim")

        assert package in rendered

    def test_exposes_the_virtual_display(self):
        rendered = render("gui-xvfb", base_image="debian:stable-slim")

        assert "ENV DISPLAY=:99" in rendered
        assert "Xvfb" in rendered

    def test_display_and_screen_are_configurable(self):
        rendered = render(
            "gui-xvfb",
            base_image="debian:stable-slim",
            display=":42",
            screen="1920x1080x24",
        )

        assert "ENV DISPLAY=:42" in rendered
        assert "1920x1080x24" in rendered

    def test_the_vnc_debug_stack_is_off_by_default(self):
        rendered = render("gui-xvfb", base_image="debian:stable-slim")

        assert "x11vnc" not in rendered.replace("(x11vnc/noVNC)", "")
        assert "EXPOSE 5900" not in rendered

    def test_the_vnc_debug_stack_can_be_requested(self):
        rendered = render(
            "gui-xvfb", base_image="debian:stable-slim", debug_tools=True
        )

        assert "x11vnc" in rendered
        assert "novnc" in rendered
        assert "EXPOSE 5900 6080" in rendered

    def test_the_recipe_is_self_contained(self):
        """tianluo owns this recipe; it must not build on someone else's image."""
        rendered = render("gui-xvfb", base_image="debian:stable-slim")

        froms = [l for l in rendered.splitlines() if l.startswith("FROM ")]
        assert froms == ["FROM debian:stable-slim"]


class TestBuildSteps:
    def test_steps_keep_their_declared_order(self):
        rendered = render(
            "base",
            base_image="python:3.12-slim",
            build_steps=["first", "second", "third"],
        )

        positions = [rendered.index("RUN " + s) for s in ("first", "second", "third")]
        assert positions == sorted(positions)

    def test_a_plain_step_becomes_a_run_layer(self):
        assert templates.expand_build_steps(["apt-get update"]) == "RUN apt-get update"

    def test_a_dockerfile_instruction_is_emitted_verbatim(self):
        expanded = templates.expand_build_steps(
            ["ENV LANG=C.UTF-8", "pip install -e .", "USER app"]
        )

        assert expanded.splitlines() == [
            "ENV LANG=C.UTF-8",
            "RUN pip install -e .",
            "USER app",
        ]

    def test_blank_steps_are_dropped(self):
        assert templates.expand_build_steps(["", "   ", "echo hi"]) == "RUN echo hi"

    def test_a_block_scalar_step_becomes_one_layer_per_line(self):
        """A YAML `|` block is a natural shape in a generated environment.yaml.

        Prefixing `RUN ` to the whole blob left every line after the first as a
        bare Dockerfile line, so `docker build` died with "unknown instruction:
        apt-get" — a configuration input misreported as a host environment
        failure, with no fix-loop path to it.
        """
        expanded = templates.expand_build_steps(
            ["apt-get update\napt-get install -y curl\n", "pip install -e ."]
        )

        assert expanded.splitlines() == [
            "RUN apt-get update",
            "RUN apt-get install -y curl",
            "RUN pip install -e .",
        ]

    def test_a_backslash_continuation_stays_one_instruction(self):
        """A step deliberately written as one long instruction keeps its layer."""
        expanded = templates.expand_build_steps(
            ["apt-get update \\\n    && apt-get install -y curl"]
        )

        assert expanded == "RUN apt-get update \\\n    && apt-get install -y curl"

    def test_instruction_and_comment_lines_inside_a_block_are_respected(self):
        expanded = templates.expand_build_steps(["# why\nENV LANG=C.UTF-8\nmake build"])

        assert expanded.splitlines() == ["# why", "ENV LANG=C.UTF-8", "RUN make build"]

    def test_every_rendered_line_is_a_valid_dockerfile_instruction(self):
        """The property the bug broke: no line may stand alone as a bare command."""
        rendered = render(
            "base",
            base_image="python:3.12-slim",
            build_steps=["apt-get update\napt-get install -y curl"],
        )

        # FROM opens the template itself; it is not admissible as a build step.
        allowed = set(templates._DOCKERFILE_INSTRUCTIONS) | {"FROM"}
        continued = False
        for line in rendered.splitlines():
            stripped = line.strip()
            was_continued, continued = continued, stripped.endswith("\\")
            if not stripped or stripped.startswith("#") or was_continued:
                continue
            assert stripped.split()[0].upper() in allowed

    def test_no_steps_renders_nothing(self):
        assert templates.expand_build_steps(None) == ""
        assert templates.expand_build_steps([]) == ""

    def test_a_bare_string_is_one_step_not_one_layer_per_character(self):
        """A string is an Iterable, so iterating it yields *letters*.

        The flow's own callers pre-wrap the YAML value, but a direct caller of the
        public API passing ``build_steps="pip install ."`` got thirteen one-letter
        ``RUN`` layers — a Dockerfile valid enough to reach ``docker build``, which
        then failed with "p: command not found" and looked like a broken host.
        """
        assert templates.expand_build_steps("pip install .") == "RUN pip install ."

        rendered = render("base", base_image="python:3.12-slim", build_steps="make test")

        assert "RUN make test" in rendered
        assert "RUN m\n" not in rendered

    def test_render_for_service_maps_service_fields(self):
        rendered = templates.render_for_service(
            "base",
            "python:3.12-slim",
            workdir="/app",
            build_steps=("pip install -e .",),
        )

        assert "FROM python:3.12-slim" in rendered
        assert "WORKDIR /app" in rendered
        assert "RUN pip install -e ." in rendered


class TestPackaging:
    def test_templates_are_locatable_as_package_data(self):
        """They must resolve from an installed wheel, not just a source tree.

        The templates are framework assets that ship unconditionally — the
        ``tianluo[e2e]`` extra isolates third-party dependencies, never
        tianluo's own code — so an install that cannot find them is broken.
        """
        files = pytest.importorskip("importlib.resources").files
        root = files("tianluo.e2e").joinpath("templates")

        for kind in templates.available_kinds():
            assert root.joinpath(kind + ".Dockerfile.tmpl").is_file()

    def test_no_third_party_template_engine_is_imported(self):
        import sys

        from tianluo.e2e import templates as module  # noqa: F401

        assert "jinja2" not in sys.modules
        assert "mako" not in sys.modules

    def test_loader_reads_through_the_import_system_only(self):
        """No path is ever joined onto ``__file__``.

        Both readers (``importlib.resources.files`` and the ``pkgutil.get_data``
        fallback) delegate to the package's loader, so a package imported from a
        zip resolves its templates like any other install.
        """
        from pathlib import Path

        source = Path(templates.__file__).read_text(encoding="utf-8")

        assert "importlib.resources" in source
        assert "pkgutil.get_data" in source
        # `__file__` survives only inside the prose that explains why it is not
        # used; no line of code references it.
        assert "__file__" not in source.replace("``__file__``", "")
