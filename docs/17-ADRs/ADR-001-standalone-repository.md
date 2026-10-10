# ADR-001: Standalone Git repository for the project
- **Status:** Accepted · **Date:** 2026-10-10
- **Context:** `/home/prophet/trading` was inside a commitless Git repository rooted at the home directory, which tracks nothing but would capture any `git add .` of personal files and secrets (`.ssh`, `.aws`, tokens).
- **Decision:** initialise a separate repository in `/home/prophet/trading` (branch `main`); never commit to the home-directory repository; no remote is configured until the owner provides one.
- **Consequences:** project history is isolated; the home repository sees `trading/` as a nested repository and is otherwise untouched.
