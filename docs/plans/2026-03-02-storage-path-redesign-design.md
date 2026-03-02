# Storage Path Redesign — Design Document

> **Date:** 2026-03-02
> **Status:** Approved
> **Goal:** Redesign CortexFS storage path specification to support user/agent memory separation, session-level staging, ACE skill sharing, and backend-controlled URI routing.

---

## 1. URI Path Specification

```
opencortex://{tenant_id}/
│
├── user/{uid}/
│   ├── memories/                          # 长期用户记忆
│   │   ├── profile/{node_id}              # 身份/属性, 可合并
│   │   ├── preferences/{node_id}          # 偏好设置, 可合并
│   │   ├── entities/{node_id}             # 实体(人/项目), 可合并
│   │   └── events/{node_id}               # 事件/决策, 不可合并
│   └── staging/                           # 会话临时记忆
│       └── {session_id}/{node_id}         # 会话结束后 LLM 分流
│
├── shared/                                # 项目级共享知识
│   ├── cases/{node_id}                    # 问题+解决方案, 不可合并
│   ├── patterns/{node_id}                 # 可复用模式, 可合并
│   └── skills/                            # ACE 自动提取 Skillbook
│       ├── error_fixes/{skill_id}         # 错误修复技能
│       ├── workflows/{skill_id}           # 工作流技能
│       └── strategies/{skill_id}          # 策略技能
│
└── resources/                             # 项目外部资源
    ├── documents/{node_id}                # 文档
    └── plans/{node_id}                    # 方案
```

### Key Rules

| Rule | Description |
|------|-------------|
| Backend-generated URIs | Client never passes URI. `_auto_uri()` builds from `(tenant_id, user_id, context_type, category)` |
| Source metadata on shared | All `shared/` records carry `source_user_id` and `source_tenant_id` in Qdrant payload |
| ACE extraction routing | RuleExtractor preferences → `user/{uid}/memories/preferences/`; error_fixes/workflows/strategies → `shared/skills/` |
| Staging lifecycle | Written during session to `staging/{session_id}/`; LLM decides promotion at session end; remainder cleaned |
| node_id format | 12-char UUID hex (`uuid4().hex[:12]`) |

---

## 2. Memory Categories & Lifecycle

### 2.1 Category Table

| Category | Scope | Belongs to | Mergeable | Storage Path | Description |
|----------|-------|-----------|-----------|--------------|-------------|
| profile | private | user | ✅ | `user/{uid}/memories/profile/` | 用户身份、角色、背景 |
| preferences | private | user | ✅ | `user/{uid}/memories/preferences/` | 偏好设置、习惯 |
| entities | private | user | ✅ | `user/{uid}/memories/entities/` | 人名、项目名、路径、URL |
| events | private | user | ❌ | `user/{uid}/memories/events/` | 决策、事件，每条独立不可覆盖 |
| cases | shared | agent | ❌ | `shared/cases/` | 问题+解决方案，完整案例 |
| patterns | shared | agent | ✅ | `shared/patterns/` | 可复用模式、最佳实践 |

### 2.2 Merge Semantics

**Mergeable** — Same category, if new memory has semantic similarity > threshold (0.85) with existing, update existing instead of creating new.

**Non-mergeable** — Each memory is independent, even if semantically similar. Preserves distinct context and solutions.

### 2.3 Session Staging Flow

```
                     会话中                          会话结束
                 ┌──────────┐                   ┌──────────────┐
  用户交互 ──→  │  staging/ │  ── LLM 分流 ──→  │  memories/   │  (永久)
                 │  {sid}/   │                   │  profile/    │
                 │  {node}   │                   │  preferences/│
                 └──────────┘                   │  entities/   │
                       │                         │  events/     │
                       │                         └──────────────┘
                       │                         ┌──────────────┐
                       └──── ACE 提取 ────────→  │  shared/     │  (永久)
                       │                         │  skills/     │
                       │                         │  cases/      │
                       │                         │  patterns/   │
                       │                         └──────────────┘
                       │
                       └──── 低置信度 ──→ 丢弃 (不持久化)
```

### 2.4 ACE Skill Extraction Routing

| RuleExtractor section | Target | Notes |
|----------------------|--------|-------|
| error_fixes | `shared/skills/error_fixes/` | 项目共享 |
| workflows | `shared/skills/workflows/` | 项目共享 |
| preferences | `user/{uid}/memories/preferences/` | 用户私有，不进 shared |

