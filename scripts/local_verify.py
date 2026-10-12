"""Drive the local production-like stack through the real portal (docs/12 §7).

Talks HTTP to the relay on 127.0.0.1 exactly as a browser would (forms, CSRF tokens, TOTP),
and uses `docker` only to kill/restart containers and to read invariants from PostgreSQL.
Results are printed as PASS/FAIL lines and written as JSON; nothing secret is printed.

Usage: uv run python scripts/local_verify.py --phase portal|chaos|restart [--iterations N]
"""

from __future__ import annotations

import argparse
import html
import json
import random
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from pathlib import Path
from typing import Any

import httpx
import pyotp

ROOT = Path(__file__).resolve().parents[1]
VERIFY = ROOT / "var" / "verify"
STATE = VERIFY / "driver-state.json"  # owner TOTP secret for later phases (gitignored, 0600)
PROJECT = "cosmictrading-verify"
BTC, ETH = "SPOT:BTC/USD@COINBASE", "SPOT:ETH/USD@COINBASE"
NON_TERMINAL_TRANSMISSION = ("AUTHORIZED", "SUBMITTING", "SUBMITTED", "UNKNOWN_ORDER_STATE")


@dataclass
class Results:
    rows: list[dict[str, Any]] = field(default_factory=list)

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append({"check": name, "result": "PASS" if ok else "FAIL", "detail": detail})
        print(
            f"{'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""), flush=True
        )
        return ok

    @property
    def failed(self) -> int:
        return sum(1 for r in self.rows if r["result"] == "FAIL")


