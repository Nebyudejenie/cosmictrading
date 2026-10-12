# Session report — S11 post-handover backlog (2026-10-12)

Format: master prompt §22 (11 parts). Follows `2026-10-11-session-report.md`; only what changed is reported here. Evidence: `docs/IMPLEMENTATION-STATUS.md` (S11).

## 1. What was inspected

`trading/execution.py` and its importers; every place that raises an incident or notifies the owner (15 incident sites, 12 notification sites); how the portal shows notifications (it did not); the API module against doc 10; post-trade reviews; AI routing, context construction and task ordering; every portal template (74 form controls); which process reads which secret.

## 2. Design documents updated

doc 09 (residual risk 6 resolved, T19 extended), doc 10 (every listed endpoint built, `/search` added, mode-change rule corrected), doc 12 (secrets per process), doc 14 (alerts), doc 18 (Alerts and Search sections), doc 00, traceability (TRD-33, OBS-03, REPO-08, QNT-11, AI-12/14/20, UX-04, DATA-10 → IMPLEMENTED), IMPLEMENTATION-STATUS S11.

## 3. Functionality implemented

| Commit | Change |
|---|---|
| `37529e5` | Execution authority split into a package of seven focused modules; behaviour unchanged (all 25 methods AST-identical) |
| `6ed0ab8` | JSON API: market bars, go-live readiness, AI providers (no key material), missions, audit, mode change (asymmetric MFA rule kept), approval revoke |
| `dffe5fa` | Actionable alerts: six required answers; incidents require one; alerts only on open/escalation; **portal Alerts inbox** (previously portal notifications were written but never shown) |
| `6b26f2f` | Post-trade attribution: market move (thesis + luck, stated as inseparable for one trade), delay, entry/exit execution, fees — parts sum exactly; sizing vs plan; on the trade page and API |
| `f0d4c41` | AI tier-routing test; fair, marked context budget (old one could drop items silently); waiting AI work ordered by decision value |
| `272258f` | Structural accessibility checks over every page (checker itself tested) |
| `175f30e` | Full-text research search over claims, reports and (with permission) proposals; migration 0007 |
| (final commit) | Web process holds no AI keys or bot token; worker and bot report presence in heartbeats |

## 4. Important files

`src/cosmic/trading/execution/` (package), `src/cosmic/notify/alerts.py`, `notify/inbox.py`, `ops/incidents.py`, `ops/integrations.py`, `ops/heartbeat.py`, `trading/attribution.py`, `market/data.py` (`mid_at`), `research/search.py`, `workforce/priority.py`, `ai/safety.py`, `db/migrations/versions/0007_search.py`, `web/routes/api.py`, templates `alerts.html`, `search.html`, `approval_detail.html`, `deploy/compose.prod.yaml`.

## 5. Commands and tests actually run

| Command | Result |
|---|---|
| `make check` | **396 passed**, 1 deselected; ruff, TODO gate, mypy strict (171 files), secret scan clean |
| bandit | no issues |
| `scripts/local-verify.sh --fresh` on the S11 image | 13/13 steps PASS (portal 22/22, chaos 17/17, restart 10/10, audit chain 68 events, restore drill PASS) |
| In that stack | web `/run/secrets`: `db_app_password metrics_token secret_key` only; worker heartbeat reports AI key presence; emergency stop produced a structured alert |

## 6. Security, cost and deployment checks

The internet-facing process lost access to four secrets. Search uses bound parameters only. Hostile search input was tested. Alerts are redacted like other notifications. Resource use is unchanged: postgres 71 MiB, worker 63 MiB, web 74 MiB idle. AI cost is still exactly $0 by policy.

## 7. Deployment state

| Target | State |
|---|---|
| Local production-like stack | **VERIFIED** (S11 image) |
| Proxmox VM | **BLOCKED** (B-02) |
| Cloudflare Tunnel | **NOT CONFIGURED** |
| DNS | **BLOCKED** (B-03) |
| Free AI providers | **NOT CONFIGURED** (B-04) |
| Telegram bot | **NOT CONFIGURED** (B-05) |
| Broker / live trading | **NOT CONFIGURED** (B-06/B-07); live trading unreachable |

## 8. Real versus simulated

Unchanged. Market data is real (delayed). Orders, fills and P&L are SIMULATED by the paper venue. AI and Telegram are tested with fakes only. Attribution and search work on whatever the system has recorded; on paper data their numbers are therefore simulated too.

## 9. Known bugs, trade-offs and limitations

No known open bug. Defects found and fixed in this session:
- Portal notifications were never displayed.
- AI context truncation could drop whole items silently.
- Protective-order failures sent one alert per attempt.
- Doc 10 promised endpoints that did not exist.

Trade-offs:
- The drift test ignores the three expression indexes by name, because Alembic cannot compare expression indexes. A separate test asserts the indexes exist.
- Attribution needs a quote at most 5 minutes old at entry and exit. Without one, that part is reported unknown.
- If the worker stops reporting, the portal shows AI keys as UNKNOWN.

## 10. Blockers needing external action

Unchanged: B-01 to B-08 (`docs/BLOCKERS.md`). The order is Proxmox access, then domain and tunnel, then AI keys, Telegram token and broker choice.

## 11. Next steps, ordered by risk and value

1. **VM (B-02).** Run `create-vm.sh --apply`, `bootstrap.sh --apply`, `deploy.sh --build`, `vm/verify.sh`, then a VM reboot test.
2. **Backups.** Set up an off-VM backup copy and run the first monthly restore drill on the VM.
3. **Domain and tunnel (B-03).** Run the DNS inventory, create one CNAME, add a Cloudflare Access policy, run `verify-public.sh`.
4. **Paper period.** Run 30 days / 20 closed trades. Attribution now shows where the results come from.
5. **AI keys (B-04).** Then watch the alert inbox for billing or quota signals.
6. **Broker sandbox adapter (B-06/B-07).** Implement it against the broker's official documentation and run it through the existing contract suite. Live trading stays disabled until gates A–I pass and you activate it.
