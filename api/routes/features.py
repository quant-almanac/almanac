"""Feature-control API.

GET is operational visibility.  POST is limited to features whose real
consumers use ``feature_controls`` as their authority.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

BASE_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(BASE_DIR))

from feature_controls import get_feature_status, list_feature_statuses, set_feature

router = APIRouter()


class FeatureUpdateRequest(BaseModel):
    enabled: bool
    rationale: Optional[str] = None


@router.get("/api/features")
async def get_features():
    return list_feature_statuses()


@router.get("/api/features/{key}")
async def get_feature(key: str):
    try:
        return get_feature_status(key)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown feature: {key}")


@router.post("/api/features/{key}")
async def update_feature(key: str, request: FeatureUpdateRequest):
    try:
        return set_feature(
            key,
            request.enabled,
            actor="webui",
            rationale=request.rationale,
        )
    except KeyError:
        raise HTTPException(
            status_code=409,
            detail=f"{key} はこの画面から変更できません",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
