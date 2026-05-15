<!-- spec-format: v1 -->
# documentation-updater Specification

## Purpose

Define the behavior of the `documentation-updater` subsystem
(`src/se3/engine/docs_updater.py`), which maintains a project's
`README.md` version badge/header and `VERSIONS.md` changelog file from
within the SE3 `commit` / `version_analyze` pipeline. The subsystem
exposes a single `DocumentationUpdater` class and a small `Template`
class used for placeholder substitution. All file I/O is anchored at a
caller-supplied `project_root`, and behavior is configurable via a
plain `config` dict so that downstream callers can override templates
without subclassing.

## Requirements

### Requirement: Template rendering with placeholder substitution

The `Template` class SHALL render a template string by substituting
`{{placeholder}}` tokens with values from a caller-supplied context
dictionary, merged on top of the template's own default placeholders.

- Each `Template` is constructed with raw `content` and an optional
  `placeholders` dict of defaults; missing `placeholders` MUST be
  treated as an empty dict (never `None`).
- `render(context)` SHALL merge defaults with `context` such that
  `context` values take precedence over `placeholders` defaults.
- Substitution MUST replace every literal occurrence of
  `{{<key>}}` with `str(value)`, so non-string values (e.g. integers)
  are stringified before insertion.
- Placeholders that appear in the template but are not present in the
  merged context MUST be left untouched in the rendered output (no
  exception, no empty-string substitution).

#### Scenario: Context overrides default placeholders
- **GIVEN** a `Template("hello {{name}}", placeholders={"name": "World"})`
- **WHEN** `render({"name": "Alice"})` is called
- **THEN** the result is `"hello Alice"`

#### Scenario: Non-string values are coerced via str()
- **GIVEN** a `Template("v={{n}}")`
- **WHEN** `render({"n": 3})` is called
- **THEN** the result is `"v=3"`

#### Scenario: Unknown placeholders are preserved
- **GIVEN** a `Template("{{a}} {{b}}")`
- **WHEN** `render({"a": "x"})` is called
- **THEN** the result is `"x {{b}}"` (the `{{b}}` token is left intact)

### Requirement: Updater initialization and default templates

`DocumentationUpdater(project_root, config=None)` SHALL bind itself to
a project root and prepare a templates registry that mixes default
templates with caller overrides.

- `config` defaults to an empty dict; the updater MUST not crash when
  none is supplied.
- The default `readme_badge` template is the markdown shields.io badge
  `![Version](https://img.shields.io/badge/version-{{version}}-blue)`
  and SHALL be installed under the key `"readme_badge"`.
- The default `versions_entry` template is a markdown section header
  `## {{version}} - {{date}}` followed by a blank line, the
  `{{changes}}` body, and a trailing blank line, installed under the
  key `"versions_entry"`.
- When `config` provides `"readme_badge_template"` or
  `"versions_entry_template"`, those values SHALL replace the defaults
  for `"readme_badge"` and `"versions_entry"` respectively.
- A `"readme_header"` template SHALL be installed ONLY when
  `config["readme_header_template"]` is present; absent that key, the
  registry MUST NOT contain a `"readme_header"` entry.

#### Scenario: Defaults are installed when config is empty
- **GIVEN** `DocumentationUpdater(project_root, config={})`
- **WHEN** the updater is constructed
- **THEN** `self.templates` contains keys `"readme_badge"` and `"versions_entry"`
- **AND** `self.templates` does NOT contain `"readme_header"`

#### Scenario: Config overrides built-in templates
- **GIVEN** `config = {"readme_badge_template": "v{{version}}", "readme_header_template": "# Project (v{{version}})"}`
- **WHEN** the updater is constructed
- **THEN** `templates["readme_badge"].content == "v{{version}}"`
- **AND** `templates["readme_header"].content == "# Project (v{{version}})"`

