# ADR-013: Deterministic risk engine as pure functions; UNKNOWN blocks exposure
- **Status:** Accepted · **Date:** 2026-10-10
- **Decision:** `risk.evaluate(context, policy)` is a pure function over a snapshot; every evaluation is persisted with its inputs; UNKNOWN ⇒ FAIL for exposure-increasing operations; policies are versioned and immutable; approvals bind the policy version, so a change invalidates outstanding approvals rather than letting them pass under new limits.
- **Consequences:** fully unit-testable; explainable (AT-18); no AI dependency.
- **Addendum (2026-10-16, the owner's directive Phase 5): two limits added.**
  - **R18 Concurrent positions** (`max_open_positions`, default 3). It counts instruments held (at least one lot; dust is not a position), instruments with a resting buy, and the new trade's instrument.
  - **R19 Total open risk** (`max_open_risk_pct`, default 3 % of equity). It is the loss if every stop is hit:
    - held quantity covered by stops, down to their stop-limit;
    - uncovered quantity at its full mark value;
    - resting entries and the new trade at their own approved stop-limit, or their full notional when they have none.

    A missing mark or equity is UNKNOWN and blocks new exposure.
  - The defaults apply to policy versions stored before the fields existed, a deliberate tightening. A trade without a stop is now refused above 3 % of equity.
  - Fields added later are hashed only when they differ from their default (`ADDED_LATER`), so stored hashes stay valid. Those defaults must never change.
