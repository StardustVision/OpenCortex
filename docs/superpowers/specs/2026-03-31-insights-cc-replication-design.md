# OpenCortex Insights — CC-Equivalent Replication Spec

> Date: 2026-03-31
> Status: Draft
> Reference: `docs/claude-code-insights-internals-fork.md` + CC source at `claude-code-fork/src/commands/insights.ts`

## 1. Goal

Replicate Claude Code's `/insights` feature inside OpenCortex, fully matching CC's data depth, analysis quality, and visual richness while leveraging OC's multi-tenant API architecture and React frontend.

**Out of scope**: CC's ant-only sections (`cc_team_improvements`, `model_behavior_improvements`), S3 upload, homespace remote collection. These are Anthropic-internal features.

---

## 2. Architecture Overview

```
┌─────────── Data Collection ───────────┐
│  Observer (enhanced)                   │
│    ↓ tool_calls with full detail       │
│  TraceSplitter → TraceStore (Qdrant)   │
└────────────────────────────────────────┘
              ↓
┌─────────── Analysis Pipeline ─────────┐
│  Phase 1: Load traces from TraceStore  │
│  Phase 2: SessionMetaExtractor (0 LLM) │
│  Phase 3: Dedup + filter               │
│  Phase 4: Facet extraction (N LLM)     │
│  Phase 5: Aggregation → AggregatedData │
│  Phase 6: 7 sections parallel (7 LLM)  │
│  Phase 7: at_a_glance serial (1 LLM)   │
└────────────────────────────────────────┘
              ↓
┌─────────── Cache ─────────────────────┐
│  CortexFS: meta/{sid}.json             │
│  CortexFS: facets/{sid}.json           │
└────────────────────────────────────────┘
              ↓
┌─────────── Output ────────────────────┐
│  CortexFS: reports/{date}/weekly.json  │
│  React frontend: Insights page         │
│  CLI: oc-cli insights-generate         │
│  MCP skill: /insights-generate         │
└────────────────────────────────────────┘
```

---

## 3. Layer 1 — Observer Enhancement

### 3.1 ToolCallDetail

Extend the tool_calls structure recorded by Observer and passed through ContextManager.

```python
# In observer.py / types.py
class ToolCallDetail(TypedDict, total=False):
    name: str                     # Tool name (Read, Edit, Bash, Write, Agent...)
    summary: str                  # Existing field
    input_params: Dict[str, Any]  # NEW: tool input (file_path, command, old_string/new_string, etc.)
    output_preview: str           # NEW: first 500 chars of result
    is_error: bool                # NEW: whether tool returned an error
    error_text: str               # NEW: error message (first 200 chars)
    duration_ms: Optional[int]    # NEW: tool execution time
```

**Backward compatibility**: All new fields are optional. Old clients that only send `{name, summary}` continue to work unchanged.

**Change points**:
- `ContextManager._commit()`: pass enriched tool_calls to `observer.record_batch()`
- MCP plugin `add_message` tool: accept and forward the extended fields
- `Turn.tool_calls`: already `List[Dict[str, Any]]`, no type change needed

### 3.2 SessionMetaExtractor

New file: `src/opencortex/insights/extractor.py`

Pure-code extraction from Trace turns. Zero LLM calls.

```python
EXTENSION_TO_LANGUAGE: Dict[str, str] = {
    ".ts": "TypeScript", ".tsx": "TypeScript",
    ".js": "JavaScript", ".jsx": "JavaScript",
    ".py": "Python", ".rb": "Ruby", ".go": "Go",
    ".rs": "Rust", ".java": "Java", ".md": "Markdown",
    ".json": "JSON", ".yaml": "YAML", ".yml": "YAML",
    ".sh": "Shell", ".css": "CSS", ".html": "HTML",
}

ERROR_CATEGORIES = [
    ("exit code",                           "Command Failed"),
    ("rejected", "doesn't want",            "User Rejected"),
    ("string to replace not found", "no changes", "Edit Failed"),
    ("modified since read",                 "File Changed"),
    ("exceeds maximum", "too large",        "File Too Large"),
    ("file not found", "does not exist",    "File Not Found"),
]
# Default: "Other"
```

**`SessionMetaExtractor.extract(trace: Trace) -> SessionMeta`**:

For each Turn in `trace.turns`:

| Extraction | Logic | CC Reference |
|------------|-------|--------------|
| tool_counts | `tool_calls[].name` → counter | Lines 538-551 |
| languages | `input_params.file_path` extension → `EXTENSION_TO_LANGUAGE` | Lines 555-564 |
| files_modified | unique file_paths from Edit/Write tools | Lines 562-564 |
| lines_added/removed | Edit: diff old_string vs new_string. Write: count newlines + 1 | Lines 567-581 |
| git_commits/pushes | Bash tool `input_params.command` contains `git commit`/`git push` | Lines 584-586 |
| input/output_tokens | `turn.token_count` (or estimate from content length) | Lines 530-532 |
| tool_errors | `tool_calls[].is_error` → classify by `error_text` keywords | Lines 645-680 |
| user_response_times | Gap between assistant turn end → next user turn start. Only 2s < gap < 3600s | Lines 626-636 |
| message_hours | `turn.timestamp` → `.hour` (0-23) | Lines 613-624 |
| user_message_timestamps | ISO timestamps of user turns (for multi-clauding) | Lines 613-624 |
| user_interruptions | Check turn_status == INTERRUPTED | Lines 685-701 |
| uses_agent | tool name == "Agent" | Special tool detection |
| uses_mcp | tool name starts with "mcp__" | Special tool detection |
| uses_web_search | tool name == "WebSearch" | Special tool detection |
| uses_web_fetch | tool name == "WebFetch" | Special tool detection |
| first_prompt | First user turn `prompt_text`, truncated to 200 chars | |

### 3.3 SessionMeta Type

```python
@dataclass
class SessionMeta:
    session_id: str
    tenant_id: str
    user_id: str
    project_path: str
    start_time: str                        # ISO
    duration_minutes: float
    user_message_count: int
    assistant_message_count: int
    tool_counts: Dict[str, int]            # {Read: 50, Edit: 30, Bash: 20, ...}
    languages: Dict[str, int]              # {TypeScript: 20, Python: 5, ...}
    git_commits: int
    git_pushes: int
    input_tokens: int
    output_tokens: int
    first_prompt: str
    summary: Optional[str] = None
    user_interruptions: int = 0
    user_response_times: List[float] = field(default_factory=list)  # seconds
    tool_errors: int = 0
    tool_error_categories: Dict[str, int] = field(default_factory=dict)
    uses_agent: bool = False
    uses_mcp: bool = False
    uses_web_search: bool = False
    uses_web_fetch: bool = False
    lines_added: int = 0
    lines_removed: int = 0
    files_modified: int = 0
    message_hours: List[int] = field(default_factory=list)           # 0-23
    user_message_timestamps: List[str] = field(default_factory=list) # ISO strings
```

