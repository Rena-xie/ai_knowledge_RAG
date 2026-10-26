"""API 成本聚合查询接口。"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from app.api.schemas import ApiCostBucketResponse, ApiCostSummaryResponse
from app.domain.cost import CostAggregate

router = APIRouter(prefix="/api/v1/costs", tags=["costs"])


def cost_bucket_response(cost: CostAggregate) -> ApiCostBucketResponse:
    return ApiCostBucketResponse(
        operation=cost.operation,
        provider=cost.provider,
        model=cost.model,
        usage_source=cost.usage_source,
        currency=cost.currency,
        price_per_million=cost.price_per_million,
        input_units=cost.input_units,
        cost=cost.cost,
        request_count=cost.request_count,
        success_count=cost.success_count,
        failure_count=cost.failure_count,
        retry_count=cost.retry_count,
        timeout_count=cost.timeout_count,
        uncosted_count=cost.uncosted_count,
        cost_status=(
            "unknown_price" if cost.cost is None else "configured_price_estimate"
        ),
    )


@router.get("", response_model=ApiCostSummaryResponse)
def list_costs(
    request: Request,
    since_hours: int = Query(default=24, ge=1, le=720),
) -> ApiCostSummaryResponse:
    """返回指定时间窗内按操作和模型聚合的 API 成本。"""

    container = request.app.state.container
    costs = container.cost_service.summary_since_hours(since_hours)
    return ApiCostSummaryResponse(
        since_hours=since_hours,
        budget_24h=container.settings.api_cost_budget_24h,
        items=[cost_bucket_response(cost) for cost in costs],
    )
