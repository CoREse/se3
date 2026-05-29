Synchronize the SE3 framework's documentation with the current state of
the CLI, specs, directory structure, and version number.

This is a documentation-consistency pass for the SE3 repository itself.
Do NOT change behavior, add features, or refactor source code — only
bring the docs back in line with what SE3 actually is today.

## Scope

Bring `README.md` and `README.zh.md` into agreement with:

- **CLI surface** — every `se3` command and subcommand currently
  registered in `src/se3/cli.py` and `src/se3/commands/` (`run`, `init`,
  `sync`, `merge`, `history`, `issue`, `daemon`, `guardrails`,
  `salvage`, and their flags). Remove docs for anything removed; add
  anything missing; correct any drifted behavior (e.g. `--preset`,
  `--output-format`, task-type defaults).
- **Specs** — the documented behavior under `se3/specs/`. The README's
  description of capabilities must not contradict the specs
  (flow-engine, se3-commands, se3-config, se3-scaffold, se3-versioning,
  se3-workflows, preset-prompts, etc.).
- **Directory / project structure** — the `src/se3/` package layout and
  the `se3/` runtime directory tree shown in the docs must match disk.
- **Version display** — the version badge and any version reference must
  match the version in `pyproject.toml`.

## Bilingual alignment (SE3-specific)

`README.md` (English) and `README.zh.md` (Simplified Chinese) are a
parallel pair and MUST stay structurally aligned:

- Section headings, ordering, code blocks, command examples, and the
  project structure tree must correspond one-to-one between the two
  files.
- When a section is added, removed, or reworded in one language, mirror
  the change in the other — never let one drift ahead.
- The Chinese README is a faithful translation, not a divergent
  document; keep terminology consistent with the specs (which are
  authored in English).

Follow the standard localized-README naming convention: the Chinese
README is `README.zh.md` (BCP 47 short form), not `README_zh.md` or
`README-zh.md`. Only escalate to a regional form like `README.zh-CN.md`
if a second Chinese regional variant ever needs to coexist.

## Constraints

- Touch only `README.md` and `README.zh.md` (and any docs SE3 keeps in
  sync with them). Do not edit source code, tests, or specs.
- Do not bump the version — the engine's `version_analyze` / `commit`
  steps own the version files. Only make the docs *display* the version
  that already exists in `pyproject.toml`.
- Preserve existing design-philosophy and rationale prose unless it is
  factually wrong.
