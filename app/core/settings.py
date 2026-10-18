"""应用配置：环境变量与默认值的唯一来源。

默认值与《项目开发说明_v1.md》10.2 配置表保持一致；修改默认值需同步回写该文档。
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_FETCH_USER_AGENT = (
    "ai-knowledge-base/v1 (+https://github.com/Rena-xie/ai_knowledge_RAG)"
)
DEFAULT_ROBOTS_CACHE_TTL_S = 300.0
DEFAULT_LOG_FILE = "data/logs/app.log"


class Settings(BaseSettings):
    """从 `.env` 与环境变量加载配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 存储
    sqlite_path: Path = Path("data/app.db")
    chroma_path: Path = Path("data/chroma")

    # Embedding（必填）
    embedding_api_base: str
    embedding_api_key: str
    embedding_model: str = "BAAI/bge-m3"

    # LLM（必填）
    llm_api_base: str
    llm_api_key: str
    llm_model: str = "deepseek-flash"

    # 检索
    dense_top_k: int = 20
    bm25_top_k: int = 20
    rrf_k: int = 60
    final_top_k: int = 8
    route_head_reserve: int = 1
    min_dense_similarity: float = 0.58

    # 切块与上下文
    parent_max_chars: int = 3000
    child_target_chars: int = 900
    child_max_chars: int = 1000
    child_overlap_chars: int = 100
    context_budget_chars: int = 12000

    # 抓取
    fetch_timeout_s: int = 10
    fetch_max_retries: int = 1
    fetch_max_bytes: int = 5242880
    fetch_max_redirects: int = 5
    polite_delay_s: float = 1.0
    fetch_user_agent: str = DEFAULT_FETCH_USER_AGENT
    robots_cache_ttl_s: float = DEFAULT_ROBOTS_CACHE_TTL_S

    # 外部调用
    embed_timeout_s: int = 12
    embed_max_retries: int = 1
    llm_timeout_s: int = 30
    llm_max_retries: int = 1

    # 入库
    ingest_timeout_s: int = 60
    ingest_lock_timeout_s: int = 30
    ingestion_max_urls: int = 20
    ingestion_queue_max: int = 100
    ingestion_job_timeout_s: int = 1800

    # 服务
    max_request_bytes: int = 1048576
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    log_file: str = DEFAULT_LOG_FILE
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    """返回进程内共享的配置实例。"""

    # 必填项由环境变量 / .env 提供，mypy 无法从构造签名推知。
    return Settings()  # type: ignore[call-arg]
