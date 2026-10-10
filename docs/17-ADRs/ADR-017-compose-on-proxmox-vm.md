# ADR-017: Docker Compose on a dedicated Ubuntu 24.04 VM under Proxmox
- **Status:** Accepted (deployment BLOCKED on B-02) · **Date:** 2026-10-10
- **Decision:** Ubuntu Server 24.04 LTS cloud image; Docker Engine + Compose plugin from Ubuntu packages; images pinned by digest; systemd unit starts the stack on boot; VM `onboot=1`; no Kubernetes.
- **Consequences:** one-command deploy; reproducible locally for verification.
