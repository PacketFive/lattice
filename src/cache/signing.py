# SPDX-License-Identifier: GPL-2.0
"""Ed25519 keys and signatures for cache entries (RFC 0001 section 14.3).

A public key is written ``<name>:<base64 of 32 bytes>`` and a secret key
``<name>:<base64 of the 32-byte seed>``, so settings, key files and
signatures all name the key they belong to. A signature is written
``<name>:<base64 of 64 bytes>``. The name only selects which trusted key
to check against; it grants nothing by itself.
"""

from __future__ import annotations

import base64
import binascii
import os
from collections.abc import Iterable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from ..errors import SettingsError
from ..settings import KEY_NAME


def _split(text: str, length: int, what: str) -> tuple[str, bytes]:
    name, colon, encoded = text.strip().partition(":")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except binascii.Error:
        raw = b""
    if not colon or not KEY_NAME.fullmatch(name) or len(raw) != length:
        raise ValueError(f"not {what} of the form <name>:<base64>")
    return name, raw


class SecretKey:
    """An Ed25519 secret key with its name."""

    def __init__(self, name: str, seed: bytes) -> None:
        """Hold the key made from the 32-byte ``seed``."""
        self.name = name
        self._key = Ed25519PrivateKey.from_private_bytes(seed)

    @classmethod
    def parse(cls, text: str) -> SecretKey:
        """Read a key written ``<name>:<base64 seed>``. Raises ValueError if it is not one."""
        return cls(*_split(text, 32, "a secret key"))

    @classmethod
    def load(cls, path: str) -> SecretKey:
        """Read the key file at ``path``. Raises SettingsError if it cannot be used."""
        try:
            with open(path, encoding="ascii") as file:
                return cls.parse(file.read())
        except (OSError, UnicodeDecodeError, ValueError) as error:
            raise SettingsError(f"{path}: cannot read a secret key: {error}") from None

    def public(self) -> str:
        """Return the public key as ``<name>:<base64>``."""
        raw = self._key.public_key().public_bytes_raw()
        return f"{self.name}:{base64.b64encode(raw).decode()}"

    def text(self) -> str:
        """Return the secret key as ``<name>:<base64 seed>``."""
        return f"{self.name}:{base64.b64encode(self._key.private_bytes_raw()).decode()}"

    def sign(self, data: bytes) -> str:
        """Return the signature of ``data`` as ``<name>:<base64>``."""
        return f"{self.name}:{base64.b64encode(self._key.sign(data)).decode()}"


def generate_key(name: str) -> SecretKey:
    """Return a new random secret key called ``name``. Raises ValueError for a bad name."""
    if not KEY_NAME.fullmatch(name):
        raise ValueError(f"{name!r} is not a key name: use letters, digits, '.', '_', '+' and '-'")
    return SecretKey(name, os.urandom(32))


def write_secret_key(path: str, key: SecretKey) -> None:
    """Write ``key`` to the new file ``path``, readable by its owner only."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as file:
        file.write(key.text() + "\n")


def verify(trusted: Iterable[str], data: bytes, signatures: Iterable[str]) -> str | None:
    """Return the trusted public key that signed ``data``, or None if none did.

    ``trusted`` holds public keys and ``signatures`` signatures, both
    written with their key name. A malformed signature counts as none.
    """
    keys: dict[str, list[bytes]] = {}
    for text in trusted:
        name, raw = _split(text, 32, "a public key")
        keys.setdefault(name, []).append(raw)
    for signature in signatures:
        try:
            name, raw_signature = _split(signature, 64, "a signature")
        except ValueError:
            continue
        for raw_key in keys.get(name, ()):
            try:
                Ed25519PublicKey.from_public_bytes(raw_key).verify(raw_signature, data)
            except InvalidSignature:
                continue
            return f"{name}:{base64.b64encode(raw_key).decode()}"
    return None
