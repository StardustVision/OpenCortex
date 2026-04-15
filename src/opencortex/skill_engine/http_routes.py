"""Skill Engine HTTP routes — REST API for SkillHub frontend.

All routes derive tenant_id/user_id from JWT via get_effective_identity().
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException

from opencortex.http.request_context import get_effective_identity

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/skills", tags=["skills"])

_skill_manager = None


def set_skill_manager(manager) -> None:
    """Bind the shared skill manager used by HTTP route handlers."""
    global _skill_manager
    _skill_manager = manager


def _get_manager():
    if _skill_manager is None:
        raise HTTPException(status_code=503, detail="Skill engine not initialized")
    return _skill_manager


@router.get("")
async def list_skills(status: Optional[str] = None):
    """List skills (filterable by status)."""
    mgr = _get_manager()
    tid, uid = get_effective_identity()
    from opencortex.skill_engine.types import SkillStatus

    s = None
    if status:
        try:
            s = SkillStatus(status)
        except ValueError as err:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Invalid status: {status}. Valid: candidate, active, deprecated"
                ),
            ) from err
    results = await mgr.list_skills(tid, uid, status=s)
    return {"skills": [r.to_dict() for r in results], "count": len(results)}


@router.get("/search")
async def search_skills(q: str, top_k: int = 5):
    """Search active skills."""
    mgr = _get_manager()
    tid, uid = get_effective_identity()
    results = await mgr.search(q, tid, uid, top_k=top_k)
    return {"skills": [r.to_dict() for r in results], "count": len(results)}


@router.post("/extract")
async def extract_skills(
    context_types: Optional[str] = None, categories: Optional[str] = None
):
    """Trigger skill extraction from memories."""
    mgr = _get_manager()
    if not mgr.extraction_available:
        raise HTTPException(
            status_code=503,
            detail="Extraction pipeline not available: SourceAdapter not configured",
        )
    tid, uid = get_effective_identity()
    from opencortex.http.request_context import get_effective_project_id

    pid = get_effective_project_id()
    ct = context_types.split(",") if context_types else None
    cats = categories.split(",") if categories else None
    results = await mgr.extract(
        tid, uid, project_id=pid, context_types=ct, categories=cats
    )
    return {"extracted": [r.to_dict() for r in results], "count": len(results)}


@router.get("/{skill_id}")
async def get_skill(skill_id: str):
    """Get skill detail + lineage."""
    mgr = _get_manager()
    tid, uid = get_effective_identity()
    r = await mgr.get_skill(skill_id, tid, uid)
    if not r:
        raise HTTPException(status_code=404, detail="Skill not found")
    return r.to_dict()


@router.post("/{skill_id}/approve")
async def approve_skill(skill_id: str):
    """Approve candidate -> ACTIVE."""
    mgr = _get_manager()
    tid, uid = get_effective_identity()
    await mgr.approve(skill_id, tid, uid)
    return {"status": "active", "skill_id": skill_id}


@router.post("/{skill_id}/reject")
async def reject_skill(skill_id: str):
    """Reject candidate -> DEPRECATED."""
    mgr = _get_manager()
    tid, uid = get_effective_identity()
    await mgr.reject(skill_id, tid, uid)
    return {"status": "deprecated", "skill_id": skill_id}


@router.post("/{skill_id}/deprecate")
async def deprecate_skill(skill_id: str):
    """Deprecate active skill."""
    mgr = _get_manager()
    tid, uid = get_effective_identity()
    await mgr.deprecate(skill_id, tid, uid)
    return {"status": "deprecated", "skill_id": skill_id}


@router.post("/{skill_id}/promote")
async def promote_skill(skill_id: str):
    """Promote skill from PRIVATE -> SHARED."""
    mgr = _get_manager()
    tid, uid = get_effective_identity()
    try:
        await mgr.promote(skill_id, tid, uid)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    return {"visibility": "shared", "skill_id": skill_id}


@router.post("/{skill_id}/fix")
async def fix_skill(skill_id: str, direction: str = ""):
    """Trigger FIX evolution → new CANDIDATE."""
    mgr = _get_manager()
    tid, uid = get_effective_identity()
    r = await mgr.fix_skill(skill_id, tid, uid, direction)
    if not r:
        raise HTTPException(status_code=400, detail="Fix evolution failed")
    return r.to_dict()


@router.post("/{skill_id}/derive")
async def derive_skill(skill_id: str, direction: str = ""):
    """Trigger DERIVED evolution → new CANDIDATE."""
    mgr = _get_manager()
    tid, uid = get_effective_identity()
    r = await mgr.derive_skill(skill_id, tid, uid, direction)
    if not r:
        raise HTTPException(status_code=400, detail="Derive evolution failed")
    return r.to_dict()
