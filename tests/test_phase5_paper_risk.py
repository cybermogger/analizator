from __future__ import annotations

import ast
from dataclasses import replace
import inspect
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from src.alerts.commands import HELP_TEXT, TelegramCommandService
from src.alerts.formatter import format_paper_recommendation, format_paper_report
from src.data.storage import SCHEMA_VERSION, SQLiteStorage
from src.paper.risk import PaperRiskConfig, PaperRiskEngine
from src.paper.service import PaperDecisionService
from src.paper.types import Evidence, PaperOpportunity, RiskContext
from src.research.evaluation import OpportunityCandidate


NOW = 1_750_000_000_000


def opportunity(**updates) -> PaperOpportunity:
    value = PaperOpportunity(
        opportunity_id="ranked-sol-1", canonical_asset_id="SOL", instrument_id="hyperliquid:SOL-PERP",
        segment="majors_l1", direction="LONG", decision_at_ms=NOW, horizon="1h",
        calibrated_probability=.68, expected_net_move=.018, entry_zone=(100.0, 101.0),
        invalidation=95.0, scenarios=("momentum continuation", "range failure"),
        targets=(105.0, 110.0), spread_bps=4, liquidity_usd=1_000_000,
        estimated_slippage_bps=8, venue_divergence_bps=12, predicted_volatility=.04,
        predicted_mae=-.02, exit_feasibility_probability=.92, venue="hyperliquid",
        supporting_evidence=(Evidence("market", "hl-book", "fresh multi-level book", "https://example.test/book", .95, 1, ("lineage-1",)),),
        contradicting_evidence=(Evidence("market", "funding", "crowded funding", None, .8, 2),),
        alternative_scenario="Break below 95 invalidates the continuation case.",
        source_credibility=.9, data_freshness_seconds=2, model_name="phase4-challenger",
        model_version="v1", data_lineage=("lineage-1", "snapshot-1"), score=.77,
        news_confirmed=True, correlation_cluster="solana-beta",
    )
    return replace(value, **updates)


def context(**updates) -> RiskContext:
    value = RiskContext(paper_equity_usd=100_000, now_ms=NOW + 1_000)
    return replace(value, **updates)


def service(tmp_path: Path, **updates) -> tuple[SQLiteStorage, PaperDecisionService]:
    values = {
        "initial_equity_usd": 100_000, "cooldown_seconds": 0,
        "maximum_liquidity_participation": .02,
    }
    values.update(updates)
    storage = SQLiteStorage(tmp_path / "paper.db")
    return storage, PaperDecisionService(storage, values)


def test_recommendation_contract_shadow_accounting_and_immutable_audit(tmp_path: Path):
    storage, paper = service(tmp_path)
    try:
        recommendation, risk, alert = paper.consider(opportunity(), context())
        assert risk.eligible and recommendation.status == "RECOMMENDED"
        assert recommendation.direction == "LONG" and recommendation.shadow_mode
        assert recommendation.supporting_evidence and recommendation.contradicting_evidence
        assert recommendation.data_lineage == ("lineage-1", "snapshot-1")
        assert recommendation.maximum_paper_risk_usd == pytest.approx(250)
        assert recommendation.paper_notional_usd > recommendation.maximum_paper_risk_usd
        assert alert and paper.portfolio.report()["open_positions"] == 1

        preview = format_paper_recommendation(recommendation.as_dict())
        assert "Supporting:" in preview and "Contradicting:" in preview
        assert "not personalized financial advice" in preview
        assert paper.record_alert(recommendation, preview, NOW + 2_000, "tg-1")
        assert not paper.record_alert(recommendation, preview, NOW + 3_000, "tg-2")

        assert paper.close(recommendation, 110.0, NOW + 3_600_000, funding_usd=3.0)
        report = paper.performance()
        assert report["reconciles"] and report["open_positions"] == 0
        assert report["gross_realized_pnl_usd"] > report["net_realized_pnl_usd"]
        assert report["fees_usd"] > 0 and report["slippage_usd"] > 0
        assert report["funding_usd"] == pytest.approx(3.0)
        assert report["cash_usd"] == pytest.approx(
            report["initial_equity_usd"] + report["net_realized_pnl_usd"]
        )
        assert "RECONCILED" in format_paper_report(report)

        with pytest.raises(sqlite3.IntegrityError):
            storage.execute("UPDATE paper_decisions SET status='MUTATED'")
        with pytest.raises(sqlite3.IntegrityError):
            storage.execute("DELETE FROM paper_ledger")
    finally:
        storage.close()


