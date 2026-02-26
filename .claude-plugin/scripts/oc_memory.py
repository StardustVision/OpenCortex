#!/usr/bin/env python3
"""OpenCortex memory bridge for Claude Code hooks.

This script provides a stable CLI interface for hook shell scripts:
- session-start: initialize orchestrator, create session marker, recall context
- ingest-stop: parse transcript last turn, summarize, store as memory
- session-end: store final session summary, clean up
- recall: search extracted memories for skill-based retrieval

Config is read from opencortex.json (tenant_id / user_id for isolation).
Session state persists in {project}/.opencortex/memory/session_state.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return _load_json(path)
    except Exception:
        return {}


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _short(text: str, n: int) -> str:
    t = " ".join(text.split())
    if len(t) <= n:
        return t
    return t[: n - 3] + "..."


# ---------------------------------------------------------------------------
# Transcript parsing (adapted from reference ov_memory.py)
# ---------------------------------------------------------------------------

def _extract_text_parts(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    chunks: List[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = _as_text(block.get("text", ""))
            if text:
                chunks.append(text)
    return "\n".join(chunks).strip()


def _extract_tool_result(content: Any) -> str:
    if not isinstance(content, list) or not content:
        return ""
    first = content[0]
    if not isinstance(first, dict) or first.get("type") != "tool_result":
        return ""
    payload = first.get("content")
    if isinstance(payload, str):
        return _short(payload, 220)
    if isinstance(payload, list):
        buf: List[str] = []
        for item in payload:
            if isinstance(item, dict) and item.get("type") == "text":
                t = _as_text(item.get("text", ""))
                if t:
                    buf.append(t)
        return _short("\n".join(buf), 220)
    return _short(_as_text(payload), 220)


def _is_user_prompt(entry: Dict[str, Any]) -> bool:
    if entry.get("type") != "user":
        return False
    msg = entry.get("message", {})
    content = msg.get("content")
    if _extract_tool_result(content):
        return False
    return bool(_extract_text_parts(content))


def _assistant_chunks(entry: Dict[str, Any]) -> List[str]:
    if entry.get("type") != "assistant":
        return []
    msg = entry.get("message", {})
    content = msg.get("content")
    if isinstance(content, str):
        text = _as_text(content)
        return [text] if text else []
    if not isinstance(content, list):
        return []
    chunks: List[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = _as_text(block.get("text", ""))
            if text:
                chunks.append(text)
        elif btype == "tool_use":
            name = _as_text(block.get("name", "tool"))
            raw_input = block.get("input")
            try:
                inp = _short(json.dumps(raw_input, ensure_ascii=False), 180)
            except Exception:
                inp = _short(_as_text(raw_input), 180)
            chunks.append(f"[tool-use] {name}({inp})")
    return chunks


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def extract_last_turn(transcript_path: Path) -> Optional[Dict[str, str]]:
    """Extract the last user-assistant turn from a Claude transcript."""
    rows = _read_jsonl(transcript_path)
    if not rows:
        return None

    last_user_idx = -1
    for i, row in enumerate(rows):
        if _is_user_prompt(row):
            last_user_idx = i

    if last_user_idx < 0:
        return None

    user_row = rows[last_user_idx]
    user_text = _extract_text_parts(user_row.get("message", {}).get("content"))
    turn_uuid = _as_text(user_row.get("uuid") or user_row.get("id"))

    chunks: List[str] = []
    for row in rows[last_user_idx + 1:]:
        if _is_user_prompt(row):
            break
        if row.get("type") == "assistant":
            chunks.extend(_assistant_chunks(row))
            continue
        if row.get("type") == "user":
            tool_result = _extract_tool_result(row.get("message", {}).get("content"))
            if tool_result:
                chunks.append(f"[tool-result] {tool_result}")

    assistant_text = "\n".join([c for c in chunks if c]).strip()

    if not turn_uuid:
        turn_uuid = str(abs(hash(user_text + assistant_text)))

    if not user_text and not assistant_text:
        return None

    return {
        "turn_uuid": turn_uuid,
        "user_text": user_text,
        "assistant_text": assistant_text,
    }


# ---------------------------------------------------------------------------
# Summarization
# ---------------------------------------------------------------------------

def _summarize_with_claude(raw: str) -> str:
    """Use claude CLI to summarize a conversation turn."""
    if not shutil.which("claude"):
        return ""
    system_prompt = (
        "You are a session memory writer. Output ONLY 3-6 bullet points. "
        "Each line must start with '- '. Focus on decisions, fixes, and concrete changes. "
        "No intro or outro."
    )
    try:
        proc = subprocess.run(
            [
                "claude", "-p",
                "--model", "haiku",
                "--no-session-persistence",
                "--no-chrome",
                "--system-prompt", system_prompt,
            ],
            input=raw,
            text=True,
            capture_output=True,
            timeout=45,
            check=False,
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _fallback_summary(turn: Dict[str, str]) -> str:
    user = _short(turn.get("user_text", ""), 200)
    assistant = _short(turn.get("assistant_text", ""), 360)
    lines = []
    if user:
        lines.append(f"- User request: {user}")
    if assistant:
        lines.append(f"- Assistant response: {assistant}")
    if not lines:
        lines.append("- Captured a conversation turn.")
    return "\n".join(lines)


def summarize_turn(turn: Dict[str, str]) -> str:
    raw = (
        "Summarize this conversation turn for long-term engineering memory.\n\n"
        f"User:\n{turn.get('user_text', '')}\n\n"
        f"Assistant:\n{turn.get('assistant_text', '')}\n"
    )
    summary = _summarize_with_claude(raw)
    if summary:
        return summary
    return _fallback_summary(turn)


# ---------------------------------------------------------------------------
# Orchestrator access
# ---------------------------------------------------------------------------

def _find_config_path(project_dir: Path, explicit: Optional[str] = None) -> Optional[Path]:
    """Find opencortex.json config file."""
    if explicit:
        p = Path(explicit)
        if p.exists():
            return p

    # Search in project dir, then current dir
    for name in ["opencortex.json", ".opencortex.json"]:
        candidate = project_dir / name
        if candidate.exists():
            return candidate

    return None


def _init_orchestrator(config_path: Path):
    """Initialize and return a MemoryOrchestrator (sync wrapper)."""
    # Add project src to path so opencortex can be imported
    project_dir = config_path.parent
    src_dir = project_dir / "src"
    if src_dir.exists() and str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))

    from opencortex.config import CortexConfig, init_config
    from opencortex.orchestrator import MemoryOrchestrator

    config = CortexConfig.load(str(config_path))
    init_config(config)
    orch = MemoryOrchestrator(config=config)
    return orch


async def _get_orchestrator(config_path: Path):
    """Get an initialized orchestrator."""
    orch = _init_orchestrator(config_path)
    await orch.init()
    return orch


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_session_start(args: argparse.Namespace) -> Dict[str, Any]:
    """Initialize session and recall context."""
    project_dir = Path(args.project_dir).resolve()
    state_file = Path(args.state_file)
    config_path = _find_config_path(project_dir, args.config)

    if not config_path:
        return {
            "ok": False,
            "status_line": "[opencortex-memory] ERROR: opencortex.json not found",
            "error": "config not found",
        }

    config_data = _load_json(config_path)
    tenant_id = config_data.get("tenant_id", "default")
    user_id = config_data.get("user_id", "default")

    # Save session state
    state = {
        "active": True,
        "project_dir": str(project_dir),
        "config_path": str(config_path),
        "tenant_id": tenant_id,
        "user_id": user_id,
        "last_turn_uuid": "",
        "ingested_turns": 0,
        "started_at": int(time.time()),
    }
    _save_json(state_file, state)

    status = (
        f"[opencortex-memory] session started "
        f"tenant={tenant_id} user={user_id}"
    )

    additional = (
        "OpenCortex memory is active. "
        "For historical context, use the memory-recall skill when needed."
    )

    return {
        "ok": True,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "status_line": status,
        "additional_context": additional,
    }


def cmd_ingest_stop(args: argparse.Namespace) -> Dict[str, Any]:
    """Parse last transcript turn, summarize, and store as memory."""
    state_file = Path(args.state_file)
    transcript = Path(args.transcript_path)

    state = _load_state(state_file)
    if not state.get("active"):
        return {"ok": True, "ingested": False, "reason": "inactive session"}
    if not transcript.exists():
        return {"ok": True, "ingested": False, "reason": "transcript not found"}

    config_path_str = state.get("config_path")
    if not config_path_str or not Path(config_path_str).exists():
        return {"ok": True, "ingested": False, "reason": "config not found"}

    # Parse transcript
    turn = extract_last_turn(transcript)
    if not turn:
        return {"ok": True, "ingested": False, "reason": "no turn parsed"}

    # Dedup: skip if same turn
    if _as_text(turn.get("turn_uuid")) == _as_text(state.get("last_turn_uuid")):
        return {"ok": True, "ingested": False, "reason": "duplicate turn"}

    # Summarize the turn
    summary = summarize_turn(turn)
    user_text = _as_text(turn.get("user_text"))
    if not user_text:
        user_text = "(No user prompt captured)"

    # Build memory content
    abstract = f"Session turn: {_short(user_text, 120)}"
    content_parts = [f"User: {user_text}"]
    if summary:
        content_parts.append(f"\nSummary:\n{summary}")

    assistant_excerpt = _as_text(turn.get("assistant_text"))
    if assistant_excerpt:
        content_parts.append(f"\nAssistant excerpt:\n{_short(assistant_excerpt, 1500)}")

    content = "\n".join(content_parts)

    # Store via orchestrator
    config_path = Path(config_path_str)
    try:
        async def _store():
            orch = await _get_orchestrator(config_path)
            try:
                result = await orch.add(
                    abstract=abstract,
                    content=content,
                    category="session",
                    context_type="memory",
                    meta={
                        "turn_uuid": turn.get("turn_uuid", ""),
                        "source": "hook:stop",
                        "timestamp": int(time.time()),
                    },
                )
                return result.uri
            finally:
                await orch.close()

        uri = asyncio.run(_store())
    except Exception as exc:
        return {
            "ok": False,
            "ingested": False,
            "error": str(exc),
        }

    # Update state
    state["last_turn_uuid"] = _as_text(turn.get("turn_uuid"))
    state["ingested_turns"] = int(state.get("ingested_turns", 0)) + 1
    state["last_ingested_at"] = int(time.time())
    _save_json(state_file, state)

    return {
        "ok": True,
        "ingested": True,
        "uri": uri,
        "turn_uuid": turn.get("turn_uuid"),
        "ingested_turns": state.get("ingested_turns"),
    }


def cmd_session_end(args: argparse.Namespace) -> Dict[str, Any]:
    """Finalize session: store session summary memory."""
    state_file = Path(args.state_file)
    state = _load_state(state_file)

    if not state.get("active"):
        return {
            "ok": True,
            "committed": False,
            "status_line": "[opencortex-memory] no active session",
        }

    config_path_str = state.get("config_path")
    ingested = int(state.get("ingested_turns", 0))

    # Store session-level summary if we ingested turns
    if ingested > 0 and config_path_str and Path(config_path_str).exists():
        config_path = Path(config_path_str)
        started_at = state.get("started_at", 0)
        duration = int(time.time()) - started_at if started_at else 0

        try:
            async def _store_session():
                orch = await _get_orchestrator(config_path)
                try:
                    await orch.add(
                        abstract=f"Session summary: {ingested} turns, {duration}s duration",
                        content=(
                            f"Session completed.\n"
                            f"Tenant: {state.get('tenant_id', 'default')}\n"
                            f"User: {state.get('user_id', 'default')}\n"
                            f"Turns ingested: {ingested}\n"
                            f"Duration: {duration}s\n"
                            f"Started: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(started_at))}\n"
                        ),
                        category="session_summary",
                        context_type="memory",
                        meta={
                            "source": "hook:session-end",
                            "ingested_turns": ingested,
                            "duration": duration,
                            "timestamp": int(time.time()),
                        },
                    )
                finally:
                    await orch.close()

            asyncio.run(_store_session())
        except Exception:
            pass  # Best-effort; don't block session end

    # Mark session inactive
    state["active"] = False
    state["ended_at"] = int(time.time())
    _save_json(state_file, state)

    status = (
        f"[opencortex-memory] session ended "
        f"turns={ingested}"
    )

    return {
        "ok": True,
        "committed": True,
        "ingested_turns": ingested,
        "status_line": status,
    }


def cmd_recall(args: argparse.Namespace) -> int:
    """Search stored memories and print results."""
    project_dir = Path(args.project_dir).resolve()
    state_file = Path(args.state_file)
    query = _as_text(args.query)

    if not query:
        print("No relevant memories found.")
        return 0

    config_path = _find_config_path(project_dir, args.config)
    if not config_path:
        print("Memory unavailable: opencortex.json not found")
        return 0

    try:
        async def _search():
            orch = await _get_orchestrator(config_path)
            try:
                result = await orch.search(
                    query=query,
                    limit=args.top_k,
                )
                return result
            finally:
                await orch.close()

        result = asyncio.run(_search())
    except Exception as exc:
        print(f"Memory recall failed: {exc}")
        return 1

    # Collect all results
    all_items = []
    for item in getattr(result, "memories", []) or []:
        all_items.append(item)
    for item in getattr(result, "resources", []) or []:
        all_items.append(item)
    for item in getattr(result, "skills", []) or []:
        all_items.append(item)

    if not all_items:
        print("No relevant memories found.")
        return 0

    # Sort by score descending
    all_items.sort(
        key=lambda x: float(getattr(x, "score", 0.0) or 0.0),
        reverse=True,
    )
    all_items = all_items[: args.top_k]

    output_lines = [f"Relevant memories for: {query}", ""]

    for i, item in enumerate(all_items, start=1):
        uri = _as_text(getattr(item, "uri", ""))
        score = float(getattr(item, "score", 0.0) or 0.0)
        abstract = _as_text(getattr(item, "abstract", ""))
        ctx_type = _as_text(getattr(item, "context_type", ""))

        output_lines.append(f"{i}. [{score:.3f}] {uri}")
        if ctx_type:
            output_lines.append(f"   type: {ctx_type}")
        if abstract:
            output_lines.append(f"   abstract: {_short(abstract, 220)}")
        output_lines.append("")

    print("\n".join(output_lines).strip())
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OpenCortex memory bridge")
    parser.add_argument("--project-dir", required=True, help="Claude project directory")
    parser.add_argument("--state-file", required=True, help="Session state file path")
    parser.add_argument("--config", default=None, help="Path to opencortex.json")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("session-start", help="Start memory session")

    p_stop = sub.add_parser("ingest-stop", help="Ingest last transcript turn")
    p_stop.add_argument("--transcript-path", required=True, help="Claude transcript path")

    sub.add_parser("session-end", help="End memory session")

    p_recall = sub.add_parser("recall", help="Search stored memories")
    p_recall.add_argument("--query", required=True, help="Recall query")
    p_recall.add_argument("--top-k", type=int, default=5, help="Number of results")

    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    try:
        if args.command == "session-start":
            print(json.dumps(cmd_session_start(args), ensure_ascii=False))
            return 0

        if args.command == "ingest-stop":
            print(json.dumps(cmd_ingest_stop(args), ensure_ascii=False))
            return 0

        if args.command == "session-end":
            print(json.dumps(cmd_session_end(args), ensure_ascii=False))
            return 0

        if args.command == "recall":
            return cmd_recall(args)

        parser.error(f"Unknown command: {args.command}")
        return 2

    except Exception as exc:
        if args.command == "recall":
            print(f"Memory recall failed: {exc}")
            return 1
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
