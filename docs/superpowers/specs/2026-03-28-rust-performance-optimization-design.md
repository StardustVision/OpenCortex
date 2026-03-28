# OpenCortex 性能优化设计：Python 异步修复 + Rust 核心库

## 概述

基于全链路性能审计（MCP stdio → HTTP → FastAPI → Embedder → Qdrant → CortexFS），本文档定义两阶段优化方案：

1. **第一阶段**：Python 层异步架构修复——消除阻塞、串行、连接浪费等架构级问题
2. **第二阶段**：引入 Rust 核心库（`opencortex-core`）——用 PyO3 重写 CPU 密集热路径

两阶段独立可交付，第一阶段是第二阶段的前置条件。

## 审计发现摘要

### 端到端延迟现状

| 路径 | 最佳 | 典型 | 最差 |
|------|------|------|------|
| Recall（查询） | 200ms | 1.5-2.5s | 25s |
| Commit（写入） | 90ms | 180ms | 500ms+ |

### 瓶颈分布

```
Recall 典型 2s 延迟分解：
  ├─ MCP→HTTP 传输          15ms  ██
  ├─ JWT + 中间件            2ms  ▏
  ├─ Intent 分析 (LLM)     500ms  ████████████████████
  ├─ Query embedding         25ms  ███
  ├─ Vector search (Qdrant)  50ms  ██████
  ├─ Reranking             100ms  ████████
  ├─ Score fusion            <1ms  ▏
  └─ 结果组装 (CortexFS)    20ms  ██

Commit 典型 180ms 延迟分解：
  ├─ Observer record          <1ms  ▏
  ├─ Embed ×2（串行）         50ms  ██████████████
  ├─ Qdrant upsert ×2（串行） 20ms  █████
  └─ CortexFS 三层写入        85ms  ████████████████████████
```

---

## 第一阶段：Python 异步架构修复

### 1.1 CortexFS 异步包装

**问题**：`LocalAGFS` 所有方法（mkdir/write/read/rm/ls/stat）都是同步调用，直接阻塞 asyncio 事件循环。每次 CortexFS 写入约 85ms，期间整个服务无法处理其他请求。

**文件**：`src/opencortex/storage/cortex_fs.py`

**方案**：在 CortexFS 的每个异步方法中，将 LocalAGFS 的同步调用包装进 `run_in_executor`：

```python
import concurrent.futures

# 模块级 bounded executor，限制并发防止内存泄漏
_fs_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="cortexfs",
)

# 修改前
async def write_context(self, uri, abstract, overview, content):
    self._local_fs.write(abstract_path, abstract)    # 阻塞事件循环
    self._local_fs.write(overview_path, overview)     # 阻塞事件循环
    self._local_fs.write(content_path, content)       # 阻塞事件循环

# 修改后：使用 bounded executor，不传 None
async def write_context(self, uri, abstract, overview, content):
    loop = asyncio.get_running_loop()
    await asyncio.gather(
        loop.run_in_executor(_fs_executor, self._local_fs.write, abstract_path, abstract),
        loop.run_in_executor(_fs_executor, self._local_fs.write, overview_path, overview),
        loop.run_in_executor(_fs_executor, self._local_fs.write, content_path, content),
    )
```

**影响范围**：`write_context`、`read_context`、`delete_context`、`list_contexts`、`stat_context`

**预期收益**：消除事件循环阻塞，三层文件写入从串行 85ms 降至并行 ~30ms

### 1.2 双写并行化（Qdrant 优先策略）

**问题**：`orchestrator.add()` 中 Qdrant upsert (~10ms) 和 CortexFS 三层写入 (~85ms) 串行执行，总耗时 ~95ms。

**文件**：`src/opencortex/orchestrator.py`（`add()` 方法的 upsert + CortexFS 写入部分）

**一致性风险**：直接 `asyncio.gather` 会导致 CortexFS 成功但 Qdrant 失败时产生 orphaned 文件（向量不存在但文件已落盘）。当前串行顺序（先 Qdrant 后 CortexFS）保证了 Qdrant 失败时不会留下 CortexFS 残留。

**方案**：保留 Qdrant 优先写入语义，CortexFS 写入异步化但后发：

