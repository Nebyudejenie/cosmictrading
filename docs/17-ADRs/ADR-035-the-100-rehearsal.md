# ADR-035: The $100 rehearsal, and what $100 can do in gold
- **Status:** Accepted · **Date:** 2026-10-24
- **Context:** the owner plans to start real money with $100 on gold and asked for the demo to be treated exactly like it (the $100 Gold Plan). Research the same day:
  - **Venues:** an HFM standard account cannot trade gold with $100 held fully paid, since its smallest trade is 1 oz (about 4,000). Binance PAXG accepts orders from 5 USDT. HFM Cent publishes 0.01 oz trades, but a Cent demo is not confirmed.
  - **Timeframe:** at $100 hourly rules lose to costs (Turtle 20/10 on Binance gold: −16.5 R over 400 days at the published 0.1 % fee), while several daily trend rules were positive on 2021–2026 gold after costs.
  - **Selection bias:** those rules were picked from 14 on the same history, so their results flatter them.
  - **Long history:** on 55 years of gold (World Bank monthly averages, 1960–2026), a trend filter (above the 10-month average, else cash) kept about the long-run return, 9.9 % a year against holding's 8.9 %, with about half the worst drop (−33 % against −62 %). That is evidence for daily trend rules, not a promise.
  - **The owner's choices (2026-10-24):** compare both venues; Bold ($2 a trade at $100); stop for good after losing $20; build the real-money link but keep it off.
- **Decision:**
  - **One action, "Start the $100 rehearsal"** (`/rehearsal`, `trading/rehearsal.py`). It needs the owner's code (APPROVE_TRADE fresh, and ADMINISTER_SYSTEM to open an account). It opens the PAPER account `rehearsal-binance-100` with 100 USDT on Binance prices, used by nothing else (library proposals skip it), and grants its auto-trader:
    - daily bars, automatic, 90 days;
    - Bold, so 2 % of the money a trade;
    - Binance gold (PAXG);
    - the owner's chosen daily rule.
  - **The stop for good stays at $80.** On renewal the rehearsal starts again from its money now (growth in steps), and the total loss limit is the money now less 80. A rehearsal that stopped itself is not restarted: that is its result.
  - **The fee comes first.** The action refuses until a Binance fee is set under Risk policy. Otherwise paper fills would charge the 0.6 % assumption.
  - **Renewals only from `/rehearsal`.** `mandates.grant` refuses any grant on a `rehearsal-` account unless it comes from the rehearsal itself. So the Operations form, the general setup and a hand-made paper account named like it cannot bypass its floor; the Operations form does not offer it, and `/ops/paper-account` refuses the prefix.
  - **Renewal is safe:**
    - it waits until no trade is open or on its way, since that trade's result would land outside the new mandate's limits;
    - the money is rounded down to the cent, never above what the account holds;
    - reminders and the page's settings button point to `/rehearsal`.
  - **The page shows, all labelled HISTORICAL SIMULATION or SIMULATED:**
    1. where $100 can trade gold: each venue's smallest trade and its loss at a daily stop (HFM Cent from published terms, said so);
    2. each daily rule replayed with $100 at Bold, with the venue's costs and the risk policy;
    3. possible years: the replayed trades resampled 4,000 times, with bad, typical and good results and the chance of the stop;
    4. how money grows with a monthly addition, beside gold bought and held over the same past years;
    5. what must be ready: the fee, the market in the policy, and where the risk policy binds first.
  - **Telegram `/rehearsal`** gives the money now and the auto-trader's lines.
- **Not done here:**
  - the 55-year World Bank series inside the system: it needs its licence confirmed and a documented URL first (DATA-05, DATA-12);
  - the HFM Cent side of the comparison (S38).
- **Consequences:**
  - The rehearsal proves the machinery and the costs, not the rule. A daily rule trades about once every one or two months, so its forward record (ADR-031) takes one and a half to two years.
  - Real money (S40) still needs every go-live gate, a rule that EARNED the auto-trader, and the owner's own activation.

## Addendum (2026-10-27): the HFM Cent side, and weekend handling (S38)
- **Context:**
  - The owner chose to compare both venues.
  - No public source confirms that HFM offers a Cent demo.
  - A daily rule on gold CFDs lost Friday's signals, because the venue closes at weekends while its signal series (PAXG) does not. In the live auto-trader the signal waited for a price until a newer bar replaced it; in the replay, a weekend flip was compared across a weekend bar. So daily rules were refused on gold CFDs (S35).
