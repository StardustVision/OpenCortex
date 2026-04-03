"""
Patch application for skill evolution — ported from OpenSpace.

Supports three edit formats:
  FULL:  Complete content replacement (*** Begin Files / *** File: markers)
  PATCH: Multi-file diff format (*** Begin Patch / *** Update File / @@ anchors)
  DIFF:  Single-file SEARCH/REPLACE blocks (<<<<<<< SEARCH / ======= / >>>>>>> REPLACE)

4-level fuzzy anchor matching: exact -> rstrip -> strip -> unicode normalize.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple


class PatchType(str, Enum):
    FULL = "full"
    PATCH = "patch"
    DIFF = "diff"


@dataclass
class PatchResult:
    ok: bool
    content: str = ""
    error: str = ""
    applied_count: int = 0


def detect_patch_type(content: str) -> PatchType:
    """Auto-detect patch format from content."""
    if "*** Begin Patch" in content:
        return PatchType.PATCH
    if "*** Begin Files" in content or "*** File:" in content:
        return PatchType.FULL
    if "<<<<<<< SEARCH" in content:
        return PatchType.DIFF
    return PatchType.FULL


def apply_patch(original: str, patch_content: str, patch_type: Optional[PatchType] = None) -> PatchResult:
    """Apply a patch to original content.

    Args:
        original: Current skill content
        patch_content: The patch/edit to apply
        patch_type: Override auto-detection

    Returns:
        PatchResult with ok=True and new content, or ok=False with error
    """
    if patch_type is None:
        patch_type = detect_patch_type(patch_content)

    try:
        if patch_type == PatchType.FULL:
            return _apply_full(patch_content)
        elif patch_type == PatchType.DIFF:
            return _apply_search_replace(original, patch_content)
        elif patch_type == PatchType.PATCH:
            return _apply_anchor_patch(original, patch_content)
        else:
            return PatchResult(ok=False, error=f"Unknown patch type: {patch_type}")
    except Exception as exc:
        return PatchResult(ok=False, error=str(exc))


def _apply_full(content: str) -> PatchResult:
    """Apply FULL format -- extract content from *** File: markers or use as-is."""
    files = parse_multi_file_full(content)
    if files:
        # Return the first file's content (skills are typically single-file)
        main = files.get("SKILL.md", next(iter(files.values())))
        return PatchResult(ok=True, content=main, applied_count=1)
    # No markers found -- treat entire content as the replacement
    # Strip any *** Begin Files / *** End Files wrappers
    cleaned = content
    cleaned = re.sub(r'\*\*\* Begin Files\s*\n?', '', cleaned)
    cleaned = re.sub(r'\*\*\* End Files\s*\n?', '', cleaned)
    return PatchResult(ok=True, content=cleaned.strip(), applied_count=1)


def parse_multi_file_full(content: str) -> Dict[str, str]:
    """Parse *** File: markers into {filename: content} dict."""
    files: Dict[str, str] = {}
    current_file: Optional[str] = None
    current_lines: List[str] = []

    for line in content.split("\n"):
        match = re.match(r'\*\*\* File:\s*(.+)', line)
        if match:
            if current_file is not None:
                files[current_file] = "\n".join(current_lines).strip()
            current_file = match.group(1).strip()
            current_lines = []
        elif current_file is not None:
            if line.strip() in ("*** End Files", "*** Begin Files"):
                continue
            current_lines.append(line)

    if current_file is not None:
        files[current_file] = "\n".join(current_lines).strip()

    return files


def _apply_search_replace(original: str, patch: str) -> PatchResult:
    """Apply DIFF format (<<<<<<< SEARCH / ======= / >>>>>>> REPLACE)."""
    blocks = re.split(r'<<<<<<< SEARCH\s*\n', patch)
    if len(blocks) <= 1:
        return PatchResult(ok=False, error="No SEARCH blocks found")

    result = original
    applied = 0

    for block in blocks[1:]:
        parts = re.split(r'=======\s*\n', block, maxsplit=1)
        if len(parts) != 2:
            continue
        search_text = parts[0].rstrip("\n")
        replace_parts = re.split(r'>>>>>>> REPLACE\s*\n?', parts[1], maxsplit=1)
        if not replace_parts:
            continue
        replace_text = replace_parts[0].rstrip("\n")

        # Try 4-level fuzzy matching
        new_result = _fuzzy_replace(result, search_text, replace_text)
        if new_result is not None:
            result = new_result
            applied += 1

    if applied == 0:
        return PatchResult(ok=False, error="No SEARCH blocks matched")
    return PatchResult(ok=True, content=result, applied_count=applied)


def _apply_anchor_patch(original: str, patch: str) -> PatchResult:
    """Apply PATCH format (*** Update File / @@ anchor lines)."""
    # Extract the patch body between *** Begin Patch and *** End Patch
    match = re.search(r'\*\*\* Begin Patch\s*\n(.*?)\*\*\* End Patch', patch, re.DOTALL)
    if not match:
        return PatchResult(ok=False, error="No *** Begin Patch / *** End Patch found")

    body = match.group(1)
    result = original
    applied = 0

    # Split into per-file sections
    sections = re.split(r'\*\*\* (?:Update|Add|Delete) File:.*\n', body)
    for section in sections:
        if not section.strip():
            continue

        # Parse @@ anchor + hunks
        hunks = re.split(r'@@\s*(.*)\n', section)
        for i in range(1, len(hunks), 2):
            anchor = hunks[i].strip()
            hunk_body = hunks[i + 1] if i + 1 < len(hunks) else ""

            if not anchor:
                continue

            # Find anchor line in result
            anchor_idx = _find_anchor(result, anchor)
            if anchor_idx is None:
                continue

            # Apply hunk: - lines removed, + lines added, space lines kept
            lines = result.split("\n")
            new_lines = lines[:anchor_idx]
            hunk_lines = hunk_body.split("\n")
            skip_original = 0

            for hl in hunk_lines:
                if hl.startswith("-"):
                    skip_original += 1
                elif hl.startswith("+"):
                    new_lines.append(hl[1:])
                elif hl.startswith(" "):
                    new_lines.append(hl[1:])
                    skip_original += 1

            # Append remaining original lines after the hunk
            new_lines.extend(lines[anchor_idx + skip_original:])
            result = "\n".join(new_lines)
            applied += 1

    if applied == 0:
        return PatchResult(ok=False, error="No hunks applied")
    return PatchResult(ok=True, content=result, applied_count=applied)


def _find_anchor(text: str, anchor: str) -> Optional[int]:
    """Find anchor line in text with 4-level fuzzy matching."""
    lines = text.split("\n")

    # Level 1: Exact match
    for i, line in enumerate(lines):
        if line == anchor:
            return i

    # Level 2: After rstrip
    anchor_r = anchor.rstrip()
    for i, line in enumerate(lines):
        if line.rstrip() == anchor_r:
            return i

    # Level 3: After strip
    anchor_s = anchor.strip()
    for i, line in enumerate(lines):
        if line.strip() == anchor_s:
            return i

    # Level 4: Unicode normalize + strip
    anchor_n = unicodedata.normalize("NFC", anchor_s)
    for i, line in enumerate(lines):
        if unicodedata.normalize("NFC", line.strip()) == anchor_n:
            return i

    return None


def _fuzzy_replace(text: str, search: str, replace: str) -> Optional[str]:
    """Try to replace search text in text with 4-level fuzzy matching."""
    # Level 1: Exact
    if search in text:
        return text.replace(search, replace, 1)

    # Level 2: rstrip each line
    search_r = "\n".join(l.rstrip() for l in search.split("\n"))
    text_r = "\n".join(l.rstrip() for l in text.split("\n"))
    if search_r in text_r:
        idx = text_r.index(search_r)
        # Find corresponding position in original
        return text[:idx] + replace + text[idx + len(search_r):]

    # Level 3: strip each line
    search_lines = [l.strip() for l in search.split("\n") if l.strip()]
    text_lines = text.split("\n")
    match_start = _find_line_sequence(text_lines, search_lines)
    if match_start is not None:
        match_end = match_start + len(search_lines)
        before = "\n".join(text_lines[:match_start])
        after = "\n".join(text_lines[match_end:])
        if before:
            return before + "\n" + replace + "\n" + after
        return replace + "\n" + after

    # Level 4: Unicode normalize
    search_norm = [unicodedata.normalize("NFC", l) for l in search_lines]
    text_norm = [unicodedata.normalize("NFC", l.strip()) for l in text_lines]
    match_start = _find_line_sequence(text_norm, search_norm, already_stripped=True)
    if match_start is not None:
        match_end = match_start + len(search_norm)
        before = "\n".join(text_lines[:match_start])
        after = "\n".join(text_lines[match_end:])
        if before:
            return before + "\n" + replace + "\n" + after
        return replace + "\n" + after

    return None


def _find_line_sequence(
    haystack: List[str], needle: List[str], already_stripped: bool = False,
) -> Optional[int]:
    """Find a sequence of lines in a larger list."""
    if not needle:
        return None
    for i in range(len(haystack) - len(needle) + 1):
        match = True
        for j, n in enumerate(needle):
            h = haystack[i + j] if already_stripped else haystack[i + j].strip()
            if h != n:
                match = False
                break
        if match:
            return i
    return None
