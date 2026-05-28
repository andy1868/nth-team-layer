"""
Agent identity primitives for Nth Team Layer.

The identity layer gives an agent a stable local profile and, when PyNaCl is
installed, an Ed25519 keypair for signing future agent-to-agent messages. The
core package keeps PyNaCl optional so the stdlib-only default install continues
to work.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Union


DEFAULT_IDENTITY_DIR = ".nth"
DEFAULT_IDENTITY_FILE = "identity.json"
AGENT_ID_SHORT_LEN = 12

_NACL_AVAILABLE = False
_SigningKey = None
_VerifyKey = None

try:
    from nacl.signing import SigningKey as _NaclSigningKey
    from nacl.signing import VerifyKey as _NaclVerifyKey

    _NACL_AVAILABLE = True
    _SigningKey = _NaclSigningKey
    _VerifyKey = _NaclVerifyKey
except ImportError:
    pass


def crypto_available() -> bool:
    return _NACL_AVAILABLE


def _require_crypto(feature: str) -> None:
    if not _NACL_AVAILABLE:
        raise ImportError(
            f"{feature} requires PyNaCl. Install with "
            "'pip install nth-team-layer[crypto]' or 'pip install pynacl>=1.5'."
        )


def canonical_json(data: Dict[str, Any]) -> bytes:
    return json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


@dataclass(frozen=True)
class AgentID:
    """Stable agent identifier.

    Plain IDs preserve current behavior. Cryptographic IDs are derived from an
    Ed25519 public key fingerprint.
    """

    value: str
    is_cryptographic: bool = False
    pubkey_hex: str = ""

    def __str__(self) -> str:
        return self.value

    @property
    def short(self) -> str:
        return self.value[:8]

    @property
    def is_plain(self) -> bool:
        return not self.is_cryptographic

    @classmethod
    def from_string(cls, agent_id: str) -> "AgentID":
        if not agent_id:
            raise ValueError("agent_id must not be empty")
        return cls(value=agent_id, is_cryptographic=False)

    @classmethod
    def from_pubkey(cls, pubkey_hex: str) -> "AgentID":
        if not pubkey_hex:
            raise ValueError("pubkey_hex must not be empty")
        fingerprint = hashlib.sha256(bytes.fromhex(pubkey_hex)).hexdigest()
        return cls(
            value=fingerprint[:AGENT_ID_SHORT_LEN],
            is_cryptographic=True,
            pubkey_hex=pubkey_hex,
        )


@dataclass
class AgentIdentity:
    """Agent profile plus optional Ed25519 signing material."""

    agent_id: AgentID
    label: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    _signing_key: Optional[bytes] = field(default=None, repr=False)
    _verify_key: Optional[bytes] = field(default=None, repr=False)

    @classmethod
    def from_string(
        cls,
        agent_id: str,
        label: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "AgentIdentity":
        return cls(
            agent_id=AgentID.from_string(agent_id),
            label=label,
            metadata=metadata or {},
        )

    @classmethod
    def generate(
        cls,
        save_path: Optional[Union[str, Path]] = None,
        label: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "AgentIdentity":
        _require_crypto("AgentIdentity.generate()")
        assert _SigningKey is not None

        signing_key = _SigningKey.generate()
        verify_key = signing_key.verify_key
        verify_bytes = verify_key.encode()
        identity = cls(
            agent_id=AgentID.from_pubkey(verify_bytes.hex()),
            label=label,
            metadata=metadata or {},
            _signing_key=signing_key.encode(),
            _verify_key=verify_bytes,
        )
        if save_path is not None:
            identity.save(save_path)
        return identity

    @classmethod
    def load(cls, path: Union[str, Path]) -> "AgentIdentity":
        identity_path = Path(path)
        data = json.loads(identity_path.read_text(encoding="utf-8"))

        signing_hex = data.get("private_key", "")
        pubkey_hex = data.get("pubkey", "")
        if signing_hex and pubkey_hex:
            _require_crypto("AgentIdentity.load() with a keypair")
            return cls(
                agent_id=AgentID.from_pubkey(pubkey_hex),
                label=data.get("label", ""),
                metadata=data.get("metadata", {}),
                _signing_key=bytes.fromhex(signing_hex),
                _verify_key=bytes.fromhex(pubkey_hex),
            )

        return cls.from_string(
            data.get("agent_id", "anonymous"),
            label=data.get("label", ""),
            metadata=data.get("metadata", {}),
        )

    @property
    def can_sign(self) -> bool:
        return bool(_NACL_AVAILABLE and self._signing_key and self._verify_key)

    @property
    def pubkey_hex(self) -> str:
        return self._verify_key.hex() if self._verify_key else self.agent_id.pubkey_hex

    def public_dict(self) -> Dict[str, Any]:
        return {
            "agent_id": str(self.agent_id),
            "label": self.label,
            "is_cryptographic": self.agent_id.is_cryptographic,
            "pubkey": self.pubkey_hex,
            "fingerprint": self.fingerprint(),
            "metadata": self.metadata,
        }

    def save(self, path: Union[str, Path]) -> None:
        identity_path = Path(path)
        identity_path.parent.mkdir(parents=True, exist_ok=True)

        data = self.public_dict()
        if self._signing_key:
            data["private_key"] = self._signing_key.hex()

        tmp = identity_path.with_suffix(identity_path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(identity_path))
        try:
            identity_path.chmod(0o600)
        except Exception:
            pass

    def sign(self, payload: bytes) -> bytes:
        if not self.can_sign:
            raise RuntimeError(
                f"Agent '{self.agent_id}' has no signing key. "
                "Use AgentIdentity.generate() with PyNaCl installed."
            )
        assert _SigningKey is not None
        assert self._signing_key is not None
        return _SigningKey(self._signing_key).sign(payload).signature

    def sign_json(self, data: Dict[str, Any]) -> str:
        return self.sign(canonical_json(data)).hex()

    def verify(
        self,
        payload: bytes,
        signature: bytes,
        pubkey: Optional[bytes] = None,
    ) -> bool:
        if not _NACL_AVAILABLE:
            return False
        verify_key = pubkey or self._verify_key
        if not verify_key:
            return False
        try:
            assert _VerifyKey is not None
            _VerifyKey(verify_key).verify(payload, signature)
            return True
        except Exception:
            return False

    def verify_json(
        self,
        data: Dict[str, Any],
        signature_hex: str,
        pubkey_hex: Optional[str] = None,
    ) -> bool:
        pubkey = bytes.fromhex(pubkey_hex) if pubkey_hex else None
        return self.verify(canonical_json(data), bytes.fromhex(signature_hex), pubkey)

    def fingerprint(self) -> str:
        payload = self.pubkey_hex or str(self.agent_id)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def default_identity_path(workspace: Union[str, Path]) -> Path:
    return Path(workspace) / DEFAULT_IDENTITY_DIR / DEFAULT_IDENTITY_FILE


def load_or_generate(
    workspace: Union[str, Path],
    label: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> AgentIdentity:
    identity_path = default_identity_path(workspace)
    if identity_path.exists():
        return AgentIdentity.load(identity_path)

    if _NACL_AVAILABLE:
        return AgentIdentity.generate(
            save_path=identity_path,
            label=label,
            metadata=metadata,
        )

    fallback = AgentIdentity.from_string(
        "agent-" + secrets.token_hex(4),
        label=label,
        metadata=metadata,
    )
    fallback.save(identity_path)
    return fallback
