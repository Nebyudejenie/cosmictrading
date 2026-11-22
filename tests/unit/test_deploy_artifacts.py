"""Regression guard for deployment safety properties (DEP-06/07/10, CF-04/05/10, T12, T19)."""

from __future__ import annotations

import base64
import re
import shutil
import subprocess
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
        "compose.bridge-link.yaml",
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
    bridge_tokens = {"hfm_bridge_token", "hfm_bridge2_token", "hfm_bridge3_token"}  # one per bridge
    assert secrets("web") == {"db_app_password", "secret_key", "metrics_token"} | bridge_tokens
    # The MT5 bridge tokens are checked by the web endpoint only (ADR-028).
    assert not bridge_tokens & (secrets("worker") | secrets("bot"))
    assert ai <= secrets("worker") and "telegram_bot_token" not in secrets("worker")
    assert "telegram_bot_token" in secrets("bot") and not ai & secrets("bot")
    # Binance test-network keys (ADR-026): the worker only, never the web or the bot.
    testnet = {"binance_testnet_api_key", "binance_testnet_private_key"}
    assert testnet <= secrets("worker")
    assert not testnet & (secrets("web") | secrets("bot") | secrets("migrate"))
    env_web = SERVICES["web"]["environment"]
    assert not any(k.endswith(("_API_KEY_FILE", "BOT_TOKEN_FILE")) for k in env_web)
    assert not any(k.startswith("COSMIC_BINANCE") for k in SERVICES["bot"]["environment"])


def test_no_service_holds_a_real_money_key_or_names_a_live_venue() -> None:
    """ADR-034: the real-money link is built and kept off. Mounting the live key or naming a
    live venue is the owner's own change to the server, never part of a deployment."""
    live = {"binance_live_api_key", "binance_live_private_key"}
    for compose in sorted((ROOT / "deploy").rglob("compose*.yaml")):
        services = (yaml.safe_load(compose.read_text()) or {}).get("services") or {}
        for name, service in services.items():
            assert not live & set((service or {}).get("secrets") or []), (compose.name, name)
            env = (service or {}).get("environment") or {}
            keys = env if isinstance(env, dict) else [str(e).split("=")[0] for e in env]
            assert not any(
                str(k).startswith(("COSMIC_BINANCE_LIVE", "COSMIC_LIVE_VENUES")) for k in keys
            ), (compose.name, name)
        top = (yaml.safe_load(compose.read_text()) or {}).get("secrets") or {}
        assert not live & set(top), compose.name


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


def test_vm_firewall_lets_only_the_jump_network_reach_sshd() -> None:
    """The SSH jump container reaches the VM's sshd from its fixed network: the firewall rule
    must name the bridge and subnet the compose file gives that network."""
    bootstrap = (ROOT / "deploy/vm/bootstrap.sh").read_text()
    ssh = yaml.safe_load((ROOT / "deploy/cloudflared/compose.ssh-access.yaml").read_text())
    jump = ssh["networks"]["jump"]
    bridge = jump["driver_opts"]["com.docker.network.bridge.name"]
    subnet = jump["ipam"]["config"][0]["subnet"].split(":-", 1)[1].rstrip("}")
    assert f"ufw allow in on {bridge} from {subnet} to any port 22 proto tcp" in bootstrap


def test_release_reaches_production_only_after_staging_passed() -> None:
    script = (ROOT / "scripts/release.sh").read_text()
    assert "git status --porcelain" in script and "origin/main" in script
    staging = script.index("release /srv/cosmic-staging --build")
    stop = script.index('if [ "$prod" -eq 0 ]')
    production = script.index("release /srv/cosmic\n")
    assert staging < stop < production
    assert "set -e;" in script  # a failed deploy or check ends the session and the run
    assert "--short=7" in script  # two clones may pick different default abbreviations


def test_drill_log_is_provisioned_for_the_admin() -> None:
    """The workstation drill appends as the admin, who cannot create files in backups/."""
    for name in ("bootstrap.sh", "staging-init.sh"):
        text = (ROOT / "deploy/vm" / name).read_text()
        assert "install -m 0660 -o 10001 -g 10001 /dev/null" in text, name
        assert "restore-drills.jsonl" in text, name


