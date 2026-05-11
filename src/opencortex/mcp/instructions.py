# SPDX-License-Identifier: Apache-2.0
"""Default MCP instructions and reusable prompt text for OpenCortex."""

from __future__ import annotations

MCP_INSTRUCTIONS = """OpenCortex is the user's long-term memory system.

Use OpenCortex proactively when prior context, preferences, decisions, resources,
project history, or durable facts may affect the answer.

Default workflow:
1. Before answering context-dependent questions, call opencortex.search with the
   user's current question. Use the returned memories/resources as private
   grounding context.
2. When the user shares durable facts, preferences, decisions, requirements, or
   important outcomes, store them with opencortex.store_memory or append the turn
   with opencortex.session_message.
3. At the end of a meaningful conversation session, call opencortex.session_end
   when a stable session_id is available.
4. Use opencortex.store_resource for documents, notes, or reference material the
   user wants OpenCortex to remember as a reusable resource.
5. Use opencortex.forget only when the user explicitly asks to delete or forget
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
- If the user provides durable facts, preferences, decisions, requirements,
  personal details, project facts, or important outcomes, call
  `opencortex.store_memory`.
- If a stable conversation/session id is available, prefer
  `opencortex.session_message` for each meaningful user/assistant turn.
- At the end of a meaningful session, call `opencortex.session_end` when a stable
  session id is available.
- Use `opencortex.store_resource` only for reusable documents, notes, or reference
  material.
- Use `opencortex.forget` only when the user explicitly requests deletion or
  forgetting.
"""


__all__ = ["MCP_CLIENT_RULES", "MCP_INSTRUCTIONS"]
