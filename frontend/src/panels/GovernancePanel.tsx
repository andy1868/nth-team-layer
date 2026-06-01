// QQ-style governance panel — propose policy change, list proposals, sign votes.

import { useEffect, useState } from "react";
import {
  type PolicyProposal,
  type UniqueGroup,
  castSignedVote,
  listProposals,
  prepareProposal,
  publishProposal
} from "../contacts";

interface Props {
  group: UniqueGroup;
  actorPubkeyHex: string;
  sign: (obj: unknown) => Promise<string>;
}

const POLICIES = ["open", "approval", "closed", "voted"] as const;

export function GovernancePanel({ group, actorPubkeyHex, sign }: Props) {
  const [proposals, setProposals] = useState<PolicyProposal[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [newPolicy, setNewPolicy] = useState<typeof POLICIES[number]>(group.policy);
  const [rationale, setRationale] = useState("");

  async function refresh() {
    try {
      setProposals(await listProposals(group.group_id));
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function propose() {
    setError(null);
    try {
      const prep = await prepareProposal({
        groupId: group.group_id,
        actorPubkeyHex,
        newPolicy,
        rationale
      });
      const proposal = { ...prep.unsigned_proposal } as PolicyProposal;
      proposal.proposer_sig = await sign(prep.to_sign);
      await publishProposal(group.group_id, proposal);
      setRationale("");
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function vote(p: PolicyProposal, choice: "yes" | "no" | "abstain") {
    setError(null);
    try {
      const voted_at = new Date().toISOString();
      const sig = await sign({ proposal_id: p.proposal_id, choice, voted_at });
      await castSignedVote(group.group_id, p.proposal_id, {
        voter_pubkey: actorPubkeyHex,
        choice,
        voted_at,
        sig
      });
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  useEffect(() => {
    void refresh();
  }, [group.group_id]);

  return (
    <section className="qq-panel">
      <h2>
        Governance · <code>{group.slug}</code>
      </h2>

      <div className="qq-policy-current">
        Current policy: <strong>{group.policy}</strong> ·{" "}
        {group.member_pubkeys.length} members ·{" "}
        Threshold to pass:{" "}
        <strong>{Math.floor(group.member_pubkeys.length / 2) + 1} yes votes</strong>
      </div>

      <div className="qq-form">
        <h3>Propose change</h3>
        <select
          value={newPolicy}
          onChange={(e) => setNewPolicy(e.target.value as typeof POLICIES[number])}
        >
          {POLICIES.map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
        <input
          placeholder="Why this change?"
          value={rationale}
          onChange={(e) => setRationale(e.target.value)}
        />
        <button onClick={propose} disabled={!rationale}>
          Sign + Submit
        </button>
      </div>

      {error && <p className="qq-flash qq-error">{error}</p>}

      <h3>Open proposals</h3>
      <ul className="qq-proposal-list">
        {proposals.map((p) => {
          const yesCount = p.votes.filter((v) => v.choice === "yes").length;
          return (
            <li key={p.proposal_id} className="qq-proposal">
              <div>
                <strong>{p.proposed_policy}</strong>{" "}
                <small>(was {group.policy})</small>
                {p.rationale && <div className="qq-rationale">{p.rationale}</div>}
                <div className="qq-vote-tally">
                  {yesCount} / {group.member_pubkeys.length} yes ·{" "}
                  {p.resolved?.passed ? "PASSED" : "open"}
                </div>
              </div>
              {!p.resolved?.passed && (
                <div className="qq-vote-buttons">
                  <button onClick={() => vote(p, "yes")}>Yes</button>
                  <button onClick={() => vote(p, "no")}>No</button>
                  <button onClick={() => vote(p, "abstain")}>Abstain</button>
                </div>
              )}
            </li>
          );
        })}
        {proposals.length === 0 && <li className="qq-empty">No open proposals.</li>}
      </ul>
    </section>
  );
}
