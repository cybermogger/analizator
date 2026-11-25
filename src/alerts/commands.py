from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from typing import Any

import structlog
from src.util.redaction import safe_exception

from .formatter import (
    format_event_preview, format_onchain_preview, format_paper_recommendation,
    format_paper_report, format_performance, format_signal,
)


HELP_TEXT = """Команды Hyperliquid Analyzer:
/site — адрес локального сайта
/status — последние SOL и HYPE snapshots
/sol — принудительный актуальный анализ SOL
/hype — принудительный актуальный анализ HYPE
/signals — последние значимые события
/watchlist — текущий динамический universe
/markets — реальные описательные наблюдения по deep watchlist
/analyze <exact-instrument-id> — разбор точного venue-инструмента
/analysis_status — состояние causal наблюдений и отложенных outcomes
/analysis_performance — описательные gross/net результаты после cost assumptions
/trade_plan <exact-instrument-id> — последняя пара виртуальных планов
/strategy_performance — сравнение двух виртуальных правил и учёт издержек
/opportunities — последние manual/shadow-рекомендации
/events — последние независимые event-intelligence previews
/asset <canonical-id> — решения по точному instrument/asset ID
/onchain — on-chain/memecoin candidates и строгие risk gates
/paper — сверяемый shadow portfolio
/performance — rolling качество прогнозов
/sources — состояние market/event/on-chain источников
/health — daemon/WS/freshness/SQLite
/help — эта справка"""


