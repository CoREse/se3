"""tianluo.e2e.assertions — the three-tier assertion ladder.

INVARIANT: the ladder is a hard rule, not a preference. A check must be made
with the *lowest* tier that can express it:

1. **Tier 1 — deterministic** (this module's ``exit_code`` / ``stdout`` /
   ``stderr`` / ``http_status`` / ``http_body`` / ``file_exists`` /
   ``file_content`` / ``dom``). The default; needs no declaration. A web check
   queries the DOM through the browser's programmatic entry point — it does not
   take a picture.
2. **Tier 2 — baseline screenshot diff** (``screenshot_diff``). Admissible only
   when the subject under test genuinely *is* a visual rendering, and only when
   the scenario says ``visual_regression: true``.
3. **Tier 3 — an LLM looks at the image** (``visual_semantic``). Last resort,
   only with ``semantic_visual: true``, and only ever admissible together with a
   reviewable evidence description.

:mod:`tianluo.e2e.config_schema` rejects a document that skips a tier, so a
violation normally fails before any container is built. The tier gates are
re-checked *here* as well, because :func:`evaluate` is also reachable from the
CLI and from callers holding a hand-built declaration that never passed through
the schema, and a silently-honoured escalation would convert deterministic
verification into probabilistic verification without leaving a trace.

Dependency isolation: tiers 1 stays stdlib-only (HTTP goes through
``urllib``, never ``requests``). Tier 2's Pillow and tier 3's ``LLMCaller`` are
imported *inside the functions that need them*, so this module is importable on
a core-only install and a missing extra surfaces as an actionable
:class:`~tianluo.e2e.errors.E2EDependencyMissingError` rather than an
``ImportError`` on an unrelated command.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from tianluo.i18n import t

from .backend import EnvironmentHandle, ExecResult, IsolationBackend
from .content_config import AssertionDecl
from .errors import E2EConfigError, E2EDependencyMissingError

logger = logging.getLogger(__name__)

__all__ = [
    "AssertionContext",
    "AssertionResult",
    "BrowserBridge",
    "ExecRecord",
    "HttpRecord",
    "TIER1_KINDS",
    "TIER2_KINDS",
    "TIER3_KINDS",
    "evaluate",
    "fetch_http",
]

TIER1_KINDS = (
    "exit_code",
    "stdout",
    "stderr",
    "http_status",
    "http_body",
    "file_exists",
    "file_content",
    "dom",
)
TIER2_KINDS = ("screenshot_diff",)
TIER3_KINDS = ("visual_semantic",)

# Marker the in-container helper programs print their JSON result behind, so a
# noisy container (a browser writing to stderr, a shell profile echoing a
# banner) cannot be mistaken for the payload.
RESULT_MARKER = "TIANLUO_E2E_JSON"

# WHY 0.0: a tier-2 baseline is generated inside the *same* image that renders
# the comparison shot, which is the whole reason the design pins Playwright's
# official image for browser scenarios — identical fonts, identical rasteriser,
# byte-identical output. Exact equality is therefore the honest default, and any
# tolerance is a deliberate concession the scenario author must write down.
DEFAULT_DIFF_THRESHOLD = 0.0

# Per-channel delta a pixel may show before it counts as different. Zero for the
# same reason as the threshold above.
DEFAULT_PIXEL_TOLERANCE = 0

_DEFAULT_HTTP_TIMEOUT = 30.0
_DEFAULT_EXEC_TIMEOUT = 60.0

# How much captured output an assertion result carries. Long enough to show the
# failure, short enough that a dozen failed assertions still fit in one step's
# fix instructions.
_ACTUAL_LIMIT = 2000


# ----------------------------------------------------------------------------
# results and execution context
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class AssertionResult:
    """Outcome of evaluating one assertion.

    ``expected`` and ``actual`` are always both populated on failure: the
    session quotes them verbatim into ``fix_instructions``, so the implementing
    agent gets "wanted X, got Y" without anybody re-deriving it from prose.

    ``evidence`` is the reviewable justification a tier-3 verdict must supply
    (and where tier 2 records its measured difference ratio); ``details`` keeps
    the machine-readable numbers for reports and the WebUI.
    """

    kind: str
    passed: bool
    tier: int = 1
    expected: str = ""
    actual: str = ""
    message: str = ""
    evidence: str = ""
    artifacts: Tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        """One-line human/LLM-facing rendering used by reports and fix text."""
        state = "PASS" if self.passed else "FAIL"
        parts = ["[{}] {}".format(state, self.kind)]
        if self.message:
            parts.append(self.message)
        if not self.passed and (self.expected or self.actual):
            parts.append("expected: {} | actual: {}".format(self.expected, self.actual))
        return " — ".join(parts)


@dataclass
class ExecRecord:
    """One command executed inside the environment during a scenario."""

    service: str
    argv: Tuple[str, ...]
    result: ExecResult


@dataclass
class HttpRecord:
    """One HTTP exchange performed during a scenario."""

    url: str
    status: int = 0
    body: str = ""
    error: str = ""
    from_service: str = ""


@dataclass
class AssertionContext:
    """Everything an assertion may read about the run so far.

    Deliberately a plain mutable record rather than a live object graph: the
    executor fills it in as it drives the scenario, and every assertion is then
    a pure function of (declaration, context). That is what makes the whole
    ladder testable against a fake backend with no container in sight.
    """

    backend: IsolationBackend
    handle: EnvironmentHandle
    driver: str
    scenario: str = ""
    project_root: Optional[Path] = None
    baselines_dir: Optional[Path] = None
    artifacts_dir: Optional[Path] = None
    # Absolute clock() value the scenario budget expires at; None = unbounded.
    deadline: Optional[float] = None
    clock: Callable[[], float] = time.monotonic
    # First-run baseline capture. Off by default: see _assert_screenshot_diff.
    write_missing_baselines: bool = False
    # Injected for tier 3 so tests never reach a real LLM.
    llm_factory: Optional[Callable[[], Any]] = None
    browser: Optional["BrowserBridge"] = None
    execs: List[ExecRecord] = field(default_factory=list)
    last_http: Optional[HttpRecord] = None
    # screenshot action name -> host path
    screenshots: Dict[str, Path] = field(default_factory=dict)
    # screenshot name -> path *inside* the container, for shots the browser took
    # itself (Playwright writes into its own filesystem, and those images must be
    # copied out rather than re-captured, which would produce a different shot).
    remote_screenshots: Dict[str, str] = field(default_factory=dict)
    artifacts: List[Path] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def last_exec(self) -> Optional[ExecRecord]:
        return self.execs[-1] if self.execs else None

    def remaining(self) -> Optional[float]:
        """Seconds left in the scenario budget, or ``None`` when unbounded."""
        if self.deadline is None:
            return None
        return self.deadline - self.clock()

    def timeout_for(self, default: float) -> float:
        """Clamp ``default`` to whatever is left of the scenario budget."""
        remaining = self.remaining()
        if remaining is None:
            return default
        # Floor of 1s: a sub-second budget would make every call time out
        # instantly and report a timeout for the *call* rather than for the
        # scenario, hiding which one actually ran out.
        return max(min(default, remaining), 1.0)

    def record_exec(self, service: str, argv: Sequence[str], result: ExecResult) -> ExecRecord:
        record = ExecRecord(service=service, argv=tuple(str(a) for a in argv), result=result)
        self.execs.append(record)
        return record

    def add_artifact(self, path: Path) -> None:
        if path not in self.artifacts:
            self.artifacts.append(Path(path))


# ----------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------


def _clip(text: Any, limit: int = _ACTUAL_LIMIT) -> str:
    rendered = "" if text is None else str(text)
    if len(rendered) > limit:
        return rendered[:limit] + "…"
    return rendered


def _as_argv(value: Any) -> List[str]:
    """Normalise a command declaration to an argv list.

    A string is run through the container's shell rather than split here:
    scenario authors write pipelines and redirections, and a naive split would
    silently mangle them.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(part) for part in value]
    return ["sh", "-lc", str(value)]


