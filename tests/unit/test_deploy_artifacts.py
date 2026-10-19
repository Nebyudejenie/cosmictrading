"""Regression guard for deployment safety properties (DEP-06/07/10, CF-04/05, T12, T19)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
PROD = yaml.safe_load((ROOT / "deploy/compose.prod.yaml").read_text())
SERVICES: dict[str, Any] = PROD["services"]
APP = ("migrate", "web", "worker", "bot", "backup-prune")
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")


def test_every_external_image_is_pinned_by_digest() -> None:
    for name, svc in SERVICES.items():
        image = svc["image"]
        if image.startswith(("cosmictrading:", "cosmictrading-backup:")):
            continue  # built from this repository at a git tag
        assert DIGEST.search(image), f"{name}: {image} is not pinned by digest"
    dockerfiles = [
        ROOT / "deploy/Dockerfile",
        ROOT / "deploy/backup/Dockerfile",
        ROOT / "deploy/bastion/Dockerfile",
    ]
    for df in dockerfiles:
        for line in df.read_text().splitlines():
            if line.startswith(("FROM ", "ARG PYTHON_IMAGE", "ARG UV_IMAGE")) and "${" not in line:
                assert DIGEST.search(line.split()[1].split("=")[-1]), f"{df.name}: {line}"
    for overlay in (
        "compose.local-verify.yaml",
        "compose.admin-access.yaml",
        "compose.staging.yaml",
        "cloudflared/compose.ssh-access.yaml",
    ):
        for name, svc in yaml.safe_load((ROOT / "deploy" / overlay).read_text())[
            "services"
        ].items():
            if "image" in svc and not svc["image"].startswith("cosmictrading"):  # repo-built
                assert DIGEST.search(svc["image"]), f"{overlay}:{name}"


def test_production_publishes_no_ports() -> None:
    assert all("ports" not in svc for svc in SERVICES.values())


def test_network_segmentation() -> None:
    nets = PROD["networks"]
    assert nets["data"]["internal"] is True and nets["edge"]["internal"] is True
    assert "internal" not in nets["egress"]
    assert set(SERVICES["web"]["networks"]) == {"data", "edge"}  # no outbound internet
    assert set(SERVICES["postgres"]["networks"]) == {"data"}
    assert "egress" in SERVICES["worker"]["networks"]


def test_app_containers_are_hardened() -> None:
    for name in APP:
        svc = SERVICES[name]
        assert svc["user"] == "10001:10001", name
        assert svc["read_only"] is True, name
        assert svc["cap_drop"] == ["ALL"], name
        assert "no-new-privileges:true" in svc["security_opt"], name
    for name in ("postgres", "web", "worker", "bot", "cloudflared"):
        limits = SERVICES[name]["deploy"]["resources"]["limits"]
        assert {"memory", "cpus", "pids"} <= set(limits), name


def test_secrets_are_least_privilege() -> None:
    def secrets(name: str) -> set[str]:
        return set(SERVICES[name].get("secrets", []))

    for runtime in ("web", "worker", "bot"):
        assert not secrets(runtime) & {"db_owner_password", "db_superuser_password", "tunnel_token"}
    assert secrets("cloudflared") == {"tunnel_token"}
    assert secrets("backup") == {"db_owner_password"}
    assert secrets("postgres") == {"db_superuser_password"}
    assert "metrics_token" in secrets("web") and "metrics_token" not in secrets("worker")
    ai = {"groq_api_key", "gemini_api_key", "openrouter_api_key"}
    # The internet-facing web process holds no AI keys and no bot token (presence is reported
    # by the worker and the bot through heartbeats).
    assert secrets("web") == {"db_app_password", "secret_key", "metrics_token", "hfm_bridge_token"}
    # The MT5 bridge token is checked by the web endpoint only (ADR-028).
    assert "hfm_bridge_token" not in secrets("worker") | secrets("bot")
    assert ai <= secrets("worker") and "telegram_bot_token" not in secrets("worker")
    assert "telegram_bot_token" in secrets("bot") and not ai & secrets("bot")
    # Binance test-network keys (ADR-026): the worker only, never the web or the bot.
    testnet = {"binance_testnet_api_key", "binance_testnet_private_key"}
    assert testnet <= secrets("worker")
    assert not testnet & (secrets("web") | secrets("bot") | secrets("migrate"))
    env_web = SERVICES["web"]["environment"]
    assert not any(k.endswith(("_API_KEY_FILE", "BOT_TOKEN_FILE")) for k in env_web)
    assert not any(k.startswith("COSMIC_BINANCE") for k in SERVICES["bot"]["environment"])


def test_web_trusts_forwarding_headers_only_from_cloudflared() -> None:
    env = SERVICES["web"]["environment"]
    assert (
        env["COSMIC_FORWARDED_ALLOW_IPS"]
        == SERVICES["cloudflared"]["networks"]["edge"]["ipv4_address"]
    )
    assert "--token-file" in SERVICES["cloudflared"]["command"]


def test_tunnel_template_has_catch_all_404_and_hides_internal() -> None:
    cfg = yaml.safe_load((ROOT / "deploy/cloudflared/config.example.yml").read_text())
    rules = cfg["ingress"]
    assert rules[-1] == {"service": "http_status:404"}
    internal = [r for r in rules if r.get("path") == "^/internal/"]
    assert internal and internal[0]["service"] == "http_status:404"
    assert rules.index(internal[0]) < next(
        i for i, r in enumerate(rules) if "web:8000" in r["service"]
    )


def test_ssh_jump_host_is_key_only_and_forwards_only_to_the_host_sshd() -> None:
    cfg = (ROOT / "deploy/bastion/sshd_config").read_text()
    for line in (
        "PasswordAuthentication no",
        "KbdInteractiveAuthentication no",
        "PermitRootLogin no",
        "AllowUsers jump",
        "PermitOpen host.docker.internal:22",
        "PermitTTY no",
        "AllowAgentForwarding no",
        "PermitTunnel no",
        "GatewayPorts no",
    ):
        assert line in cfg, line
    ssh = yaml.safe_load((ROOT / "deploy/cloudflared/compose.ssh-access.yaml").read_text())
    assert all("ports" not in svc for svc in ssh["services"].values())
    assert ssh["services"]["bastion"]["read_only"] is True


def test_every_long_running_service_has_a_healthcheck() -> None:
    """A hung bot (alive, no longer delivering) must show as unhealthy, like the worker."""
    for name in ("postgres", "web", "worker", "bot", "cloudflared"):
        assert SERVICES[name].get("healthcheck", {}).get("test"), name
    env = SERVICES["bot"]["environment"]
    heartbeat = env["COSMIC_HEARTBEAT_FILE"]
    assert heartbeat.endswith("bot.heartbeat")
    assert heartbeat in " ".join(SERVICES["bot"]["healthcheck"]["test"])  # same file
    assert env["COSMIC_TELEGRAM_BOT_TOKEN_FILE"] == "/run/secrets/telegram_bot_token"


def test_staging_overlay_cannot_reach_production() -> None:
    """Staging shares the VM with production: its project name and database volume travel
    together in the overlay, it publishes only on loopback, and it never runs the tunnel or
    the bot (they would take production's traffic or Telegram updates)."""
    staging = yaml.safe_load((ROOT / "deploy/compose.staging.yaml").read_text())
    assert staging["name"] == "cosmictrading-staging"
    assert staging["volumes"]["pgdata"]["name"] == "cosmictrading_staging_pgdata"
    assert staging["volumes"]["pgdata"]["name"] != PROD["volumes"]["pgdata"]["name"]
    assert not {"cloudflared", "bot"} & set(staging["services"])
    for name, svc in staging["services"].items():
        for port in svc.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), f"{name}: {port}"
    web_ip = staging["services"]["web"]["networks"]["edge"]["ipv4_address"]
    assert ":?" in web_ip  # no silent fallback to production's address


def test_vm_ssh_hardening_wins_and_forwards_only_to_the_relays() -> None:
    bootstrap = (ROOT / "deploy/vm/bootstrap.sh").read_text()
    assert "/etc/ssh/sshd_config.d/00-cosmic.conf" in bootstrap  # before 50-cloud-init.conf
    assert "PasswordAuthentication no" in bootstrap
    assert "AllowTcpForwarding local" in bootstrap
    assert "PermitOpen 127.0.0.1:8743 127.0.0.1:8744" in bootstrap
    assert "run sshd -t" in bootstrap
