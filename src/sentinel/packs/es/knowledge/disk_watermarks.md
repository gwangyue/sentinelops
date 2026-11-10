---
title: Disk watermarks (low, high, flood stage)
detectors: [es.disk_watermark, es.unassigned_shards]
supports: ">=9.0,<10"
source: "Elastic docs: Watermark errors — https://www.elastic.co/docs/troubleshoot/elasticsearch/fix-watermark-errors (read 2026-11-10)"
---
## Thresholds
- Low watermark (default 85%): no new shards are allocated to the node.
- High watermark (default 90%): Elasticsearch tries to move shards away from the node.
- Flood stage (default 95%): every index with a shard on the node gets the index.blocks.read_only_allow_delete block; writes fail with HTTP 429 cluster_block_exception ("disk usage exceeded flood-stage watermark, index has read-only-allow-delete block").

## How to recognize it
Disk usage per node from the cat allocation API; the allocation explain API for shards that cannot be placed; the read_only_allow_delete block in index settings at flood stage. Elasticsearch removes the flood-stage block automatically once disk usage on the node drops below the high watermark.

## Resolution
1. Temporary relief: raise the low, high and flood-stage watermarks in the cluster settings, then remove the read_only_allow_delete block. Reset the watermarks to defaults once a permanent fix is in place.
2. Watch relocating shards in cluster health and recovery progress; if shards stay on the node, use cat shards and allocation explain.
3. Permanent fix: scale out the data tier or add disk; delete or snapshot-and-delete indices that are no longer needed.
