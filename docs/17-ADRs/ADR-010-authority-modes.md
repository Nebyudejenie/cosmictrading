# ADR-010: Authority modes, APPROVED_EXECUTION as per-order authority, environment cap
- **Status:** Accepted · **Date:** 2026-10-10
- **Context:** V6 §1804 lists seven modes. Treating `APPROVED_EXECUTION` as a global mode would mean "execute anything approved" is a switch, and would conflict with `APPROVAL_REQUIRED` being the live default.
- **Decision:** global persisted modes: RESEARCH_ONLY (default), PAPER_TRADING, APPROVAL_REQUIRED, STRATEGY_AUTHORIZED, PROTECTION_ONLY, EMERGENCY_STOP. `APPROVED_EXECUTION` is recorded on each order created from a consumed approval. More permissive transitions need ADMINISTER_SYSTEM + step-up MFA + reason + preconditions; live modes need a live adapter, all go-live gates PASSED and a separate owner activation. `COSMIC_MAX_AUTHORITY_MODE` can only lower authority; exceeding it at startup forces PROTECTION_ONLY + incident.
- **Stricter-rule resolution:** paper trading also requires per-trade human approval (the directive only requires it for live), so the paper period exercises the real approval path.

## Addendum (2026-10-24): gates as evaluated, and gate I as the owner's own
- **Context:** the review of the $100 plan (S34) found two gaps:
  - `live_preconditions` read the latest recorded gate rows, not the evaluated readiness, so a record of PASSED counted even where the build blocks the gate;
  - `cosmic golive record` could record gate I, the owner's activation, as a CLI actor with no owner authentication.
  - Only the empty `LIVE_ADAPTERS` kept live modes unreachable.
- **Decision:**
  - Live modes require every gate PASSED **as `golive.readiness` evaluates it**.
  - `record_gate` refuses gate I: it can pass only through the owner's own activation with a fresh code, which this build does not have. The CLI no longer offers it.
  - Gates A and E are now evaluated from recorded evidence like B, D and H. The forced BLOCKED with the B-02 reason is gone, since B-02 was resolved on 2026-10-20.
  - Gate C lists each sandbox adapter's latest contract run, and stays BLOCKED while no live adapter exists.
- **Consequences:** recording evidence can pass A, B, D, E and H, but never opens live trading. `tests/e2e/test_command_center.py` covers the refusal of gate I and the evaluated preconditions.
