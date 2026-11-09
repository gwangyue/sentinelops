# /// script
# requires-python = ">=3.12"
# dependencies = ["langsmith==0.10.10"]
# ///
"""S4-1：验证数据集版本标签 + split（06 §9 的拉取方式）。
只负责建一个假数据集并打两个标签；不涉及真实用例格式（EvalExample 在 D4 实现）。"""
import time
from langsmith import Client

DS = "sentinel-spike"
c = Client()
if c.has_dataset(dataset_name=DS):
    c.delete_dataset(dataset_name=DS)
ds = c.create_dataset(DS, description="spike S4: fake examples, safe to delete")

def ex(q, label, split):
    return {"inputs": {"symptom": q}, "outputs": {"root_cause_label": label}, "split": split}

c.create_examples(dataset_id=ds.id, examples=[
    ex("3 个分片未分配，data-2 节点磁盘使用率 93%", "es.disk_high_watermark", "dev"),
    ex("集群状态 red，有一个节点离开集群", "es.node_left", "dev"),
    ex("索引创建失败：this action would add [2] shards, but this cluster currently has [1000]/[1000]", "es.shard_limit", "heldout"),
])
time.sleep(2)
# 标签必须绑定到某个版本的时间点。坑：dataset.modified_at 不随添加用例前进，
# 要用 list_dataset_versions() 里最新版本的 as_of（S4 实测）
def latest_version_as_of():
    return max(v.as_of for v in c.list_dataset_versions(dataset_id=ds.id))

c.update_dataset_tag(dataset_id=ds.id, as_of=latest_version_as_of(), tag="ds-spike-1")

c.create_examples(dataset_id=ds.id, examples=[ex("分片迁移卡住，cluster.routing.allocation.enable=none", "es.allocation_disabled", "dev")])
time.sleep(2)
c.update_dataset_tag(dataset_id=ds.id, as_of=latest_version_as_of(), tag="ds-spike-2")

def n(**kw):
    return len(list(c.list_examples(dataset_id=ds.id, **kw)))

print("as_of=ds-spike-1 splits=[dev]     n =", n(as_of="ds-spike-1", splits=["dev"]), "(期望 2)")
print("as_of=ds-spike-1 splits=[heldout] n =", n(as_of="ds-spike-1", splits=["heldout"]), "(期望 1)")
print("as_of=ds-spike-1 全部             n =", n(as_of="ds-spike-1"), "(期望 3)")
print("as_of=ds-spike-2 全部             n =", n(as_of="ds-spike-2"), "(期望 4)")
print("as_of=ds-spike-2 splits=[dev]     n =", n(as_of="ds-spike-2", splits=["dev"]), "(期望 3)")
