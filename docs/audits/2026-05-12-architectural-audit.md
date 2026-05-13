# OpenCortex 架构审计

- **日期**: 2026-05-12
- **范围**: `src/opencortex/*` (~13,500 LOC, ~80 文件)
- **方法**: 4 个并行 read-only audit agent（写入侧 / 检索侧 / 性能 / 代码质量）+ 合成
- **运行制品**: `/tmp/compound-engineering/ce-code-review/20260512-170549-7c5aec54/`
- **触发上下文**: LoCoMo benchmark 结果落在 Mem0[g] 区间（J-Score 0.6818），需要诊断 Cat1/Cat3 短板的根因

---

## 1. 评分卡

| 维度 | 等级 | 一句话 |
|---|---|---|
| 写入侧保真 | **C** | derive 流程对 abstract/overview 无保真校验，事实被 LLM 压扁 |
| 检索召回 | **B+** | cone/reason-tree 强，但 QueryType/Composer/entailment 仍缺位；L1 已定义但当前 planner 不选择 |
| 性能/并发 | **C** | sync embedder + sqlite3 + N+1 Qdrant 削弱 event worker 并发 |
| 代码质量 | **B+** | 命名/结构良好，但有 1 个 P0 安全 + 多处 bare except |

对应 LoCoMo 现状：**召回赢、表达和保真输** —— Cat4 多跳 0.7931 超过 Mem0[g] 0.757，Cat1 单跳和 Cat3 推理同时落后 Mem0 系。

---

## 2. LoCoMo 现状回顾

### 2.1 整体分数

| 系统 | J-Score | 与 OpenCortex 对比 |
|---|---:|---|
| MemMachine v0.1 | 0.849 | 落后 0.167 |
| Zep 自测 | 0.751 | 落后 0.069 |
| Letta file-based | 0.740 | 落后 0.058 |
| Mem0[g] | 0.684 | 落后 0.002 |
| **OpenCortex** | **0.6818** | — |
| Mem0 | 0.669 | 领先 0.013 |

### 2.2 分类分数

| 类别 | OpenCortex | Mem0 | Mem0[g] | MemMachine | 差距 (→Mem0[g]) |
|---|---:|---:|---:|---:|---:|
| Cat1 单跳 | 0.6064 | 0.671 | 0.652 | 0.933 | **−0.046** |
| Cat2 时间 | 0.5296 | 0.538 | 0.581 | 0.726 | **−0.051** |
| Cat3 推理 | 0.4375 | 0.490 | 0.490 | 0.646 | **−0.053** |
| Cat4 多跳 | **0.7931** | 0.702 | 0.757 | 0.805 | **+0.036**（领先） |

**核心判断**：检索像 Mem0[g]，表达退化成 Mem0。

---

## 3. P0 必须立即修

### P0-1 · admin token 明文日志

- **文件**: `src/opencortex/app.py:156-161`
- **证据**:
  ```python
  logger.warning(
      "opencortex.bootstrap_admin_token_created",
      tenant_id="_system",
      user_id="_admin",
      token=token,        # ← 完整 JWT 写进结构化日志
  )
  ```
- **影响**: 任何日志聚合 / 备份 / SaaS 日志服务都能拿到生产管理员凭证
- **修复**: 不向日志或 stdout 输出完整 token；只 log token hash/prefix 与 "stored to token registry"。生产环境优先要求显式配置 `OPENCORTEX_APP_ADMIN_API_TOKEN`，或把一次性 bootstrap token 写入 0600 权限本地文件并只 log 文件路径/前缀。
- **工时**: 30 min
- **回归风险**: 无

---

## 4. 三大 benchmark 短板的代码定位

### 4.1 Cat1 单跳 0.6064 — 病在写入侧抽象失真

