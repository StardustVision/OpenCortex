"""LLM prompt templates for CC-equivalent insights generation."""

FACET_EXTRACTION_PROMPT = """\
Analyze this Claude Code session and extract structured facets.

CRITICAL GUIDELINES:

1. **goal_categories**: Count ONLY what the USER explicitly asked for.
   - DO NOT count Claude's autonomous codebase exploration
   - DO NOT count work Claude decided to do on its own
   - ONLY count when user says "can you...", "please...", "I need...", "let's..."

2. **user_satisfaction_counts**: Base ONLY on explicit user signals.
   - "Yay!", "great!", "perfect!" -> happy
   - "thanks", "looks good", "that works" -> satisfied
   - "ok, now let's..." (continuing without complaint) -> likely_satisfied
   - "that's not right", "try again" -> dissatisfied
   - "this is broken", "I give up" -> frustrated

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
  "goal_categories": {{"category_name": "count (integer)"}},
  "outcome": "fully_achieved|mostly_achieved|partially_achieved|not_achieved|unclear_from_transcript",
  "user_satisfaction_counts": {{"level": "count (integer)"}},
  "claude_helpfulness": "unhelpful|slightly_helpful|moderately_helpful|very_helpful|essential",
  "session_type": "single_task|multi_task|iterative_refinement|exploration|quick_question",
  "friction_counts": {{"friction_type": "count (integer)"}},
  "friction_detail": "One sentence describing friction or empty",
  "primary_success": "none|fast_accurate_search|correct_code_edits|good_explanations|proactive_help|multi_file_changes|good_debugging",
  "brief_summary": "One sentence: what user wanted and whether they got it",
  "user_instructions_to_claude": ["instruction1", "instruction2"]
}}
"""

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

PROJECT_AREAS_PROMPT = """\
Analyze this Claude Code usage data and identify project areas.

RESPOND WITH ONLY A VALID JSON OBJECT:
{{
  "areas": [
    {{"name": "Area name", "session_count": 0, "description": "2-3 sentences about what was worked on."}}
  ]
}}

Include 4-5 areas. Skip internal operations.

DATA:
{data_context}
"""

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

SUGGESTIONS_PROMPT = """\
Analyze this usage data and suggest improvements.

## OC FEATURES REFERENCE (pick from these for features_to_try):
1. **Memory Feedback** (store + feedback): Reinforce useful memories with +1 reward, penalize irrelevant ones with -1. Adjusts future retrieval ranking through reinforcement learning.
   - How to use: After recalling a useful memory, call `feedback(uri, +1.0)`. For irrelevant recalls, `feedback(uri, -1.0)`.
   - Good for: Training your memory system to surface the right context automatically.

2. **Knowledge Pipeline**: Automatic knowledge extraction from session traces via Observer -> TraceSplitter -> Archivist -> Sandbox -> KnowledgeStore.
   - How to use: Enable `trace_splitter: true` in server config. Knowledge candidates appear for review.
   - Good for: Building an approved knowledge base from your work patterns, error fixes, and decisions.

3. **Batch Import** (batch_store): Import multiple documents, scan results, or file trees in one call.
   - How to use: `batch_store(items=[...], source_path="/project")` with file_path metadata for directory tree.
   - Good for: Onboarding project documentation, importing existing notes, bulk ingestion.

4. **Semantic Search** (recall): Intent-aware retrieval that analyzes your query to determine search strategy.
   - How to use: `recall(query="how did we handle auth?")` -- system auto-detects intent and adjusts top_k/detail.
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
