# ADR-031: The rule competition — a rule earns the auto-trader on unseen data, then forward
- **Status:** Accepted · **Date:** 2026-10-22 · Builds on ADR-024 (the strategy library) and ADR-030 (the demo auto-trader)
- **Context:**
  - **The owner's goal (2026-10-22):** "help me by automate trading to get profit". No rule can be promised to make money.
  - **What the evidence said that day:**
    - Of the library's 100 rules on daily bars, none qualified (ADR-024).
    - The auto-trader's own rule, Turtle 20/10 on hourly bars, lost money over the past year after costs (ADR-030 addendum 2).
    - Testing many rules and keeping the best one finds luck, not skill, unless the choice and the check are kept apart and the number of tries is accounted for.
- **Decision:**
  1. **Same trading as live.** Every single-market rule of the library (95; the five rotations need two markets) is replayed with the auto-trader's mechanics (`expect.py`: next-open fills, the 3×ATR stop with gaps at the open, sizing to the per-trade loss, 5 trades a day, a daily stop of 3 losses). It runs on each market the auto-trader trades, on hourly and on daily bars, with the costs it would pay there (the policy's fee for the venue, the venue's measured spread, 0.05 % slippage). Results are in R, multiples of the per-trade loss.
  2. **Chosen on the earlier part.** After 250 warm-up bars, the first 60 % of each series is the development part. A rule becomes a CANDIDATE there with at least 30 trades (hourly) or 16 (daily), a positive mean R, and a t-statistic of at least 2.
  3. **Checked on the later part**, never used for choosing. A candidate QUALIFIES when all of these hold there:
     - it is positive, with at least half as many trades;
     - its t-statistic reaches the one-sided 5 % threshold shared among all candidates (Bonferroni: 1.64 for one candidate, 2.58 for ten);
     - it is still positive at double costs.
     A candidate positive there without significance is PROMISING; everything else is REJECTED. A rule that cannot run on the bars is shown as N/A with the reason.
  4. **Proven forward.** Every rule is also scored from **2026-10-23 00:00 UTC**, a date fixed in the code before any of those bars existed, on new bars only. A QUALIFIED rule has EARNED the auto-trader once its forward record has 30 trades (hourly) or 10 (daily) and is positive after costs.
  5. **The owner decides.** Earning makes a rule eligible for review, nothing more. It joins the auto-trader's rules (`AUTO_RULES`) only by a reviewed code change, as ADR-030 requires, and is used only when the owner starts the auto-trader with it.
  6. **Kept as it was.** The worker (`research.auto_competition`) runs the competition once a day, or at once after a code change. It runs in a thread so the worker's heartbeat continues. Each run is appended to `auto_competitions` (migration 0014; append-only by trigger and grants) with its code version, data end, trials, costs and the whole leaderboard.
  7. **Shown.**
     - `/trader/competition`: the verdict, the auto-trader's own rule, the leaderboard (each rule's earlier, later, double-cost and forward results, and a small R line with the start of the unseen part), every result, and how it works.
     - `/trader` and Telegram `/trader`: one line with the verdict.
- **First results** (2026-10-22, checked locally on public Binance prices, before the first run on the VM):
  - **Hourly bars, the last year:** no rule became a candidate. Several were positive on the earlier part and negative on the later one.
  - **Daily bitcoin, 2018–2026:** 10 candidates. 9 stayed positive on 2023-05 → 2026-10 and at double costs, but none reached the 2.58 bar (best 1.53). They are PROMISING.
  - **Daily gold:** nothing.
- **Consequences:**
  - There is a fair, repeatable answer to "which rule should trade", and it currently says none has earned it. The forward test is where an earned rule can come from.
  - Choosing by hindsight is closed off by the fixed forward date and the per-run code version.
  - If a daily rule earns the auto-trader, the auto-trader first needs a daily mode. Mandates allow only `1h` today, and the database checks it.
- **Not decided here:** real money (ADR-010, gate G, owner activation).

## Addendum (2026-10-23): ideas from the research, and the forward race

- **Why:**
  - The owner asked for "all your advanced ideas".
  - The Gold research (ADR-032) suggested filters: the daily trend, and the trading session.
  - A filter found by looking at the past cannot be judged on that past.
- **Decision:**
  1. **Ideas, registered 2026-10-23** (`research/library/hypotheses.py`). Each is a published rule with a causal entry filter:
     - Turtle 20/10 with the daily trend up (close above its 50-day average; 1,200 hours on hourly bars);
     - Turtle 20/10 in London and New York hours (07–21 UTC, hourly only);
     - Turtle 20/10 with both filters;
     - Keltner breakout with the daily trend up.
     An entry still needs the rule's fresh flat → long change, and exits are the rule's own.
  2. **Judged forward only.** Ideas run in the competition with tier IDEA. They are not trials of the backtest protocol and are never CANDIDATE, QUALIFIED or PROMISING. Their earlier results are shown as "seen before it was registered" and never count. An idea EARNS the auto-trader only with a forward record of `FORWARD_TRADES` trades that is positive with a t-statistic of at least 2 — stricter than for a published rule, because it was found by hindsight. Adding an idea is a code change with a new registration date; the code version changes, so it is recorded.
  3. **The forward race** on `/trader/competition` ranks everything that has forward trades by its forward result, with a small line per row. Ranking is fair there: those prices did not exist when the rules and ideas were fixed. Ideas are kept out of the main leaderboard and have their own section.

