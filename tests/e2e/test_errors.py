"""Tests for ``tianluo.e2e.errors`` — the environment / scenario blame split.

The taxonomy is not decoration: the engine routes on it. An environment error
must reach FAILED with remediation, a scenario failure must reach the fix loop.
These tests pin the properties the step handler will rely on.
"""

from __future__ import annotations

import pytest

from tianluo.e2e.errors import (
    E2E_EXTRA,
    E2EConfigError,
    E2EDependencyMissingError,
    E2EEnvironmentError,
    E2EError,
    E2EScenarioFailure,
)


@pytest.mark.parametrize(
    "exc",
    [
        E2EConfigError("bad scenario"),
        E2EEnvironmentError("no runtime"),
        E2EDependencyMissingError("Pillow"),
        E2EScenarioFailure("login scenario failed"),
    ],
)
def test_every_error_shares_the_common_base(exc):
    assert isinstance(exc, E2EError)


class TestEnvironmentError:
    def test_remediation_defaults_to_empty_and_is_exposed(self):
        exc = E2EEnvironmentError("no runtime")

        assert exc.remediation == ""
        assert str(exc) == "no runtime"

    def test_remediation_is_appended_to_the_rendered_message(self):
        exc = E2EEnvironmentError("no runtime", remediation="install podman")

        assert exc.remediation == "install podman"
        assert "no runtime" in str(exc)
        assert "install podman" in str(exc)


class TestDependencyMissingError:
    def test_message_carries_the_actionable_install_command(self):
        exc = E2EDependencyMissingError("Pillow")

        assert "pip install 'tianluo[e2e]'" in str(exc)
        assert "Pillow" in str(exc)
        assert exc.dependency == "Pillow"

    def test_feature_context_is_included_when_given(self):
        exc = E2EDependencyMissingError("Pillow", feature="baseline screenshot diff")

        assert "baseline screenshot diff" in str(exc)
        assert "pip install 'tianluo[e2e]'" in str(exc)

    def test_routes_like_an_environment_error(self):
        # A missing package is a host problem, not a code defect: handlers that
        # already branch on E2EEnvironmentError must catch it without a second
        # case, or a missing extra would be sent into the fix loop.
        exc = E2EDependencyMissingError("Pillow")

        assert isinstance(exc, E2EEnvironmentError)
        assert not isinstance(exc, E2EScenarioFailure)

    def test_extra_name_constant_matches_the_hint(self):
        assert E2E_EXTRA == "tianluo[e2e]"
        assert E2E_EXTRA in str(E2EDependencyMissingError("Pillow"))


class TestScenarioFailure:
    def test_is_not_an_environment_error(self):
        # The whole routing split hinges on this: a scenario failure is a code
        # defect and must land in the fix loop, never in the FAILED-with-hint
        # branch reserved for host problems.
        exc = E2EScenarioFailure("checkout scenario failed")

        assert not isinstance(exc, E2EEnvironmentError)

    def test_carries_structured_results_for_fix_context(self):
        results = [{"scenario": "login", "passed": False}]
        exc = E2EScenarioFailure("login failed", scenario="login", results=results)

        assert exc.scenario == "login"
        assert exc.results == results
        assert str(exc) == "login failed"

    def test_results_default_to_an_empty_list(self):
        assert E2EScenarioFailure("boom").results == []
