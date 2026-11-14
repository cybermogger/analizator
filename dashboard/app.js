const $ = (id) => document.getElementById(id);
let markets = [];
const el = (tag, className, value) => { const node = document.createElement(tag); if (className) node.className = className; if (value != null) node.textContent = String(value); return node; };
const age = (ms) => { if (!Number.isFinite(Number(ms))) return "—"; const delta = Math.max(0, Date.now() - Number(ms)); return delta < 60000 ? "только что" : delta < 3600000 ? `${Math.floor(delta / 60000)} мин назад` : `${Math.floor(delta / 3600000)} ч назад`; };
const price = (value) => { const n = Number(value); if (!Number.isFinite(n) || n <= 0) return "—"; return n.toLocaleString("ru-RU", { maximumFractionDigits: n < .01 ? 8 : n < 1 ? 5 : 2 }); };
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
    if (url) { const link = el("a", "", "Первоисточник ↗"); link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer"; meta.append(link); }
    row.append(meta); list.append(row);
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
    drawMarkets(); drawEvents(Array.isArray(data.events) ? data.events : []);
    drawCandidates(Array.isArray(data.candidates) ? data.candidates : []);
  } catch {
    $("connection").textContent = "НЕТ СОЕДИНЕНИЯ"; $("connection").classList.add("offline");
    $("system-status").textContent = "Нет связи"; $("system-note").textContent = "Локальный анализатор недоступен";
  }
}
$("market-search").addEventListener("input", drawMarkets);
$("market-filter").addEventListener("change", drawMarkets);
setInterval(() => { $("clock").textContent = new Date().toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" }); }, 1000);
setInterval(refresh, 20000);
refresh();
