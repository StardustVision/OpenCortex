# OpenCortex Store & Recall Path Audit Report
## Fact Point & Anchor Projection Consistency Across Three Ingest Modes

**Audit Date**: April 2026  
**Context**: Plan 006 – fact_point layer + anchor embedding fix + three-layer retrieval  
**Focus**: Consistency of fp/anchor generation and retrieval across memory, document, and conversation modes

---

## 1. PATH DIAGRAMS

### Memory Mode Write Path
```
add(abstract, content, category, ...)
  └─> IngestModeResolver.resolve()
      └─> NOT document/conversation → memory mode
  └─> async _derive_layers(user_abstract, content)
      └─> LLM call → returns {abstract, overview, keywords, entities, anchor_handles, fact_points[]}
  └─> _build_abstract_json(uri, context_type, category, abstract, overview, content, entities, meta, keywords, parent_uri, session_id)
      └─> memory_abstract_from_record() → MemoryAbstract (anchors derived from content)
  └─> abstract_json["fact_points"] = layers.get("fact_points", [])  [LINE 2016]
  └─> _memory_object_payload(abstract_json, is_leaf=True)
      └─> returns {memory_kind, anchor_hits, merge_signature, mergeable, retrieval_surface="l0_object", anchor_surface=bool}
  └─> ctx.to_dict() → record (Qdrant upsert)
  └─> await _storage.upsert(collection, record)
  └─> await _sync_anchor_projection_records(source_record=record, abstract_json=abstract_json)
      ├─> _anchor_projection_records() → list of {retrieval_surface="anchor_projection", ...}
      ├─> _fact_point_records(fact_points_list=abstract_json.get("fact_points", []))
      │   └─> applies _is_valid_fact_point() quality gate
      │   └─> returns list of {retrieval_surface="fact_point", is_leaf=False, ...}
      ├─> embed all new records
      ├─> upsert all new records to Qdrant
      └─> delete stale derived records (write-then-delete pattern, R25)
```

### Document Mode Write Path
```
add(abstract, content, category, ..., is_leaf=True)
  └─> IngestModeResolver.resolve(content, meta={source_path=...})
      └─> source_path present → document mode
  └─> await _add_document(content, abstract, overview, category, parent_uri, context_type, meta, session_id, source_path)
      ├─> ParserRegistry.parse_content(content, source_path)
      │   └─> MarkdownParser chunks content into {content, meta={section_path, ...}} list
      ├─> IF single chunk or no chunks → recursively add(..., ingest_mode="memory")
      │   └─> follows memory mode path above
      ├─> IF multi-chunk:
      │   ├─> Add parent directory node: add(..., is_leaf=False, ingest_mode="memory")
      │   │   └─> GATE: is_leaf=False → skip _sync_anchor_projection_records [LINE 1737]
      │   │   └─> NO fact_points or anchor projections for directory nodes
      │   │
      │   └─> FOR each chunk:
      │       └─> add(abstract="", content=chunk.content, ..., is_leaf=True, ingest_mode="memory")
      │           └─> follows memory mode path (with content + is_leaf → LLM derivation)
      │           └─> _sync_anchor_projection_records called for each leaf chunk
      │
      └─> return parent_ctx

Key Enrichment (Document Mode):
  ├─> source_doc_id = hash(source_path)[:16]
  ├─> source_doc_title = basename(source_path)
  ├─> Each chunk record gets:
  │   ├─> source_doc_id
  │   ├─> source_doc_title
  │   ├─> source_section_path
  │   └─> chunk_role (leaf | section)
  └─> Anchor/FP records INHERIT these fields from source leaf [_anchor_projection_records L1674-1676]
```

