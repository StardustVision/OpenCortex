# SPDX-License-Identifier: Apache-2.0
"""
RuleExtractor — Zero-LLM cost pattern extraction from stored memory content.

Extracts actionable skills from memory content using regex-based rules:
- Error → Fix patterns (traceback/error + subsequent fix)
- User preferences (always/never/prefer signals)
- Tool chain / workflow patterns (ordered multi-step operations)
"""

import logging
import re
from dataclasses import dataclass
from typing import List

logger = logging.getLogger(__name__)


@dataclass
class ExtractedSkill:
    """A skill extracted by RuleExtractor."""

    content: str
    section: str  # "error_fixes" | "preferences" | "workflows"
    evidence: str = ""  # Original text that triggered extraction


# ---------------------------------------------------------------------------
# Action verbs for granularity validation
# ---------------------------------------------------------------------------
_ACTION_VERBS_EN = {
    "use", "apply", "run", "execute", "install", "configure", "set",
    "add", "remove", "delete", "update", "replace", "convert", "check",
    "verify", "test", "build", "deploy", "start", "stop", "restart",
    "enable", "disable", "fix", "resolve", "detect", "retry", "skip",
    "switch", "migrate", "create", "ensure", "validate", "call", "wrap",
    "import", "export", "encode", "decode", "parse", "format",
}

_ACTION_VERBS_ZH = {
    "使用", "应用", "运行", "执行", "安装", "配置", "设置",
    "添加", "删除", "更新", "替换", "转换", "检查", "检测",
    "验证", "测试", "构建", "部署", "启动", "停止", "重启",
    "启用", "禁用", "修复", "解决", "重试", "跳过",
    "切换", "迁移", "创建", "确保", "调用", "封装",
    "导入", "导出", "编码", "解码", "解析", "格式化",
}

# Trivial single-command patterns (not learnable)
_TRIVIAL_CMD_RE = re.compile(
    r"^(ls|cd|cat|head|tail|pwd|echo|read|grep|find|which|type|file|stat|wc|"
    r"git\s+status|git\s+log|git\s+diff|git\s+branch)\b",
    re.IGNORECASE,
)

# Error/fix detection
_ERROR_RE = re.compile(
    r"(error|exception|traceback|failed|failure|crash|bug|issue|问题|错误|异常|失败)",
    re.IGNORECASE,
)
_FIX_RE = re.compile(
    r"(fix|resolve|solution|solved|workaround|修复|解决|方案|处理)",
    re.IGNORECASE,
)

# Causal structure patterns
_CAUSAL_RE = re.compile(
    r"(when|if|whenever|once|after|before|因为|当|如果|一旦|遇到).{5,}.{0,20}"
    r"(then|do|use|apply|run|should|must|need|try|就|则|应该|需要|可以|先|再)",
    re.IGNORECASE | re.DOTALL,
)

# Preference signal keywords
_PREFERENCE_RE = re.compile(
    r"(always|never|prefer|must\s+use|don'?t\s+use|avoid|"
    r"必须|总是|不要|禁止|偏好|首选|避免|优先)",
    re.IGNORECASE,
)

# Step enumeration patterns
_STEP_RE = re.compile(
    r"(?:step\s*\d|第\s*\d\s*步|\d+\.\s+\S|\d+\)\s+\S|→|->|然后|接着|最后)",
    re.IGNORECASE,
)


