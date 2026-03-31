"""LLM prompt templates for insights generation."""

FACET_EXTRACTION_PROMPT = """\
You are analyzing a development session transcript to extract structured facets about the developer's work.

Session Transcript:
{session_transcript}

Please analyze this session and extract the following facets as a JSON object:
- project: Name of the project being worked on
- language: Primary programming language(s) used
- tasks: List of main tasks/work items completed or attempted
- errors_encountered: Any errors, bugs, or issues encountered
- solutions_applied: Solutions implemented or attempted
- tools_used: Development tools, frameworks, or libraries mentioned
- decisions_made: Key technical decisions or tradeoffs discussed

Return ONLY a valid JSON object with these keys. Ensure all values are strings or arrays of strings.
"""

CHUNK_SUMMARY_PROMPT = """\
Summarize the following chunk of a development session transcript into a concise 2-3 sentence summary.

Chunk Content:
{chunk_content}

Focus on the main work done, any problems encountered, and solutions applied.
Return ONLY the summary text, no formatting or additional text.
"""

PROJECT_AREAS_PROMPT = """\
Based on the following summary of multiple development sessions, identify the main project areas being worked on.

Sessions Summary:
{sessions_summary}

Extract and return a JSON object with:
- areas: List of distinct project areas (e.g., "API Development", "Frontend Components", "Database Schema")
- focus_distribution: Approximate percentage of effort on each area
- cross_cutting_concerns: Areas that appear across multiple projects

Return ONLY a valid JSON object.
"""

WHAT_WORKS_PROMPT = """\
Analyze the following session data to identify successful workflows, patterns, and approaches that are working well.

Session Data:
{session_data}

Identify:
- successful_patterns: Development patterns that led to quick solutions
- effective_tools: Tools or approaches that increased productivity
- workflow_strengths: Aspects of the workflow that are efficient
- repeated_successes: Problems that were solved efficiently multiple times

Return as a JSON object with these keys. Each value should be a list of strings describing the pattern/tool/strength.
"""

FRICTION_ANALYSIS_PROMPT = """\
Analyze the following session data to identify friction points, blockers, and areas causing delays or frustration.

Session Data:
{session_data}

Identify:
- blockers: Technical or procedural issues that completely blocked progress
- repeated_issues: Problems that appear multiple times across sessions
- inefficient_processes: Workflows or steps that are unnecessarily complex
- debugging_friction: Difficulty in diagnosing or fixing issues
- tool_friction: Tools or configurations causing problems

Return as a JSON object with these keys. Each value should be a list of strings describing the friction point.
"""

SUGGESTIONS_PROMPT = """\
Based on the following analysis findings (what works and friction points), generate concrete improvement suggestions.

Findings:
{findings}

Generate:
- quick_wins: Easy changes that could remove friction immediately
- process_improvements: Workflow or procedural changes
- tool_recommendations: New tools or configurations to consider
- learning_areas: Areas that could benefit from more expertise
- automation_opportunities: Repetitive tasks that could be automated

Return as a JSON object with these keys. Each value should be a list of strings with specific, actionable suggestions.
"""

ON_THE_HORIZON_PROMPT = """\
Based on the following developer context, identify emerging opportunities, upcoming work, and areas of growth.

Context:
{context}

Identify:
- emerging_patterns: New technologies or approaches the developer is exploring
- upcoming_features: Features or improvements being planned
- skill_development_areas: Technical areas where the developer is building expertise
- architectural_evolution: How the project architecture is likely to evolve
- new_problem_domains: New types of problems the developer is starting to tackle

Return as a JSON object with these keys. Each value should be a list of strings describing the opportunity or emerging area.
"""

AT_A_GLANCE_PROMPT = """\
Create a brief, high-level summary of the developer's work that can be understood at a glance.

Insights Data:
{insights_data}

Generate a JSON object with:
- headline: One sentence capturing the essence of recent work
- main_activities: 3-4 bullet points of what was accomplished
- key_challenges: Main obstacles encountered
- momentum: Assessment of progress (e.g., "Strong progress on API", "Debugging phase", etc.)
- next_focus: Likely direction for upcoming work

Return as a JSON object with these keys. Keep descriptions concise but meaningful.
"""
