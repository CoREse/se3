"""SE3 configuration management."""

import functools
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Literal, Optional
from dataclasses import dataclass, field
from enum import Enum
import yaml

logger = logging.getLogger(__name__)


PROJECT_CONFIG_FILENAME = "se3.yaml"
PROJECT_LOCAL_CONFIG_FILENAME = "se3.local.yaml"


def _resolve_main_repo_root(project_root: Path) -> Optional[Path]:
    """Detect whether ``project_root`` is inside a git worktree and, if so,
    return the main repository's working-tree root.

    Uses the git-recommended heuristic: compare ``--git-common-dir``
    against ``--git-dir``.  When they differ the repo is a worktree and
    ``--git-common-dir`` points inside the main repo's ``.git``
    directory.  We derive the main working tree root from the common
    dir's parent, and then ask git for ``--show-toplevel`` to be safe.

    Returns ``None`` when:
    - ``project_root`` is not inside a git repository,
    - the repository is not a worktree (common-dir == git-dir),
    - git is not installed / not available, or
    - any subprocess or parsing error occurs.

    Caching: results are memoized per *resolved* ``project_root`` via
    :func:`functools.lru_cache` on the inner
    ``_resolve_main_repo_root_cached`` (size 64). The outer wrapper
    resolves the argument to an absolute path *before* the cache lookup,
    so the cache key is stable across cwd changes and relative-path
    invocations from different directories.

    Long-lived processes that mutate worktree topology (loop mode
    adding/removing per-iteration worktrees, daemons, IDE integrations,
    test sessions) MUST call :func:`clear_main_repo_root_cache` (or
    ``_resolve_main_repo_root.cache_clear()``) after such changes;
    otherwise a path that has switched between "plain repo" and
    "worktree" states would observe a stale answer.
    """
    # Resolve to absolute path so the cache key is stable across cwd
    # changes and relative-path invocations from different directories.
    resolved = Path(project_root).resolve()
    return _resolve_main_repo_root_cached(resolved)


@functools.lru_cache(maxsize=64)
def _resolve_main_repo_root_cached(project_root: Path) -> Optional[Path]:
    """Actual git-probe implementation — cached on the resolved absolute path."""
    # Enforce the documented contract: the public wrapper always passes a
    # resolved absolute path, but this module-level symbol is importable by
    # third-party callers who may bypass the wrapper.  An assertion here
    # documents and defends the contract rather than silently misbehaving.
    assert project_root.is_absolute(), (
        f"_resolve_main_repo_root_cached expects an absolute path, got {project_root!r}"
    )
    # Sanitize the environment so that inherited GIT_DIR / GIT_WORK_TREE /
    # GIT_COMMON_DIR do not override the -C flag and cause the probe to
    # report the env-var-pinned repo instead of the on-disk one.
    _clean_env = {**os.environ}
    for _env_key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
        _clean_env.pop(_env_key, None)
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--git-common-dir", "--git-dir"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=_clean_env,
        )
        if result.returncode != 0:
            return None
        lines = [line.strip() for line in result.stdout.strip().splitlines() if line.strip()]
        # Safety: a worktree path with an embedded newline would produce more
        # than two lines here; we conservatively return None rather than risk
        # misparsing. This is a safe fallback (treats as non-worktree).
        if len(lines) != 2:
            return None
        common_dir, git_dir = lines
        # Normalize relative paths (git may emit relative to project_root)
        common_dir = str(Path(project_root) / common_dir) if not Path(common_dir).is_absolute() else common_dir
        git_dir = str(Path(project_root) / git_dir) if not Path(git_dir).is_absolute() else git_dir
        if Path(common_dir).resolve() == Path(git_dir).resolve():
            return None
        # Derive main repo working tree root from common-dir parent
        candidate = Path(common_dir).parent
        # Safety: confirm with git --show-toplevel from the candidate.
        # If this fails the candidate is not a valid working tree (e.g.
        # bare-repo-backed worktree or corrupt layout), so return None
        # rather than probing the wrong directory.
        toplevel_result = subprocess.run(
            ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=_clean_env,
        )
        if toplevel_result.returncode == 0:
            toplevel = toplevel_result.stdout.strip()
            if toplevel:
                return Path(toplevel).resolve()
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


# Expose cache_clear on the public wrapper name so tests and callers
# that invoke ``_resolve_main_repo_root.cache_clear()`` continue to work.
_resolve_main_repo_root.cache_clear = _resolve_main_repo_root_cached.cache_clear  # type: ignore[attr-defined]


def clear_main_repo_root_cache() -> None:
    """Invalidate the cached results of :func:`_resolve_main_repo_root`.

    Call this after any operation that may flip a path between "plain
    git repo" and "worktree-of-some-other-repo" states (loop mode
    adding/removing per-iteration worktrees, IDE integrations, test
    fixtures), so the next ``get_project_config_path`` lookup resolves
    the current topology rather than a stale snapshot.
    """
    _resolve_main_repo_root_cached.cache_clear()


def get_project_config_path(project_root: Path) -> Path:
    """Return the active project config path for ``project_root``.

    When ``project_root`` is inside a git worktree, a four-tier lookup is
    used so that the main repository's ``se3.local.yaml`` can override
    the worktree's tracked ``se3.yaml`` (since ``se3.local.yaml`` is
    gitignored and does not travel into worktrees):

    1. ``<worktree>/se3.local.yaml``  (highest)
    2. ``<main_repo>/se3.local.yaml``
    3. ``<worktree>/se3.yaml``
    4. ``<main_repo>/se3.yaml``       (lowest)

    The first existing regular file wins (``is_file()`` follows symlinks).
    If none of the four exist, the canonical ``<worktree>/se3.yaml`` is
    returned so callers know which file would be read.

    For non-worktree projects (regular git repo or not under version
    control) the old two-tier logic is preserved:
    ``<project_root>/se3.local.yaml`` wins over ``se3.yaml``.

    Using ``is_file()`` rather than ``exists()`` means a stray directory
    or dangling symlink at ``se3.local.yaml`` does not silently shadow
    the committed ``se3.yaml`` and trigger a misleading "malformed local
    file" warning downstream — only a real file participates in the
    override.

    Symlinks that resolve to a regular file are treated as real files
    (``is_file()`` follows symlinks). A layout such as
    ``se3.local.yaml -> ../shared-overrides.yaml`` is therefore picked
    up as the active override, which is the intended behaviour for users
    who share local overrides between clones via a symlink. If you want
    the committed ``se3.yaml`` to win, remove or rename the symlink.

    TOCTOU note: there is a theoretical window between this ``is_file()``
    probe and the subsequent ``_read_yaml`` open inside the caller — if
    ``se3.local.yaml`` is deleted in between, readers observe the file
    as absent and fall back to built-in defaults rather than reading
    ``se3.yaml``. The failure mode is safe (defaults) and vanishingly
    rare in practice; eliminating the window would require passing an
    already-opened file handle through the loader stack and is not
    worth the churn unless the race is actually observed.

    Relative-path fields inside the selected config (e.g.
    ``version.file_path`` or ``test.command``) are resolved by
    downstream callers against ``project_root`` (the worktree root when
    running inside a worktree), NOT against the directory that contains
    the config file. Keep this in mind when editing a main-repo
    ``se3.local.yaml`` that is read from inside a worktree: a relative
    path written there is interpreted relative to the running worktree.
    """
    root = Path(project_root)
    local = root / PROJECT_LOCAL_CONFIG_FILENAME
    if local.is_file():
        return local

    main_repo = _resolve_main_repo_root(root)
    if main_repo is not None:
        candidates = [
            main_repo / PROJECT_LOCAL_CONFIG_FILENAME,
            root / PROJECT_CONFIG_FILENAME,
            main_repo / PROJECT_CONFIG_FILENAME,
        ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate

    return root / PROJECT_CONFIG_FILENAME


# Filenames that mark a directory as an SE3 project root. Used by
# parent-walk detection in the CLI commands so that `se3.local.yaml`,
# `se3.yaml`, or the legacy `se3.config.yaml` all count as markers.
_PROJECT_ROOT_MARKERS = (
    PROJECT_LOCAL_CONFIG_FILENAME,
    PROJECT_CONFIG_FILENAME,
    "se3.config.yaml",
)


def is_se3_project_root(path: Path) -> bool:
    """Return True when ``path`` contains any recognised SE3 project marker.

    Recognised markers: ``se3.local.yaml``, ``se3.yaml``, or the legacy
    ``se3.config.yaml``. The check is non-recursive — only files directly
    under ``path`` are considered.

    Uses ``is_file()`` (not ``exists()``) to stay symmetrical with
    :func:`get_project_config_path`: a stray directory or dangling
    symlink at one of those paths would NOT be loaded as config, so it
    must not be treated as a project marker either. Otherwise the
    parent-walk commands would resolve the folder as a project root but
    every config loader would silently fall back to built-in defaults,
    leaving the user to debug a phantom "empty config" state.
    """
    p = Path(path)
    return any((p / marker).is_file() for marker in _PROJECT_ROOT_MARKERS)


# Dedup set keyed by absolute path string. The dedup is one-shot per
# (process, path): if a long-running process (daemon, test session, IDE
# integration) sees the user fix se3.local.yaml and then later
# reintroduce a typo at the same path, the second breakage will NOT
# re-warn — the path is already in this set. Tests reset the set
# explicitly between cases. Restart the process for a fresh warning.
_warned_malformed_local_for: set[str] = set()


def _maybe_warn_local_shadow(config_path: Path) -> None:
    """Warn (one-shot per path) when ``se3.local.yaml`` is unreadable.

    A malformed local override silently shadows the committed ``se3.yaml``,
    so without this warning the loaders would just fall back to built-in
    defaults and the user would never see why their project config stopped
    taking effect. Only fires for ``se3.local.yaml``.

    Dedup is per-process, per-path — see ``_warned_malformed_local_for``.
    """
    if config_path.name != PROJECT_LOCAL_CONFIG_FILENAME:
        return
    try:
        key = str(config_path.resolve())
    except OSError:
        key = str(config_path)
    if key in _warned_malformed_local_for:
        return
    _warned_malformed_local_for.add(key)
    yaml_path = config_path.parent / PROJECT_CONFIG_FILENAME
    logger.warning(
        "%s is unreadable or malformed and is shadowing %s — project "
        "configuration is falling back to built-in defaults until the "
        "local file is fixed or removed.",
        config_path, yaml_path,
    )


def _config_source_label(config_path: Path, project_root: Path) -> str:
    """Return a source label for ``config_path``.

    When the config comes from a different directory than ``project_root``
    (e.g., main repo in worktree mode), include the directory name so
    warning messages point to the right file.

    The returned label is for human display only (appears in log/warning
    messages).  It is NOT a stable machine-parseable identifier — callers
    that need deduplication should use ``_dedup_source_key`` instead.
    """
    try:
        if config_path.parent.resolve() != Path(project_root).resolve():
            return f"{config_path.parent.name}/{config_path.name}"
    except OSError:
        pass
    return config_path.name


def load_project_yaml(project_root: Path) -> tuple[dict, str]:
    """Read the active project YAML config tolerantly.

    Thin wrapper over :func:`_read_yaml` that resolves the active project
    config path (``se3.local.yaml`` when present, otherwise ``se3.yaml``)
    and returns ``(data, source_label)`` where ``data`` is an empty dict
    when the file is missing, empty, malformed, or non-mapping. Never
    raises. Malformed/non-mapping/parse-error classification and the
    local-shadow warning are handled inside ``_read_yaml``.

    This is a public API: cross-module readers (engine/context_builder,
    engine/steps/verify_spec, internal loaders) all route through this
    single entry point to pick up ``se3.local.yaml`` precedence
    uniformly. Keep the signature stable.

    ``source_label`` semantics: always the filename (or directory-prefixed
    filename when from a different directory) that
    :func:`get_project_config_path` *chose* (``se3.local.yaml`` when
    present as a regular file, otherwise ``se3.yaml``) — **not**
    necessarily the file that was successfully read. When
    ``se3.local.yaml`` exists but is unparsable, ``data`` is ``{}`` and
    ``source_label`` still names ``se3.local.yaml``. This is intentional:
    error messages and deprecation warnings should point at the file
    the user placed (and therefore needs to fix), not at the fallback
    committed file which is innocent. Callers that log ``source_label``
    in "Failed to load … from %s" messages get the correct target even
    when the file could not be read.
    """
    config_path = get_project_config_path(project_root)
    source_label = _config_source_label(config_path, project_root)
    # None here means the file is absent or unusable (parse error,
    # non-mapping top level, or I/O failure). Either way we fall back
    # to built-in defaults; the malformed-local warning (if applicable)
    # has already been emitted inside _read_yaml.
    return _read_yaml(config_path) or {}, source_label


# Back-compat alias: older code imported ``_load_project_yaml`` as a
# module-private helper. Keep the alias so any external caller continues
# to work, but prefer the public name in new code.
_load_project_yaml = load_project_yaml


# Canonical dedup token shared by ``se3.yaml`` and ``se3.local.yaml``.
# Warning dedup sets are keyed on ``source_label``, which flips between
# those two filenames depending on which file is active for the current
# call. Without canonicalization, a long-running process that sees the
# user toggle the local file on/off (delete/recreate) could emit the
# same warning once per label. Collapse both project-level labels to a
# single token so a given config mistake warns at most once regardless
# of which file is currently shadowing the other.
_PROJECT_DEDUP_TOKEN = "<project>"


def _dedup_source_key(source_label: str) -> str:
    """Return the dedup token for a config source label.

    Project-level labels (``se3.yaml`` / ``se3.local.yaml``) collapse
    to a single token; any other label (e.g. ``~/.se3/config.yaml``)
    passes through unchanged.

    In worktree mode ``_config_source_label`` may emit a directory-
    prefixed label such as ``main_repo/se3.local.yaml`` when the
    selected config lives outside the worktree. Match by basename so
    those prefixed forms collapse to the same project token —
    otherwise a deprecated key surfaced under one label and then again
    under the prefixed form (e.g. after the user adds a main-repo
    local override) would warn twice in the same process.
    """
    basename = source_label.rsplit("/", 1)[-1]
    if basename in (PROJECT_CONFIG_FILENAME, PROJECT_LOCAL_CONFIG_FILENAME):
        return _PROJECT_DEDUP_TOKEN
    return source_label


class BumpType(Enum):
    """Version bump types following Semantic Versioning."""
    MAJOR = "major"
    MINOR = "minor"
    PATCH = "patch"
    NONE = "none"


@dataclass
class VersionConfig:
    """Version management configuration.
    
    Loads version-related settings from se3.yaml with sensible defaults.
    """
    # Whether automatic version bumping is enabled
    enabled: bool = True
    
    # Version file location (relative to project root)
    version_file: Optional[str] = None  # Auto-detect if None

    # Auto-confirmation settings
    # If True, version bump is applied automatically without human confirmation
    auto_bump: bool = True
    
    # Confidence threshold for requiring human confirmation
    # None = no threshold (even "low" confidence is auto-confirmed)
    # "medium" = require confirmation for "low" confidence
    # "high" = require confirmation for "medium" or "low" confidence
    confidence_threshold: Optional[str] = None
    
    # Pre-release configuration
    prerelease_prefix: str = ""
    prerelease_number: int = 0
    
    # Template definitions
    templates: dict[str, str] = field(default_factory=lambda: {
        "readme_badge": "![Version](https://img.shields.io/badge/version-{version}-blue)",
        "versions_entry": "## {version} - {date}\n\n{changes}\n",
    })
    
    # README update settings
    readme_enabled: bool = True
    readme_marker: str = "<!-- SE3-VERSION -->"
    
    # VERSIONS.md settings  
    versions_enabled: bool = True
    versions_file: str = "VERSIONS.md"
    versions_header: str = "# Version History\n\n"
    
    # Whether to include version in commit message
    include_in_commit_message: bool = True

    # Version script interface
    script_path: Optional[str] = None  # Path to version script (None = default se3/scripts/version.py)
    auto_generate_script: bool = True  # Auto-generate script via LLM if not found
    
    @property
    def file_path(self) -> Optional[str]:
        """Alias for version_file (compatibility with version_bumper.VersionConfig)."""
        return self.version_file
    
    @classmethod
    def from_dict(cls, data: dict) -> "VersionConfig":
        """Create VersionConfig from dictionary (typically loaded from se3.yaml)."""
        if not data:
            return cls()

        # Extract version section if nested
        version_data = data.get("version", data)

        # Deprecation: bump_rules and smart_version_analysis were removed when the
        # version decision model collapsed to a single authoritative suggested_version
        # field (see se3-versioning spec). Old configs are accepted but ignored.
        for deprecated_field in ("bump_rules", "smart_version_analysis"):
            if deprecated_field in version_data:
                logger.warning(
                    "se3.yaml version.%s is deprecated and ignored; remove it from "
                    "your config (version decisions are now driven by version_analyze's "
                    "suggested_version, optionally guided by se3/version-rules.md).",
                    deprecated_field,
                )

        # Build templates from config or use defaults
        templates = cls().templates.copy()
        templates.update(version_data.get("templates", {}))

        return cls(
            enabled=version_data.get("enabled", True),
            version_file=version_data.get("version_file"),
            auto_bump=version_data.get("auto_bump", True),
            confidence_threshold=version_data.get("confidence_threshold", None),
            prerelease_prefix=version_data.get("prerelease_prefix", ""),
            prerelease_number=version_data.get("prerelease_number", 0),
            templates=templates,
            readme_enabled=version_data.get("readme_enabled", True),
            readme_marker=version_data.get("readme_marker", "<!-- SE3-VERSION -->"),
            versions_enabled=version_data.get("versions_enabled", True),
            versions_file=version_data.get("versions_file", "VERSIONS.md"),
            versions_header=version_data.get("versions_header", "# Version History\n\n"),
            include_in_commit_message=version_data.get("include_in_commit_message", True),
            script_path=version_data.get("script_path"),
            auto_generate_script=version_data.get("auto_generate_script", True),
        )
    
    @classmethod
    def load(cls, project_root: Path) -> "VersionConfig":
        """Load version configuration from the active project YAML."""
        data, _src = load_project_yaml(project_root)
        if not data:
            return cls()
        return cls.from_dict(data)
    
    def get_template(self, name: str) -> str:
        """Get a template by name.
        
        Args:
            name: Template name (e.g., "readme_badge", "versions_entry")
            
        Returns:
            The template string, or empty string if not found
        """
        return self.templates.get(name, "")
    
    def should_update_readme(self) -> bool:
        """Check if README.md should be updated."""
        return self.readme_enabled
    
    def should_update_versions(self) -> bool:
        """Check if VERSIONS.md should be updated."""
        return self.versions_enabled


@dataclass
class DocsConfig:
    """Documentation auto-update configuration.

    Loads the optional ``documentation:`` section of the active project
    YAML. This section lets a project override the templates that
    :class:`se3.engine.docs_updater.DocumentationUpdater` uses when it is
    wired into the commit pipeline, WITHOUT touching the legacy
    ``version.templates`` block (which keeps its own behavior).

    Supported keys (all optional, all natural ``DocumentationUpdater``
    config keys so :meth:`to_updater_config` can forward them verbatim):

    - ``readme_badge_template``
    - ``versions_entry_template``
    - ``readme_header_template``

    Absent / non-string values are dropped so that
    :meth:`to_updater_config` only ever surfaces real overrides and an
    empty ``documentation:`` section yields ``{}`` (the updater then
    keeps its built-in defaults).
    """

    readme_badge_template: Optional[str] = None
    versions_entry_template: Optional[str] = None
    readme_header_template: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict) -> "DocsConfig":
        """Create a DocsConfig from a loaded project-YAML mapping.

        Reads the nested ``documentation:`` section. A missing or
        non-mapping section yields an all-``None`` config; non-string
        values for any key are ignored (left as ``None``).
        """
        if not data:
            return cls()
        section = data.get("documentation", {})
        if not isinstance(section, dict):
            return cls()

        def _str_or_none(key: str) -> Optional[str]:
            val = section.get(key)
            return val if isinstance(val, str) else None

        return cls(
            readme_badge_template=_str_or_none("readme_badge_template"),
            versions_entry_template=_str_or_none("versions_entry_template"),
            readme_header_template=_str_or_none("readme_header_template"),
        )

    @classmethod
    def load(cls, project_root: Path) -> "DocsConfig":
        """Load documentation configuration from the active project YAML.

        Uses :func:`load_project_yaml`, which applies the worktree-aware
        ``se3.local.yaml`` → ``se3.yaml`` lookup, so no new file-discovery
        logic is introduced here.
        """
        data, _src = load_project_yaml(project_root)
        return cls.from_dict(data)

    def to_updater_config(self) -> dict:
        """Return only the set (non-``None``) keys as a plain dict.

        The returned mapping is directly consumable as the ``config``
        argument of :class:`DocumentationUpdater`. When no overrides are
        configured the result is ``{}``.
        """
        result: dict = {}
        if self.readme_badge_template is not None:
            result["readme_badge_template"] = self.readme_badge_template
        if self.versions_entry_template is not None:
            result["versions_entry_template"] = self.versions_entry_template
        if self.readme_header_template is not None:
            result["readme_header_template"] = self.readme_header_template
        return result