30 fields. Matches CC's `SessionMeta` exactly.

---

## 4. Layer 2 — Cache + Facet Extraction

### 4.1 InsightsCache

New file: `src/opencortex/insights/cache.py`

Storage: CortexFS at `opencortex://{tid}/{uid}/insights/cache/`

```python
class InsightsCache:
    def __init__(self, cortex_fs: CortexFS):
        self._fs = cortex_fs

    async def get_meta(self, tid, uid, session_id) -> Optional[SessionMeta]:
        """Read cached SessionMeta."""
        uri = f"opencortex://{tid}/{uid}/insights/cache/meta/{session_id}.json"
        content = await self._fs.read(uri)
        return _deserialize_meta(content) if content else None

    async def put_meta(self, tid, uid, session_id, meta: SessionMeta):
        """Write SessionMeta cache."""
        uri = f"opencortex://{tid}/{uid}/insights/cache/meta/{session_id}.json"
        await self._fs.write(uri, json.dumps(asdict(meta)))

    async def get_facet(self, tid, uid, session_id) -> Optional[SessionFacet]:
        """Read cached SessionFacet."""
        uri = f"opencortex://{tid}/{uid}/insights/cache/facets/{session_id}.json"
        content = await self._fs.read(uri)
        if not content:
            return None
        data = json.loads(content)
        if not _validate_facet(data):
            await self._fs.delete(uri)  # Corrupted, delete
            return None
        return _deserialize_facet(data)

    async def put_facet(self, tid, uid, session_id, facet: SessionFacet):
        """Write SessionFacet cache."""
        uri = f"opencortex://{tid}/{uid}/insights/cache/facets/{session_id}.json"
        await self._fs.write(uri, json.dumps(asdict(facet)))

    async def batch_check(self, tid, uid, session_ids: List[str], kind: str) -> Dict[str, bool]:
        """Check which sessions have cached data. kind = 'meta' or 'facets'."""
        results = {}
        for batch in chunked(session_ids, 50):
            checks = await asyncio.gather(*[
                self._exists(f"opencortex://{tid}/{uid}/insights/cache/{kind}/{sid}.json")
                for sid in batch
            ])
            for sid, exists in zip(batch, checks):
                results[sid] = exists
        return results
```

**Validation** (对标 CC 的 `isValidSessionFacets`):

```python
REQUIRED_FACET_FIELDS = {
    "session_id", "underlying_goal", "goal_categories",
    "outcome", "brief_summary",
}

def _validate_facet(data: dict) -> bool:
    return REQUIRED_FACET_FIELDS.issubset(data.keys())
```

### 4.2 SessionFacet Type (Rewritten)

对标 CC 的 `SessionFacets`，`goal_categories` 改为 `Dict[str, int]`（非 `List[str]`）：

```python
@dataclass
class SessionFacet:
    session_id: str
    underlying_goal: str
    goal_categories: Dict[str, int]           # CC: {"implement_feature": 2, "fix_bug": 1}
    outcome: str                               # 5 values: fully/mostly/partially/not_achieved, unclear
    user_satisfaction_counts: Dict[str, int]   # {"satisfied": 2, "happy": 1}
    claude_helpfulness: str                    # 5 levels: unhelpful → essential
    session_type: str                          # 5 types: single_task, multi_task, etc.
    friction_counts: Dict[str, int] = field(default_factory=dict)
    friction_detail: str = ""                  # One sentence
    primary_success: str = "none"              # 7 values: none, fast_accurate_search, etc.
    brief_summary: str = ""
    user_instructions_to_claude: List[str] = field(default_factory=list)  # CC-exclusive
```

### 4.3 Facet Extraction Prompt (CC-Equivalent)

```python
FACET_EXTRACTION_PROMPT = """\
Analyze this Claude Code session and extract structured facets.

CRITICAL GUIDELINES:

1. **goal_categories**: Count ONLY what the USER explicitly asked for.
   - DO NOT count Claude's autonomous codebase exploration
   - DO NOT count work Claude decided to do on its own
   - ONLY count when user says "can you...", "please...", "I need...", "let's..."

2. **user_satisfaction_counts**: Base ONLY on explicit user signals.
   - "Yay!", "great!", "perfect!" → happy
   - "thanks", "looks good", "that works" → satisfied
   - "ok, now let's..." (continuing without complaint) → likely_satisfied
   - "that's not right", "try again" → dissatisfied
   - "this is broken", "I give up" → frustrated

3. **friction_counts**: Be specific about what went wrong.
   - misunderstood_request: Claude interpreted incorrectly
   - wrong_approach: Right goal, wrong solution method
   - buggy_code: Code didn't work correctly
   - user_rejected_action: User said no/stop to a tool call
   - excessive_changes: Over-engineered or changed too much

4. If very short or just warmup, use warmup_minimal for goal_category

SESSION:
{transcript}

RESPOND WITH ONLY A VALID JSON OBJECT matching this schema:
{{
  "underlying_goal": "What the user fundamentally wanted to achieve",
  "goal_categories": {{"category_name": count, ...}},
  "outcome": "fully_achieved|mostly_achieved|partially_achieved|not_achieved|unclear_from_transcript",
  "user_satisfaction_counts": {{"level": count, ...}},
  "claude_helpfulness": "unhelpful|slightly_helpful|moderately_helpful|very_helpful|essential",
  "session_type": "single_task|multi_task|iterative_refinement|exploration|quick_question",
  "friction_counts": {{"friction_type": count, ...}},
  "friction_detail": "One sentence describing friction or empty",
  "primary_success": "none|fast_accurate_search|correct_code_edits|good_explanations|proactive_help|multi_file_changes|good_debugging",
  "brief_summary": "One sentence: what user wanted and whether they got it",
  "user_instructions_to_claude": ["instruction1", "instruction2"]
}}
"""
```

### 4.4 Transcript Formatting