```python
# 修改后：Qdrant 先行，CortexFS 异步跟随（不阻塞返回）
await self._storage.upsert(collection, record)  # 先写 Qdrant，失败则整体失败

# CortexFS 写入 fire-and-forget（后台补写）
# 已经通过 1.1 做了 run_in_executor 包装，不再阻塞事件循环
cortex_task = asyncio.create_task(
    self._cortex_fs.write(uri, abstract, overview, content)
)
cortex_task.add_done_callback(
    lambda t: t.exception() and logger.warning(
        "[Orchestrator] CortexFS write failed for %s: %s", uri, t.exception()
    )
)
```

**权衡**：CortexFS 写入不再阻塞 `add()` 返回，延迟从 95ms 降至 ~10ms。CortexFS 是 L2 内容的冗余存储（L0/L1 已在 Qdrant payload 中），短暂的写入延迟不影响搜索功能。CortexFS 写入失败仅影响 L2 content 的读取，不影响核心搜索链路。

**预期收益**：write 路径延迟从 95ms 降至 ~10ms（仅 Qdrant upsert），CortexFS 异步完成

### 1.3 commit() 消息并行写入

**问题**：`_commit()` 对每条消息串行执行 `_write_immediate()`（embed + upsert），2 条消息耗时 ~100ms。

**文件**：`src/opencortex/context/manager.py`（`_commit()` 方法）

**方案**：已在 [Event 噪声消除设计](2026-03-28-event-noise-reduction-design.md) 中详细定义，使用 `asyncio.gather()` 并行写入。

**预期收益**：commit 路径延迟从 ~100ms 降至 ~50ms

### 1.4 MCP HTTP 连接复用

**问题**：`http-client.mjs` 使用原生 `fetch()` 每次新建 TCP 连接，每次 5-50ms 握手开销。

**文件**：`plugins/opencortex-memory/lib/http-client.mjs`

**方案**：Node.js >= 18 的原生 `fetch()` 底层基于 `undici`，通过 `setGlobalDispatcher` 全局配置连接池即可，无需修改每个 fetch 调用：

```javascript
// http-client.mjs 顶部添加
import { Agent, setGlobalDispatcher } from 'undici';
setGlobalDispatcher(new Agent({
  keepAliveTimeout: 30_000,  // 30s keep-alive
  connections: 10,           // 每个 origin 最多 10 个连接
}));

// 现有 httpPost / httpGet 的 fetch() 调用无需修改
// undici Agent 自动对所有 fetch() 生效
```

**注意**：`undici` 是 Node.js 内置依赖（Node >= 18），无需额外安装。`node:http.Agent` 不适用于原生 `fetch()`——原生 fetch 的底层是 undici 而非 node:http。

**预期收益**：每次工具调用节省 5-50ms 连接建立开销

### 1.5 CachedEmbedder batch 修复

**问题**：`CachedEmbedder.embed_batch()` 退化为逐条 `embed()` 循环，浪费了底层 ONNX 和 OpenAI 的批量推理能力。

**文件**：`src/opencortex/models/embedder/cached.py`（推测路径）

**方案**：

```python
def embed_batch(self, texts: List[str]) -> List[EmbedResult]:
    results = [None] * len(texts)
    uncached_indices = []
    uncached_texts = []

    for i, text in enumerate(texts):
        cached = self._cache.get(text)
        if cached is not None:
            results[i] = cached
        else:
            uncached_indices.append(i)
            uncached_texts.append(text)

    if uncached_texts:
        batch_results = self._embedder.embed_batch(uncached_texts)
        for idx, result in zip(uncached_indices, batch_results):
            self._cache[texts[idx]] = result
            results[idx] = result

    return results
```

**预期收益**：批量导入（batch_store）场景从 N×25ms 降至 ~30ms 单次批量推理

### 1.6 Dense + Sparse 并行

**问题**：`CompositeHybridEmbedder.embed()` 串行执行 dense (~20ms) 和 sparse (~5ms)。

**文件**：`src/opencortex/models/embedder/` 中的 hybrid embedder

**方案**：

