from fastapi import APIRouter

from kf_oj import __version__
from kf_oj.contracts.health import HealthResponse

router = APIRouter()


@router.get("/health/live", response_model=HealthResponse, tags=["health"], operation_id="liveness")
async def liveness() -> HealthResponse:
    """Check process liveness only; does not attest DB, queue or judge readiness."""
    return HealthResponse(status="ok", service="platform", version=__version__)
