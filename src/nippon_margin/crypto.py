"""Packing for the catalog blob committed to git.

Two formats, chosen automatically by whether `DATA_ENCRYPTION_KEY` is set:

    NMPLAIN1 + gzip                          -- the default
    NMSTATE1 + salt + nonce + AES-256-GCM    -- when a key is present

Encryption is opt-in because it buys privacy and nothing else here: git
already content-addresses blobs, so integrity comes free, and gzip does the
compression either way. What it costs is a secret to manage and a hard
failure mode if you lose it. So the default is plain gzip -- readable with
`gunzip` and `sqlite3`, no setup -- and setting the secret later transparently
upgrades the next push. `unpack` reads either format, so switching in either
direction needs no migration.

One caveat worth knowing: turning encryption on later does not retroactively
hide what was already pushed. The `data` branch is force-pushed, so old
commits become unreachable, but unreachable objects linger on the remote for
a while. If you switch and care about the earlier data, delete the branch and
let the next run recreate it.

When a key *is* set: it is stretched per write with PBKDF2-HMAC-SHA256, and a
fresh salt and nonce every time mean two commits of identical data still
produce different bytes -- so the history leaks nothing about how much
changed day to day.
"""

from __future__ import annotations

import gzip
import os
import secrets
import zlib

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

MAGIC = b"NMSTATE1"
PLAIN_MAGIC = b"NMPLAIN1"
SALT_LEN = 16
NONCE_LEN = 12
KEY_LEN = 32
ITERATIONS = 600_000

ENV_VAR = "DATA_ENCRYPTION_KEY"

#: `gzip.decompress` raises BadGzipFile (an OSError) for a damaged header,
#: EOFError for truncation, and a bare `zlib.error` -- which is *not* an
#: OSError -- for damaged deflate data. That last case is the likeliest, so
#: catching only OSError would let the commonest corruption escape as an
#: unhandled exception instead of a clear message.
DECOMPRESS_ERRORS = (OSError, EOFError, zlib.error)


class StateCryptoError(RuntimeError):
    """Raised for a missing key, a corrupt blob, or a wrong passphrase."""


def _derive(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=KEY_LEN, salt=salt, iterations=ITERATIONS
    )
    return kdf.derive(passphrase.encode("utf-8"))


def optional_passphrase_from_env() -> str | None:
    """The passphrase if one is configured, else None.

    An unset key is a valid choice, not an error. A key that is set but
    obviously too weak still raises: that is a misconfiguration, and silently
    encrypting with `hunter2` would be worse than not encrypting at all.
    """
    key = os.environ.get(ENV_VAR, "").strip()
    if not key:
        return None
    if len(key) < 16:
        raise StateCryptoError(
            f"{ENV_VAR} is too short ({len(key)} chars). Use at least 16; "
            f"32 random URL-safe characters is the intended shape. "
            f"Unset it entirely to store the catalog unencrypted."
        )
    return key


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
    except DECOMPRESS_ERRORS as exc:  # pragma: no cover - implies a broken AEAD
        raise StateCryptoError(f"state blob decrypted but did not decompress: {exc}") from exc


# --------------------------------------------------------------------------
# Format-agnostic entry points -- what statesync actually calls.
# --------------------------------------------------------------------------
def pack(plaintext: bytes, passphrase: str | None) -> bytes:
    """Encrypt when a passphrase is given, otherwise just gzip."""
    if passphrase:
        return encrypt(plaintext, passphrase)
    return PLAIN_MAGIC + gzip.compress(plaintext, 9)


def unpack(blob: bytes, passphrase: str | None) -> bytes:
    """Read either format, whatever the current setting is.

    This is what makes turning encryption on or off a no-op for existing
    data: the blob declares its own format, so a catalog written under one
    setting is still readable under the other (given the key, if encrypted).
    """
    if blob.startswith(PLAIN_MAGIC):
        try:
            return gzip.decompress(blob[len(PLAIN_MAGIC):])
        except DECOMPRESS_ERRORS as exc:
            raise StateCryptoError(f"catalog blob did not decompress: {exc}") from exc

    if blob.startswith(MAGIC):
        if not passphrase:
            raise StateCryptoError(
                f"the stored catalog is encrypted but {ENV_VAR} is not set. "
                f"Set it to the key used when it was written, or delete the "
                f"`data` branch to start fresh unencrypted."
            )
        return decrypt(blob, passphrase)

    raise StateCryptoError(
        "catalog blob has unrecognised magic bytes -- it was not written by this tool"
    )


def describe(blob: bytes) -> str:
    """Human-readable format label, for logs and `doctor`."""
    if blob.startswith(PLAIN_MAGIC):
        return "plain gzip"
    if blob.startswith(MAGIC):
        return "encrypted (AES-256-GCM)"
    return "unknown"
