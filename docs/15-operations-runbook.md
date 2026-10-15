# 15 — Operations Runbook

> Sections state how far they have been verified. Nothing has run on the production VM yet (B-02).

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

## 2. Daily operation

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

## 5. Backup and restore *(verified locally in S9; not yet on the VM)*

**Routine.** `cosmic-backup.timer` runs daily at 02:30 UTC; `deploy.sh` also backs up before each deploy. Check: `ls -l /srv/cosmic/backups` (four files per backup) and `journalctl -u cosmic-backup.service`. Verify integrity at any time: `cd /srv/cosmic/backups && sha256sum -c cosmic-<UTC>.sha256`.

**Keys.** Backups are encrypted to the owner's `age` public key (`/srv/cosmic/config/backup-recipients.txt`). Generate the pair on the owner's own machine (`age-keygen -o cosmic-backup.key`; the public key is printed) and keep the private key in a password manager or offline media — never on the VM. Without it no backup can be restored.

**Monthly drill (AT-19).** On the VM (the key is streamed in and never stored):
```bash
cd /srv/cosmic/src
COSMIC_IMAGE_TAG=<running tag> deploy/backup/restore-drill.sh /srv/cosmic/backups cosmic-<UTC> - < /path/to/cosmic-backup.key
```
(or copy the backup set to the owner's machine and run the same script there). It must end with `DRILL PASS`. Record the date and result.

**Disaster recovery.**
1. Stop new exposure first: Operations → Emergency stop (if the portal still works).
2. Restore into a **new** database: `deploy/backup/restore.sh /srv/cosmic/backups cosmic-<UTC> cosmic_restored_<date> - < key`.
3. Check it: `docker compose -f deploy/compose.prod.yaml exec -T postgres psql -U postgres -d cosmic_restored_<date> -c "SELECT * FROM audit_verify()"`.
4. Set `COSMIC_DB_NAME=cosmic_restored_<date>` in `/srv/cosmic/config/cosmic.env`, then `systemctl restart cosmictrading`.
5. Reconcile every account before resuming; broker/venue truth wins over restored rows. Keep the old database until the owner decides to drop it.

**Total loss of the VM.** Recreate it (doc 12 §2–3), re-create the secrets (database passwords — the restored roles get new passwords from `migrate`'s bootstrap; app secret key — **MFA enrolments are encrypted with it, so a new key means re-enrolling authenticators**; metrics token; tunnel token; Telegram token; AI keys; broker keys when they exist), restore the latest backup, reconcile.

## 6. Deployment and updates *(scripts verified by dry run; VM blocked, B-02)*

```bash
cd /srv/cosmic/src && git fetch && git checkout <sha>
deploy/deploy.sh --dry-run --build <sha>   # read the plan
deploy/deploy.sh --build <sha>             # backup → build → migrate → start → smoke tests
```
Rollback: the script prints the exact commands (previous tag in `compose.env`). If the new release added a migration the old one cannot run against, restore the pre-deploy backup into a new database (§5).

First deployment on a new VM: `create-vm.sh` (on the Proxmox host) → clone the repo to `/srv/cosmic/src` → `sudo deploy/vm/bootstrap.sh --mgmt-cidr <LAN>/24 --apply` → edit `cosmic.env`/`compose.env`, place the backup recipient → `deploy/deploy.sh --build <sha>` → create the owner: `docker compose -f deploy/compose.prod.yaml run --rm --no-deps --entrypoint cosmic migrate admin create-owner` → sign in and enrol MFA through the break-glass relay (`deploy/compose.admin-access.yaml`: portal on the VM's 127.0.0.1:8743 only, reached with `ssh -L 8743:127.0.0.1:8743 cosmicadmin@<vm>`; not while the tunnel profile is up; remove it afterwards). The same relay design is what the local verification uses.

**Never** publish the portal port on the LAN, and never use `docker compose down -v` on the production project (it deletes the database volume).

## 7. Configuration reference *(completed in S1)*

See `.env.example` for every variable with its meaning and safe default.


## 8. Staging server: access and routine commands (2026-10-12)

**SSH.** From the workstation: `ssh cosmictrading` (internet path: Cloudflare Access with the service token in `~/.secrets/cosmictrading-access-service.json` → jump container → server) or `ssh cosmictrading-lan` (home network). From another computer: install `cloudflared`, add that computer's public key to `~/apps/cosmictrading/config/bastion_authorized_keys` on the server (then `docker compose -f deploy/cloudflared/compose.ssh-access.yaml restart bastion`), and use
```
Host cosmictrading-jump
    HostName ssh.cosmictrading.bond
    User jump
    ProxyCommand cloudflared access ssh --hostname %h     # opens the browser login (owner e-mail)
Host cosmictrading
    HostName host.docker.internal
    User cosmic
    ProxyJump cosmictrading-jump
```

**Deploy a new version:** commit and push, then `scripts/deploy-staging.sh` (add `COSMIC_STAGING_SSH=cosmictrading-lan` at home). It refuses an uncommitted or unpushed checkout, fixes the tag before building and re-checks it afterwards, streams the images, runs `deploy.sh` (pre-deploy backup, migrations, smoke tests), the server checks and a public `/readyz` through Cloudflare Access. Manual equivalent:
```bash
docker build -f deploy/Dockerfile -t cosmictrading:<sha> . && docker build -t cosmictrading-backup:<sha> deploy/backup
docker save cosmictrading:<sha> cosmictrading-backup:<sha> | gzip -1 | ssh cosmictrading 'gunzip | docker load'
ssh cosmictrading 'cd ~/apps/cosmictrading/src && git fetch && git checkout <sha> && COSMIC_ROOT=$HOME/apps/cosmictrading deploy/deploy.sh <sha>'
```

**Check:** `ssh cosmictrading 'cd ~/apps/cosmictrading/src && COSMIC_ROOT=$HOME/apps/cosmictrading COSMIC_SHARED_HOST=1 COSMIC_COMPOSE_OVERLAY=deploy/compose.test-server.yaml deploy/vm/verify.sh'`

**Restore drill** (key streamed from the workstation, never stored on the server):
`ssh cosmictrading 'cd ~/apps/cosmictrading/src && COSMIC_IMAGE_TAG=<sha> deploy/backup/restore-drill.sh ../backups <stem> -' < ~/.secrets/cosmictrading-backup-age.key`

**Never** run `deploy/vm/bootstrap.sh` on this shared server (it changes SSH, firewall and Docker daemon settings for every project there).

**Telegram link without the portal:** `ssh cosmictrading 'cd ~/apps/cosmictrading/src && set -a && . ../config/compose.env && set +a && docker compose -f deploy/compose.prod.yaml -f deploy/compose.test-server.yaml run --rm -T --no-deps --entrypoint cosmic migrate admin telegram-link-code nebyu --minutes 30'` prints `/link CODE` (single use, 1–60 minutes, audited as CLI); send it to @cosmictradingBot from the owner's Telegram. The bot leaves any group or channel it is added to.

**GitHub.** Fetch over HTTPS; push over SSH with the repository deploy key `~/.ssh/cosmictrading_github_deploy` (alias `github-cosmictrading`; it can also update workflow files, which the gh CLI token cannot). CI (`.github/workflows/ci.yml`) runs `make check` against a throwaway database, shellcheck and the image builds on every push to `main`.

**Server account.** The `cosmic` password was rotated on 2026-10-12; the new one is only in `~/.secrets/cosmic-server-password.txt` on the workstation (needed for sudo; SSH uses keys).

## 9. Binance Spot Test Network contract check (B-07)

Runs the broker contract against the **real** Spot Test Network from the worker container. It places one tiny limit order far below the market and cancels it, using virtual funds. Keys go over stdin, never as arguments:

```sh
ssh cosmictrading 'python3 -c "import json,pathlib; h=pathlib.Path.home()/\".secrets\"; print(json.dumps({\"api_key\": (h/\"binance-testnet-api-key.txt\").read_text().strip(), \"private_key_pem\": (h/\"binance-testnet-ed25519.pem\").read_text()}))" | docker exec -i cosmictrading-worker-1 cosmic broker binance-testnet-check'
```

It prints `PASS`/`FAIL` per step and `CONTRACT PASSED …`. Only after it passes may the adapter be registered (ADR-025), which also needs a testnet account kind.

