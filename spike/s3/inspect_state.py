"""S3 检查：从 Postgres checkpoint 读出调查状态，逐条核对通过标准。"""

import json
import sys

from langgraph.checkpoint.postgres import PostgresSaver

from app import OBS_MARKER, PG_URL, SUBAGENT_MARKER, build_graph

inv_id = sys.argv[1]
with PostgresSaver.from_conn_string(PG_URL) as saver:
    snap = build_graph(saver).get_state({"configurable": {"thread_id": inv_id}})

v = snap.values
msgs = v["agent_messages"]
blob = json.dumps(msgs, ensure_ascii=False)
tool_calls = [tc["name"] for m in msgs if m["type"] == "ai" for tc in m["data"].get("tool_calls", [])]
task_results = [m["data"]["content"] for m in msgs if m["type"] == "tool" and m["data"].get("name") == "task"]

checks = {
    "已走到结束（next 为空）": snap.next == (),
    "决定已写入": v.get("decision") is not None,
    "Gemini 发起了工具调用": len(tool_calls) > 0,
    "调用了 read_file": "read_file" in tool_calls,
    "read_file 读到了 /obs/ 内容": OBS_MARKER in blob,
    "调用了 task（子 Agent）": "task" in tool_calls,
    "主 Agent messages 不含子 Agent 原始输出": SUBAGENT_MARKER not in blob,
    "子 Agent 结论带 obs_42": any("obs_42" in str(t) for t in task_results),
}
print("工具调用顺序：", tool_calls)
print("子 Agent 交回：", [str(t)[:120] for t in task_results])
print("最终诊断：", v["diagnosis"][:300])
for k, ok in checks.items():
    print(("PASS " if ok else "FAIL ") + k)
