"""文本向量（ADR-008；05 §3.3）。和 chat.py 一样，是代码库里少数几个允许创建真实模型客户端的地方。

负责：按 config/models.yaml 的 embedding 用途生成向量（型号、维度都来自配置）；测试环境下拒绝调用。
不负责：缓存（知识库很小，索引时一次算完；查询时每次一条）。
"""

from collections.abc import Sequence

from google import genai
from google.genai import types

from sentinel.core.llm.config import guard_real_call, load_models_config


def embed(texts: Sequence[str]) -> list[list[float]]:
    guard_real_call("embedding")
    spec = load_models_config().purposes["embedding"]
    dims = spec.params().get("output_dimensionality")
    # 客户端要先存进变量：写成 genai.Client().models... 时，临时对象可能在请求途中被回收，
    # 它关闭底层 HTTP 连接后请求报 "client has been closed"（M2 D6 实测）
    client = genai.Client()
    r = client.models.embed_content(
        model=spec.model, contents=list(texts), config=types.EmbedContentConfig(output_dimensionality=dims)
    )
    return [list(e.values or []) for e in r.embeddings or []]
