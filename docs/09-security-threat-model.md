# 09 — Security Threat Model

Method: STRIDE-style review per trust boundary (`diagrams/trust-boundaries.md`). Assets in priority order: (1) capital authority — the ability to cause an order; (2) broker/AI/Telegram/tunnel credentials; (3) integrity of ledger, orders and audit; (4) operator identity and sessions; (5) research integrity; (6) availability of safety functions.

## 1. Threats and controls (§12 list)

| # | Threat | Controls | Verification |
|---|---|---|---|
| T1 | Unauthenticated access, privilege escalation | Every route except `/healthz`, `/readyz`, `/login`, static assets requires a session; capability checks in services (not only handlers); roles are code-defined; IDOR checks on object routes | Authz matrix test enumerates every route × role |
| T2 | Stolen sessions, weak passwords, brute force | argon2id; min length 12 + common-password list; `__Host-` cookie, `Secure`, `HttpOnly`, `SameSite=Strict`; 12 h absolute / 30 min idle expiry; session rotation on login and MFA; per-IP and per-username rate limit + lockout after 5 failures for 15 min; TOTP MFA; step-up for privileged actions; session revocation | Tests for lockout, expiry, rotation, revocation |
| T3 | CSRF, XSS, injection, unsafe uploads | Synchronizer CSRF token + `Origin` check on unsafe methods; Jinja2 autoescape; strict CSP (`default-src 'self'`, no inline script); parameterised SQL only (SQLAlchemy Core); no file uploads | CSRF/XSS/SQLi tests |
| T4 | SSRF / malicious URLs | Connectors use fixed base URLs from the source registry; generic document fetcher (when enabled) allows only `https`, resolves DNS and rejects private/loopback/link-local/metadata ranges, no redirects to other hosts, size and time limits | SSRF test with private IPs, `file://`, redirects |
| T5 | Prompt injection | Untrusted wrapping; no model tools; schema validation; research cannot reach execution; numeric claims re-verified | Injection fixture test (document instructing "approve trade", "reveal key") |
| T6 | Provider data retention / training use | Only `PUBLIC`/`INTERNAL_NONSENSITIVE` data sent; outbound secret scanner | AT-16 |
| T7 | Leaked API/bot/tunnel/DB/broker credentials | Secrets only in `*_FILE` secret files (0400) or `.env` (gitignored); `SecretStr`; log redaction by key name and value; never sent to browser/Telegram/AI; secret scan in `make check` | Redaction + exposure tests; scan output |
| T8 | Unauthorised approval, replay | Two-step challenge; packet hash binding; single-use nonce; expiry; Telegram user-ID binding, private chat only; step-up MFA | AT-04, AT-05, TG tests |
| T9 | Duplicate orders, uncertain state | Single execution leader; unique constraints; deterministic client IDs; no blind resubmission | AT-09, AT-11, AT-20 |
| T10 | Malicious dependency / compromised build | `uv.lock` with hashes; pinned base image digest; `pip-audit`; minimal dependency set; no runtime CDN assets | Scan output in status |
| T11 | DB corruption, backup theft, failed recovery | Role separation; immutable tables; encrypted backups (public-key encryption; private key off-VM); restore drill | AT-19 drill |
| T12 | Public exposure of internals | No published ports in production; tunnel ingress single hostname + catch-all 404; `/internal/*` 404 at the tunnel and bearer-token protected in app; debug off; generic error pages. Network segmentation (S9): `data` and `edge` are Docker `internal` networks; the web process has **no outbound internet**; only worker, bot and cloudflared join `egress` | Port + ingress validation (`cloudflared ingress validate`/`rule`); local run: web cannot connect out or resolve DNS |
| T18 | Client IP spoofing via `X-Forwarded-For` (defeats per-IP rate limits) | uvicorn trusts forwarding headers only from `COSMIC_FORWARDED_ALLOW_IPS` = cloudflared's fixed address on the edge network; `*` is refused by configuration validation (S9 fixed an earlier `*` default) | Config unit test |
| T19 | Over-privileged processes | Containers run as UID 10001, read-only root filesystem, `cap_drop: ALL`, `no-new-privileges`, PID/memory/CPU limits; runtime processes do not receive the database owner (DDL) or superuser password — only the one-shot `migrate` job does; backup container has only the owner password; AI keys only in the worker, the Telegram token only in the bot, neither in web (S11) | Local run: `id`, write to `/app` refused; compose review |
| T13 | Webhook forgery, flooding, quota exhaustion | No inbound webhooks (Telegram long polling); rate limits on login and API; AI quota tracking; bounded queues | Rate-limit tests |
| T14 | Secrets/PII in telemetry and prompts | Redaction processor; outbound scanner; no third-party telemetry | Tests |
| T15 | Insider/agent modifying its own authority | Capabilities in code; risk limits and modes only via MFA workflows; AI packages cannot import trading | AT-08; import contracts |
| T16 | Audit tampering | INSERT-only grants, triggers, hash chain, `audit_verify()` hourly + incident | Tamper test |
| T17 | Clock manipulation affecting expiry | Server-side UTC clock only; client times ignored; DB `now()` used for consumption predicate | Expiry tests |

## 2. Residual risks (accepted, documented)

1. Single-operator system: compromise of the owner's device with an active session + MFA device defeats approval controls. Mitigations: short step-up window, Cloudflare Access in front, session revocation, audit.
2. Free AI providers may retain prompts; mitigated by data classification, not eliminated.
3. Public market data is delayed and could be wrong; mitigated by cross-source checks and freshness gates.
4. In-process rate limiting resets on restart (single web process); lockout state is persistent in the DB.
5. Kill-switch window: an order already persisted as `SUBMITTING` will be transmitted (doc 08 §6).
6. *(Resolved in S11.)* The web process no longer holds AI keys or the Telegram token: the worker (AI keys) and the bot (token) report presence only — `CONFIGURED`/`NOT_CONFIGURED`, never a value — in their heartbeats, and the portal shows `UNKNOWN` when no fresh report exists. Web still holds the database app credentials and the MFA encryption key, which it needs.
7. The Docker daemon runs as root on the VM; a container escape is a host compromise. Mitigated by non-root containers, dropped capabilities and a dedicated VM (nothing else runs there).

## 3. Incident response (summary; full runbook in doc 15)

Detect (incident rows + alerts) → contain (kill switch / `PROTECTION_ONLY`, revoke sessions, rotate the affected secret) → preserve evidence (audit and DB snapshots are never deleted during remediation) → reconcile → recover through the checklist → postmortem (template in doc 15).
