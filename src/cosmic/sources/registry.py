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
)

ASSETS: tuple[AssetDef, ...] = (
    AssetDef("BTC", "Bitcoin", "CRYPTO", 8),
    AssetDef("ETH", "Ether", "CRYPTO", 8),
    AssetDef("USD", "US dollar", "FIAT", 2),
    AssetDef("USDT", "Tether USD (stablecoin; not US dollars)", "CRYPTO", 8),
    AssetDef("PAXG", "PAX Gold: token backed by 1 troy oz of LBMA gold held by Paxos", "CRYPTO", 8),
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
        False,
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
        False,
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
)

SOURCE_BY_KEY = {s.key: s for s in SOURCES}
