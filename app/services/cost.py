"""API 用量持久化、聚合与预算门禁。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.clock import utc_now_iso
from app.core.context import current_context
from app.core.settings import Settings
from app.domain.cost import (
    ApiUsageAttempt,
    CostAggregate,
    CostBudgetExceeded,
)
from app.infra.db import ApiCostEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CostScope:
    """成本查询的可选关联范围。"""

    since_hours: int | None = None
    job_id: str | None = None
    request_id: str | None = None


class CostService:
    """在现有 SQLite 中记录和聚合 API 调用成本。"""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        settings: Settings,
        clock: Callable[[], str] = utc_now_iso,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._clock = clock
        self.provider = settings.api_provider

    def check_budget(self, operation: str, input_units: int) -> None:
        budget = self._settings.api_cost_budget_24h
        price = self._price_for(operation)
        if budget is None or price is None:
            return
        cutoff = self._cutoff_iso(hours=24)
        try:
            with self._session_factory() as session:
                spent = float(
                    session.scalar(
                        select(func.coalesce(func.sum(ApiCostEvent.cost), 0.0))
                        .where(ApiCostEvent.created_at >= cutoff)
                    )
                    or 0.0
                )
        except SQLAlchemyError as error:
            raise CostBudgetExceeded("成本预算状态不可用") from error
        projected = spent + input_units * price / 1_000_000
        if projected > budget:
            raise CostBudgetExceeded("API 成本预算已用尽")

    def record_attempt(self, attempt: ApiUsageAttempt) -> None:
        request_id, job_id, source_id = current_context()
        price = self._price_for(attempt.operation)
        cost = None if price is None else attempt.input_units * price / 1_000_000
        try:
            with self._session_factory() as session:
                session.add(
                    ApiCostEvent(
                        created_at=self._clock(),
                        operation=attempt.operation,
                        provider=self.provider,
                        model=attempt.model,
                        input_units=attempt.input_units,
                        usage_source=attempt.usage_source,
                        price_per_million=price,
                        currency=self._settings.api_cost_currency,
                        cost=cost,
                        success=attempt.success,
                        request_count=1,
                        attempt=attempt.attempt,
                        timed_out=attempt.timed_out,
                        error_code=attempt.error_code,
                        request_id=request_id,
                        job_id=job_id,
                        source_id=source_id,
                    )
                )
                session.commit()
        except SQLAlchemyError:
            logger.warning(
                "API 成本记录写入失败",
                extra={"stage": "cost", "error_code": "DB_ERROR"},
            )
            return
        logger.info(
            "api_usage",
            extra={
                "request_id": request_id,
                "job_id": job_id,
                "source_id": source_id,
                "operation": attempt.operation,
                "provider": self.provider,
                "model": attempt.model,
                "input_units": attempt.input_units,
                "usage_source": attempt.usage_source,
                "currency": self._settings.api_cost_currency,
                "cost": cost,
                "success": attempt.success,
                "attempt": attempt.attempt,
                "timed_out": attempt.timed_out,
                "error_code": attempt.error_code,
            },
        )

    def summary(self, scope: CostScope) -> tuple[CostAggregate, ...]:
        statement = select(
            ApiCostEvent.operation,
            ApiCostEvent.provider,
            ApiCostEvent.model,
            ApiCostEvent.usage_source,
            ApiCostEvent.currency,
            ApiCostEvent.price_per_million,
            func.sum(ApiCostEvent.input_units),
            func.sum(ApiCostEvent.cost),
            func.sum(ApiCostEvent.request_count),
            func.sum(case((ApiCostEvent.success.is_(True), 1), else_=0)),
            func.sum(case((ApiCostEvent.success.is_(False), 1), else_=0)),
            func.sum(case((ApiCostEvent.attempt > 1, 1), else_=0)),
            func.sum(case((ApiCostEvent.timed_out.is_(True), 1), else_=0)),
            func.sum(case((ApiCostEvent.cost.is_(None), 1), else_=0)),
        )
        if scope.since_hours is not None:
            statement = statement.where(
                ApiCostEvent.created_at >= self._cutoff_iso(scope.since_hours)
            )
        if scope.job_id is not None:
            statement = statement.where(ApiCostEvent.job_id == scope.job_id)
        if scope.request_id is not None:
            statement = statement.where(
                ApiCostEvent.request_id == scope.request_id
            )
        statement = statement.group_by(
            ApiCostEvent.operation,
            ApiCostEvent.provider,
            ApiCostEvent.model,
            ApiCostEvent.usage_source,
            ApiCostEvent.currency,
            ApiCostEvent.price_per_million,
        ).order_by(ApiCostEvent.operation, ApiCostEvent.model)
        with self._session_factory() as session:
            rows = session.execute(statement).all()
        return tuple(
            CostAggregate(
                operation=str(row[0]),
                provider=str(row[1]),
                model=str(row[2]),
                usage_source=str(row[3]),
                currency=str(row[4]),
                price_per_million=row[5],
                input_units=int(row[6] or 0),
                cost=None if row[7] is None else round(float(row[7]), 8),
                request_count=int(row[8] or 0),
                success_count=int(row[9] or 0),
                failure_count=int(row[10] or 0),
                retry_count=int(row[11] or 0),
                timeout_count=int(row[12] or 0),
                uncosted_count=int(row[13] or 0),
            )
            for row in rows
        )

    def summary_for_job(self, job_id: str) -> tuple[CostAggregate, ...]:
        return self.summary(CostScope(job_id=job_id))

    def summary_for_request(self, request_id: str) -> tuple[CostAggregate, ...]:
        return self.summary(CostScope(request_id=request_id))

    def summary_since_hours(self, since_hours: int) -> tuple[CostAggregate, ...]:
        return self.summary(CostScope(since_hours=since_hours))

    def _price_for(self, operation: str) -> float | None:
        if operation == "embedding":
            return self._settings.embedding_input_price_per_million
        if operation == "rerank":
            return self._settings.rerank_input_price_per_million
        return None

    @staticmethod
    def _cutoff_iso(hours: int) -> str:
        cutoff = datetime.now(UTC) - timedelta(hours=hours)
        return cutoff.isoformat().replace("+00:00", "Z")