```python
async def embed_async(self, text: str) -> HybridEmbedResult:
    loop = asyncio.get_running_loop()
    dense_result, sparse_result = await asyncio.gather(
        loop.run_in_executor(None, self._dense.embed, text),
        loop.run_in_executor(None, self._sparse.embed, text),
    )
    return HybridEmbedResult(
        dense_vector=dense_result.dense_vector,
        sparse_vector=sparse_result.sparse_vector,
    )
```

**注意**：现有 `embed()` 是同步方法，新增 `embed_async()` 异步方法，调用方按能力选择。

**预期收益**：单次 embed 延迟从 25ms 降至 ~20ms（受 dense 推理时间主导）

### 1.7 召回链路缓存

**问题**：Intent 分析和 Rerank 无结果缓存，相同查询重复触发 LLM 调用。

**文件**：
- `src/opencortex/retrieve/intent_analyzer.py`
- `src/opencortex/retrieve/rerank_client.py`

**注意**：`IntentAnalyzer.analyze()` 和 `RerankClient.rerank()` 都是 `async def`。不能使用 `functools.lru_cache`——它会缓存 coroutine 对象而非结果，复用时触发"已 await 对象不可复用"错误。

**方案**：手写 async-aware TTL cache（dict + timestamp），无需外部依赖：

```python
# 通用 async TTL cache 实现
class AsyncTTLCache:
    def __init__(self, ttl_seconds: float = 60.0, max_size: int = 128):
        self._cache: Dict[str, Tuple[Any, float]] = {}
        self._ttl = ttl_seconds
        self._max_size = max_size

    def get(self, key: str) -> Optional[Any]:
        entry = self._cache.get(key)
        if entry is None:
            return None
        value, ts = entry
        if time.monotonic() - ts > self._ttl:
            del self._cache[key]
            return None
        return value

    def put(self, key: str, value: Any) -> None:
        if len(self._cache) >= self._max_size:
            # 淘汰最旧条目
            oldest = min(self._cache, key=lambda k: self._cache[k][1])
            del self._cache[oldest]
        self._cache[key] = (value, time.monotonic())

# IntentAnalyzer 使用示例
class IntentAnalyzer:
    def __init__(self, ...):
        self._cache = AsyncTTLCache(ttl_seconds=60.0, max_size=128)

    async def analyze(self, query, session_context=None):
        cache_key = hashlib.md5(f"{query}:{session_context}".encode()).hexdigest()
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        result = await self._llm_analyze(query, session_context)
        self._cache.put(cache_key, result)
        return result

# RerankClient 同理：key = hash(query + sorted(doc_abstracts))
```

**预期收益**：重复/相似查询免 LLM 调用，节省 1-5s/次

---

## 第二阶段：Rust 核心库（opencortex-core）

### 设计原则

- 一个 PyO3 crate，编译为 Python 扩展模块（`.so` / `.pyd`）
- `maturin` 构建，与现有 `uv` 工作流集成
- 所有函数使用 `py.allow_threads` 释放 GIL——这只是不阻塞**其他 Python 线程**，不代表不阻塞 asyncio 事件循环
- **关键约束**：所有 Rust 函数都是同步的。Python 异步代码中**必须通过 `loop.run_in_executor(None, rust_fn, args)` 调用**，否则会阻塞事件循环。这条规则必须在每个集成点落实
- 纯 Python fallback——Rust 模块加载失败时自动退化为现有 Python 实现

### 项目结构

```
opencortex-core/
├── Cargo.toml
├── pyproject.toml              # maturin 构建配置
├── src/
│   ├── lib.rs                  # PyO3 模块入口
│   ├── bm25/
│   │   ├── mod.rs
│   │   ├── tokenizer.rs        # 多语言分词（CJK unigram + EN regex）
│   │   ├── idf.rs              # Heuristic IDF 估算
│   │   ├── boost.rs            # CamelCase / ALL_CAPS / path boost
│   │   └── scorer.rs           # 完整 BM25 scorer（复刻 Python 全部权重语义）
│   └── cortexfs/
│       ├── mod.rs
│       ├── writer.rs           # 三层写入（单文件原子替换，非事务）
│       └── reader.rs           # 三层批量读取
└── tests/
```

