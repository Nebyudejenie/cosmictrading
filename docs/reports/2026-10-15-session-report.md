# Session report — S12–S13: Binance, gold, the strategy library, approved exits, test-network accounts (2026-10-14 → 15)

Format: master prompt §22 (11 parts). Follows `2026-10-12-session-report.md`. Evidence: `docs/IMPLEMENTATION-STATUS.md` (S12, S13).

## 1. What was inspected

- **Owner request:** "consider Exness and Binance … gold and crypto BTC … top 100 strategies … make and apply them".
- **Binance:** reachability from the workstation and staging (B-09), its spot symbol list and its futures list (gold is offered as the PAXG/XAUT tokens spot and as a leveraged `XAUUSDT` perpetual).
- **Exness:** its official API page and contract specifications.
- **Free gold history sources:** Stooq now requires a browser proof-of-work, and FRED removed the LBMA series, so neither can be used.
- **The existing code:** the research backtester, the mission pipeline, the execution paths for sells, R17 and protection management.
- **S13:** every place that treats paper accounts specially (authority, risk, labels, fills, ledger, reconciliation, protection, exits, reviews, the bot's emergency stop), and Binance's key-registration page (Ed25519: the user registers a public key, Binance issues only an API key).

## 2. Design documents updated

- ADR-023: gold through spot tokens; Binance data; Exness blocked.
- ADR-024: the 100-rule library and deflated Sharpe.
- ADR-025: the Binance testnet adapter, unregistered.
- ADR-026: test-network (SANDBOX) accounts: own venue and prices, fees in the received asset, owner-chosen capital with the rest UNMANAGED, one account per key, dust, contract-run gate.
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
| `175f41c`, `d26370c` | Close a position from Portfolio; `cosmic broker binance-testnet-check`; testnet key pair prepared on the server; bot healthcheck |
| `e2c6d11` | Test-network market data and venue (ADR-026 stage 1) |
| (S13 commit) | SANDBOX accounts: migration 0009, worker-only keys, recorded contract runs and the gate, opening from venue balances, fees in the received asset, dust, reconciliation with UNMANAGED, Operations card; Binance trade-window fix |

## 4. Important files

- `src/cosmic/research/library/` (catalog, engine, evaluate, stats, ta, store, signals, jobs)
- `src/cosmic/trading/execution/entry.py` (`_exit_allocation`, `_authorize_exit`, `release_exits`), `trading/exits.py`
- `src/cosmic/trading/brokers/binance_spot.py`, `binance_check.py`
- `src/cosmic/sources/connectors/binance.py`
- `src/cosmic/web/routes/library.py`
- `src/cosmic/trading/sandbox.py`, `trading/brokers/binance_testnet.py`, `db/migrations/versions/0009_sandbox_accounts.py`, `tests/trading/test_sandbox.py`

## 5. Commands and tests actually run

| Command | Result |
|---|---|
| `make check` (S12 follow-up commit) | 571 passed; ruff, mypy strict and secret scan clean (live-data and sandbox tests are opt-in) |
| `make check` (S13 commit) | 589 passed, 3 deselected; ruff, mypy strict (189 files), secret scan (445 files) clean |
| Testnet key retried after the outage (`cosmic broker binance-testnet-check`) | ping PASS; `-2015` Invalid API-key, IP, or permissions; CONTRACT FAILED |
| `pytest -m live_data` (Binance and gold listings) | passed; the workstation network drops occasional connections, the server does not |
| Library on 15 years BTC / 6 years PAXG (staging) | 195 trials per scenario; 0 QUALIFIED |
| `scripts/deploy-staging.sh` | `c1cd4b5`, then `19d356a`: server checks 12/12, public readiness OK |
| `scripts/deploy-staging.sh` (S13) | `e2c6d11`, then `4812ea0`: backup first, audit chain OK (900 events), VM VERIFIED, public readiness OK |
| `cosmic broker binance-testnet-check --from-secrets` on staging | ping PASS; `-2015`; recorded as run `01a13e88…` (key fingerprint `d0f350dca9add161`), so no test-network account can open |
| `cosmic research history` / `library` on staging | 5,537 + 2,239 daily bars in 41 s; both scenarios in 34 s |

## 6. Security, cost and deployment checks

- **Exits:** cannot oversell. R17 counts other pending exits, entries' availability excludes working exit orders, and an exit is never sent if a stop filled first (all tested).
- **Exit binding:** the closed entries are part of the hashed approval packet.
- **Switching a rule on:** needs ADMINISTER_SYSTEM and a fresh code; at most 10; REJECTED rules refused; viewers refused (authorisation matrix).
- **Binance adapter:** refuses non-testnet URLs. The private key was generated on the server and never left it.
- **Test-network keys (S13):** worker-only Docker secrets; the web and bot never receive them (deploy test). Contract runs record a 16-hex fingerprint of the API key, never the key. No test-network account without a PASSED run for the worker's key. A bad key file disables only SANDBOX.
- **AI cost:** still $0.

## 7. Deployment state

| Target | State |
|---|---|
| Staging (`staging.cosmictrading.bond`) | **RUNNING** — `4812ea0` deployed and verified |
| Binance market data | **CONNECTED** (keyless) |
| Binance trading (testnet) | **BUILT** (SANDBOX accounts, ADR-026). The saved API key is refused by Binance (`-2015`), so no contract run has passed and no test-network account exists (B-07) |
| Exness | **BLOCKED** (B-06) |
| Production VM | **BLOCKED** (B-02) |
| Free AI providers | **NOT CONFIGURED** (B-04) |
| Live trading | **DISABLED** |

## 8. Real versus simulated

- **Real:** market data (delayed) and the historical daily bars.
- **Simulated:** library results are historical simulations with assumed costs. Paper fills are simulated. Test-network funds are virtual; their orders, fills and P&L are labelled SIMULATED.

## 9. Known bugs, trade-offs and limitations

**Defects fixed this session:**
- The 400-day bar limit rejected daily history on staging.
- An exit could rest with its stop cancelled (now IOC).
- Two exits could each claim an entry's full holding.
- A plain sell beside entries was allowed.
- A failed rule exit was never retried.
- (S13) The Binance adapter searched for fills in a window ending at our clock, so a fill stamped at or after that instant was missed; it now searches in venue time with a margin.
- (S13) A Telegram emergency stop would have failed to cancel test-network orders (the bot holds no keys); the worker now does it, queued in the same transaction.

**Trade-offs:**
- A library entry carries a disaster stop that is not part of the tested rule.
- Partial-weight rules propose entries and exits only.
- Gold history is short.
- The independent multi-agent review of the exit logic could not finish (usage limit). Its findings were covered by an owner-side review and tests instead.

## 10. Blockers needing external action

- **B-07:** the saved testnet API key is refused (`-2015`). On `testnet.binance.vision`, register the server's Ed25519 public key exactly, then save the API key it shows (runbook §9).
- **Owner's suspected real Secret Key:** `~/.secrets/binance-testnet-secret.txt` is unused; keep it (renamed) or delete it.
- **B-06:** Exness account type, a demo account, and access to its API documentation.
- **Jurisdiction and fee tier** for both venues (B-08).
- B-02 and B-04 are unchanged.

## 11. Next steps, ordered by risk and value

1. **Binance testnet run.** Once Binance accepts the key: run the contract check (`--from-secrets`), open the test-network account on Operations, add the instrument to the risk policy, and make one small approved round trip.
2. **Retiring a test-network account** after Binance's monthly reset (not built).
3. **Forward test.** If the owner chooses, switch on one or two WATCH rules for paper to gather out-of-sample evidence. Keltner breakout for BTC has the best timing evidence.
4. **Exness.** Write an adapter only from verified documentation, and only after a policy decision on leveraged CFDs.
5. **Production VM** (B-02), then a backup copy off the VM.
