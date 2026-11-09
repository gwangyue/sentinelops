# /// script
# requires-python = ">=3.12"
# dependencies = ["google-genai"]
# ///
"""S3 附带：确认 gemini-3.7-flash 的思考 token 是否占用 max_output_tokens（S4 中出现了截断）。
这决定 core/llm 的预算该按什么口径计算（01 §3.4）。"""
from google import genai
from google.genai import types

g = genai.Client()
q = "ES 集群 3 个分片未分配，data-2 节点磁盘使用率 93%。用一句话给出最可能的根因。"
for cfg in (dict(max_output_tokens=200), dict(max_output_tokens=2000), dict(max_output_tokens=200, thinking_config=types.ThinkingConfig(thinking_budget=0))):
    try:
        r = g.models.generate_content(model="gemini-3.7-flash", contents=q, config=types.GenerateContentConfig(temperature=1.0, **cfg))
        u = r.usage_metadata
        print({k: (str(v) if k == "thinking_config" else v) for k, v in cfg.items()},
              "| finish:", r.candidates[0].finish_reason, "| thoughts:", u.thoughts_token_count, "| output:", u.candidates_token_count,
              "| text:", (r.text or "")[:60].replace("\n", " "))
    except Exception as e:
        print(cfg, "ERROR", str(e)[:200])