**注：Score Fusion 暂不纳入 Rust 范围（见 2.3 说明）。**

### 2.1 BM25 完整 Scorer

**当前问题**：纯 Python BM25 计算（`sparse.py`）持有 GIL ~5ms，阻塞并发。

**语义复刻要求**：当前 Python 实现不仅是分词 + 词频，而是完整的加权管线：

1. **`_tokenize(text)`** — 中英混合分词：英文 regex `[a-z0-9][a-z0-9_\-\.]*` + CJK 字符级 unigram
2. **`_estimate_idf(token)`** — 基于 token 长度和类型的启发式 IDF（0.5-2.0）
3. **`_boost_special(token, original_text)`** — CamelCase (2.0x)、ALL_CAPS (2.5x)、path-like (1.5x) 加权
4. **BM25 TF 计算** — `tf * (k1+1) / (tf + k1 * (1-b+b*dl/avg_dl))` 标准公式
5. **最终权重** — `idf * bm25_tf * boost`，top-N 截断（默认 128）

Rust 必须**完整复刻**以上全部语义，否则会改变召回分布。不能只做 tokenizer。

**Rust 实现**：

```rust
use pyo3::prelude::*;
use std::collections::HashMap;
use regex::Regex;

#[pyclass]
pub struct BM25Scorer {
    en_token_re: Regex,
    camel_re: Regex,
    caps_re: Regex,
    k1: f32,
    b: f32,
    avg_dl: f32,
    max_tokens: usize,
}

#[pymethods]
impl BM25Scorer {
    #[new]
    #[pyo3(signature = (k1=1.2, b=0.75, avg_dl=50.0, max_tokens=128))]
    fn new(k1: f32, b: f32, avg_dl: f32, max_tokens: usize) -> Self {
        Self {
            en_token_re: Regex::new(r"[a-z0-9][a-z0-9_\-\.]*[a-z0-9]|[a-z0-9]").unwrap(),
            camel_re: Regex::new(r"[a-z]+[A-Z][a-zA-Z]*").unwrap(),
            caps_re: Regex::new(r"\b[A-Z]{2,}\b").unwrap(),
            k1, b, avg_dl, max_tokens,
        }
    }

    /// 完整 BM25 embed：分词 → IDF → TF → boost → top-N
    /// 返回 {token: weight}，与 Python BM25SparseEmbedder.embed() 语义一致
    /// Python 侧必须通过 run_in_executor 调用
    fn embed(&self, py: Python<'_>, text: &str) -> PyResult<HashMap<String, f32>> {
        py.allow_threads(|| Ok(self.embed_inner(text)))
    }

    /// 批量 embed
    fn embed_batch(&self, py: Python<'_>, texts: Vec<&str>) -> PyResult<Vec<HashMap<String, f32>>> {
        py.allow_threads(|| Ok(texts.iter().map(|t| self.embed_inner(t)).collect()))
    }
}

impl BM25Scorer {
    fn embed_inner(&self, text: &str) -> HashMap<String, f32> {
        let lower = text.to_lowercase();
        let mut tokens: Vec<String> = Vec::new();

        // 英文 token
        for m in self.en_token_re.find_iter(&lower) {
            tokens.push(m.as_str().to_string());
        }
        // CJK 字符级 unigram
        for c in text.chars() {
            if is_cjk(c) {
                tokens.push(c.to_string());
            }
        }

        if tokens.is_empty() {
            return HashMap::new();
        }

        // 词频统计
        let mut tf_map: HashMap<String, u32> = HashMap::new();
        for t in &tokens {
            *tf_map.entry(t.clone()).or_default() += 1;
        }

        let dl = tokens.len() as f32;
        let mut weights: HashMap<String, f32> = HashMap::new();

        for (token, tf) in &tf_map {
            let idf = estimate_idf(token);
            let tf_f = *tf as f32;
            let numerator = tf_f * (self.k1 + 1.0);
            let denominator = tf_f + self.k1 * (1.0 - self.b + self.b * dl / self.avg_dl);
            let bm25_tf = if denominator > 0.0 { numerator / denominator } else { 0.0 };
            let boost = boost_special(token, text, &self.camel_re, &self.caps_re);
            let weight = idf * bm25_tf * boost;
            if weight > 0.0 {
                weights.insert(token.clone(), weight);
            }
        }

        // top-N 截断
        if weights.len() > self.max_tokens {
            let mut items: Vec<_> = weights.into_iter().collect();
            items.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap());
            items.truncate(self.max_tokens);
            return items.into_iter().collect();
        }
        weights
    }
}

fn is_cjk(c: char) -> bool {
    matches!(c as u32, 0x4E00..=0x9FFF | 0x3400..=0x4DBF | 0xF900..=0xFAFF)
}

/// 精确复刻 Python _estimate_idf 的分段逻辑
fn estimate_idf(token: &str) -> f32 {
    let len = token.len();
    if len == 1 && token.chars().next().map_or(false, is_cjk) { return 0.8; }
    if len <= 2 { return 0.5; }
    if len <= 4 { return 1.0; }
    if len <= 8 { return 1.5; }
    2.0
}

/// 精确复刻 Python _boost_special 的 CamelCase/ALL_CAPS/path 逻辑
fn boost_special(token: &str, original: &str, camel_re: &Regex, caps_re: &Regex) -> f32 {
    let mut boost = 1.0_f32;
    for m in camel_re.find_iter(original) {
        if m.as_str().to_lowercase() == token { boost = boost.max(2.0); }
    }
    for m in caps_re.find_iter(original) {
        if m.as_str().to_lowercase() == token { boost = boost.max(2.5); }
    }
    if token.contains('.') || token.contains('_') || token.contains('-') || token.contains('/') {
        boost = boost.max(1.5);
    }
    boost
}
```

