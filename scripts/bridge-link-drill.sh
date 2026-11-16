#!/usr/bin/env bash
# Drill for the HFM bridge's direct link (S91): deploy/vm/bridge-link.sh against a real sshd,
# in throwaway containers on this machine. Nothing outside them is touched.
#
# A "server" container gets the VM's own SSH hardening (as bootstrap.sh writes it) and a
# listener on its loopback standing in for the bridge relay. Two "trading computers" then try
# everything: the one thing the link's sign-in may do, and what it must refuse.
#
#   scripts/bridge-link-drill.sh            run it (builds a small image the first time)
#   scripts/bridge-link-drill.sh --clean    also remove that image afterwards
set -uo pipefail
cd "$(git rev-parse --show-toplevel)" || exit 2
clean=0; [ "${1:-}" != --clean ] || clean=1
image=cosmic-linkdrill:local
net=cosmic-linkdrill
server=cosmic-linkdrill-server; pc=cosmic-linkdrill-pc; other=cosmic-linkdrill-other
subnet=10.203.66.0/28; server_ip=10.203.66.2; pc_ip=10.203.66.3; other_ip=10.203.66.4
port=8745
fails=0

leftovers() {
  docker rm -f "$server" "$pc" "$other" >/dev/null 2>&1
  docker network rm "$net" >/dev/null 2>&1
}
tidy() { leftovers; [ "$clean" -eq 0 ] || docker image rm "$image" >/dev/null 2>&1; }
trap tidy EXIT
leftovers

