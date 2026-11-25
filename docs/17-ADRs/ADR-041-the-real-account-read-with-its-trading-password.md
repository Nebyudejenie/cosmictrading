# ADR-041: The owner's real account may be read with its trading password

- **Status:** accepted, 2026-11-25 (S117 follow-up).
- **Context:** terminal 3 must read the owner's real Exness account. Exness refused every investor (read-only) password the owner typed:
  - "Invalid account" on 2026-11-24 and twice on 2026-11-25, per terminal 3's journal;
  - only the account's trading password was ever accepted.
- **The owner's choice:** asked between setting a new investor password and using the trading password, they chose **"Use my trading password"** (AskUserQuestion, 2026-11-25).

## Decision

1. **A REAL account may be read while signed in with its trading password** (`mt5_brokers.REAL_TRADING_PASSWORD_OK`).
   - The Manual Trading page shows it live as **LIVE-CONNECTED · TERMINAL READ-ONLY** and says which password it is.
   - Broker Connections verifies it, and says so.
   - The "trading password" alarm is no longer raised; its open incident is resolved with the owner's choice.
2. **Reading only, unchanged:**
   - Terminal 3's bridge (13, `"read_only": true`) refuses every trading command.
   - The server hands terminal 3 only sign-in and identity commands.
   - `LIVE_ADAPTERS` is empty, and portal BUY/SELL stays locked behind the nine gates (ADR-038).
   - The account's `read_only` flag still means "the system cannot trade on it", so the execution lock that needs a non-read-only account stays closed.
3. **An offline terminal still proves nothing** (the NOT_CONNECTED rule of the same day): a sign-in counts only while MT5 is connected to the broker's server.
4. Setting `REAL_TRADING_PASSWORD_OK = False` restores the earlier rule: investor password only, otherwise NOT READ-ONLY with a CRITICAL alarm.

## Consequences

- **The risk accepted by the owner:** the password that *can* trade their real money is saved by MetaTrader on the bridge computer. Anyone, or anything, with access to that computer could trade the account in MT5. The system itself still cannot.
- The owner places every trade themselves, in their own Exness app.
- AGENTS.md records the choice beside the earlier rule.
