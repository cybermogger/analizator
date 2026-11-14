# Runtime Architecture

The service is one Python process with isolated async workers. It is read-only at every exchange/chain boundary; Phase 5 is shadow accounting only. No model is promoted as a result of Phase 6.

```text
official HTTP/RSS/X/GitHub and chain RPC adapters
  ├─ normalized public market events ─> SQLite lineage ─> multi-venue truth/universe
  ├─ immutable raw event ─> deterministic source policy/entity resolver ─> event replay
  └─ verified chain event ─> capability-gated on-chain assessment (separate pipeline)

Hyperliquid WS/REST ─> causal candles + SOL/HYPE analysis ─> alert gate
                                            └─> current-production shadow orchestrator
                                                  ├─ exact-identity event/chain evidence
                                                  ├─ provider/freshness/liquidity/risk gates
                                                  └─ immutable decision + paper ledger/outcome
```

The Phase 4 dataset/backtest/ranker is an offline research command. Its challenger remains `NOT_EVALUABLE`; the live shadow path identifies itself as `current-production-ensemble`. CEX market truth, news and on-chain evidence are not silently substituted into the live quantitative model. A source can contribute identity/evidence only under its deterministic policy; external text never controls tools, source trust, timestamps, contract matching or order execution.

`AnalyzerService` owns service lifecycle. `MultiVenueMarketService` limits concurrent calls and request start rate independently per provider, scans at most the configured scan budget, and requests L2/trades only for selected deep-watch instruments. Event-source workers await a serialized ingestion lock, which applies backpressure without dropping events. SQLite is authoritative for decisions, event identities, provider health and paper accounting. WebSocket raw trades/books rotate to Parquet; normalized raw ticks and their payload-free lineage are pruned at the configured raw retention horizon.

Telegram notification delivery uses an append-only SQLite claim/result outbox: a durable claim precedes network send, and a crash or missing acknowledgment remains `UNKNOWN` rather than triggering a blind resend. The `status.py` view exposes unknown and failed delivery counts. This is at-most-once delivery; an operator must reconcile unknown entries against the Telegram chat.

The heartbeat is an atomic JSON snapshot for liveness and runtime metrics. `status.py` combines it with current persisted provider/source/universe/ledger state, verifies the schema version and paper-ledger identity in healthcheck mode, and displays source coverage, queue/backpressure, calibration, model versions, paper reconciliation, circuit breakers and degraded reasons. A fresh heartbeat means the process is alive; it does not mean that every optional source is enabled or healthy.
