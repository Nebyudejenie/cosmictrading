# 18 — User Guide (owner / operator)

For the person who runs COSMICTRADING QUANT and approves trades. It describes the system as built on 2026-10-11: **paper trading only**. Nothing here can place a real order — there is no live broker, and the go-live gates are not passed.

## 1. First sign-in

1. The administrator creates your account with `cosmic admin create-owner` (it asks for a username and a password of at least 12 characters; nothing is passed on the command line).
2. Open the portal and sign in. The first time, you are sent to **Set up your authenticator**: add the key shown to an authenticator app (TOTP, 6 digits, 30 seconds) and enter a code. The key is shown once; reloading the page makes a new one.
3. Every later sign-in asks for a code. Privileged actions (approving a trade, changing the mode or risk limits, resuming after a stop) need a code from the last 10 minutes; the portal asks again when needed. Each code works once.
4. Five wrong passwords lock the account for 15 minutes. Sessions end after 30 minutes idle or 12 hours.

## 2. The command center (home page)

**Your HFM gold account comes first (since 3 Nov 2026).** The panel answers five questions, each ✓ yes, ✗ no (and what to do), or • not now:

1. Is my system working?
2. Is my broker connected?
3. Is automatic trading running?
4. Is my money protected?
5. Does anything need my attention?

Below them:

- **Account:** DEMO (virtual money) or REAL.
- **Money:** balance and equity, read from HFM itself.
- **Results:** today's result and the total, from HFM's own deals, after costs.
- **Trades:** the open trades, each with HFM's stop-loss, and the closed trades.
- **Gold:** HFM's gold price, and whether the market is open.
- **The auto-trader:** its latest decision (BUY, SELL or WAIT, and why), the last check of your account against HFM, and the next decision time, also in Addis Ababa time.

**Test one demo trade** (on the HFM demo only, when gold is open and nothing is open or waiting): it proposes the smallest gold trade, 0.01 lot = 1 ounce, with virtual money and a stop-loss 3 % below. You see it on the approval page first and approve it with your code within 15 minutes; only then is it sent to HFM. If the price has moved past its limit by then, nothing is bought. Look for it in MT5, then press **Close the gold trade** within half an hour and approve the close the same way. Within five minutes the system checks its records against HFM's balance, to the cent. It proves that orders reach HFM, fill, keep their stop and reconcile. It is not a trading idea: it costs the spread and whatever gold moves while it is open. When it is not possible, the panel says why.

**A real HFM account** (one line, then **Go-live readiness**): how many of the 11 checks have passed. Each is read from what is recorded: HFM's own report, the checks against HFM, the demo's own trades and how it recovered from outages. A setting never passes one. Real money at HFM stays off until every check has passed and you authorize it with your code; this version cannot trade a real HFM account at all.

**The menu, HFM gold only (since 4 Nov 2026):**
- **Dashboard:** the eight statuses, each "yes" only on evidence, and every HFM position with its ticket.
- **Gold analysis:** HFM's own price and candles, read on 1-minute to daily candles, and BUY, SELL or WAIT with the reasons.
- **My Gold Auto-Trader:** your gold trader (the simulation, Mode A).
- **HFM demo:** your actual HFM demo account (Mode B).
- **AI**, **Risk settings** and **System health**.

The rest is under **More**; the Binance pages are marked paused.

**My Gold Auto-Trader** (Mode A, the simulation; SIMULATED, nothing is sent to HFM):
1. Write **your amount** (for example $50). The simulation starts with it.
2. Choose:
   - **mode:** Simulation (HFM Demo comes next);
   - **automation:** ON or OFF;
   - **permission:** Automatic, or Ask me first.
3. Press **Review, then START**. The next page shows what the system calculated from your amount:
   - the risk per trade (2 %), the most a day may lose (6 %), the most at risk at once (5 %);
   - the stop-loss and take-profit distances, the spread and the swap;
   - the size of a trade now, or why it would wait;
   - from which amount HFM's 1-ounce minimum would fit.

   **Advanced** lets you change any limit. Press **START** with your code.