- **Decision:**
  - **An "HFM Cent terms" market.** The venue `HFM_CENT` (paper only) with `CFD:XAU/USD@HFM_CENT`:
    - prices are HFM's own XAUUSD quotes from the bridge, stored under the derived source `hfm_cent_terms`;
    - the lot rules are the Cent account's published ones (a cent lot is 1/100 of a lot, so 0.01 oz);
    - the swap is the one the bridge reports.
    It is a mirror, so the cross-venue check (R10) never counts it as a second price; HFM's gold is compared with PAXG instead.
  - **The swap on paper.** The paper venue charges a CFD's overnight swap at the exit, as HFM books it. The nights are counted by the same function the replay uses, `venue_terms.nights` (17:00 New York rollover, triple on the reported day). Only a swap paid is simulated, never a credit.
  - **Two rehearsals.** `rehearsal-hfm-cent-100` (100 USD) runs beside `rehearsal-binance-100`, started from the same page with the same terms. It needs the HFM_CENT fee set (0: no commission) and the market allowed by the risk policy.
  - **Weekend handling, in the live auto-trader and the replay alike.** For a daily rule on a market closed at weekends:
    - the decision is made on the last bar in session, against the one before it (a Monday is compared with the Friday before it);
    - it is acted on at the next bar in session: Monday 00:00 UTC for Friday's bar, when the replay fills it;
    - before then the signal waits, unconsumed.
    Daily rules on gold CFDs are allowed again. This applies to daily bars only (`Venue.decide_in_session`): hourly bars of a gold CFD compare the last two bars, live and replayed, as before.
  - **The mirror never stops HFM's own sync.** HFM's rules and prices come first, then the Cent mirror, whose failure is logged and ignored; the heartbeat and the commands come last. A broken mirror therefore shows as its own stale source.
