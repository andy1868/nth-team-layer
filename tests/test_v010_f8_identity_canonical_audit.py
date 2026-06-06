"""F-8: independent audit coverage for identity.py and canonical_json."""

from __future__ import annotations

import math

import pytest

from nth_dao.identity import AgentID, AgentIdentity, canonical_json, crypto_available


def test_canonical_json_is_deterministic_utf8_and_compact():
    payload = {"z": ["agent", 2], "a": {"name": "NTH DAO"}}
    assert canonical_json(payload) == (
        b'{"a":{"name":"NTH DAO"},"z":["agent",2]}'
    )


def test_canonical_json_rejects_non_dict_root():
    with pytest.raises(TypeError, match="root must be a dict"):
        canonical_json([{"a": 1}])  # type: ignore[arg-type]


def test_canonical_json_rejects_non_string_keys():
    with pytest.raises(TypeError, match="keys must be strings"):
        canonical_json({1: "collides-with-string-key"})  # type: ignore[dict-item]


@pytest.mark.parametrize("value", [0.0, -0.0, 1.25, math.nan, math.inf, -math.inf])
def test_canonical_json_rejects_all_floats(value):
    with pytest.raises(TypeError, match="rejects float"):
        canonical_json({"value": value})


@pytest.mark.parametrize("value", [(1, 2), {1, 2}, b"bytes"])
def test_canonical_json_rejects_python_only_value_shapes(value):
    with pytest.raises(TypeError, match="does not support"):
        canonical_json({"value": value})


def test_agent_id_from_pubkey_requires_valid_32_byte_ed25519_key():
    good = "00" * 32
    agent_id = AgentID.from_pubkey(good)
    assert agent_id.is_cryptographic
    assert agent_id.pubkey_hex == good

    with pytest.raises(ValueError, match="valid hex"):
        AgentID.from_pubkey("not-hex")
    with pytest.raises(ValueError, match="32 bytes"):
        AgentID.from_pubkey("00")
    with pytest.raises(ValueError, match="32 bytes"):
        AgentID.from_pubkey("00" * 33)


@pytest.mark.skipif(not crypto_available(), reason="PyNaCl required")
def test_verify_json_returns_false_for_malformed_hex_inputs():
    ident = AgentIdentity.generate(label="f8")
    payload = {"ok": True}
    sig = ident.sign_json(payload)

    assert ident.verify_json(payload, sig)
    assert ident.verify_json(payload, "not-hex") is False
    assert ident.verify_json(payload, sig, pubkey_hex="not-hex") is False


@pytest.mark.skipif(not crypto_available(), reason="PyNaCl required")
def test_verify_json_returns_false_for_non_canonical_payload_shapes():
    ident = AgentIdentity.generate(label="f8")
    assert ident.verify_json({"value": math.nan}, "00" * 64) is False


@pytest.mark.skipif(not crypto_available(), reason="PyNaCl required")
def test_verify_only_identity_from_did_can_verify_but_not_sign():
    signer = AgentIdentity.generate(label="signer")
    restored = AgentIdentity.from_did(signer.as_did())
    payload = {"hello": "world"}
    sig = signer.sign_json(payload)

    assert not restored.can_sign
    assert restored.verify_json(payload, sig)
    with pytest.raises(RuntimeError, match="has no signing key"):
        restored.sign_json(payload)
