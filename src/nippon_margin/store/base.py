"""Storage contract.

SQLite is the only implementation, but the contract stays written down: it is
what the pipeline codes against, and what makes the pipeline tests readable
without a database in the loop.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from ..models import (
    ChListing,
    FxRate,
    JpListing,
    ModelStats,
    Opportunity,
    RunRecord,
)


class Store(ABC):
    """Every method is idempotent: a re-run must not duplicate or lose data."""

    # -- listings -----------------------------------------------------------
    @abstractmethod
    def upsert_jp(self, listings: list[JpListing]) -> tuple[int, int]:
        """Insert/update. Returns (new, updated)."""

    @abstractmethod
    def upsert_ch(self, listings: list[ChListing]) -> tuple[int, int]:
        ...

    @abstractmethod
    def active_jp(self) -> list[JpListing]:
        ...

    @abstractmethod
    def active_ch(self) -> list[ChListing]:
        ...

    @abstractmethod
    def mark_delisted(self, *, before: datetime) -> int:
        """Flag listings not seen since `before`. Returns the count."""

    # -- derived ------------------------------------------------------------
    @abstractmethod
    def save_opportunities(self, opportunities: list[Opportunity]) -> int:
        ...

    @abstractmethod
    def load_opportunities(self, *, limit: int = 200) -> list[Opportunity]:
        ...

    @abstractmethod
    def save_model_stats(self, stats: list[ModelStats]) -> int:
        ...

    @abstractmethod
    def load_model_stats(self, *, watchlist_key: str | None = None,
                         days: int = 90) -> list[ModelStats]:
        ...

    # -- fx -----------------------------------------------------------------
    @abstractmethod
    def save_fx(self, rate: FxRate) -> None:
        ...

    @abstractmethod
    def load_fx(self, *, days: int = 30) -> list[FxRate]:
        ...

    @abstractmethod
    def latest_fx(self) -> FxRate | None:
        ...

    # -- price history ------------------------------------------------------
    @abstractmethod
    def record_price_change(self, *, doc_id: str, side: str, price: float,
                            at: datetime) -> None:
        ...

    @abstractmethod
    def price_history(self, doc_id: str) -> list[tuple[datetime, float]]:
        ...

    # -- runs + alerts ------------------------------------------------------
    @abstractmethod
    def save_run(self, run: RunRecord) -> None:
        ...

    @abstractmethod
    def recent_runs(self, *, limit: int = 20) -> list[RunRecord]:
        ...

    @abstractmethod
    def alert_sent_at(self, key: str) -> datetime | None:
        ...

    @abstractmethod
    def mark_alert_sent(self, key: str, at: datetime) -> None:
        ...

    def close(self) -> None:  # noqa: B027 - optional teardown, not every store needs it
        """Release resources."""
