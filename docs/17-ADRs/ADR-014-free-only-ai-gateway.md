# ADR-014: Free-only AI gateway with attestations and fail-closed policy
- **Status:** Accepted · **Date:** 2026-10-10
- **Context:** free tiers are account-level (Groq, Gemini) or model-level (OpenRouter `:free`); none offers an API that proves a call is free for Groq/Gemini; Cerebras' free access is a 30-day credit trial; Gemini's official pages could not be fetched on 2026-10-10.
- **Decision:** one OpenAI-compatible adapter with per-provider config; calls require an owner attestation (Groq/Gemini: account without billing), an approved model ID with evidence and review expiry, and for OpenRouter a `:free` ID plus a runtime pricing check of 0 for every price field and no fallback routing. HTTP 402 or reported cost > 0 disables the provider immediately. Cerebras is not allowlisted. No local models, no Ollama.
- **Consequences:** AI is NOT_CONFIGURED until the owner supplies keys and attestations — the honest state today.

## Addendum (S81, 2026-11-11): AI keys typed by the owner in the portal

- **The owner's choice** (2026-11-11, asked against the rule "secrets live only in secret files"): API keys are typed in Admin → AI Configuration → API Connections, so the owner can add, replace and revoke them without server access.
- **Storage:** `ai_provider_keys` (migration 0029) holds only AES-GCM ciphertext.
  - The key is derived with HKDF-SHA256 from the server's `secret_key`.
  - The provider is bound in as associated data: a stored key cannot be decrypted for, or sent to, another provider.
  - Only the last four characters are kept, for the mask.
  - One active key per provider; replaced or revoked, never deleted.
  - Every change is audited with the mask only.
- **Use:**
  - The web process only encrypts, masks and revokes.
  - The worker decrypts before each AI call (`keystore.refresh`), registers the values with the log scrubber, and prefers them to a secret file's key.
  - Free AI only (FREE_CLOUD_AI_ONLY, $0, fail closed) is unchanged: a billing signal still turns a route off.
- **The test** (`ai/connection_test.py`, recorded append-only in `ai_provider_tests`): authentication, model availability, a real inference of the gold second opinion on supplied XAUUSD data, and validation against its schema.
  - Timeouts, rate limits, refusals, billing signals and invalid answers are each recorded.
  - It trades nothing.
- **Risk accepted:** whoever holds both the database and the server's `secret_key` can read the keys. That is the same exposure as the secret files on that server.
