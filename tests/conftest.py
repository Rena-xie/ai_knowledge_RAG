"""测试公共夹具：注入测试配置并清理配置缓存。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.settings import get_settings
from app.main import create_app


@pytest.fixture(autouse=True)
def test_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """为每个测试提供隔离的存储路径与占位密钥。"""

    monkeypatch.setenv("EMBEDDING_API_BASE", "https://example.invalid/v1")
    monkeypatch.setenv("EMBEDDING_API_KEY", "test-embedding-key")
    monkeypatch.setenv("LLM_API_BASE", "https://example.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-llm-key")
    # 本地 .env 可能开启 rerank；测试必须显式关闭，避免访问外部 API。
    monkeypatch.setenv("RERANK_ENABLED", "false")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("CHROMA_PATH", str(tmp_path / "chroma"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "logs" / "app.log"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client() -> Iterator[TestClient]:
    """带生命周期的测试客户端。"""

    with TestClient(create_app()) as test_client:
        yield test_client
