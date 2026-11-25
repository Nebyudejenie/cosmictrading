# ADR-042: The portal's orders are tested on the owner's Exness demo, on terminal 3

- **Status:** accepted, 2026-11-25 (S119).
- **Context:** the owner asked to place trades from the dashboard on their real Exness account "now". That stays locked:
  - their own rule: "keep portal order execution disabled until the nine safety gates are independently verified";
  - AGENTS.md: no live broker adapter without its sandbox contract tests;
  - none of the nine gates has passed (ADR-038).
  The fastest honest route starts with a demo. The owner opened an Exness demo account and chose, over terminal 2 (AskUserQuestion, 2026-11-25), to sign it in on **terminal 3**, the real account's terminal.

## Decision

1. **A second bridge program on terminal 3, `exness-demo`, bound to that one demo** (bridge 15, `"only_account"`: the demo's fingerprint in the bridge computer's own `config.json`; the server cannot change it).
   - While that DEMO account is signed in, it reports and trades like the HFM demos' bridges.
   - While any other account is signed in (the real one above all), it reports nothing, refuses every command, sign-ins included, and never signs the terminal in to another account.
   - Every send to MT5 looks again at who is signed in, at that moment.
   - It has its own token (`exness_demo_token`), its own slot (`Terminal 3 · Exness demo`), adapter `exness_mt5_demo`, venue `EXNESS` and gold `CFD:XAU/USD@EXNESS` (`XAUUSDm`, Exness's own prices).
2. **The real account's bridge (`hfm-real`) is unchanged:**
   - `"read_only": true`; it refuses every trading command;
   - it has no adapter, is not in `BRIDGES`, and gets no fast lane;
   - the server hands it only sign-in and identity commands.
3. **Broker Connections** accepts an Exness DEMO (its own password), only on the Exness demo slot (migration 0040). An Exness REAL account stays read-only, never with an execution switch.
4. **Manual Trading → "Test on my Exness demo"** (`/manual/demo`) works the PDF plan's trade out on the demo, in its own prices, contract and balance. Each trade goes the owner's way:
   - proposed;
   - approved with their code;
   - sent with its stop-loss and take-profit held by Exness;
   - reconciled.
   Two owner buttons there: **"Set up my Exness demo here"**, which opens the account after its broker contract has passed on that demo, and **"Allow my Exness demo"**, a new risk policy version adding only what is missing.
5. **Nothing opens real trading:**
   - `LIVE_ADAPTERS` stays empty;
   - portal BUY/SELL for the real account stays locked behind the nine gates;
   - the demo's contract run is gate C's evidence for an Exness connection; it does not pass the gate.

## Consequences

- While the demo is signed in on terminal 3, the real account is not read: Manual Trading shows it not connected, and its Telegram alerts stop. The owner accepted this when choosing terminal 3.
- Two bridge programs attach to one MetaTrader terminal (checked on the bridge computer, 2026-11-25: the second attach left the first reporting).
- The real-account protection rests on three independent refusals: the real bridge's `read_only`, the demo bridge's binding to its own demo, and the server's never routing an order to `hfm-real`.
