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

Start `run-bridge.bat` (`python cosmic_mt5_bridge.py`) and keep its window open. Its log is `bridge.log`, and finished commands are recorded in `journal.jsonl` (do not delete it while orders are open). When the computer or MT5 is off, the portal shows HFM as unavailable and nothing new is sent.

## Updating

Version 2 (2026-10-16) sends the stop-loss with each entry. The server refuses entries with a stop while an older bridge is connected, so update the bridge when the server is updated:

1. Copy the new `cosmic_mt5_bridge.py` into the folder.
2. End the running bridge's `python` process; `start-bridge.bat` starts it again within 10 s with the new code.
3. Check `bridge.log`: it shows the start, and the portal's HFM prices stay fresh.

## Production

The same folder goes on a dedicated Windows VM on Proxmox (ADR-027), started at logon by Task Scheduler. It gets its own Cloudflare Access service token and bridge token.
