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
    describe,
    encrypt,
    optional_passphrase_from_env,
    pack,
    passphrase_from_env,
    unpack,
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


class TestOptionalEncryption:
    """`pack`/`unpack` are what statesync calls; they must handle both formats."""

    def test_pack_without_a_passphrase_is_plain_gzip(self):
        import gzip

        blob = pack(DATA, None)
        assert blob.startswith(b"NMPLAIN1")
        assert gzip.decompress(blob[8:]) == DATA

    def test_pack_with_a_passphrase_encrypts(self):
        blob = pack(DATA, PASS)
        assert blob.startswith(b"NMSTATE1")
        assert b"opportunity data" not in blob

    def test_unpack_reads_either_format(self):
        assert unpack(pack(DATA, None), None) == DATA
        assert unpack(pack(DATA, PASS), PASS) == DATA

    def test_a_plain_blob_reads_even_when_a_key_is_configured(self):
        """Setting a key must not break reading data written before it existed."""
        assert unpack(pack(DATA, None), PASS) == DATA

    def test_an_encrypted_blob_without_a_key_says_so(self):
        with pytest.raises(StateCryptoError, match="encrypted but"):
            unpack(pack(DATA, PASS), None)

    def test_unknown_magic_is_rejected(self):
        with pytest.raises(StateCryptoError, match="unrecognised magic"):
            unpack(b"NOTOURS1" + b"\x00" * 64, None)

    def test_a_corrupt_plain_blob_is_caught(self):
        blob = bytearray(pack(DATA, None))
        blob[20] ^= 0xFF
        with pytest.raises(StateCryptoError, match="did not decompress"):
            unpack(bytes(blob), None)

    def test_describe_labels_both_formats(self):
        assert describe(pack(DATA, None)) == "plain gzip"
        assert "encrypted" in describe(pack(DATA, PASS))
        assert describe(b"garbage") == "unknown"

    def test_plain_mode_still_compresses(self):
        assert len(pack(DATA, None)) < len(DATA) / 2


class TestOptionalPassphraseFromEnv:
    def test_unset_is_a_valid_choice(self, monkeypatch):
        monkeypatch.delenv("DATA_ENCRYPTION_KEY", raising=False)
        assert optional_passphrase_from_env() is None

    def test_a_weak_key_still_raises(self, monkeypatch):
        monkeypatch.setenv("DATA_ENCRYPTION_KEY", "hunter2")
        with pytest.raises(StateCryptoError, match="Unset it entirely"):
            optional_passphrase_from_env()

    def test_a_good_key_is_returned(self, monkeypatch):
        monkeypatch.setenv("DATA_ENCRYPTION_KEY", PASS)
        assert optional_passphrase_from_env() == PASS


class TestCorruptionAcrossTheWholeBlob:
    """Damage anywhere must surface as StateCryptoError, never a raw zlib.error.

    `gzip.decompress` raises three unrelated exception types depending on
    where the damage lands, and only one of them is an OSError.
    """

    @pytest.mark.parametrize("frac", [0.0, 0.05, 0.1, 0.2, 0.4, 0.6, 0.8, 0.99])
    def test_plain_blob_corruption_is_always_reported_cleanly(self, frac):
        blob = bytearray(pack(DATA, None))
        index = min(int(len(blob) * frac), len(blob) - 1)
        blob[index] ^= 0xFF
        try:
            recovered = unpack(bytes(blob), None)
        except StateCryptoError:
            return
        # A flipped bit can occasionally still decompress; what must never
        # happen is an unhandled exception type escaping to the caller.
        assert recovered != DATA

    @pytest.mark.parametrize("frac", [0.0, 0.05, 0.1, 0.2, 0.4, 0.6, 0.8, 0.99])
    def test_encrypted_blob_corruption_is_always_reported_cleanly(self, frac):
        blob = bytearray(pack(DATA, PASS))
        index = min(int(len(blob) * frac), len(blob) - 1)
        blob[index] ^= 0xFF
        with pytest.raises(StateCryptoError):
            unpack(bytes(blob), PASS)
