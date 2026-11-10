"""ES 的阈值：从显式集群设置里读，没设置就用 ES 的默认值（05 §3.5"绝对阈值由专家包定义"）。

检测器和 fact builder 共用，保证"检测器触发"和"事实卡片标为异常"用的是同一个标准。
"""

from collections.abc import Mapping
from typing import Any

# ES 9 的默认值
DEFAULTS = {
    "cluster.routing.allocation.disk.watermark.low": 85.0,
    "cluster.routing.allocation.disk.watermark.high": 90.0,
    "cluster.routing.allocation.disk.watermark.flood_stage": 95.0,
    "cluster.max_shards_per_node": 1000.0,
}
SHARD_USAGE_WARN = 0.9  # 分片数达到上限的 90% 就报警（S2 的探针用的也是这个值）


def _explicit(settings_raw: Mapping[str, Any], key: str) -> Any:
    # transient 优先于 persistent，和 ES 的生效顺序一致
    for scope in ("transient", "persistent"):
        v = settings_raw.get(scope, {}).get(key)
        if v is not None:
            return v
    return None


def setting(settings_raw: Mapping[str, Any] | None, key: str) -> float:
    v = _explicit(settings_raw or {}, key)
    if v is None:
        return DEFAULTS[key]
    s = str(v).strip()
    if s.endswith("%"):
        return float(s[:-1])
    try:
        return float(s)
    except ValueError:
        # TODO(edge): 水位也可以写成剩余空间（例如 "10gb"），要结合磁盘总量换算；先退回默认值
        return DEFAULTS[key]
