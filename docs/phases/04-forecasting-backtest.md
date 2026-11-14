# Phase 4 — Forecasting and Honest Backtesting

## Outcome

Add a two-stage opportunity model while retaining the current production model until a challenger proves superior out of sample after realistic costs.

## Model design

1. A high-recall candidate generator consumes causal market, event, and on-chain anomalies.
2. A cross-sectional ranker selects only the best liquid opportunities under risk constraints.

Segment models and thresholds by majors, liquid alts, derivatives movers, and new/memecoin assets. Do not scale the existing weak three-class model to the expanded universe.

Targets must include:

- forward return net of fees and estimated slippage at 5m, 15m, 1h, 4h, and 24h;
- probability that take-profit occurs before stop/invalidation;
- maximum adverse and favorable excursion;
- realized volatility and liquidity/exit feasibility.

## Point-in-time research protocol

- Use only information received by the decision timestamp.
- Join on event time with publication/receipt timestamps and appropriate embargo.
- Prevent look-ahead, survivorship, delisted-token, revised-event, and future-universe leakage.
- Closed-candle models use only closed candles. Intrabar event candidates remain a separate path.
- Use purged walk-forward `TRAIN/VALIDATION/CALIBRATION/TEST` with an untouched final test.
- Snapshot datasets, feature lineage, universe membership, code/config version, and experiment metadata reproducibly.

## Execution-aware backtest

Model fees, spread, slippage, latency, funding, price impact, partial/rejected fills, and liquidity caps. Compare with no-trade, simple momentum, mean reversion, BTC beta, and the current production model.

Primary metrics: net expectancy, precision@k, profit factor, turnover-adjusted return, maximum drawdown, calibrated Brier/ECE, false-alert rate, coverage, and performance by regime and asset class. Use Sharpe/Sortino only with adequate samples and uncertainty estimates.

Run ablations for news, social, on-chain, order book, trade flow, derivatives, and market truth. A component without stable out-of-sample contribution receives zero production weight.

## Promotion policy

A challenger is not promoted unless it beats applicable baselines after costs across multiple time blocks with adequate sample size and acceptable calibration/drawdown. Insufficient data or unstable results must produce `NO_TRADE`, `NOT_EVALUABLE`, or retention of the current champion. Never hide negative results.

## Acceptance gates

- Tests detect future-event leakage, future-universe leakage, revised-news leakage, open-candle skew, overlapping split leakage, and fee/slippage omissions.
- Backtest accounting reconciles entry/exit, partial fills, funding, fees, and slippage.
- Baseline and challenger metrics include sample counts and uncertainty intervals.
- Dataset snapshot and experiment registry reproduce the reported result.
- Champion/challenger table and model card state limitations and rejected variants.
- Full unit/integration tests, compile, migration integrity, secret scan, and diff check pass.
- Do not promote any model merely to complete the phase.
- Commit message: `phase 4 forecasting and honest backtest`.
