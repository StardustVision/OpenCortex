"""InsightsCache — CortexFS-backed cache for SessionMeta and SessionFacet."""

import json
import logging
from dataclasses import asdict
from typing import Any, Dict, List, Optional

from opencortex.insights.types import SessionMeta, SessionFacet

logger = logging.getLogger(__name__)

REQUIRED_FACET_FIELDS = {
    "session_id", "underlying_goal", "goal_categories",
    "outcome", "brief_summary",
}


def _validate_facet(data: dict) -> bool:
    """Check that a facet dict has all required fields."""
    return REQUIRED_FACET_FIELDS.issubset(data.keys())


class InsightsCache:
    """CortexFS-backed cache for insights data."""

    def __init__(self, cortex_fs: Any):
        self._fs = cortex_fs

    def _meta_uri(self, tid: str, uid: str, session_id: str) -> str:
        return f"opencortex://{tid}/{uid}/insights/cache/meta/{session_id}.json"

    def _facet_uri(self, tid: str, uid: str, session_id: str) -> str:
        return f"opencortex://{tid}/{uid}/insights/cache/facets/{session_id}.json"

    async def get_meta(self, tid: str, uid: str, session_id: str) -> Optional[SessionMeta]:
        uri = self._meta_uri(tid, uid, session_id)
        try:
            content = await self._fs.read(uri)
            if not content:
                return None
            data = json.loads(content)
            return SessionMeta(**data)
        except Exception as e:
            logger.debug(f"Cache miss for meta {session_id}: {e}")
            return None

    async def put_meta(self, tid: str, uid: str, session_id: str, meta: SessionMeta) -> None:
        uri = self._meta_uri(tid, uid, session_id)
        await self._fs.write(uri, json.dumps(asdict(meta)))

    async def get_facet(self, tid: str, uid: str, session_id: str) -> Optional[SessionFacet]:
        uri = self._facet_uri(tid, uid, session_id)
        try:
            content = await self._fs.read(uri)
            if not content:
                return None
            data = json.loads(content)
            if not _validate_facet(data):
                logger.warning(f"Corrupted facet cache for {session_id}, deleting")
                await self._fs.delete(uri)
                return None
            return SessionFacet(**{
                k: v for k, v in data.items()
                if k in SessionFacet.__dataclass_fields__
            })
        except Exception as e:
            logger.debug(f"Cache miss for facet {session_id}: {e}")
            return None

    async def put_facet(self, tid: str, uid: str, session_id: str, facet: SessionFacet) -> None:
        uri = self._facet_uri(tid, uid, session_id)
        await self._fs.write(uri, json.dumps(asdict(facet)))
