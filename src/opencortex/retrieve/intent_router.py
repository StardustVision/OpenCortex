# SPDX-License-Identifier: Apache-2.0
"""
Intent Router: three-layer intent parsing -> SearchIntent.

Layer 1: Keyword extraction (zero LLM cost)
Layer 2: LLM semantic classification (conditional: query >= 30 chars)
Layer 3: Memory Trigger (Agent reflection intent, output alongside LLM)
"""

import json
import logging
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional

from opencortex.retrieve.types import (
    ContextType,
    DetailLevel,
    SearchIntent,
    TypedQuery,
)

logger = logging.getLogger(__name__)

LLMCompletionCallable = Callable[[str], Awaitable[str]]

# =========================================================================
# Keyword rule tables
# =========================================================================

_TEMPORAL_KEYWORDS: Dict[str, List[str]] = {
    "recent": ["上次", "刚才", "刚刚", "最近", "昨天", "今天",
               "recently", "last time", "just now", "yesterday", "today"],
    "session": ["这次", "这个会话", "本次", "this session", "current session"],
    "all": ["一直", "始终", "总是", "历史上", "always", "historically"],
}

_INTENT_KEYWORDS: Dict[str, List[str]] = {
    "summarize": ["总结", "回顾", "梳理", "归纳", "盘点",
                   "summarize", "summarise", "review", "recap", "overview"],
    "deep_analysis": ["详细", "深入", "完整", "全面", "具体分析",
                       "detailed", "in-depth", "comprehensive", "full analysis"],
    "quick_lookup": ["是不是", "有没有", "是否", "确认",
                      "is it", "does it", "do we", "confirm"],
}

# Intent -> default parameters
_INTENT_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "quick_lookup":  {"top_k": 3,  "detail_level": "l0", "need_rerank": False},
    "recent_recall": {"top_k": 5,  "detail_level": "l1", "need_rerank": True},
    "deep_analysis": {"top_k": 10, "detail_level": "l2", "need_rerank": True},
    "summarize":     {"top_k": 30, "detail_level": "l1", "need_rerank": True},
    "personalized":  {"top_k": 10, "detail_level": "l1", "need_rerank": True},
}

# Minimum query length to trigger LLM classification
_ROUTER_MIN_QUERY_LEN = 30

# =========================================================================
# LLM prompt
# =========================================================================

_ROUTER_PROMPT_TEMPLATE = """You are OpenCortex's Intent Router. Analyze the user query and determine:

1. **Intent Type**: What kind of retrieval is needed?
   - quick_lookup: Simple confirmation or fact check (top_k=3, l0)
   - recent_recall: Recent context recall (top_k=5, l1)
   - deep_analysis: Detailed analysis needing full content (top_k=10, l2)
   - summarize: Aggregation over many memories (top_k=30, l1)
   - personalized: Agent needs user metadata to give personalized advice (top_k=10, l1)

2. **Memory Triggers**: What additional context does the Agent need to answer well?
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
    "intent_type": "...",
    "top_k": N,
    "detail_level": "l0|l1|l2",
    "time_scope": "recent|session|all",
    "trigger_categories": ["preferences", "goals", ...]
}}"""


def _build_router_prompt(query: str, context_type: Optional[ContextType] = None) -> str:
    scope_section = ""
    if context_type:
        scope_section = f"Context type restriction: {context_type.value}\n\n"
    return _ROUTER_PROMPT_TEMPLATE.format(query=query, scope_section=scope_section)


