# Phase 6 — Production Readiness

## Outcome

Integrate and harden the complete read-only/shadow Crypto Intelligence Agent. Add no speculative features while critical or high defects remain.

## Review scope

Review correctness, event-time integrity, concurrency, reconnects, backpressure, rate limits, retries, idempotency, database migrations, retention, secrets, prompt-injection isolation, source licensing/ToS, observability, recovery, and financial assumptions.

Verify the complete flow:

`official/social/news/on-chain event -> entity and contract resolution -> market confirmation -> point-in-time feature snapshot -> candidate ranking -> risk gate -> Telegram alert -> paper position -> outcome evaluation`.

## Required verification

- Offline unit and integration suites.
- Bounded live contract smoke tests only for configured public endpoints.
- Deterministic historical replay including TRUMP and scam/rug controls.
- Provider failover, stale/outlier/divergence behavior, and degraded modes.
- Load/rate-limit/backpressure tests for configured scan and deep universes.
- Restart/crash recovery without duplicate events, alerts, decisions, or positions.
- Additive migration of an existing SQLite database with backup/restore verification.
- Backtest and dataset reproducibility.
- Paper-accounting reconciliation.
- Security scan confirming no secrets in code, logs, fixtures, database exports, or Git.
- Prompt-injection tests across every external-text ingestion path.

## Operations and documentation

Update README, configuration example, architecture, source/cost matrix, runbook, incident procedures, retention, backup/restore, and safe startup/shutdown steps.

Provide status output for provider lag, event lag, source coverage, universe/deep-watch sizes, queue depth, dropped/backlogged events, model version, calibration drift, paper performance, circuit breakers, and degraded reasons.

Define stop conditions for stale or contradictory feeds, excessive false alerts, reconciliation failure, migration failure, model drift, and risk-limit breach.

## Acceptance gates

- All required tests pass within documented bounds; unresolved critical/high defects block completion.
- Working tree contains no secret or runtime artifacts.
- Default configuration remains shadow/read-only and starts without optional paid credentials.
- Real trading, transaction signing, and private keys remain absent.
- Final report lists commit, verification counts, live sources, disabled/degraded sources, costs, known risks, and safe launch order.
- Do not claim profitability without an adequate shadow/paper period and stable out-of-sample net results.
- Commit message: `phase 6 production readiness`.
