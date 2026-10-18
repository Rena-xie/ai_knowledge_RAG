"""依赖容器：按配置构建基础设施与服务（单进程运行，见 10.6）。"""

from __future__ import annotations

from app.core.settings import Settings
from app.infra.db import create_db_engine, create_session_factory, init_db
from app.infra.embedding import EmbeddingClient
from app.infra.fetcher import Fetcher
from app.infra.fts import FtsIndex
from app.infra.llm import LLMClient
from app.infra.vector import VectorIndex
from app.services.ingestion import IngestionService
from app.services.ingestion_jobs import IngestionWorker, JobService
from app.services.qa import QAService
from app.services.retrieval import RetrievalService
from app.services.source import SourceService


class Container:
    """进程内共享的依赖容器；测试可注入假的外部客户端。"""

    def __init__(
        self,
        settings: Settings,
        *,
        fetcher: Fetcher | None = None,
        embedder: EmbeddingClient | None = None,
        llm_client: LLMClient | None = None,
    ) -> None:
        self.settings = settings
        self.engine = create_db_engine(settings.sqlite_path)
        init_db(self.engine)
        self.session_factory = create_session_factory(self.engine)
        self.fts_index = FtsIndex(self.engine)
        self.fts_index.ensure_schema()
        self.vector_index = VectorIndex(settings.chroma_path)
        self.fetcher = (
            fetcher
            if fetcher is not None
            else Fetcher(
                timeout_s=settings.fetch_timeout_s,
                max_bytes=settings.fetch_max_bytes,
                max_redirects=settings.fetch_max_redirects,
                max_retries=settings.fetch_max_retries,
                user_agent=settings.fetch_user_agent,
                polite_delay_s=settings.polite_delay_s,
                robots_cache_ttl_s=settings.robots_cache_ttl_s,
            )
        )
        self.embedder = (
            embedder
            if embedder is not None
            else EmbeddingClient(
                api_base=settings.embedding_api_base,
                api_key=settings.embedding_api_key,
                model=settings.embedding_model,
                timeout_s=settings.embed_timeout_s,
                max_retries=settings.embed_max_retries,
            )
        )
        self.llm_client = (
            llm_client
            if llm_client is not None
            else LLMClient(
                api_base=settings.llm_api_base,
                api_key=settings.llm_api_key,
                model=settings.llm_model,
                timeout_s=settings.llm_timeout_s,
                max_retries=settings.llm_max_retries,
            )
        )
        self.source_service = SourceService(
            session_factory=self.session_factory,
            vector_index=self.vector_index,
            fts_index=self.fts_index,
        )
        self.ingestion_service = IngestionService(
            session_factory=self.session_factory,
            source_service=self.source_service,
            fetcher=self.fetcher,
            embedder=self.embedder,
            fts_index=self.fts_index,
            vector_index=self.vector_index,
            settings=settings,
        )
        self.job_service = JobService(
            session_factory=self.session_factory,
            ingestion_service=self.ingestion_service,
            settings=settings,
        )
        self.ingestion_worker = IngestionWorker(
            self.job_service, queue_max=settings.ingestion_queue_max
        )
        self.retrieval_service = RetrievalService(
            session_factory=self.session_factory,
            embedder=self.embedder,
            fts_index=self.fts_index,
            vector_index=self.vector_index,
            settings=settings,
        )
        self.qa_service = QAService(
            retrieval_service=self.retrieval_service, llm_client=self.llm_client
        )

    def start_background_tasks(self) -> None:
        """启动入库 Worker，并恢复上次未完成的 queued job。"""

        self.ingestion_worker.start()
        self.job_service.recover(enqueue=self.ingestion_worker.submit)

    def close(self) -> None:
        self.ingestion_worker.stop(timeout_s=self.settings.ingest_timeout_s + 5)
        for client in (self.fetcher, self.embedder, self.llm_client):
            close = getattr(client, "close", None)
            if callable(close):
                close()
        self.vector_index.close()
        self.engine.dispose()