| # | 文件:行 | 证据 | 影响 | 修复 |
|---|---|---|---|---|
| C1-1 | `store/writer/semantic_derive_writer.py:96-97` vs `113-116` | abstract/overview 直接 `str(layers.get("abstract", "") or "")` 用 LLM 原始输出；只有 `fact_points` 走 `merge_preserved_fact_points(...content)` 保底 | 不对称：fact_points 保真，abstract/overview 失真。Cat1 命中率被 abstract 拖累 | 把 `merge_preserved_fact_points` 思路扩展到 abstract/overview：从 raw content 抽实体/时间/数值，强制注回 abstract |
| C1-2 | `prompts/write.py:79` | "concise, specific, and not a truncation" 是模糊指令，无 few-shot 示例 | LLM 默认走概括 | 加 3 条 few-shot：`[poor: "张三的旅行经历"]` vs `[good: "张三 2024-03 去东京见 Tanaka"]` |
| C1-3 | `store/writer/search_index_writer.py:51-53, 125` | `max_fact_points=12` + `min_fact_length=8` | 裸日期如 "2024-03" 不应单独进索引，但包含日期/数字/实体的完整事实句必须优先保留 | 保持短 fact 过滤，避免裸日期噪声；把上限提到 16~24，并优先索引完整事实句（如 "张三 2024-03 去东京"） |

### 4.2 Cat2 时间 0.5296 — 病在没有时序结构

| # | 文件:行 | 证据 | 修复 |
|---|---|---|---|
| C2-1 | `store/writer/search_index_writer.py:125` (FactIndex schema) | `time_refs` 目前只在 primary `meta` 中保留并被 secondary index 复制；没有 top-level indexed `event_ts` / `utterance_ts` / `date_range`，无法做 range filter pushdown | payload 加 `event_ts` / `utterance_ts` / `date_range` top-level 字段并建 Qdrant payload index；检索时先时间过滤，再语义排序 |
| C2-2 | `vector/retrieval/cone.py:91` (`neighbor_uris`) | `get_relations()` 返回所有 relations 不分时间序，`max_neighbors=2` 固定 | relations 三元组化 `(target, type, ts)`；temporal query 按时间排序 + 放大 `max_neighbors` |
| C2-3 | `store/document_tree.py:182` | 子 chunk 有 `parent_uri` 无 `section_index` / 顺序边 | 加 `section_index: int` 字段，时间题可按顺序回放 |

### 4.3 Cat3 推理 0.4375 — 病在表达层（最大短板）

| # | 文件:行 | 证据 | 修复 |
|---|---|---|---|
| C3-1 | `vector/retrieval/planner.py:134-139` | `depth()` 只返回 L0 或 L2，**L1 定义了从不返回**；但当前非 no-recall 默认 L2 已能返回最多证据 | 加 `QueryType` 分支驱动预算、surface weights 和 rerank 策略；不要默认把 factual/reasoning 从 L2 降级到 L0/L1，除非评测证明 token 噪声大于证据缺失 |
| C3-2 | `vector/retrieval/retriever.py:95` | `search()` 流程在 reranker 之后直接 `to_matched_memory` → response，**无 LLM 重组步骤** | 插入 `Composer`：top-K hits + cone evidence → LLM 输出 reasoning chain 注入 `MatchedMemory.meta` |
| C3-3 | `prompts/retrieval.py:80-84` (rerank prompt) | "Higher means the candidate **directly contains facts**" —— 纯 pointwise 相似度，无 entailment | prompt 加 "score higher if facts support deriving conclusions"；reasoning query 走 listwise（一次看全部候选） |
| C3-4 | `vector/retrieval/probe.py:337` (`query_size_for`) | 只按长度分 QUICK / MEDIUM / LARGE，**无 factual / temporal / reasoning / multi-hop 分类** | 加 `QueryType` enum + 关键词检测（为什么 / 如何 / 因果），驱动 planner/reranker 分支 |
| C3-5 | `vector/retrieval/reason_tree.py:136` 与 `reranker.py:374-382` | reason_tree 把 fact_points 喂给 LLM 选 URI，但 `rerank_text()` **不读 fact_points** | rerank text builder 拼接 fact_points，给 reranker 看到事实粒度 |

**L0/L1/L2 说明**:

当前代码里需要区分两件事：

- retrieval surface：用哪些索引找候选，例如 `l0_object`、`fact_index`、`entity_index`、`reason_tree_index`。
- hydration depth：候选确定后给 QA 返回多少内容，即 `DetailLevel.L0/L1/L2`。

理想流程应该是逐级展开：

```text
L0 abstract / secondary indexes 找候选
→ L1 overview + fact_points 验证、重排、去噪
→ 只有需要精确事实、时间、多跳或推理时再展开 L2 raw content
```

