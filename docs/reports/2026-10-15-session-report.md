# Session report — S12: Binance, gold, the strategy library, approved exits (2026-10-14 → 15)

Format: master prompt §22 (11 parts). Follows `2026-10-12-session-report.md`. Evidence: `docs/IMPLEMENTATION-STATUS.md` (S12).

## 1. What was inspected

- **Owner request:** "consider Exness and Binance … gold and crypto BTC … top 100 strategies … make and apply them".
- **Binance:** reachability from the workstation and staging (B-09), its spot symbol list and its futures list (gold is offered as the PAXG/XAUT tokens spot and as a leveraged `XAUUSDT` perpetual).
- **Exness:** its official API page and contract specifications.
- **Free gold history sources:** Stooq now requires a browser proof-of-work, and FRED removed the LBMA series, so neither can be used.
- **The existing code:** the research backtester, the mission pipeline, the execution paths for sells, R17 and protection management.

## 2. Design documents updated

- ADR-023: gold through spot tokens; Binance data; Exness blocked.
- ADR-024: the 100-rule library and deflated Sharpe.
- ADR-025: the Binance testnet adapter, unregistered.
- `docs/integrations/binance-spot-api.md`: 80 verified facts.
- B-06, B-07, B-09 (resolved), traceability (DATA-05, QNT-08, QNT-10, TRD-22), user guide §5.11 and §12, runbook §9.

## 3. Functionality implemented

| Commit | Change |
|---|---|
| `8fb7444` | Binance market data (keyless) and gold instruments; daily bars for every listing |
| `27414d8` | Strategy library: 100 documented rules, one engine, timing Sharpe against an exposure-matched holding, deflated Sharpe ratio, tiers, look-ahead proof, append-only storage (migration 0008), owner switch, signals → proposals |
| `2a26e9c` | Approved exits: name the entries closed, cancel their stops first, IOC, never oversell, protection restored if unfilled |
| `58b38b1` | Library pages, owner switch, `cosmic research` CLI |
| `c1cd4b5` | Binance Spot Test Network adapter (unregistered) and emulator contract tests |
| `19d356a` | Bar age limit per timeframe (defect found on the first real backfill) |
| (this report's commit) | Close a position from Portfolio; `cosmic broker binance-testnet-check`; testnet key pair prepared on the server; bot healthcheck |

## 4. Important files

- `src/cosmic/research/library/` (catalog, engine, evaluate, stats, ta, store, signals, jobs)
- `src/cosmic/trading/execution/entry.py` (`_exit_allocation`, `_authorize_exit`, `release_exits`), `trading/exits.py`
- `src/cosmic/trading/brokers/binance_spot.py`, `binance_check.py`
- `src/cosmic/sources/connectors/binance.py`
- `src/cosmic/web/routes/library.py`

## 5. Commands and tests actually run

| Command | Result |
|---|---|
| `make check` (this report's commit) | 571 passed; ruff, mypy strict and secret scan clean (live-data and sandbox tests are opt-in) |
| `pytest -m live_data` (Binance and gold listings) | passed; the workstation network drops occasional connections, the server does not |
| Library on 15 years BTC / 6 years PAXG (staging) | 195 trials per scenario; 0 QUALIFIED |
| `scripts/deploy-staging.sh` | `c1cd4b5`, then `19d356a`: server checks 12/12, public readiness OK |
| `cosmic research history` / `library` on staging | 5,537 + 2,239 daily bars in 41 s; both scenarios in 34 s |

## 6. Security, cost and deployment checks

- **Exits:** cannot oversell. R17 counts other pending exits, entries' availability excludes working exit orders, and an exit is never sent if a stop filled first (all tested).
- **Exit binding:** the closed entries are part of the hashed approval packet.
- **Switching a rule on:** needs ADMINISTER_SYSTEM and a fresh code; at most 10; REJECTED rules refused; viewers refused (authorisation matrix).
- **Binance adapter:** refuses non-testnet URLs. Testnet keys pass to the check over stdin only. The private key was generated on the server and never left it.
- **AI cost:** still $0.

## 7. Deployment state

| Target | State |
|---|---|
| Staging (`staging.cosmictrading.bond`) | **RUNNING** — latest release deployed and verified |
| Binance market data | **CONNECTED** (keyless) |
| Binance trading (testnet) | **READY, NOT REGISTERED** — waiting for the owner's testnet API key (B-07) |
| Exness | **BLOCKED** (B-06) |
| Production VM | **BLOCKED** (B-02) |
| Free AI providers | **NOT CONFIGURED** (B-04) |
| Live trading | **DISABLED** |

## 8. Real versus simulated

- **Real:** market data (delayed) and the historical daily bars.
- **Simulated:** library results are historical simulations with assumed costs. Paper fills are simulated. Testnet funds would be virtual.

## 9. Known bugs, trade-offs and limitations

**Defects fixed this session:**
- The 400-day bar limit rejected daily history on staging.
- An exit could rest with its stop cancelled (now IOC).
- Two exits could each claim an entry's full holding.
- A plain sell beside entries was allowed.
- A failed rule exit was never retried.

**Trade-offs:**
- A library entry carries a disaster stop that is not part of the tested rule.
- Partial-weight rules propose entries and exits only.
- Gold history is short.
- The independent multi-agent review of the exit logic could not finish (usage limit). Its findings were covered by an owner-side review and tests instead.

## 10. Blockers needing external action

- **B-07:** register the prepared public key on `testnet.binance.vision` and save the API key on the server.
- **B-06:** Exness account type, a demo account, and access to its API documentation.
- **Jurisdiction and fee tier** for both venues (B-08).
- B-02 and B-04 are unchanged.

## 11. Next steps, ordered by risk and value

1. **Binance testnet run.** Run `cosmic broker binance-testnet-check` once the API key is saved. Then add a testnet account kind, fees charged in the received asset, and register the adapter.
2. **Forward test.** If the owner chooses, switch on one or two WATCH rules for paper to gather out-of-sample evidence. Keltner breakout for BTC has the best timing evidence.
3. **Exness.** Write an adapter only from verified documentation, and only after a policy decision on leveraged CFDs.
4. **Production VM** (B-02), then a backup copy off the VM.
