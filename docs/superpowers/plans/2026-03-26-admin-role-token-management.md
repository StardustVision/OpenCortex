# Admin Role + Token Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add admin JWT role and token management so administrators can view all users' memories and manage tokens, while regular users only manage their own data.

**Architecture:** JWT gains an optional `role` claim (`"admin"` or absent=`"user"`). Server auto-generates an admin token on first startup. Admin routes are isolated in `src/opencortex/http/admin_routes.py` (separate from business routes in `server.py`), registered via `include_router`. Frontend detects role from JWT payload and conditionally shows admin UI (Token Management page, user filter dropdowns).

**Tech Stack:** Python (FastAPI, PyJWT, contextvars), TypeScript/React (Vite, Tailwind, Lucide)

---

### Task 1: Add role support to token generation

**Files:**
- Modify: `src/opencortex/auth/token.py`

- [ ] **Step 1: Add `role` parameter to `generate_token`**

In `generate_token()`, add `role: str = "user"` parameter. Only include role in payload when it's not `"user"` (backward compat):

```python
def generate_token(tenant_id: str, user_id: str, secret: str, *, role: str = "user") -> str:
    payload: Dict[str, Any] = {
        "tid": tenant_id,
        "uid": user_id,
        "iat": int(time.time()),
    }
    if role != "user":
        payload["role"] = role
    return jwt.encode(payload, secret, algorithm=_ALGORITHM)
```

- [ ] **Step 2: Add `role` to `save_token_record`**

Add `role: str = "user"` parameter. Include in record dict:

```python
def save_token_record(
    data_root: str,
    token: str,
    tenant_id: str,
    user_id: str,
    *,
    role: str = "user",
) -> None:
    from datetime import datetime, timezone
    records = load_token_records(data_root)
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
```

- [ ] **Step 3: Add `generate_admin_token` convenience function**

```python
def generate_admin_token(secret: str) -> str:
    """Generate an admin JWT with tid=_system, uid=_admin, role=admin."""
    return generate_token("_system", "_admin", secret, role="admin")
```

- [ ] **Step 4: Verify existing tests still pass**

Run: `uv run python3 -m unittest discover -s tests -p "test_*" -v 2>&1 | tail -5`

- [ ] **Step 5: Commit**

```bash
git add src/opencortex/auth/token.py
git commit -m "feat: add role claim support to JWT token generation"
```

---

### Task 2: Add role contextvar to request context

**Files:**
- Modify: `src/opencortex/http/request_context.py`

- [ ] **Step 1: Add role contextvar and helpers**

Append after the Collection Name API section:

```python
# ---------------------------------------------------------------------------
# Role API
# ---------------------------------------------------------------------------

_request_role: ContextVar[Optional[str]] = ContextVar(
    "_request_role", default=None
)


def set_request_role(role: str) -> Token[Optional[str]]:
    """Set per-request role. Returns token for later reset."""
    return _request_role.set(role)


def reset_request_role(token: Token[Optional[str]]) -> None:
    """Reset role contextvar."""
    _request_role.reset(token)


def get_effective_role() -> str:
    """Return the effective role for the current request ('admin' or 'user')."""
    return _request_role.get() or "user"


def is_admin() -> bool:
    """Return True if the current request is from an admin token."""
    return get_effective_role() == "admin"
```

- [ ] **Step 2: Commit**

```bash
git add src/opencortex/http/request_context.py
git commit -m "feat: add role contextvar for admin/user role tracking"
```

---

### Task 3: Wire role into middleware and auto-generate admin token

**Files:**
- Modify: `src/opencortex/http/server.py`

- [ ] **Step 1: Update imports**

Add to the imports from `request_context`:
```python
from opencortex.http.request_context import (
    set_request_identity, reset_request_identity, get_effective_identity,
    set_request_project_id, reset_request_project_id,
    set_collection_name,
    set_request_role, reset_request_role,  # NEW
)
```

Add to the imports from `auth.token`:
```python
from opencortex.auth.token import (
    decode_token, ensure_secret,
    generate_admin_token, load_token_records, save_token_record,  # NEW
)
```

- [ ] **Step 2: Extract and set role in middleware dispatch**

In `RequestContextMiddleware.dispatch`, after `id_tokens = set_request_identity(tenant_id, user_id)` (line 115), add:

```python
        role = claims.get("role", "user")
        role_token = set_request_role(role)
```

In the `finally` block (after `reset_request_project_id`), add:

```python
            reset_request_role(role_token)
```

- [ ] **Step 3: Auto-generate admin token in lifespan**

In `_lifespan()`, after `logger.info("[HTTP] Orchestrator initialized ...")` (line 143), add:

