# Tasks: SE3 Directory Structure Cleanup

## Overview
统一 SE3 元数据目录结构，解决根目录污染和归档混乱问题。

## Tasks

- [x] **Task 1**: Update se3-scaffold spec to define new `.se3/` directory structure
  - Define `.se3/` as the single root for all SE3 metadata
  - Specify subdirectories: `calls/`, `collab/`, `tmp/`, `state/`
  - Define `calls/active/` and `calls/archive/` separation
  - Mark as **BREAKING** change for existing projects

- [x] **Task 2**: Update se3-commands spec with new paths
  - Update `se3:start` to create `.se3/` structure instead of scattered directories
  - Update `se3:work` to look for human-calls in `.se3/calls/active/`
  - Update `se3 handoff` to use new paths
  - Ensure backward compatibility detection

- [x] **Task 3**: Implement directory migration logic
  - Create `se3 migrate` command to move existing directories:
    - `human-calls/` → `.se3/calls/`
    - `.collab/` → `.se3/collab/`
    - `tmp*.prompt` → `.se3/tmp/`
  - Handle edge cases (missing dirs, permissions)
  - Generate migration report

- [x] **Task 4**: Implement tmp file cleanup mechanism
  - Add tmp file creation to `.se3/tmp/` instead of root
  - Add automatic cleanup on `se3:done` (files older than 7 days)
  - Add `tmp*.prompt` to `.gitignore` template as fallback

- [x] **Task 5**: Run tests and verify migration
  - Run `se3 lint` to validate spec changes
  - Run `python -m pytest tests/ -q` to ensure no regressions
  - Test migration on this project
  - Archive change
