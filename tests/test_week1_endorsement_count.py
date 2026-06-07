"""Week-1 Task 4 regression: search results carry endorsement_count.

The dashboard wants to badge each search result with a small "🟢 12
endorsements" indicator so the operator has a fast trust signal without
clicking into a detail view. We expose this from
``/api/agents/search`` for the ``group`` source (where we have the raw
pubkey natively); other sources omit the field (front-end treats
missing as 0).

Pinned invariants:
  * group-source row carries ``endorsement_count`` int (0 if no WoT
    endorsements for that pubkey)
  * non-zero counts surface when the WoT JSONL actually contains a
    matching subject_pubkey
  * a corrupt / unreadable WoT file does NOT break the search
    endpoint (degrades to count = 0)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nth_dao.web import create_app


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path))


def _seed_group(tmp_path: Path, pk: str) -> None:
    from nth_dao.group_registry import GroupPolicy, GroupRegistry

    registry = GroupRegistry(tmp_path)
    path = registry._path_for_slug("qa-team")
    path.write_text(
        json.dumps({
            "group_id": "grp-qa",
            "slug": "qa-team",
            "display_name": "QA Team",
            "description": "",
            "policy": GroupPolicy.OPEN.value,
            "founder_pubkey": pk,
            "member_pubkeys": [pk],
            "admin_pubkeys": [pk],
            "signer_pubkey": pk,
            "sig": "fake-for-test",
            "created_at": "2026-06-07T00:00:00",
            "updated_at": "2026-06-07T00:00:00",
            "metadata": {},
        }),
        encoding="utf-8",
    )


def _seed_endorsements(tmp_path: Path, subject_pk: str, count: int) -> None:
    """Write N raw endorsement records for subject_pk to the trust dir.

    We bypass TrustGraph.import_endorsement because that requires real
    Ed25519 signatures we don't want to forge in a unit test - the
    web layer only reads via list_endorsements which iterates the
    JSONL, so any structurally valid entry counts.
    """
    trust_dir = tmp_path / "team_trust"
    trust_dir.mkdir(parents=True, exist_ok=True)
    endorsements_path = trust_dir / "endorsements.jsonl"
    with endorsements_path.open("a", encoding="utf-8") as f:
        for i in range(count):
            f.write(json.dumps({
                "endorser_pubkey": f"{i:064x}",
                "endorser_agent_id": f"endorser-{i}",
                "subject_pubkey": subject_pk,
                "subject_agent_id": "subject",
                "weight": 1.0,
                "reason": "test",
                "issued_at": "2026-06-07T00:00:00",
                "ttl_days": 365,
                "depth_allowed": 2,
                "sig": "fake-for-test",
            }) + "\n")


# ===== positive: count surfaces in search row =====


def test_W1T4_group_row_carries_endorsement_count_zero_when_wot_empty(
    client, tmp_path,
):
    """No endorsements on disk -> count is 0, not missing/None."""
    pk = "ab" * 32  # 64-hex valid pubkey shape
    _seed_group(tmp_path, pk)
    resp = client.get(
        "/api/agents/search",
        params={"q": "qa", "actor_id": "admin"},
    )
    assert resp.status_code == 200
    group_rows = [r for r in resp.json()["results"] if r.get("source") == "group"]
    assert group_rows, "seed group should produce at least one row"
    for row in group_rows:
        assert "endorsement_count" in row
        assert row["endorsement_count"] == 0


def test_W1T4_group_row_endorsement_count_matches_disk(client, tmp_path):
    """3 endorsements on disk for this pubkey -> count = 3."""
    pk = "cd" * 32
    _seed_group(tmp_path, pk)
    _seed_endorsements(tmp_path, subject_pk=pk, count=3)
    resp = client.get(
        "/api/agents/search",
        params={"q": "qa", "actor_id": "admin"},
    )
    group_rows = [r for r in resp.json()["results"] if r.get("source") == "group"]
    matching = [r for r in group_rows if r.get("pubkey_prefix") == pk[:16]]
    assert len(matching) >= 1
    assert matching[0]["endorsement_count"] == 3


def test_W1T4_endorsement_count_only_for_matching_subject(client, tmp_path):
    """Endorsements pointing at a DIFFERENT pubkey must not count
    against this row. Protects against accidental sum-over-all."""
    pk_us = "11" * 32
    pk_other = "22" * 32
    _seed_group(tmp_path, pk_us)
    _seed_endorsements(tmp_path, subject_pk=pk_other, count=5)
    resp = client.get(
        "/api/agents/search",
        params={"q": "qa", "actor_id": "admin"},
    )
    group_rows = [r for r in resp.json()["results"] if r.get("source") == "group"]
    matching = [r for r in group_rows if r.get("pubkey_prefix") == pk_us[:16]]
    assert matching[0]["endorsement_count"] == 0


# ===== degradation: bad WoT file does not break search =====


def test_W1T4_corrupt_wot_file_degrades_to_zero_count_not_500(
    client, tmp_path,
):
    """If endorsements.jsonl is corrupt (e.g. mid-write truncation),
    search must still respond 200 with count=0, never 500."""
    pk = "ef" * 32
    _seed_group(tmp_path, pk)
    # Write a deliberately corrupt JSONL line + a missing-fields line
    trust_dir = tmp_path / "team_trust"
    trust_dir.mkdir(parents=True, exist_ok=True)
    (trust_dir / "endorsements.jsonl").write_text(
        "not-json at all\n{}\n", encoding="utf-8",
    )
    resp = client.get(
        "/api/agents/search",
        params={"q": "qa", "actor_id": "admin"},
    )
    assert resp.status_code == 200
    group_rows = [r for r in resp.json()["results"] if r.get("source") == "group"]
    # The endpoint kept going - we got a row at all
    assert group_rows
    # And the count fell back to 0 since no parseable endorsement matched.
    assert group_rows[0]["endorsement_count"] == 0


# ===== home / registry rows do not need to carry the field =====


def test_W1T4_home_source_row_does_not_need_endorsement_count(client):
    """For the home source we don't store pubkeys, so we don't compute
    a count. The field MAY be absent (front-end treats missing as 0).
    This test documents the contract so a future "always emit" change
    is a conscious decision, not an accidental one."""
    resp = client.get(
        "/api/agents/search",
        params={"q": "admin", "actor_id": "admin"},
    )
    home_rows = [r for r in resp.json()["results"] if r.get("source") == "home"]
    assert home_rows
    # We don't assert presence/absence - just that the call succeeded
    # and the row is well-formed. Front-end ContactsPanel reads
    # ``row.endorsement_count ?? 0`` so either shape is safe.
    assert "agent_id" in home_rows[0]