@dataclass
class Config:
    """Main SE3 configuration.

    ``confirmation_enabled`` is always ``True`` under the new per-step
    schema (the legacy global enable/disable switch has been removed —
    only steps that appear in ``confirmation.steps`` are confirmed).
    ``confirmation_steps`` is derived from the keys of the new dict
    schema for backward compatibility with code that still reads the
    list form.
    """

    project_root: Path
    confirmation_enabled: bool = True
    confirmation_steps: list[str] = field(default_factory=list)

    def __post_init__(self):
        if isinstance(self.project_root, str):
            self.project_root = Path(self.project_root)

    @classmethod
    def load(cls, project_root: Path) -> "Config":
        """Load configuration from se3.yaml using the new per-step schema."""
        confirm = load_confirmation_config(project_root)
        return cls(
            project_root=project_root,
            confirmation_enabled=True,
            confirmation_steps=list(confirm.get("steps", {}).keys()),
        )


def load_version_config(project_root: Optional[Path] = None) -> VersionConfig:
    """Load version configuration from project.
    
    Args:
        project_root: Project root directory. If None, uses current working directory.
        
    Returns:
        VersionConfig instance with loaded or default settings.
    """
    if project_root is None:
        project_root = Path.cwd()
    
    return VersionConfig.load(project_root)


def load_docs_config(project_root: Optional[Path] = None) -> DocsConfig:
    """Load documentation auto-update configuration from project.

    Reads the optional ``documentation:`` section of the active project
    YAML (see :class:`DocsConfig`). When that section is absent the
    returned config is all-``None`` and ``to_updater_config()`` is ``{}``.

    Args:
        project_root: Project root directory. If None, uses current working directory.

    Returns:
        DocsConfig instance with loaded or default (empty) settings.
    """
    if project_root is None:
        project_root = Path.cwd()

    return DocsConfig.load(project_root)


def load_config(project_root: Optional[Path] = None) -> Config:
    """Load main SE3 configuration.
    
    Args:
        project_root: Project root directory. If None, uses current working directory.
        
    Returns:
        Config instance with loaded or default settings.
    """
    if project_root is None:
        project_root = Path.cwd()
    
    return Config.load(project_root)


_CONFIRM_DEFAULT_MAX_ITERATIONS = 3
_CONFIRM_VALID_STEP_CFG_KEYS = frozenset({"reviewer", "max_iterations"})

_warned_confirmation_enabled_for: set[str] = set()
_warned_confirmation_top_reviewer_for: set[str] = set()
_warned_confirmation_llm_reviewer_for: set[str] = set()
_warned_confirmation_steps_list_for: set[str] = set()
_warned_confirmation_unknown_fields_for: set[tuple[str, str, tuple[str, ...]]] = set()


def _parse_confirmation_step_entry(
    source_label: str, step_name: str, raw: Any,
) -> Optional[dict]:
    """Validate and normalize a single ``confirmation.steps.<step>`` entry.

    Returns ``{"reviewer": str|None, "max_iterations": int|None}``, or
    ``None`` if the entry is structurally invalid (non-dict, non-None).
    Empty/missing ``reviewer`` or ``max_iterations`` map to ``None`` so
    downstream consumers can apply their own fallback.
    """
    if raw is None:
        return {"reviewer": None, "max_iterations": None}
    if not isinstance(raw, dict):
        logger.warning(
            "%s: confirmation.steps.%s is not a mapping (got %s); "
            "ignoring this step entry",
            source_label, step_name, type(raw).__name__,
        )
        return None

    extra = tuple(sorted(k for k in raw if k not in _CONFIRM_VALID_STEP_CFG_KEYS))
    if extra:
        dedup_key = (_dedup_source_key(source_label), step_name, extra)
        if dedup_key not in _warned_confirmation_unknown_fields_for:
            _warned_confirmation_unknown_fields_for.add(dedup_key)
            logger.warning(
                "%s: confirmation.steps.%s has unknown field(s) %s; "
                "only 'reviewer' and 'max_iterations' are supported "
                "— ignoring those fields",
                source_label, step_name, list(extra),
            )

    reviewer = raw.get("reviewer")
    if reviewer is not None:
        if not (isinstance(reviewer, str) and reviewer.strip()):
            logger.warning(
                "%s: confirmation.steps.%s.reviewer must be a non-empty "
                "string (got %r); ignoring reviewer (will fall back to "
                "llm_caller.defaults)",
                source_label, step_name, reviewer,
            )
            reviewer = None

    raw_iters = raw.get("max_iterations")
    if raw_iters is None:
        max_iterations: Optional[int] = None
    else:
        try:
            iters = int(raw_iters)
        except (TypeError, ValueError):
            logger.warning(
                "%s: confirmation.steps.%s.max_iterations must be a "
                "positive integer (got %r); using default %d",
                source_label, step_name, raw_iters,
                _CONFIRM_DEFAULT_MAX_ITERATIONS,
            )
            max_iterations = None
        else:
            if iters <= 0:
                logger.warning(
                    "%s: confirmation.steps.%s.max_iterations=%d must be "
                    "positive; using default %d",
                    source_label, step_name, iters,
                    _CONFIRM_DEFAULT_MAX_ITERATIONS,
                )
                max_iterations = None
            else:
                max_iterations = iters

    return {"reviewer": reviewer, "max_iterations": max_iterations}


def _warn_deprecated_confirmation_fields(source_label: str, section: dict) -> None:
    """Emit one-shot warnings for deprecated keys under ``confirmation:``.

    These keys are silently ignored by the new schema; they do not alter
    behavior. Each (source, key) pair is warned about only once per
    process lifetime to avoid log floods when ``load_confirmation_config``
    is called per step.
    """
    dedup_key = _dedup_source_key(source_label)
    if "enabled" in section and dedup_key not in _warned_confirmation_enabled_for:
        _warned_confirmation_enabled_for.add(dedup_key)
        logger.warning(
            "%s: 'confirmation.enabled' is deprecated and ignored. Under "
            "the new schema, only steps listed in 'confirmation.steps' "
            "are confirmed — there is no global on/off switch. Remove "
            "this field.",
            source_label,
        )
    if "reviewer" in section and dedup_key not in _warned_confirmation_top_reviewer_for:
        _warned_confirmation_top_reviewer_for.add(dedup_key)
        logger.warning(
            "%s: top-level 'confirmation.reviewer' is deprecated and "
            "ignored. Set 'reviewer' per step under "
            "'confirmation.steps.<step>.reviewer'.",
            source_label,
        )
    if "llm_reviewer" in section and dedup_key not in _warned_confirmation_llm_reviewer_for:
        _warned_confirmation_llm_reviewer_for.add(dedup_key)
        logger.warning(
            "%s: 'confirmation.llm_reviewer' is deprecated and ignored. "
            "Define LLM agents under top-level 'agents:' and reference "
            "them via 'confirmation.steps.<step>.reviewer: <agent_name>'; "
            "use 'confirmation.steps.<step>.max_iterations' for the "
            "review-modify cycle limit.",
            source_label,
        )


def _merge_confirmation_steps(
    global_data: dict, project_data: dict,
    project_source_label: str = PROJECT_CONFIG_FILENAME,
) -> dict[str, dict]:
    """Merge ``confirmation.steps`` from global + project YAML.

    Shared by :func:`load_confirmation_config` and
    :func:`resolve_confirm_inputs` so schema handling (deprecated-field
    warnings, list-form legacy warning, non-dict warning, per-entry
    normalization) lives in one place.

    Returns an entry-level-merged dict ``{step_name: {"reviewer": ...,
    "max_iterations": ...}}``. Project entries override global entries
    with the same key; non-conflicting entries coexist.

    This helper does NOT perform agent-name validation — callers must
    invoke :func:`_validate_confirmation_reviewer_names` to fail-fast on
    unknown references.
    """
    sources = [
        ("~/.se3/config.yaml", global_data),
        (project_source_label, project_data),
    ]

    merged_steps: dict[str, dict] = {}

    for source_label, data in sources:
        section = data.get("confirmation")
        if section is None:
            continue
        if not isinstance(section, dict):
            logger.warning(
                "%s: 'confirmation' is not a mapping (got %s); ignoring",
                source_label, type(section).__name__,
            )
            continue

        _warn_deprecated_confirmation_fields(source_label, section)

        steps_raw = section.get("steps")
        if steps_raw is None:
            continue
        if isinstance(steps_raw, list):
            dedup_key = _dedup_source_key(source_label)
            if dedup_key not in _warned_confirmation_steps_list_for:
                _warned_confirmation_steps_list_for.add(dedup_key)
                logger.warning(
                    "%s: 'confirmation.steps' is a list (legacy form) and "
                    "is no longer supported. Convert to a dict, e.g.\n"
                    "  confirmation:\n"
                    "    steps:\n"
                    "      plan: {reviewer: human}\n"
                    "Ignoring the entire confirmation.steps section for "
                    "this source.",
                    source_label,
                )
            continue
        if not isinstance(steps_raw, dict):
            logger.warning(
                "%s: 'confirmation.steps' is not a mapping (got %s); "
                "ignoring",
                source_label, type(steps_raw).__name__,
            )
            continue

        for step_name, step_cfg in steps_raw.items():
            if not (isinstance(step_name, str) and step_name.strip()):
                logger.warning(
                    "%s: confirmation.steps key %r is not a non-empty "
                    "string; skipping",
                    source_label, step_name,
                )
                continue
            normalized = _parse_confirmation_step_entry(
                source_label, step_name, step_cfg,
            )
            if normalized is not None:
                merged_steps[step_name] = normalized

    return merged_steps


