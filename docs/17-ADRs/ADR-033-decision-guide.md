# ADR-033: A decision guide on every proposal to buy — "Should I take this trade?"
- **Status:** Accepted · **Date:** 2026-10-23
- **Context:**
  - **Owner's request (2026-10-23):** "if he gets information, in which way he can decide to trade … make it easy to trade small amounts many times by recommendation and automated trading".
  - **The gap:** a proposal card shows what will be done and the risk, but not whether the moment is a good one. In ASK mode the owner decides alone.
  - **Two cautions:**
    - Many small trades pay costs many times. On hourly gold the costs decided the result (ADR-032).
    - The system must not present a guess as a recommendation.
- **Decision:**
  1. **Six signs** (`reporting/decision.py`), each FINE, CARE, AGAINST or UNKNOWN:
     - **Evidence:** the rule competition's verdict on the proposal's rule on this market (ADR-031):
       - earned: FINE;
       - qualified or promising: CARE;
       - no proven edge: AGAINST, with its result on unseen prices;
       - a manual trade or no run: UNKNOWN.
     - **Trend:** the close against its 50-day average on the signal series.
     - **Time:** the session now (London and New York: FINE; otherwise CARE).
     - **Cost now:** the spread now against its median of the last day (≤ 1.5×: FINE; ≤ 3×: CARE; more: AGAINST).
     - **Cost of this trade:** spread, both fees and both slippages against what the stop risks, in R (≤ 0.10: FINE; ≤ 0.25: CARE; more: AGAINST).
     - **Size:** the loss at the stop as a share of the account (≤ 1 %: FINE; ≤ 2 %: CARE; more: AGAINST).
  2. **A plain summary** counts the signs. When the evidence is AGAINST or UNKNOWN, it says the rule has no proven edge yet, so the trade is "a demo test, not a bet". It never says "buy".
  3. **Where it appears:** on the approval page ("Should I take this trade?") and under the Telegram card, for proposals still to be decided. An exit needs none, because it only reduces risk.
  4. **Read-only.** It changes no check, no limit and no decision. The risk checks, the mandate and the owner's code still decide.
- **Consequences:** the owner sees, at the moment of decision, the evidence for the rule, whether the market and the hour favour it, and what the trade costs before it can win. Trading small and often stays possible, and its cost is in plain view each time.
