"""What the owner reads: numbers and times from approval packets (stored as strings)."""

from __future__ import annotations

from decimal import Decimal

from cosmic.notify.telegram import format as tg
from cosmic.web.filters import fmt_dec, fmt_ts


def test_portal_filters_read_packet_strings() -> None:
    assert fmt_dec("199.999248408", 2) == "200.00"
    assert fmt_dec("0.002419600000000000") == "0.0024196"
    assert fmt_dec(Decimal("82657.980000000000000000")) == "82657.98"
    assert fmt_dec("not a number") == "not a number"
    assert fmt_dec(None) == "—"
    assert fmt_ts("2026-10-12T16:20:39.065249Z") == "2026-10-12 16:20:39 UTC"
    assert fmt_ts("soon") == "soon"


def test_telegram_shows_money_to_the_cent_and_readable_expiry() -> None:
    packet = {
        "plain_language": {
            "action": "BUY 0.0024196 BTC at a limit of 82657.98 USD",
            "why": "w",
            "key_risk": "k",
            "expires": "2026-10-12T16:20:39.065249Z",
        },
        "account": {"alias": "paper-main", "kind": "PAPER"},
        "order": {
            "side": "BUY",
            "quantity": "0.0024196",
            "order_type": "LIMIT",
            "limit_price": "82657.98",
            "price_band": {"low": "81708.82", "high": "83359.51"},
            "max_slippage_bps": "10",
            "time_in_force": "GTC",
        },
        "instrument": {"canonical_key": "SPOT:BTC/USD@COINBASE"},
        "costs": {"currency": "USD", "estimated_fee": "1.199995490448", "fee_basis": "ASSUMPTION"},
        "risk": {"estimated_max_loss": "9.089967216896", "warning": "w"},
        "checks": [],
        "horizon": "days",
        "thesis": "t",
    }
    assert "Expires: 2026-10-12 16:20 UTC" in tg.proposal_summary("T-1", packet, "PROPOSED")
    assert "Est. fee 1.20 USD" in tg.section(packet, "details")
    assert "Estimated max loss: 9.09 USD" in tg.section(packet, "risk")
    packet["risk"]["estimated_max_loss"] = None
    assert "not bounded by an order" in tg.section(packet, "risk")


def test_every_proposal_names_its_environment() -> None:
    def summary(account: dict[str, str]) -> str:
        packet = {
            "plain_language": {"action": "a", "why": "w", "key_risk": "k", "expires": "x"},
            "account": account,
        }
        return tg.proposal_summary("T-1", packet, "PROPOSED").splitlines()[0]

    hfm = summary({"alias": "hfm-demo", "kind": "SANDBOX", "adapter": "hfm_mt5_demo"})
    assert hfm == "T-1 — PROPOSED [SIMULATED · DEMO — HFM MetaTrader 5 demo account, virtual funds]"
    testnet = summary({"alias": "t", "kind": "SANDBOX", "adapter": "binance_spot_testnet"})
    assert "[SIMULATED · TESTNET — Binance Spot Test Network, virtual funds]" in testnet
    assert "Binance" not in hfm and "HFM" not in testnet
    # Packets stored before the adapter was recorded still say what they are.
    assert "[SIMULATED · PAPER — paper simulator, simulated money]" in summary(
        {"alias": "paper-main", "kind": "PAPER"}
    )
    assert summary({"alias": "x", "kind": "LIVE", "adapter": "y"}).endswith(
        "[LIVE — live venue (y), real money]"
    )
