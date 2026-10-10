# ADR-019: Encrypted logical backups with public-key encryption and restore drills
- **Status:** Accepted · **Date:** 2026-10-10
- **Decision:** `pg_dump -Fc` + configuration archive, encrypted to an `age` public key (private key held off-VM by the owner); manifest with checksums and audit head; retention 14/8/6; secrets excluded; scripted restore drill into an isolated container.
- **Consequences:** a stolen backup is useless without the owner's key; restore requires the owner's key — the runbook says where it must be kept.
