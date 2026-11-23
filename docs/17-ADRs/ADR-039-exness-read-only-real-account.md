# ADR-039: The owner's real account for Manual Trading may be at Exness, read-only

- **Status:** accepted, 2026-11-23 (S116).
- **Context:** the owner tried to add their real Exness account in Broker Connections and got "No installed terminal is free for a REAL account (Terminal 1 · demo · HFM's prices holds cosmic)". They asked to use Exness for Manual Trading.
- **Two causes:**
  1. Real accounts are read only on terminal 3, and terminal 3 is not installed on the bridge computer yet (B-12). The message named terminal 1 only because it was the one installed terminal; terminal 1 is a demo terminal and never holds a real account.
  2. The system accepted HFM only. The database allowed `broker = 'HFM'`, and Manual Trading showed an account as live only when MT5 reported "HF Markets". Its clock was checked against HFM's rule, and its gold was assumed to be named "XAUUSD".

## Decisions

1. **Exness for the owner's real account only, read-only.** Migration 0039 allows `broker IN ('HFM','EXNESS')`, and Exness only as a REAL account with its investor password and no execution switch. The robots' demo accounts stay at HFM; Robot A and Robot B are unchanged.
2. **The broker is chosen on the form and checked against MT5.** The account is verified only when MT5's own `account_info.company` names the chosen broker; otherwise WRONG_BROKER.
3. **Manual Trading recognises HFM or Exness** from that same company name (`trading/mt5_brokers.py`), and names the account's broker in its checks, calculator lines and evidence table.
4. **The gold symbol is the broker's own.** Exness may suffix its symbols by account type (XAUUSDm and others, per third-party guides; not verified from an official page).
   - Terminal 3's bridge reads `XAUUSD*` and expands it from the account's own symbols (bridge 13, `Bridge.symbols()`).
   - The server uses XAUUSD when it exists, otherwise the first reported name that starts with it.
5. **The clock is measured, never assumed, for Exness.**
   - Sources disagree on Exness's server time (UTC+0 is most often cited), and no official rule was found, so none is checked.
   - The bridge reports `server_utc_offset_confirmed` once its offset came from a price that arrived while it ran, not from a closed market's last tick.
   - An Exness price counts as current only then, and only for a whole quarter hour within ±14 hours.
   - HFM's prices are still checked against its rule, New York + 7 hours.
6. **Nothing from HFM's demo is taken for the Exness account.**
   - The spread history kept from HFM's quotes is not used as Exness's usual spread.
   - The contract table shows the Exness account's own figures, labelled as the evidence allows.
   - The PREVIEW still runs on HFM's demo contract while no live account reports, and says so.

## Consequences

- **Terminal 3 must be an MT5 that can sign in to Exness's server.** A copy of terminal 2's HFM MetaTrader 5 knows only HFM's servers until Exness is added through File → Open an Account (search "Exness"). If that search does not offer Exness's server, Exness's own MetaTrader 5 is installed as terminal 3 instead. Either way it is done on the bridge computer, from the owner's home network (B-12).
- **The owner's steps are unchanged:** one sign-in on the bridge computer's screen with the Exness **investor (read-only) password**, then one entry in Broker Connections (broker Exness, type REAL, the same investor password).
- **Portal execution stays locked** (ADR-038). An Exness trading connection would be its own reviewed change, with its own contract tests and gates; none is built.
- **Exness facts are read from MT5 once connected:** symbol names, contract size, leverage, commission, swap and the server clock. Until then the system claims none of them.
