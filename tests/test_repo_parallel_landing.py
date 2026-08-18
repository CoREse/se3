"""Guards for this repository's own pytest-xdist landing.

``test.parallel: auto`` (committed in ``tianluo.yaml``) makes every flow run the
suite under xdist. That is safe only because three separate pieces stay in
agreement, and each of them is silently breakable:

* the ``tests/test_worktree*.py`` family carries a module-level
  ``xdist_group`` marker, so those modules share one worker;
* ``--dist loadgroup`` (appended by the switch) is what turns that marker into
  an actual same-worker guarantee — under the default ``load`` scheduler the
  marker is inert and the tests scatter across workers;
* ``xdist_group`` is registered in ``[tool.pytest.ini_options]``, so an
  environment WITHOUT xdist installed runs the same modules serially with no
  ``PytestUnknownMarkWarning``.

Dropping the marker off a module, or losing the ini registration, produces no
error on its own — it surfaces later as a batch of git-contention ERRORs that
read like real regressions. These tests fail loudly instead.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SERIAL_GROUP = "repo_serial"


def _serial_group_modules() -> list[Path]:
    return sorted((REPO_ROOT / "tests").glob("test_worktree*.py"))


def _module_level_xdist_group(path: Path) -> str | None:
    """Return the ``xdist_group`` name a module pins itself to, if any.

    Parsed out of the AST rather than imported: the assertion is about the
    module-level ``pytestmark`` statement being present in the source, which is
    exactly what a careless edit would drop.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets
        ):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if not (isinstance(func, ast.Attribute) and func.attr == "xdist_group"):
            continue
        for kw in call.keywords:
            if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                return kw.value.value
        if call.args and isinstance(call.args[0], ast.Constant):
            return call.args[0].value
    return None


def test_worktree_modules_are_all_in_the_serial_group():
    modules = _serial_group_modules()
    # The family is the reason the serial group exists; an empty glob would make
    # every assertion below vacuously pass.
    assert len(modules) >= 6, [p.name for p in modules]
    missing = [
        p.name for p in modules if _module_level_xdist_group(p) != SERIAL_GROUP
    ]
    assert not missing, (
        f"these modules are not pinned to the '{SERIAL_GROUP}' xdist group: {missing}"
    )


def test_xdist_group_marker_is_registered_in_pyproject():
    """Without the ini entry, an xdist-less environment warns on every module."""
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    ini = text.split("[tool.pytest.ini_options]", 1)[1]
    assert re.search(r"^\s*markers\s*=", ini, re.MULTILINE)
    assert "xdist_group" in ini


def test_committed_config_enables_parallel_tests():
    """Read the committed file directly.

    Going through ``load_config`` would resolve to whichever ``tianluo.local.yaml``
    the developer's machine happens to carry (config loading is whole-file
    select-one), which is exactly the machine-specific state this assertion must
    not depend on.
    """
    cfg = yaml.safe_load((REPO_ROOT / "tianluo.yaml").read_text(encoding="utf-8"))
    assert cfg["test"]["parallel"] == "auto"


@pytest.mark.skipif(
    subprocess.run(
        [sys.executable, "-c", "import xdist"], capture_output=True
    ).returncode
    != 0,
    reason="pytest-xdist is not installed in this environment",
)
def test_serial_group_lands_on_a_single_worker_under_loadgroup():
    """End-to-end proof, not a restatement of the marker.

    Runs two of the marked modules through a real xdist ``--dist loadgroup``
    invocation with more workers than needed and asserts every per-test line
    carries the same ``[gwN]`` prefix. Two modules are enough to prove the
    grouping crosses file boundaries while keeping the subprocess cheap.
    """
    env = dict(os.environ)
    # The framework's own test step sets this as a recursion guard; a nested
    # pytest must not inherit it.
    env.pop("SE3_TEST_RUNNING", None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_worktree_history_sidecar.py",
            "tests/test_worktree_project_exclusion.py",
            "-n",
            "4",
            "--dist",
            "loadgroup",
            "-v",
            "-p",
            "no:cacheprovider",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    workers = set(re.findall(r"^\[(gw\d+)\] .*test_worktree", result.stdout, re.M))
    assert len(workers) == 1, (
        f"serial group scattered across workers {sorted(workers)}; "
        "--dist loadgroup is not taking effect"
    )


def test_module_level_console_width_follows_the_environment(monkeypatch):
    """A rich Console must not freeze its width at import time.

    ``rich.Console.__init__`` reads ``COLUMNS`` once and pins ``_width`` from it,
    and pytest-xdist exports ``COLUMNS=80`` into every worker. A module-level
    console built under that env renders 80-column output forever, so a test that
    widens the terminal for one CLI invocation gets clipped tables and asserts
    against ``"cla…"``. ``tests/conftest.py`` drops the variable before any
    tianluo module is imported; this pins that the drop actually happened.
    """
    from tianluo.commands import history_cmd

    monkeypatch.setenv("COLUMNS", "200")
    assert history_cmd.console.width == 200
