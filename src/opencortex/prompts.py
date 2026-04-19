# SPDX-License-Identifier: Apache-2.0
"""
Centralized prompt definitions for OpenCortex.

All LLM prompts live here — zero internal dependencies (pure leaf module).
Each prompt is either a build function (parameterized) or a template constant
(.format() placeholders).
"""

from typing import Optional


# =========================================================================
# 1. Intent Analysis  (was: retrieve/intent_analyzer.py)
# =========================================================================

def build_intent_analysis_prompt(
    compression_summary: str,
    recent_messages: str,
    current_message: str,
    context_type: str = "",
    target_abstract: str = "",
) -> str:
    """Build the intent analysis prompt for query planning."""
    scope_section = ""
    if context_type:
        scope_section = f"""
## Search Scope Constraints

**Restricted Context Type**: {context_type}
"""
        if target_abstract:
            scope_section += f"**Target Directory Abstract**: {target_abstract}\n"
        scope_section += f"\n**Important**: You can only generate `{context_type}` type queries, do not generate other types."

    return f"""You are OpenCortex's context query planner, responsible for analyzing task context gaps and generating queries.

## Session Context

### Session Summary
{compression_summary}

### Recent Conversation
{recent_messages}

### Current Message
{current_message}
{scope_section}

## Your Task

Analyze the current task, identify context gaps, and generate queries to fill in the required information.

**Core Principle**: OpenCortex's external information takes priority over built-in knowledge, actively query external context.

## Context Types and Query Styles

OpenCortex supports the following context types, **each type has a different query style**:

### 1. resource (Knowledge Resources)

**Purpose**: Documents, specifications, guides, code, configurations, and other structured knowledge

**Query Style**: **Noun phrases, describing knowledge content**

### 2. memory (User/Agent Memory)

**Purpose**: User personalization information or Agent execution experience

**Query Style**: Distinguish by memory type

## Output Format

```json
{{
    "reasoning": "1. Task type; 2. What context is needed; 3. What is already in context; 4. What is missing",
    "queries": [
        {{
            "query": "Specific query text",
            "context_type": "resource|memory",
            "intent": "Purpose of the query",
            "priority": 1
        }}
    ]
}}
```

Please output JSON:"""


# =========================================================================
# 2. Intent Router
# =========================================================================

_ROUTER_PROMPT_TEMPLATE = """You are OpenCortex's Intent Router. Analyze the user query and determine:

1. **Should Recall**: Does this query need memory retrieval at all?
   - Set false for greetings, farewells, simple acknowledgments, or chitchat
   - Set true for any query that could benefit from past context or stored knowledge

2. **Intent Type**: What kind of retrieval is needed?
   - quick_lookup: Simple confirmation or fact check (top_k=3, l0)
   - recent_recall: Recent context recall (top_k=5, l1)
   - deep_analysis: Detailed analysis needing full content (top_k=10, l2)
   - summarize: Aggregation over many memories (top_k=30, l1)
   - personalized: Agent needs user metadata to give personalized advice (top_k=10, l1)

3. **Memory Triggers**: What additional context does the Agent need to answer well?
   Think from the Agent's perspective — what background information would help
   provide a better answer? Return categories to proactively fetch:
   - preferences: User preferences, habits, style
   - goals: User goals, objectives, career direction
   - experience: Past experiences, solutions tried
   - patterns: Code patterns, architectural conventions
   - error_fixes: Previous bug fixes, troubleshooting history
   - architecture: System design decisions
   - code_style: Coding conventions, formatting preferences

{scope_section}Query: {query}

Output JSON only:
{{
    "should_recall": true,
    "intent_type": "...",
    "top_k": N,
    "detail_level": "l0|l1|l2",
    "time_scope": "recent|session|all",
    "trigger_categories": ["preferences", "goals", ...]
}}"""


def build_router_prompt(query: str, context_type: Optional[str] = None) -> str:
    """Build the intent router LLM prompt.

    Args:
        query: User query text.
        context_type: Optional context type restriction value (e.g. "memory").
    """
    scope_section = ""
    if context_type:
        scope_section = f"Context type restriction: {context_type}\n\n"
    return _ROUTER_PROMPT_TEMPLATE.format(query=query, scope_section=scope_section)


# =========================================================================
# 3. Document Summarization  (was: orchestrator.py)
# =========================================================================

