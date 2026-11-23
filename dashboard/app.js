const $ = (id) => document.getElementById(id);
let markets = [];
let forecasts = [];
let forecastRequest = 0;
const el = (tag, className, value) => { const node = document.createElement(tag); if (className) node.className = className; if (value != null) node.textContent = String(value); return node; };
const age = (ms) => { if (!Number.isFinite(Number(ms))) return "—"; const delta = Math.max(0, Date.now() - Number(ms)); return delta < 60000 ? "только что" : delta < 3600000 ? `${Math.floor(delta / 60000)} мин назад` : `${Math.floor(delta / 3600000)} ч назад`; };
const price = (value) => { const n = Number(value); if (!Number.isFinite(n) || n <= 0) return "—"; return n.toLocaleString("ru-RU", { maximumFractionDigits: n < .01 ? 8 : n < 1 ? 5 : 2 }); };
const roundedProbabilities = p => {
  const keys = ["LONG", "SHORT", "WAIT"];
  const output = Object.fromEntries(keys.map(key => [key, Math.floor(p[key])]));
  const order = [...keys].sort((a,b) => (p[b]-output[b])-(p[a]-output[a]));
  for (let n=0; n<100-keys.reduce((sum,key)=>sum+Math.floor(p[key]),0); n++) output[order[n%3]]++;
  return output;
};
function drawMarkets() {
  const query = $("market-search").value.trim().toUpperCase();
  const type = $("market-filter").value;
  const rows = $("market-rows"); rows.replaceChildren();
  const filtered = markets.filter(m => (!query || `${m.base} ${m.instrument_id}`.toUpperCase().includes(query)) && (type === "all" || (type === "perp" ? m.market_type !== "spot" : m.market_type === "spot")));
  if (!filtered.length) { const tr = el("tr"); const td = el("td", "empty", markets.length ? "По запросу ничего не найдено" : "Пока нет проверенных рыночных наблюдений"); td.colSpan = 5; tr.append(td); rows.append(tr); return; }
  for (const m of filtered) {
    const tr = el("tr"); const instrument = el("td"); instrument.append(el("span", "asset-name", m.base || "—"), el("span", "asset-sub", `${m.venue || "—"} · ${m.quote || "—"} · ${m.market_type || "—"}`)); tr.append(instrument);
    tr.append(el("td", "", price(m.price)));
    const trends = el("td"); for (const tf of ["15m", "1h", "4h"]) { const t = m.trends?.[tf]; trends.append(el("span", `trend ${t === "DOWN" ? "down" : t === "UP" ? "" : "na"}`, `${tf} ${t === "UP" ? "↑" : t === "DOWN" ? "↓" : "—"}`)); } tr.append(trends);
    const fresh = m.observation_status === "OBSERVED" && ["15m", "1h", "4h"].every(tf => m.quality?.[tf] === "FRESH") && Date.now() - Number(m.observed_at_ms) < 180000;
    tr.append(el("td", `status ${fresh ? "" : "bad"}`, fresh ? "Свежие" : "Проверь свежесть"));
    const score = el("td", "score", m.observation_score == null ? "—" : `${m.observation_score}/100`); score.append(el("small", "", age(m.observed_at_ms))); tr.append(score);
    rows.append(tr);
  }
}
function drawEvents(events) {
  const list = $("event-list"); list.replaceChildren();
  if (!events.length) { list.append(el("div", "empty", "Пока нет проверенных событий в ленте")); return; }
  for (const event of events) {
    const row = el("article", `event ${event.contradiction_state === "retracted" ? "retracted" : ""}`);
    row.append(el("div", "event-type", event.event_type || "СОБЫТИЕ"));
    const main = el("div", "event-main"); main.append(el("strong", "", event.summary || "Описание недоступно"));
    main.append(el("p", "", `${(event.assets || []).join(", ") || "Рынок в целом"} · ${(event.source_ids || []).join(", ") || "Источник не указан"}${event.contradiction_state && event.contradiction_state !== "none" ? ` · статус: ${event.contradiction_state}` : ""}`)); row.append(main);
    const meta = el("div", "event-meta", age(event.received_at_ms));
    const url = (event.evidence_urls || []).find(value => { try { const parsed = new URL(value); return parsed.protocol === "https:"; } catch { return false; } });
    if (url) { const link = el("a", "", "Первоисточник"); link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer"; meta.append(link); }
    row.append(meta); list.append(row);
  }
}
function drawSignals(signals) {
  const list = $("signal-list"); list.replaceChildren();
  if (!signals.length) { list.append(el("div", "empty", "Сейчас нет свежих экспериментальных сетапов")); return; }
  for (const signal of signals) {
    const row = el("article", "event"); row.append(el("div", "event-type", signal.direction || "СЕТАП"));
    const main = el("div", "event-main"); main.append(el("strong", "", signal.instrument_id || "—"));
    main.append(el("p", "", `Ориентир: ${price(signal.entry_reference)} · Стоп: ${price(signal.stop)} · Цель: ${price(signal.target)} · Номинал до ${price(signal.notional)} ${signal.quote || ""}`));
    main.append(el("p", "", "Вход только по новой свежей котировке до срока действия; ордер не отправлен."));
    row.append(main); row.append(el("div", "event-meta", `до ${new Date(signal.expires_at_ms).toLocaleTimeString("ru-RU")}`)); list.append(row);
  }
}
function drawPaperJournal(records) {
  const list = $("paper-list"); list.replaceChildren();
  if (!records.length) { list.append(el("div", "empty", "Пока нет виртуальных сетапов — анализатор продолжает наблюдение")); return; }
  const labels = { PENDING: "ОЖИДАЕТ ВХОДА", OPEN: "ВИРТУАЛЬНО ОТКРЫТ", CLOSED: "ЗАКРЫТ", EXPIRED: "ВХОД ПРОПУЩЕН", REJECTED: "ОТКЛОНЁН" };
  for (const record of records) {
    const row = el("article", "event"); row.append(el("div", "event-type", labels[record.status] || "НАБЛЮДЕНИЕ"));
    const main = el("div", "event-main"); main.append(el("strong", "", record.instrument_id || "—"));
    main.append(el("p", "", `Ориентир ${price(record.entry_reference)} · стоп ${price(record.stop)} · цель ${price(record.target)} · до ${price(record.notional)} ${record.quote || ""}`));
    if (record.status === "CLOSED" && Number.isFinite(Number(record.realized_net))) {
      const net = Number(record.realized_net);
      main.append(el("p", "", `Виртуальный итог после модельных издержек: ${net >= 0 ? "+" : ""}${net.toFixed(4)} ${record.quote || ""} · ${record.close_reason || "закрытие"}`));
    } else if (record.status === "OPEN") {
      main.append(el("p", "", `Виртуальный вход ${price(record.filled_price)}; итог ещё неизвестен.`));
    } else if (record.status === "PENDING") {
      main.append(el("p", "", "Вход ещё не подтверждён свежей котировкой; результат неизвестен."));
    }
    row.append(main); row.append(el("div", "event-meta", age(record.observed_at_ms))); list.append(row);
  }
}
function drawCandidates(candidates) {
  const list = $("candidate-list"); list.replaceChildren();
  if (!candidates.length) { list.append(el("div", "empty", "Ончейн-источники пока не дали проверенных кандидатов")); return; }
  for (const candidate of candidates) {
    const row = el("article", "event"); row.append(el("div", "event-type", candidate.status || "НАБЛЮДЕНИЕ"));
    const main = el("div", "event-main"); main.append(el("strong", "", `${candidate.symbol || "Без тикера"} · ${candidate.chain_id || "—"} / ${candidate.contract_address || "—"}`));
    const liquidity = Number(candidate.liquidity_usd); const capacity = Number(candidate.proven_sellable_capacity_usd);
    main.append(el("p", "", `Ликвидность: ${Number.isFinite(liquidity) ? `$${liquidity.toLocaleString("ru-RU")}` : "неизвестно"} · Доказанный выход: ${Number.isFinite(capacity) ? `$${capacity.toLocaleString("ru-RU")}` : "неизвестно"} · Идентичность: ${candidate.official_identity_confirmed ? "подтверждена" : "не подтверждена"}`));
    if (candidate.risk_flags?.length) main.append(el("p", "", `Риски: ${candidate.risk_flags.join(", ")}`)); row.append(main);
    row.append(el("div", "event-meta", age(candidate.as_of_ms))); list.append(row);
  }
}
async function refresh() {
  try {
    const response = await fetch("/api/snapshot", { cache: "no-store", signal: AbortSignal.timeout(8000) });
    if (!response.ok) throw Error(`HTTP ${response.status}`);
    const data = await response.json();
    markets = Array.isArray(data.markets) ? data.markets : [];
    $("market-count").textContent = String(markets.length);
    $("event-count").textContent = String(data.event_count ?? 0);
    $("updated").textContent = `Последнее обновление: ${new Date(data.as_of_ms).toLocaleTimeString("ru-RU")}`;
    $("connection").textContent = "СВЯЗЬ УСТАНОВЛЕНА"; $("connection").classList.remove("offline");
    $("system-status").textContent = "На связи"; $("system-note").textContent = markets.length ? "Данные доступны для просмотра" : "Ожидаем первый цикл анализа";
    if ($("forecast-horizon").value === "15m") { forecasts = data.forecasts || []; drawForecasts(); }
    else { await refreshForecasts(); }
    drawMarkets(); drawSignals(Array.isArray(data.signals) ? data.signals : []);
    drawPaperJournal(Array.isArray(data.paper_journal) ? data.paper_journal : []);
    drawEvents(Array.isArray(data.events) ? data.events : []);
    drawCandidates(Array.isArray(data.candidates) ? data.candidates : []);
  } catch {
    $("connection").textContent = "НЕТ СОЕДИНЕНИЯ"; $("connection").classList.add("offline");
    $("system-status").textContent = "Нет связи"; $("system-note").textContent = "Локальный анализатор недоступен";
    forecasts = []; drawForecasts(); drawSignals([]);
    $("forecast-status").textContent = "Нет связи: предыдущие прогнозы скрыты до восстановления данных.";
  }
}
$("market-search").addEventListener("input", drawMarkets);
$("market-filter").addEventListener("change", drawMarkets);
setInterval(() => { $("clock").textContent = new Date().toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" }); }, 1000);
setInterval(refresh, 20000);
refresh();

function drawForecasts() {
  const query = $("forecast-search").value.trim().toUpperCase();
  const selected = $("forecast-instrument").value;
  const select = $("forecast-instrument");
  const available = forecasts.filter(f => !query || `${f.base} ${f.instrument_id}`.toUpperCase().includes(query));
  select.replaceChildren(el("option", "", "Все инструменты")); select.firstChild.value = "";
  for (const f of available) { const option = el("option", "", `${f.base}/${f.quote} · ${f.venue} · ${f.market_type}`); option.value = f.instrument_id; select.append(option); }
  select.value = available.some(f => f.instrument_id === selected) ? selected : "";
  const rows = available.filter(f => (!select.value || f.instrument_id === select.value) && f.horizon === $("forecast-horizon").value);
  const list = $("forecast-list"); list.replaceChildren();
  $("forecast-status").textContent = forecasts.length ? `${forecasts.length} инструментов · ${forecasts.filter(f => f.eligible).length} кандидатов. Сильные сетапы отправляются сразу; обзор — раз в час.` : "Сканер собирает историю. Проверьте подключение бирж и дождитесь первого цикла.";
  if (!rows.length) { list.append(el("div", "empty", forecasts.length ? "Нет инструментов по запросу" : "Прогнозы ещё не готовы")); return; }
  for (const f of rows) {
    const actionable = f.eligible && Date.now() < Number(f.expires_at_ms);
    const row = el("article", `forecast-card ${actionable ? 'candidate' : ''}`);
    row.append(el("h3", "", `${f.base || f.instrument_id} · ${f.horizon}`), el("p", "", `${f.venue} · ${f.market_type} · ${f.quote}`));
    const p = f.probabilities;
    if (p) {
      const display = roundedProbabilities(p);
      const values = el("div", "probability-row");
      const bar = el("div", "probability-bar");
      for (const [key, cls] of [["LONG","long"],["SHORT","short"],["WAIT","wait"]]) {
        values.append(el("strong", cls, `${key} ${display[key]}%`));
        const part = el("span", cls); part.style.width = `${Math.max(0,Math.min(100,Number(p[key])))}%`; bar.append(part);
      }
      row.append(values,bar,el("p", "", `Эмпирическая оценка · ${f.sample_count} аналогов · не калибрована`));
      if (f.entry_reference && f.direction !== 'WAIT') row.append(el("p", "", `Вход ${price(f.entry_reference)} · стоп ${price(f.stop)} · цель ${price(f.target)}`));
    }
    row.append(el("p", "", actionable ? `Кандидат ${f.direction}: условия фильтра выполнены. Преимущество ещё проверяется.` : `Нет сигнала: ${(f.reasons || []).join('; ') || 'ожидаем свежую котировку'}`));
    row.append(el("p", "", `${f.instrument_id} · ${age(f.observed_at_ms)}`));
    list.append(row);
  }
}
async function refreshForecasts() {
  const request = ++forecastRequest;
  const horizon = $("forecast-horizon").value;
  forecasts = []; drawForecasts();
  $("forecast-status").textContent = "Обновляем оценки…";
  try {
    const response = await fetch(`/api/forecasts?horizon=${encodeURIComponent(horizon)}`, {cache:"no-store",signal:AbortSignal.timeout(8000)});
    if (!response.ok) throw Error(response.status);
    const data = await response.json();
    if (request !== forecastRequest || horizon !== $("forecast-horizon").value) return;
    forecasts = data.forecasts || []; drawForecasts();
  } catch { if (request === forecastRequest) { forecasts = []; drawForecasts(); $("forecast-status").textContent = "Не удалось получить прогнозы. Проверьте соединение."; } }
}
$("forecast-search").addEventListener("input", drawForecasts);
$("forecast-instrument").addEventListener("change", drawForecasts);
$("forecast-horizon").addEventListener("change", refreshForecasts);
