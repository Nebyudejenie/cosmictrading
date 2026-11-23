# ADR-040: The owner's PDF plan sets the rules for Manual Trading, with an honest recommendation

- **Status:** accepted, 2026-11-23 (S117).
- **Context:** the owner sent their plan, "Risk management plan challenge 100$ - 25000$" (a PDF, kept out of the repository), and asked that it be the source of truth for their real account's rules:
  - risk 10 % of the balance on each trade, worked out automatically;
  - insight, probability and recommendations, delivered fast.
- **Their choices (2026-11-23):**
  - **"Auto plan, I press Buy/Sell":** the system works the trade out; the owner places it in MT5 themselves.
  - **1:2 Balanced** as the default reward.
  - **"Exactly the PDF":** no pause after losses.

## What the PDF fixes

- Risk at most 10 % of the current balance on every trade, compounding.
- Two trades a day, Monday to Friday.
- A reward of 1.5, 2 or 3 times the risk.
- The win rates it assumes (55 %, 50 %, 45 %) are assumptions, not predictions.
- Break-even = 1 ÷ (1 + reward). "Blow-up" means the balance below $1.
- Its 20,000-run variance table, and its warnings: 10 % is high risk, costs are left out, and the expected path is not the typical one.

## Decisions

1. **The PDF's figures are reproduced, not paraphrased.**
   - `manual/challenge.py` computes the roadmap, the trades and days to $25,000, the blow-up counts and the streak table in Decimal.
   - Tests check them against the PDF to the cent: 114 trades / 57 days and $26,036.35 at 1:2; 44 and 97 losses.
   - Its simulation (fixed seed, 1,000 runs) reproduces the PDF's variance table within sampling error.
2. **"Use my PDF plan" is one button with the owner's code** (`POST /manual/plan`, audited `manual.plan_started`). It writes one new version of their append-only rules:
   - 10 % default and maximum (the existing cap of 10);
   - 2 trades a day, weekdays only, 2 positions;
   - at most two full losses a day (20 %) and 20 % open at once;
   - no cooling-off;
   - the duplicate guard stays: it catches a trade placed twice by mistake, and is not a plan rule.
   - The balance at that moment is the plan's start. Without one, the start is worked out from the balance less the trades since (deposits are not reported).
3. **The daily loss limit is a share of the day's starting balance** (balance − today's realised result), no longer of the equity now. A limit that shrank with each loss refused the PDF's second trade after a loss: $10 lost, then $9 at risk against 20 % of $90 = $18.
4. **A trade's risk is a share of the balance, or of the equity if lower.** That is never more than the PDF's "10 % of the current balance", and never more than what the account holds now.
5. **The next trade is worked out by itself** from the setup now, the plan's reward and the account's own contract:
   - side, entry at the live price, stop, target, lots rounded down, and money at risk and at the target;
   - it goes through the same sizing and rule checks as the calculator.
6. **The recommendation is honest about the evidence (`manual/advice.py`).**
   - **TAKE** only when the low end of the record's 95 % range clears break-even after the spread.
   - **YOUR CALL** when the record is above break-even but its range is not.
   - **SKIP** otherwise.
   - **UNTESTED** at a reward the record was not measured at (it was measured at 1:2).
   - The probability stays HISTORICAL SIMULATION · IN SAMPLE, not verified, until 30 forward-test trades.
   - Setup checks (what can fail now) are kept apart from it.
   - The four criteria that always held once the rule fired are no longer counted.
7. **The reality check is shown beside the PDF's figures.** The plan is simulated at the rule's own tested win rates:
   - 34.6 % over all its 596 trades;
   - 44.4 % for calm-market BUYs, the only kind recommended TAKE;
   - every trade that missed its target counted as a full loss.
   - The page says plainly that the rule shows fewer winners and fewer setups than the PDF assumes, so the PDF's pace and drawdowns do not apply to it.
8. **Fast, without slowing the robots.**
   - `/manual/now.json` reuses one reading for 10 s, and `/static/manual.js` (CSP `'self'`) updates the next-trade box every 10 s.
   - The worker job `manual.setup_watch` sends a TAKE or YOUR CALL setup to the owner's Telegram and portal once per candle and side, with no account number. It builds the page (about 0.2 s of CPU) only after a new 15-minute candle.

## Consequences

- Nothing here sends an order:
  - terminal 3 stays read-only;
  - `LIVE_ADAPTERS` stays empty;
  - the portal's BUY/SELL stays locked behind the nine gates (ADR-038).
- The robots and their HFM demos are unchanged.
- At $100 and 10 %, the smallest lot (0.01) risks more than $10 whenever the stop is wider than 10.00. The page then says NO TRADE rather than raising the risk. Calm-market stops (under 9.46) fit.
- The PDF and the owner's recording are never committed (`.git/info/exclude`).
