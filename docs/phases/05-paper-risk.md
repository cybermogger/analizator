# Phase 5 — Decision, Risk, and Paper Trading

## Outcome

Turn ranked opportunities into explainable manual recommendations and a fully auditable paper portfolio. Real orders remain impossible.

## Recommendation contract

Every recommendation contains:

- canonical asset plus exact contract and venue where applicable;
- decision timestamp, age, TTL, direction or `NO_TRADE`;
- calibrated probability and expected net move after costs;
- entry zone, invalidation, scenarios, targets, and horizon;
- spread, liquidity, estimated slippage, and venue divergence;
- supporting market/event/on-chain evidence with source links;
- contradicting evidence and an alternative scenario;
- source credibility, data freshness, model/version lineage;
- a configurable maximum paper-risk amount, not personalized financial advice.

## Paper portfolio and gates

Add volatility/liquidity-capped sizing, concurrent exposure limits, correlation clusters, cooldowns, and circuit breakers for stale feeds, provider disagreement, extreme volatility, model drift, liquidity loss, and unconfirmed news.

Defaults:

- majors/liquid alts: at most 0.25% paper-equity risk per idea;
- new/memecoin assets: at most 0.05%;
- daily paper loss limit: 1.5%;
- all parameters configurable;
- shadow mode enabled by default.

Store every decision including `NO_TRADE`, all state transitions, and subsequent outcomes. Reports separate gross/net, realized/unrealized, fees, funding, slippage, rejected opportunities, and missed opportunities.

Telegram commands: `/watchlist`, `/opportunities`, `/events`, `/asset`, `/paper`, `/performance`, `/sources`, `/health`. Alert only for a new event or material change in score, price, liquidity, evidence, or risk; deduplicate by stable decision identity.

## Acceptance gates

- Prove structurally and by tests that no real-order API, signer, wallet seed, or private key can be invoked.
- Test false news, competing-token dump, primary venue outage, price divergence, disappearing liquidity, model drift, duplicate alert, extreme gap, daily loss limit, restart recovery, and correlated exposure.
- Paper accounting reconciles positions, cash, gross/net PnL, fees, slippage, and funding.
- `NO_TRADE` decisions and retractions are persisted and evaluated.
- Restart is idempotent and does not duplicate alerts or positions.
- Existing analytical paths remain read-only.
- Full unit/integration tests, compile, migration integrity, secret scan, and diff check pass.
- Document a multi-week shadow period; do not claim profitability from a short run.
- Commit message: `phase 5 paper trading and risk`.
