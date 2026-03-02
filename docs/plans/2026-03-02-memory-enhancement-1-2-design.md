# 记忆增强 #1 词法后备检索 + #2 访问驱动遗忘 — 设计文档

> 状态: **Approved**
> 创建: 2026-03-02

## 1. 背景

当前系统两个痛点：

1. **无向量即全失败**：embedding provider 禁用或超时时，`adapter.search()` 退化为纯 scroll（无排序），检索质量骤降。
2. **遗忘机制不区分陈旧度**：`apply_decay()` 仅衰减 `reward_score`，不考虑记忆是否被持续使用。高频使用但低反馈的记忆可能被误伤，长期不用的脏记忆下沉不够快。

## 2. 设计目标

1. 无向量时仍有关键词匹配能力，不返回空/乱序结果。
2. 近期被访问的记忆获得衰减保护，长期未访问的记忆加速下沉。
3. 零外部依赖新增（利用 Qdrant 原生能力 + 标准库）。
4. 分步落地，风险可控。

## 3. 非目标

1. 不在此阶段实现 hybrid 三路融合（Step 2 未来做）。
2. 不引入 jieba 等重型中文分词库。
3. 不重构 HierarchicalRetriever 的 frontier batching 流程。

---

## 4. 增强 #1：词法后备检索

### 4.1 Schema + 存量迁移

在 `QdrantStorageAdapter` 新增 `ensure_text_indexes()` 方法，HTTP server 启动时（`MemoryOrchestrator._init()` 末尾）调用。对 context 和 skillbook 两个集合的 `abstract` + `overview` 字段创建 Qdrant 全文索引：

```python
async def ensure_text_indexes(self):
    """Ensure full-text indexes exist on pre-existing collections.
    Qdrant create_payload_index is idempotent (skips if exists).
    """
    for collection in [_CONTEXT_COLLECTION, _SKILLBOOK_COLLECTION]:
        for field in ["abstract", "overview"]:
            await client.create_payload_index(
                collection_name=collection,
                field_name=field,
                field_schema=models.TextIndexParams(
                    type="text",
                    tokenizer=models.TokenizerType.MULTILINGUAL,
                    min_token_len=2,
                    max_token_len=20,
                ),
            )
```

选 `MULTILINGUAL` tokenizer 以支持中英文混合。新建集合时也在 `init_context_collection()` 中创建。

### 4.2 策略开关 + 分层触发

adapter 新增 `lexical_mode` 参数：

```python
lexical_mode: str = "fallback_only"  # "fallback_only" | "hybrid"
```

**Step 1（本次实现）— fallback_only**：仅在以下条件触发 text search：
- `query_vector` 为 None（embedding provider 禁用）
- 或 embedding 调用超时/异常后降级（见 4.5 服务端超时）

**Step 2（未来）— hybrid**：始终三路（dense + sparse + text）RRF 融合。届时建议用 Qdrant 原生 Sparse Vectors (SPLADE/BM25) 替代 MatchText+scroll，解决截断效应。

`HierarchicalRetriever` 不关心 adapter 内部策略，只需将原始 `text_query` 传下去。

### 4.3 接口签名扩展

`VikingDBInterface.search()` 新增参数：

```python
async def search(
    self, collection, query_vector=None, ...,
    text_query: str = "",  # 新增，默认空 = 不走 text
) -> list:
```

默认值 `""` 保持向后兼容。需同步更新：
- `QdrantStorageAdapter.search()` — 实际实现 text search
- 测试中的 `InMemoryStorage` 等桩 — 接受参数但忽略

### 4.4 Text Search 实现

#### 4.4.1 Filter 构造

用 `should` 实现 abstract OR overview 匹配，`must` 保留原始权限/租户过滤：

```python
text_conditions = [
    models.FieldCondition(key="abstract", match=models.MatchText(text=text_query)),
    models.FieldCondition(key="overview", match=models.MatchText(text=text_query)),
]
text_filter = models.Filter(
    must=[qdrant_filter] if qdrant_filter else [],
    should=text_conditions,
)
```

原始 filter 作为整体嵌套进 must，不拆解子条件，保留原始 should/must_not 语义。

#### 4.4.2 中英文混合打分

scroll 无原生相关性分数，需自定义打分。`.split()` 对中文完全失效，用字符级 + 英文单词的零依赖方案：

```python
import re

def _tokenize_for_scoring(text: str) -> set[str]:
    """Zero-dependency tokenizer for Chinese+English mixed text."""
    text = (text or "").lower()
    words = set(re.findall(r'[a-z0-9_\-\.]+', text))        # 英文单词/路径/错误码
    chinese_chars = set(re.findall(r'[\u4e00-\u9fa5]', text)) # 中文单字
    return words | chinese_chars

def _compute_text_score(query: str, abstract: str, overview: str) -> float:
    """Term-overlap scoring for lexical results."""
    query_terms = _tokenize_for_scoring(query)
    if not query_terms:
        return 0.0
    abstract_terms = _tokenize_for_scoring(abstract)
    overview_terms = _tokenize_for_scoring(overview)
    # abstract 命中权重更高
    abstract_hits = len(query_terms & abstract_terms)
    overview_hits = len(query_terms & overview_terms)
    return min(1.0, (abstract_hits * 2 + overview_hits) / (len(query_terms) * 2))
```

