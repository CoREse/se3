"""Hard guard: every annotation in ``src/tianluo/`` must reference resolvable names.

Sits alongside ``tests/test_import_isolation.py`` as a project-level hard
constraint on the shipped package rather than a test of any single feature.

INVARIANT: this guard must never use "the module imported successfully" as its
pass condition. The defect it exists to catch (``config.py`` annotating a field
as ``Dict[...]`` while only ``dict`` was importable) crashed ``import
tianluo.config`` — and therefore the whole ``tianluo-server`` entry point — on
the deployment machine's Python 3.12, yet was completely invisible on the
development machine's Python 3.14, where PEP 649 defers annotation evaluation
until something actually asks for it. A guard keyed on import success would be
permanently green here and would have caught nothing. Both layers below
therefore *force* annotation evaluation instead of waiting for an import to
blow up:

* the AST layer parses sources without importing them, so it also sees
  annotations that no runtime object ever exposes (function-local
  ``AnnAssign``, which never lands in any ``__annotations__``) and modules
  behind an optional extra that a core-only install cannot import at all;
* the runtime layer evaluates real ``__annotations__`` in real module
  namespaces, catching names the AST layer forgives because the file binds them
  *somewhere* — a name bound only under ``if TYPE_CHECKING:`` and then used
  unquoted in a module without ``from __future__ import annotations`` is
  present in the source and still a NameError at import time.

Neither layer subsumes the other; the two defects of the original crash split
one to each.

A third layer checks the same annotations against the *declared* Python floor:
an annotation that is only syntax-valid above ``requires-python`` raises just as
hard on the oldest supported interpreter as an unresolvable name does, and is
equally invisible on a newer development machine.
"""

from __future__ import annotations

import ast
import builtins
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
# WHY: the worktree's src/ must win over any installed distribution — an
# installed (stale) `tianluo` would let this guard pass against code that is not
# the code under test. Same convention as tests/test_import_isolation.py.
_SRC_DIR = _REPO_ROOT / "src"
_PACKAGE_DIR = _SRC_DIR / "tianluo"

_BUILTIN_NAMES = frozenset(dir(builtins))

# Third-party distributions that live behind an optional extra. A core-only
# install legitimately cannot import the modules that need them, so a
# ModuleNotFoundError naming one of these is a skip.
# WHY: the skip list is a *whitelist of missing dependency names*, never a bare
# `except ImportError: pass`. Swallowing every import failure would silently
# excuse exactly the breakage this file exists to catch — a module that no
# longer imports would read as "nothing to check here" instead of a failure.
_OPTIONAL_EXTRA_DEPS = frozenset(
    {
        # tianluo[server]
        "fastapi",
        "uvicorn",
        "starlette",
        "websockets",
        "argon2",
        "argon2_cffi",
        # tianluo[e2e]
        "PIL",
        # tianluo[browser]
        "playwright",
    }
)


def _ast_nodes(*names: str) -> tuple:
    return tuple(cls for cls in (getattr(ast, n, None) for n in names) if cls)


_MATCH_CAPTURE_NODES = _ast_nodes("MatchAs", "MatchStar")
_MATCH_MAPPING_NODES = _ast_nodes("MatchMapping")
# PEP 695 type parameters carry a plain `str` name; `ast.TypeAlias` is
# deliberately absent — its `.name` is a Store-context `ast.Name`, already
# picked up by the generic branch above.
_TYPE_PARAM_NODES = _ast_nodes("TypeVar", "ParamSpec", "TypeVarTuple")


def _python_files() -> list[Path]:
    return sorted(p for p in _PACKAGE_DIR.rglob("*.py"))


