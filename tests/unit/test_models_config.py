from pathlib import Path

import pytest
from pydantic import ValidationError

from sentinel.core.llm.config import load_models_config
from sentinel.core.versioning.models import ModelAliasError, assert_pinned


def test_repo_config_loads_and_is_pinned() -> None:
    cfg = load_models_config()
    assert {"diagnosis", "evaluator", "embedding"} <= set(cfg.purposes)
    # thinking_budget 必须写进诊断模型参数（S3：思考 token 计入输出上限）
    assert "thinking_budget" in cfg.purposes["diagnosis"].params()


@pytest.mark.parametrize("name", ["x-latest", "x-flash-LATEST", "latest", ""])
def test_alias_rejected(name: str) -> None:
    with pytest.raises(ModelAliasError):
        assert_pinned(name)


def test_alias_in_config_rejected(tmp_path: Path) -> None:
    p = tmp_path / "m.yaml"
    p.write_text("schema_version: 1\npurposes:\n  diagnosis: {provider: google, model: some-flash-latest}\n")
    with pytest.raises(ValidationError, match="别名"):
        load_models_config(str(p))