def _validate_confirmation_reviewer_names(
    merged_steps: dict[str, dict],
    global_data: dict,
    project_data: dict,
    project_source_label: str = PROJECT_CONFIG_FILENAME,
) -> None:
    """Fail-fast if any ``confirmation.steps.<step>.reviewer`` is an
    unknown agent name.

    Walks every entry in ``merged_steps`` (not just a single target) so
    all typos surface at startup regardless of which step is about to
    run. The registry is built lazily, so configs with no agent-name
    reviewers pay no cost.
    """
    if not merged_steps:
        return
    registry: Optional[dict[str, AgentDef]] = None
    for step_name, cfg in merged_steps.items():
        reviewer = cfg.get("reviewer")
        if reviewer in (None, "human"):
            continue
        if registry is None:
            registry, _ = _agent_registry_from_data(
                global_data, project_data, project_source_label,
            )
        if reviewer not in registry:
            available = sorted(registry.keys())
            raise ValueError(
                f"confirmation.steps.{step_name}.reviewer: unknown "
                f"agent name {reviewer!r}; registered agents: {available}"
            )


def load_confirmation_config(project_root: Optional[Path] = None) -> dict:
    """Load per-step confirmation configuration from global + project YAML.

    Returns a dict shaped as::

        {"steps": {step_name: {"reviewer": Optional[str],
                                "max_iterations": Optional[int]}}}

    A step is confirmed iff it appears as a key in ``steps``. The
    legacy global ``enabled`` switch has been removed — there is no
    other on/off control.

    ``reviewer`` semantics inside each step entry:
    - ``'human'``   → MCP call file + interactive resume path
    - agent name   → LLM review using that registered agent (fail-fast
      at startup if the name is not in the top-level ``agents`` registry)
    - ``None``     → LLM review using the default ``llm_caller.defaults``
      chain (resolved by ``state_machine`` at step build time)

    Deprecated fields (``confirmation.enabled``, top-level
    ``confirmation.reviewer``, ``confirmation.llm_reviewer``, list-form
    ``confirmation.steps``) are detected, warned about once per source,
    and ignored.

    Global + project ``confirmation.steps`` are merged at the entry
    level: same step key in project overrides global; non-conflicting
    entries from either side coexist.
    """
    if project_root is None:
        project_root = Path.cwd()

    global_data, project_data, project_source_label = _load_agent_configs(project_root)
    merged_steps = _merge_confirmation_steps(
        global_data, project_data, project_source_label,
    )
    _validate_confirmation_reviewer_names(
        merged_steps, global_data, project_data, project_source_label,
    )
    return {"steps": merged_steps}


def insert_confirmation_steps(
    steps: list,
    project_root: Optional[Path] = None,
) -> list:
    """Insert CONFIRM steps after each step type that requires confirmation.

    A step type triggers a CONFIRM insertion iff it is a key in
    ``confirmation.steps`` (the new per-step dict) AND it actually
    appears in the supplied step sequence. There is no global on/off
    switch — opting a step out of confirmation simply means omitting it
    from ``confirmation.steps``.
    """
    config = load_confirmation_config(project_root)
    steps_dict = config.get("steps", {})
    if not steps_dict:
        return steps

    step_type_names = set()
    for s in steps:
        if hasattr(s, "value"):
            step_type_names.add(s.value)
        else:
            step_type_names.add(str(s))

    steps_to_confirm = {s for s in steps_dict.keys() if s in step_type_names}
    if not steps_to_confirm:
        return steps

    from .engine.models import StepType

    result = []
    for step in steps:
        result.append(step)
        step_value = step.value if hasattr(step, "value") else str(step)
        if step_value in steps_to_confirm:
            result.append(StepType.CONFIRM)
    return result


_GLOBAL_CONFIG_PATH_SUFFIX = (".se3", "config.yaml")

_BUILTIN_DEFAULT_AGENT_NAME = "claude"


@dataclass
class AgentDef:
    """Typed representation of an agent entry in the top-level registry.

    ``agents`` in the new schema is a dict ``{name: AgentDef}``; the key
    is the name, and the value carries ``type`` / ``cmd`` / ``priority``.
    """

    name: str
    type: str = "claude-code"
    cmd: str = ""
    priority: int = 0

    def to_agent_dict(self) -> dict:
        """Return the legacy ``list[dict]`` shape consumed by LLMCaller."""
        return {
            "name": self.name,
            "type": self.type,
            "cmd": self.cmd,
            "priority": self.priority,
        }


def _read_yaml(path: Path) -> Optional[dict]:
    """Read and parse a YAML file; log on parse error and return None.

    Returns the parsed dict, an empty dict if the file is empty/yaml-null,
    or None if the file does not exist, is non-mapping, or failed to parse.
    Non-mapping top levels (including falsy ones like ``[]``/``0``/``''``)
    are treated as malformed and produce a warning.

    This is the single source of truth for tolerant YAML reads. Whenever
    the path actually exists but is unusable (parse error or non-mapping
    top level), a one-shot local-shadow warning is emitted via
    ``_maybe_warn_local_shadow`` — a no-op for any file other than
    ``se3.local.yaml`` — so a broken local override cannot silently shadow
    the committed ``se3.yaml`` regardless of which loader path reads it.
    """
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except OSError as exc:
        logger.warning("failed to read %s: %s", path, exc)
        _maybe_warn_local_shadow(path)
        return None
    except yaml.YAMLError as exc:
        logger.warning("failed to parse %s: %s", path, exc)
        _maybe_warn_local_shadow(path)
        return None
    except Exception as exc:
        logger.warning("failed to load %s: %s", path, exc)
        _maybe_warn_local_shadow(path)
        return None
    if data is None:
        return {}
    if not isinstance(data, dict):
        logger.warning(
            "expected a YAML mapping at top of %s; got %s — ignoring",
            path, type(data).__name__,
        )
        _maybe_warn_local_shadow(path)
        return None
    return data


def _load_agent_configs(
    project_root: Optional[Path],
) -> tuple[dict, dict, str]:
    """Read global and project YAML configs in one pass.

    Returns ``(global_data, project_data, project_source_label)``;
    missing/invalid files are returned as empty dicts so callers can
    uniformly use ``.get(...)``. ``project_source_label`` is the
    filename of the project config actually consulted
    (``se3.local.yaml`` when it exists, otherwise ``se3.yaml``) — used
    to produce accurate warning/error messages.
    """
    global_data = _read_yaml(Path.home() / _GLOBAL_CONFIG_PATH_SUFFIX[0] / _GLOBAL_CONFIG_PATH_SUFFIX[1]) or {}
    project_data: dict = {}
    project_source_label = PROJECT_CONFIG_FILENAME
    if project_root is not None:
        project_config_path = get_project_config_path(project_root)
        # _read_yaml emits the local-shadow warning internally on
        # parse-error / non-mapping, so every caller gets the signal.
        project_data = _read_yaml(project_config_path) or {}
        project_source_label = _config_source_label(
            project_config_path, Path(project_root),
        )
    return global_data, project_data, project_source_label


_warned_list_agents_for: set[str] = set()
_warned_claude_commands_ignored_for: set[str] = set()
_warned_claude_commands_deprecated_for: set[str] = set()
# One-shot (per source) warning that ``agents.<name>.priority`` is deprecated.
# Agent rotation order now follows the written list order in
# ``llm_caller.defaults`` / ``llm_caller.steps.<step>``; priority is accepted
# but ignored. Keyed by the deduped source token so a config carrying several
# priority fields warns at most once per source.
_warned_agent_priority_deprecated_for: set[str] = set()


def _slugify_cmd(cmd: str) -> str:
    """Slugify a cmd string to form an agent name.

    Letters, digits, hyphens, and underscores are preserved. Everything
    else becomes ``_``. Empty result defaults to ``agent``.
    """
    import re

    if not cmd:
        return "agent"
    slug = re.sub(r"[^A-Za-z0-9_\-]", "_", cmd)
    return slug or "agent"


def _unique_name(base: str, existing: set[str]) -> str:
    """Return ``base`` if unused, else ``base_2`` / ``base_3`` …"""
    if base not in existing:
        return base
    i = 2
    while f"{base}_{i}" in existing:
        i += 1
    return f"{base}_{i}"


def _migrate_claude_commands(
    source_label: str,
    commands: list,
) -> tuple[dict[str, AgentDef], list[str]]:
    """Convert legacy ``claude_commands`` list to a registry + defaults.

    Returns ``(registry_entries, default_names)`` — ``registry_entries``
    is a mapping of ``name -> AgentDef`` synthesized from each legacy
    entry, and ``default_names`` is the name list in the original order
    to be used as an implicit ``llm_caller.defaults`` fallback.

    Name generation: each cmd is slugified; collisions append
    ``_2`` / ``_3`` / …. Emits a single DeprecationWarning per source
    showing the equivalent new-schema YAML snippet.
    """
    normalized = _normalize_commands(commands)
    if not normalized:
        return {}, []

    registry: dict[str, AgentDef] = {}
    defaults: list[str] = []
    for entry in normalized:
        cmd = entry["cmd"]
        priority = entry.get("priority", 0)
        base = _slugify_cmd(cmd)
        name = _unique_name(base, set(registry))
        registry[name] = AgentDef(
            name=name, type="claude-code", cmd=cmd, priority=priority,
        )
        defaults.append(name)

    dedup_key = _dedup_source_key(source_label)
    if dedup_key not in _warned_claude_commands_deprecated_for:
        _warned_claude_commands_deprecated_for.add(dedup_key)
        lines = ["agents:"]
        for name, agent in registry.items():
            parts = [f"type: {agent.type}", f"cmd: {agent.cmd}"]
            if agent.priority:
                parts.append(f"priority: {agent.priority}")
            lines.append(f"  {name}: {{{', '.join(parts)}}}")
        lines.append("llm_caller:")
        lines.append(f"  defaults: [{', '.join(defaults)}]")
        snippet = "\n".join(lines)
        logger.warning(
            "%s: 'claude_commands' is deprecated; converting to top-level "
            "'agents' registry + 'llm_caller.defaults'. Equivalent new "
            "config:\n%s",
            source_label, snippet,
        )

    return registry, defaults


def _agents_dict_from_source(
    source_label: str, raw: Any,
) -> Optional[dict[str, AgentDef]]:
    """Parse a single source's top-level ``agents`` field.

    Returns the parsed registry, or ``None`` when the field is absent.
    Invalid shapes (list, scalar, non-dict) log a warning and return
    ``None`` so the caller can decide how to fall back.
    """
    if raw is None:
        return None
    if isinstance(raw, list):
        # Legacy list form is removed. Warn and ignore.
        dedup_key = _dedup_source_key(source_label)
        if dedup_key not in _warned_list_agents_for:
            _warned_list_agents_for.add(dedup_key)
            logger.warning(
                "%s: top-level 'agents' is a list — this legacy form is "
                "no longer supported. Use a dict keyed by agent name "
                "instead, e.g. `agents:\\n  primary: {cmd: claude}`. "
                "Ignoring the entire agents field for this source.",
                source_label,
            )
        return None
    if not isinstance(raw, dict):
        logger.warning(
            "%s: top-level 'agents' is not a mapping (got %s); ignoring",
            source_label, type(raw).__name__,
        )
        return None

    registry: dict[str, AgentDef] = {}
    saw_priority = False
    for name, entry in raw.items():
        if not isinstance(name, str) or not name.strip():
            logger.warning(
                "%s: agents registry key %r is not a non-empty string; "
                "skipping",
                source_label, name,
            )
            continue
        if isinstance(entry, str):
            registry[name] = AgentDef(
                name=name, type="claude-code", cmd=entry, priority=0,
            )
            continue
        if not isinstance(entry, dict):
            logger.warning(
                "%s: agents.%s is not a mapping (got %s); skipping",
                source_label, name, type(entry).__name__,
            )
            continue
        cmd = entry.get("cmd", "")
        if not (isinstance(cmd, str) and cmd.strip()):
            logger.warning(
                "%s: agents.%s has no usable 'cmd'; skipping",
                source_label, name,
            )
            continue
        if "priority" in entry:
            saw_priority = True
        registry[name] = AgentDef(
            name=name,
            type=entry.get("type", "claude-code"),
            cmd=cmd,
            priority=entry.get("priority", 0),
        )

    if saw_priority:
        dedup_key = _dedup_source_key(source_label)
        if dedup_key not in _warned_agent_priority_deprecated_for:
            _warned_agent_priority_deprecated_for.add(dedup_key)
            logger.warning(
                "%s: 'agents.<name>.priority' is deprecated and ignored. "
                "Agent rotation order now follows the written order of "
                "'llm_caller.defaults' / 'llm_caller.steps.<step>'. Remove "
                "the priority field(s) and order the name lists explicitly.",
                source_label,
            )
    return registry


def _agent_registry_from_data(
    global_data: dict, project_data: dict,
    project_source_label: str = PROJECT_CONFIG_FILENAME,
) -> tuple[dict[str, AgentDef], list[str]]:
    """Build the agent registry from global + project YAML data.

    Returns ``(registry, legacy_defaults)`` — the registry is the
    entry-level merged agent directory (project overrides global by
    name); ``legacy_defaults`` is an implicit default chain (list of
    names) derived from ``claude_commands`` when either source omits
    ``agents`` (project legacy defaults win over global).
    """
    sources = [
        ("~/.se3/config.yaml", global_data),
        (project_source_label, project_data),
    ]

    merged: dict[str, AgentDef] = {}
    legacy_defaults: list[str] = []

    for source_label, data in sources:
        raw_agents = data.get("agents")
        parsed = _agents_dict_from_source(source_label, raw_agents)

        has_explicit_agents = parsed is not None and raw_agents is not None and isinstance(raw_agents, dict)

        if parsed:
            # Merge entry-level — later source (project) overrides by
            # name; non-conflicting entries from either side coexist.
            for name, agent in parsed.items():
                merged[name] = agent

        claude_commands = data.get("claude_commands")
        if claude_commands:
            if has_explicit_agents:
                # Both set: ignore claude_commands with warning.
                dedup_key = _dedup_source_key(source_label)
                if dedup_key not in _warned_claude_commands_ignored_for:
                    _warned_claude_commands_ignored_for.add(dedup_key)
                    logger.warning(
                        "%s: both 'agents' and 'claude_commands' are "
                        "set; ignoring legacy 'claude_commands'",
                        source_label,
                    )
            else:
                migrated, defaults = _migrate_claude_commands(
                    source_label, claude_commands,
                )
                # Merge migrated entries, not overriding an already-merged
                # name (so project claude_commands can still add to a
                # registry built from global agents).
                for name, agent in migrated.items():
                    if name not in merged:
                        merged[name] = agent
                # Project's legacy defaults take precedence over global's.
                legacy_defaults = defaults

    return merged, legacy_defaults


