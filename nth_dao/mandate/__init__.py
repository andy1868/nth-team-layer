"""nth_dao.mandate - AP2-shape signed authorisation primitives.

Three W3C Verifiable Credentials that together form a complete
agentic-commerce transaction:

  IntentMandate (T-1, this release)
      The issuer (a DAO or human) authorises a specific agent to act
      within bounded parameters: spend limit, allowed counterparties,
      expiry, allowed settlement methods.

  CartMandate (T-2, next)
      A counterparty's offer that binds to a specific IntentMandate
      digest. The agent can accept it only if its constraints are
      satisfied.

  PaymentMandate (T-3)
      The DAO accepts the cart and authorises settlement against the
      chosen rail (x402, AP2 card, manual, etc.).

All three are W3C VC 2.0 dicts signed with Ed25519Signature2020 over
canonical JSON, so external verifiers (AP2 facilitators, x402 gateways,
ERC-8004 indexers) can validate them without trusting our store.

See: docs/architecture/NTH_DAO_ROADMAP_2026_2028.md Pillar D for the
end-to-end design rationale.
"""

from .intent import (
    INTENT_CONTEXT,
    INTENT_TYPE,
    PROOF_TYPE,
    PROOF_PURPOSE,
    build_intent_mandate,
    intent_mandate_digest,
    is_intent_expired,
    sign_intent_mandate,
    verify_intent_mandate,
)

__all__ = [
    "INTENT_CONTEXT",
    "INTENT_TYPE",
    "PROOF_TYPE",
    "PROOF_PURPOSE",
    "build_intent_mandate",
    "intent_mandate_digest",
    "is_intent_expired",
    "sign_intent_mandate",
    "verify_intent_mandate",
]
