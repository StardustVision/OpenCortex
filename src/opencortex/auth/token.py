# SPDX-License-Identifier: Apache-2.0
"""
JWT token generation, verification, and record management.

Tokens use HS256 signing with a server-generated secret key stored at
``{data_root}/auth_secret.key``.  Token records (issued tokens with metadata)
are persisted in ``{data_root}/tokens.json``.
"""

import os
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import jwt
import orjson as json

_SECRET_KEY_FILE = "auth_secret.key"
_TOKEN_RECORDS_FILE = "tokens.json"
_ALGORITHM = "HS256"


# ---------------------------------------------------------------------------
# Secret key management
# ---------------------------------------------------------------------------

def ensure_secret(data_root: str) -> str:
    """Read or auto-generate the HS256 secret key.

    The key file is stored at ``{data_root}/auth_secret.key``.  If it does
    not exist a new 64-byte hex secret is generated and written atomically.
    """
    key_path = Path(data_root) / _SECRET_KEY_FILE
    if key_path.exists():
        return key_path.read_text(encoding="utf-8").strip()

    key_path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_hex(64)
    # Atomic write via temp + rename
    tmp_path = key_path.with_suffix(".tmp")
    tmp_path.write_text(secret, encoding="utf-8")
    tmp_path.rename(key_path)
    # Restrict permissions to owner-only
    os.chmod(key_path, 0o600)
    return secret


# ---------------------------------------------------------------------------
# Token generation / verification
# ---------------------------------------------------------------------------

def generate_token(tenant_id: str, user_id: str, secret: str, *, role: str = "user") -> str:
    """Generate a JWT with tenant and user identity claims.

    Claims::

        {
            "tid": tenant_id,
            "uid": user_id,
            "iat": <unix timestamp>,
            "role": "<role>"  # only when role != "user"
        }

    The token does **not** expire (no ``exp`` claim).
    """
    payload: Dict[str, Any] = {
        "tid": tenant_id,
        "uid": user_id,
        "iat": int(time.time()),
    }
    if role != "user":
        payload["role"] = role
    return jwt.encode(payload, secret, algorithm=_ALGORITHM)


def decode_token(token: str, secret: str) -> Dict[str, Any]:
    """Verify signature and decode JWT claims.

    Returns the decoded payload dict.
    Raises ``jwt.InvalidTokenError`` (or subclass) on failure.
    """
    return jwt.decode(
        token,
        secret,
        algorithms=[_ALGORITHM],
        options={"require": ["tid", "uid", "iat"]},
    )


def generate_admin_token(secret: str) -> str:
    """Generate an admin JWT with tid=_system, uid=_admin, role=admin."""
    return generate_token("_system", "_admin", secret, role="admin")


# ---------------------------------------------------------------------------
# Token records (issued token bookkeeping)
# ---------------------------------------------------------------------------

def _records_path(data_root: str) -> Path:
    return Path(data_root) / _TOKEN_RECORDS_FILE


def load_token_records(data_root: str) -> List[Dict[str, Any]]:
    """Read all token records from ``{data_root}/tokens.json``."""
    p = _records_path(data_root)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_bytes())
    except Exception:
        return []


def save_token_record(
    data_root: str,
    token: str,
    tenant_id: str,
    user_id: str,
    *,
    role: str = "user",
) -> None:
    """Save a token record to ``{data_root}/tokens.json``.

    Deduplicates by (tenant_id, user_id) — if a record with the same
    identity already exists, it is replaced with the new token.
    """
    from datetime import datetime, timezone

    records = load_token_records(data_root)
    # Remove existing record for the same tenant_id + user_id
    records = [
        r for r in records
        if not (r["tenant_id"] == tenant_id and r["user_id"] == user_id)
    ]
    records.append({
        "token": token,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "role": role,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    p = _records_path(data_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(json.dumps(records, option=json.OPT_INDENT_2))


def revoke_token(data_root: str, token_prefix: str) -> Optional[Dict[str, Any]]:
    """Remove a token record matching *token_prefix* (first match).

    Returns the removed record, or ``None`` if not found.
    """
    records = load_token_records(data_root)
    for i, rec in enumerate(records):
        if rec["token"].startswith(token_prefix):
            removed = records.pop(i)
            p = _records_path(data_root)
            p.write_bytes(json.dumps(records, option=json.OPT_INDENT_2))
            return removed
    return None
