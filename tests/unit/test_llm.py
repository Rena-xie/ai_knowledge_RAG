"""LLM 客户端与提示词加载（见《项目开发说明_v1.md》7.5、10.4）。"""

import json
from pathlib import Path

import httpx
import pytest

from app.core.errors import ErrorCode
from app.infra.llm import LLMClient, LLMError, load_prompt, split_prompt

API_BASE = "https://api.example.com/v1"


def make_client(handler, **overrides) -> LLMClient:
    options = {
        "api_base": API_BASE,
        "api_key": "test-key",
        "model": "deepseek-flash",
        "timeout_s": 1,
        "max_retries": 0,
        "backoff_s": 0.0,
    }
    options.update(overrides)
    return LLMClient(transport=httpx.MockTransport(handler), **options)


def completion_response(content: str = "答案 [S1:C2]") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        },
    )


def test_returns_content_and_usage() -> None:
    result = make_client(lambda request: completion_response()).complete(
        system_prompt="系统", user_prompt="问题"
    )

    assert result.text == "答案 [S1:C2]"
    assert result.prompt_tokens == 12
    assert result.completion_tokens == 3


def test_sends_model_and_messages() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return completion_response()

    make_client(handler).complete(system_prompt="系统提示", user_prompt="用户问题")

    assert captured["url"] == f"{API_BASE}/chat/completions"
    body = captured["body"]
    assert body["model"] == "deepseek-flash"
    assert body["messages"] == [
        {"role": "system", "content": "系统提示"},
        {"role": "user", "content": "用户问题"},
    ]


def test_retries_on_server_error() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503)
        return completion_response("重试成功")

    result = make_client(handler, max_retries=1).complete(
        system_prompt="系统", user_prompt="问题"
    )

    assert result.text == "重试成功"
    assert len(calls) == 2


def test_raises_llm_failed_after_retries() -> None:
    with pytest.raises(LLMError) as excinfo:
        make_client(lambda request: httpx.Response(500)).complete(
            system_prompt="系统", user_prompt="问题"
        )

    assert excinfo.value.code == ErrorCode.LLM_FAILED


def test_loads_prompt_template() -> None:
    prompt = load_prompt("system_prompt_v1")

    assert "{evidence}" in prompt
    assert "{question}" in prompt
    assert "版本：v1" not in prompt


def test_missing_prompt_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_prompt("not_exists", prompts_dir=tmp_path)


def test_split_prompt_separates_system_and_user() -> None:
    system, user = split_prompt(load_prompt("system_prompt_v1"))

    assert "不可信数据" in system
    assert "{evidence}" not in system
    assert "{evidence}" in user
    assert "{question}" in user


def test_split_prompt_requires_markers() -> None:
    with pytest.raises(ValueError):
        split_prompt("没有分段标记的模板")
