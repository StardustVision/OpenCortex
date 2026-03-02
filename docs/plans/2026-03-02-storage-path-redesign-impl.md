# Storage Path Redesign Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Redesign CortexFS storage paths to support user/agent memory separation, session staging, ACE skill sharing, and backend-controlled URI routing.

**Architecture:** Extend ContextType enum with 3 new types (case, pattern, staging). Rewrite `_auto_uri()` routing table. Move Skillbook prefix from `user/{uid}/skillbooks` to `shared/skills`. Add staging lifecycle with TTL cleanup. Add scope-aware search filters. Migrate existing data.

**Tech Stack:** Python 3.10+, Qdrant, asyncio, unittest

---

### Task 1: Extend ContextType Enum & Collection Schema Fields

**Files:**
- Modify: `src/opencortex/retrieve/types.py:15-20`
- Modify: `src/opencortex/storage/collection_schemas.py:40-77,98-120`
- Test: `tests/test_e2e_phase1.py`

**Step 1: Write the failing test**

Add to `tests/test_e2e_phase1.py` (after imports):

```python
class TestContextTypeEnum(unittest.TestCase):
    def test_new_context_types_exist(self):
        from opencortex.retrieve.types import ContextType
        self.assertEqual(ContextType.CASE.value, "case")
        self.assertEqual(ContextType.PATTERN.value, "pattern")
        self.assertEqual(ContextType.STAGING.value, "staging")

    def test_legacy_context_types_unchanged(self):
        from opencortex.retrieve.types import ContextType
        self.assertEqual(ContextType.MEMORY.value, "memory")
        self.assertEqual(ContextType.RESOURCE.value, "resource")
        self.assertEqual(ContextType.SKILL.value, "skill")
```

**Step 2: Run test to verify it fails**

Run: `uv run python3 -m unittest tests.test_e2e_phase1.TestContextTypeEnum -v`
Expected: FAIL — `ContextType has no member 'CASE'`

**Step 3: Implement ContextType extension**

In `src/opencortex/retrieve/types.py`, replace lines 15-20:

```python
class ContextType(str, Enum):
    """Context type for retrieval."""

    MEMORY = "memory"
    RESOURCE = "resource"
    SKILL = "skill"
    CASE = "case"
    PATTERN = "pattern"
    STAGING = "staging"
```

**Step 4: Add new fields to collection schemas**

In `src/opencortex/storage/collection_schemas.py`, add these fields to the context collection `Fields` list (after the `"protected"` field, before the closing `]`):

```python
    {"FieldName": "category", "FieldType": "string"},
    {"FieldName": "scope", "FieldType": "string"},
    {"FieldName": "session_id", "FieldType": "string"},
    {"FieldName": "source_user_id", "FieldType": "string"},
    {"FieldName": "mergeable", "FieldType": "bool"},
    {"FieldName": "ttl_expires_at", "FieldType": "string"},
```

Add to context collection `ScalarIndex` list:

```python
    "category",
    "scope",
    "session_id",
    "source_user_id",
    "mergeable",
    "ttl_expires_at",
```

Add to skillbook collection `Fields` list (after `"share_reason"` field):

```python
    {"FieldName": "source_user_id", "FieldType": "string"},
    {"FieldName": "source_tenant_id", "FieldType": "string"},
```

**Step 5: Run tests to verify they pass**

Run: `uv run python3 -m unittest tests.test_e2e_phase1.TestContextTypeEnum -v`
Expected: PASS

**Step 6: Commit**

```bash
git add src/opencortex/retrieve/types.py src/opencortex/storage/collection_schemas.py tests/test_e2e_phase1.py
git commit -m "feat: extend ContextType enum and collection schema fields for path redesign"
```

---

### Task 2: Update URI Sub-Scopes and Rewrite `_auto_uri()`

**Files:**
- Modify: `src/opencortex/utils/uri.py:53-62`
- Modify: `src/opencortex/orchestrator.py:1669-1707`
- Test: `tests/test_e2e_phase1.py`

**Step 1: Write the failing test**

Add to `tests/test_e2e_phase1.py`:

