# ADR-015: Coinbase Exchange (primary) and Bitstamp (cross-check) keyless public data
- **Status:** Accepted · **Date:** 2026-10-10
- **Context:** owner chose crypto spot first. From the development network, Kraken and Binance APIs time out; Coinbase Exchange, Bitstamp and Gemini respond. Plan originally named Kraken.
- **Decision:** Coinbase Exchange public REST for instruments, quotes and candles (paper fills priced from it); Bitstamp public REST as an independent cross-check; Coinbase status API for venue health. All labelled DELAYED (polling). Fixtures captured on 2026-10-10 back the tests.
- **Consequences:** two independent sources enable disagreement checks; Kraken can be added behind the same contract if the production network reaches it.