def _registry_to_list(
    registry: dict[str, AgentDef], names: list[str],
) -> list[dict]:
    """Resolve a name list against the registry, preserving written order.

    The ``priority`` field is intentionally NOT used for ordering: the
    written order of ``names`` (the order they appear in
    ``llm_caller.defaults`` / ``llm_caller.steps.<step>``) is the
    rotation order. ``priority`` is retained on each agent dict as
    deprecated compatibility data only.

    Caller is responsible for validating that all names are registered.
    """
    return [registry[n].to_agent_dict() for n in names]


# Back-compat alias: the old name advertised priority sorting, which is
# now removed. Kept so any external importer keeps working.
_registry_to_sorted_list = _registry_to_list


def load_agent_registry(
    project_root: Optional[Path] = None,
) -> dict[str, AgentDef]:
    """Load the top-level agent registry from global + project configs.

    Returns a merged ``{name: AgentDef}`` mapping. Legacy
    ``claude_commands`` in either source is auto-migrated when that
    source omits ``agents``. Invalid top-level forms (list, scalar)
    produce a warning and are ignored.
    """
    global_data, project_data, project_source_label = _load_agent_configs(project_root)
    registry, _ = _agent_registry_from_data(
        global_data, project_data, project_source_label,
    )
    return registry


def _resolve_name_list(
    reference_location: str,
    names: list[str],
    registry: dict[str, AgentDef],
) -> list[dict]:
    """Resolve a list of names against the registry.

    Raises ``ValueError`` on any unknown name, with a message containing
    the reference location and the sorted list of registered names.
    """
    missing = [n for n in names if n not in registry]
    if missing:
        available = sorted(registry.keys())
        raise ValueError(
            f"{reference_location}: unknown agent name(s) {missing!r}; "
            f"registered agents: {available}"
        )
    return _registry_to_list(registry, names)


def _read_llm_caller_section(
    data: dict, source_label: str,
) -> Optional[dict]:
    """Read and validate the ``llm_caller`` section of a source."""
    llm_caller = data.get("llm_caller")
    if llm_caller is None:
        return None
    if not isinstance(llm_caller, dict):
        dedup_key = _dedup_source_key(source_label)
        if dedup_key not in _warned_non_dict_llm_caller_for:
            _warned_non_dict_llm_caller_for.add(dedup_key)
            logger.warning(
                "%s: top-level 'llm_caller' is not a mapping (got %s); "
                "ignoring",
                source_label, type(llm_caller).__name__,
            )
        return None
    return llm_caller


def _explicit_defaults(
    data: dict, source_label: str,
) -> Optional[list[str]]:
    """Read ``llm_caller.defaults`` from a source as a list of names.

    Returns ``None`` when absent; warns + returns ``None`` for malformed
    values (non-list, or list with non-string entries after filtering).
    """
    llm_caller = _read_llm_caller_section(data, source_label)
    if llm_caller is None:
        return None
    raw = llm_caller.get("defaults")
    if raw is None:
        return None
    if not isinstance(raw, list):
        logger.warning(
            "%s: llm_caller.defaults is not a list (got %s); ignoring",
            source_label, type(raw).__name__,
        )
        return None
    names: list[str] = []
    for entry in raw:
        if isinstance(entry, str) and entry.strip():
            names.append(entry)
        else:
            logger.warning(
                "%s: llm_caller.defaults entry %r is not a non-empty "
                "string; skipping",
                source_label, entry,
            )
    return names if names else None


def _default_chain_from_data(
    global_data: dict, project_data: dict,
    project_source_label: str = PROJECT_CONFIG_FILENAME,
) -> list[dict]:
    """Build the default agent chain from already-parsed YAML data.

    Priority (first non-empty wins):
      1. ``project.llm_caller.defaults`` (explicit, name list)
      2. ``global.llm_caller.defaults`` (explicit, name list)
      3. Implicit defaults from legacy ``claude_commands`` (project > global)
      4. Built-in ``[claude]``.

    Unknown names at the selected level raise ``ValueError``.
    """
    registry, legacy_defaults = _agent_registry_from_data(
        global_data, project_data, project_source_label,
    )

    # 1 & 2: explicit defaults.
    project_names = _explicit_defaults(project_data, project_source_label)
    if project_names is not None:
        return _resolve_name_list(
            f"{project_source_label}: llm_caller.defaults", project_names, registry,
        )
    global_names = _explicit_defaults(global_data, "~/.se3/config.yaml")
    if global_names is not None:
        return _resolve_name_list(
            "~/.se3/config.yaml: llm_caller.defaults", global_names, registry,
        )

    # 3: implicit from legacy claude_commands.
    if legacy_defaults:
        return _resolve_name_list(
            "legacy claude_commands migration", legacy_defaults, registry,
        )

    # 4: built-in.
    return [
        {
            "name": _BUILTIN_DEFAULT_AGENT_NAME,
            "type": "claude-code",
            "cmd": _BUILTIN_DEFAULT_AGENT_NAME,
            "priority": 0,
        }
    ]


def _valid_step_keys() -> set[str]:
    """Return the set of known StepType value strings (for typo detection)."""
    # Import locally to avoid circular import at module load.
    from .engine.models import StepType

    return {s.value for s in StepType}


_warned_unknown_step_keys_for: set[tuple[str, ...]] = set()
_warned_non_dict_llm_caller_for: set[str] = set()


def _warn_on_unknown_step_keys(
    source_label: str, steps_dict: dict,
) -> None:
    """Log a warning when ``llm_caller.steps`` contains keys that are not
    valid StepType values. Idempotent per (source, keyset) pair to avoid
    flooding logs when resolve_agents is called many times per flow.
    """
    if not steps_dict:
        return
    valid = _valid_step_keys()
    unknown = sorted(k for k in steps_dict.keys() if k not in valid)
    if not unknown:
        return
    dedup_key = (_dedup_source_key(source_label), *unknown)
    if dedup_key in _warned_unknown_step_keys_for:
        return
    _warned_unknown_step_keys_for.add(dedup_key)
    logger.warning(
        "%s: llm_caller.steps has unknown step key(s) %s — likely a typo; "
        "these declarations will be ignored",
        source_label, unknown,
    )


def _llm_caller_steps_section(data: dict, source_label: str) -> dict:
    """Return the ``llm_caller.steps`` mapping for a source (or ``{}``)."""
    llm_caller = _read_llm_caller_section(data, source_label)
    if llm_caller is None:
        return {}
    section = llm_caller.get("steps", {})
    return section if isinstance(section, dict) else {}


def _extract_step_names(
    raw_list: list, source_label: str, step_type: str,
) -> tuple[list[str], bool]:
    """Extract agent name references from a flat ``llm_caller.steps.<step>`` list.

    Returns ``(names, per_entry_warned)``. Inline dict entries
    (``- cmd: claude-opus``) and other non-string entries are skipped
    with a warning — the new schema requires name references into the
    top-level ``agents`` registry.
    """
    per_entry_warned = False
    names: list[str] = []
    for entry in raw_list:
        if isinstance(entry, str):
            if entry.strip():
                names.append(entry)
            else:
                logger.warning(
                    "%s: llm_caller.steps.%s entry %r is a blank string; "
                    "skipping",
                    source_label, step_type, entry,
                )
                per_entry_warned = True
        elif isinstance(entry, dict):
            # New schema uses name references only; inline dict form is
            # a breaking change → warn and skip this entry.
            logger.warning(
                "%s: llm_caller.steps.%s contains an inline dict entry "
                "%r — the new schema requires agent name references "
                "(list of str). Define the agent under top-level "
                "'agents' and reference it by name. Skipping this entry.",
                source_label, step_type, entry,
            )
            per_entry_warned = True
        else:
            logger.warning(
                "%s: llm_caller.steps.%s contains non-str entry %r; "
                "skipping",
                source_label, step_type, entry,
            )
            per_entry_warned = True
    return names, per_entry_warned


def _step_override_from_data(
    global_data: dict, project_data: dict, step_type: str,
    project_source_label: str = PROJECT_CONFIG_FILENAME,
) -> Optional[list[dict]]:
    """Extract and validate a per-step override from already-parsed YAML.

    New schema: ``llm_caller.steps.<step>`` is a list of name strings
    that refer to entries in the top-level ``agents`` registry. Legacy
    inline-dict entries (``- cmd: claude-opus``) are rejected with a
    warning and treated as "no override"; an unknown agent name raises
    ``ValueError`` at startup so typos fail loudly.

    Nested-list values (``[[a], [b, c]]``) are only meaningful for
    ``self_check`` (see :func:`_self_check_resolution_from_data`); for
    every other step a nested form is an illegal flat override — its
    sub-list entries are non-strings and are skipped with a warning,
    yielding "no override".

    Returns the agent dicts in written order, or ``None`` if no valid
    override is declared for ``step_type``.
    """
    global_steps = _llm_caller_steps_section(global_data, "~/.se3/config.yaml")
    project_steps = _llm_caller_steps_section(project_data, project_source_label)

    _warn_on_unknown_step_keys("~/.se3/config.yaml", global_steps)
    _warn_on_unknown_step_keys(project_source_label, project_steps)

    # Source label is tracked so ValueError messages point the user to
    # the exact YAML file where the typo lives.
    if step_type in project_steps:
        raw = project_steps[step_type]
        source_label = project_source_label
    elif step_type in global_steps:
        raw = global_steps[step_type]
        source_label = "~/.se3/config.yaml"
    else:
        return None

    if not isinstance(raw, list):
        logger.warning(
            "%s: llm_caller.steps.%s is not a list (got %s); ignoring "
            "override",
            source_label, step_type, type(raw).__name__,
        )
        return None

    names, per_entry_warned = _extract_step_names(raw, source_label, step_type)

    if not names:
        if not per_entry_warned:
            logger.warning(
                "%s: llm_caller.steps.%s is empty or has no valid "
                "entries; ignoring override",
                source_label, step_type,
            )
        return None

    registry, _ = _agent_registry_from_data(
        global_data, project_data, project_source_label,
    )
    return _resolve_name_list(
        f"{source_label}: llm_caller.steps.{step_type}",
        names,
        registry,
    )


_SELF_CHECK_STEP_NAME = "self_check"


@dataclass
class SelfCheckResolution:
    """Resolved ``llm_caller.steps.self_check`` agent-chain configuration.

    ``form`` is one of:

    - ``"flat"``    — a flat list of agent names. A single chain is reused
      for every self_check pass (fully back-compatible). ``chains`` has
      length 1.
    - ``"nested"``  — a list of sub-lists, one agent chain per self_check
      pass. ``chains`` has length == number of declared sub-lists.
    - ``"default"`` — no usable self_check override: the key is absent, or
      the declaration is mixed (strings AND sub-lists) / otherwise
      malformed and falls back to ``llm_caller.defaults``. ``chains`` is
      empty.
    """

    form: str
    chains: list[list[dict]] = field(default_factory=list)
    source_label: Optional[str] = None

    @property
    def chain_count(self) -> int:
        return len(self.chains)

    @property
    def is_override(self) -> bool:
        return self.form in ("flat", "nested")

    def chain_for_pass(self, pass_index: int) -> Optional[list[dict]]:
        """Return the agent chain for a 1-based ``pass_index``.

        Passes beyond the number of declared chains reuse the LAST chain.
        Returns ``None`` for the ``default`` form (caller uses the default
        chain).
        """
        if not self.chains:
            return None
        idx = max(1, int(pass_index or 1)) - 1
        if idx >= len(self.chains):
            idx = len(self.chains) - 1
        return self.chains[idx]


def _self_check_resolution_from_data(
    global_data: dict, project_data: dict,
    project_source_label: str = PROJECT_CONFIG_FILENAME,
) -> SelfCheckResolution:
    """Parse ``llm_caller.steps.self_check`` into a :class:`SelfCheckResolution`.

    Supports both the flat list form (one chain reused for every pass) and
    the nested list form (``[[a], [b, c]]`` — one chain per pass). A mixed
    form (strings AND sub-lists in the same list) is a configuration error:
    it logs a WARNING and falls back to the default chain (``default``
    form). Unknown agent names still fail fast via :func:`_resolve_name_list`.
    """
    global_steps = _llm_caller_steps_section(global_data, "~/.se3/config.yaml")
    project_steps = _llm_caller_steps_section(project_data, project_source_label)

    _warn_on_unknown_step_keys("~/.se3/config.yaml", global_steps)
    _warn_on_unknown_step_keys(project_source_label, project_steps)

    if _SELF_CHECK_STEP_NAME in project_steps:
        raw = project_steps[_SELF_CHECK_STEP_NAME]
        source_label = project_source_label
    elif _SELF_CHECK_STEP_NAME in global_steps:
        raw = global_steps[_SELF_CHECK_STEP_NAME]
        source_label = "~/.se3/config.yaml"
    else:
        return SelfCheckResolution(form="default")

    if not isinstance(raw, list):
        logger.warning(
            "%s: llm_caller.steps.self_check is not a list (got %s); "
            "ignoring override",
            source_label, type(raw).__name__,
        )
        return SelfCheckResolution(form="default", source_label=source_label)

    has_sublist = any(isinstance(e, list) for e in raw)
    has_string = any(isinstance(e, str) for e in raw)

    registry, _ = _agent_registry_from_data(
        global_data, project_data, project_source_label,
    )

    # Mixed form: both bare strings and sub-lists. Treat as a config error
    # and fall back to llm_caller.defaults.
    if has_sublist and has_string:
        logger.warning(
            "%s: llm_caller.steps.self_check mixes bare agent names with "
            "sub-lists ([[a], [b, c]]) — this is invalid. Use either a "
            "flat list (one chain for all passes) or a fully nested list "
            "(one chain per pass). Falling back to llm_caller.defaults.",
            source_label,
        )
        return SelfCheckResolution(form="default", source_label=source_label)

    if has_sublist:
        chains: list[list[dict]] = []
        for sub in raw:
            if not isinstance(sub, list):
                logger.warning(
                    "%s: llm_caller.steps.self_check nested entry %r is "
                    "not a list; skipping this pass chain",
                    source_label, sub,
                )
                continue
            names, _warned = _extract_step_names(
                sub, source_label, _SELF_CHECK_STEP_NAME,
            )
            if not names:
                logger.warning(
                    "%s: llm_caller.steps.self_check nested entry %r has "
                    "no valid agent names; skipping this pass chain",
                    source_label, sub,
                )
                continue
            chains.append(
                _resolve_name_list(
                    f"{source_label}: llm_caller.steps.self_check",
                    names, registry,
                )
            )
        if not chains:
            return SelfCheckResolution(form="default", source_label=source_label)
        return SelfCheckResolution(
            form="nested", chains=chains, source_label=source_label,
        )

    # Flat form (back-compatible): one chain reused for every pass.
    names, per_entry_warned = _extract_step_names(
        raw, source_label, _SELF_CHECK_STEP_NAME,
    )
    if not names:
        if not per_entry_warned:
            logger.warning(
                "%s: llm_caller.steps.self_check is empty or has no valid "
                "entries; ignoring override",
                source_label,
            )
        return SelfCheckResolution(form="default", source_label=source_label)
    chain = _resolve_name_list(
        f"{source_label}: llm_caller.steps.self_check", names, registry,
    )
    return SelfCheckResolution(
        form="flat", chains=[chain], source_label=source_label,
    )