```python
class TestAutoUri(unittest.TestCase):
    """Test _auto_uri routing table."""

    def setUp(self):
        from opencortex.http.request_context import set_request_identity
        self._token = set_request_identity("testteam", "alice")

    def tearDown(self):
        from opencortex.http.request_context import reset_request_identity
        reset_request_identity(self._token)

    def _auto_uri(self, context_type, category):
        from opencortex.orchestrator import MemoryOrchestrator
        o = MemoryOrchestrator.__new__(MemoryOrchestrator)
        return o._auto_uri(context_type, category)

    def test_memory_profile(self):
        uri = self._auto_uri("memory", "profile")
        self.assertIn("/user/alice/memories/profile/", uri)
        self.assertTrue(uri.startswith("opencortex://testteam/"))

    def test_memory_preferences(self):
        uri = self._auto_uri("memory", "preferences")
        self.assertIn("/user/alice/memories/preferences/", uri)

    def test_memory_empty_category_defaults_to_events(self):
        uri = self._auto_uri("memory", "")
        self.assertIn("/user/alice/memories/events/", uri)

    def test_case(self):
        uri = self._auto_uri("case", "anything")
        self.assertIn("/shared/cases/", uri)
        self.assertNotIn("/user/", uri)

    def test_pattern(self):
        uri = self._auto_uri("pattern", "")
        self.assertIn("/shared/patterns/", uri)

    def test_skill_error_fixes(self):
        uri = self._auto_uri("skill", "error_fixes")
        self.assertIn("/shared/skills/error_fixes/", uri)

    def test_skill_empty_defaults_to_general(self):
        uri = self._auto_uri("skill", "")
        self.assertIn("/shared/skills/general/", uri)

    def test_resource_documents(self):
        uri = self._auto_uri("resource", "documents")
        self.assertIn("/resources/documents/", uri)

    def test_staging(self):
        uri = self._auto_uri("staging", "")
        self.assertIn("/user/alice/staging/", uri)
```

**Step 2: Run test to verify it fails**

Run: `uv run python3 -m unittest tests.test_e2e_phase1.TestAutoUri -v`
Expected: FAIL — routing doesn't match new structure

**Step 3: Update URI sub-scopes**

In `src/opencortex/utils/uri.py`, replace lines 55-61:

```python
    # Sub-scopes that exist directly under {team_id}/
    SHARED_SUB_SCOPES = {"resources", "shared", "agent", "queue", "temp"}

    # Sub-scopes that exist under {team_id}/user/{user_id}/
    PRIVATE_SUB_SCOPES = {"memories", "staging", "reinforcement", "feedback", "workspace", "session"}

    # All recognized sub-scopes
    ALL_SUB_SCOPES = SHARED_SUB_SCOPES | PRIVATE_SUB_SCOPES | {"user"}
```

**Step 4: Rewrite `_auto_uri()`**

In `src/opencortex/orchestrator.py`, replace the `_auto_uri` method (lines 1669-1690):

```python
    # Valid user memory categories
    _USER_MEMORY_CATEGORIES = {"profile", "preferences", "entities", "events"}

    def _auto_uri(self, context_type: str, category: str) -> str:
        """Generate a URI based on context type and category.

        Routing table:
          memory  + category  → user/{uid}/memories/{category}/{nid}
          memory  + (empty)   → user/{uid}/memories/events/{nid}
          case    + *         → shared/cases/{nid}
          pattern + *         → shared/patterns/{nid}
          skill   + section   → shared/skills/{section}/{nid}
          skill   + (empty)   → shared/skills/general/{nid}
          resource+ category  → resources/{category}/{nid}
          staging + *         → user/{uid}/staging/{nid}
        """
        tid, uid = get_effective_identity()
        node_id = uuid4().hex[:12]

        if context_type == "memory":
            cat = category if category in self._USER_MEMORY_CATEGORIES else "events"
            return CortexURI.build_private(tid, uid, "memories", cat, node_id)

        elif context_type == "case":
            return CortexURI.build_shared(tid, "shared", "cases", node_id)

        elif context_type == "pattern":
            return CortexURI.build_shared(tid, "shared", "patterns", node_id)

        elif context_type == "skill":
            section = category or "general"
            return CortexURI.build_shared(tid, "shared", "skills", section, node_id)

        elif context_type == "resource":
            if category:
                return CortexURI.build_shared(tid, "resources", category, node_id)
            return CortexURI.build_shared(tid, "resources", node_id)

        elif context_type == "staging":
            return CortexURI.build_private(tid, uid, "staging", node_id)

        # Fallback: treat as user memory event
        return CortexURI.build_private(tid, uid, "memories", "events", node_id)
```

**Step 5: Update `_infer_context_type()`**

Replace the `_infer_context_type` method (lines 1701-1707):

```python
    def _infer_context_type(self, uri: str) -> ContextType:
        """Infer ContextType from URI path segments."""
        if "/staging/" in uri:
            return ContextType.STAGING
        elif "/memories/" in uri:
            return ContextType.MEMORY
        elif "/shared/cases/" in uri:
            return ContextType.CASE
        elif "/shared/patterns/" in uri:
            return ContextType.PATTERN
        elif "/skills/" in uri:
            return ContextType.SKILL
        return ContextType.RESOURCE
```

**Step 6: Run tests**

Run: `uv run python3 -m unittest tests.test_e2e_phase1.TestAutoUri -v`
Expected: PASS

**Step 7: Commit**

```bash
git add src/opencortex/utils/uri.py src/opencortex/orchestrator.py tests/test_e2e_phase1.py
git commit -m "feat: rewrite _auto_uri routing table and URI sub-scopes for new path design"
```

---

### Task 3: Add Scope & Category Fields to `orchestrator.add()`

**Files:**
- Modify: `src/opencortex/orchestrator.py:507-565`
- Modify: `src/opencortex/core/context.py` (if Context dataclass needs fields)
- Test: `tests/test_e2e_phase1.py`