```python
    # Auto-generate admin token on first startup
    records = load_token_records(config.data_root)
    admin_rec = next((r for r in records if r.get("role") == "admin"), None)
    if admin_rec:
        logger.info("[HTTP] Admin token (existing): %s", admin_rec["token"])
    else:
        admin_token = generate_admin_token(_jwt_secret)
        save_token_record(config.data_root, admin_token, "_system", "_admin", role="admin")
        logger.info("[HTTP] Admin token (new): %s", admin_token)
```

- [ ] **Step 4: Verify server starts and logs admin token**

Run: `uv run opencortex-server &; sleep 3; kill %1`

Expected: Log line containing `[HTTP] Admin token (new): eyJ...` on first run.

- [ ] **Step 5: Commit**

```bash
git add src/opencortex/http/server.py
git commit -m "feat: wire role into middleware, auto-generate admin token on startup"
```

---

### Task 4: Add Pydantic models for admin

**Files:**
- Modify: `src/opencortex/http/models.py`

- [ ] **Step 1: Add admin request models**

Append to `models.py`:

```python
# =========================================================================
# Admin — Token Management
# =========================================================================

class CreateTokenRequest(BaseModel):
    tenant_id: str
    user_id: str


class RevokeTokenRequest(BaseModel):
    token_prefix: str
```

- [ ] **Step 2: Commit**

```bash
git add src/opencortex/http/models.py
git commit -m "feat: add admin token management Pydantic models"
```

---

### Task 5: Add `list_memories_admin` to orchestrator

**Files:**
- Modify: `src/opencortex/orchestrator.py`

- [ ] **Step 1: Add `list_memories_admin` method**

Insert after `list_memories()` (after line 1745):

```python
    async def list_memories_admin(
        self,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        category: Optional[str] = None,
        context_type: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List memories across all users (admin only). No scope isolation."""
        self._ensure_init()

        conds: List[Dict[str, Any]] = [
            {"op": "must_not", "field": "context_type", "conds": ["staging"]},
        ]
        if tenant_id:
            conds.append({"op": "must", "field": "source_tenant_id", "conds": [tenant_id]})
        if user_id:
            conds.append({"op": "must", "field": "source_user_id", "conds": [user_id]})
        if category:
            conds.append({"op": "must", "field": "category", "conds": [category]})
        if context_type:
            conds.append({"op": "must", "field": "context_type", "conds": [context_type]})

        combined: Dict[str, Any] = {"op": "and", "conds": conds}

        records = await self._storage.filter(
            self._get_collection(),
            combined,
            limit=limit,
            offset=offset,
            order_by="updated_at",
            order_desc=True,
        )

        return [
            {
                "uri": r.get("uri", ""),
                "abstract": r.get("abstract", ""),
                "category": r.get("category", ""),
                "context_type": r.get("context_type", ""),
                "scope": r.get("scope", ""),
                "project_id": r.get("project_id", ""),
                "source_tenant_id": r.get("source_tenant_id", ""),
                "source_user_id": r.get("source_user_id", ""),
                "updated_at": r.get("updated_at", ""),
                "created_at": r.get("created_at", ""),
            }
            for r in records
        ]
```

- [ ] **Step 2: Commit**

```bash
git add src/opencortex/orchestrator.py
git commit -m "feat: add admin cross-tenant memory listing to orchestrator"
```

---

### Task 6: Create `admin_routes.py` — isolated admin router

This is the key isolation step. All admin + auth endpoints go into a separate file. Existing admin endpoints (`reembed`, `search_debug`, `bench collection`, `migration`) are also moved here from `server.py`.

**Files:**
- Create: `src/opencortex/http/admin_routes.py`
- Modify: `src/opencortex/http/server.py`

- [ ] **Step 1: Create `admin_routes.py` with all admin routes**

