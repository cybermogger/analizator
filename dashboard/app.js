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
function tradePlan(f) {
  const plan=f.trade_plan;
  if(!plan)return null;
  const box=el('div','trade-plan');
  box.dataset.expires=String(plan.expires_at_ms);
  const valid=Date.now()<plan.expires_at_ms;
  box.append(el('strong','',`${plan.status==='SIGNAL' && valid?'План сигнала':'Условный план'} · ${plan.direction==='LONG'?'ЛОНГ':'ШОРТ'}`));
  const levels=el('dl','plan-levels');
  for(const [label,value] of [['Вход',plan.entry],['Стоп-лосс',plan.stop_loss],['Тейк-профит',plan.take_profit]]){
    const cell=el('div');cell.append(el('dt','',label),el('dd','',price(value)));levels.append(cell);
  }
  box.append(levels,el('p','',`${plan.quote} · риск ${plan.stop_distance_percent.toFixed(2)}% · цель +${plan.target_distance_percent.toFixed(2)}% · прибыль/риск ${plan.reward_risk.toFixed(2)}`));
  if(plan.leverage){
    box.append(el('p','plan-roi',`Плечо ${plan.leverage}× · цель на маржу +${plan.target_net_margin_percent.toFixed(2)}% чистыми · стоп −${plan.stop_net_margin_percent.toFixed(2)}%`));
    box.append(el('small','','Движение цены и доходность маржи — разные величины. Цель не гарантирована; плечо увеличивает и убыток.'));
  }
  box.append(el('p','',`С учётом расходов: ${plan.reward_risk_after_costs.toFixed(2)} · расходы ≈ ${plan.estimated_roundtrip_cost_percent.toFixed(2)}%`));
  box.append(el('p',`plan-expiry ${valid?'':'warning'}`,valid?`Вход до ${new Date(plan.expires_at_ms).toLocaleTimeString('ru-RU')} · горизонт ${f.horizon}`:'Котировка устарела — дождитесь обновления входа.'));
  if(plan.status!=='SIGNAL')box.append(el('small','','Условия входа не выполнены. Уровни показаны для сценария, торгового сигнала нет.'));
  return box;
}
function drawMarkets() {
  const query = $("market-search").value.trim().toUpperCase();
  const type = $("market-filter").value;
  const rows = $("market-rows"); rows.replaceChildren();
  let filtered = markets.filter(m => (!query || `${m.base} ${m.instrument_id}`.toUpperCase().includes(query)) && (type === "all" || (type === "perp" ? m.market_type !== "spot" : m.market_type === "spot")));
  const coinRows=new Map(); for(const item of filtered){ const saved=coinRows.get(item.base); if(!saved || item.instrument_id===preferredMarkets.get(item.base) || item.quote==='USDT' && saved.quote!=='USDT')coinRows.set(item.base,item); } filtered=[...coinRows.values()];
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
    row.append(el("div", "event-type", ({LISTING:'Листинг',DELISTING:'Делистинг',PROTOCOL_UPGRADE:'Обновление сети',HACK_EXPLOIT:'Инцидент безопасности',REGULATORY:'Регулирование',ETF:'ETF',MACRO:'Макроэкономика',PARTNERSHIP:'Партнёрство',OUTAGE:'Сбой',TOKEN_UNLOCK:'Разблокировка',BURN:'Сжигание',GOVERNANCE:'Управление'}[event.event_type] || 'Событие рынка')));
    const main = el("div", "event-main"); main.append(el("strong", "", event.summary_ru || "Перевод готовится…"));
    main.append(el("p", "", `${(event.assets || []).join(", ") || "Рынок в целом"} · ${(event.source_ids || []).join(", ") || "Источник не указан"}${event.contradiction_state && event.contradiction_state !== "none" ? ` · статус: ${event.contradiction_state}` : ""}`)); row.append(main);
    main.append(el('div','translation-label',event.translation_status==='ORIGINAL_RU'?'Оригинал на русском':event.summary_ru?'Машинный перевод · EN → RU':'Русский перевод пока недоступен'));
    const original=el('details','event-original');original.append(el('summary','','Оригинал'),el('p','',event.summary || ''));main.append(original);
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
    $("market-count").textContent = String(new Set(markets.map(m=>m.base)).size);
    $("event-count").textContent = String(data.event_count ?? 0);
    $("updated").textContent = `Последнее обновление: ${new Date(data.as_of_ms).toLocaleTimeString("ru-RU")}`;
    $("connection").textContent = "СВЯЗЬ УСТАНОВЛЕНА"; $("connection").classList.remove("offline");
    $("system-status").textContent = "На связи"; $("system-note").textContent = markets.length ? "Данные доступны для просмотра" : "Ожидаем первый цикл анализа";
    if ($("forecast-horizon").value === "15m") { forecasts = data.forecasts || []; drawForecasts(); }
    else { await refreshForecasts(); }
    if (workspaceTab === "best") await refreshBest();
    drawMarkets(); drawSignals(Array.isArray(data.signals) ? data.signals : []);
    drawPaperJournal(Array.isArray(data.paper_journal) ? data.paper_journal : []);
    drawEvents(Array.isArray(data.events) ? data.events : []);
    drawCandidates(Array.isArray(data.candidates) ? data.candidates : []);
  } catch {
    $("connection").textContent = "НЕТ СОЕДИНЕНИЯ"; $("connection").classList.add("offline");
    $("system-status").textContent = "Нет связи"; $("system-note").textContent = "Локальный анализатор недоступен";
    forecasts = []; drawForecasts(); drawSignals([]);
    $('best-content').replaceChildren(el('div','empty','Нет связи. Предыдущий сигнал скрыт до восстановления свежих данных.'));
    $("forecast-status").textContent = "Нет связи: предыдущие прогнозы скрыты до восстановления данных.";
  }
}
$("market-search").addEventListener("input", drawMarkets);
$("market-filter").addEventListener("change", drawMarkets);
setInterval(() => { $("clock").textContent = new Date().toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" }); }, 1000);
setInterval(()=>document.querySelectorAll('.trade-plan[data-expires]').forEach(box=>{
  if(Date.now()>=Number(box.dataset.expires)){
    const label=box.querySelector('.plan-expiry');label.textContent='Котировка устарела — дождитесь обновления входа.';label.classList.add('warning');
    box.classList.add('plan-expired');
  }
}),1000);
setInterval(refresh, 20000);
refresh();

let marketView = 'all';
const preferredMarkets = new Map();
let workspaceTab='market';
let lastBestKey='';
let bestRequest=0;
const seenCards = new Set();
let favorites;
try { favorites = new Set(JSON.parse(localStorage.getItem('analizator.favorites') || '[]')); } catch { favorites = new Set(); }
const venueName = name => ({'binance-usdm':'Binance Futures','binance-spot':'Binance','coinbase-advanced':'Coinbase','hyperliquid':'Hyperliquid','kraken-spot':'Kraken'}[name] || name);
function sparkline(values) {
  if (!values || values.length < 2) return null;
  const svg = document.createElementNS('http://www.w3.org/2000/svg','svg');
  svg.setAttribute('viewBox','0 0 280 64'); svg.setAttribute('role','img'); svg.setAttribute('aria-label','История закрытых 5-минутных свечей'); svg.classList.add('sparkline');
  const min = Math.min(...values), max = Math.max(...values), range = max-min || 1;
  const points = values.map((v,i)=>`${i*280/(values.length-1)},${56-(v-min)*48/range}`).join(' ');
  const area = document.createElementNS(svg.namespaceURI,'polygon'); area.setAttribute('points',`0,64 ${points} 280,64`); area.classList.add('spark-area');
  const line = document.createElementNS(svg.namespaceURI,'polyline'); line.setAttribute('points',points); line.classList.add('spark-line');
  svg.append(area,line); return svg;
}
function brief(f, actionable) {
  if (actionable) return ['candidate','Кандидат '+f.direction,'Условия фильтров выполнены'];
  if (!f.probabilities) return ['warming',f.data_reason === 'latest_closed_candle_interval_is_old' ? 'Обновляем свечи' : 'Собираем историю',`${f.history_count || 0} закрытых свечей · нужно 160`];
  if ((f.reasons || []).some(r=>r.includes('котировки') || r.includes('Источник'))) return ['warming','Обновляем котировку','Оценка есть, свежие условия входа пока недоступны'];
  if (f.direction === 'WAIT') return ['neutral','Ждём условия для сделки','TP до стопа в заданный срок не подтверждён'];
  if (f.direction === 'SHORT' && f.market_type === 'spot') return ['neutral','Медвежий уклон','Для SHORT выбери фьючерсный инструмент'];
  return ['neutral',f.direction === 'LONG' ? 'Бычий уклон' : 'Медвежий уклон','Для входа пока недостаточно подтверждений'];
}
function drawForecasts() {
  const query = $('forecast-search').value.trim().toUpperCase();
  const select = $('forecast-instrument'), selected = select.value;
  const matching = forecasts.filter(f => !query || `${f.base} ${f.instrument_id}`.toUpperCase().includes(query));
  select.replaceChildren(el('option','','Все инструменты')); select.firstChild.value = '';
  for (const f of matching) { const option=el('option','',`${f.base}/${f.quote} · ${venueName(f.venue)} · ${f.market_type === 'spot' ? 'Спот' : 'Фьючерсы'}`); option.value=f.instrument_id; select.append(option); }
  select.value = matching.some(f=>f.instrument_id===selected) ? selected : '';
  const isReady = f => f.eligible && Date.now() < Number(f.expires_at_ms);
  let rows = matching.filter(f=>(!select.value || f.instrument_id===select.value) && f.horizon===$('forecast-horizon').value &&
    (marketView==='all' || marketView===f.market_type || marketView==='favorites' && favorites.has(f.instrument_id) || marketView==='ready' && isReady(f)));
  if (!select.value) {
    const groups=new Map();
    for (const f of rows) { const key=String(f.base).toUpperCase(); if (!groups.has(key)) groups.set(key,[]); groups.get(key).push(f); }
    rows=[...groups.values()].map(items=>{
      items.sort((a,b)=>(Number(Boolean(b.probabilities))-Number(Boolean(a.probabilities))) || (Number(b.market_type==='perpetual')-Number(a.market_type==='perpetual')) || (Number(b.quote==='USDT')-Number(a.quote==='USDT')) || a.instrument_id.localeCompare(b.instrument_id));
      return items.find(f=>f.instrument_id===preferredMarkets.get(items[0].base)) || items[0];
    });
  }
  if ($('forecast-sort').value==='name') rows.sort((a,b)=>String(a.base).localeCompare(String(b.base)));
  const list=$('forecast-list');
  const expanded = new Set([...list.querySelectorAll('details[open]')].map(x=>x.dataset.identity));
  list.replaceChildren();
  $('forecast-status').textContent=forecasts.length ? `${rows.length} инструментов в обзоре · ${forecasts.filter(f=>f.probabilities).length} оценок готовы · ${forecasts.filter(isReady).length} кандидатов` : 'Получаем рыночные данные…';
  if (!rows.length) { list.append(el('div','empty',forecasts.length ? 'В этом списке пока нет инструментов. Измени поиск или фильтр.' : 'Загружаем историю с бирж. Оценки появятся после первого цикла.')); return; }
  rows.forEach((f,index)=>{
    const actionable=isReady(f), state=brief(f,actionable), key=f.instrument_id+'|'+f.horizon;
    const row=el('article',`forecast-card ${state[0]}`); row.dataset.identity=key;
    if (!seenCards.has(key)) { row.classList.add('enter'); row.style.animationDelay=`${Math.min(index,8)*35}ms`; seenCards.add(key); }
    const head=el('div','coin-head'), name=el('div','coin-name');
    name.append(el('span','coin-icon',String(f.base || '?').slice(0,2)),el('h3','',f.base || 'Актив'));
    const star=el('button','favorite',favorites.has(f.instrument_id)?'★':'☆'); star.type='button'; star.setAttribute('aria-label',`${favorites.has(f.instrument_id)?'Убрать из':'Добавить в'} избранное ${f.base}`); star.setAttribute('aria-pressed',String(favorites.has(f.instrument_id)));
    star.addEventListener('click',()=>{ favorites.has(f.instrument_id)?favorites.delete(f.instrument_id):favorites.add(f.instrument_id); try { localStorage.setItem('analizator.favorites',JSON.stringify([...favorites])); } catch {} drawForecasts(); });
    head.append(name,star); row.append(head,el('p','venue-line',`${venueName(f.venue)} · ${f.market_type==='spot'?'Спот':'Фьючерсы'} · ${f.quote} · ${f.horizon}`));
    const alternatives=matching.filter(item=>item.base===f.base && (marketView==='all' || marketView==='favorites' || marketView==='ready' || item.market_type===marketView));
    if (alternatives.length>1 && !select.value) {
      const picker=el('select','venue-picker'); picker.setAttribute('aria-label',`Выбрать рынок ${f.base}`);
      for (const item of alternatives) { const option=el('option','',`${venueName(item.venue)} · ${item.market_type==='spot'?'Спот':'Фьючерсы'} · ${item.quote}`);option.value=item.instrument_id;picker.append(option); }
      picker.value=f.instrument_id;picker.addEventListener('change',()=>{preferredMarkets.set(f.base,picker.value);drawForecasts();});row.append(picker);
    }
    const chart=sparkline(f.sparkline); if (chart) row.append(chart);
    if (f.probabilities) {
      const display=roundedProbabilities(f.probabilities), values=el('div','probability-row'), bar=el('div','probability-bar');
      for (const [key,cls,label] of [['LONG','long','Лонг'],['SHORT','short','Шорт'],['WAIT','wait','Ждать']]) {
        const cell=el('div',cls); cell.append(el('span','',label),el('strong','',`${display[key]}%`)); values.append(cell);
        const part=el('span',cls); part.style.width=`${f.probabilities[key]}%`; bar.append(part);
      }
      row.append(values,bar);
    } else {
      const progress=el('progress','history-progress'); progress.max=160; progress.value=Math.min(160,f.history_count||0); progress.setAttribute('aria-label','Готовность истории'); row.append(progress);
    }
    const badge=el('div','scenario'); badge.append(el('span','scenario-dot'),el('strong','',state[1])); row.append(badge,el('p','scenario-summary',state[2]));
    const plan=tradePlan(f);if(plan)row.append(plan);
    const details=el('details','forecast-details'); details.dataset.identity=key; details.open=expanded.has(key);
    details.append(el('summary','','Разбор оценки'));
    const reasons=el('ul'); for (const reason of f.reasons || []) reasons.append(el('li','',reason)); details.append(reasons);
    details.append(el('p','',f.probabilities ? `${f.sample_count} аналогов. Эмпирическая оценка, калибровка не подтверждена.` : (f.reasons || []).join('; ')),el('p','instrument-id',f.instrument_id));
    row.append(details,el('div','card-meta',`${age(f.observed_at_ms)} · ${f.horizon}`)); list.append(row);
  });
}
async function refreshForecasts() {
  const request = ++forecastRequest;
  const horizon = $("forecast-horizon").value;
  if (forecasts.some(f => f.horizon !== horizon)) { forecasts = []; drawForecasts(); }
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

document.querySelectorAll('[data-filter]').forEach(button=>button.addEventListener('click',()=>{
  marketView=button.dataset.filter;
  document.querySelectorAll('[data-filter]').forEach(item=>{item.classList.toggle('selected',item===button);item.setAttribute('aria-pressed',String(item===button));});
  drawForecasts();
}));
$('forecast-sort').addEventListener('change',drawForecasts);
window.addEventListener('scroll',()=>{
  const height=document.documentElement.scrollHeight-window.innerHeight;
  $('reading-progress').style.transform=`scaleX(${height>0?window.scrollY/height:0})`;
},{passive:true});
if ('IntersectionObserver' in window) {
  const observer=new IntersectionObserver(entries=>entries.forEach(entry=>{
    if (entry.isIntersecting) document.querySelectorAll('.nav-link').forEach(link=>link.classList.toggle('active',link.hash==='#'+entry.target.id));
  }),{rootMargin:'-15% 0px -65% 0px'});
  document.querySelectorAll('main[id],section[id]').forEach(section=>observer.observe(section));
}

async function refreshBest() {
  const host=$('best-content'),request=++bestRequest;
  try {
    const response=await fetch('/api/best',{cache:'no-store',signal:AbortSignal.timeout(12000)});
    if(!response.ok) throw Error(response.status);
    const data=await response.json();if(request!==bestRequest)return;host.replaceChildren();
    if(!data.leader){host.append(el('div','empty','Нет свежих оценок. Лидер появится после обновления котировок.'));return;}
    const f=data.leader, side=f.leading_side==='LONG'?'ЛОНГ':'ШОРТ';
    const key=f.instrument_id+'|'+f.horizon+'|'+f.leading_side;
    const box=el('article','best-leader'+(lastBestKey && key!==lastBestKey?' best-updated':''));
    const title=el('div','best-title');title.append(el('h2','',f.base+' · '+side),el('span','',f.horizon+' · '+venueName(f.venue)+' · '+f.quote));box.append(title,el('div','best-number',Number(f.leading_percent).toFixed(1)+'%'));
    box.append(el('p','',`LONG ${f.probabilities.LONG.toFixed(1)}% · SHORT ${f.probabilities.SHORT.toFixed(1)}% · WAIT ${f.probabilities.WAIT.toFixed(1)}%`));
    box.append(el('p',f.eligible?'':'warning',f.eligible && Date.now()<Number(f.expires_at_ms)?'Условия входа выполнены · экспериментальный кандидат':'Лидер по проценту, но вход не подтверждён: '+(f.reasons || []).join('; ')));
    const plan=tradePlan(f);if(plan)box.append(plan);
    const chart=sparkline(f.sparkline);if(chart)box.append(chart);
    box.append(el('p','',key!==lastBestKey&&lastBestKey?'Появился новый лидер.':'Лидер пересчитывается при каждом обновлении данных.'));host.append(box);lastBestKey=key;
    const peers=el('div','best-comparison');
    for(const runner of data.runners_up){
      const card=el('div','best-runner');
      card.append(el('strong','',`${runner.base} · ${runner.leading_side} ${runner.leading_percent.toFixed(1)}%`),el('span','',`${runner.horizon} · ${venueName(runner.venue)} · ${runner.quote}`));
      const plan=tradePlan(runner);
      if(plan){const details=el('details');details.append(el('summary','','Уровни и условия'),plan);for(const reason of runner.reasons || [])details.append(el('p','',reason));card.append(details);}
      peers.append(card);
    }
    host.append(peers,el('p','',data.semantics));
  }catch{host.replaceChildren(el('div','empty','Не удалось обновить лидера. Предыдущая оценка скрыта.'));}
}
async function refreshX(){
  try{const response=await fetch('/api/x-status',{cache:'no-store',signal:AbortSignal.timeout(8000)});if(!response.ok)throw Error(response.status);const data=await response.json();
    $('x-state').textContent=(data.status==='healthy'?'X API подключён. ':'X API не подключён: требуется доступ и Bearer Token. ')+data.note;
    const host=$('x-accounts');host.replaceChildren();for(const account of data.accounts){const link=el('a','x-account','@'+account.handle);link.href=account.profile_url;link.target='_blank';link.rel='noopener noreferrer';link.append(el('small','',account.category));host.append(link);}
  }catch{$('x-state').textContent='Не удалось проверить подключение X.';}
}
function researchTable(labels,rows){
  const wrap=el('div','table-scroll'), table=el('table'), header=el('tr');
  for(const label of labels)header.append(el('th','',label));
  table.append(header);
  for(const values of rows){
    const row=el('tr');
    for(const value of values)row.append(el('td','',String(value)));
    table.append(row);
  }
  wrap.append(table);
  return wrap;
}

function renderChallenge(host,report){
  const num=(n,d=2)=>n==null?'—':Number(n).toLocaleString('ru-RU',{minimumFractionDigits:d,maximumFractionDigits:d});
  const pct=n=>n==null?'—':num(n)+'%';
  const price=n=>Number(n).toLocaleString('ru-RU',{maximumSignificantDigits:8});
  const names={sweep:'Отбой от экстремума',relative_reversal:'Возврат к BTC',breakout:'Пробой',pullback:'Откат по тренду',ridge:'Ридж-регрессия',boost:'Градиентный бустинг',flow_boost:'Бустинг с потоком заявок',absorption:'Поглощение агрессивных сделок',flow_trend:'Импульс агрессивных сделок',crowded_reversal:'Разворот по funding и отклонению от BTC'};
  const list=(p,s)=>[p,s.trades,pct(s.net_winrate_percent),num(s.profit_factor),pct(s.net_equity_return_percent),pct(s.max_closed_equity_drawdown_percent)];
  host.append(el('h3','',names[report.selected.name]||report.selected.name),
    el('p','',`Сравнили ${report.candidate_count} вариантов. Отбор по сентябрю и октябрю. Независимая проверка с 1 по 24 ноября 2026 года, Binance USD-M, плечо 5×, удержание до двух часов.`),
    researchTable(['Период','Сделок','Винрейт','Profit factor','Доход счёта','Просадка*'],[
      list('Сентябрь · отбор',report.selected.validation.September),list('Октябрь · отбор',report.selected.validation.October),
      list('Ноябрь · тест',report.stats),list('Ноябрь · двойные издержки',report.double_costs)]),
    el('p','',`В ноябре сделки появились в ${report.active_days} из 24 дней. Тейк достигнут в ${pct(report.stats.tp_hit_percent)} сделок. Средний результат на маржу ${pct(report.stats.mean_net_margin_percent)}.`));
  const journal=el('details');
  journal.append(el('summary','',`Журнал ноябрьского теста · ${report.fills.length} сделок`),
    researchTable(['Вход, UTC','Контракт','Сторона','Вход','Стоп','Тейк','Выход','Причина','Издержки¹','Нетто на маржу'],report.fills.map(r=>[
      new Date(r.entry_at_ms).toISOString().slice(5,16).replace('T',' '),r.symbol,r.direction,price(r.entry),price(r.stop_loss),
      price(r.take_profit),price(r.exit),r.exit_reason,pct((r.fees+r.spread+r.slippage+r.funding)*100),pct(r.net_return*500)])),
    el('p','','¹ На номинал позиции за вход и выход, включая спред, проскальзывание и funding.'));
  const variants=el('details');
  variants.append(el('summary','',`Все ${report.candidate_count} вариантов на отборе`),researchTable(
    ['Модель','Вход','Порог отбора²','Фильтр','Сентябрь · сделки / доход','Октябрь · сделки / доход'],
    report.comparison.map(c=>[names[c.name]||c.name,c.limit_depth?'Лимит от '+pct(c.limit_depth*100):'Рыночный',
      pct(c.quantile*100),c.regime==='active'?'Объём и волатильность':'Все режимы',...c.periods.map(s=>s.trades+' / '+pct(s.return))])),
    el('p','','² Процентиль оценки на августовских данных. Высокий процентиль означает редкость условия.'));
  host.append(journal,variants,el('h3','','Предыдущая проверка ближайших аналогов'));
}

async function refreshAudit(){
  const host=$('audit-content');
  try{
    const response=await fetch('/api/signal-audit',{cache:'no-store',signal:AbortSignal.timeout(8000)});
    if(!response.ok)throw Error(response.status);const data=await response.json();host.replaceChildren();
    if(!data.test){host.append(el('p','',data.note||'Проверка ещё выполняется.'));return;}
    if(data.research_challenge)renderChallenge(host,data.research_challenge);
    host.append(el('p','',`${data.period} · ${data.symbols.length} монет · плечо ${data.costs.leverage}×`));
    host.append(el('p',data.promotion_passed?'':'warning',data.promotion_passed?'Проверка пройдена; требуется дальнейшее наблюдение на новых данных.':'Вне обучения преимущество не подтверждено. Автоматические торговые сигналы заблокированы, условные планы остаются доступны.'));
    const table=el('table'),head=el('tr');for(const label of ['Стратегия','Сделок','Винрейт, нетто','Profit factor','Доход счёта','Просадка*'])head.append(el('th','',label));table.append(head);
    const number=(value,digits=2)=>value==null?'—':Number(value).toFixed(digits);
    for(const [name,row]of Object.entries(data.test)){
      const tr=el('tr');for(const value of [name,row.trades,number(row.net_winrate_percent)+'%',number(row.profit_factor),number(row.net_equity_return_percent)+'%',number(row.max_closed_equity_drawdown_percent)+'%'])tr.append(el('td','',value));table.append(tr);
    }
    const scroll=el('div','table-scroll');scroll.append(table);host.append(scroll);
    const stress=data.test_double_costs[data.selected_profile];host.append(el('p','',`Выбран по сентябрю: ${data.selected_profile}. Октябрь не использовался для выбора. Удвоенные расходы: ${number(stress.net_equity_return_percent)}% счёта.`));
    host.append(el('p','','* Просадка по закрытым сделкам; плавающая просадка может быть выше. Максимум 4 позиции, риск 0,25% счёта на сделку. Это симуляция, не результат реальной торговли.'));
    const details=el('details');details.append(el('summary','','Допущения и ограничения'));for(const assumption of data.assumptions)details.append(el('p','',assumption));host.append(details);
  }catch{host.replaceChildren(el('p','warning','Не удалось загрузить историческую проверку.'));}
}
function changeWorkspace(value){workspaceTab=value;document.querySelectorAll('[data-tab]').forEach(button=>button.setAttribute('aria-selected',String(button.dataset.tab===value)));const panels={best:'best-panel',x:'x-panel',audit:'audit-panel'};document.querySelectorAll('main > section').forEach(section=>{section.hidden=value==='market'?Object.values(panels).includes(section.id):section.id!==panels[value];});if(value==='best')refreshBest();if(value==='x')refreshX();if(value==='audit')refreshAudit();}
document.querySelectorAll('[data-tab]').forEach(button=>button.addEventListener('click',()=>changeWorkspace(button.dataset.tab)));
document.querySelectorAll('.nav-link').forEach(link=>link.addEventListener('click',()=>changeWorkspace('market')));