#### Scenario: Context defaults include version/date/year
- **WHEN** the updater builds rendering context for a given `version`
- **THEN** the context MUST contain `"version"`, `"date"` (current local
  date as `YYYY-MM-DD`), and `"year"` (current year as a string)
- **AND** any caller-supplied `additional_context` SHALL override the
  defaults for matching keys

### Requirement: README.md version badge update

`update_readme(version, template_name=None, additional_context=None)`
SHALL update an existing `README.md` to reflect a new version, using
three badge-replacement regex strategies in order; if none match, it
falls back to one of two insertion modes.

- The method MUST raise `FileNotFoundError` if `README.md` does not
  exist at `project_root / "README.md"`.
- Badge replacement SHALL try, in order (case-insensitive,
  single-match):
  1. Markdown badge `![Version](...version-X-...)` — pattern
     `!\[Version\]\([^)]*version-[^-\s)]*-[^)]*\)`
  2. Generic markdown version badge — pattern `!\[version\]\([^)]*\)`
  3. HTML `<img>` tag containing the literal word `version` —
     pattern `<img[^>]*version[^>]*>`
- The first pattern that matches the file SHALL be replaced exactly
  once with the rendered `readme_badge` template; the remaining
  patterns MUST NOT be applied in the same call.
- If NONE of the three patterns match:
  - If the first non-empty line starts with `#` (markdown heading), the
    new badge SHALL be inserted as the third line (blank line, then
    badge) so the existing title is preserved.
  - Otherwise, the new badge SHALL be prepended to the file followed
    by a blank line.
- After badge replacement (or insertion), `_replace_version_header`
  SHALL run ONLY when `"readme_header"` is present in the templates
  registry; absent that key, the header step is a no-op.
- `update_readme` MUST write to disk only when the rendered content
  differs from the original content (no-op-safe).

#### Scenario: Existing markdown badge is replaced in place
- **GIVEN** a `README.md` containing `![Version](https://img.shields.io/badge/version-0.1.0-blue)`
- **WHEN** `update_readme("0.2.0")` is invoked
- **THEN** the file is rewritten with `version-0.2.0-blue` in the badge
- **AND** the file is written exactly once

#### Scenario: README without any badge gets one inserted after the title
- **GIVEN** a `README.md` whose first line is `# My Project`
- **AND** the file contains no version badge
- **WHEN** `update_readme("1.0.0")` is invoked
- **THEN** the rendered badge appears as the third line of the file,
  preceded by a blank line and the original `# My Project` heading

#### Scenario: README without title gets badge prepended
- **GIVEN** a `README.md` whose first line is plain prose (no leading `#`)
- **WHEN** `update_readme("1.0.0")` is invoked
- **THEN** the rendered badge is prepended to the file, followed by a
  blank line and the original content

#### Scenario: No write when content is unchanged
- **GIVEN** a `README.md` whose badge already matches the rendered output
- **WHEN** `update_readme(version)` is invoked
- **THEN** the file's mtime is unchanged (no write occurs)

#### Scenario: Missing README raises FileNotFoundError
- **GIVEN** no `README.md` exists at the project root
- **WHEN** `update_readme(version)` is invoked
- **THEN** `FileNotFoundError` is raised

### Requirement: README version header replacement

When the `"readme_header"` template is registered, `_replace_version_header`
SHALL search the README content for an existing version-style header and
replace the first match with the rendered header template; if no
matching header is found, the content is returned unchanged.

- The method MUST be a no-op (return `content` unchanged) when no
  `"readme_header"` template is present in the registry.
- When the template IS present, the rendered header SHALL be computed
  once from the caller-supplied context.
- The method SHALL try the following header patterns, in order, using
  multiline matching:
  1. `^#+ .*[Vv]ersion.*$` — any markdown ATX heading (one or more
     leading `#`) whose text contains the word `version` (case-
     insensitive on the `V`).
  2. `^\*\*[Vv]ersion:\*\*.*$` — a bolded `**Version:**` line.
