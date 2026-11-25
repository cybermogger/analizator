from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _price(value: float | None) -> str:
    if value is None:
        return "н/д"
    if abs(value) >= 1000:
        return f"${value:,.1f}"
    if abs(value) >= 10:
        return f"${value:.2f}"
    return f"${value:.4f}"


def _zone(zone: Any) -> str:
    return "н/д" if not zone else f"{_price(float(zone[0]))}–{_price(float(zone[1]))}"


def _move(values: Any) -> str:
    if not values:
        return "н/д"
    return f"{float(values[0]):+.1f}%…{float(values[1]):+.1f}%"


def _heading(signal: dict[str, Any]) -> str:
    coin, direction = signal["coin"], signal["direction"]
    event = signal.get("event")
    if event == "SIGNAL_WEAKENED":
        return f"⚠️ {coin} — {direction} ОСЛАБ"
    if event == "SIGNAL_INVALIDATED":
        previous = signal.get("previous_direction", direction)
        return f"❌ {coin} — {previous} INVALIDATED"
    if event == "REVERSAL_DETECTED":
        return f"{'🟢' if direction == 'LONG' else '🔴'} {coin} — REVERSAL → {direction}"
    if event == "SIGNAL_STRENGTHENED":
        return f"{'🟢' if direction == 'LONG' else '🔴'} {coin} — {direction} УСИЛИЛСЯ"
    if event == "TARGET_REACHED":
        return f"🎯 {coin} — TARGET REACHED"
    if event == "BREAKOUT":
        return f"🚀 {coin} — BREAKOUT"
    if event == "BREAKDOWN":
        return f"🔻 {coin} — BREAKDOWN"
    if event == "VOLATILITY_SPIKE":
        return f"⚡ {coin} — VOLATILITY SPIKE"
    if event == "BTC_SHOCK":
        return f"⚡ {coin} — BTC SHOCK"
    if event == "MARKET_REGIME_CHANGED":
        return f"🔄 {coin} — РЕЖИМ ИЗМЕНИЛСЯ"
    emoji = "🟢" if direction == "LONG" else "🔴" if direction == "SHORT" else "⚪"
    return f"{emoji} {coin} — {direction}"


def format_signal(signal: dict[str, Any]) -> str:
    primary = signal["horizons"][signal["primary_horizon"]]
    freshness = signal.get("data_freshness", {})
    reasons = [str(reason).replace("\n", " ") for reason in signal.get("reasons", [])[:4]]
    lines = [
        _heading(signal),
        f"Цена: {_price(signal['mark'])}",
        f"Горизонт: {signal['primary_horizon']} · оценки ансамбля, калибровка итога не подтверждена",
        f"LONG {primary['LONG']:.0f}% | SHORT {primary['SHORT']:.0f}% | WAIT {primary['WAIT']:.0f}%",
        f"Confidence: {float(signal.get('confidence', 0)):.0f}/100",
        f"Signal strength: {float(signal.get('signal_strength', 0)):.0f}/100",
        f"Режим: {signal.get('regime', 'UNKNOWN')}",
        f"Ожидание: {_move(signal.get('expected_move'))}",
        f"Диапазон: {_zone(signal.get('expected_range'))}",
        f"Цель: {_zone(signal.get('target_range'))}",
        f"Invalidation: {_price(signal.get('invalidation'))}",
        f"Поддержка: {_price(signal.get('support'))}",
        f"Сопротивление: {_price(signal.get('resistance'))}",
        f"Волатильность: {float(signal.get('volatility', {}).get('percent', 0)):.2f}%",
        f"Данные: {freshness.get('status', 'UNKNOWN')} ({float(freshness.get('age_seconds', 0)):.0f}s)",
    ]
    if reasons:
        lines.append("Причины: " + "; ".join(reasons))
    return "\n".join(lines)


def format_performance(summary: dict[str, Any], coin: str | None = None) -> str:
    name = coin or "SOL/HYPE"
    if not summary.get("samples"):
        return f"📊 {name}: ещё нет завершённых прогнозов."
    percent = lambda value: "н/д" if value is None else f"{float(value) * 100:.1f}%"
    number = lambda value: "н/д" if value is None else f"{float(value):.4f}"
    lines = [
        f"📊 {name} — качество прогнозов",
        f"Samples: {summary['samples']}",
        f"Directional accuracy: {percent(summary.get('directional_accuracy'))}",
        f"Balanced accuracy: {percent(summary.get('balanced_accuracy'))}",
        f"Precision LONG/SHORT: {percent(summary.get('precision_long'))} / {percent(summary.get('precision_short'))}",
        f"Brier: {number(summary.get('brier'))} | ECE: {number(summary.get('ece'))}",
        f"Avg return LONG/SHORT: {percent(summary.get('average_forward_return_long'))} / {percent(summary.get('average_forward_return_short'))}",
        "Calibration buckets:",
    ]
    for bucket in summary.get("calibration_buckets", []):
        predicted = "н/д" if bucket.get("predicted_probability") is None else f"{bucket['predicted_probability']:.0f}%"
        actual = "н/д" if bucket.get("success_rate") is None else f"{bucket['success_rate']:.0f}%"
        lines.append(f"{bucket['bucket']}: n={bucket['count']} forecast={predicted} actual={actual}")
    return "\n".join(lines)