def _parse_json_from_response(response: str) -> Optional[dict]:
    """Parse JSON from an LLM response string."""
    if not response:
        return None
    try:
        return json.loads(response.strip())
    except json.JSONDecodeError:
        pass
    # Try markdown code block
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", response, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass
    # Try first {...} block
    match = re.search(r"\{.*\}", response, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    return None


# =========================================================================
# IntentRouter
# =========================================================================

class IntentRouter:
    """Three-layer intent router: keywords -> LLM -> Memory Trigger."""

    def __init__(self, llm_completion: Optional[LLMCompletionCallable] = None):
        self._llm = llm_completion

    async def route(
        self,
        query: str,
        context_type: Optional[ContextType] = None,
    ) -> SearchIntent:
        """Route a query through the three-layer intent analysis pipeline.

        Args:
            query: User query text
            context_type: Optional context type restriction

        Returns:
            SearchIntent with resolved parameters
        """
        # Layer 1: keyword-based quick match
        intent = self._keyword_extract(query)

        # Layer 2+3: LLM classification + Memory Trigger (long queries only)
        if len(query) >= _ROUTER_MIN_QUERY_LEN and self._llm:
            try:
                llm_intent = await self._llm_classify(query, context_type)
                if llm_intent:
                    intent = self._merge(intent, llm_intent)
            except Exception as exc:
                logger.warning("[IntentRouter] LLM classification failed: %s", exc)

        # Build TypedQueries from intent
        intent.queries = self._build_queries(query, context_type, intent)
        return intent

    def _keyword_extract(self, query: str) -> SearchIntent:
        """Layer 1: keyword matching for initial intent."""
        intent_type = "recent_recall"  # default
        time_scope = "all"

        for scope, keywords in _TEMPORAL_KEYWORDS.items():
            if any(kw in query for kw in keywords):
                time_scope = scope
                if scope == "recent":
                    intent_type = "recent_recall"
                break

        for itype, keywords in _INTENT_KEYWORDS.items():
            if any(kw in query for kw in keywords):
                intent_type = itype
                break

        defaults = _INTENT_DEFAULTS.get(intent_type, _INTENT_DEFAULTS["recent_recall"])
        return SearchIntent(
            intent_type=intent_type,
            top_k=defaults["top_k"],
            detail_level=DetailLevel(defaults["detail_level"]),
            time_scope=time_scope,
            need_rerank=defaults["need_rerank"],
        )

    async def _llm_classify(
        self, query: str, context_type: Optional[ContextType]
    ) -> Optional[SearchIntent]:
        """Layer 2+3: LLM semantic classification + Memory Trigger."""
        prompt = _build_router_prompt(query, context_type)
        response = await self._llm(prompt)
        parsed = _parse_json_from_response(response)
        if not parsed:
            return None

        itype = parsed.get("intent_type", "recent_recall")
        if itype not in _INTENT_DEFAULTS:
            itype = "recent_recall"

        dl_str = parsed.get("detail_level", "l1")
        try:
            dl = DetailLevel(dl_str)
        except ValueError:
            dl = DetailLevel.L1

        top_k = parsed.get("top_k")
        if not isinstance(top_k, int) or top_k < 1:
            top_k = _INTENT_DEFAULTS.get(itype, {}).get("top_k", 5)
        top_k = min(max(top_k, 1), 50)

        time_scope = parsed.get("time_scope", "all")
        if time_scope not in ("recent", "session", "all"):
            time_scope = "all"

        trigger_categories = parsed.get("trigger_categories", [])
        if not isinstance(trigger_categories, list):
            trigger_categories = []

        return SearchIntent(
            intent_type=itype,
            top_k=top_k,
            detail_level=dl,
            time_scope=time_scope,
            need_rerank=_INTENT_DEFAULTS.get(itype, {}).get("need_rerank", True),
            trigger_categories=trigger_categories,
        )

    def _merge(self, keyword_intent: SearchIntent, llm_intent: SearchIntent) -> SearchIntent:
        """Merge LLM result over keyword result (LLM takes priority)."""
        return SearchIntent(
            intent_type=llm_intent.intent_type,
            top_k=llm_intent.top_k,
            detail_level=llm_intent.detail_level,
            time_scope=llm_intent.time_scope,
            need_rerank=llm_intent.need_rerank,
            trigger_categories=llm_intent.trigger_categories,
        )

    def _build_queries(
        self,
        query: str,
        context_type: Optional[ContextType],
        intent: SearchIntent,
    ) -> List[TypedQuery]:
        """Build query list from intent, including trigger category queries."""
        # Determine context types to search
        if context_type:
            types = [context_type]
        else:
            types = [ContextType.MEMORY, ContextType.RESOURCE, ContextType.SKILL]

        queries = [
            TypedQuery(
                query=query,
                context_type=ct,
                intent=intent.intent_type,
                priority=1,
                detail_level=intent.detail_level,
            )
            for ct in types
        ]

        # Memory Trigger: append extra category queries for triggered categories
        for cat in intent.trigger_categories:
            queries.append(
                TypedQuery(
                    query=f"{query} {cat}",
                    context_type=ContextType.MEMORY,
                    intent=f"trigger:{cat}",
                    priority=2,
                    detail_level=DetailLevel.L0,  # trigger queries use L0 to save tokens
                )
            )

        return queries
