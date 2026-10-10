# ADR-013: Deterministic risk engine as pure functions; UNKNOWN blocks exposure
- **Status:** Accepted · **Date:** 2026-10-10
- **Decision:** `risk.evaluate(context, policy)` is a pure function over a snapshot; every evaluation is persisted with its inputs; UNKNOWN ⇒ FAIL for exposure-increasing operations; policies are versioned and immutable; approvals bind the policy version, so a change invalidates outstanding approvals rather than letting them pass under new limits.
- **Consequences:** fully unit-testable; explainable (AT-18); no AI dependency.