def format_event_preview(event: dict[str, Any]) -> str:
    assets = event.get("impacted_assets", [])
    asset_names = ", ".join(
        str(item.get("canonical_asset_id") or item.get("project_id")) for item in assets[:4]
    ) or "не разрешены детерминированно"
    risk = "да" if bool(event.get("authenticity_risk")) else "нет"
    contradiction = str(event.get("contradiction_state", "none")).upper()
    contracts = ", ".join(str(item.get("contract_address")) for item in assets if item.get("contract_address")) or "н/д"
    def utc(value: Any) -> str:
        try:
            return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        except (TypeError, ValueError, OSError):
            return "н/д"
    lines = [
        f"📰 EVENT — {event.get('event_type', 'UNKNOWN')}",
        str(event.get("summary", ""))[:280],
        f"Assets: {asset_names}",
        f"Contract/mint: {contracts}",
        f"Credibility: {float(event.get('credibility', 0)) * 100:.0f}% | confirmations: {int(event.get('confirmation_count', 0))}",
        f"Novelty: {float(event.get('novelty', 0)) * 100:.0f}% | confidence: {float(event.get('confidence', 0)) * 100:.0f}%",
        f"Authenticity risk: {risk} | contradiction: {contradiction}",
        f"Published: {utc(event.get('first_published_at_ms'))}",
        f"Received: {utc(event.get('first_received_at_ms'))}",
        f"Decay half-life/TTL: {float(event.get('decay_half_life_minutes', 0)):.0f} min",
        "ℹ️ Контекст события, не торговый сигнал.",
    ]
    evidence = [str(url) for url in event.get("evidence_urls", []) if url]
    if evidence:
        lines.append("Evidence: " + " | ".join(evidence[:2]))
    return "\n".join(lines)


def format_onchain_preview(candidate: dict[str, Any]) -> str:
    """Explainable read-only candidate preview; never an order instruction."""
    quote = candidate.get("paper_exit_quote") or {}
    authorities = candidate.get("authorities") or {}
    scores = candidate.get("scores") or {}
    flags = [str(value) for value in candidate.get("risk_flags", [])]
    invalidation = [str(value) for value in candidate.get("invalidation", [])]
    symbol = candidate.get("symbol") or "UNKNOWN"
    chain = str(candidate.get("chain_id", "unknown"))
    contract = str(candidate.get("contract_address", "unknown"))
    age = float(candidate.get("token_age_seconds", 0))
    notional = float(quote.get("notional_usd", 0))
    slippage = float(quote.get("effective_slippage_bps", quote.get("slippage_bps", 0))) / 100
    top10 = candidate.get("top10_holder_percent")
    top10_text = "н/д" if top10 is None else f"{float(top10):.1f}%"
    authority_text = ", ".join(
        f"{name}={value if value is not None else 'revoked'}"
        for name, value in (
            ("mint", authorities.get("mint")),
            ("freeze", authorities.get("freeze")),
            ("update", authorities.get("update")),
            ("proxy", authorities.get("proxy_implementation")),
        )
    )
    lines = [
        f"🧬 ON-CHAIN — {symbol} / {candidate.get('status', 'UNKNOWN')}",
        f"Chain: {chain}",
        f"Exact contract/mint: {contract}",
        f"Token age: {age:.0f}s",
        f"Pool/provider: {candidate.get('pool_id') or 'н/д'} / {candidate.get('pool_provider') or 'н/д'}",
        f"Liquidity / proven exit capacity: ${float(candidate.get('liquidity_usd', 0)):,.0f} / ${float(candidate.get('proven_sellable_capacity_usd', 0)):,.0f}",
        f"Paper exit ${notional:,.0f}: slippage {slippage:.2f}% / net ${float(quote.get('net_output_usd', 0)):,.2f}",
        f"Top-10 holders: {top10_text}",
        f"Authorities: {authority_text}",
        f"Scores A/Auth/Liq/Dist/Mom/Manip/Exit: {float(scores.get('attention', 0)):.0f}/{float(scores.get('authenticity', 0)):.0f}/{float(scores.get('liquidity', 0)):.0f}/{float(scores.get('distribution', 0)):.0f}/{float(scores.get('onchain_momentum', 0)):.0f}/{float(scores.get('manipulation_risk', 0)):.0f}/{float(scores.get('exit_feasibility', 0)):.0f}",
        "Evidence: " + (", ".join(str(value) for value in candidate.get("evidence_event_ids", [])[:5]) or "none"),
        "Risk flags: " + (", ".join(flags) or "none"),
        f"TTL: {int(candidate.get('ttl_minutes', 0))} min",
        "Invalidation: " + ("; ".join(invalidation) or "none"),
        "ℹ️ Intelligence preview only — no automatic purchase or order execution.",
    ]
    return "\n".join(lines)