**Python 侧集成**：

```python
# src/opencortex/models/embedder/sparse.py
try:
    from opencortex_core import BM25Scorer as _RustBM25Scorer
except ImportError:
    _RustBM25Scorer = None

class BM25SparseEmbedder(SparseEmbedderBase):
    def __init__(self, k1=1.2, b=0.75, avg_dl=50.0, max_tokens=128):
        super().__init__(model_name="bm25-sparse")
        self._k1, self._b, self._avg_dl, self._max_tokens = k1, b, avg_dl, max_tokens
        # Rust scorer（参数透传，保证语义一致）
        self._rust = _RustBM25Scorer(k1, b, avg_dl, max_tokens) if _RustBM25Scorer else None

    def embed(self, text: str) -> EmbedResult:
        if self._rust:
            # 注意：调用方必须通过 run_in_executor 包装，否则阻塞事件循环
            return EmbedResult(sparse_vector=self._rust.embed(text))
        # 现有 Python 实现（_tokenize + _estimate_idf + _boost_special + BM25 公式）
        return self._python_embed(text)

    def _python_embed(self, text: str) -> EmbedResult:
        # ... 当前 embed() 方法的完整逻辑，重命名为 _python_embed 作为 fallback
```

**验证**：Rust 版本必须通过对照测试——对同一组输入文本，Rust `embed()` 和 Python `_python_embed()` 的输出 `{token: weight}` 完全一致（浮点精度 1e-6 内）。

**预期收益**：完整 BM25 embed 5ms → <0.2ms，释放 GIL

### 2.2 CortexFS I/O

**当前问题**：三层文件（.abstract.md / .overview.md / content.md）分别写入，同步阻塞。

**原子性说明**：每个文件通过 tmp + rename 实现**单文件原子替换**（POSIX rename 语义）。但三个文件之间**不是事务**——读者可能看到"abstract 已更新、overview/content 还未更新"的中间态。这与当前 Python 实现的一致性语义相同（Python 也是三次独立写入），不引入新的回归。

**Rust 实现**：

