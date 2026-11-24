from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 13

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS candles (
  coin TEXT NOT NULL, interval TEXT NOT NULL, open_time INTEGER NOT NULL,
  close_time INTEGER NOT NULL, open REAL NOT NULL, high REAL NOT NULL,
  low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL, trades INTEGER NOT NULL,
  received_at_ms INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (coin, interval, open_time)
);
CREATE INDEX IF NOT EXISTS idx_candles_lookup ON candles(coin, interval, open_time DESC);

CREATE TABLE IF NOT EXISTS asset_contexts (
  coin TEXT NOT NULL, timestamp INTEGER NOT NULL, mark REAL NOT NULL, oracle REAL NOT NULL,
  mid REAL, funding REAL NOT NULL, open_interest REAL NOT NULL,
  day_notional_volume REAL NOT NULL, premium REAL, previous_day_price REAL NOT NULL,
  exchange_timestamp_ms INTEGER, received_at_ms INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (coin, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_ctx_lookup ON asset_contexts(coin, timestamp DESC);

CREATE TABLE IF NOT EXISTS trade_aggregates (
  coin TEXT NOT NULL, timestamp INTEGER NOT NULL, buy_volume REAL NOT NULL,
  sell_volume REAL NOT NULL, delta REAL NOT NULL, large_buy_volume REAL NOT NULL,
  large_sell_volume REAL NOT NULL, trade_count INTEGER NOT NULL,
  poc REAL, hvn_json TEXT, lvn_json TEXT, PRIMARY KEY (coin, timestamp)
);

CREATE TABLE IF NOT EXISTS book_aggregates (
  coin TEXT NOT NULL, timestamp INTEGER NOT NULL, best_bid REAL, best_ask REAL,
  spread REAL, relative_spread REAL, bid_depth REAL, ask_depth REAL,
  imbalance REAL, weighted_imbalance REAL, microprice REAL, book_pressure REAL,
  depth_slope REAL, walls_json TEXT, PRIMARY KEY (coin, timestamp)
);

CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, coin TEXT NOT NULL, timestamp INTEGER NOT NULL,
  primary_horizon TEXT NOT NULL, direction TEXT NOT NULL, probability REAL NOT NULL,
  payload_json TEXT NOT NULL, model_status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signals_coin_time ON signals(coin, timestamp DESC);

CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, coin TEXT NOT NULL, timestamp INTEGER NOT NULL,
  direction TEXT NOT NULL, probability REAL NOT NULL, message TEXT NOT NULL,
  telegram_message_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_coin_time ON alerts(coin, timestamp DESC);

CREATE TABLE IF NOT EXISTS model_metrics (
  coin TEXT NOT NULL, horizon TEXT NOT NULL, trained_at INTEGER NOT NULL,
  ready INTEGER NOT NULL, balanced_accuracy REAL, brier REAL, log_loss REAL,
  ece REAL, metrics_json TEXT NOT NULL, PRIMARY KEY (coin, horizon, trained_at)
);

CREATE TABLE IF NOT EXISTS critical_errors (
  fingerprint TEXT PRIMARY KEY, scope TEXT NOT NULL,
  first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL,
  last_notified INTEGER, occurrences INTEGER NOT NULL,
  message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_critical_errors_notified ON critical_errors(last_notified DESC);

CREATE TABLE IF NOT EXISTS signal_alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, coin TEXT NOT NULL, timestamp INTEGER NOT NULL,
  event TEXT NOT NULL, direction TEXT NOT NULL, probability REAL NOT NULL,
  signal_strength REAL NOT NULL, payload_json TEXT NOT NULL, message TEXT NOT NULL,
  telegram_message_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_signal_alerts_coin_time ON signal_alerts(coin, timestamp DESC);

CREATE TABLE IF NOT EXISTS signal_outcomes (
  signal_id INTEGER PRIMARY KEY, coin TEXT NOT NULL, horizon TEXT NOT NULL,
  due_timestamp INTEGER NOT NULL, evaluated_at INTEGER NOT NULL,
  outcome_price REAL NOT NULL, forward_return REAL NOT NULL,
  actual_label TEXT NOT NULL, success INTEGER NOT NULL,
  FOREIGN KEY(signal_id) REFERENCES signals(id)
);
CREATE INDEX IF NOT EXISTS idx_signal_outcomes_coin_due ON signal_outcomes(coin, due_timestamp DESC);

CREATE TABLE IF NOT EXISTS technical_state (
  name TEXT PRIMARY KEY, state TEXT NOT NULL, changed_at INTEGER NOT NULL,
  last_notified INTEGER, details TEXT
);

CREATE TABLE IF NOT EXISTS venues (
  provider_id TEXT NOT NULL, venue TEXT NOT NULL, kind TEXT NOT NULL,
  enabled INTEGER NOT NULL, config_json TEXT NOT NULL, updated_at_ms INTEGER NOT NULL,
  PRIMARY KEY(provider_id, venue)
);

CREATE TABLE IF NOT EXISTS instruments (
  instrument_id TEXT PRIMARY KEY, canonical_asset_id TEXT NOT NULL,
  provider_id TEXT NOT NULL, venue TEXT NOT NULL, market_type TEXT NOT NULL,
  chain TEXT, contract_address TEXT, base TEXT NOT NULL, quote TEXT NOT NULL,
  venue_symbol TEXT NOT NULL, price_decimals INTEGER NOT NULL,
  size_decimals INTEGER NOT NULL, active INTEGER NOT NULL,
  metadata_json TEXT NOT NULL, updated_at_ms INTEGER NOT NULL,
  UNIQUE(provider_id, venue, market_type, venue_symbol)
);
CREATE INDEX IF NOT EXISTS idx_instruments_canonical ON instruments(canonical_asset_id, active);

CREATE TABLE IF NOT EXISTS data_lineage (
  lineage_id TEXT PRIMARY KEY, provider_id TEXT NOT NULL, venue TEXT NOT NULL,
  instrument_id TEXT NOT NULL, event_type TEXT NOT NULL, source TEXT NOT NULL,
  exchange_timestamp_ms INTEGER NOT NULL, received_at_ms INTEGER NOT NULL,
  sequence INTEGER, payload_hash TEXT NOT NULL, recorded_at_ms INTEGER NOT NULL,
  FOREIGN KEY(instrument_id) REFERENCES instruments(instrument_id)
);
CREATE INDEX IF NOT EXISTS idx_lineage_instrument_time
  ON data_lineage(instrument_id, exchange_timestamp_ms DESC);

CREATE TABLE IF NOT EXISTS normalized_ticks (
  lineage_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, instrument_id TEXT NOT NULL,
  exchange_timestamp_ms INTEGER NOT NULL, received_at_ms INTEGER NOT NULL,
  sequence INTEGER, price REAL, bid REAL, ask REAL, size REAL,
  payload_json TEXT NOT NULL,
  FOREIGN KEY(lineage_id) REFERENCES data_lineage(lineage_id),
  FOREIGN KEY(instrument_id) REFERENCES instruments(instrument_id)
);
CREATE INDEX IF NOT EXISTS idx_normalized_ticks_lookup
  ON normalized_ticks(instrument_id, event_type, exchange_timestamp_ms DESC);

CREATE TABLE IF NOT EXISTS provider_health (
  provider_id TEXT PRIMARY KEY, status TEXT NOT NULL, checked_at_ms INTEGER NOT NULL,
  last_received_at_ms INTEGER, lag_ms INTEGER, consecutive_failures INTEGER NOT NULL,
  last_error TEXT
);

CREATE TABLE IF NOT EXISTS universe_membership (
  instrument_id TEXT NOT NULL, canonical_asset_id TEXT NOT NULL, bucket TEXT NOT NULL,
  score REAL NOT NULL, deep_watchlist INTEGER NOT NULL,
  valid_from_ms INTEGER NOT NULL, valid_to_ms INTEGER,
  PRIMARY KEY(instrument_id, valid_from_ms),
  FOREIGN KEY(instrument_id) REFERENCES instruments(instrument_id)
);
CREATE INDEX IF NOT EXISTS idx_universe_current
  ON universe_membership(valid_to_ms, deep_watchlist, score DESC);

CREATE TABLE IF NOT EXISTS market_discovery_decisions (
  instrument_id TEXT PRIMARY KEY, provider_id TEXT NOT NULL, scope TEXT NOT NULL,
  source TEXT NOT NULL, catalog_received_at_ms INTEGER NOT NULL,
  evaluated_at_ms INTEGER NOT NULL, decision TEXT NOT NULL,
  reasons_json TEXT NOT NULL, metrics_json TEXT NOT NULL,
  FOREIGN KEY(instrument_id) REFERENCES instruments(instrument_id)
);
CREATE INDEX IF NOT EXISTS idx_discovery_provider_decision
  ON market_discovery_decisions(provider_id, decision, evaluated_at_ms DESC);

CREATE TABLE IF NOT EXISTS event_sources (
  source_id TEXT PRIMARY KEY, source_tier TEXT NOT NULL, adapter TEXT NOT NULL,
  enabled INTEGER NOT NULL, official INTEGER NOT NULL, config_json TEXT NOT NULL,
  updated_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS event_entities (
  project_id TEXT PRIMARY KEY, canonical_asset_id TEXT NOT NULL, name TEXT NOT NULL,
  tickers_json TEXT NOT NULL, contracts_json TEXT NOT NULL,
  official_accounts_json TEXT NOT NULL, official_domains_json TEXT NOT NULL,
  updated_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_events (
  raw_event_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, source_tier TEXT NOT NULL,
  author_id TEXT, url TEXT NOT NULL, published_at_ms INTEGER NOT NULL,
  received_at_ms INTEGER NOT NULL, ingestion_latency_ms INTEGER NOT NULL,
  content_hash TEXT NOT NULL, language TEXT, payload_json TEXT NOT NULL,
  text TEXT NOT NULL, relation_type TEXT NOT NULL, related_event_id TEXT,
  thread_id TEXT, authenticity TEXT NOT NULL, explicit_project_id TEXT,
  contracts_json TEXT NOT NULL, deleted INTEGER NOT NULL,
  recorded_at_ms INTEGER NOT NULL,
  FOREIGN KEY(source_id) REFERENCES event_sources(source_id)
);
CREATE INDEX IF NOT EXISTS idx_raw_events_receive ON raw_events(received_at_ms DESC);
CREATE INDEX IF NOT EXISTS idx_raw_events_hash ON raw_events(content_hash, published_at_ms);

CREATE TABLE IF NOT EXISTS normalized_events (
  event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, summary TEXT NOT NULL,
  novelty REAL NOT NULL, credibility REAL NOT NULL, confirmation_count INTEGER NOT NULL,
  expected_direction TEXT NOT NULL, likely_horizons_json TEXT NOT NULL,
  decay_half_life_minutes REAL NOT NULL, contradiction_state TEXT NOT NULL,
  confidence REAL NOT NULL, first_published_at_ms INTEGER NOT NULL,
  first_received_at_ms INTEGER NOT NULL, latest_received_at_ms INTEGER NOT NULL,
  raw_event_ids_json TEXT NOT NULL, source_ids_json TEXT NOT NULL,
  evidence_urls_json TEXT NOT NULL DEFAULT '[]',
  authenticity_risk INTEGER NOT NULL, high_impact INTEGER NOT NULL,
  updated_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_normalized_events_receive
  ON normalized_events(latest_received_at_ms DESC);

CREATE TABLE IF NOT EXISTS event_asset_links (
  event_id TEXT NOT NULL, project_id TEXT NOT NULL, canonical_asset_id TEXT NOT NULL,
  chain_id TEXT, contract_address TEXT, expected_direction TEXT NOT NULL,
  likely_horizons_json TEXT NOT NULL,
  PRIMARY KEY(event_id, project_id, canonical_asset_id),
  FOREIGN KEY(event_id) REFERENCES normalized_events(event_id),
  FOREIGN KEY(project_id) REFERENCES event_entities(project_id)
);

CREATE TABLE IF NOT EXISTS event_confirmations (
  event_id TEXT NOT NULL, raw_event_id TEXT NOT NULL, source_id TEXT NOT NULL,
  confirmation_kind TEXT NOT NULL, confirmed_at_ms INTEGER NOT NULL,
  PRIMARY KEY(event_id, raw_event_id),
  FOREIGN KEY(event_id) REFERENCES normalized_events(event_id),
  FOREIGN KEY(raw_event_id) REFERENCES raw_events(raw_event_id)
);

CREATE TABLE IF NOT EXISTS event_relations (
  raw_event_id TEXT PRIMARY KEY, relation_type TEXT NOT NULL,
  related_event_id TEXT, thread_id TEXT,
  FOREIGN KEY(raw_event_id) REFERENCES raw_events(raw_event_id)
);

CREATE TABLE IF NOT EXISTS event_revisions (
  revision_id INTEGER PRIMARY KEY AUTOINCREMENT, raw_event_id TEXT NOT NULL,
  revision_type TEXT NOT NULL, observed_at_ms INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  FOREIGN KEY(raw_event_id) REFERENCES raw_events(raw_event_id)
);

CREATE TABLE IF NOT EXISTS event_claims (
  claim_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, claim_type TEXT NOT NULL,
  evidence_url TEXT NOT NULL, payload_json TEXT NOT NULL, recorded_at_ms INTEGER NOT NULL,
  FOREIGN KEY(event_id) REFERENCES normalized_events(event_id)
);

CREATE TABLE IF NOT EXISTS event_source_health (
  source_id TEXT PRIMARY KEY, status TEXT NOT NULL, checked_at_ms INTEGER NOT NULL,
  last_received_at_ms INTEGER, lag_ms INTEGER, consecutive_failures INTEGER NOT NULL,
  last_error TEXT, last_successful_poll_at_ms INTEGER
);

CREATE TABLE IF NOT EXISTS onchain_providers (
  provider_id TEXT PRIMARY KEY, chain_id TEXT NOT NULL, adapter TEXT NOT NULL,
  enabled INTEGER NOT NULL, config_json TEXT NOT NULL, updated_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS onchain_assets (
  identity_key TEXT PRIMARY KEY, chain_id TEXT NOT NULL, contract_address TEXT NOT NULL,
  symbol TEXT, name TEXT, first_seen_at_ms INTEGER NOT NULL, last_seen_at_ms INTEGER NOT NULL,
  metadata_json TEXT NOT NULL, UNIQUE(chain_id, contract_address)
);

CREATE TABLE IF NOT EXISTS onchain_events (
  event_id TEXT PRIMARY KEY, provider_id TEXT NOT NULL, identity_key TEXT NOT NULL,
  chain_id TEXT NOT NULL, contract_address TEXT NOT NULL, event_type TEXT NOT NULL,
  block_number INTEGER NOT NULL, block_hash TEXT NOT NULL, parent_hash TEXT,
  transaction_id TEXT NOT NULL, transaction_index INTEGER NOT NULL, log_index INTEGER NOT NULL,
  position_key TEXT NOT NULL, exchange_timestamp_ms INTEGER NOT NULL,
  received_at_ms INTEGER NOT NULL, payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
  chain_verified INTEGER NOT NULL, confirmations INTEGER NOT NULL, recorded_at_ms INTEGER NOT NULL,
  FOREIGN KEY(identity_key) REFERENCES onchain_assets(identity_key)
);
CREATE INDEX IF NOT EXISTS idx_onchain_events_identity_time
  ON onchain_events(identity_key, received_at_ms, block_number, transaction_index, log_index);
CREATE INDEX IF NOT EXISTS idx_onchain_events_position
  ON onchain_events(position_key, received_at_ms, event_id);

CREATE TABLE IF NOT EXISTS onchain_canonicality_history (
  event_id TEXT NOT NULL, observed_at_ms INTEGER NOT NULL, is_canonical INTEGER NOT NULL,
  block_hash TEXT NOT NULL, reason TEXT NOT NULL,
  PRIMARY KEY(event_id, observed_at_ms, is_canonical),
  FOREIGN KEY(event_id) REFERENCES onchain_events(event_id)
);

CREATE TABLE IF NOT EXISTS onchain_reorgs (
  reorg_id INTEGER PRIMARY KEY AUTOINCREMENT, position_key TEXT NOT NULL,
  old_event_id TEXT NOT NULL, new_event_id TEXT NOT NULL,
  old_block_hash TEXT NOT NULL, new_block_hash TEXT NOT NULL,
  observed_at_ms INTEGER NOT NULL,
  UNIQUE(position_key, old_event_id, new_event_id, observed_at_ms)
);

CREATE TABLE IF NOT EXISTS onchain_assessments (
  identity_key TEXT NOT NULL, as_of_ms INTEGER NOT NULL, status TEXT NOT NULL,
  payload_json TEXT NOT NULL, recorded_at_ms INTEGER NOT NULL,
  PRIMARY KEY(identity_key, as_of_ms),
  FOREIGN KEY(identity_key) REFERENCES onchain_assets(identity_key)
);
CREATE INDEX IF NOT EXISTS idx_onchain_assessments_latest
  ON onchain_assessments(as_of_ms DESC, status);

CREATE TABLE IF NOT EXISTS onchain_provider_health (
  provider_id TEXT PRIMARY KEY, chain_id TEXT NOT NULL, status TEXT NOT NULL,
  checked_at_ms INTEGER NOT NULL, last_received_at_ms INTEGER, lag_ms INTEGER,
  consecutive_failures INTEGER NOT NULL, last_error TEXT
);

CREATE TABLE IF NOT EXISTS research_dataset_snapshots (
  snapshot_id TEXT PRIMARY KEY, content_hash TEXT NOT NULL UNIQUE,
  created_at_ms INTEGER NOT NULL, decision_start_ms INTEGER,
  decision_end_ms INTEGER, row_count INTEGER NOT NULL,
  code_version TEXT NOT NULL, config_hash TEXT NOT NULL,
  feature_schema_json TEXT NOT NULL, lineage_json TEXT NOT NULL,
  universe_json TEXT NOT NULL, snapshot_path TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_experiments (
  experiment_id TEXT PRIMARY KEY, snapshot_id TEXT,
  created_at_ms INTEGER NOT NULL, status TEXT NOT NULL,
  split_plan_json TEXT NOT NULL, cost_model_json TEXT NOT NULL,
  baseline_metrics_json TEXT NOT NULL, challenger_metrics_json TEXT NOT NULL,
  model_card_json TEXT NOT NULL, promotion_decision TEXT NOT NULL,
  promoted INTEGER NOT NULL CHECK(promoted IN (0, 1)),
  FOREIGN KEY(snapshot_id) REFERENCES research_dataset_snapshots(snapshot_id)
);

CREATE TABLE IF NOT EXISTS analysis_observations (
  observation_id TEXT PRIMARY KEY, instrument_id TEXT NOT NULL, provider_id TEXT NOT NULL,
  venue TEXT NOT NULL, market_type TEXT NOT NULL, base TEXT NOT NULL, quote TEXT NOT NULL,
  venue_symbol TEXT NOT NULL, canonical_asset_id TEXT NOT NULL, chain TEXT, contract_address TEXT,
  price_decimals INTEGER NOT NULL, size_decimals INTEGER NOT NULL, observed_at_ms INTEGER NOT NULL,
  universe_snapshot_id TEXT,
  feature_version TEXT NOT NULL, feature_config_hash TEXT NOT NULL, reference_price REAL,
  reference_spread_bps REAL, reference_received_at_ms INTEGER, reference_exchange_timestamp_ms INTEGER,
  reference_time_semantics TEXT NOT NULL, baseline_direction INTEGER NOT NULL,
  universe_member INTEGER NOT NULL, universe_bucket TEXT, universe_score REAL,
  universe_reasons_json TEXT NOT NULL, features_json TEXT NOT NULL, quality_json TEXT NOT NULL,
  source_lineage_json TEXT NOT NULL, context_json TEXT NOT NULL, cost_policy_json TEXT NOT NULL,
  recorded_at_ms INTEGER NOT NULL,
  UNIQUE(instrument_id,feature_version,feature_config_hash,observed_at_ms)
);
CREATE INDEX IF NOT EXISTS idx_analysis_observations_time
  ON analysis_observations(instrument_id,observed_at_ms);
CREATE TABLE IF NOT EXISTS analysis_outcomes (
  observation_id TEXT NOT NULL, horizon TEXT NOT NULL, target_at_ms INTEGER NOT NULL, deadline_at_ms INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('PENDING','OBSERVED','UNAVAILABLE')), reason TEXT, target_mid REAL, target_spread_bps REAL,
  target_exchange_timestamp_ms INTEGER, target_received_at_ms INTEGER, target_time_semantics TEXT,
  gross_reference_return REAL, long_reference_net_return REAL, costs_adjusted_observational_return REAL,
  baseline_direction INTEGER NOT NULL, baseline_gross_return REAL,
  baseline_costs_adjusted_return REAL, cost_assumptions_json TEXT NOT NULL,
  observed_costs_json TEXT NOT NULL DEFAULT '{}',
  updated_at_ms INTEGER NOT NULL, PRIMARY KEY(observation_id,horizon),
  FOREIGN KEY(observation_id) REFERENCES analysis_observations(observation_id)
);
CREATE TABLE IF NOT EXISTS analysis_universe_snapshots (
  snapshot_id TEXT PRIMARY KEY, observed_at_ms INTEGER NOT NULL, cadence_bucket INTEGER NOT NULL,
  universe_hash TEXT NOT NULL, candidate_count INTEGER NOT NULL, deep_count INTEGER NOT NULL,
  members_json TEXT NOT NULL, selection_lineage_json TEXT NOT NULL, recorded_at_ms INTEGER NOT NULL,
  UNIQUE(universe_hash,cadence_bucket)
);
CREATE INDEX IF NOT EXISTS idx_analysis_universe_snapshot_time ON analysis_universe_snapshots(observed_at_ms DESC);
CREATE INDEX IF NOT EXISTS idx_analysis_outcomes_due ON analysis_outcomes(status,target_at_ms);
CREATE INDEX IF NOT EXISTS idx_research_experiments_created
  ON research_experiments(created_at_ms DESC, status);

CREATE TABLE IF NOT EXISTS paper_decisions (
  decision_id TEXT PRIMARY KEY, opportunity_id TEXT NOT NULL,
  canonical_asset_id TEXT NOT NULL, instrument_id TEXT NOT NULL,
  decision_at_ms INTEGER NOT NULL, direction TEXT NOT NULL, status TEXT NOT NULL,
  recommendation_json TEXT NOT NULL, risk_json TEXT NOT NULL,
  created_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_paper_decisions_time
  ON paper_decisions(decision_at_ms DESC, status);

CREATE TABLE IF NOT EXISTS paper_recommendation_revisions (
  revision_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL,
  observed_at_ms INTEGER NOT NULL, material_hash TEXT NOT NULL,
  recommendation_json TEXT NOT NULL,
  UNIQUE(decision_id, material_hash),
  FOREIGN KEY(decision_id) REFERENCES paper_decisions(decision_id)
);

CREATE TABLE IF NOT EXISTS paper_state_transitions (
  transition_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL,
  previous_state TEXT, new_state TEXT NOT NULL, reason TEXT NOT NULL,
  occurred_at_ms INTEGER NOT NULL, payload_json TEXT NOT NULL,
  FOREIGN KEY(decision_id) REFERENCES paper_decisions(decision_id)
);
CREATE INDEX IF NOT EXISTS idx_paper_transitions_decision
  ON paper_state_transitions(decision_id, occurred_at_ms, transition_id);

CREATE TABLE IF NOT EXISTS paper_ledger (
  entry_id TEXT PRIMARY KEY, decision_id TEXT, event_type TEXT NOT NULL,
  occurred_at_ms INTEGER NOT NULL, direction INTEGER NOT NULL,
  quantity_delta REAL NOT NULL, price REAL NOT NULL, notional_usd REAL NOT NULL,
  cash_delta_usd REAL NOT NULL, gross_pnl_usd REAL NOT NULL,
  net_pnl_usd REAL NOT NULL, fees_usd REAL NOT NULL,
  funding_usd REAL NOT NULL, slippage_usd REAL NOT NULL,
  metadata_json TEXT NOT NULL,
  FOREIGN KEY(decision_id) REFERENCES paper_decisions(decision_id)
);
CREATE INDEX IF NOT EXISTS idx_paper_ledger_time
  ON paper_ledger(occurred_at_ms, entry_id);

CREATE TABLE IF NOT EXISTS paper_outcomes (
  outcome_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL,
  evaluated_at_ms INTEGER NOT NULL, outcome_state TEXT NOT NULL,
  gross_pnl_usd REAL NOT NULL, net_pnl_usd REAL NOT NULL,
  payload_json TEXT NOT NULL,
  FOREIGN KEY(decision_id) REFERENCES paper_decisions(decision_id)
);

CREATE TABLE IF NOT EXISTS paper_alerts (
  alert_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL,
  material_hash TEXT NOT NULL, sent_at_ms INTEGER NOT NULL,
  telegram_message_id TEXT, message TEXT NOT NULL,
  UNIQUE(decision_id, material_hash),
  FOREIGN KEY(decision_id) REFERENCES paper_decisions(decision_id)
);

-- A durable claim is written before network I/O. Delivery results are append-only;
-- a claim without a result after restart is UNKNOWN and is never resent blindly.
CREATE TABLE IF NOT EXISTS telegram_delivery_claims (
  delivery_key TEXT PRIMARY KEY, claimed_at_ms INTEGER NOT NULL,
  message_hash TEXT NOT NULL, message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS telegram_delivery_results (
  delivery_key TEXT NOT NULL, observed_at_ms INTEGER NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('SENT','FAILED','UNKNOWN')),
  telegram_message_id TEXT, error_class TEXT,
  PRIMARY KEY(delivery_key, observed_at_ms),
  FOREIGN KEY(delivery_key) REFERENCES telegram_delivery_claims(delivery_key)
);
CREATE INDEX IF NOT EXISTS idx_telegram_delivery_results_key
  ON telegram_delivery_results(delivery_key, observed_at_ms DESC);
CREATE TRIGGER IF NOT EXISTS telegram_delivery_claims_no_update
BEFORE UPDATE ON telegram_delivery_claims BEGIN
  SELECT RAISE(ABORT, 'telegram delivery claims are immutable');
END;
CREATE TRIGGER IF NOT EXISTS telegram_delivery_claims_no_delete
BEFORE DELETE ON telegram_delivery_claims BEGIN
  SELECT RAISE(ABORT, 'telegram delivery claims are immutable');
END;
CREATE TRIGGER IF NOT EXISTS telegram_delivery_results_no_update
BEFORE UPDATE ON telegram_delivery_results BEGIN
  SELECT RAISE(ABORT, 'telegram delivery results are immutable');
END;
CREATE TRIGGER IF NOT EXISTS telegram_delivery_results_no_delete
BEFORE DELETE ON telegram_delivery_results BEGIN
  SELECT RAISE(ABORT, 'telegram delivery results are immutable');
END;

CREATE TRIGGER IF NOT EXISTS raw_events_no_update
BEFORE UPDATE ON raw_events BEGIN
  SELECT RAISE(ABORT, 'raw_events are immutable');
END;

CREATE TRIGGER IF NOT EXISTS raw_events_no_delete
BEFORE DELETE ON raw_events BEGIN
  SELECT RAISE(ABORT, 'raw_events are immutable');
END;

CREATE TRIGGER IF NOT EXISTS onchain_events_no_update
BEFORE UPDATE ON onchain_events BEGIN
  SELECT RAISE(ABORT, 'onchain_events are immutable');
END;

CREATE TRIGGER IF NOT EXISTS onchain_events_no_delete
BEFORE DELETE ON onchain_events BEGIN
  SELECT RAISE(ABORT, 'onchain_events are immutable');
END;

CREATE TRIGGER IF NOT EXISTS research_dataset_snapshots_no_update
BEFORE UPDATE ON research_dataset_snapshots BEGIN
  SELECT RAISE(ABORT, 'research dataset snapshots are immutable');
END;

CREATE TRIGGER IF NOT EXISTS research_dataset_snapshots_no_delete
BEFORE DELETE ON research_dataset_snapshots BEGIN
  SELECT RAISE(ABORT, 'research dataset snapshots are immutable');
END;

CREATE TRIGGER IF NOT EXISTS research_experiments_no_update
BEFORE UPDATE ON research_experiments BEGIN
  SELECT RAISE(ABORT, 'research experiments are immutable');
END;

CREATE TRIGGER IF NOT EXISTS research_experiments_no_delete
BEFORE DELETE ON research_experiments BEGIN
  SELECT RAISE(ABORT, 'research experiments are immutable');
END;

CREATE TRIGGER IF NOT EXISTS paper_decisions_no_update
BEFORE UPDATE ON paper_decisions BEGIN
  SELECT RAISE(ABORT, 'paper decisions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS paper_decisions_no_delete
BEFORE DELETE ON paper_decisions BEGIN
  SELECT RAISE(ABORT, 'paper decisions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS paper_revisions_no_update
BEFORE UPDATE ON paper_recommendation_revisions BEGIN
  SELECT RAISE(ABORT, 'paper recommendation revisions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS paper_revisions_no_delete
BEFORE DELETE ON paper_recommendation_revisions BEGIN
  SELECT RAISE(ABORT, 'paper recommendation revisions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS paper_transitions_no_update
BEFORE UPDATE ON paper_state_transitions BEGIN
  SELECT RAISE(ABORT, 'paper state transitions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS paper_transitions_no_delete
BEFORE DELETE ON paper_state_transitions BEGIN
  SELECT RAISE(ABORT, 'paper state transitions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS paper_ledger_no_update
BEFORE UPDATE ON paper_ledger BEGIN
  SELECT RAISE(ABORT, 'paper ledger is immutable');
END;
CREATE TRIGGER IF NOT EXISTS paper_ledger_no_delete
BEFORE DELETE ON paper_ledger BEGIN
  SELECT RAISE(ABORT, 'paper ledger is immutable');
END;
CREATE TRIGGER IF NOT EXISTS paper_outcomes_no_update
BEFORE UPDATE ON paper_outcomes BEGIN
  SELECT RAISE(ABORT, 'paper outcomes are immutable');
END;
CREATE TRIGGER IF NOT EXISTS paper_outcomes_no_delete
BEFORE DELETE ON paper_outcomes BEGIN
  SELECT RAISE(ABORT, 'paper outcomes are immutable');
END;
CREATE TRIGGER IF NOT EXISTS paper_alerts_no_update
BEFORE UPDATE ON paper_alerts BEGIN
  SELECT RAISE(ABORT, 'paper alerts are immutable');
END;
CREATE TRIGGER IF NOT EXISTS paper_alerts_no_delete
BEFORE DELETE ON paper_alerts BEGIN
  SELECT RAISE(ABORT, 'paper alerts are immutable');
END;
"""


class SQLiteStorage:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        with self._lock:
            self._connection.executescript(SCHEMA)
            self._ensure_column("candles", "received_at_ms", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("asset_contexts", "exchange_timestamp_ms", "INTEGER")
            self._ensure_column("asset_contexts", "received_at_ms", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("normalized_events", "evidence_urls_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column("event_source_health", "last_successful_poll_at_ms", "INTEGER")
            # Before schema v4 AssetContext.timestamp was already the local receive
            # time, so this backfill preserves the original semantics exactly.
            self._connection.execute(
                "UPDATE asset_contexts SET received_at_ms=timestamp WHERE received_at_ms=0"
            )
            versions = self._connection.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()
            previous_version = int(versions[0]) if versions and versions[0] is not None else None
            if previous_version is None:
                migration_versions = (SCHEMA_VERSION,)
            else:
                migration_versions = range(max(5, previous_version + 1), SCHEMA_VERSION + 1)
            self._connection.executemany(
                "INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES (?,CAST(strftime('%s','now') AS INTEGER)*1000)",
                ((version,) for version in migration_versions),
            )
            self._connection.commit()

    def _ensure_column(self, table: str, column: str, declaration: str) -> None:
        columns = {
            str(row[1]) for row in self._connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            self._connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._connection.execute(sql, tuple(params))
            self._connection.commit()
            return cursor

    def claim_telegram_delivery(
        self, delivery_key: str, claimed_at_ms: int, message: str
    ) -> bool:
        import hashlib
        message_hash = hashlib.sha256(message.encode("utf-8")).hexdigest()
        cursor = self.execute(
            """INSERT OR IGNORE INTO telegram_delivery_claims(
            delivery_key,claimed_at_ms,message_hash,message) VALUES (?,?,?,?)""",
            (delivery_key, claimed_at_ms, message_hash, message),
        )
        return cursor.rowcount > 0

    def record_telegram_delivery(
        self, delivery_key: str, observed_at_ms: int, state: str,
        telegram_message_id: str | None = None, error_class: str | None = None,
    ) -> bool:
        if state not in {"SENT", "FAILED"}:
            raise ValueError("delivery state must be SENT or FAILED")
        cursor = self.execute(
            """INSERT OR IGNORE INTO telegram_delivery_results(
            delivery_key,observed_at_ms,state,telegram_message_id,error_class)
            VALUES (?,?,?,?,?)""",
            (delivery_key, observed_at_ms, state, telegram_message_id, error_class),
        )
        return cursor.rowcount > 0

    def telegram_delivery_status(self) -> dict[str, int]:
        row = self.query("""SELECT
          (SELECT COUNT(*) FROM telegram_delivery_claims) AS claimed,
          (SELECT COUNT(*) FROM telegram_delivery_claims c WHERE NOT EXISTS
            (SELECT 1 FROM telegram_delivery_results r WHERE r.delivery_key=c.delivery_key)) +
          (SELECT COUNT(*) FROM telegram_delivery_results WHERE state='UNKNOWN') AS unknown,
          (SELECT COUNT(*) FROM telegram_delivery_results WHERE state='FAILED') AS failed,
          (SELECT COUNT(*) FROM telegram_delivery_results WHERE state='SENT') AS sent""")[0]
        return {key: int(value) for key, value in row.items()}

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self._lock:
            self._connection.executemany(sql, rows)
            self._connection.commit()

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._connection.execute(sql, tuple(params)).fetchall()]

    def health(self, *, deep: bool = True) -> dict[str, Any]:
        # Full integrity checks scan every page and index. Keep them available
        # for explicit diagnostics, outside the daemon's periodic heartbeat.
        result = self.query("PRAGMA integrity_check") if deep else []
        foreign_key_errors = self.query("PRAGMA foreign_key_check") if deep else []
        versions = self.query("SELECT MAX(version) AS version FROM schema_migrations")
        integrity = next(iter(result[0].values())) if result else "not_checked"
        if foreign_key_errors:
            integrity = "foreign_key_error"
        size = 0 if str(self.path) == ":memory:" else self.path.stat().st_size
        return {"path": str(self.path), "integrity": integrity, "schema_version": versions[0]["version"] if versions else None, "bytes": size}

    @staticmethod
    def json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=float)
