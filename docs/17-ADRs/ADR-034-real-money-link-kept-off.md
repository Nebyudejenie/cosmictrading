# ADR-034: The real-money link for Binance, built and kept off
- **Status:** Accepted · **Date:** 2026-10-30
- **Context:**
  - On 2026-10-24 the owner chose to start real money with $100 on gold, and to have the real-money link built now but kept off until every gate passes and they switch it on themselves (the $100 Gold Plan, ADR-035).
  - Today live trading is unreachable only because `LIVE_ADAPTERS` is empty (ADR-010 addendum), plus the server's `COSMIC_MAX_AUTHORITY_MODE` cap.
  - `live_activations` (migration 0003) exists but has never been written.
  - The mandate trigger refuses any account that is not PAPER or SANDBOX. The account CHECK accepts any adapter on a LIVE account.
  - The review of the plan (2026-10-24) set these rules:
    - real money starts with the owner approving each trade;
    - `approvals_mandate_guard` stays untouched;
    - `LIVE_ADAPTERS` stays a reviewed code constant;
    - REST quotes stay DELAYED;
    - a key with withdrawals or transfers is refused;
    - an IP-restricted key needs a fixed outgoing address.
- **Facts checked on 2026-10-30** (official sources; recorded in `docs/integrations/binance-spot-api.md` §12):
  - **`GET /sapi/v1/account/apiRestrictions`** (USER_DATA, weight 1, `timestamp` and optional `recvWindow`) returns the key's own permissions:
    - `ipRestrict`, `createTime`, `enableReading`, `enableWithdrawals`, `enableInternalTransfer`, `enableMargin`, `enableFutures`, `permitsUniversalTransfer`;
    - `enableVanillaOptions`, `enableFixApiTrade`, `enableFixReadOnly`, `enableSpotAndMarginTrading`, `enablePortfolioMarginTrading`.
    - Withdrawals can only be enabled on an IP-restricted key.
    - `/sapi` does not exist on the Spot Test Network, so this check can be tested only against an emulator.
  - **`GET /api/v3/account`** reports the *account's* `canTrade`, `canWithdraw`, `accountType` and `permissions`, not the key's.
  - **`GET /api/v3/ticker/bookTicker`** returns the best bid and ask from the venue's memory, without a timestamp.
  - **Binance key rules** (Binance FAQ "How to Create API Keys", updated 2025-03-20):
    - system-generated (HMAC) keys without an IP restriction can only read;
    - self-generated Ed25519 or RSA keys can trade without one;
    - Binance strongly recommends an IP restriction for any permission beyond reading.
