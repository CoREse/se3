# Tasks: SE3 Directory Visible (Remove Dot Prefix)

## Overview
Change `.se3/` to `se3/` to make human calls visible to humans, aligning with "human-as-MCP" philosophy.

## Tasks

- [x] **Task 1**: Update se3-scaffold spec to use `se3/` instead of `.se3/`
  - Update project structure diagram
  - Update all path references
  - Add rationale for visible directory
  - Mark as **BREAKING** change

- [x] **Task 2**: Update se3-commands spec to use `se3/` paths
  - Update `se3 migrate` command spec
  - Update `se3 init` command spec
  - Add scenario for migrating from hidden `.se3/`

- [x] **Task 3**: Update CLI implementation
  - Update `se3 init` to create `se3/` instead of `.se3/`
  - Update `se3 migrate` to move `.se3/` → `se3/`
  - Update `se3 start` to check `se3/` paths (with fallback to `.se3/`)
  - Update `se3 done` to cleanup `se3/tmp/`

- [x] **Task 4**: Run migration and verify
  - Migrated this project from `.se3/` to `se3/`
  - All 207 tests pass
  - se3 lint passes 15 specs
  - Archive change
