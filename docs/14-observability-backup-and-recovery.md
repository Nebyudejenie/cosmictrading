# 14 — Observability, Backup and Recovery

## 1. Logs

structlog JSON to stdout: `ts` (UTC), `level`, `event`, `logger`, `process`, `correlation_id` (from `X-Request-ID` or generated; propagated into jobs and audit events), plus context. A redaction processor removes values under sensitive keys (`password`, `secret`, `token`, `api_key`, `authorization`, `cookie`, `totp`, `otp`, `csrf`, `dsn`) and scrubs any configured secret value and known key patterns anywhere in the event. Docker `json-file` driver with `max-size=20m`, `max-file=5` per service. No stack traces reach HTTP responses; they are logged server-side with the correlation ID shown to the user.

## 2. Metrics and health

- `/healthz` (liveness), `/readyz` (DB + migration head).
- `worker_heartbeats` every 15 s; the status page and `/api/v1/status` show heartbeat age; > 60 s raises an incident.
- `/internal/metrics` (token-protected, blocked at the tunnel): process uptime, HTTP request counts by status class, job queue depth and oldest age by kind, job failures, source freshness age per source, AI calls/errors/rate-limited state per provider, AI reported cost (must be 0), orders by status, unknown orders, last reconciliation age and status per account, open incidents by severity, disk free %, DB size.
- A full Prometheus/Grafana stack is not deployed initially (resource budget); the endpoint makes it possible later.

## 3. Incidents and alerts

`incidents` with `dedup_key` (one open incident per key, occurrences counted), severity (`INFO`, `WARNING`, `CRITICAL`), category (`ACCOUNTING`, `SECURITY`, `DATA`, `AI`, `OPS`), owner (`operator`), and resolution. Alerts are notifications created on open and on severity escalation only (rate-limited: the same key at most once per 30 minutes), each stating what happened, what is verified, what is uncertain, current risk, action taken and what the owner must do. Built-in rules: reconciliation failed; unknown order state; protective order degraded; kill switch activated; AI billing signal; audit chain broken; worker heartbeat missing; source stale > 3× limit; disk < 15 % (WARNING) / < 10 % (CRITICAL); config cap breach.

## 4. Backups

| Item | Method | Frequency | Retention |
|---|---|---|---|
| Database | `pg_dump -Fc` as `cosmic_owner`, streamed into **public-key encryption** (`age` recipient key in the backup image; the private key never lives on the VM) | daily 02:30 UTC + before each deploy/migration | 14 daily, 8 weekly, 6 monthly |
| Configuration | `compose.prod.yaml`, `config/`, non-secret `.env` values (allowlisted keys) — encrypted the same way | with each DB backup | same |
| Secrets | **not** included in backups. The owner keeps them in a password manager; the runbook lists which secrets to re-create. | — | — |

Each backup writes a manifest (file name, size, SHA-256, DB size, row counts of critical tables, audit head sequence and hash, git revision). `backup-inventory` lists backups and verifies checksums. VM snapshots (if the owner enables them in Proxmox) are an extra layer, not a substitute.

## 5. Restore and drills

`scripts/restore-drill.sh <backup>`: start an **isolated** `postgres:17-alpine` container on a private Docker network with no ports, decrypt with the drill private key, `pg_restore`, run checks: migration head, `audit_verify()` passes and audit head matches the manifest, row counts match, ledger balances per transaction, every order's events are contiguous, approvals/orders/fills/reconciliation rows present (AT-19). The drill never touches the live database. Production restore (`deploy/restore.sh`) requires `--confirm`, stops `worker` and `web` first, restores into a **new** database name, verifies, then switches by configuration; the old database is kept until the owner removes it.

## 6. Recovery objectives (initial, single VM)

- **RPO:** 24 h for routine data (daily dump); 0 for broker-side truth because reconciliation re-imports broker-confirmed orders/fills. Lower RPO needs WAL archiving to off-VM storage (not configured).
- **RTO:** ~1 h to rebuild a VM from the documented steps and restore the latest backup, given the secrets.

## 7. Migrations

Every migration runs after a pre-migration backup. `downgrade()` is tested in CI for each revision (`tests/integration/test_migrations.py`). Destructive changes use expand → migrate → contract across releases.
