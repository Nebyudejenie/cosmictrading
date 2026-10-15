# ADR-029: One environment label per account; approval terms that match the venue
- **Status:** Accepted · **Date:** 2026-10-16 · Follows the Phase 1 audit (`docs/reports/2026-10-15-audit.md`)
- **Context:**
  - The audit found that every approval packet described its trade as spot crypto: "STOP_LIMIT sell", "24x7 (UTC)", "funding: none (spot, no leverage)".
    - At HFM the stop is a broker stop-loss on the position, filled at the market price.
    - CFDs trade in the broker's hours; gold closes at weekends.
    - Open CFD positions pay or earn an overnight swap. The instrument rules from the bridge carry the swap rates.
  - SANDBOX accounts were all labelled "Binance test network", including the HFM demo account: in Telegram, the packet's account line and the Portfolio heading.
  - The owner's directive of 2026-10-15 asks for an environment label on every proposal and page: PAPER, TESTNET or LIVE (DEMO for a broker demo account).
- **Decision:**
  1. **`trading/modes.environment_of(kind, adapter)`** names every account the same way everywhere:
     - `PAPER`: paper simulator, simulated money;
     - `TESTNET`: Binance Spot Test Network, virtual funds;
     - `DEMO`: HFM MetaTrader 5 demo account, virtual funds;
     - `LIVE`: real money.

     An unknown SANDBOX adapter is a test venue; anything that is neither PAPER nor SANDBOX is LIVE. An unknown account never reads safer than it is.
  2. **The approval packet records the account's adapter and environment.**
     - It gains `account.adapter`, `account.environment` and `instrument.asset_class`.
     - The terms that differ by venue come from the pure module `trading/venue_terms.py`:
       - the stop type and whether it is attached to the entry (by adapter);
       - the session and funding (by asset class, with the broker's swap rates from the instrument rules).
     - Stored packets are never rebuilt; their hashes stay valid. Old packets without an adapter fall back to the account kind.
  3. **Telegram and the portal** use the same label: Telegram prefixes simulated environments with `SIMULATED ·`, and the confirmation screens and Portfolio use the environment label.
- **Consequences:**
  - New packets carry three more keys. The previous image ignores them, so a rollback needs nothing beyond redeploying it.
  - The loss estimate is unchanged: it still assumes the stop fills at the plan's stop-limit price. For HFM the packet says this is a slippage allowance, and that a market fill can be worse in a gap.
  - Swap amounts are shown in the broker's swap units. They are not converted to money, because the bridge does not report the swap mode yet.
- **Addendum (the Telegram card):**
  - New packets also record the proposal's strategy: the rule and version behind its research opportunity, or "manual proposal by the operator".
  - They also record the estimated loss at the stop as a percentage of the account's equity.
  - The Telegram card shows the approved terms on the card itself: instrument and venue, order and band, the quote and its time, stop and target, notional, loss at the stop with its share of equity, the assumed fee, the strategy, and the risk checks passed.
  - Anything an older packet lacks is left out, never invented.
