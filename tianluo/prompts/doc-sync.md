Synchronize the SE3 framework's published user-facing documentation with
the current state of its code — the CLI surface, the specs, the project
structure, and the version display.

This is a documentation-consistency pass for the SE3 repository itself.
Do NOT change behavior, add features, or refactor source code — only
bring the published docs back in line with what SE3 actually is today.

## Scope

Reconcile **every piece of user-facing documentation this project
actually publishes** against the current code. "Published
documentation" means the docs that already exist in this repository's
present state — not a fixed path you must conjure into being. Enumerate
what is actually there and reconcile each:

- **README** — the project's standard entry document (`README.md` plus
  any localized siblings; see *Localized documentation alignment*
  below). README is always in scope.
- **A `docs/` tree (or its equivalent)** — when the project publishes
  longer-form user docs (commonly under `docs/`, but it may instead be a
  `documentation/`, `guide/`, handbook, or similar directory),
  reconcile those pages too. A project that publishes no such tree
  simply yields an empty set here — this part is then a natural no-op,
  and `docs/` is NOT a path you create or impose.

Anchor on the documentation that is *already published*, not on a
required directory layout: README is one published doc; a docs tree (or
its equivalent) is another. Do not elevate `docs/` into a mandatory
standard — this pass stays structure-agnostic and works across projects
whether or not they keep a docs tree.

### README reconciliation (baseline — preserved in full)

README is the standard SE3 document and carries the established
item-by-item reconciliation contract. Bring `README.md` and
`README.zh.md` (and any other localized siblings) into agreement with:

- **CLI surface** — every `luo` command and subcommand currently
  registered in `src/tianluo/cli.py` and `src/tianluo/commands/` (`run`, `init`,
  `code-index`, `migrate`, `guardrails`, `merge`, `merge-respond`,
  `merge-unlock`, `history`, `issue`, `daemon`, `worktree`,
  `end-session`, `salvage`, and their flags). Remove docs for anything
  removed; add anything missing; correct any drifted behavior (e.g.
  `--preset`, `--output-format`, task-type defaults).
- **Knowledge system** — the README's description of capabilities must
  not contradict the code-first knowledge system's actual artifacts
  (`tianluo/code-index.md`, `tianluo/charter.md`, colocated
  why-comments) and their documented behavior.
- **Directory / project structure** — the `src/tianluo/` package layout and
  the `tianluo/` runtime directory tree shown in the docs must match disk.
- **Version display** — the version badge and any version reference must
  match the version recorded in `pyproject.toml`. Only correct a
  *displayed* version so it matches; never bump the version — the
  engine's `version_analyze` / `commit` steps own the version files.

These README requirements are the baseline and are preserved exactly.
The broader "all published documentation" scope above is an
**additional layer on top of** this baseline, not a replacement for it:
docs-tree (or equivalent) coverage *extends* the README contract; it
must not dilute or supersede it.

### Reporting documentation gaps (report only — never create)

While reconciling, you may notice that a subsystem has a clear
user-facing surface (a command, an API endpoint, a configuration knob)
with **no corresponding documentation anywhere**. Report each such gap —
as a note in your summary, or as a tracked issue for a human to triage —
and stop there. Do **NOT** create new documentation files to fill the
gap on your own. Inventing docs invites doc-sprawl and makes this pass
non-deterministic and hard to re-run cleanly; the decision of whether
and where to document a newly exposed surface is left to a human / issue
triage.

## Localized documentation alignment (SE3-specific)

`README.md` (English) and `README.zh.md` (Simplified Chinese) are a
parallel pair and MUST stay structurally aligned; the same rule applies
to every other document SE3 publishes in more than one language:

- Section headings, ordering, code blocks, command examples, and the
  project structure tree correspond one-to-one across the language
  variants.
- When a section is added, removed, or reworded in one language, mirror
  the change in every other variant — never let one drift ahead.
- A translated variant is a faithful translation, not a divergent
  document; keep terminology consistent with the specs (which are
  authored in English).

Follow the standard localized-naming convention: a language variant
carries a BCP 47 short code as a dotted suffix on the base name — the
Chinese README is `README.zh.md`, not `README_zh.md` or `README-zh.md`.
Escalate to a regional form like `README.zh-CN.md` only if a second
Chinese regional variant ever needs to coexist. Extend the same
convention to docs-tree pages: a localized `docs/<name>.md` becomes
`docs/<name>.<lang>.md`, and every language variant of a docs page stays
in sync just as the READMEs do.

## Constraints (scope discipline)

- Edit **only documentation** — the README(s), the docs tree (or its
  equivalent), and their localized variants. Do NOT edit source code,
  tests, or specs.
- Do not bump the version — the engine's `version_analyze` / `commit`
  steps own the version files. Only make the docs *display* the version
  that already exists in `pyproject.toml`.
- Never create new documentation files: surface an uncovered subsystem
  as a gap to report, not as a file to author.
- Preserve existing design-philosophy and rationale prose unless it is
  factually wrong.
