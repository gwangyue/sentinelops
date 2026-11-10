"""工具摘要的输出预算（03 §3.1：output_budget_tokens；04 §3.3）。

负责：估算 token 数，超预算时按行截断并标注。
不负责：精确计数——TODO(edge)：换成和诊断模型一致的分词器；现在的估算偏保守（宁可多算）。
"""


def estimate_tokens(text: str) -> int:
    # ASCII 大约 3–4 个字符一个 token，这里按 3 算；非 ASCII（中文等）按 1 个字符一个 token 算
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    return -(-ascii_chars // 3) + (len(text) - ascii_chars)


def fit_budget(digest: str, budget: int, note: str) -> str:
    """超预算时从尾部按行丢弃，最后加一行说明，告诉模型完整内容在哪里（分段读取，04 §3.3）。"""
    if estimate_tokens(digest) <= budget:
        return digest
    lines = digest.splitlines()
    while lines and estimate_tokens("\n".join([*lines, note])) > budget:
        lines.pop()
    return "\n".join([*lines, note])