**Step 1: Write the failing test**

Add to `tests/test_e2e_phase1.py`:

```python
class TestAddScopeFields(unittest.TestCase):
    """Test that add() populates scope/category/source fields in Qdrant."""

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self):
        self.loop.close()

    def test_add_memory_sets_private_scope(self):
        from opencortex.http.request_context import set_request_identity, reset_request_identity
        token = set_request_identity("t1", "u1")
        try:
            storage = InMemoryStorage()
            orch = self._build_orch(storage)
            ctx = self.loop.run_until_complete(
                orch.add(abstract="test pref", category="preferences", context_type="memory")
            )
            # Check Qdrant record has scope fields
            records = self.loop.run_until_complete(
                storage.filter("opencortex_contexts",
                    {"op": "must", "field": "uri", "conds": [ctx.uri]}, limit=1)
            )
            self.assertEqual(records[0].get("scope"), "private")
            self.assertEqual(records[0].get("category"), "preferences")
            self.assertEqual(records[0].get("source_user_id"), "u1")
            self.assertTrue(records[0].get("mergeable"))
        finally:
            reset_request_identity(token)

    def test_add_case_sets_shared_scope(self):
        from opencortex.http.request_context import set_request_identity, reset_request_identity
        token = set_request_identity("t1", "u1")
        try:
            storage = InMemoryStorage()
            orch = self._build_orch(storage)
            ctx = self.loop.run_until_complete(
                orch.add(abstract="bug fix", context_type="case")
            )
            records = self.loop.run_until_complete(
                storage.filter("opencortex_contexts",
                    {"op": "must", "field": "uri", "conds": [ctx.uri]}, limit=1)
            )
            self.assertEqual(records[0].get("scope"), "shared")
            self.assertFalse(records[0].get("mergeable"))
        finally:
            reset_request_identity(token)

    def _build_orch(self, storage):
        """Create a minimal orchestrator with the given storage."""
        from opencortex.orchestrator import MemoryOrchestrator
        from opencortex.config import CortexConfig
        cfg = CortexConfig(embedding_provider="none")
        orch = MemoryOrchestrator(config=cfg)
        orch._storage = storage
        orch._embedder = MockEmbedder()
        orch._initialized = True
        # Initialize CortexFS for write_context
        from opencortex.storage.cortex_fs import CortexFS
        import tempfile
        orch._fs = CortexFS(data_root=tempfile.mkdtemp())
        return orch
```

**Step 2: Run test to verify it fails**

Run: `uv run python3 -m unittest tests.test_e2e_phase1.TestAddScopeFields -v`
Expected: FAIL — `scope` field missing from Qdrant record

**Step 3: Implement scope/category fields in `add()`**

In `src/opencortex/orchestrator.py`, add a helper and modify the `add()` method to populate new fields. After `record["vector"] = ctx.vector` (around line 555), add:

```python
        # Populate new path-redesign fields
        _MERGEABLE_CATEGORIES = {"profile", "preferences", "entities", "patterns"}
        inferred_scope = "private" if "/user/" in uri else "shared"
        effective_category = category or self._extract_category_from_uri(uri)
        record["scope"] = inferred_scope
        record["category"] = effective_category
        record["source_user_id"] = uid
        record["mergeable"] = effective_category in _MERGEABLE_CATEGORIES
        record["session_id"] = session_id or ""
        record["ttl_expires_at"] = ""
```

Add the helper method:

```python
    @staticmethod
    def _extract_category_from_uri(uri: str) -> str:
        """Extract category from URI path. E.g. /memories/preferences/abc → preferences."""
        parts = uri.split("/")
        # Look for known parent segments, return next part
        for parent in ("memories", "cases", "patterns", "skills", "staging", "resources"):
            if parent in parts:
                idx = parts.index(parent)
                if idx + 1 < len(parts) and len(parts[idx + 1]) > 12:
                    return parts[idx + 1]
                elif parent in ("cases", "patterns"):
                    return parent
        return ""
```

**Step 4: Run tests**

Run: `uv run python3 -m unittest tests.test_e2e_phase1.TestAddScopeFields -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/opencortex/orchestrator.py tests/test_e2e_phase1.py
git commit -m "feat: populate scope/category/source fields in orchestrator.add()"
```

---

### Task 4: Migrate Skillbook Prefix to `shared/skills/`

**Files:**
- Modify: `src/opencortex/ace/skillbook.py:695-699`
- Modify: `src/opencortex/ace/engine.py:38`
- Test: `tests/test_ace_phase1.py`

**Step 1: Write the failing test**

Add to `tests/test_ace_phase1.py`:

```python
class TestSkillbookPrefix(unittest.TestCase):
    def test_resolve_prefix_uses_shared_skills(self):
        """Skillbook prefix should be shared/skills, not user/skillbooks."""
        from opencortex.ace.skillbook import Skillbook
        sb = Skillbook.__new__(Skillbook)
        sb._prefix = "opencortex://myteam/shared/skills"
        result = sb._resolve_prefix("myteam", "alice")
        self.assertEqual(result, "opencortex://myteam/shared/skills")
        self.assertNotIn("skillbooks", result)
        self.assertNotIn("/user/", result)
```

