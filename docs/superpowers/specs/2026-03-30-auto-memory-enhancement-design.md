# Auto Memory Enhancement

Three targeted improvements to OpenCortex memory quality: index API, prompt-level storage guidance, and server-side soft checks. No enum changes, no pipeline changes, no migration.

## Motivation

Agents using OpenCortex lack two things: (1) a global view of what's stored (recall is vector-search-only, so agents don't know what memories exist), and (2) guidance on what to store (no type/quality rules, so agents dump everything including code snippets and ephemeral context, creating retrieval noise).

## 1. Memory Index API

### Problem

Agent has no way to see "what do I know?" at session start. Every recall is a vector search that only returns semantically similar results, missing memories with different phrasing.

### Endpoint

```
GET /api/v1/memory/index?context_type=memory,resource&limit=200
```

Parameters:
- `context_type` (optional): comma-separated filter, uses existing enum values
- `limit` (optional, default 200): max records total

### Response

```json
{
  "index": {
    "memory": [
      {"uri": "opencortex://t/u/memories/memory/preferences/abc123", "abstract": "Senior backend engineer, deep Go expertise", "category": "profile", "created_at": "2026-03-28T10:00:00Z"}
    ],
    "resource": [...],
    "pattern": [...]
  },
  "total": 42
}
```

### Implementation

- New handler in `http/server.py`: `GET /api/v1/memory/index`
- Query Qdrant via `scroll()` (no embedding needed), fetch `uri` + `abstract` + `context_type` + `category` + `created_at`
- Group by `context_type`, order by `created_at` desc within each group
- Truncate each `abstract` to 150 characters
- Scope: tenant + user isolation (same as search)

### Orchestrator Method

New method `async def memory_index(context_type=None, limit=200) -> dict` in `orchestrator.py`:
- Scroll through collection with optional `context_type` filter
- Return grouped dict

### MCP Tool

New tool `memory_index` in `mcp-server.mjs`:

```javascript
{
  name: "memory_index",
  description: "Get a lightweight index of all stored memories, grouped by type. Call at session start to understand what context is available.",
  inputSchema: {
    properties: {
      context_type: { type: "string", description: "Comma-separated types to include (memory,resource,skill,case,pattern). Omit for all." },
      limit: { type: "number", description: "Max records to return (default 200)" }
    }
  }
}
```

## 2. Storage Guidance (Prompt Layer)

### Problem

Agents store everything indiscriminately — code snippets, file paths, debugging steps, git history — creating noise that degrades retrieval quality.

### Implementation

Add a "Memory Storage Guide" section to the MCP `usage-guide` prompt in `mcp-server.mjs`:

```
## Memory Storage Guide

### What to Store
- **User context**: Role, expertise, preferences, working style, communication style
- **Behavioral feedback**: Corrections to your approach, confirmed good patterns, things to avoid
- **Project context**: Active goals, deadlines (use absolute dates), key decisions, blockers
- **Reference pointers**: URLs, doc locations, tool configurations, reusable procedures

### What NOT to Store
- Code structure, file paths, architecture — derivable from reading the codebase
- Git history, recent changes — use git log / git blame
- Debugging steps or fix recipes — the fix is in the code, the context in the commit
- Anything already in CLAUDE.md, AGENTS.md, or project docs
- Ephemeral task state or current conversation context
- Raw code snippets — store a description of the pattern instead

### Storage Tips
- Use descriptive abstracts (>10 chars) that capture the "why" not just the "what"
- Set a meaningful category to improve dedup and retrieval
- Convert relative dates to absolute dates before storing
```

This is a text-only change in the `getPromptContent()` function. Zero code logic changes.

## 3. Server Soft Checks (Store Warnings)

### Problem

Even with prompt guidance, agents sometimes store low-quality memories. A server-side safety net catches obvious issues.

### Implementation

In the `store` handler in `http/server.py`, check conditions BEFORE calling `orchestrator.add()`. Return warnings in the response (HTTP 200, never reject):

| Condition | Warning Key | Message |
|-----------|-------------|---------|
| `len(abstract.strip()) < 10` | `abstract_too_short` | `"Memory abstract should be at least 10 characters for useful retrieval"` |
| Abstract is >80% code lines | `code_snippet_detected` | `"Consider storing a description of the code pattern rather than raw code"` |

Code detection heuristic (simple, no dependencies):

```python
def _looks_like_code(text: str) -> bool:
    """Return True if >80% of non-empty lines look like code."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return False
    code_patterns = re.compile(
        r"^\s*(def |class |import |from |if |for |while |return |const |let |var |function |\{|\}|//|#!)"
    )
    code_lines = sum(1 for ln in lines if code_patterns.match(ln))
    return code_lines / len(lines) > 0.8
```

Response shape:

```json
{
  "uri": "...",
  "context_type": "memory",
  "category": "...",
  "warnings": [
    {"key": "abstract_too_short", "message": "Memory abstract should be at least 10 characters for useful retrieval"}
  ]
}
```

No warnings → `warnings` key omitted. Warnings are advisory — the memory is still stored.

Note: the near-duplicate warning (>0.95 similarity) is NOT included here because dedup already handles this with its merge/skip logic. Adding a pre-check would double the embedding cost.

## Files Changed

### Modified Files (4 files)
- `src/opencortex/orchestrator.py` — new `memory_index()` method (~30 lines)
- `src/opencortex/http/server.py` — new `GET /api/v1/memory/index` endpoint + store warning checks (~50 lines)
- `plugins/opencortex-memory/lib/mcp-server.mjs` — new `memory_index` tool + usage-guide prompt update (~40 lines)
- `src/opencortex/http/models.py` — optional: index response model (or just return dict)

### No Changes To
- `retrieve/types.py` — ContextType enum unchanged
- `hierarchical_retriever.py` — retrieval pipeline unchanged
- `context/manager.py` — recall formatting unchanged
- `collection_schemas.py` — Qdrant schema unchanged
- No migration files needed