def _match_text(
    actual: str, spec: Mapping[str, Any]
) -> Tuple[bool, str]:
    """Apply a ``matches`` / ``contains`` / ``equals`` expectation to ``actual``.

    Returns ``(passed, expected_description)``. An expectation naming none of
    the three is a schema violation, so it never reaches here through the normal
    path; the defensive branch treats it as "non-empty output expected".
    """
    if spec.get("matches") is not None:
        pattern = str(spec["matches"])
        try:
            matcher = re.compile(pattern, re.MULTILINE | re.DOTALL)
        except re.error as exc:
            raise E2EConfigError(
                t("e2e.assert.bad_pattern", pattern=pattern, detail=str(exc))
            ) from exc
        return bool(matcher.search(actual)), "matches /{}/".format(pattern)
    if spec.get("contains") is not None:
        needle = str(spec["contains"])
        return needle in actual, "contains {!r}".format(needle)
    if spec.get("equals") is not None:
        expected = str(spec["equals"])
        return actual == expected, "equals {!r}".format(expected)
    return bool(actual.strip()), "non-empty"


def _numeric(value: Any, default: float) -> float:
    """A YAML number, or ``default``. Booleans are rejected, not coerced —
    ``threshold: true`` is a typo, and reading it as 1.0 would silently accept
    any image at all."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return float(default)
    return float(value)


def _tier_of(kind: str) -> int:
    if kind in TIER2_KINDS:
        return 2
    if kind in TIER3_KINDS:
        return 3
    return 1


# ----------------------------------------------------------------------------
# HTTP (tier 1) — stdlib only
# ----------------------------------------------------------------------------

# Stdlib fetch executed *inside* a container. Written as a python3 program
# because that is the one interpreter every base image the design sanctions
# already carries (python:*-slim, the Playwright images, tianluo's GUI recipe) —
# curl is not guaranteed, and installing it would mean an extra build layer for
# every project just to make an assertion work.
_IN_CONTAINER_FETCH = "\n".join(
    (
        "import json, sys, urllib.error, urllib.request",
        "url, timeout = sys.argv[1], float(sys.argv[2])",
        "out = {}",
        "try:",
        "    with urllib.request.urlopen(url, timeout=timeout) as response:",
        "        out['status'] = int(getattr(response, 'status', 0)"
        " or response.getcode() or 0)",
        "        out['body'] = response.read().decode('utf-8', 'replace')",
        "except urllib.error.HTTPError as exc:",
        "    out['status'] = int(exc.code)",
        "    out['body'] = exc.read().decode('utf-8', 'replace')",
        "except Exception as exc:",
        "    out['error'] = str(exc)",
        "print('{} ' + json.dumps(out))".format(RESULT_MARKER),
    )
)


def _parse_marked_json(stdout: str) -> Optional[Dict[str, Any]]:
    """Pull the marker-prefixed JSON payload out of captured stdout."""
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not line.startswith(RESULT_MARKER):
            continue
        payload = line[len(RESULT_MARKER):].strip()
        try:
            data = json.loads(payload)
        except ValueError:
            logger.debug("unparseable e2e helper payload: %r", payload[:200])
            return None
        return data if isinstance(data, dict) else None
    return None


def fetch_http(
    url: str,
    *,
    ctx: Optional[AssertionContext] = None,
    from_service: Optional[str] = None,
    timeout: Optional[float] = None,
) -> HttpRecord:
    """Perform one GET and describe the answer.

    WHY two paths: by default the request leaves from the *host* and therefore
    targets a port the service publishes — the same rule the readiness layer's
    ``http`` probe follows, so one URL style works for both. A URL naming a
    peer *inside* the shared network is unreachable from the host, so declaring
    ``from: <service>`` routes the identical stdlib fetch through that
    container instead. Never ``requests``: e2e must add no third-party
    dependency to the core install for something the stdlib does.
    """
    budget = timeout if timeout is not None else _DEFAULT_HTTP_TIMEOUT
    if ctx is not None:
        budget = ctx.timeout_for(budget)

    if from_service:
        if ctx is None:
            raise E2EConfigError(t("e2e.assert.http_needs_context", url=url))
        result = ctx.backend.exec(
            ctx.handle,
            from_service,
            ["python3", "-c", _IN_CONTAINER_FETCH, url, str(budget)],
            timeout=budget,
        )
        ctx.record_exec(from_service, ["python3", "-c", "<http fetch>", url], result)
        payload = _parse_marked_json(result.stdout)
        if payload is None:
            return HttpRecord(
                url=url,
                error=t(
                    "e2e.assert.http_in_container_failed",
                    service=from_service,
                    detail=_clip(result.stderr or result.stdout, 400) or "-",
                ),
                from_service=from_service,
            )
        return HttpRecord(
            url=url,
            status=int(payload.get("status") or 0),
            body=str(payload.get("body") or ""),
            error=str(payload.get("error") or ""),
            from_service=from_service,
        )

    try:
        with urllib.request.urlopen(url, timeout=budget) as response:
            status = int(getattr(response, "status", 0) or response.getcode() or 0)
            body = response.read().decode("utf-8", "replace")
        return HttpRecord(url=url, status=status, body=body)
    except urllib.error.HTTPError as exc:
        # A 4xx/5xx is a perfectly ordinary *observation*, not a transport
        # failure: an assertion may well be checking for exactly that status.
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:  # pragma: no cover - body already consumed
            body = ""
        return HttpRecord(url=url, status=int(exc.code), body=body)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return HttpRecord(url=url, error=str(exc))


# ----------------------------------------------------------------------------
# browser bridge (tier-1 DOM access, and the browser action driver)
# ----------------------------------------------------------------------------

# The Playwright program the driver container runs. One program per scenario:
# `backend.exec` is one-shot, so page state cannot survive between calls, and
# re-driving the prefix of a UI flow for every single query would re-submit
# forms. Batching the whole flow plus every DOM query into one program keeps the
# browser session alive for exactly as long as the scenario needs it, executes
# each user action exactly once, and still leaves the backend interface at five
# verbs (no "session" verb had to be invented for it).
_BROWSER_PROGRAM = """
const {{ chromium }} = require('playwright');
const PROGRAM = {program};
(async () => {{
  const out = {{ ops: [], queries: [], error: '' }};
  let browser = null;
  try {{
    browser = await chromium.launch();
    const page = await browser.newPage();
    for (const step of PROGRAM.ops) {{
      const entry = {{ op: step.op, ok: true, error: '' }};
      try {{
        if (step.op === 'goto') {{
          await page.goto(step.url, {{ waitUntil: step.wait_until || 'load' }});
        }} else if (step.op === 'click') {{
          await page.click(step.selector);
        }} else if (step.op === 'fill') {{
          await page.fill(step.selector, String(step.value == null ? '' : step.value));
        }} else if (step.op === 'press') {{
          await page.press(step.selector || 'body', step.key);
        }} else if (step.op === 'select') {{
          await page.selectOption(step.selector, String(step.value));
        }} else if (step.op === 'wait_for') {{
          await page.waitForSelector(step.selector, {{ state: step.state || 'visible' }});
        }} else if (step.op === 'wait') {{
          await page.waitForTimeout(Number(step.seconds || 0) * 1000);
        }} else if (step.op === 'screenshot') {{
          await page.screenshot({{ path: step.path, fullPage: !!step.full_page }});
        }} else {{
          entry.ok = false;
          entry.error = 'unsupported browser op: ' + step.op;
        }}
      }} catch (err) {{
        entry.ok = false;
        entry.error = String((err && err.message) || err);
      }}
      out.ops.push(entry);
    }}
    for (const query of PROGRAM.queries) {{
      const entry = {{ selector: query.selector, count: 0, text: '', error: '' }};
      try {{
        if (query.url) {{
          await page.goto(query.url, {{ waitUntil: query.wait_until || 'load' }});
        }}
        const locator = page.locator(query.selector);
        entry.count = await locator.count();
        if (entry.count > 0) {{
          entry.text = (await locator.first().innerText().catch(() => '')) || '';
          if (query.attribute) {{
            entry.attribute =
              await locator.first().getAttribute(query.attribute).catch(() => null);
          }}
        }}
      }} catch (err) {{
        entry.error = String((err && err.message) || err);
      }}
      out.queries.push(entry);
    }}
  }} catch (err) {{
    out.error = String((err && err.message) || err);
  }} finally {{
    if (browser) {{ await browser.close().catch(() => {{}}); }}
  }}
  process.stdout.write('{marker} ' + JSON.stringify(out) + '\\n');
}})();
"""


class BrowserBridge:
    """Programmatic (never coordinate-based) access to the driver's browser.

    INVARIANT: this is the tier-1 entry point for web scenarios — actions are
    driven through selectors and assertions read the DOM. Taking a picture and
    reasoning about it is tier 2/3 and lives elsewhere in this module; a browser
    scenario that can express its check as a selector query must do so here.
    """

    def __init__(
        self,
        backend: IsolationBackend,
        handle: EnvironmentHandle,
        service: str,
        *,
        node_binary: str = "node",
        timeout: Optional[float] = None,
    ) -> None:
        self.backend = backend
        self.handle = handle
        self.service = service
        self.node_binary = node_binary
        self.timeout = timeout
        self.ops: List[Dict[str, Any]] = []
        self.queries: List[Dict[str, Any]] = []
        self.op_results: List[Dict[str, Any]] = []
        self.query_results: List[Dict[str, Any]] = []
        self.error: str = ""
        self.ran = False
        self.exec_result: Optional[ExecResult] = None

    # -- program assembly -------------------------------------------------

    def add_op(self, op: str, params: Mapping[str, Any]) -> int:
        entry: Dict[str, Any] = {"op": op}
        entry.update({k: v for k, v in params.items() if k not in ("action", "service")})
        self.ops.append(entry)
        return len(self.ops) - 1

    def add_query(self, params: Mapping[str, Any]) -> int:
        query = {
            "selector": str(params.get("selector") or ""),
            "url": params.get("url"),
            "wait_until": params.get("wait_until"),
            "attribute": params.get("attribute"),
        }
        self.queries.append(query)
        return len(self.queries) - 1

    @property
    def pending(self) -> bool:
        return bool(self.ops or self.queries)

    def render_program(self) -> str:
        """The JS source that will be handed to ``node -e``."""
        return _BROWSER_PROGRAM.format(
            program=json.dumps({"ops": self.ops, "queries": self.queries}),
            marker=RESULT_MARKER,
        )

    # -- execution --------------------------------------------------------

    def run(self, *, timeout: Optional[float] = None) -> None:
        """Execute the accumulated program once inside the driver container."""
        if self.ran:
            return
        self.ran = True
        if not self.pending:
            return
        budget = timeout if timeout is not None else self.timeout
        result = self.backend.exec(
            self.handle,
            self.service,
            [self.node_binary, "-e", self.render_program()],
            timeout=budget,
        )
        self.exec_result = result
        payload = _parse_marked_json(result.stdout)
        if payload is None:
            self.error = t(
                "e2e.assert.browser_program_failed",
                service=self.service,
                detail=_clip(result.stderr or result.stdout, 600) or "-",
            )
            return
        self.op_results = [o for o in payload.get("ops") or [] if isinstance(o, dict)]
        self.query_results = [
            q for q in payload.get("queries") or [] if isinstance(q, dict)
        ]
        self.error = str(payload.get("error") or "")

    def observation(self, index: int) -> Optional[Dict[str, Any]]:
        """The DOM query result registered at ``index``, if the program ran."""
        if 0 <= index < len(self.query_results):
            return self.query_results[index]
        return None

    def failed_ops(self) -> List[Dict[str, Any]]:
        return [entry for entry in self.op_results if not entry.get("ok", True)]


# ----------------------------------------------------------------------------
# tier 1 — deterministic assertions
# ----------------------------------------------------------------------------


def _assert_exit_code(decl: AssertionDecl, ctx: AssertionContext) -> AssertionResult:
    expected = decl.get("equals", 0)
    try:
        expected_code = int(expected)
    except (TypeError, ValueError):
        raise E2EConfigError(
            t("e2e.assert.bad_exit_code", value=repr(expected))
        ) from None
    record = ctx.last_exec
    if record is None:
        return AssertionResult(
            kind="exit_code",
            passed=False,
            expected="exit_code == {}".format(expected_code),
            actual=t("e2e.assert.no_exec"),
            message=t("e2e.assert.no_exec"),
        )
    actual = record.result.exit_code
    return AssertionResult(
        kind="exit_code",
        passed=actual == expected_code and not record.result.timed_out,
        expected="exit_code == {}".format(expected_code),
        actual="exit_code == {}{}".format(
            actual, " (timed out)" if record.result.timed_out else ""
        ),
        details={
            "command": " ".join(record.argv),
            "service": record.service,
            "timed_out": record.result.timed_out,
        },
    )


def _assert_stream(
    decl: AssertionDecl, ctx: AssertionContext, stream: str
) -> AssertionResult:
    record = ctx.last_exec
    if record is None:
        return AssertionResult(
            kind=stream,
            passed=False,
            expected=_match_text("", decl.params)[1],
            actual=t("e2e.assert.no_exec"),
            message=t("e2e.assert.no_exec"),
        )
    text = record.result.stdout if stream == "stdout" else record.result.stderr
    passed, expected = _match_text(text, decl.params)
    return AssertionResult(
        kind=stream,
        passed=passed,
        expected="{} {}".format(stream, expected),
        actual=_clip(text) or "(empty)",
        details={"command": " ".join(record.argv), "service": record.service},
    )


def _http_record_for(decl: AssertionDecl, ctx: AssertionContext) -> HttpRecord:
    url = str(decl.get("url") or "")
    from_service = decl.get("from")
    record = fetch_http(
        url,
        ctx=ctx,
        from_service=str(from_service) if from_service else None,
        timeout=decl.get("timeout"),
    )
    ctx.last_http = record
    return record


def _assert_http_status(decl: AssertionDecl, ctx: AssertionContext) -> AssertionResult:
    record = _http_record_for(decl, ctx)
    expected_status = decl.get("equals", 200)
    try:
        expected_code = int(expected_status)
    except (TypeError, ValueError):
        raise E2EConfigError(
            t("e2e.assert.bad_status", value=repr(expected_status))
        ) from None
    if record.error:
        return AssertionResult(
            kind="http_status",
            passed=False,
            expected="GET {} -> {}".format(record.url, expected_code),
            actual=record.error,
            message=t("e2e.assert.http_unreachable", url=record.url),
            details={"url": record.url},
        )
    return AssertionResult(
        kind="http_status",
        passed=record.status == expected_code,
        expected="GET {} -> {}".format(record.url, expected_code),
        actual="GET {} -> {}".format(record.url, record.status),
        details={"url": record.url, "status": record.status},
    )


def _assert_http_body(decl: AssertionDecl, ctx: AssertionContext) -> AssertionResult:
    record = _http_record_for(decl, ctx)
    if record.error:
        return AssertionResult(
            kind="http_body",
            passed=False,
            expected="GET {} body {}".format(
                record.url, _match_text("", decl.params)[1]
            ),
            actual=record.error,
            message=t("e2e.assert.http_unreachable", url=record.url),
            details={"url": record.url},
        )

    subject = record.body
    json_path = decl.get("json_path")
    if json_path:
        # A JSON field assertion stays tier 1: it reads the API's own
        # structured answer instead of regexing the rendered page.
        found, subject = _read_json_path(record.body, str(json_path))
        if not found:
            return AssertionResult(
                kind="http_body",
                passed=False,
                expected="{} present in GET {}".format(json_path, record.url),
                actual=_clip(record.body, 400) or "(empty body)",
                message=t("e2e.assert.json_path_missing", path=json_path),
                details={"url": record.url, "json_path": json_path},
            )

    passed, expected = _match_text(subject, decl.params)
    label = "GET {} {}".format(record.url, json_path or "body")
    return AssertionResult(
        kind="http_body",
        passed=passed,
        expected="{} {}".format(label, expected),
        actual=_clip(subject) or "(empty)",
        details={"url": record.url, "status": record.status, "json_path": json_path},
    )


def _read_json_path(body: str, path: str) -> Tuple[bool, str]:
    """Resolve a dotted/indexed path inside a JSON body.

    Deliberately tiny (dots and ``[n]`` only): a fuller query language would be
    a third-party dependency, and a scenario needing more than this is better
    served by an ``exec`` action that does the extraction in the container.
    """
    try:
        current: Any = json.loads(body or "")
    except ValueError:
        return False, ""
    for raw_part in path.replace("[", ".[").split("."):
        part = raw_part.strip()
        if not part:
            continue
        if part.startswith("[") and part.endswith("]"):
            try:
                index = int(part[1:-1])
            except ValueError:
                return False, ""
            if not isinstance(current, (list, tuple)) or not (
                -len(current) <= index < len(current)
            ):
                return False, ""
            current = current[index]
            continue
        if not isinstance(current, Mapping) or part not in current:
            return False, ""
        current = current[part]
    if isinstance(current, (dict, list)):
        return True, json.dumps(current, sort_keys=True)
    if isinstance(current, bool):
        return True, "true" if current else "false"
    return True, "" if current is None else str(current)


def _assert_file(
    decl: AssertionDecl, ctx: AssertionContext, *, content: bool
) -> AssertionResult:
    kind = "file_content" if content else "file_exists"
    target = str(decl.get("path") or "")
    service = str(decl.get("service") or ctx.driver)
    timeout = ctx.timeout_for(float(decl.get("timeout") or _DEFAULT_EXEC_TIMEOUT))

    if not content:
        probe = ctx.backend.exec(
            ctx.handle, service, ["test", "-e", target], timeout=timeout
        )
        exists = bool(getattr(probe, "ok", False))
        want_absent = bool(decl.get("absent", False))
        return AssertionResult(
            kind=kind,
            passed=(not exists) if want_absent else exists,
            expected="{} {} in {}".format(
                target, "absent" if want_absent else "exists", service
            ),
            actual="{} {} in {}".format(
                target, "absent" if not exists else "exists", service
            ),
            details={"path": target, "service": service},
        )

    read = ctx.backend.exec(ctx.handle, service, ["cat", target], timeout=timeout)
    if not getattr(read, "ok", False):
        return AssertionResult(
            kind=kind,
            passed=False,
            expected="{} readable in {}".format(target, service),
            actual=_clip(read.stderr or read.stdout, 400) or "(unreadable)",
            message=t("e2e.assert.file_unreadable", path=target, service=service),
            details={"path": target, "service": service},
        )
    passed, expected = _match_text(read.stdout, decl.params)
    return AssertionResult(
        kind=kind,
        passed=passed,
        expected="{} {}".format(target, expected),
        actual=_clip(read.stdout) or "(empty)",
        details={"path": target, "service": service},
    )


def _assert_dom(
    decl: AssertionDecl,
    ctx: AssertionContext,
    observation: Optional[Mapping[str, Any]] = None,
) -> AssertionResult:
    """Selector query against the driver's live DOM — tier 1 for web scenarios.

    WHY no screenshot here: whatever the DOM can answer must be answered by the
    DOM. Rendering the page to an image and comparing pictures is a *higher*
    tier and is admissible only for checks whose subject genuinely is the
    visual result.
    """
    selector = str(decl.get("selector") or "")
    if observation is None:
        bridge = ctx.browser
        if bridge is None:
            raise E2EConfigError(
                t("e2e.assert.dom_no_driver", selector=selector, scenario=ctx.scenario)
            )
        # Lazy single-query run: the common pure-DOM scenario declares no
        # browser actions at all, so nobody has driven the program yet.
        index = bridge.add_query(decl.params)
        bridge.run(timeout=ctx.timeout_for(_DEFAULT_EXEC_TIMEOUT))
        observation = bridge.observation(index)
        if observation is None:
            return AssertionResult(
                kind="dom",
                passed=False,
                expected="{} queried".format(selector),
                actual=bridge.error or t("e2e.assert.dom_no_observation"),
                message=bridge.error or t("e2e.assert.dom_no_observation"),
                details={"selector": selector},
            )

    error = str(observation.get("error") or "")
    count = int(observation.get("count") or 0)
    text = str(observation.get("text") or "")
    details = {
        "selector": selector,
        "count": count,
        "attribute": observation.get("attribute"),
    }

    if error:
        return AssertionResult(
            kind="dom",
            passed=False,
            expected="{} queryable".format(selector),
            actual=error,
            message=t("e2e.assert.dom_query_error", selector=selector),
            details=details,
        )

    if decl.get("count") is not None:
        try:
            wanted = int(decl.get("count"))
        except (TypeError, ValueError):
            raise E2EConfigError(
                t("e2e.assert.bad_count", value=repr(decl.get("count")))
            ) from None
        return AssertionResult(
            kind="dom",
            passed=count == wanted,
            expected="{} matches {} element(s)".format(selector, wanted),
            actual="{} matches {} element(s)".format(selector, count),
            details=details,
        )

    if bool(decl.get("absent", False)):
        return AssertionResult(
            kind="dom",
            passed=count == 0,
            expected="{} absent".format(selector),
            actual="{} matches {} element(s)".format(selector, count),
            details=details,
        )

    if count == 0:
        return AssertionResult(
            kind="dom",
            passed=False,
            expected="{} present".format(selector),
            actual="{} matches no element".format(selector),
            details=details,
        )

    attribute = decl.get("attribute")
    if attribute is not None:
        subject = observation.get("attribute")
        subject_text = "" if subject is None else str(subject)
        passed, expected = _match_text(subject_text, decl.params)
        return AssertionResult(
            kind="dom",
            passed=passed,
            expected="{}[{}] {}".format(selector, attribute, expected),
            actual=_clip(subject_text) or "(absent)",
            details=details,
        )

    if any(decl.get(key) is not None for key in ("matches", "contains", "equals")):
        passed, expected = _match_text(text, decl.params)
        return AssertionResult(
            kind="dom",
            passed=passed,
            expected="{} text {}".format(selector, expected),
            actual=_clip(text) or "(empty)",
            details=details,
        )

    return AssertionResult(
        kind="dom",
        passed=True,
        expected="{} present".format(selector),
        actual="{} matches {} element(s)".format(selector, count),
        details=details,
    )


# ----------------------------------------------------------------------------
# tier 2 — baseline screenshot diff
# ----------------------------------------------------------------------------


def _resolve_screenshot(decl: AssertionDecl, ctx: AssertionContext) -> Path:
    """Host path of the image this assertion compares.

    Three sources, in order of preference:

    1. a shot a ``screenshot`` action already pulled onto the host — reused
       as-is, because capturing a second time would compare a *different* moment
       than the one the scenario's actions set up;
    2. an image the browser wrote inside its own container (a Playwright
       ``screenshot`` op) — copied out as a plain file;
    3. otherwise capture now through the backend's screenshot verb, which is the
       GUI/Xvfb path (``scrot`` against the virtual display).
    """
    named = str(decl.get("screenshot") or decl.get("name") or "")
    if named and named in ctx.screenshots:
        return ctx.screenshots[named]

    service = str(decl.get("service") or ctx.driver)
    destination = None
    if ctx.artifacts_dir is not None:
        stem = named or str(decl.get("baseline") or "screenshot")
        destination = Path(ctx.artifacts_dir) / "{}-{}".format(
            _safe_stem(ctx.scenario), Path(stem).name
        )
        if destination.suffix.lower() != ".png":
            destination = destination.with_suffix(".png")

    remote = decl.get("remote") or (ctx.remote_screenshots.get(named) if named else None)
    if remote:
        snapshot = ctx.backend.snapshot(
            ctx.handle, service, str(remote), kind="file", destination=destination
        )
    else:
        snapshot = ctx.backend.snapshot(
            ctx.handle,
            service,
            str(decl.get("target") or ""),
            kind="screenshot",
            destination=destination,
        )
    ctx.add_artifact(snapshot.path)
    if named:
        ctx.screenshots[named] = snapshot.path
    return snapshot.path


def _safe_stem(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", text or "scenario").strip("-")
    return cleaned or "scenario"


def _assert_screenshot_diff(
    decl: AssertionDecl, ctx: AssertionContext
) -> AssertionResult:
    """Tier 2: deterministic pixel comparison against a git-tracked baseline.

    WHY the baseline must be produced inside the same image: fonts, hinting and
    the rasteriser all live in the image, so a baseline captured anywhere else
    (a developer's laptop, a different base tag) differs from the comparison
    shot for reasons that have nothing to do with the code under test. That is
    the reason browser scenarios pin Playwright's official image, and the reason
    a missing baseline is *not* silently invented here by default.
    """
    if not decl.visual_regression:
        # Defensive twin of the schema rule: a hand-built declaration that
        # reached this function without the opt-in must not quietly escalate.
        raise E2EConfigError(
            t("e2e.assert.tier2_undeclared", scenario=ctx.scenario)
        )

    baseline_name = str(decl.get("baseline") or "")
    if not baseline_name:
        raise E2EConfigError(t("e2e.assert.baseline_unnamed", scenario=ctx.scenario))
    baselines = Path(ctx.baselines_dir) if ctx.baselines_dir else None
    baseline_path = (baselines / baseline_name) if baselines else Path(baseline_name)

    actual_path = _resolve_screenshot(decl, ctx)

    if not baseline_path.is_file():
        if ctx.write_missing_baselines:
            # Explicitly requested first capture: write it and still report the
            # assertion as *not passed*, because nobody has looked at the image
            # yet. Reporting a pass would let a wrong rendering become the
            # reference simply by being the first one produced.
            baseline_path.parent.mkdir(parents=True, exist_ok=True)
            baseline_path.write_bytes(Path(actual_path).read_bytes())
            ctx.add_artifact(baseline_path)
            return AssertionResult(
                kind="screenshot_diff",
                passed=False,
                tier=2,
                expected="baseline {}".format(baseline_name),
                actual=str(baseline_path),
                message=t(
                    "e2e.assert.baseline_created",
                    baseline=baseline_name,
                    path=str(baseline_path),
                ),
                artifacts=(str(actual_path), str(baseline_path)),
                details={"baseline_created": True, "baseline": baseline_name},
            )
        return AssertionResult(
            kind="screenshot_diff",
            passed=False,
            tier=2,
            expected="baseline {}".format(baseline_name),
            actual=t("e2e.assert.baseline_absent", path=str(baseline_path)),
            message=t(
                "e2e.assert.baseline_missing_hint",
                baseline=baseline_name,
                directory=str(baselines or "-"),
            ),
            artifacts=(str(actual_path),),
            details={"baseline": baseline_name},
        )

    threshold = _numeric(decl.get("threshold"), DEFAULT_DIFF_THRESHOLD)
    tolerance = int(_numeric(decl.get("pixel_tolerance"), DEFAULT_PIXEL_TOLERANCE))

    diff = compare_images(baseline_path, Path(actual_path), pixel_tolerance=tolerance)
    if diff.get("size_mismatch"):
        return AssertionResult(
            kind="screenshot_diff",
            passed=False,
            tier=2,
            expected="{} at {}".format(baseline_name, diff["baseline_size"]),
            actual="{} at {}".format(Path(actual_path).name, diff["actual_size"]),
            message=t(
                "e2e.assert.size_mismatch",
                baseline=diff["baseline_size"],
                actual=diff["actual_size"],
            ),
            artifacts=(str(actual_path), str(baseline_path)),
            details=diff,
        )

    ratio = float(diff["ratio"])
    passed = ratio <= threshold
    return AssertionResult(
        kind="screenshot_diff",
        passed=passed,
        tier=2,
        expected="pixel difference <= {:.4%} vs {}".format(threshold, baseline_name),
        actual="pixel difference {:.4%} ({} of {} pixels)".format(
            ratio, diff["differing"], diff["total"]
        ),
        message="" if passed else t(
            "e2e.assert.diff_exceeds",
            baseline=baseline_name,
            ratio="{:.4%}".format(ratio),
            threshold="{:.4%}".format(threshold),
        ),
        evidence="pixel difference {:.4%} against {}".format(ratio, baseline_name),
        artifacts=(str(actual_path), str(baseline_path)),
        details=dict(diff, threshold=threshold, baseline=baseline_name),
    )


def compare_images(
    baseline: Path, actual: Path, *, pixel_tolerance: int = DEFAULT_PIXEL_TOLERANCE
) -> Dict[str, Any]:
    """Compare two images and report how much of them differs.

    Returns ``{"ratio", "differing", "total", "size_mismatch", ...}``. The ratio
    is what a report quotes, so it is computed in pixels rather than as an
    opaque score.
    """
    Image, ImageChops = _load_pillow()

    with Image.open(baseline) as baseline_image, Image.open(actual) as actual_image:
        base = baseline_image.convert("RGB")
        shot = actual_image.convert("RGB")
        if base.size != shot.size:
            return {
                "size_mismatch": True,
                "baseline_size": "{}x{}".format(*base.size),
                "actual_size": "{}x{}".format(*shot.size),
                "ratio": 1.0,
                "differing": 0,
                "total": base.size[0] * base.size[1],
            }

        # Per-channel thresholding then union: converting the difference to
        # luminance first would round a small single-channel shift down to zero
        # and silently pass a real colour regression.
        mask = None
        for band in ImageChops.difference(base, shot).split():
            thresholded = band.point(
                lambda value, limit=pixel_tolerance: 255 if value > limit else 0
            )
            mask = thresholded if mask is None else ImageChops.lighter(mask, thresholded)

        total = base.size[0] * base.size[1]
        differing = int(mask.histogram()[255]) if mask is not None else 0

    return {
        "size_mismatch": False,
        "baseline_size": "{}x{}".format(*base.size),
        "actual_size": "{}x{}".format(*shot.size),
        "differing": differing,
        "total": total,
        "ratio": (differing / total) if total else 0.0,
        "pixel_tolerance": pixel_tolerance,
    }


def _load_pillow():
    """Import Pillow at call time, or explain how to install it.

    WHY inside the function: Pillow is the only third-party dependency the e2e
    subsystem needs, so it is the whole content of the ``tianluo[e2e]`` extra.
    A module-level import would make every core-only install of tianluo — the
    overwhelming majority, since e2e is off by default — fail to import the CLI.
    """
    try:
        from PIL import Image, ImageChops  # type: ignore[import-not-found]
    except ImportError as exc:
        raise E2EDependencyMissingError(
            "Pillow", feature=t("e2e.assert.feature.visual_regression")
        ) from exc
    return Image, ImageChops


# ----------------------------------------------------------------------------
# tier 3 — LLM semantic visual assertion
# ----------------------------------------------------------------------------

_SEMANTIC_PROMPT = """You are checking one e2e assertion by looking at a screenshot.

Screenshot: {image}
Scenario: {scenario}
Question to answer: {question}

Answer strictly about what is visible in the image. Do not guess about
behaviour you cannot see.

Return JSON:
{{"verdict": "pass" | "fail",
  "evidence": "<what you actually see that justifies the verdict — name the
  concrete visible elements, text and their positions>",
  "confidence": "high" | "medium" | "low"}}

The evidence field is mandatory and must describe observable detail a human
reviewer could check against the same image. A verdict without such evidence is
not admissible and will be treated as a failure."""


def _assert_visual_semantic(
    decl: AssertionDecl, ctx: AssertionContext
) -> AssertionResult:
    """Tier 3: an LLM inspects the screenshot. The floor, not the default.

    WHY this exists at all: a desktop GUI with no programmatic entry point can
    have properties ("the chart is legible", "the layout is not overlapping")
    that neither a DOM query nor a fixed baseline can express — a baseline diff
    demands a *known* correct rendering, which a brand-new screen does not have.

    WHY it is gated and evidence-bound: an LLM verdict is probabilistic, so
    admitting it as an ordinary assertion would quietly convert the whole suite
    from verification into opinion. Hence three locks: the scenario must declare
    ``semantic_visual``, the verdict is worthless without a reviewable evidence
    description (missing evidence fails the assertion no matter what the verdict
    says), and the schema refuses the escalation when the declaration itself
    proves a selector or text check would have done the job.
    """
    if not decl.semantic_visual:
        raise E2EConfigError(t("e2e.assert.tier3_undeclared", scenario=ctx.scenario))

    question = str(decl.get("question") or "")
    if not question:
        raise E2EConfigError(t("e2e.assert.question_missing", scenario=ctx.scenario))

    image_path = _resolve_screenshot(decl, ctx)
    response = _call_semantic_llm(ctx, question, Path(image_path))
    if response is None:
        return AssertionResult(
            kind="visual_semantic",
            passed=False,
            tier=3,
            expected=question,
            actual=t("e2e.assert.llm_unavailable"),
            message=t("e2e.assert.llm_unavailable"),
            artifacts=(str(image_path),),
        )

    verdict = str(response.get("verdict") or "").strip().lower()
    evidence = str(response.get("evidence") or "").strip()
    # INVARIANT: no evidence, no pass. `require_evidence` is mandatory in the
    # schema, so an answer without a checkable description is a failed
    # assertion rather than an unverifiable pass.
    if not evidence:
        return AssertionResult(
            kind="visual_semantic",
            passed=False,
            tier=3,
            expected=question,
            actual=t("e2e.assert.evidence_absent", verdict=verdict or "-"),
            message=t("e2e.assert.evidence_required"),
            artifacts=(str(image_path),),
            details={"verdict": verdict, "image": str(image_path)},
        )

    passed = verdict in ("pass", "true", "yes")
    return AssertionResult(
        kind="visual_semantic",
        passed=passed,
        tier=3,
        expected=question,
        actual="verdict={} — {}".format(verdict or "?", _clip(evidence, 600)),
        message="" if passed else t("e2e.assert.semantic_failed", question=question),
        evidence=evidence,
        artifacts=(str(image_path),),
        details={
            "verdict": verdict,
            "confidence": str(response.get("confidence") or ""),
            "image": str(image_path),
        },
    )


def _call_semantic_llm(
    ctx: AssertionContext, question: str, image: Path
) -> Optional[Dict[str, Any]]:
    """Ask the configured LLM about ``image``; ``None`` when it cannot answer.

    WHY the import is inside: :mod:`tianluo.e2e` sits *below* the engine in the
    dependency order (the engine's step handler imports e2e, not the reverse). A
    module-level ``from tianluo.engine...`` would close that loop and make the
    whole e2e package unimportable from the CLI.
    """
    try:
        if ctx.llm_factory is not None:
            caller = ctx.llm_factory()
        else:
            from tianluo.engine.llm_caller import LLMCaller

            caller = LLMCaller(ctx.project_root or Path.cwd())
        prompt = _SEMANTIC_PROMPT.format(
            image=str(image), scenario=ctx.scenario or "-", question=question
        )
        response = caller.call(
            prompt=prompt,
            context_files=[Path(image)],
            json_mode="two_phase",
            json_schema_hint='{"verdict": "pass|fail", "evidence": "...", '
            '"confidence": "high|medium|low"}',
            required_keys=["verdict", "evidence"],
        )
    except Exception as exc:
        logger.warning("tier-3 visual assertion could not reach the LLM: %s", exc)
        return None

    if isinstance(response, Mapping):
        return dict(response)

    from tianluo.engine.utils.json_parser import parse_json_response

    parsed = parse_json_response(str(response or ""), required_keys=["verdict"])
    if parsed is None:
        logger.warning("tier-3 visual assertion got an unparseable LLM answer")
        return None
    return parsed


# ----------------------------------------------------------------------------
# dispatch
# ----------------------------------------------------------------------------


def evaluate(
    decl: AssertionDecl,
    ctx: AssertionContext,
    *,
    observation: Optional[Mapping[str, Any]] = None,
) -> AssertionResult:
    """Evaluate one assertion declaration against the run's context.

    ``observation`` lets the executor hand in a DOM query result it already
    collected as part of the scenario's single browser program; without it a
    ``dom`` assertion runs its own one-query program.
    """
    kind = (decl.kind or "").strip()

    if kind == "exit_code":
        return _assert_exit_code(decl, ctx)
    if kind in ("stdout", "stderr"):
        return _assert_stream(decl, ctx, kind)
    if kind == "http_status":
        return _assert_http_status(decl, ctx)
    if kind == "http_body":
        return _assert_http_body(decl, ctx)
    if kind == "file_exists":
        return _assert_file(decl, ctx, content=False)
    if kind == "file_content":
        return _assert_file(decl, ctx, content=True)
    if kind == "dom":
        return _assert_dom(decl, ctx, observation)
    if kind in TIER2_KINDS:
        return _assert_screenshot_diff(decl, ctx)
    if kind in TIER3_KINDS:
        return _assert_visual_semantic(decl, ctx)

    raise E2EConfigError(
        t(
            "e2e.assert.unknown_kind",
            kind=decl.kind,
            known=", ".join(TIER1_KINDS + TIER2_KINDS + TIER3_KINDS),
        )
    )


def tier_of(decl: AssertionDecl) -> int:
    """Assertion tier of ``decl`` (1 deterministic, 2 baseline diff, 3 LLM)."""
    return _tier_of((decl.kind or "").strip())
