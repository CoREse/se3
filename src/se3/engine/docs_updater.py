"""Documentation updater for maintaining README.md and VERSIONS.md.

This module provides the DocumentationUpdater class for updating
version documentation in SE3 projects based on configurable templates.
"""

from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime
import re


def _versions_md_template_path() -> Path:
    """Return the path to the packaged ``versions_md.md`` template.

    Resolved relative to this module so it works regardless of the
    project being operated on (mirrors
    ``version_script_interface._load_template``). Factored out as a
    module-level function so tests can monkeypatch the location.
    """
    return Path(__file__).parent.parent / "templates" / "versions_md.md"


def _versions_entry_template_from_file() -> Optional[str]:
    """Read the first ``##`` section block of the ``versions_md.md`` template.

    Used as a fallback ``versions_entry`` template when the caller's
    config does not supply ``versions_entry_template``. Returns the block
    spanning the first ``## `` heading through the line before the next
    ``## `` heading (or end of file).

    The block is only returned when it is a genuine *entry template* — it
    MUST contain both the ``{{version}}`` and ``{{changes}}`` placeholders
    that :class:`Template` substitutes. The packaged ``versions_md.md`` is
    an *init* template whose first section is a concrete release entry
    (single-brace ``{date}``, a hardcoded version, and no ``{{changes}}``);
    treating that as an entry template would silently discard the new
    version, date, and changelog bullets, so such a block is rejected here
    and the caller falls through to ``DEFAULT_VERSIONS_ENTRY_TEMPLATE``.

    Returns ``None`` when the template file is absent / unreadable /
    contains no ``## `` heading, or when the first section is not a valid
    placeholder-based entry template. Never raises.
    """
    template_path = _versions_md_template_path()
    try:
        text = template_path.read_text(encoding="utf-8")
    except OSError:
        return None

    lines = text.split("\n")
    start = None
    for i, line in enumerate(lines):
        if line.startswith("## "):
            start = i
            break
    if start is None:
        return None

    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break

    block = "\n".join(lines[start:end]).strip("\n")
    if not block.strip():
        return None

    # Only accept the section as an entry template when it actually carries
    # the dynamic placeholders. Otherwise it is a concrete entry (e.g. the
    # init template's "## 0.1.0 - {date}" block) that render() cannot fill.
    if "{{version}}" not in block or "{{changes}}" not in block:
        return None

    return block


class Template:
    """Template configuration for documentation updates."""

    def __init__(self, content: str, placeholders: Optional[Dict[str, str]] = None):
        """Initialize template with content and optional placeholder defaults.

        Args:
            content: The template content with placeholders
            placeholders: Default values for placeholders
        """
        self.content = content
        self.placeholders = placeholders or {}

    def render(self, context: Dict[str, str]) -> str:
        """Render template by replacing placeholders with context values.

        Args:
            context: Dictionary of placeholder values

        Returns:
            Rendered template string
        """
        result = self.content
        merged_context = {**self.placeholders, **context}
        for key, value in merged_context.items():
            result = result.replace(f"{{{{{key}}}}}", str(value))
        return result


