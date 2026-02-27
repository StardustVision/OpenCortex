#!/usr/bin/env python3
"""
Lightweight MCP HTTP client for OpenCortex hooks.

Calls MCP tools via streamable-http transport. Designed to be invoked
from Claude Code hooks (settings.json) as a replacement for npx ruvector.

Usage:
    uv run python scripts/mcp-call.py <tool_name> '<json_args>'
    uv run python scripts/mcp-call.py memory_search '{"query": "user preferences"}'
    uv run python scripts/mcp-call.py hooks_route '{"task": "edit file"}'

Environment:
    OPENCORTEX_MCP_URL  — MCP server URL (default: http://127.0.0.1:8920)
"""

import json
import sys
import os
from pathlib import Path


def _resolve_mcp_base_url() -> str:
    """Resolve MCP base URL (without /mcp suffix).

    Priority:
    1. OPENCORTEX_MCP_URL env var
    2. plugins/opencortex-memory/config.json (mode-aware)
    3. Default http://127.0.0.1:8920
    """
    env_url = os.environ.get("OPENCORTEX_MCP_URL")
    if env_url:
        return env_url.rstrip("/").removesuffix("/mcp")

    # Walk up from this script to find the project root (contains plugins/)
    script_dir = Path(__file__).resolve().parent
    for candidate in (script_dir.parent, Path.cwd()):
        cfg_path = candidate / "plugins" / "opencortex-memory" / "config.json"
        if cfg_path.is_file():
            try:
                cfg = json.loads(cfg_path.read_text())
                mode = cfg.get("mode", "local")
                if mode == "remote":
                    url = cfg.get("remote", {}).get("mcp_url", "")
                    if url:
                        return url.rstrip("/").removesuffix("/mcp")
                else:
                    port = cfg.get("local", {}).get("mcp_port", 8920)
                    return f"http://127.0.0.1:{port}"
            except (json.JSONDecodeError, OSError):
                pass
            break

    return "http://127.0.0.1:8920"


MCP_URL = _resolve_mcp_base_url()


def call_tool(tool_name: str, args: dict) -> dict:
    """Call an MCP tool via HTTP POST.

    Uses urllib to avoid external dependencies.
    """
    import urllib.request
    import urllib.error

    url = f"{MCP_URL}/mcp"
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": args,
        },
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            result = json.loads(body)
            # Extract content from MCP response
            if "result" in result:
                content = result["result"].get("content", [])
                if content and isinstance(content, list):
                    for item in content:
                        if item.get("type") == "text":
                            try:
                                return json.loads(item["text"])
                            except (json.JSONDecodeError, KeyError):
                                return {"text": item.get("text", "")}
                return result["result"]
            elif "error" in result:
                return {"error": result["error"].get("message", "Unknown error")}
            return result
    except urllib.error.URLError as e:
        return {"error": f"MCP server not reachable: {e}"}
    except Exception as e:
        return {"error": str(e)}


def main():
    if len(sys.argv) < 2:
        print("Usage: mcp-call.py <tool_name> [json_args]", file=sys.stderr)
        sys.exit(1)

    tool_name = sys.argv[1]
    args = {}
    if len(sys.argv) >= 3:
        try:
            args = json.loads(sys.argv[2])
        except json.JSONDecodeError:
            # Treat as simple string argument
            args = {"input": sys.argv[2]}

    result = call_tool(tool_name, args)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
