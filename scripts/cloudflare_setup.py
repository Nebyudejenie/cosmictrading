"""Cloudflare Tunnel, DNS and Access for cosmictrading.bond (docs/12 §5).

Modes (the token is read from a file and never printed):

    inventory   read-only: zone, nameservers, every DNS record, Zero Trust team, tunnels
    plan        read-only: what `apply` would create; refuses if a target name already has
                a DNS record that this script did not create
    apply       create (idempotently) the tunnels, their ingress, one proxied CNAME per
                hostname, Access applications and policies, and an SSH service token

Nothing is ever deleted or modified outside the objects this script owns (identified by
name prefix "cosmictrading-" or the comment "managed-by: cosmictrading-setup").

Secrets written (0600, never printed):
    ~/.secrets/cosmictrading-tunnel-<name>.token     tunnel connector tokens
    ~/.secrets/cosmictrading-access-ssh-service.json SSH service token (client id/secret)

Usage: python3 scripts/cloudflare_setup.py {inventory|plan|apply} [--token-file PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

API = "https://api.cloudflare.com/client/v4"
ZONE = "cosmictrading.bond"
OWNER_EMAILS = ["nebiyudejenie@gmail.com"]
MARK = "managed-by: cosmictrading-setup"
SECRETS = Path.home() / ".secrets"


@dataclass(frozen=True)
class Route:
    hostname: str
    tunnel: str  # tunnel name
    ingress: list[dict[str, Any]]  # rules for this hostname (before the catch-all)
    access_name: str
    allow_service_token: bool


ROUTES = [
    Route(
        hostname=f"staging.{ZONE}",
        tunnel="cosmictrading-staging",
        ingress=[
            {"hostname": f"staging.{ZONE}", "path": "^/internal/", "service": "http_status:404"},
            {
                "hostname": f"staging.{ZONE}",
                "service": "http://web:8000",
                "originRequest": {"connectTimeout": 10, "httpHostHeader": f"staging.{ZONE}"},
            },
        ],
        access_name="cosmictrading-staging",
        allow_service_token=True,  # lets automated checks reach /readyz behind Access
    ),
    Route(
        hostname=f"ssh.{ZONE}",
        tunnel="cosmictrading-ssh",
        ingress=[{"hostname": f"ssh.{ZONE}", "service": "ssh://localhost:22"}],
        access_name="cosmictrading-ssh",
        allow_service_token=True,  # non-interactive SSH for the operator's tooling
    ),
]
SERVICE_TOKEN_NAME = "cosmictrading-ssh-automation"  # noqa: S105 - a name


class CF:
    def __init__(self, token: str) -> None:
        self._token = token

    def call(self, method: str, path: str, body: Any = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(f"{API}{path}", data=data, method=method)  # noqa: S310
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed https API
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            payload = json.loads(exc.read() or b"{}")
            errors = payload.get("errors") or [{"message": str(exc)}]
            raise SystemExit(f"Cloudflare API {method} {path.split('?')[0]}: {errors}") from None
        if not payload.get("success", False):
            raise SystemExit(
                f"Cloudflare API {method} {path.split('?')[0]}: {payload.get('errors')}"
            )
        return payload

    def all(self, path: str) -> list[Any]:
        out, page = [], 1
        while True:
            sep = "&" if "?" in path else "?"
            p = self.call("GET", f"{path}{sep}page={page}&per_page=100")
            out += p["result"]
            info = p.get("result_info") or {}
            if page >= int(info.get("total_pages") or 1):
                return out
            page += 1


def secret_write(name: str, content: str) -> Path:
    SECRETS.mkdir(mode=0o700, exist_ok=True)
    path = SECRETS / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(content)
    return path


def context(cf: CF) -> tuple[str, str, dict[str, Any]]:
    zones = cf.call("GET", f"/zones?name={ZONE}")["result"]
    if not zones:
        raise SystemExit(f"zone {ZONE} is not visible to this token (needs Zone:Read on it)")
    z = zones[0]
    return z["id"], z["account"]["id"], z


def inventory(cf: CF) -> dict[str, Any]:
    zone_id, account_id, z = context(cf)
    records = cf.all(f"/zones/{zone_id}/dns_records")
    try:
        org = cf.call("GET", f"/accounts/{account_id}/access/organizations")["result"]
    except SystemExit as exc:
        org = {"error": str(exc)}
    tunnels = cf.all(f"/accounts/{account_id}/cfd_tunnel?is_deleted=false")
    return {"zone": z, "records": records, "org": org, "tunnels": tunnels, "account": account_id}


def print_inventory(inv: dict[str, Any]) -> None:
    z = inv["zone"]
    print(f"zone {z['name']} status={z['status']} plan={z.get('plan', {}).get('name')}")
    print(f"nameservers: {', '.join(z.get('name_servers', []))}")
    print(f"DNS records ({len(inv['records'])}):")
    for r in inv["records"]:
        print(
            f"  {r['type']:6} {r['name']:40} {r['content'][:60]:60} proxied={r.get('proxied')}"
            f" {('comment=' + r['comment']) if r.get('comment') else ''}"
        )
    org = inv["org"]
    if "error" in org:
        print(f"Zero Trust: NOT AVAILABLE ({org['error'][:160]})")
    else:
        print(f"Zero Trust team domain: {org.get('auth_domain')}")
    print(f"tunnels ({len(inv['tunnels'])}): " + ", ".join(t["name"] for t in inv["tunnels"]))


def plan(cf: CF, inv: dict[str, Any]) -> list[str]:
    problems, steps = [], []
    by_name = {}
    for r in inv["records"]:
        by_name.setdefault(r["name"], []).append(r)
    tunnels = {t["name"]: t for t in inv["tunnels"]}
    if "error" in inv["org"]:
        problems.append("Zero Trust is not enabled for this account (Access is required)")
    for route in ROUTES:
        t = tunnels.get(route.tunnel)
        steps.append(f"tunnel {route.tunnel}: {'exists' if t else 'create (remotely managed)'}")
        steps.append(
            "  ingress: "
            + str([i.get("path", "") + " " + i["service"] for i in route.ingress])
            + " + catch-all 404"
        )
        existing = by_name.get(route.hostname, [])
        ours = [r for r in existing if MARK in (r.get("comment") or "")]
        if existing and not ours:
            problems.append(
                f"{route.hostname} already has a DNS record not created by this script"
                " — not touching it"
            )
        steps.append(
            f"DNS {route.hostname}: "
            + ("exists (ours)" if ours else "create proxied CNAME → <tunnel>.cfargotunnel.com")
        )
        steps.append(
            f"Access app {route.access_name}: allow {OWNER_EMAILS}"
            + (" + service token" if route.allow_service_token else "")
        )
    for s in steps:
        print(s)
    for p in problems:
        print(f"BLOCKED: {p}")
    return problems


def find_app(cf: CF, account_id: str, domain: str) -> dict[str, Any] | None:
    for a in cf.all(f"/accounts/{account_id}/access/apps"):
        if a.get("domain") == domain:
            return a
    return None


def apply(cf: CF, inv: dict[str, Any]) -> None:
    if plan(cf, inv):
        raise SystemExit("refusing to apply while the plan has BLOCKED items")
    zone_id, account_id = inv["zone"]["id"], inv["account"]
    # Service token for non-interactive access (created once; the secret is shown only once).
    tokens = {t["name"]: t for t in cf.all(f"/accounts/{account_id}/access/service_tokens")}
    st = tokens.get(SERVICE_TOKEN_NAME)
    if st is None:
        st = cf.call(
            "POST",
            f"/accounts/{account_id}/access/service_tokens",
            {"name": SERVICE_TOKEN_NAME, "duration": "8760h"},
        )["result"]
        path = secret_write(
            "cosmictrading-access-ssh-service.json",
            json.dumps({"client_id": st["client_id"], "client_secret": st["client_secret"]}),
        )
        print(f"service token created; credentials saved to {path}")
    else:
        print("service token exists (secret was saved when it was created)")
    tunnels = {t["name"]: t for t in inv["tunnels"]}
    for route in ROUTES:
        t = tunnels.get(route.tunnel)
        if t is None:
            t = cf.call(
                "POST",
                f"/accounts/{account_id}/cfd_tunnel",
                {"name": route.tunnel, "config_src": "cloudflare"},
            )["result"]
            print(f"tunnel {route.tunnel} created ({t['id']})")
        tid = t["id"]
        cf.call(
            "PUT",
            f"/accounts/{account_id}/cfd_tunnel/{tid}/configurations",
            {"config": {"ingress": [*route.ingress, {"service": "http_status:404"}]}},
        )
        token = cf.call("GET", f"/accounts/{account_id}/cfd_tunnel/{tid}/token")["result"]
        path = secret_write(
            f"cosmictrading-tunnel-{route.tunnel.removeprefix('cosmictrading-')}.token", token
        )
        print(f"tunnel {route.tunnel}: ingress set; connector token saved to {path}")
        recs = cf.call("GET", f"/zones/{zone_id}/dns_records?name={route.hostname}")["result"]
        if not recs:
            cf.call(
                "POST",
                f"/zones/{zone_id}/dns_records",
                {
                    "type": "CNAME",
                    "name": route.hostname,
                    "content": f"{tid}.cfargotunnel.com",
                    "proxied": True,
                    "ttl": 1,
                    "comment": MARK,
                },
            )
            print(f"DNS {route.hostname} → tunnel created (proxied CNAME)")
        app = find_app(cf, account_id, route.hostname)
        include_people = [{"email": {"email": e}} for e in OWNER_EMAILS]
        if app is None:
            app = cf.call(
                "POST",
                f"/accounts/{account_id}/access/apps",
                {
                    "name": route.access_name,
                    "domain": route.hostname,
                    "type": "self_hosted",
                    "session_duration": "12h",
                    "app_launcher_visible": False,
                    "auto_redirect_to_identity": False,
                },
            )["result"]
            print(f"Access app {route.access_name} created")
        existing = {
            p["name"] for p in cf.all(f"/accounts/{account_id}/access/apps/{app['id']}/policies")
        }
        if "owner" not in existing:
            cf.call(
                "POST",
                f"/accounts/{account_id}/access/apps/{app['id']}/policies",
                {"name": "owner", "decision": "allow", "include": include_people, "precedence": 1},
            )
        if route.allow_service_token and "automation" not in existing:
            cf.call(
                "POST",
                f"/accounts/{account_id}/access/apps/{app['id']}/policies",
                {
                    "name": "automation",
                    "decision": "non_identity",
                    "precedence": 2,
                    "include": [{"service_token": {"token_id": st["id"]}}],
                },
            )
        print(
            f"Access {route.hostname}: owner "
            + ("+ service token " if route.allow_service_token else "")
            + "policies in place"
        )
    print("done. Next: put the connector tokens on the server and start the connectors.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["inventory", "plan", "apply"])
    ap.add_argument("--token-file", default=str(SECRETS / "cosmictrading-cf-token"))
    args = ap.parse_args()
    path = Path(args.token_file)
    if not path.is_file():
        raise SystemExit(f"token file {path} not found")
    if path.stat().st_mode & 0o077:
        raise SystemExit(f"{path} must not be readable by others (chmod 600)")
    cf = CF(path.read_text().strip())
    inv = inventory(cf)
    if args.mode == "inventory":
        print_inventory(inv)
    elif args.mode == "plan":
        print_inventory(inv)
        print("--- plan ---")
        plan(cf, inv)
    else:
        apply(cf, inv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