**Step 2: Run test to verify it fails**

Run: `uv run python3 -m unittest tests.test_ace_phase1.TestSkillbookPrefix -v`
Expected: FAIL — returns `opencortex://myteam/user/alice/skillbooks`

**Step 3: Update `_resolve_prefix()`**

In `src/opencortex/ace/skillbook.py`, replace lines 695-699:

```python
    def _resolve_prefix(self, tenant_id: str = "", user_id: str = "") -> str:
        """Resolve the URI prefix for shared skills storage."""
        if tenant_id:
            return f"opencortex://{tenant_id}/shared/skills"
        return self._prefix or "opencortex://default/shared/skills"
```

**Step 4: Update ACEngine `__init__` prefix**

In `src/opencortex/ace/engine.py`, replace line 38:

```python
        prefix = f"opencortex://{tenant_id}/shared/skills"
```

**Step 5: Run tests**

Run: `uv run python3 -m unittest tests.test_ace_phase1.TestSkillbookPrefix tests.test_ace_phase1 tests.test_ace_phase2 -v`
Expected: PASS (all ACE tests still pass)

**Step 6: Commit**

```bash
git add src/opencortex/ace/skillbook.py src/opencortex/ace/engine.py tests/test_ace_phase1.py
git commit -m "feat: migrate skillbook prefix from user/skillbooks to shared/skills"
```

---

### Task 5: Route ACE Preferences to User Memories

**Files:**
- Modify: `src/opencortex/orchestrator.py:574-589` (`_try_extract_skills`)
- Modify: `src/opencortex/ace/engine.py:96-116` (`remember`)
- Test: `tests/test_e2e_phase1.py`

**Step 1: Write the failing test**

Add to `tests/test_e2e_phase1.py`:

```python
class TestAcePreferencesRouting(unittest.TestCase):
    """ACE-extracted preferences should route to user/memories, not shared/skills."""

    def test_preferences_section_routes_to_user_memory(self):
        """When RuleExtractor extracts a preferences skill, it should be stored
        as a user memory, not as a shared skill."""
        from opencortex.ace.engine import ACEngine
        # The remember() method should detect preferences section
        # and route to user memory instead of skillbook
        # This test validates the routing decision
        self.assertIn("preferences", ACEngine._USER_MEMORY_SECTIONS)

    def test_error_fixes_routes_to_shared(self):
        self.assertNotIn("error_fixes", ACEngine._USER_MEMORY_SECTIONS)
```

**Step 2: Run test to verify it fails**

Run: `uv run python3 -m unittest tests.test_e2e_phase1.TestAcePreferencesRouting -v`
Expected: FAIL — `ACEngine has no attribute '_USER_MEMORY_SECTIONS'`

**Step 3: Implement routing split in ACEngine**

In `src/opencortex/ace/engine.py`, add class-level constant and modify `remember()`:

```python
class ACEngine:
    # Sections that should route to user/memories instead of shared/skills
    _USER_MEMORY_SECTIONS = {"preferences"}
```

Modify the `remember()` method (lines 96-116):

```python
    async def remember(
        self,
        content: str,
        memory_type: str = "general",
        tenant_id: str = "",
        user_id: str = "",
    ) -> Dict[str, Any]:
        """Store content in Skillbook or user memory depending on section."""
        tid = tenant_id or self._default_tenant_id
        uid = user_id or self._default_user_id

        # Preferences route to user memory, not shared skills
        if memory_type in self._USER_MEMORY_SECTIONS and self._store_memory_fn:
            uri = await self._store_memory_fn(
                abstract=content,
                content=content,
                category=memory_type,
                context_type="memory",
            )
            return {
                "success": True,
                "uri": getattr(uri, "uri", str(uri)) if uri else "",
                "section": memory_type,
                "routed_to": "user_memory",
            }

        # All other sections → shared skills
        skill = await self._skillbook.add_skill(
            section=memory_type, content=content, tenant_id=tid, user_id=uid,
        )
        prefix = self._skillbook._resolve_prefix(tid, uid)
        uri = f"{prefix}/{skill.section}/{skill.id}"
        return {
            "success": True,
            "uri": uri,
            "skill_id": skill.id,
            "section": skill.section,
        }
```

Add `_store_memory_fn` to `__init__`:

```python
        self._store_memory_fn = None  # Set by orchestrator for user memory routing
```

Wire it in the orchestrator's `_init` method where ACEngine is created — set `self._hooks._store_memory_fn = self.add`.

**Step 4: Run tests**

Run: `uv run python3 -m unittest tests.test_e2e_phase1.TestAcePreferencesRouting tests.test_ace_phase1 tests.test_ace_phase2 -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/opencortex/ace/engine.py src/opencortex/orchestrator.py tests/test_e2e_phase1.py
git commit -m "feat: route ACE-extracted preferences to user/memories instead of shared/skills"
```

