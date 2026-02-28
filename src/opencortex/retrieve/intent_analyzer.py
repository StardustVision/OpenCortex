# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# Ported from OpenViking (https://github.com/volcengine/openviking)
# SPDX-License-Identifier: Apache-2.0
"""
Intent analyzer for OpenCortex retrieval.

Analyzes session context to generate query plans.
"""

import logging
from typing import Awaitable, Callable, List, Optional

from opencortex.core.message import Message
from opencortex.retrieve.types import ContextType, QueryPlan, TypedQuery
from opencortex.utils.json_parse import parse_json_from_response as _parse_json_from_response

logger = logging.getLogger(__name__)

# Type alias for the LLM completion callable
# Takes a prompt string and returns the completion string (async)
LLMCompletionCallable = Callable[[str], Awaitable[str]]


def _build_intent_analysis_prompt(
    compression_summary: str,
    recent_messages: str,
    current_message: str,
    context_type: str = "",
    target_abstract: str = "",
) -> str:
    """
    Build the intent analysis prompt.

    Implements the same template logic as the OpenViking
    retrieval.intent_analysis YAML template.

    Args:
        compression_summary: Session summary text
        recent_messages: Formatted recent conversation
        current_message: Current user message
        context_type: Optional restricted context type
        target_abstract: Optional abstract of the target directory

    Returns:
        Rendered prompt string
    """
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

### 1. skill (Execution Capability)

**Purpose**: Executable tools, functions, APIs, automation scripts

**Query Style**: **Start with verbs, maintain operational intent**

### 2. resource (Knowledge Resources)

**Purpose**: Documents, specifications, guides, code, configurations, and other structured knowledge

**Query Style**: **Noun phrases, describing knowledge content**

### 3. memory (User/Agent Memory)

**Purpose**: User personalization information or Agent execution experience

**Query Style**: Distinguish by memory type

## Output Format

```json
{{
    "reasoning": "1. Task type; 2. What context is needed; 3. What is already in context; 4. What is missing",
    "queries": [
        {{
            "query": "Specific query text",
            "context_type": "skill|resource|memory",
            "intent": "Purpose of the query",
            "priority": 1
        }}
    ]
}}
```

Please output JSON:"""


class IntentAnalyzer:
    """
    Intent analyzer: generates query plans from session context.

    Responsibilities:
    1. Integrate session context (compression + recent messages + current message)
    2. Call LLM to analyze intent
    3. Generate multiple TypedQueries for memory/resources/skill

    The LLM completion callable must be provided at construction time or passed
    to analyze(). This decouples the analyzer from any specific LLM client,
    config system, or API key management.

    Example:
        async def my_llm(prompt: str) -> str:
            # call your LLM client here
            return await client.complete(prompt)

        analyzer = IntentAnalyzer(llm_completion=my_llm)
        plan = await analyzer.analyze(summary, messages, current_msg)
    """

    def __init__(
        self,
        llm_completion: Optional[LLMCompletionCallable] = None,
        max_recent_messages: int = 5,
    ):
        """Initialize intent analyzer.

        Args:
            llm_completion: Async callable that takes a prompt string and returns
                            a completion string. If None, must be provided to analyze().
            max_recent_messages: Maximum number of recent messages to include in context.
        """
        self._llm_completion = llm_completion
        self.max_recent_messages = max_recent_messages

    async def analyze(
        self,
        compression_summary: str,
        messages: List[Message],
        current_message: Optional[str] = None,
        context_type: Optional[ContextType] = None,
        target_abstract: str = "",
        llm_completion: Optional[LLMCompletionCallable] = None,
    ) -> QueryPlan:
        """Analyze session context and generate query plan.

        Args:
            compression_summary: Session compression summary
            messages: Session message history
            current_message: Current message (if any)
            context_type: Constrained context type (only generate queries for this type)
            target_abstract: Target directory abstract for more precise queries
            llm_completion: Override the instance-level LLM callable for this call.

        Returns:
            QueryPlan with typed queries.

        Raises:
            ValueError: If no LLM callable is configured or if the response cannot be parsed.
        """
        completion_fn = llm_completion or self._llm_completion
        if completion_fn is None:
            raise ValueError(
                "No LLM completion callable configured. "
                "Provide one via the constructor or the llm_completion argument."
            )

        # Build context prompt
        prompt = self._build_context_prompt(
            compression_summary,
            messages,
            current_message,
            context_type,
            target_abstract,
        )

        # Call LLM
        response = await completion_fn(prompt)

        # Parse result
        parsed = _parse_json_from_response(response)
        if not parsed:
            raise ValueError("Failed to parse intent analysis response")

        # Build QueryPlan
        queries = []
        for q in parsed.get("queries", []):
            try:
                query_context_type = ContextType(q.get("context_type", "resource"))
            except ValueError:
                query_context_type = ContextType.RESOURCE

            queries.append(
                TypedQuery(
                    query=q.get("query", ""),
                    context_type=query_context_type,
                    intent=q.get("intent", ""),
                    priority=q.get("priority", 3),
                )
            )

        # Log analysis result
        for i, q in enumerate(queries):
            logger.info(
                f'  [{i + 1}] type={q.context_type.value}, priority={q.priority}, query="{q.query}"'
            )
        logger.debug(f"[IntentAnalyzer] Reasoning: {parsed.get('reasoning', '')[:200]}...")

        return QueryPlan(
            queries=queries,
            session_context=self._summarize_context(compression_summary, current_message),
            reasoning=parsed.get("reasoning", ""),
        )

    def _build_context_prompt(
        self,
        compression_summary: str,
        messages: List[Message],
        current_message: Optional[str],
        context_type: Optional[ContextType] = None,
        target_abstract: str = "",
    ) -> str:
        """Build prompt for intent analysis."""
        # Format compression info
        summary = compression_summary if compression_summary else "None"

        # Format recent messages
        recent = messages[-self.max_recent_messages:] if messages else []
        recent_messages = (
            "\n".join(f"[{m.role}]: {m.content}" for m in recent if m.content) if recent else "None"
        )

        # Current message
        current = current_message if current_message else "None"

        return _build_intent_analysis_prompt(
            compression_summary=summary,
            recent_messages=recent_messages,
            current_message=current,
            context_type=context_type.value if context_type else "",
            target_abstract=target_abstract,
        )

    def _summarize_context(
        self,
        compression_summary: str,
        current_message: Optional[str],
    ) -> str:
        """Generate context summary."""
        parts = []
        if compression_summary:
            parts.append(f"Session summary: {compression_summary}")
        if current_message:
            parts.append(f"Current message: {current_message[:100]}")
        return " | ".join(parts) if parts else "No context"
