# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# Ported from OpenViking (https://github.com/volcengine/openviking)
# SPDX-License-Identifier: Apache-2.0
"""
URI utilities for OpenCortex.

All context objects in OpenCortex are identified by URIs in the format:
opencortex://tenant/{team_id}/{sub_scope}/...

The tenant-based URI structure supports multi-team, multi-user isolation:

  Shared (team-level):
    opencortex://tenant/{team_id}/resources/...
    opencortex://tenant/{team_id}/agent/skills/...
    opencortex://tenant/{team_id}/agent/memories/patterns/...

  Private (user-level):
    opencortex://tenant/{team_id}/user/{user_id}/memories/...
    opencortex://tenant/{team_id}/user/{user_id}/reinforcement/...
    opencortex://tenant/{team_id}/user/{user_id}/feedback/...
    opencortex://tenant/{team_id}/user/{user_id}/workspace/...
    opencortex://tenant/{team_id}/user/{user_id}/session/...
    opencortex://tenant/{team_id}/user/{user_id}/agent/memories/cases/...
"""

import re
from typing import Dict, List, Optional


class CortexURI:
    """
    OpenCortex URI handler with tenant-based isolation.

    URI Format: opencortex://tenant/{team_id}/{sub_scope}/...

    Top-level scope is always "tenant". Sub-scopes define data categories:

    Shared sub-scopes (team-level, no user_id):
    - resources: Team resources (opencortex://tenant/{tid}/resources/...)
    - agent: Agent skills & shared patterns (opencortex://tenant/{tid}/agent/...)
    - queue: Internal queue (opencortex://tenant/{tid}/queue/...)
    - temp: Temporary data (opencortex://tenant/{tid}/temp/...)

    Private sub-scopes (require user_id via /user/{uid}/ prefix):
    - memories: User memories (opencortex://tenant/{tid}/user/{uid}/memories/...)
    - reinforcement: HRCM data (opencortex://tenant/{tid}/user/{uid}/reinforcement/...)
    - feedback: Feedback data (opencortex://tenant/{tid}/user/{uid}/feedback/...)
    - workspace: Workspace (opencortex://tenant/{tid}/user/{uid}/workspace/...)
    - session: Session data (opencortex://tenant/{tid}/user/{uid}/session/...)
    - agent/memories/cases: Private cases (opencortex://tenant/{tid}/user/{uid}/agent/memories/cases/...)
    """

    SCHEME = "opencortex"

    # The only valid top-level scope
    TOP_SCOPE = "tenant"

    # Sub-scopes that exist directly under tenant/{team_id}/
    SHARED_SUB_SCOPES = {"resources", "agent", "queue", "temp"}

    # Sub-scopes that exist under tenant/{team_id}/user/{user_id}/
    PRIVATE_SUB_SCOPES = {"memories", "reinforcement", "feedback", "workspace", "session"}

    # All recognized sub-scopes (for validation of the path component after tenant_id or user_id)
    ALL_SUB_SCOPES = SHARED_SUB_SCOPES | PRIVATE_SUB_SCOPES | {"user"}

    def __init__(self, uri: str):
        """
        Initialize URI handler.

        Args:
            uri: URI string (e.g., "opencortex://tenant/default/resources/...")
        """
        self.uri = uri
        self._parsed = self._parse()

    def _parse(self) -> Dict[str, str]:
        """
        Parse OpenCortex URI into components.

        Returns:
            Dictionary with URI components:
            - scheme: "opencortex"
            - tenant_id: team identifier
            - sub_scope: first path component after tenant_id (or after user_id)
            - user_id: user identifier if present (for /user/{uid}/ paths)
            - full_path: everything after scheme://
        """
        prefix = f"{self.SCHEME}://"
        if not self.uri.startswith(prefix):
            raise ValueError(f"URI must start with '{prefix}'")

        path = self.uri[len(prefix):]
        parts = [p for p in path.split("/") if p]

        if len(parts) < 1:
            raise ValueError(f"Invalid URI format: {self.uri}")

        # First part must be "tenant"
        if parts[0] != self.TOP_SCOPE:
            raise ValueError(
                f"URI must start with '{prefix}{self.TOP_SCOPE}/'. Got: {self.uri}"
            )

        tenant_id = parts[1] if len(parts) > 1 else ""
        if not tenant_id:
            raise ValueError(f"Missing tenant_id in URI: {self.uri}")

        # Determine sub_scope and user_id
        sub_scope = ""
        user_id = ""
        if len(parts) > 2:
            if parts[2] == "user":
                # Private path: tenant/{tid}/user/{uid}/...
                user_id = parts[3] if len(parts) > 3 else ""
                sub_scope = parts[4] if len(parts) > 4 else "user"
            else:
                # Shared path: tenant/{tid}/{sub_scope}/...
                sub_scope = parts[2]

        return {
            "scheme": self.SCHEME,
            "tenant_id": tenant_id,
            "sub_scope": sub_scope,
            "user_id": user_id,
            "full_path": path,
        }

    # ========== Properties ==========

    @property
    def tenant_id(self) -> str:
        """Get tenant (team) identifier."""
        return self._parsed["tenant_id"]

    @property
    def user_id(self) -> str:
        """Get user identifier (empty string if shared/team-level URI)."""
        return self._parsed["user_id"]

    @property
    def sub_scope(self) -> str:
        """Get sub-scope (e.g., 'resources', 'memories', 'agent')."""
        return self._parsed["sub_scope"]

    @property
    def scope(self) -> str:
        """Get the sub-scope. Alias for sub_scope for backward compatibility."""
        return self._parsed["sub_scope"]

    @property
    def full_path(self) -> str:
        """Get full path (everything after scheme://)."""
        return self._parsed["full_path"]

    @property
    def is_private(self) -> bool:
        """Check if this URI is user-private (contains /user/{uid}/)."""
        return bool(self._parsed["user_id"])

    @property
    def is_shared(self) -> bool:
        """Check if this URI is team-shared (no /user/{uid}/)."""
        return not self._parsed["user_id"]

    @property
    def resource_name(self) -> Optional[str]:
        """
        Get resource name for resources sub-scope.

        Returns:
            Resource name (e.g., 'my_project' from .../resources/my_project/...)
            or None for non-resources URIs.
        """
        if self.sub_scope != "resources":
            return None
        parts = self.full_path.split("/")
        # tenant/{tid}/resources/{name}/...
        idx = parts.index("resources") if "resources" in parts else -1
        if idx >= 0 and idx + 1 < len(parts):
            return parts[idx + 1]
        return None

    # ========== Navigation ==========

    def matches_prefix(self, prefix: str) -> bool:
        """Check if this URI matches a prefix."""
        return self.uri.startswith(prefix)

    @property
    def parent(self) -> Optional["CortexURI"]:
        """
        Get parent URI (one level up).

        Returns:
            Parent URI or None if at tenant root.
        """
        uri = self.uri.rstrip("/")
        scheme_sep = "://"
        scheme_end = uri.find(scheme_sep)
        if scheme_end == -1:
            return None

        after_scheme = uri[scheme_end + len(scheme_sep):]
        # Don't go above tenant/{tid}
        parts = after_scheme.split("/")
        if len(parts) <= 2:  # "tenant" and "{tid}" are minimum
            return None

        last_slash = uri.rfind("/")
        if last_slash > scheme_end + len(scheme_sep):
            return CortexURI(uri[:last_slash])
        return None

    def join(self, part: str) -> "CortexURI":
        """Join URI parts, handling slashes correctly."""
        if not part:
            return self
        result = self.uri.rstrip("/")
        part = part.strip("/")
        if part:
            result = f"{result}/{part}"
        return CortexURI(result)

    # ========== Builders ==========

    @staticmethod
    def build_shared(tenant_id: str, sub_scope: str, *path_parts: str) -> str:
        """
        Build a shared (team-level) URI.

        Args:
            tenant_id: Team identifier
            sub_scope: Sub-scope (resources, agent, queue, temp)
            *path_parts: Additional path components

        Returns:
            URI string

        Example:
            CortexURI.build_shared("myteam", "resources", "project1", "docs")
            -> "opencortex://tenant/myteam/resources/project1/docs"
        """
        parts = [CortexURI.TOP_SCOPE, tenant_id, sub_scope] + list(path_parts)
        parts = [p for p in parts if p]
        return f"{CortexURI.SCHEME}://{'/'.join(parts)}"

    @staticmethod
    def build_private(tenant_id: str, user_id: str, sub_scope: str, *path_parts: str) -> str:
        """
        Build a private (user-level) URI.

        Args:
            tenant_id: Team identifier
            user_id: User identifier
            sub_scope: Sub-scope (memories, reinforcement, feedback, workspace, session, agent)
            *path_parts: Additional path components

        Returns:
            URI string

        Example:
            CortexURI.build_private("myteam", "alice", "memories", "preferences")
            -> "opencortex://tenant/myteam/user/alice/memories/preferences"
        """
        parts = [CortexURI.TOP_SCOPE, tenant_id, "user", user_id, sub_scope] + list(path_parts)
        parts = [p for p in parts if p]
        return f"{CortexURI.SCHEME}://{'/'.join(parts)}"

    @staticmethod
    def build(tenant_id: str = "default", sub_scope: str = "", *path_parts: str) -> str:
        """
        Build an OpenCortex URI (shared). Convenience alias for build_shared.

        Args:
            tenant_id: Team identifier (default: "default")
            sub_scope: Sub-scope
            *path_parts: Additional path components

        Returns:
            URI string
        """
        return CortexURI.build_shared(tenant_id, sub_scope, *path_parts)

    @staticmethod
    def build_semantic_uri(
        parent_uri: str,
        semantic_name: str,
        node_id: Optional[str] = None,
        is_leaf: bool = False,
    ) -> str:
        """Build a semantic URI based on parent URI."""
        safe_name = CortexURI.sanitize_segment(semantic_name)

        if not is_leaf:
            return f"{parent_uri}/{safe_name}"
        else:
            if not node_id:
                raise ValueError("Leaf node must have a node_id")
            return f"{parent_uri}/{safe_name}/{node_id}"

    # ========== Validation ==========

    @staticmethod
    def is_valid(uri: str) -> bool:
        """Check if a URI string is valid."""
        try:
            CortexURI(uri)
            return True
        except ValueError:
            return False

    @staticmethod
    def normalize(uri: str) -> str:
        """
        Normalize URI by ensuring it has the opencortex:// scheme.

        Examples:
            "tenant/default/resources" -> "opencortex://tenant/default/resources"
            "opencortex://tenant/default/resources" -> "opencortex://tenant/default/resources"
        """
        prefix = f"{CortexURI.SCHEME}://"
        if uri.startswith(prefix):
            return uri
        uri = uri.lstrip("/")
        return f"{prefix}{uri}"

    # ========== URI Utilities ==========

    @staticmethod
    def sanitize_segment(text: str) -> str:
        """
        Sanitize text for use in URI segment.

        Preserves CJK characters and other common scripts
        while replacing special characters.
        """
        safe = re.sub(
            r"[^\w\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af\u3400-\u4dbf\U00020000-\U0002a6df\-]",
            "_",
            text,
        )
        safe = re.sub(r"_+", "_", safe)
        safe = safe.strip("_")[:50]
        return safe or "unnamed"

    @classmethod
    def create_temp_uri(cls, tenant_id: str = "default") -> str:
        """Create temp directory URI."""
        import datetime
        import uuid

        temp_id = uuid.uuid4().hex[:6]
        ts = datetime.datetime.now().strftime("%m%d%H%M")
        return cls.build_shared(tenant_id, "temp", f"{ts}_{temp_id}")

    # ========== Extraction helpers ==========

    def extract_after(self, segment: str) -> Optional[str]:
        """
        Extract the path component immediately after a given segment.

        Example:
            uri = CortexURI("opencortex://tenant/t1/user/alice/memories/prefs")
            uri.extract_after("user")  -> "alice"
            uri.extract_after("memories")  -> "prefs"
        """
        parts = self.full_path.split("/")
        try:
            idx = parts.index(segment)
            if idx + 1 < len(parts):
                return parts[idx + 1]
        except ValueError:
            pass
        return None

    # ========== Dunder methods ==========

    def __str__(self) -> str:
        return self.uri

    def __repr__(self) -> str:
        return f"CortexURI('{self.uri}')"

    def __eq__(self, other) -> bool:
        if isinstance(other, CortexURI):
            return self.uri == other.uri
        return self.uri == str(other)

    def __hash__(self) -> int:
        return hash(self.uri)
