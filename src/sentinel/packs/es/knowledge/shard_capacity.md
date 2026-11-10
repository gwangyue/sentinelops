---
title: Shard capacity limits (max shards per node)
detectors: [es.shard_limit]
supports: ">=9.0,<10"
source: "Elastic docs: Troubleshoot shard capacity health issues — https://www.elastic.co/docs/troubleshoot/elasticsearch/troubleshooting-shards-capacity-issues (read 2026-11-10)"
---
## The limit
cluster.max_shards_per_node limits the number of open shards per data node outside the frozen tier (cluster.max_shards_per_node.frozen applies to the frozen tier). The cluster-wide limit is this value times the number of data nodes.

## Symptoms
The health report warns that the cluster is close to reaching the configured maximum number of shards for data nodes. Once the limit is reached, creating an index (or adding replicas) fails because the action would exceed the maximum normal shards open.

## Investigation
Compare active shards with cluster.max_shards_per_node times the number of data nodes; check whether the setting was lowered from its default in the cluster settings.

## Resolution
Reduce the number of shards (delete or shrink unneeded indices, use fewer primary shards), add data nodes, or temporarily increase the limit through the cluster settings and reset it once the shard count is reduced.