```rust
use pyo3::prelude::*;
use std::fs;
use std::path::PathBuf;
use std::io::Write;

#[pyclass]
pub struct CortexFSWriter {
    root: PathBuf,
}

#[pymethods]
impl CortexFSWriter {
    #[new]
    fn new(root: &str) -> Self {
        Self { root: PathBuf::from(root) }
    }

    /// 三层写入：每个文件 tmp + rename（单文件原子替换，三文件间非事务）
    /// Python 侧必须通过 run_in_executor 调用
    fn write_three_layer(
        &self, py: Python<'_>,
        rel_path: &str, abstract_: &str, overview: &str, content: &str,
    ) -> PyResult<()> {
        py.allow_threads(|| {
            let dir = self.root.join(rel_path);
            fs::create_dir_all(&dir)?;

            // tmp 后缀用 PID + thread ID 避免并发冲突
            let tmp_id = format!(".tmp.{}.{:?}",
                std::process::id(),
                std::thread::current().id(),
            );
            let layers = [
                (".abstract.md", abstract_),
                (".overview.md", overview),
                ("content.md", content),
            ];

            for (name, data) in &layers {
                let tmp_path = dir.join(format!("{}{}", name, tmp_id));
                let final_path = dir.join(name);
                let mut f = fs::File::create(&tmp_path)?;
                f.write_all(data.as_bytes())?;
                fs::rename(&tmp_path, &final_path)?;
            }

            Ok(())
        })
    }

    /// 三层读取
    /// Python 侧必须通过 run_in_executor 调用
    fn read_three_layer(
        &self, py: Python<'_>, rel_path: &str,
    ) -> PyResult<(String, String, String)> {
        py.allow_threads(|| {
            let dir = self.root.join(rel_path);
            let abstract_ = fs::read_to_string(dir.join(".abstract.md")).unwrap_or_default();
            let overview = fs::read_to_string(dir.join(".overview.md")).unwrap_or_default();
            let content = fs::read_to_string(dir.join("content.md")).unwrap_or_default();
            Ok((abstract_, overview, content))
        })
    }

    /// 批量读取（用于 read_batch 场景）
    /// Python 侧必须通过 run_in_executor 调用
    fn read_batch(
        &self, py: Python<'_>, paths: Vec<&str>, level: &str,
    ) -> PyResult<Vec<Option<String>>> {
        py.allow_threads(|| {
            Ok(paths.iter().map(|p| {
                let dir = self.root.join(p);
                let filename = match level {
                    "l0" => ".abstract.md",
                    "l1" => ".overview.md",
                    "l2" | _ => "content.md",
                };
                fs::read_to_string(dir.join(filename)).ok()
            }).collect())
        })
    }
}
```

**预期收益**：
- 三层写入从 Python 3 次同步 I/O (~85ms) 降至 Rust 连续写入 (~10-15ms)
- 三层读取从逐文件 Python I/O 降至单次 Rust 批量读取
- `allow_threads` 释放 GIL，**但不等于不阻塞事件循环**——Python 侧必须通过 `run_in_executor` 调用，否则仍会卡住 asyncio 事件循环线程

### 2.3 Score Fusion — 暂缓，先统一 Python 抽象

**当前状况**：分数融合逻辑散布在 `hierarchical_retriever.py` 的至少 5 个位置（L490 flat rerank、L683 starting points、L755 recursive、L931 frontier、L1189 late rerank），每处公式略有差异（是否包含 hotness、是否用 alpha 混合 parent score）。

**问题**：如果直接提供一个 Rust `fuse_scores_batch()`，但 Python 侧 5 处调用点仍然各自内联计算，会面临：
- 只替换部分调用点 → 收益有限
- 替换全部 → 需要统一抽象，而各处参数组合不同
- 长期维护 → Python/Rust 两套公式容易漂移

**决策**：Score Fusion 的 Rust 化推迟到 Python 侧先完成统一抽象（将 5 处内联计算提取为一个 `_fuse_score()` 方法），之后再用 Rust 替换该方法。这是重构先行，Rust 跟进。

**当前不纳入 `opencortex-core` 初版范围。**

### 2.4 PyO3 模块入口

```rust
// src/lib.rs
use pyo3::prelude::*;

mod bm25;
mod cortexfs;

#[pymodule]
fn opencortex_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<bm25::BM25Scorer>()?;
    m.add_class::<cortexfs::CortexFSWriter>()?;
    Ok(())
}
```

### 构建集成

**`pyproject.toml`**：