- The first pattern that matches SHALL be replaced exactly once
  (`count=1`) with the rendered header; remaining patterns MUST NOT be
  applied in the same call.
- If NEITHER pattern matches, the method MUST return the original
  `content` unchanged — it MUST NOT insert, prepend, or append a
  header. (Header insertion is reserved for the badge step; the header
  step only updates an existing header.)

#### Scenario: Existing markdown version header is replaced
- **GIVEN** a `"readme_header"` template rendering to `"# Project v2.0.0"`
- **AND** README content containing a line `## Version 1.0.0`
- **WHEN** `_replace_version_header` runs
- **THEN** the `## Version 1.0.0` line is replaced with `# Project v2.0.0`
- **AND** the rest of the file is preserved

#### Scenario: Bold Version line is replaced
- **GIVEN** a `"readme_header"` template rendering to `"**Version:** 2.0.0"`
- **AND** README content containing a line `**Version:** 1.0.0`
- **WHEN** `_replace_version_header` runs
- **THEN** the `**Version:** 1.0.0` line is replaced with the rendered header

#### Scenario: No existing header leaves content untouched
- **GIVEN** a `"readme_header"` template is registered
- **AND** README content with no markdown version heading and no
  `**Version:**` line
- **WHEN** `_replace_version_header` runs
- **THEN** the returned content is byte-identical to the input
- **AND** no header is inserted at the top, bottom, or anywhere else

#### Scenario: No header template makes the step a no-op
- **GIVEN** no `"readme_header"` template is present in the registry
- **WHEN** `_replace_version_header` runs
- **THEN** the returned content is byte-identical to the input
  regardless of any matching headers it may contain

### Requirement: VERSIONS.md changelog entry insertion

`update_versions_md(version, changes, template_name=None, additional_context=None)`
SHALL render a new changelog entry and either insert it into an
existing `VERSIONS.md` (preserving prior history) or create a fresh
file with a top-level header.

- The rendering context SHALL include the standard `version` / `date`
  / `year` keys plus a derived `changes` value formatted as markdown
  bullets:
  - When `changes` is empty, the formatted body SHALL be the literal
    sentinel `"- No changes recorded"`.
  - Each change entry SHALL be normalized to start with `"- "`; an
    entry that already begins with `-` SHALL NOT be double-prefixed.
- Template lookup precedence:
  1. If `template_name` is provided AND the name exists in the
     registry, that template is used.
  2. Otherwise the `"versions_entry"` template from the registry is
     used.
  3. As a final fallback, a `Template` built from
     `DEFAULT_VERSIONS_ENTRY_TEMPLATE` is used.
- When `VERSIONS.md` exists:
  - If the new `version` already appears as a `## <version> - ...`
    header anywhere in the file, the content SHALL be left unchanged
    (no duplicate insertion).
  - Otherwise the new entry is inserted after the file's title
    (`# ...`) or the literal `## Changelog` header, skipping any blank
    lines that follow that header; if there is no such header, the
    entry is inserted before the first non-header non-blank line.
- When `VERSIONS.md` does NOT exist, it SHALL be created with
  `# Version History` as the title followed by the rendered entry.
- The file SHALL be written unconditionally at the end of the call
  (even when the in-memory content is identical, in contrast to
  `update_readme`).

#### Scenario: Empty changes list yields the sentinel
- **WHEN** `update_versions_md(version, changes=[])` is called
- **THEN** the rendered entry's `{{changes}}` body is `"- No changes recorded"`

#### Scenario: Changes are bulletized exactly once
- **GIVEN** `changes = ["fix bug", "- already bulleted"]`
- **WHEN** `update_versions_md(version, changes)` is called
- **THEN** the rendered body contains `"- fix bug"` and `"- already bulleted"` (no `"- - already bulleted"`)