Create `src/opencortex/http/admin_routes.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""
Admin and auth API routes for OpenCortex.

Isolated from business routes (server.py). All /api/v1/admin/* endpoints
require admin role JWT. /api/v1/auth/* endpoints are open to all
authenticated users.
"""

import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from opencortex.auth.token import (
    generate_token, load_token_records, revoke_token, save_token_record,
)
from opencortex.http.models import (
    CreateTokenRequest, MemorySearchRequest, RevokeTokenRequest,
)
from opencortex.http.request_context import (
    get_effective_identity, get_effective_role, is_admin,
)

logger = logging.getLogger(__name__)

# Router will be included by create_app() in server.py
router = APIRouter()

# Set by register_admin_routes() — avoids circular imports
_orchestrator = None
_jwt_secret = None


def register_admin_routes(orchestrator, jwt_secret: str) -> None:
    """Bind orchestrator and secret so route handlers can use them."""
    global _orchestrator, _jwt_secret
    _orchestrator = orchestrator
    _jwt_secret = jwt_secret


def _require_admin() -> None:
    """Raise 403 if the current request is not from an admin token."""
    if not is_admin():
        raise HTTPException(status_code=403, detail="Admin access required")


# =========================================================================
# Auth (any authenticated user)
# =========================================================================

@router.get("/api/v1/auth/me")
async def auth_me() -> Dict[str, Any]:
    """Return current user identity and role."""
    tid, uid = get_effective_identity()
    return {"tenant_id": tid, "user_id": uid, "role": get_effective_role()}


# =========================================================================
# Admin — Token Management
# =========================================================================

@router.get("/api/v1/admin/tokens")
async def admin_list_tokens() -> Dict[str, Any]:
    """List all token records (token truncated to prefix)."""
    _require_admin()
    records = load_token_records(_orchestrator.config.data_root)
    return {"tokens": [
        {
            "tenant_id": r["tenant_id"],
            "user_id": r["user_id"],
            "role": r.get("role", "user"),
            "created_at": r.get("created_at", ""),
            "token_prefix": r["token"][:20] + "...",
        }
        for r in records
    ]}


@router.post("/api/v1/admin/tokens")
async def admin_create_token(req: CreateTokenRequest) -> Dict[str, Any]:
    """Create a new user token."""
    _require_admin()
    token = generate_token(req.tenant_id, req.user_id, _jwt_secret)
    save_token_record(_orchestrator.config.data_root, token, req.tenant_id, req.user_id)
    return {"token": token, "tenant_id": req.tenant_id, "user_id": req.user_id, "role": "user"}


@router.delete("/api/v1/admin/tokens")
async def admin_revoke_token(req: RevokeTokenRequest) -> Dict[str, Any]:
    """Revoke a token by prefix. Cannot revoke admin tokens."""
    _require_admin()
    records = load_token_records(_orchestrator.config.data_root)
    target = next((r for r in records if r["token"].startswith(req.token_prefix)), None)
    if not target:
        raise HTTPException(status_code=404, detail="Token not found")
    if target.get("role") == "admin":
        raise HTTPException(status_code=400, detail="Cannot revoke admin token")
    removed = revoke_token(_orchestrator.config.data_root, req.token_prefix)
    return {"status": "ok", "revoked": {"tenant_id": removed["tenant_id"], "user_id": removed["user_id"]}}


# =========================================================================
# Admin — Memory Listing (cross-tenant)
# =========================================================================

@router.get("/api/v1/admin/memories")
async def admin_list_memories(
    tenant_id: Optional[str] = None,
    user_id: Optional[str] = None,
    category: Optional[str] = None,
    context_type: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    """List memories across all users (admin only)."""
    _require_admin()
    items = await _orchestrator.list_memories_admin(
        tenant_id=tenant_id, user_id=user_id,
        category=category, context_type=context_type,
        limit=limit, offset=offset,
    )
    return {"results": items, "total": len(items)}


# =========================================================================
# Admin — System Operations (moved from server.py)
# =========================================================================

@router.post("/api/v1/admin/reembed")
async def admin_reembed() -> Dict[str, Any]:
    """Re-embed all records with the current embedding model."""
    _require_admin()
    count = await _orchestrator.reembed_all()
    return {"status": "ok", "updated": count}


@router.post("/api/v1/admin/search_debug")
async def admin_search_debug(req: MemorySearchRequest) -> Dict[str, Any]:
    """Diagnostic: show raw vector scores, rerank scores, and fused scores."""
    _require_admin()
    storage = _orchestrator._storage
    embedder = _orchestrator._embedder
    retriever = _orchestrator._retriever

    loop = asyncio.get_running_loop()
    embed_result = await asyncio.wait_for(
        loop.run_in_executor(None, embedder.embed_query, req.query),
        timeout=2.0,
    )
    raw_results = await storage.search(
        "context",
        query_vector=embed_result.dense_vector,
        sparse_query_vector=embed_result.sparse_vector,
        limit=req.limit or 10,
    )

    rerank_scores = None
    if retriever._rerank_client:
        docs = [r.get("abstract", "") for r in raw_results]
        rerank_scores = await retriever._rerank_client.rerank(req.query, docs)

    rows = []
    beta = retriever._fusion_beta
    for i, r in enumerate(raw_results):
        raw_score = r.get("_score", 0.0)
        rr_score = rerank_scores[i] if rerank_scores else None
        fused = (
            beta * rr_score + (1 - beta) * raw_score
            if rr_score is not None
            else raw_score
        )
        rows.append({
            "rank": i + 1,
            "abstract": r.get("abstract", "")[:80],
            "raw_vector_score": round(raw_score, 5),
            "rerank_score": round(rr_score, 5) if rr_score is not None else None,
            "fused_score": round(fused, 5),
            "uri": r.get("uri", ""),
        })

    return {
        "query": req.query,
        "fusion_beta": beta,
        "rerank_mode": retriever._rerank_client.mode if retriever._rerank_client else "disabled",
        "results": rows,
    }


@router.post("/api/v1/admin/collection")
async def create_bench_collection(request: Request):
    """Create a benchmark-isolated collection (name must start with bench_)."""
    _require_admin()
    body = await request.json()
    name = body.get("name", "")
    if not name.startswith("bench_"):
        return JSONResponse({"error": "Collection name must start with bench_"}, status_code=400)
    dim = _orchestrator._config.embedding_dimension
    from opencortex.storage.collection_schemas import CollectionSchemas
    schema = CollectionSchemas.context_collection(name, dim)
    await _orchestrator._storage.create_collection(name, schema)
    return {"status": "created", "collection": name}


@router.delete("/api/v1/admin/collection/{name}")
async def delete_bench_collection(name: str):
    """Delete a benchmark-isolated collection (name must start with bench_)."""
    _require_admin()
    if not name.startswith("bench_"):
        return JSONResponse({"error": "Can only delete bench_ collections"}, status_code=400)
    await _orchestrator._storage.drop_collection(name)
    return {"status": "deleted", "collection": name}


# =========================================================================
# Migration (admin only)
# =========================================================================

@router.post("/api/v1/migration/overview-first")
async def migration_overview_first(
    dry_run: bool = False,
    batch: int = 50,
) -> Dict[str, Any]:
    """Run v0.3.2 overview-first migration (re-generate L0/L1 from L2)."""
    _require_admin()
    from opencortex.migration.v032_overview_first import migrate_overview_first
    return await migrate_overview_first(
        _orchestrator, dry_run=dry_run, batch_size=batch,
    )
```

