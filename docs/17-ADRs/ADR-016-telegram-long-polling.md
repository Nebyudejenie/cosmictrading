# ADR-016: Telegram via outbound long polling
- **Status:** Accepted · **Date:** 2026-10-10
- **Decision:** the `bot` process uses aiogram long polling; no webhook endpoint is exposed. Operator identity = immutable Telegram user ID linked through a one-time portal code; private chats only; handlers call the same services as the portal.
- **Reason:** removes webhook forgery and public ingress for Telegram entirely (SEC-20).
