---
title: Troubleshooting unassigned shards (yellow / red cluster)
detectors: [es.unassigned_shards, es.node_count, es.disk_watermark]
supports: ">=9.0,<10"
source: "Elastic docs: Red or yellow cluster health status — https://www.elastic.co/docs/troubleshoot/elasticsearch/red-yellow-cluster-status (read 2026-11-10)"
---
## What the health status means
Green means there are no unassigned shards. Yellow means only replica shards are unassigned. Red means one or more primary shards are unassigned.

## Diagnosis order
1. Check cluster health and the count of unassigned shards.
2. List shards and find those in state UNASSIGNED; prirep "p" is a primary, "r" is a replica.
3. For each unassigned shard, call the cluster allocation explain API with the index, shard number and whether it is a primary.
4. Review allocation-related index settings and cluster settings.
5. Check disk usage per node.
6. Check whether shard allocation has been disabled in the cluster settings.

## Common causes
- A replica never shares a node with its primary: when the number of copies (primary plus replicas) exceeds the number of data nodes, the extra replicas cannot be assigned.
- A node left the cluster (restart, overload or GC pauses, network issues). Its shard copies become unassigned until it rejoins; allocation of replicas is delayed by one minute by default.
- Misconfigured allocation settings: index-level allocation filtering, allocation awareness, or cluster.routing.allocation.enable.
- A data node's disk is above the low watermark (default 85%), so no new shards are allocated to it.
- High JVM memory pressure can trip circuit breakers that stop allocation.

## Fixes (by cause)
- Replicas exceed nodes: add a data node, or lower index.number_of_replicas for that index.
- Node left: recover the node; it rejoins and shards are reassigned.
- Allocation disabled after maintenance: reset cluster.routing.allocation.enable to null in the persistent settings.
- Disk watermark: free or add disk, or add nodes. Raising the watermarks is only a temporary fix.
