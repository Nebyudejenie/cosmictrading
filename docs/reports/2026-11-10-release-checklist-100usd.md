# Release checklist — the 100 USD gold robot (S74–S78), 2026-11-10

**Scope.** The 100 USD robot is the owner's own account, with its rules, trading **SIMULATED** on HFM's live prices. It sits beside the hourly HFM demo robot, which stays untouched.

**Not in scope.**
- **Real money:** no live adapter exists (ADR-034 lock 1). It switches on only by the owner's own activation.
- **Cent account:** an HFM Cent account (0.01-ounce steps, USC) is not open yet.

**Responsible developer:** Claude, implementation CTO (session trading-de). Reviewer: the owner's CTO AI ("Automated gold trading system").

**Test runs:**
- Each test is recorded once. A test is run again only when its code changes.
- The full `make check` for the release commit is recorded at the end.

| # | Requirement | Implementation | Test result | Evidence | Outstanding defect |
|---|---|---|---|---|---|
| 1 | Isolated 100 USD robot: own configuration, account, state, risk | Its own simulated account (`gold_sim_accounts`), settings, ledger and limits. The start no longer touches the HFM robots (S78). | PASS | `test_the_100_usd_robot_starts_isolated_and_never_touches_the_hfm_robot`: the HFM robot stays ACTIVE with its money. On the same candle each robot decides only for itself. | — |
| 2 | One robot never controls another's trades | The simulation never sends to HFM. At HFM, each order carries its own magic number, and each robot holds only its mandate's entries. Each HFM account has its own bridge, and one MT5 account never serves two bridges (S74). | PASS | Test 1 above; `test_each_robot_trades_only_its_own_hfm_demo`; `test_one_mt5_account_never_serves_two_bridges`; `test_each_bridge_syncs_only_as_itself_and_only_the_first_feeds_prices` | — |
| 3 | Deposit 100 USD; denomination verified | The account starts at 100 USD. HFM accounts in any currency but USD (a Cent account's USC included) are refused, not misread (S55). | PASS (simulated USD); Cent NOT VERIFIABLE yet | Test 1 (`starting_balance == 100`); `test_an_account_in_cents_is_never_traded_or_reconciled` | **Cent (USC) support** is not built; it is needed before a real Cent account |
| 4 | At most 2 USD a trade, counting spread, commission and slippage | `sim.costs` (one spread of slippage at each fill, commission both ways); `sim.size` sizes to ≤ 2 USD in 0.01-oz steps; the risk never grows after a loss | PASS | `test_the_100_usd_rules_never_risk_more_than_2_and_target_twice_that` (stops 8–120 $/oz: ≤ 2.00 and the largest size that fits); the examples table below | A gap past the stop (weekend or news) can lose more: `test_a_simulated_sell_stopped_through_a_jump_loses_more_than_planned`. Disclosed, as at any broker. |
| 5 | Target 4 USD (1:2) | The target sits at twice an ounce's whole risk, costs included | PASS | `test_a_simulated_buy_opens_at_hfms_ask_and_its_target_closes_it` (stop 4,010.30, target 4,101.50); the sell mirror | — |
| 6 | No martingale, grid or uncontrolled compounding | One trade at a time. The size base is the amount, or the money left after losses, whichever is less, so the risk never rises after a loss. | PASS | `test_the_100_usd_rules_…` (80 left → ≤ 1.60); `test_each_limit_says_why_it_waits` | — |
| 7 | Lot size from the contract and the stop distance; refused when the risk is over the limit | Size = floor(2 USD ÷ (stop + costs) ÷ 0.01 oz). Under 0.01 oz: WAIT, with the amount it would trade from | PASS | `test_too_small_says_wait_and_from_which_amount`; `test_the_size_follows_your_amount_and_never_your_risk` | The contract is taken from HFM Cent's published terms (0.01 oz); a Cent account's own specification is not read yet (needs the account) |
| 8 | Daily loss limit 6 USD | No new trade once 6 USD is lost today, nor a trade whose full loss would pass 6 USD | PASS | `test_a_trade_that_could_pass_the_6_usd_day_waits` | — |
| 9 | Pause after 3 losses in a row | It pauses itself, tells the owner, and audits it. It resumes only with the owner's ON, and counts afresh after that. | PASS | `test_three_losing_trades_in_a_row_pause_the_robot_until_the_owner_resumes` | — |
| 10 | Emergency stop | No new trade in EMERGENCY_STOP or PROTECTION_ONLY; open trades keep their stop and target | PASS | `test_the_emergency_stop_halts_the_100_usd_robot` | — |
| 11 | Idempotent execution; unique IDs; no duplicates after retries or restarts | One decision per candle (unique key: account and candle). At HFM: client order IDs plus magic numbers, and an ambiguous send is never resent. | PASS | `test_a_candle_is_decided_once_even_after_a_restart`; `test_at20_restart_recovers_without_resubmitting`; `test_an_ambiguous_buy_is_closed_by_the_trade_check`; the daily robot's `test_a_daily_breakout_…_once_a_day…` | — |
| 12 | Broker-confirmed state; verified stop and target | Simulation: its own book. HFM robot: the stop is attached to the entry and read back from HFM's position; the self-test is reconciled. | PASS (HFM robot) / N/A (simulation) | `test_buy_stop_and_exit_through_the_bridge`; `test_the_entry_is_protected_before_its_stop_order_exists`; production self-test PASSED 2026-11-09 (HFM deals, stop at HFM) | **Checking the confirmed stop against 2 USD after each fill** is to be built with the Cent or live link |
| 13 | Persistent state after restart | Everything is in PostgreSQL; decisions are append-only | PASS | `test_a_candle_is_decided_once_even_after_a_restart`; AT-20 | — |
| 14 | Reconciliation with HFM | Per account, against its own bridge | PASS (HFM) / N/A (simulation) | `test_a_trade_cycle_on_the_demo_agrees_with_hfms_history`; two-account reconciliation in `test_each_robot_trades_only_its_own_hfm_demo` | — |
| 15 | Connection loss and stale prices | No price, or a report over 15 s old: nothing opens; the daily robot waits for fresh stored data | PASS | `test_no_current_price_no_action`; `test_a_stop_without_a_price_closes_at_the_next_one`; `test_a_disconnected_terminal_blocks_new_hfm_trades_until_it_reconnects`; `test_a_silent_bridge_is_one_incident_and_never_a_disagreement`; `test_a_closed_market_decides_nothing_until_hfms_price_is_fresh` | — |
| 16 | Rejected orders and partial fills | HFM refusals end the order; a partial fill is protected and booked | PASS | `test_a_stop_out_after_a_partial_fill_leaves_the_books_flat`; `test_an_old_bridge_is_refused_an_entry_with_a_stop`; trade check (HFM rejects below its minimum volume) | — |
| 17 | Live-price execution in a safe environment | The simulation on HFM's live prices; the HFM demo robot on HFM's real demo | PASS (HFM demo, production 2026-11-04→10: 8 entries, stops and targets at HFM) / after release (100 USD robot) | Production read-only probes, 2026-11-10 | The 100 USD robot's first live-price trades come after the owner starts it |
| 18 | Complete audit logs | The owner's start and the automatic pause are in the hash-chained audit; decisions, positions and the ledger are kept | PASS | Test 1 (`gold_sim.started_100_usd`); test 9 (`gold_sim.paused_after_losses`); `verify.sh`: audit chain verifies | — |
| 19 | Live execution disabled | No live adapter (ADR-034 lock 1). Mode PAPER_TRADING. The bridge and the server refuse a real account. | PASS | `verify.sh`: "live trading disabled"; `test_a_real_account_is_never_traded` | — |
| 20 | Existing robot unaffected | No code path of the release touches its mandate or positions | PASS (test); production probe after the release | Test 1; probe after the release (below) | — |
| 21 | Rollback | `scripts/release.sh` deploys any earlier pushed commit: production 703cd01 → its previous 8e4eff8. S76–S78 adds no migration; 0026 and 0027 have tested downgrades. | READY | `deploy-history.log` on the VM; `test_migrations.py` | — |

## Examples: the 100 USD robot's sizes

These come from the robot's own sizing on HFM's hourly stops (1.5 × H1 ATR) over the 30 days to 2026-11-09, with gold at 4,100 and a spread of 0.35:

| Case | Stop $/oz | Ounces | Standard lots | Max loss $ (costs incl.) | At the target $ | HFM's smallest standard trade (1 oz) would risk $ |
|---|---|---|---|---|---|---|
| calmest | 14.78 | 0.12 | 0.0012 | 1.86 | 3.72 | 15.48 |
| quiet | 18.69 | 0.10 | 0.0010 | 1.94 | 3.88 | 19.39 |
| typical | 22.08 | 0.08 | 0.0008 | 1.82 | 3.64 | 22.78 |
| busy | 25.49 | 0.07 | 0.0007 | 1.83 | 3.66 | 26.19 |
| wildest | 35.76 | 0.05 | 0.0005 | 1.82 | 3.64 | 36.46 |

## Open items (none critical or high for this release; all needed before real money)

1. **A Cent account.** The owner opens one in myHF, if HFM offers it.
2. **Cent support.** Read USC money and the account's own contract specification on the second bridge.
3. **The confirmed-stop check.** After each fill, check HFM's confirmed stop against the 2 USD, and close at once when it would lose more.
4. **The live link.** An HFM live adapter with its sandbox contract tests, gates A–I, and the owner's activation.

## Release record

- **`make check`** (2026-11-11, on the release tree):
  - ruff format and lint clean;
  - mypy strict clean (264 source files and the bridge);
  - secret scan passed (623 files);
  - **1,187 tests passed**, 0 failed, 3 deselected (live-data and sandbox markers), in 61 min.
- **The commit, the staging and production checks, and the post-release probe** are reported to the owner and to the CTO AI with the release.