class RuleExtractor:
    """Extract actionable skills from memory content using regex rules.

    Zero LLM cost — all extraction is done via pattern matching.
    """

    # Content length filter: skip short/trivial content
    MIN_CONTENT_LEN = 100

    def extract(self, abstract: str, content: str) -> List[ExtractedSkill]:
        """Extract learnable patterns from stored memory content.

        Args:
            abstract: Memory abstract (L0).
            content: Full memory content (L2).

        Returns:
            List of validated ExtractedSkill instances.
        """
        if not content or len(content) < self.MIN_CONTENT_LEN:
            return []

        skills: List[ExtractedSkill] = []
        skills.extend(self._extract_error_fixes(content))
        skills.extend(self._extract_preferences(content))
        skills.extend(self._extract_tool_patterns(content))
        return [s for s in skills if self._validate_granularity(s)]

    # -----------------------------------------------------------------
    # Extraction methods
    # -----------------------------------------------------------------

    def _extract_error_fixes(self, content: str) -> List[ExtractedSkill]:
        """Extract error→fix patterns."""
        skills = []
        if not _ERROR_RE.search(content):
            return skills

        # Split into paragraphs and look for error-then-fix sequences
        paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
        for i, para in enumerate(paragraphs):
            if not _ERROR_RE.search(para):
                continue
            # Look for fix in same paragraph or next paragraph
            fix_text = ""
            if _FIX_RE.search(para):
                fix_text = para
            elif i + 1 < len(paragraphs) and _FIX_RE.search(paragraphs[i + 1]):
                fix_text = f"{para}\n{paragraphs[i + 1]}"

            if not fix_text:
                continue

            # Synthesize a skill from the error→fix pair
            skill_content = self._synthesize_error_fix(para, fix_text)
            if skill_content:
                skills.append(ExtractedSkill(
                    content=skill_content,
                    section="error_fixes",
                    evidence=fix_text[:200],
                ))
        return skills

    def _extract_preferences(self, content: str) -> List[ExtractedSkill]:
        """Extract user preference signals."""
        skills = []
        # Search line by line for preference keywords
        for line in content.split("\n"):
            line = line.strip()
            if not line or len(line) < 10:
                continue
            match = _PREFERENCE_RE.search(line)
            if match:
                # Use the line (or sentence containing the match) as the skill
                skill_content = self._extract_sentence(line, match.start())
                if skill_content and 10 <= len(skill_content) <= 300:
                    skills.append(ExtractedSkill(
                        content=skill_content,
                        section="preferences",
                        evidence=line[:200],
                    ))
        return skills

    def _extract_tool_patterns(self, content: str) -> List[ExtractedSkill]:
        """Extract tool chain / workflow patterns (≥3 ordered steps)."""
        skills = []
        # Find sequences of numbered/ordered steps
        step_matches = list(_STEP_RE.finditer(content))
        if len(step_matches) < 3:
            return skills

        # Extract the full workflow region, including the preceding line
        # for trigger context (e.g. "When deploying, then:")
        start = step_matches[0].start()
        prev_newline = content.rfind("\n", 0, start)
        if prev_newline > 0:
            prev_prev = content.rfind("\n", 0, prev_newline)
            start = (prev_prev + 1) if prev_prev >= 0 else 0
        elif prev_newline == 0:
            start = 0
        end = step_matches[-1].end()
        # Extend end to include the rest of the last step's line
        next_newline = content.find("\n", end)
        if next_newline > 0:
            end = next_newline
        workflow_text = content[start:end].strip()

        if 30 <= len(workflow_text) <= 300:
            skills.append(ExtractedSkill(
                content=workflow_text,
                section="workflows",
                evidence=workflow_text[:200],
            ))
        elif len(workflow_text) > 300:
            # Truncate long workflows to a summary
            lines = workflow_text.split("\n")
            truncated = "\n".join(lines[:6])
            if len(truncated) > 300:
                truncated = truncated[:297] + "..."
            skills.append(ExtractedSkill(
                content=truncated,
                section="workflows",
                evidence=workflow_text[:200],
            ))
        return skills

    # -----------------------------------------------------------------
    # Granularity validation
    # -----------------------------------------------------------------

    def _validate_granularity(self, skill: ExtractedSkill) -> bool:
        """Validate skill granularity — must be actionable, not trivial."""
        content = skill.content
        # 1. Length gate: 10-300 chars
        if len(content) < 10 or len(content) > 300:
            return False
        # 2. Must contain an action verb
        if not self._has_action_verb(content):
            return False
        # 3. Exclude trivial command records
        if self._is_trivial_command(content):
            return False
        # 4. Error fixes and workflows must have causal structure
        if skill.section in ("error_fixes", "workflows") and not self._has_causal_structure(content):
            return False
        return True

    def _has_action_verb(self, content: str) -> bool:
        """Check if content contains at least one action verb."""
        lower = content.lower()
        words = set(re.findall(r"[a-z]+", lower))
        if words & _ACTION_VERBS_EN:
            return True
        for verb in _ACTION_VERBS_ZH:
            if verb in content:
                return True
        return False

    def _is_trivial_command(self, content: str) -> bool:
        """Check if content is just a trivial single command."""
        stripped = content.strip()
        # Remove leading markdown markers
        stripped = re.sub(r"^[`$#>\-*\s]+", "", stripped)
        return bool(_TRIVIAL_CMD_RE.match(stripped))

    def _has_causal_structure(self, content: str) -> bool:
        """Check if content has a when-X-then-Y causal structure."""
        return bool(_CAUSAL_RE.search(content))

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    @staticmethod
    def _synthesize_error_fix(error_text: str, fix_text: str) -> str:
        """Synthesize a concise error→fix skill description."""
        # Extract key error phrase
        error_lines = [l.strip() for l in error_text.split("\n") if l.strip()]
        error_summary = error_lines[0] if error_lines else error_text[:80]
        if len(error_summary) > 80:
            error_summary = error_summary[:77] + "..."

        # Extract key fix phrase
        fix_lines = [l.strip() for l in fix_text.split("\n") if l.strip()]
        fix_summary = ""
        for line in fix_lines:
            if _FIX_RE.search(line):
                fix_summary = line
                break
        if not fix_summary and fix_lines:
            fix_summary = fix_lines[-1]
        if len(fix_summary) > 120:
            fix_summary = fix_summary[:117] + "..."

        result = f"当遇到 {error_summary} 时，{fix_summary}"
        if len(result) > 300:
            result = result[:297] + "..."
        return result

    @staticmethod
    def _extract_sentence(text: str, match_pos: int) -> str:
        """Extract the sentence containing the match position."""
        # Find sentence boundaries (period, newline, or string boundary)
        start = max(0, text.rfind(".", 0, match_pos) + 1)
        # Also check for Chinese sentence endings
        for sep in ("。", "\n"):
            alt_start = text.rfind(sep, 0, match_pos)
            if alt_start > start:
                start = alt_start + 1

        end = len(text)
        for sep in (".", "。", "\n"):
            next_end = text.find(sep, match_pos)
            if 0 <= next_end < end:
                end = next_end

        sentence = text[start:end].strip()
        return sentence
