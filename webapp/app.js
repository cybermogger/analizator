(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  };
  const fmtTime = value => {
    if (!value) return 'время неизвестно';
    const date = new Date(Number(value));
    return Number.isNaN(date.getTime()) ? 'время неизвестно' : new Intl.DateTimeFormat('ru-RU', {hour:'2-digit', minute:'2-digit', day:'2-digit', month:'short', timeZone:'UTC'}).format(date) + ' UTC';
  };
  const num = (value, digits = 2) => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value)) ? new Intl.NumberFormat('ru-RU', {maximumFractionDigits:digits}).format(Number(value)) : '—';
  const usd = value => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value)) ? '$' + num(value, 2) : '—';
  const age = ms => ms !== null && ms !== undefined && ms !== '' && Number.isFinite(Number(ms)) ? `${num(Math.max(0, Number(ms))/1000, 1)} с назад` : 'нет данных';
  const pill = (label, kind = '') => el('span', `pill ${kind}`, label);
  const empty = message => el('div', 'empty', message);
  const rows = (container, items, renderer, emptyMessage) => {
    container.replaceChildren();
    if (!items || !items.length) container.append(empty(emptyMessage));
    else items.forEach(item => container.append(renderer(item)));
  };
  const statusKind = status => ['healthy','fresh','ready','verified','healthy'].includes(String(status).toLowerCase()) ? 'good' : ['degraded','stale','warning','not_available'].includes(String(status).toLowerCase()) ? 'warn' : ['disabled','unavailable','failed','rejected'].includes(String(status).toLowerCase()) ? 'bad' : '';
  const signalClass = d => d === 'LONG' ? 'long' : d === 'SHORT' ? 'short' : '';
  const freshnessText = value => {
    const f=value||{};
    const part=(label,key)=>`${label} ${f[key]==null?'—':`${num(f[key],1)} с`}`;
    return `${String(f.status||'UNKNOWN')} · ${part('context','context_receive_age_seconds')} · ${part('book','book_receive_age_seconds')} · ${part('candle','candle_receive_age_seconds')}`;
  };

  function renderAssetCard(signal, truth) {
    const card = el('article', 'card asset-card');
    const top = el('div', 'asset-top');
    top.append(el('span', 'asset-name', signal.asset), el('span', `direction ${signalClass(signal.direction)}`, signal.direction || 'WAIT'));
    card.append(top);
    card.append(el('div', 'price', signal.price ? usd(signal.price) : '—'));
    card.append(el('div', 'subtle', `Hyperliquid mark · горизонт ${signal.horizon || '—'} · снимок ${fmtTime(signal.analysis_at_ms)}`));
    const prob = el('div', 'prob-row');
    prob.append(el('span', 'prob', `${num(signal.probability_percent, 1)}%`), el('span', 'prob-label', 'оценка направления'));
    card.append(prob);
    const meter = el('div', 'meter');
    const activeBars = Math.round(Math.max(0, Math.min(100, Number(signal.probability_percent) || 0)) / 5);
    for (let index=0; index<20; index+=1) meter.append(el('i', index<activeBars?'active':''));
    card.append(meter);
    const freshness = signal.freshness || {};
    const meta = el('div', 'asset-meta');
    meta.append(el('span', '', `Freshness: ${freshnessText(freshness)}`));
    meta.append(el('span', '', `Multi-venue: ${truth ? truth.status : 'нет truth'} · ${truth && truth.quorum != null ? `quorum ${truth.quorum}/${truth.required_quorum}` : 'не рассчитан'}`));
    if (truth) card.append(el('div','meta-line',`Venue divergence ${num(truth.venue_divergence_bps,1)} bps · basis ${num(truth.cross_exchange_basis_bps,1)} bps · обновлено ${fmtTime(truth.calculated_at_ms)}${truth.reason?` · ${truth.reason}`:''}`));
    card.append(el('div','meta-line',`Multi-venue canonical price: ${truth?.price == null ? 'не опубликована (quorum/freshness gate)' : usd(truth.price)} · источник оценки: независимые свежие venues`));
    card.append(meta);
    card.append(el('div', 'meta-line', `Уверенность ${num(signal.confidence_percent, 1)}% · режим ${signal.regime} · модель ${signal.model_status} (${signal.model_version})`));
    card.append(el('div', 'detail-line', signal.explanation));
    return card;
  }

  function dataCard(title, badge, details, time) {
    const card = el('article', 'card data-card');
    const header = el('div', 'row-head'); header.append(el('div','row-title',title), pill(badge, statusKind(badge))); card.append(header);
    details.filter(Boolean).forEach(text => card.append(el('div','detail-line',text)));
    if (time) card.append(el('div','meta-line',time));
    return card;
  }

  function renderSignal(item) {
    const probs = item.probabilities || {};
    return dataCard(`${item.asset} · ${item.direction}`, `${num(item.probability_percent,1)}%`, [
      `Цена: ${usd(item.price)} · горизонт: ${item.horizon} · режим: ${item.regime}`,
      `LONG ${num(probs.LONG,1)}% · SHORT ${num(probs.SHORT,1)}% · WAIT ${num(probs.WAIT,1)}%`,
      `Freshness: ${freshnessText(item.freshness)} · confidence ${num(item.confidence_percent,1)}% · ${item.model_status} ${item.model_version}`,
      item.explanation
    ], `Снимок обновлён: ${fmtTime(item.analysis_at_ms)}`);
  }
  function renderEvent(item) {
    const impacted = (item.impacted_assets || []).map(x => x.canonical_asset_id || [x.chain_id,x.contract_address].filter(Boolean).join(':')).filter(Boolean).join(', ') || 'актив не установлен';
    return dataCard(item.event_type || 'Событие', `${item.credibility ?? 'credibility н/д'} · ${item.confirmation_count} подтвержд.`, [
      item.summary || 'Описание отсутствует.',
      `Активы: ${impacted} · направление: ${item.expected_direction} · противоречия: ${item.contradiction_state}`,
      `Новизна ${item.novelty ?? 'н/д'} · confidence ${item.confidence ?? 'н/д'} · риск подлинности ${item.authenticity_risk ? 'есть' : 'не отмечен'}`,
      `Источники: ${(item.source_ids || []).join(', ') || 'нет'} · публикация ${fmtTime(item.published_at_ms)}`,
      `Evidence URLs: ${(item.evidence_urls || []).join(' · ') || 'не сохранены'}`
    ], `Получено: ${fmtTime(item.received_at_ms)}`);
  }
  function renderCandidate(item) {
    const identity = [item.chain_id, item.contract_address].filter(Boolean).join(' · ') || 'точная идентичность отсутствует';
    return dataCard(`${item.name || item.symbol || 'On-chain кандидат'}${item.symbol ? ` · ${item.symbol}` : ''}`, item.status, [
      `Идентичность: ${identity}`,
      `Pool: ${item.pool_provider || 'не установлен'} · ${item.pool_id || 'pool ID отсутствует'}`,
      `Ликвидность ${usd(item.liquidity_usd)} · доказанная sellable capacity ${usd(item.sellable_capacity_usd)} · top-10 holders ${item.top10_holder_percent == null ? 'NOT_AVAILABLE' : `${num(item.top10_holder_percent,1)}%`}`,
      `Risk flags: ${(item.risk_flags || []).join(', ') || 'не указаны'} · автоматическая покупка отключена`,
      `Invalidation: ${(item.invalidation || []).join(', ') || 'нет данных'} · evidence IDs: ${(item.evidence_event_ids || []).join(', ') || 'нет данных'}`,
      item.paper_exit_quote ? `Paper exit quote: ${usd(item.paper_exit_quote.notional_usd)} вход / ${usd(item.paper_exit_quote.net_output_usd)} выход · slippage ${num(item.paper_exit_quote.effective_slippage_bps,1)} bps` : 'Paper exit quote: NOT_AVAILABLE'
    ], `Оценка: ${fmtTime(item.as_of_ms)}`);
  }
  function renderRecommendation(item) {
    return dataCard(`${item.asset_id || item.instrument_id} · ${item.direction}`, item.status, [
      `Вероятность ${num(item.probability_percent,1)}% · горизонт ${item.horizon} · ${item.model} ${item.model_version}`,
      `Paper notional ${usd(item.paper_notional_usd)} · ожидаемое движение ${item.expected_net_move == null ? '—' : `${num(Number(item.expected_net_move)*100,2)}%`} · свежесть ${age(item.freshness_seconds == null ? null : item.freshness_seconds*1000)}`,
      `Риски: ${(item.risk_reasons || []).join(', ') || 'нет дополнительных причин'}`,
      `Поддерживает: ${(item.supporting_evidence || []).map(x => x.summary).join(' · ') || 'нет подтверждённых факторов'}`,
      `Противоречит: ${(item.contradicting_evidence || []).map(x => x.summary).join(' · ') || 'нет данных'}`
    ], `Решение: ${fmtTime(item.updated_at_ms)}`);
  }
  function renderSource(name, item, context) {
    const last = item.last_received_at_ms || item.last_successful_poll_at_ms;
    const capabilities = item.not_available?.length ? ` · недоступно: ${item.not_available.join(', ')}` : '';
    const enabled = item.enabled === false ? ' · отключён' : '';
    return dataCard(name, String(item.status || 'unknown').toUpperCase(), [
      `${context || ''}${enabled}${capabilities}`,
      `Задержка: ${item.lag_ms == null ? 'неизвестна' : `${num(item.lag_ms/1000,1)} с`} · ошибок подряд: ${item.consecutive_failures ?? 'н/д'}`,
      item.reason ? `Причина: ${item.reason}` : ''
    ], `Последнее получение: ${fmtTime(last)}`);
  }

  function render(snapshot) {
    const signals = snapshot.assets || [];
    const truth = snapshot.market_truth || {};
    const truthFor = asset => {
      const id = asset === 'SOL' ? 'native:solana' : asset === 'HYPE' ? 'native:hyperliquid' : '';
      return truth[id];
    };
    const assets = $('#overview-assets'); assets.replaceChildren();
    for (const name of ['SOL','HYPE']) {
      const item = signals.find(x => x.asset === name);
      if (item) assets.append(renderAssetCard(item, truthFor(name)));
      else assets.append(dataCard(name, 'НЕТ СНИМКА', ['Сохранённый анализ ещё отсутствует. Показатели не подменяются примерными значениями.'], 'Обновление: нет данных'));
    }
    $('#snapshot-time').textContent = fmtTime(snapshot.updated_at_ms);
    const healthy = Boolean(snapshot.service?.websocket_connected);
    $('#service-dot').className = 'dot dot-good';
    $('#service-label').textContent = 'API отвечает';
    $('#ws-badge').textContent = healthy ? 'WS CONNECTED' : 'WS DISCONNECTED';
    $('#ws-badge').className = `badge ${healthy ? 'pill good' : 'pill warn'}`;
    const db = snapshot.service?.database || {};
    const serviceCard = $('#service-card'); serviceCard.replaceChildren();
    [["Hyperliquid WebSocket",healthy?'CONNECTED':'DISCONNECTED'],["Рынок обновлён",fmtTime(snapshot.service?.last_market_update_ms)],["SQLite",`${db.integrity || 'unknown'} · schema ${db.schema_version ?? '—'}`],["База",`${db.bytes == null ? 'размер неизвестен' : `${num(db.bytes/1024/1024,1)} MB`}`],["Режим",'read-only / shadow'],["Multi-venue",`${Object.values(truth).filter(x=>x.status==='healthy').length} healthy / ${Object.keys(truth).length} активов`]].forEach(([label,value])=>{const c=el('div','service-cell');c.append(el('small','',label),el('b','',value));serviceCard.append(c);});
    const p=snapshot.paper||{}; const summary=$('#paper-summary'); summary.replaceChildren();
    const left=el('div');left.append(el('div','eyebrow','VIRTUAL EQUITY'),el('div','summary-balance',usd(p.liquidation_equity_usd ?? p.initial_equity_usd)),el('div','subtle',`Обновлено ${fmtTime(p.updated_at_ms)} · сверка ${p.reconciles?'OK':'ошибка'}`));
    const right=el('div','summary-items');[["Позиции",p.open_position_count??0],["Net realized",usd(p.net_realized_pnl_usd)],["Решения",(p.decisions||[]).length]].forEach(([k,v])=>{const x=el('div');x.append(el('small','',k),el('b','',v));right.append(x);});summary.append(left,right);
    rows($('#signals-list'),signals,renderSignal,'Сохранённых signal snapshots нет.');
    rows($('#events-list'),snapshot.events,renderEvent,'Сохранённых событий пока нет. Состояние источников показано в разделе «Источники».');
    rows($('#candidates-list'),snapshot.onchain_candidates,renderCandidate,'On-chain кандидаты отсутствуют; это не означает, что токен безопасен.');
    const metrics=$('#portfolio-metrics');metrics.replaceChildren();[["Equity",usd(p.liquidation_equity_usd)],["Cash",usd(p.cash_usd)],["Realized net",usd(p.net_realized_pnl_usd)],["Unrealized net",usd(p.net_unrealized_pnl_usd)],["Fees",usd(p.fees_usd)],["Funding",usd(p.funding_usd)],["Slippage",usd(p.slippage_usd)],["Сверка",p.reconciles?'OK':'ошибка']].forEach(([label,value])=>{const m=el('div','metric');m.append(el('small','',label),el('b','',value));metrics.append(m);});
    rows($('#positions-list'),p.positions,(item)=>dataCard(`${item.asset_id} · ${item.direction}`,'OPEN',[`Вход ${usd(item.entry_price)} · notional ${usd(item.notional_usd)} · quantity ${num(item.quantity,6)}`],`Открыта ${fmtTime(item.opened_at_ms)}`),'Открытых paper-позиций нет.');
    rows($('#recommendations-list'),p.decisions,renderRecommendation,'Сохранённых paper-рекомендаций или NO_TRADE-решений пока нет.');
    rows($('#market-sources'),Object.entries(snapshot.providers||{}).map(([name,item])=>({name,item})),x=>renderSource(x.name,x.item,'Публичные market data'),'Market provider status отсутствует.');
    rows($('#event-sources'),Object.entries(snapshot.event_sources||{}).map(([name,item])=>({name,item})),x=>renderSource(x.name,x.item,`Event feed · ${x.item.official?'official':'не помечен official'}`),'Event sources не настроены.');
    rows($('#onchain-sources'),Object.entries(snapshot.onchain_sources||{}).map(([name,item])=>({name,item})),x=>renderSource(x.name,x.item,`Chain ${x.item.chain_id ?? '—'}`),'On-chain providers не настроены.');
    $('#updated-at').textContent=`Снимок API · ${fmtTime(snapshot.updated_at_ms)}`;
  }

  function selectView(name) {
    document.querySelectorAll('.view').forEach(node => node.classList.toggle('active', node.id === `view-${name}`));
    document.querySelectorAll('[data-nav]').forEach(node => node.classList.toggle('active', node.dataset.nav === name));
    window.scrollTo({top:0,behavior:'smooth'});
  }
  document.querySelectorAll('[data-nav]').forEach(button => button.addEventListener('click',()=>selectView(button.dataset.nav)));
  const webApp = window.Telegram?.WebApp;
  if (webApp) { webApp.ready(); webApp.expand(); }
  async function load() {
    const gate=$('#auth-gate'), dashboard=$('#dashboard'), nav=$('#bottom-nav'), message=$('#gate-message');
    const initData = webApp?.initData || '';
    if (!initData) { dashboard.hidden=true;nav.hidden=true;gate.hidden=false;message.textContent='Нет Telegram initData. Откройте Mini App кнопкой /app в Telegram; локальная страница не получает доступ к приватному API.';return; }
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 10000);
    try {
      const response=await fetch('/api/v1/snapshot',{headers:{Authorization:`tma ${initData}`},cache:'no-store',credentials:'same-origin',signal:controller.signal});
      const value=await response.json();
      if (!response.ok) throw new Error(response.status===503?'Серверная авторизация не настроена или данные временно недоступны.':'Проверка Telegram-подписи или allowlist не пройдена.');
      render(value);gate.hidden=true;dashboard.hidden=false;nav.hidden=false;
    } catch (error) { dashboard.hidden=true;nav.hidden=true;gate.hidden=false;message.textContent=error.name==='AbortError'?'Ответ сервера не пришёл за 10 секунд. Попробуйте позже.':'Не удалось подключиться к защищённому API. Проверьте связь и настройки HTTPS.'; }
    finally { window.clearTimeout(timeout); }
  }
  $('#retry-button').addEventListener('click',load);
  load();
})();