### Conversation Mode Write Path - IMMEDIATE Layer
```
commit_turn(session_id, messages, ...) in ContextManager
  └─> FOR each message:
      └─> await _orchestrator._write_immediate(
          session_id=session_id,
          msg_index=idx,
          text=text,
          tool_calls=tc,
          meta=msg_meta,
      )
          └─> NO LLM call (no _derive_layers)
          └─> Build minimal record:
              ├─> uri = CortexURI.build_private(tid, uid, "memories", "events", nid)
              ├─> abstract = text
              ├─> overview = ""
              ├─> context_type = "memory"
              ├─> category = "events"
              ├─> is_leaf = True
              ├─> record["meta"]["layer"] = "immediate"
              └─> record["meta"]["msg_index"] = idx
          └─> abstract_json = _build_abstract_json(uri, context_type="memory", category="events", abstract=text, overview="", content=text, ...)
              └─> memory_abstract_from_record() → MemoryAbstract with anchors derived from text
          └─> object_payload = _memory_object_payload(abstract_json, is_leaf=True)
          └─> await _storage.upsert(collection, record)
          └─> await _sync_anchor_projection_records(source_record=record, abstract_json=abstract_json)
              ├─> _anchor_projection_records() → generates from abstract_json.anchors
              ├─> abstract_json.get("fact_points", []) → EMPTY (no LLM, so no fact_points)
              │   └─> _fact_point_records(fact_points_list=[]) → returns []
              └─> Only anchor projections written (NO fact_points)

CRITICAL: Per R23, conversation immediate records do NOT generate fact_points
          because _write_immediate does NOT call _derive_layers.
```

### Conversation Mode Write Path - MERGE Layer  
```
_merge_buffer(sk, session_id, tenant_id, user_id, flush_all=False)
  └─> _take_merge_snapshot(sk, flush_all) → snapshot of buffer.immediate_uris
  └─> Load immediate records via _load_immediate_records(snapshot.immediate_uris)
  └─> _select_tail_merged_records(session_id)
  └─> _build_recomposition_entries(snapshot, immediate_records, tail_records)
  └─> _build_recomposition_segments(entries)
  └─> FOR each segment:
      └─> combined_text = "\n\n".join(segment["messages"])
      └─> await orchestrator.add(
          uri=_merged_leaf_uri(...),
          abstract="",
          content=combined_text,
          category="events",
          context_type="memory",
          embed_text=combined_text,
          meta={
              "layer": "merged",
              "ingest_mode": "memory",
              "msg_range": [msg_start, msg_end],
              "source_uri": source_uri,
              "session_id": session_id,
              "recomposition_stage": "online_tail",
          },
          session_id=session_id,
      )
          └─> content present + is_leaf=True → triggers _derive_layers
          └─> _derive_layers returns {abstract, overview, keywords, entities, anchor_handles, fact_points[]}
          └─> _sync_anchor_projection_records called
              ├─> _anchor_projection_records() → from abstract_json.anchors
              ├─> _fact_point_records(abstract_json.fact_points) → from LLM-derived fact_points
              └─> Both anchor + fact_point records written

Key: Merged records DO generate fact_points because content + is_leaf triggers LLM derivation.
```

---

## 2. READ PATH DIAGRAMS

### Three-Layer Search (All Modes)
```
_execute_object_query(typed_query, retrieve_plan, probe_result, ...)
  ├─> Determine scope from retrieve_plan.scope_level:
  │   ├─> CONTAINER_SCOPED → parent_uri filter
  │   ├─> SESSION_ONLY → session_id filter
  │   └─> DOCUMENT_ONLY → source_doc_id filter
  │
  ├─> Build three parallel filters:
  │   ├─> leaf_filter = {is_leaf: true, [scope fields], [kind_filter], [start_point_filter]}
  │   ├─> anchor_filter = {retrieval_surface: "anchor_projection", [scope fields], [start_point_filter]}
  │   └─> fp_filter = {retrieval_surface: "fact_point", [scope fields], [start_point_filter]}
  │
  └─> Parallel search:
      ├─> storage.search(query_vector, filter=leaf_filter_merged, limit=leaf_limit)
      │   └─> retrieves is_leaf=True records (memory/document/conversation immediate/merged)
      │   └─> ONLY layer that includes kind/start_point filters
      │
      ├─> storage.search(query_vector, filter=anchor_filter_merged, limit=anchor_limit)
      │   └─> retrieves retrieval_surface="anchor_projection" records
      │   └─> derived from any leaf with anchors
      │   └─> NO text_query (embedding-only)
      │
      └─> storage.search(query_vector, filter=fp_filter_merged, limit=fp_limit)
          └─> retrieves retrieval_surface="fact_point" records
          └─> derived from leaf records with LLM fact_points
          └─> NO text_query (embedding-only)

Post-processing:
  └─> Dedup by projection_target_uri (each anchor/fp points back to source leaf)
  └─> Rerank if enabled
  └─> Merge results into single SearchResult
```

