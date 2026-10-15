# ADR-026: Test-network (SANDBOX) accounts on the Binance Spot Test Network
- **Status:** Accepted · **Date:** 2026-10-15
- **Context:** ADR-025 built an unregistered Binance Spot adapter for the Spot Test Network. To trade there through the normal proposal → approval → execution path, the system needs an account that is neither simulated paper money (our own simulator) nor real money.
- **Decision:**
  1. **New account kind `SANDBOX`.** Virtual funds on a real venue's test matching engine. A SANDBOX account must use the `binance_spot_testnet` adapter, a PAPER account the `paper` adapter (unchanged), and LIVE stays as before (migration 0009).
  2. **Authority:** SANDBOX money is not real, so SANDBOX is treated like PAPER. It is allowed in PAPER_TRADING, delayed data is acceptable, and every order, fill and P&L is labelled SIMULATED (testnet). Live modes stay unreachable.
  3. **Its own prices.** The test network has its own order book; on 2026-10-15 its best prices matched the real market but the quantities differed. Test-network instruments get their own venue `BINANCE_TESTNET` and quotes from the test network's public market data (`binance_testnet_public`). Risk checks, price bands and marks therefore use the book the order will meet. Research and the strategy library keep using real-market (EXCHANGE) data only.
  4. **Fees in the received asset.** Binance charges a buy's commission in the base asset (BTC, PAXG). The booked position is the quantity received net of that fee; the cost basis is quantity × price. A quote-asset fee is booked as today. A fee in any other asset (e.g. BNB) is booked in that asset against the unmanaged holdings, left out of P&L, and raised as an incident.
  5. **Capital comes from the venue, and only what the owner chooses is managed.**
     - The test account holds virtual balances the system did not buy. At opening, the owner chooses how much USDT the account manages; it cannot exceed the venue's free USDT.
     - Everything else the venue holds in the tracked assets (USDT and the instruments' base assets) is recorded in a separate ledger book, `UNMANAGED`. It is outside equity, positions and risk.
     - Reconciliation expects, per tracked asset: venue total = ledger CASH or POSITION + UNMANAGED. Other assets on the test account are not compared.
     - A monthly test-network reset therefore shows up as a reconciliation mismatch and an incident, never a silent change. The owner then disables that account and opens a new one.
  6. **One account per key.** The adapter speaks for exactly one venue account (the key's). At most one ACTIVE SANDBOX account may exist per adapter (unique index).
  7. **Remainders below one lot.** A fee in the base asset can leave a holding that is not a multiple of the venue's lot step. Stops, exits and the "open entry" test use the sellable quantity (rounded down to the lot step). A remainder below one step is dust: it stays on the books and in reconciliation, but nothing tries to sell or protect it.
  8. **Registration gate.**
     - The adapter is built in the worker only when the testnet key files are mounted as secrets.
     - `cosmic broker binance-testnet-check` records each run in the append-only `broker_contract_runs`, with a fingerprint of the API key (a truncated SHA-256, never the key), and audits it.
     - A SANDBOX account can only be opened when a PASSED run exists for the key the worker holds. A new key needs a new passing run.
     - No PASSED run means no SANDBOX account (fail closed).
     - Opening runs in the worker, which holds the keys; the portal only queues the request.
- **Consequences:**
  - The full path can be tested on Binance's real matching engine with virtual funds before any live decision: proposal, approval, Binance order, fill, protective stop, exit, reconciliation.
  - Nothing here touches real money or the owner's real-account key.
  - The strategy library and AI missions still propose for PAPER accounts only. Test-network trades are proposed by the owner (the portal's proposal form).