```python
def format_transcript_for_facets(trace: Trace, meta: SessionMeta) -> str:
    """Format trace into CC-style transcript."""
    header = (
        f"Session: {trace.session_id[:8]}\n"
        f"Date: {meta.start_time}\n"
        f"Project: {meta.project_path}\n"
        f"Duration: {meta.duration_minutes} min\n\n"
    )
    lines = []
    for turn in trace.turns:
        if turn.prompt_text:
            lines.append(f"[User]: {turn.prompt_text[:500]}")
        if turn.final_text:
            lines.append(f"[Assistant]: {turn.final_text[:300]}")
        for tc in turn.tool_calls:
            lines.append(f"[Tool: {tc.get('name', 'unknown')}]")
    return header + "\n".join(lines)
```

### 4.5 Long Transcript Handling

CC thresholds: 30k chars total, 25k per chunk.

```python
TRANSCRIPT_THRESHOLD = 30000   # chars
CHUNK_SIZE = 25000             # chars per chunk

CHUNK_SUMMARY_PROMPT = """\
Summarize this portion of a Claude Code session transcript. Focus on:
1. What the user asked for
2. What Claude did (tools used, files modified)
3. Any friction or issues
4. The outcome

Keep it concise - 3-5 sentences. Preserve specific details like file names, error messages, and user feedback.

TRANSCRIPT CHUNK:
{chunk}
"""

async def format_transcript_with_summarization(
    trace: Trace, meta: SessionMeta, llm
) -> str:
    """Format transcript, summarizing if over threshold."""
    raw = format_transcript_for_facets(trace, meta)
    if len(raw) <= TRANSCRIPT_THRESHOLD:
        return raw

    chunks = [raw[i:i+CHUNK_SIZE] for i in range(0, len(raw), CHUNK_SIZE)]
    summaries = await asyncio.gather(*[
        llm.generate(CHUNK_SUMMARY_PROMPT.format(chunk=c)) for c in chunks
    ])

    header = (
        f"Session: {trace.session_id[:8]}\n"
        f"Date: {meta.start_time}\n"
        f"Project: {meta.project_path}\n"
        f"Duration: {meta.duration_minutes} min\n"
        f"[Long session - {len(chunks)} parts summarized]\n\n"
    )
    return header + "\n\n---\n\n".join(summaries)
```

### 4.6 Concurrent Facet Extraction

```python
MAX_FACET_EXTRACTIONS = 50
FACET_CONCURRENCY = 50

async def extract_facets_batch(
    sessions: List[Tuple[Trace, SessionMeta]],
    cache: InsightsCache,
    llm,
    tid: str, uid: str,
) -> List[SessionFacet]:
    """Extract facets with caching and concurrency. CC-equivalent."""
    # Phase 1: load all cached facets
    session_ids = [meta.session_id for _, meta in sessions]
    cached_map = await cache.batch_check(tid, uid, session_ids, "facets")

    cached_facets = {}
    uncached = []
    for trace, meta in sessions:
        if cached_map.get(meta.session_id):
            facet = await cache.get_facet(tid, uid, meta.session_id)
            if facet:
                cached_facets[meta.session_id] = facet
                continue
        uncached.append((trace, meta))

    # Phase 2: extract uncached (max MAX_FACET_EXTRACTIONS, CONCURRENCY parallel)
    to_extract = uncached[:MAX_FACET_EXTRACTIONS]
    semaphore = asyncio.Semaphore(FACET_CONCURRENCY)

    async def _extract_one(trace, meta):
        async with semaphore:
            transcript = await format_transcript_with_summarization(trace, meta, llm)
            prompt = FACET_EXTRACTION_PROMPT.format(transcript=transcript)
            response = await llm.generate_async(prompt, max_tokens=4096)
            data = json.loads(response)
            facet = SessionFacet(
                session_id=meta.session_id,
                underlying_goal=data.get("underlying_goal", "Unknown"),
                goal_categories=data.get("goal_categories", {}),
                outcome=data.get("outcome", "unclear_from_transcript"),
                user_satisfaction_counts=data.get("user_satisfaction_counts", {}),
                claude_helpfulness=data.get("claude_helpfulness", "moderately_helpful"),
                session_type=data.get("session_type", "unknown"),
                friction_counts=data.get("friction_counts", {}),
                friction_detail=data.get("friction_detail", ""),
                primary_success=data.get("primary_success", "none"),
                brief_summary=data.get("brief_summary", ""),
                user_instructions_to_claude=data.get("user_instructions_to_claude", []),
            )
            await cache.put_facet(tid, uid, meta.session_id, facet)
            return facet

    extracted = await asyncio.gather(*[
        _extract_one(t, m) for t, m in to_extract
    ], return_exceptions=True)

    # Combine
    all_facets = []
    for trace, meta in sessions:
        sid = meta.session_id
        if sid in cached_facets:
            all_facets.append(cached_facets[sid])
        else:
            match = [f for f in extracted if isinstance(f, SessionFacet) and f.session_id == sid]
            if match:
                all_facets.append(match[0])

    return all_facets
```

---

## 5. Layer 3 — Aggregation + Sections

### 5.1 AggregatedData

```python
@dataclass
class AggregatedData:
    total_sessions: int
    total_sessions_scanned: int
    sessions_with_facets: int
    date_range: Dict[str, str]                # {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}
    total_messages: int
    total_duration_hours: float
    total_input_tokens: int
    total_output_tokens: int
    tool_counts: Dict[str, int]
    languages: Dict[str, int]
    git_commits: int
    git_pushes: int
    projects: Dict[str, int]                  # project_path → session_count
    goal_categories: Dict[str, int]           # from facets
    outcomes: Dict[str, int]
    satisfaction: Dict[str, int]
    helpfulness: Dict[str, int]
    session_types: Dict[str, int]
    friction: Dict[str, int]
    success: Dict[str, int]
    session_summaries: List[Dict[str, str]]   # [{id, date, summary, goal}]
    total_interruptions: int
    total_tool_errors: int
    tool_error_categories: Dict[str, int]
    user_response_times: List[float]
    median_response_time: float
    avg_response_time: float
    sessions_using_agent: int
    sessions_using_mcp: int
    sessions_using_web_search: int
    sessions_using_web_fetch: int
    total_lines_added: int
    total_lines_removed: int
    total_files_modified: int
    days_active: int
    messages_per_day: float
    message_hours: List[int]                  # all user message hours (0-23)
    multi_clauding: Dict[str, int]            # {overlap_events, sessions_involved, user_messages_during}
```

