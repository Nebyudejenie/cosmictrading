# ADR-008: UUIDv7 identifiers; separate human trade codes
- **Status:** Accepted · **Date:** 2026-10-10
- **Decision:** primary keys are UUIDv7 generated in Python (RFC 9562; implemented locally because Python 3.12 lacks `uuid.uuid7`). Trades also get a human code `T-YYYYMMDD-XXXX` (Crockford base32, unique).
- **Consequences:** time-ordered keys index well; codes are for humans and chat, never foreign keys.