def format_paper_recommendation(value: dict[str, Any]) -> str:
    """Explain one manual recommendation without presenting it as an order."""
    recommendation = value.get("recommendation", value)
    supporting = recommendation.get("supporting_evidence", [])
    contradicting = recommendation.get("contradicting_evidence", [])

    def evidence_lines(items: list[dict[str, Any]]) -> str:
        values = []
        for item in items[:4]:
            suffix = f" ({item['url']})" if item.get("url") else ""
            values.append(f"{item.get('summary', 'evidence')}{suffix}")
        return "; ".join(values) or "none"

    identity = recommendation.get("canonical_asset_id", "unknown")
    if recommendation.get("contract_address"):
        identity += f" / {recommendation.get('chain_id')}:{recommendation['contract_address']}"
    probability = float(recommendation.get("calibrated_probability", 0)) * 100
    expected = float(recommendation.get("expected_net_move", 0)) * 100
    zone = recommendation.get("entry_zone", [])
    targets = recommendation.get("targets", [])
    reasons = recommendation.get("risk_reasons", [])
    return "\n".join([
        f"🧾 SHADOW — {recommendation.get('direction', 'NO_TRADE')} / {recommendation.get('status', 'UNKNOWN')}",
        f"Asset: {identity}",
        f"Venue/instrument: {recommendation.get('venue') or 'n/a'} / {recommendation.get('instrument_id', 'n/a')}",
        f"Decision age/TTL: {float(recommendation.get('age_seconds', 0)):.0f}s / {int(recommendation.get('ttl_seconds', 0))}s",
        f"Calibrated TP: {probability:.1f}% | expected net move: {expected:+.2f}%",
        f"Opportunity score: {float(recommendation.get('score', 0)):.2f}",
        f"Entry: {_zone(zone)} | invalidation: {_price(recommendation.get('invalidation'))}",
        "Targets: " + (", ".join(_price(float(item)) for item in targets) or "n/a"),
        f"Horizon: {recommendation.get('horizon', 'n/a')}",
        f"Spread/slippage/divergence: {float(recommendation.get('spread_bps', 0)):.1f}/{float(recommendation.get('estimated_slippage_bps', 0)):.1f}/{float(recommendation.get('venue_divergence_bps', 0)):.1f} bps",
        f"Liquidity: ${float(recommendation.get('liquidity_usd', 0)):,.0f}",
        f"Paper risk/notional cap: ${float(recommendation.get('maximum_paper_risk_usd', 0)):,.2f} / ${float(recommendation.get('paper_notional_usd', 0)):,.2f}",
        f"Supporting: {evidence_lines(supporting)}",
        f"Contradicting: {evidence_lines(contradicting)}",
        f"Alternative: {recommendation.get('alternative_scenario', 'remain in cash')}",
        f"Credibility/freshness: {float(recommendation.get('source_credibility', 0)) * 100:.0f}% / {float(recommendation.get('data_freshness_seconds', 0)):.0f}s",
        f"Model: {recommendation.get('model_name', 'unknown')}@{recommendation.get('model_version', 'unknown')}",
        "Lineage: " + (", ".join(recommendation.get("data_lineage", [])[:4]) or "none"),
        "Risk gates: " + (", ".join(str(item) for item in reasons) or "passed"),
        "ℹ️ Shadow paper recommendation only; not personalized financial advice.",
    ])


def format_paper_report(report: dict[str, Any]) -> str:
    return "\n".join([
        "📒 Shadow paper portfolio",
        f"Cash/equity: ${float(report.get('cash_usd', 0)):,.2f} / ${float(report.get('equity_usd', 0)):,.2f}",
        f"Open positions / gross exposure: {int(report.get('open_positions', 0))} / ${float(report.get('gross_exposure_usd', 0)):,.2f}",
        f"Gross/net realized PnL: ${float(report.get('gross_realized_pnl_usd', 0)):,.2f} / ${float(report.get('net_realized_pnl_usd', 0)):,.2f}",
        f"Gross/net unrealized PnL: ${float(report.get('gross_unrealized_pnl_usd', 0)):,.2f} / ${float(report.get('net_unrealized_pnl_usd', 0)):,.2f}",
        f"Fees/funding/slippage: ${float(report.get('fees_usd', 0)):,.2f} / ${float(report.get('funding_usd', 0)):,.2f} / ${float(report.get('slippage_usd', 0)):,.2f}",
        f"Rejected/missed: {int(report.get('rejected_opportunities', 0))} / {int(report.get('missed_opportunities', 0))}",
        f"Accounting: {'RECONCILED' if report.get('reconciles') else 'FAILED'}",
        "ℹ️ Shadow observations are not evidence of profitability.",
    ])
