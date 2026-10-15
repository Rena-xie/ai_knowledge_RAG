"""端到端流水线：本地夹具站点 + 假外部客户端（见《项目介绍.md》11.1）。"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterator, Sequence
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.deps import Container
from app.core.settings import get_settings
from app.infra.db import KnowledgeSource
from app.infra.fetcher import Fetcher
from app.infra.llm import LLMResult
from app.main import create_app

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FIXTURE_PAGE = "python-basic.html"


class QuietHandler(SimpleHTTPRequestHandler):
    """静默的静态文件处理器，避免测试输出噪音。"""

    def log_message(self, format: str, *args: object) -> None:
        return None


@pytest.fixture
def fixture_site() -> Iterator[str]:
    handler = partial(QuietHandler, directory=str(FIXTURES_DIR))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class FakeEmbedder:
    def embed(
        self, texts: Sequence[str], *, budget_s: float | None = None
    ) -> list[list[float]]:
        return [[1.0, 0.0, 0.0] for _ in texts]

    def close(self) -> None:
        return None


class EchoCitationLLM:
    """把提示词中的第一个引用标识回显为答案。"""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *, system_prompt: str, user_prompt: str) -> LLMResult:
        self.calls += 1
        match = re.search(r"\[S\d+:C\d+\]", user_prompt)
        text = f"答案 {match.group(0)}" if match else "知识库缺少相关证据。"
        return LLMResult(text=text, prompt_tokens=7, completion_tokens=3)

    def close(self) -> None:
        return None


@pytest.fixture
def client() -> Iterator[TestClient]:
    settings = get_settings()
    fetcher = Fetcher(
        timeout_s=5,
        max_bytes=2_000_000,
        max_redirects=3,
        max_retries=0,
        # 端到端测试使用本地夹具站点，放行回环地址；生产默认仍拦截内网。
        validator=lambda _address: True,
    )
    container = Container(
        settings,
        fetcher=fetcher,
        embedder=FakeEmbedder(),
        llm_client=EchoCitationLLM(),
    )
    app = create_app()
    app.state.container = container
    with TestClient(app) as test_client:
        yield test_client


def test_full_pipeline_ingest_search_and_answer(
    client: TestClient, fixture_site: str
) -> None:
    page_url = f"{fixture_site}/{FIXTURE_PAGE}"

    submitted = client.post("/api/v1/sources", json={"url": page_url})

    assert submitted.status_code == 200
    body = submitted.json()
    assert body["status"] == "completed"
    # trafilatura 的标题提取优先取正文 H1
    assert body["title"] == "数据类型和变量"
    assert body["triggered"] is True

    listed = client.get("/api/v1/sources").json()
    assert listed["total"] == 1

    searched = client.post("/api/v1/search", json={"question": "字符串 转义"})
    search_body = searched.json()
    assert searched.status_code == 200
    assert search_body["bm25_ids"]
    assert search_body["fused_ids"]
    assert search_body["evidences"]

    answered = client.post("/api/v1/query", json={"question": "字符串如何表示？"})
    answer_body = answered.json()
    assert answered.status_code == 200
    assert answer_body["answer_type"] == "answered"
    assert answer_body["citations_validated"] is True
    assert answer_body["citations"]
    citation = answer_body["citations"][0]
    assert citation["url"] == page_url
    assert citation["heading_path"]
    assert citation["title"] == "数据类型和变量"


def test_pipeline_reports_fetch_error_for_missing_page(
    client: TestClient, fixture_site: str
) -> None:
    response = client.post(
        "/api/v1/sources", json={"url": f"{fixture_site}/miss.html"}
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "FETCH_HTTP_ERROR"
    source = client.get("/api/v1/sources/1").json()
    assert source["status"] == "failed"
    assert source["error_code"] == "FETCH_HTTP_ERROR"


def test_delete_removes_indexed_data(client: TestClient, fixture_site: str) -> None:
    created = client.post(
        "/api/v1/sources", json={"url": f"{fixture_site}/{FIXTURE_PAGE}"}
    ).json()
    container: Container = client.app.state.container
    assert container.vector_index.count() > 0

    response = client.delete(f"/api/v1/sources/{created['id']}")

    assert response.status_code == 204
    assert container.vector_index.count() == 0
    assert client.get(f"/api/v1/sources/{created['id']}").status_code == 404


def test_startup_recovers_interrupted_sources() -> None:
    """应用启动时回收遗留的 pending/processing 记录（见 10.6 第 4 条）。"""

    settings = get_settings()
    container = Container(
        settings,
        fetcher=Fetcher(
            timeout_s=5,
            max_bytes=2_000_000,
            max_redirects=3,
            max_retries=0,
            validator=lambda _address: True,
        ),
        embedder=FakeEmbedder(),
        llm_client=EchoCitationLLM(),
    )
    with container.session_factory() as session:
        session.add(
            KnowledgeSource(
                url="https://example.com/interrupted",
                canonical_url="https://example.com/interrupted",
                status="processing",
                created_at="2026-10-09T00:00:00Z",
                updated_at="2026-10-09T00:00:00Z",
            )
        )
        session.commit()

    app = create_app()
    app.state.container = container
    with TestClient(app):
        pass

    recovered = container.source_service.get(1)
    assert recovered.status == "failed"
    assert recovered.error_code == "INGEST_INTERRUPTED"
