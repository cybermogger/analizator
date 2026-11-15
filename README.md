# Multi-venue Crypto Intelligence Analyzer

Production-oriented, read-only 24/7 analyzer. The established **SOL/HYPE signal and ML pipeline remains Hyperliquid-based and unchanged**. Multi-venue prices, Event Intelligence, and the separate on-chain candidate pipeline are diagnostic context; none is an input to the current trading models. A separate experimental spot setup alert now covers eligible instruments beyond SOL/HYPE, including liquid exchange-listed memecoins, under strict freshness and daily/hourly caps; it is not a calibrated probability or a proven edge.

The standalone local dashboard is available at `http://127.0.0.1:8081` when the daemon runs. It shows up to 30 exact market instruments, fresh experimental setup plans, a bounded virtual paper journal with confirmed closed net results, source-linked events and on-chain candidates. It binds to loopback only and is separate from the Telegram Mini App. Telegram can additionally report newly discovered official CEX spot pairs after a persisted first catalog baseline, clearly distinguished from a new token launch. See [Phase 09](docs/phases/09-expanded-alerts-dashboard.md) for eligibility, news/new-token reports and current deployment limits.

The service never places orders and never asks for a wallet, private key, seed phrase, or Hyperliquid API wallet. Telegram and optional read-only feed credentials are environment-only.

## Data boundary and official providers

Only direct, official public endpoints are configured. No provider key is used:

- `POST https://api.hyperliquid.xyz/info`
  - `metaAndAssetCtxs`: mark, oracle, funding, open interest, 24h notional volume, premium when supplied;
  - `candleSnapshot`: 1m, 5m, 15m, 30m, 1h, 4h candles (official limit: latest 5000);
  - `l2Book`: REST fallback, up to 20 levels on each side;
  - `allMids`: schema/live availability check.
- `wss://api.hyperliquid.xyz/ws`
  - `candle`, `trades`, `l2Book`, `bbo`, `allMids`, `activeAssetCtx`.
- Binance Spot market-data REST/WebSocket and USDⓈ-M Futures REST/WebSocket:
  `https://data-api.binance.vision`, `https://fapi.binance.com`,
  `wss://stream.binance.com:9443/ws`, `wss://fstream.binance.com/ws`.
- Coinbase Advanced Trade public market data:
  `https://api.coinbase.com`, `wss://advanced-trade-ws.coinbase.com`.
- Kraken Spot public REST and WebSocket v2:
  `https://api.kraken.com`, `wss://ws.kraken.com/v2`.

There is no paid feed, DEX scraper, private account endpoint, trading endpoint, wallet, or private key. CoinGecko and DEX Screener are not used as market truth. Event sources use permitted official APIs, RSS/Atom, iCalendar, and GitHub REST only. X and GitHub credentials are optional environment variables. BTC context inside the existing model remains the Hyperliquid BTC perpetual until a later separately validated model migration.

