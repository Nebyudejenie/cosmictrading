# ADR-012: Single serialized execution authority with persist-before-send
- **Status:** Accepted · **Date:** 2026-10-10
- **Decision:** one worker holds a Postgres advisory lock and is the only code path calling `submit_order`. Order rows are persisted as SUBMITTING before transmission; client order IDs are deterministic per intent and purpose; timeouts become UNKNOWN_ORDER_STATE resolved only by reconciliation; a startup sweep handles crashes. `authority_state` is read `FOR SHARE` in authorising transactions so the kill switch serialises with submissions.
- **Residual risk:** an order already marked SUBMITTING when the kill switch commits is transmitted; it is reconciled and handled under the emergency policy (documented in doc 08/09).
