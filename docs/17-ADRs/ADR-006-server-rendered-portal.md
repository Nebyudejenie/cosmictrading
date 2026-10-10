# ADR-006: Server-rendered portal without a SPA or JavaScript framework
- **Status:** Accepted · **Date:** 2026-10-10
- **Decision:** Jinja2 templates, one CSS file, HTML forms (POST-redirect-GET), inline SVG charts rendered on the server, strict CSP with no inline script, no CDN assets.
- **Reason:** smallest attack surface for a capital-control console; no secrets or business logic in the browser; no build toolchain; accessible by default.
- **Consequences:** less interactivity (pages refresh); acceptable for an approval/monitoring console. A Mini App or SPA can be added later behind the same JSON API.
