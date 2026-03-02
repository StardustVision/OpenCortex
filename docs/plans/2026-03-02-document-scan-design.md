# OpenCortex Document Scan 设计方案

> 状态: **Pending**
> 创建: 2026-03-02

## 1. 背景与问题

当前 OpenCortex 缺少文档批量导入能力。用户需要一个**记忆初始化**功能：扫描项目目录中的文档和代码文件，分块后批量导入为记忆，让 agent 快速了解项目上下文。

### 1.1 OpenViking 参考架构

OpenViking 的文件 load 采用三阶段流水线：

```
Client → POST /api/v1/resources {path, target, instruction, wait}
    ↓
ResourceService.add_resource()
    ↓
ResourceProcessor.process_resource()
    ├─ Phase 1: scan_directory() → ClassifiedFile[] (processable / unsupported)
    ├─ Phase 2: TreeBuilder.finalize_from_temp() → 移入 AGFS
    └─ Phase 3: SemanticQueue → L0/L1 生成 + 向量化（异步）
```

**关键参考点**：

| 组件 | 文件 | 借鉴 |
|------|------|------|
| `scan_directory()` | `openviking/parse/directory_scan.py` | 文件分类（processable/unsupported/skipped）、include/exclude glob、SKIP_DIRS |
| `DirectoryParser` | `openviking/parse/parsers/directory.py` | per-file parser 分派模式 |
| `AddResourceRequest` | `openviking/server/routers/resources.py` | HTTP 请求模型设计 |
| `ClassifiedFile` | `openviking/parse/directory_scan.py:30-36` | 文件分类数据结构 |

### 1.2 架构决策：扫描在 Agent 侧

与 OpenViking 不同，OpenCortex 的扫描发生在 **agent 侧**（客户端），而非服务端：

- HTTP server 可能运行在远端（Docker、云端），无法访问项目文件
- Agent（Claude Code）直接访问本地文件系统
- 扫描结果通过 HTTP 批量上传到服务端

## 2. 设计目标

1. 用户通过 **Skill**（`/memory-scan`）触发，Skill 指导 Claude 完成扫描和上传。
2. 服务端新增 **batch_store** 端点，支持一次请求上传多个 chunk。
3. 同时提供 **MCP tool** 作为程序化入口。
4. Markdown 按 heading 分块，代码/文本整文件存储。
5. 幂等：确定性 URI + upsert 去重。

## 3. 非目标

1. 不在服务端实现文件系统扫描逻辑。
2. 不实现文件上传（multipart upload），只接受文本内容。
3. 不实现 OpenViking 的 VLM/MediaProcessor。
4. 不实现 SemanticQueue 异步向量化（用 add() 同步嵌入）。

## 4. 架构

```
用户触发 /memory-scan
    ↓
Skill (SKILL.md) 指导 Claude:
    1. bash: git ls-files / find 发现文件
    2. bash: node plugins/opencortex-memory/bin/oc-scan.mjs <path> → JSON
    3. bash: curl POST /api/v1/memory/batch_store（或 MCP tool memory_batch_store）
    ↓
HTTP Server:
    POST /api/v1/memory/batch_store
        ↓
    orchestrator.batch_add(items)
        ↓
    for item in items:
        await self.add(abstract, content, uri, ...)
            → embed → Qdrant upsert → CortexFS write → async skill extraction
```

## 5. 服务端改造

### 5.1 新增 HTTP 端点：`POST /api/v1/memory/batch_store`

**请求模型**（`src/opencortex/http/models.py`）：

```python
class MemoryBatchItem(BaseModel):
    abstract: str
    content: str = ""
    overview: str = ""
    category: str = ""
    context_type: str = "resource"
    uri: Optional[str] = None
    parent_uri: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None

class MemoryBatchStoreRequest(BaseModel):
    items: List[MemoryBatchItem]
    source_path: str = ""           # 扫描的目录路径
    scan_meta: Optional[Dict[str, Any]] = None  # {total_files, scan_time, ...}
```

**响应**：

```json
{
    "status": "ok",
    "total": 15,
    "imported": 14,
    "errors": [{"index": 7, "uri": "...", "error": "embed failed"}]
}
```

**路由**（`src/opencortex/http/server.py`）：

```python
@app.post("/api/v1/memory/batch_store")
async def memory_batch_store(req: MemoryBatchStoreRequest) -> Dict[str, Any]:
    return await _orchestrator.batch_add(
        items=[item.model_dump() for item in req.items],
        source_path=req.source_path,
        scan_meta=req.scan_meta,
    )
```

### 5.2 Orchestrator 新增 `batch_add()`

**位置**：`src/opencortex/orchestrator.py`

