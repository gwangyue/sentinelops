#!/usr/bin/env bash
# 干扰项 X3：一个限速的 _update_by_query，在 _tasks 里显示为长时间运行的任务，和故障无关。
# requests_per_second=1 加上 scroll_size=10：每批 10 条要等 10 秒，2000 条要跑半个多小时，录制期间一直在。
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
source "$HERE/_lib.sh"
case ${1:?inject|revert} in
  inject)
    es -XPOST "$U/bench/_update_by_query?conflicts=proceed&wait_for_completion=false&requests_per_second=1&scroll_size=10" \
      -d '{"query":{"match_all":{}}}' >/dev/null
    sleep 12 ;;  # 让任务跑过 _tasks 工具"长时间运行"的 10 秒门槛
  revert)
    es -XPOST "$U/_tasks/_cancel?actions=*byquery" >/dev/null || true ;;
esac
