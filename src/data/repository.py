from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, Iterable

import pandas as pd

from src.hyperliquid.schemas import AssetContext, Candle
from src.providers.common import payload_hash
from src.providers.instruments import InstrumentRegistry
from src.providers.types import (
    NormalizedBBO,
    NormalizedEvent,
    NormalizedTrade,
    ProviderHealth,
)
from src.providers.universe import UniverseMembership

from .storage import SQLiteStorage


class MarketRepository:
    def __init__(self, storage: SQLiteStorage):
        self.storage = storage

    def sync_provider_catalog(
        self,
        providers: dict[str, dict[str, Any]],
        instruments: InstrumentRegistry,
        timestamp_ms: int,
    ) -> None:
        venue_rows: list[tuple[Any, ...]] = []
        for provider_id, settings in providers.items():
            venues = sorted({
                item.venue for item in instruments.all(active_only=False) if item.provider == provider_id
            }) or [provider_id]
            safe_settings = {
                key: value for key, value in settings.items()
                if not any(token in str(key).lower() for token in ("secret", "private", "token", "key"))
            }
            for venue in venues:
                venue_rows.append((
                    provider_id, venue, str(settings.get("kind", provider_id)),
                    int(bool(settings.get("enabled", False))), self.storage.json(safe_settings), timestamp_ms,
                ))
        if venue_rows:
            self.storage.executemany(
                """INSERT INTO venues(provider_id,venue,kind,enabled,config_json,updated_at_ms)
                VALUES (?,?,?,?,?,?) ON CONFLICT(provider_id,venue) DO UPDATE SET
                kind=excluded.kind,enabled=excluded.enabled,config_json=excluded.config_json,
                updated_at_ms=excluded.updated_at_ms""",
                venue_rows,
            )
        instrument_rows = [
            (
                item.instrument_id, item.canonical_asset_id, item.provider, item.venue,
                item.market_type.value, item.chain, item.contract_address, item.base, item.quote,
                item.venue_symbol, item.price_decimals, item.size_decimals, int(item.active),
                self.storage.json(item.metadata), timestamp_ms,
            )
            for item in instruments.all(active_only=False)
        ]
        if instrument_rows:
            self.storage.executemany(
                """INSERT INTO instruments(
                instrument_id,canonical_asset_id,provider_id,venue,market_type,chain,contract_address,
                base,quote,venue_symbol,price_decimals,size_decimals,active,metadata_json,updated_at_ms
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(instrument_id) DO UPDATE SET
                canonical_asset_id=excluded.canonical_asset_id,provider_id=excluded.provider_id,
                venue=excluded.venue,market_type=excluded.market_type,chain=excluded.chain,
                contract_address=excluded.contract_address,base=excluded.base,quote=excluded.quote,
                venue_symbol=excluded.venue_symbol,price_decimals=excluded.price_decimals,
                size_decimals=excluded.size_decimals,active=excluded.active,
                metadata_json=excluded.metadata_json,updated_at_ms=excluded.updated_at_ms""",
                instrument_rows,
            )

    def save_normalized_event(self, event: NormalizedEvent) -> bool:
        meta = event.metadata
        event_type = type(event).__name__
        body = asdict(event)
        encoded = self.storage.json(body)
        lineage_id = meta.lineage_id
        cursor = self.storage.execute(
            """INSERT OR IGNORE INTO data_lineage(
            lineage_id,provider_id,venue,instrument_id,event_type,source,exchange_timestamp_ms,
            received_at_ms,sequence,payload_hash,recorded_at_ms
            ) VALUES (?,?,?,?,?,?,?,?,?,?,CAST(strftime('%s','now') AS INTEGER)*1000)""",
            (
                lineage_id, meta.provider, meta.venue, meta.instrument_id, event_type, meta.source,
                meta.exchange_timestamp_ms, meta.received_at_ms, meta.sequence, payload_hash(body),
            ),
        )
        if cursor.rowcount == 0:
            return False
        price = getattr(event, "price", None)
        bid = event.bid if isinstance(event, NormalizedBBO) else None
        ask = event.ask if isinstance(event, NormalizedBBO) else None
        size = event.size if isinstance(event, NormalizedTrade) else None
        self.storage.execute(
            """INSERT OR IGNORE INTO normalized_ticks(
            lineage_id,event_type,instrument_id,exchange_timestamp_ms,received_at_ms,sequence,
            price,bid,ask,size,payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                lineage_id, event_type, meta.instrument_id, meta.exchange_timestamp_ms,
                meta.received_at_ms, meta.sequence, price, bid, ask, size, encoded,
            ),
        )
        return True

    def prune_normalized_ticks(self, cutoff_received_at_ms: int) -> int:
        """Delete expired raw market ticks and their payload-free lineage rows."""
        removed = self.storage.execute(
            "DELETE FROM normalized_ticks WHERE received_at_ms < ?",
            (int(cutoff_received_at_ms),),
        ).rowcount
        self.storage.execute(
            "DELETE FROM data_lineage WHERE received_at_ms < ? "
            "AND NOT EXISTS (SELECT 1 FROM normalized_ticks t WHERE t.lineage_id=data_lineage.lineage_id)",
            (int(cutoff_received_at_ms),),
        )
        return int(removed)

    def save_provider_health(self, health: ProviderHealth) -> None:
        self.storage.execute(
            """INSERT INTO provider_health(
            provider_id,status,checked_at_ms,last_received_at_ms,lag_ms,consecutive_failures,last_error
            ) VALUES (?,?,?,?,?,?,?) ON CONFLICT(provider_id) DO UPDATE SET
            status=excluded.status,checked_at_ms=excluded.checked_at_ms,
            last_received_at_ms=excluded.last_received_at_ms,lag_ms=excluded.lag_ms,
            consecutive_failures=excluded.consecutive_failures,last_error=excluded.last_error""",
            (
                health.provider, health.status.value, health.checked_at_ms, health.last_received_at_ms,
                health.lag_ms, health.consecutive_failures, health.last_error,
            ),
        )

    def save_universe_membership(
        self, membership: Iterable[UniverseMembership], timestamp_ms: int
    ) -> None:
        self.storage.execute(
            "UPDATE universe_membership SET valid_to_ms=? WHERE valid_to_ms IS NULL",
            (timestamp_ms,),
        )
        rows = [
            (
                item.instrument_id, item.canonical_asset_id, item.bucket.value, item.score,
                int(item.deep_watchlist), timestamp_ms,
            )
            for item in membership
        ]
        if rows:
            self.storage.executemany(
                """INSERT INTO universe_membership(
                instrument_id,canonical_asset_id,bucket,score,deep_watchlist,valid_from_ms,valid_to_ms
                ) VALUES (?,?,?,?,?,?,NULL)""",
                rows,
            )

    def save_market_discovery_decisions(self, rows: Iterable[dict[str, Any]]) -> None:
        allowed = {"ADMITTED", "EXCLUDED", "DELISTED", "STALE_CATALOG"}
        values = []
        for row in rows:
            decision = str(row["decision"])
            if decision not in allowed:
                raise ValueError("invalid market discovery decision")
            values.append((
                str(row["instrument_id"]), str(row["provider_id"]), str(row["scope"]),
                str(row["source"]), int(row["catalog_received_at_ms"]), int(row["evaluated_at_ms"]),
                decision, self.storage.json(tuple(row.get("reasons", ()))),
                self.storage.json(dict(row.get("metrics", {}))),
            ))
        if values:
            self.storage.executemany(
                """INSERT INTO market_discovery_decisions(
            instrument_id,provider_id,scope,source,catalog_received_at_ms,evaluated_at_ms,
            decision,reasons_json,metrics_json) VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(instrument_id) DO UPDATE SET provider_id=excluded.provider_id,
            scope=excluded.scope,source=excluded.source,
            catalog_received_at_ms=excluded.catalog_received_at_ms,
            evaluated_at_ms=excluded.evaluated_at_ms,decision=excluded.decision,
            reasons_json=excluded.reasons_json,metrics_json=excluded.metrics_json""",
                values,
            )

    def market_discovery_decisions(self, *, limit: int = 500) -> list[dict[str, Any]]:
        return self.storage.query(
            "SELECT * FROM market_discovery_decisions ORDER BY evaluated_at_ms DESC,instrument_id LIMIT ?",
            (max(1, min(int(limit), 5000)),),
        )

    def current_universe(self) -> list[dict[str, Any]]:
        return self.storage.query(
            """SELECT * FROM universe_membership WHERE valid_to_ms IS NULL
            ORDER BY deep_watchlist DESC,score DESC,instrument_id"""
        )

    def provider_health_rows(self) -> list[dict[str, Any]]:
        return self.storage.query("SELECT * FROM provider_health ORDER BY provider_id")

    def upsert_candles(self, candles: Iterable[Candle]) -> None:
        rows = [
            (
                c.coin, c.interval, c.open_time, c.close_time, c.open, c.high, c.low,
                c.close, c.volume, c.trades, c.received_at_ms,
            )
            for c in candles
        ]
        if rows:
            self.storage.executemany(
                """INSERT INTO candles(
                    coin,interval,open_time,close_time,open,high,low,close,volume,trades,received_at_ms
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(coin,interval,open_time) DO UPDATE SET
                close_time=excluded.close_time,open=excluded.open,high=excluded.high,
                low=excluded.low,close=excluded.close,volume=excluded.volume,trades=excluded.trades,
                received_at_ms=excluded.received_at_ms""",
                rows,
            )

    def candles(self, coin: str, interval: str, limit: int = 5000) -> pd.DataFrame:
        rows = self.storage.query(
            "SELECT * FROM candles WHERE coin=? AND interval=? ORDER BY open_time DESC LIMIT ?",
            (coin, interval, limit),
        )
        if not rows:
            return pd.DataFrame(columns=["open_time", "close_time", "open", "high", "low", "close", "volume", "trades"])
        return pd.DataFrame(rows).sort_values("open_time").reset_index(drop=True)

    def latest_candle_time(self, coin: str, interval: str) -> int | None:
        rows = self.storage.query("SELECT MAX(open_time) AS value FROM candles WHERE coin=? AND interval=?", (coin, interval))
        return int(rows[0]["value"]) if rows and rows[0]["value"] is not None else None

    def upsert_contexts(self, contexts: Iterable[AssetContext]) -> None:
        rows = [
            (c.coin, c.timestamp, c.mark, c.oracle, c.mid, c.funding, c.open_interest,
             c.day_notional_volume, c.premium, c.previous_day_price,
             c.exchange_timestamp_ms, c.received_at_ms)
            for c in contexts
        ]
        if rows:
            self.storage.executemany(
                """INSERT OR REPLACE INTO asset_contexts(
                    coin,timestamp,mark,oracle,mid,funding,open_interest,day_notional_volume,
                    premium,previous_day_price,exchange_timestamp_ms,received_at_ms
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )

    def context_history(self, coin: str, since_ms: int = 0, limit: int = 10000) -> pd.DataFrame:
        rows = self.storage.query(
            "SELECT * FROM asset_contexts WHERE coin=? AND timestamp>=? ORDER BY timestamp DESC LIMIT ?",
            (coin, since_ms, limit),
        )
        return pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True) if rows else pd.DataFrame()

    def latest_context(self, coin: str) -> AssetContext | None:
        rows = self.storage.query("SELECT * FROM asset_contexts WHERE coin=? ORDER BY timestamp DESC LIMIT 1", (coin,))
        if not rows:
            return None
        row = rows[0]
        return AssetContext(
            coin=row["coin"], timestamp=int(row["timestamp"]), mark=float(row["mark"]),
            oracle=float(row["oracle"]), mid=float(row["mid"]) if row["mid"] is not None else None,
            funding=float(row["funding"]), open_interest=float(row["open_interest"]),
            day_notional_volume=float(row["day_notional_volume"]),
            premium=float(row["premium"]) if row["premium"] is not None else None,
            previous_day_price=float(row["previous_day_price"]),
            received_at_ms=int(row.get("received_at_ms") or 0),
            exchange_timestamp_ms=(
                int(row["exchange_timestamp_ms"])
                if row.get("exchange_timestamp_ms") is not None else None
            ),
        )

    def save_book_aggregate(self, coin: str, timestamp: int, values: dict[str, Any]) -> None:
        self.storage.execute(
            "INSERT OR REPLACE INTO book_aggregates VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (coin, timestamp, values.get("best_bid"), values.get("best_ask"), values.get("spread"),
             values.get("relative_spread"), values.get("bid_depth"), values.get("ask_depth"),
             values.get("imbalance"), values.get("weighted_imbalance"), values.get("microprice"),
             values.get("book_pressure"), values.get("depth_slope"), self.storage.json(values.get("walls", []))),
        )

    def save_trade_aggregate(self, coin: str, timestamp: int, values: dict[str, Any]) -> None:
        self.storage.execute(
            "INSERT OR REPLACE INTO trade_aggregates VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (coin, timestamp, values.get("buy_volume", 0), values.get("sell_volume", 0), values.get("delta", 0),
             values.get("large_buy_volume", 0), values.get("large_sell_volume", 0), values.get("trade_count", 0),
             values.get("poc"), self.storage.json(values.get("hvn", [])), self.storage.json(values.get("lvn", []))),
        )

    def save_signal(self, signal: dict[str, Any]) -> int:
        cursor = self.storage.execute(
            "INSERT INTO signals(coin,timestamp,primary_horizon,direction,probability,payload_json,model_status) VALUES (?,?,?,?,?,?,?)",
            (signal["coin"], signal["timestamp"], signal["primary_horizon"], signal["direction"],
             signal["probability"], self.storage.json(signal), signal["model_status"]),
        )
        return int(cursor.lastrowid)

    def last_alert(self, coin: str) -> dict[str, Any] | None:
        rows = self.storage.query("SELECT * FROM alerts WHERE coin=? ORDER BY timestamp DESC LIMIT 1", (coin,))
        return rows[0] if rows else None

    def save_alert(self, coin: str, timestamp: int, direction: str, probability: float, message: str, message_id: str | None = None) -> None:
        self.storage.execute(
            "INSERT INTO alerts(coin,timestamp,direction,probability,message,telegram_message_id) VALUES (?,?,?,?,?,?)",
            (coin, timestamp, direction, probability, message, message_id),
        )

    def save_model_metrics(self, coin: str, horizon: str, trained_at: int, ready: bool, metrics: dict[str, Any]) -> None:
        self.storage.execute(
            "INSERT INTO model_metrics VALUES (?,?,?,?,?,?,?,?,?)",
            (coin, horizon, trained_at, int(ready), metrics.get("balanced_accuracy"), metrics.get("brier"),
             metrics.get("log_loss"), metrics.get("ece"), self.storage.json(metrics)),
        )

    def record_critical_error(self, fingerprint: str, scope: str, timestamp: int, message: str) -> dict[str, Any]:
        self.storage.execute(
            """INSERT INTO critical_errors(
                fingerprint,scope,first_seen,last_seen,last_notified,occurrences,message
            ) VALUES (?,?,?,?,NULL,1,?)
            ON CONFLICT(fingerprint) DO UPDATE SET
                scope=excluded.scope,last_seen=excluded.last_seen,
                occurrences=critical_errors.occurrences+1,message=excluded.message""",
            (fingerprint, scope, timestamp, timestamp, message),
        )
        return self.storage.query("SELECT * FROM critical_errors WHERE fingerprint=?", (fingerprint,))[0]

    def mark_critical_error_notified(self, fingerprint: str, timestamp: int) -> None:
        self.storage.execute(
            "UPDATE critical_errors SET last_notified=? WHERE fingerprint=?",
            (timestamp, fingerprint),
        )

    def latest_signal(self, coin: str) -> dict[str, Any] | None:
        rows = self.storage.query("SELECT * FROM signals WHERE coin=? ORDER BY timestamp DESC LIMIT 1", (coin,))
        return rows[0] if rows else None

    def latest_telegram_event(self) -> dict[str, Any] | None:
        alerts = self.storage.query(
            "SELECT timestamp,coin AS scope,message,'signal' AS kind FROM alerts ORDER BY timestamp DESC LIMIT 1"
        )
        critical = self.storage.query(
            """SELECT last_notified AS timestamp,scope,message,'critical' AS kind
            FROM critical_errors WHERE last_notified IS NOT NULL ORDER BY last_notified DESC LIMIT 1"""
        )
        signal_alerts = self.storage.query(
            """SELECT timestamp,coin AS scope,message,'signal' AS kind
            FROM signal_alerts ORDER BY timestamp DESC LIMIT 1"""
        )
        candidates = alerts + critical + signal_alerts
        return max(candidates, key=lambda item: int(item["timestamp"])) if candidates else None

    def recent_signal_payloads(self, coin: str, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.storage.query(
            "SELECT payload_json FROM signals WHERE coin=? ORDER BY timestamp DESC,id DESC LIMIT ?",
            (coin, limit),
        )
        return [json.loads(row["payload_json"]) for row in rows]

    def save_signal_alert(self, signal: dict[str, Any], event: str, message: str, message_id: str | None) -> None:
        primary = signal["horizons"][signal["primary_horizon"]]
        direction = str(signal["direction"])
        probability = float(primary.get(direction, max(primary["LONG"], primary["SHORT"])))
        self.storage.execute(
            """INSERT INTO signal_alerts(
                coin,timestamp,event,direction,probability,signal_strength,payload_json,message,telegram_message_id
            ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                signal["coin"], signal["timestamp"], event, direction, probability,
                signal["signal_strength"], self.storage.json(signal), message, message_id,
            ),
        )

    def last_signal_alert(self, coin: str) -> dict[str, Any] | None:
        rows = self.storage.query(
            "SELECT * FROM signal_alerts WHERE coin=? ORDER BY timestamp DESC,id DESC LIMIT 1",
            (coin,),
        )
        if rows:
            rows[0]["payload"] = json.loads(rows[0]["payload_json"])
            return rows[0]
        legacy = self.last_alert(coin)
        if legacy is None:
            return None
        signals = self.storage.query(
            "SELECT payload_json FROM signals WHERE coin=? AND timestamp<=? ORDER BY timestamp DESC,id DESC LIMIT 1",
            (coin, legacy["timestamp"]),
        )
        if not signals:
            return None
        return {**legacy, "event": "LEGACY_SIGNAL", "signal_strength": 0.0, "payload": json.loads(signals[0]["payload_json"])}

    def recent_signal_alerts(self, limit: int = 10, coin: str | None = None) -> list[dict[str, Any]]:
        if coin:
            return self.storage.query(
                "SELECT * FROM signal_alerts WHERE coin=? ORDER BY timestamp DESC,id DESC LIMIT ?", (coin, limit)
            )
        return self.storage.query("SELECT * FROM signal_alerts ORDER BY timestamp DESC,id DESC LIMIT ?", (limit,))

    def pending_signal_outcomes(self, now_ms: int, limit: int = 500) -> list[dict[str, Any]]:
        return self.storage.query(
            """SELECT s.* FROM signals s LEFT JOIN signal_outcomes o ON o.signal_id=s.id
            WHERE o.signal_id IS NULL AND s.timestamp<=?
            ORDER BY s.timestamp DESC LIMIT ?""",
            (now_ms - 900_000, limit),
        )

    def context_at_or_after(self, coin: str, timestamp: int, latest_timestamp: int) -> dict[str, Any] | None:
        rows = self.storage.query(
            """SELECT timestamp,mark FROM asset_contexts
            WHERE coin=? AND timestamp>=? AND timestamp<=? ORDER BY timestamp ASC LIMIT 1""",
            (coin, timestamp, latest_timestamp),
        )
        return rows[0] if rows else None

    def save_signal_outcome(
        self,
        signal_id: int,
        coin: str,
        horizon: str,
        due_timestamp: int,
        evaluated_at: int,
        outcome_price: float,
        forward_return: float,
        actual_label: str,
        success: bool,
    ) -> None:
        self.storage.execute(
            "INSERT OR IGNORE INTO signal_outcomes VALUES (?,?,?,?,?,?,?,?,?)",
            (signal_id, coin, horizon, due_timestamp, evaluated_at, outcome_price, forward_return, actual_label, int(success)),
        )

    def evaluated_signal_rows(self, coin: str | None = None, limit: int = 5000) -> list[dict[str, Any]]:
        if coin:
            return self.storage.query(
                """SELECT o.*,s.payload_json,s.primary_horizon,s.timestamp FROM signal_outcomes o
                JOIN signals s ON s.id=o.signal_id WHERE o.coin=? ORDER BY o.due_timestamp DESC LIMIT ?""",
                (coin, limit),
            )
        return self.storage.query(
            """SELECT o.*,s.payload_json,s.primary_horizon,s.timestamp FROM signal_outcomes o
            JOIN signals s ON s.id=o.signal_id ORDER BY o.due_timestamp DESC LIMIT ?""",
            (limit,),
        )

    def technical_state(self, name: str) -> dict[str, Any] | None:
        rows = self.storage.query("SELECT * FROM technical_state WHERE name=?", (name,))
        return rows[0] if rows else None

    def save_technical_state(
        self, name: str, state: str, changed_at: int, last_notified: int | None, details: str | None
    ) -> None:
        self.storage.execute(
            """INSERT INTO technical_state(name,state,changed_at,last_notified,details) VALUES (?,?,?,?,?)
            ON CONFLICT(name) DO UPDATE SET state=excluded.state,changed_at=excluded.changed_at,
            last_notified=excluded.last_notified,details=excluded.details""",
            (name, state, changed_at, last_notified, details),
        )

    def save_analysis_observation(self, row: dict[str, Any]) -> bool:
        if row.get("universe_snapshot"):
            self.save_analysis_universe_snapshot(row["universe_snapshot"])
        normalized = dict(row)
        normalized["cost_policy_json"] = self.storage.json(row.get("cost_policy", {}))
        columns = (
            "observation_id", "instrument_id", "provider_id", "venue", "market_type", "base", "quote",
            "venue_symbol", "canonical_asset_id", "chain", "contract_address", "observed_at_ms", "feature_version", "feature_config_hash",
            "price_decimals", "size_decimals", "universe_snapshot_id",
            "reference_price", "reference_spread_bps", "reference_received_at_ms", "reference_exchange_timestamp_ms",
            "reference_time_semantics", "baseline_direction", "universe_member", "universe_bucket", "universe_score",
            "universe_reasons_json", "features_json", "quality_json", "source_lineage_json", "context_json", "cost_policy_json", "recorded_at_ms",
        )
        values = [normalized.get(key) for key in columns]
        # Commit a snapshot and its three pending horizons atomically. Otherwise a
        # process crash could leave an immutable snapshot permanently without labels.
        connection = self.storage._connection
        with self.storage._lock:
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    f"INSERT OR IGNORE INTO analysis_observations({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                    values,
                )
                observation_inserted = bool(cursor.rowcount)
                exists = connection.execute(
                    "SELECT 1 FROM analysis_observations WHERE observation_id=?", (row["observation_id"],)
                ).fetchone()
                if exists is None:
                    nullable = {"chain", "contract_address", "universe_snapshot_id", "reference_price",
                        "reference_spread_bps", "reference_received_at_ms", "reference_exchange_timestamp_ms",
                        "universe_bucket", "universe_score"}
                    missing = [key for key in columns if key not in nullable and normalized.get(key) is None]
                    raise ValueError(f"immutable observation insert was ignored; missing={missing}")
                cursor = connection.executemany(
                    "INSERT OR IGNORE INTO analysis_outcomes(observation_id,horizon,target_at_ms,deadline_at_ms,status,baseline_direction,cost_assumptions_json,updated_at_ms) VALUES (?,?,?,?, 'PENDING',?,?,?)",
                    [(row["observation_id"], horizon, int(row["observed_at_ms"])+delta,
                      int(row["observed_at_ms"])+delta+max(5_000,min(300_000,int(row.get("cost_policy", {}).get("outcome_max_latency_ms",60_000)))),
                      int(row["baseline_direction"]),
                      self.storage.json(row.get("cost_policy", {})), int(row["recorded_at_ms"]))
                     for horizon, delta in (("15m", 900_000), ("1h", 3_600_000), ("4h", 14_400_000))],
                )
                connection.commit()
                return observation_inserted
            except Exception:
                connection.rollback()
                raise

    def save_analysis_universe_snapshot(self, row: dict[str, Any]) -> bool:
        cursor = self.storage.execute(
            """INSERT OR IGNORE INTO analysis_universe_snapshots(
            snapshot_id,observed_at_ms,cadence_bucket,universe_hash,candidate_count,deep_count,
            members_json,selection_lineage_json,recorded_at_ms) VALUES (?,?,?,?,?,?,?,?,?)""",
            (row["snapshot_id"], int(row["observed_at_ms"]), int(row["cadence_bucket"]), row["universe_hash"],
             int(row["candidate_count"]), int(row["deep_count"]), self.storage.json(row["members"]),
             self.storage.json(row.get("selection_lineage", {})), int(row["recorded_at_ms"])),
        )
        return bool(cursor.rowcount)

    def due_analysis_outcomes(self, now_ms: int, limit: int = 5) -> list[dict[str, Any]]:
        return self.storage.query(
            """SELECT o.*,a.instrument_id,a.provider_id,a.venue,a.market_type,a.base,a.quote,a.venue_symbol,
            a.reference_price,a.reference_spread_bps,a.observed_at_ms,a.canonical_asset_id,a.chain,a.contract_address,
            a.price_decimals,a.size_decimals,a.feature_version,a.feature_config_hash
            FROM analysis_outcomes o JOIN analysis_observations a USING(observation_id)
            WHERE o.status='PENDING' AND o.target_at_ms<=? AND o.deadline_at_ms>=?
            ORDER BY o.deadline_at_ms,o.target_at_ms LIMIT ?""",
            (int(now_ms), int(now_ms), max(1, min(2_000, int(limit)))),
        )

    def expired_analysis_outcomes(self, now_ms: int, limit: int = 2_000) -> list[dict[str, Any]]:
        return self.storage.query(
            """SELECT observation_id,horizon FROM analysis_outcomes
            WHERE status='PENDING' AND deadline_at_ms<? ORDER BY deadline_at_ms LIMIT ?""",
            (int(now_ms), max(1, min(10_000, int(limit)))),
        )

    def finish_analysis_outcome(self, observation_id: str, horizon: str, values: dict[str, Any]) -> bool:
        return bool(self.finish_analysis_outcomes([(observation_id, horizon, values)]))

    def finish_analysis_outcomes(self, items: Iterable[tuple[str, str, dict[str, Any]]]) -> int:
        rows = []
        for observation_id, horizon, values in items:
            if values.get("status") not in {"OBSERVED", "UNAVAILABLE"}:
                raise ValueError("analysis outcome transition must be terminal")
            rows.append((values["status"], values.get("reason"), values.get("target_mid"),
                values.get("target_spread_bps"), values.get("target_exchange_timestamp_ms"),
                values.get("target_received_at_ms"), values.get("target_time_semantics"),
                values.get("gross_reference_return"), values.get("long_reference_net_return"),
                values.get("costs_adjusted_observational_return"), values.get("baseline_gross_return"),
                values.get("baseline_costs_adjusted_return"), self.storage.json(values.get("observed_costs", {})),
                int(values["updated_at_ms"]), observation_id, horizon))
        if not rows:
            return 0
        connection = self.storage._connection
        with self.storage._lock:
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.executemany(
                    """UPDATE analysis_outcomes SET status=?,reason=?,target_mid=?,target_spread_bps=?,
                    target_exchange_timestamp_ms=?,target_received_at_ms=?,target_time_semantics=?,gross_reference_return=?,
                    long_reference_net_return=?,costs_adjusted_observational_return=?,baseline_gross_return=?,baseline_costs_adjusted_return=?,
                    observed_costs_json=?,updated_at_ms=? WHERE observation_id=? AND horizon=? AND status='PENDING'""",
                    rows,
                )
                changed = cursor.rowcount
                connection.commit()
                return int(changed)
            except Exception:
                connection.rollback()
                raise

    def analysis_performance(self, now_ms: int) -> dict[str, Any]:
        rows = self.storage.query(
            """SELECT a.feature_version,a.feature_config_hash,o.cost_assumptions_json,
            horizon,status,COUNT(*) AS n,AVG(gross_reference_return) AS gross,
            AVG(long_reference_net_return) AS long_reference_net,
            AVG(baseline_gross_return) AS baseline_gross,
            AVG(baseline_costs_adjusted_return) AS baseline_net,
            COUNT(gross_reference_return) AS gross_n,COUNT(long_reference_net_return) AS long_reference_net_n,
            SUM(CASE WHEN o.status='OBSERVED' AND o.baseline_direction<>0 THEN 1 ELSE 0 END) AS baseline_position_n,
            SUM(CASE WHEN o.status='OBSERVED' AND o.baseline_direction=0 THEN 1 ELSE 0 END) AS baseline_no_position_n,
            SUM(CASE WHEN o.status='OBSERVED' AND o.target_mid=a.reference_price THEN 1 ELSE 0 END) AS zero_return_n,
            SUM(CASE WHEN reason='instrument_inactive_or_delisted' THEN 1 ELSE 0 END) AS delisted
            FROM analysis_outcomes o JOIN analysis_observations a USING(observation_id)
            GROUP BY a.feature_version,a.feature_config_hash,o.cost_assumptions_json,horizon,status
            ORDER BY horizon,feature_version,feature_config_hash,status"""
        )
        totals = self.storage.query("""SELECT COUNT(DISTINCT instrument_id) AS markets,COUNT(*) AS observations,
          MAX(observed_at_ms) AS last_observation FROM analysis_observations""")[0]
        due = self.storage.query("SELECT COUNT(*) AS n FROM analysis_outcomes WHERE status='PENDING' AND target_at_ms<=? AND deadline_at_ms>=?", (now_ms, now_ms))[0]["n"]
        expired_pending = self.storage.query("SELECT COUNT(*) AS n FROM analysis_outcomes WHERE status='PENDING' AND deadline_at_ms<?", (now_ms,))[0]["n"]
        cohorts = self.storage.query(
            """SELECT o.horizon,a.feature_version,a.feature_config_hash,o.cost_assumptions_json,
            COUNT(*) AS denominator,
            SUM(CASE WHEN o.status='OBSERVED' THEN 1 ELSE 0 END) AS observed,
            SUM(CASE WHEN o.status='PENDING' THEN 1 ELSE 0 END) AS pending,
            SUM(CASE WHEN o.status='UNAVAILABLE' THEN 1 ELSE 0 END) AS unavailable,
            SUM(CASE WHEN a.reference_price IS NULL THEN 1 ELSE 0 END) AS missing_reference,
            SUM(CASE WHEN o.reason='instrument_inactive_or_delisted' THEN 1 ELSE 0 END) AS delisted,
            SUM(CASE WHEN a.universe_member=1 THEN 1 ELSE 0 END) AS deep_universe,
            SUM(CASE WHEN a.universe_member=0 THEN 1 ELSE 0 END) AS outside_universe,
            SUM(CASE WHEN o.status='OBSERVED' AND a.baseline_direction<>0 THEN 1 ELSE 0 END) AS baseline_position_n,
            SUM(CASE WHEN o.status='OBSERVED' AND a.baseline_direction=0 THEN 1 ELSE 0 END) AS baseline_no_position_n,
            SUM(CASE WHEN o.status='OBSERVED' AND o.target_mid=a.reference_price THEN 1 ELSE 0 END) AS zero_return_n,
            COUNT(o.gross_reference_return) AS gross_metric_n,COUNT(o.long_reference_net_return) AS long_net_metric_n
            FROM analysis_outcomes o JOIN analysis_observations a USING(observation_id)
            GROUP BY o.horizon,a.feature_version,a.feature_config_hash,o.cost_assumptions_json
            ORDER BY o.horizon,a.feature_version,a.feature_config_hash"""
        )
        universe = self.storage.query(
            """SELECT snapshot_id,observed_at_ms,candidate_count,deep_count,universe_hash
            FROM analysis_universe_snapshots ORDER BY observed_at_ms DESC LIMIT 1"""
        )
        return {"markets_observed": totals["markets"], "observations": totals["observations"],
                "last_observation_at_ms": totals["last_observation"], "due_pending": due,
                "expired_pending": expired_pending,
                "by_horizon_status": rows, "cohorts": cohorts,
                "latest_universe_snapshot": universe[0] if universe else None,
                "limitations": ["Observational reference returns are not executed trades or PnL.", "Real performance remains NOT_EVALUABLE until adequate causal samples accrue."]}