---

### Task 6: Update Session Extractor Categories

**Files:**
- Modify: `src/opencortex/session/extractor.py:86-108`
- Test: `tests/test_e2e_phase1.py`

**Step 1: Update extraction prompt**

In `src/opencortex/session/extractor.py`, replace the category section of the prompt (lines 86-108):

```python
        return f"""You are a memory extraction system. Analyze the following conversation and extract persistent memories that should be saved for future sessions.

{summary_section}
Session Quality Score: {quality_score:.1f}/1.0

Conversation:
{conversation}

Extract memories in these categories:

User memories (private to this user):
- **profile**: User identity, roles, background attributes
- **preferences**: User preferences, settings, workflow habits
- **entities**: Important entities — people, projects, paths, URLs, configurations
- **events**: Decisions, milestones, key events (each unique, never merge)

Agent knowledge (shared at project level):
- **cases**: Problem + solution pairs (each unique, never merge)
- **patterns**: Reusable patterns, best practices, recurring solutions

For each memory, provide:
- abstract: Short summary (1-2 sentences, used for vector search)
- content: Full details
- category: One of: profile, preferences, entities, events, cases, patterns
- context_type: "memory" for user categories (profile/preferences/entities/events), "case" for cases, "pattern" for patterns
- confidence: 0.0 to 1.0 (how confident this is a persistent, reusable memory)

Return ONLY a JSON array. Example:
[
  {{"abstract": "User prefers dark theme", "content": "User explicitly set dark theme in VS Code and terminal", "category": "preferences", "context_type": "memory", "confidence": 0.9}},
  {{"abstract": "Fix import error by checking PYTHONPATH", "content": "When imports fail, check PYTHONPATH includes src/", "category": "cases", "context_type": "case", "confidence": 0.7}}
]

If no meaningful memories can be extracted, return an empty array: []
Memories:"""
```

**Step 2: Run full regression**

