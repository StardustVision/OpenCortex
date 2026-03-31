# Plugin Repository Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split `plugins/opencortex-memory/` into the independent repository at `/Users/hugo/CodeSpace/Work/OpenCortex-Memory` (`StardustVision/OpenCortex-Memory`) and replace the original directory with a git submodule.

**Architecture:** Copy all plugin files + test + skills to the new repo, update paths in the test file and metadata in package.json/plugin.json, then replace the plugin directory in the main repo with a submodule pointer.

**Tech Stack:** Git, Node.js, npm

---

### Task 1: Copy plugin files to new repo

**Files:**
- Source: `plugins/opencortex-memory/` (all files and directories)
- Destination: `/Users/hugo/CodeSpace/Work/OpenCortex-Memory/`

- [ ] **Step 1: Copy all plugin files**

```bash
# Copy everything except .git from plugin dir to new repo
cp -R /Users/hugo/CodeSpace/Work/OpenCortex/plugins/opencortex-memory/lib /Users/hugo/CodeSpace/Work/OpenCortex-Memory/
cp -R /Users/hugo/CodeSpace/Work/OpenCortex/plugins/opencortex-memory/bin /Users/hugo/CodeSpace/Work/OpenCortex-Memory/
cp -R /Users/hugo/CodeSpace/Work/OpenCortex/plugins/opencortex-memory/skills /Users/hugo/CodeSpace/Work/OpenCortex-Memory/
cp -R /Users/hugo/CodeSpace/Work/OpenCortex/plugins/opencortex-memory/.claude-plugin /Users/hugo/CodeSpace/Work/OpenCortex-Memory/
cp /Users/hugo/CodeSpace/Work/OpenCortex/plugins/opencortex-memory/.mcp.json /Users/hugo/CodeSpace/Work/OpenCortex-Memory/
cp /Users/hugo/CodeSpace/Work/OpenCortex/plugins/opencortex-memory/.npmrc /Users/hugo/CodeSpace/Work/OpenCortex-Memory/
cp /Users/hugo/CodeSpace/Work/OpenCortex/plugins/opencortex-memory/gemini-extension.json /Users/hugo/CodeSpace/Work/OpenCortex-Memory/
cp /Users/hugo/CodeSpace/Work/OpenCortex/plugins/opencortex-memory/package.json /Users/hugo/CodeSpace/Work/OpenCortex-Memory/
```

- [ ] **Step 2: Copy README from plugin (replaces placeholder)**

```bash
cp /Users/hugo/CodeSpace/Work/OpenCortex/plugins/opencortex-memory/README.md /Users/hugo/CodeSpace/Work/OpenCortex-Memory/README.md
```

- [ ] **Step 3: Verify file structure**

```bash
ls -la /Users/hugo/CodeSpace/Work/OpenCortex-Memory/
ls -la /Users/hugo/CodeSpace/Work/OpenCortex-Memory/lib/
ls -la /Users/hugo/CodeSpace/Work/OpenCortex-Memory/bin/
ls -la /Users/hugo/CodeSpace/Work/OpenCortex-Memory/skills/
```

Expected: all directories and files present matching the original plugin structure.

- [ ] **Step 4: Commit**

```bash
cd /Users/hugo/CodeSpace/Work/OpenCortex-Memory
git add -A
git commit -m "feat: import plugin files from OpenCortex monorepo"
```

---

### Task 2: Copy and adapt MCP test

**Files:**
- Source: `/Users/hugo/CodeSpace/Work/OpenCortex/tests/test_mcp_server.mjs`
- Create: `/Users/hugo/CodeSpace/Work/OpenCortex-Memory/tests/test_mcp_server.mjs`

- [ ] **Step 1: Create tests directory and copy test file**

```bash
mkdir -p /Users/hugo/CodeSpace/Work/OpenCortex-Memory/tests
cp /Users/hugo/CodeSpace/Work/OpenCortex/tests/test_mcp_server.mjs /Users/hugo/CodeSpace/Work/OpenCortex-Memory/tests/
```

- [ ] **Step 2: Update path references in test file**

