# COSMICTRADING MetaTrader 5 bridge (HFM, ADR-028)

One small program that runs on **Windows** next to a logged-in MetaTrader 5 terminal. It connects the terminal to the portal. Outbound HTTPS only, demo accounts only, buys and protective stops on positions only.

## What it needs

- MetaTrader 5, logged in to the HFM **demo** account, with the password **saved**. Check **Tools → Options → Expert Advisors**: "Disable algorithmic trading via external Python API" must be unticked.
- Python 3.11+ for Windows, and the official package: `python -m pip install --user MetaTrader5==5.0.6231`.
- A folder on the PC containing:

  | File | Content |
  |---|---|
  | `cosmic_mt5_bridge.py` | this program |
  | `config.json` | see below |
  | `bridge_token.txt` | the same value as the server's `secrets/hfm_bridge_token` |
  | `cf_access.json` | Cloudflare Access service token `{"client_id": …, "client_secret": …}` |

`config.json`:

```json
{
  "url": "https://staging.cosmictrading.bond",
  "bridge": "hfm-demo",
  "terminal": "C:\\Program Files\\MetaTrader 5\\terminal64.exe",
  "symbols": ["XAUUSD", "#BTCUSD"],
  "token_file": "bridge_token.txt",
  "cf_access_file": "cf_access.json"
}
```

## Running

Start `start-bridge.bat`, which runs `python cosmic_mt5_bridge.py config.json` and starts it again 10 s after it stops, and keep its window open. On the owner's PC it starts at logon, from `COSMICTRADING MT5 bridge.cmd` in the Windows Startup folder (since 2026-10-18; delete that file to stop it). `run-bridge.bat` runs it once, without the restart loop. Its log is `bridge.log`, and finished commands are recorded in `journal.jsonl` (do not delete it while orders are open). When the computer or MT5 is off, the portal shows HFM as unavailable and nothing new is sent.

## Updating

Version 2 (2026-10-16) sends the stop-loss with each entry. The server refuses entries with a stop while an older bridge is connected, so update the bridge when the server is updated.

Version 3 (2026-10-22) also reports each symbol's swap mode, triple-swap day and point size, so the server can count the overnight swap in its tests (ADR-032 addendum). Nothing else changes: an older bridge keeps working, and the swap is then shown as "not reported yet".

To update:

1. Copy the new `cosmic_mt5_bridge.py` into the folder.
2. End the running bridge's `python` process; `start-bridge.bat` starts it again within 10 s with the new code.
3. Check `bridge.log`: it shows the start, and the portal's HFM prices stay fresh.

## Production

Since 2026-10-22 the bridge runs on a dedicated Windows VM on the owner's Proxmox host (ADR-027), in `C:\Users\cosmic\cosmic-bridge`, with Python 3.11.9 and `MetaTrader5==5.0.6231`. Windows signs in by itself, and the scheduled task "COSMICTRADING MT5 bridge" runs `start-bridge.bat` at that sign-in and every 5 minutes if it is not running. The owner's PC no longer runs it (docs/15 §11).

MT5 does not accept a saved login copied from another computer ("Accounts deleted due security reason"), so a new machine needs the demo password typed once in MT5. After an account change, check that Algo Trading is on.

## A second bridge (S74)

A second robot trades its own HFM demo account through a second MT5 terminal and bridge on the same computer:
- **MT5:** a portable copy of the MT5 folder, started with `/portable`, logged in to the second demo account with the password saved. Its options are the same as the first terminal's.
- **Bridge folder:** `C:\Users\cosmic\cosmic-bridge-2`, with the same files.
- **`config.json`:** `"bridge": "hfm-demo-2"` and the portable terminal's `terminal64.exe` path.
- **`bridge_token.txt`:** the same value as the server's `secrets/hfm_bridge2_token`.
- **Startup:** its own scheduled task, "COSMICTRADING MT5 bridge 2".

The bridges never share a terminal or an MT5 account. The server refuses a second bridge signed in to the first account.

## Version 7: accounts from the portal, and the read-only third terminal (S83)

- **Signing in comes from the portal:** Admin → Broker Connections. A terminal's MT5 is signed in to the account saved there, with its password, by the bridge (`LOGIN`). Nobody needs to sign in on the bridge computer, except when MT5 asks for something only a person can do.
  - An account MT5 is already signed in to is only checked (`VERIFY`), never signed in again.
  - A refused sign-in signs MT5 back in to the account it had.
  - The password is used once for `mt5.login` and is never written to `journal.jsonl` or `bridge.log`.
- **Every order names its account** (`expected_account`); MT5 signed in to any other account refuses it, sending nothing.
- **A third terminal for a REAL account, read-only:**
  - folder `C:\Users\cosmic\cosmic-bridge-3`, with its own portable MT5;
  - `config.json`: `"bridge": "hfm-real"` and `"read_only": true`;
  - `bridge_token.txt`: the server's `hfm_bridge3_token`;
  - its own scheduled task.
  It reports the account and signs in; it refuses every trading command, whatever the server sends. The portal holds the REAL account only with its investor (read-only) password.
- **Before bridge 7 is installed:** the portal can still verify the demo account a terminal is already signed in to, from that terminal's report (no command, no password). Every other test needs version 7.

## Version 8: the fast lane (S87)

The owner's rule (2026-11-12): an order leaves for HFM within one second of its prediction, or not at all.

- **Why:** the report (`/bridge/v1/sync`) carries the whole account. Measured on the owner's demo it arrives every two to three seconds, so HFM's price reached the robot that late and an order waited for the next report.
- **The fast lane** is a second request the bridge keeps open all the time, on one connection that stays up (`/bridge/v1/fast`), in its own thread:
  - up: HFM's newest prices, a few times a second, and the results of the orders it ran;
  - down: an order, the moment one waits (the server holds the request for up to a quarter of a second and answers at once when an order arrives).
  The order is sent to MT5 at once. The report then follows immediately.
- **It starts only when the server offers it** (the report's answer says so). With an older server, version 8 behaves like version 7. If the lane fails, orders go with the report as before, and the robot opens nothing until the lane is back.
- **Only orders travel on it** (SUBMIT, CANCEL), and only beside a fresh report of the demo account. Sign-ins and candles still go with the report.
- **An order's time limit (`ttl_ms`):** how long the order may still be sent, counted here from its arrival, on this computer's clock. Past it, the order is refused and nothing is sent.
- **Only one thread ever calls MT5:** the main one, as in every version. The report and the fast lane each wait on the network in their own thread and pass what arrives to it through a queue. So an order never waits for a request in flight, the account is read as of one moment (no order runs between its four readings), and only one command runs at a time.
- **The order of things:** every finished command gets a number, and every report says how many were finished when it read the account. The server reads a command's outcome only from a report that has reached its number.
- **Measured and reported:** per order, how long it waited here and how long HFM took to answer; per report, how long the snapshot and the request took.
- **`config.json`:** nothing to add. `"fast_lane": false` switches the lane off.
- **Installing it** is the same as every version: replace `cosmic_mt5_bridge.py` in the bridge folder and restart its scheduled task. Positions and orders at HFM are not touched by it; MT5 stays signed in.

