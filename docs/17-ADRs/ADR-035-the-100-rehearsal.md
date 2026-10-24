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
