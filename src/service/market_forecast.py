"""Causal multi-market forecasts. Empirical estimates are explicitly uncalibrated.

Uses identical first-barrier labels for every horizon; no SOL/HYPE model is
silently transferred to another venue. Source data stays in the existing cache.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from src.features.technical import add_technical_features
from src.models.targets import triple_barrier_labels


HORIZONS = {"5m": 1, "15m": 3, "1h": 12}


class MarketForecastService:
    def __init__(self, analysis: Any, storage: Any, settings: dict | None = None, send_once=None):
        self.analysis, self.storage = analysis, storage
        self.cfg = settings or {}
        self.send_once = send_once
        self.last_error: str | None = None
        storage.execute("""CREATE TABLE IF NOT EXISTS market_forecasts_v1(
            forecast_id TEXT PRIMARY KEY, instrument_id TEXT NOT NULL,
            horizon TEXT NOT NULL, observed_at_ms INTEGER NOT NULL,
            due_at_ms INTEGER NOT NULL, payload_json TEXT NOT NULL)""")
        storage.execute("CREATE INDEX IF NOT EXISTS forecast_due_v1 ON market_forecasts_v1(due_at_ms)")
        storage.execute("""CREATE TABLE IF NOT EXISTS market_forecast_outcomes_v1(
            forecast_id TEXT PRIMARY KEY, status TEXT NOT NULL, payload_json TEXT NOT NULL)""")
        storage.execute("CREATE INDEX IF NOT EXISTS forecast_outcome_status_v1 ON market_forecast_outcomes_v1(status)")
        storage.execute("""CREATE TABLE IF NOT EXISTS market_forecast_pending_v1(
            forecast_id TEXT PRIMARY KEY, instrument_id TEXT NOT NULL, due_at_ms INTEGER NOT NULL)""")
        storage.execute("CREATE INDEX IF NOT EXISTS forecast_pending_due_v1 ON market_forecast_pending_v1(due_at_ms)")
        storage.execute("CREATE INDEX IF NOT EXISTS forecast_pending_instrument_v1 ON market_forecast_pending_v1(instrument_id,due_at_ms)")
        # Repair the mutable projection after any crash between evidence writes.
        storage.execute("DELETE FROM market_forecast_pending_v1 WHERE forecast_id IN (SELECT forecast_id FROM market_forecast_outcomes_v1)")
        storage.execute("""INSERT OR IGNORE INTO market_forecast_pending_v1
            SELECT f.forecast_id,f.instrument_id,f.due_at_ms FROM market_forecasts_v1 f
            LEFT JOIN market_forecast_outcomes_v1 o USING(forecast_id) WHERE o.forecast_id IS NULL""")
        storage.execute("""CREATE TABLE IF NOT EXISTS market_forecast_notifications_v1(
            notification_key TEXT PRIMARY KEY, attempted_at_ms INTEGER NOT NULL)""")
        for table in ("market_forecasts_v1", "market_forecast_outcomes_v1"):
            for operation in ("UPDATE", "DELETE"):
                storage.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'immutable'); END")
        self._cache: dict[tuple, dict] = {}

    def forecast(self, card: dict, horizon: str, now: int | None = None) -> dict:
        if horizon not in HORIZONS:
            raise ValueError("Supported horizons: 5m, 15m, 1h")
        now = int(now if now is not None else self.analysis._now_ms())
        iid = card["instrument_id"]
        result = {"instrument_id": iid, "base": card.get("base"), "venue": card.get("venue"),
            "market_type": card.get("market_type"), "quote": card.get("quote"),
            "horizon": horizon, "observed_at_ms": now, "probabilities": None,
            "status": "UNAVAILABLE", "direction": "WAIT", "eligible": False,
            "model_version": "empirical-barrier-neighbours-v1", "calibrated": False,
            "semantics": "Эмпирическая оценка первого достижения ATR-барьера; не калиброванная вероятность прибыли.",
            "reasons": [], "sample_count": 0}
        quality = (card.get("candle_quality") or {}).get("5m", {})
        result["history_count"] = int(quality.get("count") or 0)
        result["data_status"] = quality.get("status", "UNAVAILABLE")
        result["data_reason"] = quality.get("reason")
        if quality.get("status") not in {"FRESH", "FRESH_WITH_GAPS"}:
            result["reasons"] = ["Обновляем закрытые 5m свечи" if quality.get("reason") == "latest_closed_candle_interval_is_old" else "Нет непрерывной свежей истории закрытых 5m свечей"]
            return result
        source = (self.analysis.candle_history(iid, "5m") if hasattr(self.analysis, "candle_history") else
            [bar for (identity, interval, _), bar in self.analysis._candles.items() if identity == iid and interval == "5m"])
        bars = sorted((bar for bar in source if bar.closed
            and bar.close_time_ms < now and bar.metadata.received_at_ms <= now), key=lambda b: b.open_time_ms)
        if len(bars) < 160:
            result["reasons"] = [f"Накопление истории: {len(bars)}/160 закрытых свечей"]
            return result
        # Use only the latest contiguous suffix; never jump across a missing bar.
        start = len(bars)-1
        while start > 0 and bars[start].open_time_ms-bars[start-1].open_time_ms == 300_000:
            start -= 1
        bars = bars[start:]
        result["history_count"] = len(bars)
        if len(bars) < 160:
            result["reasons"] = ["Недостаточно непрерывной истории после пропуска"]
            return result
        key = (iid, horizon, bars[-1].open_time_ms)
        if key not in self._cache:
            frame = add_technical_features(pd.DataFrame([{
                "open": b.open, "high": b.high, "low": b.low, "close": b.close,
                "volume": b.volume, "open_time": b.open_time_ms} for b in bars]))
            steps = HORIZONS[horizon]
            labels = triple_barrier_labels(frame, steps, use_structure=False)
            features = frame[["return_1", "dist_ema_21", "rsi_14", "atr_pct", "relative_volume"]]
            # Both labels and all normalization observations precede the current
            # decision. Exclude one additional horizon as an outcome embargo.
            historical = features.iloc[:-(2*steps+1)].dropna()
            historical = historical.loc[labels.loc[historical.index].notna()]
            current = features.iloc[-1]
            if len(historical) < 60 or current.isna().any():
                result["reasons"] = ["Недостаточно прошлых состояний с завершённым исходом"]
                return result
            scale = historical.std(ddof=0).replace(0, 1)
            distances = (((historical-current)/scale)**2).mean(axis=1)
            nearest = distances.nsmallest(min(60, len(historical))).index
            counts = np.bincount(labels.loc[nearest].astype(int), minlength=3)
            # Dirichlet shrinkage; not advertised as calibrated model output.
            probabilities = (counts + np.array([3., 3., 6.]))/(len(nearest)+12)
            returns = (frame.close.shift(-steps)/frame.close-1).loc[nearest]
            self._cache[key] = {"probabilities": {k: float(p*100) for k,p in zip(("LONG","SHORT","WAIT"), probabilities)},
                "sample_count": len(nearest), "atr_pct": float(frame.atr_pct.iloc[-1]),
                "historical_mean_return": float(returns.mean()),
                "source_close": float(frame.close.iloc[-1]),
                "signal_anchor_ms": bars[-1].open_time_ms,
                "source_received_at_ms": bars[-1].metadata.received_at_ms}
            if len(self._cache) > 500:
                self._cache.pop(next(iter(self._cache)))
        result.update(self._cache[key])
        result["sparkline"] = [float(b.close) for b in bars[-36:]]
        p = result["probabilities"]
        direction = max(p, key=p.get)
        result.update(status="ESTIMATE", direction=direction)
        bbo = card.get("bbo") or {}
        received = int(bbo.get("received_at_ms") or 0)
        bid, ask = float(bbo.get("bid") or 0), float(bbo.get("ask") or 0)
        fresh = (0 < bid < ask and 0 < received <= now and now-received <= 30_000
            and bbo.get("source_provider") == card.get("provider") and bbo.get("source_venue") == card.get("venue"))
        if not fresh:
            result["reasons"].append("Нет свежей котировки точного инструмента")
        spread = float(bbo.get("spread_bps") or 0)
        costs = (2*float(self.cfg.get("fee_bps_per_side", 4))+2*float(self.cfg.get("slippage_bps_per_side", 2))+spread)/10_000
        net = result["historical_mean_return"]*(1 if direction=="LONG" else -1)-costs
        result.update(historical_directional_net_return=net, cost_fraction=costs, spread_bps=spread,
            rank_score=max(p["LONG"],p["SHORT"])-p["WAIT"])
        result["reference_mid"] = (bid+ask)/2 if fresh else None
        result["quote_received_at_ms"] = received if fresh else None
        if direction == "WAIT": result["reasons"].append("Нейтральный исход преобладает")
        if direction == "SHORT" and card.get("market_type") != "perpetual":
            result["reasons"].append("SHORT доступен только на фьючерсах")
        if max(p["LONG"],p["SHORT"]) < float(self.cfg.get("minimum_directional_percent", 60)):
            result["reasons"].append("Оценка направления ниже порога")
        if abs(p["LONG"]-p["SHORT"]) < 20: result["reasons"].append("Малое преимущество направления")
        if spread > 20: result["reasons"].append("Слишком широкий спред")
        if net <= 0: result["reasons"].append("Историческое среднее движение не покрывает издержки")
        if card.get("observation_status") != "OBSERVED": result["reasons"].append("Источник работает с ограничениями")
        if fresh and result.get("source_close") and abs((bid+ask)/2/result["source_close"]-1) > .75*result["atr_pct"]:
            result["reasons"].append("Цена уже ушла от состояния закрытой свечи")
        if fresh:
            plan_direction = 'LONG' if p['LONG'] >= p['SHORT'] else 'SHORT'
            entry = ask if plan_direction=="LONG" else bid
            distance = min(.20, max(.00125, 1.25*result["atr_pct"]))
            sign = 1 if plan_direction=="LONG" else -1
            result.update(entry_reference=entry, stop=entry*(1-sign*distance),
                target=entry*(1+sign*distance), expires_at_ms=min(now+60_000,received+30_000))
            result['trade_plan'] = {
                'direction': plan_direction, 'entry': entry,
                'stop_loss': result['stop'], 'take_profit': result['target'],
                'stop_distance_percent': distance*100, 'target_distance_percent': distance*100,
                'reward_risk': 1.0,
                'reward_risk_after_costs': max(0.0,(distance-costs)/(distance+costs)),
                'estimated_roundtrip_cost_percent': costs*100,
                'expires_at_ms': result['expires_at_ms'],
                'time_exit_at_ms': now+HORIZONS[horizon]*300_000,
                'quote_received_at_ms': received,
                'instrument_id': iid, 'quote': card.get('quote'),
                'basis': '1.25 ATR, matching the forecast barriers',
            }
        result["eligible"] = not result["reasons"]
        if result.get('trade_plan'):
            result['trade_plan']['status'] = 'SIGNAL' if result['eligible'] else 'CONDITIONAL'
        return result

    def forecasts(self, horizon: str = "15m") -> list[dict]:
        now = self.analysis._now_ms()
        return sorted([self.forecast(card,horizon,now) for card in self.analysis.markets(30)],
            key=lambda x: (-x.get("rank_score", -100), x["instrument_id"]))

    def _claim(self, key: str, now: int) -> bool:
        return bool(self.storage.execute("INSERT OR IGNORE INTO market_forecast_notifications_v1 VALUES(?,?)", (key,now)).rowcount)

    async def process(self, card: dict) -> None:
        now = self.analysis._now_ms()
        self.resolve(card, now)
        # One immutable snapshot per instrument/horizon/cadence. All assessments,
        # including rejected ones, remain available for forward evaluation.
        for horizon in HORIZONS:
            f = self.forecast(card,horizon,now)
            if f["probabilities"] is None: continue
            cadence = max(60_000, int(self.cfg.get("snapshot_cadence_seconds", 300))*1000)
            identity = hashlib.sha256(f"{f['model_version']}|{f['instrument_id']}|{horizon}|{now//cadence}".encode()).hexdigest()
            inserted = self.storage.execute("INSERT OR IGNORE INTO market_forecasts_v1 VALUES(?,?,?,?,?,?)",
                (identity,f["instrument_id"],horizon,now,now+HORIZONS[horizon]*300_000,json.dumps(f,allow_nan=False)))
            if inserted.rowcount:
                self.storage.execute("INSERT OR IGNORE INTO market_forecast_pending_v1 VALUES(?,?,?)",
                    (identity,f["instrument_id"],now+HORIZONS[horizon]*300_000))
            if not f["eligible"] or self.send_once is None: continue
            # Shared per-instrument cooldown across horizons; persisted at-most-once.
            recent = self.storage.query("SELECT 1 FROM market_forecast_notifications_v1 WHERE notification_key LIKE ? AND attempted_at_ms>? LIMIT 1",
                (f"signal:{f['instrument_id']}:%",now-900_000))
            count = self.storage.query("SELECT COUNT(*) n FROM market_forecast_notifications_v1 WHERE notification_key LIKE 'signal:%' AND attempted_at_ms>?",(now-3_600_000,))[0]["n"]
            if recent or count >= int(self.cfg.get("max_per_hour", 8)): continue
            key = f"signal:{f['instrument_id']}:{now//900_000}"
            if self._claim(key,now): await self.send_once(key,self.format(f),now)
        await self.digest(now)

    def resolve(self, card: dict, now: int) -> None:
        """Forward-price observations, explicitly distinct from trade execution.

        A late quote is never substituted for the requested horizon. Missing
        observations expire even if an instrument disappears from the watchlist.
        """
        self.storage.execute("""INSERT OR IGNORE INTO market_forecast_outcomes_v1
            SELECT f.forecast_id,'UNAVAILABLE','{"reason":"target_quote_window_missed"}'
            FROM market_forecast_pending_v1 f WHERE f.due_at_ms<? LIMIT 500""", (now-180_000,))
        self.storage.execute("""DELETE FROM market_forecast_pending_v1 WHERE forecast_id IN
            (SELECT p.forecast_id FROM market_forecast_pending_v1 p
            JOIN market_forecast_outcomes_v1 o USING(forecast_id) WHERE p.due_at_ms<? LIMIT 500)""",(now-180_000,))
        bbo=card.get("bbo") or {}
        received=int(bbo.get("received_at_ms") or 0)
        bid,ask=float(bbo.get("bid") or 0),float(bbo.get("ask") or 0)
        if not (0<bid<ask and 0<received<=now and now-received<=30_000): return
        rows=self.storage.query("""SELECT f.* FROM market_forecast_pending_v1 p
            JOIN market_forecasts_v1 f USING(forecast_id)
            WHERE p.instrument_id=? AND p.due_at_ms<=?
            AND p.due_at_ms>=? ORDER BY p.due_at_ms LIMIT 60""",
            (card["instrument_id"],received,received-180_000))
        for row in rows:
            forecast=json.loads(row["payload_json"])
            reference=forecast.get("reference_mid")
            outcome={"received_at_ms":received,"latency_ms":received-row["due_at_ms"],
                "semantics":"forward quote return; not executed PnL or TP-before-stop outcome"}
            if reference:
                gross=((bid+ask)/2)/reference-1
                outcome.update(gross_return=gross,
                    directional_reference_net=(gross*(1 if forecast["direction"]=="LONG" else -1)-forecast["cost_fraction"])
                    if forecast["direction"]!="WAIT" else None)
            else: outcome["reason"]="missing_fresh_entry_reference"
            self.storage.execute("INSERT OR IGNORE INTO market_forecast_outcomes_v1 VALUES(?,?,?)",
                (row["forecast_id"],"OBSERVED" if reference else "UNAVAILABLE",json.dumps(outcome)))
            self.storage.execute("DELETE FROM market_forecast_pending_v1 WHERE forecast_id=?",(row["forecast_id"],))

    def performance(self) -> dict:
        rows=self.storage.query("SELECT status,COUNT(*) n FROM market_forecast_outcomes_v1 GROUP BY status")
        total=self.storage.query("SELECT COUNT(*) n FROM market_forecasts_v1")[0]["n"]
        return {"status":"NOT_EVALUABLE", "calibrated":False, "snapshots":total,
            "outcomes":{r["status"]:r["n"] for r in rows},
            "semantics":"Forward quote observations; overlapping samples are not independent trades."}

    @staticmethod
    def format(f: dict) -> str:
        p=f["probabilities"]
        return (f"🧪 Сильный экспериментальный сетап · {f['instrument_id']} · {f['horizon']}\n"
            f"{f['direction']} · LONG {p['LONG']:.0f}% / SHORT {p['SHORT']:.0f}% / WAIT {p['WAIT']:.0f}%\n"
            f"Вход-ориентир {f['entry_reference']:.8g}; стоп-лосс {f['stop']:.8g}; тейк-профит {f['target']:.8g} {f.get('quote','')}.\n"
            f"Риск/прибыль 1:1 до расходов. Вход действителен до {datetime.fromtimestamp(f['expires_at_ms']/1000,timezone.utc):%H:%M:%S} UTC; горизонт {f['horizon']}.\n"
            f"Исторических аналогов: {f['sample_count']}; расходы {f['cost_fraction']*100:.3f}%.\n"
            "Оценка не калибрована; преимущество на будущих данных не подтверждено. TTL котировки до 30 секунд. Ордер не отправлен.")

    async def digest(self, now: int) -> None:
        if self.send_once is None: return
        key=f"digest:{now//3_600_000}"
        if not self._claim(key,now): return
        rows=self.forecasts("15m")
        lines=["📊 Часовой обзор рынка · эмпирические оценки, не проверенные вероятности"]
        # Symbol grouping is presentation only; exact IDs remain on every line.
        seen=set()
        for row in rows:
            if row["base"] in seen or row["probabilities"] is None: continue
            seen.add(row["base"]); p=row["probabilities"]
            lines.append(f"{row['instrument_id']}: L {p['LONG']:.0f}% / S {p['SHORT']:.0f}% / WAIT {p['WAIT']:.0f}% · "
                + ("кандидат" if row["eligible"] else "; ".join(row["reasons"][:2])))
            if row['eligible']:
                lines.append(f"Вход {row['entry_reference']:.8g} · SL {row['stop']:.8g} · TP {row['target']:.8g} {row['quote']}")
            if len(seen)>=5: break
        if not seen: lines.append("Кандидатов пока нет: история или котировки ещё собираются.")
        await self.send_once(key,"\n".join(lines)[:3900],now)