4. The status says what it does:
   - **WAITING** for an opportunity (with the reason);
   - **RUNNING** with a trade open;
   - **PAUSED** (automation OFF);
   - **NEEDS ATTENTION** (a trade waits for your answer, HFM's bridge is silent, or it has not decided for 3 minutes while the market is open);
   - **STOPPED**.
5. **Change your settings** at any time: they are saved in place, and the trader keeps running.
6. **Automation OFF** stops new trades; open trades keep their stop-loss and take-profit. It needs no code; ON does.
7. With **Ask me first**, each trade waits on the panel for 5 minutes: Approve (with your code) or Reject. Telegram tells you, and never asks for your code.

When a trade would need more than your risk allows, it says "WAIT — Your selected trading amount is too small for this opportunity at your chosen risk level", and from which amount it would trade at your risk. Your amount and risk are never changed.

**Approvals:** trades are approved in the portal only, with your authenticator code there. Telegram alerts you, lets you reject a trade, and stops everything, but it never asks for your code.

**Gold analysis** also says whether now suits a trade (open, too quiet, too wild, too costly, or closed), which sessions are open, and when London and New York are next open together (gold's busiest hours, a habit and not a promise). News is not checked.

**Mode B, the HFM demo** (your actual HFM demo account, its own rules):
- **HFM demo** shows HFM's own balance, positions (with tickets: yours by hand, or the system's) and deals.
- It answers whether HFM would take a trade from your amount at your risk. HFM's smallest gold trade is 0.01 lot = 1 ounce.
- **Test one demo trade** sends one real order to the demo, with your code.
- Automatic HFM demo orders from the analysis come next.

**Your own trades, made by hand in MT5** (since 4 Nov 2026): trade by hand on the demo whenever you like. Such trades are yours, kept apart from the system's money:

- the panel marks them "(yours, by hand)", and a sell as "sell";
- one line gives what they have made or lost after costs;
- the system never touches them, and its limits and results do not count them;
- "Is my money protected?" does count them: a trade of yours without a stop-loss makes it "no", until you set one in MT5 or close it.

Do not close or change the system's own trades by hand: the system treats that as a disagreement with HFM and stops new trades until it is understood.

**STOP ALL NEW TRADES** blocks every new trade at once; open trades keep their stop-losses, and starting again needs your code. A stop-loss is not a guaranteed limit: when gold jumps past it (at the weekend opening, on news), HFM closes the trade at the next price.

The paper rehearsals are folded below, marked SIMULATED and "not HFM money": nothing simulated is shown as your HFM account.

Then, top to bottom:

- **Authority banner** — the current mode. `RESEARCH_ONLY` (default): nothing trades. `PAPER_TRADING`: simulated trades only. `EMERGENCY_STOP`: no new exposure. Live modes do not exist in this build.
- **Needs attention** — anything that blocks trading or needs you: reconciliation not OK, orders in an unknown state, degraded protection, stale worker, open critical incidents, an open recovery checklist.
- **Market events and sources** — every price shows where it came from, when, and a label.
- **Pending approvals**, **tasks and research**, **system health** and **account health** — kept separate on purpose: the system can be healthy while an account is not.

## 3. Alerts

**Alerts** (top of the menu) lists everything the system wants you to know, unread first; the command center shows how many are unread. Each alert answers six questions: *what happened*, *what is verified*, *what is uncertain*, *the risk now*, *what was already done*, and *what you should do*. Mark an alert as read once you have dealt with it. Alerts appear here whether or not Telegram is set up.

**Every morning** (about 08:00 in Addis Ababa) Telegram brings a short daily report:
- the mode and the system's health;
- each account on its own line (PAPER, TESTNET or DEMO), in its own currency, never added together;
- trades closed in the last day;
- whether any strategy qualifies.

Send `/status` for the health lines at any time. On Mondays a weekly report compares each account with the week before, and on the 1st a monthly report does the same for the month. Each lists trades closed, fees, the largest drawdown and how each figure is defined.

**Operations → Workers → Backups** shows the newest backup and the last restore drill. An alert opens when a backup is late or incomplete, or a monthly drill is due.

**Fees.** Under **Risk policy → Fee per venue**, enter your real fee for each venue in bps (10 bps = 0.10 %), e.g. `BINANCE=10, HFM=0`. Proposals, the cost check and paper fills then use it. Venues you leave out are estimated at a cautious 60 bps. Take the numbers from your own accounts: Binance's fee page shows your tier, and MT5's History shows each HFM deal's commission.

## 4. What the labels mean

| Label | Meaning |
|---|---|
| `DELAYED` | Real public market data, not real-time. All market data in this build is DELAYED. |
| `SIMULATED` | Produced by the paper venue. Fills, balances and P&L in paper mode are simulated — never real money. |
| `STALE` | Older than its freshness limit. Anything that needs a current price is blocked. |
| `UNKNOWN` / `UNAVAILABLE` | The system does not know. Unknown never counts as safe: new exposure is blocked. |
| `ASSUMPTION` | A number the system had to assume (for example the 60 bps fee estimate) because the real value is not confirmed. |
| `VERIFIED` / `INFERRED` / `CONTRADICTED` | For research claims: checked by code against data / stated by AI or reasoning only / contradicted by data. |

## 5. Paper trading, step by step

1. **Operations → Paper account**: create an account (for example `paper-binance`), keep **Prices from: BINANCE (USDT)** and give simulated capital (for example 10,000 USDT). Fills are simulated against Binance's own prices. Coinbase and Bitstamp are being retired (ADR-027): the old `paper-main` uses Coinbase prices, so close its position before Coinbase is switched off.
2. **Operations → Authority mode → Switch to**: choose `PAPER_TRADING`, give a reason and press **Switch mode**. If your last authenticator code is more than 10 minutes old, the form asks for a new one.
3. **Portfolio → Reconcile now** (or wait for the worker): new exposure is blocked until the account's reconciliation is `OK`.
4. **Approvals → Create a manual proposal** (or wait for a research mission to propose one). Fill in the terms: instrument, quantity, limit price, a price band, maximum slippage, an optional stop and take-profit, and the reasoning — summary, thesis, strongest counterargument, red-team view, scenario, invalidation, entry conditions.
5. The system runs 18 deterministic risk checks for a buy. Two of them, added on 2026-10-16, limit how many positions an account holds at once (3 by default) and how much it could lose if every stop were hit (3 % of equity by default; a trade without a stop counts its whole value). Change them under Risk policy. If any fails, the proposal is `NON_EXECUTABLE` and the page says exactly which check failed and why (for example "R10: cross-venue check unavailable", "R03: reconciliation is UNKNOWN"). Nothing can override a failed check.
6. **Review** an executable proposal: the plain-language summary comes first, then every term — costs (fees are an *assumption*), estimated maximum loss with its warning, market data at proposal time, the checks, the management plan and the actions the system will *not* take.
7. **Approve exact trade**: you confirm a short-lived challenge bound to this exact packet. Approval is single-use; it expires; any change to the terms needs a new proposal and a new approval.
8. The worker re-checks risk immediately before submitting. If anything changed (price outside the band, data stale, limits, mode), it does not submit and tells you why.
9. **Orders** shows each step with its source: `LOCAL` (our request) or `BROKER` (venue confirmed). "Submitted" is not "filled"; only a broker-confirmed event counts.
10. After a fill, the protective stop is placed as a separate order (there is no atomic bracket). A stop is **not** a guaranteed maximum loss: prices can gap past it, and a stop-limit may not fill.
11. **Closing a position:** on **Portfolio**, give a reason next to the position and press **Propose closing**. You get an ordinary proposal to approve:
    - It sells at once or not at all (IOC), 0.1% under the bid.
    - Approving it cancels the position's protective stop first, then sells.
    - If the sale doesn't fill, the stop is placed again automatically.
    - It never sells more than you hold.

## 5a. Trading on Binance's test network (virtual funds)

Binance runs a test copy of its exchange (the Spot Test Network) with free, virtual money. Orders there meet Binance's real matching engine, so it is the step between paper trading and any real account. Everything about it is labelled **SIMULATED**.

1. **Key** (done 2026-10-15). On testnet.binance.vision, the server's public key (Ed25519) is registered, and the API key Binance showed is saved on the server (runbook §9). This kind of key has no "secret key".
2. **Check** (passed 2026-10-15). The administrator runs the contract check. **Operations → Binance test network** shows `SUCCESS` with the key's fingerprint, or `FAILED` with the reason.
3. **Open the account.** Same card: choose an alias (for example `binance-test`) and how much USDT it manages, at most what the test account holds.
   - The rest of the test account's money is shown as *unmanaged*: it is counted when balances are checked, but never traded or included in equity.
   - If opening is refused, the reason appears under Incidents.
4. **Allow it in the risk policy.** Open **Risk policy → What may be traded**. Add `, SPOT:BTC/USDT@BINANCE_TESTNET` to `allowed_instruments`, `, BINANCE_TESTNET` to `allowed_venues` and `, USDT` to `allowed_quote_currencies`. Give a reason and a fresh code. Without all three, the page tells you which one is missing.
5. **Propose, approve, watch.** Use **Approvals → Create a manual proposal** with that account and instrument, then approve as in section 5.

Differences from paper:
- Binance takes a buy's fee in the coin you receive. Buying 0.004 BTC leaves 0.003996 BTC (at 0.1%); the cost per coin includes the fee.
- Binance only trades whole steps (0.00001 BTC). Stops and exits use whole steps, and a leftover smaller than one step ("dust") stays on the account. Nothing tries to sell it.
- Binance resets the test network about once a month. The account then fails its balance check and stays blocked (safe); retiring it is not built yet.

## 5b. Trading on HFM's demo account (MetaTrader 5)

HFM is reached through MetaTrader 5 on the Windows virtual machine (since 22 Oct 2026; before, your Windows PC). A small program, the **bridge**, runs beside it. Keep MT5 logged in to the demo account and the bridge window open. If either stops, HFM shows as unavailable and nothing new is sent.

1. **Check.** The administrator runs the HFM contract check (runbook §11). **Operations → HFM demo** then shows `SUCCESS`.
2. **Open the account.** Same card: an alias (for example `hfm-demo`) and how much USD it manages. The rest of the demo balance is shown as *unmanaged*.
3. **Allow it in the risk policy:** add `CFD:XAU/USD@HFM`, venue `HFM` and currency `USD`. Raise the per-order and per-position limits above about USD 4,200, because the smallest gold trade is 1 oz.
4. **Propose and approve** as in section 5, with account `hfm-demo` and instrument `CFD:XAU/USD@HFM`. The quantity is in ounces (gold) or BTC.
5. **Or test one trade from the home page** (section 2): the same proposal and approval, filled in for you.

What is different at HFM:
- **These are CFDs.** You hold a price contract, not metal or coins. The system still pays for each position in full from the account's cash, so there is no leverage.
- **Buy only.** A sale only closes what you hold.
- **Stops.** The protective stop is the position's own stop-loss at HFM. When touched, it sells at the market (it can fill below the stop in a fast market).
- **Fees.** Commission and the overnight swap (about −0.79 USD per night for 1 oz of gold on 2026-10-15) are charged by HFM and booked as fees.

## 6. Emergency stop

**Operations → Emergency stop** (or Telegram `/stop <reason>` once the bot is configured). It immediately blocks all new exposure, revokes unconsumed approvals, cancels unfilled entry orders and starts a reconciliation. It does **not** close positions or cancel protective stops.

To resume: the recovery checklist must be complete (every account reconciled after the stop, no unknown orders, critical incidents acknowledged), you confirm that you reviewed positions and protective orders, give a reason and a fresh code, and choose a mode no more permissive than before the stop.

## 7. Risk policy

**Risk policy** shows every limit with what it measures, its currency, boundary, and what happens when data is missing. Changing a limit creates a new immutable version (needs the capability, a fresh code and a reason). Approvals made under the old version must be re-approved. Leverage, short selling and market orders cannot be enabled.

## 8. AI

AI is optional and free-only. Without provider keys (the current state), research missions pause at the narrative step (`WAITING_FOR_AI`) and no AI-researched proposal is made; prices, risk checks, approvals, orders and reconciliation all work without AI. To enable a provider, an administrator adds its key as a secret file, then you attest its free status and approve specific free model IDs on the **AI** page. Any sign of billing disables the provider.

## 9. What you cannot do (yet)

- **Trade real money:** no live broker adapter exists and live modes can't be selected (B-06/B-07). Binance's test network (virtual funds, section 5a) is the step before that. Exness is blocked until its API documentation and a demo trading account are available.
- **Use AI-written research:** no free AI keys are configured yet (B-04). The strategy library (section 12) works without AI.

The staging portal is at `https://staging.cosmictrading.bond` (behind Cloudflare Access), and Telegram is linked.

## 10. Searching research

**Search** finds research claims (each marked VERIFIED, INFERRED or CONTRADICTED), mission reports and — if you can read accounts — proposals. Use plain words, "quoted phrases", OR, and -word to exclude. Search covers stored research, not live prices.

## 11. When something looks wrong

Use the **Audit** page: every proposal, decision, order step and mode change is recorded in a tamper-evident log, and `cosmic audit verify` checks it. `docs/15-operations-runbook.md` lists the first actions for each incident type.

## 11a. The auto-trader: what to expect, and the market radar

- **Before you start:** on **Auto-trader → Start** (or **Change money or settings**), enter your money and care level and press **Show my plan**. Under **What to expect**, the auto-trader's own rule is replayed on the last 12 months of hourly prices with exactly that plan. You see:
  - what it would have made or lost;
  - how often it traded and won;
  - its typical win and loss;
  - its worst losing streak and deepest drop;
  - each month and each market.
- **Read it as a rehearsal, not a promise.** A rule that made money last year can lose next year, and a loss is shown just as plainly. The costs are assumptions; open **How this was tested** for every detail.
- **Start with $100 (since 24 Oct 2026):** the menu item **Start with $100** explains, in plain numbers, what $100 can do in gold and starts a rehearsal: 100 USDT of simulated money on Binance gold prices, treated exactly like your real $100 will be. First set your Binance fee under Risk policy. The rehearsal decides once a day, risks at most 2 % of its money a trade, and stops for good at 80.
- **Two rehearsals to compare (since 27 Oct 2026):** besides Binance gold, a second $100 rehearsal runs on HFM's own gold prices with an HFM Cent account's terms (tiny 0.01-ounce trades, no commission, the nightly swap charged). Start with $100 shows both side by side. Gold closes at weekends, so a Friday signal is acted on at Monday's open.
- **Your choices after the audit (since 3 Nov 2026):**
  - **HFM Cent stops at market,** as a real HFM account does: when gold jumps past its stop, it sells at once at the market price, so the loss can be larger than planned. Binance keeps the stop-limit of a real Binance account: in a jump, you are asked to approve the exit.
  - **The swap is kept aside:** the HFM rehearsal stops for good a little earlier, keeping back what its next trade could pay in nightly swap (about 0.90), so that it ends at or above 80.
  - **A missed morning is tried again:** if the 03:00 decision is refused only because some data was not there yet, it tries again each hour that day; you are told once.
  - **Good to know:** on HFM Cent the smallest trade is 0.01 ounce. At half size (after a 10 drop) it may risk more than the 1.00 allowed, and then that rehearsal waits instead of trading; it is told as a skip.
- **Daily rules (since 24 Oct 2026):** on the setup page you can choose **daily bars** instead of 1-hour bars. A daily rule trades a few times a year and holds for weeks, so fees weigh much less; with $100 that matters most. It decides once a day, around 03:00 in Addis Ababa, and trades by itself (it cannot ask first, because the proposal would expire while you sleep). **What to expect** then replays the rule over up to five years of daily prices.
- **Your risk policy comes first (since 24 Oct 2026):** besides the auto-trader's own limits, every trade passes your risk policy, which applies to the whole account. The auto-trader now makes a trade smaller rather than have it refused. The setup page tells you where the policy is stricter at your money, and which value to set. The most important one: after **4 losing trades in a row** (max_loss_streak), the policy refuses every new trade on that account until you change it. "What to expect" now shows this too.
- **More than one auto-trader:** each paper or demo account can run its own, for example the HFM demo on 1-hour bars and a $100 rehearsal on daily bars. The auto-trader page lists them; each is changed and stopped on its own.
- **Half size after a bad run:** after five losing trades' worth below its best result, the auto-trader risks half as much on each trade until it recovers; Telegram tells you when it switches.
- **Every morning on Telegram:** the daily report ends with your auto-trader's state and result, the rule competition's verdict and leader, and gold's price and trend.
- **Deciding a trade:** every proposal to buy shows "Should I take this trade?". It checks:
  - whether the rule has proven itself in the competition;
  - whether the trade goes with the daily trend;
  - whether it is a busy trading hour;
  - whether the spread is normal right now;
  - how much of the risk the costs eat;
  - how big the trade is for your account.
  Many ❌ marks mean waiting is the better choice. It is a guide, not advice.
- **The total loss stop:** besides the daily stop, the auto-trader switches itself off for good after losing ten trades' worth (10 % of your money at Normal). It never starts a trade that could take it past that. If it stops, the page says STOPPED ITSELF; start it again only when you want to.
- **Ideas and the race:** the competition page also has "Ideas from the research" and "The forward test: the race on new prices". Ideas count only on prices after 23 Oct 2026, so the race is free of hindsight. Check it every week or two.
- **The rule competition:** **See the leaderboard** on the auto-trader page shows every published rule tried the way the auto-trader would trade it. Each rule is chosen on older prices and checked on newer prices it never saw, then scored on new prices from 23 Oct 2026.
  - **PROMISING:** it made money on the unseen prices, but not clearly beyond luck.
  - **QUALIFIED:** it passed every test.
  - **EARNED:** it is qualified and also made money forward. Only an earned rule can be suggested for your auto-trader, and you decide.
- **The radar:** each market on the auto-trader page has a chart of its last 48 hours.
  - **Waiting:** the dashed orange line is where it buys.
  - **In a trade:** the green dashed line is where it sells, the dotted line is your stop, and a dot marks where it bought.

## 11b. Gold

- **Open Gold in the menu** (or send `/gold` on Telegram). It shows:
  - gold's price and how it moved;
  - whether it is trending, and how wild it is today;
  - which trading session is open now;
  - whether one ounce fits your money.
- **"Why the auto-trader lost on gold"** replays its rule on the last year twice, with and without costs. It shows what costs took, how trades ended, which hours lost most, and how the trend mattered, in plain sentences.
- **"How to win on gold"** lists the levers. The biggest is cost: tell the system HFM's real commission (MT5 → History → Commission column; then Risk policy → Fee per venue).
- **"Gold over the long run"** shows what buying a fixed amount every month and holding would have done. It is the bar any gold trading has to beat. It is a benchmark, not advice.

## 12. The strategy library (BTC and gold)

**Strategies** lists 100 published rules (moving averages, breakouts, momentum, mean reversion, volatility, volume, calendar effects, and five BTC/gold rotations). Each has its rule in plain words and its source.

**How they were tested:**

- Every rule is tested once, with its published settings and nothing tuned to our data. BTC uses Bitstamp BTC/USD daily since 2011; gold uses Binance PAXG/USDT daily since 2020.
- It is tested under two cost assumptions: Binance-like and Coinbase-like.
- The first year is skipped. The rest is split into an earlier "development" part and a later "validation" part.
- The question asked: did the rule's **timing** beat simply holding the same average amount all the time?
- Because 100 rules were tried, the best ones look good partly by luck. The **deflated Sharpe probability** corrects for that.

**Tiers:**

- **QUALIFIED:** the timing skill is significant after that correction, held up in validation, survives double costs, made at least 10 trades and fell less than buy-and-hold.
- **WATCH:** the timing was positive in both periods but isn't proven.
- **REJECTED:** everything else.

Expect few or none to qualify. That's the honest result.

**Switching a rule on (paper only):**

1. Open the rule, choose the instrument (Coinbase BTC/USD or PAXG/USD), give a reason and confirm with your authenticator code.
2. A WATCH rule also needs you to tick "forward test, not a proven edge".

At most 10 rules can be on at once. REJECTED rules can't be switched on.

**What a switched-on rule does:**

- After each daily close, if the rule turns long, it creates an ordinary proposal. The proposal shows the rule's evidence, its tier, its counterarguments and a protective stop at 3× the 20-day average daily range.
- That stop is required by the risk policy. It is not part of the tested rule, so results can differ from the backtest.
- When the rule turns flat, it proposes selling exactly what this rule bought. Approving that exit cancels the rule's protective stop first, then sells.
- Every trade still needs your approval of the exact terms.

**Gold notes:**

- Gold is held as PAXG: a token backed by one troy ounce of gold, with issuer and custody risk, and it can trade slightly above or below spot gold.
- Binance's XAUUSDT contract and Exness XAUUSD are leveraged products, outside the risk policy.