class TelegramCommandService:
    def __init__(self, analyzer: Any):
        self.analyzer = analyzer
        self.telegram = analyzer.telegram
        self.repository = analyzer.repository
        self._stop = asyncio.Event()
        self._offset: int | None = None
        self.log = structlog.get_logger("telegram_commands")

    async def stop(self) -> None:
        self._stop.set()

    @staticmethod
    def _utc(timestamp: int) -> str:
        return datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).strftime("%m-%d %H:%MZ")

    def _latest(self, coin: str) -> dict[str, Any] | None:
        row = self.repository.latest_signal(coin)
        return json.loads(row["payload_json"]) if row else None

    @classmethod
    def _market_card(cls, card: dict[str, Any]) -> str:
        identity = card
        observed = cls._utc(int(card["observed_at_ms"]))
        status_ru = {"OBSERVED": "НАБЛЮДЕНИЕ", "DEGRADED": "ДАННЫЕ ОГРАНИЧЕНЫ"}.get(identity["observation_status"], identity["observation_status"])
        lines = [
            f"📊 Наблюдение {identity['instrument_id']}",
            f"{identity['provider']} / {identity['venue']} · {identity['market_type']} · {identity['base']}/{identity['quote']} · symbol={identity['venue_symbol']}",
            f"Обновлено {observed} UTC · статус {status_ru} · оценка покрытия/согласия {identity['observation_score']}/100",
            "Оценка описательная, это не вероятность успеха, прогноз доходности или рекомендация.",
            str(identity.get("source_note", "Источник не подтверждён")),
        ]
        for interval, metrics in identity.get("candles", {}).items():
            lines.append(
                f"{interval}: trend {metrics['trend']} ({metrics['trend_20_bar_pct']:+.2f}%), "
                f"momentum {metrics['momentum_4_bar_pct']:+.2f}%" if metrics.get("momentum_4_bar_pct") is not None
                else f"{interval}: trend {metrics['trend']} ({metrics['trend_20_bar_pct']:+.2f}%)"
            )
            if metrics.get("realized_return_volatility_pct_per_bar") is not None:
                lines.append(f"  реализованная волатильность/бар {metrics['realized_return_volatility_pct_per_bar']:.3f}% · relative volume {metrics.get('relative_volume') if metrics.get('relative_volume') is not None else 'н/д'} · возраст закрытой свечи {metrics['candle_event_age_seconds']:.0f}s · получена {metrics['candle_receive_age_seconds']:.1f}s назад")
        for interval, quality in identity.get("candle_quality", {}).items():
            if quality.get("status") not in {"FRESH", "FRESH_WITH_GAPS"} or quality.get("gaps"):
                receive_age = (cls._utc(int(quality["received_at_ms"])) if quality.get("received_at_ms") else "нет snapshot")
                quality_ru = {"FRESH": "СВЕЖИЕ", "FRESH_WITH_GAPS": "СВЕЖИЕ, ЕСТЬ ПРОПУСК", "STALE": "УСТАРЕЛИ", "INSUFFICIENT_HISTORY": "МАЛО ИСТОРИИ", "DEGRADED": "ОШИБКА ИСТОЧНИКА", "NOT_AVAILABLE": "НЕ ПОДДЕРЖИВАЮТСЯ", "UNAVAILABLE": "НЕТ ДАННЫХ"}.get(quality.get("status"), quality.get("status"))
                lines.append(f"{interval} качество {quality_ru}: {quality.get('reason') or 'есть пропуск свечи'} · баров={quality.get('count')} · snapshot={receive_age} UTC · возраст закрытой свечи={quality.get('candle_event_age_seconds')}s · исключено open/future={quality.get('open_or_future_dropped')}")
        agreement = identity.get("multi_timeframe_agreement")
        lines.append(f"Согласие таймфреймов: {agreement * 100:.0f}%" if agreement is not None else "Согласие таймфреймов: н/д")
        bbo = identity.get("bbo")
        if bbo:
            lines.append(f"BBO {bbo['bid']:.8g}/{bbo['ask']:.8g} {identity['quote']} · spread {bbo['spread_bps']:.2f} bps · возраст {bbo['receive_age_seconds']:.1f}s")
        else:
            lines.append("BBO: НЕДОСТУПЕН или stale")
        book = identity.get("book", {})
        if book.get("status") == "FRESH":
            lines.append(f"L2 spread {book['spread_bps']:.2f} bps · дисбаланс top-5 {book.get('top5_imbalance')!s} · отображаемая глубина в {identity['quote']}; это не исполнимая ёмкость")
        else:
            lines.append(f"L2 {book.get('status', 'UNAVAILABLE')}: {book.get('reason', 'нет данных')}")
        funding, oi = identity.get("funding", {}), identity.get("open_interest", {})
        if "rate" in funding:
            lines.append(f"Funding {funding['rate']:.6g} · receive age {funding['receive_age_seconds']:.1f}s")
        else:
            lines.append(f"Funding: {funding.get('status', 'н/д')}")
        if "base" in oi:
            lines.append(f"Open interest {oi['base']:.8g} {identity['base']}" + (f" / {oi['quote']:.8g} {identity['quote']}" if oi.get("quote") is not None else ""))
        else:
            lines.append(f"Open interest: {oi.get('status', 'н/д')}")
        if identity.get("reasons"):
            lines.append("Качество/причины: " + "; ".join(identity["reasons"]))
        if identity.get("limitations"):
            lines.append("Ограничения: " + " ".join(identity["limitations"]))
        return "\n".join(lines)[:3900]

    async def _forced(self, coin: str) -> list[str]:
        timeout = float(self.analyzer.config["service"]["cycle_timeout_seconds"])
        try:
            signals = await asyncio.wait_for(
                self.analyzer.analysis_cycle(send_telegram=False, evaluate_alerts=False), timeout=timeout
            )
        except TimeoutError:
            return [f"⚠️ {coin}: анализ не завершён за {timeout:.0f}s"]
        signal = next((item for item in signals if item["coin"] == coin), None)
        return [format_signal(signal)] if signal else [f"⚠️ {coin}: свежий анализ недоступен"]

    async def handle(self, command: str) -> list[str]:
        parts = command.strip().split(maxsplit=1)
        argument = parts[1].strip() if len(parts) > 1 else ""
        command = parts[0].lower().split("@", 1)[0]
        if command == "/site":
            port = self.analyzer.config.get("dashboard", {}).get("port", 8081)
            return [f"Сайт на компьютере с анализатором: http://127.0.0.1:{port}/"]
        if command == "/help":
            return [HELP_TEXT]
        if command in {"/sol", "/hype"}:
            return await self._forced(command[1:].upper())
        if command == "/status":
            signals = [self._latest(coin) for coin in self.analyzer.config["market"]["assets"]]
            available = [format_signal(signal) for signal in signals if signal]
            return available or ["Snapshots ещё не накоплены."]
        if command == "/signals":
            rows = self.repository.recent_signal_alerts(10)
            if not rows:
                return ["Значимых сигналов ещё не было."]
            lines = ["Последние значимые сигналы:"]
            lines.extend(
                f"{self._utc(int(row['timestamp']))} {row['coin']} {row['event']} {row['direction']} {float(row['probability']):.0f}%"
                for row in rows
            )
            return ["\n".join(lines)]
        if command == "/performance":
            messages = [format_performance(self.analyzer.forecast_performance.summary())]
            paper = getattr(self.analyzer, "paper", None)
            if paper is not None:
                messages.append(format_paper_report(paper.performance()))
            return messages
        if command == "/watchlist":
            current = getattr(self.repository, "current_universe", lambda: [])()
            if not current:
                return ["Dynamic watchlist: данных ещё нет."]
            lines = ["Dynamic watchlist:"]
            lines.extend(
                f"{row['instrument_id']} [{row['bucket']}] score={float(row['score']):.2f}"
                for row in current[:30]
            )
            return ["\n".join(lines)]
        if command == "/markets":
            cards = self.analyzer.market_analysis.markets(10)
            if not cards:
                return ["Описательные оценки ещё собираются. Ниже выбранные точные инструменты; это не список сигналов:", *(f"{item.instrument_id} [{item.bucket.value}]" for item in self.analyzer.market_data.membership[:10])] if self.analyzer.market_data.membership else ["Deep watchlist пока пуст: нет завершённого свежего market catalog/BBO."]
            lines = ["Наблюдаемые рынки · descriptive only (топ-10 по покрытию/согласию):"]
            for card in cards:
                tf = card.get("candles", {})
                summary = ", ".join(f"{key} {value['trend']}" for key, value in tf.items()) or "свечи н/д"
                lines.append(f"{card['instrument_id']} · {card['observation_status']} · {card['observation_score']:.0f}/100 · {summary}")
            lines.append("Точный инструмент: /analyze <instrument_id>; виртуальный план: /trade_plan <instrument_id>. Оценки не являются win probability.")
            return ["\n".join(lines)[:3900]]
        if command == "/trade_plan":
            strategy = getattr(self.analyzer, "strategy_shadow", None)
            if strategy is None:
                return ["Виртуальные планы недоступны."]
            if not argument:
                return ["Использование: /trade_plan <exact-instrument-id>. Список ID: /markets."]
            rows = strategy.latest_plans(argument)
            if not rows:
                return ["Для этого точного instrument ID планов пока нет; проверьте /markets."]
            lines = [f"Виртуальные планы · {argument}", "Эвристика для проверки, не сигнал и не вероятность прибыли."]
            for row in rows:
                p, events = row["plan"], row["events"]
                lifecycle = next((e["type"] for e in reversed(events) if e["type"] in {"CLOSED", "EXPIRED", "REJECTED", "FILLED"}), p["status"])
                lines.append(f"{p['variant']}: {lifecycle} · {p['direction']} · свеча {self._utc(p['signal_anchor_ms']) if p.get('signal_anchor_ms') else 'нет'}")
                if p["status"] == "PROPOSED":
                    lines.append(f"  reference {p['entry_reference']:.8g}, stop {p['stop']:.8g}, target {p['target']:.8g} {p.get('quote')}; nominal {p['notional']:.2f} {p.get('quote')}; TTL до {self._utc(p['expires_at_ms'])}")
                    lines.append(f"  fee {p.get('fee_bps_per_side')} bps/side + slippage {p.get('slippage_bps_per_side')} bps/side; policy {p.get('policy_id', '')[:8]}")
                if p["reasons"]:
                    lines.append("  Причины: " + "; ".join(p["reasons"]))
            return ["\n".join(lines)[:3900]]
        if command == "/strategy_performance":
            strategy = getattr(self.analyzer, "strategy_shadow", None)
            if strategy is None:
                return ["Виртуальный эксперимент недоступен."]
            analysis = getattr(self.analyzer, "market_analysis", None)
            report = strategy.summary(analysis._now_ms() if analysis is not None else None)
            lines = ["Виртуальный эксперимент · baseline vs disciplined", "NOT_EVALUABLE: нет подтверждённой форвардной доходности; USDT и USD не суммируются."]
            for name, item in report.items():
                lines.append(f"{name}: plans={item['plans']} no_trade={item['no_trade']} pending={item['pending']} filled={item['filled']} open={item['open']} closed={item['closed']} expired={item['expired']} rejected={item['rejected']} unknown_marks={item['unknown_marks']}")
                for quote, values in sorted(item["by_quote"].items()):
                    lines.append(f"  {quote}: closed n={values['closed']}, realized net={values['realized_net']:.4f}, open mark net={values['marked_unrealized_net']:.4f}, wins={values['wins']}, turnover={values['turnover_notional']:.2f}, fees={values['fees']:.4f}, sampled max drawdown={values['max_drawdown']:.4f}, marks={values['mark_count']}, max quote gap={values['max_quote_gap_seconds']:.0f}s")
            paired = strategy.paired_comparison()
            for quote, values in sorted(paired["by_quote"].items()):
                lines.append(f"Пары {quote}: решений={values['paired_decisions']}, неполных пар={values['unpaired_decisions']}, одинаковых входов={values['both_proposed']}, оба закрыты={values['both_closed']}, ещё не завершены={values['both_unresolved']}, только baseline={values['baseline_only']}, только disciplined={values['disciplined_only']}, несовпадение политики={values['policy_mismatch']}, версий правил={len(values['policy_ids'])}")
                lines.append(f"  Разница net на закрытых парах disciplined−baseline={values['paired_disciplined_minus_baseline_net']:.4f} {quote}; односторонние закрытия показаны отдельно: baseline={values['baseline_only_closed']}, disciplined={values['disciplined_only_closed']}")
            lines.append("Издержки моделируются; mark и MFE/MAE выборочные, не биржевое исполнение. Нет причин считать эти числа прогнозом.")
            return ["\n".join(lines)[:3900]]
        if command in {"/analysis_status", "/analysis_performance"}:
            analysis = getattr(self.analyzer, "market_analysis", None)
            if analysis is None or self.repository is None or not hasattr(self.repository, "analysis_performance"):
                return ["Multi-asset observational pipeline недоступен."]
            report = self.repository.analysis_performance(analysis._now_ms())
            if command == "/analysis_status":
                health = analysis.health()
                return ["\n".join([
                    f"Causal analysis: {health['status']}; циклов {health['cycles']}; ошибок {health['failures']}",
                    f"Инструментов с наблюдениями: {report['markets_observed']}; snapshots: {report['observations']}",
                    f"Последний snapshot UTC: {self._utc(report['last_observation_at_ms']) if report['last_observation_at_ms'] else 'нет данных'}",
                    f"Outcomes: live due {report['due_pending']}; просрочено для финализации {report['expired_pending']}; обработано {health.get('outcomes_completed', 0)}; недоступно {health.get('outcomes_unavailable', 0)}",
                    f"Universe snapshot: кандидатов {report.get('latest_universe_snapshot', {}).get('candidate_count', 0) if report.get('latest_universe_snapshot') else 0}; deep {report.get('latest_universe_snapshot', {}).get('deep_count', 0) if report.get('latest_universe_snapshot') else 0}",
                    f"Каденс {health['observation_cadence_seconds']:.0f}s; окно target quote {health['outcome_max_latency_seconds']:.0f}s; очередь {health.get('outcome_request_capacity_per_minute', 0):.1f}/мин при оценочном спросе {health.get('outcome_request_demand_per_minute', 0):.1f}/мин",
                    "Наблюдательные метрики не являются торговым PnL; predictive performance остаётся NOT_EVALUABLE.",
                ])]
            lines = [f"Наблюдательные outcomes · рынков {report['markets_observed']} · snapshots {report['observations']}"]
            for cohort in report.get("cohorts", []):
                policy = json.loads(cohort["cost_assumptions_json"])
                version = f"{cohort['feature_version']}:{cohort['feature_config_hash'][:8]}"
                lines.append(f"{cohort['horizon']} · {version} · n={cohort['denominator']}: измерено={cohort['observed']}, pending={cohort['pending']}, unavailable={cohort['unavailable']}, нет reference={cohort['missing_reference']}, delisted={cohort['delisted']}")
                lines.append(f"  sample n gross/long-net={cohort['gross_metric_n']}/{cohort['long_net_metric_n']}; baseline positions/no-position={cohort['baseline_position_n']}/{cohort['baseline_no_position_n']}; нулевой gross={cohort['zero_return_n']}")
                lines.append(f"  costs policy={policy.get('policy_version')}; fee={policy.get('fee_bps_per_side')}bps/side, slippage={policy.get('slippage_bps_per_side')}bps/side, spread=half-entry+half-exit once, latency={policy.get('outcome_max_latency_ms')}ms")
            for item in report["by_horizon_status"]:
                version = f"{item['feature_version']}:{item['feature_config_hash'][:8]}"
                if item["status"] == "OBSERVED":
                    lines.append(f"{item['horizon']} {version}: gross avg={item['gross']} (n={item['gross_n']}); long-reference net avg={item['long_reference_net']} (n={item['long_reference_net_n']}); trend baseline gross/net={item['baseline_gross']}/{item['baseline_net']} (positions n={item['baseline_position_n']}, no-position n={item['baseline_no_position_n']})")
            lines.extend(["Это reference-price наблюдения, не исполненные сделки и не PnL.", "Нет OOS proof или достаточной истории; прогнозное качество NOT_EVALUABLE."])
            return ["\n".join(lines)[:3900]]
        if command == "/analyze":
            if not argument:
                return ["Использование: /analyze <exact-instrument-id>, например /analyze binance:spot:SOL-USDT"]
            if argument not in self.analyzer.market_analysis._cards:
                matches = [item.instrument_id for item in self.analyzer.market_data.instruments.all() if item.base.casefold() == argument.casefold()]
                if matches:
                    return ["Ticker сам по себе неоднозначен. Используйте точный instrument ID:\n" + "\n".join(matches[:10])]
            try:
                instrument = self.analyzer.market_data.instruments.get(argument)
            except KeyError:
                instrument = None
            current_deep = {item.instrument_id for item in self.analyzer.market_analysis._eligible_members()}
            if instrument is not None and (not instrument.active or argument not in current_deep):
                return [f"Инструмент {argument} снят или сейчас не входит в deep watchlist; актуальное наблюдение недоступно."]
            card = self.analyzer.market_analysis.get(argument)
            if card is None:
                card = await self.analyzer.market_analysis.analyze_instrument(argument)
            return [self._market_card(card)] if card is not None else ["Не найден точный instrument ID. Сначала проверьте /watchlist."]
        if command in {"/opportunities", "/asset"}:
            paper = getattr(self.analyzer, "paper", None)
            if paper is None:
                return ["Shadow recommendations: service unavailable."]
            if command == "/asset" and not argument:
                return ["Usage: /asset <canonical-asset-id or exact instrument-id>"]
            rows = paper.latest(5, argument or None)
            return [format_paper_recommendation(row) for row in rows] or [
                "Shadow recommendations: решений ещё нет."
            ]
        if command == "/paper":
            paper = getattr(self.analyzer, "paper", None)
            return [format_paper_report(paper.performance())] if paper else [
                "Shadow portfolio: service unavailable."
            ]
        if command == "/events":
            rows = self.analyzer.events.latest(5)
            return [format_event_preview(row) for row in rows] or ["Event Intelligence: событий ещё нет."]
        if command == "/onchain":
            rows = self.analyzer.onchain.latest(5)
            return [format_onchain_preview(row) for row in rows] or ["On-chain candidates: данных ещё нет."]
        if command == "/sources":
            health = self.analyzer.health()
            groups = (
                ("Market", health.get("providers", {})),
                ("Events", health.get("event_sources", {})),
                ("On-chain", health.get("onchain_providers", {})),
            )
            lines = ["Source health:"]
            for label, values in groups:
                summary = ", ".join(
                    f"{name}={str(item.get('status', 'unknown')).upper()}"
                    + (f" ({str(item.get('disabled_reason'))[:100]})" if item.get("disabled_reason") else "")
                    for name, item in values.items()
                ) or "none"
                lines.append(f"{label}: {summary}")
            return ["\n".join(lines)]
        if command == "/health":
            health = self.analyzer.health()
            freshness = []
            for coin in self.analyzer.config["market"]["assets"]:
                signal = self._latest(coin)
                status = signal.get("data_freshness", {}).get("status", "UNKNOWN") if signal else "NO_DATA"
                freshness.append(f"{coin}={status}")
            providers = ", ".join(
                f"{name}={str(value.get('status', 'unknown')).upper()}"
                for name, value in health.get("providers", {}).items()
            ) or "no samples"
            event_sources = ", ".join(
                f"{name}={str(value.get('status', 'unknown')).upper()}"
                for name, value in health.get("event_sources", {}).items()
            ) or "no sources"
            onchain_sources = ", ".join(
                f"{name}={str(value.get('status', 'unknown')).upper()}"
                + (f" (NOT_AVAILABLE:{len(value.get('not_available', []))})" if value.get("not_available") else "")
                for name, value in health.get("onchain_providers", {}).items()
            ) or "no providers"
            return [
                "\n".join([
                    "Crypto Intelligence Analyzer health",
                    f"Hyperliquid WS: {'CONNECTED' if health['ws_connected'] else 'DISCONNECTED'}",
                    f"Providers: {providers}",
                    f"Event sources: {event_sources}",
                    f"On-chain providers: {onchain_sources}",
                    f"Data: {', '.join(freshness)}",
                    f"SQLite: {health['db']['integrity']} / schema {health['db']['schema_version']}",
                    f"Paper: {health.get('paper', {}).get('mode', 'SHADOW')} / real execution disabled",
                ])
            ]
        return [HELP_TEXT]

    async def run_forever(self) -> None:
        if not self.telegram.configured:
            return
        while not self._stop.is_set():
            updates = await self.telegram.get_updates(self._offset, poll_seconds=5)
            if updates is None:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=5.0)
                except TimeoutError:
                    pass
                continue
            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    self._offset = update_id + 1
                message = update.get("message", {})
                chat_id = str(message.get("chat", {}).get("id", ""))
                text = str(message.get("text", "")).strip()
                if chat_id != str(self.telegram.chat_id) or not text.startswith("/"):
                    continue
                try:
                    command_name = text.split(maxsplit=1)[0].lower().split("@", 1)[0]
                    for response in await self.handle(text):
                        await self.telegram.send_to(chat_id, response)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self.log.error(
                        "telegram_command_failed", command=text.split(maxsplit=1)[0],
                        error=safe_exception(error),
                    )
                    await self.telegram.send_to(chat_id, "⚠️ Команда временно не выполнена.")
            if not updates:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=0.25)
                except TimeoutError:
                    pass
