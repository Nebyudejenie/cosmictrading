"""Manual Trading (S113, the owner's specification of 2026-11-22): the owner's own workspace for
the gold trades they place by hand on their real HFM account.

- **Read-only.** The real account is read from terminal 3 (the read-only terminal, S83): its
  bridge refuses every trading command and the server sends it none. Nothing in this package
  places, changes or closes an order; the owner places each trade in MT5 themselves.
- **Never simulated as live.** An account is shown as live only when HFM's own report says REAL,
  it is the account Broker Connections verified on that terminal, and the report is fresh; any
  other case is said, and the figures that depend on a current price are not calculated.
- **Apart from the robots.** No mandate, robot or demo account is read or changed here, and a
  manual trade is never counted as a robot's (the robots trade their own demo terminals).
- **Portal execution is built locked** (``execution``): it refuses, with every lock that is
  still closed, until the nine go-live gates pass and the owner activates live trading.
"""