```toml
[build-system]
requires = ["maturin>=1.0,<2.0"]
build-backend = "maturin"

[project]
name = "opencortex-core"
requires-python = ">=3.10"

[tool.maturin]
features = ["pyo3/extension-module"]
```

**CI/开发构建**：

```bash
# 开发模式（debug 构建 + 自动安装到当前 venv）
cd opencortex-core && maturin develop

# Release 构建
maturin build --release

# 与 uv 集成
uv pip install ./opencortex-core
```

**Python 侧 fallback 模式**：

```python
# src/opencortex/native.py — 统一入口
try:
    import opencortex_core
    HAS_NATIVE = True
except ImportError:
    HAS_NATIVE = False
    opencortex_core = None
```

各调用点通过 `HAS_NATIVE` 判断，Rust 不可用时退化为现有 Python 实现，保证所有环境都能运行。

---

## 改动文件汇总

### 第一阶段（Python）

| 文件 | 改动 |
|------|------|
| `src/opencortex/storage/cortex_fs.py` | 所有同步 I/O 包装 `run_in_executor`，三层写入 `asyncio.gather` 并行 |
| `src/opencortex/orchestrator.py` | `add()` Qdrant 优先写入 + CortexFS fire-and-forget 异步跟随 |
| `src/opencortex/context/manager.py` | `_commit()` 消息并行写入（与噪声消除设计联动） |
| `plugins/opencortex-memory/lib/http-client.mjs` | 引入 `undici` `setGlobalDispatcher` 连接池 |
| `src/opencortex/models/embedder/cached.py` | `embed_batch()` 透传底层批量接口 |
| `src/opencortex/models/embedder/` hybrid | 新增 `embed_async()` 并行 dense + sparse |
| `src/opencortex/retrieve/intent_analyzer.py` | AsyncTTLCache (60s TTL, 128 entries) |
| `src/opencortex/retrieve/rerank_client.py` | AsyncTTLCache (query+docs hash) |

### 第二阶段（Rust）

| 文件 | 改动 |
|------|------|
| `opencortex-core/` （新 crate） | BM25Scorer + CortexFSWriter |
| `src/opencortex/native.py` （新） | Rust 模块统一入口 + fallback |
| `src/opencortex/models/embedder/sparse.py` | 集成 Rust BM25Scorer（完整语义复刻 + 对照测试） |
| `src/opencortex/storage/cortex_fs.py` | 集成 Rust CortexFSWriter（所有调用走 run_in_executor） |

**后续**（需 Python 侧先重构）：
| 文件 | 改动 |
|------|------|
| `src/opencortex/retrieve/hierarchical_retriever.py` | 统一 5 处 score fusion 为 `_fuse_score()` 方法 |
| `opencortex-core/` | 待 Python 抽象稳定后，增加 Rust `fuse_scores_batch` |

---

## 预期收益总览

| 路径 | 当前 | 第一阶段后 | 第二阶段后 |
|------|------|-----------|-----------|
| Commit (典型) | 180ms | ~35ms | ~25ms |
| Recall (典型) | 2s | 1.2s | 1.1s |
| Batch import (100条) | 2500ms | 250ms | 200ms |
| 事件循环阻塞 | 85ms/次 | 0ms | 0ms |
| MCP 工具调用开销 | 15-50ms | 1-5ms | 1-5ms |

### 投入产出比说明

Recall 典型 2s 延迟中，最大头是 Intent LLM (500ms) 和 Reranking (100ms)——都是 I/O bound，Rust 无法加速。第一阶段的 Python 异步修复（CortexFS 解阻塞、双写优化、连接复用、缓存）覆盖了绝大部分可优化延迟。

第二阶段 Rust 的主要收益在：
- **BM25**：释放 GIL，改善并发吞吐（多请求同时 embed 不再互相阻塞），而非单次延迟
- **CortexFS**：连续 I/O 减少系统调用开销，批量读取场景（搜索结果 L2 组装）有实质收益
- **整体**：第二阶段是"锦上添花"，第一阶段是"止血"。必须先完成第一阶段。

## 风险