class Portal:
    """Minimal browser: manual cookie jar (Secure cookies over the 127.0.0.1 relay)."""

    def __init__(self, base: str) -> None:
        self.http = httpx.Client(base_url=base, follow_redirects=False, timeout=30)
        self.cookies: dict[str, str] = {}
        self.totp: pyotp.TOTP | None = None
        self.last_step = -1

    def request(self, method: str, path: str, data: dict[str, str] | None = None) -> httpx.Response:
        headers = {"Cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items())}
        r = self.http.request(method, path, data=data, headers=headers)
        for raw in r.headers.get_list("set-cookie"):
            pair, _, attrs = raw.partition(";")
            name, _, value = pair.strip().partition("=")
            if "max-age=0" in attrs.lower() or not value:
                self.cookies.pop(name, None)
            else:
                self.cookies[name] = value
        return r

    def get(self, path: str) -> httpx.Response:
        return self.request("GET", path)

    def post(self, path: str, data: dict[str, str]) -> httpx.Response:
        return self.request("POST", path, data)

    @staticmethod
    def csrf(page: str) -> str:
        m = re.search(r'name="csrf_token" value="([^"]+)"', page)
        if m is None:
            raise RuntimeError("no CSRF token on page")
        return html.unescape(m.group(1))

    def form_post(self, page_path: str, action: str, fields: dict[str, str]) -> httpx.Response:
        token = self.csrf(self.get(page_path).text)
        return self.post(action, {"csrf_token": token, **fields})

    def code(self) -> str:
        """A TOTP code from a time step not used before (codes are single-use, also across
        driver runs — the last step is persisted)."""
        assert self.totp is not None
        used = max(self.last_step, load_state().get("last_step", -1) if STATE.exists() else -1)
        while (step := int(time.time() // 30)) <= used:
            time.sleep(1)
        self.last_step = step
        if STATE.exists():
            save_state({**load_state(), "last_step": step})
        return self.totp.now()


def docker(*args: str, check: bool = True) -> str:
    out = subprocess.run(["docker", *args], capture_output=True, text=True, check=False)
    if check and out.returncode != 0:
        raise RuntimeError(f"docker {args[0]} failed: {out.stderr.strip()[:300]}")
    return out.stdout.strip()


def sql(query: str) -> list[list[str]]:
    out = docker(
        "exec",
        f"{PROJECT}-postgres-1",
        "psql",
        "-U",
        "postgres",
        "-d",
        "cosmic",
        "-XAt",
        "-F",
        "\t",
        "-c",
        query,
    )
    return [line.split("\t") for line in out.splitlines() if line]


def wait_healthy(service: str, timeout: int = 180) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = docker(
            "inspect", "-f", "{{.State.Health.Status}}", f"{PROJECT}-{service}-1", check=False
        )
        if status == "healthy":
            return True
        time.sleep(2)
    return False


def quote(p: Portal, key: str) -> tuple[Decimal, Decimal]:
    page = p.get(f"/market/{key}").text
    m = re.search(r"Bid / Ask</th><td>([\d,.]+) / ([\d,.]+)", page)
    if m is None:
        raise RuntimeError(f"no quote shown for {key}")
    return Decimal(m.group(1).replace(",", "")), Decimal(m.group(2).replace(",", ""))


def wait_fresh_quote(p: Portal, key: str, timeout: int = 150) -> tuple[Decimal, Decimal]:
    deadline = time.time() + timeout
    while True:
        page = p.get(f"/market/{key}").text
        if "DELAYED" in page and "Bid / Ask" in page:
            return quote(p, key)
        if time.time() > deadline:
            raise RuntimeError(f"no fresh quote for {key}")
        time.sleep(5)


def propose(p: Portal, key: str, notional: Decimal, tag: str) -> tuple[str, httpx.Response]:
    bid, ask = wait_fresh_quote(p, key)
    step = Decimal("0.01")
    limit = (ask * Decimal("1.0015")).quantize(step, ROUND_UP)  # marketable: fills on next tick
    qty = (notional / limit).quantize(Decimal("0.00000001"), ROUND_DOWN)
    fields = {
        "account_alias": "paper-main",
        "instrument_key": key,
        "quantity": str(qty),
        "limit_price": str(limit),
        "price_band_low": str((bid * Decimal("0.996")).quantize(step)),
        "price_band_high": str((ask * Decimal("1.004")).quantize(step)),
        "max_slippage_bps": "10",
        "time_in_force": "GTC",
        "stop_price": str((bid * Decimal("0.97")).quantize(step)),
        "stop_limit_price": str((bid * Decimal("0.968")).quantize(step)),
        "take_profit_price": "",
        "cancel_unfilled_after_seconds": "",
        "horizon": "days",
        "plain_summary": f"Local verification paper trade ({tag}).",
        "thesis": "Exercise the approval and execution path end to end with a tiny position.",
        "counterargument": "This is a verification trade with no market view.",
        "red_team": "Fills are simulated; nothing here says anything about edge.",
        "scenario": "Price moves either way; the stop limits the loss.",
        "invalidation": "Any verification failure.",
        "entry_conditions": "Marketable limit at the current ask plus 15 bps.",
    }
    r = p.form_post("/proposals/new", "/proposals", fields)
    m = re.search(r"/approvals/([0-9a-f-]{36})", r.headers.get("location", ""))
    if r.status_code != 303 or m is None:
        raise RuntimeError(f"proposal not created: HTTP {r.status_code} {r.text[:300]}")
    return m.group(1), r


def approve(p: Portal, intent_id: str) -> httpx.Response:
    page = p.get(f"/approvals/{intent_id}")
    token = p.csrf(page.text)
    confirm = p.post(f"/approvals/{intent_id}/review", {"csrf_token": token})
    body = confirm.text
    ch = re.search(r'name="challenge" value="([^"]+)"', body)
    ph = re.search(r'name="packet_hash" value="([^"]+)"', body)
    if confirm.status_code != 200 or ch is None or ph is None:
        raise RuntimeError(f"review failed: HTTP {confirm.status_code}")
    fields = {
        "csrf_token": p.csrf(body),
        "challenge": html.unescape(ch.group(1)),
        "packet_hash": ph.group(1),
        "decision": "APPROVE",
        "note": "local verification",
    }
    if 'name="code"' in body:
        fields["code"] = p.code()
    return p.post(f"/approvals/{intent_id}/decide", fields)


def alert_text(page: str) -> str:
    m = re.search(r'role="alert"[^>]*>(.*?)</', page, re.S)
    return html.unescape(re.sub(r"\s+", " ", m.group(1))).strip()[:200] if m else ""


def intent_status(intent_id: str) -> str:
    rows = sql(f"SELECT status FROM trade_intents WHERE id = '{intent_id}'")
    return rows[0][0] if rows else "MISSING"


def entry_order(intent_id: str) -> list[str] | None:
    rows = sql(
        f"SELECT id, status, client_order_id FROM orders WHERE intent_id = '{intent_id}' "
        "AND purpose = 'ENTRY'"
    )
    return rows[0] if rows else None


def reconcile(p: Portal, res: Results | None = None, label: str = "reconciliation OK") -> bool:
    """Request reconciliation and wait until a run that finished after the request is OK."""
    since = sql("SELECT now()")[0][0]
    p.form_post("/portfolio", "/portfolio/paper-main/reconcile", {})
    rec = wait_for(
        lambda: sql(
            "SELECT reconciliation_status FROM accounts WHERE alias = 'paper-main' "
            f"AND last_reconciled_at >= '{since}'::timestamptz"
        ),
        lambda r: r == [["OK"]],
        120,
    )
    if res is not None:
        return res.check(label, rec == [["OK"]], str(rec))
    return rec == [["OK"]]


def wait_for(fn: Any, predicate: Any, timeout: int, every: float = 2) -> Any:
    deadline = time.time() + timeout
    value = fn()
    while not predicate(value) and time.time() < deadline:
        time.sleep(every)
        value = fn()
    return value


def login(p: Portal, res: Results, username: str, password: str, secret: str | None) -> str:
    page = p.get("/login")
    bad = p.post(
        "/login",
        {"csrf_token": p.csrf(page.text), "username": username, "password": password + "-wrong"},
    )
    res.check(
        "wrong password refused",
        bad.status_code in (200, 401) and "location" not in bad.headers,
        f"HTTP {bad.status_code}",
    )
    page = p.get("/login")
    r = p.post(
        "/login", {"csrf_token": p.csrf(page.text), "username": username, "password": password}
    )
    target = r.headers.get("location", "")
    res.check(
        "password accepted, MFA required next",
        r.status_code == 303 and target in ("/mfa", "/mfa/enroll"),
        target,
    )
    if target == "/mfa/enroll":
        enroll = p.get("/mfa/enroll").text
        m = re.search(r'aria-label="Authenticator key"><code>([A-Z2-7 ]+)</code>', enroll)
        if m is None:
            raise RuntimeError("enrolment page shows no key")
        secret = m.group(1).replace(" ", "")
        p.totp = pyotp.TOTP(secret)
        done = p.post("/mfa/enroll", {"csrf_token": p.csrf(enroll), "code": p.code()})
        res.check(
            "TOTP enrolment completes", done.status_code == 303, done.headers.get("location", "")
        )
    else:
        assert secret is not None
        p.totp = pyotp.TOTP(secret)
        page = p.get("/mfa")
        done = p.post("/mfa", {"csrf_token": p.csrf(page.text), "next": "/", "code": p.code()})
        res.check("TOTP verification completes", done.status_code == 303)
    assert secret is not None
    return secret


def save_state(data: dict[str, Any]) -> None:
    STATE.touch(mode=0o600, exist_ok=True)
    STATE.write_text(json.dumps(data), encoding="utf-8")


def load_state() -> dict[str, Any]:
    return json.loads(STATE.read_text(encoding="utf-8"))


def signed_in(base: str, res: Results) -> Portal:
    st = load_state()
    p = Portal(base)
    st["secret"] = login(p, res, st["username"], st["password"], st["secret"])
    save_state({**load_state(), "secret": st["secret"]})
    return p


def phase_portal(base: str, res: Results) -> None:
    p = Portal(base)
    r = p.get("/readyz")
    res.check("readyz through the relay", r.status_code == 200, r.text[:80])
    r = p.get("/login")
    h = {k.lower(): v for k, v in r.headers.items()}
    res.check(
        "security headers on /login",
        all(
            d in h.get("content-security-policy", "")
            for d in (
                "default-src 'self'",
                "script-src 'self';",
                "frame-ancestors 'none'",
                "object-src 'none'",
            )
        )
        and "unsafe-inline" not in h.get("content-security-policy", "")
        and h.get("x-content-type-options") == "nosniff"
        and h.get("x-frame-options") == "DENY"
        and h.get("referrer-policy") == "same-origin",
        "CSP self-only, no inline script, frame-ancestors none, nosniff, DENY, same-origin",
    )
    res.check(
        "/internal/metrics hidden without token", p.get("/internal/metrics").status_code == 404
    )
    res.check(
        "unknown Host header refused",
        p.http.get("/readyz", headers={"Host": "evil.example"}).status_code == 400,
    )
    password = (VERIFY / "owner-password").read_text().strip()
    saved = load_state().get("secret") if STATE.exists() else None
    secret = login(p, res, "owner", password, saved)
    save_state(
        {
            **(load_state() if STATE.exists() else {}),
            "username": "owner",
            "password": password,
            "secret": secret,
            "last_step": p.last_step,
        }
    )
    home = p.get("/")
    res.check("command center renders", home.status_code == 200 and "RESEARCH_ONLY" in home.text)

    r = p.form_post("/portfolio", "/ops/paper-account", {"alias": "paper-main", "capital": "10000"})
    res.check(
        "paper account created (10,000 USD simulated)",
        r.status_code == 303,
        f"HTTP {r.status_code}",
    )
    r = p.form_post(
        "/ops", "/ops/mode", {"mode": "PAPER_TRADING", "reason": "local verification of paper flow"}
    )
    res.check(
        "authority mode → PAPER_TRADING",
        r.status_code == 303 and "PAPER_TRADING" in p.get("/ops").text,
    )
    reconcile(p, res, "reconciliation completes with status OK")

    intent_id, _ = propose(p, BTC, Decimal(150), "portal flow")
    status = intent_status(intent_id)
    res.check("proposal passes all pre-approval risk checks", status == "PROPOSED", status)
    if status != "PROPOSED":
        detail = sql(f"SELECT status_reason FROM trade_intents WHERE id = '{intent_id}'")
        print("      reason:", detail)
        return
    d = approve(p, intent_id)
    res.check(
        "approval decided with bound challenge", d.status_code == 303, f"HTTP {d.status_code}"
    )
    order = wait_for(
        lambda: entry_order(intent_id), lambda o: o is not None and o[1] == "FILLED", 90
    )
    res.check(
        "approved order executed and FILLED by the paper venue",
        order is not None and order[1] == "FILLED",
        "status " + (order[1] if order else "none"),
    )
    stops = sql(f"SELECT status FROM orders WHERE intent_id = '{intent_id}' AND purpose <> 'ENTRY'")
    res.check(
        "protective stop placed after the fill",
        any(s[0] in ("ACKNOWLEDGED", "SUBMITTED") for s in stops),
        str(stops),
    )
    dup = p.form_post(f"/approvals/{intent_id}", f"/approvals/{intent_id}/review", {})
    res.check(
        "consumed approval cannot be re-used",
        dup.status_code in (400, 403, 409),
        f"HTTP {dup.status_code}",
    )

    r = p.form_post(
        "/ops", "/ops/emergency-stop", {"reason": "local verification of the kill switch"}
    )
    res.check(
        "kill switch → EMERGENCY_STOP",
        r.status_code == 303 and "EMERGENCY_STOP" in p.get("/ops").text,
    )
    stops_after = sql(
        f"SELECT status FROM orders WHERE intent_id = '{intent_id}' AND purpose <> 'ENTRY'"
    )
    res.check("protective stop kept by the kill switch", stops_after == stops, str(stops_after))
    # A complete, otherwise valid proposal must be refused by R01 while stopped.
    blocked_id, _ = propose(p, ETH, Decimal(100), "during emergency stop")
    why = sql(f"SELECT status, status_reason FROM trade_intents WHERE id = '{blocked_id}'")
    res.check(
        "complete proposal refused while stopped (R01)",
        bool(why) and why[0][0] == "NON_EXECUTABLE" and "R01" in why[0][1],
        str(why),
    )
    # The stop queues its own reconciliation; the recovery checklist needs it to pass first.
    seqs = wait_for(
        lambda: sql(
            "SELECT (SELECT max(seq) FROM audit_events WHERE action = 'killswitch.activated'), "
            "(SELECT max(seq) FROM audit_events WHERE action = 'reconciliation.passed' "
            "AND payload->>'trigger' = 'emergency_stop')"
        ),
        lambda r: bool(r) and len(r[0]) == 2 and r[0][1] != "",
        60,
    )
    stop_seq, rec_seq = (seqs[0] + ["", ""])[:2] if seqs else ("", "")
    res.check(
        "the stop triggered a reconciliation that passed",
        rec_seq != "" and int(rec_seq) > int(stop_seq),
        f"audit seq stop={stop_seq} reconciliation={rec_seq}",
    )
    r = p.form_post(
        "/ops",
        "/ops/resume",
        {
            "mode": "PAPER_TRADING",
            "positions_reviewed": "yes",
            "reason": "verified positions and stops after the drill",
        },
    )
    res.check(
        "resume with recovery checklist → PAPER_TRADING",
        r.status_code == 303 and "PAPER_TRADING" in p.get("/ops").text,
        f"HTTP {r.status_code} {alert_text(r.text)}",
    )
    order = sql(
        "SELECT action FROM audit_events WHERE action IN ('killswitch.activated', "
        "'killswitch.resumed') OR (action = 'reconciliation.passed' "
        "AND payload->>'trigger' = 'emergency_stop') ORDER BY seq"
    )
    res.check(
        "audit order: stop → stop-triggered reconciliation → resume",
        [a[0] for a in order][-3:]
        == ["killswitch.activated", "reconciliation.passed", "killswitch.resumed"],
        " → ".join(a[0] for a in order),
    )


def phase_chaos(base: str, res: Results, iterations: int, seed: int) -> None:
    """AT-20 at container level: SIGKILL the worker at random moments after approval."""
    rng = random.Random(seed)
    p = signed_in(base, res)
    reconcile(p, res, "reconciliation OK before the chaos run")
    kills: list[dict[str, Any]] = []
    for i in range(iterations):
        key = (ETH, BTC)[i % 2]
        intent_id, _ = propose(p, key, Decimal(100), f"chaos {i + 1}")
        if intent_status(intent_id) != "PROPOSED":
            res.check(
                f"chaos {i + 1}: proposal executable",
                False,
                str(sql(f"SELECT status_reason FROM trade_intents WHERE id = '{intent_id}'")),
            )
            continue
        approve(p, intent_id)
        delay = round(rng.uniform(0, 6), 2)
        time.sleep(delay)
        docker("kill", "--signal", "KILL", f"{PROJECT}-worker-1")
        order = entry_order(intent_id)
        kills.append(
            {
                "iteration": i + 1,
                "instrument": key,
                "delay_s": delay,
                "order_state_at_kill": order[1] if order else "NOT_CREATED",
            }
        )
        print(
            f"      kill {i + 1}: {delay}s after approval, "
            f"entry order {kills[-1]['order_state_at_kill']}"
        )
        docker("start", f"{PROJECT}-worker-1")
        res.check(f"chaos {i + 1}: worker healthy after SIGKILL", wait_healthy("worker"))
        final = wait_for(
            lambda iid=intent_id: entry_order(iid),
            lambda o: o is not None and o[1] not in NON_TERMINAL_TRANSMISSION,
            120,
        )
        res.check(
            f"chaos {i + 1}: entry order resolved",
            final is not None and final[1] not in NON_TERMINAL_TRANSMISSION,
            final[1] if final else "none",
        )
    stuck = sql(
        "SELECT status, count(*) FROM orders WHERE status IN "
        f"({','.join(repr(s) for s in NON_TERMINAL_TRANSMISSION)}) GROUP BY 1"
    )
    res.check("no order left in an uncertain transmission state", not stuck, str(stuck))
    dups = sql("SELECT client_order_id, count(*) FROM paper_orders GROUP BY 1 HAVING count(*) > 1")
    res.check("no duplicate venue order for any client order ID", not dups, str(dups))
    multi = sql(
        "SELECT intent_id, count(*) FROM orders WHERE purpose = 'ENTRY' "
        "GROUP BY 1 HAVING count(*) > 1"
    )
    res.check("at most one entry order per approved intent", not multi, str(multi))
    reused = sql(
        "SELECT count(*) FROM approvals WHERE consumed_at IS NOT NULL "
        "GROUP BY intent_id HAVING count(*) > 1"
    )
    res.check("no approval consumed twice", not reused)
    reconcile(p, res, "reconciliation OK after the kills")
    (VERIFY / "chaos-kills.json").write_text(json.dumps(kills, indent=2), encoding="utf-8")


def phase_restart(base: str, res: Results) -> None:
    """docker compose restart with resting orders. The market keeps moving during the
    restart, so a resting order may legitimately fill (and then get its protective stop);
    what must not happen is loss, cancellation, duplication or uncertainty caused by it."""
    before = dict(
        sql("SELECT id, status FROM orders WHERE status IN ('ACKNOWLEDGED','PARTIALLY_FILLED')")
    )
    subprocess.run(
        [str(ROOT / "scripts" / "verify-compose.sh"), "restart"], check=True, capture_output=True
    )
    res.check(
        "all services healthy after docker compose restart",
        wait_healthy("postgres") and wait_healthy("web") and wait_healthy("worker"),
    )
    p = signed_in(base, res)
    res.check("portal usable after restart", p.get("/").status_code == 200)
    time.sleep(15)
    after = dict(sql("SELECT id, status FROM orders")) if before else {}
    moved = {i: (s, after.get(i)) for i, s in before.items() if after.get(i) != s}
    bad = {i: v for i, v in moved.items() if v[1] not in ("PARTIALLY_FILLED", "FILLED")}
    res.check(
        "every resting order survived (still resting or filled by the venue)",
        bool(before) and not bad,
        f"{len(before)} resting before; {len(moved)} filled meanwhile; problems {bad}",
    )
    not_broker = []
    for order_id in moved:
        src = sql(
            f"SELECT source FROM order_events WHERE order_id = '{order_id}' "
            "ORDER BY seq DESC LIMIT 1"
        )
        if src != [["BROKER"]]:
            not_broker.append((order_id, src))
    res.check(
        "changes during the restart came only from the venue", not not_broker, str(not_broker)
    )
    dups = sql("SELECT client_order_id FROM paper_orders GROUP BY 1 HAVING count(*) > 1")
    orphans = sql(
        "SELECT p.client_order_id FROM paper_orders p "
        "LEFT JOIN orders o ON o.client_order_id = p.client_order_id WHERE o.id IS NULL"
    )
    res.check(
        "no duplicate or orphan venue orders after restart",
        not dups and not orphans,
        f"duplicates {dups} orphans {orphans}",
    )
    stuck = sql(
        "SELECT status, count(*) FROM orders WHERE status IN "
        f"({','.join(repr(s) for s in NON_TERMINAL_TRANSMISSION)}) GROUP BY 1"
    )
    res.check("no order in an uncertain transmission state", not stuck, str(stuck))
    reconcile(p, res, "reconciliation OK after restart")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["portal", "chaos", "restart"], required=True)
    ap.add_argument("--base", default="http://127.0.0.1:8743")
    ap.add_argument("--iterations", type=int, default=4)
    ap.add_argument("--seed", type=int, default=20261011)
    args = ap.parse_args()
    res = Results()
    try:
        if args.phase == "portal":
            phase_portal(args.base, res)
        elif args.phase == "chaos":
            phase_chaos(args.base, res, args.iterations, args.seed)
        else:
            phase_restart(args.base, res)
    except Exception as exc:  # report, never hide
        res.check(
            f"{args.phase} phase completed without an exception",
            False,
            f"{type(exc).__name__}: {exc}",
        )
    out = VERIFY / f"result-{args.phase}.json"
    out.write_text(json.dumps(res.rows, indent=2), encoding="utf-8")
    print(f"{len(res.rows) - res.failed}/{len(res.rows)} checks passed ({out.relative_to(ROOT)})")
    return 1 if res.failed else 0


if __name__ == "__main__":
    sys.exit(main())
