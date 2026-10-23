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
- **Daily report** (Telegram and the portal's alerts) at the first check after 05:00 UTC (08:00 in Addis Ababa; `COSMIC_DAILY_REPORT_HOUR_UTC`):
  - mode, worker, incidents, failed jobs, backups, waiting proposals;
  - each account on its own line, in its own currency and environment: equity, cash, realised and unrealised P&L, limit use, reconciliation, open positions with their stop;
  - trades closed in the last 24 h;
  - strategy status.
- **`/status` in Telegram** gives the same health lines at any time.
- **Weekly report** (Mondays) and **monthly report** (the 1st), at the same hour, for the last complete ISO week or calendar month in UTC:
  - each account's equity change against the period before, and its largest drawdown in the period;
  - trades closed (won and lost), realised P&L and fees per currency;
  - incidents (the longest named);
  - backups and strategies;
  - the definitions used.

**Scheduled jobs.** **Operations → Workers → Scheduled jobs** lists every scheduled job with its interval, last success, run time and state. Incidents:
- a job with no success for three intervals: `ops.job_overdue:<job>`;
- three failures in a row: `ops.job_failing:<job>`.

Both are CRITICAL for `trading.*` jobs and WARNING otherwise, and both close after the next success. After a worker restart, the last successes are read back from the job history; run times show again after the next run.

**Protective order ended at the venue.** `trading.protection_ended_at_venue:<order>` (WARNING) means a stop was cancelled or expired at the venue although COSMICTRADING did not ask. A replacement at the same level is placed on the next cycle.
- Check the venue's own order history.
- If nobody you know did it, treat the account's access as compromised: rotate its API keys or password, and consider the emergency stop.

**Cancel not confirmed.** `trading.cancel_unconfirmed:<order>` (CRITICAL for an entry, WARNING otherwise) means a cancel was sent five times, a minute apart, and the venue still shows the order open, or could not be reached. The cancel keeps being sent every 5 minutes until the venue confirms it.
- If you can, cancel the order in the venue's own app (HFM: MetaTrader 5). During HFM's closed hours that is not possible either; the next send after the market opens cancels it.
- Then press **Reconcile** on Portfolio. The incident closes by itself once the venue reports the order finished.
- An entry that fills first becomes a position, protected by its plan.

**Execution step failing.** `trading.cycle_step_failing:<step>` (CRITICAL) means one step of the execution cycle failed in three cycles in a row: `executed`, `released`, `synced`, `managed`, `stopped` or `cancels_resent`. The other steps keep running every cycle, and the incident closes by itself when the step works again.
- The worker log names the error (`execution_step_failed`).
- Common causes: a risk policy that fails its integrity check, or a venue adapter missing in the worker (for example the test-network keys).

**Order missing at the venue.** `trading.order_missing_at_venue:<order>` (WARNING) means the venue has answered "no such order" for more than a minute for an order it had acknowledged. The Binance test network does this to every order when it resets, about once a month. The order is recorded as ended (`CANCELLED` if a cancel was pending, `EXPIRED` otherwise), and a protective order is replaced under its plan.
- Check the venue's order and trade history.
- The next reconciliation compares balances and positions. A fill the venue lost with the order shows there as a mismatch; none is invented.

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

## 5. Backup and restore *(on the production VM since 2026-10-19)*

**Routine.** `cosmic-backup.timer` runs daily at 02:30 UTC; `deploy.sh` also backs up before each deploy. Check: `ls -l /srv/cosmic/backups` (four files per backup) and `journalctl -u cosmic-backup.service`. Verify integrity at any time: `cd /srv/cosmic/backups && sha256sum -c cosmic-<UTC>.sha256`.

**Keys.** Backups are encrypted to the owner's `age` public key (`/srv/cosmic/config/backup-recipients.txt`). Generate the pair on the owner's own machine (`age-keygen -o cosmic-backup.key`; the public key is printed) and keep the private key in a password manager or offline media — never on the VM. Without it no backup can be restored.

**Alerts.** The worker checks the backup directory every 30 minutes; **Operations → Workers → Backups** shows the newest set and the last drill. Incidents open when the newest set is over 26 h old or incomplete, or the monthly drill is due or failed (ADR-019 addendum). Each one resolves by itself once fixed.

**Monthly drill from the workstation (production's backups).** The private key stays on the owner's machine:
```bash
scripts/drill-backups.sh            # newest set; the age identity is the script's default file
```
It copies the newest encrypted set from `/srv/cosmic/backups` on the VM (`COSMIC_SSH`, default `cosmictrading`). If the workstation lacks the backup image of the release production runs, it builds it from that commit's `deploy/backup` (the VM builds the release images). It runs the drill locally in a network-less container, appends the result to `restore-drills.jsonl` next to the backups (the worker reads it), and deletes the local copy. It must end with `PASS`. Add `--negative` (at least once a quarter) to also prove that the drill can fail: a copy with one flipped byte in the dump and a freshly generated wrong key must both be refused (the result line records `"refused":"tampered,wrong-key"`).

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

## 6. Deployment and updates *(on the production VM since 2026-10-19; routine releases: §8)*

```bash
cd /srv/cosmic/src && git fetch && git checkout <sha>
sudo deploy/deploy.sh --dry-run --build <sha>   # read the plan
sudo deploy/deploy.sh --build <sha>             # backup → build → migrate → start → smoke tests
```
Production refuses a tag that has not run on staging (CF-10): staging's `deploy.sh` records a tag only after its smoke tests passed, and a tag production ran before is accepted (a rollback). Only when there is no staging, for example the first deployment on a new VM, pass `--untested`; production's `deploy-history.log` then marks that line `UNTESTED`.

Rollback: the script prints the exact commands (previous tag in `compose.env`). If the new release added a migration the old one cannot run against, restore the pre-deploy backup into a new database (§5).

First deployment on a new VM: `create-vm.sh` (on the Proxmox host) → clone the repo to `/srv/cosmic/src` → `sudo deploy/vm/bootstrap.sh --mgmt-cidr <LAN>/24 --apply` → edit `cosmic.env`/`compose.env`, place the backup recipient → `sudo deploy/vm/staging-init.sh --apply`, then `scripts/release.sh --production` (staging first; without staging: `sudo deploy/deploy.sh --build --untested <sha>`) → create the owner: `docker compose -f deploy/compose.prod.yaml run --rm --no-deps --entrypoint cosmic migrate admin create-owner` → sign in and enrol MFA through the break-glass relay (`deploy/compose.admin-access.yaml`: portal on the VM's 127.0.0.1:8743 only, reached with `ssh -L 8743:127.0.0.1:8743 cosmicadmin@<vm>`; not while the tunnel profile is up; remove it afterwards). The same relay design is what the local verification uses.

**Never** publish the portal port on the LAN, and never use `docker compose down -v` on the production project (it deletes the database volume).

## 7. Configuration reference *(completed in S1)*

See `.env.example` for every variable with its meaning and safe default.


## 8. The production VM: access and routine commands (2026-10-19)

Production and staging run on one dedicated VM (Ubuntu, Docker Compose). The shared test server that held staging from 2026-10-12 is retired: its stack is stopped with restart disabled, its backup cron line is commented out, and its volumes are kept until the owner decommissions it. Production took over the public name `staging.cosmictrading.bond` without a DNS change.

| | Production | Staging |
|---|---|---|
| Root on the VM | `/srv/cosmic/{src,config,secrets,backups}` | `/srv/cosmic-staging/{src,config,secrets,backups}` (`deploy/vm/staging-init.sh`) |
| Compose | project `cosmictrading`, `deploy/compose.prod.yaml` | project `cosmictrading-staging`, plus `deploy/compose.staging.yaml` (named in its `compose.env`) |
| Reached at | `https://staging.cosmictrading.bond` (Cloudflare Access, then app login and MFA) | only the VM's `127.0.0.1:8744`: `ssh -L 8744:127.0.0.1:8744 cosmictrading-lan`, then http://127.0.0.1:8744 |
| Tunnel, Telegram bot, HFM bridge | yes | never (`deploy.sh` refuses those profiles for staging) |
| Data | moved from the old server on 2026-10-19 (audit chain identical: 4,637 events, same head hash) | its own, empty at the start; its own secrets, live tokens empty |
| Backups | `cosmic-backup.timer`, daily 02:30 UTC | `cosmic-staging-backup.timer`, daily 03:30 UTC |
| At boot | `cosmictrading.service`, and Docker's restart policy | Docker's restart policy |

**SSH.** From the workstation: `ssh cosmictrading` (internet path: Cloudflare Access with the service token from the owner's secret files → jump container → the VM's sshd) or `ssh cosmictrading-lan` (home network). SSH accepts keys only.
- The jump container and its own tunnel connector run on the VM: `deploy/cloudflared/compose.ssh-access.yaml`, project `cosmictrading-ssh`. The firewall lets only its network (`br-cosmic-jump`, 10.203.65.0/29) reach sshd, besides the LAN.
- Start or update it:
  ```bash
  cd /srv/cosmic/src && COSMIC_SECRETS_DIR=/srv/cosmic/secrets COSMIC_CONFIG_DIR=/srv/cosmic/config \
    COSMIC_BASTION_TAG=<tag> COSMIC_HOST_UID=10001 COSMIC_HOST_GID=10001 \
    docker compose -f deploy/cloudflared/compose.ssh-access.yaml up -d
  ```
- From another computer: install `cloudflared`, and add that computer's public key in two places: `/srv/cosmic/config/bastion_authorized_keys` for the jump container (root-owned: `sudoedit`), and `~cosmic/.ssh/authorized_keys` on the VM, since its sshd checks the key again. Then restart the `bastion` service the same way, and use:
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

**Release a new version.** Commit and push, then, in a terminal:
```bash
scripts/release.sh                 # build on the VM, deploy to staging, check staging
scripts/release.sh --production    # the same, then the same images to production and its checks
```
- It refuses an uncommitted or unpushed checkout, and a second run while one is going.
- It moves each checkout on the VM to exactly that commit. `deploy.sh --build` refuses any other.
- The VM builds the images, so nothing large crosses the owner's line.
- Each root runs `deploy.sh` (pre-deploy backup, migrations, smoke tests) and then `verify.sh`. Any failure stops the run before production.
- With `--production`, it also checks `/readyz` through Cloudflare Access.
- `sudo` runs `deploy.sh` and `verify.sh` without a password: `/etc/sudoers.d/cosmic-deploy`, which `bootstrap.sh` installs and which the owner chose on 2026-10-20, allows those two scripts and nothing else. Without the rule, run the script in a terminal and type the password. Add `COSMIC_VM_SSH=cosmictrading-lan` at home.

Manual equivalent for one root:
```bash
ssh -t cosmictrading 'cd /srv/cosmic-staging/src && git fetch -q && git checkout --detach <sha> && sudo /srv/cosmic-staging/src/deploy/deploy.sh --build <tag>'
```
Production uses `/srv/cosmic` and no `--build`: the images are already on the VM. It refuses a tag staging has not run (§6).

**Check:** `ssh -t cosmictrading 'sudo /srv/cosmic/src/deploy/vm/verify.sh'` (production, 15 checks) and the same with `/srv/cosmic-staging/src` (staging, 11 checks). Both must end with `VM VERIFIED`.

**Restore drill:** `scripts/drill-backups.sh` (§5).

**Staging owner account** (once; the owner chooses the password and enrols MFA through the loopback address above):
```bash
ssh -t cosmictrading-lan 'cd /srv/cosmic-staging/src && set -a && . ../config/compose.env && set +a && docker compose -f deploy/compose.prod.yaml -f deploy/compose.staging.yaml run --rm --no-deps --entrypoint cosmic migrate admin create-owner'
```

**Telegram link without the portal:** `ssh cosmictrading 'cd /srv/cosmic/src && set -a && . ../config/compose.env && set +a && docker compose -f deploy/compose.prod.yaml run --rm -T --no-deps --entrypoint cosmic migrate admin telegram-link-code nebyu --minutes 30'` prints `/link CODE` (single use, 1–60 minutes, audited as CLI); send it to @cosmictradingBot from the owner's Telegram. The bot leaves any group or channel it is added to.

**GitHub.** Fetch over HTTPS; push over SSH with the repository's deploy key (SSH alias `github-cosmictrading`). CI (`.github/workflows/ci.yml`) runs `make check` against a throwaway database, shellcheck and the image builds on every push to `main`.

**Files and permissions.**
- `config/` and `secrets/` belong to root, group 10001 (the containers' user), mode 0750. Secret files are 0440. Changing one needs `sudo`.
- `backups/` belongs to 10001, mode 0750, with files 0640. The backup container writes them; the `cosmic` account reads them through group 10001.
- `restore-drills.jsonl` is group-writable (0660), so the drill can append its result line.
- Docker's group gives root on the VM, so only the admin account belongs to it.

**Server account.** The `cosmic` password is needed only for `sudo` (deploys, checks, root-owned files). Password login over SSH is off. Keep the password in the owner's password manager.

**Rollback to the old server** (only if the VM cannot serve; data written on the VM since the move would need its backups):
1. On the VM, stop production and keep it from starting at boot. `systemctl stop` alone is not enough: the stack keeps running if the unit is not active, for example after `deploy.sh` started it. Run this over the home network, because step 2 stops the internet path:
   ```bash
   ssh -t cosmictrading-lan 'sudo systemctl disable cosmictrading && cd /srv/cosmic/src && set -a && . ../config/compose.env && set +a && docker compose -f deploy/compose.prod.yaml stop'
   ```
   Then check that `docker ps --filter label=com.docker.compose.project=cosmictrading` lists nothing. Sourcing `compose.env` sets the profiles, so the bot and the tunnel connector stop too.
2. On the VM, stop the `cosmictrading-ssh` project (the old server's SSH connector uses the same tunnel).
3. On the old server: put the live tokens back (steps with the owner), set the containers' restart policy back to `unless-stopped`, and start the stack with its test-server overlay. Start its SSH project last.

## 9. Binance Spot Test Network: keys, contract check, test-network account (B-07, ADR-026)

Virtual funds only. The test network has its own order book, which Binance resets about once a month.

**Keys.** The worker reads two Docker secrets, files in `/srv/cosmic/secrets/` on the VM (root, group 10001, 0440, like the other secrets). An empty file means "not configured". The web and bot containers never receive them.

| File | Content |
|---|---|
| `binance_testnet_api_key` | the API key Binance shows after the public key is registered on testnet.binance.vision |
| `binance_testnet_private_key` | the Ed25519 private key (PEM); generated on the old server, moved to the VM on 2026-10-19 without being stored anywhere else |

The public key to register is the one in `docs/BLOCKERS.md` (B-07); it is public. To save a new API key without echoing it, then make the worker read it:

```sh
ssh -t cosmictrading 'read -rs -p "Testnet API key: " k; echo; printf %s "$k" | sudo tee /srv/cosmic/secrets/binance_testnet_api_key >/dev/null; unset k'
ssh cosmictrading 'docker restart cosmictrading-worker-1'
```

**Contract check.** It runs the broker contract against the real test network. It places one tiny limit order far below the market and cancels it. Every run is recorded (append-only `broker_contract_runs`, audited) with a fingerprint of the API key, never the key itself:

```sh
ssh cosmictrading 'docker exec cosmictrading-worker-1 cosmic broker binance-testnet-check --from-secrets'
```

It prints `PASS`/`FAIL` per step, the run ID and key fingerprint, and then `CONTRACT PASSED` or `CONTRACT FAILED`. Error `-2015` means Binance does not recognise the API key (wrong key, or a different public key registered). The latest run shows on **Operations → Binance test network**. Keys can also be passed as JSON on stdin (`{"api_key": …, "private_key_pem": …}`) instead of `--from-secrets`.

**Opening the account.** On **Operations → Binance test network**, give an alias and the USDT to manage (a fresh code is needed). The worker then:

1. Checks that a PASSED run exists for the key it holds.
2. Reads the test account's balances.
3. Books the chosen USDT as the account's capital and everything else in USDT, BTC and PAXG as `UNMANAGED`.
4. Reconciles.

If any step refuses, nothing is created and an incident explains why. Only one test-network account can be open per key.

**Trading it.**
- **Risk policy → What may be traded:** add `SPOT:BTC/USDT@BINANCE_TESTNET` (and/or `SPOT:PAXG/USDT@BINANCE_TESTNET`), venue `BINANCE_TESTNET` and currency `USDT`. This is a new policy version and needs a fresh code. Added entries must exist and be tradable, with their venue and currency allowed.
- **Proposals:** use the manual proposal form with the account's alias and that instrument.
- **Price check:** the risk checks compare the test network's price with the real Binance price for the same pair.

**After a Binance reset.**
- The account's reconciliation fails (balances and trades no longer match) and an incident is raised. New exposure on it stays blocked.
- Retiring that account and opening a new one is not built yet (follow-up). Until then the account stays blocked, which is safe.

## 10. Retiring a venue (ADR-027: Coinbase and Bitstamp)

A retired venue's sources are no longer polled and its instruments are no longer tradable. Everything stored stays: prices, bars, orders, fills and the audit trail. Retirement is a setting, so it can be undone.

1. **Close what is open there first.**
   - On **Portfolio**, propose closing each position on that venue and approve it. For Coinbase this is `paper-main` (0.0024196 BTC on 2026-10-15).
   - Wait until the exit is filled and the stop is cancelled.
2. **Set the retirement** in `/srv/cosmic/config/cosmic.env` on the VM (root-owned: `sudoedit`):
   ```
   COSMIC_RETIRED_VENUES=["COINBASE","BITSTAMP"]
   ```
3. **Recreate the web and worker containers** so they read it (`docker restart` does not reread the environment file):
   ```sh
   ssh cosmictrading 'cd /srv/cosmic/src && set -a && . ../config/compose.env && set +a && docker compose -f deploy/compose.prod.yaml up -d --no-deps --force-recreate web worker'
   ```
4. **Check:**
   - **Sources** shows the retired sources disabled, and no stale-data incident appears for them.
   - The paper form offers only Binance.
   - If anything is still held or working on a retired venue, the worker raises *"Positions or orders remain on retired venue …"* within 10 minutes. To fix it, remove the venue from the setting, close the position, and retire the venue again.

To undo, remove the venue from `COSMIC_RETIRED_VENUES` and recreate the containers again. Its sources are polled again and its instruments become tradable.

## 11. HFM MetaTrader 5 bridge (ADR-028, B-10)

The bridge runs on a dedicated Windows VM on the owner's Proxmox host, next to MT5 (since 2026-10-22; setup in `bridge/README.md`). It calls `POST /bridge/v1/sync` about once a second. Until 2026-10-22 it ran on the owner's PC, which is retired: its MT5 is uninstalled and its startup entry renamed `COSMICTRADING MT5 bridge.cmd.disabled-20261022`.

- **Server token:** `/srv/cosmic/secrets/hfm_bridge_token` on the VM (root, group 10001, 0440; an empty file means no bridge endpoint). Production only: staging's is empty. The web container reads it; the worker and the bot never do.
- **Windows token:** the same value, in `bridge_token.txt` in the bridge folder on the Windows VM (`C:\Users\cosmic\cosmic-bridge`, readable only by that user, SYSTEM and administrators).
- **Changing the token:** write a new value in both places, then recreate the web container (`up -d --no-deps --force-recreate web`, as in §10).
- **Health:**
  - **Market** shows `CFD:XAU/USD@HFM` and `CFD:BTC/USD@HFM` prices (stored every 15 s).
  - **Sources** shows `hfm_mt5_bridge` and `hfm_mt5_status`. HFM's venue status is OPERATIONAL only while the terminal is connected, trading is allowed and the account is a demo account.
  - A non-demo account raises a CRITICAL incident, and no command is sent.
- **When it stops:** prices become STALE after 60 s, and every check that needs them blocks. On the Windows VM (Proxmox console), check that MT5 is logged in (the bottom-right corner shows a ping) and that **Algo Trading** is green. The scheduled task "COSMICTRADING MT5 bridge" starts the bridge again within 5 minutes; to restart it now, run that task from Task Scheduler.
- **At boot (Windows VM, 2026-10-22):**
  - **Sign-in:** Windows signs in as `cosmic` by itself (`netplwiz`; the password is stored encrypted, not in the registry).
  - **The bridge task:** "COSMICTRADING MT5 bridge" runs `start-bridge.bat` at that sign-in, and every 5 minutes if it is not running. It has no time limit, and the bridge's own lock prevents a second copy. The bridge starts MT5 if it is not running.
  - **MT5:** it logs in with its saved password.
  - **Its options** (*Tools → Options → Expert Advisors*): Algo Trading on; "disable when the account/profile has changed" off; "Disable algorithmic trading via external Python API" off. MT5 turns Algo Trading off after an account change unless that box is off.
  - **Windows:** High performance, never sleeps or hibernates, and no automatic restart for updates while signed in. SSH is allowed only from the home network.
  - **Proxmox:** the VM must have *Start at boot* set.
  - **A Windows password change** must be repeated in `netplwiz`, or the next boot stops at the sign-in screen.
- **Alerts during an outage** (from 2026-10-16):
  - One WARNING, "Bridge hfm-demo not reporting", once its heartbeat is over 180 s old. It covers the price feed too.
  - "hfm-demo: venue unreachable, account not reconciled": WARNING while the account holds nothing, CRITICAL with a position or an active order.
  - Both close by themselves when the bridge reports again.
  - While the heartbeat is fresh but prices are not (for example gold's daily break or the weekend), only the price feed's own stale alert opens.
  - A real disagreement found by reconciliation is still the CRITICAL "Reconciliation failed".
- **Contract check (gate for opening the HFM account):** with the bridge running, run:
  ```sh
  ssh cosmictrading 'docker exec cosmictrading-worker-1 cosmic broker hfm-check'
  ```
  It places one buy limit for 1 oz of gold far below the market on the demo account, with a stop-loss below it in the same request (contract version 2). It finds the order again (also by re-sending it), checks that HFM holds the stop on it, lists it, cancels it and reads the balances. Nothing can fill. Each run is recorded with the account's fingerprint (a hash; never the account number).
- **Bridge version 3 (2026-10-22):** it also reports the swap mode, the triple-swap day and the point size, so the tests count the overnight swap. Update it as in `bridge/README.md` → Updating; an older bridge keeps working, and the pages then say the swap is "not reported yet".
- **Stops:** each HFM entry carries its approved stop-loss, so HFM holds the stop from the fill even if the bridge is offline. The server refuses an entry with a stop while a bridge older than version 2 is connected; update it as in `bridge/README.md` → Updating.
- **Opening the account:** on **Operations → HFM demo**, choose an alias and the USD to manage. This needs a PASSED check for the account the bridge reports.
- **Trading it:** under **Risk policy → What may be traded**, add `CFD:XAU/USD@HFM` (and/or `CFD:BTC/USD@HFM`), venue `HFM` and currency `USD`. Raise `max_order_notional`, `max_position_notional` and `max_gross_exposure` above about USD 4,200 (the smallest gold trade is 1 oz). The cross-venue price check compares HFM gold with PAXG/USDT and HFM BTC with BTC/USDT.


## 12. Demo auto-trader (ADR-030)

**Since 2026-10-22 the auto-trader has its own page: Auto-trader (`/trader`, first in the menu).** It shows the money, the limits and today's use; the total and today's result, wins and losses, best and worst; open trades with their result now and the loss if the stop is hit; trades waiting for you; how far each market is from its breakout; a result curve and a bar per trade; and the history. It refreshes itself every minute. On Telegram, `/trader` sends the same in a few lines.

- **Start or change it:**
  - Press **Change money or settings** (or **Start**).
  - Enter **how much money** it may use, choose **Careful / Normal / Bold** (0.5 / 1 / 2 % of that money per trade) and **Automatic** or **Ask me first**, then **Show my plan**.
  - The plan shows the most one trade can lose, the daily stop, and for each market what one trade ties up and risks today, and the least money each care level needs.
  - Tick the markets and press **Start** (with your authenticator code if asked). New settings replace the old ones at once.
- **Decision guide (since 2026-10-23, ADR-033):** every proposal to buy carries "Should I take this trade?" on the approval page and under the Telegram card. It shows six signs: evidence, trend, time, cost now, cost of this trade and size. Each is ✅ fine, ⚠️ care, ❌ against or ❔ unknown. It is a guide, not advice.
- **Ask me first:** Telegram says **⏰ TIME TO TRADE** with the proposal card. Approve it there or in the portal, as any proposal. Nothing trades until you do.
- **Heads-up:** when a market is within half an hour's typical move of its breakout level, Telegram says a trade may come with the next hourly close (at most once per market every six hours).
- **Results:** each closed trade is reported as "✅ WIN +… USD" or "❌ LOSS −… USD" after fees.
- **Never more than your money:** open trades are never worth more than the money you gave (no leverage). A trade that does not fit is skipped and reported, never made bigger.
- **Half size after a bad run (since 2026-10-23, addendum 4):** five losing trades' worth below its best result, it sizes every trade to half your limit. It goes back to full size once the drop is under two and a half. Telegram tells you each switch, and the page shows "Trade size".
- **Stops for good (since 2026-10-22, addendum 3):** after losing ten trades' worth in total (5 / 10 / 20 % of the money at Careful / Normal / Bold), it switches itself off.
  - It starts a trade only if a full loss on it and on the open trades stays within that total.
  - When it stops, the page shows STOPPED ITSELF and Telegram says why. Open trades keep their stop.
  - To go on, start it again; a new total begins then.
- **What to expect (since 2026-10-22, addendum 2):** **Show my plan** also replays the auto-trader's own rule on the last 12 months of hourly prices with that plan. It shows:
  - the result, and its share of your money;
  - trades a week and the win rate;
  - the typical win and loss, the worst losing streak and the deepest drop;
  - each half, each month and each market;
  - the signals it would have skipped and why.
  The auto-trader page shows a short version for the settings in force, and Telegram `/trader` gives it in one line. It is a HISTORICAL SIMULATION with assumed costs (open "How this was tested"), not a promise. Right after a deploy the hourly history is still being fetched, and the page says how many days are stored.
- **Ideas and the forward race (since 2026-10-23):** the competition page also shows ideas from the system's own research (with the daily trend, in London and New York hours). They are judged on new prices only, and the race ranks every rule and idea by its result since 23 Oct 2026.
- **The rule competition (ADR-031):** **Auto-trader → See the leaderboard** (`/trader/competition`) shows whether any published rule has earned the auto-trader. Each rule is chosen on the earlier part of the prices, checked on the later part, then proven forward from 23 Oct 2026. It runs once a day by itself; the first run comes within hours of a deploy. "EARNED" means eligible for review: a rule is added only by a reviewed code change, and used only when you start the auto-trader with it.
- **Gold (since 2026-10-22, ADR-032):** **Gold** in the menu shows:
  - gold now: price, change, trend, volatility and the session;
  - a one-year chart, and whether one ounce fits your money;
  - why the auto-trader lost on gold (costs, stops, sessions, trend), how to win, gold in the rule competition, the long-run benchmark, and your gold trades.
  Telegram `/gold` gives the summary. Costs decide the hourly gold result: confirm HFM's commission (MT5 History) and set Risk policy → Fee per venue.
- **The market radar:** each market card draws the last 48 hours of the price the rule watches.
  - **Out of a trade:** the dashed orange line is where it buys.
  - **In a trade:** the green dashed line is where it sells, the dotted line is the stop, and a dot marks the purchase.
  Hover an hour to see its close.

The Operations card below still works for the original limits-only mandate.

The auto-trader trades by itself on a **paper or demo account only**, under a mandate the owner grants on **Operations → Demo auto-trader**. Live accounts cannot have a mandate: the database refuses one.

- **Start:**
  - Choose the account, for example `hfm-demo [SIMULATED · DEMO]`, the rule (Turtle 20/10) and the instruments.
  - Set the limits. The defaults are a $30 loss at the stop per trade, 5 trades a day, 2 open positions, no new trades after a $60 loss in a day, and 30 days.
  - Enter an authenticator code if the last one is older than 10 minutes.
  - Telegram confirms: "Auto-trader ON for …".
- **What it does:**
  - **When:** every 5 minutes it checks the newest completed 1-hour bar. HFM bitcoin follows Binance BTC/USDT, and HFM gold follows Binance PAXG/USDT.
  - **Buying:** on a close above the prior 20-bar high it buys, sized so the loss at the stop (3×ATR(20) of the hourly bars below entry) is at most the limit.
  - **Selling:** on a close below the prior 10-bar low it sells what it bought.
  - **Reports:** each trade arrives as "AUTO TRADE … on hfm-demo [SIMULATED · DEMO]", followed by the usual order and fill messages.
  - **Skips:** a skipped trade says why, for example "the smallest trade (1) would risk about 44.10", "today's 5 auto trades are used", or "mode EMERGENCY_STOP".
- **What still applies:** every risk check, the protective stop on every entry, the emergency stop, and reconciliation. A trade the risk checks refuse is recorded NON_EXECUTABLE with the reason.
- **Stop:** press "Stop the auto-trader" on Operations; no code is needed. Open positions keep their stop. Close them from Portfolio with an approved exit if you want.
- **Watch for:**
  - With the PC asleep, the HFM bridge stops, prices go STALE and nothing is proposed.
  - Gold trades only while its hourly range is small enough for one ounce to fit the limit.
  - The rule was tested on daily bars; on hourly bars the results are a forward test, not evidence of an edge.
