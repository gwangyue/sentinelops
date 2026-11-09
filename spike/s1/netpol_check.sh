#!/usr/bin/env bash
# S1：从允许 / 不允许的命名空间访问 es-target，看 NetworkPolicy 是否被执行
CTX=kind-sentinel
for ns in sentinel-system other; do kubectl --context $CTX create ns $ns >/dev/null 2>&1; done
kubectl --context $CTX apply -f "$(dirname "$0")/netpol.yaml" >/dev/null
for ns in sentinel-system other; do
  code=$(kubectl --context $CTX -n $ns run probe-$RANDOM --rm -i --restart=Never --image=curlimages/curl:8.10.1 --quiet -- \
    curl -sk -o /dev/null -w '%{http_code}' --max-time 5 https://es-target-es-http.targets:9200 2>/dev/null)
  echo "$ns -> es-target: ${code:-timeout}"
done
