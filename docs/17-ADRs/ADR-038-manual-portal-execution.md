# ADR-038: The portal's own BUY/SELL for the owner's real HFM account — designed now, built behind its locks, enabled only by the owner

- **Status:** accepted as the design, 2026-11-22 (S113, S114). **Nothing of the sending part is built**; the page's button is locked (`manual/execution.py`).
- **Context:** the owner's Manual Trading specification of 2026-11-22 and its follow-up.
  - They chose "Both: MT5 now, portal execution button later".
  - They place every manual trade in MT5 themselves today. Trader Insight advises and never executes.
  - The portal's own BUY/SELL stays disabled until all nine safety gates pass and they explicitly authorise its activation.
  - When enabled, it must:
    - ask for a final confirmation for every order;
    - validate fresh quotes, the live account, the lot size, margin, stop-loss, target and risk limits on the server;
    - prevent duplicate submissions;
    - confirm HFM's actual execution result;
    - reconcile every order with HFM's position records.

## Decisions

### 1. Two terminals, never one that changes its powers

- **Terminal 3 stays read-only for good.** It runs the investor sign-in and `"read_only": true`; it is the account view the page trusts. Bridge 13 reports `read_only`, and the server shows the account as connected only when it does (S114).
- **Orders, when enabled, go through a separate terminal 4** ("Terminal 4 · real · trading"), with its own bridge, token, slot and adapter (`hfm_mt5_live`).
  - Its bridge runs a live mode that refuses every account but the one activation names, and refuses everything while the server says the activation is not in force.
  - Monitoring never gains trading power by a password change, and a mistake on the trading side never blinds the monitoring side.
- `SLOTS[BRIDGE_REAL].adapter` stays `None`; a test keeps it so (S114, the connection check "The server has no way to send an order to terminal 3").

### 2. The flow of one order

1. **Preview (today's page):** the server sizes the ticket on HFM's own contract from the verified account's equity. It runs every risk rule and shows the costs, the margin and every warning. A browser figure is never used: the form carries the owner's inputs, and the server recalculates.
2. **Summary and confirmation:**
   - "Review order" creates a pending `manual_orders` row: side, lots, order type (market, limit, stop), entry, stop-loss, take-profit, cash at risk and its %, margin, costs, the account fingerprint, the quote and its time.
   - The row has a single-use confirmation key, unique in the table, that expires after 30 seconds.
   - The page shows the summary. The owner confirms with their code (APPROVE_TRADE, step-up).
   - **A stop-loss is required.** An order without one is never offered.
3. **On confirmation, everything again on the server, at that moment:**
   - The quote is under 15 s old and the bridge's clock agrees with HFM's server time.
   - The account is LIVE-CONNECTED on terminal 3, and terminal 4 is signed in to the same fingerprint.
   - The lot size is on HFM's grid, and the margin is within the free margin.
   - Every rule of the owner's current version passes: daily loss, open risk, positions, cooling-off, trades a day, duplicates.
   - Every lock is open: the live adapter registered, the server's ceiling and live venues, authority APPROVAL_REQUIRED, the nine gates PASSED, the owner's activation (money cap and end date) in force, the kill switch off.
   - The price may have moved: when the risk at the new price exceeds the confirmed one by more than one price step, the order is refused, never resized silently.
4. **Duplicates:**
   - The confirmation key is unique. A second click, a retried request or a reloaded page finds the same row and never sends a second order.
   - The bridge journals the client order ID and looks it up before sending and after any uncertain result; that is bridge 7's rule, kept.
5. **Sending:**
   - The order goes to terminal 4 with `expected_account` (the fingerprint) and `ttl_ms` (the owner's speed rule: never sent late).
   - It carries a manual magic number, never a robot's, and the comment "MANUAL", so that a manual trade is never mistaken for an automated one.
6. **What the page says:** "Sent, waiting for HFM", never "trade opened".
   - Only HFM's own answer moves the row on: the result code, the order and deal tickets, the fill price and volume.
   - Then the row is FILLED, PARTIAL, REJECTED (with HFM's reason) or UNKNOWN.
7. **Reconciliation:**
   - Within seconds, terminal 3's report must show the position: the same ticket, volume, stop-loss and take-profit.
   - A mismatch, or no answer, is UNKNOWN: an incident for the owner, and never a retry until HFM's own records settle it.
   - The journal entry is written from HFM's fill, not from the request.
8. **Audit:** every step is in the append-only audit log: preview, review, confirmation, send, HFM's answer and reconciliation.

### 3. What must be tested before the lock opens

These are the evidence for gates C and D, beside the existing suites.

- **Contract tests on an HFM demo, through the same code path** (terminal 4's bridge in live mode against a demo account):
  - market, limit and stop orders, each with a stop-loss and a take-profit;
  - a rejection, a partial fill, a timeout, a requote and a closed market.
- **Adversarial tests:**
  - a double click, a replayed confirmation, an expired key;
  - a stale or skewed quote;
  - another account signed in on terminal 4;
  - a ticket changed after confirmation;
  - the risk exceeded at the new price;
  - the kill switch during sending;
  - the server restarted between send and answer.
- **The read-only side:** terminal 3 still refuses every trading command, and no command for it is ever handed out (`tests/trading/test_broker_connections.py` test 9).

## Consequences

- The page is complete today without any sending part: the owner places trades in MT5, and the journal and results follow HFM's own deals.
- Building terminal 4 and the adapter is its own reviewed change (ADR-034 lock 1). It needs:
  - its contract tests;
  - its own installation on the bridge computer, with the owner's yes;
  - then the gates and the owner's activation.
- Gate G was written for the Binance path (paper fills against Binance's real order book). Whether an HFM fill comparison should be added beside it, never instead of it, is the owner's decision.
