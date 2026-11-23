# Go-live gates A, B, D, E and H: each acceptance criterion and its evidence (2026-11-23, S115)

**The rule (the owner, 2026-11-23):**
- A gate passes only when its documented acceptance criteria and evidence are satisfied.
- A successful release check does not count as every gate.
- The owner's approval is no substitute for missing technical evidence.

The criteria are the master build prompt's (§1830). Each one is listed with the evidence that covers it: an executable test, a measured record, or a drill. Its status is MET, PARTIAL or MISSING.

- A gate is recorded PASSED only when every criterion is MET.
- Otherwise it is recorded NOT_MET, with the same checklist, so the gap is visible.
- Each record names the release it was checked on; the Manual Trading page and `/golive` show the checklist.

Test results come from the full test run (`make check`) of the release, with a per-test result file kept beside its log (`~/.cache/cosmic-checks/gate-evidence/`). Measured records come from read-only production probes.

## Gate A: Repository and environment assessment

| Criterion | Evidence | Status |
|---|---|---|
| Inventory of repository, services, database, bot, Mini App, portal, deployment, secrets, dependencies | `docs/01-*` to `docs/16-*` and `docs/IMPLEMENTATION-STATUS.md`, kept per release | MET |
| The VM's actual CPU, RAM and disk limits and their current use | measured 2026-11-23 05:16 UTC: 3 vCPU (load 1.84 / 1.90 / 1.73), 10,176 MiB RAM (7,625 MiB available), 98 GB disk (35 % used); each container within its memory limit (worker 141 MiB of 1 GiB, web 106 MiB of 512 MiB, database 699 MiB of 2 GiB) | MET |
| Current revision, working-tree state, existing tests and a backup before modifications | every release: `make check` passes before the commit (AGENTS.md); `scripts/release.sh` refuses uncommitted changes and releases only the pushed commit; `deploy/deploy.sh` takes an encrypted backup with the running release before it migrates. The nightly backup of 2026-11-23 02:30 UTC exists | MET |
| The broker, account type, test environment and data entitlements identified; if unspecified, stop at paper mode | the broker (HFM, MT5) and its demo are identified; **the real account's type, leverage, commission and swap terms are not**, because terminal 3 has not reported it. The system stays at paper mode | **MISSING** until terminal 3 reports the real account |

**Gate A: NOT MET.**

## Gate B: Unit and property tests