#### 4.4.3 Qdrant MatchText 截断效应

**已知限制**：MatchText 是布尔过滤器，`scroll(limit=N)` 返回最早写入的 N 个匹配记录，不按相关性排序。

**Step 1 缓解**：scroll 取 `limit * 3` 的过采样，然后 `_compute_text_score` 重排后截取 Top-K。

**Step 2 路线**：迁移到 Qdrant 原生 Sparse Vectors，获得真正的 BM25 级别排序。

#### 4.4.4 adapter.search() 集成

在现有 search 方法末尾，当 `lexical_mode == "fallback_only"` 且无向量结果时：

```python
# 在现有 dense/sparse/scroll 三分支之后
if not points and text_query:
    # Fallback: lexical search
    text_results, _ = await client.scroll(
        collection_name=collection,
        scroll_filter=text_filter,
        limit=limit * 3,  # 过采样
        with_payload=True,
        with_vectors=with_vector,
    )
    # 打分 + 排序
    for p in text_results:
        p.payload["_text_score"] = _compute_text_score(
            text_query, p.payload.get("abstract", ""), p.payload.get("overview", "")
        )
    text_results.sort(key=lambda p: p.payload.get("_text_score", 0), reverse=True)
    points = text_results[:limit]
```

### 4.5 服务端分段超时

客户端 Hook 有 3s 硬超时。服务端必须在此之前完成 fallback。

#### 4.5.1 Embedding 超时

`embedder.embed()` 是同步方法，需 `run_in_executor` + `asyncio.wait_for` 双层包装：

```python
import asyncio
from concurrent.futures import ThreadPoolExecutor

_embed_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="embed")

async def _embed_with_timeout(self, text: str, timeout: float = 2.0):
    """Embed with server-side timeout. Returns None on timeout."""
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(_embed_pool, self.embedder.embed, text),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning("Embedding timeout (%.1fs), falling back to lexical", timeout)
        return None
```

#### 4.5.2 HTTP Client 内部超时

embedding 和 rerank 的 HTTP client 层也要配超时，双保险：

```python
# httpx client 配置
httpx.AsyncClient(timeout=httpx.Timeout(connect=2.0, read=3.0, write=2.0, pool=2.0))
```

`wait_for` 超时后 executor 中的线程任务可能仍在跑，HTTP client 层的超时确保底层连接也能及时释放。

### 4.6 调用链

```
orchestrator.search(query)
    → HierarchicalRetriever.retrieve(query, text_query=query)
        → _embed_with_timeout(query)  # 2s 超时
            → 成功: query_vector = [...], text_query 传但 adapter 忽略 (fallback_only)
            → 超时: query_vector = None, adapter 走 text search fallback
        → storage.search(query_vector, text_query=query, ...)
            → adapter 根据 lexical_mode + query_vector 决定路径
```

---

## 5. 增强 #2：访问驱动遗忘

### 5.1 Schema 改动

在 `collection_schemas.py` 的 context collection 新增字段：

```python
{"FieldName": "accessed_at", "FieldType": "date_time"},
```

加入标量索引列表。`active_count` 已存在无需改动。

通过 `ensure_text_indexes()`（或重命名为 `ensure_indexes()`）在启动时对已有集合补索引。

### 5.2 访问回写

**时机**：`orchestrator.search()` 返回 Top-K 结果后，异步批量回写。

```python
# orchestrator.search() 末尾
if final_results:
    asyncio.create_task(self._update_access_stats(
        [r["id"] for r in final_results[:top_k]]
    ))

async def _update_access_stats(self, ids: list[str]):
    """Async batch update access_count + accessed_at for retrieved records."""
    now = datetime.utcnow().isoformat() + "Z"
    for record_id in ids:
        try:
            records = await self._storage.fetch(_CONTEXT_COLLECTION, [record_id])
            if records:
                count = records[0].get("active_count", 0)
                await self._storage.update(
                    _CONTEXT_COLLECTION, record_id,
                    {"active_count": count + 1, "accessed_at": now},
                )
        except Exception:
            pass  # 访问统计失败不影响主流程
```

**关键点**：
- `asyncio.create_task()` 不阻塞搜索返回
- 逐条 fetch+update 初期可接受；后续可用 `set_payload` 批量化

### 5.3 衰减公式改造

修改 `QdrantStorageAdapter.apply_decay()`，在计算每条记录的衰减率时引入访问时间因子：

```python
import math

# 现有逻辑
rate = protected_rate if is_protected else decay_rate  # 0.99 or 0.95

# 新增：访问时间保护
accessed_at = payload.get("accessed_at")
if accessed_at:
    days_since = (now - parse_iso(accessed_at)).days
    # 近期访问过的记忆获得衰减保护
    # 30天内访问: bonus 最高 0.04 → rate 从 0.95 升至 ~0.99
    # 超过30天: bonus 指数衰减趋近 0
    access_bonus = 0.04 * math.exp(-days_since / 30)
    rate = min(1.0, rate + access_bonus)

new_reward = reward * rate
```

