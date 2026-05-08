# SPDX-License-Identifier: Apache-2.0
"""URI helpers for opencortex_app."""

from __future__ import annotations


class CortexURI:
    """Small URI parser for scope checks."""

    def __init__(self, uri: str) -> None:
        self.uri = uri

    @property
    def is_private(self) -> bool:
        """Return whether the URI points at a private user scope."""
        parts = self.uri.split("/")
        try:
            tenant_index = parts.index("opencortex:") + 2
        except ValueError:
            return True
        if len(parts) <= tenant_index + 1:
            return True
        return parts[tenant_index + 1] != "shared"