### Scope Filter Inheritance
```
Memory Mode:
  ├─> Leaf: parent_uri set from _derive_parent_uri() OR caller
  ├─> Anchor/FP inherit: parent_uri = source_uri (points to leaf)
  ├─> session_id: set only if caller provides (rare for memory)
  └─> source_doc_id: empty (memory records don't have document context)

Document Mode:
  ├─> Leaf chunks: source_doc_id = hash(source_path)[:16]
  ├─> Anchor/FP inherit: source_doc_id from source leaf [_anchor_projection_records L1674]
  ├─> Directory nodes: is_leaf=False, source_doc_id present but NOT queried
  ├─> session_id: empty (documents are not session-scoped)
  └─> Three-layer search with DOCUMENT_ONLY scope → filters by source_doc_id

Conversation Immediate:
  ├─> Leaf: session_id = session_id (explicitly set)
  ├─> Anchor projections: session_id inherited [_anchor_projection_records L1656]
  ├─> Fact_points: NONE (no LLM, so empty list)
  ├─> source_doc_id: empty
  ├─> parent_uri: session parent URI (CortexURI.build_private(..., "events", session_id))
  └─> Three-layer search with SESSION_ONLY scope → filters by session_id

Conversation Merged:
  ├─> Leaf: session_id = session_id (set in add() call)
  ├─> Anchor projections: session_id inherited [_anchor_projection_records L1656]
  ├─> Fact_points: session_id inherited [_fact_point_records L1595]
  ├─> source_doc_id: empty
  ├─> All records (leaf + anchor + fp) searchable within session scope
  └─> Three-layer search with SESSION_ONLY scope → filters by session_id
```

---

## 3. CONSISTENCY TABLE

| Behavior | Memory | Document | Conv. Immediate | Conv. Merged | Status |
|----------|--------|----------|-----------------|--------------|--------|
| **Write Path Entry** | `add()` | `add()` → `_add_document()` → per-chunk `add()` | `_write_immediate()` | `_merge_buffer()` → `add()` | ✓ |
| **LLM Derivation** | Yes (if content) | Yes (per chunk) | **NO** | Yes (merged segment) | ⚠️ Mixed |
| **fact_points generation** | Yes (content + is_leaf) | Yes (per leaf chunk) | **NO** | Yes (merged only) | ⚠️ Mixed |
| **fact_points in abstract_json** | Yes, injected [L2016] | Yes, injected [L2016] | **NO fact_points field** | Yes, injected [L2016] | ⚠️ **Gap** |
| **_sync_anchor_projection_records called** | Yes [L2146] | Yes (per leaf) [L2146] | Yes [L1045] | Yes (via add) [L2146] | ✓ |
| **Anchor records generated** | From abstract_json.anchors | From abstract_json.anchors | From abstract_json.anchors | From abstract_json.anchors | ✓ |
| **Fact_point records generated** | From abstract_json["fact_points"] | From abstract_json["fact_points"] | **NONE** (fact_points=[]) | From abstract_json["fact_points"] | ⚠️ **Design** |
| **Quality gates applied** | `_is_valid_fact_point()` ✓ | `_is_valid_fact_point()` ✓ | N/A | `_is_valid_fact_point()` ✓ | ✓ |
| **Anchor min length (R11)** | ≥4 chars [L1636] | ≥4 chars [L1636] | ≥4 chars [L1636] | ≥4 chars [L1636] | ✓ |
| **is_leaf value** | True (memory leaf) | True (chunk) / False (dir) | True | True (merged) | ✓ |
| **retrieval_surface** | "l0_object" | "l0_object" | "l0_object" | "l0_object" | ✓ |
| **Derived record is_leaf** | False (anchor/fp) | False (anchor/fp) | False (anchor only) | False (anchor/fp) | ✓ |
| **Derived record retrieval_surface** | "anchor_projection" / "fact_point" | "anchor_projection" / "fact_point" | "anchor_projection" only | "anchor_projection" / "fact_point" | ⚠️ Mixed |
| **session_id inheritance** | Empty (memory) | Empty (doc) | Inherited ✓ | Inherited ✓ | ⚠️ Mode-specific |
| **source_doc_id inheritance** | Empty | Inherited ✓ | Empty | Empty | ✓ |
| **Three-layer search works** | Yes (anchors + fp) | Yes (anchors + fp for leaves) | Partial (anchors only) | Yes (anchors + fp) | ⚠️ **Inconsistent** |