40+ fields. Matches CC's `AggregatedData`.

### 5.2 Multi-Clauding Detection

Port CC's sliding window algorithm (30-minute window):

```python
OVERLAP_WINDOW_MS = 30 * 60 * 1000  # 30 minutes

def detect_multi_clauding(
    sessions: List[SessionMeta],
) -> Dict[str, int]:
    """Sliding window detection of concurrent session usage.

    Detects pattern: session1 message → session2 message → session1 message
    within a 30-minute window.
    """
    all_messages = []
    for meta in sessions:
        for ts_str in meta.user_message_timestamps:
            try:
                ts = datetime.fromisoformat(ts_str).timestamp() * 1000
                all_messages.append({"ts": ts, "session_id": meta.session_id})
            except ValueError:
                continue

    all_messages.sort(key=lambda m: m["ts"])

    session_pairs = set()
    messages_during = set()
    window_start = 0
    session_last_index: Dict[str, int] = {}

    for i, msg in enumerate(all_messages):
        # Shrink window
        while window_start < i and msg["ts"] - all_messages[window_start]["ts"] > OVERLAP_WINDOW_MS:
            expiring = all_messages[window_start]
            if session_last_index.get(expiring["session_id"]) == window_start:
                del session_last_index[expiring["session_id"]]
            window_start += 1

        # Check for interleaving
        prev_idx = session_last_index.get(msg["session_id"])
        if prev_idx is not None:
            for j in range(prev_idx + 1, i):
                between = all_messages[j]
                if between["session_id"] != msg["session_id"]:
                    pair = tuple(sorted([msg["session_id"], between["session_id"]]))
                    session_pairs.add(pair)
                    messages_during.add(f"{all_messages[prev_idx]['ts']}:{msg['session_id']}")
                    messages_during.add(f"{between['ts']}:{between['session_id']}")
                    messages_during.add(f"{msg['ts']}:{msg['session_id']}")
                    break

        session_last_index[msg["session_id"]] = i

    involved = set()
    for s1, s2 in session_pairs:
        involved.add(s1)
        involved.add(s2)

    return {
        "overlap_events": len(session_pairs),
        "sessions_involved": len(involved),
        "user_messages_during": len(messages_during),
    }
```

### 5.3 Deduplication + Filtering

```python
MAX_SESSIONS_TO_LOAD = 200
MIN_USER_MESSAGES = 2
MIN_DURATION_MINUTES = 1

def deduplicate_sessions(
    entries: List[Tuple[Trace, SessionMeta]],
) -> List[Tuple[Trace, SessionMeta]]:
    """Keep branch with most user messages per session_id (tie-break: duration)."""
    best: Dict[str, Tuple[Trace, SessionMeta]] = {}
    for trace, meta in entries:
        sid = meta.session_id
        existing = best.get(sid)
        if (
            not existing
            or meta.user_message_count > existing[1].user_message_count
            or (
                meta.user_message_count == existing[1].user_message_count
                and meta.duration_minutes > existing[1].duration_minutes
            )
        ):
            best[sid] = (trace, meta)
    return list(best.values())

def filter_substantive(entries: List[Tuple[Trace, SessionMeta]]) -> List[Tuple[Trace, SessionMeta]]:
    """Remove non-substantive sessions."""
    return [
        (t, m) for t, m in entries
        if m.user_message_count >= MIN_USER_MESSAGES
        and m.duration_minutes >= MIN_DURATION_MINUTES
    ]

def filter_warmup_only(
    entries: List[Tuple[Trace, SessionMeta]],
    facets: List[SessionFacet],
) -> Tuple[List[Tuple[Trace, SessionMeta]], List[SessionFacet]]:
    """Remove sessions where only goal is warmup_minimal."""
    facet_map = {f.session_id: f for f in facets}
    filtered_entries = []
    filtered_facets = []
    for trace, meta in entries:
        facet = facet_map.get(meta.session_id)
        if facet and set(facet.goal_categories.keys()) == {"warmup_minimal"}:
            continue
        filtered_entries.append((trace, meta))
        if facet:
            filtered_facets.append(facet)
    return filtered_entries, filtered_facets
```

### 5.4 DataContext Construction

```python
def build_data_context(
    agg: AggregatedData,
    facets: List[SessionFacet],
) -> str:
    """Build CC-equivalent fullContext string (~50KB)."""
    json_part = json.dumps({
        "sessions": agg.total_sessions,
        "analyzed": agg.sessions_with_facets,
        "date_range": agg.date_range,
        "messages": agg.total_messages,
        "hours": round(agg.total_duration_hours),
        "commits": agg.git_commits,
        "top_tools": sorted(agg.tool_counts.items(), key=lambda x: -x[1])[:8],
        "top_goals": sorted(agg.goal_categories.items(), key=lambda x: -x[1])[:8],
        "outcomes": agg.outcomes,
        "satisfaction": agg.satisfaction,
        "friction": agg.friction,
        "success": agg.success,
        "languages": agg.languages,
    }, indent=2)

    summaries = "\n".join(
        f"- {s['summary']} ({s.get('goal', '')})"
        for s in agg.session_summaries[:50]
    )

    friction_details = "\n".join(
        f"- {f.friction_detail}"
        for f in facets[:20]
        if f.friction_detail
    )

    instructions = set()
    for f in facets:
        instructions.update(f.user_instructions_to_claude)
    instr_text = "\n".join(f"- {i}" for i in list(instructions)[:15]) or "None captured"

    return (
        f"{json_part}\n\n"
        f"SESSION SUMMARIES:\n{summaries}\n\n"
        f"FRICTION DETAILS:\n{friction_details}\n\n"
        f"USER INSTRUCTIONS TO CLAUDE:\n{instr_text}"
    )
```

### 5.5 Section Prompts (All 8)

