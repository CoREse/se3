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
            return "- No changes recorded"

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
        version_pattern = rf'^##\s+{re.escape(version)}\s+-'
        for i, line in enumerate(lines):
            if re.match(version_pattern, line):
                return self._merge_change_lines(lines, i, new_entry)

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

    def _merge_change_lines(self, lines: List[str], header_index: int, new_entry: str) -> str:
        """Merge the new entry's changelog bullets into an existing block.

        Appends only bullets not already present (so a verbatim re-write is a
        no-op and repeated merges are idempotent), placing them after the
        last existing bullet of the block and before the next version header.

        Args:
            lines: VERSIONS.md split into lines
            header_index: Index of the existing ``## version`` header line
            new_entry: New rendered entry whose bullets are to be merged

        Returns:
            Updated content
        """
        # Extent of the existing version block (up to the next ## header).
        block_end = len(lines)
        for j in range(header_index + 1, len(lines)):
            if lines[j].startswith('## '):
                block_end = j
                break

        # Changelog bullets carried by the rendered entry: everything after
        # its own header line, blanks dropped.
        change_lines = [ln for ln in new_entry.split('\n')[1:] if ln.strip()]
        existing = {ln.strip() for ln in lines[header_index:block_end] if ln.strip()}
        to_add = [ln for ln in change_lines if ln.strip() not in existing]
        if not to_add:
            return '\n'.join(lines)

        # Append after the last non-blank line of the block.
        insert_at = block_end
        while insert_at > header_index + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        lines[insert_at:insert_at] = to_add
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
