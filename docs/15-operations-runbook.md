# 15 — Operations Runbook

> Initial version written at design time. Each section is completed and re-verified as the corresponding slice lands; sections marked *(pending S9/S10)* describe the intended procedure and have not been executed yet.

## 1. Local development

```bash
make setup          # uv sync (dev dependencies), generate .env with random local secrets
make db-up          # start dev PostgreSQL on 127.0.0.1:55442 (compose project cosmictrading-dev)
make migrate        # bootstrap roles + alembic upgrade head
uv run cosmic admin create-owner   # prompts for username and password (never as arguments)
make run-web        # http://127.0.0.1:8742
make run-worker
make check          # format, lint, types, secret scan, tests
```

## 2. Daily operation *(pending S8)*

- Open the command center: mode banner, proposals awaiting approval, incidents, health vs account health.
- Review proposals in plain language, then details; approve only the exact terms you want executed.
- Watch reconciliation status; anything other than `OK` blocks new exposure by design.

## 3. Kill switch

Portal → Operations → **Emergency stop** (or Telegram `/stop <reason>`). Effect: no new exposure, approvals revoked, unfilled entry orders cancelled (default policy), protective orders and positions kept, reconciliation queued. Stopping new entries does **not** close positions.

Resume: complete the recovery checklist (reconciliation passed after the stop, no unknown orders, incidents acknowledged, positions reviewed, reason), confirm with MFA, choose a mode no more permissive than before the stop.

## 4. Incidents

| Incident | First actions |
|---|---|
| Reconciliation failed | Do not resume. Open the run, compare broker vs internal items, check fills imported, contact venue if needed. |
| Unknown order state | Wait for reconciliation; never resubmit manually; if unresolved, query the venue directly and record the outcome. |
| AI billing signal | Provider is auto-disabled. Check the provider account for billing/credits; remove the payment method; re-attest only when free status is confirmed. |
| Audit chain broken | Treat as a security incident: stop, preserve the database (backup now), do not "fix" rows, investigate access logs. |
| Secret leak suspected | Revoke/rotate the secret at the provider, replace the secret file, restart services, revoke all sessions, review audit. |
| Disk critical | Background work pauses automatically; prune old backups per retention, check log sizes, extend the disk. |

### Postmortem template

Summary · Impact (financial, data, availability) · Timeline (UTC) · Detection · Root cause · What went well · What went wrong · Actions (owner, due date) · Evidence references (audit seq range, incident IDs, backups).

## 5. Backup and restore *(pending S9)*

Daily encrypted backups to `/srv/cosmic/backups`; `scripts/backup-inventory.sh` lists and verifies them; `scripts/restore-drill.sh <file>` restores into an isolated container and verifies. Keep the `age` private key off the VM (password manager or offline media). Secrets to re-create after a total loss: database passwords, app secret key, metrics token, tunnel token, Telegram bot token, AI provider keys, broker keys.

## 6. Deployment and updates *(pending S9)*

See doc 12: `deploy/deploy.sh --dry-run <version>`, then without `--dry-run`. Pre-deploy backup is automatic. Rollback: redeploy the previous version; restore only if a migration made data incompatible.

## 7. Configuration reference *(completed in S1)*

See `.env.example` for every variable with its meaning and safe default.