```python
async def batch_add(
    self,
    items: List[Dict[str, Any]],
    source_path: str = "",
    scan_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """批量添加记忆，用于文档扫描导入。

    对每个 item 调用 self.add()，收集结果。
    """
    self._ensure_init()
    imported = 0
    errors = []
    for i, item in enumerate(items):
        try:
            await self.add(
                abstract=item["abstract"],
                content=item.get("content", ""),
                overview=item.get("overview", ""),
                category=item.get("category", ""),
                context_type=item.get("context_type", "resource"),
                uri=item.get("uri"),
                parent_uri=item.get("parent_uri"),
                meta=item.get("meta"),
            )
            imported += 1
        except Exception as exc:
            errors.append({"index": i, "uri": item.get("uri", ""), "error": str(exc)})
            if len(errors) > 50:
                break
    return {
        "status": "ok" if not errors else "partial",
        "total": len(items),
        "imported": imported,
        "errors": errors[:20],
    }
```

### 5.3 MCP Tool 新增 `memory_batch_store`

**位置**：`plugins/opencortex-memory/lib/mcp-server.mjs`

```javascript
memory_batch_store: ['POST', '/api/v1/memory/batch_store',
  'Batch store multiple memories at once. Use for document scan import.', {
    items:       { type: 'array',  description: 'Array of memory items [{abstract, content, category, context_type, uri, parent_uri, meta}]', required: true },
    source_path: { type: 'string', description: 'Source directory path', default: '' },
    scan_meta:   { type: 'object', description: 'Scan metadata', default: {} },
  }],
```

## 6. Skill 设计

### 6.1 设计原则

- **零 Python 依赖**：扫描脚本用纯 Node.js 实现（`oc-scan.mjs`），复用插件已有的 Node.js 运行时
- **跨平台**：文件发现和路径处理全部用 Node.js `fs`/`path`/`child_process` API，不依赖 `find`、`awk` 等 Unix 工具
- Node.js >= 18 已被 MCP server 要求，不引入新依赖

### 6.2 `plugins/opencortex-memory/skills/memory-scan/SKILL.md`

Skill 指导 Claude 完成以下步骤：

1. **扫描 + 分块**：`node plugins/opencortex-memory/bin/oc-scan.mjs <path>` → 输出 JSON 到 stdout
2. **批量上传**：将 JSON 通过 MCP tool `memory_batch_store` 或 `curl POST /api/v1/memory/batch_store` 上传
3. **报告结果**：告知用户导入了多少文件/chunk

### 6.3 Node.js 扫描脚本 `plugins/opencortex-memory/bin/oc-scan.mjs`

纯 Node.js，零外部依赖，跨平台：

