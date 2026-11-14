from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Mapping

from .repository import PaperRepository, stable_hash


@dataclass(frozen=True)
class PaperPosition:
    decision_id: str
    direction: int
    quantity: float
    entry_price: float
    notional_usd: float
    estimated_exit_cost_bps: float


class ShadowPortfolio:
    """Append-only shadow ledger. It has no venue, order, wallet, or signer dependency."""

    def __init__(self, repository: PaperRepository, initial_equity_usd: float):
        self.repository = repository
        self.initial_equity_usd = float(initial_equity_usd)
        inserted = self.repository.append_ledger({
            "entry_id": stable_hash("shadow-account", "initial-cash"),
            "decision_id": None,
            "event_type": "ACCOUNT_OPEN",
            "occurred_at_ms": 0,
            "cash_delta_usd": self.initial_equity_usd,
            "metadata": {"mode": "SHADOW", "initial_equity_usd": self.initial_equity_usd},
        })
        if not inserted:
            account_rows = [
                row for row in self.repository.ledger() if row["event_type"] == "ACCOUNT_OPEN"
            ]
            persisted = float(json.loads(account_rows[0]["metadata_json"])["initial_equity_usd"])
            if abs(persisted - self.initial_equity_usd) > 1e-8:
                raise ValueError("configured initial paper equity differs from immutable account ledger")

    def positions(self) -> dict[str, PaperPosition]:
        state: dict[str, dict[str, float]] = {}
        for row in self.repository.ledger():
            decision_id = row.get("decision_id")
            if not decision_id or row["event_type"] not in {"SHADOW_OPEN", "SHADOW_CLOSE"}:
                continue
            item = state.setdefault(decision_id, {
                "quantity": 0.0, "entry_value": 0.0, "entry_quantity": 0.0,
                "direction": float(row["direction"]), "notional": 0.0,
                "exit_cost_bps": 0.0,
            })
            quantity = float(row["quantity_delta"])
            item["quantity"] += quantity
            if row["event_type"] == "SHADOW_OPEN":
                item["entry_value"] += abs(quantity) * float(row["price"])
                item["entry_quantity"] += abs(quantity)
                item["notional"] += float(row["notional_usd"])
                metadata = json.loads(row["metadata_json"])
                item["exit_cost_bps"] = float(metadata.get("estimated_exit_cost_bps", 0.0))
        result: dict[str, PaperPosition] = {}
        for decision_id, item in state.items():
            if abs(item["quantity"]) <= 1e-12:
                continue
            result[decision_id] = PaperPosition(
                decision_id, int(item["direction"]), item["quantity"],
                item["entry_value"] / item["entry_quantity"], item["notional"],
                item["exit_cost_bps"],
            )
        return result

    def open(
        self,
        decision_id: str,
        direction: str,
        notional_usd: float,
        price: float,
        occurred_at_ms: int,
        fee_bps: float,
        slippage_bps: float,
    ) -> bool:
        if direction not in {"LONG", "SHORT"}:
            raise ValueError("shadow positions require explicit LONG or SHORT direction")
        side = 1 if direction == "LONG" else -1
        quantity = side * notional_usd / price
        fees = notional_usd * fee_bps / 10_000
        slippage = notional_usd * slippage_bps / 10_000
        inserted = self.repository.append_ledger({
            "entry_id": stable_hash(decision_id, "SHADOW_OPEN"),
            "decision_id": decision_id,
            "event_type": "SHADOW_OPEN",
            "occurred_at_ms": occurred_at_ms,
            "direction": side,
            "quantity_delta": quantity,
            "price": price,
            "notional_usd": notional_usd,
            "cash_delta_usd": -(fees + slippage),
            "gross_pnl_usd": 0.0,
            "net_pnl_usd": -(fees + slippage),
            "fees_usd": fees,
            "slippage_usd": slippage,
            "metadata": {
                "mode": "SHADOW",
                "estimated_exit_cost_bps": fee_bps + slippage_bps,
            },
        })
        if inserted:
            self.repository.append_transition(
                decision_id, "RECOMMENDED", "OPEN", "SHADOW_POSITION_OPENED",
                occurred_at_ms, {"notional_usd": notional_usd, "price": price},
            )
        return inserted

    def close(
        self,
        decision_id: str,
        price: float,
        occurred_at_ms: int,
        fee_bps: float,
        slippage_bps: float,
        funding_usd: float = 0.0,
        reason: str = "HORIZON_OR_INVALIDATION",
    ) -> bool:
        position = self.positions().get(decision_id)
        if position is None:
            return False
        exit_notional = abs(position.quantity) * price
        gross = position.quantity * (price - position.entry_price)
        fees = exit_notional * fee_bps / 10_000
        slippage = exit_notional * slippage_bps / 10_000
        net = gross - fees - slippage - funding_usd
        prior_rows = [
            row for row in self.repository.ledger() if row.get("decision_id") == decision_id
        ]
        prior_net = sum(float(row["net_pnl_usd"]) for row in prior_rows)
        prior_fees = sum(float(row["fees_usd"]) for row in prior_rows)
        prior_funding = sum(float(row["funding_usd"]) for row in prior_rows)
        prior_slippage = sum(float(row["slippage_usd"]) for row in prior_rows)
        inserted = self.repository.append_ledger({
            "entry_id": stable_hash(decision_id, "SHADOW_CLOSE", reason),
            "decision_id": decision_id,
            "event_type": "SHADOW_CLOSE",
            "occurred_at_ms": occurred_at_ms,
            "direction": position.direction,
            "quantity_delta": -position.quantity,
            "price": price,
            "notional_usd": exit_notional,
            "cash_delta_usd": net,
            "gross_pnl_usd": gross,
            "net_pnl_usd": net,
            "fees_usd": fees,
            "funding_usd": funding_usd,
            "slippage_usd": slippage,
            "metadata": {"mode": "SHADOW", "reason": reason},
        })
        if inserted:
            previous = "OPEN"
            state = "RETRACTED" if reason.startswith("RETRACTION") else "CLOSED"
            self.repository.append_transition(
                decision_id, previous, state, reason, occurred_at_ms,
                {"price": price, "gross_pnl_usd": gross, "net_pnl_usd": net},
            )
            self.repository.save_outcome(
                decision_id, occurred_at_ms, state, gross, prior_net + net,
                {
                    "fees_usd": prior_fees + fees,
                    "funding_usd": prior_funding + funding_usd,
                    "slippage_usd": prior_slippage + slippage,
                },
            )
        return inserted

    def report(self, marks: Mapping[str, float] | None = None) -> dict:
        marks = marks or {}
        rows = self.repository.ledger()
        positions = self.positions()
        cash = sum(float(row["cash_delta_usd"]) for row in rows)
        gross_realized = sum(float(row["gross_pnl_usd"]) for row in rows)
        net_realized = sum(float(row["net_pnl_usd"]) for row in rows)
        fees = sum(float(row["fees_usd"]) for row in rows)
        funding = sum(float(row["funding_usd"]) for row in rows)
        slippage = sum(float(row["slippage_usd"]) for row in rows)
        gross_unrealized = sum(
            position.quantity * (float(marks.get(decision_id, position.entry_price)) - position.entry_price)
            for decision_id, position in positions.items()
        )
        estimated_exit_cost = sum(
            abs(position.quantity) * float(marks.get(decision_id, position.entry_price))
            * position.estimated_exit_cost_bps / 10_000
            for decision_id, position in positions.items()
        )
        net_unrealized = gross_unrealized - estimated_exit_cost
        expected_net = gross_realized - fees - funding - slippage
        if abs(expected_net - net_realized) > 1e-8:
            raise AssertionError("paper accounting identity failed")
        decision_rows = self.repository.decisions(100_000)
        rejected = sum(
            row["recommendation"].get("status") in {"REJECTED", "NO_TRADE"}
            for row in decision_rows
        )
        missed = sum(
            str(row["new_state"]) in {"EXPIRED", "RETRACTED"}
            for row in self.repository.transitions()
        )
        return {
            "mode": "SHADOW",
            "initial_equity_usd": self.initial_equity_usd,
            "cash_usd": cash,
            "equity_usd": cash + gross_unrealized,
            "liquidation_equity_usd": cash + net_unrealized,
            "gross_realized_pnl_usd": gross_realized,
            "net_realized_pnl_usd": net_realized,
            "gross_unrealized_pnl_usd": gross_unrealized,
            "net_unrealized_pnl_usd": net_unrealized,
            "unrealized_pnl_usd": net_unrealized,
            "estimated_exit_cost_usd": estimated_exit_cost,
            "fees_usd": fees,
            "funding_usd": funding,
            "slippage_usd": slippage,
            "open_positions": len(positions),
            "gross_exposure_usd": sum(item.notional_usd for item in positions.values()),
            "rejected_opportunities": rejected,
            "missed_opportunities": missed,
            "reconciles": True,
        }
