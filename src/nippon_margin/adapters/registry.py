"""Adapter registry: name -> constructed adapter, honouring config.

`build_adapters` is the single place that decides what runs. `--source X`
narrows it; `enabled: false` in config.yaml removes a source without a code
change, which is what you want at 07:00 when a site has started returning
captchas.
"""

from __future__ import annotations

import logging

from ..config import Config, SourceConfig
from ..http import Fetcher
from .base import Adapter
from .ch.autouncle import AutoUncleAdapter
from .declarative import DeclarativeChAdapter, DeclarativeJpAdapter
from .jp.beforward import BeForwardAdapter
from .jp.carused import CarusedAdapter
from .jp.exportfrom import ExportFromAdapter
from .jp.sbtjapan import SbtJapanAdapter
from .specs import CH_SPECS, JP_SPECS

log = logging.getLogger(__name__)

#: Hand-written adapters, verified against captured markup (see tests/fixtures).
JP_ADAPTERS = {
    "exportfrom": ExportFromAdapter,
    "carused": CarusedAdapter,
    "beforward": BeForwardAdapter,
    "sbtjapan": SbtJapanAdapter,
}
CH_ADAPTERS = {
    "autouncle": AutoUncleAdapter,
}


def all_source_names() -> list[str]:
    return sorted({*JP_ADAPTERS, *JP_SPECS, *CH_ADAPTERS, *CH_SPECS})


def build_adapters(cfg: Config, fetcher: Fetcher, *, only: str | None = None,
                   side: str | None = None) -> list[Adapter]:
    """Every adapter that should run, in the order they should run."""
    out: list[Adapter] = []

    def add(name: str, source_cfg: SourceConfig, adapter: Adapter | None) -> None:
        if adapter is None:
            return
        if only and name != only:
            return
        if not source_cfg.enabled and name != only:
            return
        if side and adapter.side != side:
            return
        out.append(adapter)

    for name, source_cfg in cfg.sources.japan.items():
        if name in JP_ADAPTERS:
            add(name, source_cfg, JP_ADAPTERS[name](cfg, source_cfg, fetcher))
        elif name in JP_SPECS:
            add(name, source_cfg,
                DeclarativeJpAdapter(cfg, source_cfg, fetcher, JP_SPECS[name]))
        else:
            log.warning("no adapter for japan source %r in config.yaml", name)

    for name, source_cfg in cfg.sources.switzerland.items():
        if name in CH_ADAPTERS:
            add(name, source_cfg, CH_ADAPTERS[name](cfg, source_cfg, fetcher))
        elif name in CH_SPECS:
            add(name, source_cfg,
                DeclarativeChAdapter(cfg, source_cfg, fetcher, CH_SPECS[name]))
        else:
            log.warning("no adapter for swiss source %r in config.yaml", name)

    if only and not out:
        raise SystemExit(
            f"unknown source {only!r}. Known sources: {', '.join(all_source_names())}"
        )
    return out