```python
# --- project_areas ---
PROJECT_AREAS_PROMPT = """\
Analyze this Claude Code usage data and identify project areas.

RESPOND WITH ONLY A VALID JSON OBJECT:
{{
  "areas": [
    {{"name": "Area name", "session_count": N, "description": "2-3 sentences about what was worked on."}}
  ]
}}

Include 4-5 areas. Skip internal operations.

DATA:
{data_context}
"""

# --- interaction_style ---
INTERACTION_STYLE_PROMPT = """\
Analyze this Claude Code usage data and describe the user's interaction style.

RESPOND WITH ONLY A VALID JSON OBJECT:
{{
  "narrative": "2-3 paragraphs analyzing HOW the user interacts. Use second person 'you'. Describe patterns: iterate quickly vs detailed upfront specs? Interrupt often or let Claude run? Include specific examples. Use **bold** for key insights.",
  "key_pattern": "One sentence summary of most distinctive interaction style"
}}

DATA:
{data_context}
"""

# --- what_works ---
WHAT_WORKS_PROMPT = """\
Analyze this Claude Code usage data and identify what's working well. Use second person ("you").

RESPOND WITH ONLY A VALID JSON OBJECT:
{{
  "intro": "1 sentence of context",
  "impressive_workflows": [
    {{"title": "Short title (3-6 words)", "description": "2-3 sentences. Use 'you' not 'the user'."}}
  ]
}}

Include 3 impressive workflows.

DATA:
{data_context}
"""

# --- friction_analysis ---
FRICTION_ANALYSIS_PROMPT = """\
Analyze this Claude Code usage data and identify friction points. Use second person ("you").

RESPOND WITH ONLY A VALID JSON OBJECT:
{{
  "intro": "1 sentence summarizing friction patterns",
  "categories": [
    {{"category": "Concrete category name", "description": "1-2 sentences. Use 'you' not 'the user'.", "examples": ["Specific example with consequence", "Another example"]}}
  ]
}}

Include 3 friction categories with 2 examples each.

DATA:
{data_context}
"""

# --- suggestions (with OC FEATURES REFERENCE) ---
SUGGESTIONS_PROMPT = """\
Analyze this usage data and suggest improvements.

## OC FEATURES REFERENCE (pick from these for features_to_try):
1. **Memory Feedback** (store + feedback): Reinforce useful memories with +1 reward, penalize irrelevant ones with -1. Adjusts future retrieval ranking through reinforcement learning.
   - How to use: After recalling a useful memory, call `feedback(uri, +1.0)`. For irrelevant recalls, `feedback(uri, -1.0)`.
   - Good for: Training your memory system to surface the right context automatically.

2. **Knowledge Pipeline**: Automatic knowledge extraction from session traces via Observer → TraceSplitter → Archivist → Sandbox → KnowledgeStore.
   - How to use: Enable `trace_splitter: true` in server config. Knowledge candidates appear for review.
   - Good for: Building an approved knowledge base from your work patterns, error fixes, and decisions.

3. **Batch Import** (batch_store): Import multiple documents, scan results, or file trees in one call.
   - How to use: `batch_store(items=[...], source_path="/project")` with file_path metadata for directory tree.
   - Good for: Onboarding project documentation, importing existing notes, bulk ingestion.

4. **Semantic Search** (recall): Intent-aware retrieval that analyzes your query to determine search strategy.
   - How to use: `recall(query="how did we handle auth?")` — system auto-detects intent and adjusts top_k/detail.
   - Good for: Finding past decisions, error fixes, and context without remembering exact terms.

5. **Memory Decay**: Time-based reward decay so only consistently valuable memories retain high ranking.
   - How to use: Call `decay()` periodically. Memories you access frequently resist decay naturally.
   - Good for: Keeping your memory space clean without manual pruning.

RESPOND WITH ONLY A VALID JSON OBJECT:
{{
  "features_to_try": [
    {{"feature": "Feature name from OC FEATURES REFERENCE above", "one_liner": "What it does", "why_for_you": "Why this would help YOU based on your sessions", "example_code": "Actual command or config to copy"}}
  ],
  "usage_patterns": [
    {{"title": "Short title", "suggestion": "1-2 sentence summary", "detail": "3-4 sentences explaining how this applies to YOUR work", "copyable_prompt": "A specific prompt to copy and try"}}
  ]
}}

IMPORTANT for features_to_try: Pick 2-3 from the OC FEATURES REFERENCE above.

DATA:
{data_context}
"""

# --- on_the_horizon ---
ON_THE_HORIZON_PROMPT = """\
Analyze this usage data and identify future opportunities.

RESPOND WITH ONLY A VALID JSON OBJECT:
{{
  "intro": "1 sentence about evolving AI-assisted development",
  "opportunities": [
    {{"title": "Short title (4-8 words)", "whats_possible": "2-3 ambitious sentences about autonomous workflows", "how_to_try": "1-2 sentences mentioning relevant tooling", "copyable_prompt": "Detailed prompt to try"}}
  ]
}}

Include 3 opportunities. Think BIG - autonomous workflows, parallel agents, iterating against tests.

DATA:
{data_context}
"""

# --- fun_ending ---
FUN_ENDING_PROMPT = """\
Analyze this usage data and find a memorable moment.

RESPOND WITH ONLY A VALID JSON OBJECT:
{{
  "headline": "A memorable QUALITATIVE moment from the transcripts - not a statistic. Something human, funny, or surprising.",
  "detail": "Brief context about when/where this happened"
}}

Find something genuinely interesting or amusing from the session summaries.

DATA:
{data_context}
"""

# --- at_a_glance (SERIAL — depends on all other sections) ---
AT_A_GLANCE_PROMPT = """\
You're writing an "At a Glance" summary for an insights report. The goal is to help users understand their usage and improve how they use their AI tools.

Use this 4-part structure:

1. **What's working** - What is the user's unique style of interacting and what are some impactful things they've done? Keep it high level. Don't be fluffy or overly complimentary. Don't focus on tool calls.

2. **What's hindering you** - Split into (a) Claude's fault (misunderstandings, wrong approaches, bugs) and (b) user-side friction (not providing enough context, environment issues). Be honest but constructive.

3. **Quick wins to try** - Specific features they could try from the examples below, or a compelling workflow technique.

4. **Ambitious workflows for better models** - As models improve over the next 3-6 months, what should they prepare for? What workflows that seem impossible now will become possible?

Keep each section to 2-3 not-too-long sentences. Don't mention specific numerical stats or underlined_categories. Use a coaching tone.

RESPOND WITH ONLY A VALID JSON OBJECT:
{{
  "whats_working": "(refer to instructions above)",
  "whats_hindering": "(refer to instructions above)",
  "quick_wins": "(refer to instructions above)",
  "ambitious_workflows": "(refer to instructions above)"
}}

SESSION DATA:
{full_context}

## Project Areas (what user works on)
{project_areas_text}

## Big Wins (impressive accomplishments)
{big_wins_text}

## Friction Categories (where things go wrong)
{friction_text}

## Features to Try
{features_text}

## Usage Patterns to Adopt
{patterns_text}

## On the Horizon (ambitious workflows for better models)
{horizon_text}
"""
```

