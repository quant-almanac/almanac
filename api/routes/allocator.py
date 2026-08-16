"""Human review endpoint for the first capital-allocator comparisons."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()
BASE_DIR = Path(__file__).parent.parent.parent


class AllocatorComparisonReview(BaseModel):
    decision: str


@router.post("/api/allocator-comparisons/{run_id}/review")
async def review_allocator_comparison(run_id: str, request: AllocatorComparisonReview):
    """Record a user review; this cannot place orders or change cash."""
    try:
        from capital_allocator import review_comparison

        row = review_comparison(run_id, request.decision, base_dir=BASE_DIR)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="allocator comparison run not found")
    return row
