# ADR-018: Cloudflare Tunnel + Access in front, application auth always on
- **Status:** Accepted (configuration BLOCKED on B-03) · **Date:** 2026-10-10
- **Decision:** remotely-managed tunnel with token in a secret file (`--token-file`); one public hostname for the portal; `/internal/*` and unknown hostnames return 404; Access policy restricted to the owner; app login + TOTP still required; no public SSH hostname; no DNS changes beyond one new CNAME, only after inventory and owner authorisation. No domain is guessed or reused.