def load_self_check_resolution(
    project_root: Optional[Path] = None,
) -> SelfCheckResolution:
    """Load the resolved ``llm_caller.steps.self_check`` configuration.

    See :class:`SelfCheckResolution`. Reads global + project YAML in one
    pass; project declaration of ``self_check`` fully replaces the global
    one (no merge), mirroring the other per-step overrides.
    """
    global_data, project_data, project_source_label = _load_agent_configs(project_root)
    return _self_check_resolution_from_data(
        global_data, project_data, project_source_label,
    )


def effective_self_check_passes_required(
    workflow_cfg: "WorkflowConfig",
    resolution: SelfCheckResolution,
) -> int:
    """Derive the effective self_check pass count from config + chain resolution.

    Single source of truth for the ``#i/N`` denominator, shared by the state
    machine's per-transition cached path (``_get_self_check_passes_required``)
    and the ``se3 history show`` history-only renderer:

    - nested chains + no explicit ``self_check_passes_required`` → number of
      declared chains (the chain list alone expresses the intent);
    - both set → the explicit count wins (over/under-shoot reuses/skips chains);
    - flat / default / no override → the configured ``self_check_passes_required``.
    """
    if resolution.form != "nested":
        return workflow_cfg.self_check_passes_required
    if not workflow_cfg.self_check_passes_required_explicit:
        return resolution.chain_count
    return workflow_cfg.self_check_passes_required


def resolve_self_check_passes_required(project_root: Optional[Path] = None) -> int:
    """Load config + resolution from disk and return the effective pass count.

    Convenience wrapper around :func:`effective_self_check_passes_required` for
    callers (e.g. ``se3 history show``) that do not have the cached config and
    resolution objects on hand. Degrades to the raw
    ``workflow.self_check_passes_required`` on any resolution loader error so a
    malformed self_check chain never crashes history rendering.
    """
    if project_root is None:
        project_root = Path.cwd()
    workflow_cfg = WorkflowConfig.load(project_root)
    try:
        resolution = load_self_check_resolution(project_root)
    except (ValueError, IOError, OSError):
        return workflow_cfg.self_check_passes_required
    return effective_self_check_passes_required(workflow_cfg, resolution)


def load_agents(project_root: Optional[Path] = None) -> list[dict]:
    """Load agent configurations from project and global configuration.

    Supports two configuration formats:
    1. New ``agents`` field (recommended): list of dicts with name, type, cmd, priority.
    2. Legacy ``claude_commands`` field: auto-converted with type='claude-code'.

    When both exist, ``agents`` takes priority.  Project config overrides global.

    Args:
        project_root: Project root directory. If None, uses global config only.

    Returns:
        List of agent config dicts ``{name, type, cmd, priority}`` in the
        configured chain order (``priority`` is deprecated and not used for
        ordering).
    """
    global_data, project_data, project_source_label = _load_agent_configs(project_root)
    return _default_chain_from_data(global_data, project_data, project_source_label)


def load_step_agents(
    project_root: Optional[Path],
    step_type: Optional[str],
) -> Optional[list[dict]]:
    """Load per-step agent override from ``llm_caller.steps.<step_type>``.

    Reads ``llm_caller.steps.<step_type>`` from project-level se3.yaml with
    fallback to the global ``~/.se3/config.yaml`` entry of the same shape.
    Project-level declaration of a given step fully replaces the global
    declaration for that step (no deep merge), mirroring ``load_agents``.

    When the step has no declaration, returns None so callers can fall back
    to the default chain from :func:`load_agents`. Empty lists and
    structurally invalid values (not a list, entries not str/dict, dict
    entries lacking a usable ``cmd``) are treated as "no declaration" — a
    warning is logged but no exception is raised, matching the
    clamp-and-warn policy used elsewhere (e.g. :class:`TestConfig`).

    Args:
        project_root: Project root directory, or None to use global config
            only.
        step_type: StepType value string (e.g. ``"implement"``, ``"plan"``).
            None or empty string short-circuits to None.

    Returns:
        Normalized agent dicts in the written list order (``priority`` is
        deprecated and ignored for ordering), or None when no override is
        declared for this step.
    """
    if not step_type:
        return None
    global_data, project_data, project_source_label = _load_agent_configs(project_root)
    return _step_override_from_data(
        global_data, project_data, step_type, project_source_label,
    )


def resolve_confirm_inputs(
    project_root: Optional[Path],
    reviewed_step_type: str,
) -> Optional[dict]:
    """Resolve all data a CONFIRM step needs in a single YAML read.

    Returns ``None`` when ``reviewed_step_type`` is not configured for
    confirmation (i.e. absent from ``confirmation.steps``), so the caller
    can defensively fall back without an extra read.

    Otherwise returns ``{"reviewer": str|None, "max_iterations": int|None,
    "agents": list[dict]|None}``:
    - ``reviewer == 'human'``   → ``agents`` is ``None``; caller routes
      to the MCP-call path.
    - ``reviewer`` is an agent name → ``agents`` is a single-element list
      holding that agent's resolved dict form. Unknown names raise
      ``ValueError`` via the shared registry check.
    - ``reviewer is None``        → ``agents`` is the resolved
      ``llm_caller.defaults`` chain.

    Consolidates three prior reads (``load_confirmation_config`` +
    ``load_agent_registry`` + ``load_agents``) into one
    ``_load_agent_configs`` call, to keep CONFIRM transitions cheap.

    Reuses :func:`_merge_confirmation_steps` and
    :func:`_validate_confirmation_reviewer_names` so that every entry in
    ``confirmation.steps`` is fail-fast validated — not only the
    ``reviewed_step_type``. This protects resumed flows whose persisted
    step sequence may bypass :func:`insert_confirmation_steps`, since
    any unknown agent-name reference under a different step key would
    otherwise slip past unnoticed.
    """
    global_data, project_data, project_source_label = _load_agent_configs(project_root)
    merged_steps = _merge_confirmation_steps(
        global_data, project_data, project_source_label,
    )
    _validate_confirmation_reviewer_names(
        merged_steps, global_data, project_data, project_source_label,
    )

    step_cfg = merged_steps.get(reviewed_step_type)
    if step_cfg is None:
        return None

    reviewer = step_cfg.get("reviewer")
    max_iterations = step_cfg.get("max_iterations")

    if reviewer == "human":
        return {
            "reviewer": "human",
            "max_iterations": max_iterations,
            "agents": None,
        }

    if reviewer is None:
        agents = _default_chain_from_data(
            global_data, project_data, project_source_label,
        )
        return {
            "reviewer": None,
            "max_iterations": max_iterations,
            "agents": agents,
        }

    # Name already validated above; resolve against registry.
    registry, _ = _agent_registry_from_data(
        global_data, project_data, project_source_label,
    )
    return {
        "reviewer": reviewer,
        "max_iterations": max_iterations,
        "agents": [registry[reviewer].to_agent_dict()],
    }


def resolve_agents(
    project_root: Optional[Path],
    step_type: Optional[str],
    *,
    self_check_pass_index: Optional[int] = None,
) -> tuple[list[dict], bool]:
    """Resolve the effective agent chain for a step in a single YAML read.

    Returns ``(agents, is_step_override)``. When ``step_type`` declares a
    valid ``llm_caller.steps.<step_type>`` override, that list is returned
    verbatim (no fallback to the default chain) and the flag is True.
    Otherwise the default chain from the top-level ``agents`` /
    ``claude_commands`` (or built-in default) is returned and the flag is
    False.

    ``self_check`` additionally supports a nested per-pass schema
    (``[[a], [b, c]]``). When ``step_type == "self_check"`` the chain is
    selected by ``self_check_pass_index`` (1-based; passes beyond the
    declared chain count reuse the last chain). A flat self_check list
    behaves as a single chain for every pass; a mixed / malformed
    declaration falls back to the default chain.

    Used by :class:`LLMCaller` to avoid the cost of reading the same YAML
    files twice (once via ``load_step_agents``, once via ``load_agents``).
    """
    global_data, project_data, project_source_label = _load_agent_configs(project_root)

    if step_type == _SELF_CHECK_STEP_NAME:
        resolution = _self_check_resolution_from_data(
            global_data, project_data, project_source_label,
        )
        if resolution.is_override:
            chain = resolution.chain_for_pass(self_check_pass_index or 1)
            if chain:
                return chain, True
        return _default_chain_from_data(
            global_data, project_data, project_source_label,
        ), False

    if step_type:
        override = _step_override_from_data(
            global_data, project_data, step_type, project_source_label,
        )
        if override:
            return override, True
    return _default_chain_from_data(
        global_data, project_data, project_source_label,
    ), False


def load_claude_commands(project_root: Optional[Path] = None) -> list[dict]:
    """Load Claude CLI commands from project and global configuration.

    .. deprecated::
        Use :func:`load_agents` instead.  This function now delegates to
        ``load_agents()`` and converts the result back to the legacy
        ``{cmd, priority}`` format for backward compatibility.

    Args:
        project_root: Project root directory. If None, uses global config only.

    Returns:
        List of command dictionaries with 'cmd' and 'priority' keys, in the
        configured chain order ('priority' is deprecated and not used for
        ordering).
    """
    agents = load_agents(project_root)
    return _agents_to_commands(agents)


def _agents_to_commands(agents: list[dict]) -> list[dict]:
    """Convert agent dicts back to legacy command dicts."""
    return [
        {"cmd": a.get("cmd", "claude"), "priority": a.get("priority", 0)}
        for a in agents
    ]


def _normalize_commands(commands: list) -> list[dict]:
    """Normalize command entries to dictionaries.

    Args:
        commands: List of command entries (dicts or strings)

    Returns:
        List of normalized command dictionaries
    """
    normalized = []
    for cmd in commands:
        if isinstance(cmd, str):
            normalized.append({"cmd": cmd, "priority": 0})
        elif isinstance(cmd, dict):
            if "cmd" in cmd:
                normalized.append({
                    "cmd": cmd["cmd"],
                    "priority": cmd.get("priority", 0)
                })
    return normalized


def get_language_labels(language: str) -> dict[str, str]:
    """Get translated labels for a given language.
    
    Args:
        language: Language code (e.g., 'en', 'zh', 'zh-CN')
        
    Returns:
        Dictionary of translated labels
    """
    labels = {
        "en": {
            "human_input": "Human Input",
            "review": "Review",
            "approve": "Approve",
            "reject": "Reject",
            "comment": "Comment",
            "submit": "Submit",
        },
        "zh": {
            "human_input": "人工输入",
            "review": "审查",
            "approve": "批准",
            "reject": "拒绝",
            "comment": "评论",
            "submit": "提交",
        },
    }
    
    # Map language codes
    lang = language.lower()
    if lang.startswith("zh"):
        return labels["zh"]
    return labels["en"]


@dataclass
class LanguageConfig:
    """Language configuration for controlling output language.

    Two-tier language settings:
    - language: for human-facing steps (summarize, discovery, confirmed steps)
    - spec_language: for spec writing (update_spec step)
    Both default to None (no restriction).
    """

    language: Optional[str] = None
    spec_language: Optional[str] = None

    @classmethod
    def load(cls, project_root: Path) -> "LanguageConfig":
        """Load language configuration from the active project YAML."""
        data, _src = load_project_yaml(project_root)
        if not data:
            return cls()
        lang_section = data.get("language", {})
        if not lang_section or not isinstance(lang_section, dict):
            return cls()
        return cls(
            language=lang_section.get("language"),
            spec_language=lang_section.get("spec_language"),
        )


def load_language_config(project_root: Optional[Path] = None) -> "LanguageConfig":
    """Load language configuration from project.

    Args:
        project_root: Project root directory. If None, uses current working directory.

    Returns:
        LanguageConfig instance with loaded or default settings.
    """
    if project_root is None:
        project_root = Path.cwd()
    return LanguageConfig.load(project_root)


def get_language_instruction(
    language: Optional[str],
    context: str = "",
    *,
    for_spec: bool = False,
) -> str:
    """Get a language instruction string for LLM prompts.

    Args:
        language: Language code (e.g., 'zh-CN', 'en'). None means no restriction.
        context: Optional context description for the instruction.
        for_spec: When True, the instruction is tailored for a spec-writing
            path: it states that the configured spec language is authoritative
            for the written spec (prose + SHALL/MUST requirement statements).
            Used by ``update_spec`` and the ``sync_*`` write paths.

    Returns:
        Prompt instruction string when language is set, empty string when None.
        The contract that ``language is None``/empty returns ``""`` is
        preserved regardless of ``for_spec``.
    """
    if not language:
        return ""
    ctx = f" in the {context} step" if context else ""
    parts = [f"\n\nIMPORTANT: You MUST respond in {language}{ctx}."]
    # Technical symbols must survive translation verbatim — a translated
    # identifier / command / API name would break the spec or the code it
    # documents. This clause applies to every language-restricted prompt.
    parts.append(
        "Preserve all technical symbols verbatim in their original form — do "
        "NOT translate code identifiers, function/class names, command names, "
        "API names, file paths, or literal config keys/values."
    )
    if for_spec:
        # Spec-writing context: spec_language is the single authority for the
        # written spec body. Make that explicit so the agent does not mirror
        # the source code's incidental language.
        parts.append(
            f"This content is written into a spec file: the configured spec "
            f"language ({language}) is authoritative. Write all prose and "
            f"every SHALL/MUST requirement statement in {language}."
        )
    return " ".join(parts)


def is_chinese_language(language: str) -> bool:
    """Check if the language is Chinese.

    Args:
        language: Language code (e.g., 'en', 'zh', 'zh-CN')

    Returns:
        True if the language is Chinese
    """
    return language.lower().startswith("zh")


@dataclass
class ConflictResolverConfig:
    """Conflict resolution configuration for loop branch merges.

    Supports two strategies:
    - 'human': Preserve conflict state, create call file, wait for human resolution.
    - 'llm': Attempt LLM-based per-file resolution, fallback to human on failure.
    """

    strategy: str = "human"

    @classmethod
    def from_dict(cls, data: dict) -> "ConflictResolverConfig":
        """Create ConflictResolverConfig from dictionary."""
        if not data:
            return cls()
        strategy = data.get("strategy", "human")
        if strategy not in ("human", "llm"):
            strategy = "human"
        return cls(strategy=strategy)

    @classmethod
    def load(cls, project_root: Path) -> "ConflictResolverConfig":
        """Load conflict resolver configuration from the active project YAML."""
        data, _src = load_project_yaml(project_root)
        if not data:
            return cls()
        cr_data = data.get("conflict_resolver", {})
        if not cr_data or not isinstance(cr_data, dict):
            return cls()
        return cls.from_dict(cr_data)