- [ ] **Step 2: Remove admin/migration routes from `server.py` and include the router**

In `server.py`:

1. Delete the entire `# Admin` section (lines ~466-550): `admin_reembed`, `admin_search_debug`, `create_bench_collection`, `delete_bench_collection`
2. Delete the entire `# Migration` section (lines ~552-565): `migration_overview_first`
3. In `create_app()`, after `app.add_middleware(RequestContextMiddleware)`, add:

```python
    from opencortex.http.admin_routes import router as admin_router, register_admin_routes
    app.include_router(admin_router)
```

4. In `_lifespan()`, after the admin token auto-generation block, add:

```python
    from opencortex.http.admin_routes import register_admin_routes
    register_admin_routes(_orchestrator, _jwt_secret)
```

- [ ] **Step 3: Verify endpoints still work**

```bash
ADMIN_TOKEN="<from server log>"

# Auth
curl -s -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:8921/api/v1/auth/me
# Expected: {"tenant_id":"_system","user_id":"_admin","role":"admin"}

# Tokens
curl -s -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:8921/api/v1/admin/tokens
# Expected: {"tokens":[...]}

# Create token
curl -s -H "Authorization: Bearer $ADMIN_TOKEN" -X POST -H "Content-Type: application/json" \
  -d '{"tenant_id":"test","user_id":"u1"}' http://localhost:8921/api/v1/admin/tokens
# Expected: {"token":"eyJ...","tenant_id":"test","user_id":"u1","role":"user"}

# Memories
curl -s -H "Authorization: Bearer $ADMIN_TOKEN" \
  "http://localhost:8921/api/v1/admin/memories?limit=3" | python3 -m json.tool
# Expected: results with source_tenant_id, source_user_id fields

# Moved endpoints still work
curl -s -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:8921/api/v1/system/status?type=health
# Expected: still works (this is a business route, not moved)

# Admin guard
USER_TOKEN="<regular user token>"
curl -s -w "%{http_code}" -H "Authorization: Bearer $USER_TOKEN" -X POST http://localhost:8921/api/v1/admin/reembed
# Expected: 403
```

- [ ] **Step 4: Commit**

```bash
git add src/opencortex/http/admin_routes.py src/opencortex/http/server.py
git commit -m "feat: isolate admin routes into admin_routes.py with admin guard"
```

---

### Task 7: Frontend — add role detection to API context

**Files:**
- Modify: `web/src/api/Context.tsx`

- [ ] **Step 1: Add JWT decode helper and role to context**

