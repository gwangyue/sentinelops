"""读取专家包的 runbook：文件头（YAML）声明适用的检测器、版本范围、出处；正文按二级标题切成段落。

负责：语料的解析和内容哈希。哈希进入 AgentVersion（runbook 改了，Agent 的行为就变了），也用来校验索引是否过期。
不负责：索引和检索（es_index.py）。
"""

import hashlib
import importlib.resources
from dataclasses import dataclass

import yaml

from sentinel.core.packs_registry import LoadedPack


@dataclass(frozen=True)
class Section:
    doc_id: str  # 文件名#段落序号
    title: str
    heading: str
    text: str
    detectors: tuple[str, ...]
    source: str


def _files(pack: LoadedPack) -> list[tuple[str, str]]:
    root = importlib.resources.files(f"sentinel.packs.{pack.manifest.pack}")
    out = []
    for k in pack.manifest.knowledge:
        d = root.joinpath(k.path)
        for f in sorted(d.iterdir(), key=lambda p: p.name):
            if f.name.endswith(".md"):
                out.append((f.name, f.read_text(encoding="utf-8")))
    return out


def knowledge_hash(pack: LoadedPack) -> str:
    h = hashlib.sha256()
    for name, text in _files(pack):
        h.update(name.encode() + b"\0" + text.encode() + b"\0")
    return h.hexdigest()


def sections(pack: LoadedPack) -> list[Section]:
    out = []
    for name, text in _files(pack):
        _, front, body = text.split("---", 2)
        meta = yaml.safe_load(front)
        parts = [p for p in body.split("\n## ") if p.strip()]
        for i, part in enumerate(parts):
            heading, _, content = part.lstrip("# ").partition("\n")
            out.append(
                Section(
                    f"{name}#{i}",
                    meta["title"],
                    heading.strip(),
                    content.strip(),
                    tuple(meta.get("detectors", [])),
                    meta["source"],
                )
            )
    return out