### 5.6 Parallel Section Execution

```python
PARALLEL_SECTIONS = [
    ("project_areas",     PROJECT_AREAS_PROMPT),
    ("interaction_style", INTERACTION_STYLE_PROMPT),
    ("what_works",        WHAT_WORKS_PROMPT),
    ("friction_analysis", FRICTION_ANALYSIS_PROMPT),
    ("suggestions",       SUGGESTIONS_PROMPT),
    ("on_the_horizon",    ON_THE_HORIZON_PROMPT),
    ("fun_ending",        FUN_ENDING_PROMPT),
]

async def generate_parallel_insights(
    data_context: str, llm, max_tokens: int = 8192
) -> Dict[str, Any]:
    """Stage 1: 7 sections in parallel. Stage 2: at_a_glance serial."""

    # Stage 1: parallel
    async def _gen(name, prompt_template):
        prompt = prompt_template.format(data_context=data_context)
        response = await llm.generate_async(prompt, max_tokens=max_tokens)
        return name, json.loads(response)

    results_list = await asyncio.gather(*[
        _gen(name, tmpl) for name, tmpl in PARALLEL_SECTIONS
    ], return_exceptions=True)

    results = {}
    for r in results_list:
        if isinstance(r, tuple):
            results[r[0]] = r[1]

    # Stage 2: at_a_glance (depends on all sections)
    pa = results.get("project_areas", {})
    ww = results.get("what_works", {})
    fa = results.get("friction_analysis", {})
    sg = results.get("suggestions", {})
    oh = results.get("on_the_horizon", {})

    project_areas_text = "\n".join(
        f"- {a['name']}: {a.get('description', '')}"
        for a in pa.get("areas", [])
    )
    big_wins_text = "\n".join(
        f"- {w['title']}: {w.get('description', '')}"
        for w in ww.get("impressive_workflows", [])
    )
    friction_text = "\n".join(
        f"- {c['category']}: {c.get('description', '')}"
        for c in fa.get("categories", [])
    )
    features_text = "\n".join(
        f"- {f['feature']}: {f.get('one_liner', '')}"
        for f in sg.get("features_to_try", [])
    )
    patterns_text = "\n".join(
        f"- {p['title']}: {p.get('suggestion', '')}"
        for p in sg.get("usage_patterns", [])
    )
    horizon_text = "\n".join(
        f"- {o['title']}: {o.get('whats_possible', '')}"
        for o in oh.get("opportunities", [])
    )

    at_a_glance_prompt = AT_A_GLANCE_PROMPT.format(
        full_context=data_context,
        project_areas_text=project_areas_text or "None",
        big_wins_text=big_wins_text or "None",
        friction_text=friction_text or "None",
        features_text=features_text or "None",
        patterns_text=patterns_text or "None",
        horizon_text=horizon_text or "None",
    )
    at_a_glance_response = await llm.generate_async(at_a_glance_prompt, max_tokens=max_tokens)
    results["at_a_glance"] = json.loads(at_a_glance_response)

    return results
```

### 5.7 Label Map

```python
# src/opencortex/insights/labels.py

LABEL_MAP: Dict[str, str] = {
    # Goal categories
    "debug_investigate": "Debug/Investigate",
    "implement_feature": "Implement Feature",
    "fix_bug": "Fix Bug",
    "write_script_tool": "Write Script/Tool",
    "refactor_code": "Refactor Code",
    "configure_system": "Configure System",
    "create_pr_commit": "Create PR/Commit",
    "analyze_data": "Analyze Data",
    "understand_codebase": "Understand Codebase",
    "write_tests": "Write Tests",
    "write_docs": "Write Docs",
    "deploy_infra": "Deploy/Infra",
    "warmup_minimal": "Cache Warmup",
    # Success factors
    "fast_accurate_search": "Fast/Accurate Search",
    "correct_code_edits": "Correct Code Edits",
    "good_explanations": "Good Explanations",
    "proactive_help": "Proactive Help",
    "multi_file_changes": "Multi-file Changes",
    "handled_complexity": "Multi-file Changes",
    "good_debugging": "Good Debugging",
    # Friction types
    "misunderstood_request": "Misunderstood Request",
    "wrong_approach": "Wrong Approach",
    "buggy_code": "Buggy Code",
    "user_rejected_action": "User Rejected Action",
    "claude_got_blocked": "Claude Got Blocked",
    "user_stopped_early": "User Stopped Early",
    "wrong_file_or_location": "Wrong File/Location",
    "excessive_changes": "Excessive Changes",
    "slow_or_verbose": "Slow/Verbose",
    "tool_failed": "Tool Failed",
    "user_unclear": "User Unclear",
    "external_issue": "External Issue",
    # Satisfaction
    "frustrated": "Frustrated",
    "dissatisfied": "Dissatisfied",
    "likely_satisfied": "Likely Satisfied",
    "satisfied": "Satisfied",
    "happy": "Happy",
    "unsure": "Unsure",
    "neutral": "Neutral",
    "delighted": "Delighted",
    # Session types
    "single_task": "Single Task",
    "multi_task": "Multi Task",
    "iterative_refinement": "Iterative Refinement",
    "exploration": "Exploration",
    "quick_question": "Quick Question",
    # Outcomes
    "fully_achieved": "Fully Achieved",
    "mostly_achieved": "Mostly Achieved",
    "partially_achieved": "Partially Achieved",
    "not_achieved": "Not Achieved",
    "unclear_from_transcript": "Unclear",
    # Helpfulness
    "unhelpful": "Unhelpful",
    "slightly_helpful": "Slightly Helpful",
    "moderately_helpful": "Moderately Helpful",
    "very_helpful": "Very Helpful",
    "essential": "Essential",
}

def label(key: str) -> str:
    return LABEL_MAP.get(key, key.replace("_", " ").title())
```

### 5.8 Constants