class DocumentationUpdater:
    """Updates README.md and VERSIONS.md with version information.

    This class handles:
    - README.md version badge/header replacement
    - VERSIONS.md changelog entry insertion
    - Template-based rendering with placeholder replacement
    """

    # Default templates
    DEFAULT_README_BADGE_TEMPLATE = "![Version](https://img.shields.io/badge/version-{{version}}-blue)"
    DEFAULT_VERSIONS_ENTRY_TEMPLATE = """## {{version}} - {{date}}

{{changes}}

"""

    # Body written for a version block that carries no changelog bullets. The
    # released number must still land a VERSIONS.md header (historical_versions()
    # anti-collision source of truth), but this placeholder is mutually exclusive
    # with real bullets — merge logic drops it the moment a real bullet arrives.
    NO_CHANGES_PLACEHOLDER = "- No changes recorded"

    def __init__(self, project_root: Path, config: Optional[Dict[str, Any]] = None):
        """Initialize the documentation updater.

        Args:
            project_root: Root directory of the project
            config: Optional configuration dictionary with templates
        """
        self.project_root = project_root
        self.config = config or {}
        self.templates = self._load_templates()

    def _load_templates(self) -> Dict[str, Template]:
        """Load templates from configuration or use defaults.

        Returns:
            Dictionary of template name to Template objects
        """
        templates = {}

        # README badge template
        readme_badge = self.config.get("readme_badge_template", self.DEFAULT_README_BADGE_TEMPLATE)
        templates["readme_badge"] = Template(readme_badge)

        # README header template (optional)
        if "readme_header_template" in self.config:
            templates["readme_header"] = Template(self.config["readme_header_template"])

        # VERSIONS entry template. Precedence:
        #   1. config["versions_entry_template"] (explicit override)
        #   2. first ## block of the packaged versions_md.md template
        #   3. DEFAULT_VERSIONS_ENTRY_TEMPLATE (built-in fallback)
        if "versions_entry_template" in self.config:
            versions_entry = self.config["versions_entry_template"]
        else:
            versions_entry = _versions_entry_template_from_file()
            if versions_entry is None:
                versions_entry = self.DEFAULT_VERSIONS_ENTRY_TEMPLATE
        templates["versions_entry"] = Template(versions_entry)

        return templates

    def update_readme(
        self,
        version: str,
        template_name: Optional[str] = None,
        additional_context: Optional[Dict[str, str]] = None
    ) -> None:
        """Update README.md with new version information.

        Replaces version badges and headers using configurable patterns.

        Args:
            version: The new version string
            template_name: Optional template name to use (default: auto-detect)
            additional_context: Additional context for template rendering

        Raises:
            FileNotFoundError: If README.md does not exist
        """
        readme_path = self.project_root / "README.md"
        if not readme_path.exists():
            raise FileNotFoundError(f"README.md not found at {readme_path}")

        content = readme_path.read_text(encoding="utf-8")
        original_content = content

        # Build context with version and date
        context = self._build_context(version, additional_context)

        # Update version badge
        content = self._replace_version_badge(content, version, context)

        # Update version header if template configured
        if "readme_header" in self.templates:
            content = self._replace_version_header(content, version, context)

        # Only write if content changed
        if content != original_content:
            readme_path.write_text(content, encoding="utf-8")

    def _build_context(
        self,
        version: str,
        additional_context: Optional[Dict[str, str]] = None
    ) -> Dict[str, str]:
        """Build context dictionary for template rendering.

        Args:
            version: The version string
            additional_context: Additional context values

        Returns:
            Merged context dictionary
        """
        context = {
            "version": version,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "year": str(datetime.now().year),
        }
        if additional_context:
            context.update(additional_context)
        return context

    def _replace_version_badge(self, content: str, version: str, context: Dict[str, str]) -> str:
        """Replace version badge in README content.

        Args:
            content: README content
            version: New version string
            context: Template context

        Returns:
            Updated content
        """
        # Common badge patterns
        badge_patterns = [
            # Markdown badge: ![Version](.../version-x.x.x-...)
            (r'!\[Version\]\([^)]*version-[^-\s)]*-[^)]*\)', 'version'),
            # Markdown badge: ![version](...)
            (r'!\[version\]\([^)]*\)', 'version'),
            # HTML badge with version
            (r'<img[^>]*version[^>]*>', 'version'),
        ]

        template = self.templates.get("readme_badge")
        if not template:
            return content

        new_badge = template.render(context)

        # Try to replace existing badge
        for pattern, _ in badge_patterns:
            if re.search(pattern, content, re.IGNORECASE):
                return re.sub(pattern, new_badge, content, count=1, flags=re.IGNORECASE)

        # No existing badge found. Find the insertion point by skipping a
        # leading YAML front-matter block and any leading HTML comment
        # lines, so the badge lands after the real title heading instead
        # of breaking front-matter or sitting above a license comment.
        lines = content.split('\n')
        n = len(lines)
        idx = 0

        # Skip a leading YAML front-matter block (--- ... ---). Only when
        # the very first line is a bare --- and a closing --- follows.
        if idx < n and lines[idx].strip() == '---':
            close = None
            for j in range(idx + 1, n):
                if lines[j].strip() == '---':
                    close = j
                    break
            if close is not None:
                idx = close + 1

        # Skip leading blank lines and whole-line HTML comments (which may
        # span multiple lines, e.g. a license header).
        while idx < n:
            stripped = lines[idx].strip()
            if stripped == '':
                idx += 1
            elif stripped.startswith('<!--'):
                if stripped.endswith('-->'):
                    idx += 1
                else:
                    closed = False
                    for j in range(idx + 1, n):
                        if lines[j].strip().endswith('-->'):
                            idx = j + 1
                            closed = True
                            break
                    if not closed:
                        break
            else:
                break

        if idx < n and lines[idx].startswith('#'):
            # First real content line is a markdown heading — insert the
            # badge right after it (blank line + badge), preserving the
            # heading and any front-matter / comments above it.
            lines.insert(idx + 1, '')
            lines.insert(idx + 2, new_badge)
            return '\n'.join(lines)
        else:
            # No heading at the top — prepend the badge.
            return new_badge + '\n\n' + content

    def _replace_version_header(self, content: str, version: str, context: Dict[str, str]) -> str:
        """Replace version header in README content.

        Args:
            content: README content
            version: New version string
            context: Template context

        Returns:
            Updated content
        """
        template = self.templates.get("readme_header")
        if not template:
            return content

        new_header = template.render(context)

        # Look for version header patterns
        header_patterns = [
            r'^#+ .*[Vv]ersion.*$',
            r'^\*\*[Vv]ersion:\*\*.*$',
        ]

        for pattern in header_patterns:
            if re.search(pattern, content, re.MULTILINE):
                return re.sub(pattern, new_header, content, count=1, flags=re.MULTILINE)

        # No existing header found, don't add one
        return content

    def update_versions_md(
        self,
        version: str,
        changes: List[str],
        template_name: Optional[str] = None,
        additional_context: Optional[Dict[str, str]] = None
    ) -> None:
        """Update VERSIONS.md with new changelog entry.

        Inserts new version entries in proper changelog format while
        preserving existing changelog history.

        Args:
            version: The new version string
            changes: List of change descriptions for this version
            template_name: Optional template name to use
            additional_context: Additional context for template rendering

        Raises:
            FileNotFoundError: If VERSIONS.md does not exist (will create if allowed)
        """
        versions_path = self.project_root / "VERSIONS.md"

        # Build context
        context = self._build_context(version, additional_context)
        context["changes"] = self._format_changes(changes)

        # Get template
        if template_name and template_name in self.templates:
            template = self.templates[template_name]
        else:
            template = self.templates.get("versions_entry", Template(self.DEFAULT_VERSIONS_ENTRY_TEMPLATE))

        new_entry = template.render(context)

        if versions_path.exists():
            content = versions_path.read_text(encoding="utf-8")
            content = self._insert_version_entry(content, new_entry, version)
        else:
            # Create new VERSIONS.md with header
            content = self._create_new_versions_md(new_entry)

        versions_path.write_text(content, encoding="utf-8")

    def _format_changes(self, changes: List[str]) -> str:
        """Format list of changes into markdown.

        Args:
            changes: List of change descriptions

        Returns:
            Formatted markdown string
        """
        if not changes:
            return self.NO_CHANGES_PLACEHOLDER

        formatted = []
        for change in changes:
            # Ensure each change starts with bullet
            if not change.strip().startswith('-'):
                change = '- ' + change
            formatted.append(change)

        return '\n'.join(formatted)

    def _insert_version_entry(self, content: str, new_entry: str, version: str) -> str:
        """Insert (or merge) a version entry into existing VERSIONS.md content.

        When ``version`` already has an entry, the new changelog bullets are
        *merged* into that block rather than discarded. The previous
        "version exists -> return unchanged" branch silently swallowed a
        distinct feature's changelog whenever two concurrent flows landed on
        the same version number (the 11.12.0 collision), so a byte-identical
        re-write must never lose entries.

        Args:
            content: Existing VERSIONS.md content
            new_entry: New rendered entry (``## ver - date`` + changes)
            version: Version string (for locating an existing block)

        Returns:
            Updated content
        """
        # Normalising here (not only on the create path) is what drains the
        # historical head-blank accumulation on the first write and keeps
        # every subsequent insertion from re-growing it.
        content = self._normalize_head_blanks(content)
        lines = content.split('\n')

        # Merge path: an entry for this exact version already exists.
        version_pattern = self._version_header_pattern(version)
        for i, line in enumerate(lines):
            if version_pattern.match(line):
                return self._merge_change_lines(lines, i, new_entry, version)

        entry = new_entry.rstrip('\n')

        # New version: insert directly above the newest existing version
        # block. Head normalization guarantees exactly one blank line
        # between the title and that block, and entries abut each other with
        # no separating blank (the established VERSIONS.md style), so no
        # extra blank line is inserted here — that unconditional insert was
        # the source of the head-blank accumulation.
        for i, line in enumerate(lines):
            if line.startswith('## ') and not line.startswith('## Changelog'):
                lines.insert(i, entry)
                return '\n'.join(lines)

        # No existing version block: anchor to the title with a single blank
        # separator, or (title-less file) before the first prose line.
        for i, line in enumerate(lines):
            if line.startswith('# ') or line.startswith('## Changelog'):
                lines[i + 1:i + 1] = ['', entry]
                return '\n'.join(lines)
            if line.strip() and not line.startswith('#'):
                lines[i:i] = [entry, '']
                return '\n'.join(lines)

        # Empty / whitespace-only file: the entry is the whole content.
        if not content.strip():
            return entry
        return '\n'.join(lines + [entry])

    def _versions_entry_header_line(self) -> str:
        """The version-bearing header line of the effective ``versions_entry`` template.

        This is the line that carries ``{{version}}`` — the entry's header,
        whether or not it is a markdown heading. A non-heading template such as
        ``ENTRY {{version}} | {{changes}}`` has no ``#`` line, yet its
        version-bearing line is still the block header the existing-version
        detector and block-boundary marker must key off; keying only off ``#``
        headings made those helpers blind to it, so a second update inserted a
        duplicate block instead of merging bullets into the existing entry.

        Falls back to the first heading line when no line carries ``{{version}}``
        (defensive — an entry template should always contain it). Any heading
        level counts (``#`` … ``######``): a project may use a Keep-a-Changelog
        ``## [{{version}}] - {{date}}`` or a level-1 ``# {{version}}`` header.
        Returns ``""`` when no template / no usable line.
        """
        template = self.templates.get("versions_entry")
        if template is None:
            return ""
        lines = template.content.splitlines()
        for line in lines:
            if "{{version}}" in line:
                return line.strip()
        for line in lines:
            if line.strip().startswith("#"):
                return line.strip()
        return ""

    def _version_header_pattern(self, version: str) -> "re.Pattern[str]":
        """Regex matching an existing VERSIONS.md block header for *version*.

        Must recognise whatever header shape the project's ``versions_entry``
        template renders — the default ``## <version> - <date>``, a suffixless
        ``## {{version}}``, a level-1 ``# {{version}}``, AND a bracketed
        Keep-a-Changelog ``## [{{version}}] - {{date}}`` where a literal ``]``
        abuts the version with no separating whitespace. The old pattern only
        tolerated a *whitespace-separated* suffix, so ``## [1.2.3] - …`` was never
        recognised and a second update for the same version inserted a duplicate
        block instead of merging bullets into the existing one.

        Derived from the effective template: the literal anchor BEFORE
        ``{{version}}`` and the literal text immediately AFTER it (up to the next
        ``{{placeholder}}``) are both honoured, so an abutting ``]`` is matched
        while a variable date/build suffix stays flexible. Degrades to a
        permissive default (optional suffix) on any template-resolution fault.
        """
        header_line = self._versions_entry_header_line()
        if "{{version}}" in header_line:
            prefix = header_line.split("{{version}}", 1)[0].rstrip()
            after = header_line.split("{{version}}", 1)[1]
            # Literal text right after the version, up to the next placeholder
            # (e.g. ``]`` for ``[{{version}}]``, `` - `` for `` - {{date}}``).
            after_literal = after.split("{{", 1)[0].rstrip()
            try:
                if after_literal:
                    # Accept EITHER the template's literal tail (``]`` abutting
                    # the version, `` -`` before a date) OR a bare
                    # whitespace/EOL boundary. A same-version header written
                    # under a different / suffixless template shape (or
                    # hand-edited to plain ``## 1.2.3``) must still be recognised
                    # and merged into, not duplicated. The alternation is also
                    # the whole anti-prefix guard: ``11.13`` cannot match
                    # ``## [11.13.1]`` (next char ``]`` is the literal tail only
                    # for the full version) nor ``## 11.13.1`` (next char ``.``
                    # is neither the tail nor whitespace/EOL).
                    follow = (
                        r'(?:' + re.escape(after_literal) + r'|\s|$).*$'
                    )
                else:
                    # Version at end / before an immediate placeholder: require a
                    # whitespace boundary or EOL so ``11.13`` does not prefix-match
                    # header ``## 11.13.1``.
                    follow = r'(?:\s+.*)?\s*$'
                return re.compile(
                    r'^' + re.escape(prefix) + r'\s*' + re.escape(version) + follow
                )
            except re.error:
                pass
        return re.compile(rf'^#{{1,6}}\s+{re.escape(version)}(?:\s+-.*)?\s*$')

    def _version_header_marker(self) -> str:
        """The literal ``#``-prefix that begins a version block header line.

        Derived from the effective ``versions_entry`` template (default ``## ``)
        so block-boundary detection recognises whatever heading level the
        project renders — a custom ``### {{version}}`` block ends at the next
        ``### `` header, a level-1 ``# {{version}}`` block at the next ``# ``.
        Using the wrong level makes the block scan run past the next version
        header and merge bullets into an older block. For a non-heading template
        the literal text before ``{{version}}`` (e.g. ``ENTRY `` for
        ``ENTRY {{version}} | …``) begins every entry line and serves the same
        boundary role. Defaults to ``## `` on any template-resolution fault.
        """
        header_line = self._versions_entry_header_line()
        if header_line.startswith("#"):
            hashes = header_line[: len(header_line) - len(header_line.lstrip("#"))]
            if hashes:
                return hashes + " "
        if "{{version}}" in header_line:
            prefix = header_line.split("{{version}}", 1)[0]
            if prefix.strip():
                return prefix
        return "## "

    def _inline_changes_from_header(self, header_line: str, version: str) -> Optional[str]:
        """Extract the changelog text rendered ON the header line, if any.

        Some entry templates place ``{{changes}}`` on the same line as
        ``{{version}}`` (e.g. ``ENTRY {{version}} | {{changes}}``) rather than
        on following lines. For those the bullets live inside the header line,
        so the line-based merge — which only scans lines *after* the header —
        would miss them and silently drop a second update's changes. This
        recovers that inline text so it participates in dedupe/merge.

        Returns the rendered ``{{changes}}`` substring, or ``None`` when the
        template keeps changes on separate lines (the default markdown shape)
        or the header line does not match the template.
        """
        tmpl = self._versions_entry_header_line()
        if "{{version}}" not in tmpl or "{{changes}}" not in tmpl:
            return None
        # Reconstruct the rendered header from its template: literals match
        # verbatim, the known version is pinned, any other placeholder (e.g.
        # ``{{date}}``) is a wildcard, and ``{{changes}}`` captures the rest of
        # the inline text.
        parts = re.split(r"(\{\{[a-zA-Z_]+\}\})", tmpl)
        regex = "^"
        for part in parts:
            if part == "{{version}}":
                regex += re.escape(version)
            elif part == "{{changes}}":
                regex += r"(?P<changes>.*)"
            elif re.fullmatch(r"\{\{[a-zA-Z_]+\}\}", part):
                regex += r".*?"
            else:
                regex += re.escape(part)
        regex += "$"
        try:
            match = re.match(regex, header_line)
        except re.error:
            return None
        return match.group("changes") if match else None

    def _merge_change_lines(self, lines: List[str], header_index: int, new_entry: str, version: str) -> str:
        """Merge the new entry's changelog bullets into an existing block.

        Appends only bullets not already present (so a verbatim re-write is a
        no-op and repeated merges are idempotent), placing them after the
        last existing bullet of the block and before the next version header.

        Args:
            lines: VERSIONS.md split into lines
            header_index: Index of the existing ``## version`` header line
            new_entry: New rendered entry whose bullets are to be merged
            version: Version string (used to recover bullets rendered inline
                on the header line of a single-line entry template)

        Returns:
            Updated content
        """
        # Extent of the existing version block (up to the next version header).
        # Terminate on the effective header level (``## `` by default, ``### ``
        # for a custom template) so the same header-pattern family that located
        # this block also bounds it — otherwise a custom-level next header is
        # not recognised and bullets leak into the older block below it.
        marker = self._version_header_marker()
        block_end = len(lines)
        for j in range(header_index + 1, len(lines)):
            if lines[j].startswith(marker):
                block_end = j
                break

        placeholder = self.NO_CHANGES_PLACEHOLDER

        # Changelog bullets carried by the rendered entry: everything after its
        # own header line, blanks dropped, plus any bullets rendered inline ON
        # the header line for a single-line template (``ENTRY {{version}} |
        # {{changes}}``) — those would otherwise be missed and the update lost.
        # Its placeholder (if the new entry carried no real changes) is NOT a
        # mergeable bullet — a placeholder must never be appended beside real
        # bullets — so exclude it from what we add.
        new_entry_lines = new_entry.split('\n')
        new_inline = self._inline_changes_from_header(new_entry_lines[0], version)
        change_lines = [
            ln for ln in (new_inline.split('\n') if new_inline else [])
            if ln.strip()
        ]
        change_lines += [ln for ln in new_entry_lines[1:] if ln.strip()]
        new_real = [ln for ln in change_lines if ln.strip() != placeholder]

        # The existing block body (between this header and the next), blank
        # framing preserved. Split its content into the real bullets (drives the
        # dedupe set) vs. the stale placeholder (dropped once real bullets exist).
        # Bullets living inline on the existing header line join the dedupe set
        # too, so a re-write of the same version stays a no-op.
        block = lines[header_index + 1:block_end]
        existing_real = {
            ln.strip() for ln in block if ln.strip() and ln.strip() != placeholder
        }
        existing_inline = self._inline_changes_from_header(lines[header_index], version)
        if existing_inline:
            existing_real.update(
                ln.strip() for ln in existing_inline.split('\n')
                if ln.strip() and ln.strip() != placeholder
            )
        to_add = [ln for ln in new_real if ln.strip() not in existing_real]

        has_real = bool(existing_real) or bool(to_add)
        placeholder_present = any(ln.strip() == placeholder for ln in block)

        if has_real:
            # Real bullets win: drop any contradictory placeholder, then append
            # the new bullets before the block's trailing blank run. Nothing to do
            # when there are no additions and no placeholder to strip.
            if not to_add and not placeholder_present:
                return '\n'.join(lines)
            new_block = [ln for ln in block if ln.strip() != placeholder]
            insert_at = len(new_block)
            while insert_at > 0 and not new_block[insert_at - 1].strip():
                insert_at -= 1
            new_block[insert_at:insert_at] = to_add
        else:
            # No real bullets anywhere: keep exactly one placeholder as the body.
            if placeholder_present:
                return '\n'.join(lines)
            insert_at = len(block)
            while insert_at > 0 and not block[insert_at - 1].strip():
                insert_at -= 1
            new_block = block[:insert_at] + [placeholder] + block[insert_at:]

        lines[header_index + 1:block_end] = new_block
        return '\n'.join(lines)

    def _normalize_head_blanks(self, content: str) -> str:
        """Collapse the blank-line run just after the title to a single blank.

        Idempotent: a head already at one blank is returned unchanged, so
        repeated writes stay stable. Only the title heading (``# ...`` /
        ``## Changelog``) anchors cleanup; a file whose first heading is a
        version entry, or one with no heading, is left untouched.

        Args:
            content: VERSIONS.md content

        Returns:
            Content with a single blank line after the title
        """
        lines = content.split('\n')

        title_idx = None
        for i, line in enumerate(lines):
            if line.startswith('# ') or line.startswith('## Changelog'):
                title_idx = i
                break
            if line.strip():
                # First non-blank line is not a title (e.g. a version entry
                # or prose) — no title to anchor cleanup to.
                return content
        if title_idx is None:
            return content

        end = title_idx + 1
        while end < len(lines) and not lines[end].strip():
            end += 1

        if end >= len(lines):
            # Title followed only by blank lines: drop the trailing blanks.
            new_lines = lines[:title_idx + 1]
        else:
            new_lines = lines[:title_idx + 1] + [''] + lines[end:]
        return '\n'.join(new_lines)

    def _create_new_versions_md(self, first_entry: str) -> str:
        """Create new VERSIONS.md content.

        Args:
            first_entry: First version entry

        Returns:
            Complete VERSIONS.md content
        """
        return f"""# Version History

{first_entry}"""

    def render_template(self, template_name: str, context: Dict[str, str]) -> str:
        """Render a template with the given context.

        Args:
            template_name: Name of the template to render
            context: Context dictionary for placeholder replacement

        Returns:
            Rendered template string

        Raises:
            KeyError: If template not found
        """
        if template_name not in self.templates:
            raise KeyError(f"Template '{template_name}' not found")

        return self.templates[template_name].render(context)

    def add_template(self, name: str, content: str, placeholders: Optional[Dict[str, str]] = None) -> None:
        """Add a custom template.

        Args:
            name: Template name
            content: Template content with placeholders
            placeholders: Default placeholder values
        """
        self.templates[name] = Template(content, placeholders)

    def update_both(
        self,
        version: str,
        changes: List[str],
        additional_context: Optional[Dict[str, str]] = None
    ) -> Dict[str, bool]:
        """Update both README.md and VERSIONS.md in one call.

        Args:
            version: The new version string
            changes: List of change descriptions
            additional_context: Additional context for templates

        Returns:
            Dictionary with 'readme' and 'versions' keys indicating success
        """
        results = {"readme": False, "versions": False}

        try:
            self.update_readme(version, additional_context=additional_context)
            results["readme"] = True
        except FileNotFoundError:
            pass  # README.md doesn't exist

        try:
            self.update_versions_md(version, changes, additional_context=additional_context)
            results["versions"] = True
        except FileNotFoundError:
            pass  # VERSIONS.md doesn't exist and shouldn't be created

        return results