def build_doc_summarization_prompt(file_path: str, content: str) -> str:
    """Build prompt for document abstract + overview generation.

    Args:
        file_path: Document file path.
        content: Document content (caller handles chunking for oversized content).
    """
    return f"""Summarize this document for a memory system.

File: {file_path}
Content:
{content}

Return JSON: {{"abstract": "1-2 sentence summary", "overview": "1 paragraph overview"}}"""


# =========================================================================
# 5. L1 Overview Generation  (was: orchestrator.py)
# =========================================================================

def build_layer_derivation_prompt(content: str, user_abstract: str = "") -> str:
    """Build prompt for overview-first derivation from L2 content.

    Args:
        content: Content text (caller handles chunking for oversized content).
        user_abstract: Optional user-supplied abstract to guide generation.
    """
    user_hint = ""
    if user_abstract:
        user_hint = (
            f"\nThe user described this memory as: \"{user_abstract}\"\n"
            "Do not generate another abstract. Keep this text in mind while generating overview and anchors.\n"
        )
    return f"""Analyze the following content and produce a structured summary for a memory system.
{user_hint}
Content:
{content}

Return a JSON object with exactly these fields:
{{
  "overview": "Structured Markdown summary (see rules below)",
  "keywords": ["term1", "term2", "..."],
  "entities": ["entity1", "entity2", "..."],
  "anchor_handles": ["handle1", "handle2", "..."],
  "fact_points": ["atomic fact 1", "atomic fact 2", "..."]
}}

Rules — overview (PRIMARY field, write this FIRST and make it the longest):
This is the primary semantic surface for retrieval. Total length MUST be 300-600 words.
Write it as Markdown with EXACTLY this heading structure:

## Summary
One concise sentence capturing the core topic and participants.

## Key Events and Statements
List the important things said or done. Attribute to speakers by name.
Preserve original phrasing — do NOT paraphrase or compress. Include all specific
names, dates, numbers, and locations verbatim.

## Decisions and Outcomes
Conclusions reached, plans made, or commitments given.

## Key Quotes
Preserve 3-6 important original sentences verbatim from the content.
Prioritize sentences containing specific facts: names, dates, numbers, locations,
decisions, or commitments. Use the EXACT original wording — do not rewrite.

Hard constraints:
- Every sentence in "Key Events" MUST contain at least one concrete detail (name, date, number, or location).
- "Key Quotes" MUST be verbatim copies from the content, not paraphrases.
- Do NOT produce generic summaries. A reader must be able to answer specific factual questions from the overview alone.

Rules — other fields:
- keywords: 3-15 key terms (names, tools, technologies, concepts). No generic words.
- entities: Named entities only — people, systems, tools, organizations, places. Max 10.
- anchor_handles: 0-6 short retrieval handles. Prefer concrete entities, numbers, paths, module names, or compact noun phrases.
- fact_points: 0-8 atomic fact statements. Each ≤80 chars, self-contained, must contain at least one concrete signal (name, number, date, path, or technical term).
- Return ONLY the JSON object, no other text."""


def build_overview_prompt(abstract: str, content: str) -> str:
    """Build prompt for L1 overview generation from content.

    Args:
        abstract: L0 abstract / title.
        content: Full content text.
    """
    return (
        "Generate a concise overview (3-8 sentences) of the following content. "
        "The FIRST sentence must be a standalone summary of the key point. "
        "Then provide supporting details: facts, decisions, and actionable information.\n\n"
        f"Title: {abstract}\n\nContent:\n{content}\n\nOverview:"
    )


# =========================================================================
# 6. Trace Split  (was: alpha/trace_splitter.py)
# =========================================================================

TRACE_SPLIT_PROMPT = """Analyze this conversation transcript and identify distinct tasks.
For each task, provide:
- summary: one-line description (L0)
- key_steps: bullet-point steps taken (L1)
- turn_indices: which turn indices (0-based) belong to this task
- outcome: success/failure/timeout/cancelled
- task_type: coding/debug/chat/config/docs/review/other

Transcript ({turn_count} turns):
{transcript}

Return a JSON array of objects. Example:
[{{"summary": "Fixed import error in auth.py", "key_steps": ["Read error", "Fixed typo"], "turn_indices": [0, 1, 2], "outcome": "success", "task_type": "debug"}}]

Return ONLY the JSON array, no other text."""


# =========================================================================
# 7. Knowledge Extraction  (was: alpha/archivist.py)
# =========================================================================