| Criterion | Evidence (tests) | Status |
|---|---|---|
| Risk-limit arithmetic | `tests/unit/test_risk_engine.py` (R01–R20 scenarios), `tests/unit/test_risk_rooms.py`, `tests/unit/test_manual_calc.py` | MET |
| Decimal precision | `tests/unit/test_money.py`, `tests/unit/test_schema_rules.py::test_no_floating_point_columns` | MET |
| Tick and lot rules | `tests/unit/test_manual_examples.py` (HFM's grid, the round-down property test), `tests/unit/test_trading_primitives.py` | MET |
| Approval binding | `tests/trading/test_acceptance.py::test_at04_wrong_operator_channel_or_hash`, `::test_approval_packet_contains_every_material_term` | MET |
| Expiry | `test_acceptance.py::test_at05_expired_approval_cannot_execute`, `::test_at05_challenge_and_intent_expiry` | MET |
| Idempotency | `test_acceptance.py::test_at04_approval_single_use_and_not_replayable`, `::test_at09_no_duplicate_exposure`, `::test_at09_timeout_does_not_resubmit` | MET |
| Order state transitions | `tests/unit/test_order_state_machine.py` | MET |
| P&L and fees | `tests/unit/test_attribution.py`, `tests/unit/test_trading_primitives.py`, `tests/unit/test_manual_calc.py::test_results_from_hfms_deals`, `tests/unit/test_manual_periods.py` | MET |
| Timezones | `tests/unit/test_schema_rules.py::test_every_timestamp_is_timezone_aware`, `tests/unit/test_review_fixes_replay.py::test_the_weekend_close_and_the_rollover_follow_new_york_time`, `tests/unit/test_mt5_bridge.py` (the broker's clock offset) | MET |
| Drawdown boundaries | `tests/unit/test_gate_b_boundaries.py` (S115): R08's drawdown, daily loss and streak refuse at their limit and not a cent or a trade before it, as property tests | MET |
| Failure-state rules | `tests/unit/test_risk_engine.py::test_broken_check_fails_closed` and every UNKNOWN scenario (it fails closed) | MET |
| Deterministic fixtures and property-based tests where practical | a frozen clock and fixtures throughout; property tests (Hypothesis) in `test_risk_engine`, `test_trading_primitives`, `test_money`, `test_attribution`, `test_manual_examples` and `test_gate_b_boundaries` | MET |

**Gate B: every criterion MET, once the release's full run passes.**

## Gate D: Adversarial safety tests

| Criterion | Evidence (tests) | Status |
|---|---|---|
| Expired | `test_acceptance.py::test_at05_expired_approval_cannot_execute` | MET |
| Replayed | `test_acceptance.py::test_at04_approval_single_use_and_not_replayable`, `::test_at04_wrong_operator_channel_or_hash` (a portal challenge replayed through Telegram) | MET |
| Wrong account | `test_acceptance.py::test_at15_account_mismatch_blocks`, `tests/trading/test_gate_d_tampering.py` (another account), `tests/trading/test_hfm_home.py::test_a_wrong_account_or_a_silent_bridge_is_a_problem` | MET |
| Wrong symbol | `tests/trading/test_gate_d_tampering.py` (S115: another instrument written into an approved trade is refused by the database; the order goes out as approved) | MET |
| Changed quantity | `test_gate_d_tampering.py`, `test_acceptance.py::test_at05_materially_changed_conditions` | MET |
| Changed side | `test_gate_d_tampering.py` (S115) | MET |
| Excessive price | `test_acceptance.py::test_at05_materially_changed_conditions` (the price band, R12), `test_gate_d_tampering.py` | MET |
| Stale data | `test_acceptance.py::test_at14_stale_data_blocks`, the R09 scenarios of `test_risk_engine.py` | MET |
| Unsupported order type | `test_risk_engine.py` (R04, a market order), `test_gate_d_tampering.py` (an order type changed after approval) | MET |
| Missing risk check | `test_gate_d_tampering.py::test_no_order_is_sent_when_its_pre_send_risk_check_cannot_run_or_be_recorded` (S115), `test_acceptance.py::test_at07_risk_veto_after_approval` (both stages run) | MET |
| Risk-limit breaching | `test_acceptance.py::test_at07_risk_veto_after_approval` | MET |
| Unauthorized strategy | `test_acceptance.py::test_at08_strategy_authority_locked`, `tests/unit/test_capabilities.py` | MET |
| The AI cannot bypass a veto | `tests/trading/test_acceptance_ai.py::test_at13_ai_outage_does_not_affect_safety`, `tests/trading/test_gold_ai.py::test_the_ai_reads_hfms_candles_and_can_only_stop_a_trade` | MET |
| The AI cannot invoke the broker | `tests/unit/test_architecture.py::test_forbidden_imports` (the AI gateway never imports trading or brokers; trading never imports the AI) | MET |
| Web security | `tests/security/` (authorization matrix, sign-in, secret exposure) | MET |

**Gate D: every criterion MET, once the release's full run passes.** The portal button's own cases (a double click, a stale price, the wrong account, a ticket changed after confirmation) belong to the HFM live connection and are tested with it (gate C, ADR-038).

## Gate E: Recovery and chaos tests

| Criterion | Evidence | Status |
|---|---|---|
| Network timeouts | `test_acceptance.py::test_at09_timeout_does_not_resubmit`, `tests/unit/test_mt5_bridge.py` (the bridge's fallbacks) | MET |
| Lost broker responses | `tests/trading/test_hfm.py::test_an_ambiguous_buy_is_closed_by_the_trade_check`, `::test_a_lost_or_late_close_is_tried_again_until_it_is_closed` | MET |
| Duplicate callbacks | `test_acceptance.py::test_at09_no_duplicate_exposure`, `::test_reconciliation_detects_unexpected_venue_fill_order` | MET |
| Worker crashes | `tests/integration/test_queue.py::test_expired_lease_is_recovered`, `test_acceptance.py::test_at20_restart_recovers_without_resubmitting` | MET |
| Database restart | `tests/integration/test_database_outage.py` (pages fail closed; the worker keeps running) | MET |
| VM restart | the drill of 2026-10-20 (15 of 15 and 11 of 11 checks, IMPLEMENTATION-STATUS S20), on a version a month older | **PARTIAL**: to be repeated on the version that goes live, with the owner's OK (it restarts the server) |
| Full disk | `tests/unit/test_resources.py::test_disk_thresholds`, `tests/integration/test_queue.py::test_background_work_is_shed_under_disk_pressure` | MET |
| Low memory | S115: `tests/unit/test_resources.py` (memory against the container's own limit), `tests/integration/test_queue.py::test_background_work_is_shed_when_memory_is_short`, and the new `ops.memory_check` incident | MET |
| Stale data | `test_acceptance.py::test_at14_stale_data_blocks` | MET |
| Provider quota exhaustion | `tests/integration/test_ai_gateway.py::test_rate_limit_falls_back_to_other_free_route`, `::test_local_quota_budget` | MET |
| Broker outage | `tests/trading/test_broker_connections.py::test_4_5_6_a_closed_terminal_a_lost_network_and_the_reconnection`, `tests/trading/test_hfm_real_ready.py::test_recovery_is_shown_from_a_real_outage` | MET |
| Unknown order status | `test_acceptance.py::test_at11_unknown_state_blocks_new_exposure` | MET |
| No duplicate order is submitted | `test_at09_*`, `test_at20_restart_recovers_without_resubmitting` | MET |
| Reconciliation precedes resumption | `test_acceptance.py::test_resume_refused_until_a_reconciliation_after_the_stop_passes`, R03 | MET |

**Gate E: NOT MET**, until the VM restart drill on the version that goes live.

## Gate H: Operational readiness

| Criterion | Evidence | Status |
|---|---|---|
| Monitoring | production, 24 hours to 2026-11-23 05:30 UTC: `ops.disk_check` 288 runs, `ops.backup_check` 48, `ops.audit_verify` 24, all DONE; `deploy/vm/verify.sh` on every release; `tests/unit/test_job_health.py` | MET |
| Alert delivery | production, 7 days: 48 alerts sent by Telegram, none failed (median 3.4 s to send, worst 139 s); no incident open | MET |
| Logs | structured logs with secrets masked (`tests/security/test_secret_exposure.py`); the worker's log read back for the clock audit (2026-11-20 to 2026-11-23) | MET |
| Disk protections | disk checks every 5 minutes, background work shed under pressure (tests above); disk 35 % used | MET |
| Backups | `cosmic-backup.timer` nightly; the set of 2026-11-23 02:30 UTC (encrypted dump, configuration, manifest, checksum) | MET |
| Restore tests | `scripts/drill-backups.sh`: PASS on 2026-11-03 (10 checks; a tampered copy and a wrong key refused), within the monthly window; a new drill on the S115 release's backup | MET when the new drill passes |
| Database migration rollback | `tests/integration/test_migrations.py::test_fresh_upgrade_downgrade_upgrade` and each migration's own downgrade test | MET |
| Credential revocation | AI keys (`tests/integration/test_ai_connections.py`), sessions (`tests/integration/test_identity.py::test_session_expiry_idle_and_revocation`), HFM passwords (`tests/trading/test_broker_connections.py::test_a_revoked_password_is_never_decrypted_again_and_the_revocation_is_audited`, S115) | MET |
| Incident runbooks and recovery checklist | `docs/15-operations-runbook.md` (incidents, disk, memory (S115), restore, the real-money locks) | MET |
| Resource use within the VM's budget under representative workload | measured with both robots running (above): CPU load 61 % of 3 vCPU, 25 % of RAM in use, 35 % of disk | MET |

**Gate H: every criterion MET, once the new restore drill passes.**

## What this does not do

Recording a gate never enables live trading:
- C and F stay BLOCKED while no live adapter exists;
- G needs its paper period and fill comparison (`docs/reports/2026-11-23-gate-g.md`);
- I is the owner's own activation, which no command can record.
