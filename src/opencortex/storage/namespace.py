# SPDX-License-Identifier: Apache-2.0
"""URI namespace generation for opencortex."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from uuid import uuid4

from opencortex.core.identity import IdentityProfile, get_identity_profile
from opencortex.store.types import ContextType


class CortexNamespace:
    """Generate store URIs without depending on legacy OpenCortex code."""

    def __init__(self, *, collection_resolver: object) -> None:
        self.collection_resolver = collection_resolver

    async def resolve(
        self,
        *,
        context_type: ContextType,
        category: str,
        abstract: str,
    ) -> tuple[str, str]:
        """Resolve a primary URI and parent URI."""
        profile = get_identity_profile()
        bucket = "resources" if context_type == ContextType.RESOURCE else "memories"
        node = slug(abstract) or uuid4().hex
        parent = self._base(profile, bucket, category)
        return f"{parent}/{node}", parent

    def session_events_parent(
        self,
        session_id: str,
        *,
        profile: IdentityProfile,
    ) -> str:
        """Return the session event parent URI."""
        return f"{self._base(profile, 'memories', 'events')}/{slug(session_id)}"

    def session_immediate_uri(self, *, profile: IdentityProfile) -> str:
        """Return a URI for one immediate session message."""
        parent = self.session_events_parent(profile.session_id, profile=profile)
        return f"{parent}/immediate/{uuid4().hex}"

    def session_merged_uri(
        self,
        session_id: str,
        msg_range: list[int],
        *,
        profile: IdentityProfile,
    ) -> str:
        """Return a URI for one merged session segment."""
        start, end = msg_range
        parent = self.session_events_parent(session_id, profile=profile)
        return f"{parent}/merged/{start}-{end}"

    def session_final_uri(self, session_id: str, *, profile: IdentityProfile) -> str:
        """Return a URI for the final session record."""
        return f"{self.session_events_parent(session_id, profile=profile)}/final"

    def parent(self, uri: str) -> str:
        """Return the parent URI for one OpenCortex URI."""
        path = self.path(uri)
        if path is None or len(path.parts) <= 1:
            return ""
        return f"opencortex://{path.parent.as_posix()}"

    def parent_chain(self, parent_uri: str) -> list[str]:
        """Return ancestor URIs from root-most to the provided parent URI."""
        chain: list[str] = []
        current = parent_uri
        while current:
            chain.append(current)
            current = self.parent(current)
        return list(reversed(chain))

    def segments(self, uri: str) -> list[str]:
        """Return OpenCortex URI path segments."""
        path = self.path(uri)
        return list(path.parts) if path is not None else []

    @staticmethod
    def path(uri: str) -> PurePosixPath | None:
        """Return the normalized path portion of an OpenCortex URI."""
        prefix = "opencortex://"
        if not uri.startswith(prefix):
            return None
        parts = [
            part
            for part in PurePosixPath(uri[len(prefix) :].strip("/")).parts
            if part not in {"", "."}
        ]
        if not parts or any(part == ".." for part in parts):
            return None
        return PurePosixPath(*parts)

    @staticmethod
    def _base(profile: IdentityProfile, bucket: str, category: str) -> str:
        return (
            f"opencortex://{slug(profile.tenant_id) or 'default'}/"
            f"{slug(profile.user_id) or 'default'}/{bucket}/"
            f"{slug(profile.project_id) or 'public'}/{slug(category) or 'semantic'}"
        )


def slug(value: str) -> str:
    """Return a stable path segment."""
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9_-]+", "_", text)
    return text.strip("_")[:80]