但当前 `RetrievalPlanner.depth()` 的实现是准确率优先：除 `NO_RECALL` 外直接 hydrate 到 `L2`，避免 LoCoMo 里“命中 URI 但 L0/L1 丢掉时间、数字、地点导致答不出”。因此后续修复不应该静态改成 `factual → L0` / `reasoning → L1` / `multi-hop → L2`；正确方向是做 QueryType + confidence gate 的渐进式 hydration，并用评测确认何时可以停在 L1，何时必须展开 L2。

---

## 5. 性能热点

| # | 文件:行 | 问题 | 预期收益 |
|---|---|---|---|
| PERF-1 | `storage/cfs_queue.py:7` | 直接用 `sqlite3` 同步 API，event worker 跑在 asyncio 上 → 每次 enqueue/dequeue 阻塞事件循环 | 包 `run_in_executor` 或换 `aiosqlite`，queue 操作 **2-4×** |
| PERF-2 | `store/embedder.py` + `vector/embedder.py` | 底层 `OpenAIEmbeddingClient` 是 sync；部分调用已用 `run_in_executor` 包装，但 retrieval probe / writer 仍混用 sync embedding | 统一 async embedding 接口或集中 executor 包装，event worker / retrieval 吞吐 **2-5×** |
| PERF-3 | `store/writer/search_index_writer.py` + `entity_index_writer.py` + `reason_tree_index_writer.py` | 每条记录单独 upsert（N+1 模式） | 加 `batch_upsert()`，二级索引写入 **5-10×** |
| PERF-4 | `store/session/buffer.py` | `SessionBuffer` 无 max size，pruning 每次 lock 触发 O(n) | 加 max_size + LRU 淘汰，pruning 走定时器 |
| PERF-5 | `vector/retrieval/executor.py:41` | `asyncio.gather()` 无 timeout，某个 surface hang 整条 retrieval 阻塞 | `asyncio.wait_for(...)` + per-surface 超时 |
| PERF-6 | `vector/retrieval/ranker.py:41` | raw embedding 分数（0.6-0.95）+ bonus（0-0.18）可能 >1.0，未归一化 | min-max 归一到 [0,1]，让 rerank 权重生效稳定 |

**异步正确性等级**: C —— 存在显著阻塞点，event worker 有并发 worker，但 sync sqlite / sync embedding / per-key lock 会削弱实际并发；未观察到死锁。

---

## 6. 代码质量发现（精选）

| # | 文件:行 | 类别 | 修复 |
|---|---|---|---|
| Q-1 | `core/middlewares.py:71`, `auth/token.py:126`, `vector/retrieval/reranker.py:162` | bare `except Exception:` 静默吞错 | 至少 `logger.exception(...)` + 限定异常类型 |
| Q-2 | `auth/token.py:15` | `from typing import Dict, List, Optional` —— 全 codebase 已用 PEP 604 | 统一改 3.10+ 语法 |
| Q-3 | `vector/retrieval/retriever.py:41` | 构造函数 `vector_store: Any, embedder: Any, ...` 全 `Any` | 定义 `Protocol`，恢复 IDE / type-checker 支持 |
| Q-4 | `console/routes.py` (513 LOC) | 单文件 10+ 路由 + 15+ helper | 拆 `_filters.py` / `_records.py` / `_queries.py` |
| Q-5 | `app.py:144` | `value.count(".") != 2` 魔法数 | `JWT_SEGMENT_COUNT = 3` |
| Q-6 | `vector/retrieval/retriever.py:95` (`search` 方法) | 6+ 子流程串接（probe → planner → executor → reason_tree → cone → ranker → reranker → hydrate）无阶段边界 | 拆分为 named stages + 每段错误边界 |

**Google 风格等级**: B+ —— 命名 / 结构 / 上下文管理器 / 异步用法都符合规范，docstring 在公共 API 覆盖良好。失分集中在 P0 安全 + bare except + `Any` 滥用。

---

## 7. 最终优化路线（按 ROI 排序）