**效果表**：

| 上次访问 | access_bonus | effective_rate | 语义 |
|---------|-------------|----------------|------|
| 昨天 | 0.039 | 0.989 | 几乎不衰减 |
| 7 天前 | 0.032 | 0.982 | 轻微衰减 |
| 30 天前 | 0.015 | 0.965 | 接近基础衰减 |
| 90 天前 | 0.002 | 0.952 | 回归正常衰减 |
| 从未访问 | 0 | 0.950 | 基础衰减率 |

### 5.4 Profile 扩展

`rl_types.py` 的 `Profile` 新增 `accessed_at: str = ""` 字段。`get_profile()` 返回时填充，便于调试和观测。

---

## 6. 改动文件清单

### 增强 #1

| 文件 | 操作 | 说明 |
|------|------|------|
| `storage/collection_schemas.py` | 修改 | 新建集合时创建 abstract/overview 全文索引 |
| `storage/vikingdb_interface.py` | 修改 | `search()` 新增 `text_query: str = ""` 参数 |
| `storage/qdrant/adapter.py` | 修改 | `ensure_text_indexes()` + text search fallback + `_compute_text_score()` + `_tokenize_for_scoring()` + `lexical_mode` |
| `retrieve/hierarchical_retriever.py` | 修改 | 传递 `text_query` 到 storage.search()；`_embed_with_timeout()` 服务端超时 |
| `orchestrator.py` | 修改 | `search()` 传递 query 文本到 retriever |
| 测试桩（InMemoryStorage 等） | 修改 | `search()` 接受 `text_query` 参数 |

### 增强 #2

| 文件 | 操作 | 说明 |
|------|------|------|
| `storage/collection_schemas.py` | 修改 | 新增 `accessed_at` 字段 + 索引 |
| `storage/qdrant/adapter.py` | 修改 | `apply_decay()` 引入访问时间因子；`get_profile()` 返回 `accessed_at` |
| `storage/qdrant/rl_types.py` | 修改 | `Profile` 新增 `accessed_at` |
| `orchestrator.py` | 修改 | `search()` 末尾异步调用 `_update_access_stats()` |

---

## 7. 测试计划

### 新增测试

| # | 测试 | 验证 |
|---|------|------|
| 1 | `test_text_search_fallback_no_vector` | 无向量时 text search 返回匹配结果 |
| 2 | `test_text_search_filter_preserves_tenant` | text search 保留原始权限 filter |
| 3 | `test_text_score_chinese` | 中文 query 的文本打分不为 0 |
| 4 | `test_text_score_english_keywords` | 英文关键词（文件名、错误码）正确匹配 |
| 5 | `test_embed_timeout_fallback` | embedding 超时后降级到 text search |
| 6 | `test_ensure_text_indexes_idempotent` | 重复调用 ensure_text_indexes 不报错 |
| 7 | `test_access_stats_updated_on_search` | 搜索后 Top-K 的 access_count 递增 |
| 8 | `test_accessed_at_updated_on_search` | 搜索后 Top-K 的 accessed_at 被设置 |
| 9 | `test_decay_recent_access_slower` | 近期访问的记忆衰减率更低 |
| 10 | `test_decay_old_access_normal` | 长期未访问的记忆衰减率回归基础值 |
| 11 | `test_decay_never_accessed` | 从未访问的记忆使用基础衰减率 |
| 12 | `test_profile_includes_accessed_at` | get_profile 返回 accessed_at |

### 回归

现有 111+ Python 测试全部通过。`search()` 签名变更（新增默认参数）向后兼容，不影响现有调用。

---

## 8. 落地分步

| 步骤 | 范围 | 验收 |
|------|------|------|
| **Step 1a** | 服务端分段超时（`_embed_with_timeout`） | embedding 超时 2s 后返回 None，不阻塞 |
| **Step 1b** | 全文索引 + ensure_indexes + text search fallback + 打分 | 无向量时搜索返回有序匹配结果 |
| **Step 1c** | accessed_at 字段 + 异步访问回写 + decay 公式改造 | 近期访问记忆衰减更慢 |
| **Step 2（未来）** | hybrid 三路融合 + Qdrant Sparse Vectors | 硬关键词 Recall@5 提升，需 A/B 验证 |

---

## 9. 风险与缓解

| 风险 | 概率 | 缓解 |
|------|------|------|
| Qdrant MULTILINGUAL tokenizer 中文效果不佳 | 中 | Step 1 仅做 fallback，Python 层打分兜底；Step 2 换 Sparse Vectors |
| MatchText scroll 截断导致漏召 | 低 | 过采样 limit*3 + 重排；Step 2 用原生 Sparse Vectors 解决 |
| 异步访问回写丢失（进程崩溃） | 低 | 访问统计是辅助信号，丢失不影响核心功能 |
| run_in_executor 线程泄漏 | 低 | ThreadPoolExecutor(max_workers=2) 限制 + HTTP client 内部超时双保险 |
| VikingDBInterface 签名变更影响测试 | 低 | 新参数有默认值，现有调用向后兼容 |
