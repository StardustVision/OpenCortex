# Plugin Repository Split

Split `plugins/opencortex-memory/` into an independent repository `StardustVision/opencortex-memory`. The main OpenCortex repo retains a git submodule pointer.

## Motivation

Avoid pulling the entire server codebase when only the plugin (MCP server + CLI + skills) is needed. The plugin is already fully decoupled: pure Node.js, zero Python imports, communicates with the server exclusively via HTTP.

## New Repository: `StardustVision/opencortex-memory`

### Structure

```
StardustVision/opencortex-memory/
├── lib/                       # MCP server, HTTP client, common, setup, ui-server, transcript
├── bin/                       # oc-cli.mjs, oc-scan.mjs
├── skills/                    # 7 skills (memory-recall, memory-store, memory-stats, etc.)
├── tests/
│   └── test_mcp_server.mjs    # Moved from main repo tests/
├── .claude-plugin/
│   └── plugin.json
├── .mcp.json
├── .npmrc
├── gemini-extension.json
├── package.json
├── README.md
├── LICENSE
└── .gitignore
```

### Content Origin

All files come from the existing `plugins/opencortex-memory/` directory plus `tests/test_mcp_server.mjs`. Zero new code.

### package.json Changes

```json
{
  "repository": {
    "type": "git",
    "url": "git+https://github.com/StardustVision/opencortex-memory.git"
  },
  "homepage": "https://github.com/StardustVision/opencortex-memory",
  "bugs": {
    "url": "https://github.com/StardustVision/opencortex-memory/issues"
  }
}
```

Remove `"directory"` field (no longer a monorepo sub-directory).

### Git History

Fresh initial commit. No `git filter-branch` from main repo — path history diverges and benefit is minimal.

## Main Repository Changes

### 1. Submodule

Replace `plugins/opencortex-memory/` directory with a git submodule pointing to `StardustVision/opencortex-memory`.

```
[submodule "plugins/opencortex-memory"]
    path = plugins/opencortex-memory
    url = https://github.com/StardustVision/opencortex-memory.git
```

### 2. Remove MCP Test

Delete `tests/test_mcp_server.mjs` (now lives in the plugin repo).

### 3. Documentation Updates

- `CLAUDE.md`: mark plugin as submodule, update directory structure section
- `README.md` / `PKG-INFO`: update plugin references to point to new repo

## Version Strategy

Independent versioning from the split point onward. Both start at 0.6.4; subsequent releases evolve independently.

## npm Publishing

No change to npm publishing workflow — `opencortex-memory` package is already published from the plugin directory. The only difference is the source repo.

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| Submodule not initialized on clone | Document `git clone --recurse-submodules` in README |
| Version drift between server API and plugin | Plugin already handles this via HTTP; no tight coupling |
| CI needs both repos | Submodule auto-checkout in CI config |
