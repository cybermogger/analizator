# Compact phase prompts

Send one prompt at a time in the same Codex task. After each completion, independently review the commit before sending the next prompt. The detailed scope and acceptance gates live in `docs/phases/`, so they do not need to be pasted into chat.

## Phase 2

```text
Work from a clean HEAD. Read AGENTS.md and docs/phases/02-event-intelligence.md, then implement only phase 2. Run every acceptance gate, fix failures, and create the specified checkpoint commit only when green. Keep the final report to 15 lines or fewer; show full logs only for an unresolved failure. Do not start phase 3.
```

## Phase 3

```text
Work from a clean HEAD. Read AGENTS.md and docs/phases/03-onchain-memecoins.md, then implement only phase 3. Run every acceptance gate, fix failures, and create the specified checkpoint commit only when green. Keep the final report to 15 lines or fewer; show full logs only for an unresolved failure. Do not start phase 4.
```

## Phase 4

```text
Work from a clean HEAD. Read AGENTS.md and docs/phases/04-forecasting-backtest.md, then implement only phase 4. Run every acceptance gate, preserve negative results, and create the specified checkpoint commit only when green. Keep the final report to 15 lines or fewer; show full logs only for an unresolved failure. Do not start phase 5.
```

## Phase 5

```text
Work from a clean HEAD. Read AGENTS.md and docs/phases/05-paper-risk.md, then implement only phase 5. Run every acceptance gate, prove that real execution is impossible, and create the specified checkpoint commit only when green. Keep the final report to 15 lines or fewer; show full logs only for an unresolved failure. Do not start phase 6.
```

## Phase 6

```text
Work from a clean HEAD. Read AGENTS.md and docs/phases/06-production-readiness.md, then implement only phase 6. Run every acceptance gate, fix critical and high defects, and create the specified checkpoint commit only when green. Keep the final report to 20 lines or fewer; show full logs only for an unresolved failure. Do not enable real trading.
```

## Recovery after interruption

```text
Continue from the current repository state. Read AGENTS.md and the phase file referenced in my previous message. Inspect git status and completed tests, then resume only the unfinished acceptance items. Do not redo verified work or start a later phase.
```
