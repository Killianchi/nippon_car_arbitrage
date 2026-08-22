"""Storage backends. `--local` selects SQLite; otherwise Firestore."""

from __future__ import annotations

from ..config import Config
from .base import Store


def open_store(cfg: Config, *, local: bool = False, db_path: str | None = None) -> Store:
    if local:
        from .sqlite_store import SqliteStore

        return SqliteStore(db_path or "data/nippon.db")

    from .firestore_store import FirestoreStore

    return FirestoreStore(cfg.meta.firebase_project_id or None)


__all__ = ["Store", "open_store"]
