import pytest

from sentinel.eval.guards import EvalGuardError, check_no_cache, require_dataset_tag


@pytest.mark.parametrize("tag", [None, "", "  ", "latest", "LATEST"])
def test_dataset_tag_required(tag: str | None) -> None:
    with pytest.raises(EvalGuardError):
        require_dataset_tag(tag)


def test_dataset_tag_ok() -> None:
    assert require_dataset_tag("ds-2026-11-20") == "ds-2026-11-20"


def test_cache_rejected() -> None:
    with pytest.raises(EvalGuardError):
        check_no_cache({"LANGSMITH_TEST_CACHE": "tests/cassettes"})
    check_no_cache({})