echo "# image (Ubuntu 26.04, the VM's release: its sshd and ssh)"
# --network host: on some machines a build container resolves no names on the default network.
docker build --network host -q -t "$image" - >/dev/null <<'EOF' || { echo "cannot build the drill image" >&2; exit 2; }
FROM ubuntu:26.04
RUN apt-get update -q && DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends \
      openssh-server openssh-client curl python3 procps ca-certificates && \
    rm -rf /var/lib/apt/lists/* && mkdir -p /run/sshd
EOF
docker network create --subnet "$subnet" "$net" >/dev/null || exit 2
docker run -d --name "$server" --network "$net" --ip "$server_ip" "$image" sleep 3600 >/dev/null || exit 2
docker run -d --name "$pc" --network "$net" --ip "$pc_ip" "$image" sleep 3600 >/dev/null || exit 2
docker run -d --name "$other" --network "$net" --ip "$other_ip" "$image" sleep 3600 >/dev/null || exit 2
on() { local c="$1"; shift; docker exec -i "$c" bash -c "$*"; }

# The server: the VM's hardening, sshd, a "relay" on its loopback, and a second loopback
# service the link must never reach (it stands for the admin's own relays, 8743 and 8744).
# Only for the drill: no penalties for a computer whose sign-ins fail (the steps below fail on
# purpose, many times, from one address), and an sshd that can be told to reject its
# configuration (/tmp/reject), to see a rejected change taken back.
on "$server" "cat > /etc/ssh/sshd_config.d/00-cosmic.conf" <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
X11Forwarding no
AllowTcpForwarding local
PermitOpen 127.0.0.1:8743 127.0.0.1:8744
MaxAuthTries 3
PerSourcePenalties no
EOF
on "$server" "cat > /srv/relay.py" <<'EOF'
"""The stand-in for the bridge relay: says who it is, and answers a POST as the portal would."""
import http.server
import json
import sys


class Relay(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def answer(self, body: bytes) -> None:  # in one piece, as a relay passes it on
        self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body)

    def do_GET(self) -> None:
        self.answer(b"relay\n")

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers["Content-Length"]))
        self.answer(json.dumps({"commands": [], "path": self.path}).encode())

    def log_message(self, *args: object) -> None:
        pass


http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Relay).serve_forever()
EOF
on "$server" "mkdir -p /srv/admin && echo admin > /srv/admin/index.html
  (python3 /srv/relay.py $port >/dev/null 2>&1 &)
  (cd /srv/admin && python3 -m http.server 8743 --bind 127.0.0.1 >/dev/null 2>&1 &)
  printf '%s\n' '#!/bin/sh' 'if [ -e /tmp/reject ] && [ \"\$1\" = -t ]; then echo rejected >&2; exit 1; fi' \
    'exec /usr/sbin/sshd \"\$@\"' > /usr/local/bin/sshd && chmod +x /usr/local/bin/sshd
  /usr/sbin/sshd -t && /usr/sbin/sshd"
docker cp deploy/vm/bridge-link.sh "$server:/usr/local/bin/bridge-link.sh" >/dev/null
link() { on "$server" "COSMIC_SSHD_RELOAD='pkill -HUP -o -x sshd' bridge-link.sh $*"; }

# The trading computer's key (made there; only its public half leaves), the server's own key
# pinned on it, and the admin's key (root, to show the admin's sign-in is untouched).
on "$pc" "ssh-keygen -q -t ed25519 -N '' -f /root/link_key && ssh-keygen -q -t ed25519 -N '' -f /root/other_key
  ssh-keygen -q -t ed25519 -N '' -f /root/admin_key"
on "$pc" "cat /root/link_key.pub" | on "$server" "cat > /tmp/link.pub"
on "$pc" "cat /root/admin_key.pub" | on "$server" "mkdir -p /root/.ssh && cat > /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys"
host_key="$(on "$server" "cat /etc/ssh/ssh_host_ed25519_key.pub" | cut -d' ' -f1,2)"
for c in "$pc" "$other"; do echo "$server_ip $host_key" | on "$c" "cat > /root/known"; done
on "$pc" "cat /root/link_key" | on "$other" "cat > /root/link_key && chmod 600 /root/link_key"  # a stolen key

ssh_as() { # key user [ssh arguments] → the bridge's own ssh options
  local key="$1" user="$2"; shift 2
  printf 'ssh -F none -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/root/known -o ExitOnForwardFailure=yes -o ServerAliveInterval=5 -o ServerAliveCountMax=2 -o LogLevel=ERROR -i /root/%s %s %s@%s' \
    "$key" "$*" "$user" "$server_ip"
}
through() { # container key user remote-port → what a request through that tunnel gets (or "refused")
  local c="$1" key="$2" user="$3" to="$4"
  on "$c" "$(ssh_as "$key" "$user" -N -T -L "127.0.0.1:9000:127.0.0.1:$to") & t=\$!
    sleep 1.5; curl -s --max-time 3 http://127.0.0.1:9000/ || echo refused; kill \$t 2>/dev/null; wait \$t 2>/dev/null"
}
step() { # name expected actual
  if [ "$2" = "$3" ]; then echo "PASS  $1"; else echo "FAIL  $1 (expected: $2; got: $3)"; fails=$((fails + 1)); fi
}
passes() { local name="$1"; shift; if "$@" >/dev/null 2>&1; then echo "PASS  $name"; else echo "FAIL  $name"; fails=$((fails + 1)); fi; }
refuses() { local name="$1"; shift; if "$@" >/dev/null 2>&1; then echo "FAIL  $name"; fails=$((fails + 1)); else echo "PASS  $name"; fi; }

echo "# what is handed in is checked before anything changes"
on "$server" "echo 'ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQC7 x' > /tmp/rsa.pub
  { cat /tmp/link.pub; cat /tmp/link.pub; } > /tmp/two.pub
  echo \"command=\\\"/bin/sh\\\" \$(cat /tmp/link.pub)\" > /tmp/options.pub"
refuses "a key of another kind is not taken" link --check --key /tmp/rsa.pub --from "$pc_ip"
refuses "two keys are not taken" link --check --key /tmp/two.pub --from "$pc_ip"
refuses "a key line with options of its own is not taken" link --check --key /tmp/options.pub --from "$pc_ip"
refuses "an address that is not one is not taken" link --check --key /tmp/link.pub --from "10.203.66.3/24"
passes "the key and the address are taken" link --check --key /tmp/link.pub --from "$pc_ip"

echo "# a dry run changes nothing"
link --key /tmp/link.pub --from "$pc_ip" >/dev/null
refuses "after a dry run there is no sign-in" on "$server" "id cosmicbridge"

echo "# a configuration sshd rejects is taken back, and sshd is not reloaded"
before="$(on "$server" "ls /etc/ssh/sshd_config.d")"
refuses "a port that is none is refused" on "$server" \
  "COSMIC_BRIDGE_LINK_PORT=99999 bridge-link.sh --apply --key /tmp/link.pub --from $pc_ip"
refuses "a change sshd rejects fails" on "$server" \
  "touch /tmp/reject; COSMIC_SSHD_RELOAD='touch /tmp/reloaded' bridge-link.sh --apply --key /tmp/link.pub --from $pc_ip"
on "$server" "rm -f /tmp/reject"
step "the sshd configuration is as it was" "$before" "$(on "$server" "ls /etc/ssh/sshd_config.d")"
refuses "and sshd was not reloaded" on "$server" "test -e /tmp/reloaded"
passes "sshd still accepts its configuration" on "$server" "sshd -t"
link --apply --remove >/dev/null 2>&1  # the sign-in that attempt had already made

echo "# set up"
admin_before="$(on "$server" "sshd -T -C user=root,host=x,addr=192.0.2.1,laddr=192.0.2.2,lport=22 | sort")"
passes "the link is set up" link --apply --key /tmp/link.pub --from "$pc_ip"
passes "a second run changes nothing" on "$server" \
  "COSMIC_SSHD_RELOAD='touch /tmp/reloaded' bridge-link.sh --apply --key /tmp/link.pub --from $pc_ip && ! test -e /tmp/reloaded"
passes "the server reports the link as it must be" link --verify
step "what sshd applies to the admin is unchanged" "$admin_before" \
  "$(on "$server" "sshd -T -C user=root,host=x,addr=192.0.2.1,laddr=192.0.2.2,lport=22 | sort")"
sleep 1

echo "# the one thing the link may do"
step "the trading computer reaches the relay through the link" relay "$(through "$pc" link_key cosmicbridge "$port")"

echo "# the bridge's own code keeps the tunnel (bridge/cosmic_mt5_bridge.py, as on the trading computer)"
docker cp bridge/cosmic_mt5_bridge.py "$pc:/root/cosmic_mt5_bridge.py" >/dev/null
kept="$(on "$pc" "cd /root && SERVER=$server_ip python3 -" <<'EOF'
import os
import pathlib
import subprocess
import threading
import time

import cosmic_mt5_bridge as b

settings = {"host": os.environ["SERVER"], "user": "cosmicbridge", "key_file": "link_key",
            "known_hosts_file": "known"}
link = b.Link(b.Direct.load(pathlib.Path("/root"), settings))
stop = threading.Event()
thread = threading.Thread(target=link.run, args=(stop,), daemon=True)
thread.start()


def up(seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while link.port() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    return link.port() is not None


config = b.Config("https://portal.invalid", "hfm-demo", "terminal64.exe", ("XAUUSD",), "token")
post = b.two_ways(
    link,
    b.direct_post(config, link, "/bridge/v1/fast", 3.0),
    lambda payload: {"commands": [], "path": "the internet"},
)
first = post({"n": 1}) if up(10) else {}
started = time.monotonic()
for n in range(100):
    post({"n": n})
each_ms = (time.monotonic() - started) * 10
subprocess.run(["pkill", "-x", "ssh"], check=False)  # the tunnel dies under the bridge
during = post({"n": 2})  # the same request goes over the internet at once
again = post({"n": 3}) if up(20) else {}  # and the tunnel comes back by itself
stop.set()
thread.join(timeout=10)
left = subprocess.run(["pgrep", "-x", "ssh"], capture_output=True, check=False).returncode == 0
print(first.get("via"), first.get("path"), during.get("via"), again.get("via"),
      "tunnel-left" if left else "tunnel-ended", f"{each_ms:.1f}ms")
EOF
)"
step "a request on the tunnel, the internet path when it dies, the tunnel back by itself, and ended with the bridge" \
  "direct /bridge/v1/fast internet direct tunnel-ended" "${kept% *}"
echo "      a request through the tunnel took ${kept##* } here (one machine, no network between)"

echo "# everything else is refused"
step "another port on the server's loopback (the admin's relay)" refused "$(through "$pc" link_key cosmicbridge 8743)"
step "the server's own sshd through the link" refused "$(through "$pc" link_key cosmicbridge 22)"
refuses "a command" on "$pc" "$(ssh_as link_key cosmicbridge) id"
refuses "a shell" on "$pc" "$(ssh_as link_key cosmicbridge -tt) </dev/null"
refuses "copying a file (sftp)" on "$pc" "echo 'ls' | sftp -F none -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/root/known -i /root/link_key cosmicbridge@$server_ip"
refuses "a port opened on the server towards the trading computer (-R)" on "$pc" \
  "$(ssh_as link_key cosmicbridge -N -T -R 127.0.0.1:9100:127.0.0.1:22) >/dev/null 2>&1 & t=\$!; sleep 1.5
   kill -0 \$t; r=\$?; kill \$t 2>/dev/null; exit \$r"
step "a proxy to anywhere (-D) reaches nothing" refused "$(on "$pc" \
  "$(ssh_as link_key cosmicbridge -N -T -D 127.0.0.1:9200) & t=\$!; sleep 1.5
   curl -s --max-time 3 --socks5 127.0.0.1:9200 http://127.0.0.1:8743/ || echo refused; kill \$t 2>/dev/null")"
step "the same key from another computer" refused "$(through "$other" link_key cosmicbridge "$port")"
step "another key from the trading computer" refused "$(through "$pc" other_key cosmicbridge "$port")"
step "a server showing another key is not trusted" refused "$(on "$pc" \
  "echo \"$server_ip \$(cut -d' ' -f1,2 /root/other_key.pub)\" > /root/wrong
   ssh -F none -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/root/wrong -o ExitOnForwardFailure=yes -o LogLevel=ERROR -i /root/link_key -N -T -L 127.0.0.1:9000:127.0.0.1:$port cosmicbridge@$server_ip & t=\$!
   sleep 1.5; curl -s --max-time 3 http://127.0.0.1:9000/ || echo refused; kill \$t 2>/dev/null")"

echo "# the admin's own sign-in is as before"
step "the admin still reaches the admin's relay" admin "$(through "$pc" admin_key root 8743)"
step "and not the bridge's (it was never the admin's)" refused "$(through "$pc" admin_key root "$port")"

echo "# a changed file is noticed"
on "$server" "sed -i 's/PermitTTY no/PermitTTY yes/' /etc/ssh/sshd_config.d/99-cosmic-bridge-link.conf"
refuses "the check fails on an edited rule" link --verify
passes "and a run puts it right" link --apply --key /tmp/link.pub --from "$pc_ip"
passes "the server reports the link as it must be again" link --verify

echo "# a new address for the trading computer"
passes "the link is moved to another address" link --apply --key /tmp/link.pub --from "$other_ip"
step "the old address is refused" refused "$(through "$pc" link_key cosmicbridge "$port")"
step "the new one reaches the relay" relay "$(through "$other" link_key cosmicbridge "$port")"

echo "# removal"
on "$other" "$(ssh_as link_key cosmicbridge -N -T -L "127.0.0.1:9000:127.0.0.1:$port") >/dev/null 2>&1 & sleep 1.5"
step "a connection of the link is open" relay "$(on "$other" "curl -s --max-time 3 http://127.0.0.1:9000/ || echo refused")"
passes "the link is removed" link --apply --remove
sleep 1
step "an open connection of the link was ended" refused "$(on "$other" "curl -s --max-time 3 http://127.0.0.1:9000/ || echo refused")"
step "the link no longer reaches the relay" refused "$(through "$other" link_key cosmicbridge "$port")"
refuses "the sign-in is gone" on "$server" "id cosmicbridge"
passes "nothing of it is left, and sshd accepts its configuration" on "$server" \
  "! test -e /etc/ssh/cosmic-bridge-link.keys && ! test -e /etc/ssh/sshd_config.d/99-cosmic-bridge-link.conf && sshd -t"
step "the admin still reaches the admin's relay" admin "$(through "$pc" admin_key root 8743)"

echo "# a release (deploy/deploy.sh as root, with a stand-in for docker that notes what it is asked)"
# Two roots as on the VM: production and staging, each with this checkout's deploy/ and the
# files deploy.sh asks for before it does anything.
for root in /srv/cosmic /srv/cosmic-staging; do
  on "$server" "mkdir -p $root/src $root/config $root/secrets $root/backups
    echo COSMIC_ENV=PAPER_TRADING > $root/config/cosmic.env; echo recipient > $root/config/backup-recipients.txt
    printf 'COSMIC_IMAGE_TAG=old1234\nCOMPOSE_PROFILES=\n' > $root/config/compose.env
    for s in db_superuser_password db_owner_password db_app_password secret_key metrics_token; do echo x > $root/secrets/\$s; done
    for s in groq_api_key gemini_api_key openrouter_api_key telegram_bot_token tunnel_token binance_testnet_api_key \
      binance_testnet_private_key hfm_bridge_token hfm_bridge2_token hfm_bridge3_token; do : > $root/secrets/\$s; done"
  docker cp deploy "$server:$root/src/deploy" >/dev/null
done
on "$server" "echo COSMIC_COMPOSE_OVERLAY=deploy/compose.staging.yaml >> /srv/cosmic-staging/config/compose.env
  printf '%s\n' '#!/bin/sh' 'echo \"\$*\" >> /tmp/docker.log' \
    'case \"\$*\" in *\"config --format json\"*) echo \"{\\\"name\\\": \\\"cosmictrading\\\"}\";; esac' \
    > /usr/local/bin/docker && chmod +x /usr/local/bin/docker"
release() { # root [what is handed in beforehand, as shell] → deploy.sh's exit status
  local root="$1"
  on "$server" "rm -f /tmp/docker.log; mkdir -p $root/src/var; ${2:-true}
    COSMIC_SSHD_RELOAD='pkill -HUP -o -x sshd' $root/src/deploy/deploy.sh --untested new5678 >/tmp/deploy.log 2>&1"
}
asked() { on "$server" "{ cat /tmp/docker.log 2>/dev/null || true; } | grep -c -- '$1' || true"; }  # how often
up_with_link="-f /srv/cosmic/src/deploy/compose.bridge-link.yaml up -d"

passes "a release without the link" release /srv/cosmic
step "the stack is started without the relay" 0 "$(asked "$up_with_link")"
refuses "and nothing of the link is on the machine" on "$server" "id cosmicbridge"

refuses "a key handed in without an address stops the release" release /srv/cosmic \
  "cp /tmp/link.pub /srv/cosmic/src/var/bridge-link.pub"
step "before anything was started" 0 "$(asked ' up -d')"
refuses "a key that is none stops the release" release /srv/cosmic \
  "cp /tmp/rsa.pub /srv/cosmic/src/var/bridge-link.pub; echo $pc_ip > /srv/cosmic/src/var/bridge-link.from"
step "before anything was started" 0 "$(asked ' up -d')"

passes "a release with the key and the address handed in" release /srv/cosmic \
  "cp /tmp/link.pub /srv/cosmic/src/var/bridge-link.pub; echo $pc_ip > /srv/cosmic/src/var/bridge-link.from"
step "the stack is started with the relay" 1 "$(asked "$up_with_link")"
step "what was handed in is kept in production's config, root's" "root 644 root 644" \
  "$(on "$server" "stat -c '%U %a' /srv/cosmic/config/bridge-link.pub /srv/cosmic/config/bridge-link.from | tr '\n' ' ' | sed 's/ \$//'")"
refuses "and no longer lies in the checkout" on "$server" "ls /srv/cosmic/src/var/bridge-link.*"
passes "the server reports the link as it must be" link --verify
sleep 1
step "the trading computer reaches the relay through the link" relay "$(through "$pc" link_key cosmicbridge "$port")"

passes "the next release, with nothing handed in" release /srv/cosmic
step "keeps the relay" 1 "$(asked "$up_with_link")"
passes "and the link" link --verify

passes "a staging release with a key lying in its checkout" release /srv/cosmic-staging \
  "cp /tmp/link.pub /srv/cosmic-staging/src/var/bridge-link.pub; echo $other_ip > /srv/cosmic-staging/src/var/bridge-link.from"
step "starts no relay" 0 "$(asked 'compose.bridge-link.yaml')"
step "and leaves production's link as it is" relay "$(through "$pc" link_key cosmicbridge "$port")"

passes "a release with the removal asked for" release /srv/cosmic ": > /srv/cosmic/src/var/bridge-link.remove"
step "the stack is started without the relay" 0 "$(asked "$up_with_link")"
sleep 1
step "the link no longer reaches the relay" refused "$(through "$pc" link_key cosmicbridge "$port")"
passes "and nothing of it is left" on "$server" \
  "! id cosmicbridge && ! ls /srv/cosmic/config/bridge-link.* /srv/cosmic/src/var/bridge-link.* /etc/ssh/cosmic-bridge-link.keys /etc/ssh/sshd_config.d/99-cosmic-bridge-link.conf 2>/dev/null | grep -q ."

if [ "$fails" -eq 0 ]; then echo "DRILL PASSED"; else echo "$fails step(s) failed"; exit 1; fi