def load_conflict_resolver_config(project_root: Optional[Path] = None) -> ConflictResolverConfig:
    """Load conflict resolver configuration from project.

    Args:
        project_root: Project root directory. If None, uses current working directory.

    Returns:
        ConflictResolverConfig instance with loaded or default settings.
    """
    if project_root is None:
        project_root = Path.cwd()
    return ConflictResolverConfig.load(project_root)

DEFAULT_MAX_FIX_ITERATIONS = 100
DEFAULT_SELF_CHECK_PASSES_REQUIRED = 1
DEFAULT_SELF_CHECK_CONVERGENCE_ENABLED = False
# Per-flow attempt cap for looping on inherited (baseline) test failures
# (mechanism B). Independent of ``max_fix_iterations`` (which may be the
# unlimited sentinel 0); baseline failures must stay independently bounded.
# ``0`` disables baseline looping entirely (inherited failures are surfaced,
# never looped); negative values are rejected fail-fast at load.
DEFAULT_BASELINE_FIX_MAX_ATTEMPTS = 3
# Threshold below which a self_check pass that finds only a few non-critical/high
# issues defers its fix and lets the remaining nested self_check passes run first
# (accumulating their findings) before entering one consolidated fix loop. ``0``
# (or ``null``) disables deferral entirely (every pass that finds issues triggers
# fix immediately — the historical behavior). Negative values are rejected
# fail-fast at load.
DEFAULT_SELF_CHECK_DEFER_FIX_THRESHOLD = 3

# Dedup set for the "which config source won" load-time log line, keyed by the
# resolved active config path. ``WorkflowConfig.load`` is called per step, so
# without dedup the effective-source line would flood the log; logging once per
# (process, config file) is enough to surface the se3.local.yaml-shadows-se3.yaml
# ambiguity. Tests use fresh tmp_path dirs (distinct keys), so each gets its line.
_logged_workflow_source_for: set[str] = set()


class ConfigError(ValueError):
    """Raised when project configuration is invalid.

    Inherits from ValueError so callers that catch ValueError also catch this.
    """


@dataclass
class WorkflowConfig:
    """Workflow-level configuration for the fix loop and self_check behavior.

    Loaded from se3.yaml ``workflow:`` section with sensible defaults.
    """

    max_fix_iterations: int = DEFAULT_MAX_FIX_ITERATIONS
    self_check_passes_required: int = DEFAULT_SELF_CHECK_PASSES_REQUIRED
    self_check_convergence_enabled: bool = DEFAULT_SELF_CHECK_CONVERGENCE_ENABLED
    baseline_fix_max_attempts: int = DEFAULT_BASELINE_FIX_MAX_ATTEMPTS
    self_check_defer_fix_threshold: int = DEFAULT_SELF_CHECK_DEFER_FIX_THRESHOLD
    # Whether ``workflow.self_check_passes_required`` was set explicitly in
    # the YAML. When False and ``llm_caller.steps.self_check`` is a nested
    # per-pass chain, the effective pass count is derived from the number
    # of declared chains (see state_machine effective-pass resolution).
    self_check_passes_required_explicit: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> "WorkflowConfig":
        """Create WorkflowConfig from dictionary with validation.

        Args:
            data: Raw dict from the ``workflow:`` YAML section (or the full
                config dict, in which case the ``workflow`` key is extracted).

        Raises:
            ConfigError: If ``self_check_passes_required`` is < 1,
                ``max_fix_iterations`` is negative, or
                ``baseline_fix_max_attempts`` is negative.
        """
        if not data:
            return cls()

        workflow_data = data.get("workflow", data)
        if not isinstance(workflow_data, dict):
            return cls()

        # Sentinel: None/null is normalized to 0 (= unlimited). 0 is a valid
        # value meaning "no upper bound". Non-integer types (bool/float/
        # arbitrary strings) warn and fall back to the default — symmetric
        # with ``self_check_passes_required`` below. Negative integers are
        # rejected fail-fast so a typo cannot silently disable exhaustion;
        # only an explicit 0 (or null) opts into unlimited.
        if "max_fix_iterations" in workflow_data and workflow_data["max_fix_iterations"] is None:
            max_fix = 0
        else:
            raw_max_fix = workflow_data.get(
                "max_fix_iterations", DEFAULT_MAX_FIX_ITERATIONS
            )
            if isinstance(raw_max_fix, bool) or isinstance(raw_max_fix, float):
                # Floats fall back even when numerically integral (0.0, 5.0,
                # ...) — the spec requires literal int per ``Workflow
                # Configuration`` so YAML readers don't silently accept
                # ``max_fix_iterations: 0.0`` as the unlimited sentinel.
                # Surface the float case explicitly so users typing 0.0
                # expecting unlimited see why they got the default cap.
                if isinstance(raw_max_fix, float) and raw_max_fix == 0.0:
                    logger.warning(
                        f"workflow.max_fix_iterations={raw_max_fix!r} is a float, "
                        f"not an integer; the unlimited sentinel must be the literal "
                        f"int 0 or null/None, not 0.0. Falling back to default "
                        f"{DEFAULT_MAX_FIX_ITERATIONS} — write `max_fix_iterations: 0` "
                        f"or `max_fix_iterations: null` to opt into unlimited."
                    )
                else:
                    logger.warning(
                        f"workflow.max_fix_iterations={raw_max_fix!r} is not a valid integer; "
                        f"falling back to default {DEFAULT_MAX_FIX_ITERATIONS}"
                    )
                max_fix = DEFAULT_MAX_FIX_ITERATIONS
            else:
                try:
                    max_fix = int(raw_max_fix)
                except (TypeError, ValueError):
                    logger.warning(
                        f"workflow.max_fix_iterations={raw_max_fix!r} is not a valid integer; "
                        f"falling back to default {DEFAULT_MAX_FIX_ITERATIONS}"
                    )
                    max_fix = DEFAULT_MAX_FIX_ITERATIONS
        if max_fix < 0:
            raise ConfigError(
                f"workflow.max_fix_iterations={max_fix!r} must be >= 0 "
                f"(use 0 or null for unlimited)"
            )

        passes_explicit = "self_check_passes_required" in workflow_data
        raw_passes = workflow_data.get(
            "self_check_passes_required", DEFAULT_SELF_CHECK_PASSES_REQUIRED
        )
        # Tolerant parsing: bool/float/non-integer types warn and fall back
        # to the default. Out-of-scope of the unlimited-sentinel work; we
        # only fail-fast on the explicit < 1 case below (preserved per spec
        # Scenario "self_check_passes_required=0 fail-fast"), since that
        # case is the documented invariant users opted into.
        if isinstance(raw_passes, bool) or isinstance(raw_passes, float):
            logger.warning(
                f"workflow.self_check_passes_required={raw_passes!r} is not a valid integer; "
                f"falling back to default {DEFAULT_SELF_CHECK_PASSES_REQUIRED}"
            )
            passes = DEFAULT_SELF_CHECK_PASSES_REQUIRED
        else:
            try:
                passes = int(raw_passes)
            except (TypeError, ValueError):
                logger.warning(
                    f"workflow.self_check_passes_required={raw_passes!r} is not a valid integer; "
                    f"falling back to default {DEFAULT_SELF_CHECK_PASSES_REQUIRED}"
                )
                passes = DEFAULT_SELF_CHECK_PASSES_REQUIRED
        if passes < 1:
            raise ConfigError(
                f"workflow.self_check_passes_required={passes!r} must be >= 1"
            )

        raw_convergence = workflow_data.get("self_check_convergence_enabled")
        convergence = _coerce_bool(
            raw_convergence,
            default=DEFAULT_SELF_CHECK_CONVERGENCE_ENABLED,
        )
        if raw_convergence is not None and not isinstance(raw_convergence, (bool, int, float)):
            if isinstance(raw_convergence, str):
                if raw_convergence.strip().lower() not in (
                    "true", "1", "yes", "on", "false", "0", "no", "off",
                ):
                    logger.warning(
                        f"workflow.self_check_convergence_enabled={raw_convergence!r} is not a valid boolean; "
                        f"falling back to default {DEFAULT_SELF_CHECK_CONVERGENCE_ENABLED}"
                    )
            else:
                logger.warning(
                    f"workflow.self_check_convergence_enabled={raw_convergence!r} is not a valid boolean; "
                    f"falling back to default {DEFAULT_SELF_CHECK_CONVERGENCE_ENABLED}"
                )

        # baseline_fix_max_attempts (mechanism B): per-flow cap on looping
        # inherited (baseline) failures, independent of max_fix_iterations.
        # ``0`` disables baseline looping; negatives fail-fast. bool/float/
        # non-integer types warn and fall back to the default — mirrors
        # ``self_check_passes_required`` handling above.
        raw_baseline = workflow_data.get(
            "baseline_fix_max_attempts", DEFAULT_BASELINE_FIX_MAX_ATTEMPTS
        )
        if isinstance(raw_baseline, bool) or isinstance(raw_baseline, float):
            logger.warning(
                f"workflow.baseline_fix_max_attempts={raw_baseline!r} is not a valid integer; "
                f"falling back to default {DEFAULT_BASELINE_FIX_MAX_ATTEMPTS}"
            )
            baseline_attempts = DEFAULT_BASELINE_FIX_MAX_ATTEMPTS
        else:
            try:
                baseline_attempts = int(raw_baseline)
            except (TypeError, ValueError):
                logger.warning(
                    f"workflow.baseline_fix_max_attempts={raw_baseline!r} is not a valid integer; "
                    f"falling back to default {DEFAULT_BASELINE_FIX_MAX_ATTEMPTS}"
                )
                baseline_attempts = DEFAULT_BASELINE_FIX_MAX_ATTEMPTS
        if baseline_attempts < 0:
            raise ConfigError(
                f"workflow.baseline_fix_max_attempts={baseline_attempts!r} must be >= 0 "
                f"(use 0 to disable baseline looping)"
            )

        # self_check_defer_fix_threshold (item 1): below this many non-critical/
        # high issues, a non-terminal self_check pass defers its fix so the
        # remaining nested passes run first. ``None`` (null) is normalized to 0
        # (= disabled), mirroring the max_fix_iterations sentinel handling.
        # bool/float/non-integer types warn and fall back to the default; a
        # negative value is rejected fail-fast (mirrors baseline_fix_max_attempts).
        if (
            "self_check_defer_fix_threshold" in workflow_data
            and workflow_data["self_check_defer_fix_threshold"] is None
        ):
            defer_threshold = 0
        else:
            raw_defer = workflow_data.get(
                "self_check_defer_fix_threshold", DEFAULT_SELF_CHECK_DEFER_FIX_THRESHOLD
            )
            if isinstance(raw_defer, bool) or isinstance(raw_defer, float):
                logger.warning(
                    f"workflow.self_check_defer_fix_threshold={raw_defer!r} is not a valid integer; "
                    f"falling back to default {DEFAULT_SELF_CHECK_DEFER_FIX_THRESHOLD}"
                )
                defer_threshold = DEFAULT_SELF_CHECK_DEFER_FIX_THRESHOLD
            else:
                try:
                    defer_threshold = int(raw_defer)
                except (TypeError, ValueError):
                    logger.warning(
                        f"workflow.self_check_defer_fix_threshold={raw_defer!r} is not a valid integer; "
                        f"falling back to default {DEFAULT_SELF_CHECK_DEFER_FIX_THRESHOLD}"
                    )
                    defer_threshold = DEFAULT_SELF_CHECK_DEFER_FIX_THRESHOLD
        if defer_threshold < 0:
            raise ConfigError(
                f"workflow.self_check_defer_fix_threshold={defer_threshold!r} must be >= 0 "
                f"(use 0 or null to disable deferral)"
            )

        return cls(
            max_fix_iterations=max_fix,
            self_check_passes_required=passes,
            self_check_convergence_enabled=convergence,
            baseline_fix_max_attempts=baseline_attempts,
            self_check_defer_fix_threshold=defer_threshold,
            self_check_passes_required_explicit=passes_explicit,
        )

    @classmethod
    def load(cls, project_root: Path) -> "WorkflowConfig":
        """Load workflow configuration from the active project YAML.

        Args:
            project_root: Project root directory.

        Raises:
            ConfigError: If ``self_check_passes_required`` is < 1 or
                ``max_fix_iterations`` is negative.
        """
        data, src = load_project_yaml(project_root)
        if not data:
            return cls()
        cfg = cls.from_dict(data)
        cls._log_effective_source(project_root, src, cfg)
        return cfg

    @staticmethod
    def _log_effective_source(
        project_root: Path, source_label: str, cfg: "WorkflowConfig",
    ) -> None:
        """Record which config file the resolved ``max_fix_iterations`` came from.

        ``se3.local.yaml`` shadows ``se3.yaml`` as a whole, so when both set
        ``workflow.max_fix_iterations`` the committed value can be silently
        overridden. Surfacing the winning source (and the resolved value) at
        load time makes that override visible. Deduped per resolved config path
        so the per-step ``load`` calls do not flood the log.
        """
        try:
            key = str(get_project_config_path(project_root).resolve())
        except OSError:
            key = source_label
        if key in _logged_workflow_source_for:
            return
        _logged_workflow_source_for.add(key)
        logger.info(
            "workflow config: max_fix_iterations=%d (effective source: %s)",
            cfg.max_fix_iterations, source_label,
        )


def load_workflow_config(project_root: Optional[Path] = None) -> WorkflowConfig:
    """Load workflow configuration from project.

    Args:
        project_root: Project root directory. If None, uses current working directory.

    Returns:
        WorkflowConfig instance with loaded or default settings.
    """
    if project_root is None:
        project_root = Path.cwd()
    return WorkflowConfig.load(project_root)


