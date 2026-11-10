"""记忆快照（05 §4.3）：导出成 JSONL，或从 JSONL 加载到一个索引里（评测时复现"调查开始时的记忆状态"）。"""

import json
from pathlib import Path

from sentinel.contracts import MemoryRecord
from sentinel.core.memory.store import MemoryStore, StoredMemory


def export(store: MemoryStore, namespace: str, path: Path) -> int:
    rows = store.all(namespace)
    path.write_text(
        "".join(
            json.dumps(
                {"record": m.record.model_dump(mode="json"), "embedding": m.embedding, "harm_count": m.harm_count}
            )
            + "\n"
            for m in rows
        ),
        encoding="utf-8",
    )
    return len(rows)


def load(store: MemoryStore, path: Path) -> int:
    n = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        d = json.loads(line)
        store.put(StoredMemory(MemoryRecord.model_validate(d["record"]), d["embedding"], d.get("harm_count", 0)))
        n += 1
    return n
