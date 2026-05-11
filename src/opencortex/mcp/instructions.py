# SPDX-License-Identifier: Apache-2.0
"""Default MCP instructions and reusable prompt text for OpenCortex."""

from __future__ import annotations

MCP_INSTRUCTIONS = """OpenCortex is the user's long-term memory system.

Route information by shape. Conversation turns go to sessions, explicit durable
facts go to memory, and large reusable documents go to resources.

Default workflow:
1. Before answering context-dependent questions, call opencortex.search with the
   user's current question. Use the returned memories/resources as private
   grounding context.
2. For every meaningful conversation turn, call opencortex.session_message when
   a stable session_id and turn_id are available. This records the raw dialogue
   for later merge and extraction.
3. When the user explicitly asks to remember something, states a durable fact,
   preference, decision, requirement, profile detail, or important outcome, call
   opencortex.store_memory. This does not replace opencortex.session_message.
4. When the user provides a document, notes, specifications, API reference,
   pasted article, or other reusable long-form material, call
   opencortex.store_resource instead of opencortex.store_memory.
5. At the end of a meaningful conversation session, call opencortex.session_end
   when a stable session_id is available.
6. Use opencortex.forget only when the user explicitly asks to delete or forget
   information.

Do not expose internal OpenCortex URIs, tool names, or storage details unless the
user asks for them. If search results are irrelevant, ignore them and answer from
the conversation context.
"""

MCP_CLIENT_RULES = """# OpenCortex Memory Rules

OpenCortex is available as a long-term memory system through MCP tools.

Before answering:
- If the user asks anything that may depend on prior context, preferences,
  decisions, project history, resources, or durable facts, call
  `opencortex.search` first with the user's current question.
- Treat returned memories and resources as private grounding context.
- If results are irrelevant, ignore them and answer normally.

While answering:
- Do not mention OpenCortex, MCP, internal URIs, scores, or storage details unless
  the user explicitly asks.
- Prefer concise answers grounded in the best matching memory/resource evidence.

After or during the turn:
- For every meaningful conversation turn, call `opencortex.session_message` when
  a stable `session_id` and `turn_id` are available. Use this for ordinary
  dialogue recording.
- If the user provides durable facts, preferences, decisions, requirements,
  personal details, project facts, important outcomes, or explicitly says to
  remember something, call `opencortex.store_memory`. This does not replace
  `opencortex.session_message`.
- If the user provides a document, notes, specifications, API reference, pasted
  article, or other reusable long-form material, call `opencortex.store_resource`
  instead of `opencortex.store_memory`.
- At the end of a meaningful session, call `opencortex.session_end` when a stable
  session id is available.
- Use `opencortex.forget` only when the user explicitly requests deletion or
  forgetting.
"""


__all__ = ["MCP_CLIENT_RULES", "MCP_INSTRUCTIONS"]
