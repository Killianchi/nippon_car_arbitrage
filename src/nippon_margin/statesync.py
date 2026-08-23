"""The catalog lives in git.

Actions runners are ephemeral, so state has to go somewhere durable. Rather
than a database service, the SQLite file is committed — encrypted — to an
orphan `data` branch of this repository.

Why an orphan branch: it shares no history with `main`, so the state blob
never appears in a code diff, never triggers CI, and can be force-pushed on a
schedule without touching the source history. `--depth 1` fetches only the
latest blob, so the clone stays small no matter how many days accumulate.

Why encrypted: the repository is public. See `crypto.py`.

The daily run is:

    sync pull  ->  scrape  ->  analyze  ->  export  ->  sync push
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .crypto import StateCryptoError, decrypt, encrypt, passphrase_from_env

log = logging.getLogger(__name__)

DATA_BRANCH = "data"
STATE_FILE = "nippon.db.enc"
#: Git refuses pushes over ~100 MB and warns past 50; the state blob is a few
#: hundred KB, so anything near this means something has gone wrong.
MAX_BLOB_BYTES = 40 * 1024 * 1024


class SyncError(RuntimeError):
    pass


#: Anything that looks like credentials in a URL, so a git error message can
#: never carry a token into a log or an exception.
_SECRET = re.compile(r"(https?://)[^/@\s]*@")


def redact(text: str) -> str:
    return _SECRET.sub(r"\1***@", text)


def _git(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise SyncError(
            f"git {redact(' '.join(args))} failed ({result.returncode}): {redact(detail)}"
        )
    return result


def with_token(url: str, token: str | None) -> str:
    """Splice an Actions token into an https remote URL.

    `push` builds a throwaway repository rather than reusing the checkout, so
    it does not inherit the credentials `actions/checkout` installs. Without
    this the push fails with a 403 on a schedule nobody is watching.

    SSH remotes and URLs that already carry credentials are returned as-is.
    """
    token = (token or "").strip()
    if not token or not url.startswith(("http://", "https://")):
        return url
    parts = urlsplit(url)
    if "@" in parts.netloc:
        return url
    return urlunsplit(
        (parts.scheme, f"x-access-token:{token}@{parts.netloc}",
         parts.path, parts.query, parts.fragment)
    )


def _remote_url(repo: Path) -> str:
    url = _git("remote", "get-url", "origin", cwd=repo).stdout.strip()
    return with_token(url, os.environ.get("GITHUB_TOKEN"))


def pull(db_path: Path, *, repo: Path | None = None) -> bool:
    """Fetch and decrypt the state blob into `db_path`.

    Returns True when state was restored, False on a genuinely empty history
    (the first run). A wrong key or a corrupt blob raises instead: silently
    starting from scratch would throw away the catalog and, with it, every
    `first_seen` date and price history point.
    """
    repo = repo or Path.cwd()
    passphrase = passphrase_from_env()

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "state"
        result = _git(
            "clone", "--branch", DATA_BRANCH, "--depth", "1", "--single-branch",
            _remote_url(repo), str(work), check=False,
        )
        if result.returncode != 0:
            log.info("no `%s` branch yet -- starting a fresh catalog", DATA_BRANCH)
            return False

        blob = work / STATE_FILE
        if not blob.exists():
            log.warning("`%s` branch exists but holds no %s", DATA_BRANCH, STATE_FILE)
            return False

        plaintext = decrypt(blob.read_bytes(), passphrase)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_path.write_bytes(plaintext)
        log.info("restored %s (%.0f KB) from the %s branch",
                 db_path, len(plaintext) / 1024, DATA_BRANCH)
        return True


def push(db_path: Path, *, repo: Path | None = None, message: str | None = None) -> bool:
    """Encrypt `db_path` and force-push it to the `data` branch.

    The branch is rewritten each run rather than appended to: we want the
    latest state, not a growing pile of multi-hundred-KB binaries. Point-in-time
    history lives *inside* the database (`price_history`, `model_stats_daily`),
    where it can actually be queried.
    """
    repo = repo or Path.cwd()
    if not db_path.exists():
        raise SyncError(f"nothing to push: {db_path} does not exist")

    passphrase = passphrase_from_env()
    plaintext = db_path.read_bytes()
    blob = encrypt(plaintext, passphrase)
    if len(blob) > MAX_BLOB_BYTES:
        raise SyncError(
            f"state blob is {len(blob) / 1e6:.0f} MB, over the {MAX_BLOB_BYTES / 1e6:.0f} MB "
            f"guard. Prune the catalog before pushing."
        )

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "state"
        work.mkdir(parents=True)
        _git("init", "--quiet", "--initial-branch", DATA_BRANCH, cwd=work)
        _git("remote", "add", "origin", _remote_url(repo), cwd=work)

        (work / STATE_FILE).write_bytes(blob)
        (work / "README.md").write_text(
            "# nippon-margin state\n\n"
            "`nippon.db.enc` is the scraper's SQLite catalog, gzipped and encrypted\n"
            "with AES-256-GCM (see `src/nippon_margin/crypto.py` on `main`).\n\n"
            "This branch is force-pushed by the daily workflow and shares no history\n"
            "with the source. Do not merge it into `main`.\n\n"
            "Restore locally with:\n\n"
            "    DATA_ENCRYPTION_KEY=... nippon-margin sync pull\n",
            encoding="utf-8",
        )
        _git("add", "-A", cwd=work)
        _git("-c", "user.email=actions@github.com", "-c", "user.name=nippon-margin",
             "commit", "--quiet", "-m", message or "Update catalog state", cwd=work)
        _git("push", "--force", "origin", DATA_BRANCH, cwd=work)

    log.info("pushed %.0f KB of encrypted state (%.0f KB raw) to the %s branch",
             len(blob) / 1024, len(plaintext) / 1024, DATA_BRANCH)
    return True


def wipe_local(db_path: Path) -> None:
    """Remove the decrypted database. Called after a push in CI."""
    if db_path.exists():
        db_path.unlink()
    shutil.rmtree(db_path.parent / "__pycache__", ignore_errors=True)


__all__ = ["pull", "push", "wipe_local", "with_token", "redact", "SyncError",
           "StateCryptoError", "DATA_BRANCH", "STATE_FILE"]
