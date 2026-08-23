"""Storage.

One backend: SQLite. The database is durable across ephemeral Actions runners
because the daily workflow pulls it from -- and pushes it back to -- an
encrypted blob on the repository's `data` branch. See `statesync.py`.

The `Store` ABC is kept even with a single implementation: it is the written
contract the pipeline codes against, and it is what makes the pipeline tests
readable without a database in the loop.
"""

from __future__ import annotations

from ..config import Config
from .base import Store
from .sqlite_store import SqliteStore

DEFAULT_DB_PATH = "data/nippon.db"


def open_store(cfg: Config, *, db_path: str | None = None) -> Store:
    return SqliteStore(db_path or DEFAULT_DB_PATH)


__all__ = ["Store", "SqliteStore", "open_store", "DEFAULT_DB_PATH"]