```python
# src/opencortex/insights/constants.py

# Session loading
MAX_SESSIONS_TO_LOAD = 200
MAX_FACET_EXTRACTIONS = 50
FACET_CONCURRENCY = 50
META_BATCH_SIZE = 50

# Transcript processing
TRANSCRIPT_THRESHOLD = 30000      # chars: summarize if over
CHUNK_SIZE = 25000                # chars per chunk

# Multi-clauding
OVERLAP_WINDOW_MS = 30 * 60 * 1000  # 30 minutes

# Response time
MIN_RESPONSE_TIME_SEC = 2
MAX_RESPONSE_TIME_SEC = 3600

# Filtering
MIN_USER_MESSAGES = 2
MIN_DURATION_MINUTES = 1

# Response time histogram buckets
RESPONSE_TIME_BUCKETS = [
    ("2-10s",   2,    10),
    ("10-30s",  10,   30),
    ("30s-1m",  30,   60),
    ("1-2m",    60,   120),
    ("2-5m",    120,  300),
    ("5-15m",   300,  900),
    (">15m",    900,  float("inf")),
]

# Satisfaction display order
SATISFACTION_ORDER = [
    "frustrated", "dissatisfied", "likely_satisfied",
    "satisfied", "happy", "unsure",
]

# Outcome display order
OUTCOME_ORDER = [
    "not_achieved", "partially_achieved", "mostly_achieved",
    "fully_achieved", "unclear_from_transcript",
]
```

---

## 6. Layer 4 — Frontend

### 6.1 API Changes

**Existing endpoints** (unchanged):
- `POST /generate` — now returns richer data after backend rewrite
- `GET /latest`, `GET /history`, `GET /report` — unchanged

**New endpoint**:
- `GET /api/v1/insights/aggregated?report_uri=...` — returns `AggregatedData` JSON for chart consumption

**InsightsReport type update** (backend):
Add new fields to the JSON serialization to expose full CC-equivalent data to the frontend:

```python
# Added to report serialization
"interaction_style": {"narrative": "...", "key_pattern": "..."},
"fun_ending": {"headline": "...", "detail": "..."},
"at_a_glance": {
    "whats_working": "...",
    "whats_hindering": "...",
    "quick_wins": "...",
    "ambitious_workflows": "...",
},
"aggregated": {  # Embedded for charts
    "tool_counts": {...},
    "languages": {...},
    "goal_categories": {...},
    "session_types": {...},
    "friction": {...},
    "satisfaction": {...},
    "outcomes": {...},
    "success": {...},
    "message_hours": [...],
    "user_response_times": [...],
    "median_response_time": float,
    "avg_response_time": float,
    "multi_clauding": {...},
    "total_lines_added": int,
    "total_lines_removed": int,
    "total_files_modified": int,
    "days_active": int,
    "messages_per_day": float,
},
```

### 6.2 TypeScript Types Update

```typescript
// web/src/api/types.ts — updated

interface AtAGlance {
  whats_working: string;
  whats_hindering: string;
  quick_wins: string;
  ambitious_workflows: string;
}

interface InteractionStyle {
  narrative: string;
  key_pattern: string;
}

interface ImpressionWorkflow {
  title: string;
  description: string;
}

interface FrictionCategory {
  category: string;
  description: string;
  examples: string[];
}

interface FeatureToTry {
  feature: string;
  one_liner: string;
  why_for_you: string;
  example_code?: string;
}

interface UsagePattern {
  title: string;
  suggestion: string;
  detail?: string;
  copyable_prompt?: string;
}

interface HorizonOpportunity {
  title: string;
  whats_possible: string;
  how_to_try?: string;
  copyable_prompt?: string;
}

interface FunEnding {
  headline: string;
  detail: string;
}

interface AggregatedChartData {
  tool_counts: Record<string, number>;
  languages: Record<string, number>;
  goal_categories: Record<string, number>;
  session_types: Record<string, number>;
  friction: Record<string, number>;
  satisfaction: Record<string, number>;
  outcomes: Record<string, number>;
  success: Record<string, number>;
  message_hours: number[];
  user_response_times: number[];
  median_response_time: number;
  avg_response_time: number;
  multi_clauding: { overlap_events: number; sessions_involved: number; user_messages_during: number };
  total_lines_added: number;
  total_lines_removed: number;
  total_files_modified: number;
  days_active: number;
  messages_per_day: number;
}

interface InsightsReport {
  // ... existing fields ...
  at_a_glance: AtAGlance;
  interaction_style?: InteractionStyle;
  what_works_detail?: { intro: string; impressive_workflows: ImpressionWorkflow[] };
  friction_detail?: { intro: string; categories: FrictionCategory[] };
  suggestions_detail?: { features_to_try: FeatureToTry[]; usage_patterns: UsagePattern[] };
  on_the_horizon_detail?: { intro: string; opportunities: HorizonOpportunity[] };
  fun_ending?: FunEnding;
  aggregated?: AggregatedChartData;
}
```

### 6.3 Page Structure

```
web/src/pages/Insights.tsx                    # Main page (rewrite)
web/src/components/insights/
  ├── AtAGlance.tsx                           # Gold gradient, 4-part structure, anchor links
  ├── StatsRow.tsx                            # 5 columns: Messages, Lines+/-, Files, Days, Msgs/Day
  ├── ProjectAreaCard.tsx                     # Card with name, session count, description
  ├── InsightBarChart.tsx                     # Recharts BarChart wrapper (label map, fixed order support)
  ├── ResponseTimeHistogram.tsx               # 7-bucket histogram with median/avg annotation
  ├── TimeOfDayChart.tsx                      # 24h BarChart + timezone select dropdown
  ├── MultiClaudingPanel.tsx                  # Overlap events / sessions / % of messages
  ├── InteractionStyleSection.tsx             # Narrative paragraphs + key insight highlight box
  ├── ImpressionCard.tsx                      # Green card: title + description
  ├── FrictionCard.tsx                        # Red card: category + description + example bullets
  ├── FeatureCard.tsx                         # Green card: feature + one_liner + why + example code
  ├── UsagePatternCard.tsx                    # Blue card: title + suggestion + detail + copyable prompt
  ├── HorizonCard.tsx                         # Purple gradient: title + whats_possible + how_to_try + prompt
  ├── FunEnding.tsx                           # Gold gradient: headline (quoted) + detail
  ├── CopyButton.tsx                          # Clipboard copy with "Copied!" feedback
  └── SectionNav.tsx                          # TOC anchor navigation bar
```

### 6.4 Color Scheme (Tailwind)

