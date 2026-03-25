# Volcengine 清除 + 混合检索抽象统一 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 清除全部 Volcengine 代码依赖，统一混合检索包装为 `_wrap_with_hybrid()` 方法，修复 CachedEmbedder 属性代理。

**Architecture:** 将 orchestrator 中三处重复的 `CompositeHybridEmbedder(embedder, BM25SparseEmbedder())` 抽取为 `_wrap_with_hybrid()` 方法。删除 Volcengine embedder、LLM factory 的 Ark SDK 后端、及所有测试/配置中的 Volcengine 引用。

**Tech Stack:** Python 3.10+, Qdrant, FastEmbed, BM25SparseEmbedder

---

### Task 1: 统一 _wrap_with_hybrid 抽象

**Files:**
- Modify: `src/opencortex/orchestrator.py:325-490`

- [ ] **Step 1: 添加 _wrap_with_hybrid 方法**

在 `_wrap_with_cache` 方法（line 506）之前添加：

```python
def _wrap_with_hybrid(self, embedder: EmbedderBase) -> EmbedderBase:
    """Wrap dense embedder with BM25 sparse for hybrid search.

    No-op if embedder is already hybrid.
    """
    from opencortex.models.embedder.base import HybridEmbedderBase
    if isinstance(embedder, HybridEmbedderBase):
        return embedder
    from opencortex.models.embedder.sparse import BM25SparseEmbedder
    from opencortex.models.embedder.base import CompositeHybridEmbedder
    return CompositeHybridEmbedder(embedder, BM25SparseEmbedder())
```

- [ ] **Step 2: 重构 OpenAI 分支使用 _wrap_with_hybrid**

将 `orchestrator.py:418-421` 从：
```python
from opencortex.models.embedder.sparse import BM25SparseEmbedder
from opencortex.models.embedder.base import CompositeHybridEmbedder
composite = CompositeHybridEmbedder(embedder, BM25SparseEmbedder())
return self._wrap_with_cache(composite)
```
改为：
```python
return self._wrap_with_cache(self._wrap_with_hybrid(embedder))
```

- [ ] **Step 3: 重构 local 分支使用 _wrap_with_hybrid**

将 `orchestrator.py:484-490` 从：
```python
from opencortex.models.embedder.sparse import BM25SparseEmbedder
from opencortex.models.embedder.base import CompositeHybridEmbedder
hybrid = CompositeHybridEmbedder(embedder, BM25SparseEmbedder())
return self._wrap_with_cache(hybrid)
```
改为：
```python
return self._wrap_with_cache(self._wrap_with_hybrid(embedder))
```

- [ ] **Step 4: 更新 _create_default_embedder docstring**

删除 line 298-299 关于 volcengine 的描述，更新为：
```
Resolution order:
1. If ``embedding_provider == "local"``, create a LocalEmbedder (FastEmbed ONNX).
2. If ``embedding_provider == "openai"``, create an OpenAIDenseEmbedder.
3. If nothing works, log a warning and return ``None``.

All embedders are wrapped with BM25 sparse (hybrid search) then LRU cache.
```

- [ ] **Step 5: 运行测试验证**

Run: `uv run python3 -m unittest tests.test_e2e_phase1 -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/opencortex/orchestrator.py
git commit -m "refactor: extract _wrap_with_hybrid() for unified hybrid search wrapping"
```

---

### Task 2: 修复 CachedEmbedder 属性代理

**Files:**
- Modify: `src/opencortex/models/embedder/cache.py:82-92`

- [ ] **Step 1: 添加 is_sparse 和 is_hybrid 属性代理**

在 `cache.py` 的 `close` 方法（line 82）之后，`stats` 属性（line 84）之前添加：

```python
@property
def is_sparse(self) -> bool:
    return self._inner.is_sparse

@property
def is_hybrid(self) -> bool:
    return self._inner.is_hybrid
```

- [ ] **Step 2: Commit**

```bash
git add src/opencortex/models/embedder/cache.py
git commit -m "fix: CachedEmbedder delegates is_sparse/is_hybrid to inner embedder"
```

---

### Task 3: 删除 Volcengine embedder

**Files:**
- Delete: `src/opencortex/models/embedder/volcengine_embedders.py`
- Modify: `src/opencortex/orchestrator.py:325-379`

- [ ] **Step 1: 删除 volcengine_embedders.py**

```bash
rm src/opencortex/models/embedder/volcengine_embedders.py
```

- [ ] **Step 2: 删除 orchestrator volcengine 分支**

删除 `orchestrator.py:325-379`（整个 `if provider == "volcengine":` 块），替换为降级警告：

