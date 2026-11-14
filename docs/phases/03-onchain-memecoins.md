# Phase 3 — On-chain and Memecoin Intelligence

## Outcome

Add a separate high-recall on-chain candidate pipeline followed by strict authenticity, liquidity, manipulation, and exit-feasibility filters. Do not mix new tokens with thresholds or models for majors and liquid alts.

## Network scope

- Implement Solana first through a direct RPC/streaming/webhook adapter with transaction verification.
- Add Base as the first EVM adapter only after the Solana contract and offline gates pass.
- DEX aggregators may enrich or back up direct chain data; they cannot be the sole truth source.
- Provider credentials are environment-only and optional for offline operation.

## Signals and evidence

Track:

- mint/token creation and new liquidity pools;
- initial, growing, removed, and realistically sellable liquidity;
- deployer funding path and linked wallets;
- mint/freeze/update authorities, ownership, proxy, and upgrade risk;
- holder concentration, bundles, snipers, and synchronized early buys;
- buy/sell counts, unique buyers, organic wallet growth, and repeat traders;
- realized volume and wash/self-trading proxies;
- price impact and slippage for configured paper notionals;
- smart-wallet flows, exchange flows, large transfers, and treasury actions;
- honeypot, tax, blacklist, and transfer restrictions where applicable;
- whether an official identity published the exact chain and contract/mint.

## Candidate and ranking flow

Use two stages:

1. high-recall candidate generation;
2. strict risk/ranking filter.

Expose separate scores for attention, authenticity, liquidity, distribution, on-chain momentum, manipulation risk, and exit feasibility. Never interpret market capitalization or a whale's marked position as realizable profit.

## Replay and alerts

Add point-in-time fixtures for official TRUMP and multiple rug/scam patterns. The replay may detect an opportunity only after public confirmation and must model latency, MEV disadvantage, liquidity, and feasible exit. It must not claim access to private information or the minimum launch price.

Telegram output must include chain, exact contract/mint, token age, pool/provider, liquidity, configured-notional slippage, holder concentration, authorities, evidence, risk flags, TTL, and invalidation. No automatic purchase.

## Acceptance gates

- Offline fixtures cover official launch, liquidity removal, honeypot/high sell tax, concentrated holders, wash volume, bundled/sniper wallets, fake ticker/contract, proxy/authority risk, and high market cap with poor sellable liquidity.
- Same-ticker contracts remain isolated across and within chains.
- Chain reorg/duplicate/out-of-order events are idempotent and point-in-time safe.
- Losing one RPC/provider degrades only its network and never fabricates data.
- Risk filter rejects candidates that cannot support the configured paper notional.
- Existing CEX/Hyperliquid analysis and phase-2 event pipeline remain compatible.
- Full offline unit/integration tests, migration integrity, compile, secret scan, and diff check pass.
- No transaction signing, wallet seed, private key, or order execution exists.
- Commit message: `phase 3 onchain memecoin intelligence`.