@pytest.mark.parametrize(("candidate", "risk_context", "reason"), [
    (opportunity(news_confirmed=False), context(), "UNCONFIRMED_NEWS"),
    (opportunity(contradicting_evidence=(Evidence("event", "dump", "competing-token dump", None, .8, 1),)), context(liquidity_loss=True), "LIQUIDITY_LOSS"),
    (opportunity(), context(primary_venue_outage=True), "PRIMARY_VENUE_OUTAGE"),
    (opportunity(venue_divergence_bps=500), context(), "VENUE_DIVERGENCE"),
    (opportunity(liquidity_usd=1_000), context(), "LIQUIDITY_LOSS"),
    (opportunity(), context(model_drift=True), "MODEL_DRIFT"),
    (opportunity(spread_bps=900), context(extreme_gap=True), "EXTREME_GAP"),
    (opportunity(), context(daily_net_pnl_usd=-1_501), "DAILY_LOSS_LIMIT"),
    (opportunity(data_freshness_seconds=300), context(feed_stale=True), "STALE_FEED"),
])
def test_circuit_breakers_fail_closed(candidate, risk_context, reason):
    result = PaperRiskEngine(PaperRiskConfig(cooldown_seconds=0)).evaluate(candidate, risk_context)
    assert not result.eligible and reason in result.reasons


def test_correlated_and_concurrent_exposure_limits():
    config = PaperRiskConfig(cooldown_seconds=0)
    engine = PaperRiskEngine(config)
    correlated = engine.evaluate(opportunity(), context(
        current_gross_exposure_usd=10_000,
        cluster_exposure_usd={"solana-beta": 15_000},
    ))
    assert not correlated.eligible and "CORRELATED_EXPOSURE_LIMIT" in correlated.reasons
    concurrent = engine.evaluate(opportunity(), context(open_position_count=8))
    assert not concurrent.eligible and "CONCURRENT_EXPOSURE_LIMIT" in concurrent.reasons


@pytest.mark.parametrize(("candidate", "risk_context", "reason"), [
    (opportunity(predicted_volatility=.5), context(), "EXTREME_VOLATILITY"),
    (opportunity(predicted_mae=-.2), context(), "ADVERSE_EXCURSION"),
    (opportunity(exit_feasibility_probability=.2), context(), "EXIT_NOT_FEASIBLE"),
    (opportunity(evidence_retracted=True), context(), "EVIDENCE_RETRACTED"),
    (opportunity(expected_net_move=-.01), context(), "NON_POSITIVE_EXPECTED_NET_MOVE"),
    (opportunity(calibrated_probability=.3), context(), "CALIBRATED_PROBABILITY_TOO_LOW"),
    (opportunity(risk_eligible=False), context(), "NOT_RANKED_ELIGIBLE"),
    (opportunity(), context(current_gross_exposure_usd=50_000), "GROSS_EXPOSURE_LIMIT"),
    (opportunity(), context(last_decision_at_ms={"hyperliquid:SOL-PERP": NOW}), "COOLDOWN"),
    (opportunity(), context(now_ms=NOW + 2_000_000), "OPPORTUNITY_EXPIRED"),
])
def test_remaining_configurable_gates(candidate, risk_context, reason):
    result = PaperRiskEngine(PaperRiskConfig()).evaluate(candidate, risk_context)
    assert not result.eligible and reason in result.reasons


def test_ranked_phase4_candidate_maps_without_changing_forecast_units():
    candidate = OpportunityCandidate(
        "ranked-sol-1", "row", "1h", NOW, "hyperliquid:SOL-PERP",
        "majors_l1", "trend", .68, .018, -.02, .04, .04, .92,
        1_000_000, True, {"momentum": 1.0},
    )
    mapped = PaperOpportunity.from_ranked(
        candidate, direction="LONG", canonical_asset_id="SOL",
        entry_zone=(100, 101), invalidation=95, targets=(105, 110),
        spread_bps=4, estimated_slippage_bps=8, venue_divergence_bps=12,
        scenarios=("continuation", "failure"), model_name="phase4",
        model_version="v1", data_lineage=("snapshot",), venue="hyperliquid",
        source_credibility=.9, data_freshness_seconds=1,
    )
    assert mapped.calibrated_probability == candidate.tp_probability
    assert mapped.expected_net_move == candidate.expected_net_return
    assert mapped.horizon == candidate.horizon and mapped.risk_eligible


