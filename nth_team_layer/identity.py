"""
Identity — Agent 密码学身份（Ed25519 密钥对 + 签名/验证）

每个 Agent 可以拥有一个 Ed25519 密钥对：
- agent_id = SHA-256(公钥) 的前 12 字符（短指纹）
- 所有消息/操作都可以被签名和验证
- 身份持久化到 ~/.nth/identity.json（权限 600）

依赖：
- pynacl >= 1.5（可选 extra: pip install nth-team-layer[crypto]）
- 未安装时降级为简单字符串身份（向后兼容）

设计：
- AgentIdentity：密钥对 + 签名/验证
- AgentID：不可变的身份标识符（pubkey fingerprint）
- 向后兼容：不传私钥时，agent_id 仍是普通字符串

用法：
    # 生成新身份
    ident = AgentIdentity.generate(save_path="~/.nth/identity.json")

    # 加载已有身份
    ident = AgentIdentity.load("~/.nth/identity.json")

    # 签名消息
    sig = ident.sign(b"hello world")

    # 验证签名
    assert ident.verify(b"hello world", sig)

    # 在 attach() 中使用（自动生成或加载）
    team = nth.attach(identity=ident, ...)
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ─────────────────── 常量 ───────────────────

DEFAULT_IDENTITY_DIR = ".nth"
DEFAULT_IDENTITY_FILE = "identity.json"
AGENT_ID_SHORT_LEN = 12  # agent_id 短指纹长度


# ─────────────────── 可选依赖检测 ───────────────────

_NACL_AVAILABLE = False
_SigningKey_class: type | None = None
_VerifyKey_class: type | None = None

try:
    from nacl.signing import SigningKey as _SK, VerifyKey as _VK  # type: ignore[assignment]
    _NACL_AVAILABLE = True
    _SigningKey_class = _SK
    _VerifyKey_class = _VK
except ImportError:
    pass


def _nacl_required(feature: str) -> None:
    """检查 pynacl 是否可用，不可用则抛出友好错误"""
    if not _NACL_AVAILABLE:
        raise ImportError(
            f"{feature} requires pynacl. Install with:\n"
            f"  pip install nth-team-layer[crypto]\n"
            f"  (or: pip install pynacl>=1.5)"
        )


# ─────────────────── AgentID ───────────────────


@dataclass(frozen=True)
class AgentID:
    """不可变的 Agent 身份标识符

    - 密码学身份：pubkey_hash = SHA-256(ed25519 pubkey hex)[:12]
    - 简单身份：plain = 任意字符串（向后兼容）
    """

    value: str
    is_cryptographic: bool = False
    pubkey_hex: str = ""  # 仅密码学身份时有值

    def __str__(self) -> str:
        return self.value

    def __hash__(self) -> int:
        return hash(self.value)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, AgentID):
            return self.value == other.value
        if isinstance(other, str):
            return self.value == other
        return False

    @property
    def short(self) -> str:
        """前 8 字符，用于紧凑显示"""
        return self.value[:8]

    @property
    def is_anonymous(self) -> bool:
        """是否为匿名身份（无密钥支持的简单字符串）"""
        return not self.is_cryptographic

    @classmethod
    def from_string(cls, agent_id: str) -> "AgentID":
        """从普通字符串创建身份（向后兼容）"""
        return cls(value=agent_id, is_cryptographic=False)

    @classmethod
    def from_pubkey(cls, pubkey_hex: str) -> "AgentID":
        """从 Ed25519 公钥创建密码学身份"""
        fingerprint = hashlib.sha256(pubkey_hex.encode()).hexdigest()[:AGENT_ID_SHORT_LEN]
        return cls(
            value=fingerprint,
            is_cryptographic=True,
            pubkey_hex=pubkey_hex,
        )


# ─────────────────── AgentIdentity ───────────────────


@dataclass
class AgentIdentity:
    """Agent 的密码学身份（Ed25519 密钥对）

    每个 Agent 应该有一个持久化的身份文件。
    身份一旦生成就不应该改变（变更 = 新的 Agent）。

    未安装 pynacl 时，仍可创建简单身份（无签名能力）。
    """

    agent_id: AgentID
    _signing_key: Optional[bytes] = field(default=None, repr=False)
    _verify_key: Optional[bytes] = field(default=None, repr=False)
    label: str = ""  # 人类可读的标签（可选）
    metadata: dict = field(default_factory=dict)

    # ─────────── 工厂方法 ───────────

    @classmethod
    def generate(
        cls,
        save_path: Optional[Path] = None,
        label: str = "",
        metadata: Optional[dict] = None,
    ) -> "AgentIdentity":
        """生成新的 Ed25519 密钥对

        Args:
            save_path: 可选持久化路径（如 ~/.nth/identity.json）
            label: 人类可读标签
            metadata: 附加元数据

        Returns:
            新的 AgentIdentity

        Raises:
            ImportError: 如果 pynacl 未安装
        """
        _nacl_required("AgentIdentity.generate()")

        sk = _SigningKey_class.generate()
        vk = sk.verify_key

        sk_bytes = sk.encode()
        vk_bytes = vk.encode()

        pubkey_hex = vk_bytes.hex()
        agent_id = AgentID.from_pubkey(pubkey_hex)

        ident = cls(
            agent_id=agent_id,
            _signing_key=sk_bytes,
            _verify_key=vk_bytes,
            label=label,
            metadata=metadata or {},
        )

        if save_path:
            ident.save(save_path)

        return ident

    @classmethod
    def load(cls, path: Path) -> "AgentIdentity":
        """从持久化文件加载身份

        Args:
            path: 身份文件路径（如 ~/.nth/identity.json）

        Returns:
            AgentIdentity

        Raises:
            FileNotFoundError: 文件不存在
            ImportError: 如果 pynacl 未安装
        """
        if not path.exists():
            raise FileNotFoundError(f"Identity file not found: {path}")

        data = json.loads(path.read_text(encoding="utf-8"))

        sk_hex = data.get("private_key", "")
        vk_hex = data.get("pubkey", "")

        if sk_hex and vk_hex:
            _nacl_required("AgentIdentity.load() with keypair")
            sk = _SigningKey_class(bytes.fromhex(sk_hex))
            return cls(
                agent_id=AgentID.from_pubkey(vk_hex),
                _signing_key=sk.encode(),
                _verify_key=bytes.fromhex(vk_hex),
                label=data.get("label", ""),
                metadata=data.get("metadata", {}),
            )
        else:
            # 简单身份（无密钥）
            return cls.from_string(data.get("agent_id", "anonymous"))

    @classmethod
    def from_string(cls, agent_id: str, label: str = "") -> "AgentIdentity":
        """从普通字符串创建简单身份（向后兼容，无签名能力）"""
        return cls(
            agent_id=AgentID.from_string(agent_id),
            label=label,
        )

    # ─────────── 持久化 ───────────

    def save(self, path: Path) -> None:
        """将身份持久化到文件（权限 600）"""
        path.parent.mkdir(parents=True, exist_ok=True)

        data: dict = {
            "agent_id": str(self.agent_id),
            "label": self.label,
            "metadata": self.metadata,
        }

        if self._signing_key and self._verify_key:
            data["pubkey"] = self._verify_key.hex()
            data["private_key"] = self._signing_key.hex()

        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(str(tmp), str(path))

        # 设置权限 600（仅 owner 可读写）
        try:
            path.chmod(0o600)
        except Exception:
            pass  # Windows 不支持 chmod

    # ─────────── 签名 / 验证 ───────────

    @property
    def can_sign(self) -> bool:
        """是否有签名能力（拥有私钥）"""
        return self._signing_key is not None and _NACL_AVAILABLE

    @property
    def pubkey_hex(self) -> str:
        """公钥的十六进制表示"""
        if self._verify_key:
            return self._verify_key.hex()
        return ""

    def sign(self, payload: bytes) -> bytes:
        """对字节 payload 签名（Ed25519 detached signature）

        Returns:
            64 字节的 Ed25519 签名

        Raises:
            RuntimeError: 如果没有私钥
        """
        if not self.can_sign:
            raise RuntimeError(
                f"Agent '{self.agent_id}' has no signing key. "
                f"Generate a keypair with AgentIdentity.generate() or "
                f"install pynacl: pip install nth-team-layer[crypto]"
            )

        assert self._signing_key is not None
        assert _SigningKey_class is not None
        sk = _SigningKey_class(self._signing_key)
        signed = sk.sign(payload)
        return signed.signature  # 64 bytes detached

    def sign_json(self, data: dict) -> str:
        """对 JSON 可序列化的 dict 签名

        内部将 dict 序列化为规范 JSON 后签名。

        Returns:
            签名的十六进制字符串（128 字符）
        """
        canonical = json.dumps(data, sort_keys=True, ensure_ascii=False)
        sig = self.sign(canonical.encode("utf-8"))
        return sig.hex()

    def verify(self, payload: bytes, signature: bytes, pubkey: Optional[bytes] = None) -> bool:
        """验证签名

        Args:
            payload: 原始数据
            signature: 64 字节 Ed25519 签名
            pubkey: 签名者的公钥（None 则验证自己的签名）

        Returns:
            True 如果签名有效
        """
        if not _NACL_AVAILABLE:
            return False

        vk_bytes = pubkey or self._verify_key
        if not vk_bytes:
            return False

        try:
            assert _VerifyKey_class is not None
            vk = _VerifyKey_class(vk_bytes)
            vk.verify(payload, signature)
            return True
        except Exception:
            return False

    def verify_json(self, data: dict, signature_hex: str, pubkey_hex: Optional[str] = None) -> bool:
        """验证 JSON dict 的签名

        Args:
            data: 原始 dict
            signature_hex: 签名的十六进制字符串
            pubkey_hex: 签名者的公钥十六进制
        """
        canonical = json.dumps(data, sort_keys=True, ensure_ascii=False)
        sig = bytes.fromhex(signature_hex)
        pubkey = bytes.fromhex(pubkey_hex) if pubkey_hex else None
        return self.verify(canonical.encode("utf-8"), sig, pubkey)

    # ─────────── 工具 ───────────

    def fingerprint(self) -> str:
        """返回身份的完整指纹（SHA-256 的 16 进制）"""
        payload = self.pubkey_hex or str(self.agent_id)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def __str__(self) -> str:
        return f"AgentIdentity({self.agent_id})"

    def __repr__(self) -> str:
        crypto = "🔑" if self.can_sign else "📝"
        return f"{crypto} {self.agent_id}" + (f" ({self.label})" if self.label else "")


# ─────────────────── 便捷函数 ───────────────────


def load_or_generate(
    workspace: Path,
    label: str = "",
) -> AgentIdentity:
    """自动加载已有身份，不存在则生成新的

    这是 attach() 中推荐的标准用法：
        identity = load_or_generate(workspace, label="my-agent")

    Args:
        workspace: 工作目录（身份保存在 {workspace}/.nth/identity.json）
        label: 人类可读标签
    """
    identity_path = workspace / DEFAULT_IDENTITY_DIR / DEFAULT_IDENTITY_FILE

    if identity_path.exists():
        try:
            return AgentIdentity.load(identity_path)
        except Exception:
            pass  # 文件损坏，重新生成

    if _NACL_AVAILABLE:
        ident = AgentIdentity.generate(save_path=identity_path, label=label)
    else:
        # 降级：用随机字符作为简单 agent_id
        fallback_id = "agent-" + secrets.token_hex(4)
        ident = AgentIdentity.from_string(fallback_id, label=label)
        ident.save(identity_path)

    return ident