@dataclass
class TestConfig:
    """Test step configuration loaded from se3.yaml test: section."""

    command: Optional[str] = None
    timeout: int = 1800
    phases: list[dict] = field(default_factory=list)
    timeout_multiplier: float = 2.0
    min_dynamic_timeout: int = 30
    # Upper sanity cap on computed dynamic timeout. Without this, repeated
    # timeouts in the fix loop can compound the LLM's estimated duration
    # beyond any reasonable bound, masking a hung test as "just slow".
    max_dynamic_timeout: int = 14400  # 4 hours
    # Critical acceptance tests: a list of test-ID substrings/prefixes that
    # identify tests whose verification value is so high that a SKIP (or the
    # test going missing entirely) must be treated as a verification failure,
    # not a pass. Empty by default — this is an explicit opt-in so ordinary
    # platform/optional-dependency skips are never penalised. See the test
    # step's _detect_critical_failures for the matching/gating semantics.
    critical_tests: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, project_root: Path) -> "TestConfig":
        """Load test configuration from the active project YAML."""
        data, source_label = load_project_yaml(project_root)
        if not data:
            return cls()
        try:
            test_data = data.get("test", {})
            if not test_data:
                return cls()

            # Validate timeout_multiplier: clamp to >= 1.0 so a typo like
            # 0 / negative / 0.1 does not silently disable the feature.
            raw_multiplier = test_data.get("timeout_multiplier", 2.0)
            try:
                multiplier = float(raw_multiplier)
            except (TypeError, ValueError):
                logger.warning(
                    "Invalid timeout_multiplier %r in %s; using default 2.0",
                    raw_multiplier, source_label,
                )
                multiplier = 2.0
            if multiplier < 1.0:
                logger.warning(
                    "timeout_multiplier=%.3f is below the minimum of 1.0; clamping",
                    multiplier,
                )
                multiplier = 1.0

            timeout = int(test_data.get("timeout", 1800))

            # Parse min_dynamic_timeout with validation.
            raw_min = test_data.get("min_dynamic_timeout", 30)
            try:
                min_dyn = int(raw_min)
            except (TypeError, ValueError):
                logger.warning(
                    "Invalid min_dynamic_timeout %r in %s; using default 30",
                    raw_min, source_label,
                )
                min_dyn = 30
            if min_dyn < 1:
                logger.warning(
                    "min_dynamic_timeout=%d is below the minimum of 1; clamping",
                    min_dyn,
                )
                min_dyn = 1

            # Default max_dynamic_timeout respects the user's fallback timeout:
            # a project that deliberately sets test.timeout above 14400s (e.g.
            # a legitimately slow suite) should not have dynamic timeouts
            # silently capped below that explicit intent.
            default_max = max(14400, timeout)
            raw_max = test_data.get("max_dynamic_timeout", default_max)
            try:
                max_dyn = int(raw_max)
            except (TypeError, ValueError):
                logger.warning(
                    "Invalid max_dynamic_timeout %r in %s; using default %d",
                    raw_max, source_label, default_max,
                )
                max_dyn = default_max
            if max_dyn < min_dyn:
                logger.warning(
                    "max_dynamic_timeout=%d is below min_dynamic_timeout=%d; "
                    "raising max to match min",
                    max_dyn, min_dyn,
                )
                max_dyn = min_dyn

            # Parse critical_tests: must be a list of strings. A non-list
            # value is tolerated (reset to empty + warning) rather than
            # raising, matching the clamp-and-warn policy used for the other
            # fields above. Elements are coerced to str so YAML scalars
            # (e.g. an accidental bare number) do not break substring matching
            # downstream.
            raw_critical = test_data.get("critical_tests", [])
            if raw_critical is None:
                critical_tests: list[str] = []
            elif isinstance(raw_critical, list):
                critical_tests = [str(item) for item in raw_critical]
            else:
                logger.warning(
                    "test.critical_tests in %s is not a list (got %s); "
                    "ignoring (critical-test gating disabled)",
                    source_label, type(raw_critical).__name__,
                )
                critical_tests = []

            return cls(
                command=test_data.get("command"),
                timeout=timeout,
                phases=test_data.get("phases", []),
                timeout_multiplier=multiplier,
                min_dynamic_timeout=min_dyn,
                max_dynamic_timeout=max_dyn,
                critical_tests=critical_tests,
            )
        except Exception as e:
            logger.warning(
                "Failed to load TestConfig from %s, using defaults: %s",
                source_label, e,
            )
            return cls()

    def get_phases_for_run(self, is_fix_iteration: bool = False) -> list[dict]:
        """Get phases to run, filtering by fix loop if needed."""
        if not self.phases:
            return []
        if not is_fix_iteration:
            return self.phases
        return [p for p in self.phases if p.get("in_fix_loop", True)]


_USE_WORKTREE_ENV_VAR = "SE3_IMPLEMENT_USE_WORKTREE"


