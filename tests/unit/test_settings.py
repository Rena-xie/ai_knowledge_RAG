"""配置默认值与入库预算规则（见《项目介绍.md》10.2、10.4）。"""

from app.core.settings import Settings


def build_settings() -> Settings:
    return Settings(
        _env_file=None,
        embedding_api_base="https://example.invalid/v1",
        embedding_api_key="test-key",
        llm_api_base="https://example.invalid/v1",
        llm_api_key="test-key",
    )


def test_defaults_match_design_doc() -> None:
    settings = build_settings()

    assert settings.embedding_model == "BAAI/bge-m3"
    assert settings.dense_top_k == 20
    assert settings.bm25_top_k == 20
    assert settings.rrf_k == 60
    assert settings.final_top_k == 8
    assert settings.parent_max_chars == 3000
    assert settings.child_target_chars == 900
    assert settings.child_max_chars == 1000
    assert settings.child_overlap_chars == 100
    assert settings.context_budget_chars == 12000
    assert settings.fetch_max_bytes == 5242880
    assert settings.fetch_max_redirects == 5
    assert settings.polite_delay_s == 1.0
    assert settings.robots_cache_ttl_s == 300
    assert settings.fetch_user_agent.startswith("ai-knowledge-base/")
    assert settings.ingest_lock_timeout_s == 30
    assert settings.max_request_bytes == 1048576
    assert settings.app_host == "127.0.0.1"
    assert settings.app_port == 8000


def test_ingest_budget_covers_stage_timeouts() -> None:
    settings = build_settings()

    fetch_worst_case = settings.fetch_timeout_s * (1 + settings.fetch_max_retries)
    embed_worst_case = settings.embed_timeout_s * (1 + settings.embed_max_retries)

    assert settings.ingest_timeout_s == 60
    assert fetch_worst_case + embed_worst_case < settings.ingest_timeout_s
