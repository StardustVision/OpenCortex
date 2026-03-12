# SPDX-License-Identifier: Apache-2.0
"""
Intent Router: three-layer intent parsing -> SearchIntent.

Layer 1: Keyword extraction (zero LLM cost)
Layer 2: LLM semantic classification (whenever LLM is available)
Layer 3: Memory Trigger (Agent reflection intent, output alongside LLM)
"""

import logging
import re
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from opencortex.prompts import build_router_prompt
from opencortex.retrieve.types import (
    ContextType,
    DetailLevel,
    SearchIntent,
    TypedQuery,
)
from opencortex.utils.json_parse import parse_json_from_response as _parse_json_from_response

logger = logging.getLogger(__name__)

LLMCompletionCallable = Callable[[str], Awaitable[str]]

# =========================================================================
# Hard keyword detection patterns
# =========================================================================

_CAMEL_CASE_RE = re.compile(r"[a-z]+[A-Z][a-zA-Z]*")            # TrafficRule, OutboundType
_ALL_CAPS_RE = re.compile(r"\b[A-Z]{2,}\b")                       # TUN, DNS, SCIM
_PATH_SYMBOL_RE = re.compile(r"[a-zA-Z0-9]+[_./-][a-zA-Z0-9]+")  # traffic_rule.proto

_HARD_KEYWORD_LEXICAL_BOOST = 0.55
_DEFAULT_LEXICAL_BOOST = 0.3

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

# Patterns that indicate no memory recall is needed (greetings, farewells, thanks)
_NO_RECALL_PATTERNS: List[str] = [
    # Chinese
    "你好", "您好", "嗨", "早上好", "下午好", "晚上好",
    "再见", "拜拜", "下次见", "回头见",
    "谢谢", "感谢", "多谢", "辛苦了",
    "好的", "可以", "明白", "知道了", "收到",
    # English
    "hello", "hi", "hey", "good morning", "good afternoon", "good evening",
    "goodbye", "bye", "see you", "later",
    "thanks", "thank you", "thx",
    "ok", "okay", "got it", "understood", "sure",
]

# Intent -> default parameters
_INTENT_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "quick_lookup":  {"top_k": 3,  "detail_level": "l0", "need_rerank": False},
    "recent_recall": {"top_k": 5,  "detail_level": "l1", "need_rerank": True},
    "deep_analysis": {"top_k": 10, "detail_level": "l2", "need_rerank": True},
    "summarize":     {"top_k": 30, "detail_level": "l1", "need_rerank": True},
    "personalized":  {"top_k": 10, "detail_level": "l1", "need_rerank": True},
}

# =========================================================================
# IntentRouter
# =========================================================================

_CACHE_TTL_SECONDS = 60
_CACHE_MAX_SIZE = 128


