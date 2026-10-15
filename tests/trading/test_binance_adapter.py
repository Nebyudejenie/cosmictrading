"""Binance Spot (Spot Test Network) adapter against the broker contract.

Runs against ``tests/binance_emulator.py``, which follows the official documentation (reviewed
2026-10-14). The same contract must still pass against the real Spot Test Network with the
owner's testnet keys before the adapter may be registered (B-07):
``tests/sandbox/test_binance_testnet.py`` (opt-in).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from cosmic.core.clock import FrozenClock
from cosmic.trading.brokers.base import (
    BrokerAmbiguousError,
    BrokerOrderNotFoundError,
    BrokerUnavailableError,
    SubmitRequest,
    VenueOrderStatus,
)
from cosmic.trading.brokers.binance_spot import (
    BinanceSpotAdapter,
    Ed25519Signer,
    HmacSigner,
)
from tests.binance_emulator import BinanceEmulator

D = Decimal
ACCOUNT = uuid.uuid4()
BTC = "SPOT:BTC/USDT@BINANCE"
SYMBOLS = {BTC: "BTCUSDT", "SPOT:PAXG/USDT@BINANCE": "PAXGUSDT"}


def adapter(
    emu: BinanceEmulator, *, signer: object | None = None, **kw: object
) -> BinanceSpotAdapter:
    clock = FrozenClock(datetime.fromtimestamp(emu.server_ms / 1000, tz=UTC))
    return BinanceSpotAdapter(
        httpx.AsyncClient(transport=emu.transport()),
        api_key=emu.api_key,
        signer=signer or HmacSigner(emu.hmac_secret or ""),  # type: ignore[arg-type]
        symbols=SYMBOLS,
        clock=clock,
        **kw,  # type: ignore[arg-type]
    )


def req(coid: str, **kw: object) -> SubmitRequest:
    base: dict[str, object] = {
        "client_order_id": coid,
        "instrument_key": BTC,
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": D("0.001"),
        "limit_price": D("82000.00"),
        "stop_price": None,
        "time_in_force": "GTC",
        "expires_at": None,
    }
    base.update(kw)
    return SubmitRequest(**base)  # type: ignore[arg-type]


def test_hmac_signature_matches_the_official_example() -> None:
    """Known answer from the official rest-api.md (HMAC keys section)."""
    signer = HmacSigner("NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j")
    payload = (
        "symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1"
        "&recvWindow=5000&timestamp=1499827319559"
    )
    assert (
        signer.sign(payload) == "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"
    )


def test_only_the_spot_test_network_is_allowed() -> None:
    with pytest.raises(ValueError, match="Spot Test Network"):
        adapter(BinanceEmulator(), base_url="https://api.binance.com/api")
    with pytest.raises(ValueError, match="recvWindow"):
        adapter(BinanceEmulator(), recv_window_ms=60_001)


def test_capabilities_declared() -> None:
    caps = adapter(BinanceEmulator()).capabilities()
    assert not caps.is_live and caps.is_simulated and not caps.bracket_or_oco
    assert caps.idempotent_client_ids and caps.partial_fills
    assert caps.order_types == ("LIMIT", "STOP_LIMIT")


async def test_submit_is_idempotent_by_client_order_id() -> None:
    emu = BinanceEmulator()
    a = adapter(emu)
    r1 = await a.submit_order(ACCOUNT, req("ct-T1-E"))
    r2 = await a.submit_order(ACCOUNT, req("ct-T1-E"))
    assert r1.accepted and r2.accepted and r1.venue_order_id == r2.venue_order_id
    assert len(await a.list_open_orders(ACCOUNT)) == 1
    # Still the same order after it has filled (Binance itself would accept a re-used ID then).
    emu.move("BTCUSDT", "81990.00", "81995.00")
    r3 = await a.submit_order(ACCOUNT, req("ct-T1-E"))
    assert r3.accepted and r3.venue_order_id == r1.venue_order_id
    assert sum(1 for line in emu.requests if line == "POST /v3/order") == 1


async def test_rejections_are_results_not_errors() -> None:
    a = adapter(BinanceEmulator())
    lot = await a.submit_order(ACCOUNT, req("c2", quantity=D("0.0000001")))
    assert not lot.accepted and "LOT_SIZE" in (lot.reject_reason or "")
    funds = await a.submit_order(ACCOUNT, req("c3", quantity=D("1")))
    assert not funds.accepted and "insufficient balance" in (funds.reject_reason or "")
    market = await a.submit_order(ACCOUNT, req("c4", order_type="MARKET"))
    assert not market.accepted and "not supported" in (market.reject_reason or "")
    gtd = await a.submit_order(ACCOUNT, req("c5", time_in_force="GTD"))
    assert not gtd.accepted
    unknown = await a.submit_order(ACCOUNT, req("c6", instrument_key="SPOT:ETH/USD@COINBASE"))
    assert not unknown.accepted and "not traded on Binance" in (unknown.reject_reason or "")


async def test_get_order_not_found_is_explicit() -> None:
    with pytest.raises(BrokerOrderNotFoundError):
        await adapter(BinanceEmulator()).get_order(ACCOUNT, "never-sent")


async def test_cancel_and_balances() -> None:
    a = adapter(BinanceEmulator())
    await a.submit_order(ACCOUNT, req("c7", limit_price=D("80000.00")))
    usdt = {b.asset: b for b in await a.get_balances(ACCOUNT)}["USDT"]
    assert usdt.available < usdt.total  # funds on hold
    await a.cancel_order(ACCOUNT, "c7")
    assert (await a.get_order(ACCOUNT, "c7")).status is VenueOrderStatus.CANCELLED
    usdt = {b.asset: b for b in await a.get_balances(ACCOUNT)}["USDT"]
    assert usdt.available == usdt.total
    await a.cancel_order(ACCOUNT, "c7")  # cancelling a finished order is a no-op
    with pytest.raises(BrokerOrderNotFoundError):
        await a.cancel_order(ACCOUNT, "never-sent")


async def test_fills_are_reported_with_fee_and_client_id() -> None:
    emu = BinanceEmulator()
    a = adapter(emu)
    r = await a.submit_order(ACCOUNT, req("c8", limit_price=D("83100.00")))  # marketable
    assert r.accepted
    o = await a.get_order(ACCOUNT, "c8")
    assert o.status is VenueOrderStatus.FILLED and o.filled_qty == D("0.001")
    fills = await a.get_fills(ACCOUNT, since=None)
    assert [f.client_order_id for f in fills] == ["c8"]
    assert fills[0].price == D("83000.00") and fills[0].liquidity == "TAKER"
    assert fills[0].fee > 0 and fills[0].fee_asset == "BTC"  # received asset
    recent = await a.get_fills(
        ACCOUNT, since=datetime.fromtimestamp(emu.server_ms / 1000 - 60, tz=UTC)
    )
    assert [f.venue_fill_id for f in recent] == [f.venue_fill_id for f in fills]


async def test_stop_limit_rests_then_triggers() -> None:
    emu = BinanceEmulator()
    a = adapter(emu)
    stop = req(
        "c9-S1",
        side="SELL",
        order_type="STOP_LIMIT",
        quantity=D("0.5"),
        limit_price=D("80000.00"),
        stop_price=D("80100.00"),
    )
    assert (await a.submit_order(ACCOUNT, stop)).accepted
    assert (await a.get_order(ACCOUNT, "c9-S1")).status is VenueOrderStatus.OPEN
    emu.move("BTCUSDT", "80050.00", "80060.00")  # through the stop, above its limit
    filled = await a.get_order(ACCOUNT, "c9-S1")
    assert filled.status is VenueOrderStatus.FILLED
    late = req(
        "c10-S1",
        side="SELL",
        order_type="STOP_LIMIT",
        quantity=D("0.1"),
        limit_price=D("80000.00"),
        stop_price=D("81000.00"),
    )  # above the market
    r = await a.submit_order(ACCOUNT, late)
    assert not r.accepted and "trigger immediately" in (r.reject_reason or "")


@pytest.mark.parametrize("fault", ["5xx", "timeout", "connect", "429"])
async def test_a_failed_lookup_means_nothing_was_sent(fault: str) -> None:
    emu = BinanceEmulator()
    a = adapter(emu)
    await a.venue_status()
    await a.get_balances(ACCOUNT)  # synchronised with server time
    emu.faults = [fault]  # hits the idempotency lookup that precedes every submit
    with pytest.raises(BrokerUnavailableError):
        await a.submit_order(ACCOUNT, req("c11"))
    assert "POST /v3/order" not in emu.requests


@pytest.mark.parametrize(
    ("fault", "error"),
    [
        ("5xx", BrokerAmbiguousError),  # documented: execution status UNKNOWN
        ("timeout", BrokerAmbiguousError),
        ("connect", BrokerUnavailableError),  # never reached the venue
        ("429", BrokerUnavailableError),  # refused by the rate limiter
    ],
)
async def test_faults_on_the_order_request(fault: str, error: type[Exception]) -> None:
    emu = BinanceEmulator()

    def on_post(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            emu.faults = [fault]
        return emu.handle(request)

    a = adapter(emu)
    a.client = httpx.AsyncClient(transport=httpx.MockTransport(on_post))
    with pytest.raises(error):
        await a.submit_order(ACCOUNT, req("c12"))


async def test_clock_and_signature_problems_mean_not_accepted() -> None:
    emu = BinanceEmulator()
    a = adapter(emu)
    await a.venue_status()
    emu.server_ms += 60_000  # our clock now lags: requests fall outside recvWindow
    a._offset_ms = 0  # pretend we never synchronised
    with pytest.raises(BrokerUnavailableError, match="-1021"):
        await a.get_balances(ACCOUNT)
    await a.get_balances(ACCOUNT)  # resynchronised from server time, then fine
    wrong = adapter(emu, signer=HmacSigner("not-the-secret"))
    with pytest.raises(BrokerUnavailableError, match="-1022"):
        await wrong.get_balances(ACCOUNT)


async def test_ed25519_keys_sign_and_verify() -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
    )

    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    pub = key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    emu = BinanceEmulator(hmac_secret=None, ed25519_public_pem=pub)
    a = adapter(emu, signer=Ed25519Signer(pem))
    assert (await a.submit_order(ACCOUNT, req("c13"))).accepted
    with pytest.raises(ValueError, match="Ed25519"):
        from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key

        rsa = generate_private_key(public_exponent=65537, key_size=2048)
        Ed25519Signer(rsa.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))


async def test_the_testnet_contract_check_passes_against_the_emulator() -> None:
    """The steps `cosmic broker binance-testnet-check` runs on the real Spot Test Network."""
    from cosmic.trading.brokers.binance_check import run_contract

    emu = BinanceEmulator()
    steps = await run_contract(adapter(emu))
    assert [s.name for s in steps if not s.passed] == []
    assert len(steps) == 8
    assert emu.requests.count("POST /v3/order") == 1  # the re-submit was answered by lookup
