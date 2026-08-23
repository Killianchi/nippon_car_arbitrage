"""Authenticated encryption for the state blob committed to git.

The repository is public, so the catalog cannot be. The daily run keeps its
SQLite database on an orphan `data` branch as a single encrypted file: the
code stays readable, the deal flow does not.

Format (all binary, concatenated):

    magic "NMSTATE1"  8 bytes
    salt             16 bytes   random per write
    nonce            12 bytes   random per write
    ciphertext       AES-256-GCM over the gzipped database

The key is derived per write with PBKDF2-HMAC-SHA256 from the passphrase in
`DATA_ENCRYPTION_KEY`. A fresh salt and nonce every time means two commits of
identical data still produce different bytes, so git history leaks nothing
about how much changed day to day.
"""

from __future__ import annotations

import gzip
import os
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

MAGIC = b"NMSTATE1"
SALT_LEN = 16
NONCE_LEN = 12
KEY_LEN = 32
ITERATIONS = 600_000

ENV_VAR = "DATA_ENCRYPTION_KEY"


class StateCryptoError(RuntimeError):
    """Raised for a missing key, a corrupt blob, or a wrong passphrase."""


def _derive(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=KEY_LEN, salt=salt, iterations=ITERATIONS
    )
    return kdf.derive(passphrase.encode("utf-8"))


def passphrase_from_env() -> str:
    key = os.environ.get(ENV_VAR, "").strip()
    if not key:
        raise StateCryptoError(
            f"{ENV_VAR} is not set. Generate one with "
            f"`python -c 'import secrets; print(secrets.token_urlsafe(32))'` "
            f"and add it as a GitHub Actions secret."
        )
    if len(key) < 16:
        raise StateCryptoError(
            f"{ENV_VAR} is too short ({len(key)} chars). Use at least 16; "
            f"32 random URL-safe characters is the intended shape."
        )
    return key


def encrypt(plaintext: bytes, passphrase: str) -> bytes:
    """Gzip then encrypt. Returns the full on-disk blob."""
    salt = secrets.token_bytes(SALT_LEN)
    nonce = secrets.token_bytes(NONCE_LEN)
    key = _derive(passphrase, salt)
    # The magic bytes are authenticated too, so a truncated or spliced file
    # fails the tag check rather than decrypting to garbage.
    ciphertext = AESGCM(key).encrypt(nonce, gzip.compress(plaintext, 9), MAGIC)
    return MAGIC + salt + nonce + ciphertext


def decrypt(blob: bytes, passphrase: str) -> bytes:
    """Inverse of `encrypt`. Raises StateCryptoError on any mismatch."""
    header = len(MAGIC) + SALT_LEN + NONCE_LEN
    if len(blob) < header + 16:
        raise StateCryptoError("state blob is too short to be valid")
    if not blob.startswith(MAGIC):
        raise StateCryptoError(
            "state blob has the wrong magic bytes -- it was not written by this tool"
        )

    salt = blob[len(MAGIC) : len(MAGIC) + SALT_LEN]
    nonce = blob[len(MAGIC) + SALT_LEN : header]
    key = _derive(passphrase, salt)

    try:
        compressed = AESGCM(key).decrypt(nonce, blob[header:], MAGIC)
    except InvalidTag as exc:
        raise StateCryptoError(
            f"could not decrypt the state blob: wrong {ENV_VAR}, or the file is corrupt. "
            f"If you rotated the key, the old history cannot be read with the new one."
        ) from exc

    try:
        return gzip.decompress(compressed)
    except OSError as exc:  # pragma: no cover - implies a broken AEAD, not user error
        raise StateCryptoError(f"state blob decrypted but did not decompress: {exc}") from exc