class IntentRouter:
    """Three-layer intent router: keywords -> LLM -> Memory Trigger."""

    def __init__(self, llm_completion: Optional[LLMCompletionCallable] = None):
        self._llm = llm_completion
        # LRU cache: key -> (SearchIntent, timestamp)
        self._cache: OrderedDict[str, Tuple["SearchIntent", float]] = OrderedDict()

    @staticmethod
    def _detect_hard_keywords(query: str) -> bool:
        """Detect hard keywords (CamelCase, ALL_CAPS, path/symbol patterns).

        These patterns indicate technical identifiers that benefit from
        exact lexical matching over semantic similarity.
        """
        return bool(
            _CAMEL_CASE_RE.search(query)
            or _ALL_CAPS_RE.search(query)
            or _PATH_SYMBOL_RE.search(query)
        )

    async def route(
        self,
        query: str,
        context_type: Optional[ContextType] = None,
        session_context: Optional[Dict[str, Any]] = None,
    ) -> SearchIntent:
        """Route a query through the three-layer intent analysis pipeline.

        Args:
            query: User query text
            context_type: Optional context type restriction
            session_context: Optional session context dict. When None, LLM
                classification is skipped entirely (keyword-only path).

        Returns:
            SearchIntent with resolved parameters
        """
        # Layer 1: keyword-based quick match
        intent = self._keyword_extract(query)

        # Short-circuit: keyword layer already determined no recall needed
        if not intent.should_recall:
            return intent

        # Layer 2+3: LLM classification + Memory Trigger
        # Only when session_context is provided AND an LLM callable is available
        if session_context is not None and self._llm:
            # Check LRU cache first
            cached = self._cache_get(query)
            if cached is not None:
                intent = self._merge(intent, cached)
            else:
                try:
                    llm_intent = await self._llm_classify(query, context_type, session_context)
                    if llm_intent:
                        self._cache_put(query, llm_intent)
                        intent = self._merge(intent, llm_intent)
                except Exception as exc:
                    logger.warning("[IntentRouter] LLM classification failed: %s", exc)

        # Build TypedQueries from intent
        intent.queries = self._build_queries(query, context_type, intent)
        return intent

    def _cache_get(self, key: str) -> Optional["SearchIntent"]:
        """Return cached SearchIntent if present and within TTL, else None."""
        if key not in self._cache:
            return None
        intent, ts = self._cache[key]
        if time.monotonic() - ts > _CACHE_TTL_SECONDS:
            del self._cache[key]
            return None
        # Move to end (most recently used)
        self._cache.move_to_end(key)
        return intent

    def _cache_put(self, key: str, intent: "SearchIntent") -> None:
        """Insert or update a cache entry, evicting oldest if over maxsize."""
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = (intent, time.monotonic())
        while len(self._cache) > _CACHE_MAX_SIZE:
            self._cache.popitem(last=False)

    def _keyword_extract(self, query: str) -> SearchIntent:
        """Layer 1: keyword matching for initial intent."""
        # Check no-recall patterns: short queries that exactly match
        query_stripped = query.strip()
        query_lower = query_stripped.lower()
        if query_lower in _NO_RECALL_PATTERNS or query_stripped in _NO_RECALL_PATTERNS:
            return SearchIntent(
                intent_type="quick_lookup",
                top_k=0,
                detail_level=DetailLevel.L0,
                time_scope="all",
                need_rerank=False,
                should_recall=False,
            )

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

        # Detect hard keywords for lexical boost
        lexical_boost = (
            _HARD_KEYWORD_LEXICAL_BOOST
            if self._detect_hard_keywords(query_stripped)
            else _DEFAULT_LEXICAL_BOOST
        )

        defaults = _INTENT_DEFAULTS.get(intent_type, _INTENT_DEFAULTS["recent_recall"])
        return SearchIntent(
            intent_type=intent_type,
            top_k=defaults["top_k"],
            detail_level=DetailLevel(defaults["detail_level"]),
            time_scope=time_scope,
            need_rerank=defaults["need_rerank"],
            lexical_boost=lexical_boost,
        )

    async def _llm_classify(
        self,
        query: str,
        context_type: Optional[ContextType],
        session_context: Optional[Dict[str, Any]] = None,
    ) -> Optional[SearchIntent]:
        """Layer 2+3: LLM semantic classification + Memory Trigger."""
        prompt = build_router_prompt(
            query, context_type.value if context_type else "",
        )
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

        should_recall = parsed.get("should_recall", True)
        if not isinstance(should_recall, bool):
            should_recall = True

        # Parse multi-query array from LLM response (Task 15)
        llm_queries: List[TypedQuery] = []
        raw_queries = parsed.get("queries", [])
        if isinstance(raw_queries, list):
            for q in raw_queries:
                if not isinstance(q, dict):
                    continue
                q_text = q.get("query", "")
                if not q_text:
                    continue
                ct_str = q.get("context_type", "any")
                try:
                    ct = ContextType(ct_str)
                except ValueError:
                    ct = ContextType.ANY
                q_intent = q.get("intent", itype)
                llm_queries.append(TypedQuery(
                    query=q_text,
                    context_type=ct,
                    intent=q_intent,
                    priority=1,
                    detail_level=dl,
                ))

        return SearchIntent(
            intent_type=itype,
            top_k=top_k,
            detail_level=dl,
            time_scope=time_scope,
            need_rerank=_INTENT_DEFAULTS.get(itype, {}).get("need_rerank", True),
            should_recall=should_recall,
            trigger_categories=trigger_categories,
            queries=llm_queries,
        )

    def _merge(self, keyword_intent: SearchIntent, llm_intent: SearchIntent) -> SearchIntent:
        """Merge LLM result over keyword result (LLM takes priority).

        lexical_boost uses max(keyword, llm) to ensure regex-detected hard
        keywords are never downgraded by LLM classification.
        """
        return SearchIntent(
            intent_type=llm_intent.intent_type,
            top_k=llm_intent.top_k,
            detail_level=llm_intent.detail_level,
            time_scope=llm_intent.time_scope,
            need_rerank=llm_intent.need_rerank,
            should_recall=llm_intent.should_recall,
            trigger_categories=llm_intent.trigger_categories,
            lexical_boost=max(keyword_intent.lexical_boost, llm_intent.lexical_boost),
            queries=llm_intent.queries,
        )

    def _build_queries(
        self,
        query: str,
        context_type: Optional[ContextType],
        intent: SearchIntent,
    ) -> List[TypedQuery]:
        """Build query list from intent, including trigger category queries.

        If the LLM returned a pre-built queries list (multi-query, Task 15),
        use those as the base. Otherwise fall back to single-query construction.
        """
        # Use LLM-provided multi-query list when available
        if intent.queries:
            queries = list(intent.queries)
        else:
            if context_type:
                types = [context_type]
            else:
                # Single global query — no context_type filter
                types = [ContextType.ANY]

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
                    context_type=ContextType.ANY,
                    intent=f"trigger:{cat}",
                    priority=2,
                    detail_level=DetailLevel.L0,
                )
            )

        return queries