def _coerce_bool(value: Any, default: bool) -> bool:
    """Coerce a YAML/env scalar to bool, tolerating 'true'/'false'/'0'/'1'.

    Unknown strings fall back to ``default`` so a typo cannot silently
    flip behaviour. Real booleans pass through.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off"):
            return False
    return default


@dataclass
class ImplementConfig:
    """Implement step configuration loaded from se3.yaml implement: section."""

    group_loc_threshold: int = 300
    use_worktree: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> "ImplementConfig":
        """Create ImplementConfig from dictionary."""
        if not data:
            return cls()
        return cls(
            group_loc_threshold=int(data.get("group_loc_threshold", 300)),
            use_worktree=_coerce_bool(data.get("use_worktree", True), default=True),
        )

    @classmethod
    def load(cls, project_root: Path) -> "ImplementConfig":
        """Load implement configuration from the active project YAML."""
        data, _src = load_project_yaml(project_root)
        impl_data = data.get("implement", {}) if data else {}
        if not isinstance(impl_data, dict):
            impl_data = {}
        config = cls.from_dict(impl_data)

        env_raw = os.environ.get(_USE_WORKTREE_ENV_VAR)
        if env_raw is not None:
            config.use_worktree = _coerce_bool(env_raw, default=config.use_worktree)

        return config


@dataclass
class StepConfig:
    """Step sequence configuration loaded from se3.yaml steps: section.

    Allows appending optional steps (e.g. summarize) back into the default
    step sequence via configuration.

    Example se3.yaml:
        steps:
          append:
            - summarize
    """

    append_steps: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, project_root: Path) -> "StepConfig":
        """Load step configuration from the active project YAML."""
        data, _src = load_project_yaml(project_root)
        if not data:
            return cls()
        steps_data = data.get("steps", {})
        if not steps_data or not isinstance(steps_data, dict):
            return cls()
        append_raw = steps_data.get("append", [])
        if not isinstance(append_raw, list):
            return cls()
        return cls(append_steps=[str(s) for s in append_raw])


def load_step_config(project_root: Optional[Path] = None) -> StepConfig:
    """Load step configuration from project.

    Args:
        project_root: Project root directory. If None, uses current working directory.

    Returns:
        StepConfig instance with loaded or default settings.
    """
    if project_root is None:
        project_root = Path.cwd()
    return StepConfig.load(project_root)


def apply_step_config(steps: list, project_root: Optional[Path] = None) -> list:
    """Append configured steps to the step sequence.

    Reads ``steps.append`` from se3.yaml and appends valid StepType values
    to the end of the step sequence (if not already present).

    Args:
        steps: Original step sequence (list of StepType)
        project_root: Project root directory for loading config

    Returns:
        Modified step sequence with appended steps
    """
    config = load_step_config(project_root)
    if not config.append_steps:
        return steps

    from .engine.models import StepType

    # Build set of existing step values for dedup
    existing = {s.value if hasattr(s, "value") else str(s) for s in steps}

    result = list(steps)
    for step_name in config.append_steps:
        if step_name in existing:
            continue
        # Validate the step name against StepType enum
        try:
            step_type = StepType(step_name)
            result.append(step_type)
            existing.add(step_name)
        except ValueError:
            pass  # Ignore invalid step names

    return result


@dataclass
class SpecLoadingConfig:
    """Spec loading mode configuration per step.

    Controls whether downstream steps receive ``items`` (header + selected
    requirements only) or ``full_spec`` (entire spec file) content.
    """

    steps: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "SpecLoadingConfig":
        """Create from the ``spec_loading`` YAML section."""
        if not data:
            return cls()
        steps_raw = data.get("steps", {})
        if not isinstance(steps_raw, dict):
            logger.warning(
                "spec_loading.steps has invalid type %s (expected dict); "
                "using default empty config.",
                type(steps_raw).__name__,
            )
            return cls()
        steps: dict[str, str] = {}
        for step_name, mode in steps_raw.items():
            if isinstance(mode, str) and mode.strip().lower() in ("items", "full_spec"):
                steps[str(step_name)] = mode.strip().lower()
            else:
                logger.warning(
                    "spec_loading.steps.%s has invalid value %r; "
                    "expected 'items' or 'full_spec'. Skipping — "
                    "built-in default will apply.",
                    step_name, mode,
                )
                # Skip invalid entries so mode_for() falls through to the
                # per-step built-in default instead of shadowing it.
        return cls(steps=steps)

    # Steps that default to full_spec loading (can be overridden in se3.yaml)
    _DEFAULT_FULL_SPEC_STEPS = {"update_spec"}

    def mode_for(self, step_type: str) -> Literal["items", "full_spec"]:
        """Return the loading mode for *step_type*.

        Defaults to ``"items"`` for most steps. ``update_spec`` defaults to
        ``"full_spec"`` so the LLM can see all existing spec names for naming
        collision avoidance and cross-spec consistency checks. Both defaults
        can be overridden via ``spec_loading.steps.<step>`` in ``se3.yaml``.
        """
        # Explicit config always wins
        if step_type in self.steps:
            mode = self.steps[step_type]
            if mode in ("items", "full_spec"):
                return mode  # type: ignore[return-value]
            return "items"
        # Default: full_spec for update_spec, items for everything else
        if step_type in self._DEFAULT_FULL_SPEC_STEPS:
            return "full_spec"  # type: ignore[return-value]
        return "items"

    @classmethod
    def load(cls, project_root: Path) -> "SpecLoadingConfig":
        """Load spec loading configuration from the active project YAML."""
        data, _src = load_project_yaml(project_root)
        if not data:
            return cls()
        sl_data = data.get("spec_loading", {})
        if not sl_data or not isinstance(sl_data, dict):
            return cls()
        return cls.from_dict(sl_data)


def load_spec_loading_config(project_root: Optional[Path] = None) -> SpecLoadingConfig:
    """Load spec loading configuration from project.

    Args:
        project_root: Project root directory. If None, uses current working directory.

    Returns:
        SpecLoadingConfig instance with loaded or default settings.
    """
    if project_root is None:
        project_root = Path.cwd()
    return SpecLoadingConfig.load(project_root)


def get_max_fix_iterations(project_root: Optional[Path] = None) -> int:
    f"""Get the maximum number of fix iterations for the test-verify-fix loop.

    Reads from se3.yaml workflow.max_fix_iterations, defaults to {DEFAULT_MAX_FIX_ITERATIONS}.

    A return value of ``0`` is the sentinel for "unlimited" — fix-loop
    comparison points must treat ``max_iter == 0`` as no upper bound.
    Negative values are rejected at config load time.

    Args:
        project_root: Project root directory. If None, uses current working directory.

    Returns:
        Maximum number of fix iterations allowed (0 == unlimited).
    """
    if project_root is None:
        project_root = Path.cwd()
    else:
        project_root = Path(project_root)

    return WorkflowConfig.load(project_root).max_fix_iterations


@dataclass
class MergeConfig:
    """Merge command configuration loaded from se3.yaml merge: section."""

    strategy: str = "fast"
    # Default flipped to True in 4.13.x: `se3 merge` now deletes merged
    # branches (and archives their worktrees) by default. Pass
    # `--no-delete-merged` on the command line to opt out.
    delete_merged_default: bool = True
    strict_runtime_sync: bool = False
    max_conflict_resolve_iterations: int = 10

    @classmethod
    def from_dict(cls, data: dict) -> "MergeConfig":
        """Create MergeConfig from dictionary.

        Strategy strings are validated via ``MergeStrategy.from_str``;
        the removed ``default`` / ``robust`` names raise immediately so
        a stale config cannot silently change merge semantics.
        """
        if not data:
            return cls()
        from .engine.merge.conflict_resolver import MergeStrategy

        strategy_raw = data.get("strategy", "fast")
        # MergeStrategy.from_str raises ValueError with a migration hint
        # for the removed legacy names and for unknown values.  We
        # propagate it as a ConfigError so callers see the same
        # fail-fast surface used elsewhere in this module.
        try:
            strategy = MergeStrategy.from_str(strategy_raw).value
        except ValueError as exc:
            raise ConfigError(f"merge.strategy: {exc}") from exc

        max_iter_raw = data.get("max_conflict_resolve_iterations", 10)
        try:
            max_iter = int(max_iter_raw)
        except (TypeError, ValueError):
            logger.warning(
                "Invalid merge.max_conflict_resolve_iterations %r; "
                "falling back to default 10",
                max_iter_raw,
            )
            max_iter = 10
        if max_iter < 1:
            raise ConfigError(
                "merge.max_conflict_resolve_iterations must be >= 1, "
                f"got {max_iter}"
            )

        return cls(
            strategy=strategy,
            delete_merged_default=_coerce_bool(
                data.get("delete_merged_default", True), default=True,
            ),
            strict_runtime_sync=_coerce_bool(
                data.get("strict_runtime_sync", False), default=False,
            ),
            max_conflict_resolve_iterations=max_iter,
        )

    @classmethod
    def load(cls, project_root: Path) -> "MergeConfig":
        """Load merge configuration from the active project YAML."""
        data, _src = load_project_yaml(project_root)
        if not data:
            return cls()
        merge_data = data.get("merge", {})
        if not isinstance(merge_data, dict):
            return cls()
        return cls.from_dict(merge_data)


def load_merge_config(project_root: Optional[Path] = None) -> MergeConfig:
    """Load merge configuration from project.

    Args:
        project_root: Project root directory. If None, uses current working directory.

    Returns:
        MergeConfig instance with loaded or default settings.
    """
    if project_root is None:
        project_root = Path.cwd()
    return MergeConfig.load(project_root)


# Allowed values for ``claude_subprocess.setting_sources``.  Mirrors the
# Claude CLI ``--setting-sources`` flag accepted tokens.
_ALLOWED_SETTING_SOURCES = ("user", "project", "local")
_DEFAULT_SETTING_SOURCES = ("user",)


@dataclass
class ClaudeSubprocessConfig:
    """Configuration for SE3-spawned Claude CLI subprocesses.

    ``setting_sources`` controls which settings files Claude CLI loads
    when SE3 spawns it as a worker.  The default ``["user"]`` isolates
    SE3 workers from the target project's ``.claude/settings.json`` so
    that ``permissions.deny`` rules intended for the *downstream*
    project's sub-LLMs do not lock out SE3's own plan/implement/review
    children.  Set explicitly (e.g. ``["user", "project"]``) to opt back
    into project-level settings.
    """

    setting_sources: list[str] = field(
        default_factory=lambda: list(_DEFAULT_SETTING_SOURCES)
    )

    @classmethod
    def from_dict(cls, data: dict) -> "ClaudeSubprocessConfig":
        """Build a config from the ``claude_subprocess`` YAML section.

        Validates ``setting_sources``:
        - non-list / non-string-element → ``ValueError``
        - empty list → ``ValueError``
        - element outside ``{user, project, local}`` → ``ValueError``
        Missing key returns the built-in default ``["user"]``.
        """
        if not isinstance(data, dict):
            raise ValueError(
                "claude_subprocess: expected a mapping, got "
                f"{type(data).__name__}"
            )

        if "setting_sources" not in data:
            return cls()

        raw = data["setting_sources"]
        if not isinstance(raw, list):
            raise ValueError(
                "claude_subprocess.setting_sources must be a list of "
                f"strings, got {type(raw).__name__}. Allowed values: "
                f"{list(_ALLOWED_SETTING_SOURCES)}"
            )
        if len(raw) == 0:
            raise ValueError(
                "claude_subprocess.setting_sources must not be empty. "
                f"Allowed values: {list(_ALLOWED_SETTING_SOURCES)}"
            )

        normalized: list[str] = []
        for item in raw:
            if not isinstance(item, str):
                raise ValueError(
                    "claude_subprocess.setting_sources entries must be "
                    f"strings, got {type(item).__name__} ({item!r}). "
                    f"Allowed values: {list(_ALLOWED_SETTING_SOURCES)}"
                )
            if item not in _ALLOWED_SETTING_SOURCES:
                raise ValueError(
                    "claude_subprocess.setting_sources contains invalid "
                    f"value {item!r}. Allowed values: "
                    f"{list(_ALLOWED_SETTING_SOURCES)}"
                )
            normalized.append(item)

        return cls(setting_sources=normalized)

    @classmethod
    def load(cls, project_root: Optional[Path] = None) -> "ClaudeSubprocessConfig":
        """Load from the active project YAML, falling back to defaults."""
        if project_root is None:
            return cls()
        data, _src = load_project_yaml(project_root)
        if not data:
            return cls()
        section = data.get("claude_subprocess")
        if section is None:
            return cls()
        if not isinstance(section, dict):
            raise ValueError(
                "claude_subprocess: expected a mapping, got "
                f"{type(section).__name__}"
            )
        return cls.from_dict(section)


def load_claude_subprocess_config(
    project_root: Optional[Path] = None,
) -> ClaudeSubprocessConfig:
    """Load Claude subprocess configuration.

    Args:
        project_root: Project root directory.  ``None`` returns the
            built-in default (``setting_sources=["user"]``).

    Raises:
        ValueError: If ``claude_subprocess.setting_sources`` is set but
            invalid (empty list, non-list, or contains values outside
            ``{user, project, local}``).
    """
    return ClaudeSubprocessConfig.load(project_root)


# ---------------------------------------------------------------------------
# Server / auth configuration (multi-tenant control plane)
# ---------------------------------------------------------------------------
#
# These settings configure the central server's authentication and identity
# layer. Unlike the daemon/server *runtime* params documented in the
# se3-config spec (host / port / poll_interval, sourced from CLI flags), the
# ``server:`` section here lives in se3.yaml (and the global config) and drives
# the pluggable auth providers, UI session cookie security, the embedded sqlite
# path, and the local-auth rate-limit / lockout parameters. Every item has an
# explicit default so a server with no ``server:`` section still comes up with
# ``providers=['local']`` and fail-safe cookie attributes.
#
# The ``server`` key is a normal top-level config key, so it follows the
# documented merge rule "project-level config overrides global config at the
# top-level key level (no deep merge)": when the project YAML defines
# ``server:`` it wholly replaces the global one; otherwise the global
# ``server:`` (if any) is used.

DEFAULT_SERVER_DB_PATH = "~/.se3/server.db"

# Auth providers the registry knows how to assemble. ``local`` is the built-in
# self-managed username+password provider (v1 mandatory); ``oidc`` and
# ``proxy_header`` are disabled-by-default seams (see the future auth/oidc.py
# and auth/proxy_header.py modules) that later groups flesh out.
_KNOWN_AUTH_PROVIDERS = ("local", "oidc", "proxy_header")
_DEFAULT_AUTH_PROVIDERS = ("local",)

# Valid SameSite cookie attribute values (compared lower-cased).
_VALID_COOKIE_SAMESITE = ("lax", "strict", "none")

# Default OIDC scopes when the (disabled) seam is configured.
_DEFAULT_OIDC_SCOPES = ("openid", "email", "profile")


def _coerce_positive_int(value: Any, default: int, label: str) -> int:
    """Coerce ``value`` to a positive int, warning + falling back on failure.

    Booleans are rejected (``True`` / ``False`` are not meaningful counts) and
    non-positive results fall back to ``default`` so a typo cannot silently
    disable a lockout / rate-limit window.
    """
    if isinstance(value, bool):
        logger.warning(
            "%s=%r is not a valid integer; using default %d", label, value, default,
        )
        return default
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        logger.warning(
            "%s=%r is not a valid integer; using default %d", label, value, default,
        )
        return default
    if coerced <= 0:
        logger.warning(
            "%s=%d must be positive; using default %d", label, coerced, default,
        )
        return default
    return coerced


def _str_or_default(value: Any, default: str, label: str) -> str:
    """Return ``value`` when it is a non-empty string, else warn + default."""
    if isinstance(value, str) and value.strip():
        return value
    logger.warning(
        "%s=%r is not a non-empty string; using default %r", label, value, default,
    )
    return default


def _parse_auth_providers(raw: Any) -> list[str]:
    """Validate ``server.auth.providers`` into a deduplicated known-name list.

    Unknown / blank / non-string entries are dropped with a warning. An
    absent, non-list, or fully-invalid value falls back to ``['local']`` so the
    server never comes up with an empty provider chain (which would otherwise
    mean "no way to authenticate"). Fail-closed enforcement (refusing to serve
    when no usable provider is configured) is the auth registry's job in a
    later group; here we only guarantee a sane, typed default.
    """
    if raw is None:
        return list(_DEFAULT_AUTH_PROVIDERS)
    if not isinstance(raw, list):
        logger.warning(
            "server.auth.providers is not a list (got %s); using default %s",
            type(raw).__name__, list(_DEFAULT_AUTH_PROVIDERS),
        )
        return list(_DEFAULT_AUTH_PROVIDERS)
    result: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            logger.warning(
                "server.auth.providers entry %r is not a non-empty string; "
                "skipping", entry,
            )
            continue
        name = entry.strip().lower()
        if name not in _KNOWN_AUTH_PROVIDERS:
            logger.warning(
                "server.auth.providers entry %r is unknown (known: %s); "
                "skipping", entry, list(_KNOWN_AUTH_PROVIDERS),
            )
            continue
        if name not in result:
            result.append(name)
    if not result:
        logger.warning(
            "server.auth.providers resolved to no valid providers; using "
            "default %s", list(_DEFAULT_AUTH_PROVIDERS),
        )
        return list(_DEFAULT_AUTH_PROVIDERS)
    return result


@dataclass
class SessionConfig:
    """UI session cookie security attributes for the local auth provider.

    Defaults are fail-safe for a public deployment behind a TLS-terminating
    reverse proxy: ``Secure`` + ``HttpOnly`` cookies with ``SameSite=lax``.
    """

    cookie_name: str = "se3_session"
    cookie_secure: bool = True
    cookie_httponly: bool = True
    cookie_samesite: str = "lax"
    max_age_seconds: int = 86400  # 24h session lifetime

    @classmethod
    def from_dict(cls, data: Any) -> "SessionConfig":
        if not isinstance(data, dict) or not data:
            return cls()
        samesite_raw = data.get("cookie_samesite", cls.cookie_samesite)
        samesite = samesite_raw.lower() if isinstance(samesite_raw, str) else ""
        if samesite not in _VALID_COOKIE_SAMESITE:
            logger.warning(
                "server.auth.session.cookie_samesite=%r is invalid (expected "
                "one of %s); using default %r",
                samesite_raw, list(_VALID_COOKIE_SAMESITE), cls.cookie_samesite,
            )
            samesite = cls.cookie_samesite
        return cls(
            cookie_name=_str_or_default(
                data.get("cookie_name", cls.cookie_name), cls.cookie_name,
                "server.auth.session.cookie_name",
            ),
            cookie_secure=_coerce_bool(
                data.get("cookie_secure", cls.cookie_secure),
                default=cls.cookie_secure,
            ),
            cookie_httponly=_coerce_bool(
                data.get("cookie_httponly", cls.cookie_httponly),
                default=cls.cookie_httponly,
            ),
            cookie_samesite=samesite,
            max_age_seconds=_coerce_positive_int(
                data.get("max_age_seconds", cls.max_age_seconds),
                cls.max_age_seconds, "server.auth.session.max_age_seconds",
            ),
        )


@dataclass
class LocalAuthConfig:
    """Login lockout / rate-limit parameters for the local auth provider.

    ``max_failed_attempts`` consecutive failures lock an account for
    ``lockout_seconds``; independently, at most ``ratelimit_max_attempts``
    login attempts are accepted per ``ratelimit_window_seconds`` window. Both
    guards exist to blunt brute-force attacks (security baseline).
    """

    max_failed_attempts: int = 5
    lockout_seconds: int = 300  # 5 minutes
    ratelimit_window_seconds: int = 60
    ratelimit_max_attempts: int = 10

    @classmethod
    def from_dict(cls, data: Any) -> "LocalAuthConfig":
        if not isinstance(data, dict) or not data:
            return cls()
        return cls(
            max_failed_attempts=_coerce_positive_int(
                data.get("max_failed_attempts", cls.max_failed_attempts),
                cls.max_failed_attempts,
                "server.auth.local.max_failed_attempts",
            ),
            lockout_seconds=_coerce_positive_int(
                data.get("lockout_seconds", cls.lockout_seconds),
                cls.lockout_seconds, "server.auth.local.lockout_seconds",
            ),
            ratelimit_window_seconds=_coerce_positive_int(
                data.get("ratelimit_window_seconds", cls.ratelimit_window_seconds),
                cls.ratelimit_window_seconds,
                "server.auth.local.ratelimit_window_seconds",
            ),
            ratelimit_max_attempts=_coerce_positive_int(
                data.get("ratelimit_max_attempts", cls.ratelimit_max_attempts),
                cls.ratelimit_max_attempts,
                "server.auth.local.ratelimit_max_attempts",
            ),
        )


@dataclass
class OidcConfig:
    """OIDC social-login provider seam — disabled by default (v1 optional).

    This is only a config seam so the schema does not block a future OIDC
    provider; the provider itself is not implemented in v1. When ``enabled``
    is false the remaining fields are inert.
    """

    enabled: bool = False
    issuer: Optional[str] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    redirect_url: Optional[str] = None
    scopes: list[str] = field(
        default_factory=lambda: list(_DEFAULT_OIDC_SCOPES),
    )

    @classmethod
    def from_dict(cls, data: Any) -> "OidcConfig":
        if not isinstance(data, dict) or not data:
            return cls()

        def _opt_str(key: str) -> Optional[str]:
            val = data.get(key)
            if val is None:
                return None
            if isinstance(val, str) and val.strip():
                return val
            logger.warning(
                "server.auth.oidc.%s=%r is not a non-empty string; ignoring",
                key, val,
            )
            return None

        scopes_raw = data.get("scopes")
        if scopes_raw is None:
            scopes = list(_DEFAULT_OIDC_SCOPES)
        elif isinstance(scopes_raw, list) and all(
            isinstance(s, str) and s.strip() for s in scopes_raw
        ) and scopes_raw:
            scopes = list(scopes_raw)
        else:
            logger.warning(
                "server.auth.oidc.scopes=%r is not a non-empty list of "
                "strings; using default %s",
                scopes_raw, list(_DEFAULT_OIDC_SCOPES),
            )
            scopes = list(_DEFAULT_OIDC_SCOPES)

        return cls(
            enabled=_coerce_bool(data.get("enabled", False), default=False),
            issuer=_opt_str("issuer"),
            client_id=_opt_str("client_id"),
            client_secret=_opt_str("client_secret"),
            redirect_url=_opt_str("redirect_url"),
            scopes=scopes,
        )


@dataclass
class ProxyHeaderConfig:
    """Reverse-proxy trusted-identity-header provider seam — disabled by default.

    Hard security precondition when enabled (enforced by a later group, not
    here): the reverse proxy MUST strip any client-supplied copy of ``header``
    and the server MUST NOT be reachable while bypassing the proxy; otherwise
    the injected identity is forgeable and this is an authz hole. v1 ships the
    seam only.
    """

    enabled: bool = False
    trust_proxy: bool = False
    header: str = "X-Forwarded-Email"

    @classmethod
    def from_dict(cls, data: Any) -> "ProxyHeaderConfig":
        if not isinstance(data, dict) or not data:
            return cls()
        return cls(
            enabled=_coerce_bool(data.get("enabled", False), default=False),
            trust_proxy=_coerce_bool(data.get("trust_proxy", False), default=False),
            header=_str_or_default(
                data.get("header", cls.header), cls.header,
                "server.auth.proxy_header.header",
            ),
        )


@dataclass
class AuthConfig:
    """Pluggable authentication configuration (A-layer).

    ``providers`` is the ordered chain of auth providers the registry should
    assemble; ``session`` / ``local`` configure the built-in local provider;
    ``oidc`` / ``proxy_header`` are disabled-by-default seams.
    """

    providers: list[str] = field(
        default_factory=lambda: list(_DEFAULT_AUTH_PROVIDERS),
    )
    session: SessionConfig = field(default_factory=SessionConfig)
    local: LocalAuthConfig = field(default_factory=LocalAuthConfig)
    oidc: OidcConfig = field(default_factory=OidcConfig)
    proxy_header: ProxyHeaderConfig = field(default_factory=ProxyHeaderConfig)

    @classmethod
    def from_dict(cls, data: Any) -> "AuthConfig":
        if not isinstance(data, dict) or not data:
            return cls()
        return cls(
            providers=_parse_auth_providers(data.get("providers")),
            session=SessionConfig.from_dict(data.get("session", {})),
            local=LocalAuthConfig.from_dict(data.get("local", {})),
            oidc=OidcConfig.from_dict(data.get("oidc", {})),
            proxy_header=ProxyHeaderConfig.from_dict(data.get("proxy_header", {})),
        )


@dataclass
class ServerConfig:
    """Central-server auth / identity configuration (``server:`` YAML section).

    Loaded with global→project top-level-key override (no deep merge), matching
    the documented config precedence. All fields default so an absent
    ``server:`` section yields ``db_path='~/.se3/server.db'`` and
    ``auth.providers=['local']``.
    """

    db_path: str = DEFAULT_SERVER_DB_PATH
    auth: AuthConfig = field(default_factory=AuthConfig)

    @classmethod
    def from_dict(cls, data: Any) -> "ServerConfig":
        if not isinstance(data, dict) or not data:
            return cls()
        db_path = _str_or_default(
            data.get("db_path", DEFAULT_SERVER_DB_PATH),
            DEFAULT_SERVER_DB_PATH, "server.db_path",
        )
        return cls(
            db_path=db_path,
            auth=AuthConfig.from_dict(data.get("auth", {})),
        )

    @classmethod
    def load(cls, project_root: Optional[Path]) -> "ServerConfig":
        """Load the ``server:`` config from global + project YAML.

        ``server`` is a normal top-level key: the project ``server:`` section,
        when present, wholly replaces the global one (no deep merge). Missing /
        non-mapping sections fall back to built-in defaults.
        """
        global_data, project_data, _src = _load_agent_configs(project_root)
        server_data = project_data.get("server")
        if server_data is None:
            server_data = global_data.get("server")
        if not isinstance(server_data, dict):
            server_data = {}
        return cls.from_dict(server_data)

    def resolved_db_path(self) -> Path:
        """Return ``db_path`` with ``~`` expanded to an absolute Path."""
        return Path(self.db_path).expanduser()


def load_server_config(project_root: Optional[Path] = None) -> ServerConfig:
    """Load central-server auth / identity configuration.

    Args:
        project_root: Project root directory. If None, uses the current
            working directory (and still reads the global ``~/.se3/config.yaml``).

    Returns:
        ServerConfig instance with loaded or default settings.
    """
    if project_root is None:
        project_root = Path.cwd()
    return ServerConfig.load(project_root)
