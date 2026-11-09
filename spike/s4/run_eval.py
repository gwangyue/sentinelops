# /// script
# requires-python = ">=3.12"
# dependencies = ["langsmith==0.10.10", "google-genai"]
# ///
"""S4-2：验证 evaluate() 的 num_repetitions / metadata / experiment_prefix，以及重复运行是否被缓存。
target 直接调 google-genai（spike 用，正式实现要走 core/llm 封装，01 §3.4）。"""
import argparse, os
from google import genai
from google.genai import types
from langsmith import Client, evaluate

MODEL = "gemini-3.7-flash"  # ADR-008：2026-11-09 选定的固定型号
ap = argparse.ArgumentParser()
ap.add_argument("--version", required=True)
args = ap.parse_args()
assert not os.environ.get("LANGSMITH_TEST_CACHE"), "被测 Agent 不开缓存（08 §6.4）"

g = genai.Client()

def target(inputs: dict) -> dict:
    # temperature=1.0：故意让输出有随机性，用来判断重复运行是否真的重新调用了模型
    r = g.models.generate_content(
        model=MODEL,
        contents=f"你是 ES 运维专家。症状：{inputs['symptom']}\n用一句话给出最可能的根因和第一步排查动作。",
        config=types.GenerateContentConfig(temperature=1.0, max_output_tokens=200),
    )
    return {"answer": r.text}

def mentions_label_keyword(outputs: dict, reference_outputs: dict) -> dict:
    # 确定性评分器：只看答案里有没有出现根因标签的关键词，够 spike 用
    kw = reference_outputs["root_cause_label"].split(".")[-1].split("_")[0]
    return {"key": "kw_hit", "score": int(kw.lower() in outputs["answer"].lower())}

c = Client()
res = evaluate(
    target,
    data=c.list_examples(dataset_name="sentinel-spike", as_of="ds-spike-2", splits=["dev"]),
    evaluators=[mentions_label_keyword],
    num_repetitions=3,
    max_concurrency=4,
    metadata={"agent_version": args.version, "dataset_version": "ds-spike-2"},
    experiment_prefix=args.version,
    client=c,
)
print("experiment:", res.experiment_name)