```python
if provider == "volcengine":
    logger.warning(
        "[MemoryOrchestrator] embedding_provider='volcengine' is deprecated. "
        "Use 'openai' with the same API key/base URL."
    )
    return None
```

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "remove: Volcengine embedder provider and SDK dependency"
```

---

### Task 4: 删除 Volcengine LLM factory 后端

**Files:**
- Modify: `src/opencortex/models/llm_factory.py`

- [ ] **Step 1: 清理 llm_factory.py**

删除：
- Line 1: Volcengine copyright header → 改为 `# SPDX-License-Identifier: Apache-2.0`
- Lines 8-10: Volcengine Ark SDK 描述
- Lines 25-28: `_DEFAULT_ARK_MODEL` 和 `_DEFAULT_ARK_BASE_URL` 常量
- Lines 37-38: docstring 中的 Volcengine 优先级描述
- Lines 61-85: 整个 Backend 1 Volcengine Ark SDK 块
- Lines 123-145: `_make_ark_callable` 函数

更新 docstring 为只描述 OpenAI-compatible 后端。

- [ ] **Step 2: Commit**

```bash
git add src/opencortex/models/llm_factory.py
git commit -m "remove: Volcengine Ark SDK LLM backend from llm_factory"
```

---

### Task 5: 清理配置、注释和文档

**Files:**
- Modify: `src/opencortex/config.py:97`
- Modify: `src/opencortex/retrieve/rerank_client.py:7,193`
- Modify: `src/opencortex/retrieve/rerank_config.py:14,23`
- Modify: `src/opencortex/models/embedder/base.py:231`
- Modify: `pyproject.toml:18`
- Modify: `docker-compose.yml:19`
- Modify: `CLAUDE.md`

- [ ] **Step 1: config.py** — line 97 注释从 `"volcengine" | "jina" | ...` 改为 `"jina" | "cohere" | "local" | "llm"`

- [ ] **Step 2: rerank_client.py** — lines 7, 193 注释中移除 "Volcengine/"

- [ ] **Step 3: rerank_config.py** — lines 14, 23 注释中移除 "Volcengine"

- [ ] **Step 4: base.py** — line 231 示例中 `VolcengineSparseEmbedder` → `BM25SparseEmbedder`

- [ ] **Step 5: pyproject.toml** — 删除 line 18 `"volcengine-python-sdk>=5.0.12"`

- [ ] **Step 6: docker-compose.yml** — 删除 line 19 volcengine env var 注释

- [ ] **Step 7: CLAUDE.md** — 搜索 "Volcengine" 引用并移除/替换

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "chore: remove Volcengine references from config, docs, and dependencies"
```

---

### Task 6: 清理测试文件

**Files:**
- Modify: `tests/test_perf_fixes.py:36-59`
- Modify: `tests/test_real_integration.py` (entire file may be deleted)
- Modify: `tests/test_rl_integration.py:7-8,27-32,52`
- Modify: `tests/test_mcp_qdrant.py:5,31-32,37,48`
- Modify: `tests/test_live_servers.py:289`

- [ ] **Step 1: test_perf_fixes.py** — 删除 `test_volcengine_embedder_wrapped_with_cache` 测试方法

- [ ] **Step 2: test_real_integration.py** — 删除整个文件（纯 Volcengine/OpenViking 集成测试）

- [ ] **Step 3: test_rl_integration.py** — 将 Volcengine embedder 替换为 OpenAI embedder 或本地 mock，更新 docstring

- [ ] **Step 4: test_mcp_qdrant.py** — 将 Volcengine embedder 引用替换为 OpenAI embedder 或本地 mock，更新 docstring

- [ ] **Step 5: test_live_servers.py** — line 289 注释中移除 Volcengine 引用

- [ ] **Step 6: 运行测试**

Run: `uv run python3 -m unittest tests.test_e2e_phase1 tests.test_perf_fixes -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "test: remove Volcengine test fixtures, update integration tests"
```

---

### Task 7: 最终验证

- [ ] **Step 1: 检查残留引用**

Run: `grep -ri volcengine src/ tests/ pyproject.toml docker-compose.yml CLAUDE.md`
Expected: 零结果（除了 orchestrator 中的降级警告）

- [ ] **Step 2: 依赖锁定**

Run: `uv lock && uv sync`
Expected: 成功，volcengine-python-sdk 不再出现在锁文件中

- [ ] **Step 3: 重启服务器**

Run: `kill $(lsof -ti:8921) 2>/dev/null; sleep 2; uv run opencortex-server --host 127.0.0.1 --port 8921 &`
Expected: 服务器正常启动，日志显示 "Auto-created OpenAIDenseEmbedder"

- [ ] **Step 4: 跑 memory quick test**

Run: `uv run python benchmarks/unified_eval.py --mode memory --max-qa 10 --llm-base https://yunwu.ai/v1 --llm-key <key> --llm-model gpt-5.4-nano --output docs/benchmark`
Expected: 正常完成，功能不受影响