#### Scenario: Duplicate version is not re-inserted
- **GIVEN** an existing `VERSIONS.md` already containing `## 1.2.3 - 2026-01-01`
- **WHEN** `update_versions_md("1.2.3", changes=["x"])` is called
- **THEN** the file is rewritten but its content is byte-identical to
  before the call (no new entry, no duplicate header)

#### Scenario: New entry inserted after top-level header
- **GIVEN** a `VERSIONS.md` whose first line is `# Version History`
  followed by an existing `## 1.0.0 - 2026-01-01` entry
- **WHEN** `update_versions_md("1.1.0", changes=["x"])` is called
- **THEN** the new `## 1.1.0 - ...` entry appears between the title
  and the prior `## 1.0.0` entry

#### Scenario: Missing VERSIONS.md is created with a title
- **GIVEN** no `VERSIONS.md` exists at the project root
- **WHEN** `update_versions_md("1.0.0", changes=["x"])` is called
- **THEN** the file is created
- **AND** it starts with `# Version History`
- **AND** the rendered entry follows the title

### Requirement: Custom template registration and lookup

`add_template(name, content, placeholders=None)` and
`render_template(name, context)` SHALL allow callers to register and
render arbitrary named templates against the same context model used by
the built-in templates.

- `add_template` SHALL install (or replace) a `Template` under the
  given `name`; `placeholders` defaults to `None`, which the `Template`
  constructor treats as an empty dict.
- `render_template(name, context)` SHALL raise `KeyError` (with a
  message that names the missing template) when `name` is not present
  in the registry.
- A successful `render_template` call SHALL delegate to the named
  template's `render(context)` method without otherwise consulting the
  updater's default context builder, so callers are responsible for
  constructing the context they need.

#### Scenario: Custom template can be rendered
- **GIVEN** `updater.add_template("greeting", "hello {{name}}")`
- **WHEN** `updater.render_template("greeting", {"name": "World"})` is called
- **THEN** the result is `"hello World"`

#### Scenario: Missing template name raises KeyError
- **WHEN** `updater.render_template("missing", {})` is called and no
  `"missing"` template has been registered
- **THEN** a `KeyError` is raised whose message contains `"missing"`

### Requirement: Combined update with independent error handling

`update_both(version, changes, additional_context=None)` SHALL invoke
`update_readme` and `update_versions_md` in sequence, returning a
status dict that reports each side independently.

- The method SHALL return a dict with exactly two keys, `"readme"` and
  `"versions"`, each mapped to a boolean indicating whether that side
  completed without raising.
- A `FileNotFoundError` raised from `update_readme` SHALL be swallowed
  and leave `results["readme"] == False`; it MUST NOT prevent
  `update_versions_md` from running.
- A `FileNotFoundError` raised from `update_versions_md` SHALL be
  swallowed and leave `results["versions"] == False`. (Note:
  `update_versions_md` itself creates the file when missing, so this
  path is reserved for I/O errors that surface as `FileNotFoundError`
  — e.g. a missing intermediate directory.)
- `additional_context`, when provided, SHALL be forwarded unchanged to
  both underlying calls so that the README badge and the VERSIONS
  entry see the same caller-extended context.

#### Scenario: README missing does not block VERSIONS update
- **GIVEN** a project with no `README.md` but with a writable project root
- **WHEN** `update_both("1.0.0", ["x"])` is called
- **THEN** the return value is `{"readme": False, "versions": True}`
- **AND** `VERSIONS.md` is created at the project root

#### Scenario: Both sides succeed
- **GIVEN** an existing `README.md` and `VERSIONS.md`
- **WHEN** `update_both("1.0.0", ["x"])` is called
- **THEN** the return value is `{"readme": True, "versions": True}`

#### Scenario: additional_context propagates to both sides
- **GIVEN** `additional_context = {"author": "Alice"}`
- **AND** custom templates that reference `{{author}}`
- **WHEN** `update_both(version, changes, additional_context)` is called
- **THEN** both the README badge/header render and the VERSIONS entry
  render observe `author == "Alice"` in their template context
