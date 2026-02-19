# 如prompts/ensure-openspec-integrity.md所列，现在se3的openspec系统还存在很多问题，解决这些问题，目的是只要按照se3规 范执行se3 command，就能够保证openspec系统的有序，让它一直能保持它在se3标准里应有的地位和作用，并且保持健康。 (Iteration 8/10)

## Tasks

- [x] 如prompts/ensure-openspec-integrity.md所列，现在se3的openspec系统还存在很多问题，解决这些问题，目的是只要按照se3规 范执行se3 command，就能够保证openspec系统的有序，让它一直能保持它在se3标准里应有的地位和作用，并且保持健康。
  - Enhanced `se3 done` to automatically run health checks and display OpenSpec integrity status
  - Enhanced `se3 work` to include health check results when listing active changes
  - Health status now visible at start (work) and end (done) of every session
