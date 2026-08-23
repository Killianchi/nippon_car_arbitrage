"""Secret handling in the alert channels.

Pasting a secret into the GitHub UI -- especially on a phone -- routinely
appends a newline. That newline reaches the Telegram URL and httpx rejects it
with `Invalid non-printable ASCII character in URL, '\\n' at position 74`,
which gives no hint that the cause is a stray newline in a secret. This
happened on a real run; these tests are why it cannot happen again.
"""

import httpx
import pytest

from nippon_margin.alerting import email as email_channel
from nippon_margin.alerting import env_id, env_secret, telegram
from nippon_margin.config import EmailConfig

TOKEN = "8880807589:AAGXuoTESTTOKENvalue-not-real-abcdef"
CHAT = "1021777214"


class TestEnvHelpers:
    def test_env_id_strips_a_trailing_newline(self, monkeypatch):
        monkeypatch.setenv("X", TOKEN + "\n")
        assert env_id("X") == TOKEN

    @pytest.mark.parametrize("suffix", ["\n", "\r\n", " ", "\t", "  \n"])
    def test_env_id_strips_every_flavour_of_whitespace(self, monkeypatch, suffix):
        monkeypatch.setenv("X", f" {TOKEN}{suffix}")
        assert env_id("X") == TOKEN

    def test_env_id_of_an_unset_variable_is_empty(self, monkeypatch):
        monkeypatch.delenv("X", raising=False)
        assert env_id("X") == ""

    def test_env_secret_keeps_interior_and_edge_spaces(self):
        """A password may legitimately contain spaces; only newlines go."""
        import os

        os.environ["X"] = " correct horse battery staple \n"
        try:
            assert env_secret("X") == " correct horse battery staple "
        finally:
            del os.environ["X"]


class TestTelegramConfigured:
    def test_a_newline_suffixed_token_still_counts_as_configured(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN + "\n")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT + "\n")
        assert telegram.configured() is True

    def test_whitespace_only_is_not_configured(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "  \n")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
        assert telegram.configured() is False

    def test_unset_is_not_configured(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        assert telegram.configured() is False


class TestTelegramSend:
    def test_the_url_never_carries_a_newline(self, monkeypatch):
        """The exact regression: httpx refuses a URL containing '\\n'."""
        seen = {}

        def fake_post(url, **kwargs):
            seen["url"] = url
            seen["json"] = kwargs.get("json")
            return httpx.Response(200, json={"ok": True},
                                  request=httpx.Request("POST", "https://x"))

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN + "\n")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT + "\n")
        monkeypatch.setattr(httpx, "post", fake_post)

        assert telegram.send("hello") is True
        assert "\n" not in seen["url"]
        assert seen["url"] == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        assert seen["json"]["chat_id"] == CHAT

    def test_a_real_httpx_call_would_accept_the_cleaned_url(self, monkeypatch):
        """Guard the property directly: httpx rejects a URL with a raw newline."""
        with pytest.raises(httpx.InvalidURL):
            httpx.Request("POST", f"https://api.telegram.org/bot{TOKEN}\n/sendMessage")
        # ...and accepts the stripped form.
        httpx.Request("POST", f"https://api.telegram.org/bot{TOKEN}/sendMessage")

    def test_send_without_configuration_returns_false(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        assert telegram.send("hello") is False

    def test_a_long_message_is_split_on_line_boundaries(self):
        text = "\n".join(f"line {i}" for i in range(2000))
        chunks = telegram._split(text)
        assert len(chunks) > 1
        assert all(len(c) <= telegram.MAX_LEN for c in chunks)
        # Splitting must not lose or mangle any line.
        assert "\n".join(chunks) == text


class TestEmailConfigured:
    def test_a_newline_suffixed_password_still_counts(self, monkeypatch):
        monkeypatch.setenv("SMTP_PASSWORD", "hunter2\n")
        cfg = EmailConfig(smtp_host="smtp.x", from_addr="a@x", to_addr="b@x")
        assert email_channel.configured(cfg) is True

    def test_missing_password_is_not_configured(self, monkeypatch):
        monkeypatch.delenv("SMTP_PASSWORD", raising=False)
        cfg = EmailConfig(smtp_host="smtp.x", from_addr="a@x", to_addr="b@x")
        assert email_channel.configured(cfg) is False
