## Why

SE 3.0 has three gaps compared to Anthropic's original long-running agent approach:
1. No E2E testing protocol — agents mark features complete without verification
2. No spec anti-tampering guardrails — agents can freely weaken or delete spec requirements
3. No init.sh environment automation — each session manually sets up dev environment

## What Changes

- Add a verification protocol: agents MUST run tests before marking a change complete
- Add spec guardrails: explicit rules about what agents can/cannot do with specs
- Add init.sh to the project structure and startup protocol

## Capabilities

### Modified Capabilities
- `session-protocol`: Add init.sh to startup, add verification step to shutdown
- `se3-scaffold`: Add init.sh and testing to project structure and CLAUDE.md

## Impact

- output/CLAUDE.md updated with verification protocol, spec guardrails, init.sh
- output/CLAUDE.global.md updated with spec guardrails
- docs/best-practices.md updated
