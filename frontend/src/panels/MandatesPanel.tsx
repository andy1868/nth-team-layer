// v0.10 T-9: Mandate sidebar - lists IntentMandates the DAO has
// issued, CartMandates received from counterparties, and
// PaymentMandates authorised. Each row carries a Verify button that
// triggers the server-side signature + binding gate.
//
// Why per-row Verify (rather than auto-verifying on load)?
//   The full body fetch + signature check is O(KB), not free at scale;
//   showing the cached digest immediately and verifying on demand
//   keeps the sidebar snappy when the store has hundreds of rows.

import { useEffect, useState, type ReactNode } from "react";
import { getMandate, listMandates, verifyMandate } from "../api";
import type {
  CartMandateSummary,
  IntentMandateSummary,
  MandateKind,
  MandateListing,
  MandateVerifyResult,
  PaymentMandateSummary
} from "../types";

interface Props {
  /**
   * Wallet did:key. Currently informational only - the sidebar is
   * read-only at T-9 (issuing new IntentMandates from the browser
   * lands in a later sprint). Threading this through now keeps the
   * prop shape stable for that work.
   */
  walletDid?: string;
}

type VerifyState =
  | { status: "idle" }
  | { status: "running" }
  | { status: "done"; result: MandateVerifyResult };

type RowVerifyMap = Record<string, VerifyState>;

const EMPTY_LISTING: MandateListing = {
  intents: [],
  carts: [],
  payments: []
};

