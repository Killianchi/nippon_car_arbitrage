"""Alert delivery channels."""

from __future__ import annotations

import os


def env_id(name: str) -> str:
    """An identifier-shaped secret from the environment, whitespace stripped.

    Pasting a secret into the GitHub UI -- especially on a phone -- routinely
    picks up a trailing newline. That newline then lands in a URL and httpx
    refuses it with `Invalid non-printable ASCII character in URL`, which
    looks nothing like "your secret has a stray newline". Tokens, chat ids and
    usernames never legitimately contain surrounding whitespace, so strip it.
    """
    return os.environ.get(name, "").strip()


def env_secret(name: str) -> str:
    """A free-form secret: only line endings are stripped.

    Unlike an identifier, a password may legitimately contain spaces, so this
    removes just the newline a copy-paste tends to append.
    """
    return os.environ.get(name, "").strip("\r\n")


__all__ = ["env_id", "env_secret"]