- **Consequences:**
  - The replay's change alters gold CFD results in the rule competition; its code version changes, so a new run is recorded (ADR-031).
  - The HFM Cent line flatters a real Cent account only in its spread (the standard account's prices), which is said on the page.
  - A real Cent demo (path A) replaces it when one can be read through a second bridge.

## Addendum (2026-10-28): the two side by side, and fill prices measured (S39)
- **Context:**
  - The owner chose "both, then compare". Two rehearsals run, but nothing yet compared them, or compared either with what its replay says it should have done.
  - Every replay assumes 5 bps of slippage a fill (`expect.SLIPPAGE`), and gate G asks for "fills compared against a live venue". Neither assumption was measured.
- **Decision:**
  - **Fill prices measured** (`trading/measured.fill_slips`, `fill_quality`):
    - **The reference price:**
      - An entry is compared with the ask, and an exit with the bid, *when it was sent*.
      - An order sent within 60 s of being made (the normal case: one cycle) uses the quote its pre-submission risk check recorded. That is the latest *passed* check of the trade its approval was for, made before the order.
      - An order sent later uses the stored quote at its `submitted_at`, at most 5 minutes old, else it is left out. Examples: an exit waiting for its stop to be cancelled, or a venue that was down. A price move while it waited is not slippage.
      - A stop is compared with its stop price and **shown apart**. A price that gaps through the stop is counted there, while the replays fill a gapped stop at the gapped open; so stops never set the typical figure or the advice.
    - **The cost** is in bps: what a buy paid above, or a sell got below, that price. Negative means it did better.
    - **Groups:** results are grouped by venue *and environment*, so these never mix:
      - the paper simulator: it adds the replays' 5 bps itself;
      - a demo or test network: the venue's own prices, virtual money;
      - real money.
      Both kinds of virtual money are labelled SIMULATED.
    - **Advice** comes from at least 3 fills, when the median is 5 bps or more from the replays' 5 bps:
      - from real money, either way;
      - from a demo, only when it is worse: a demo tends to fill better than a real account, so a better demo proves nothing;
      - never from paper.
      It appears on Risk policy and in the morning briefing, over the fills of the last 90 days (by fill time: a stop sent months ago may fill today). A demo's line in the briefing carries `[SIMULATED · DEMO]`.
    - `REPLAY_SLIPPAGE_BPS` sits in trading, which may not import research. A test keeps it equal to `expect.SLIPPAGE`.
  - **The two side by side** on Start with $100, for each started rehearsal:
    - money now, trades and wins, the result;
    - the commission and swap its fills paid (`measured.costs_paid`);
    - its fill prices;
    - the signals it could not act on (the `auto_trade_skipped` notices, counted once across owners), with the latest reason.
  - **Each against its own replay of the same days** (`autotrader.expectation(since=, until=)`):
    - The replay uses the mandate's own money, limits and rule, with the money it was given as the equity, not today's.
    - It starts at the open of the last bar completed when the mandate began (`replay_from`). The live auto-trader's first decision is on that bar; for gold closed at weekends, the last bar in session.
    - The replay starts R08 from the account's history when the mandate began: its equity's high and its losing streak, read by `context_builder.loss_history`, the function the live checks use. A renewal after losses therefore skips what the live checks skip.
    - Trades are matched by the day they opened. A daily rule opens at most one a day on a market; two the same day are shown as one.
    - The exits are compared too: a matched trade must still be open on both sides, or have closed the same day the same way (stop-loss, rule exit). Otherwise it is named as closed differently.
    - Live trades count from the replay's first possible fill, the bar after its start. So a trade bought under the previous mandate on the day of a renewal still counts: the renewal does not buy that signal again, and the new mandate's replay does. When the renewal changed the rule, they count only from the grant.
    - The page calls the paper trades "the rehearsal", never "live".
    - The page says which window each figure covers. Trades, result, fees and skipped signals cover the account's life. "Its current run" and its replay cover the mandate in force.
    - A day only one side traded is named, so the reason can be found before real money.
    - A trade opened, or an exit made, after the last stored bar is "waiting", not a difference: the replay acts at the next bar's open, which exists only once that bar is stored.
    - `test_a_live_daily_trade_and_its_replay_open_on_the_same_day` runs the live auto-trader and its replay on the same bars.
- **Consequences:**
  - Gate G's fill comparison has its measure. The gate stays BLOCKED until a live venue exists (B-07); until then only demo venues (the HFM demo, the Binance test network) give real-venue fills.
  - A paper rehearsal's fill prices say little by design. Its replay tracking says whether the machinery does what the simulation says: the same days, the same exits. That must hold before real money.

## Addendum (2026-11-01): told when a rehearsal parts from its replay (S45)
- **Context:** the rehearsal page compares each rehearsal with its own replay over the same days, trade by trade (S39). Nobody was told when they part, and gate G's evidence assumes the system does what its simulation says.
- **Decision (`reporting/replay_watch.py`, worker job `rehearsal.replay_watch` every 6 hours):**
  - Each started rehearsal is compared with its replay exactly as the page does (`rehearsal.trackings`). One rehearsal that cannot be compared never hides the other.
  - **A parting** is a day the replay traded and the rehearsal did not, a day the rehearsal traded and the replay did not, or a trade that closed another way. What the replay cannot have yet (its fill waits for the next bar) is not one.
  - **Told once per run:** the first parting of a run (a mandate) opens one WARNING alert, with the six answers and the days, naming the account with its environment (SIMULATED · PAPER). Each later parting day is told once more, as a notice on that alert.
  - **Never told twice for a day:** a replay of past days is worked out again each time (the typical spread, the policy now), so a borderline day can come and go. The alert therefore does not end when the comparison matches again: it stays until the owner acknowledges it, or the run ends or is renewed.
  - **The road to real money** (`/golive`, S43) shows an alert not yet acknowledged as a step, and gives no date for gate G's evidence while it stands.
  - It changes nothing: no trade, no limit and no gate is touched, and a WARNING blocks nothing.
- **Fixed on the page too (found by the review):**
  - an exit made by the rule was named "exit" on the live side and "rule exit" in the replay, so every rule exit showed as a difference;
  - on a venue closed at weekends (HFM Cent), a stop hit at Sunday evening's reopening counts for Monday, where the replay makes it.
- **Consequences:**
  - A skipped signal, a limit or a fault that makes a rehearsal differ from its simulation reaches the owner within six hours, while the money is still simulated.
  - What stays a parting by design: the HFM Cent rehearsal trades on HFM's quotes while its replay uses daily bars. A price only one of them reached (a Friday-evening move after HFM closed, say) is a real difference and is told.

## Addendum (2026-11-01): both rehearsals ready in one step (S46)
- **Context:** before a rehearsal can start, the risk policy needs its venue's fee and its market, venue and currency in the lists; the page also advised three limits at $100. All of it was left to the owner to type under Risk policy.
- **Decision (`reporting/rehearsal.ready_changes`, `ready_params`, `POST /rehearsal/ready`):**
  - The page lists what the policy lacks for each rehearsal whose market can be traded here, and the advised limits, each with its value now and after. A rehearsal whose market cannot be traded here (a venue retired, the registry not yet synced) is left out and named, so it never blocks the other.
  - **Needed:** each venue's fee (Binance as the owner enters it, 0–100 bps; HFM Cent 0, its published terms) and the missing list entries. Each fee says it applies to every account on that venue, a real-money one included.
  - **Advised, only when ticked:** `max_open_risk_pct`, `max_loss_streak`, `max_drawdown_pct`, at the values the page's notes give (`autotrader.policy_advice`, shared with them), each marked as applying to every account.
  - **One change, the owner's own:** the CHANGE_RISK_LIMITS capability and a fresh code, through `change_policy` with the list as its reason. It is worked out again from the policy version the page showed; another change since (a second tab, a double click) is refused, under the policy's lock as well (`change_policy(expected_version=)`).
- **Consequences:** after a deploy, the owner gets both rehearsals ready with one form and one code, then starts each. Nothing the owner did not see or tick is changed.

## Addendum (2026-11-02): how far to trust the history (S47)
- **Context:** the four daily rules were chosen on 2026-10-24 from replays of the same gold history (2021–26, a long rise). Their results flatter them: the choice saw the answers. The owner asked to be made wealthy by gold; what the history promises must be weighed honestly before real money.
- **Decision (`reporting/edge.py`, on Start with $100 → 2. What history says):** three views, all HISTORICAL SIMULATION on the same prices, costs and limits, from one prepared replay bench (`autotrader.Bench`: the prices are read once, each rule's signals worked out once; the views are kept for the day):
  - **Nearby settings:** each rule with its numbers 20 % shorter and longer (breakouts), or 12.5 % and 25 % (the 200-day average). A result that holds across them did not come from one lucky number. The ensemble has no single number to vary.
  - **Switching, and restarting:** on each 1 January (from a full year after the replay starts), the rule that had made the most so far *as it stood that day* (an open trade valued at that day's close), started afresh for the year; beside it, each rule kept running, and each rule restarted every 1 January. Without the stop for good: a fresh start each year would have its own.
  - **Since chosen:** each rule replayed from its registration (`mandates.RULES_REGISTERED`, 2026-10-24) only: days the research never saw. Beside it, how often its own history did as badly over as many trades, each trade counted against its own risk (a half-size trade's loss counts as half), from trades closed before the registration (10,000 resamples, seed 7); below 5 % is marked unusual.
- **Research behind it (2026-11-02, Binance PAXG daily, public, 2020-08-28 → 2026-11-01; replay from 2021-11-03; $100 at Bold, 10 bps fee, 5 bps slippage; HISTORICAL SIMULATION):**
  - **Nearby settings:** every one made money. Turtle 20/10: 9 of 9, +12.90 to +25.85. Turtle 55/20: 9 of 9, +28.47 to +39.83. The 200-day average: 5 of 5, +52.20 to +59.33. Robust to its numbers, but all on one market's rise.
  - **Switching, 2023 to 2026 so far:** −3.18, below all four rules kept running over the same years (Turtle 20/10 +16.04, Turtle 55/20 +32.38, the 200-day average +59.60, the ensemble +54.79).
  - **Restarted every 1 January:** Turtle 20/10 +11.66, Turtle 55/20 +20.72, the 200-day average −1.96, the ensemble −1.78. A rule started afresh waits for its own next signal; the long-trend rules then missed the trends they had been riding. Measured here, not assumed: keep the auto-trader running and renew it before it ends.
  - Only four years, and one market's rise: a small sample either way.
  - **A first version of this view was wrong and was withdrawn before release:** it picked each year's rule with trades that closed later in the year, and an open trade valued at today's price (hindsight). The review caught it; the figures above are the corrected ones.
  - **A stop that follows the price up hurts** (no change made): raising the protective stop each day to the breakout's own exit level, or 3×ATR under the highest high, cut Turtle 55/20 from +32.18 to +5.78 and +14.41, and the 200-day average from +56.76 to −5.55. The rules earn by sitting through pullbacks; the protective stop stays 3×ATR under the entry, and the rule's exit decides at the close.
- **Consequences:** the page says how far each result can be trusted, and the forward record grows on its own. What it cannot say: whether gold's next years trend as 2021–26 did. Real money still needs a rule that EARNED it forward (ADR-031).

## Addendum (2026-11-02): told when a rule in use does worse than its history (S48)
- **Context:** S47 shows each daily rule's forward record since its registration against its own history. Nobody was told when the rule an auto-trader runs falls outside it, which is the first sign a rule may have stopped working.
- **Decision (`reporting/rule_watch.py`, worker job `rehearsal.rule_watch` daily):**
  - Every daily rule an active auto-trader runs on gold has its forward record (S47, `edge.forward_records`, Binance gold as the rehearsals trade it, kept for the day) set against its history before the registration, each trade counted against its own risk. A mandate on another market is not judged on gold.
  - **Told once** when the record is rarer than 1 run in 20 of as many trades drawn from that history: one WARNING alert per rule with the trades, the result (SIMULATED), how rare it is (never rounded up past the threshold), and the accounts running it.
  - **Still watched** while the record is below 1 in 10: the alert keeps the latest numbers and says so, without telling again. **Ends** once the record is usual again, whether or not an auto-trader still runs the rule (a rule may come back for real money later); a flagged rule stays checked daily until then.
  - **Real money:** when a real-money auto-trader runs the rule, the alert names that account, says that each of its trades still waits for the owner's code, and tells that account once on its own.
  - **The road to real money** shows an alert not yet acknowledged as a step; **Telegram `/rehearsal`** shows each rule in use since it was chosen, usual or not, and still answers if the records cannot be worked out.
  - It changes nothing by itself: each auto-trader keeps its own limits and its floor. The alert says what it cannot know: a daily rule's record is short (about two trades a month), the resampling draws trades one by one so losses that cluster look rarer than they are, and with several rules checked daily an alert now and then is chance.
- **Consequences:** a rule that stops working can be noticed while the money is simulated, with numbers, before the owner commits real money to it; the check has little power over a few trades, so its alert means "look", not "it is broken".

## Addendum (2026-11-02): a rehearsal that renews itself (S49)
- **Context:** a rehearsal's run lasts 90 days and was renewed only by the owner, with a code, after it ended and only once it held no trade. Between the end and the renewal the auto-trader decides nothing, and a trade still open keeps only its protective stop: its rule's exit is not taken (the approval of a mandated exit needs an active mandate). S47 measured what a fresh start costs these rules: restarted every 1 January, the 200-day average made −1.96 instead of +59.60 kept running, because a rule started afresh waits for its own next signal.
- **Decision (migration 0019, `trading/rehearsal.renew_due`, `mandates.renew`):**
  - **The owner's choice, made once:** when starting a rehearsal (with the code), "Renew it by itself" is part of the run's terms (`trading_mandates.auto_renew`, immutable like the other terms). Unticked by default.
  - **When a run ends** at its expiry, the auto-trader's 5-minute housekeeping renews it: a new 90-day run on the same rule, from the money it holds then (rounded down), granted under the owner's choice (actor `rehearsal-renewal`, audited as `mandate.renewed`, the owner told).
  - **A trade still open is taken over** by the new run, so the rule goes on managing its exit; its worst case at its stop (R19's open risk, rounded up) is kept apart from the new run's total loss stop, so the floor of $80 still holds if that trade is stopped out. The owner's own renewal works the same way now (it no longer waits until the rehearsal holds no trade). It waits only while an entry is on its way or the open trade has no fresh price.
  - **Never** after a run that stopped itself at its floor or that the owner ended, never once the owner pressed "Stop renewing" (append-only `mandate_renewal_stops`; like stopping, it needs no fresh code; a stop that lands just after a renewal reaches the new run), and not while new paper trades are paused (the emergency stop among them). Never on real money.
  - **It checks again what a start checks:** the venue's fee set and the market allowed by the risk policy, the rule still offered on daily bars, the market tradable, and the owner who chose it still active and allowed to approve trades. A renewal that needs the owner (at its floor, the fee removed, an error) is told once; the page says why the ended run has not renewed itself yet.
  - **One rehearsal at a time:** each renews in its own savepoint, so one's failure never undoes the other's; a renewal and a "stop renewing" of the same run lock its row, so neither misses the other.
  - **The database checks it** (`trading_mandates_guard`, 0019): a self-renewing run only on a PAPER account; a renewal continues the account's latest run, one that expired with renewal chosen and not stopped, keeps its account, rule, timeframe, markets, mode, care level, trade counts and owner, a loss per trade within the care level and the same daily stop for it, never lowers the floor (money less its total loss stop), lasts no longer, and continues a run at most once (a unique index).
- **Consequences:** a rehearsal can run for as long as the owner lets it, its rule managing its trades across runs, and its record stays continuous for gate G and the replay comparison. The owner keeps control: the choice is made with a code, can be stopped at any time, and real money never renews by itself (its activation lasts at most 90 days, ADR-034). What remains: a run that is not renewed (renewal not chosen, or stopped) still leaves an open trade with only its stop until the owner renews.
