"""All numeric constants, bucket definitions, and display orders for insights."""

from typing import Dict, List, Tuple

# Session loading limits
MAX_SESSIONS_TO_LOAD = 200
MAX_FACET_EXTRACTIONS = 50
FACET_CONCURRENCY = 50
META_BATCH_SIZE = 50

# Transcript processing
TRANSCRIPT_THRESHOLD = 30000  # chars: summarize if over
CHUNK_SIZE = 25000            # chars per chunk

# Multi-clauding
OVERLAP_WINDOW_MS = 30 * 60 * 1000  # 30 minutes

# Response time
MIN_RESPONSE_TIME_SEC = 2
MAX_RESPONSE_TIME_SEC = 3600

# Filtering
MIN_USER_MESSAGES = 2
MIN_DURATION_MINUTES = 1

# Response time histogram buckets: (label, lower_bound, upper_bound)
RESPONSE_TIME_BUCKETS: List[Tuple[str, float, float]] = [
    ("2-10s",  2,   10),
    ("10-30s", 10,  30),
    ("30s-1m", 30,  60),
    ("1-2m",   60,  120),
    ("2-5m",   120, 300),
    ("5-15m",  300, 900),
    (">15m",   900, float("inf")),
]

# Display orders (fixed sort for charts)
SATISFACTION_ORDER: List[str] = [
    "frustrated", "dissatisfied", "likely_satisfied",
    "satisfied", "happy", "unsure",
]

OUTCOME_ORDER: List[str] = [
    "not_achieved", "partially_achieved", "mostly_achieved",
    "fully_achieved", "unclear_from_transcript",
]

# Language mapping (file extension → language name)
EXTENSION_TO_LANGUAGE: Dict[str, str] = {
    ".ts": "TypeScript", ".tsx": "TypeScript",
    ".js": "JavaScript", ".jsx": "JavaScript",
    ".py": "Python", ".rb": "Ruby", ".go": "Go",
    ".rs": "Rust", ".java": "Java", ".md": "Markdown",
    ".json": "JSON", ".yaml": "YAML", ".yml": "YAML",
    ".sh": "Shell", ".css": "CSS", ".html": "HTML",
}

# Error classification rules: (keywords_tuple, category_name)
ERROR_CATEGORIES: List[Tuple[Tuple[str, ...], str]] = [
    (("exit code",),                                    "Command Failed"),
    (("rejected", "doesn't want"),                      "User Rejected"),
    (("string to replace not found", "no changes"),     "Edit Failed"),
    (("modified since read",),                          "File Changed"),
    (("exceeds maximum", "too large"),                  "File Too Large"),
    (("file not found", "does not exist"),              "File Not Found"),
]