| 风险 | 缓解 |
|------|------|
| Rust 编译增加开发/CI 复杂度 | maturin 一键构建；Docker 镜像预编译；纯 Python fallback |
| PyO3 版本与 Python 版本耦合 | 锁定 PyO3 >= 0.22，支持 Python 3.10-3.13 |
| Rust 模块 segfault 影响服务稳定性 | 所有 Rust 函数无 unsafe；单元测试 + Python 侧集成测试覆盖 |
| BM25 Rust 版本语义漂移 | 对照测试：同一输入，Python/Rust 输出 `{token: weight}` 在 1e-6 内一致 |
| CortexFS 三文件非事务写入 | 与当前 Python 一致性语义相同，不引入新回归 |
| Rust 函数在 async 代码中未用 run_in_executor | 设计原则强制要求；代码审查检查点；集成测试验证 |
| 第一阶段 run_in_executor 增加线程池压力 | 使用独立 bounded executor（见下方详述） |
| run_in_executor 内存泄漏 | 限制并发 + 背压 + 弱引用（见下方详述） |

### run_in_executor 内存泄漏风险

`run_in_executor` 有三类泄漏路径，必须在实现中逐一防范：

**1. 默认线程池无界队列**

`loop.run_in_executor(None, fn)` 使用默认 `ThreadPoolExecutor`，队列无界。突发大量 CortexFS 写入时，任务堆积在队列中，每个任务持有闭包引用（包含完整 content 字符串），内存持续增长直到任务执行完毕。

缓解：创建独立的 bounded executor，替代默认 `None`：

```python
import concurrent.futures

# 模块级创建，限制最大并发线程数
_fs_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=4,              # I/O bound，4 线程足够
    thread_name_prefix="cortexfs",
)

# 使用时传入指定 executor
await loop.run_in_executor(_fs_executor, self._local_fs.write, path, data)
```

**2. fire-and-forget 任务未追踪**

1.2 双写优化中 CortexFS 写入改为 `asyncio.create_task` fire-and-forget。如果写入速度持续超过 I/O 吞吐，task 对象和闭包引用堆积。

缓解：复用现有 `_pending_tasks` 模式 + 背压限制：

```python
# 追踪 + done callback 清理（已有模式）
task = asyncio.create_task(self._cortex_fs.write(...))
self._pending_tasks.add(task)
task.add_done_callback(self._pending_tasks.discard)

# 背压：pending 超过阈值时降级为同步等待
MAX_PENDING_FS_WRITES = 32
if len(self._pending_tasks) > MAX_PENDING_FS_WRITES:
    # 等待最早的一批完成
    done, _ = await asyncio.wait(
        self._pending_tasks,
        return_when=asyncio.FIRST_COMPLETED,
    )
    for t in done:
        self._pending_tasks.discard(t)
```

**3. 闭包捕获大对象延长生命周期**

传给 `run_in_executor` 的 callable 及其参数在线程池队列中排队时不会被 GC。如果参数包含大字符串（L2 content 可达 50KB），排队 N 个任务会额外占用 N × 50KB。

缓解：
- executor `max_workers=4` 限制队列深度
- 不在 executor 闭包中捕获不必要的上下文（如 orchestrator 实例）——只传必要参数
- 对于批量操作（batch_store），分批提交而非一次性全部 submit

## 验证方式

### 第一阶段

- 运行全量测试套件：`uv run python3 -m unittest discover -s tests -v`
- 对比 commit 延迟：在 `_commit()` 前后加 `time.monotonic()` 日志
- 对比 recall 延迟：`SearchExplainSummary` 已有分段耗时，对比 intent_ms / search_ms / rerank_ms
- 验证 MCP 连接复用：`netstat` 观察连接数不随工具调用增长

### 第二阶段

- Rust 单元测试：`cargo test`
- **BM25 对照测试**：对 100+ 条多语言文本（中英混合、CamelCase、ALL_CAPS、path-like），验证 Rust `embed()` 和 Python `_python_embed()` 输出一致（浮点精度 1e-6）
- **CortexFS 集成测试**：验证所有调用点通过 `run_in_executor` 包装（可通过 monkey-patch 事件循环检测同步调用）
- 基准测试：`pytest-benchmark` 对比 BM25 embed / CortexFS write
- Fallback 验证：卸载 opencortex-core 后服务正常启动运行
