from __future__ import annotations

import argparse
import json
import sys
import time
import uuid

from .config import load_config
from .engine import flush, maybe_flush, sync_pending
from .models import MemoryEvent
from .spool import SpoolStore



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memcortex",
        description="MemCortex Phase-1 scaffold CLI",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Initialize local spool database")

    capture_parser = subparsers.add_parser("capture", help="Capture one event into local spool")
    capture_parser.add_argument("--source-tool", required=True)
    capture_parser.add_argument("--session-id", required=True)
    capture_parser.add_argument("--event-type", required=True)
    capture_parser.add_argument("--content")
    capture_parser.add_argument("--meta-json", default="{}")
    capture_parser.add_argument("--domain-hint")
    capture_parser.add_argument("--confidence", type=float)
    capture_parser.add_argument("--event-id")

    maybe_flush_parser = subparsers.add_parser(
        "maybe-flush", help="Flush only when thresholds are met"
    )
    maybe_flush_parser.add_argument("--local-only", action="store_true")

    flush_parser = subparsers.add_parser("flush", help="Force flush local queue")
    flush_parser.add_argument("--local-only", action="store_true")
    flush_parser.add_argument("--force", action="store_true")

    sync_parser = subparsers.add_parser("sync", help="Sync processed events to remote MCP")
    sync_parser.add_argument("--limit", type=int, default=50)

    subparsers.add_parser("status", help="Show queue and sync status")
    return parser



def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = load_config()

    if args.command == "init":
        SpoolStore(cfg.db_path)
        _print({"ok": True, "db_path": str(cfg.db_path), "home": str(cfg.home)})
        return 0

    if args.command == "capture":
        content = args.content if args.content is not None else sys.stdin.read().strip()
        if not content:
            _print({"ok": False, "error": "content is required"})
            return 2
        try:
            meta = json.loads(args.meta_json)
            if not isinstance(meta, dict):
                raise ValueError("meta-json must be an object")
        except Exception as exc:
            _print({"ok": False, "error": f"invalid meta-json: {exc}"})
            return 2

        event = MemoryEvent(
            event_id=args.event_id or f"evt_{uuid.uuid4().hex}",
            source_tool=args.source_tool,
            session_id=args.session_id,
            event_type=args.event_type,
            content=content,
            meta=meta,
            domain_hint=args.domain_hint,
            confidence=args.confidence,
            created_at=time.time(),
        )
        spool = SpoolStore(cfg.db_path)
        payload_hash = spool.add_event(event)
        _print({"ok": True, "event_id": event.event_id, "payload_hash": payload_hash})
        return 0

    if args.command == "maybe-flush":
        result = maybe_flush(cfg=cfg, local_only=args.local_only)
        _print({"ok": True, **result})
        return 0

    if args.command == "flush":
        result = flush(cfg=cfg, local_only=args.local_only, force=args.force)
        _print({"ok": True, **result})
        return 0

    if args.command == "sync":
        synced = sync_pending(cfg=cfg, limit=args.limit)
        _print({"ok": True, "synced": synced})
        return 0

    if args.command == "status":
        spool = SpoolStore(cfg.db_path)
        stats = spool.stats()
        _print({
            "ok": True,
            "backlog_count": stats.backlog_count,
            "oldest_age_seconds": stats.oldest_age_seconds,
            "pending_sync_count": stats.pending_sync_count,
            "db_bytes": stats.db_bytes,
            "db_path": str(cfg.db_path),
        })
        return 0

    _print({"ok": False, "error": "unknown command"})
    return 2



def _print(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    raise SystemExit(main())