Official references: [Hyperliquid Info](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint), [Hyperliquid WebSocket](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket), [Binance Spot REST](https://developers.binance.com/en/docs/products/spot/rest-api), [Binance USDⓈ-M market streams](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams), [Coinbase Advanced Trade public WebSocket](https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/websocket/websocket-endpoints), and [Kraken Spot WebSocket v2 ticker](https://docs.kraken.com/api/docs/websocket-v2/ticker/).

## Independent Event Intelligence

- Sources are failure-isolated workers for the official X Filtered Stream, official RSS/Atom feeds, BLS iCalendar, and public GitHub releases/security advisories. X and project-specific GitHub sources stay disabled until configured. BLS iCalendar is currently disabled because it returned HTTP 403 in this audit environment; no authentication bypass is attempted. Missing credentials never prevent daemon startup.
- Raw payloads are immutable and retain stable source/author IDs, URL, language, SHA-256 hash, publication/receive timestamps, ingestion latency, relations, authenticity state, and revision/retraction records.
- Entity resolution uses canonical project IDs and exact `chain_id + contract/mint`. Ticker text is only an ambiguous hint and never an identity join.
- Cross-source exact/near-duplicate detection, quote/repost/reply threading, source credibility, confirmations, contradiction state, novelty, horizon, decay half-life and evidence URLs are deterministic.
- Optional LLM integration is disabled by default and restricted to extraction/classification/concise summary. External text is serialized as an untrusted document; LLM output cannot authenticate a source, contract, timestamp or override policy.
- `/events` returns a Telegram preview explicitly labelled as event context, not a trading signal. Event information never enters `_analyze_coin()`, the ensemble, alert gate, or order execution.
- Historical replay filters on `received_at` and orders by receive/publication time. The checked-in TRUMP regression proves no pre-announcement prediction, exact Solana contract resolution after the verified announcement, authenticity risk, and later independent confirmations. Wallet attribution/social claims remain separate evidence; realized results exclude peak mark-to-market and are net of fees/slippage.

Configured no-key official feeds include the Federal Reserve monetary-policy RSS, SEC press-release RSS, Kraken status Atom feed, and the [Ethereum Foundation English RSS](https://blog.ethereum.org/en/feed.xml). The official BLS calendar URL is documented in the [BLS iCalendar instructions](https://www.bls.gov/help/hlpical.htm), but its direct request returned HTTP 403 from the audit environment and it is disabled pending successful HTTP 200 verification. Add project blogs, exchange announcement feeds, governance feeds and reputable secondary providers only through their documented API/RSS endpoints in `config.yaml`; do not configure scraped HTML pages.

Event-source references: [X Filtered Stream](https://docs.x.com/x-api/posts/filtered-stream/introduction), [GitHub public releases](https://docs.github.com/en/rest/releases/releases#list-releases), [GitHub repository security advisories](https://docs.github.com/en/rest/security-advisories/repository-advisories), [Federal Reserve RSS](https://www.federalreserve.gov/feeds/feeds.htm), [SEC RSS](https://www.sec.gov/about/rss-feeds), and [BLS release calendar](https://www.bls.gov/schedule/news_release/cpi.htm).

## Independent on-chain and memecoin intelligence

- High-recall discovery accepts a candidate only from a directly verified chain transaction containing token/mint or pool creation. DEX/API enrichment alone cannot originate a candidate.
- The strict second stage separately scores attention, authenticity, liquidity, distribution, on-chain momentum, manipulation risk, and feasible exit. It rejects unverified identity, liquidity removal, honeypot/tax/blacklist restrictions, unsafe authorities/proxies, holder/bundle/sniper/wash concentration, excessive slippage, or inability to exit the configured `$1,000` paper notional.
- Identity is always `chain_id + exact contract/mint`; matching ticker text never merges assets. Raw on-chain events are immutable and keep chain event time, local receive time, provider, block/parent hash, transaction/log position, payload hash, confirmations, and canonicality/reorg history.
- Solana uses official read-only JSON-RPC (`getTransaction`, `getBlock`, account/token reads) plus `logsSubscribe`, then verifies the transaction and canonical block before decoding SPL mint initialization. Base uses read-only Ethereum JSON-RPC receipts/blocks/code/storage/logs and detects EIP-1967 proxy upgrade risk. No submit/sign method is allowlisted.
- Base is implemented after the Solana offline gates and uses bounded HTTP block/log polling because Base's public endpoint is not a transaction-submission or guaranteed WebSocket service. Both network workers remain disabled by default because public RPC capacity is deployment-specific. Enable only a provider whose read endpoint is suitable for continuous monitoring; credentials, if any, stay in environment variables.
- Phase-2 official events may confirm identity only when an exact chain and contract are linked. They cannot create a candidate without direct chain evidence. Replay is causal by `received_at`; the TRUMP fixture makes no pre-announcement claim and models detection latency, MEV disadvantage, slippage, fees, and feasible exit rather than peak mark-to-market.
- `/onchain` previews include exact identity, age, pool/provider, chain liquidity, proven exit capacity, exact configured-position slippage/net output, holder concentration, authorities/proxy, evidence, scores, risk flags, TTL, and invalidation. They explicitly state that no automatic purchase or execution occurs.
- Live evidence is capability-gated. Solana RPC verifies SPL mint/authority/supply and largest-token-account concentration. Exact operator-configured DEX program/pool/vault identities enable conservative quote-vault liquidity and vault-delta trade evidence. Jupiter Swap V2 `/order` may add quote-only round-trip paper-exit evidence; the client supplies no taker and rejects any response containing transaction material.
- Generic Solana RPC cannot aggregate token accounts into beneficial holders or prove bundle/sniper/wash attribution and token-specific transfer restrictions. Generic Base RPC cannot derive holder distribution, DEX reserves/trades or an executable paper quote. These fields are reported as `NOT_AVAILABLE` in provider health and make the candidate ineligible; zero or benign values are never fabricated.

Official references: [Solana `getTransaction`](https://solana.com/docs/rpc/http/gettransaction), [Solana `logsSubscribe`](https://solana.com/docs/rpc/websocket/logssubscribe), [Base network endpoints](https://docs.base.org/get-started/connect-to-base), [Ethereum `eth_getTransactionReceipt`](https://ethereum.org/developers/apis/json-rpc/#eth_gettransactionreceipt), and [Ethereum `eth_getLogs`](https://ethereum.org/developers/apis/json-rpc/#eth_getlogs).

## Phase 1 market truth

- `Instrument` identity is explicit: provider, venue, market type, base, quote, venue symbol, decimals, plus chain and contract/mint when applicable. A separate `canonical_asset_id` must be configured; ticker text is never an identity join.
- Normalized immutable types cover trades, BBO, L2, candles, funding, open interest, liquidations, mark/index, timestamps, lineage, and provider health.
- Canonical price uses only fresh USD/USDC/USDT-equivalent BBO/trade observations, rejects a deviating venue when at least three observations exist, and weights by receive freshness, exchange-to-receive latency, and available volume. If exactly two sources diverge beyond the configured threshold, neither can be identified safely as the outlier, so price publication is suppressed as `DEGRADED`.
- A canonical price is not published unless at least two distinct fresh venues remain. Missing quorum, a stale/disconnected provider, or an excluded outlier is explicit `DEGRADED`/`STALE`, never ordinary confidence.
- Diagnostics include venue divergence, spot/perpetual cross-exchange basis, weighted spread, volume concentration, rejected outliers, stale sources, and per-provider lag.
- The dynamic CEX universe has `majors_l1`, `derivatives_movers`, and a reserved `dex_memecoin_candidates` basket. Scan/deep budgets are 150/30 maximum; L2 and trades are requested only for deep-watchlist instruments. Phase-3 on-chain candidates remain a separate risk pipeline and are not silently promoted into the CEX/major model universe.
- Official catalog discovery refreshes every 15 minutes for Binance Spot/USDⓈ-M, Coinbase Spot, and Kraken Spot. Candidates require a supported quote, at least `$1,000,000` 24h quote turnover, and a fresh BBO with spread no wider than 50 bps. Binance/Kraken use bulk BBO endpoints; Coinbase enriches up to 10 products per poll through the provider rate gate. Discovery, exclusion, delisting, stale-catalog and source lineage decisions are persisted in SQLite schema v13. These newly discovered assets are currently a separate monitoring universe: unknown tickers receive venue-scoped unresolved identity and are not merged cross-venue or fed into SOL/HYPE production probabilities.
- Coinbase products traverse the documented nested pagination object and a catalog is complete only after a terminal page. Invalid quote metrics are isolated to the affected instrument and recorded as exclusion reasons; they do not invalidate or truncate valid catalog identities. Binance exchange `closeTime` is checked for stale/future timestamps. Its absolute 24h percentage change is a movement-magnitude proxy, not statistical volatility.
- Quote assets are never converted or treated as identical without a verified FX observation. Canonical multi-venue prices use a single exact quote-currency quorum; sources in other quote currencies remain explicitly uncomparable and can keep the result degraded.

## Phase 7B descriptive multi-asset analysis

- The ordinary daemon rotates through the selected deep watchlist (up to 30 exact venue instruments) and caches closed 5m/15m/1h/4h candles by `(instrument_id, interval, open_time_ms)`. It computes descriptive trend, four-bar momentum, realized log-return volatility, relative volume, and agreement across available intervals. Gaps, open/future bars, insufficient history, stale candle snapshots, and provider errors remain visible as quality states.
- Each card retains provider, venue, market type, base, quote and venue symbol. A single venue can provide a descriptive observation, but the card says so; no ticker-only joining, quote conversion, contract multiplier conversion, canonical quorum, legacy SOL/HYPE ML, win probability or trading recommendation is inferred.
- Fresh observed L2 reports spread, displayed top-five depth in native quote units, and imbalance. Displayed depth is not executable capacity. Funding/open interest are sampled only for perpetual instruments whose provider implements the derivatives interface; unsupported spot data is `NOT_AVAILABLE`.
- Requests are rotated across the deep list, bounded by per-provider concurrency/rate gates, per-request HTTP timeout (maximum 10s), a per-cycle request budget and a bounded in-memory candle cache. Additive schema v13 persists immutable causal observation snapshots at a configurable cadence (default 5 minutes), with exact venue identity, frozen trend-only baseline, point-in-time source lineage, feature version/config hash, quality and universe membership. Outcomes at 15m/1h/4h use the first exact-instrument BBO mid returned within the configured target latency (default 60s); missed windows become `UNAVAILABLE`, never an arbitrary later price. Quote-spread, per-side fee and slippage assumptions are recorded separately; funding is excluded, and observational returns are not trading PnL.
- Telegram `/markets` shows up to ten cached observations. `/analyze <exact-instrument-id>` shows the source-aware card; a bare ticker is rejected as ambiguous when it maps to one or more venue instruments. `/analysis_status` and `/analysis_performance` report persisted cohort coverage and observational outcome states. Real model performance remains `NOT_EVALUABLE`; no training, model promotion, or production probability change occurs here. Event/on-chain context is marked `NO_VERIFIED_CONTEXT` unless an exact causal mapping is available.

## What is implemented

- Six-timeframe causal technical engine: EMA 9/21/50/100/200, SMA 20/50/200, RSI 7/14/21, MACD/histogram, ATR 7/14, ADX/+DI/-DI, Bollinger Bands, rolling VWAP, ROC, momentum, Stochastic RSI, Williams %R, CCI, OBV proxy, volume averages/relative volume/acceleration, realized volatility, rolling standard deviation, candle body/wicks, gap proxy, and impulse/range expansion.
- Causal price action: confirmed-from-the-left swings, HH/HL/LH/LL, BOS, CHoCH, support/resistance, breakout, false breakout, retest, range/trend efficiency, acceleration/exhaustion, volatility expansion/contraction, impulse/correction, and distance from recent extremes.
- Trade flow and rotating raw Parquet: aggressive buy/sell proxy from official trade side, cumulative delta proxy, large-trade quantiles, rolling volume profile, POC/HVN/LVN proxies.
- L2 microstructure: spread, relative spread, depth, multiple-distance imbalance, distance-weighted imbalance, microprice, pressure, slope, liquidity walls, wall additions/removals, and short-term book momentum.
- Derivatives history: mark/oracle premium, funding, OI, notional volume, 1m/5m/15m/30m/1h/4h changes, and price/OI state combinations after local history has accumulated.
- BTC context: trend, momentum, volatility, rolling correlation, beta, and lead/lag for the Hyperliquid BTC perp.
- Regimes: `TREND_UP`, `TREND_DOWN`, `RANGE`, `HIGH_VOLATILITY`, `LOW_VOLATILITY`, `BREAKOUT`, `REVERSAL_RISK`. Regime and horizon alter component weights.
- Statistical analogues: rolling normalization uses only past observations; nearest historical states use an outcome embargo.
- Per-coin/per-horizon `HistGradientBoostingClassifier` models with dynamic ATR/S/R triple barriers, chronological TRAIN/VALIDATION/CALIBRATION/TEST, walk-forward evaluation, and one-vs-rest isotonic calibration.
- Metrics: multiclass Brier score, log loss, ECE, balanced accuracy, class precision/recall/F1, confusion matrix, and 40–50/50–60/60–70/70–80/80–90% calibration buckets.
- Quality-aware ensemble: technical, structure, derivatives, order book, trade flow, statistical, ML, and BTC context. Missing components receive zero effective weight. An ML artifact below the configured OOS thresholds is not loaded.
- Event-driven Telegram gate: directional gap, confidence, WAIT, timeframe/component agreement, freshness, consecutive confirmation, intelligent cooldown, and deduplication against prior SQLite state. Fast breakout/breakdown, volatility, order-flow, and BTC shocks have separate high-confidence gates.
- Every snapshot stores confidence, signal strength, ATR/volatility/historical-distribution range, S/R-derived targets and invalidation, component probabilities/weights, freshness, and alert decision. Mature snapshots are evaluated automatically for Brier, calibration, directional/balanced accuracy, class precision, forward returns, regime, and probability buckets.
- Telegram commands: `/app`, `/status`, `/sol`, `/hype`, `/signals`, `/watchlist`, `/markets`, `/analyze`, `/analysis_status`, `/analysis_performance`, `/opportunities`, `/events`, `/asset`, `/onchain`, `/paper`, `/performance`, `/sources`, `/health`, `/help`.
- Resilience: REST timeouts/retries/backoff/rate-limit retry, WebSocket reconnect/backoff, REST L2 fallback, per-asset error isolation, rotating JSON logs, SQLite WAL, health logging, and Docker restart policy.

“Premium”, delta, positioning state, POC/HVN/LVN, OBV, and aggressive flow are explicitly treated as computed features or **proxies**, not fields invented as exchange facts. If an official field or enough history is unavailable, its component quality becomes zero and the UI shows `MODEL_WARMING_UP`.

## Project layout

```text
bot.py                         daemon, one-cycle and smoke-test CLI
train.py                       official backfill + chronological ML training
status.py                      current SOL/HYPE, WS, DB and Telegram status
setup_telegram.py              bounded interactive Telegram setup
watchdog.py                    stale-heartbeat process supervisor
config.yaml                    non-secret service/model/alert parameters
src/hyperliquid/               strict REST/WS schemas and clients
src/providers/                 normalized interfaces, official adapters, identities, truth, universe
src/data/                      SQLite repository and Parquet rotation
src/events/                    immutable evidence, sources, policy, identity, replay
src/onchain/                   direct Solana/Base RPC, immutable evidence, replay/risk pipeline
src/paper/                     manual recommendations, risk gates, append-only shadow ledger
src/features/                  independent causal feature engines
src/models/                    rules, targets, calibration, ML, ensemble
src/alerts/                    gate, Russian formatter, Telegram client
src/webapp/                    signed Telegram initData auth, read-only API, async server
webapp/                         Russian mobile-first Telegram Mini App
src/service/                   analyzer, scheduler, health, logging
tests/                         offline fixtures and unit/integration tests
```

## 1. Install locally

Python 3.11–3.14 is supported. PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Linux/macOS:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

## 2. Configure Telegram

1. Open Telegram’s `@BotFather`, run `/newbot`, and copy the bot token.
2. Run the bounded setup helper (each Telegram request has an 8-second timeout):

```bash
python3 setup_telegram.py
```

3. Follow its prompt, send one message to the bot, and press Enter. The helper validates the bot, discovers the chat ID with one `getUpdates` call, writes `.env` with mode `0600`, and sends one test message.

Manual configuration is also supported:

```dotenv
TELEGRAM_BOT_TOKEN=123456:replace_me
TELEGRAM_CHAT_ID=123456789
STRICT_ALERT_MODE=false
```

`.env` is ignored by Git and is never copied into source code.

## 3. Telegram Mini App

The read-only Mini App is served by the same async Python service at `/app/`; it reads the live in-memory service state plus existing signal/event/on-chain/paper repositories through `GET /api/v1/snapshot`. Private API data requires a valid Telegram `initData` HMAC, a recent `auth_date` (five minutes by default), and an explicit numeric user-ID allowlist. The browser sends the raw `Telegram.WebApp.initData`; the server verifies it with `TELEGRAM_BOT_TOKEN`. The page never trusts `initDataUnsafe`. Telegram's official validation rules are documented at [Telegram Mini Apps: validating data received via the Mini App](https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app).

Set these values in the operator-managed `.env` (do not commit it):

```dotenv
TELEGRAM_MINI_APP_URL=https://<your-owned-domain>/app/
TELEGRAM_MINI_APP_ALLOWED_USER_IDS=<numeric-telegram-user-id>[,<another-id>]
MINI_APP_PORT=8080
```

The URL must be the real public HTTPS URL for the domain you control; this project does not invent or register a domain. Configure that domain with Telegram/BotFather as required for your bot and terminate TLS in a reverse proxy that forwards to `127.0.0.1:${MINI_APP_PORT:-8080}`. Compose publishes the app on loopback only. The app route has no independent HTTPS certificate or public hostname. After TLS, DNS, and Telegram domain setup are live, open the bot in a private Telegram chat and send `/app`; it replies with a `web_app` button. With no valid public URL it gives setup instructions and sends no launch button.

For a local static preview without starting analysis workers, run `python -m http.server 8080 --bind 127.0.0.1 --directory webapp` from the project root, then open `http://127.0.0.1:8080/` in a browser. It shows the real shell and a clear “open through Telegram” authorization gate; no sample or synthetic market values are supplied. This static preview has no private API. In deployment, the same page is served at `/app/` by the analyzer. Use browser responsive/device mode to inspect the mobile layout; desktop uses the same five sections and persistent navigation.

Only allowlist user IDs that should see the complete saved signal, event, on-chain and shadow portfolio data. Telegram `initData` is sent in an HTTPS same-origin Authorization header and is never logged or returned by the API. The response disables caching; external event descriptions are rendered as text. This interface contains no order, wallet, signing, or transaction endpoints and does not change the analysis models.

## 4. Run offline tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

The tests never access the internet: an autouse fixture blocks socket connections, provider contracts use checked-in fixtures, and `pytest-timeout` enforces a 30-second ceiling per test. They also cover provider disconnects, duplicate/out-of-order events, stale feeds, venue outliers, quorum, ticker collisions, universe budgets, normalized lineage, and additive schema migration.

## 5. Run a live smoke-test

```powershell
.\.venv\Scripts\python.exe bot.py --smoke-test
```

This separate live check validates official Hyperliquid REST/WS schemas and performs a bounded, failure-isolated public BBO refresh across all enabled venues. It is capped at 60 seconds overall, does not require provider secrets, and does not send Telegram messages. A geographically unavailable venue is reported degraded while the remaining checks continue.

If a restricted host blocks DNS but HTTPS works, resolve the official hostname operationally and set the optional address. TLS still validates `api.hyperliquid.xyz`; no market-data source changes:

```dotenv
HYPERLIQUID_RESOLVED_IP=replace_with_current_dns_answer
```

Normal VPS deployments should leave it unset.

## 6. Run one complete analysis cycle

Compact result and Russian Telegram preview, without sending:

```powershell
.\.venv\Scripts\python.exe bot.py --once --print-messages
```

Full component diagnostics:

```powershell
.\.venv\Scripts\python.exe bot.py --once --full-json
```

Add `--send-telegram` only when you intentionally want the one-cycle run to use the alert gate.

## 7. Backfill and train models

The trainer requests at most the latest 5000 candles from official `candleSnapshot`. It then runs a purged chronological protocol: barrier/feature/weight selection on walk-forward validation, a separate calibration segment, and one untouched final test:

```powershell
.\.venv\Scripts\python.exe train.py --history 5000
```

Train one model:

```powershell
.\.venv\Scripts\python.exe train.py --coin SOL --horizon 1h --history 5000
```

Run the same quant audit without another network backfill:

```powershell
.\.venv\Scripts\python.exe quant_audit.py --no-promote
```

Every candidate is first saved under `models/challengers/`. A challenger replaces `models/<COIN>_<HORIZON>.joblib` only when it passes the OOS admission gates and is not worse than an existing champion on the same recent holdout. The previous champion is archived under `models/champions/`. Reports include target parameters, training/calibration/test periods, exact feature set, permutation importance, metrics, calibration buckets, weights, model version, and timestamp.

`MODEL_REJECTED_OOS` is deliberate: rejected artifacts remain available for inspection but receive weight zero in the daemon. Automatic retraining is disabled by default; use `python train.py` explicitly when a new champion/challenger audit is intended.

## 8. Ubuntu VPS from zero

The following uses Docker's official Ubuntu apt repository. No Hyperliquid wallet, API wallet, private key, or seed phrase is used.

```bash
sudo apt update
sudo apt install -y ca-certificates curl git python3
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
newgrp docker
docker compose version
```

Copy with Git:

```bash
git clone YOUR_REPOSITORY_URL hyperliquid_analyzer
cd hyperliquid_analyzer
cp .env.example .env
python3 setup_telegram.py
mkdir -p data/raw models logs
# If `id -u` or `id -g` is not 1000, put those values in ANALYZER_UID/ANALYZER_GID in .env.
docker compose up -d --build
docker compose ps
docker compose exec analyzer python status.py
```

If the project is delivered as an archive or by `scp`, copy/unpack it to `hyperliquid_analyzer`, enter that directory, then run the same commands starting with `cp .env.example .env`.

The Compose service has `restart: unless-stopped`. Persistent host directories `./data`, `./models`, and `./logs` are mounted at `/data`, `/models`, and `/logs`; rebuilding or replacing the container does not remove them. The liveness heartbeat is checked every 30 seconds. A stale event loop is terminated by `watchdog.py`, allowing Docker to restart it. A network outage leaves the daemon alive so WebSocket reconnect and cached-data fallback can recover without a restart loop.

Docker stdout logs and `analyzer.log` are independently capped at five 10 MB files. SIGTERM gets a 40-second graceful-shutdown window.

Official Docker reference: [Install Docker Engine on Ubuntu](https://docs.docker.com/engine/install/ubuntu/).

## 9. Run commands

Local foreground daemon:

```powershell
.\.venv\Scripts\python.exe bot.py
```

One complete SOL/HYPE check without Telegram delivery:

```bash
python bot.py --once
```

Forced champion/challenger retrain:

```bash
python train.py
```

Current operational status:

```bash
python status.py
```

VPS daemon:

```bash
cp .env.example .env
nano .env
docker compose up -d --build
docker compose logs -f analyzer
```

One Docker check, retrain, and status:

```bash
docker compose run --rm analyzer python bot.py --once
docker compose run --rm analyzer python train.py
docker compose exec analyzer python status.py
```

## 10. Inspect logs, DB, and model quality

```bash
docker compose logs --tail=200 analyzer
docker compose logs -f --tail=200 analyzer
docker compose exec analyzer sh -c 'tail -f /logs/analyzer.log'
docker compose exec analyzer python status.py
docker compose exec analyzer sh -c 'cat /models/SOL_15m.metrics.json'
```

Health is written to logs every `service.health_interval_hours`; it is not sent to Telegram. Provider health and lag are stored separately. Normalized event lineage is stored in SQLite; high-volume raw WebSocket buffers rotate to compressed Parquet under `data/raw/` with seven-day raw retention.

Schema v5 added multi-venue market truth and v6 added independent event intelligence. Additive schema v7 adds on-chain providers/assets, immutable events, canonicality/reorg history, assessments, and provider health. Schema v8 adds immutable Phase 4 dataset and experiment registries. Schema v9 adds immutable paper decisions, recommendation revisions, state transitions, ledger entries, outcomes and alert deduplication. Existing rows are preserved; update/delete triggers enforce immutable evidence and Phase 4/5 audit records.

## 9a. Phase 4 forecasting research (offline only)

Phase 4 is isolated under `src/research/`; it does not alter `train.py`, the live SOL/HYPE probability path, Telegram delivery, or any execution setting. It implements a high-recall causal candidate screen followed by a segment-aware cross-sectional ranker for majors/L1s, liquid alts, derivatives movers, and new/memecoin candidates.

Every research row carries feature availability time, lineage, point-in-time universe membership, instrument activity and forward-label end time. Event publication/receipt/revision time and closed-candle time are validated before a snapshot can be written. Dataset files are canonical JSON addressed by SHA-256 over code version, config, feature schema, lineage, universe definition and sorted rows; the registry verifies the file hash before accepting it.

Purged expanding walk-forward folds have separate TRAIN, VALIDATION and CALIBRATION blocks plus one fixed untouched TEST block. Label windows crossing a boundary are purged and a configurable embargo is applied. Regularization is selected only from validation folds. The final pre-test model is fitted from development rows, isotonic TP-before-stop calibration and the execution-aware candidate threshold use disjoint halves of CALIBRATION, and a one-shot vault releases TEST only after all task models and thresholds are frozen.

The runner fits separate deterministic heads for every supported segment and 5m/15m/1h/4h/24h task with enough data: calibrated TP-before-stop probability, execution-adjusted forward return, MAE, MFE, realized volatility and exit-feasibility probability. The return target is recomputed from development outcomes through the configured execution ledger; the stored `net_return` is never trusted. Candidate generation and cross-sectional top-k ranking remain separate stages. Brier/ECE and precision are measured only against TP-before-stop; net expectancy, net win rate and drawdown are calculated separately from simulated execution.

The ranker uses predicted execution-adjusted return in return units as its primary key. Calibrated TP probability, predicted exit feasibility, predicted MFE and decision-time liquidity are deterministic tie-breakers. Candidates fail closed when predicted return is non-positive, predicted exit feasibility is below its configured gate, predicted MAE exceeds the adverse-excursion limit, predicted volatility is excessive, or causal risk/liquidity evidence is unavailable.

Every selected TEST prediction is passed directly through the same execution ledger. It applies entry/exit fees, spread, slippage, latency, funding, impact, liquidity participation caps, partial fills and rejected fills; precomputed snapshot net returns are never trusted. The accounting identity is checked for every simulated fill. Required comparisons are no-trade, momentum, mean reversion, BTC beta and current production. Results include sample/trade counts, deterministic time-block bootstrap uncertainty, expectancy after costs, precision@k, profit factor, turnover-adjusted return, drawdown, Brier/ECE, false-alert rate, coverage, regimes, horizons and segments. Negative ablations and `NOT_EVALUABLE` variants are retained in each model card.

Inspect the honest cold-start state without training or network access:

```bash
python research.py
```

Run the complete offline snapshot → split → fit → calibrate → execution backtest → evaluate → registry/model-card pipeline:

```bash
python research.py --snapshot data/research/snapshot.json --config config.yaml --db data/research/registry.db --output data/research/model-card.json
```

The command does not load `.env`, use the network, modify live models, or enable order execution. A valid outcome needs an explicit horizon, TP-before-stop target, direction, entry/exit prices and entry/exit liquidity. Missing task evidence is reported as `NOT_EVALUABLE`, never filled with synthetic values.

No Phase 4 challenger is promoted unless its lower 95% OOS net-expectancy bound beats every applicable baseline after costs, it has enough samples/trades/time blocks, acceptable calibration/drawdown, and stable block results. With no real reproducible Phase 4 snapshot, the checked-in model card remains `NOT_EVALUABLE` and the existing production ensemble remains champion.

## 9b. Phase 5 manual recommendations and shadow portfolio

Phase 5 is shadow-only and has no exchange order client, signer, wallet, seed phrase, or private-key setting. An eligible ranked opportunity is converted into an explainable manual recommendation with exact instrument identity, horizon, calibrated TP probability, predicted net move, entry/invalidation/targets, liquidity and cost context, supporting and contradicting evidence, freshness and model/data lineage. Rejected, `NO_TRADE`, expired and retracted decisions remain auditable.

The normal `AnalyzerService.analysis_cycle` invokes the shadow decision orchestrator after the causal production signal and alert gate are finalized. It labels lineage as `current-production-ensemble` (never as the unpromoted Phase 4 challenger), derives spread/depth/slippage from the decision-time Hyperliquid book, and requires fresh multi-venue truth before a shadow position can open. Phase 2 events and Phase 3 assessments join only on canonical identity and, when applicable, exact chain plus contract/mint.

Sizing is capped by volatility/invalidation distance, liquidity participation, total exposure and correlation-cluster exposure. Configurable circuit breakers fail closed on stale feeds, venue disagreement or outage, extreme volatility/gaps, model drift, liquidity loss, unconfirmed news, cooldowns and the daily paper-loss limit. Defaults are 0.25% paper-equity risk for liquid assets, 0.05% for new/memecoin assets, and a 1.5% daily loss limit.

The append-only shadow ledger records opening and closing costs separately: gross and net PnL, fees, funding and slippage reconcile exactly. Stable decision and ledger identities make restarts idempotent. Telegram sends use a durable at-most-once claim before network I/O: a crash with an ambiguous result is shown as `unknown_after_crash` in status and is not resent blindly. This trades possible alert loss in the narrow claim/send crash window for avoiding duplicate alerts; reconcile that item manually with Telegram before clearing the incident. The `/paper` report explicitly separates realized/unrealized PnL, costs, rejections and missed/retracted opportunities.

Run shadow mode for at least four weeks across multiple market regimes before judging operational usefulness. Fixture tests or a short shadow run are not evidence of profitability and must never be presented as such.

## 10. Change alert and model parameters

Edit `config.yaml` and restart. Common parameters:

- `service.analysis_interval_seconds`: 30–60 recommended, default 45;
- `alerts.alert_threshold`: directional percentage threshold, default 40;
- `alerts.confirmation_cycles`: default 2;
- `alerts.realert_delta`: percentage points, default 5;
- `alerts.cooldown_minutes`: default 15;
- `alerts.minimum_directional_gap`, `minimum_confidence`, `minimum_signal_strength`, `maximum_wait`: quality gates;
- `alerts.fast_event_confidence`, `fast_event_strength`: bypass gates for genuine market shocks;
- `alerts.strict_alert_mode`: alert every qualifying cycle; default false;
- `market.timeframe_weights`: base 1m/5m/15m/30m/1h/4h weights;
- `providers.*`: official URLs, enable flags, HTTP timeouts, stale thresholds, and per-provider subscription budgets;
- `events.sources.*`: official feed/API URLs, allowlisted domains, polling limits and environment-variable names for optional credentials;
- `onchain.providers.*`: direct read-only RPC/WebSocket URLs, enable flags, timeouts and optional credential environment-variable names;
- `onchain.providers.solana_mainnet.configured_pools`: optional exact DEX program, pool, token/quote mint and vault identities; unconfigured pool discovery is deliberately unavailable;
- `onchain.enrichment.jupiter_quote`: disabled-by-default official Swap V2 quote-only endpoint, timeout/rate limit, stable mint and optional API-key environment-variable name;
- `onchain.paper_notionals_usd` and `onchain.risk_filter.*`: exact paper-position quote sizes, exitability, liquidity, authority, holder, tax, slippage and manipulation gates. The service automatically adds a capacity probe at `paper_notional_usd * minimum_sellable_liquidity_multiple`; capacity is the largest fresh input notional with acceptable round-trip slippage, never the trade's net output;
- `paper.*`: shadow-only equity, per-idea risk, daily-loss, liquidity/volatility, exposure/cluster, cooldown, freshness/divergence and model-risk gates;
- `market_truth.*`: fresh-source age, quorum, outlier threshold, latency/freshness weighting, and refresh cadence;
- `universe.*`: scan/deep budgets, bucket minimums, and ranking weights;
- `ensemble.base_weights`: validation-derived fallback weights used when no accepted per-task audit exists;
- `models.barrier_atr_multiplier`: dynamic triple-barrier width;
- `models.min_training_samples`, `min_balanced_accuracy`, `max_brier_score`, `min_brier_skill`: model admission gates. Positive Brier skill versus a prior estimated strictly before the test period is required by default.
- `models.auto_retrain.enabled`: false by default; production models change only after an explicit `python train.py` run;
- `forecast_performance.*`: minimum samples, shrinkage, adaptive-weight bounds, and overconfidence-to-WAIT limits.

Restart after changes:

```bash
docker compose restart analyzer
```

## 11. Restart and update safely

Restart without rebuilding:

```bash
docker compose restart analyzer
docker compose exec analyzer python status.py
```

For a Git deployment:

```bash
git pull --ff-only
docker compose up -d --build
docker compose exec analyzer python status.py
docker compose logs --tail=100 analyzer
```

The persistent host directories survive `docker compose down` and image rebuilds. Database migrations are additive (`CREATE TABLE IF NOT EXISTS`). Back up `data/`, `models/`, `logs/`, and `.env` before major upgrades.

## Operational notes

- Cold start: candles are immediately available; OI changes need 1 minute to 4 hours of local snapshots; WebSocket flow/profile features need local trades; an accepted ML model needs enough labelled history and OOS quality.
- The analysis loop uses cached SQLite candles and requests only the newest bars after bootstrap, avoiding repeated 5000-candle calls.
- A single SOL failure does not stop HYPE. Telegram or ML failure does not stop rule/statistical analysis.
- Alerts are statistical model output, not a guarantee or financial advice.

## Phase 6 production operations

Read [architecture](docs/architecture.md), the [official source and cost matrix](docs/source-cost-matrix.md), and the [operations runbook](docs/operations-runbook.md) before enabling a deployment. The runbook defines stop conditions, retention, incident capture, and backup/restore steps. SQLite schema migrations are additive; the current schema is v12. A backup is made with SQLite's online backup API and verified using both integrity and foreign-key checks before it may be used for restore.

Before each deployment, take a verified database snapshot, run `python bot.py --smoke-test` (bounded public read-only endpoints), inspect `python status.py`, and start with `docker compose up -d --build` only when source coverage, database integrity, paper reconciliation, and configured shadow mode are acceptable. Stop the service on stale/contradictory feeds, unexplained delivery claims, accounting or migration errors, repeated false-alert threshold breach, or calibrated-model drift. Keep Phase 4 challenger `NOT_EVALUABLE`; do not claim profitability from fixtures or a short shadow run. X/GitHub, RPC enrichment and LLM extraction remain disabled unless their documented configuration and access are separately available. The active source costs are public/no-key; VPS/egress and external API plan costs are deployment-dependent and no paid source is enabled.
