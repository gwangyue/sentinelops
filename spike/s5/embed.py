# /// script
# requires-python = ">=3.12"
# dependencies = ["google-genai"]
# ///
"""S5-2：Gemini embedding 的默认维度、可选维度和延迟（05 §3.3 向量召回）。"""
import time
from google import genai
from google.genai import types

g = genai.Client()
text = "未分配分片 磁盘使用率高 集群黄色"
for model in ("gemini-embedding-001", "gemini-embedding-2"):
    for dim in (None, 768):
        t = time.perf_counter()
        cfg = types.EmbedContentConfig(output_dimensionality=dim) if dim else None
        r = g.models.embed_content(model=model, contents=text, config=cfg)
        print(f"{model:22} dim_req={str(dim):5} dim={len(r.embeddings[0].values):5} {1000*(time.perf_counter()-t):.0f}ms")
