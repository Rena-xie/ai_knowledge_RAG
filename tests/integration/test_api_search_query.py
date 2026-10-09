"""检索与问答接口（见《项目介绍.md》8.2、8.3）。"""

import re
from collections.abc import Iterator, Sequence

import pytest
from fastapi.testclient import TestClient

from app.api.deps import Container
from app.core.settings import get_settings
from app.infra.fetcher import FetchResult
from app.infra.llm import LLMError, LLMResult
from app.main import create_app

ARTICLE = """
<html><head><title>BM25 指南</title></head><body><article>
<h1>BM25 指南</h1>
<p>BM25 是一种稀疏检索算法，通过对词频和文档长度归一化来计算相关性得分。</p>
<h2>适用场景</h2>
<p>当查询包含专有名词或代码函数名时，BM25 往往比向量检索更准确。</p>
<p>混合检索使用倒数排名融合算法合并两路结果，兼顾语义召回与关键词召回。</p>
</article></body></html>
"""


class FakeFetcher:
    def __init__(self, html: str) -> None:
        self._html = html

    def fetch(self, url: str) -> FetchResult:
        return FetchResult(url=url, content_type="text/html", html=self._html)

    def close(self) -> None:
        return None


class FakeEmbedder:
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0] for _ in texts]

    def close(self) -> None:
        return None


class EchoCitationLLM:
    """把提示词中的第一个引用标识回显为答案，模拟有据生成。"""

    def __init__(self) -> None:
        self.calls = 0
        self.fail = False

    def complete(self, *, system_prompt: str, user_prompt: str) -> LLMResult:
        self.calls += 1
        if self.fail:
            raise LLMError("模拟 LLM 失败")
        match = re.search(r"\[S\d+:C\d+\]", user_prompt)
        text = f"答案 {match.group(0)}" if match else "知识库缺少相关证据。"
        return LLMResult(text=text, prompt_tokens=5, completion_tokens=2)

    def close(self) -> None:
        return None


@pytest.fixture
def client() -> Iterator[TestClient]:
    settings = get_settings()
    llm = EchoCitationLLM()
    container = Container(
        settings,
        fetcher=FakeFetcher(ARTICLE),
        embedder=FakeEmbedder(),
        llm_client=llm,
    )
    app = create_app()
    app.state.container = container
    with TestClient(app) as test_client:
        yield test_client


def test_search_returns_three_way_results(client: TestClient) -> None:
    client.post("/api/v1/sources", json={"url": "https://example.com/a"})

    response = client.post("/api/v1/search", json={"question": "BM25 是什么"})

    body = response.json()
    assert response.status_code == 200
    assert body["dense_ids"]
    assert body["bm25_ids"]
    assert body["fused_ids"]
    assert body["evidences"]
    assert body["evidences"][0]["score"] > 0
    assert body["evidences"][0]["text"]
    assert body["elapsed_ms"] >= 0


def test_query_returns_validated_citations(client: TestClient) -> None:
    client.post("/api/v1/sources", json={"url": "https://example.com/a"})

    response = client.post("/api/v1/query", json={"question": "BM25 的适用场景"})

    body = response.json()
    assert response.status_code == 200
    assert body["answer_type"] == "answered"
    assert body["citations_validated"] is True
    assert len(body["citations"]) == 1
    citation = body["citations"][0]
    assert citation["source_id"] == 1
    assert citation["url"] == "https://example.com/a"
    assert citation["heading_path"]
    assert body["usage"] == {"prompt_tokens": 5, "completion_tokens": 2}


def test_query_without_evidence_skips_llm(client: TestClient) -> None:
    response = client.post("/api/v1/query", json={"question": "无关问题"})

    body = response.json()
    assert response.status_code == 200
    assert body["answer_type"] == "insufficient_evidence"
    assert body["answer"] == "知识库缺少相关证据。"
    assert body["citations"] == []
    assert client.app.state.container.llm_client.calls == 0


def test_question_is_required(client: TestClient) -> None:
    response = client.post("/api/v1/search", json={"question": ""})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_llm_failure_returns_502(client: TestClient) -> None:
    client.post("/api/v1/sources", json={"url": "https://example.com/a"})
    client.app.state.container.llm_client.fail = True

    response = client.post("/api/v1/query", json={"question": "BM25"})

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "LLM_FAILED"
