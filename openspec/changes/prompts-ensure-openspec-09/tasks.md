# 如prompts/ensure-openspec-integrity.md所列，现在se3的openspec系统还存在很多问题，解决这些问题，目的是只要按照se3规 范执行se3 command，就能够保证openspec系统的有序，让它一直能保持它在se3标准里应有的地位和作用，并且保持健康。 (Iteration 9/10)

## Tasks

- [x] Verified se3 health command is fully implemented with all required checks:
  - Zombie changes detection (inactive, no progress for 30+ days)
  - Old format change detection (missing .openspec.yaml)
  - Unarchived completed changes detection
  - Stale changes detection (no activity for specified days)
  - Naming convention validation
  - Directory structure drift detection
  - Spec-change association validation
- [x] Verified se3:done integrates health checks automatically
- [x] Verified se3 lint validates all specs correctly
- [x] All 15 specs pass validation
- [x] All 207 tests pass
