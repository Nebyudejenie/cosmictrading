# SSH jump host for remote access through Cloudflare Tunnel (docs/12 §5, "SSH").
# Key-only, no shell, forwarding only to the host's sshd. It never publishes a port.
FROM alpine:3.22@sha256:5291449c3df73caf6ed85e649dec1b9e818b39a5d8c871e97afc13e9cd5e8fa8
RUN apk add --no-cache openssh-server \
 && adduser -D -u 10022 -s /sbin/nologin jump \
 # "!" (locked) would make sshd refuse even key logins; "*" = no password, not locked
 && sed -i 's/^jump:!:/jump:*:/' /etc/shadow \
 && mkdir -p /etc/ssh/keys /etc/ssh/authorized /etc/bastion
COPY sshd_config /etc/ssh/sshd_config
COPY entrypoint.sh /usr/local/bin/bastion-entrypoint
RUN chmod 0555 /usr/local/bin/bastion-entrypoint
EXPOSE 2222
ENTRYPOINT ["/usr/local/bin/bastion-entrypoint"]