```javascript
#!/usr/bin/env node
/**
 * oc-scan.mjs — Scan project files, chunk, and output JSON for batch import.
 * Pure Node.js, zero external dependencies, cross-platform.
 *
 * Usage: node oc-scan.mjs [path]
 * Env:   OC_TENANT_ID, OC_USER_ID (default: "default")
 * Output: JSON to stdout { items, source_path, scan_meta }
 */
import { createHash } from 'node:crypto';
import { readFileSync, statSync, readdirSync } from 'node:fs';
import { resolve, relative, extname, basename, join, sep } from 'node:path';
import { execSync } from 'node:child_process';

const MARKDOWN_EXTS = new Set(['.md', '.mdx']);
const CODE_EXTS = new Set([
  '.py', '.js', '.ts', '.tsx', '.jsx', '.go', '.rs', '.java',
  '.c', '.cpp', '.h', '.hpp', '.rb', '.sh', '.yaml', '.yml',
  '.toml', '.json', '.css', '.html',
]);
const TEXT_EXTS = new Set(['.txt', '.rst', '.text', '.adoc']);
const ALL_EXTS = new Set([...MARKDOWN_EXTS, ...CODE_EXTS, ...TEXT_EXTS]);

const SKIP_DIRS = new Set([
  '.git', 'node_modules', '__pycache__', '.venv', 'venv',
  'dist', 'build', '.tox', '.mypy_cache', '.next', '.nuxt',
  'coverage', '.cache', '.turbo',
]);

const MAX_FILE_SIZE = 1_048_576; // 1 MB

const LANG_MAP = {
  '.py': 'python', '.js': 'javascript', '.ts': 'typescript',
  '.tsx': 'tsx', '.jsx': 'jsx', '.go': 'go', '.rs': 'rust',
  '.java': 'java', '.c': 'c', '.cpp': 'cpp', '.h': 'c-header',
  '.hpp': 'cpp-header', '.rb': 'ruby', '.sh': 'shell',
  '.yaml': 'yaml', '.yml': 'yaml', '.toml': 'toml',
  '.json': 'json', '.css': 'css', '.html': 'html',
};

const HEADING_RE = /^(#{1,6})\s+(.+)$/gm;
const FRONTMATTER_RE = /^---\s*\n[\s\S]*?\n---\s*\n/;

// ── Helpers ──────────────────────────────────────────────────────────────

function makeNodeId(filePath, heading = '') {
  return createHash('md5').update(`${filePath}::${heading}`).digest('hex').slice(0, 12);
}

function safePath(relPath) {
  // Normalize to forward slash then replace with __
  return relPath.replace(/[\\/]/g, '__');
}

function safeHeading(text) {
  return text.replace(/[^a-zA-Z0-9_-]/g, '_').slice(0, 50);
}

// ── File Discovery (cross-platform) ─────────────────────────────────────

function discoverViaGit(repoPath) {
  try {
    const out = execSync('git ls-files --cached --others --exclude-standard', {
      cwd: repoPath, encoding: 'utf-8', timeout: 10_000,
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    return out.trim().split('\n').filter(Boolean).map(f => resolve(repoPath, f));
  } catch {
    return null;
  }
}

function discoverViaWalk(dirPath) {
  const files = [];
  const walk = (dir) => {
    let entries;
    try { entries = readdirSync(dir, { withFileTypes: true }); } catch { return; }
    for (const e of entries) {
      if (e.name.startsWith('.') && SKIP_DIRS.has(e.name)) continue;
      if (SKIP_DIRS.has(e.name)) continue;
      const full = join(dir, e.name);
      if (e.isDirectory()) walk(full);
      else if (e.isFile()) files.push(full);
    }
  };
  walk(dirPath);
  return files;
}

function discoverFiles(repoPath) {
  const abs = resolve(repoPath);
  let raw = discoverViaGit(abs);
  if (!raw) raw = discoverViaWalk(abs);
  return raw.filter(f => {
    const ext = extname(f);
    if (!ALL_EXTS.has(ext)) return false;
    try { return statSync(f).size <= MAX_FILE_SIZE; } catch { return false; }
  });
}

// ── Chunkers ─────────────────────────────────────────────────────────────

function chunkMarkdown(content, relPath, tid, uid) {
  // Strip frontmatter
  const fm = FRONTMATTER_RE.exec(content);
  if (fm) content = content.slice(fm[0].length);

  // Collect headings
  const headings = [];
  let m;
  const re = new RegExp(HEADING_RE.source, 'gm');
  while ((m = re.exec(content)) !== null) {
    headings.push({ pos: m.index, level: m[1].length, full: m[0], text: m[2].trim() });
  }

  const sp = safePath(relPath);
  if (headings.length === 0) {
    // Whole file as one chunk
    const stem = basename(relPath, extname(relPath));
    return [{
      abstract: stem,
      content,
      category: 'documents',
      context_type: 'resource',
      uri: `opencortex://${tid}/user/${uid}/resources/documents/${sp}/${makeNodeId(relPath)}`,
      meta: { source: 'scan', file_path: relPath, file_type: 'markdown' },
    }];
  }

  const chunks = [];
  for (let i = 0; i < headings.length; i++) {
    const { pos, level, full, text } = headings[i];
    if (level > 2) continue;
    let end = content.length;
    for (let j = i + 1; j < headings.length; j++) {
      if (headings[j].level <= level) { end = headings[j].pos; break; }
    }
    const section = content.slice(pos, end).trim();
    if (!section) continue;
    chunks.push({
      abstract: text,
      content: section,
      category: 'documents',
      context_type: 'resource',
      uri: `opencortex://${tid}/user/${uid}/resources/documents/${sp}/${safeHeading(text)}/${makeNodeId(relPath, full)}`,
      parent_uri: `opencortex://${tid}/user/${uid}/resources/documents/${sp}`,
      meta: { source: 'scan', file_path: relPath, file_type: 'markdown' },
    });
  }
  return chunks;
}

function chunkCode(content, relPath, tid, uid) {
  const ext = extname(relPath);
  const lang = LANG_MAP[ext] || ext.slice(1);
  const sp = safePath(relPath);
  return [{
    abstract: `${basename(relPath)} (${lang})`,
    content,
    category: 'documents',
    context_type: 'resource',
    uri: `opencortex://${tid}/user/${uid}/resources/documents/${sp}/${makeNodeId(relPath)}`,
    meta: { source: 'scan', file_path: relPath, file_type: 'code', language: lang },
  }];
}