def test_memecoin_identity_and_lower_risk_budget():
    with pytest.raises(ValueError):
        opportunity(segment="new_memecoin")
    meme = opportunity(
        opportunity_id="meme", segment="new_memecoin", canonical_asset_id="solana:mint-1",
        instrument_id="solana:mint-1", chain_id="solana", contract_address="mint-1",
    )
    result = PaperRiskEngine(PaperRiskConfig(cooldown_seconds=0)).evaluate(meme, context())
    assert result.paper_risk_usd == pytest.approx(50.0)


def test_no_trade_retraction_expiry_and_outcomes_are_persisted(tmp_path: Path):
    storage, paper = service(tmp_path)
    try:
        no_trade, risk, _ = paper.consider(
            opportunity(opportunity_id="wait", direction="NO_TRADE"), context()
        )
        assert not risk.eligible and no_trade.status == no_trade.direction == "NO_TRADE"
        assert paper.expire(no_trade, NOW + 2_000)
        expired_counts = paper.repository.counts()
        assert not paper.expire(no_trade, NOW + 3_000)
        assert paper.repository.counts() == expired_counts
        eligible, _, _ = paper.consider(
            opportunity(opportunity_id="later", decision_at_ms=NOW + 10_000),
            context(now_ms=NOW + 11_000),
        )
        assert paper.retract(eligible, 99.0, NOW + 12_000)
        retracted_counts = paper.repository.counts()
        assert not paper.retract(eligible, 98.0, NOW + 13_000)
        assert paper.repository.counts() == retracted_counts
        states = [row["new_state"] for row in paper.repository.transitions()]
        assert {"NO_TRADE", "EXPIRED", "RECOMMENDED", "OPEN", "RETRACTED"} <= set(states)
        outcome_states = {row["outcome_state"] for row in paper.repository.outcomes()}
        assert outcome_states == {"NO_TRADE", "RETRACTED"}
    finally:
        storage.close()


def test_shadow_open_rejects_non_directional_values(tmp_path: Path):
    storage, paper = service(tmp_path)
    try:
        with pytest.raises(ValueError, match="LONG or SHORT"):
            paper.portfolio.open("invalid", "WAIT", 1_000, 100, NOW, 4, 8)
        assert not [row for row in paper.repository.ledger() if row["event_type"] == "SHADOW_OPEN"]
    finally:
        storage.close()


def test_restart_is_idempotent_for_account_decision_alert_and_position(tmp_path: Path):
    database = tmp_path / "restart.db"
    storage1 = SQLiteStorage(database)
    first = PaperDecisionService(storage1, {"cooldown_seconds": 0})
    recommendation, _, _ = first.consider(opportunity(), context())
    first.record_alert(recommendation, "message", NOW + 2_000)
    before = first.repository.counts()
    storage1.close()

    storage2 = SQLiteStorage(database)
    second = PaperDecisionService(storage2, {"cooldown_seconds": 0})
    try:
        duplicate, _, alert = second.consider(opportunity(), context())
        after = second.repository.counts()
        assert duplicate.decision_id == recommendation.decision_id and not alert
        assert after["paper_decisions"] == before["paper_decisions"] == 1
        assert after["paper_alerts"] == before["paper_alerts"] == 1
        assert len(second.portfolio.positions()) == 1
        assert second.current_state(recommendation.decision_id) == "OPEN"
        opens = [row for row in second.repository.ledger() if row["event_type"] == "SHADOW_OPEN"]
        assert len(opens) == 1
    finally:
        storage2.close()


def test_material_change_creates_revision_and_new_alert_eligibility(tmp_path: Path):
    storage, paper = service(tmp_path)
    try:
        first, _, alert = paper.consider(opportunity(), observed_at_ms=NOW + 1_000)
        assert alert and paper.record_alert(first, "first", NOW + 1_000)
        changed, _, changed_alert = paper.consider(
            opportunity(score=.91, liquidity_usd=900_000), observed_at_ms=NOW + 2_000
        )
        assert changed.decision_id == first.decision_id and changed_alert
        assert paper.repository.counts()["paper_recommendation_revisions"] == 2
        assert paper.latest(1)[0]["recommendation"]["score"] == pytest.approx(.91)
        assert paper.current_state(first.decision_id) == "OPEN"
    finally:
        storage.close()