```typescript
import React, { createContext, useContext, useState } from 'react';
import { OpenCortexClient } from './client';

function decodeJwtPayload(token: string): Record<string, any> {
  try {
    const payload = token.split('.')[1];
    return JSON.parse(atob(payload));
  } catch {
    return {};
  }
}

interface ApiContextType {
  client: OpenCortexClient | null;
  token: string | null;
  role: string;
  setToken: (token: string) => void;
  logout: () => void;
}

const ApiContext = createContext<ApiContextType | undefined>(undefined);

export const ApiProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [token, setTokenState] = useState<string | null>(() => {
    const urlParams = new URLSearchParams(window.location.search);
    const urlToken = urlParams.get('token');
    if (urlToken) {
      localStorage.setItem('opencortex_token', urlToken);
      const newUrl = window.location.pathname;
      window.history.replaceState({}, '', newUrl);
      return urlToken;
    }
    return localStorage.getItem('opencortex_token');
  });

  const [role, setRole] = useState<string>(() => {
    if (token) return decodeJwtPayload(token).role || 'user';
    return 'user';
  });

  const [client, setClient] = useState<OpenCortexClient | null>(() => {
    if (token) return new OpenCortexClient('', token);
    return null;
  });

  const setToken = (newToken: string) => {
    localStorage.setItem('opencortex_token', newToken);
    setTokenState(newToken);
    setRole(decodeJwtPayload(newToken).role || 'user');
    setClient(new OpenCortexClient('', newToken));
  };

  const logout = () => {
    localStorage.removeItem('opencortex_token');
    setTokenState(null);
    setRole('user');
    setClient(null);
  };

  return (
    <ApiContext.Provider value={{ client, token, role, setToken, logout }}>
      {children}
    </ApiContext.Provider>
  );
};

export const useApi = () => {
  const context = useContext(ApiContext);
  if (context === undefined) {
    throw new Error('useApi must be used within an ApiProvider');
  }
  return context;
};
```

- [ ] **Step 2: Run type check**

Run: `cd web && npx tsc --noEmit`

- [ ] **Step 3: Commit**

```bash
git add web/src/api/Context.tsx
git commit -m "feat(web): add role detection from JWT payload"
```

---

### Task 8: Frontend — add admin types and API client methods

**Files:**
- Modify: `web/src/api/types.ts`
- Modify: `web/src/api/client.ts`

- [ ] **Step 1: Add types**

Append to `types.ts`:

```typescript
export interface TokenRecord {
  tenant_id: string;
  user_id: string;
  role: string;
  created_at: string;
  token_prefix: string;
}

export interface AuthMe {
  tenant_id: string;
  user_id: string;
  role: string;
}

export interface AdminMemoryRecord {
  uri: string;
  abstract: string;
  category: string;
  context_type: string;
  scope: string;
  project_id: string;
  source_tenant_id: string;
  source_user_id: string;
  updated_at: string;
  created_at: string;
}

export interface AdminListResponse {
  results: AdminMemoryRecord[];
  total: number;
}
```

- [ ] **Step 2: Add client methods**

Add imports and methods to `client.ts`:

```typescript
import {
  SystemHealth, MemoryStats, SearchResponse, ListResponse, ContentResponse,
  KnowledgeCandidate, ArchivistStatus, SearchDebugResponse,
  TokenRecord, AuthMe, AdminListResponse  // NEW
} from './types';
```

Add methods to the class:

```typescript
  // Auth
  getMe(): Promise<AuthMe> {
    return this.request('GET', '/api/v1/auth/me');
  }

  // Admin — Tokens
  listTokens(): Promise<{ tokens: TokenRecord[] }> {
    return this.request('GET', '/api/v1/admin/tokens');
  }

  createToken(tenant_id: string, user_id: string): Promise<{ token: string; tenant_id: string; user_id: string; role: string }> {
    return this.request('POST', '/api/v1/admin/tokens', { tenant_id, user_id });
  }

  revokeToken(token_prefix: string): Promise<{ status: string }> {
    return this.request('DELETE', '/api/v1/admin/tokens', { token_prefix });
  }

  // Admin — Memories
  listAllMemories(params: { tenant_id?: string; user_id?: string; category?: string; context_type?: string; limit?: number; offset?: number }): Promise<AdminListResponse> {
    const query = new URLSearchParams();
    if (params.tenant_id) query.append('tenant_id', params.tenant_id);
    if (params.user_id) query.append('user_id', params.user_id);
    if (params.category) query.append('category', params.category);
    if (params.context_type) query.append('context_type', params.context_type);
    if (params.limit) query.append('limit', params.limit.toString());
    if (params.offset) query.append('offset', params.offset.toString());
    return this.request('GET', `/api/v1/admin/memories?${query.toString()}`);
  }
```

- [ ] **Step 3: Run type check**

Run: `cd web && npx tsc --noEmit`

- [ ] **Step 4: Commit**

```bash
git add web/src/api/types.ts web/src/api/client.ts
git commit -m "feat(web): add admin API types and client methods"
```

---

### Task 9: Frontend — Token Management page

**Files:**
- Create: `web/src/pages/Tokens.tsx`
- Modify: `web/src/App.tsx`
- Modify: `web/src/components/layout/Sidebar.tsx`

- [ ] **Step 1: Create Tokens page**

