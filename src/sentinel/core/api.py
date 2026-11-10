"""core 的公开接口（01 §2"eval 只调用 core 的公开接口"）。评测子系统只能 import 这个模块（import-linter 检查）。

负责：列出 core 对外提供的东西。这里只做转出，不写逻辑。
不负责：线上入口（cli、api 直接用 core 内部模块是允许的，它们本来就是 core 的一部分的外壳）。
加一个公开接口 = 在这里加一行；评测需要 core 的新能力时，先想清楚它该不该公开。
"""

from sentinel.core.credentials import resolve as resolve_credential
from sentinel.core.detect import DetectResult, load_env, run_detect
from sentinel.core.diagnosis.evidence import ClaimCheck, check_evidence, support_rate
from sentinel.core.graph.investigation import InvState
from sentinel.core.graph.run import current_version, investigate, load_packs, make_deps, patrol
from sentinel.core.llm.config import load_models_config
from sentinel.core.observations.store import ObservationStore
from sentinel.core.packs_registry import LoadedPack, load_pack
from sentinel.core.store import MemoryStore, Store, open_store
from sentinel.core.toolbackend import Mode, ToolBackend, open_backend
from sentinel.core.versioning.freeze import FreezeError

__all__ = [
    "ClaimCheck",
    "DetectResult",
    "FreezeError",
    "InvState",
    "LoadedPack",
    "MemoryStore",
    "Mode",
    "ObservationStore",
    "Store",
    "ToolBackend",
    "check_evidence",
    "current_version",
    "investigate",
    "load_env",
    "load_models_config",
    "load_pack",
    "load_packs",
    "make_deps",
    "open_backend",
    "open_store",
    "patrol",
    "resolve_credential",
    "run_detect",
    "support_rate",
]
