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


def test_the_card_carries_every_approved_term() -> None:
    packet = {
        "plain_language": {
            "action": "BUY 1 XAU at a limit of 4101.00 USD",
            "why": "w",
            "key_risk": "k",
            "expires": "2026-10-16T08:31:00+00:00",
        },
        "account": {"alias": "hfm-demo", "kind": "SANDBOX", "adapter": "hfm_mt5_demo"},
        "instrument": {"canonical_key": "CFD:XAU/USD@HFM", "venue": "HFM", "quote": "USD"},
        "order": {
            "side": "BUY",
            "quantity": "1",
            "order_type": "LIMIT",
            "limit_price": "4101.00",
            "price_band": {"low": "4090.00", "high": "4110.00"},
            "max_slippage_bps": "10",
            "time_in_force": "GTC",
            "max_notional": "4101.00",
        },
        "market_at_proposal": {
            "bid": "4100.00",
            "ask": "4100.30",
            "quote_ts": "2026-10-16T08:01:00+00:00",
            "label": "DELAYED",
        },
        "management_plan": {
            "stop_loss": {"stop_price": "4000.00", "limit_price": "3990.00", "type": "t"},
            "take_profit": None,
        },
        "costs": {"currency": "USD", "estimated_fee": "2.4606", "fee_basis": "ASSUMPTION"},
        "risk": {"estimated_max_loss": "115.92", "estimated_max_loss_pct_of_equity": "0.5796"},
        "strategy": {"key": None, "name": "manual proposal by the operator", "version": None},
        "checks": [{"code": "R01", "status": "PASS"}, {"code": "R10", "status": "UNKNOWN"}],
    }
    card = tg.proposal_summary("T-1", packet, "PROPOSED").splitlines()
    for line in (
        "Instrument: CFD:XAU/USD@HFM (venue HFM)",
        "Order: LIMIT 4101.00 USD, GTC · band 4090.00–4110.00 · slippage ≤ 10 bps",
        "Quote: bid 4100.00 / ask 4100.30 at 2026-10-16 08:01 UTC (DELAYED)",
        "Stop-loss: 4000.00 · take-profit: none",
        "Notional ≤ 4,101.00 USD · est. loss at stop 115.92 USD (0.58% of equity)"
        " · est. fee 2.46 USD (assumed)",
        "Strategy: manual proposal by the operator",
        "Risk checks: 1/2 passed — not passed: R10",
    ):
        assert line in card, line
    assert card[-1] == "Expires: 2026-10-16 08:31 UTC"
    packet["strategy"] = {"key": "donchian-55", "name": "Donchian 55", "version": 3}
    assert "Strategy: donchian-55 v3" in tg.proposal_summary("T-1", packet, "PROPOSED")
