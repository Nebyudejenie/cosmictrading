# ADR-043: An accounting alarm stops its own account; a security alarm stops every account

- **Status:** accepted, 2026-11-26 (S120).
- **Context:** R15 refused every new trade, on every account, while any critical SECURITY or ACCOUNTING incident was open. On 2026-11-26, Robot B's HFM demo failed its reconciliation after the owner's hand trades and refill. That alone refused every trade on the owner's Exness demo, whose own books matched Exness.
- **The owner's choice** (AskUserQuestion, 2026-11-26): "Both". They clear the HFM alarm themselves (close the hand trade, accept HFM's balance), and R15 changes so that an accounting alarm blocks only its own account.

## Decision

1. **An ACCOUNTING incident whose key names one account stops that account only:**
   - a failed reconciliation (`accounting.reconciliation_failed:<alias>`);
   - an unreachable venue (`accounting.venue_unreachable:<alias>…`);
   - a foreign fee (`accounting.foreign_fee:<account id>:…`).
   That account's own trades are still refused by R15, and by R03 (its reconciliation status).
2. **These still stop every account (fail closed):**
   - a SECURITY incident;
   - an ACCOUNTING incident that names no single account (an order's overfill, oversell, wrong direction or fee above the fill).
3. The readiness panel keeps counting every open critical incident.

## Consequences

- One account's books no longer freeze every other account. A system-wide doubt (security, or an order whose account is not named) still does.
- `trading/context_builder.critical_categories(conn, account)`; `tests/trading/test_r15_scope.py`.