export function MandatesPanel(_: Props) {
  const [listing, setListing] = useState<MandateListing>(EMPTY_LISTING);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [verifyState, setVerifyState] = useState<RowVerifyMap>({});

  async function refresh() {
    setLoading(true);
    setError(null);
    try {
      setListing(await listMandates());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  /**
   * Verify a single row.
   *
   * For Carts and Payments, fetch the binding mandate first and
   * pass it to /verify so the binding gate runs. This is the same
   * shape a settlement adapter would use: never trust a Cart
   * without binding it to the Intent it claims to fulfil.
   */
  async function verifyRow(
    kind: MandateKind,
    digest: string,
    bindingIntentDigest?: string,
    bindingCartDigest?: string
  ) {
    setVerifyState((prev) => ({ ...prev, [digest]: { status: "running" } }));
    try {
      const mandate = await getMandate(kind, digest);
      const params: Parameters<typeof verifyMandate>[0] = { kind, mandate };
      if (kind === "cart" && bindingIntentDigest) {
        try {
          params.againstIntent = await getMandate("intent", bindingIntentDigest);
        } catch {
          // Intent not in store - fall back to signature-only check
        }
      }
      if (kind === "payment" && bindingCartDigest) {
        try {
          params.againstCart = await getMandate("cart", bindingCartDigest);
        } catch {
          // Cart not in store - fall back to signature-only check
        }
      }
      const result = await verifyMandate(params);
      setVerifyState((prev) => ({
        ...prev,
        [digest]: { status: "done", result }
      }));
    } catch (e) {
      setVerifyState((prev) => ({
        ...prev,
        [digest]: {
          status: "done",
          result: {
            ok: false,
            reason: (e as Error).message,
            checks: []
          }
        }
      }));
    }
  }

  const total =
    listing.intents.length + listing.carts.length + listing.payments.length;

  return (
    <div className="qq-panel mandate-panel">
      <header className="mandate-header">
        <h3>Mandates</h3>
        <button
          type="button"
          onClick={() => void refresh()}
          disabled={loading}
        >
          {loading ? "Refreshing..." : "Refresh"}
        </button>
      </header>

      {error && <p className="qq-flash qq-error">{error}</p>}

      {!loading && total === 0 && (
        <p className="qq-empty">
          No mandates yet. The DAO has not issued any IntentMandates,
          received any CartMandates, or authorised any Payments.
        </p>
      )}

      <MandateSection
        title="Issued intents"
        rows={listing.intents}
        renderRow={(row) => (
          <IntentRow
            key={row.digest}
            row={row}
            verify={verifyState[row.digest] ?? { status: "idle" }}
            onVerify={() => verifyRow("intent", row.digest)}
          />
        )}
      />
      <MandateSection
        title="Pending carts"
        rows={listing.carts}
        renderRow={(row) => (
          <CartRow
            key={row.digest}
            row={row}
            verify={verifyState[row.digest] ?? { status: "idle" }}
            onVerify={() =>
              verifyRow("cart", row.digest, row.intent_digest, undefined)
            }
          />
        )}
      />
      <MandateSection
        title="Completed payments"
        rows={listing.payments}
        renderRow={(row) => (
          <PaymentRow
            key={row.digest}
            row={row}
            verify={verifyState[row.digest] ?? { status: "idle" }}
            onVerify={() =>
              verifyRow("payment", row.digest, undefined, row.cart_digest)
            }
          />
        )}
      />
    </div>
  );
}

function MandateSection<T>(props: {
  title: string;
  rows: T[];
  renderRow: (row: T) => ReactNode;
}) {
  return (
    <section className="mandate-section">
      <h4>
        {props.title} <span className="mandate-count">({props.rows.length})</span>
      </h4>
      {props.rows.length === 0 ? (
        <p className="qq-empty mandate-empty">None.</p>
      ) : (
        <ul className="mandate-list">{props.rows.map(props.renderRow)}</ul>
      )}
    </section>
  );
}

function IntentRow({
  row,
  verify,
  onVerify
}: {
  row: IntentMandateSummary;
  verify: VerifyState;
  onVerify: () => void;
}) {
  return (
    <li className={`mandate-row ${row.expired ? "mandate-expired" : ""}`}>
      <div className="mandate-row-head">
        <span className="mandate-headline">
          {row.max_amount.value} {row.max_amount.currency}
        </span>
        <DigestBadge digest={row.digest} />
        {row.expired && <span className="mandate-tag mandate-tag-expired">expired</span>}
      </div>
      <div className="mandate-row-body">
        <p className="mandate-purpose">{row.purpose}</p>
        <p className="qq-did">authorises agent {short(row.agent)}</p>
        <p className="qq-did">issuer {short(row.issuer)}</p>
        <p className="mandate-meta">
          rails: {row.allowed_settlement_methods.join(", ") || "none"}
        </p>
        <p className="mandate-meta">expires {row.expires_at}</p>
      </div>
      <VerifyControl verify={verify} onVerify={onVerify} />
    </li>
  );
}

function CartRow({
  row,
  verify,
  onVerify
}: {
  row: CartMandateSummary;
  verify: VerifyState;
  onVerify: () => void;
}) {
  return (
    <li className={`mandate-row ${row.expired ? "mandate-expired" : ""}`}>
      <div className="mandate-row-head">
        <span className="mandate-headline">
          {row.total.value} {row.total.currency}
        </span>
        <DigestBadge digest={row.digest} />
        {row.expired && <span className="mandate-tag mandate-tag-expired">expired</span>}
      </div>
      <div className="mandate-row-body">
        <p className="qq-did">seller {short(row.issuer)}</p>
        <p className="mandate-meta">
          binds intent {short(row.intent_digest)}
        </p>
        <p className="mandate-meta">
          rails offered: {row.settlement_methods.join(", ") || "none"}
        </p>
        <p className="mandate-meta">
          {row.line_item_count} line item{row.line_item_count === 1 ? "" : "s"}
          {" - expires "}
          {row.expires_at}
        </p>
      </div>
      <VerifyControl verify={verify} onVerify={onVerify} />
    </li>
  );
}

function PaymentRow({
  row,
  verify,
  onVerify
}: {
  row: PaymentMandateSummary;
  verify: VerifyState;
  onVerify: () => void;
}) {
  return (
    <li className={`mandate-row ${row.expired ? "mandate-expired" : ""}`}>
      <div className="mandate-row-head">
        <span className="mandate-headline">{row.settlement_choice}</span>
        <DigestBadge digest={row.digest} />
        {row.expired && <span className="mandate-tag mandate-tag-expired">expired</span>}
      </div>
      <div className="mandate-row-body">
        <p className="qq-did">payee {short(row.payee)}</p>
        <p className="qq-did">issuer {short(row.issuer)}</p>
        <p className="mandate-meta">binds cart {short(row.cart_digest)}</p>
        <p className="mandate-meta">
          authorised {row.issued_at} - window {row.expires_at}
        </p>
      </div>
      <VerifyControl verify={verify} onVerify={onVerify} />
    </li>
  );
}

function VerifyControl({
  verify,
  onVerify
}: {
  verify: VerifyState;
  onVerify: () => void;
}) {
  if (verify.status === "running") {
    return <p className="mandate-verify mandate-verify-running">verifying...</p>;
  }
  if (verify.status === "done") {
    const cls = verify.result.ok ? "mandate-verify-ok" : "mandate-verify-bad";
    return (
      <div className={`mandate-verify ${cls}`}>
        <strong>{verify.result.ok ? "OK" : "FAIL"}</strong>
        {verify.result.reason && <span> - {verify.result.reason}</span>}
        {verify.result.checks.length > 0 && (
          <details className="mandate-verify-detail">
            <summary>{verify.result.checks.length} checks</summary>
            <ul>
              {verify.result.checks.map((c, i) => (
                <li key={i}>
                  {c.ok ? "[OK]" : "[FAIL]"} {c.name}
                  {c.reason ? ` - ${c.reason}` : ""}
                </li>
              ))}
            </ul>
          </details>
        )}
        <button type="button" onClick={onVerify}>
          re-verify
        </button>
      </div>
    );
  }
  return (
    <button type="button" className="mandate-verify-btn" onClick={onVerify}>
      Verify
    </button>
  );
}

function DigestBadge({ digest }: { digest: string }) {
  return (
    <code className="mandate-digest" title={digest}>
      {short(digest)}
    </code>
  );
}

function short(value: string): string {
  if (!value) return "(none)";
  if (value.length <= 16) return value;
  return `${value.slice(0, 8)}...${value.slice(-6)}`;
}
