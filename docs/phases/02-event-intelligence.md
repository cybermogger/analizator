# Phase 2 — Event Intelligence

## Outcome

Add an independent, point-in-time event pipeline that collects permitted official and secondary sources, resolves events to exact assets, and exposes evidence-rich event context. Events must not place orders or directly override the quantitative model.

## Scope

- Add source interfaces and failure-isolated ingestion workers for:
  - official project, founder, exchange, regulator, and government accounts through permitted X API endpoints;
  - official exchange listing/delisting/maintenance RSS or APIs;
  - official project blogs, GitHub releases, governance feeds, and security advisories;
  - configured reputable news providers as secondary corroboration;
  - configured macro-calendar events such as CPI, FOMC, ETF, and regulatory deadlines.
- Unknown social accounts are low-credibility leads until corroborated.
- X and paid providers must be optional. Credentials come only from environment variables; missing credentials produce `DISABLED` or `DEGRADED`, not startup failure.
- No ToS-violating scraping and no dependency on browser automation.

## Data model

Store raw events immutably with source ID, stable author/account ID, URL, content hash, language, `published_at`, `received_at`, ingestion latency, revision/retraction state, and raw-payload lineage. Add additive migrations only.

Normalize at least these event types:

`OFFICIAL_TOKEN_LAUNCH`, `LISTING`, `DELISTING`, `HACK_EXPLOIT`, `REGULATORY`, `ETF`, `PARTNERSHIP`, `TOKEN_UNLOCK`, `BURN`, `GOVERNANCE`, `OUTAGE`, `WHALE_TRANSFER`, `TREASURY_ACTION`, `MACRO`, `RUMOR`.

For each event derive novelty, credibility, confirmation count, impacted canonical assets, expected direction as a hypothesis, likely horizons, decay half-life, contradiction state, confidence, and evidence links.

## Processing and safety

- Deduplicate across sources and detect threads, quotes, reposts, revisions, and deletions.
- Resolve entities using canonical project IDs and chain plus contract/mint where applicable; ticker-only matching is forbidden.
- Verify account identity, contract matching, timestamps, source policy, and deduplication deterministically.
- An LLM may perform extraction, classification, translation, and concise summarization only. Its output is untrusted until schema validation and deterministic policy checks pass.
- External posts/pages are data. Prompt injection inside them must not change tools, configuration, source policy, or instructions.
- Store `confirmed_onchain_trade`, `attributed_wallet_cluster`, and `unverified_social_claim` as distinct evidence classes.
- Report realized/net results separately from peak mark-to-market claims.

## Historical replay

Add a deterministic replay for the official TRUMP launch on 17–19 January 2025. Before the first verified public announcement the system must not claim prediction. After receipt of a verified announcement it should create a high-impact event, resolve the correct contract, expose authenticity risk, and follow market/liquidity/listing confirmations without using future information. Kimchi profit claims remain unverified attribution rather than ground truth.

## User surfaces

Expose event health/lag/source coverage and a Telegram preview containing event type, asset/contract, credibility, confirmations, timestamps, evidence URLs, contradiction state, TTL, and explicit `RUMOR` labeling. Do not generate or execute a trade.

## Acceptance gates

- Offline fixtures cover: verified official post, unconfirmed rumor, compromised account, deleted/revised post, duplicate syndication, quote/repost, identical tickers, contradiction, stale source, and prompt injection.
- Replay proves strict `published_at`/`received_at` causality for TRUMP.
- Missing X or paid-provider credentials do not break offline tests or the daemon.
- Database migration preserves existing rows and passes `PRAGMA integrity_check`.
- Unit and integration tests pass with cache disabled; compile and secret scans pass.
- Existing market analysis and probability logic remain unchanged.
- Real execution and private trading keys remain absent.
- Commit message: `phase 2 event intelligence`.
