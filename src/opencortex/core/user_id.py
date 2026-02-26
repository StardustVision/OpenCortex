# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# Ported from OpenViking (https://github.com/volcengine/openviking)
# SPDX-License-Identifier: Apache-2.0
"""User identifier for OpenCortex with tenant-based isolation."""

import hashlib
import re
from typing import Optional


class UserIdentifier:
    """Identifies a user within a tenant for data isolation.

    The triple (tenant_id, user_id, agent_id) uniquely identifies a user context.
    tenant_id and user_id come from CortexConfig; agent_id identifies the AI agent.
    """

    def __init__(self, tenant_id: str, user_id: str, agent_id: str = "default"):
        self._tenant_id = tenant_id
        self._user_id = user_id
        self._agent_id = agent_id

        verr = self._validate_error()
        if verr:
            raise ValueError(verr)

    @classmethod
    def from_config(cls, agent_id: str = "default") -> "UserIdentifier":
        """Create UserIdentifier from global CortexConfig."""
        from opencortex.config import get_config

        config = get_config()
        return cls(config.tenant_id, config.user_id, agent_id)

    @classmethod
    def the_default_user(cls) -> "UserIdentifier":
        """Create default user (tenant=default, user=default, agent=default)."""
        return cls("default", "default", "default")

    def _validate_error(self) -> str:
        """Validate: all fields must be non-empty, chars in [a-zA-Z0-9_-]."""
        pattern = re.compile(r"^[a-zA-Z0-9_-]+$")
        for field_name, value in [
            ("tenant_id", self._tenant_id),
            ("user_id", self._user_id),
            ("agent_id", self._agent_id),
        ]:
            if not value:
                return f"{field_name} is empty"
            if not pattern.match(value):
                return f"{field_name} must be alpha-numeric string."
        return ""

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    @property
    def user_id(self) -> str:
        return self._user_id

    @property
    def agent_id(self) -> str:
        return self._agent_id

    def unique_space_name(self, short: bool = True) -> str:
        """Anonymized space name: {user_id}_{md5[:8]}."""
        h = hashlib.md5((self._user_id + self._agent_id).encode()).hexdigest()
        if short:
            return f"{self._user_id}_{h[:8]}"
        return f"{self._user_id}_{h}"

    def memory_space_uri(self) -> str:
        """User's private memory URI under tenant."""
        from opencortex.utils.uri import CortexURI

        return CortexURI.build_private(
            self._tenant_id, self._user_id, "memories"
        )

    def agent_cases_uri(self) -> str:
        """User's private agent cases URI under tenant."""
        from opencortex.utils.uri import CortexURI

        return CortexURI.build_private(
            self._tenant_id, self._user_id, "agent", "memories", "cases"
        )

    def workspace_uri(self, project: str = "") -> str:
        """User's workspace URI under tenant."""
        from opencortex.utils.uri import CortexURI

        parts = ["workspace"]
        if project:
            parts.append(project)
        return CortexURI.build_private(
            self._tenant_id, self._user_id, *parts
        )

    def reinforcement_uri(self) -> str:
        """User's reinforcement data URI under tenant."""
        from opencortex.utils.uri import CortexURI

        return CortexURI.build_private(
            self._tenant_id, self._user_id, "reinforcement"
        )

    def feedback_uri(self) -> str:
        """User's feedback data URI under tenant."""
        from opencortex.utils.uri import CortexURI

        return CortexURI.build_private(
            self._tenant_id, self._user_id, "feedback"
        )

    def session_uri(self, session_id: str) -> str:
        """User's session URI under tenant."""
        from opencortex.utils.uri import CortexURI

        return CortexURI.build_private(
            self._tenant_id, self._user_id, "session", session_id
        )

    def to_dict(self) -> dict:
        return {
            "tenant_id": self._tenant_id,
            "user_id": self._user_id,
            "agent_id": self._agent_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "UserIdentifier":
        return cls(
            data.get("tenant_id", "default"),
            data.get("user_id", "default"),
            data.get("agent_id", "default"),
        )

    def __str__(self) -> str:
        return f"{self._tenant_id}:{self._user_id}:{self._agent_id}"

    def __repr__(self) -> str:
        return self.__str__()

    def __eq__(self, other) -> bool:
        if not isinstance(other, UserIdentifier):
            return False
        return (
            self._tenant_id == other._tenant_id
            and self._user_id == other._user_id
            and self._agent_id == other._agent_id
        )
