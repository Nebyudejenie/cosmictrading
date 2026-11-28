# ADR-045: Learning mode on the robots' demos: every signal, stopped for the day, and a learning card

- **Status:** accepted, 2026-11-28 (S124).
- **Context:**
  - By 2026-11-28 neither robot had ever traded by itself on its demo.
  - Robot B's 1-minute rule found about 40 setups a weekday. Its calibrated probability stayed at 26.5–27 % (break-even 33.3 %), so the owner's bands rejected every one: 15,532 decisions, no trade.
  - Robot A's trend rule opens by itself only where its record is 30 % or more and made money. Its terminal 2 has been signed in to no account since 2026-11-26.
  - The owner asked: "make the auto trader robots trade … start trade by themselves … find one logic, the percentage of losing and winning and probability … learn … it is demo money".
  - Their answers (2026-11-28):
    - **"Yes, learning mode"**: on the demos only;
    - **"Stop for the day"** after three losses in a row;
    - **"Use an Exness demo"** for Robot A. That move is a separate change.

## Decision

1. **Every signal, by itself, on a demo** (`gold.learning.on`: a SANDBOX account, virtual money).
   - Each robot takes every valid signal of its own logic, with no approval:
     - Robot B: its 1-minute setups, below the 30 % line too. No approval ticket is used.
     - Robot A: its trend signals, below 30 % and where its record made nothing.
   - Everything else still holds:
     - the mandate's money limits;
     - the day ceiling (no trade whose full loss would pass the daily limit);
     - one trade at a time (Robot A) and the most open at once (Robot B);
     - the speed rule, the AI's veto (Robot B) and every risk check (R01–R16, R08's account limits included);
     - the stop-loss and take-profit held by the broker.
   - Each such trade says LEARNING on its intent (`details.learning`), its proposal and the page.
   - A real account never trades this way. No LIVE adapter exists either.
2. **Stopped for the day** (`controls.day_stop`).
   - What stops it: as many losing trades in a row today as the owner's pause allows (3), or the daily loss limit.
   - It stops opening trades until 00:00 UTC. Its status stays RUNNING, so with the next day it trades again by itself.
   - Off a demo, and with learning mode off, the old rules hold: PAUSED or LOSS_LIMIT, ended only by the owner's RESUME.
   - The owner's own PAUSE, and a pause for a stop or target the broker does not hold, are never lifted by the day.
3. **One logic for its odds: the learning card** (`gold.learning.Card`).
   - Each robot's chance of winning starts from its tested record:
     - Robot B: 26.9 % (lab Run 6);
     - Robot A: 34.6 %, BUY and SELL together (lab Run 8).
   - Each closed learning trade moves it: `(30 × record + won) / (30 + trades)`. The record counts as 30 trades.
   - Beside it: the measured win and loss percentages, the 95 % (Wilson) range, the net result and the break-even.
   - Its label is LEARNING · DEMO up to 30 trades, then MEASURED · DEMO. It is never called verified.
   - **The rules are never tuned on these results.** A rule fitted to its own few trades only fools itself. A change of rule is the owner's decision, on evidence.
   - Each closed learning trade is told once, on Telegram and the portal, with the card after it. This is the minute job's work (`gold.robot_setup`), never the robots' one-second step.

## Consequences

- The robots trade on their demos from the next market open without any step from the owner. Robot B's HFM demo is connected.
- The owner was told plainly: Robot B's own record says it should lose about 0.39 USD a trade; the demo now measures whether it does.
- R08's account-wide limits stay as the owner set them (20 % below the account's best, or 10 losses in a row). If one is reached, that account stops until the owner decides.
- To switch learning mode off: `learning.KINDS` empty (a code change with its tests). The old tests of the owner's bands run with it off.
- Tests:
  - `tests/trading/test_robot_learning.py`:
    - Robot B takes a 27 % setup by itself, a loss is counted and told once, a day stop that never pauses it, and it trades again the next day;
    - Robot A sends a 26.7 % SELL by itself;
    - the card's arithmetic;
    - only a demo learns.
