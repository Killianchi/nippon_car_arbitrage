"""State-blob encryption.

The repository is public. If this is wrong, the catalog is readable by
anyone who clones it, so it gets the same scrutiny as the cost engine.
"""

import gzip

import pytest

from nippon_margin.crypto import (
    MAGIC,
    StateCryptoError,
    decrypt,
    encrypt,
    passphrase_from_env,
)

PASS = "correct-horse-battery-staple-32chars"
DATA = b"SQLite format 3\x00" + b"opportunity data" * 500


class TestRoundTrip:
    def test_round_trips(self):
        assert decrypt(encrypt(DATA, PASS), PASS) == DATA

    def test_empty_payload_round_trips(self):
        assert decrypt(encrypt(b"", PASS), PASS) == b""

    def test_output_is_compressed(self):
        """A SQLite file is mostly zeroes; committing it raw would be wasteful."""
        assert len(encrypt(DATA, PASS)) < len(DATA) / 2


class TestSecrecy:
    def test_plaintext_does_not_appear_in_the_blob(self):
        blob = encrypt(b"Porsche 911 landed CHF 20891", PASS)
        assert b"Porsche" not in blob
        assert b"20891" not in blob

    def test_two_writes_of_the_same_data_differ(self):
        """Fresh salt and nonce per write: git history leaks no change size."""
        a, b = encrypt(DATA, PASS), encrypt(DATA, PASS)
        assert a != b
        assert decrypt(a, PASS) == decrypt(b, PASS) == DATA

    def test_a_wrong_passphrase_is_rejected(self):
        blob = encrypt(DATA, PASS)
        with pytest.raises(StateCryptoError, match="wrong DATA_ENCRYPTION_KEY"):
            decrypt(blob, "not-the-passphrase-at-all-really")


class TestTamperDetection:
    def test_a_flipped_ciphertext_byte_is_caught(self):
        blob = bytearray(encrypt(DATA, PASS))
        blob[-5] ^= 0x01
        with pytest.raises(StateCryptoError):
            decrypt(bytes(blob), PASS)

    def test_a_flipped_magic_byte_is_caught(self):
        blob = bytearray(encrypt(DATA, PASS))
        blob[2] ^= 0x01
        with pytest.raises(StateCryptoError, match="magic bytes"):
            decrypt(bytes(blob), PASS)

    def test_a_swapped_nonce_is_caught(self):
        a = bytearray(encrypt(DATA, PASS))
        b = encrypt(DATA, PASS)
        a[24:36] = b[24:36]
        with pytest.raises(StateCryptoError):
            decrypt(bytes(a), PASS)

    def test_truncation_is_caught(self):
        blob = encrypt(DATA, PASS)
        with pytest.raises(StateCryptoError):
            decrypt(blob[:-20], PASS)

    def test_a_foreign_file_is_rejected_clearly(self):
        with pytest.raises(StateCryptoError, match="magic bytes"):
            decrypt(gzip.compress(bytes(range(256)) * 40), PASS)

    def test_a_tiny_file_is_rejected(self):
        with pytest.raises(StateCryptoError, match="too short"):
            decrypt(MAGIC + b"\x00" * 8, PASS)


class TestPassphraseFromEnv:
    def test_missing_key_explains_how_to_make_one(self, monkeypatch):
        monkeypatch.delenv("DATA_ENCRYPTION_KEY", raising=False)
        with pytest.raises(StateCryptoError, match="token_urlsafe"):
            passphrase_from_env()

    def test_a_short_key_is_refused(self, monkeypatch):
        monkeypatch.setenv("DATA_ENCRYPTION_KEY", "short")
        with pytest.raises(StateCryptoError, match="too short"):
            passphrase_from_env()

    def test_surrounding_whitespace_is_stripped(self, monkeypatch):
        monkeypatch.setenv("DATA_ENCRYPTION_KEY", f"  {PASS}\n")
        assert passphrase_from_env() == PASS