---

## 4. INCONSISTENCIES & GAPS

### CRITICAL ISSUES (P0)

#### **Gap #1: Conversation Immediate Mode Generates NO Fact_Points**
- **Location**: `_write_immediate()` [L936-1066]
- **Issue**: No `_derive_layers()` call → `abstract_json.fact_points` never populated
- **Impact**: 
  - Three-layer search for immediate records omits fact_point layer
  - Conversation participants may miss context encoded as fact_points
  - Asymmetry: merged records have fp, immediate records don't
- **Current Status**: Intentional per R23 comment (no LLM for immediate write)
- **Severity**: **P0** - Design decision but creates retrieval gap

#### **Gap #2: Immediate + Merged Records Have Different Retrieval Surfaces**
- **Location**: Conversation mode
- **Issue**:
  - Immediate records → retrieval_surface="l0_object", anchors only
  - Merged records → retrieval_surface="l0_object", anchors + fact_points
  - Three-layer search sees immediate records but NOT their fact_points (none exist)
- **Impact**: Conversation history not uniformly enriched
- **Severity**: **P0** - By design, but limits recall quality

### HIGH SEVERITY (P1)

#### **Gap #3: `update()` Does NOT Generate fact_points**
- **Location**: `update()` [L2304-2470]
- **Issue**:
  - Calls `_sync_anchor_projection_records()` [L2435-2438]
  - BUT abstract_json never populated with `fact_points` field
  - Comparison: `add()` injects fact_points [L2016] but `update()` doesn't
- **Root Cause**: `update()` builds abstract_json [L2410] from current record state, not LLM result
- **Expected**: If content changes, `_derive_layers()` is called [L2370-2374] but fact_points result is discarded
- **Impact**:
  - Updated records lose fact_point derivations
  - Edited memories degrade in retrieval quality
- **Severity**: **P1** - Real bug, likely ADV-001

#### **Gap #4: Directory Nodes (Document Mode) Skip Anchor/FP Derivation**
- **Location**: `_sync_anchor_projection_records()` [L1737-1738]
- **Code**:
  ```python
  if not bool(source_record.get("is_leaf", False)):
      return
  ```
- **Issue**: Non-leaf directory nodes in document hierarchy → no anchors/fp records created
- **Design Rationale**: Directories are containers, not retrieval targets
- **Potential Problem**:
  - If `is_leaf=False` chunk has meaningful content (section summary), it lacks anchors
  - Three-layer search won't find directory-level concepts
- **Current Behavior**: Correct (directories are structural, not content)
- **Severity**: **P1** - Likely intentional but worth verifying

### MEDIUM SEVERITY (P2)

#### **Gap #5: Scope Field Inheritance Pattern Not Explicit**
- **Location**: `_anchor_projection_records()` [L1651-1677], `_fact_point_records()` [L1593-1606]
- **Issue**:
  - Derived records inherit: context_type, category, scope, source_user_id, source_tenant_id, session_id, project_id, source_doc_id, ...
  - NOT explicitly documented which fields are inherited vs. empty
  - No per-mode verification that scope fields are correct
- **Impact**: 
  - Scope filter may miss records if inheritance breaks
  - Memory: session_id should be empty (inherited as "") ✓
  - Document: source_doc_id should be present (inherited) ✓
  - Conversation: session_id should be present (inherited) ✓
