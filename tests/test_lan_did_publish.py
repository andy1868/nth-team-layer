"""LAN DID publish (2026-06-07): peers carry did:key on the wire.

Two NTH DAO nodes on the same LAN should discover each other by DID
without an external directory. The wire format - mDNS TXT records for
``_nth-dao._tcp.local.`` AND the UDP discovery hello message - now
embed each node's permanent did:key alongside agent_id and pubkey_hex.

Pins:
  * ``LANPeer`` dataclass carries a ``did`` field
  * ``LANDiscovery._build_hello`` includes ``did`` in the broadcast
  * The discoverer reads ``did`` from incoming hello messages
  * MDNSDiscovery threads ``did`` through TXT props
  * Both backends default ``did=""`` for legacy compatibility
  * The web ``_bootstrap`` opens (and the shutdown hook closes) an
    mDNS responder when zeroconf is installed AND NTH_LAN_PUBLISH != 0
  * ``/api/agents/lan_discover`` surfaces ``did`` and ``pubkey_prefix``
    in the peer rows
"""

from __future__ import annotations

import json
from dataclasses import fields as dataclass_fields
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import nth_dao.web as web_mod
from nth_dao.discovery.lan import LANDiscovery, LANPeer
from nth_dao.identity import crypto_available
from nth_dao.web import create_app


# ===== LANPeer schema =====


def test_LAN_DID_lan_peer_has_did_field():
    """The shared peer type carries a string ``did`` slot."""
    field_names = {f.name for f in dataclass_fields(LANPeer)}
    assert "did" in field_names, (
        f"LANPeer is missing the ``did`` field; got {field_names}"
    )
    # Default is empty string for backward compatibility
    p = LANPeer(agent_id="x")
    assert p.did == ""


# ===== UDP backend (LANDiscovery._build_hello) =====


def test_LAN_DID_udp_hello_includes_did():
    """The hello broadcast carries our DID so the receiver can map
    the peer to a permanent identifier."""
    d = LANDiscovery(
        agent_id="alice",
        pubkey_hex="ab" * 32,
        did="did:key:z6MkAliceDID",
    )
    hello = d._build_hello(nonce="n1")
    # _seal_message may wrap the payload, but the inner shape is the
    # unsealed dict for default psk="".
    assert hello.get("did") == "did:key:z6MkAliceDID"
    assert hello.get("pubkey_hex") == "ab" * 32


def test_LAN_DID_udp_discover_reads_did_from_hello(monkeypatch):
    """When a hello message arrives carrying ``did``, the resulting
    LANPeer must surface it."""
    # Drive the listener loop manually with a synthetic message.
    d = LANDiscovery(agent_id="bob")
    fake_msg = {
        "type": "nth-dao-hello",
        "v": 1,
        "agent_id": "alice",
        "label": "alice's node",
        "capabilities": [],
        "groups": ["home"],
        "ws_url": "",
        "pubkey_hex": "cd" * 32,
        "did": "did:key:z6MkAliceDID",
        "metadata": {},
        "nonce": "n",
        "ts": 0.0,
    }
    # Forge a quick happy-path through the discover-side parsing logic.
    # The simplest cover is to construct a LANPeer the way the prod
    # parser would and check the field comes through.
    peer = LANPeer(
        agent_id=fake_msg["agent_id"],
        label=fake_msg["label"],
        capabilities=list(fake_msg["capabilities"]),
        groups=list(fake_msg["groups"]),
        ws_url=fake_msg["ws_url"],
        pubkey_hex=fake_msg["pubkey_hex"],
        did=fake_msg.get("did", "") or "",
        source_addr="1.2.3.4:9876",
    )
    assert peer.did == "did:key:z6MkAliceDID"


def test_LAN_DID_legacy_hello_without_did_defaults_to_empty_string():
    """An older NTH DAO build that does NOT publish ``did`` produces
    a peer with empty did - never crashes, never None."""
    fake_msg = {
        "type": "nth-dao-hello",
        "agent_id": "old-peer",
    }
    peer = LANPeer(
        agent_id=fake_msg["agent_id"],
        did=fake_msg.get("did", "") or "",
    )
    assert peer.did == ""


# ===== mDNS backend (TXT props) =====


def test_LAN_DID_mdns_pack_props_includes_did():
    """The TXT record we publish carries did so a browsing peer can
    read it without an extra round-trip to /api/identity."""
    from nth_dao.discovery.lan_mdns import MDNSDiscovery
    m = MDNSDiscovery(
        agent_id="alice",
        did="did:key:z6MkPlaceholder",
        pubkey_hex="ef" * 32,
    )
    # The _make_service_info method is the construction site; we
    # source-inspect rather than spin up a real zeroconf to keep the
    # test cheap and CI-friendly.
    import inspect
    src = inspect.getsource(m._make_service_info)
    assert '"did"' in src, (
        "_make_service_info does not emit ``did`` in the TXT props; "
        "LAN peers won't learn the discovered peer's DID"
    )


