"""启动自检行为（见《项目开发说明_v1.md》10.6）。"""

from pathlib import Path

import pytest

from app.core.selfcheck import (
    SelfCheckError,
    check_fts5_available,
    ensure_ascii_path,
    ensure_writable_dir,
    run_startup_checks,
    validate_runtime_settings,
)
from app.core.settings import Settings


def build_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "embedding_api_base": "https://example.invalid/v1",
        "embedding_api_key": "test-key",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_fts5_is_available(tmp_path: Path) -> None:
    check_fts5_available(tmp_path / "app.db")


def test_writable_dir_accepts_new_directory(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "chroma"

    ensure_writable_dir(target)

    assert target.is_dir()


def test_writable_dir_rejects_path_under_file(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")

    with pytest.raises(SelfCheckError):
        ensure_writable_dir(blocker / "chroma")


def test_ascii_path_is_accepted(tmp_path: Path) -> None:
    ensure_ascii_path(tmp_path / "chroma")


def test_non_ascii_path_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SelfCheckError):
        ensure_ascii_path(tmp_path / "知识库" / "chroma")


def test_runtime_settings_accept_defaults() -> None:
    validate_runtime_settings(build_settings())


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("embedding_api_base", ""),
        ("embedding_api_key", ""),
        ("fetch_timeout_s", 0),
        ("fetch_max_bytes", -1),
        ("max_request_bytes", 0),
        ("embedding_input_price_per_million", -1),
        ("api_cost_budget_24h", -0.01),
    ],
)
def test_runtime_settings_reject_invalid_boundaries(
    field_name: str,
    invalid_value: object,
) -> None:
    settings = build_settings(**{field_name: invalid_value})

    with pytest.raises(SelfCheckError):
        validate_runtime_settings(settings)


def test_budget_requires_configured_prices() -> None:
    settings = build_settings(
        api_cost_budget_24h=1.0,
        rerank_enabled=True,
    )

    with pytest.raises(SelfCheckError, match="Embedding 单价"):
        validate_runtime_settings(settings)


def test_startup_checks_reject_empty_api_key(tmp_path: Path) -> None:
    settings = build_settings(
        embedding_api_key="",
        sqlite_path=tmp_path / "app.db",
        chroma_path=tmp_path / "chroma",
    )

    with pytest.raises(SelfCheckError, match="embedding_api_key"):
        run_startup_checks(settings)
