# /// script
# requires-python = ">=3.12"
# dependencies = ["google-genai"]
# ///
"""S3-1：列出 Gemini 可用模型，供 ADR-008 选定固定版本号（G2：禁止别名）。
只负责列出候选；选哪一个由人看输出后写进 spike-results.md。"""
from google import genai

client = genai.Client()
for m in client.models.list():
    actions = getattr(m, "supported_actions", None) or []
    if any(k in m.name for k in ("flash", "pro", "embedding")):
        print(f"{m.name:55} {m.version or '-':12} {','.join(actions)}")