Shared records carry: `source_user_id`, `source_tenant_id`, `scope="shared"`.

---

## 3. URI Construction & API Routing

### 3.1 `_auto_uri()` Routing Table

| context_type | category | Generated URI | scope |
|-------------|----------|--------------|-------|
| `memory` | `profile` | `opencortex://{tid}/user/{uid}/memories/profile/{nid}` | private |
| `memory` | `preferences` | `opencortex://{tid}/user/{uid}/memories/preferences/{nid}` | private |
| `memory` | `entities` | `opencortex://{tid}/user/{uid}/memories/entities/{nid}` | private |
| `memory` | `events` | `opencortex://{tid}/user/{uid}/memories/events/{nid}` | private |
| `memory` | (other/empty) | `opencortex://{tid}/user/{uid}/memories/events/{nid}` | private |
| `case` | * | `opencortex://{tid}/shared/cases/{nid}` | shared |
| `pattern` | * | `opencortex://{tid}/shared/patterns/{nid}` | shared |
| `skill` | `error_fixes` | `opencortex://{tid}/shared/skills/error_fixes/{nid}` | shared |
| `skill` | `workflows` | `opencortex://{tid}/shared/skills/workflows/{nid}` | shared |
| `skill` | `strategies` | `opencortex://{tid}/shared/skills/strategies/{nid}` | shared |
| `skill` | (other/empty) | `opencortex://{tid}/shared/skills/general/{nid}` | shared |
| `resource` | `documents` | `opencortex://{tid}/resources/documents/{nid}` | shared |
| `resource` | `plans` | `opencortex://{tid}/resources/plans/{nid}` | shared |
| `resource` | (other/empty) | `opencortex://{tid}/resources/{category}/{nid}` | shared |
| `staging` | * | `opencortex://{tid}/user/{uid}/staging/{sid}/{nid}` | private |

**Fallback:** Unknown category for `memory` defaults to `events` (non-mergeable, safest). Unknown `skill` defaults to `general`.

### 3.2 ContextType Enum Extension

```python
class ContextType(str, Enum):
    MEMORY   = "memory"      # User memories (profile/preferences/entities/events)
    RESOURCE = "resource"    # Project resources (documents/plans)
    SKILL    = "skill"       # ACE-extracted skills (error_fixes/workflows/strategies)
    CASE     = "case"        # Project cases (problem+solution)
    PATTERN  = "pattern"     # Project patterns (reusable patterns)
    STAGING  = "staging"     # Session temporary memories
```

### 3.3 MCP Tool Parameter Changes

`memory_store` context_type expands to: `memory | resource | skill | case | pattern | staging`

`memory_search` adds `category` filter: `profile | preferences | entities | events | error_fixes | workflows | strategies | ...`

### 3.4 Scope Inference

```python
def _infer_scope(uri: str) -> str:
    if "/user/" in uri:
        return "private"
    elif "/shared/" in uri or "/resources/" in uri:
        return "shared"
    return "shared"
```

### 3.5 Skillbook `_resolve_prefix` Change

```python
# Old: Skills stored under user path
def _resolve_prefix(self, tenant_id, user_id):
    return f"opencortex://{tenant_id}/user/{user_id}/skillbooks"

# New: Skills go to project-shared path
def _resolve_prefix(self, tenant_id, user_id):
    return f"opencortex://{tenant_id}/shared/skills"
```

ACE-extracted preferences route to `user/{uid}/memories/preferences/` instead of `shared/skills/`.

---

## 4. Session Lifecycle & Staging Management

### 4.1 Session State Machine

```
    session_begin          session_message (N次)         session_end / TTL
  ──────────────→  ACTIVE  ──────────────────→  ACTIVE  ──────────────→  CLOSED
                     │                                        │
                     │  创建 staging/{sid}/                    │  LLM 分流 or 清理
                     ▼                                        ▼
              staging/{sid}/                          memories/{category}/
              临时写入                                shared/skills/
                                                      或丢弃
```

### 4.2 Normal Flow: Hook-Driven

```
1. session_begin(session_id)
   → Create staging/{sid}/ directory
   → Qdrant record: session_id + created_at + ttl_expires_at

2. session_message(session_id, role, content)
   → Write to staging/{sid}/{node_id}
   → Buffer in memory only, excluded from global search

3. Claude Code stop hook → session_end(session_id)
   → MemoryExtractor LLM analyzes conversation
   → Route by category:
     - profile/preferences/entities/events → user/{uid}/memories/{cat}/
     - error_fixes/workflows → shared/skills/{section}/
     - Low confidence → discard
   → Clean staging/{sid}/ (Qdrant + CortexFS)
```