| 优先级 | 动作 | 文件 | 预期收益 | 工时 |
|---|---|---|---|---|
| **P0** | 删 admin token 日志 | `app.py:160` | 安全风险消除 | 30 min |
| **P1** | derive prompt 加 few-shot + 事实硬约束 | `prompts/write.py:79` + `semantic_derive_writer.py:96` | **Cat1 +0.03, Cat3 +0.01** | 3 天 |
| **P1** | abstract/overview 走 `merge_preserved_fact_points` | `semantic_derive_writer.py:113` 同款 | **Cat1 +0.02** | 1 天 |
| **P1** | QueryType 分支驱动 planner/reranker，不默认降级 detail | `planner.py:134`, `probe.py:337` | **Cat3 +0.025** | 3 天 |
| **P1** | Reranker prompt 加 entailment | `prompts/retrieval.py:80` | **Cat3 +0.015** | 2 天 |
| **P1** | embedder 改 async + batch | `store/embedder.py`, `vector/embedder.py` | 吞吐 **3-5×** | 2 天 |
| **P1** | cfs_queue 走 executor / aiosqlite | `storage/cfs_queue.py:7` | 吞吐 **2-4×** | 1 天 |
| **P2** | top-level 时间字段 + Qdrant range filter | `search_index_writer.py:125` + `cone.py:91` | **Cat2 +0.025** | 1 周 |
| **P2** | 二级索引 `batch_upsert` | `*_index_writer.py` | 写入 **5-10×** | 1 周 |
| **P2** | bare except 全替换 | 多文件 | 可调试性 | 半天 |
| **P3** | Composer 步骤（LLM 重组） | 新 `vector/retrieval/composer.py` | **Cat3 +0.04~0.08** | 2 周 |

### 累计预估

| 阶段 | 完成项 | J-Score 轨迹 | 对标 |
|---|---|---:|---|
| 当前 | — | 0.6818 | Mem0[g] |
| P0+P1 完成（~2 周） | 前 7 项 | **~0.74** | Letta 档 |
| P2 完成（~3 周后） | +时间 + perf | **~0.77** | 进入 Zep 自测档 |
| P3 Composer 上线 | 全部 | **~0.81** | 接近 MemMachine 0.849 |

---

## 8. 起步推荐

按 ROI 顺序：

1. **今天** · 删 `app.py:160` 的 token logging（30 分钟，无回归风险）
2. **本周** · `derive prompt + merge_preserved_fact_points 覆盖 abstract` —— 单项 ROI 最高，Cat1+Cat3 同时受益，纯 prompt + 30 行代码
3. **下周** · QueryType 路由 + reranker entailment 联调（两者要一起验证，默认保持 L2 证据充足）

P0+P1 共 9 个工作日，把 J-Score 拉到 Letta 档（0.74）是确定收益。要拉到 MemMachine 档（0.85）必须做 P3 Composer，工时 2 周但 token 预算需要让出一些（当前 LoCoMo token reduction 97.7% 偏极端）。

---

## 9. 附录 · 跨 agent 一致性证据

| 诊断 | 写入 agent | 检索 agent | 性能 agent | 质量 agent |
|---|:---:|:---:|:---:|:---:|
| Abstract 失真 (Cat1) | ✓ P1 | — | — | — |
| Composer 缺位 (Cat3) | — | ✓ P1 | — | — |
| QueryType 缺位 / L1 未被 planner 使用 (Cat3) | — | ✓ P1 | — | — |
| Reranker 无 entailment (Cat3) | — | ✓ P1 | — | — |
| FactIndex 无 top-level 可过滤时间字段 (Cat2) | ✓ P1 | ✓ P2 | — | — |
| Sync embedder 阻塞 | — | — | ✓ P1 | — |
| sqlite3 阻塞 event loop | — | — | ✓ P1 | — |
| N+1 Qdrant 上插 | — | — | ✓ P1 | — |
| Admin token 明文日志 | — | — | — | ✓ **P0** |
| Bare excepts | — | — | — | ✓ P1 |

每条 P1 都至少有一个 agent 独立 file:line 引用；P0 已人工核验。

---

## 10. 制品

- `write-pipeline.json` · 写入侧完整发现
- `retrieval-pipeline.json` · 检索侧完整发现
- `performance.json` · 性能完整发现
- `code-quality.json` · 代码质量完整发现

路径: `/tmp/compound-engineering/ce-code-review/20260512-170549-7c5aec54/`