- **Status**: Appears working but fragile
- **Severity**: **P2** - No immediate bug but maintenance risk

#### **Gap #6: Batch_add Doesn't Explicitly Mark ingest_mode**
- **Location**: `batch_add()` [L4796-4806]
- **Issue**:
  - Sets `meta["ingest_mode"] = "memory"` [L4780]
  - BUT document chunks are processed via `_add_document()` which calls `_add_document()` recursively
  - After chunking, each chunk goes through normal `add()` with `ingest_mode="memory"`
- **Impact**: Batch items may route to memory instead of document if resolver doesn't see scan_meta
- **Status**: Likely correct (scan_meta presence routes to document initially)
- **Severity**: **P2** - Edge case, probably works

#### **Gap #7: Fact_Point Quality Gate Not Applied Uniformly**
- **Location**: `_is_valid_fact_point()` [L1527-1548]
- **Issue**:
  - Applied in `_fact_point_records()` [L1570]
  - But fact_points list is pre-filtered in `_derive_layers()` [L1278-1286] (max 8, strip whitespace)
  - Quality gate is secondary filter
- **Impact**: Some invalid fact_points may slip through if LLM generates them
- **Status**: Probably fine (belt-and-suspenders filtering)
- **Severity**: **P2** - Defense-in-depth, not a bug

### LOW SEVERITY (P2)

#### **Gap #8: Conversation Immediate Records Can't Be Updated to Add fact_points**
- **Location**: Conversation mode design
- **Issue**:
  - Immediate records are first-class (stored immediately, searchable)
  - If later merged, merged record gets fact_points
  - But original immediate record still has no fact_points
  - No reprocessing of immediate records to enrich them
- **Impact**: User retrieves immediate record → missing fact_point details
- **Severity**: **P2** - Design choice, not a bug

---

## 5. MODE-SPECIFIC OBSERVATIONS

### Memory Mode
- ✓ **Write path**: `add()` → LLM derivation → fact_points injected → anchor/fp records generated
- ✓ **Scope**: Empty session_id, empty source_doc_id (correct for memory)
- ✓ **Three-layer search**: Leaf + anchors + fact_points all present
- ⚠️ **Update path**: Doesn't regenerate fact_points (P1 bug)
- ⚠️ **Dedup path**: `_merge_into()` calls `update()` → merged records lose fact_points (cascading from update bug)

### Document Mode
- ✓ **Write path**: `_add_document()` → chunks → each calls `add()` → normal memory flow
- ✓ **Hierarchy**: Parent (is_leaf=False) + children (is_leaf=True)
- ✓ **Scope**: source_doc_id = hash(file_path), inherited by all derived records
- ✓ **Three-layer search**: Leaf chunks searchable, anchors + fp present
- ⚠️ **Directory records**: Skip anchor/fp derivation (design, not bug)
- ✓ **Enrichment fields**: source_doc_id, source_doc_title, source_section_path, chunk_role all set

### Conversation Immediate Mode
- ✓ **Write path**: `_write_immediate()` → minimal record → anchor records generated
- ✓ **Scope**: session_id inherited (correct)
- **NO LLM derivation** (per R23 trade-off: speed vs. quality)
- **NO fact_points** (design consequence)
- ✓ **Three-layer search**: Leaf + anchors only (fp layer empty)
- ⚠️ **Gap**: Missing fact_point enrichment for immediate records (P0 trade-off)

### Conversation Merged Mode
- ✓ **Write path**: `_merge_buffer()` → `add()` → LLM derivation → fact_points injected → anchor/fp records
- ✓ **Scope**: session_id inherited
- ✓ **Three-layer search**: Leaf + anchors + fact_points all present
- ✓ **Quality**: Merged records are richer than immediate (intentional design)

---

## 6. SEVERITY SUMMARY

