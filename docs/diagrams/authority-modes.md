# Authority modes (V6 §1804, ADR-010)

```mermaid
stateDiagram-v2
  direction LR
  [*] --> RESEARCH_ONLY : default on first start
  RESEARCH_ONLY --> PAPER_TRADING : ADMINISTER_SYSTEM + step-up MFA + reason
  PAPER_TRADING --> RESEARCH_ONLY : any admin (more restrictive)
  PAPER_TRADING --> APPROVAL_REQUIRED : LIVE gates A–I passed + live adapter + owner activation (unreachable in this build)
  APPROVAL_REQUIRED --> STRATEGY_AUTHORIZED : ENABLE_STRATEGY_AUTHORITY + mandate (disabled by default)
  STRATEGY_AUTHORIZED --> APPROVAL_REQUIRED : revoke / expiry
  APPROVAL_REQUIRED --> PAPER_TRADING : more restrictive
  RESEARCH_ONLY --> PROTECTION_ONLY
  PAPER_TRADING --> PROTECTION_ONLY
  APPROVAL_REQUIRED --> PROTECTION_ONLY : reconciliation failure / operator
  STRATEGY_AUTHORIZED --> PROTECTION_ONLY
  RESEARCH_ONLY --> EMERGENCY_STOP : kill switch
  PAPER_TRADING --> EMERGENCY_STOP : kill switch
  APPROVAL_REQUIRED --> EMERGENCY_STOP : kill switch
  STRATEGY_AUTHORIZED --> EMERGENCY_STOP : kill switch
  PROTECTION_ONLY --> EMERGENCY_STOP : kill switch
  EMERGENCY_STOP --> PROTECTION_ONLY : recovery checklist + reconciliation PASSED + MFA
  PROTECTION_ONLY --> RESEARCH_ONLY : reconciliation PASSED + MFA
  PROTECTION_ONLY --> PAPER_TRADING : reconciliation PASSED + MFA (≤ mode before stop)
```

`APPROVED_EXECUTION` is not a global mode: it is the authority attached to an individual order created from a valid, consumed approval (ADR-010). An environment cap (`COSMIC_MAX_AUTHORITY_MODE`) can only lower authority; if the stored mode exceeds the cap at startup the system enters `PROTECTION_ONLY` and raises an incident.
