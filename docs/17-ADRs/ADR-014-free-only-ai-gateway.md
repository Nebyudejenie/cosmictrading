# ADR-014: Free-only AI gateway with attestations and fail-closed policy
- **Status:** Accepted · **Date:** 2026-10-10
- **Context:** free tiers are account-level (Groq, Gemini) or model-level (OpenRouter `:free`); none offers an API that proves a call is free for Groq/Gemini; Cerebras' free access is a 30-day credit trial; Gemini's official pages could not be fetched on 2026-10-10.
- **Decision:** one OpenAI-compatible adapter with per-provider config; calls require an owner attestation (Groq/Gemini: account without billing), an approved model ID with evidence and review expiry, and for OpenRouter a `:free` ID plus a runtime pricing check of 0 for every price field and no fallback routing. HTTP 402 or reported cost > 0 disables the provider immediately. Cerebras is not allowlisted. No local models, no Ollama.
- **Consequences:** AI is NOT_CONFIGURED until the owner supplies keys and attestations — the honest state today.
