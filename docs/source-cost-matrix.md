# Source and Cost Matrix

All listed market and calendar feeds are configured as public read-only endpoints. “No subscription charge” excludes VPS, network egress, optional account plans, provider rate limits, and future pricing changes. No paid API is enabled by default.

| Source | Use | Default state | Credentials | Cost / limit treatment |
|---|---|---|---|---|
| Hyperliquid Info + WebSocket | SOL/HYPE/BTC analysis; public derivative data | Active | None | Public endpoints; provider limits apply; REST timeout 8s |
| Binance Spot + USDⓈ-M public market data | Dynamic exchange catalog, independent BBO and derivative context | Active | None | Official `exchangeInfo` + `ticker/24hr`; bulk `bookTicker`; signed 24h change stored only as an absolute-move proxy; per-provider cap 4 concurrent, 100ms minimum request spacing; catalog every 15m |
| Coinbase Advanced Trade public market data | Dynamic Spot catalog and independent spot BBO | Active | None | Public product catalog follows nested `pagination.has_next/next_cursor`; incomplete traversal is quarantined; per-product book enrichment capped at 10 per cycle and each request uses the same local provider gate |
| Kraken Spot public market data | Dynamic spot catalog and independent spot BBO | Active | None | Official `AssetPairs` + bulk `Ticker`; per-provider cap 4 concurrent, 100ms minimum request spacing; catalog every 15m |
| Federal Reserve RSS, SEC RSS, Kraken Status Atom | Official macro/regulatory/status events | Active; smoke checked | None | Public feeds; bounded 8s requests and bounded item counts |
| BLS iCalendar (`https://www.bls.gov/schedule/news_release/bls.ics`) | Macro calendar | Disabled pending reachability | None | BLS documents this as its public iCalendar URL, but the configured client received HTTP 403 in the audit environment. No auth or access-control bypass is used. Re-enable only after an HTTP 200 smoke check. When re-enabled, polling starts at 6h; failed polls back off 12h then at most 24h. |
| X Filtered Stream | Official-project/social event evidence | Disabled until configured | `X_BEARER_TOKEN`; account IDs must be explicitly verified | Access tier and current account pricing are not configured or assumed |
| GitHub releases/security advisories | Primary code/security evidence | Disabled until repositories are configured | Optional `GITHUB_EVENT_TOKEN` | Public API; unauthenticated/authorized rate limits apply; no paid plan assumed |
| Solana RPC, Base RPC | Candidate discovery and supported on-chain evidence | Disabled | Optional provider authorization environment variables | Public RPC capacity is deployment-specific; no continuous-use guarantee assumed |
| Jupiter quote enrichment | Quote-only exit evidence | Disabled | Optional `JUPITER_API_KEY` | No quote spend or subscription assumed while disabled |
| LLM extraction | Extraction/classification/summary only | Disabled | No credential | No token cost while disabled; never a source-authentication authority |

Live smoke tests issue only bounded read requests to configured public market/event endpoints. They do not test account APIs, place transactions, or enable disabled providers. Inspect `status.py` for active/disabled/degraded coverage before interpreting any output.

Market catalog discovery is a separate diagnostic universe with a 150 scan / 30 deep-watch maximum, a configurable quote-turnover floor and spread ceiling. It does not feed the existing SOL/HYPE production probabilities. Quote turnover/depth are kept in native quote units; no USD/USDT/USDC FX normalization or ticker-only cross-venue identity is inferred. See [Phase 7A](phases/07-multi-asset-analysis.md) for its scope and limitations.