def _declared_python_floor() -> tuple[int, int]:
    """The ``(major, minor)`` lower bound declared in ``requires-python``.

    Read with a regex rather than ``tomllib`` so the guard itself keeps working
    on the floor it polices — ``tomllib`` only exists from 3.11.
    """
    text = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    spec = re.search(r'^requires-python\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert spec, "pyproject.toml declares no requires-python"
    floor = re.search(r">=\s*(\d+)\.(\d+)", spec.group(1))
    assert floor, f"unsupported requires-python form: {spec.group(1)!r}"
    return int(floor.group(1)), int(floor.group(2))


def _subprocess_env() -> dict:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(_SRC_DIR) + (os.pathsep + existing if existing else "")
    return env


# --------------------------------------------------------------------------
# Layer 1 — AST: annotation names vs. names bound anywhere in the same file
# --------------------------------------------------------------------------


def _bound_names(tree: ast.AST) -> set[str]:
    """Every name the file binds anywhere, as a deliberately over-broad set.

    WHY: over-approximating the bindings keeps this guard free of false
    positives (a red light on unrelated work is a guard people delete or route
    around), at the cost of missing scope-level errors it was never meant to
    police. A genuinely missing import — the defect class here — is absent from
    the file at *every* scope, so the loose set still catches it.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name.split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
        # Pattern-matching and PEP 695 type-parameter nodes only exist on newer
        # grammars; look them up by name so this guard still runs on 3.9.
        elif isinstance(node, _MATCH_CAPTURE_NODES) and node.name:
            names.add(node.name)
        elif isinstance(node, _MATCH_MAPPING_NODES) and node.rest:
            names.add(node.rest)
        elif isinstance(node, _TYPE_PARAM_NODES):
            names.add(node.name)
    return names


def _annotation_expressions(tree: ast.AST) -> list[ast.expr]:
    """Every annotation expression in the file, at any nesting depth.

    WHY: function-local ``AnnAssign`` is included on purpose — such annotations
    never reach any ``__annotations__`` mapping, so the runtime layer is
    structurally blind to them, and one of the two original defects lived there.
    """
    found: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            found.append(node.annotation)
        elif isinstance(node, ast.arg) and node.annotation is not None:
            found.append(node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.returns is not None:
                found.append(node.returns)
    return found


def _referenced_names(expr: ast.expr) -> list[tuple[int, str]]:
    """``(lineno, name)`` for every name an annotation expression looks up.

    Dotted paths contribute only their leftmost name (that is the only one
    resolved against the module namespace). String literals are skipped
    wholesale: inside ``Literal["auto"]`` they are data, not names, telling the
    two apart is guesswork, and neither case can raise at import time anyway —
    Python never evaluates a quoted annotation on its own.
    """
    return [
        (node.lineno, node.id)
        for node in ast.walk(expr)
        if isinstance(node, ast.Name)
    ]


def test_ast_annotations_reference_only_bound_names() -> None:
    """No annotation may name something the file never binds and builtins lack."""
    findings: list[str] = []
    files = _python_files()
    assert files, f"no Python sources found under {_PACKAGE_DIR}"

    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        defined = _bound_names(tree) | _BUILTIN_NAMES
        for annotation in _annotation_expressions(tree):
            for lineno, name in _referenced_names(annotation):
                if name not in defined:
                    rel = path.relative_to(_REPO_ROOT)
                    findings.append(f"{rel}:{lineno}: undefined name {name}")

    assert not findings, (
        "annotations reference names that are never defined in their module "
        "(evaluating them raises NameError on any Python without PEP 649 lazy "
        "annotations, i.e. everything before 3.14):\n  "
        + "\n  ".join(sorted(set(findings)))
    )


# --------------------------------------------------------------------------
# Layer 1b — AST: annotation syntax vs. the declared ``requires-python`` floor
# --------------------------------------------------------------------------

# Builtin containers became subscriptable in 3.9 (PEP 585); `X | Y` became a
# type expression in 3.10 (PEP 604). Both are ordinary expressions evaluated at
# import time, so on an older interpreter they raise TypeError rather than
# failing to parse — invisible to any check that only imports on a new runtime.
_PEP585_GENERICS = frozenset(
    {"list", "dict", "set", "frozenset", "tuple", "type"}
)


def _eagerly_evaluated_annotations(tree: ast.Module) -> list[ast.expr]:
    """Annotations Python actually evaluates, for a module without PEP 563.

    WHY: function-*local* ``AnnAssign`` is excluded here — the opposite of the
    layer above. Those annotations are never evaluated, so version-incompatible
    syntax inside one cannot break an import; flagging it would be a false
    positive. Signature annotations stay in scope at every nesting depth,
    because a nested ``def`` evaluates them as soon as its enclosing function
    runs.
    """
    found: list[ast.expr] = []

    def visit(node: ast.AST, in_function: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = child.args
                slots = [
                    *getattr(args, "posonlyargs", []),
                    *args.args,
                    *args.kwonlyargs,
                    args.vararg,
                    args.kwarg,
                ]
                found.extend(
                    arg.annotation
                    for arg in slots
                    if arg is not None and arg.annotation is not None
                )
                if child.returns is not None:
                    found.append(child.returns)
                visit(child, True)
            elif isinstance(child, ast.ClassDef):
                # A class body runs at definition time, so its annotations are
                # evaluated even when the class is nested inside a function.
                visit(child, False)
            else:
                if isinstance(child, ast.AnnAssign) and not in_function:
                    found.append(child.annotation)
                visit(child, in_function)

    visit(tree, False)
    return found


def _required_version(expr: ast.expr) -> list[tuple[int, tuple[int, int], str]]:
    """``(lineno, minimum_version, construct)`` for each version-gated node."""
    gated: list[tuple[int, tuple[int, int], str]] = []
    for node in ast.walk(expr):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            gated.append((node.lineno, (3, 10), "PEP 604 union (X | Y)"))
        elif isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            if node.value.id in _PEP585_GENERICS:
                gated.append(
                    (
                        node.lineno,
                        (3, 9),
                        f"PEP 585 builtin generic ({node.value.id}[...])",
                    )
                )
    return gated


def test_annotations_are_evaluable_on_declared_python_floor() -> None:
    """No evaluated annotation may need a newer Python than we advertise.

    WHY: ``requires-python`` is what pip trusts when deciding an install is
    allowed. A floor below what the annotations really need turns into an
    install that succeeds and then raises TypeError on first import — the same
    "declaration diverges from reality" defect as the NameError above, one
    layer up.
    """
    floor = _declared_python_floor()
    findings: list[str] = []

    for path in _python_files():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        # PEP 563 turns every annotation into a string, so no annotation in
        # such a module is ever evaluated and none can raise.
        if any(
            isinstance(node, ast.ImportFrom)
            and node.module == "__future__"
            and any(alias.name == "annotations" for alias in node.names)
            for node in tree.body
        ):
            continue
        for annotation in _eagerly_evaluated_annotations(tree):
            for lineno, needed, construct in _required_version(annotation):
                if needed > floor:
                    rel = path.relative_to(_REPO_ROOT)
                    findings.append(
                        f"{rel}:{lineno}: {construct} needs Python "
                        f"{needed[0]}.{needed[1]}"
                    )

    assert not findings, (
        "annotations are evaluated at import time but require a newer Python "
        f"than the declared requires-python floor {floor[0]}.{floor[1]} — fix "
        "by adding `from __future__ import annotations` to the module, using "
        "the typing spelling, or raising the declared floor:\n  "
        + "\n  ".join(sorted(set(findings)))
    )


# --------------------------------------------------------------------------
# Layer 2 — runtime: force real ``__annotations__`` evaluation
# --------------------------------------------------------------------------

# Runs in a fresh interpreter: importing ~190 modules into the pytest session
# would leak import side effects into every later test in the run.
_RUNTIME_PROBE = '''
import importlib
import inspect
import json
import sys
from pathlib import Path

package_dir = Path(sys.argv[1])
optional_deps = set(json.loads(sys.argv[2]))

def module_name(path):
    rel = path.relative_to(package_dir.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)

def force(obj, label, errors):
    # WHY: eval_str stays False on purpose. The default already forces every
    # annotation Python itself would evaluate at import time — on 3.14 it
    # resolves the PEP 649 deferred ones and raises here exactly where <=3.13
    # raises during class creation — while leaving quoted forward references as
    # strings. eval_str=True would additionally resolve those quotes, which
    # nothing at runtime ever does, flagging the deliberate
    # `if TYPE_CHECKING:` + `"Name"` idiom as a defect.
    try:
        # inspect.get_annotations landed in 3.10; on 3.9 the attribute is
        # already eagerly evaluated, so touching it is the same forcing.
        getter = getattr(inspect, "get_annotations", None)
        if getter is not None:
            getter(obj)
        else:
            getattr(obj, "__annotations__", None)
    except NameError as exc:
        errors.append("%s: %s" % (label, exc))
    except Exception:
        # Only unresolvable names are in scope for this guard; anything else
        # (odd descriptors, exotic __annotations__) is not a NameError defect.
        pass

errors = []
skipped = []
failed = []

for path in sorted(package_dir.rglob("*.py")):
    name = module_name(path)
    try:
        module = importlib.import_module(name)
    except ModuleNotFoundError as exc:
        missing = (exc.name or "").split(".")[0]
        if missing in optional_deps:
            skipped.append("%s (needs %s)" % (name, missing))
            continue
        failed.append("%s: %r" % (name, exc))
        continue
    except NameError as exc:
        # Before 3.14 the annotation blows up during import itself, so this is
        # the very defect under guard rather than a generic import failure.
        errors.append("%s (raised at import): %s" % (name, exc))
        continue
    except Exception as exc:
        failed.append("%s: %r" % (name, exc))
        continue

    force(module, name, errors)
    for attr, value in list(vars(module).items()):
        if getattr(value, "__module__", None) != name:
            continue
        if inspect.isclass(value):
            force(value, "%s.%s" % (name, attr), errors)
            for sub_name, sub in list(vars(value).items()):
                target = sub
                if isinstance(target, (staticmethod, classmethod)):
                    target = target.__func__
                elif isinstance(target, property):
                    target = target.fget
                if inspect.isfunction(target):
                    force(target, "%s.%s.%s" % (name, attr, sub_name), errors)
        elif inspect.isfunction(value):
            force(value, "%s.%s" % (name, attr), errors)

print(json.dumps({"errors": errors, "skipped": skipped, "failed": failed}))
'''


def test_runtime_annotations_evaluate_without_nameerror() -> None:
    """Real ``__annotations__`` must evaluate — not merely parse — cleanly."""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(_RUNTIME_PROBE),
            str(_PACKAGE_DIR),
            json.dumps(sorted(_OPTIONAL_EXTRA_DEPS)),
        ],
        capture_output=True,
        text=True,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0, (
        f"annotation probe crashed:\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )
    report = json.loads(proc.stdout.strip().splitlines()[-1])

    assert not report["failed"], (
        "modules failed to import for reasons unrelated to a missing optional "
        "extra — treated as a failure rather than a skip so that a genuinely "
        "broken module cannot pass this guard by being unimportable:\n  "
        + "\n  ".join(report["failed"])
    )
    assert not report["errors"], (
        "evaluating annotations raised NameError:\n  "
        + "\n  ".join(report["errors"])
    )


def test_runtime_layer_actually_covered_core_modules() -> None:
    """The runtime layer must not degenerate into skipping everything.

    WHY: a skip list that quietly grew to cover the whole package would leave
    this file green while checking nothing — the same silent-pass failure mode
    the guard was written against.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(_RUNTIME_PROBE),
            str(_PACKAGE_DIR),
            json.dumps(sorted(_OPTIONAL_EXTRA_DEPS)),
        ],
        capture_output=True,
        text=True,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    total = len(_python_files())
    skipped = len(report["skipped"])
    assert skipped < total // 2, (
        f"{skipped}/{total} modules were skipped; the runtime layer is no "
        "longer meaningfully covering the package"
    )


@pytest.mark.parametrize("module", ["tianluo.config", "tianluo.cli"])
def test_hot_path_module_annotations_evaluate(module: str) -> None:
    """Spot-check the modules on the ``tianluo-server`` start-up path.

    ``server/app.py`` reaches ``load_server_config`` through ``tianluo.config``;
    a NameError in that module's annotations takes the whole entry point down.
    """
    code = f"""
        import importlib, inspect

        # WHY: same version-safe accessor as _RUNTIME_PROBE.force() — this guard
        # polices a 3.9 floor, and inspect.get_annotations only exists from 3.10;
        # on 3.9 annotations are already eager, so touching the attribute forces
        # them just as well.
        getter = getattr(inspect, "get_annotations", None)
        if getter is None:
            getter = lambda obj: getattr(obj, "__annotations__", None)

        m = importlib.import_module({module!r})
        getter(m)
        for value in list(vars(m).values()):
            if inspect.isclass(value) and value.__module__ == {module!r}:
                getter(value)
        print("OK")
    """
    proc = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "OK" in proc.stdout
