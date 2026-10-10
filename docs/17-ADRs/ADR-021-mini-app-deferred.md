# ADR-021: Telegram Mini App deferred
- **Status:** Accepted · **Date:** 2026-10-10
- **Context:** V6 header lists Telegram Mini App as a primary interface; its detailed requirements are in the missing baseline (B-01).
- **Decision:** first release uses the server-rendered portal and the bot. A Mini App would need `initData` signature verification, a separate origin and a frontend build; it is added later behind the same JSON API.
