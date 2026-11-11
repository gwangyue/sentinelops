"""调查、审计、版本、观测索引的存储（01 §2 第⑤层）。"""

import os

from sentinel.core.store.base import InvestigationRecord, Store
from sentinel.core.store.memory import MemoryStore

# 哈希链的规则在 governance（执行器也写同一条链，它不能依赖 core）；这里重新导出，原来的调用方不用改
from sentinel.governance.audit import GENESIS_HASH, event_hash


def open_store() -> Store:
    """SENTINEL_PG_URL 有值就用 Postgres；本地 compose 默认是 postgresql://postgres:sentinel-local@localhost/sentinel。"""
    from sentinel.core.store.postgres import PgStore

    url = os.environ.get("SENTINEL_PG_URL", "postgresql://postgres:sentinel-local@localhost:5432/sentinel")
    store = PgStore(url)
    store.init()
    return store


__all__ = ["GENESIS_HASH", "InvestigationRecord", "MemoryStore", "Store", "event_hash", "open_store"]
