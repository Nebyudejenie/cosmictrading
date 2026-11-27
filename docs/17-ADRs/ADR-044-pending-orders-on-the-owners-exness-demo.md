# ADR-044: Pending orders on the owner's Exness demo, held by Exness until gold reaches their price

- **Status:** accepted, 2026-11-27 (S123).
- **Context:** On 2026-11-27 the owner's demo BUY was held back because gold moved in the seconds before they approved it (S122 widened the market order's price range). The owner then asked for pending orders as in MT5: "when I fill the form and press the start trade button, the gold price is down or up, so the trade is not initiated … add this now". The demo's bridge already left a non-IOC limit at the broker. It had no order that waits for gold to *rise* (a BUY) or *fall* (a SELL) to a price.

## Decision

1. **The owner chooses:** the side, the price at which the trade opens, and how long it waits (1, 4 or 8 hours). Lots, stop-loss and take-profit are worked out on the server from the PDF plan at that price, as for a trade at the market. It is proposed for their approval with their code. Nothing is sent before that.
2. **What it is, from where the price is:**
   - **LIMIT:** a BUY below the price, or a SELL above it. It waits for gold to come back to a better price.
   - **STOP_LIMIT:** a BUY above the price, or a SELL below it. It waits for gold to go on to that price, and its stop price is its level.
   - Within a spread of the price, it is refused as a pending order (that is a market trade). More than 3 % away, it is refused as a typing slip.
3. **Held by Exness**, with its stop-loss and take-profit (MT5's BUY/SELL LIMIT and, from bridge 17, BUY/SELL STOP). At its level Exness fills it at the market. A STOP may fill a little past its level in a fast move; this is said in the proposal.
4. **Its time:**
   - The proposal is GTD and lasts the chosen hours. The order's `expires_at` is that time.
   - The bridge leaves it as GTC. The server cancels it once its time is up (`cancel_expired_entries`, every execution cycle). `follow_up_cancels` sends the cancel again until Exness confirms.
   - It is never placed to wait across gold's daily break or the weekend.
   - The owner may cancel it at any time from the page (CANCEL_ORDER).
5. **The price range (R10):** from the price now (0.1 % beyond it) to the order's level. A LIMIT's level side reaches 0.1 % past it, never past halfway to its stop-loss.
6. **One at a time:** a pending order waiting at Exness counts as the demo's trade in flight. Cancel it to place another.
7. **Never on an older bridge:** a bridge before 17 would take a STOP_LIMIT entry as a limit and fill it at once. The adapter refuses to send one (`STOP_ENTRY_VERSION`), and the page refuses to propose one.
8. **The policy:** `allowed_order_types` may name STOP_LIMIT. The demo's "Allow" button adds it, with the owner's code (one new policy version). The default stays LIMIT only.

## Consequences

- The owner can trade the demo at their own price without being at the screen.
- A filled pending order is reconciled like any other trade. A cancelled or expired one tells the owner that nothing filled, and why (S107).
- Unchanged:
  - the real account (its terminal's bridge refuses every order; portal execution LOCKED, ADR-038);
  - the robots;
  - HFM's paths;
  - R10 itself.
- Bridge 17 is installed on terminal 3's demo bridge only (`cosmic-bridge-3d`). The other bridges stay at their versions and never receive a STOP_LIMIT entry.
- Tests:
  - `tests/trading/test_exness_demo.py`: a BUY STOP waits at Exness and opens when gold reaches it; a SELL LIMIT is cancelled by the owner, and another when its hour is up; refusals; an older bridge is never asked;
  - `tests/unit/test_mt5_bridge.py` (bridge 17).
