"""Causal, heuristic-only multi-asset shadow plans; never an execution client."""
from __future__ import annotations

from dataclasses import dataclass, asdict
import hashlib
import json
import math
import time
from typing import Any


@dataclass(frozen=True)
class ShadowPlan:
    plan_id: str
    variant: str
    instrument_id: str
    direction: str
    status: str
    observed_at_ms: int
    entry_reference: float | None
    stop: float | None
    target: float | None
    expires_at_ms: int
    max_hold_until_ms: int
    notional: float
    reasons: tuple[str, ...]
    score: int
    semantics: str = "heuristic setup score; not probability or expected return"
    provider: str = ""
    venue: str = ""
    quote: str = ""
    signal_anchor_ms: int = 0
    fee_bps_per_side: float = 4.0
    slippage_bps_per_side: float = 2.0
    holding_ms: int = 3600000
    bbo_received_at_ms: int = 0
    bbo_exchange_timestamp_ms: int | None = None
    bbo_exchange_time_kind: str = ""
    initial_equity: float = 10000.0
    max_total_risk_fraction: float = 0.01
    policy_id: str = ""
    entry_policy: str = "next_fresh_ask_within_ttl"
    max_bbo_age_ms: int = 30000
    max_entry_spread_bps: float = 30.0
    cooldown_ms: int = 900000


