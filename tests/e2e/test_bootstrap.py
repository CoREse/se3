"""Tests for :mod:`tianluo.e2e.bootstrap`.

Three properties carry most of the weight here, because each one protects
something a user would only discover after it had already gone wrong:

1. **``tianluo.yaml`` is never written.** The ``e2e.enabled`` switch is the
   user's promise about their machine; the flow may suggest flipping it and
   nothing more. Asserted by byte-comparing the file across every entry point.
2. **A rejected proposal leaves no half-written directory.** Validation happens
   in memory, so a failed generation must be indistinguishable on disk from one
   that never ran.
3. **Evolution is incremental.** Anything the model does not name — a hand-tuned
   readiness probe, an extra key, another scenario — survives untouched.

No test calls a real LLM: every one injects a scripted ``caller``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pytest
import yaml

from tianluo.e2e import bootstrap
from tianluo.e2e.config_schema import validate_content
from tianluo.e2e.content_config import content_dir, load_content_config
from tianluo.e2e.errors import E2EConfigError


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class FakeCaller:
    """A scripted stand-in for :class:`tianluo.engine.llm_caller.LLMCaller`.

    Answers are popped in order; the last one repeats, which is what makes
    "the model keeps returning the same broken document" easy to express.
    """

    def __init__(self, responses: Sequence[Any]) -> None:
        self.responses = list(responses)
        self.prompts: List[str] = []

    def call(self, prompt: str, **kwargs: Any) -> str:
        self.prompts.append(prompt)
        answer = (
            self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        )
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, str):
            return answer
        return json.dumps(answer)


def env_doc(**overrides: Any) -> Dict[str, Any]:
    document: Dict[str, Any] = {
        "network": "tianluo-e2e",
        "services": [
            {
                "name": "app",
                "image": "python:3.12-slim",
                "base_kind": "base",
                "build": ["pip install -e ."],
                "readiness": {"kind": "command", "command": ["python", "--version"]},
            }
        ],
    }
    document.update(overrides)
    return document


def scenario_doc(name: str = "cli-smoke", **overrides: Any) -> Dict[str, Any]:
    document: Dict[str, Any] = {
        "name": name,
        "driver": "app",
        "actions": [{"action": "exec", "command": ["luo", "--version"]}],
        "assertions": [{"kind": "exit_code", "equals": 0}],
    }
    document.update(overrides)
    return document


def generation(
    environment: Optional[Dict[str, Any]] = None,
    scenarios: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    return {
        "environment": env_doc() if environment is None else environment,
        "scenarios": list(
            scenarios
            if scenarios is not None
            else [{"file": "cli-smoke.yaml", "document": scenario_doc()}]
        ),
    }


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project root that looks like a real one to the context collector."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    return tmp_path


def write_content(root: Path, *, scenarios: Optional[Dict[str, Any]] = None) -> None:
    """Put a valid content directory on disk without going through the model."""
    directory = content_dir(root)
    (directory / "scenarios").mkdir(parents=True, exist_ok=True)
    (directory / "environment.yaml").write_text(
        yaml.safe_dump(env_doc(), sort_keys=False), encoding="utf-8"
    )
    for name, document in (scenarios or {"cli-smoke.yaml": scenario_doc()}).items():
        (directory / "scenarios" / name).write_text(
            yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
        )


def snapshot(root: Path) -> Dict[str, bytes]:
    """Every file under the project root, by relative path."""
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# ---------------------------------------------------------------------------
# first-time generation
# ---------------------------------------------------------------------------


class TestEnsureContent:
    def test_generates_both_documents_when_nothing_exists(self, project: Path) -> None:
        caller = FakeCaller([generation()])

        result = bootstrap.ensure_content(project, None, caller=caller)

        assert result.created is True
        assert result.written == (
            "tianluo/e2e/environment.yaml",
            "tianluo/e2e/scenarios/cli-smoke.yaml",
        )
        assert (content_dir(project) / "environment.yaml").is_file()

    def test_generated_content_is_loadable(self, project: Path) -> None:
        """The point of validating before writing: the loader must accept it."""
        bootstrap.ensure_content(project, None, caller=FakeCaller([generation()]))

        content = load_content_config(project)

        assert content is not None
        assert [s.name for s in content.services] == ["app"]
        assert [s.name for s in content.scenarios] == ["cli-smoke"]

    def test_baselines_directory_is_seeded(self, project: Path) -> None:
        bootstrap.ensure_content(project, None, caller=FakeCaller([generation()]))

        assert (content_dir(project) / "baselines" / ".gitkeep").is_file()

    def test_existing_content_is_left_alone_without_calling_the_model(
        self, project: Path
    ) -> None:
        write_content(project)
        before = snapshot(project)
        caller = FakeCaller([generation()])

        result = bootstrap.ensure_content(project, None, caller=caller)

        assert result.created is False
        assert result.written == ()
        assert caller.prompts == []
        assert snapshot(project) == before

    def test_existing_environment_is_used_and_not_rewritten(
        self, project: Path
    ) -> None:
        """A half-present directory gets only its missing half generated."""
        directory = content_dir(project)
        directory.mkdir(parents=True)
        custom = env_doc(services=[{"name": "svc", "image": "debian:stable-slim"}])
        (directory / "environment.yaml").write_text(
            yaml.safe_dump(custom, sort_keys=False), encoding="utf-8"
        )
        environment_before = (directory / "environment.yaml").read_bytes()
        caller = FakeCaller(
            [
                generation(
                    scenarios=[
                        {"file": "smoke.yaml", "document": scenario_doc(driver="svc")}
                    ]
                )
            ]
        )

        result = bootstrap.ensure_content(project, None, caller=caller)

        assert result.written == ("tianluo/e2e/scenarios/smoke.yaml",)
        assert (directory / "environment.yaml").read_bytes() == environment_before
        # The existing topology was handed to the model rather than guessed at.
        assert "svc" in caller.prompts[0]


class TestUnusableContentOnDisk:
    """A document that exists but cannot be read is an error, not an absence.

    Reading it tolerantly made the two halves of generation disagree — the prompt
    asked for a complete environment while the writer kept the unusable one — so
    both LLM calls produced answers that were discarded before a guaranteed
    validation failure. The file is never rewritten either: unparsable YAML may
    still hold work a person typed.
    """

    @pytest.mark.parametrize("body", ["", "\n# only a comment\n", "services: [oops\n"])
    def test_an_unusable_environment_fails_before_any_model_call(
        self, project: Path, body: str
    ) -> None:
        directory = content_dir(project)
        directory.mkdir(parents=True)
        (directory / "environment.yaml").write_text(body, encoding="utf-8")
        before = snapshot(project)
        caller = FakeCaller([generation()])

        with pytest.raises(E2EConfigError) as excinfo:
            bootstrap.ensure_content(project, None, caller=caller)

        assert "environment.yaml" in str(excinfo.value)
        assert caller.prompts == []
        assert snapshot(project) == before

    def test_an_unusable_scenario_file_fails_before_any_model_call(
        self, project: Path
    ) -> None:
        write_content(project)
        (content_dir(project) / "scenarios" / "cli-smoke.yaml").write_text(
            "", encoding="utf-8"
        )
        caller = FakeCaller([generation()])

        with pytest.raises(E2EConfigError) as excinfo:
            bootstrap.ensure_content(project, None, caller=caller)

        assert "cli-smoke.yaml" in str(excinfo.value)
        assert caller.prompts == []


class TestGenerationIsValidatedBeforeWriting:
    def test_a_ladder_violation_is_retried_with_the_validator_complaints(
        self, project: Path
    ) -> None:
        illegal = scenario_doc(
            assertions=[{"kind": "screenshot_diff", "baseline": "home.png"}]
        )
        caller = FakeCaller(
            [
                generation(scenarios=[{"file": "x.yaml", "document": illegal}]),
                generation(),
            ]
        )

        result = bootstrap.ensure_content(project, None, caller=caller)

        assert result.created is True
        assert len(caller.prompts) == 2
        assert "visual_regression" in caller.prompts[1]

    def test_a_declared_tier2_scenario_is_accepted_before_its_baseline_exists(
        self, project: Path
    ) -> None:
        """A first baseline can only come from running the scenario.

        Requiring the image at generation time would make it impossible for the
        flow to ever author a visual-regression scenario, however clearly the
        subject under test is a rendering.
        """
        visual = scenario_doc(
            "home",
            assertions=[
                {
                    "kind": "screenshot_diff",
                    "baseline": "home.png",
                    "visual_regression": True,
                }
            ],
        )
        caller = FakeCaller(
            [generation(scenarios=[{"file": "home.yaml", "document": visual}])]
        )

        result = bootstrap.ensure_content(project, None, caller=caller)

        assert result.created is True
        assert len(caller.prompts) == 1
        assert "tianluo/e2e/scenarios/home.yaml" in result.written

    def test_a_persistently_invalid_proposal_writes_nothing(
        self, project: Path
    ) -> None:
        illegal = scenario_doc(
            assertions=[{"kind": "visual_semantic", "question": "does it look ok?"}]
        )
        caller = FakeCaller(
            [generation(scenarios=[{"file": "x.yaml", "document": illegal}])]
        )
        before = snapshot(project)

        with pytest.raises(E2EConfigError) as excinfo:
            bootstrap.ensure_content(project, None, caller=caller)

        assert "semantic_visual" in str(excinfo.value)
        assert snapshot(project) == before
        assert not (content_dir(project) / "environment.yaml").exists()

    def test_attempts_are_bounded(self, project: Path) -> None:
        caller = FakeCaller([generation(scenarios=[])])

        with pytest.raises(E2EConfigError):
            bootstrap.ensure_content(project, None, caller=caller)

        assert len(caller.prompts) == bootstrap.MAX_GENERATION_ATTEMPTS

    def test_an_unparsable_answer_writes_nothing(self, project: Path) -> None:
        caller = FakeCaller(["I could not do that, sorry."])
        before = snapshot(project)

        with pytest.raises(E2EConfigError):
            bootstrap.ensure_content(project, None, caller=caller)

        assert snapshot(project) == before

    def test_a_caller_failure_writes_nothing(self, project: Path) -> None:
        caller = FakeCaller([RuntimeError("agent unavailable")])
        before = snapshot(project)

        with pytest.raises(E2EConfigError):
            bootstrap.ensure_content(project, None, caller=caller)

        assert snapshot(project) == before


class TestWriteContainment:
    def test_a_traversing_file_name_is_dropped(self, project: Path) -> None:
        caller = FakeCaller(
            [
                generation(
                    scenarios=[
                        {"file": "../../evil.yaml", "document": scenario_doc()}
                    ]
                )
            ]
        )

        with pytest.raises(E2EConfigError):
            bootstrap.ensure_content(project, None, caller=caller)

        assert not (project.parent / "evil.yaml").exists()
        assert not (project / "evil.yaml").exists()

    def test_the_containment_guard_refuses_an_escaping_path(
        self, project: Path
    ) -> None:
        """The last line of defence, asserted directly.

        File names originate from an LLM, so containment cannot be a convention:
        every write funnels through this guard.
        """
        with pytest.raises(E2EConfigError) as excinfo:
            bootstrap._resolve_target(project, "../../tianluo.yaml")

        assert "tianluo.yaml" in str(excinfo.value)


class TestNeverWritesProjectConfig:
    """The single most important guarantee in this module."""

    def test_generation_leaves_tianluo_yaml_byte_identical(
        self, project: Path
    ) -> None:
        config = project / "tianluo.yaml"
        config.write_text(
            yaml.safe_dump({"e2e": {"enabled": False}}, sort_keys=False),
            encoding="utf-8",
        )
        before = config.read_bytes()

        bootstrap.ensure_content(project, None, caller=FakeCaller([generation()]))

        assert config.read_bytes() == before

    def test_evolution_leaves_tianluo_yaml_byte_identical(
        self, project: Path
    ) -> None:
        config = project / "tianluo.yaml"
        config.write_text(
            yaml.safe_dump({"e2e": {"enabled": True}}, sort_keys=False),
            encoding="utf-8",
        )
        before = config.read_bytes()
        write_content(project)

        bootstrap.evolve_content(
            project,
            None,
            ["cover the new endpoint"],
            caller=FakeCaller(
                [
                    {
                        "scenarios": [
                            {
                                "file": "extra.yaml",
                                "operation": "add",
                                "document": scenario_doc("extra"),
                            }
                        ]
                    }
                ]
            ),
        )

        assert config.read_bytes() == before

    def test_no_write_path_targets_the_project_config(self) -> None:
        """Nothing in the module even names ``tianluo.yaml`` as a write target."""
        source = Path(bootstrap.__file__).read_text(encoding="utf-8")

        # The only occurrences are prose: the module docstring's invariant and
        # the suggestion's rationale. No open()/write_text() aimed at it exists.
        assert "write_text" in source
        for line in source.splitlines():
            if "write_text" in line or "open(" in line:
                assert "tianluo.yaml" not in line


# ---------------------------------------------------------------------------
# incremental evolution
# ---------------------------------------------------------------------------


class TestEvolveContent:
    def test_appending_an_assertion_preserves_everything_else(
        self, project: Path
    ) -> None:
        hand_written = scenario_doc(
            "cli-smoke",
            description="hand written by a person",
            tags=["smoke"],
            timeout=45,
        )
        write_content(project, scenarios={"cli-smoke.yaml": hand_written})
        caller = FakeCaller(
            [
                {
                    "scenarios": [
                        {
                            "file": "cli-smoke.yaml",
                            "operation": "update",
                            "append": {
                                "assertions": [
                                    {"kind": "stdout", "contains": "tianluo"}
                                ]
                            },
                        }
                    ]
                }
            ]
        )

        result = bootstrap.evolve_content(project, None, ["a hint"], caller=caller)

        assert result.evolved is True
        document = yaml.safe_load(
            (content_dir(project) / "scenarios" / "cli-smoke.yaml").read_text()
        )
        # Everything the model did not name survived verbatim.
        assert document["description"] == "hand written by a person"
        assert document["tags"] == ["smoke"]
        assert document["timeout"] == 45
        assert document["actions"] == hand_written["actions"]
        # And the append landed on top of the original assertion, not instead.
        assert document["assertions"][0] == {"kind": "exit_code", "equals": 0}
        assert {"kind": "stdout", "contains": "tianluo"} in document["assertions"]

    def test_set_replaces_only_the_named_keys(self, project: Path) -> None:
        write_content(project)
        caller = FakeCaller(
            [
                {
                    "scenarios": [
                        {
                            "file": "cli-smoke.yaml",
                            "operation": "update",
                            "set": {"timeout": 240},
                        }
                    ]
                }
            ]
        )

        bootstrap.evolve_content(project, None, None, caller=caller)

        document = yaml.safe_load(
            (content_dir(project) / "scenarios" / "cli-smoke.yaml").read_text()
        )
        assert document["timeout"] == 240
        assert document["driver"] == "app"
        assert document["assertions"] == [{"kind": "exit_code", "equals": 0}]

    def test_adding_a_new_scenario_file(self, project: Path) -> None:
        write_content(project)
        caller = FakeCaller(
            [
                {
                    "scenarios": [
                        {
                            "file": "api.yaml",
                            "operation": "add",
                            "document": scenario_doc("api"),
                        }
                    ]
                }
            ]
        )

        result = bootstrap.evolve_content(project, None, None, caller=caller)

        assert result.written == ("tianluo/e2e/scenarios/api.yaml",)
        content = load_content_config(project)
        assert sorted(s.name for s in content.scenarios) == ["api", "cli-smoke"]

    def test_add_over_an_existing_file_is_refused(self, project: Path) -> None:
        write_content(project)
        before = (content_dir(project) / "scenarios" / "cli-smoke.yaml").read_bytes()
        caller = FakeCaller(
            [
                {
                    "scenarios": [
                        {
                            "file": "cli-smoke.yaml",
                            "operation": "add",
                            "document": scenario_doc("cli-smoke", driver="app"),
                        }
                    ]
                }
            ]
        )

        result = bootstrap.evolve_content(project, None, None, caller=caller)

        assert "cli-smoke.yaml" in result.skipped
        assert result.written == ()
        assert (
            content_dir(project) / "scenarios" / "cli-smoke.yaml"
        ).read_bytes() == before

    def test_no_operation_can_delete_a_scenario(self, project: Path) -> None:
        write_content(project)
        caller = FakeCaller(
            [
                {
                    "scenarios": [
                        {"file": "cli-smoke.yaml", "operation": "delete"}
                    ]
                }
            ]
        )

        bootstrap.evolve_content(project, None, None, caller=caller)

        assert (content_dir(project) / "scenarios" / "cli-smoke.yaml").is_file()

    def test_repeating_the_same_append_changes_nothing(self, project: Path) -> None:
        """Idempotence: a fix loop revisits this step, and duplicates accumulate."""
        write_content(project)
        proposal = {
            "scenarios": [
                {
                    "file": "cli-smoke.yaml",
                    "operation": "update",
                    "append": {
                        "assertions": [{"kind": "stdout", "contains": "tianluo"}]
                    },
                }
            ]
        }

        bootstrap.evolve_content(
            project, None, None, caller=FakeCaller([proposal])
        )
        second = bootstrap.evolve_content(
            project, None, None, caller=FakeCaller([proposal])
        )

        assert second.written == ()
        assert second.evolved is False
        document = yaml.safe_load(
            (content_dir(project) / "scenarios" / "cli-smoke.yaml").read_text()
        )
        assert len(document["assertions"]) == 2

    def test_evolving_an_unbootstrapped_project_generates_instead(
        self, project: Path
    ) -> None:
        caller = FakeCaller([generation()])

        result = bootstrap.evolve_content(project, None, ["hint"], caller=caller)

        assert result.created is True
        assert (content_dir(project) / "environment.yaml").is_file()

    def test_a_corrupted_scenario_file_is_reported_not_silently_completed(
        self, project: Path
    ) -> None:
        """Only a *half-present* directory falls through to generation.

        A valid environment plus one unparsable scenario is corruption, and every
        other e2e command rejects it. Treating it as "nothing to evolve" reported
        success at the exact command the user ran to have the content maintained.
        """
        write_content(project)
        (content_dir(project) / "scenarios" / "cli-smoke.yaml").write_text(
            "name: [unclosed\n", encoding="utf-8"
        )
        before = snapshot(project)
        caller = FakeCaller([generation()])

        with pytest.raises(E2EConfigError) as excinfo:
            bootstrap.evolve_content(project, None, ["hint"], caller=caller)

        assert "cli-smoke.yaml" in str(excinfo.value)
        assert caller.prompts == []
        assert snapshot(project) == before

    def test_an_invalid_proposal_degrades_instead_of_breaking_the_run(
        self, project: Path
    ) -> None:
        """Existing content is valid and runnable — a bad suggestion is discarded."""
        write_content(project)
        before = snapshot(project)
        caller = FakeCaller(
            [
                {
                    "scenarios": [
                        {
                            "file": "bad.yaml",
                            "operation": "add",
                            "document": {"name": "bad", "driver": "nope"},
                        }
                    ]
                }
            ]
        )

        result = bootstrap.evolve_content(project, None, None, caller=caller)

        assert result.errors
        assert result.written == ()
        assert snapshot(project) == before

    def test_the_environment_can_gain_a_service_incrementally(
        self, project: Path
    ) -> None:
        write_content(project)
        caller = FakeCaller(
            [
                {
                    "environment": {
                        "append": {
                            "services": [
                                {
                                    "name": "db",
                                    "image": "postgres:16",
                                    "mount_source": False,
                                }
                            ]
                        }
                    },
                    "scenarios": [],
                }
            ]
        )

        bootstrap.evolve_content(project, None, None, caller=caller)

        content = load_content_config(project)
        assert [s.name for s in content.services] == ["app", "db"]
        # The original service kept its build steps.
        assert content.services[0].build == ("pip install -e .",)


# ---------------------------------------------------------------------------
# suggest_enable
# ---------------------------------------------------------------------------


class TestSuggestEnable:
    def test_suggests_for_a_project_that_looks_like_a_fit(
        self, project: Path
    ) -> None:
        message = bootstrap.suggest_enable(project)

        assert message
        assert "e2e.enabled" in message

    def test_writes_nothing_at_all(self, project: Path) -> None:
        before = snapshot(project)

        bootstrap.suggest_enable(project)

        assert snapshot(project) == before
        assert not (project / "tianluo.yaml").exists()

    def test_silent_when_e2e_is_already_enabled(self, project: Path) -> None:
        (project / "tianluo.yaml").write_text(
            yaml.safe_dump({"e2e": {"enabled": True}}), encoding="utf-8"
        )

        assert bootstrap.suggest_enable(project) == ""

    def test_silent_when_content_already_exists(self, project: Path) -> None:
        write_content(project)

        assert bootstrap.suggest_enable(project) == ""

    def test_silent_for_a_project_with_no_recognisable_shape(
        self, tmp_path: Path
    ) -> None:
        assert bootstrap.suggest_enable(tmp_path) == ""


# ---------------------------------------------------------------------------
# shipped assets and dependency isolation
# ---------------------------------------------------------------------------


class TestShippedExamples:
    def test_both_example_documents_ship_with_the_package(self) -> None:
        assert bootstrap._read_asset("environment.example.yaml").strip()
        assert bootstrap._read_asset("scenario.example.yaml").strip()

    def test_the_examples_satisfy_the_schema(self) -> None:
        """A reference sample that the validator would reject teaches the wrong
        shape to both the model and the human reading it."""
        environment = yaml.safe_load(
            bootstrap._read_asset("environment.example.yaml")
        )
        scenario = yaml.safe_load(bootstrap._read_asset("scenario.example.yaml"))

        errors = validate_content(
            {
                "environment": environment,
                "environment_source": "tianluo/e2e/environment.yaml",
                "scenarios": {"tianluo/e2e/scenarios/api-smoke.yaml": scenario},
            },
            "tianluo/e2e",
        )

        assert errors == []

    def test_the_example_scenario_illustrates_the_three_base_kinds(self) -> None:
        text = bootstrap._read_asset("environment.example.yaml")

        for kind in ("base", "playwright", "gui-xvfb"):
            assert kind in text


class TestDependencyIsolation:
    def test_the_llm_caller_import_lives_inside_a_function(self) -> None:
        """A module-scope engine import would put the LLM stack on the core path."""
        source = Path(bootstrap.__file__).read_text(encoding="utf-8")

        matches = [
            line
            for line in source.splitlines()
            if "import LLMCaller" in line or "import parse_json_response" in line
        ]
        assert matches
        for line in matches:
            assert line.startswith("    "), line
