# ADR-020: Observability without a metrics stack initially
- **Status:** Accepted · **Date:** 2026-10-10
- **Decision:** structured JSON logs with redaction and correlation IDs, health endpoints, worker heartbeats, incidents/alerts in the database, and a token-protected Prometheus text endpoint; no Prometheus/Grafana/Loki containers until the resource budget is measured on the VM.