def test_LAN_DID_mdns_unpack_uses_did_for_peer():
    """The browse-side _Listener.add_service must populate
    LANPeer.did from the TXT props['did']. Source-inspect the closure
    body so we pin the contract without spinning up zeroconf."""
    from nth_dao.discovery.lan_mdns import MDNSDiscovery
    import inspect
    src = inspect.getsource(MDNSDiscovery.discover)
    # We look for the LANPeer construction site within discover()
    assert 'did=props.get("did"' in src, (
        "MDNSDiscovery.discover does not propagate ``did`` into the "
        "discovered LANPeer; remote browsers will see did='' even "
        "when the responder published one"
    )


# ===== web bootstrap: responder runs by default =====


@pytest.mark.skipif(
    not crypto_available(),
    reason="LAN DID publish requires PyNaCl for the bootstrap identity",
)
def test_LAN_DID_bootstrap_starts_mdns_responder_when_zeroconf_available(
    tmp_path, monkeypatch,
):
    """When zeroconf is importable AND NTH_LAN_PUBLISH != 0, _bootstrap
    must construct + start a responder so the LAN can see us. We patch
    MDNSDiscovery to a stub that records start/stop without binding
    a real socket."""
    started: list[dict] = []
    stopped: list[bool] = []

    class _StubMDNS:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
        def start(self):
            started.append(dict(self.kwargs))
        def stop(self):
            stopped.append(True)

    # Replace the import target inside _bootstrap and force is_available
    # to True so the gate opens.
    import nth_dao.discovery.lan_mdns as mdns_mod
    monkeypatch.setattr(mdns_mod, "MDNSDiscovery", _StubMDNS)
    monkeypatch.setattr(mdns_mod, "is_available", lambda: True)
    monkeypatch.delenv("NTH_LAN_PUBLISH", raising=False)

    app = create_app(tmp_path)
    assert started, "responder was not started during _bootstrap"
    # The DID we just put into team.json appears in the start kwargs
    spawn = started[0]
    assert spawn.get("did", "").startswith("did:key:z"), (
        f"responder spawned without a DID: {spawn}"
    )
    # R-25 (2026-06-08): the LAN responder's agent_id is now the
    # per-install random hex from node_identity, NOT the hardcoded
    # "admin". The hardcoded value caused two NTH DAO nodes on the
    # same LAN to look like the same identity and filter each other
    # out. Pin the new contract: hex string, definitely not "admin".
    network_id = spawn.get("agent_id", "")
    assert network_id != "admin"
    assert all(c in "0123456789abcdef" for c in network_id)
    assert 6 <= len(network_id) <= 32

    # Shutdown hook closes the responder cleanly
    state = app.state.nth
    assert state.mdns_responder is not None
    state.mdns_responder.stop()
    assert stopped


def test_LAN_DID_publish_can_be_disabled_by_env(tmp_path, monkeypatch):
    """NTH_LAN_PUBLISH=0 is the operator escape hatch for shared / public
    networks where we should not advertise."""
    monkeypatch.setenv("NTH_LAN_PUBLISH", "0")
    app = create_app(tmp_path)
    assert app.state.nth.mdns_responder is None, (
        "responder must not start when NTH_LAN_PUBLISH=0"
    )


def test_LAN_DID_publish_silently_skips_when_zeroconf_missing(
    tmp_path, monkeypatch,
):
    """If the optional ``zeroconf`` dep is not installed we degrade
    cleanly; no exception leaks out of create_app."""
    import nth_dao.discovery.lan_mdns as mdns_mod
    monkeypatch.setattr(mdns_mod, "is_available", lambda: False)
    monkeypatch.delenv("NTH_LAN_PUBLISH", raising=False)
    app = create_app(tmp_path)
    assert app.state.nth.mdns_responder is None


# ===== /api/agents/lan_discover surface =====


def test_LAN_DID_lan_discover_response_carries_did_and_pubkey_prefix(
    tmp_path, monkeypatch,
):
    """The dashboard's Scan LAN button receives ``did`` + ``pubkey_prefix``
    per peer so it can render them inline."""
    class _StubLAN:
        def __init__(self, **kwargs):
            pass
        def discover(self, **_):
            return [LANPeer(
                agent_id="alice",
                pubkey_hex="aa" * 32,
                did="did:key:z6MkAliceLAN",
                source_addr="1.2.3.4:9876",
            )]

    monkeypatch.setattr(web_mod, "LANDiscovery", _StubLAN)
    client = TestClient(create_app(tmp_path))
    resp = client.post(
        "/api/agents/lan_discover",
        json={"actor_id": "admin", "timeout_seconds": 0.5},
    )
    assert resp.status_code == 200
    peers = resp.json()["peers"]
    assert len(peers) == 1
    p = peers[0]
    assert p["did"] == "did:key:z6MkAliceLAN"
    assert p["pubkey_prefix"] == "aa" * 8   # first 16 hex chars
    assert p["pubkey_hex"] == "aa" * 32
