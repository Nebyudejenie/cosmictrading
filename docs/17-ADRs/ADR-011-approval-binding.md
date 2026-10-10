# ADR-011: Approval binding via packet hash, two-step challenge and atomic consumption
- **Status:** Accepted · **Date:** 2026-10-10
- **Decision:** canonical-JSON packet → SHA-256 hash; review issues a single-use challenge (hashed, 5 min, bound to user/channel/packet); confirm creates one approval (unique nonce, expiry ≤ intent expiry, policy version); execution consumes it with one conditional UPDATE. Telegram callback data carries only the challenge reference. Step-up MFA (portal) or TOTP-in-chat (Telegram, default on).
- **Consequences:** replay, double press, stale button, generic "ok" and cross-channel inference all fail server-side; changing any material term requires a new packet and approval.
