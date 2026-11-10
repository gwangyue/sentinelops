---
title: Nodes leaving the cluster
detectors: [es.node_count, es.unassigned_shards]
supports: ">=9.0,<10"
source: "Elastic docs: Troubleshoot an unstable cluster — https://www.elastic.co/docs/troubleshoot/elasticsearch/troubleshooting-unstable-cluster ; Red or yellow cluster health status (read 2026-11-10)"
---
## Symptoms
A node normally leaves only when deliberately shut down. Unexpected departures show as a lower node count, yellow or red health, and unassigned or initializing shards for the copies that lived on the missing node.

## Typical causes
- Disconnected: the connection from the master to the node closed (firewall timeouts, packet loss, congestion, network partition).
- Lagging: the node did not apply a cluster state update in time (cluster.follower_lag.timeout, default 90s).
- Follower checks failed: three consecutive health checks failed (GC pauses, VM pauses, network issues).
- Restarts: the node restarted and rejoins.

## Investigation
Compare the current node list with the expected one to identify which node is missing. The master's logs (node-left / node-join with a reason) explain why; they are not available through the read-only APIs, so the root cause of the departure itself may need a human.

## Resolution
Recover or restart the missing node; once it rejoins, its shards are reassigned. Replica allocation after a node leaves is delayed by one minute by default.
