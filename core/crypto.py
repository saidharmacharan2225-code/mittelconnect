"""Local secret encryption and pseudonymisation.

Credentials in config.yaml are stored as ``ENC[<fernet token>]`` and decrypted
in memory only. The master key never lives in the configuration file: it is
read from an environment variable or a secret file mounted into the container.

Fernet provides AES-128-CBC encryption with HMAC-SHA256 authentication, so a
tampered ciphertext is rejected rather than silently decrypted to garbage.
MultiFernet is used so keys can be rotated: the first key encrypts, every key
can decrypt, and ``rotate`` re-encrypts old tokens under the newest key.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import stat
from pathlib import Path
from typing import Iterable, Optional

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

logger = logging.getLogger(__name__)

ENC_PATTERN = re.compile(r"^ENC\[(?P<token>[A-Za-z0-9_\-=]+)\]$")


class CryptoError(Exception):
    """Raised for missing keys, malformed keys or undecryptable values."""


def generate_key() -> str:
    """Return a new url-safe base64 Fernet key as text."""
    return Fernet.generate_key().decode("ascii")


def _validate_key(raw: str) -> bytes:
    candidate = raw.strip().encode("ascii")
    try:
        decoded = base64.urlsafe_b64decode(candidate)
    except (ValueError, base64.binascii.Error) as exc:
        raise CryptoError("Master key is not valid url-safe base64") from exc
    if len(decoded) != 32:
        raise CryptoError("Master key must decode to exactly 32 bytes")
    return candidate


def write_key_file(path: str | os.PathLike, key: Optional[str] = None) -> str:
    """Write a key file readable only by the owner (mode 0600) and return the key."""
    key = key or generate_key()
    _validate_key(key)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise CryptoError(f"Refusing to overwrite existing key file {target}")
    fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(key + "\n")
    return key


def load_master_keys(env_name: str, key_file: Optional[str]) -> list[bytes]:
    """Load one or more master keys.

    Several keys may be supplied comma-separated (newest first) to support
    rotation. The environment variable wins over the key file.
    """
    raw = os.environ.get(env_name, "").strip()
    source = f"environment variable {env_name}"
    if not raw and key_file:
        path = Path(key_file)
        if path.is_file():
            mode = path.stat().st_mode
            if mode & (stat.S_IRWXG | stat.S_IRWXO):
                logger.warning(
                    "Master key file is readable by group/others; restrict to 0600",
                    extra={"key_file": str(path)},
                )
            raw = path.read_text(encoding="ascii").strip()
            source = f"key file {path}"
    if not raw:
        raise CryptoError(
            f"No master key found: set {env_name} or provide the key file {key_file}"
        )
    keys = [_validate_key(part) for part in raw.split(",") if part.strip()]
    if not keys:
        raise CryptoError(f"Master key from {source} is empty")
    logger.debug("Loaded %d master key(s) from %s", len(keys), source)
    return keys


class SecretBox:
    """Encrypts and decrypts secrets and payloads with the master key(s)."""

    def __init__(self, keys: Iterable[bytes]):
        key_list = list(keys)
        if not key_list:
            raise CryptoError("SecretBox requires at least one key")
        self._fernet = MultiFernet([Fernet(k) for k in key_list])

    @classmethod
    def from_settings(cls, env_name: str, key_file: Optional[str]) -> "SecretBox":
        return cls(load_master_keys(env_name, key_file))

    def encrypt_bytes(self, data: bytes) -> bytes:
        return self._fernet.encrypt(data)

    def decrypt_bytes(self, token: bytes, ttl: Optional[int] = None) -> bytes:
        try:
            if ttl is None:
                return self._fernet.decrypt(token)
            return self._fernet.decrypt(token, ttl=ttl)
        except InvalidToken as exc:
            raise CryptoError(
                "Decryption failed: wrong master key or tampered ciphertext"
            ) from exc

    def encrypt_text(self, plaintext: str) -> str:
        return self.encrypt_bytes(plaintext.encode("utf-8")).decode("ascii")

    def decrypt_text(self, token: str) -> str:
        return self.decrypt_bytes(token.encode("ascii")).decode("utf-8")

    def wrap(self, plaintext: str) -> str:
        """Encrypt and wrap in the ENC[...] marker used in config.yaml."""
        return f"ENC[{self.encrypt_text(plaintext)}]"

    def unwrap(self, value: str) -> str:
        """Decrypt an ENC[...] value; return any other value unchanged."""
        match = ENC_PATTERN.match(value.strip()) if isinstance(value, str) else None
        if not match:
            return value
        return self.decrypt_text(match.group("token"))

    def rotate(self, value: str) -> str:
        """Re-encrypt an ENC[...] value under the newest key."""
        match = ENC_PATTERN.match(value.strip())
        if not match:
            raise CryptoError("Value is not an ENC[...] token")
        rotated = self._fernet.rotate(match.group("token").encode("ascii"))
        return f"ENC[{rotated.decode('ascii')}]"


def is_encrypted(value: object) -> bool:
    return isinstance(value, str) and ENC_PATTERN.match(value.strip()) is not None


class Pseudonymizer:
    """Keyed, deterministic pseudonyms for personal data (GDPR Art. 4(5), 32).

    HMAC-SHA256 keeps the mapping stable (the same worker name always yields
    the same pseudonym, so SAP reporting still works) while making it
    impossible to reverse without the separately stored key.
    """

    def __init__(self, key: str, prefix: str = "PSN", length: int = 20):
        if not key or len(key) < 16:
            raise CryptoError("Pseudonymisation key must be at least 16 characters")
        if length < 8 or length > 64:
            raise CryptoError("Pseudonym length must be between 8 and 64")
        self._key = key.encode("utf-8")
        self._prefix = prefix
        self._length = length

    def pseudonymize(self, value: object) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        normalized = text.casefold().encode("utf-8")
        digest = hmac.new(self._key, normalized, hashlib.sha256).hexdigest().upper()
        return f"{self._prefix}{digest[: self._length - len(self._prefix)]}"