| Component | Classes |
|-----------|---------|
| At a Glance | `bg-gradient-to-br from-amber-100 to-amber-200 border-amber-400` |
| Stats Row card | `bg-white border-gray-200` |
| Project Area card | `bg-white border-gray-200` |
| Impression card | `bg-green-50 border-green-200 text-green-900` |
| Friction card | `bg-red-50 border-red-300 text-red-900` |
| Feature card | `bg-green-50 border-green-300` |
| Pattern card | `bg-blue-50 border-blue-300` |
| Horizon card | `bg-gradient-to-br from-purple-50 to-violet-50 border-violet-300` |
| Fun Ending | `bg-gradient-to-br from-amber-100 to-amber-200 border-amber-300` |
| Key Insight | `bg-green-50 border-green-200 text-green-800` |

### 6.5 Chart Colors (Recharts)

| Chart | Color | CC Equivalent |
|-------|-------|---------------|
| Goals | `#4f46e5` (indigo-600) | `#2563eb` |
| Tools | `#0891b2` (cyan-600) | `#0891b2` |
| Languages | `#059669` (emerald-600) | `#10b981` |
| Session Types | `#7c3aed` (violet-600) | `#8b5cf6` |
| Response Time | `#6366f1` (indigo-500) | `#6366f1` |
| Time of Day | `#8b5cf6` (violet-500) | `#8b5cf6` |
| Tool Errors | `#dc2626` (red-600) | `#dc2626` |
| Success Factors | `#16a34a` (green-600) | `#16a34a` |
| Outcomes | `#7c3aed` (violet-600) | `#8b5cf6` |
| Friction Types | `#dc2626` (red-600) | `#dc2626` |
| Satisfaction | `#ca8a04` (yellow-600) | `#eab308` |

---

## 7. Full Pipeline (Revised InsightsAgent)

```python
async def analyze_async(self, tenant_id, user_id, start_date, end_date) -> InsightsReport:
    """CC-equivalent 8-phase pipeline."""

    # Phase 1: Load traces from TraceStore
    traces = await self._collector.fetch_traces(tenant_id, user_id, start_date, end_date)
    traces = traces[:MAX_SESSIONS_TO_LOAD]

    # Phase 2: Extract SessionMeta (zero LLM, uses cache)
    extractor = SessionMetaExtractor()
    entries = []
    for trace in traces:
        cached = await self._cache.get_meta(tenant_id, user_id, trace.session_id)
        if cached:
            entries.append((trace, cached))
        else:
            meta = extractor.extract(trace)
            await self._cache.put_meta(tenant_id, user_id, trace.session_id, meta)
            entries.append((trace, meta))

    # Phase 3: Dedup + filter
    entries = deduplicate_sessions(entries)
    entries = filter_substantive(entries)

    # Phase 4: Facet extraction (with cache + concurrency)
    facets = await extract_facets_batch(entries, self._cache, self._llm, tenant_id, user_id)

    # Phase 5: Filter warmup-only
    entries, facets = filter_warmup_only(entries, facets)

    # Phase 6: Aggregate
    metas = [m for _, m in entries]
    agg = aggregate_data(metas, facets)

    # Phase 7: Build context + generate sections (7 parallel + 1 serial)
    data_context = build_data_context(agg, facets)
    insights = await generate_parallel_insights(data_context, self._llm)

    # Phase 8: Assemble report
    return InsightsReport(
        tenant_id=tenant_id,
        user_id=user_id,
        # ... all fields from agg + insights ...
    )
```

---

## 8. File Manifest

### New files
```
src/opencortex/insights/
  extractor.py          # SessionMetaExtractor (pure code, 0 LLM)
  cache.py              # InsightsCache (CortexFS-backed)
  labels.py             # LABEL_MAP (40+ entries)
  constants.py          # All constants (thresholds, buckets, orders)
  multi_clauding.py     # detect_multi_clauding() algorithm

web/src/components/insights/
  AtAGlance.tsx
  StatsRow.tsx
  ProjectAreaCard.tsx
  InsightBarChart.tsx
  ResponseTimeHistogram.tsx
  TimeOfDayChart.tsx
  MultiClaudingPanel.tsx
  InteractionStyleSection.tsx
  ImpressionCard.tsx
  FrictionCard.tsx
  FeatureCard.tsx
  UsagePatternCard.tsx
  HorizonCard.tsx
  FunEnding.tsx
  CopyButton.tsx
  SectionNav.tsx
```

### Modified files
```
src/opencortex/alpha/observer.py           # ToolCallDetail extended fields
src/opencortex/alpha/types.py              # Turn.tool_calls type docs
src/opencortex/context/manager.py          # Pass enriched tool_calls
src/opencortex/insights/types.py           # SessionMeta, SessionFacet, AggregatedData, InsightsReport rewrite
src/opencortex/insights/prompts.py         # All 9 prompts rewritten (CC-equivalent)
src/opencortex/insights/agent.py           # 8-phase pipeline with parallel execution
src/opencortex/insights/collector.py       # Use SessionMetaExtractor, remove old aggregation
src/opencortex/insights/report.py          # Serialize enriched report JSON (no more HTML server-side)
src/opencortex/insights/api.py             # Add /aggregated endpoint

plugins/opencortex-memory/src/mcp-server.mjs  # Accept extended tool_calls in add_message

web/src/pages/Insights.tsx                 # Full rewrite with CC-equivalent sections
web/src/api/types.ts                       # All new TypeScript interfaces
web/src/api/client.ts                      # Add getAggregatedData() method
web/src/App.tsx                            # Route already exists
```

### Test files (new/updated)
```
tests/insights/test_extractor.py           # SessionMetaExtractor unit tests
tests/insights/test_cache.py               # InsightsCache tests
tests/insights/test_multi_clauding.py      # Multi-clauding detection tests
tests/insights/test_labels.py              # Label mapping tests
tests/insights/test_agent.py               # Updated for 8-phase pipeline
tests/insights/test_prompts.py             # Updated for 9 prompts
```

---

## 9. LLM Call Budget

| Phase | Calls | Notes |
|-------|-------|-------|
| SessionMeta extraction | 0 | Pure code |
| Long transcript summarization | 0-N | Only for sessions > 30k chars |
| Facet extraction | 0-50 | Cached sessions skip |
| project_areas | 1 | Parallel |
| interaction_style | 1 | Parallel |
| what_works | 1 | Parallel |
| friction_analysis | 1 | Parallel |
| suggestions | 1 | Parallel |
| on_the_horizon | 1 | Parallel |
| fun_ending | 1 | Parallel |
| at_a_glance | 1 | Serial (depends on above) |
| **Total per run** | **~8 + N** | N = uncached sessions (max 50) |
| **Repeat run (cached)** | **8** | All facets cached |
