"""收集决定 Agent 行为的全部内容，算出版本哈希（03 §5.2）。

负责：VersionComponents 的每一项从哪里来；哈希的计算方式。
不负责：版本的存储和复用（freeze.py）。
"""

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import yaml

from sentinel.contracts import VersionComponents
from sentinel.core.knowledge.corpus import knowledge_hash
from sentinel.core.llm.config import load_models_config
from sentinel.core.packs_registry import LoadedPack

REPO = Path(__file__).resolve().parents[4]
PROMPTS_DIR = Path(__file__).resolve().parents[1] / "diagnosis" / "prompts"
CONTEXT_POLICY = REPO / "config" / "context_policy.yaml"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def load_context_policy() -> dict[str, Any]:
    return dict(yaml.safe_load(CONTEXT_POLICY.read_text(encoding="utf-8")))


# 影响 Agent 行为的内容（ADR-012）：代码、配置、依赖版本。文档、测试、部署脚本的改动不产生新版本
BEHAVIOR_PATHS = ("src", "config", "uv.lock", "pyproject.toml")


def _git(*args: str) -> str:
    # 只去掉末尾的换行：porcelain 输出每行开头的空格是状态码的一部分，不能 strip
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=True).stdout.rstrip("\n")


def behavior_sha() -> str:
    """HEAD 里这几个路径的 git 对象哈希（目录是 tree，文件是 blob）合在一起再算 sha256。

    只看已提交的内容：未提交的改动由 behavior_dirty() 拦住，所以这个哈希一定对应一份可以检出的代码。
    """
    objs = [f"{path}={_git('rev-parse', f'HEAD:{path}')}" for path in BEHAVIOR_PATHS]
    return "sha256:" + _sha("\n".join(objs))


def behavior_dirty() -> list[str]:
    """上述路径里未提交的改动（包括未跟踪的新文件），返回文件列表；空列表表示干净。"""
    out = _git("status", "--porcelain", "--untracked-files=normal", "--", *BEHAVIOR_PATHS)
    return [line[3:] for line in out.splitlines() if line.strip()]  # 每行是 "XY 路径"


def collect_components(packs: list[LoadedPack], code_sha: str | None = None) -> VersionComponents:
    models = {
        p: {"provider": m.provider, "model": m.model, **m.params()} for p, m in load_models_config().purposes.items()
    }
    tools = {
        # 模型看到的是描述 + 参数定义，两者和预算一起决定工具行为（03 §8"工具描述改了，AgentVersion 也要变"）
        tid: _sha(
            json.dumps(
                {
                    "description": t.impl.description,
                    "budget": t.decl.output_budget_tokens,
                    "args": t.impl.args_schema.model_json_schema() if t.impl.args_schema else None,
                },
                sort_keys=True,
                ensure_ascii=False,
            )
        )
        for p in packs
        for tid, t in p.tools.items()
    }
    return VersionComponents(
        prompts={
            **{f.name: _sha(f.read_text(encoding="utf-8")) for f in sorted(PROMPTS_DIR.glob("*.md"))},
            # runbook 打开 L4 之后会进入上下文；改了 runbook，Agent 的行为就变了（04 §3.5）
            **{f"knowledge:{p.manifest.pack}": knowledge_hash(p) for p in packs},
            # 根因表会渲染进诊断提示词，所以它的变化也要体现在版本里（ADR-011）
            **{
                f"root_causes:{p.manifest.pack}": _sha(
                    json.dumps([r.model_dump() for r in p.manifest.root_causes], ensure_ascii=False)
                )
                for p in packs
            },
        },
        models=models,
        tools=tools,
        context_policy=load_context_policy(),
        memory_policy={},  # M2 D6–D7
        packs={p.manifest.pack: p.manifest.version for p in packs},
        code_sha=code_sha if code_sha is not None else behavior_sha(),
    )


def components_hash(c: VersionComponents) -> str:
    return "sha256:" + _sha(json.dumps(c.model_dump(mode="json"), sort_keys=True, ensure_ascii=False))