### 4.3 Fallback: TTL Cleanup

```
Periodic task (piggyback on apply_decay):
  → Scan: WHERE context_type = "staging" AND ttl_expires_at < now()
  → Delete directly (Qdrant + CortexFS)
  → No LLM extraction (data may be incomplete)
  → Log: "Cleaned orphan staging session {sid}"
```

Default TTL: 24 hours from session_begin.

### 4.4 Staging Isolation Rules

| Rule | Description |
|------|-------------|
| No global search | `memory_search` excludes `context_type=staging` by default |
| No decay | Staging records not affected by `apply_decay` |
| No feedback | Staging records do not accept reward feedback |
| Session-internal read | `session_search` can retrieve current session's staging memories as context |

### 4.5 Merge Behavior During Promotion

```python
MERGEABLE_CATEGORIES = {"profile", "preferences", "entities", "patterns"}

for memory in extracted_memories:
    if memory.category in MERGEABLE_CATEGORIES:
        existing = await search(memory.abstract, category=memory.category)
        if existing and existing[0].score > DEDUP_THRESHOLD:
            await update(existing[0].uri, merged_content)
        else:
            await add(memory)
    else:
        # Non-mergeable (events, cases): always create new
        await add(memory)
```

---

## 5. Qdrant Storage Model Changes

### 5.1 Context Collection Field Changes

| Field | Change | Type | Indexed | Description |
|-------|--------|------|---------|-------------|
| `context_type` | **Expand values** | string | ✅ | Add `"case"`, `"pattern"`, `"staging"` |
| `category` | **New** | string | ✅ | `profile/preferences/entities/events/error_fixes/workflows/...` |
| `scope` | **New** | string | ✅ | `"private"` or `"shared"` |
| `session_id` | **New** | string | ✅ | Associated session for staging records |
| `source_user_id` | **New** | string | ✅ | Source user for shared records |
| `mergeable` | **New** | bool | ✅ | Whether category supports merging |
| `ttl_expires_at` | **New** | string | ✅ | Expiry time for staging records (ISO 8601) |

All existing fields (`uri`, `abstract`, `overview`, `vector`, `reward_score`, `accessed_at`, `active_count`, `protected`, etc.) remain unchanged.

### 5.2 Skillbook Collection Field Changes

| Field | Change | Description |
|-------|--------|-------------|
| `uri` | **Path migration** | `opencortex://{tid}/user/{uid}/skillbooks/...` → `opencortex://{tid}/shared/skills/...` |
| `source_user_id` | **New** | Source attribution (replaces owner concept under shared) |
| `source_tenant_id` | **New** | Source project (reserved for cross-project sharing) |
| `scope` | **Fixed** | Skillbook records always `"shared"` |
| `owner_user_id` | **Keep** | Backward compatibility, redundant write with `source_user_id` |

### 5.3 Search Filter Changes

```python
def _build_search_filter(tenant_id, user_id, context_type=None, category=None):
    """Tenant-isolated + scope-aware search filter.

    Default: return user's private memories + project-level shared content.
    Exclude: staging records never appear in global search.
    """
    conds = [
        {"op": "must", "field": "tenant_id", "conds": [tenant_id]},
        {"op": "must_not", "field": "context_type", "conds": ["staging"]},
        {"op": "or", "conds": [
            {"op": "must", "field": "scope", "conds": ["shared"]},
            {"op": "and", "conds": [
                {"op": "must", "field": "scope", "conds": ["private"]},
                {"op": "must", "field": "source_user_id", "conds": [user_id]},
            ]},
        ]},
    ]
    if context_type:
        conds.append({"op": "must", "field": "context_type", "conds": [context_type]})
    if category:
        conds.append({"op": "must", "field": "category", "conds": [category]})
    return {"op": "and", "conds": conds}
```

### 5.4 Index Strategy

New ScalarIndex fields in `collection_schemas.py`:

```python
"category", "scope", "session_id", "source_user_id", "mergeable", "ttl_expires_at"
```

---

## 6. Data Migration & Backward Compatibility

### 6.1 Existing Data Inventory