Create `web/src/pages/Tokens.tsx`:

```tsx
import React, { useState } from 'react';
import { PageLayout } from '../components/layout/PageLayout';
import { Card } from '../components/common/Card';
import { Button } from '../components/common/Button';
import { Badge } from '../components/common/Badge';
import { Modal } from '../components/common/Modal';
import { LoadingSpinner } from '../components/common/LoadingSpinner';
import { EmptyState } from '../components/common/EmptyState';
import { useApi } from '../api/Context';
import { useFetch } from '../hooks/useFetch';
import { TokenRecord } from '../api/types';
import { Key, Plus, Trash2, Copy, Check, AlertTriangle } from 'lucide-react';

export const Tokens: React.FC = () => {
  const { client, role } = useApi();
  const { data, loading, refetch } = useFetch(() => client!.listTokens());

  const [showCreate, setShowCreate] = useState(false);
  const [tenantId, setTenantId] = useState('');
  const [userId, setUserId] = useState('');
  const [creating, setCreating] = useState(false);
  const [createdToken, setCreatedToken] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const [revokeTarget, setRevokeTarget] = useState<TokenRecord | null>(null);
  const [revoking, setRevoking] = useState(false);

  if (role !== 'admin') {
    return (
      <PageLayout title="Tokens">
        <EmptyState
          icon={<AlertTriangle size={48} className="text-red-300" />}
          title="Access Denied"
          message="Admin access is required to manage tokens."
        />
      </PageLayout>
    );
  }

  const handleCreate = async () => {
    if (!client || !tenantId.trim() || !userId.trim()) return;
    setCreating(true);
    try {
      const res = await client.createToken(tenantId.trim(), userId.trim());
      setCreatedToken(res.token);
      setTenantId('');
      setUserId('');
      refetch();
    } catch (e) {
      console.error('Failed to create token', e);
    } finally {
      setCreating(false);
    }
  };

  const handleRevoke = async () => {
    if (!client || !revokeTarget) return;
    setRevoking(true);
    try {
      await client.revokeToken(revokeTarget.token_prefix.replace('...', ''));
      setRevokeTarget(null);
      refetch();
    } catch (e) {
      console.error('Failed to revoke token', e);
    } finally {
      setRevoking(false);
    }
  };

  const copyToken = (text: string) => {
    navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const tokens = data?.tokens || [];

  return (
    <PageLayout title="Token Management" onRefresh={refetch} isLoading={loading}>
      <div className="space-y-6">
        {/* Token List */}
        <Card>
          <div className="flex items-center justify-between mb-6">
            <h2 className="text-lg font-bold text-gray-900">Issued Tokens</h2>
            <Button onClick={() => setShowCreate(!showCreate)}>
              <Plus size={16} className="mr-2" /> New Token
            </Button>
          </div>

          {loading ? (
            <LoadingSpinner />
          ) : tokens.length === 0 ? (
            <EmptyState
              icon={<Key size={48} className="text-gray-200" />}
              title="No tokens"
              message="No tokens have been issued yet."
            />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left">
                <thead>
                  <tr className="border-b border-gray-100">
                    <th className="pb-3 text-sm font-semibold text-gray-600">Tenant</th>
                    <th className="pb-3 text-sm font-semibold text-gray-600">User</th>
                    <th className="pb-3 text-sm font-semibold text-gray-600">Role</th>
                    <th className="pb-3 text-sm font-semibold text-gray-600">Created</th>
                    <th className="pb-3 text-sm font-semibold text-gray-600">Token Prefix</th>
                    <th className="pb-3 text-sm font-semibold text-gray-600">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-50">
                  {tokens.map((t, i) => (
                    <tr key={i} className={`hover:bg-gray-50 ${t.role === 'admin' ? 'bg-indigo-50/50' : ''}`}>
                      <td className="py-3 text-sm text-gray-900">{t.tenant_id}</td>
                      <td className="py-3 text-sm text-gray-900">{t.user_id}</td>
                      <td className="py-3">
                        <Badge color={t.role === 'admin' ? 'indigo' : 'gray'}>{t.role}</Badge>
                      </td>
                      <td className="py-3 text-sm text-gray-500">
                        {t.created_at ? new Date(t.created_at).toLocaleDateString() : '-'}
                      </td>
                      <td className="py-3 text-xs font-mono text-gray-400">{t.token_prefix}</td>
                      <td className="py-3">
                        {t.role !== 'admin' && (
                          <Button variant="ghost" size="sm" className="text-red-500 hover:bg-red-50" onClick={() => setRevokeTarget(t)}>
                            <Trash2 size={14} />
                          </Button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        {/* Create Token Form */}
        {showCreate && (
          <Card>
            <h3 className="text-md font-bold text-gray-900 mb-4">Generate New Token</h3>
            <div className="flex gap-4 items-end">
              <div className="flex-1">
                <label className="block text-sm font-medium text-gray-700 mb-1">Tenant ID</label>
                <input
                  className="w-full px-3 py-2 border border-gray-200 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
                  value={tenantId}
                  onChange={(e) => setTenantId(e.target.value)}
                  placeholder="e.g. netops"
                />
              </div>
              <div className="flex-1">
                <label className="block text-sm font-medium text-gray-700 mb-1">User ID</label>
                <input
                  className="w-full px-3 py-2 border border-gray-200 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
                  value={userId}
                  onChange={(e) => setUserId(e.target.value)}
                  placeholder="e.g. john"
                />
              </div>
              <Button onClick={handleCreate} loading={creating} disabled={!tenantId.trim() || !userId.trim()}>
                Generate
              </Button>
            </div>
          </Card>
        )}

        {/* Created Token Display */}
        {createdToken && (
          <Card className="border-green-200 bg-green-50">
            <div className="flex items-start gap-4">
              <Check size={20} className="text-green-600 mt-1 shrink-0" />
              <div className="flex-1 min-w-0">
                <p className="text-sm font-bold text-green-800 mb-2">Token created — copy it now, it will not be shown again.</p>
                <div className="flex items-center gap-2 bg-white border border-green-200 rounded p-2">
                  <code className="text-xs text-gray-700 truncate flex-1">{createdToken}</code>
                  <button
                    onClick={() => copyToken(createdToken)}
                    className="p-1 hover:bg-green-100 rounded shrink-0"
                  >
                    {copied ? <Check size={16} className="text-green-600" /> : <Copy size={16} className="text-gray-400" />}
                  </button>
                </div>
              </div>
              <button onClick={() => setCreatedToken(null)} className="text-gray-400 hover:text-gray-600 text-sm">Dismiss</button>
            </div>
          </Card>
        )}
      </div>

      {/* Revoke Confirmation Modal */}
      <Modal
        isOpen={!!revokeTarget}
        onClose={() => setRevokeTarget(null)}
        title="Revoke Token"
        footer={
          <>
            <Button variant="ghost" onClick={() => setRevokeTarget(null)}>Cancel</Button>
            <Button variant="danger" onClick={handleRevoke} loading={revoking}>Revoke</Button>
          </>
        }
      >
        <p className="text-gray-600">
          Revoke token for <strong>{revokeTarget?.tenant_id}/{revokeTarget?.user_id}</strong>? This cannot be undone.
        </p>
      </Modal>
    </PageLayout>
  );
};
```

