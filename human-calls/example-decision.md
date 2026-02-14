---
type: decision
priority: medium
status: responded
created: 2026-02-14
---

# Example: Choose database approach

## Context
The project needs to persist user data. Current scale is small (<1000 users) but future growth should be considered. This is an example file demonstrating the human-call format.

## Request
Choose a database approach from the options below.

## Options
- **A**: SQLite — lightweight, no deployment needed, good for small scale. Limited concurrent writes.
- **B**: PostgreSQL — powerful, scalable. Requires deployment and maintenance.
- **C**: File system (JSON/YAML) — simplest, no dependencies. Not suited for complex queries or large data.

---

## Response
Option A (SQLite). Keep it simple for now, migrate to PostgreSQL later if needed.
