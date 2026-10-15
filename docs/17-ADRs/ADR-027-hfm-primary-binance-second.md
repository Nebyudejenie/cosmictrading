# ADR-027: HFM (MetaTrader 5) primary, Binance second; Coinbase and Bitstamp retired; Exness dropped
- **Status:** Accepted (owner decisions of 2026-10-15) · **Date:** 2026-10-15 · Supersedes the venue parts of ADR-023
- **Context:**
  - The owner chose HFM (HF Markets, client area `my.hfm.com`) as the main broker and Binance as the second venue. Exness, Coinbase and Bitstamp are not to be used.
  - **HFM:** no official HFM trading API was found. HFM's own MetaTrader 5 page names Expert Advisors as its automated-trading route, and its website blocks automated reading. HFM products are leveraged CFDs (forex, metals, crypto).
  - **MetaTrader 5 from Python:** MetaQuotes' official `MetaTrader5` Python package talks to a running MetaTrader 5 terminal on the same computer ("interprocessor communication", mql5.com), and PyPI ships it for Windows only (`win_amd64`). Facts used for the adapter are in `docs/integrations/mt5-python-api.md`.
  - **Exness:** its own API is available only in Vietnam and Thailand and is not usable by MetaTrader accounts (Exness help centre, 2026-10-15).
- **Decision:**
  1. **HFM is the primary venue, through MetaTrader 5.**
     - A bridge program runs on Windows next to the MT5 terminal. The terminal is logged in to the owner's HFM **demo** account, and its credentials stay on that computer.
     - The bridge only makes outbound connections to the system, never accepts inbound ones, and authenticates with its own revocable token.
     - Now it runs on the owner's Windows PC; in production, on a dedicated Windows VM on Proxmox (B-02).
     - If the computer sleeps or disconnects, the venue is unreachable and nothing new is sent (fail closed).
  2. **HFM rule: demo only, buy only, no leverage.**
     - No short positions; a sell only closes or reduces a long.
     - A new position's notional value plus existing exposure may not exceed the account's equity, so the effective leverage is at most 1 whatever the broker's margin allows.
     - Any real-money HFM account needs go-live gates A–I and a separate owner activation (unchanged).
  3. **Binance is second.**
     - Paper trading moves to Binance prices: BTC/USDT and PAXG/USDT, with cash in USDT.
     - Test-network accounts (ADR-026) are unchanged.
  4. **Coinbase and Bitstamp are retired.**
     - No new prices are fetched, and nothing is traded or proposed on them.
     - Their stored prices, bars and the paper-trade record are kept (append-only evidence).
     - Retirement is deployed only after the paper position on Coinbase is closed by an approved exit. Afterwards the worker raises an incident if any account still holds a position or an active order on a retired venue.
  5. **Strategy library.**
     - Rules keep their recorded evaluations on the stored Bitstamp BTC/USD history (2011–2026) and on Binance PAXG/USDT.
     - Live signals and paper proposals use Binance BTC/USDT and PAXG/USDT daily bars: rules judged on long history, applied where the account trades.
  6. **Exness is dropped.** The B-06 work on it stops.
- **Consequences:**
  - The cross-venue price check for BTC/USDT and PAXG/USDT now compares Binance with the Binance test network. That is weaker than an independent exchange, but still catches stale or broken feeds.
  - The HFM link needs:
    - a bridge transport (outbound from Windows, authenticated);
    - an MT5 contract emulator for tests, built only from documented behaviour;
    - CFD accounting: lots, contract size, margin, swaps, commission;
    - a new SANDBOX adapter value;
    - owner steps (HFM demo account, MT5 install, the bridge).
  - The production plan gains a Windows VM next to the Linux VM.