The test currently uses:
```js
const PROJECT_ROOT = join(__dirname, '..');
const MCP_SERVER = join(PROJECT_ROOT, 'plugins', 'opencortex-memory', 'lib', 'mcp-server.mjs');
```

Change to:
```js
const PROJECT_ROOT = join(__dirname, '..');
const MCP_SERVER = join(PROJECT_ROOT, 'lib', 'mcp-server.mjs');
```

The test also spawns the HTTP server in local mode:
```js
httpServer = spawn('uv', ['run', 'python3', '-m', 'opencortex.http', ...], {
  cwd: PROJECT_ROOT,
```

Since the new repo does NOT contain the Python server, update the `before` hook to skip auto-starting the HTTP server and instead require it to be running externally:

```js
before(async () => {
  const ok = await healthCheck();
  if (!ok) throw new Error(
    `HTTP server unreachable at ${HTTP_URL}. Start it first: uv run opencortex-server --host 127.0.0.1 --port 8921`
  );
});
```

Remove the `after` hook that kills the httpServer (no longer needed).

Remove the `httpServer` variable declaration at the top.

- [ ] **Step 3: Add test script to package.json**

Add to the `"scripts"` section in `/Users/hugo/CodeSpace/Work/OpenCortex-Memory/package.json`:

```json
"test": "node --test tests/test_mcp_server.mjs"
```

- [ ] **Step 4: Commit**

```bash
cd /Users/hugo/CodeSpace/Work/OpenCortex-Memory
git add tests/test_mcp_server.mjs package.json
git commit -m "feat: add MCP tests, adapt paths for standalone repo"
```

---

### Task 3: Update package.json and plugin.json metadata

**Files:**
- Modify: `/Users/hugo/CodeSpace/Work/OpenCortex-Memory/package.json`
- Modify: `/Users/hugo/CodeSpace/Work/OpenCortex-Memory/.claude-plugin/plugin.json`

- [ ] **Step 1: Update package.json repository/homepage/bugs fields**

Change:
```json
{
  "repository": {
    "type": "git",
    "url": "git+https://github.com/StardustVision/OpenCortex.git",
    "directory": "plugins/opencortex-memory"
  },
  "homepage": "https://github.com/StardustVision/OpenCortex",
  "bugs": {
    "url": "https://github.com/StardustVision/OpenCortex/issues"
  }
}
```

To:
```json
{
  "repository": {
    "type": "git",
    "url": "git+https://github.com/StardustVision/OpenCortex-Memory.git"
  },
  "homepage": "https://github.com/StardustVision/OpenCortex-Memory",
  "bugs": {
    "url": "https://github.com/StardustVision/OpenCortex-Memory/issues"
  }
}
```

- [ ] **Step 2: Add `"files"` entry for skills and tests**

Current `"files"` array:
```json
"files": ["bin", "lib", ".mcp.json", "gemini-extension.json", "README.md"]
```

Add skills (for Claude plugin distribution):
```json
"files": ["bin", "lib", "skills", ".mcp.json", "gemini-extension.json", "README.md"]
```

- [ ] **Step 3: Update plugin.json repository field**

Change `"repository"` in `.claude-plugin/plugin.json` from:
```json
"repository": "https://github.com/StardustVision/OpenCortex"
```

To:
```json
"repository": "https://github.com/StardustVision/OpenCortex-Memory"
```

- [ ] **Step 4: Commit**

```bash
cd /Users/hugo/CodeSpace/Work/OpenCortex-Memory
git add package.json .claude-plugin/plugin.json
git commit -m "chore: update metadata to point to standalone repo"
```

---

### Task 4: Add .gitignore and LICENSE to new repo

**Files:**
- Create: `/Users/hugo/CodeSpace/Work/OpenCortex-Memory/.gitignore`
- Create: `/Users/hugo/CodeSpace/Work/OpenCortex-Memory/LICENSE`

- [ ] **Step 1: Create .gitignore**

```
node_modules/
.DS_Store
*.log
```

- [ ] **Step 2: Copy LICENSE from main repo (if exists) or create MIT license**

```bash
# Check if main repo has a LICENSE
ls /Users/hugo/CodeSpace/Work/OpenCortex/LICENSE* 2>/dev/null
```

