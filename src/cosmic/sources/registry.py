"""Code-defined source, venue, asset and instrument registry (reviewed in Git).

Every source carries the provenance metadata required by directive §6.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SourceDef:
    key: str
    name: str
    publisher: str
    jurisdiction: str
    language: str
    access_method: str
    base_url: str
    terms_url: str | None
    terms_status: str
    licence_note: str
    data_class: str
    latency_class: str
    update_frequency_seconds: int
    freshness_limit_seconds: int
    timezone: str
    currency_unit: str | None
    known_bias: str
    missingness_note: str
    quality_checks: tuple[str, ...]
    owner: str
    fallback_source_key: str | None
    enabled: bool = True


@dataclass(frozen=True)
class VenueDef:
    key: str
    name: str
    kind: str
    timezone: str
    session: str
    status_source_key: str | None


@dataclass(frozen=True)
class AssetDef:
    symbol: str
    name: str
    kind: str
    display_decimals: int


@dataclass(frozen=True)
class InstrumentDef:
    canonical_key: str
    pair_key: str
    venue_key: str
    base: str
    quote: str
    tradable: bool
    listings: dict[str, str] = field(default_factory=dict)  # source key → source symbol
    asset_class: str = "CRYPTO_SPOT"


_MARKET_CHECKS = (
    "ohlc_consistency",
    "positive_prices",
    "non_negative_volume",
    "timestamp_not_future",
    "bar_alignment",
    "bid_not_above_ask",
)

SOURCES: tuple[SourceDef, ...] = (
    SourceDef(
        key="coinbase_exchange_public",
        name="Coinbase Exchange public market data",
        publisher="Coinbase",
        jurisdiction="US",
        language="en",
        access_method="REST (keyless public endpoints)",
        base_url="https://api.exchange.coinbase.com",
        terms_url="https://docs.cdp.coinbase.com/exchange/introduction/welcome",
        terms_status="UNVERIFIED",
        licence_note=(
            "Public market-data endpoints used for the operator's own analysis; no redistribution. "
            "Owner to confirm the venue terms for their jurisdiction."
        ),
        data_class="MARKET_DATA",
        latency_class="DELAYED_POLLING",
        update_frequency_seconds=60,
        freshness_limit_seconds=120,
        timezone="UTC",
        currency_unit="quote currency of each product",
        known_bias="Single venue prices; may diverge from other venues during stress.",
        missingness_note="Candles are omitted for intervals without trades.",
        quality_checks=_MARKET_CHECKS,
        owner="operator",
        fallback_source_key="bitstamp_public",
    ),
    SourceDef(
        key="bitstamp_public",
        name="Bitstamp public market data",
        publisher="Bitstamp",
        jurisdiction="LU/GB",
        language="en",
        access_method="REST (keyless public endpoints)",
        base_url="https://www.bitstamp.net/api/v2",
        terms_url="https://www.bitstamp.net/api/",
        terms_status="UNVERIFIED",
        licence_note=(
            "Public data functions without authentication; documented limits 400 req/s and "
            "10,000 req per 10 minutes. Used as an independent cross-check, own use only."
        ),
        data_class="MARKET_DATA",
        latency_class="DELAYED_POLLING",
        update_frequency_seconds=60,
        freshness_limit_seconds=120,
        timezone="UTC",
        currency_unit="counter currency of each market",
        known_bias="Lower volume than the primary venue; spreads can be wider.",
        missingness_note="OHLC includes the current open candle unless excluded.",
        quality_checks=_MARKET_CHECKS,
        owner="operator",
        fallback_source_key="coinbase_exchange_public",
    ),
    SourceDef(
        key="binance_public",
        name="Binance Spot public market data",
        publisher="Binance",
        jurisdiction="global (owner to confirm availability in their jurisdiction)",
        language="en",
        access_method="REST (keyless public market-data base data-api.binance.vision)",
        base_url="https://data-api.binance.vision/api/v3",
        terms_url="https://developers.binance.com/docs/binance-spot-api-docs/rest-api",
        terms_status="UNVERIFIED",
        licence_note=(
            "Public market-data endpoints, documented limit 6000 request weight per minute per "
            "IP; this system uses about 4%. Own analysis only, no redistribution."
        ),
        data_class="MARKET_DATA",
        latency_class="DELAYED_POLLING",
        update_frequency_seconds=60,
        freshness_limit_seconds=120,
        timezone="UTC",
        currency_unit="quote asset of each symbol (USDT is a stablecoin, not US dollars)",
        known_bias=(
            "Largest spot venue by volume; USDT-quoted prices differ from USD prices by the "
            "USDT/USD rate. Gold tokens (PAXG, XAUT) trade at a premium or discount to spot gold."
        ),
        missingness_note="Klines include the still-open interval; quotes carry the server time.",
        quality_checks=_MARKET_CHECKS,
        owner="operator",
        fallback_source_key="coinbase_exchange_public",
    ),
    SourceDef(
        key="binance_status",
        name="Binance system status",
        publisher="Binance",
        jurisdiction="global",
        language="en",
        access_method="REST (keyless GET /sapi/v1/system/status)",
        base_url="https://api.binance.com/sapi/v1/system/status",
        terms_url="https://developers.binance.com/docs/wallet/others/system-status",
        terms_status="UNVERIFIED",
        licence_note="Public status endpoint; operational signal only.",
        data_class="VENUE_STATUS",
        latency_class="DELAYED_POLLING",
        update_frequency_seconds=300,
        freshness_limit_seconds=900,
        timezone="UTC",
        currency_unit=None,
        known_bias="Self-reported; distinguishes only normal operation from maintenance.",
        missingness_note="The response has no timestamp; freshness is the time it was fetched.",
        quality_checks=("indicator_known",),
        owner="operator",
        fallback_source_key=None,
    ),
    SourceDef(
        key="binance_testnet_public",
        name="Binance Spot Test Network market data",
        publisher="Binance",
        jurisdiction="global (test environment, virtual funds)",
        language="en",
        access_method="REST (keyless public endpoints of testnet.binance.vision)",
        base_url="https://testnet.binance.vision/api/v3",
        terms_url="https://github.com/binance/binance-spot-api-docs/blob/master/testnet/general-info.md",
        terms_status="UNVERIFIED",
        licence_note=(
            "The test network's own order book: the prices test-network (SANDBOX) orders meet. "
            "Virtual funds; periodically reset."
        ),
        data_class="MARKET_DATA",
        latency_class="DELAYED_POLLING",
        update_frequency_seconds=60,
        freshness_limit_seconds=120,
        timezone="UTC",
        currency_unit="quote asset of each symbol (USDT is a stablecoin, not US dollars)",
        known_bias=(
            "A test order book: prices usually track the real market (they matched on "
            "2026-10-15) but depth differs and nothing guarantees it."
        ),
        missingness_note="Quotes only; no bars are stored for the test network.",
        quality_checks=_MARKET_CHECKS,
        owner="operator",
        fallback_source_key=None,
    ),
    SourceDef(
        key="binance_testnet_status",
        name="Binance Spot Test Network reachability",
        publisher="Binance",
        jurisdiction="global (test environment)",
        language="en",
        access_method="REST (keyless GET /api/v3/ping)",
        base_url="https://testnet.binance.vision/api/v3/ping",
        terms_url="https://github.com/binance/binance-spot-api-docs/blob/master/testnet/general-info.md",
        terms_status="UNVERIFIED",
        licence_note="Reachability only; the test network has no system-status endpoint.",
        data_class="VENUE_STATUS",
        latency_class="DELAYED_POLLING",
        update_frequency_seconds=300,
        freshness_limit_seconds=900,
        timezone="UTC",
        currency_unit=None,
        known_bias="A successful ping does not prove matching works.",
        missingness_note="No timestamp; freshness is the time it was fetched.",
        quality_checks=("indicator_known",),
        owner="operator",
        fallback_source_key=None,
    ),
    SourceDef(
        key="hfm_mt5_bridge",
        name="HFM prices through the MetaTrader 5 bridge",
        publisher="HF Markets (via the owner's MetaTrader 5 terminal)",
        jurisdiction="broker (demo server HFMarketsGlobal-Demo)",
        language="en",
        access_method="pushed by the bridge (MetaTrader5 symbol_info_tick), ADR-028",
        base_url="bridge:hfm-demo",
        terms_url="https://www.mql5.com/en/docs/python_metatrader5",
        terms_status="UNVERIFIED",
        licence_note="Broker quotes for the owner's own account; not redistributed.",
        data_class="MARKET_DATA",
        latency_class="DELAYED_POLLING",
        update_frequency_seconds=5,
        freshness_limit_seconds=60,
        timezone="UTC",
        currency_unit="USD (profit currency of the CFD)",
        known_bias="CFD prices set by the broker; demo prices may differ from the real server.",
        missingness_note="Nothing arrives while the owner's computer or MT5 is off.",
        quality_checks=("positive_prices", "bid_le_ask"),
        owner="operator",
        fallback_source_key=None,
    ),
    SourceDef(
        key="hfm_mt5_status",
        name="HFM MetaTrader 5 bridge heartbeat",
        publisher="the owner's MetaTrader 5 terminal",
        jurisdiction="broker (demo server HFMarketsGlobal-Demo)",
        language="en",
        access_method="pushed by the bridge (terminal_info, account_info), ADR-028",
        base_url="bridge:hfm-demo",
        terms_url="https://www.mql5.com/en/docs/python_metatrader5",
        terms_status="UNVERIFIED",
        licence_note="Status of the owner's terminal only.",
        data_class="VENUE_STATUS",
        latency_class="DELAYED_POLLING",
        update_frequency_seconds=5,
        freshness_limit_seconds=60,
        timezone="UTC",
        currency_unit=None,
        known_bias="Shows the terminal's connection, not the broker's own status page.",
        missingness_note="No heartbeat means UNAVAILABLE: nothing new is sent to HFM.",
        quality_checks=("indicator_known",),
        owner="operator",
        fallback_source_key=None,
    ),
    SourceDef(
        key="coinbase_status",
        name="Coinbase status page",
        publisher="Coinbase (Atlassian Statuspage)",
        jurisdiction="US",
        language="en",
        access_method="REST (keyless public status API)",
        base_url="https://status.coinbase.com/api/v2",
        terms_url="https://status.coinbase.com",
        terms_status="UNVERIFIED",
        licence_note="Public status page; operational signal only.",
        data_class="VENUE_STATUS",
        latency_class="DELAYED_POLLING",
        update_frequency_seconds=300,
        freshness_limit_seconds=900,
        timezone="UTC",
        currency_unit=None,
        known_bias="Self-reported by the venue; incidents may be posted late.",
        missingness_note="Covers all Coinbase products, not only the Exchange.",
        quality_checks=("indicator_known",),
        owner="operator",
        fallback_source_key=None,
    ),
)

VENUES: tuple[VenueDef, ...] = (
    VenueDef("COINBASE", "Coinbase Exchange", "EXCHANGE", "UTC", "24x7", "coinbase_status"),
    VenueDef("BITSTAMP", "Bitstamp", "EXCHANGE", "UTC", "24x7", None),
    VenueDef("BINANCE", "Binance Spot", "EXCHANGE", "UTC", "24x7", "binance_status"),
    # Virtual funds on Binance's test matching engine (ADR-026): SIMULATED, never researched.
    VenueDef(
        "BINANCE_TESTNET",
        "Binance Spot Test Network",
        "SIMULATED",
        "UTC",
        "24x7",
        "binance_testnet_status",
    ),
    # HFM through the owner's MetaTrader 5 terminal (ADR-027/028): a broker, CFDs, demo account.
    VenueDef(
        "HFM", "HFM (HF Markets) via MetaTrader 5", "BROKER", "UTC", "broker", "hfm_mt5_status"
    ),
)

ASSETS: tuple[AssetDef, ...] = (
    AssetDef("BTC", "Bitcoin", "CRYPTO", 8),
    AssetDef("ETH", "Ether", "CRYPTO", 8),
    AssetDef("USD", "US dollar", "FIAT", 2),
    AssetDef("USDT", "Tether USD (stablecoin; not US dollars)", "CRYPTO", 8),
    AssetDef("PAXG", "PAX Gold: token backed by 1 troy oz of LBMA gold held by Paxos", "CRYPTO", 8),
    AssetDef("XAU", "Gold, troy ounce (HFM CFD: a price contract, no metal delivered)", "METAL", 2),
    AssetDef(
        "XAUT", "Tether Gold: token backed by 1 troy oz of LBMA gold held by Tether", "CRYPTO", 8
    ),
)

INSTRUMENTS: tuple[InstrumentDef, ...] = (
    InstrumentDef(
        "SPOT:BTC/USD@COINBASE",
        "BTC/USD",
        "COINBASE",
        "BTC",
        "USD",
        True,
        {"coinbase_exchange_public": "BTC-USD"},
    ),
    InstrumentDef(
        "SPOT:ETH/USD@COINBASE",
        "ETH/USD",
        "COINBASE",
        "ETH",
        "USD",
        True,
        {"coinbase_exchange_public": "ETH-USD"},
    ),
    InstrumentDef(
        "SPOT:BTC/USD@BITSTAMP",
        "BTC/USD",
        "BITSTAMP",
        "BTC",
        "USD",
        False,
        {"bitstamp_public": "btcusd"},
    ),
    InstrumentDef(
        "SPOT:ETH/USD@BITSTAMP",
        "ETH/USD",
        "BITSTAMP",
        "ETH",
        "USD",
        False,
        {"bitstamp_public": "ethusd"},
    ),
    # Gold: spot gold tokens (1 token = 1 troy oz), traded spot like crypto — no leverage.
    InstrumentDef(
        "SPOT:PAXG/USD@COINBASE",
        "PAXG/USD",
        "COINBASE",
        "PAXG",
        "USD",
        True,
        {"coinbase_exchange_public": "PAXG-USD"},
    ),
    InstrumentDef(
        "SPOT:PAXG/USD@BITSTAMP",
        "PAXG/USD",
        "BITSTAMP",
        "PAXG",
        "USD",
        False,
        {"bitstamp_public": "paxgusd"},
    ),
    # Binance: market data and research now; trading needs the sandbox adapter (B-06/B-07).
    InstrumentDef(
        "SPOT:BTC/USDT@BINANCE",
        "BTC/USDT",
        "BINANCE",
        "BTC",
        "USDT",
        True,  # paper trading on Binance prices (ADR-027); no live adapter exists
        {"binance_public": "BTCUSDT"},
    ),
    InstrumentDef(
        "SPOT:BTC/USDT@COINBASE",
        "BTC/USDT",
        "COINBASE",
        "BTC",
        "USDT",
        False,
        {"coinbase_exchange_public": "BTC-USDT"},
    ),
    InstrumentDef(
        "SPOT:PAXG/USDT@BINANCE",
        "PAXG/USDT",
        "BINANCE",
        "PAXG",
        "USDT",
        True,  # paper trading on Binance prices (ADR-027); no live adapter exists
        {"binance_public": "PAXGUSDT"},
    ),
    InstrumentDef(
        "SPOT:XAUT/USDT@BINANCE",
        "XAUT/USDT",
        "BINANCE",
        "XAUT",
        "USDT",
        False,
        {"binance_public": "XAUTUSDT"},
    ),
    # Binance Spot Test Network (SANDBOX accounts only; R02 keeps other accounts away).
    InstrumentDef(
        "SPOT:BTC/USDT@BINANCE_TESTNET",
        "BTC/USDT",
        "BINANCE_TESTNET",
        "BTC",
        "USDT",
        True,
        {"binance_testnet_public": "BTCUSDT"},
    ),
    InstrumentDef(
        "SPOT:PAXG/USDT@BINANCE_TESTNET",
        "PAXG/USDT",
        "BINANCE_TESTNET",
        "PAXG",
        "USDT",
        True,
        {"binance_testnet_public": "PAXGUSDT"},
    ),
    # HFM CFDs through the MetaTrader 5 bridge (ADR-028): quantities in the base unit (oz of gold,
    # BTC); the bridge converts to lots with the symbol's contract size. Trading needs the
    # HFM adapter in the worker, a PASSED contract run and an HFM SANDBOX account.
    InstrumentDef(
        "CFD:XAU/USD@HFM",
        "XAU/USD",
        "HFM",
        "XAU",
        "USD",
        True,
        {"hfm_mt5_bridge": "XAUUSD"},
        "CFD",
    ),
    InstrumentDef(
        "CFD:BTC/USD@HFM",
        "BTC/USD",
        "HFM",
        "BTC",
        "USD",
        True,
        {"hfm_mt5_bridge": "#BTCUSD"},
        "CFD",
    ),
)

SOURCE_BY_KEY = {s.key: s for s in SOURCES}


def source_venues(source_key: str) -> frozenset[str]:
    """Venues a source serves: those of the instruments it lists and those whose status it
    reports."""
    listed = {i.venue_key for i in INSTRUMENTS if source_key in i.listings}
    status = {v.key for v in VENUES if v.status_source_key == source_key}
    return frozenset(listed | status)


def source_retired(source_key: str, retired_venues: frozenset[str]) -> bool:
    """A source is retired when every venue it serves is retired (ADR-027)."""
    served = source_venues(source_key)
    return bool(served) and served <= retired_venues