KNOWLEDGE_EXTRACT_PROMPT = """Given these related task traces, extract reusable knowledge.

Traces ({count} total):
{traces_text}

For each piece of knowledge you identify, classify it as one of:
- belief: A judgment rule or best practice
- sop: A standard operating procedure with ordered steps
- negative_rule: Something that should never be done
- root_cause: A recurring error pattern with its cause and fix

Return a JSON array of knowledge items:
[{{"type": "belief|sop|negative_rule|root_cause", "statement": "...", "objective": "...", "action_steps": ["step1", "step2"] (for sop only), "error_pattern": "..." (for root_cause), "cause": "..." (for root_cause), "fix_suggestion": "..." (for root_cause), "severity": "low|medium|high" (for negative_rule), "trigger_keywords": ["kw1", "kw2"]}}]

Return ONLY the JSON array."""


# =========================================================================
# 8. Knowledge Verification  (was: alpha/sandbox.py)
# =========================================================================

KNOWLEDGE_VERIFY_PROMPT = """You are evaluating whether a knowledge item would have improved
the outcome of a historical task trace.

Knowledge item:
Type: {knowledge_type}
Statement: {statement}
Objective: {objective}
Action steps: {action_steps}

Historical trace summary:
{trace_summary}

Question: If the agent had applied this knowledge during the trace above,
would the outcome have improved? Answer with a JSON object:
{{"improved": true/false, "reason": "brief explanation"}}"""


# =========================================================================
# 9. Rerank (LLM fallback)  (was: retrieve/rerank_client.py)
# =========================================================================

def build_rerank_prompt(query: str, docs_text: str) -> str:
    """Build LLM prompt for listwise reranking.

    Args:
        query: Search query text.
        docs_text: Pre-formatted document text (e.g. "[0] doc..." lines).
    """
    return (
        "You are a relevance scoring system. "
        "Score each document's relevance to the query on a scale of 0.0 to 1.0.\n\n"
        f"Query: {query}\n\n"
        f"Documents:\n{docs_text}\n\n"
        "Return ONLY a JSON array of scores in the same order as the documents. "
        "Example: [0.95, 0.3, 0.8]\n"
        "Scores:"
    )

# =========================================================================
# 10. Overview Compression  (for chunked derivation)
# =========================================================================

def build_overview_compression_prompt(overviews: str) -> str:
    """Compress multiple chunk overviews into a single 3-8 sentence overview.

    Args:
        overviews: Concatenated overviews from multiple chunks.
    """
    return f"""Compress the following multiple overview sections into a single coherent overview.

Source overviews:
{overviews}

Rules:
- Produce 3-8 sentences that cover the key facts from ALL source overviews
- Do NOT repeat information
- Maintain factual accuracy
- Return ONLY the compressed overview text, no JSON wrapping"""


# =========================================================================
# 11. Parent / Session Summarization  (bottom-up from children abstracts)
# =========================================================================

def build_parent_summarization_prompt(doc_title: str, children_abstracts: list) -> str:
    """Build prompt to summarize a parent node from its children's abstracts.

    Used for document section nodes (bottom-up) and conversation session summaries.

    Args:
        doc_title: Title of the parent document or session identifier.
        children_abstracts: List of L0 abstract strings from child records.
    """
    numbered = "\n".join(
        f"{i + 1}. {a}" for i, a in enumerate(children_abstracts)
    )
    fallback_note = (
        "\nNote: No child abstracts available. Generate a minimal placeholder summary."
        if not children_abstracts
        else ""
    )
    return f"""Summarize the following child sections into a parent-level summary for a memory system.

Document: {doc_title}

Child section abstracts:
{numbered}{fallback_note}

Return a JSON object with exactly these fields:
{{
  "abstract": "One concise sentence (≤200 chars) capturing the core theme across all children. Must be a complete sentence, not a truncation.",
  "overview": "Comprehensive overview synthesizing the key facts, decisions, and topics from all child sections.",
  "keywords": ["term1", "term2", "..."]
}}

Rules:
- abstract: Synthesize, do not copy from any single child. ≤200 characters.
- overview: This is the primary semantic surface for retrieval. Write 200-500 words. Structure as:
  1. Opening paragraph: participants, main topics, and time context. Preserve original names, dates, and locations verbatim.
  2. Key discussions and statements: important things said or done, with speaker attribution. Use original phrasing where possible.
  3. Decisions and outcomes: conclusions reached, plans made, or commitments given.
  Do NOT compress into generic summaries. Cover all major themes from ALL children with no repetition. Factually grounded.
- keywords: 3-10 key terms across all children.
- Return ONLY the JSON object, no other text."""
