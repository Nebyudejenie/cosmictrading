# Order state machine (V6 §1812)

Broker-grounded: a transition into `ACKNOWLEDGED`, `PARTIALLY_FILLED`, `FILLED`, `REJECTED`, `CANCELLED`, `EXPIRED` requires a broker response or reconciliation evidence. Local-only transitions are marked (local).

```mermaid
stateDiagram-v2
  [*] --> CREATED : intent consumed (local)
  CREATED --> VALIDATED : schema + precision OK (local)
  CREATED --> REJECTED : validation failed (local, never sent)
  VALIDATED --> AUTHORIZED : approval consumed + risk revalidated (local)
  VALIDATED --> REJECTED : risk veto (local, never sent)
  AUTHORIZED --> SUBMITTING : persisted before transmission (local)
  SUBMITTING --> SUBMITTED : transport returned (local)
  SUBMITTING --> UNKNOWN_ORDER_STATE : timeout / crash / ambiguous
  SUBMITTED --> ACKNOWLEDGED : broker accepted
  SUBMITTED --> REJECTED : broker rejected
  SUBMITTED --> UNKNOWN_ORDER_STATE : no confirmation in time
  ACKNOWLEDGED --> PARTIALLY_FILLED : broker fill
  ACKNOWLEDGED --> FILLED : broker fill (complete)
  PARTIALLY_FILLED --> PARTIALLY_FILLED : more fills
  PARTIALLY_FILLED --> FILLED : broker fill (complete)
  ACKNOWLEDGED --> CANCEL_PENDING : cancel requested
  PARTIALLY_FILLED --> CANCEL_PENDING : cancel requested
  CANCEL_PENDING --> CANCELLED : broker confirmed
  CANCEL_PENDING --> FILLED : fill won the race
  CANCEL_PENDING --> PARTIALLY_FILLED : partial fill during cancel
  ACKNOWLEDGED --> REPLACE_PENDING : replace requested (within mandate)
  REPLACE_PENDING --> ACKNOWLEDGED : broker confirmed replacement
  ACKNOWLEDGED --> EXPIRED : broker expired (TIF)
  PARTIALLY_FILLED --> EXPIRED : broker expired remainder
  UNKNOWN_ORDER_STATE --> ACKNOWLEDGED : reconciled
  UNKNOWN_ORDER_STATE --> PARTIALLY_FILLED : reconciled
  UNKNOWN_ORDER_STATE --> FILLED : reconciled
  UNKNOWN_ORDER_STATE --> REJECTED : reconciled (broker has no such order)
  UNKNOWN_ORDER_STATE --> CANCELLED : reconciled
  FILLED --> [*]
  REJECTED --> [*]
  CANCELLED --> [*]
  EXPIRED --> [*]
```

`UNKNOWN_ORDER_STATE → REJECTED` ("not found at broker") is allowed only after the broker's order lookup by client order ID returns a definitive not-found **and** a grace period has elapsed; otherwise the order stays unknown and is escalated.
