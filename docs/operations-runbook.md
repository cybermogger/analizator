# Operations Runbook

## Safe startup and shutdown

1. Keep `paper.shadow_mode: true`; do not add credentials for any exchange account.
2. Before an upgrade, stop the service and take a verified SQLite snapshot:

   ```bash
   docker compose stop analyzer
   docker compose run --rm analyzer python -m src.data.backup backup /data/analyzer.db /data/backups/analyzer-UTC_TIMESTAMP.sqlite
   ```

3. Run the bounded public smoke check, then one offline-delivery cycle:

   ```bash
   docker compose run --rm analyzer python bot.py --smoke-test
   docker compose run --rm analyzer python bot.py --once
   ```

4. Start shadow/read-only service and inspect coverage, lag, degraded reasons, accounting and logs:

   ```bash
   docker compose up -d --build
   docker compose exec analyzer python status.py
   docker compose logs --tail=100 analyzer
   ```

5. Graceful shutdown: `docker compose stop analyzer`. Do not kill the SQLite owner during migration or backup.

## Local website

Run the daemon and open http://127.0.0.1:8081/. Telegram is notifications-only. Keep the listener private; see README section 3 for forecast semantics and limits.

## Stop conditions and response

| Condition | Operator response |
|---|---|
| Hyperliquid book/candle freshness is stale, or primary WS is disconnected | Pause directional alerts; inspect WS and REST fallback. Keep source degraded; do not widen freshness thresholds to mask clock skew. |
| Canonical market truth lacks two fresh venues, is `DEGRADED/STALE`, or divergence exceeds `market_truth.outlier_threshold_bps` | Do not use a canonical price or open a new paper position. Restore quorum and verify source clocks before resuming. |
| Source status is stale/degraded, event identity is ambiguous, or an event is contradicted/retracted | Treat it as context/rumor; do not promote it manually. Exact contract and independent evidence are required for token identity. |
| BLS calendar remains disabled or returns 403 | Continue in degraded macro-calendar coverage, display the disabled reason, and do not bypass access controls. Re-enable only after one bounded request to the official public URL returns HTTP 200; after re-enable, retries back off from 6h to 12h and then cap at 24h. |
| Paper `RECONCILIATION_FAILURE`, SQLite integrity/FK failure, or migration failure | Stop the analyzer and paper alerts. Preserve the DB; restore a verified snapshot only after taking a second backup and stopping all DB users. |
| Paper daily loss/correlation/concurrent-position/liquidity gate trips | Do not override the gate. Review ledger and evidence; resume only after the configured gate naturally clears and accounting reconciles. |
| At least 100 matured forecasts and calibration drift breaches configured Brier/ECE/balanced-accuracy limits | Keep the current champion unchanged, stop treating its directional alerts as actionable, and retain negative OOS results. |
| More than 12 of the latest 20 matured directional alerts are wrong, or source-attributed false alerts cannot be audited | Set `alerts.enabled: false`, preserve collection and shadow accounting, investigate before re-enabling. This is an operator stop condition, not a profitability claim. |
| `status.py` reports `unknown_after_crash` Telegram deliveries | Compare the stable delivery key/message against the authorized Telegram chat. Do not resend blindly; preserve the claim/result rows for audit. |
| Backup restore fails `integrity_check`, `foreign_key_check`, or preserved-row verification | Do not start the analyzer. Retain both source and backup files and resolve offline. |

## Backup and restore

`src.data.backup` uses SQLite's online backup API, so committed WAL contents are included. It writes a temporary destination, checks integrity and foreign keys, then atomically installs the snapshot. It refuses to overwrite an existing backup file.

```bash
# Service stopped; use a unique destination name.
docker compose run --rm analyzer python -m src.data.backup backup /data/analyzer.db /data/backups/analyzer-2026-10-29T120000Z.sqlite

# Before restore, create a second snapshot of the current database.
docker compose run --rm analyzer python -m src.data.backup backup /data/analyzer.db /data/backups/pre-restore-2026-10-29T120000Z.sqlite
docker compose run --rm analyzer python -m src.data.backup restore /data/backups/analyzer-2026-10-29T120000Z.sqlite /data/analyzer.db
docker compose run --rm analyzer python status.py
```

Restore requires the service stopped. Migrations are additive; test backup, migration and restore on a copy before upgrading production. Schema v12 adds auditable CEX market discovery decisions while preserving existing rows; previous migrations remain applied.

## Retention and incident evidence

- Raw WebSocket trades/books in Parquet and SQLite normalized ticks/payload-free lineage: `storage.raw_retention_days` (default 7 days).
- Candles, decisions, signals/outcomes, event raw payload/revisions, on-chain evidence, paper transitions and ledger: retained until manual cleanup.
- Model artifacts/metrics: retained under `/models`; rotate only after preserving champion/challenger lineage.
- Logs: bounded rotating files; export only redacted diagnostics. Never attach `.env`, tokens, DB files, WAL/SHM, or raw payload archives to an incident ticket.

Capture `status.py`, bounded logs, source IDs, UTC timestamps, relevant decision IDs and a verified DB backup. Do not copy a live `.db` file without SQLite backup; WAL mode can leave committed rows in sidecars.
