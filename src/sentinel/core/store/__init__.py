"""调查、审计、版本、观测索引的存储（01 §2 第⑤层）。"""

import os

from sentinel.core.store.base import GENESIS_HASH, InvestigationRecord, Store, event_hash
from sentinel.core.store.memory import MemoryStore


def open_store() -> Store:
    """SENTINEL_PG_URL 有值就用 Postgres；本地 compose 默认是 postgresql://postgres:sentinel-local@localhost/sentinel。"""
    from sentinel.core.store.postgres import PgStore

    url = os.environ.get("SENTINEL_PG_URL", "postgresql://postgres:sentinel-local@localhost:5432/sentinel")
    store = PgStore(url)
    store.init()
    return store


__all__ = ["GENESIS_HASH", "InvestigationRecord", "MemoryStore", "Store", "event_hash", "open_store"]
