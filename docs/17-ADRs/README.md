# Architecture Decision Records

| ADR | Decision | Status |
|---|---|---|
| [001](ADR-001-standalone-repository.md) | Standalone Git repository | Accepted |
| [002](ADR-002-missing-baseline.md) | Requirements only from master prompt; baseline missing | Accepted |
| [003](ADR-003-modular-monolith.md) | Modular monolith, three processes | Accepted |
| [004](ADR-004-backend-stack.md) | Python/FastAPI/SQLAlchemy Core/Alembic stack | Accepted |
| [005](ADR-005-postgres-only.md) | PostgreSQL only; queue in Postgres | Accepted |
| [006](ADR-006-server-rendered-portal.md) | Server-rendered portal | Accepted |
| [007](ADR-007-decimal-money.md) | Decimal / NUMERIC(38,18) | Accepted |
| [008](ADR-008-uuidv7.md) | UUIDv7 + human trade codes | Accepted |
| [009](ADR-009-hash-chained-audit.md) | Hash-chained append-only audit | Accepted |
| [010](ADR-010-authority-modes.md) | Authority modes and env cap | Accepted |
| [011](ADR-011-approval-binding.md) | Approval binding and consumption | Accepted |
| [012](ADR-012-execution-authority.md) | Single execution authority | Accepted |
| [013](ADR-013-deterministic-risk.md) | Deterministic risk engine | Accepted |
| [014](ADR-014-free-only-ai-gateway.md) | Free-only AI gateway | Accepted |
| [015](ADR-015-initial-market-data-sources.md) | Coinbase + Bitstamp keyless data | Accepted |
| [016](ADR-016-telegram-long-polling.md) | Telegram long polling | Accepted |
| [017](ADR-017-compose-on-proxmox-vm.md) | Compose on Ubuntu VM under Proxmox | Accepted (deploy blocked) |
| [018](ADR-018-tunnel-access-and-app-auth.md) | Tunnel + Access + app auth | Accepted (blocked) |
| [019](ADR-019-encrypted-backups.md) | Encrypted backups + drills | Accepted |
| [020](ADR-020-observability-scope.md) | Observability scope | Accepted |
| [021](ADR-021-mini-app-deferred.md) | Mini App deferred | Accepted |
| [022](ADR-022-paper-broker-simulator.md) | Independent PaperBroker | Accepted |
| [023](ADR-023-gold-through-spot-tokens.md) | Gold via spot tokens; Binance data; Exness blocked | Accepted |
| [024](ADR-024-strategy-library.md) | 100-rule library with deflated Sharpe | Accepted |
| [025](ADR-025-binance-testnet-adapter.md) | Binance Spot adapter, testnet only, unregistered | Accepted |
| [026](ADR-026-binance-testnet-accounts.md) | Test-network (SANDBOX) accounts on the Binance Spot Test Network | Accepted |
| [027](ADR-027-hfm-primary-binance-second.md) | HFM (MetaTrader 5) primary, Binance second; Coinbase and Bitstamp retired; Exness dropped | Accepted |
| [028](ADR-028-hfm-mt5-bridge.md) | The HFM link: MetaTrader 5 bridge, CFDs held like fully paid spot | Accepted |
| [029](ADR-029-environment-labels-and-venue-terms.md) | One environment label per account; approval terms that match the venue | Accepted |
| [030](ADR-030-demo-auto-trader.md) | The demo auto-trader: a bounded mandate instead of a click per trade, demo money only | Accepted |
| [031](ADR-031-rule-competition.md) | The rule competition: a rule earns the auto-trader on unseen data, then forward | Accepted |
| [032](ADR-032-gold-page.md) | The Gold page: gold trading made visible, explained and benchmarked | Accepted |
| [033](ADR-033-decision-guide.md) | A decision guide on every proposal to buy: "Should I take this trade?" | Accepted |
| [035](ADR-035-the-100-rehearsal.md) | The $100 rehearsal: simulated money treated like the real $100, and what $100 can do in gold | Accepted |
