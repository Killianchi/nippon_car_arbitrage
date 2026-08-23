"""Git-backed state sync, exercised against a real bare repository.

The daily run has exactly one durable artefact, and this is the code that
writes it. A silent failure here loses every `first_seen` date and every
price-history point in the catalog.
"""

import subprocess
from pathlib import Path

import pytest

from nippon_margin.crypto import StateCryptoError
from nippon_margin.statesync import DATA_BRANCH, STATE_FILE, SyncError, pull, push

KEY = "test-key-that-is-long-enough-1234"


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A working repo whose `origin` is a real bare repo on disk."""
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", KEY)
    bare = tmp_path / "origin.git"
    git("init", "--bare", "--quiet", str(bare))

    work = tmp_path / "work"
    work.mkdir()
    git("init", "--quiet", "--initial-branch", "main", cwd=work)
    git("remote", "add", "origin", str(bare), cwd=work)
    (work / "README.md").write_text("code")
    git("add", "-A", cwd=work)
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "--quiet", "-m", "init", cwd=work)
    git("push", "--quiet", "origin", "main", cwd=work)
    return work


def make_db(path: Path, payload: bytes = b"SQLite format 3\x00catalog") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


class TestRoundTrip:
    def test_push_then_pull_restores_the_bytes(self, repo, tmp_path):
        db = make_db(repo / "data" / "nippon.db")
        assert push(db, repo=repo) is True

        db.unlink()
        assert pull(db, repo=repo) is True
        assert db.read_bytes() == b"SQLite format 3\x00catalog"

    def test_a_second_push_replaces_the_first(self, repo):
        db = make_db(repo / "data" / "nippon.db", b"day one")
        push(db, repo=repo)
        make_db(repo / "data" / "nippon.db", b"day two")
        push(db, repo=repo)

        db.unlink()
        pull(db, repo=repo)
        assert db.read_bytes() == b"day two"

    def test_pull_creates_the_parent_directory(self, repo):
        db = make_db(repo / "data" / "nippon.db")
        push(db, repo=repo)
        import shutil

        shutil.rmtree(repo / "data")
        assert pull(db, repo=repo) is True
        assert db.exists()


class TestFirstRun:
    def test_pull_with_no_data_branch_is_not_an_error(self, repo):
        db = repo / "data" / "nippon.db"
        assert pull(db, repo=repo) is False
        assert not db.exists()


class TestSecrecy:
    def test_the_pushed_blob_is_encrypted(self, repo, tmp_path):
        make_db(repo / "data" / "nippon.db", b"Porsche 911 margin CHF 20891")
        push(repo / "data" / "nippon.db", repo=repo)

        checkout = tmp_path / "peek"
        git("clone", "--quiet", "--branch", DATA_BRANCH,
            str(tmp_path / "origin.git"), str(checkout))
        blob = (checkout / STATE_FILE).read_bytes()
        assert b"Porsche" not in blob
        assert b"20891" not in blob
        assert blob.startswith(b"NMSTATE1")

    def test_the_branch_carries_a_readme_not_just_a_binary(self, repo, tmp_path):
        push(make_db(repo / "data" / "nippon.db"), repo=repo)
        checkout = tmp_path / "peek"
        git("clone", "--quiet", "--branch", DATA_BRANCH,
            str(tmp_path / "origin.git"), str(checkout))
        readme = (checkout / "README.md").read_text()
        assert "Do not merge it into `main`" in readme

    def test_the_data_branch_shares_no_history_with_main(self, repo, tmp_path):
        push(make_db(repo / "data" / "nippon.db"), repo=repo)
        bare = tmp_path / "origin.git"
        # An orphan branch has no merge base with main.
        result = subprocess.run(
            ["git", "merge-base", "main", DATA_BRANCH],
            cwd=bare, capture_output=True, text=True,
        )
        assert result.returncode != 0


class TestFailureModes:
    def test_a_wrong_key_raises_rather_than_starting_fresh(self, repo, monkeypatch):
        db = make_db(repo / "data" / "nippon.db")
        push(db, repo=repo)
        db.unlink()

        monkeypatch.setenv("DATA_ENCRYPTION_KEY", "a-completely-different-key-here")
        with pytest.raises(StateCryptoError):
            pull(db, repo=repo)
        # The catalog must not be silently replaced by an empty one.
        assert not db.exists()

    def test_a_missing_key_raises(self, repo, monkeypatch):
        monkeypatch.delenv("DATA_ENCRYPTION_KEY", raising=False)
        with pytest.raises(StateCryptoError, match="not set"):
            pull(repo / "data" / "nippon.db", repo=repo)

    def test_pushing_a_missing_file_raises(self, repo):
        with pytest.raises(SyncError, match="does not exist"):
            push(repo / "data" / "nowhere.db", repo=repo)

    def test_an_oversized_blob_is_refused(self, repo, monkeypatch):
        import nippon_margin.statesync as sync

        monkeypatch.setattr(sync, "MAX_BLOB_BYTES", 100)
        db = make_db(repo / "data" / "nippon.db", b"x" * 500_000)
        with pytest.raises(SyncError, match="guard"):
            sync.push(db, repo=repo)


class TestCredentialHandling:
    """`push` builds a throwaway repo, so it must supply its own credentials —
    and must never let them reach a log line or an exception message."""

    def test_a_token_is_spliced_into_an_https_origin(self):
        from nippon_margin.statesync import with_token

        assert with_token("https://github.com/o/r", "ghs_secret") == (
            "https://x-access-token:ghs_secret@github.com/o/r"
        )

    def test_no_token_leaves_the_url_alone(self):
        from nippon_margin.statesync import with_token

        assert with_token("https://github.com/o/r", None) == "https://github.com/o/r"
        assert with_token("https://github.com/o/r", "  ") == "https://github.com/o/r"

    def test_an_ssh_origin_is_never_rewritten(self):
        from nippon_margin.statesync import with_token

        assert with_token("git@github.com:o/r.git", "ghs_secret") == "git@github.com:o/r.git"
        assert with_token("ssh://git@github.com/o/r", "ghs_secret") == "ssh://git@github.com/o/r"

    def test_existing_credentials_are_not_doubled_up(self):
        from nippon_margin.statesync import with_token

        assert with_token("https://user:pw@github.com/o/r", "ghs_secret") == (
            "https://user:pw@github.com/o/r"
        )

    def test_a_local_path_remote_is_untouched(self):
        """The tests here push to a bare repo on disk; that must keep working."""
        from nippon_margin.statesync import with_token

        assert with_token("/tmp/origin.git", "ghs_secret") == "/tmp/origin.git"

    def test_redaction_strips_credentials(self):
        from nippon_margin.statesync import redact

        assert redact("https://x-access-token:ghs_abc123@github.com/o/r") == (
            "https://***@github.com/o/r"
        )
        assert "ghs_abc123" not in redact(
            "fatal: could not read https://x-access-token:ghs_abc123@github.com/o/r"
        )

    def test_redaction_leaves_clean_urls_intact(self):
        from nippon_margin.statesync import redact

        assert redact("https://github.com/o/r") == "https://github.com/o/r"

    def test_a_failing_push_does_not_leak_the_token(self, repo, monkeypatch):
        from nippon_margin.statesync import _git, push

        _git("remote", "set-url", "origin",
             "https://github.invalid/nope/nope.git", cwd=repo)
        monkeypatch.setenv("GITHUB_TOKEN", "ghs_supersecret")
        db = make_db(repo / "data" / "nippon.db")
        with pytest.raises(SyncError) as exc:
            push(db, repo=repo)
        assert "ghs_supersecret" not in str(exc.value)