class StrategyShadow:
    """Two isolated virtual variants using closed candles and exact venue IDs."""
    def __init__(self, storage: Any, settings: dict[str, Any] | None = None):
        self.storage = storage
        self.cfg = dict(settings or {})
        self.ttl_ms = int(float(self.cfg.get("entry_ttl_seconds", 60)) * 1000)
        self.cooldown_ms = int(float(self.cfg.get("cooldown_seconds", 900)) * 1000)
        self.risk_fraction = min(.0025, max(0.0, float(self.cfg.get("risk_fraction", .001))))
        self.initial_equity = max(0.0, float(self.cfg.get("initial_equity", 10000)))
        self.max_notional = max(0.0, float(self.cfg.get("max_notional", 250)))
        self.max_total_risk_fraction = min(.05, max(0.0, float(self.cfg.get("max_total_risk_fraction", .01))))
        self.fee_bps = max(0.0, float(self.cfg.get("fee_bps_per_side", 4)))
        self.slip_bps = max(0.0, float(self.cfg.get("slippage_bps_per_side", 2)))
        self.holding_ms = int(max(1.0, float(self.cfg.get("maximum_holding_seconds", 3600))) * 1000)
        self.max_bbo_age_ms = int(max(0.0, float(self.cfg.get("max_bbo_age_seconds", 30))) * 1000)
        self.max_entry_spread_bps = max(0.0, float(self.cfg.get("max_entry_spread_bps", 30)))
        self.allowed_quotes = frozenset(self.cfg.get("allowed_quotes", ("USD", "USDT")))
        self.policy_id = hashlib.sha256(json.dumps({"version": "shadow-v1", "ttl_ms": self.ttl_ms,
            "cooldown_ms": self.cooldown_ms, "holding_ms": self.holding_ms,
            "risk_fraction": self.risk_fraction, "initial_equity": self.initial_equity,
            "max_notional": self.max_notional, "max_total_risk_fraction": self.max_total_risk_fraction,
            "fee_bps": self.fee_bps, "slip_bps": self.slip_bps,
            "max_entry_spread_bps": self.max_entry_spread_bps,
            "max_bbo_age_ms": self.max_bbo_age_ms,
            "allowed_quotes": sorted(self.allowed_quotes)}, sort_keys=True).encode()).hexdigest()
        storage.execute("""CREATE TABLE IF NOT EXISTS strategy_shadow_plans(
          plan_id TEXT PRIMARY KEY, variant TEXT NOT NULL, instrument_id TEXT NOT NULL,
          observed_at_ms INTEGER NOT NULL, payload_json TEXT NOT NULL,
          UNIQUE(variant,instrument_id,observed_at_ms))""")
        storage.execute("CREATE INDEX IF NOT EXISTS strategy_shadow_plans_instrument ON strategy_shadow_plans(instrument_id,observed_at_ms)")
        storage.execute("""CREATE TRIGGER IF NOT EXISTS strategy_shadow_immutable_update
          BEFORE UPDATE ON strategy_shadow_plans BEGIN SELECT RAISE(ABORT,'immutable'); END""")
        storage.execute("""CREATE TRIGGER IF NOT EXISTS strategy_shadow_immutable_delete
          BEFORE DELETE ON strategy_shadow_plans BEGIN SELECT RAISE(ABORT,'immutable'); END""")
        storage.execute("""CREATE TABLE IF NOT EXISTS strategy_shadow_events(
          event_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, variant TEXT NOT NULL,
          instrument_id TEXT NOT NULL, event_at_ms INTEGER NOT NULL, event_type TEXT NOT NULL,
          payload_json TEXT NOT NULL, FOREIGN KEY(plan_id) REFERENCES strategy_shadow_plans(plan_id))""")
        storage.execute("CREATE INDEX IF NOT EXISTS strategy_shadow_close_lookup ON strategy_shadow_events(event_type,variant,plan_id)")
        for op in ("UPDATE", "DELETE"):
            storage.execute(f"CREATE TRIGGER IF NOT EXISTS strategy_shadow_events_no_{op.lower()} BEFORE {op} ON strategy_shadow_events BEGIN SELECT RAISE(ABORT,'immutable'); END")
        # Mutable query projection; immutable plans/events remain the authority.
        storage.execute("""CREATE TABLE IF NOT EXISTS strategy_shadow_state(
          plan_id TEXT PRIMARY KEY, variant TEXT NOT NULL, instrument_id TEXT NOT NULL,
          state TEXT NOT NULL, event_at_ms INTEGER NOT NULL)""")
        storage.execute("CREATE INDEX IF NOT EXISTS strategy_shadow_state_lookup ON strategy_shadow_state(state,instrument_id,variant)")
        storage.execute("""CREATE TRIGGER IF NOT EXISTS strategy_shadow_plan_state AFTER INSERT ON strategy_shadow_plans
          BEGIN INSERT OR IGNORE INTO strategy_shadow_state VALUES(
            NEW.plan_id,NEW.variant,NEW.instrument_id,
            CASE WHEN json_extract(NEW.payload_json,'$.status')='PROPOSED' THEN 'PENDING' ELSE 'NO_TRADE' END,
            NEW.observed_at_ms); END""")
        storage.execute("""CREATE TRIGGER IF NOT EXISTS strategy_shadow_event_state AFTER INSERT ON strategy_shadow_events
          BEGIN UPDATE strategy_shadow_state SET state=CASE NEW.event_type
            WHEN 'FILLED' THEN 'OPEN' WHEN 'CLOSED' THEN 'CLOSED'
            WHEN 'EXPIRED' THEN 'EXPIRED' WHEN 'REJECTED' THEN 'REJECTED'
            ELSE state END, event_at_ms=NEW.event_at_ms WHERE plan_id=NEW.plan_id; END""")
        storage.execute("""INSERT OR IGNORE INTO strategy_shadow_state
          SELECT plan_id,variant,instrument_id,
            CASE WHEN json_extract(payload_json,'$.status')='PROPOSED' THEN 'PENDING' ELSE 'NO_TRADE' END,
            observed_at_ms FROM strategy_shadow_plans""")
        storage.execute("""UPDATE strategy_shadow_state SET
          state=COALESCE((SELECT CASE event_type WHEN 'FILLED' THEN 'OPEN' WHEN 'CLOSED' THEN 'CLOSED'
            WHEN 'EXPIRED' THEN 'EXPIRED' WHEN 'REJECTED' THEN 'REJECTED' END
            FROM strategy_shadow_events e WHERE e.plan_id=strategy_shadow_state.plan_id
            AND e.event_type IN ('FILLED','CLOSED','EXPIRED','REJECTED')
            ORDER BY event_at_ms DESC,event_id DESC LIMIT 1),state)
            WHERE state IN ('PENDING','OPEN')""")

    def evaluate(self, card: dict[str, Any]) -> list[ShadowPlan]:
        iid = str(card.get("instrument_id", "")); now = int(card.get("observed_at_ms", 0))
        candles = card.get("candles") or {}; bbo = card.get("bbo") or {}
        quality = card.get("candle_quality") or {}
        anchor = int(candles.get("1h", {}).get("latest_exchange_time_ms") or 0)
        bid = self._positive(bbo.get("bid")); ask = self._positive(bbo.get("ask"))
        received = int(bbo.get("received_at_ms") or 0)
        exchange_kind = str(bbo.get("exchange_time_kind") or "")
        exchange_ms = int(bbo.get("exchange_timestamp_ms") or 0)
        bbo_ok = bool(iid and now > 0 and 0 < bid <= ask and 0 < received <= now
            and now - received <= self.max_bbo_age_ms
            and (ask-bid)/((ask+bid)/2)*10000 <= self.max_entry_spread_bps
            and bbo.get("source_provider") == card.get("provider")
            and bbo.get("source_venue") == card.get("venue")
            and (exchange_kind == "RECEIVE_TIME_FALLBACK" or
                 (exchange_kind == "EXCHANGE_EVENT" and 0 < exchange_ms <= now
                  and now - exchange_ms <= self.max_bbo_age_ms)))
        plans: list[ShadowPlan] = []
        for variant, intervals in (("baseline", ("1h",)), ("disciplined", ("15m", "1h", "4h"))):
            # A closed 1h bar can produce at most one decision per variant.
            decision_bar = anchor if anchor > 0 else now // 3_600_000 * 3_600_000
            pid = hashlib.sha256(f"shadow-v1|{variant}|{iid}|{decision_bar}".encode()).hexdigest()
            existing = self.storage.query("SELECT payload_json FROM strategy_shadow_plans WHERE plan_id=?", (pid,))
            if existing:
                saved = json.loads(existing[0]["payload_json"])
                saved["reasons"] = tuple(saved["reasons"])
                plans.append(ShadowPlan(**saved))
                continue
            reasons: list[str] = []
            directions = [candles.get(tf, {}).get("trend") for tf in intervals]
            trend = directions[0] if directions and directions[0] in ("UP", "DOWN") else None
            direction = "LONG" if trend == "UP" else "NO_TRADE"
            if trend == "DOWN": reasons.append("spot shadow supports long-only setups")
            if trend is None or any(value != trend for value in directions):
                direction = "NO_TRADE"; reasons.append("timeframe disagreement or missing interval")
            causal_intervals = all(quality.get(tf, {}).get("status") == "FRESH"
                and 0 < int(quality.get(tf, {}).get("received_at_ms") or 0) <= now
                and 0 < int(candles.get(tf, {}).get("latest_exchange_time_ms") or 0) <= now
                and candles.get(tf, {}).get("source_provider") == card.get("provider")
                and candles.get(tf, {}).get("source_venue") == card.get("venue")
                for tf in intervals)
            if anchor <= 0 or anchor > now or not causal_intervals:
                direction = "NO_TRADE"; reasons.append("closed causal candle quality unavailable")
            if card.get("market_type") != "spot":
                direction = "NO_TRADE"; reasons.append("unsupported market type: funding/accounting incomplete")
            if card.get("quote") not in self.allowed_quotes:
                direction = "NO_TRADE"; reasons.append("unsupported quote currency")
            if not bbo_ok:
                direction = "NO_TRADE"; reasons.append("exact-identity fresh BBO unavailable")
            mid = (bid + ask) / 2 if bbo_ok else 0
            vol = self._positive(candles.get("1h", {}).get("realized_return_volatility_pct_per_bar")) / 100
            stop_pct = min(.05, max(.005, 2 * vol)) if vol > 0 else 0
            risk = self.initial_equity * self.risk_fraction
            entry_half_spread = (ask-mid)/mid if mid > 0 else 0
            modeled_risk_fraction = stop_pct + entry_half_spread + 2*(self.fee_bps+self.slip_bps)/10000
            notional = min(self.max_notional, risk / modeled_risk_fraction) if stop_pct else 0
            if notional <= 0: direction = "NO_TRADE"; reasons.append("risk size unavailable")
            if direction == "LONG" and self._has_active_plan(variant, iid, anchor, now):
                direction = "NO_TRADE"; reasons.append("active plan, closed-bar rearm, or cooldown")
            score = 0 if direction == "NO_TRADE" else min(100, 50 + 10 * len(intervals))
            stop = mid * (1-stop_pct) if direction == "LONG" else None
            target = mid * (1+2*stop_pct) if direction == "LONG" else None
            plan = ShadowPlan(pid, variant, iid, direction, "PROPOSED" if direction != "NO_TRADE" else "NO_TRADE",
                              now, mid or None, stop, target, now+self.ttl_ms,
                              now+self.ttl_ms+self.holding_ms,
                              notional if direction != "NO_TRADE" else 0,
                              tuple(reasons), score, provider=str(card.get("provider", "")), venue=str(card.get("venue", "")),
                              quote=str(card.get("quote", "")), signal_anchor_ms=anchor,
                              fee_bps_per_side=self.fee_bps, slippage_bps_per_side=self.slip_bps,
                              holding_ms=self.holding_ms, bbo_received_at_ms=received,
                              bbo_exchange_timestamp_ms=exchange_ms if exchange_kind == "EXCHANGE_EVENT" else None,
                              bbo_exchange_time_kind=exchange_kind, initial_equity=self.initial_equity,
                              max_total_risk_fraction=self.max_total_risk_fraction, policy_id=self.policy_id,
                              max_bbo_age_ms=self.max_bbo_age_ms, max_entry_spread_bps=self.max_entry_spread_bps,
                              cooldown_ms=self.cooldown_ms)
            payload = json.dumps(asdict(plan), sort_keys=True)
            self.storage.execute("INSERT OR IGNORE INTO strategy_shadow_plans VALUES(?,?,?,?,?)",
                                 (pid, variant, iid, now, payload))
            plans.append(plan)
        return plans

    @staticmethod
    def _positive(value: Any) -> float:
        try:
            number = float(value)
            return number if math.isfinite(number) and number > 0 else 0.0
        except (TypeError, ValueError, OverflowError):
            return 0.0

    def _has_active_plan(self, variant: str, iid: str, anchor: int, now: int) -> bool:
        if any(x["plan"]["variant"] == variant for x in self.open_positions(iid)):
            return True
        rows = self.storage.query("SELECT payload_json FROM strategy_shadow_plans WHERE variant=? AND instrument_id=? ORDER BY observed_at_ms DESC LIMIT 30", (variant, iid))
        for row in rows:
            previous = json.loads(row["payload_json"])
            if previous["status"] != "PROPOSED":
                continue
            events = self._events(previous["plan_id"])
            end = next((e for e in reversed(events) if e["type"] in {"CLOSED", "EXPIRED", "REJECTED"}), None)
            if end is None and (now < previous["expires_at_ms"] or any(e["type"] == "FILLED" for e in events)):
                return True
            if variant == "disciplined" and end is not None:
                if anchor <= previous.get("signal_anchor_ms", 0) or now < end["at"] + int(previous.get("cooldown_ms", 900000)):
                    return True
                break
        return False

    def _events(self, plan_id: str) -> list[dict[str, Any]]:
        return [json.loads(r["payload_json"]) for r in self.storage.query(
            "SELECT payload_json FROM strategy_shadow_events WHERE plan_id=? ORDER BY event_at_ms,event_id", (plan_id,))]

    def monitored_instrument_ids(self, now_ms: int, state: str | None = None) -> list[str]:
        """Return instruments needing an entry, expiry, or exit observation."""
        if state not in (None, "PENDING", "OPEN"):
            raise ValueError("unsupported shadow state")
        states = (state,) if state else ("PENDING", "OPEN")
        return [row["instrument_id"] for row in self.storage.query(
            f"SELECT DISTINCT instrument_id FROM strategy_shadow_state WHERE state IN ({','.join('?' for _ in states)}) ORDER BY instrument_id",
            states)]

    def expire_pending(self, now_ms: int) -> int:
        rows = self.storage.query("""SELECT p.payload_json FROM strategy_shadow_plans p
            JOIN strategy_shadow_state s ON s.plan_id=p.plan_id
            WHERE s.state='PENDING' AND json_extract(p.payload_json,'$.expires_at_ms')<=?""", (now_ms,))
        for row in rows:
            self._event(json.loads(row["payload_json"]), "EXPIRED", now_ms, {"reason": "entry_ttl"})
        return len(rows)

    def _event(self, plan: dict[str, Any], kind: str, at: int, payload: dict[str, Any]) -> None:
        raw=json.dumps({"type":kind,"at":at,**payload},sort_keys=True)
        eid=hashlib.sha256(f"{plan['plan_id']}|{kind}|{at}".encode()).hexdigest()
        self.storage.execute("INSERT OR IGNORE INTO strategy_shadow_events VALUES(?,?,?,?,?,?,?)",
            (eid,plan["plan_id"],plan["variant"],plan["instrument_id"],at,kind,raw))

    def advance(self, quote: dict[str, Any], now_ms: int) -> dict[str, Any]:
        """Advance all persisted plans using a later, fresh exact-instrument BBO."""
        rows=self.storage.query("""SELECT p.payload_json FROM strategy_shadow_plans p
            JOIN strategy_shadow_state s ON s.plan_id=p.plan_id
            WHERE s.instrument_id=? AND s.state IN ('PENDING','OPEN') ORDER BY p.observed_at_ms""", (quote.get("instrument_id"),))
        changed=[]
        for row in rows:
            p=json.loads(row["payload_json"]); events=self._events(p["plan_id"])
            if p["status"] != "PROPOSED":
                continue
            terminal=any(e["type"] in {"EXPIRED","CLOSED","REJECTED"} for e in events)
            opened=next((e for e in events if e["type"]=="FILLED"),None)
            if terminal: continue
            exact=(quote.get("instrument_id")==p["instrument_id"] and quote.get("provider")==p["provider"]
                and quote.get("venue")==p["venue"] and quote.get("quote")==p.get("quote")
                and quote.get("market_type")=="spot")
            received=int(quote.get("received_at_ms",0) or 0)
            bid=self._positive(quote.get("bid")); ask=self._positive(quote.get("ask"))
            kind=quote.get("exchange_time_kind")
            exchange=int(quote.get("exchange_timestamp_ms") or 0)
            allowed_age=int(p.get("max_bbo_age_ms", 30000))
            event_ok=(kind == "RECEIVE_TIME_FALLBACK" or
                (kind == "EXCHANGE_EVENT" and 0 < exchange <= now_ms
                 and now_ms-exchange <= allowed_age))
            fresh=(exact and received>p["observed_at_ms"] and 0<=now_ms-received<=allowed_age
                and 0<bid<=ask and event_ok
                and (kind != "EXCHANGE_EVENT" or exchange > max(p["observed_at_ms"], p.get("bbo_exchange_timestamp_ms") or 0)))
            if not opened:
                if now_ms>=p["expires_at_ms"]:
                    self._event(p,"EXPIRED",now_ms,{"reason":"entry_ttl"}); changed.append(p["plan_id"]); continue
                if not fresh: continue
                if (ask-bid)/((ask+bid)/2)*10000 > float(p.get("max_entry_spread_bps",30)):
                    continue
                if any(x["plan"]["variant"]==p["variant"] for x in self.open_positions(p["instrument_id"])):
                    continue
                fill=ask*(1+float(p.get("slippage_bps_per_side", 2))/10000)
                units=p["notional"]/fill
                positions = [x for x in self.open_positions() if x["plan"]["variant"] == p["variant"]
                    and x["plan"].get("quote") == p.get("quote")]
                committed = sum(float(x["entry"]["units"])*float(x["entry"]["price"]) for x in positions)
                risk_used = sum(self._risk_at_stop(x["plan"], x["entry"]) for x in positions)
                candidate_entry = {"units": units, "price": fill,
                    "fees": p["notional"]*float(p.get("fee_bps_per_side", 4))/10000}
                new_risk = self._risk_at_stop(p, candidate_entry)
                equity = max(0.0, float(p.get("initial_equity", self.initial_equity)) +
                    self._realized_pnl(p["variant"], p.get("quote", "")))
                if (committed+p["notional"] > equity or
                    risk_used+new_risk > equity*float(p.get("max_total_risk_fraction", .01))):
                    self._event(p,"REJECTED",received,{"reason":"virtual_cash_or_risk_budget"})
                    changed.append(p["plan_id"])
                    continue
                fees=p["notional"]*float(p.get("fee_bps_per_side", 4))/10000
                self._event(p,"FILLED",received,{"price":fill,"fees":fees,"units":units,
                    "exchange_timestamp_ms":exchange if kind == "EXCHANGE_EVENT" else None,
                    "exchange_time_kind":kind})
                changed.append(p["plan_id"]); continue
            last_mark = next((event for event in reversed(events) if event["type"] == "MARK"), None)
            last_observed_at = last_mark["at"] if last_mark else opened["at"]
            last_exchange_at = (last_mark or opened).get("exchange_timestamp_ms") or 0
            if not fresh or received <= last_observed_at or (kind == "EXCHANGE_EVENT" and
                exchange <= max(opened["at"], last_exchange_at)):
                continue
            units=float(opened["units"]); entry=float(opened["price"]); exit_price=None; reason=None
            marked_exit=bid*(1-float(p.get("slippage_bps_per_side",2))/10000)
            marked_fee=marked_exit*units*float(p.get("fee_bps_per_side",4))/10000
            marked_net=(marked_exit-entry)*units-float(opened["fees"])-marked_fee
            self._event(p,"MARK",received,{"bid":bid,"net_pnl_if_closed":marked_net,
                "exchange_timestamp_ms":exchange if kind == "EXCHANGE_EVENT" else None})
            # Bid is the conservative executable side for a long spot position.
            if bid<=float(p["stop"]): exit_price=min(bid,float(p["stop"])); reason="STOP"
            elif bid>=float(p["target"]): exit_price=float(p["target"]); reason="TARGET"
            elif now_ms>=opened["at"]+int(p.get("holding_ms",p["max_hold_until_ms"]-p["observed_at_ms"])):
                exit_price=bid; reason="TIME"
            if exit_price is not None:
                slip=float(p.get("slippage_bps_per_side",2))/10000
                actual_exit=exit_price*(1-slip)
                fees=actual_exit*units*float(p.get("fee_bps_per_side",4))/10000
                net=(actual_exit-entry)*units-opened["fees"]-fees
                self._event(p,"CLOSED",received,{"reason":reason,"price":actual_exit,"fees":fees,
                    "gross_pnl":(exit_price-entry)*units,"net_pnl":net,"quote":p.get("quote", ""),
                    "exchange_timestamp_ms":exchange if kind == "EXCHANGE_EVENT" else None})
                changed.append(p["plan_id"])
        return {"advanced":changed,"open_positions":self.open_positions(quote.get("instrument_id"))}

    @staticmethod
    def _risk_at_stop(plan: dict[str, Any], entry: dict[str, Any]) -> float:
        units = float(entry["units"])
        stop = float(plan["stop"])
        close_cost = stop*units*(float(plan.get("fee_bps_per_side", 4)) +
            float(plan.get("slippage_bps_per_side", 2)))/10000
        return units*max(0.0, float(entry["price"])-stop) + close_cost + float(entry["fees"])

    def _realized_pnl(self, variant: str, quote: str) -> float:
        rows = self.storage.query("""SELECT COALESCE(SUM(json_extract(e.payload_json,'$.net_pnl')),0) AS pnl
            FROM strategy_shadow_events e JOIN strategy_shadow_plans p ON p.plan_id=e.plan_id
            WHERE e.event_type='CLOSED' AND e.variant=? AND json_extract(p.payload_json,'$.quote')=?""",
            (variant, quote))
        return float(rows[0]["pnl"] or 0)

    def open_positions(self, instrument_id: str | None = None) -> list[dict[str, Any]]:
        result=[]
        rows = self.storage.query("""SELECT p.payload_json FROM strategy_shadow_plans p
            JOIN strategy_shadow_state s ON s.plan_id=p.plan_id WHERE s.state='OPEN' AND s.instrument_id=?""",
            (instrument_id,)) if instrument_id else self.storage.query("""SELECT p.payload_json FROM strategy_shadow_plans p
            JOIN strategy_shadow_state s ON s.plan_id=p.plan_id WHERE s.state='OPEN'""")
        for row in rows:
            p=json.loads(row["payload_json"]); ev=self._events(p["plan_id"])
            if any(e["type"]=="FILLED" for e in ev) and not any(e["type"] in {"CLOSED","EXPIRED","REJECTED"} for e in ev):
                result.append({"plan":p,"entry":next(e for e in ev if e["type"]=="FILLED")})
        return result

    def summary(self, now_ms: int | None = None) -> dict[str, Any]:
        now_ms = int(time.time()*1000) if now_ms is None else now_ms
        result = {name: {"plans": 0, "no_trade": 0, "proposed": 0, "filled": 0,
            "pending": 0, "open": 0, "closed": 0, "expired": 0, "rejected": 0, "unknown_marks": 0,
            "by_quote": {}, "status": "NOT_EVALUABLE"} for name in ("baseline", "disciplined")}
        for row in self.storage.query("SELECT payload_json FROM strategy_shadow_plans ORDER BY observed_at_ms,plan_id"):
            p = json.loads(row["payload_json"])
            account = result[p["variant"]]
            account["plans"] += 1
            if p["status"] == "NO_TRADE":
                account["no_trade"] += 1
                continue
            account["proposed"] += 1
            ev = self._events(p["plan_id"])
            fill = next((e for e in ev if e["type"] == "FILLED"), None)
            close = next((e for e in ev if e["type"] == "CLOSED"), None)
            expired = next((e for e in ev if e["type"] == "EXPIRED"), None)
            rejected = next((e for e in ev if e["type"] == "REJECTED"), None)
            if expired: account["expired"] += 1
            if rejected: account["rejected"] += 1
            if not fill:
                if not expired and not rejected: account["pending"] += 1
                continue
            account["filled"] += 1
            quote = p.get("quote") or "UNKNOWN_QUOTE"
            metrics = account["by_quote"].setdefault(quote, {"closed": 0, "open": 0,
                "realized_net": 0.0, "marked_unrealized_net": 0.0, "wins": 0,
                "turnover_notional": 0.0, "fees": 0.0, "max_drawdown": 0.0,
                "mark_count": 0, "max_quote_gap_seconds": 0.0,
                "holding_seconds_total": 0.0, "mfe_total": 0.0, "mae_total": 0.0,
                "policy_ids": [], "_closed_pnl": []})
            if p.get("policy_id") and p["policy_id"] not in metrics["policy_ids"]:
                metrics["policy_ids"].append(p["policy_id"])
            entry_notional = float(fill["price"])*float(fill["units"])
            metrics["turnover_notional"] += entry_notional
            metrics["fees"] += float(fill["fees"])
            marks = [float(e["net_pnl_if_closed"]) for e in ev if e["type"] == "MARK"]
            mark_times = [int(e["at"]) for e in ev if e["type"] == "MARK"]
            metrics["mark_count"] += len(mark_times)
            observed_times = [int(fill["at"]), *mark_times]
            if close:
                observed_times.append(int(close["at"]))
            else:
                observed_times.append(max(int(now_ms), observed_times[-1]))
            metrics["max_quote_gap_seconds"] = max(metrics["max_quote_gap_seconds"],
                max((later - earlier) / 1000 for earlier, later in zip(observed_times, observed_times[1:])))
            if close:
                account["closed"] += 1
                metrics["closed"] += 1
                metrics["realized_net"] += float(close["net_pnl"])
                metrics["_closed_pnl"].append((close["at"], p["plan_id"], float(close["net_pnl"])))
                metrics["wins"] += float(close["net_pnl"]) > 0
                metrics["turnover_notional"] += float(close["price"])*float(fill["units"])
                metrics["fees"] += float(close["fees"])
                metrics["holding_seconds_total"] += max(0, close["at"]-fill["at"])/1000
                marks.append(float(close["net_pnl"]))
            else:
                account["open"] += 1
                metrics["open"] += 1
                last_mark = next((e for e in reversed(ev) if e["type"] == "MARK"), None)
                allowed_age = int(p.get("max_bbo_age_ms", 30000))
                if last_mark and 0 <= now_ms-last_mark["at"] <= allowed_age:
                    metrics["marked_unrealized_net"] += float(last_mark["net_pnl_if_closed"])
                else:
                    account["unknown_marks"] += 1
            if marks:
                metrics["mfe_total"] += max(0.0, max(marks))
                metrics["mae_total"] += min(0.0, min(marks))
        for account in result.values():
            for metrics in account["by_quote"].values():
                cumulative = peak = 0.0
                for _, _, net in sorted(metrics.pop("_closed_pnl")):
                    cumulative += net
                    peak = max(peak, cumulative)
                    metrics["max_drawdown"] = max(metrics["max_drawdown"], peak-cumulative)
        return result

    def latest_plans(self, instrument_id: str) -> list[dict[str, Any]]:
        rows = self.storage.query("SELECT payload_json FROM strategy_shadow_plans WHERE instrument_id=? ORDER BY observed_at_ms DESC LIMIT 2", (instrument_id,))
        return [{"plan": json.loads(row["payload_json"]), "events": self._events(json.loads(row["payload_json"])["plan_id"])} for row in rows]