- [ ] **Step 2: Add Tokens route to App.tsx**

Add import: `import { Tokens } from './pages/Tokens';`

Add route: `<Route path="/tokens" element={<Tokens />} />`

- [ ] **Step 3: Add conditional nav item to Sidebar.tsx**

Import `Key` from lucide-react and `useApi`:

```typescript
import { Key } from 'lucide-react';
import { useApi } from '../../api/Context';
```

Add `adminOnly` to the nav item type and the Tokens entry. The `navItems` array needs an explicit type:

```typescript
interface NavItem {
  icon: React.FC<any>;
  label: string;
  path: string;
  status: string;
  adminOnly?: boolean;
}

const navItems: NavItem[] = [
  { icon: LayoutDashboard, label: 'Dashboard', path: '/', status: 'active' },
  { icon: Brain, label: 'Memories', path: '/memories', status: 'active' },
  { icon: BookOpen, label: 'Knowledge', path: '/knowledge', status: 'active' },
  { icon: SearchCode, label: 'Search Debug', path: '/search-debug', status: 'active' },
  { icon: Settings, label: 'System', path: '/system', status: 'active' },
  { icon: Key, label: 'Tokens', path: '/tokens', status: 'active', adminOnly: true },
  { icon: Sparkles, label: 'Skills', path: '/skills', status: 'coming-soon' },
];
```

Inside the component, get the role and filter:

```typescript
const { logout, role } = useApi();
const visibleItems = navItems.filter(item => !item.adminOnly || role === 'admin');
```

Use `visibleItems` instead of `navItems` in the JSX map.

- [ ] **Step 4: Run type check and build**

```bash
cd web && npx tsc --noEmit && npx vite build
```

- [ ] **Step 5: Commit**

```bash
git add web/src/pages/Tokens.tsx web/src/App.tsx web/src/components/layout/Sidebar.tsx
git commit -m "feat(web): add Token Management page with admin-only nav"
```

---

### Task 10: Frontend — admin memory filter on Memories page

**Files:**
- Modify: `web/src/pages/Memories.tsx`

- [ ] **Step 1: Add admin filter dropdowns**

Import `useApi` for role and `AdminMemoryRecord` type. Add state for admin filters:

