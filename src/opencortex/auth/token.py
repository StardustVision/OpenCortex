# SPDX-License-Identifier: Apache-2.0
"""JWT token generation, verification, and record management.

Tokens use HS256 signing with a server-generated secret key stored at
``{data_root}/auth_secret.key``.  Token records (issued tokens with metadata)
are persisted in ``{data_root}/tokens.json``.
"""

import hashlib
import os
import secrets
import time
from pathlib import Path
from threading import RLock
from typing import Any

import jwt
import orjson as json
import structlog

_SECRET_KEY_FILE = "auth_secret.key"
_TOKEN_RECORDS_FILE = "tokens.json"
_ALGORITHM = "HS256"
_cache_lock = RLock()
_record_cache: dict[str, tuple[int, int, list[dict[str, Any]]]] = {}
logger = structlog.get_logger(__name__)


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
    tmp_path = key_path.with_suffix(".tmp")
    tmp_path.write_text(secret, encoding="utf-8")
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, key_path)
    return secret


# ---------------------------------------------------------------------------
# Token generation / verification
# ---------------------------------------------------------------------------


def generate_token(
    tenant_id: str,
    user_id: str,
    secret: str,
    *,
    role: str = "user",
) -> str:
    """Generate a JWT with tenant and user identity claims.

    Claims::

        {
            "tid": tenant_id,
            "uid": user_id,
            "iat": <unix timestamp>,
            "role": "<role>"
        }

    The token does **not** expire (no ``exp`` claim).
    """
    payload: dict[str, Any] = {
        "tid": tenant_id,
        "uid": user_id,
        "iat": int(time.time()),
        "role": role,
    }
    return jwt.encode(payload, secret, algorithm=_ALGORITHM)


def decode_token(token: str, secret: str) -> dict[str, Any]:
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


def load_token_records(data_root: str) -> list[dict[str, Any]]:
    """Read all token records from ``{data_root}/tokens.json``."""
    p = _records_path(data_root)
    if not p.exists():
        return []
    stat = p.stat()
    cache_key = str(p)
    with _cache_lock:
        cached = _record_cache.get(cache_key)
        if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
            return [dict(record) for record in cached[2]]
    try:
        records = json.loads(p.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "opencortex.token_records_load_failed",
            path=str(p),
            error_type=type(exc).__name__,
        )
        return []
    if not isinstance(records, list):
        return []
    normalized = [dict(record) for record in records if isinstance(record, dict)]
    with _cache_lock:
        _record_cache[cache_key] = (stat.st_mtime_ns, stat.st_size, normalized)
    return [dict(record) for record in normalized]


def public_token_record(record: dict[str, Any]) -> dict[str, Any]:
    """Return a token record for admin API responses."""
    token = str(record.get("token", "") or "")
    token_prefix = str(record.get("token_prefix", "") or "")
    if not token_prefix and token:
        token_prefix = token_hash(token)[:16]
    return {
        "tenant_id": str(record.get("tenant_id", "") or ""),
        "user_id": str(record.get("user_id", "") or ""),
        "role": str(record.get("role", "") or "user"),
        "created_at": str(record.get("created_at", "") or ""),
        "token_prefix": token_prefix,
        "token": token,
    }


def token_hash(token: str) -> str:
    """Return the persisted hash for one token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def find_token_record(data_root: str, token: str) -> dict[str, Any] | None:
    """Return the saved token record matching an issued token."""
    hashed = token_hash(token)
    for record in load_token_records(data_root):
        if record.get("token_hash") == hashed or record.get("token") == token:
            return record
    return None


def store_token_records(data_root: str, records: list[dict[str, Any]]) -> None:
    """Persist token records atomically."""
    p = _records_path(data_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp_path.write_bytes(json.dumps(records, option=json.OPT_INDENT_2))
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, p)
    stat = p.stat()
    with _cache_lock:
        _record_cache[str(p)] = (
            stat.st_mtime_ns,
            stat.st_size,
            [dict(r) for r in records],
        )


def register_token_record(
    data_root: str,
    token: str,
    *,
    secret: str,
) -> dict[str, Any]:
    """Decode and persist an externally configured token."""
    claims = decode_token(token, secret)
    tenant_id = str(claims.get("tid", "") or "")
    user_id = str(claims.get("uid", "") or "")
    role = str(claims.get("role", "") or "user")
    if not tenant_id or not user_id:
        raise ValueError("Configured token must contain tid and uid claims")
    save_token_record(
        data_root,
        token,
        tenant_id,
        user_id,
        role=role,
    )
    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "role": role,
    }


def save_token_record(
    data_root: str,
    token: str,
    tenant_id: str,
    user_id: str,
    *,
    role: str = "user",
) -> None:
    """Save a token record to ``{data_root}/tokens.json``.

    Deduplicates by (tenant_id, user_id, role). If a record with
    the same identity already exists, it is replaced with the new token.
    """
    from datetime import datetime, timezone

    with _cache_lock:
        records = load_token_records(data_root)
        records = [
            r
            for r in records
            if not (
                r.get("tenant_id") == tenant_id
                and r.get("user_id") == user_id
                and str(r.get("role", "user") or "user") == role
            )
        ]
        hashed = token_hash(token)
        records.append(
            {
                "token_hash": hashed,
                "token_prefix": hashed[:16],
                "token": token,
                "tenant_id": tenant_id,
                "user_id": user_id,
                "role": role,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        store_token_records(data_root, records)


def revoke_token(data_root: str, token_prefix: str) -> dict[str, Any] | None:
    """Remove a token record matching *token_prefix* (first match).

    Returns the removed record, or ``None`` if not found.
    """
    with _cache_lock:
        records = load_token_records(data_root)
        for i, rec in enumerate(records):
            prefix = str(rec.get("token_prefix", "") or "").removesuffix("...")
            hashed = str(rec.get("token_hash", "") or "")
            legacy_token = str(rec.get("token", "") or "")
            if (
                prefix == token_prefix
                or hashed.startswith(token_prefix)
                or legacy_token.startswith(token_prefix)
            ):
                removed = records.pop(i)
                store_token_records(data_root, records)
                return removed
    return None
