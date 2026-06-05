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
    PROOF_PURPOSE as INTENT_PROOF_PURPOSE,
    build_intent_mandate,
    intent_mandate_digest,
    is_intent_expired,
    sign_intent_mandate,
    verify_intent_mandate,
)
from .cart import (
    CART_CONTEXT,
    CART_TYPE,
    PROOF_PURPOSE as CART_PROOF_PURPOSE,
    build_cart_mandate,
    cart_mandate_digest,
    cart_satisfies_intent,
    is_cart_expired,
    sign_cart_mandate,
    verify_cart_mandate,
)
from .payment import (
    PAYMENT_CONTEXT,
    PAYMENT_TYPE,
    PROOF_PURPOSE as PAYMENT_PROOF_PURPOSE,
    build_payment_mandate,
    complete_triad_chain,
    is_payment_expired,
    payment_mandate_digest,
    payment_satisfies_cart,
    sign_payment_mandate,
    verify_payment_mandate,
)

__all__ = [
    # IntentMandate (T-1)
    "INTENT_CONTEXT",
    "INTENT_TYPE",
    "INTENT_PROOF_PURPOSE",
    "PROOF_TYPE",
    "build_intent_mandate",
    "intent_mandate_digest",
    "is_intent_expired",
    "sign_intent_mandate",
    "verify_intent_mandate",
    # CartMandate (T-2)
    "CART_CONTEXT",
    "CART_TYPE",
    "CART_PROOF_PURPOSE",
    "build_cart_mandate",
    "cart_mandate_digest",
    "cart_satisfies_intent",
    "is_cart_expired",
    "sign_cart_mandate",
    "verify_cart_mandate",
    # PaymentMandate (T-3)
    "PAYMENT_CONTEXT",
    "PAYMENT_TYPE",
    "PAYMENT_PROOF_PURPOSE",
    "build_payment_mandate",
    "complete_triad_chain",
    "is_payment_expired",
    "payment_mandate_digest",
    "payment_satisfies_cart",
    "sign_payment_mandate",
    "verify_payment_mandate",
]