- **Decision:** real money is reachable only when **every lock below is opened by the owner, each on its own**. No code path, deployment, test or AI opens one. Each lock refuses alone, and a test proves it.

  | # | Lock | Who opens it | Where it is enforced |
  |---|---|---|---|
  | 1 | **`LIVE_ADAPTERS`** stays a reviewed code constant, **empty in this build**. Adding `binance_spot_live` is its own reviewed commit, made only after the owner decides. | Code review | `authority.live_preconditions`, `golive` gates C, F, G, I, the broker registry |
  | 2 | **`COSMIC_MAX_AUTHORITY_MODE`** stays `PAPER_TRADING` on the server. A live mode needs `APPROVAL_REQUIRED`; `STRATEGY_AUTHORIZED` stays refused in code. | The owner, in the server's env file | `authority.change_mode`, `enforce_cap` at startup |
  | 3 | **`COSMIC_LIVE_VENUES`** (new, empty by default) names the venues the server may trade with real money. Only `BINANCE` is accepted; any other value stops the process at startup. | The owner, in the server's env file | `Settings`, `live_preconditions`, `enforce_cap`, `binance_live.build` (S40b) |
  | 4 | **Gates A–H PASSED as evaluated** (ADR-010 addendum). | Recorded evidence | `golive.readiness` |
  | 5 | **The key check** (`live_key_checks`, append-only). The key must read and trade spot, be IP-restricted, and have every other permission off; a missing field fails. | Binance's key settings | Activation refuses without a PASSED check under 24 h old; a FAIL, or none for 26 h, ends it; the worker checks every 6 h (S40b) |
  | 6 | **The live activation** (gate I): one LIVE account, a money cap, an expiry of at most 90 days, a written reason, the owner's fresh code. | The owner, in the portal | `live.activate`, the two database guards, R20 |
  | 7 | **Each trade approved with a code.** ASK mandates only; no MANDATE approval on a LIVE account. | The owner, per trade | `approvals_mandate_guard` (unchanged), `trading_mandates_guard` |
  | 8 | **A LIVE quote at the moment of sending:** the venue's own book, read then. Anything older or public is refused. | — | R09 (`context_builder.venue_market`, S40b) |

  - **Lock 5, the key check.** The judge is a pure function, so the rule is in code, not in a person's reading of a settings page.
    - **PASS needs:**
      - `enableReading` and `enableSpotAndMarginTrading` true;
      - `ipRestrict` true;
      - false for `enableWithdrawals`, `enableInternalTransfer`, `permitsUniversalTransfer`, `enableMargin`, `enableFutures`, `enableVanillaOptions`, `enablePortfolioMarginTrading` and `enableFixApiTrade`.
    - **Fail closed:** a field missing, null or not a boolean fails the check.
    - **What is stored:** the booleans, the failures and a key fingerprint (the first 16 hex characters of SHA-256 of the public API key). Never the key.
    - **Stricter than Binance:** Binance lets an Ed25519 key trade without an IP restriction; this system does not. A leaked private key must not work from another address.
    - **A failure** ends every activation on that adapter and opens a security incident. It is CRITICAL when real money was active, so R15 blocks all new exposure until the owner reviews it. A failure while setting up, with no activation active, is a WARNING, so the rehearsals go on.
  - **Lock 6, the activation.**
    - **Refused unless:**
      - every lock before it is open;
      - the account is ACTIVE and reconciled OK;
      - no other activation is active for that account.
    - **Limits:**
      - the cap is at most `ACTIVATION_CAP_LIMIT` = 1,000 in the account's currency in this build, and the plan's default is 100; raising the limit is a reviewed code change;
      - the expiry is at most 90 days, matching mandates.
    - **Records:**
      - the rows are immutable, with UPDATE, DELETE and TRUNCATE revoked from the app role;
      - a revocation is its own append-only row (`live_activation_revocations`), so the record of what was allowed is never rewritten;
      - the evaluated gates are stored with the activation.
    - **It ends on:**
      - the owner's revoke;
      - its expiry;
      - the emergency stop, which ends every unrevoked activation (even one stamped a little ahead by another process's clock), so recovery never resumes real money by itself;
      - a failed key check;
      - a key check overdue by more than 26 hours, because the daily check must keep passing while money is live.
    - **Concurrency:** activating, ending, a mode change and the emergency stop each take the authority row's lock first, so they never interleave. Activating is refused in EMERGENCY_STOP or PROTECTION_ONLY.
    - **At startup** a stored live mode while `COSMIC_LIVE_VENUES` names no venue is forced to PROTECTION_ONLY with a CRITICAL incident, like a cap breach. The env file only takes effect at a restart, so callers without settings (the banner) can rely on it.
    - When the last active activation ends, the system restricts a live mode to `PAPER_TRADING`. Entering a live mode records the activation in `authority_state.live_activation_id`.
  - **The database refuses real money without an activation**, whatever the application does:
    - `orders_live_guard`: an order on a LIVE account needs an activation of that account, and the order's value (quantity × limit price) must be within the cap.
      - The activation must be active at the later of the order's `created_at` and the database's own clock, so an order stamped in the past cannot reach back into an activation that has since expired.
      - Only a reduce-only SELL exit or stop goes without one. On spot every buy adds exposure, whatever its flags say.
      - A real-money order's account, purpose, side, size and price never change: an update of any of them on an order that is or would be on a LIVE account is refused.
    - `trading_mandates_guard` (extended, not weakened): a mandate on a LIVE account needs:
      - mode ASK;
      - an active activation of that account;
      - money (`capital`) within the cap;
      - an expiry no later than the activation's.
    - `approvals_mandate_guard` is unchanged: no MANDATE-channel approval on a LIVE account.
    - The account CHECK accepts a LIVE account only with the adapter `binance_spot_live`.
  - **R20 "Real-money activation"** (risk engine, exposure-increasing checks):
    - on a LIVE account it needs an active activation, and holdings at their mark plus resting entries at their limit plus this order within the cap (UNKNOWN without a fresh mark);
    - on a PAPER or SANDBOX account it passes as "not real money".
  - **Readiness:** gate I passes only when gates A–H pass as evaluated and an activation is active. It stays BLOCKED while `LIVE_ADAPTERS` is empty, as today.
  - **The go-live page** lists the locks in plain words, each with its state now.
- **Built in three parts:**
  - **S40a** (2026-10-30, `e6c4178`):
    - migration 0016: the activation, revocation and key-check tables, the two guards and the account CHECK;
    - `trading/live.py`: activate, revoke, expire, the key judge and its record;
    - `COSMIC_LIVE_VENUES`;
    - the authority and readiness changes, R20, and the emergency stop revoking;
    - the go-live page's locks;
    - the lock tests.
  - **S40b, the link itself:**
    - **The live adapter:** `BinanceSpotAdapter.live_link` is the only way to its production base `https://api.binance.com/api`; the ordinary constructor refuses it. It is the same code as the test network's, named `binance_spot_live`, with `is_live=True`, the same order types (LIMIT buys, STOP_LOSS_LIMIT sells), and sells that only reduce (R04).
    - **`binance_live.build`** (worker only) returns the link only while locks 1 and 3 are open and both live key files are mounted. No deployment mounts them (a test checks every compose file), so it returns None. The read-only live check also uses the link, and places no order.
    - **The registry** serves a LIVE account only through that link, and only while `LIVE_ADAPTERS` still registers it at the moment of the lookup.
    - **Its contract:** the existing Spot Test Network run (AGENTS.md: no live adapter without its sandbox contract tests).
      - Its code version now ends with a hash of every file that shapes what is sent: the adapter, the contract, the broker types and the number formatting (`binance-spot-contract-1+<hash>`).
      - An activation needs a PASSED run for the code as it is.
      - A release that changes that code ends every activation at the worker's next sweep, and R20 refuses at once. A run counts only for the code that would trade.
    - **Reading the key:** `key_restrictions` reads `/sapi/v1/account/apiRestrictions` on the real host only; the test network has none.
      - The worker's `live.key_check` reads and records it every 6 hours, so a passing check is never a day old. If it cannot read the key, it raises a WARNING incident and records nothing; after 26 hours without a passing check, real money ends.
      - `cosmic broker binance-live-check` does the same by hand. It is read-only (ping, `exchangeInfo`, `account`, `apiRestrictions`, `bookTicker`; no order) and runs only while `COSMIC_LIVE_VENUES` names BINANCE. A failed step (the account cannot trade spot, say) fails the record too.
      - A later successful read resolves the "could not be read" warning.
    - **The R09 live-quote path:**
      - For a LIVE account the execution reads `bookTicker` through the link just before the pre-submission check, outside any transaction. The answer has no timestamp, so its time is when it arrived.
      - It is the check's bid and ask: LIVE when the round trip took at most 2 s (timed by a monotonic clock); STALE otherwise, or for a negative round trip; UNAVAILABLE when it could not be read or was not usable (NaN, infinite, crossed). Holdings are still valued at their stored marks (R07, R19, R20), as for every account.
      - R09 accepts only LIVE, at most 5 s old whatever the policy allows, and never stamped in the future. Its age is measured after the check's database locks.
      - Just before sending, a real-money entry whose price is now more than 5 s old is not sent, and its proposal ends (`live_price_too_old`). Exits and stops do not read the book: they only reduce.
      - The quote is used for that check and kept in its record (the PRE_SUBMISSION run's market context), like every other check's price. It is not stored as a quote under a new source, as first planned: a source that is never read would show as UNKNOWN on every page while real money is off.
      - Stored public quotes stay DELAYED and never count for a LIVE account.
    - **Tests** against the emulator, which serves `/sapi` on the real host only; no test calls a real venue.
    - **Docs:** ADR-025 addendum, the config message, the go-live banner, the LIVE label's definition and the runbook.
  - **S40c-1, the account and the owner's controls:**
    - **LIVE accounts** (`live.open_live_account`, worker job `trading.open_live_account`) open from Binance's balances through the live link, after a PASSED contract run of this code and a passing check of the very key the worker holds.
      - The owner chooses how much USDT the system manages, at most 1,000. The rest of the assets it trades (USDT, BTC, PAXG, XAUT) is booked as unmanaged. Reconciliation compares only those, and ignores trades from before the opening, as on the test network; other coins (BNB, say) are the owner's own.
      - The alias starts with `real-`.
      - Migration 0017: the account records its key's fingerprint, required for a LIVE account and fixed for its life; at most one LIVE account is active; a LIVE account stays LIVE.
      - Lock 5 counts only a check of that key, and a failed check ends only the activations of the account whose key failed.
      - The worker trades an account only with its own key: the registry refuses another one, and the worker's key check ends an activation whose account key is not the worker's.
      - No account becomes LIVE, or stops being LIVE, by an update; the fingerprint is 16 hex characters.
      - A refused activation is kept in the audit trail in its own transaction.
    - **The go-live page controls:**
      - "Open the real-money account" (owner, fresh code), shown only once locks 1 and 3 are open;
      - "Activate real money" (owner, fresh code; refused with every reason unless each lock before it is open);
      - "End real money now" (no fresh code, like a restriction; owner or emergency-stop holder).
  - **S40c-2, real-money proposals that ask first:**
    - **The grant** (`mandates.grant`): on a LIVE account the auto-trader only asks (ASK), only on daily bars, only with an active activation, with its money within the activation's cap; its expiry is clamped to the activation's. The database refuses AUTO, no activation, money above the cap and an expiry past the activation's (migration 0016; not the timeframe). The grant takes the authority lock, so a revoke under way cannot miss it.
    - **The proposal lifetime** (`mandates.proposal_ttl`): a real-money daily proposal lasts 18 hours (`LIVE_ASK_TTL`), through the owner's day; simulated ones keep the policy's lifetime.
    - **The auto-trader:**
      - for a LIVE account the worker reads the venue's book through the registry, outside any transaction, and times the decision after it;
      - the proposal's check is priced only by that book (`create_candidate(venue_quote=)`), labelled as the risk checks label it;
      - without the registry (any process but the worker) a real-money mandate is skipped, never priced by a stored quote.
      - R10's cross-venue check still compares stored public quotes (Binance against its test network): the venue's book prices the trade, and the stored quotes check that markets agree.
    - **Ending:**
      - ending an activation (revoke, emergency stop, a failed or overdue key check, a code change) revokes its account's auto-trader in the same transaction, audited, with the owner told;
      - at expiry the auto-trader ends by itself at the same moment, since its expiry was clamped to the activation's;
      - a real-money auto-trader's waiting proposals (PROPOSED, REQUIRES_REAPPROVAL, or APPROVED but not yet sent) end with it, and their approvals are revoked.
    - **A late approval:** the proposal's price band (0.5 % around the book it was made on) and its limit are fixed when it is made, and binding the approval to them is the point (ADR-011). If the price has left the band by the time the owner approves, the send is refused and the proposal ends (BLOCKED, never REQUIRES_REAPPROVAL: no one can re-price it). That day's signal is then lost, as any refused signal is. The page and the runbook say so.
    - **The go-live page:** "Let the auto-trader ask me" (owner, fresh code: daily gold on PAXG, an allowlisted daily rule, money and care) and "Stop it asking".
    - **Wording:** a real-money proposal never says "demo": it says it was made by the real-money auto-trader, which only asks, with REAL MONEY.
    - **An end-to-end test on the emulator** (`tests/trading/test_live_trade.py`), in real time:
      - the auto-trader asks, priced by the venue's book, with an 18-hour proposal;
      - the owner approves with a code;
      - the order goes out as real money on a fresh book;
      - ending the activation ends the auto-trader and the live mode;
      - stopping the auto-trader ends its waiting proposal;
      - a proposal approved after the price left its band ends, and nothing is sent.
- **Consequences:**
  - Nothing changes at runtime in this build. Live trading stays DISABLED, gate I stays BLOCKED, and `verify.sh` keeps finding "live trading: DISABLED".
  - When the owner decides, the path is fixed and visible:
    1. gates A–H;
    2. the server's env file (locks 2 and 3);
    3. the reviewed commit (lock 1);
    4. a key with withdrawals off and an IP restriction, then the key check;
    5. the account with its managed money;
    6. the activation with a code;
    7. the mode with a code;
    8. each trade with a code.
  - **New blocker B-11.** An IP-restricted key needs the server's outgoing address to stay fixed. The production VM sits on the owner's network, and whether that address is static is unknown. It must be checked with the owner's internet provider. Until then, the key check cannot pass.
- **Accepted limits of S40a (from the review of 2026-10-30):**
  - **The database cap is per order.** The total of holdings and resting entries is held by R20, not by the database.
- **Not done here:**
  - **Fully automatic real money** (STRATEGY_AUTHORIZED): a separate, later decision of the owner.
  - **HFM real money.** The bridge keeps refusing real accounts. It is designed only if the comparison picks HFM: a second activation per bridge account, the same database guards, and the bridge refusing any account the server's activation does not cover.
  - **A WebSocket book stream for LIVE quotes:** the request-time book is enough for daily rules.
