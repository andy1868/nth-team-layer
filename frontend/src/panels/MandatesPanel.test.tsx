import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getMandate, listMandates, verifyMandate } from "../api";
import type { MandateListing } from "../types";
import { MandatesPanel } from "./MandatesPanel";

vi.mock("../api", () => ({
  getMandate: vi.fn(),
  listMandates: vi.fn(),
  verifyMandate: vi.fn()
}));

const mockedListMandates = vi.mocked(listMandates);
const mockedGetMandate = vi.mocked(getMandate);
const mockedVerifyMandate = vi.mocked(verifyMandate);

const LISTING: MandateListing = {
  intents: [
    {
      kind: "intent",
      digest: "intent-digest-1",
      issuer: "did:key:zIssuer",
      agent: "did:key:zAgent",
      purpose: "Buy compute",
      max_amount: { value: "100.00", currency: "USDC" },
      expires_at: "2026-07-01T00:00:00+00:00",
      expired: false,
      allowed_counterparties: ["did:key:zSeller"],
      allowed_settlement_methods: ["x402:usdc"]
    }
  ],
  carts: [
    {
      kind: "cart",
      digest: "cart-digest-1",
      issuer: "did:key:zSeller",
      intent_digest: "intent-digest-1",
      total: { value: "50.00", currency: "USDC" },
      settlement_methods: ["x402:usdc"],
      expires_at: "2026-06-30T00:00:00+00:00",
      expired: false,
      line_item_count: 1
    }
  ],
  payments: [
    {
      kind: "payment",
      digest: "payment-digest-1",
      issuer: "did:key:zIssuer",
      cart_digest: "cart-digest-1",
      payee: "did:key:zSeller",
      settlement_choice: "x402:usdc",
      issued_at: "2026-06-01T00:00:00+00:00",
      expires_at: "2026-06-30T00:00:00+00:00",
      expired: false
    }
  ]
};

const INTENT_BODY = {
  id: "urn:nth:intent:intent-digest-1",
  type: ["VerifiableCredential", "IntentMandate"]
};

const CART_BODY = {
  id: "urn:nth:cart:cart-digest-1",
  type: ["VerifiableCredential", "CartMandate"],
  credentialSubject: {
    intent_mandate_digest: "intent-digest-1"
  }
};

const PAYMENT_BODY = {
  id: "urn:nth:payment:payment-digest-1",
  type: ["VerifiableCredential", "PaymentMandate"]
};

describe("MandatesPanel", () => {
  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    vi.clearAllMocks();
    mockedListMandates.mockResolvedValue(LISTING);
    mockedVerifyMandate.mockResolvedValue({
      ok: true,
      reason: "",
      checks: [{ name: "complete_triad", ok: true }]
    });
  });

  it("verifies payment mandates with the bound cart and intent", async () => {
    mockedGetMandate.mockImplementation(async (kind, digest) => {
      if (kind === "payment" && digest === "payment-digest-1") return PAYMENT_BODY;
      if (kind === "cart" && digest === "cart-digest-1") return CART_BODY;
      if (kind === "intent" && digest === "intent-digest-1") return INTENT_BODY;
      throw new Error(`unexpected mandate lookup ${kind}:${digest}`);
    });

    render(<MandatesPanel actorId="actor-a" />);

    const verifyPayment = await screen.findByRole("button", {
      name: "Verify payment payment-digest-1"
    });
    fireEvent.click(verifyPayment);

    await waitFor(() => expect(mockedVerifyMandate).toHaveBeenCalledTimes(1));
    expect(mockedVerifyMandate).toHaveBeenCalledWith({
      kind: "payment",
      mandate: PAYMENT_BODY,
      againstCart: CART_BODY,
      againstIntent: INTENT_BODY,
      actorId: "actor-a"
    });
  });

  it("refuses payment verification when the cart has no intent digest", async () => {
    mockedGetMandate.mockImplementation(async (kind, digest) => {
      if (kind === "payment" && digest === "payment-digest-1") return PAYMENT_BODY;
      if (kind === "cart" && digest === "cart-digest-1") {
        return { ...CART_BODY, credentialSubject: {} };
      }
      throw new Error(`unexpected mandate lookup ${kind}:${digest}`);
    });

    render(<MandatesPanel actorId="actor-a" />);

    const verifyPayment = await screen.findByRole("button", {
      name: "Verify payment payment-digest-1"
    });
    fireEvent.click(verifyPayment);

    await screen.findByText(/Payment verification requires/);
    expect(mockedVerifyMandate).not.toHaveBeenCalled();
  });
});