```
Current .cortex/ structure:
├── agents/content.md                          ← Root-level junk (bug)
├── coder-frontend/content.md                  ← Root-level junk
├── ...12 root-level directories...            ← All need cleanup
├── default/user/default/skillbooks/           ← Old Skillbook path
│   ├── error_fixes/ (6 skills)
│   ├── preferences/ (7 skills)
│   └── workflows/ (10 skills)
├── netops/resources/documents/ (9 docs)       ← Compliant, keep
├── netops/resources/plans/ (4 plans)          ← Compliant, keep
└── netops/user/liaowh4/memories/preferences/  ← Compliant, keep
```

### 6.2 Migration Mapping

| Old Path | New Path | Action |
|----------|----------|--------|
| Root-level dirs (agents/, coder-*/, etc.) | — | **Delete** (bug-generated junk) |
| `default/user/default/skillbooks/error_fixes/*` | `default/shared/skills/error_fixes/*` | **Move** + update Qdrant URI |
| `default/user/default/skillbooks/workflows/*` | `default/shared/skills/workflows/*` | **Move** + update Qdrant URI |
| `default/user/default/skillbooks/preferences/*` | `default/user/default/memories/preferences/*` | **Move** + update Qdrant URI |
| `netops/resources/**` | `netops/resources/**` | **Keep** (already compliant) |
| `netops/user/liaowh4/memories/**` | `netops/user/liaowh4/memories/**` | **Keep** (already compliant) |

### 6.3 Qdrant Record Migration

```python
async def migrate_record(storage, old_uri, new_uri, updates):
    """Migrate single record: update URI + backfill new fields. Idempotent."""
    records = await storage.filter(
        COLLECTION, {"op": "must", "field": "uri", "conds": [old_uri]}, limit=1
    )
    if not records:
        return
    record_id = records[0]["id"]
    await storage.update(COLLECTION, record_id, {
        "uri": new_uri,
        "scope": updates.get("scope", "shared"),
        "category": updates.get("category", ""),
        "source_user_id": updates.get("source_user_id", "default"),
        "source_tenant_id": updates.get("source_tenant_id", "default"),
        "mergeable": updates.get("mergeable", False),
    })
```

### 6.4 Migration Execution Strategy

One-time script, run at server startup:

```python
async def run_migration(storage, cortex_fs):
    """v0.2.x → v0.3.0 storage path migration. Idempotent."""
    # Step 1: Clean root-level junk (CortexFS + Qdrant)
    root_junk = ["agents", "coder-frontend", "coder-go", "coder-python",
                 "coder-rust", "coding-style", "git-workflow", "hooks",
                 "patterns", "performance", "security", "testing"]
    for name in root_junk:
        await cleanup_uri(storage, cortex_fs, f"opencortex://{name}")

    # Step 2: Migrate skillbooks → shared/skills or user/memories
    await migrate_skillbooks(storage, cortex_fs)

    # Step 3: Backfill new fields on existing compliant records
    await backfill_new_fields(storage)
```

### 6.5 Field Backfill Rules

| Field | Backfill Rule |
|-------|--------------|
| `scope` | URI contains `/user/` → `"private"`, otherwise → `"shared"` |
| `category` | Parse from URI: `/memories/preferences/` → `"preferences"` |
| `source_user_id` | Parse `user/{uid}` from URI; shared records set to `""` |
| `mergeable` | Lookup by category: profile/preferences/entities/patterns → `true`, others → `false` |
| `session_id` | Empty (non-staging) |
| `ttl_expires_at` | Empty (non-staging) |

### 6.6 Backward Compatibility

| Measure | Description |
|---------|-------------|
| Dual URI read | Search matches both old `skillbooks/` and new `shared/skills/` prefix for 1 version |
| Idempotent migration | Script can be re-run safely, already-migrated records skipped |
| Legacy field retention | Skillbook `owner_user_id` kept, redundant with `source_user_id` |
| Version marker | Qdrant collection gets `schema_version` metadata: `"0.3.0"` |

---

## References

- [OpenViking URI Design](https://github.com/volcengine/OpenViking/blob/main/docs/en/concepts/04-viking-uri.md)
- [OpenViking Session Concepts](https://github.com/volcengine/OpenViking/blob/main/docs/en/concepts/08-session.md)
- [ACE Claude Code Integration](https://github.com/kayba-ai/agentic-context-engine/tree/main/ace/integrations/claude_code)
- [MemOS Memory Overview](https://memos-docs.openmem.net/cn/open_source/modules/memories/overview)
