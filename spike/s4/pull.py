# /// script
# requires-python = ">=3.12"
# dependencies = ["langsmith==0.10.10"]
# ///
"""S4-3：按 metadata.agent_version 筛选实验，并用 SDK 把 runs + feedback 拉到本地（06 §9 的“统计”环节）。"""
import argparse, json
from collections import defaultdict
from langsmith import Client

ap = argparse.ArgumentParser()
ap.add_argument("--version", required=True)
ap.add_argument("--out", required=True)
args = ap.parse_args()

c = Client()
projs = list(c.list_projects(reference_dataset_name="sentinel-spike", metadata={"agent_version": args.version}))
print("按 agent_version 筛选到的实验：", [p.name for p in projs])
assert len(projs) == 1, f"期望只有 1 个实验，实际 {len(projs)} 个"

runs = list(c.list_runs(project_id=projs[0].id, is_root=True))
fb = defaultdict(list)
for f in c.list_feedback(run_ids=[r.id for r in runs]):
    fb[str(f.run_id)].append({"key": f.key, "score": f.score})

by_ex = defaultdict(lambda: {"outputs": [], "feedback": []})
for r in runs:
    e = by_ex[str(r.reference_example_id)]
    e["outputs"].append((r.outputs or {}).get("answer"))
    e["feedback"].extend(fb[str(r.id)])
data = {"experiment": projs[0].name, "metadata": projs[0].metadata,
        "examples": [{"example_id": k, **v} for k, v in by_ex.items()]}
json.dump(data, open(args.out, "w"), ensure_ascii=False, indent=2)
for e in data["examples"]:
    print(e["example_id"][:8], "runs =", len(e["outputs"]), "distinct =", len(set(e["outputs"])), "feedback =", len(e["feedback"]))
