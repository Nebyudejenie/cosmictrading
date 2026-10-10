# 07 — Market Data and Research

## 1. First market: crypto spot (owner decision 2026-10-10)

Instruments: `SPOT:BTC/USD@COINBASE`, `SPOT:ETH/USD@COINBASE` (tradable in paper mode) and the Bitstamp listings `SPOT:BTC/USD@BITSTAMP`, `SPOT:ETH/USD@BITSTAMP` (reference/cross-check only). 24/7 sessions, UTC.

## 2. Sources

| Key | Publisher | Access | Data | Latency label | Freshness limit | Terms status |
|---|---|---|---|---|---|---|
| `coinbase_exchange_public` | Coinbase | Keyless REST `api.exchange.coinbase.com` | products (tick/lot/min funds/status), ticker (bid/ask/last), candles (60/300/900/3600/21600/86400 s; max 300 per request; `[time, low, high, open, close, volume]`, newest first, includes the open bar) | `DELAYED` (polling) | quotes 120 s; 1h bars 2 h | Public market-data endpoints, own use; owner to confirm for jurisdiction |
| `bitstamp_public` | Bitstamp | Keyless REST `www.bitstamp.net/api/v2` | `markets/` (decimals, minimum order value), `ticker/{pair}/`, `ohlc/{pair}/` (`step` 60…259200, `limit` 1–1000, `exclude_current_candle`) | `DELAYED` | quotes 120 s; 1h bars 2 h | Public data functions, no auth; documented limit 400 req/s and 10,000 req/10 min |
| `coinbase_status` | Coinbase (Statuspage) | Keyless `status.coinbase.com/api/v2/status.json` | venue status indicator | `DELAYED` | 15 min | Public status page |

Not used: Kraken and Binance (unreachable from the development network, B-09); Yahoo-style scraping (terms); any paid feed. Nothing is labelled `LIVE`: polling public REST is delayed by at least the polling interval. `LIVE` is reserved for a licensed streaming feed, which does not exist here.

Our polling budget is far below the published limits: quotes every 60 s and bars every 5 min per instrument per venue (≈ 3 requests/min/venue). A per-source token bucket enforces it.

## 3. Ingestion pipeline

1. Fetch (httpx, 10 s timeout, User-Agent `cosmictrading-quant/<version>`), recorded as a `fetch_run`.
2. Parse into typed records; strings → `Decimal` directly (no float round-trip).
3. Validate: `low ≤ open, close ≤ high`, positive prices, non-negative volume, timestamp within [now − horizon, now + 2 min], bid ≤ ask. Failures are counted as rejected with `data_quality_results`.
4. Normalise: canonical instrument via `instrument_listings`; UTC timestamps; bar `open_time` aligned to timeframe.
5. Upsert idempotently on the natural key. The still-open bar is stored with `is_complete = false` and is excluded from indicators and backtests.
6. Update `sources.last_success_at` / `last_error`.

### Freshness labels (`sources.freshness.classify`)

| Label | Meaning |
|---|---|
| `LIVE` | Licensed streaming data within its latency SLA — **not available in this build** |
| `DELAYED` | Fresh within the source's limit, but polled/delayed by design |
| `STALE` | Older than the freshness limit |
| `SIMULATED` | Produced by the paper broker or a simulation |
| `UNKNOWN` | No timestamp or unparseable |
| `UNAVAILABLE` | Source disabled, failing, or never fetched |

Risk check R09 requires `DELAYED` (paper policy) for exposure-increasing actions; anything else blocks.

## 4. Instrument master

Synced daily from Coinbase `products/{id}` (tick = `quote_increment`, lot = `base_increment`, min notional = `min_market_funds`, status, `trading_disabled`, `cancel_only`, `limit_only`, `post_only`) and Bitstamp `markets/` (decimals, `minimum_order_value`, `trading`). Each sync stores provenance (source, fetch run, time). A material change (tick/lot/status) raises a market event and invalidates open proposals on that instrument.

## 5. Analytics (deterministic)

- Indicators (`market/indicators.py`): SMA, EMA (α = 2/(n+1), seeded with SMA), RSI (Wilder smoothing), ATR (Wilder), Bollinger (population σ), MACD (12/26/9), realised volatility (log returns, annualised with √(8760) for hourly bars — disclosed), max drawdown, Pearson correlation of returns. Reference tests: hand-computed small series and an independent naive implementation compared on random series.
- Regime (`market/regime.py`): trend = sign of (SMA50 − SMA200) and slope of SMA50 over 24 bars; volatility = percentile of 24-bar realised vol within the last 720 bars (low < 33 % ≤ normal < 67 % ≤ high). Label = `TREND_UP|TREND_DOWN|RANGE` × `LOW_VOL|NORMAL_VOL|HIGH_VOL`; `INSUFFICIENT_DATA` when fewer than 200 complete bars exist.
- Material-change detectors: 1-bar log return beyond 3σ of the trailing 168 bars; volume beyond 3× the trailing median; cross-source mid deviation beyond the policy limit; regime label change; strategy signal change.

## 6. Strategy research

- Registry: `strategies` → `strategy_versions` (params + code reference → SHA-256 hash). Changing logic or params creates a new version.
- Initial strategy `trend_sma_atr` (transparent, for exercising the pipeline — **not** a claim of edge): long when SMA(fast) crosses above SMA(slow) and regime is not `HIGH_VOL`; exit on opposite cross; stop = entry − k·ATR; take profit = entry + m·ATR. Long-only, spot, no leverage.
- Backtester (`research/backtest.py`): event-driven over complete bars; decision at bar close uses only data with `open_time ≤ t`; fill at the **next** bar's open adjusted by spread/2 + slippage; fees per fill; position sizing via the same `trading.sizing` helper used live; no partial-bar data. Outputs: trades, equity curve, total return, max drawdown, Sharpe (hourly, annualised, disclosed), win rate, exposure time, fees paid, turnover, number of parameter trials.
- Walk-forward: rolling windows (train 60 days, test 15 days); parameters selected on train from a small declared grid; only test-window results are reported as out-of-sample; the total number of configurations tried is disclosed (multiple-testing note).
- Leakage test: perturbing all bars after time t must not change any decision at or before t.
- Survivorship/corporate actions: not applicable to the two spot pairs; recorded as such. Data vintage (fetch-run IDs, bar counts, max ingestion time) is stored with each run.
- Every report carries: "Historical simulation with assumed costs. Not evidence of future profitability." plus sample size and assumption values.

## 7. Opportunity lifecycle

`IDEA → RESEARCH → CHALLENGED → QUALIFIED → APPROVAL_REQUIRED → APPROVED | REJECTED | EXPIRED → EXECUTION → REVIEW`, plus `NON_EXECUTABLE` and `NO_TRADE`. Transitions are recorded in `opportunity_transitions`. No path leads from `IDEA` directly to execution.

## 8. Capability map (not in the first release)

Equities (SEC EDGAR keyless; prices need a keyed provider), FX reference rates (ECB), macro (FRED needs a free key; World Bank keyless), news/filings with claim extraction, broader venues. Each requires licence/terms review, quality evaluation and an owner decision before activation (DATA-11).