def test_password_free_sudo_covers_only_the_deploy_and_check_scripts() -> None:
    bootstrap = (ROOT / "deploy/vm/bootstrap.sh").read_text()
    rule = next(line for line in bootstrap.splitlines() if "NOPASSWD:" in line)
    allowed = rule.split("NOPASSWD:", 1)[1].rstrip('"').split(", ")
    assert [a.strip() for a in allowed] == [
        "/srv/cosmic/src/deploy/deploy.sh",
        "/srv/cosmic/src/deploy/vm/verify.sh",
        "/srv/cosmic-staging/src/deploy/deploy.sh",
        "/srv/cosmic-staging/src/deploy/vm/verify.sh",
    ]
    assert bootstrap.index("visudo -cqf") < bootstrap.index('mv "$sudoers.tmp-$stamp" "$sudoers"')


def test_verify_fails_on_a_failed_boot_unit() -> None:
    verify = (ROOT / "deploy/vm/verify.sh").read_text()
    assert "systemctl is-failed --quiet cosmictrading.service" in verify


def test_backup_timers_run_without_waiting_for_a_reboot() -> None:
    """An enabled timer starts only at the next boot: staging's never ran on 2026-10-19/20."""
    deploy = (ROOT / "deploy/deploy.sh").read_text()
    start = deploy.index('run systemctl start "$timer"')
    assert deploy.index("smoke tests") < start < deploy.index("deploy-history.log")
    assert "timer=cosmic-staging-backup.timer" in deploy
    init = (ROOT / "deploy/vm/staging-init.sh").read_text()
    assert "systemctl enable --now cosmic-staging-backup.timer" in init
    verify = (ROOT / "deploy/vm/verify.sh").read_text()
    assert "systemctl is-active cosmic-backup.timer" in verify
    assert "systemctl is-active cosmic-staging-backup.timer" in verify


def _gate(tag: str, production: Path, staging: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(ROOT / "deploy/tested-on-staging.sh"), tag, str(production), str(staging)],
        capture_output=True,
        text=True,
        check=False,
    )


def _history(root: Path, *lines: str) -> None:
    (root / "config").mkdir(parents=True)
    (root / "config/deploy-history.log").write_text("".join(f"{line}\n" for line in lines))


def test_production_takes_only_a_build_that_ran_on_staging(tmp_path: Path) -> None:
    """CF-10: deploy.sh records a tag only after its smoke tests pass; production accepts a tag
    staging recorded, or one production ran before (a rollback), and nothing else."""
    production, staging = tmp_path / "prod", tmp_path / "staging"
    _history(
        staging,
        "2026-10-19T14:26:45Z deployed ebe52b0 (previous f9d2b2a)",
        "x deployed 1.2 (previous none)",
    )
    _history(production, "2026-10-19T12:53:08Z deployed f9d2b2a (previous none)")
    assert _gate("ebe52b0", production, staging).returncode == 0  # passed staging
    assert _gate("f9d2b2a", production, staging).returncode == 0  # rollback
    refused = _gate("2864dec", production, staging)
    assert refused.returncode == 1 and "has not run on staging" in refused.stderr
    assert _gate("ebe52b", production, staging).returncode == 1  # a prefix is another tag
    assert _gate("1x2", production, staging).returncode == 1  # "." is not a wildcard
    assert _gate("ebe52b0", tmp_path / "none", tmp_path / "none").returncode == 1  # no history
    assert _gate("bad tag", production, staging).returncode == 2


def test_deploy_runs_the_staging_gate_for_production() -> None:
    deploy = (ROOT / "deploy/deploy.sh").read_text()
    gate = deploy.index('"$src/deploy/tested-on-staging.sh" "$TAG" "$R" /srv/cosmic-staging')
    assert 'if [ "$R" = /srv/cosmic ]; then' in deploy[:gate]  # production root, any overlay
    assert gate < deploy.index("# pre-deploy encrypted backup") < deploy.index("compose up -d")
    assert 'untested=" UNTESTED"' in deploy  # an override is recorded in the history


# ---- S91: the HFM bridge's direct link ----------------------------------------------------------

LINK = yaml.safe_load((ROOT / "deploy/compose.bridge-link.yaml").read_text())
LINK_SCRIPT = ROOT / "deploy/vm/bridge-link.sh"


