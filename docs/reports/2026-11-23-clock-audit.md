# Robot A's 14-hour clock discrepancy: audit (2026-11-23, S115)

**Question (the owner, 2026-11-23):** did the wrong clock offset measured on terminal 2 over the weekend of 21–22 November change trading behaviour, not only whether orders were executed?

**Answer:** no trading decision, order, fill or candle used by a robot was changed by it. One older instance of the same flaw was found: seven candles stored on 25 October. Replaying every robot reading with and without them changes nothing. The robots were not changed; the proposed fix waits for the owner's approval.

All figures come from read-only probes of production run on 2026-11-23 between 05:03 and 05:16 UTC (`SET TRANSACTION READ ONLY`). The probes are kept outside the repository, under `~/.cache/cosmic-checks/` (`clock_audit_probe.py`, `clock_audit2_probe.py`, `clock_replay_probe.py`, `clock_replay_b_probe.py`).

## 1. The flaw

MT5 stamps ticks, orders and deals in HFM's server time (New York + 7 hours: UTC+2 in winter, UTC+3 in summer). The bridge learns that offset by comparing the newest tick's time with its own UTC clock. It rounds the difference to a whole quarter of an hour and accepts it only when the remainder is under two minutes.

When the market is closed, the newest tick stops moving and only ages:
- each time its age passes a whole quarter of an hour, the rule accepts it again, with an offset short by that age;
- terminal 2 trades gold only, so over the weekend its offset sank in quarter-hour steps to −14 hours (the bridge's own ±14-hour bound);
- terminal 1 also carries `#BTCUSD`, which trades at weekends, so its newest tick stays fresh and its offset stays right.

Bridge 13 (S114) learns the offset only from a tick that has arrived since the last reading. It is installed nowhere yet: it goes to terminal 3 first, and to terminals 1 and 2 only with the owner's approval.

## 2. HFM server time against each terminal now

| Terminal | Bridge | Offset reported | HFM's rule now | Newest gold tick (true age) |
|---|---|---|---|---|
| 1 (Robot B, HFM's prices) | 12 | +2.0 h | +2.0 h | 1.2 s |
| 2 (Robot A's own demo) | 12 | +2.0 h | +2.0 h | 1.2 s |

The fast lanes of both terminals also report +2.0 h. Terminal 2 corrected itself at the market's reopening, as expected.

## 3. What reads which terminal's clock

| Use | Which terminal | Effect of a wrong offset on terminal 2 |
|---|---|---|
| Robot A's signals (`trend_test.reading_now`: 15-minute and hourly candles) | terminal 1's candles (`hfm_candles`, primary bridge only) | none |
| Robot A's price and freshness (`sim.price_now`) | terminal 1 (`sim.BRIDGE = "hfm-demo"`) | none |
| Candle boundaries (`hfm_candles.record`) | primary bridge only (`spec.primary`) | none |
| HFM's own candles (`gold.hfm_history`, `deep_history`) | terminal 1, refused unless the offset matches HFM's rule within 60 s | none |
| Risk checks' quote freshness (R09) | stored quotes, terminal 1 only (`bridge_sync`: primary only) | none |
| Robot A's setup ("market closed" check) | terminal 1's price | none |
| Fill, deal and position times of Robot A's own demo (`HfmBridgeAdapter._utc`) | terminal 2 | yes, in principle; see §4 |
| Reconciliation of terminal 2's account | terminal 2 | none: fills are matched by HFM's deal number, quantity and price, never by time |
| Sending an order (the 1-second rule, `ttl_ms`) | the bridge's own request timing, not the broker offset | none |
| News or session filters | no news source is connected; sessions follow the server's own UTC clock | none |

## 4. Evidence from production

**Fills.**
- Terminal 1: 30 fills, from 2026-11-04 to 2026-11-12. Every stored fill time equals HFM's own deal time, converted by HFM's rule, to within 1 second.
- Terminal 2: no fills. Robot A's own demo opened at 2026-11-22 23:16:07 UTC and has not traded yet: its START waits for the owner.
- So no stored time of terminal 2 can have been placed wrongly.

**Decisions taken while the market was closed** (server Saturday, Sunday or Monday before 01:00):
- Robot A's demo decisions (`gold_hfm_decisions`): 5 of 11,522.
- Robot A's simulated decisions (`gold_sim_decisions`): 4 of 11,723.
- Every one is a WAIT. Each was decided at the Friday close (22:00:29–36 UTC) on the candle just closed, or, on 2026-11-07, at the first run after a release, on Friday's last candle.
- None opened, closed or proposed a trade.

**Signal and order timestamps.**
- 4 of the demo decisions came more than 30 minutes after their candle opened. Three were the first decisions after the 2026-11-07 release (on 2026-11-06's last candle). One came on 2026-11-17 at 08:39 for the 05:59 candle: HFM's stored prices stop at 05:59:00 and resume at 08:39:36 (the terminal was silent), and the robot decides only on a fresh price.
- All four were WAIT. None of them comes from the offset.

**Robot A's setup checks on terminal 2** (`broker_contract_runs`):

| When (UTC) | Result | Run by | Cause |
|---|---|---|---|
| 2026-11-21 06:07:18 (server Saturday 08:07) | FAILED, "10018 Market closed" | CLI | a manual check run during the S110–S111 work, not the robot |
| 2026-11-22 23:00:18 (server Monday 01:00) | FAILED, "10018 Market closed" | the robot | the first price after the reopening arrives a few minutes before HFM accepts orders |
| 2026-11-22 23:16:05 | PASSED | the robot | retried after its 15 minutes; the account opened at 23:16:07 |

Neither failure comes from the offset. The second one shows a separate, harmless timing fact: prices resume before orders are accepted. The setup retried as designed.

**Logs, 2026-11-20 21:00 to 2026-11-23 01:00 UTC** (worker):
- 17 `robot_setup` lines: the failed check at 23:00:18, one "waiting to try again" each minute until 23:15, then "account opened" at 23:16:07;
- 119 `gold_hfm_step`, 7 `gold_test_sim`, 4 `hfm_history`;
- no `hfm_history_clock` warning (terminal 1's offset never left HFM's rule);
- no send failure.

## 5. The older instance: 25 October

Five HFM quotes are stored with tick times inside the closed market. All are at Friday's closing price, 3995.49, and each tick time is exactly 15 minutes after the last:
- tick times 2026-10-23 21:13:59.921, 21:28:59.921, 21:43:59.921, 21:58:59.921 and 22:13:59.921 UTC;
- stored on 2026-10-25 at 00:13, 00:28, 00:43, 00:58 and 18:08 UTC.

This is the signature of the same flaw family: one stale tick re-timed each quarter hour. Bridge 3 ran then, with the same rule. The offset itself was not kept, so the exact path cannot be proved from what is stored.

On 2026-11-04 the candle backfill rebuilt candles from these quotes. That stored seven impossible flat candles on HFM's Saturday:
- 15-minute candles at 21:00, 21:15, 21:30, 21:45 and 22:00 UTC;
- hourly candles at 21:00 and 22:00 UTC.

They are still in the database. Every other candle on a closed-market time is HFM's own: its Monday 00:00–00:45 server candles in the weeks the US and European clocks change differ.

**Did they change a decision?** Every reading was replayed twice on the candles stored today, once with them and once without:

| Rule | Window that held them | Readings replayed | Readings changed |
|---|---|---|---|
| Robot A (`trend_buy.read`, 800 hourly and 500 15-minute candles) | every 15-minute close from 2026-11-05 12:58 to 2026-11-23 05:00 | 1,073 | 0 (decision, 4-hour trend, hourly trend) |
| Robot B and the 1-minute simulations (`analysis.read`, 240 hourly candles) | every minute from their storing (2026-11-04 16:05) to 2026-11-09 03:46 | 3,402 | 0 (decision, stop distance) |

Trader Insight (Manual Trading, 720 hourly candles) still held them. From S115 it reads only candles inside HFM's trading week, and support and resistance from the last 30 calendar days. Its "30 days" was 720 hourly candles before, which is about six weeks of trading hours.

## 6. Could it change behaviour in future?

On a gold-only terminal running bridge 12, the same flaw recurs every weekend and in every daily break. During HFM's daily break of about an hour, the offset can fall short by 15 to 60 minutes until the first new tick.

Today the only consumer of terminal 2's offset is its own fill and deal times:
- a fill can only happen while the market trades, and the first fresh tick of that report corrects the offset;
- a deal read in a closed market is matched by its number, not its time;
- one edge remains: the owner's hand deals on terminal 2's account are booked "since the account opened" by their converted time. A hand deal made up to 16 hours before the account's creation could be read as after it, but only in a closed market. None exists.

## 7. Proposed fix (needs the owner's approval; the robots are unchanged until then)

1. **Bridge 13 on terminals 1 and 2:** the offset is learned only from a tick that has arrived. It also reports HFM's own margin, freeze level and stop-out. It is installed with the same steps as terminal 3, one terminal at a time, when no trade is open.
2. **A server guard:** no quote or candle whose time falls on HFM's Saturday or Sunday is stored, and the readers skip any already stored. The seven candles and five quotes of 25 October would be marked, not deleted. This changes the robots' inputs, which the replay shows to be harmless, so it waits for the same approval.
3. **Keeping the offset's history:** each change of a terminal's reported offset is stored, so that a future audit can prove the path.

Approval to give, if wanted: "Install bridge 13 on terminals 1 and 2 and add the weekend guard."
