"""LLM 客户端与提示词加载（见《项目介绍.md》7.5、10.4）。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.core.errors import ErrorCode

DEFAULT_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class LLMError(Exception):
    """生成调用失败。"""

    def __init__(self, message: str, *, code: ErrorCode = ErrorCode.LLM_FAILED) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class LLMResult:
    """生成结果与 token 用量。"""

    text: str
    prompt_tokens: int
    completion_tokens: int


def load_prompt(name: str, *, prompts_dir: Path | None = None) -> str:
    """加载提示词模板，自动跳过文件头部元信息。"""

    directory = prompts_dir or DEFAULT_PROMPTS_DIR
    path = directory / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(f"提示词模板不存在: {path}")
    lines = path.read_text(encoding="utf-8").splitlines()
    if "---" in lines:
        lines = lines[lines.index("---") + 1 :]
    return "\n".join(lines).strip()


class LLMClient:
    """调用 OpenAI 兼容的 chat completions 接口。"""

    def __init__(
        self,
        *,
        api_base: str,
        api_key: str,
        model: str,
        timeout_s: float,
        max_retries: int,
        backoff_s: float = 1.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._api_base = api_base.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._max_retries = max_retries
        self._backoff_s = backoff_s
        self._client = httpx.Client(timeout=timeout_s, transport=transport)

    def complete(self, *, system_prompt: str, user_prompt: str) -> LLMResult:
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        last_error: LLMError | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.post(
                    f"{self._api_base}/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
            except httpx.TimeoutException as error:
                last_error = LLMError(f"LLM 调用超时: {error}")
            except httpx.HTTPError as error:
                last_error = LLMError(f"LLM 调用失败: {error}")
            else:
                if response.status_code == 200:
                    return self._parse(response)
                last_error = LLMError(f"LLM 返回 HTTP {response.status_code}")
                if response.status_code not in RETRYABLE_STATUSES:
                    raise last_error
            if attempt < self._max_retries and self._backoff_s:
                time.sleep(self._backoff_s * (2**attempt))
        raise last_error or LLMError("LLM 调用失败")

    @staticmethod
    def _parse(response: httpx.Response) -> LLMResult:
        try:
            body = response.json()
            text = body["choices"][0]["message"]["content"] or ""
            usage = body.get("usage") or {}
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise LLMError(f"LLM 响应格式不正确: {error}") from error
        return LLMResult(
            text=text,
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
        )