function chunkText(content, relPath, tid, uid) {
  const sp = safePath(relPath);
  return [{
    abstract: basename(relPath),
    content,
    category: 'documents',
    context_type: 'resource',
    uri: `opencortex://${tid}/user/${uid}/resources/documents/${sp}/${makeNodeId(relPath)}`,
    meta: { source: 'scan', file_path: relPath, file_type: 'text' },
  }];
}

// ── Main ─────────────────────────────────────────────────────────────────

const repoPath = process.argv[2] || '.';
const tid = process.env.OC_TENANT_ID || 'default';
const uid = process.env.OC_USER_ID || 'default';

const files = discoverFiles(repoPath);
const abs = resolve(repoPath);
const allChunks = [];

for (const f of files) {
  let content;
  try { content = readFileSync(f, 'utf-8'); } catch { continue; }
  const rel = relative(abs, f).split(sep).join('/'); // normalize to forward slash
  const ext = extname(f);
  if (MARKDOWN_EXTS.has(ext))  allChunks.push(...chunkMarkdown(content, rel, tid, uid));
  else if (CODE_EXTS.has(ext)) allChunks.push(...chunkCode(content, rel, tid, uid));
  else if (TEXT_EXTS.has(ext)) allChunks.push(...chunkText(content, rel, tid, uid));
}

const output = {
  items: allChunks,
  source_path: abs,
  scan_meta: { total_files: files.length, total_chunks: allChunks.length },
};
process.stdout.write(JSON.stringify(output));
```

### 6.4 URI 方案

参考 `CortexURI.build_private()`（`src/opencortex/utils/uri.py:238`）：

```
文件级:  opencortex://{tid}/user/{uid}/resources/documents/{safe_path}/{node_id}
段落级:  opencortex://{tid}/user/{uid}/resources/documents/{safe_path}/{safe_heading}/{node_id}
```

- `safe_path` = 相对路径中 `/` 和 `\` 替换为 `__`
- `node_id` = `md5(relative_path + "::" + heading)[:12]`
- 确定性 URI → Qdrant upsert 自动去重

### 6.5 跨平台说明

| 关注点 | 方案 |
|--------|------|
| 文件发现 | Node.js `fs.readdirSync` 递归遍历，不依赖 `find` |
| Git 加速 | `child_process.execSync('git ls-files ...')` 跨平台可用，失败时 fallback 到 fs walk |
| 路径分隔符 | `path.sep` + `relative().split(sep).join('/')` 统一为正斜杠 |
| 文件读取 | `readFileSync(f, 'utf-8')` 跨平台一致 |
| 运行时依赖 | 仅 Node.js >= 18（MCP server 已要求） |

## 7. 改造文件清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `src/opencortex/http/models.py` | **修改** | 新增 `MemoryBatchItem` + `MemoryBatchStoreRequest` |
| `src/opencortex/http/server.py` | **修改** | 新增 `POST /api/v1/memory/batch_store` 路由 |
| `src/opencortex/orchestrator.py` | **修改** | 新增 `batch_add()` 方法 |
| `plugins/opencortex-memory/lib/mcp-server.mjs` | **修改** | 新增 `memory_batch_store` tool 定义 |
| `plugins/opencortex-memory/bin/oc-scan.mjs` | **新增** | Node.js 扫描 + 分块脚本（跨平台，零外部依赖） |
| `plugins/opencortex-memory/skills/memory-scan/SKILL.md` | **新增** | Skill 定义 |
| `tests/test_batch_store.py` | **新增** | batch_store 端点测试 |

## 8. 测试计划

| # | 测试 | 验证 |
|---|------|------|
| 1 | `test_batch_store_basic` | batch_store 正常存储多条记忆 |
| 2 | `test_batch_store_with_uri` | 传入确定性 URI，upsert 去重 |
| 3 | `test_batch_store_partial_error` | 部分失败返回 partial + errors |
| 4 | `test_batch_store_empty` | 空 items 返回 ok, total=0 |
| 5 | `test_batch_store_searchable` | 导入后可通过 memory_search 检索 |

## 9. 实施顺序

1. **Phase A**：`models.py` + `server.py` + `orchestrator.py` — 服务端 batch_store
2. **Phase B**：`mcp-server.mjs` — MCP tool
3. **Phase C**：`bin/oc-scan.mjs` + `skills/memory-scan/SKILL.md` — Node.js 扫描脚本 + Skill 定义
4. **Phase D**：`tests/test_batch_store.py` — 测试

## 10. 验收标准

1. `POST /api/v1/memory/batch_store` 接受 items 数组，批量存储。
2. `/memory-scan` skill 可扫描项目目录，分块上传到服务端。
3. Markdown 文件按 ## heading 分块，每个 section 独立可搜索。
4. 重复扫描不产生重复记忆（URI + upsert 去重）。
5. 测试全部通过。