def test_disappearing_liquidity_retracts_existing_shadow_position(tmp_path: Path):
    storage, paper = service(tmp_path)
    try:
        recommendation, _, _ = paper.consider(opportunity(), context())
        assert recommendation.decision_id in paper.portfolio.positions()
        updated, risk, alert = paper.consider(
            opportunity(liquidity_usd=1_000), context(liquidity_loss=True),
            observed_at_ms=NOW + 5_000,
        )
        assert not risk.eligible and alert and updated.status == "RETRACTED"
        assert recommendation.decision_id not in paper.portfolio.positions()
        assert paper.current_state(recommendation.decision_id) == "RETRACTED"
        assert any(row["outcome_state"] == "RETRACTED" for row in paper.repository.outcomes())
    finally:
        storage.close()


def test_phase5_schema_v8_to_v9_is_additive_and_immutable(tmp_path: Path):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript("""
      CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL);
      INSERT INTO schema_migrations VALUES (8, 1);
      CREATE TABLE signals(
        id INTEGER PRIMARY KEY, coin TEXT NOT NULL, timestamp INTEGER NOT NULL,
        primary_horizon TEXT NOT NULL, direction TEXT NOT NULL, probability REAL NOT NULL,
        payload_json TEXT NOT NULL, model_status TEXT NOT NULL
      );
      INSERT INTO signals VALUES (1,'SOL',1,'1h','WAIT',50,'{}','MODEL_REJECTED_OOS');
    """)
    connection.commit()
    connection.close()
    storage = SQLiteStorage(path)
    try:
        assert SCHEMA_VERSION == storage.health()["schema_version"] == 13
        assert storage.health()["integrity"] == "ok"
        assert storage.query("SELECT coin FROM signals")[0]["coin"] == "SOL"
        assert storage.query("SELECT COUNT(*) AS n FROM paper_decisions")[0]["n"] == 0
    finally:
        storage.close()


@pytest.mark.asyncio
async def test_required_telegram_commands(tmp_path: Path):
    storage, paper = service(tmp_path)
    paper.consider(opportunity(), context())
    repository = SimpleNamespace(
        latest_signal=lambda _coin: None, recent_signal_alerts=lambda _limit: [],
        current_universe=lambda: [{"instrument_id": "hyperliquid:SOL-PERP", "bucket": "majors_l1", "score": 1}],
    )
    analyzer = SimpleNamespace(
        telegram=SimpleNamespace(), repository=repository, paper=paper,
        config={"market": {"assets": ["SOL", "HYPE"]}, "service": {"cycle_timeout_seconds": 10}},
        forecast_performance=SimpleNamespace(summary=lambda: {"samples": 0}),
        events=SimpleNamespace(latest=lambda _limit: []),
        onchain=SimpleNamespace(latest=lambda _limit: []),
        health=lambda: {"ws_connected": False, "providers": {}, "event_sources": {},
                        "onchain_providers": {}, "db": {"integrity": "ok", "schema_version": 13},
                        "paper": paper.health()},
    )
    commands = TelegramCommandService(analyzer)
    try:
        for name in ("/watchlist", "/opportunities", "/events", "/asset", "/paper",
                     "/performance", "/sources", "/health"):
            assert name in HELP_TEXT
        assert "Dynamic watchlist" in (await commands.handle("/watchlist"))[0]
        assert "SHADOW" in (await commands.handle("/opportunities"))[0]
        assert "SOL" in (await commands.handle("/asset SOL"))[0]
        assert "RECONCILED" in (await commands.handle("/paper"))[0]
        assert "Source health" in (await commands.handle("/sources"))[0]
    finally:
        storage.close()


def test_paper_layer_has_no_execution_or_secret_capability():
    root = Path(__file__).parents[1] / "src" / "paper"
    forbidden_calls = {"place_order", "create_order", "send_transaction", "sign_transaction", "execute_order"}
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = {
            node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
            for node in ast.walk(tree) if isinstance(node, ast.Call)
            and isinstance(node.func, (ast.Attribute, ast.Name))
        }
        assert not calls & forbidden_calls
    fields = set(PaperRiskConfig.__dataclass_fields__)
    assert not fields & {"api_key", "private_key", "seed_phrase", "wallet", "exchange_credentials"}
    assert set(inspect.signature(PaperDecisionService).parameters) == {"storage", "config"}