def test_the_bridge_links_relay_is_on_the_loopback_and_reaches_only_the_bridge() -> None:
    """Nothing new listens on the home network: the relay is published on the VM's loopback
    only; it is not the proxy the web process trusts; and the web process serves its address
    the bridge's two addresses and nothing else."""
    assert list(LINK["services"]) == ["bridge-relay"]
    relay = LINK["services"]["bridge-relay"]
    assert relay["ports"] == ["127.0.0.1:${COSMIC_BRIDGE_LINK_PORT:-8745}:8000"]
    # To the web process and nowhere else; and no waiting to fill a packet, on either side.
    assert relay["command"] == ["TCP-LISTEN:8000,fork,reuseaddr,nodelay", "TCP:web:8000,nodelay"]
    assert relay["read_only"] is True and relay["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in relay["security_opt"]
    assert {"memory", "cpus", "pids"} <= set(relay["deploy"]["resources"]["limits"])
    assert relay["restart"] == "unless-stopped"  # verify.sh: every container restarts with Docker
    address = relay["networks"]["edge"]["ipv4_address"]
    web = SERVICES["web"]["environment"]
    assert web["COSMIC_BRIDGE_ONLY_PEERS"] == f'["{address}"]'
    assert address != SERVICES["cloudflared"]["networks"]["edge"]["ipv4_address"]
    assert address != SERVICES["web"]["networks"]["edge"]["ipv4_address"]
    assert address not in web["COSMIC_FORWARDED_ALLOW_IPS"]
    # The address the web process restricts is the one this file gives the relay, in the code too.
    from cosmic.web.security import BRIDGE_PATHS

    assert BRIDGE_PATHS == ("/bridge/v1/sync", "/bridge/v1/fast")


def test_the_bridge_link_is_production_only_and_checked_at_every_release() -> None:
    deploy = (ROOT / "deploy/deploy.sh").read_text()
    verify = (ROOT / "deploy/vm/verify.sh").read_text()
    staging = yaml.safe_load((ROOT / "deploy/compose.staging.yaml").read_text())
    assert "bridge-relay" not in staging["services"]
    # deploy.sh: only for production's root; what is handed in is checked before it is taken;
    # the sign-in is set up after the release itself is up, and its failure never fails it.
    block = deploy[deploy.index("# The HFM bridge's direct link") : deploy.index("compose() {")]
    assert 'if [ "$R" = /srv/cosmic ]; then' in block
    assert block.index('"$link_tool" --check') < block.index(
        'run install -m 0644 -o root -g root "$handed_key"'
    )
    assert deploy.index("run compose up -d") < deploy.index('"$link_tool" "${apply[@]}" --key')
    assert "the release goes on without it" in deploy
    # verify.sh: with the link on, the relay and the sign-in are checked; with it off, that
    # nothing of it is left.
    for check in (
        "bridge link: the relay is on this machine's loopback only",
        "bridge link: the relay reaches the bridge's two addresses and nothing else",
        "bridge link: its sign-in can forward to the relay and do nothing else",
        "bridge link: off, and nothing of it left on this machine",
    ):
        assert check in verify, check
    assert '"$(answer "http://127.0.0.1:$link_port/login")" = 404' in verify
    assert '"$(answer -X POST "http://127.0.0.1:$link_port/bridge/v1/fast")" = 401' in verify


def test_the_second_bridges_token_is_made_once_on_production_and_never_shown() -> None:
    """S110: terminal 2's bridge token is made by the release on production only, only while its
    file is empty (never replaced), with the same owner and mode as the other secrets, and never
    printed."""
    deploy = (ROOT / "deploy/deploy.sh").read_text()
    block = deploy[
        deploy.index("# Terminal 2's bridge token") : deploy.index("for s in groq_api_key")
    ]
    assert 'if [ "$R" = /srv/cosmic ] && [ ! -s "$R/secrets/hfm_bridge2_token" ]; then' in block
    assert "od -An -N32 -tx1 /dev/urandom" in block and "umask 077" in block
    assert 'install -m 0440 -o root -g 10001 "$new_token" "$R/secrets/hfm_bridge2_token"' in block
    assert 'if [ "$DRY" -eq 0 ]; then' in block
    for line in block.splitlines():  # nothing that could show it
        if "echo" in line or "printf" in line:
            assert "new_token" not in line and 'hfm_bridge2_token"' not in line, line
    assert "cat " not in block


def test_the_third_bridges_token_is_made_once_on_production_and_never_shown() -> None:
    """S113: terminal 3's (the real account's read-only terminal) token, made as terminal 2's
    was: on production only, only while empty, never replaced, never printed."""
    deploy = (ROOT / "deploy/deploy.sh").read_text()
    block = deploy[
        deploy.index("# Terminal 3's bridge token") : deploy.index("for s in groq_api_key")
    ]
    assert 'if [ "$R" = /srv/cosmic ] && [ ! -s "$R/secrets/hfm_bridge3_token" ]; then' in block
    assert "od -An -N32 -tx1 /dev/urandom" in block and "umask 077" in block
    assert 'install -m 0440 -o root -g 10001 "$new_token" "$R/secrets/hfm_bridge3_token"' in block
    for line in block.splitlines():
        if "echo" in line or "printf" in line:
            assert "new_token" not in line and 'hfm_bridge3_token"' not in line, line
    assert "cat " not in block


def test_the_bridge_links_sign_in_can_forward_to_the_relay_and_do_nothing_else() -> None:
    """The rules sshd is given for the link's sign-in, and that sshd itself checks them before
    it is reloaded (never restarted), with the change taken back otherwise. The drill
    (scripts/bridge-link-drill.sh) runs all of it against a real sshd."""
    script = LINK_SCRIPT.read_text()
    for rule in (
        "AuthorizedKeysFile $KEYS",
        "AuthenticationMethods publickey",
        "PasswordAuthentication no",
        "AllowTcpForwarding local",
        "PermitOpen 127.0.0.1:$PORT",
        "PermitListen none",
        "AllowAgentForwarding no",
        "X11Forwarding no",
        "PermitTTY no",
        "PermitTunnel no",
        "MaxSessions 0",
        "ForceCommand /usr/sbin/nologin",
    ):
        assert f"    {rule}\n" in script, rule
        assert f'"{rule.lower()}"' in script.lower(), rule  # and asked of sshd afterwards
    assert (
        'restrict,port-forwarding,permitopen=\\"127.0.0.1:$PORT\\",from=\\"$address\\" $key'
        in script
    )
    assert (
        "--shell /usr/sbin/nologin" in script and "KEYS=/etc/ssh/cosmic-bridge-link.keys" in script
    )
    setup = script[script.index("\nsetup)") :]
    assert setup.index("sshd -t || back") < setup.index("$RELOAD")
    assert setup.index('[ "$before" = "$after" ] || back') < setup.index("$RELOAD")
    assert setup.index('link_rules_hold "$address" || back') < setup.index("$RELOAD")
    assert "restart" not in script.replace("never restarted", "")
    # One port everywhere: the script, the relay, the release check, and the bridge's own default.
    from bridge.cosmic_mt5_bridge import Direct

    port = str(Direct.__dataclass_fields__["port"].default)
    assert f'PORT="${{COSMIC_BRIDGE_LINK_PORT:-{port}}}"' in script
    assert f"COSMIC_BRIDGE_LINK_PORT:-{port}" in LINK["services"]["bridge-relay"]["ports"][0]
    assert (
        f'link_port="${{COSMIC_BRIDGE_LINK_PORT:-{port}}}"'
        in (ROOT / "deploy/vm/verify.sh").read_text()
    )


def test_only_one_plain_ed25519_key_and_one_address_are_taken_for_the_link(tmp_path: Path) -> None:
    """What the admin hands in becomes a line sshd reads: nothing but one key and one address."""
    if shutil.which("ssh-keygen") is None:  # the check asks ssh-keygen whether it is a key
        return
    blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes(range(32))
    good = f"ssh-ed25519 {base64.b64encode(blob).decode()} trading-computer\n"

    def check(key: str, address: str) -> int:
        (tmp_path / "k.pub").write_text(key)
        return subprocess.run(
            [str(LINK_SCRIPT), "--check", "--key", str(tmp_path / "k.pub"), "--from", address],
            capture_output=True,
            check=False,
        ).returncode

    assert check(good, "192.0.2.202") == 0
    assert check(good + good, "192.0.2.202") != 0  # two keys
    assert check(f'command="/bin/sh" {good}', "192.0.2.202") != 0  # options of its own
    assert check(good.replace("ssh-ed25519 ", "ssh-rsa "), "192.0.2.202") != 0
    assert check("ssh-ed25519 AAAAnotakey\n", "192.0.2.202") != 0
    for address in ("192.0.2.0/24", "192.0.2.999", "*", "host.example", '1.2.3.4",from="*'):
        assert check(good, address) != 0, address