```typescript
const { client, role } = useApi();
const [adminFilters, setAdminFilters] = useState({ tenant_id: '', user_id: '' });
const [users, setUsers] = useState<{ tenant_id: string; user_id: string }[]>([]);
```

On mount (admin only), fetch user list from tokens:

```typescript
useEffect(() => {
  if (role === 'admin' && client) {
    client.listTokens().then(res => {
      setUsers(res.tokens
        .filter(t => t.role !== 'admin')
        .map(t => ({ tenant_id: t.tenant_id, user_id: t.user_id }))
      );
    }).catch(() => {});
  }
}, [role, client]);
```

- [ ] **Step 2: Update fetchMemories for admin mode**

In `fetchMemories`, when `role === 'admin'` and not in search mode, use `client.listAllMemories(...)` instead of `client.listMemories(...)`:

```typescript
if (role === 'admin' && !query) {
  const res = await client.listAllMemories({
    tenant_id: adminFilters.tenant_id || undefined,
    user_id: adminFilters.user_id || undefined,
    limit: 20,
    offset: currentOffset,
    context_type: filters.context_type || undefined,
    category: filters.category || undefined,
  });
  results = res.results;
  totalCount = res.total;
  setIsSearchMode(false);
}
```

Add `adminFilters` and `role` to the `useCallback` and `useEffect` dependencies.

- [ ] **Step 3: Render admin filter dropdowns**

Before the existing filter row, add (only when `role === 'admin'`):

```tsx
{role === 'admin' && (
  <div className="flex gap-2">
    <select
      className="flex-1 text-sm border border-indigo-200 rounded-md p-2 bg-indigo-50 outline-none focus:ring-2 focus:ring-indigo-500"
      value={adminFilters.tenant_id}
      onChange={(e) => setAdminFilters(f => ({ ...f, tenant_id: e.target.value }))}
    >
      <option value="">All Tenants</option>
      {[...new Set(users.map(u => u.tenant_id))].map(tid => (
        <option key={tid} value={tid}>{tid}</option>
      ))}
    </select>
    <select
      className="flex-1 text-sm border border-indigo-200 rounded-md p-2 bg-indigo-50 outline-none focus:ring-2 focus:ring-indigo-500"
      value={adminFilters.user_id}
      onChange={(e) => setAdminFilters(f => ({ ...f, user_id: e.target.value }))}
    >
      <option value="">All Users</option>
      {users
        .filter(u => !adminFilters.tenant_id || u.tenant_id === adminFilters.tenant_id)
        .map(u => <option key={u.user_id} value={u.user_id}>{u.user_id}</option>)
      }
    </select>
  </div>
)}
```

- [ ] **Step 4: Show tenant/user badge on memory cards in admin mode**

In the memory card rendering, add after the existing badges:

```tsx
{'source_tenant_id' in memory && role === 'admin' && (
  <Badge color="green">{(memory as any).source_tenant_id}/{(memory as any).source_user_id}</Badge>
)}
```

- [ ] **Step 5: Run type check and build**

```bash
cd web && npx tsc --noEmit && npx vite build
```

- [ ] **Step 6: Commit**

```bash
git add web/src/pages/Memories.tsx
git commit -m "feat(web): add admin user filter dropdowns on Memories page"
```

---

### Task 11: End-to-end verification

- [ ] **Step 1: Start backend**

```bash
uv run opencortex-server
# Copy admin token from log output
```

- [ ] **Step 2: Start frontend**

```bash
cd web && npm run dev
```

- [ ] **Step 3: Test admin flow**

1. Open browser to `http://localhost:5174?token=<admin_token>`
2. Verify Sidebar shows: Dashboard, Memories, Knowledge, Search Debug, System, **Tokens**, Skills
3. Go to Tokens page — verify admin token is listed, no revoke button
4. Create a new user token — verify JWT is shown, copy it
5. Go to Memories page — verify tenant/user filter dropdowns appear
6. Select a tenant — verify memories load from that tenant

- [ ] **Step 4: Test user flow**

1. Open incognito browser to `http://localhost:5174?token=<user_token>`
2. Verify Sidebar does NOT show Tokens
3. Verify Memories page does NOT show tenant/user filter dropdowns
4. Navigate to `/tokens` directly — verify "Access Denied" message

- [ ] **Step 5: Test permission isolation**

```bash
USER_TOKEN="<from step 3>"
curl -s -w "%{http_code}" -H "Authorization: Bearer $USER_TOKEN" http://localhost:8921/api/v1/admin/tokens
# Expected: 403

curl -s -H "Authorization: Bearer $USER_TOKEN" http://localhost:8921/api/v1/auth/me
# Expected: {"tenant_id":"test","user_id":"u1","role":"user"}
```
