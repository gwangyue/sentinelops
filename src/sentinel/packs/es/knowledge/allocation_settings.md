---
title: Cluster-level shard allocation settings and rolling restarts
detectors: [es.unassigned_shards]
supports: ">=9.0,<10"
source: "Elastic docs: Full cluster restart and rolling restart procedures — https://www.elastic.co/docs/deploy-manage/maintenance/start-stop-services/full-cluster-restart-rolling-restart-procedures ; Red or yellow cluster health status (read 2026-11-10)"
---
## cluster.routing.allocation.enable
Controls which shards may be allocated: all (default), primaries, new_primaries, or none. With none, no shards are allocated, including replicas of restarted nodes and shards of new indices. With primaries, only primary shards are allocated, so replicas stay unassigned and the cluster stays yellow.

## Rolling restart procedure
Step 1 sets cluster.routing.allocation.enable to primaries before stopping a node. After the node rejoins, the setting must be removed (set to null) so replica allocation resumes. Forgetting the re-enable step leaves replicas unassigned indefinitely.

## How to check
Read the persistent and transient cluster settings for cluster.routing.allocation.enable, and use the allocation explain API on an unassigned shard.