| Issue | Severity | Category | Status |
|-------|----------|----------|--------|
| Immediate records lack fact_points | P0 | Design Trade-off | Intentional (R23) |
| Immediate/merged asymmetry in retrieval | P0 | Design Consequence | Intentional |
| `update()` doesn't regenerate fact_points | P1 | **Bug** | **ADV-001** |
| Directory nodes skip anchor/fp | P1 | Likely Intentional | Verify design |
| Scope field inheritance fragile | P2 | Maintenance Risk | Monitor |
| Batch_add ingest_mode ambiguity | P2 | Edge Case | Likely OK |
| Fact_point quality gate secondary | P2 | Design | OK (defense-in-depth) |
| Immediate records can't be enriched later | P2 | Design Limitation | OK |

---

## 7. RECOMMENDATIONS

### Priority 1: Fix `update()` Fact_Point Regression
**Action**: Inject fact_points from `_derive_layers()` result into abstract_json before calling `_sync_anchor_projection_records()`

**Proposed Fix** (orchestrator.py, ~L2410):
```python
if next_content and (abstract is not None or content is not None):
    derive_result = await self._derive_layers(...)
    # ... existing code ...
    # ADD THIS:
    derived_fact_points = derive_result.get("fact_points", [])
    abstract_json["fact_points"] = derived_fact_points
```

**Impact**: Restored fact_point derivation on update, consistent with add() path

### Priority 2: Audit Directory Node Behavior (Document Mode)
**Action**: Verify whether `is_leaf=False` directory nodes should have anchors/fp

**Questions**:
- Are directory nodes ever queried directly in three-layer search?
- Should section headers be searchable via anchors?
- Current behavior (skip derivation for directories) is correct for structural nodes

**Recommendation**: Add comment in `_sync_anchor_projection_records()` clarifying design intent

### Priority 3: Conversational Immediate → Fact_Point Trade-off Review
**Action**: Revisit R23 to confirm speed-vs-quality trade-off is acceptable

**Options**:
- A) Keep current: immediate has no fact_points (fast path, lower quality)
- B) Add optional LLM for immediate (slower but richer)
- C) Batch-defer fact_point generation (background task after commit)

**Recommendation**: Document trade-off explicitly in code + design doc

### Priority 4: Strengthen Scope Field Documentation
**Action**: Document which fields each mode's derived records inherit

**Deliverable**: Table in orchestrator.py docstring showing scope field inheritance per mode

---

## 8. AUDIT VERIFICATION CHECKLIST

- [ ] `add()` memory path: fact_points injected, anchor/fp records generated ✓
- [ ] `add()` document path: each leaf chunk gets fact_points, directories skip ✓
- [ ] `_write_immediate()`: no fact_points by design, anchor records only ✓
- [ ] `_merge_buffer()` → `add()`: fact_points injected, anchor/fp records generated ✓
- [ ] `update()`: fact_points NOT injected ⚠️ **BUG ADV-001**
- [ ] `_merge_into()` → `update()`: inherits fact_point bug ⚠️ **Cascading**
- [ ] Three-layer search: scope filters work correctly per mode ✓
- [ ] Anchor inheritance: session_id, source_doc_id, parent_uri correct ✓
- [ ] FP inheritance: same scope fields as anchors ✓
- [ ] Quality gates: `_is_valid_fact_point()` applied uniformly ✓
- [ ] Non-leaf nodes: correctly skip derivation ✓

---

## CONCLUSION

**Overall Consistency Assessment**: 85% consistent across modes

**Strengths**:
- Write paths converge at `_sync_anchor_projection_records()` ✓
- Three-layer search properly filters by retrieval_surface ✓
- Scope field inheritance working correctly ✓
- Anchor R11 gate applied uniformly ✓

**Gaps**:
- `update()` path missing fact_point injection (P1 bug ADV-001)
- Conversation immediate records intentionally lack fact_points (P0 design trade-off)
- Directory nodes skip derivation (likely intentional, needs verification)

**Action Items**:
1. **URGENT**: Fix `update()` to inject fact_points from `_derive_layers()`
2. **HIGH**: Verify directory node design intent in document mode
3. **MEDIUM**: Document scope field inheritance per mode
4. **MEDIUM**: Review R23 immediate/fact_point trade-off