Run: `uv run python3 -m unittest tests.test_e2e_phase1 -v`
Expected: PASS (prompt change doesn't break existing tests)

**Step 3: Commit**

```bash
git add src/opencortex/session/extractor.py
git commit -m "feat: update session extractor categories to new 6-category taxonomy"
```

---

### Task 7: Add Staging Lifecycle & TTL Cleanup

**Files:**
- Modify: `src/opencortex/orchestrator.py` (add staging support in add, TTL cleanup in decay)
- Test: `tests/test_e2e_phase1.py`

**Step 1: Write the failing test**

Add to `tests/test_e2e_phase1.py`:

```python
class TestStagingLifecycle(unittest.TestCase):
    """Test staging records: TTL, exclusion from search, cleanup."""

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self):
        self.loop.close()

    def test_staging_record_has_ttl(self):
        """Staging records must have ttl_expires_at set."""
        from opencortex.http.request_context import set_request_identity, reset_request_identity
        token = set_request_identity("t1", "u1")
        try:
            storage = InMemoryStorage()
            orch = self._build_orch(storage)
            ctx = self.loop.run_until_complete(
                orch.add(abstract="temp note", context_type="staging")
            )
            records = self.loop.run_until_complete(
                storage.filter("opencortex_contexts",
                    {"op": "must", "field": "uri", "conds": [ctx.uri]}, limit=1)
            )
            self.assertTrue(records[0].get("ttl_expires_at"))
            self.assertEqual(records[0].get("context_type"), "staging")
        finally:
            reset_request_identity(token)

    def _build_orch(self, storage):
        from opencortex.orchestrator import MemoryOrchestrator
        from opencortex.config import CortexConfig
        import tempfile
        from opencortex.storage.cortex_fs import CortexFS
        cfg = CortexConfig(embedding_provider="none")
        orch = MemoryOrchestrator(config=cfg)
        orch._storage = storage
        orch._embedder = MockEmbedder()
        orch._initialized = True
        orch._fs = CortexFS(data_root=tempfile.mkdtemp())
        return orch
```

**Step 2: Run test to verify it fails**

Run: `uv run python3 -m unittest tests.test_e2e_phase1.TestStagingLifecycle -v`
Expected: FAIL — `ttl_expires_at` is empty

**Step 3: Implement staging TTL in `add()`**

In the `add()` method of `orchestrator.py`, after the scope/category field population (from Task 3), add:

```python
        # Set TTL for staging records (24 hours from now)
        if context_type == "staging":
            from datetime import datetime, timezone, timedelta
            expires = datetime.now(timezone.utc) + timedelta(hours=24)
            record["ttl_expires_at"] = expires.strftime("%Y-%m-%dT%H:%M:%SZ")
```

**Step 4: Add TTL cleanup method**

Add to `orchestrator.py`:

```python
    async def cleanup_expired_staging(self) -> int:
        """Delete staging records past their TTL. Returns count of cleaned records."""
        self._ensure_init()
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        expired = await self._storage.filter(
            _CONTEXT_COLLECTION,
            {"op": "and", "conds": [
                {"op": "must", "field": "context_type", "conds": ["staging"]},
            ]},
            limit=1000,
        )
        cleaned = 0
        for record in expired:
            ttl = record.get("ttl_expires_at", "")
            if ttl and ttl < now:
                rid = record.get("id", "")
                uri = record.get("uri", "")
                if rid:
                    await self._storage.delete(_CONTEXT_COLLECTION, rid)
                if uri:
                    try:
                        await self._fs.write_context(uri)  # cleanup
                    except Exception:
                        pass
                cleaned += 1
                logger.info("[Orchestrator] Cleaned orphan staging: %s", uri)
        return cleaned
```

**Step 5: Run tests**

Run: `uv run python3 -m unittest tests.test_e2e_phase1.TestStagingLifecycle -v`
Expected: PASS

**Step 6: Commit**

```bash
git add src/opencortex/orchestrator.py tests/test_e2e_phase1.py
git commit -m "feat: add staging TTL lifecycle and cleanup_expired_staging()"
```

---

### Task 8: Update MCP Tool Definitions

**Files:**
- Modify: `plugins/opencortex-memory/lib/mcp-server.mjs:14-30`
- Modify: `src/opencortex/http/models.py:18-25`

**Step 1: Update MCP tool definitions**

In `plugins/opencortex-memory/lib/mcp-server.mjs`, update `memory_store`:

```javascript
  memory_store: ['POST', '/api/v1/memory/store',
    'Store a new memory, resource, or skill. Returns the URI and metadata of the stored context.', {
      abstract:     { type: 'string',  description: 'Short summary of the memory', required: true },
      content:      { type: 'string',  description: 'Full content to store', default: '' },
      category:     { type: 'string',  description: 'Category: profile, preferences, entities, events, cases, patterns, error_fixes, workflows, strategies, documents, plans', default: '' },
      context_type: { type: 'string',  description: 'Type: memory, resource, skill, case, pattern', default: 'memory' },
      meta:         { type: 'object',  description: 'Optional metadata key-value pairs' },
    }],
```

**Step 2: Run MCP tests**

Run: `node --test tests/test_mcp_server.mjs`
Expected: 8/8 PASS

**Step 3: Commit**

```bash
git add plugins/opencortex-memory/lib/mcp-server.mjs src/opencortex/http/models.py
git commit -m "feat: update MCP tool definitions with new context_type and category values"
```

---

### Task 9: Add Scope-Aware Search Filter

**Files:**
- Modify: `src/opencortex/orchestrator.py` (search method filter construction)
- Test: `tests/test_e2e_phase1.py`

**Step 1: Write the failing test**

Add to `tests/test_e2e_phase1.py`:

```python
class TestScopeAwareSearch(unittest.TestCase):
    """Search should return user's private + project shared, exclude staging."""

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self):
        self.loop.close()

    def test_search_excludes_staging(self):
        from opencortex.http.request_context import set_request_identity, reset_request_identity
        token = set_request_identity("t1", "u1")
        try:
            storage = InMemoryStorage()
            orch = self._build_orch(storage)
            # Add a staging record and a normal memory
            self.loop.run_until_complete(
                orch.add(abstract="staging note", context_type="staging")
            )
            self.loop.run_until_complete(
                orch.add(abstract="permanent pref", category="preferences", context_type="memory")
            )
            result = self.loop.run_until_complete(
                orch.search(query="note pref")
            )
            uris = [m.uri for m in result.memories + result.resources + result.skills]
            # staging should not appear
            for uri in uris:
                self.assertNotIn("/staging/", uri)
        finally:
            reset_request_identity(token)

    def _build_orch(self, storage):
        from opencortex.orchestrator import MemoryOrchestrator
        from opencortex.config import CortexConfig
        import tempfile
        from opencortex.storage.cortex_fs import CortexFS
        cfg = CortexConfig(embedding_provider="none")
        orch = MemoryOrchestrator(config=cfg)
        orch._storage = storage
        orch._embedder = MockEmbedder()
        orch._initialized = True
        orch._fs = CortexFS(data_root=tempfile.mkdtemp())
        return orch
```

**Step 2: Run test to verify it fails**

Run: `uv run python3 -m unittest tests.test_e2e_phase1.TestScopeAwareSearch -v`
Expected: FAIL — staging record appears in search results

**Step 3: Add staging exclusion filter**

In `src/opencortex/orchestrator.py`, in the `search()` method, add staging exclusion to the metadata filter before passing to retriever. Find where `metadata_filter` is passed to the retriever and wrap it:

```python
        # Exclude staging from global search
        staging_exclude = {"op": "must_not", "field": "context_type", "conds": ["staging"]}
        if metadata_filter:
            metadata_filter = {"op": "and", "conds": [metadata_filter, staging_exclude]}
        else:
            metadata_filter = staging_exclude
```

**Step 4: Run tests**

Run: `uv run python3 -m unittest tests.test_e2e_phase1.TestScopeAwareSearch -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/opencortex/orchestrator.py tests/test_e2e_phase1.py
git commit -m "feat: add staging exclusion to search filter"
```

---

### Task 10: Add Merge Behavior to Session End

**Files:**
- Modify: `src/opencortex/session/manager.py:174-197`
- Test: `tests/test_e2e_phase1.py`

**Step 1: Write the failing test**

Add to `tests/test_e2e_phase1.py`:

```python
class TestMergeBehavior(unittest.TestCase):
    """Mergeable categories should update existing instead of creating duplicate."""

    def test_mergeable_categories_constant(self):
        from opencortex.session.manager import MERGEABLE_CATEGORIES
        self.assertIn("profile", MERGEABLE_CATEGORIES)
        self.assertIn("preferences", MERGEABLE_CATEGORIES)
        self.assertIn("entities", MERGEABLE_CATEGORIES)
        self.assertIn("patterns", MERGEABLE_CATEGORIES)
        self.assertNotIn("events", MERGEABLE_CATEGORIES)
        self.assertNotIn("cases", MERGEABLE_CATEGORIES)
```

**Step 2: Run test to verify it fails**

Run: `uv run python3 -m unittest tests.test_e2e_phase1.TestMergeBehavior -v`
Expected: FAIL — `cannot import name 'MERGEABLE_CATEGORIES'`

**Step 3: Add MERGEABLE_CATEGORIES and update merge logic**

In `src/opencortex/session/manager.py`, add at module level:

```python
MERGEABLE_CATEGORIES = {"profile", "preferences", "entities", "patterns"}
```

Then update the `_try_merge` call in `end()` (lines 179-186) to check mergeability:

```python
        for memory in extracted:
            if memory.confidence < _MIN_CONFIDENCE:
                result.skipped_count += 1
                continue

            # Only attempt merge for mergeable categories
            if memory.category in MERGEABLE_CATEGORIES:
                is_merged = await self._try_merge(memory)
                if is_merged:
                    result.merged_count += 1
                    continue

            # Non-mergeable or no merge found: store new
            stored = await self._store_memory(memory)
            if stored:
                result.stored_count += 1
            else:
                result.skipped_count += 1
```

**Step 4: Run tests**

Run: `uv run python3 -m unittest tests.test_e2e_phase1.TestMergeBehavior -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/opencortex/session/manager.py tests/test_e2e_phase1.py
git commit -m "feat: add MERGEABLE_CATEGORIES and category-aware merge in session end"
```

---

### Task 11: Data Migration Script

**Files:**
- Create: `src/opencortex/migration/v030_path_redesign.py`
- Modify: `src/opencortex/orchestrator.py` (call migration on init)
- Test: `tests/test_e2e_phase1.py`

**Step 1: Write the failing test**

Add to `tests/test_e2e_phase1.py`:

```python
class TestMigrationBackfill(unittest.TestCase):
    """Test that backfill_new_fields adds scope/category to existing records."""

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self):
        self.loop.close()

    def test_backfill_infers_scope_from_uri(self):
        from opencortex.migration.v030_path_redesign import infer_scope
        self.assertEqual(infer_scope("opencortex://t1/user/u1/memories/pref/abc"), "private")
        self.assertEqual(infer_scope("opencortex://t1/shared/skills/err/abc"), "shared")
        self.assertEqual(infer_scope("opencortex://t1/resources/docs/abc"), "shared")

    def test_backfill_infers_category_from_uri(self):
        from opencortex.migration.v030_path_redesign import infer_category
        self.assertEqual(infer_category("opencortex://t1/user/u1/memories/preferences/abc"), "preferences")
        self.assertEqual(infer_category("opencortex://t1/shared/skills/error_fixes/abc"), "error_fixes")
        self.assertEqual(infer_category("opencortex://t1/resources/documents/abc"), "documents")

    def test_backfill_infers_mergeable(self):
        from opencortex.migration.v030_path_redesign import infer_mergeable
        self.assertTrue(infer_mergeable("preferences"))
        self.assertTrue(infer_mergeable("profile"))
        self.assertFalse(infer_mergeable("events"))
        self.assertFalse(infer_mergeable("cases"))
```

**Step 2: Run test to verify it fails**

Run: `uv run python3 -m unittest tests.test_e2e_phase1.TestMigrationBackfill -v`
Expected: FAIL — `No module named 'opencortex.migration'`

**Step 3: Create migration module**

Create `src/opencortex/migration/__init__.py` (empty) and `src/opencortex/migration/v030_path_redesign.py`:

```python
"""v0.2.x → v0.3.0 storage path migration. Idempotent."""

import logging

logger = logging.getLogger(__name__)

_MERGEABLE = {"profile", "preferences", "entities", "patterns"}


def infer_scope(uri: str) -> str:
    """Infer scope from URI path."""
    if "/user/" in uri:
        return "private"
    return "shared"


def infer_category(uri: str) -> str:
    """Extract category from URI path segments."""
    parts = uri.replace("opencortex://", "").split("/")
    parents = ("memories", "skills", "resources", "cases", "patterns", "staging")
    for parent in parents:
        if parent in parts:
            idx = parts.index(parent)
            if parent in ("cases", "patterns"):
                return parent
            if idx + 1 < len(parts):
                candidate = parts[idx + 1]
                # Skip node_id (12-char hex)
                if len(candidate) != 12:
                    return candidate
    return ""


def infer_mergeable(category: str) -> bool:
    """Check if a category supports merging."""
    return category in _MERGEABLE


async def backfill_new_fields(storage, collection: str) -> int:
    """Backfill scope/category/mergeable on existing records missing them.

    Idempotent: skips records that already have scope set.
    Returns count of updated records.
    """
    all_records = await storage.filter(collection, None, limit=10000)
    updated = 0
    for record in all_records:
        if record.get("scope"):
            continue  # Already migrated
        uri = record.get("uri", "")
        rid = record.get("id", "")
        if not uri or not rid:
            continue
        scope = infer_scope(uri)
        category = infer_category(uri)
        await storage.update(collection, rid, {
            "scope": scope,
            "category": category,
            "mergeable": infer_mergeable(category),
            "source_user_id": record.get("owner_user_id", ""),
            "session_id": "",
            "ttl_expires_at": "",
        })
        updated += 1
    logger.info("[Migration] Backfilled %d records in %s", updated, collection)
    return updated


# Root-level junk directories created by the URI bug (pre-v0.2.3)
ROOT_JUNK = [
    "agents", "coder-frontend", "coder-go", "coder-python",
    "coder-rust", "coding-style", "git-workflow", "hooks",
    "patterns", "performance", "security", "testing",
]


async def cleanup_root_junk(storage, cortex_fs, collection: str) -> int:
    """Delete root-level junk entries from CortexFS and Qdrant."""
    cleaned = 0
    for name in ROOT_JUNK:
        uri = f"opencortex://{name}"
        records = await storage.filter(
            collection,
            {"op": "must", "field": "uri", "conds": [uri]},
            limit=10,
        )
        for rec in records:
            rid = rec.get("id", "")
            if rid:
                await storage.delete(collection, rid)
                cleaned += 1
        # CortexFS cleanup (best-effort)
        try:
            path = cortex_fs._uri_to_path(uri)
            cortex_fs.agfs.rm(path, recursive=True)
        except Exception:
            pass
    if cleaned:
        logger.info("[Migration] Cleaned %d root-level junk records", cleaned)
    return cleaned
```

**Step 4: Run tests**

Run: `uv run python3 -m unittest tests.test_e2e_phase1.TestMigrationBackfill -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/opencortex/migration/__init__.py src/opencortex/migration/v030_path_redesign.py tests/test_e2e_phase1.py
git commit -m "feat: add v0.3.0 migration module with backfill and junk cleanup"
```

---

### Task 12: Wire Migration into Orchestrator Init & Bump Version

**Files:**
- Modify: `src/opencortex/orchestrator.py` (call migration in _init)
- Modify: `pyproject.toml`
- Modify: `plugins/opencortex-memory/.claude-plugin/plugin.json`

**Step 1: Wire migration into orchestrator**

In `src/opencortex/orchestrator.py`, in the `_init()` method (after `ensure_text_indexes` call), add:

```python
        # Run v0.3.0 path migration (idempotent)
        try:
            from opencortex.migration.v030_path_redesign import (
                backfill_new_fields, cleanup_root_junk,
            )
            await cleanup_root_junk(self._storage, self._fs, _CONTEXT_COLLECTION)
            await backfill_new_fields(self._storage, _CONTEXT_COLLECTION)
        except Exception as exc:
            logger.warning("[Orchestrator] Migration skipped: %s", exc)
```

**Step 2: Bump version to 0.3.0**

In `pyproject.toml`: `version = "0.3.0"`
In `plugins/opencortex-memory/.claude-plugin/plugin.json`: `"version": "0.3.0"`

**Step 3: Commit**

```bash
git add src/opencortex/orchestrator.py pyproject.toml plugins/opencortex-memory/.claude-plugin/plugin.json
git commit -m "feat: wire v0.3.0 migration into orchestrator init, bump version"
```

---

### Task 13: Final Regression Test

**Files:**
- All modified files

**Step 1: Run full Python test suite**

Run: `uv run python3 -m unittest tests.test_e2e_phase1 tests.test_ace_phase1 tests.test_ace_phase2 tests.test_rule_extractor tests.test_skill_search_fusion tests.test_integration_skill_pipeline tests.test_text_scoring -v`
Expected: ALL PASS

**Step 2: Run MCP tests**

Run: `node --test tests/test_mcp_server.mjs`
Expected: 8/8 PASS

**Step 3: Fix any failures**

If tests fail, investigate and fix. Common issues:
- InMemoryStorage mock may need `scope`/`category` fields in filter evaluation
- Skillbook tests may reference old `skillbooks/` prefix
- Search tests may need adjustment for staging exclusion

**Step 4: Final commit if fixes were needed**

```bash
git add -A
git commit -m "fix: resolve test failures from storage path redesign"
```
