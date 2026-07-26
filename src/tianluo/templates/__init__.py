"""SE3 templates for project initialization.

Template files in this package (rendered by `luo init` / `luo migrate`):

- ``charter.md`` — the project **charter** template; a shrunk rename of the
  retired ``base_spec.md``. Written to ``tianluo/charter.md`` and injected in full
  into every `luo run` step. Carries only code-inexpressible, whole-project
  high-altitude content (project identity, top-level architecture, project-wide
  conventions, version management); per-module/per-symbol locators are dropped
  in favour of code-index.
- ``base_spec.md`` — the legacy base-spec template (retired by the
  code-index + charter refactor; retained until the spec system is removed).
- ``readme_md.md`` / ``versions_md.md`` — README and VERSIONS.md templates.
- ``version_script.py.tmpl`` — the project version-management script template.

``CHARTER_TEMPLATE`` is the canonical filename of the charter template, so
callers reference one constant rather than a scattered string literal.
"""

#: Filename of the charter template within this package.
CHARTER_TEMPLATE = "charter.md"

__all__ = ["CHARTER_TEMPLATE"]