If it exists, copy it. If not, create an MIT LICENSE since package.json declares `"license": "MIT"`.

- [ ] **Step 3: Commit**

```bash
cd /Users/hugo/CodeSpace/Work/OpenCortex-Memory
git add .gitignore LICENSE
git commit -m "chore: add .gitignore and LICENSE"
```

---

### Task 5: Push new repo to remote

- [ ] **Step 1: Push all commits**

```bash
cd /Users/hugo/CodeSpace/Work/OpenCortex-Memory
git push origin main
```

- [ ] **Step 2: Verify on remote**

```bash
cd /Users/hugo/CodeSpace/Work/OpenCortex-Memory
git log --oneline
```

Expected: 4 commits (initial + import + tests + metadata + gitignore).

---

### Task 6: Replace plugin directory with submodule in main repo

**Files:**
- Remove: `/Users/hugo/CodeSpace/Work/OpenCortex/plugins/opencortex-memory/` (entire directory)
- Remove: `/Users/hugo/CodeSpace/Work/OpenCortex/tests/test_mcp_server.mjs`
- Create: `.gitmodules` entry

- [ ] **Step 1: Remove the plugin directory from git tracking**

```bash
cd /Users/hugo/CodeSpace/Work/OpenCortex
git rm -rf plugins/opencortex-memory
```

- [ ] **Step 2: Remove the MCP test file**

```bash
cd /Users/hugo/CodeSpace/Work/OpenCortex
git rm tests/test_mcp_server.mjs
```

- [ ] **Step 3: Add submodule**

```bash
cd /Users/hugo/CodeSpace/Work/OpenCortex
git submodule add https://github.com/StardustVision/OpenCortex-Memory.git plugins/opencortex-memory
```

- [ ] **Step 4: Verify submodule**

```bash
cd /Users/hugo/CodeSpace/Work/OpenCortex
git submodule status
cat .gitmodules
```

Expected: `.gitmodules` contains the submodule entry pointing to `StardustVision/OpenCortex-Memory.git`.

- [ ] **Step 5: Commit**

```bash
cd /Users/hugo/CodeSpace/Work/OpenCortex
git add .gitmodules plugins/opencortex-memory
git commit -m "refactor: replace plugin directory with git submodule

plugins/opencortex-memory is now an independent repository:
https://github.com/StardustVision/OpenCortex-Memory"
```

---

### Task 7: Update main repo documentation

**Files:**
- Modify: `/Users/hugo/CodeSpace/Work/OpenCortex/CLAUDE.md`
- Modify: `/Users/hugo/CodeSpace/Work/OpenCortex/README.md`

- [ ] **Step 1: Update CLAUDE.md directory structure**

In the directory structure section, change:
```
plugins/opencortex-memory/       # MCP plugin (pure Node.js, no hooks)
  lib/common.mjs                 # Config discovery, state, uv/python detection, server launcher
  lib/http-client.mjs            # Native fetch wrapper + buildClientHeaders()
  lib/transcript.mjs             # JSONL parsing (diagnostic utility)
  lib/mcp-server.mjs             # MCP stdio server (9 tools + session lifecycle)
  bin/oc-cli.mjs                 # CLI tool
```

To:
```
plugins/opencortex-memory/       # Git submodule → github.com/StardustVision/OpenCortex-Memory
```

- [ ] **Step 2: Update CLAUDE.md test section**

Remove the Node.js MCP test entry:
```bash
# Node.js MCP tests (requires running HTTP server)
node --test tests/test_mcp_server.mjs
```

Replace with:
```bash
# Node.js MCP tests (in plugin submodule, requires running HTTP server)
cd plugins/opencortex-memory && npm test
```

- [ ] **Step 3: Update README.md plugin references**

Update the section that references `plugins/opencortex-memory` to note it's a submodule. Update the MCP test command to point to the submodule.

Add a note near the top about cloning with submodules:
```bash
git clone --recurse-submodules https://github.com/StardustVision/OpenCortex.git
```

- [ ] **Step 4: Commit**

```bash
cd /Users/hugo/CodeSpace/Work/OpenCortex
git add CLAUDE.md README.md
git commit -m "docs: update references for plugin submodule split"
```
