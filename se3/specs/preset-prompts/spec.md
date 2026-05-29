<!-- spec-format: v1 -->
# preset-prompts Specification

## Purpose

Define the SE3 *preset prompt* mechanism — a small library of
common-task prompts (e.g. `doc-sync`) that recurring, standardized tasks
can reuse without retyping the full task description. Presets are
resolved by `src/se3/preset_loader.py` into a single registry merged
from two layers (built-in package data and project-local files) and are
launched via `se3 run --preset <name>` (see the `se3-commands` spec).
This spec governs the registry's layer sources, merge/override order,
resolution result, and error handling.

## Requirements

### Requirement: Preset Prompt Two-Layer Registry

The system SHALL resolve preset prompts from a registry merged from two
layers — a built-in layer and a project layer — exposing each preset as
a `(name, type, prompt_file, layer)` entry.

**Built-in layer (package data, read at runtime):**

- Built-in preset prompt bodies are markdown files shipped as package
  data under `src/se3/templates/prompts/*.md`. The preset name is the
  file stem (e.g. `doc-sync.md` → `doc-sync`).
- These files are read at runtime from the installed package path
  (resolved relative to the loader module, NOT the current working
  directory), so any project with SE3 installed has the latest built-in
  presets with zero configuration.
- Built-in presets SHALL NOT be copied into a project by `se3 init`.
  Runtime reading is deliberate: copying would re-introduce the
  copy-drift problem that previously affected the base-spec template.
- Built-in preset metadata (currently the task `type`) is declared
  inside the loader module. A built-in markdown file with no declared
  metadata falls back to the default task type `feature`.

**Project layer (committed with the project):**

- Project-local preset bodies are markdown files under
  `<project_root>/se3/prompts/*.md` (the zero-config path — file stem is
  the preset name, default type `feature`). The directory is
  `se3/prompts/`, NOT `se3/templates/`, so it is not confused with the
  package's `src/se3/templates/` directory.
- Project metadata MAY be supplied or overridden via the `presets:`
  section of `se3.yaml`, keyed by preset name, with optional `type` and
  `prompt_file` fields (see the `se3-config` spec). A `prompt_file`
  redirects the preset to a specific path relative to the project root;
  when omitted, the conventional `se3/prompts/<name>.md` path is used.

**Merge and override order:**

- The registry is built by loading the built-in layer first, then
  overlaying the project layer. When the same preset name exists in both
  layers, the **project layer entry overrides the built-in layer entry**
  (project wins).
- `list_presets(project_root)` SHALL return all available presets from
  both layers, sorted by name, each tagged with its resolved source
  layer (`builtin` or `project`).

**Resolution result:**

- `resolve(name, project_root)` SHALL return the resolved
  `(task_type, prompt_text, layer)` for an existing preset, reading the
  prompt body from the resolved `prompt_file`.

#### Scenario: Built-in preset is available with zero project config
- **GIVEN** a project with SE3 installed and no `se3/prompts/` directory
  and no `presets:` section in `se3.yaml`
- **WHEN** the registry is built for that project
- **THEN** the built-in `doc-sync` preset is present in the registry
  tagged with the `builtin` layer
- **AND** no file was copied into the project to make it available

#### Scenario: Project preset overrides a built-in of the same name
- **GIVEN** a built-in preset `doc-sync` and a project file
  `se3/prompts/doc-sync.md`
- **WHEN** the registry is built and `doc-sync` is resolved
- **THEN** the resolved entry's prompt text comes from the project file
- **AND** the resolved entry's layer is `project`

#### Scenario: Project preset metadata overlays via se3.yaml
- **GIVEN** `se3.yaml` declares
  `presets: { doc-sync: { type: feature, prompt_file: se3/prompts/doc-sync.md } }`
- **WHEN** `doc-sync` is resolved
- **THEN** the preset's task type is `feature`
- **AND** the prompt body is read from `se3/prompts/doc-sync.md`

#### Scenario: list_presets enumerates both layers with source tags
- **GIVEN** a built-in `doc-sync` preset and a project preset
  `se3/prompts/release-notes.md`
- **WHEN** `list_presets(project_root)` is called
- **THEN** the returned list includes both presets, sorted by name
- **AND** each entry carries its source layer (`builtin` or `project`)
  and resolved task type

### Requirement: Preset Resolution Error Handling

Preset resolution errors SHALL be explicit and never silently swallowed.

- Requesting a preset name that is not present in the merged registry
  SHALL raise a not-found error whose message includes the list of
  available preset names (or a `(none)` marker when the registry is
  empty).
- A preset whose resolved `prompt_file` does not exist on disk (declared
  in `se3.yaml` or expected at the conventional location but missing)
  SHALL raise an error identifying the preset, its layer, and the
  missing path — it MUST NOT be silently ignored.
- `list_presets` MAY enumerate a declared-but-missing project preset
  without raising (existence is verified lazily at `resolve` time), so
  listing remains robust even when one declared `prompt_file` is absent.

#### Scenario: Unknown preset name lists available presets
- **GIVEN** a registry that does not contain a preset named `nope`
- **WHEN** `resolve("nope", project_root)` is called
- **THEN** a not-found error is raised whose message includes the list
  of available preset names

#### Scenario: Declared prompt_file missing raises rather than swallowing
- **GIVEN** `se3.yaml` declares `presets: { foo: { prompt_file: se3/prompts/foo.md } }`
- **AND** `se3/prompts/foo.md` does not exist on disk
- **WHEN** `resolve("foo", project_root)` is called
- **THEN** an error is raised identifying the preset, its layer, and the
  missing prompt file path
- **AND** the error is not silently swallowed
