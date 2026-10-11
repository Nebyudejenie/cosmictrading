# 14 — Observability, Backup and Recovery

## 1. Logs

structlog JSON to stdout: `ts` (UTC), `level`, `event`, `logger`, `process`, `correlation_id` (from `X-Request-ID` or generated; propagated into jobs and audit events), plus context. A redaction processor removes values under sensitive keys (`password`, `secret`, `token`, `api_key`, `authorization`, `cookie`, `totp`, `otp`, `csrf`, `dsn`) and scrubs any configured secret value and known key patterns anywhere in the event. Docker `json-file` driver with `max-size=10m`, `max-file=5` per service (set in `compose.prod.yaml` and as the daemon default by `deploy/vm/bootstrap.sh`); journald capped at 1 GB. No stack traces reach HTTP responses; they are logged server-side with the correlation ID shown to the user.

## 2. Metrics and health

- `/healthz` (liveness), `/readyz` (DB + migration head).
- `worker_heartbeats` every 15 s; the command center, `/api/v1/status` and `/internal/metrics` show heartbeat age, and the command center lists a heartbeat older than 60 s under "needs attention". No incident row is written for it (a dead worker cannot write one). Recovery is automatic instead: the container healthcheck fails when the heartbeat file is older than 60 s, and a watchdog thread exits the worker when its loop makes no progress for 120 s, so `restart: unless-stopped` starts a fresh worker whose startup recovery resolves uncertain orders (Docker alone only *marks* an unhealthy container).
- `/internal/metrics` (token-protected, blocked at the tunnel): process uptime, HTTP request counts by status class, job queue depth and oldest age by kind, job failures, source freshness age per source, AI calls/errors/rate-limited state per provider, AI reported cost (must be 0), orders by status, unknown orders, last reconciliation age and status per account, open incidents by severity, disk free %, DB size.
- A full Prometheus/Grafana stack is not deployed initially (resource budget); the endpoint makes it possible later.

## 3. Incidents and alerts

`incidents` with `dedup_key` (one open incident per key, occurrences counted), severity (`INFO`, `WARNING`, `CRITICAL`), category (`ACCOUNTING`, `SECURITY`, `DATA`, `AI`, `OPS`), owner (`operator`), and resolution. Alerts are notifications created on open and on severity escalation only (rate-limited: the same key at most once per 30 minutes), each stating what happened, what is verified, what is uncertain, current risk, action taken and what the owner must do. Built-in rules: reconciliation failed; unknown order state; protective order degraded; kill switch activated; AI billing signal; audit chain broken; worker heartbeat missing; source stale > 3× limit; disk < 15 % (WARNING) / < 10 % (CRITICAL); config cap breach.

## 4. Backups (implemented in S9; `deploy/backup/`)

| Item | Method | Frequency | Retention |
|---|---|---|---|
| Database | `pg_dump -Fc` as `cosmic_owner` from a backup container built on the **same** `postgres:17-alpine` digest as the server, streamed straight into `age` public-key encryption (recipient in `/srv/cosmic/config/backup-recipients.txt`; the private key never lives on the VM). No plaintext dump is ever written to disk. | daily 02:30 UTC (`cosmic-backup.timer`) + before each deploy (`deploy.sh`) | 14 daily, 8 weekly, 6 monthly, and always the 3 newest sets (so a pre-deploy backup survives a later backup the same day) — `cosmic backup prune`, unit-tested |
| Configuration | `/srv/cosmic/config` (non-secret `cosmic.env`, `compose.env`, recipients, deploy history) as an encrypted tar. Compose files and code are in git. | with each DB backup | same |
| Secrets | **not** included. The owner keeps them in a password manager; docs/15 lists which secrets to re-create. | — | — |

Each backup set is four files sharing a stem `cosmic-<UTC>`: `.dump.age`, `.config.tar.age`, `.sha256` (verifiable with `sha256sum -c`) and `.manifest.json` (schema revision, `pg_dump` and `age` versions, the audit head sequence and hash taken before the dump, row counts of every table taken before the dump, file sizes and SHA-256). Counts and audit head are recorded *before* the dump starts, so they are lower bounds for the consistent snapshot that follows. VM snapshots (if the owner enables them in Proxmox) are an extra layer, not a substitute.

## 5. Restore and drills

`deploy/backup/restore-drill.sh <dir> <stem> <identity|->` starts a throwaway container from the backup image with `--network none` (no network at all), streams the identity into a tmpfs inside it (`-` reads it from stdin, so the key never has to be stored on the VM), restores, checks, and removes the container and its volume. Checks: checksums; `pg_restore --exit-on-error`; configuration archive decrypts; schema revision equals the manifest; `audit_verify()` passes; the pre-dump audit head is present with the identical hash; append-only tables hold at least their pre-dump row counts; every ledger transaction balances per asset; every order's event sequence is contiguous and no order lacks events; `UPDATE` and `TRUNCATE` on `audit_events` are refused (triggers survived the restore). Negative cases verified: a tampered dump fails on checksum, a wrong key fails to decrypt.

Production restore (`deploy/backup/restore.sh <dir> <stem> <new-db> <identity|->`) restores into a **new** database on the production server; it refuses a name that already exists and never drops or overwrites anything. The operator runs the drill checks against it, then switches `COSMIC_DB_NAME` and restarts web and worker; the old database is kept until the owner removes it.

## 6. Recovery objectives (initial, single VM)

- **RPO:** 24 h for routine data (daily dump); 0 for broker-side truth because reconciliation re-imports broker-confirmed orders/fills. Lower RPO needs WAL archiving to off-VM storage (not configured).
- **RTO:** ~1 h to rebuild a VM from the documented steps and restore the latest backup, given the secrets.

## 7. Migrations

Every migration runs after a pre-migration backup. `downgrade()` is tested in CI for each revision (`tests/integration/test_migrations.py`). Destructive changes use expand → migrate → contract across releases.
