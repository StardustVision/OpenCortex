# OpenCortex Phase 1 优化计划

> 状态：Draft  
> 日期：2026-03-14  
> 目标：只聚焦 Phase 1，不讨论自动画像与自动 Skill 学习

## 1. 文档目标

本文档给出一版收敛后的 Phase 1 优化计划。

Phase 1 的目标不是把系统做成“会进化的知识体”，而是把系统做成一套可靠的记忆基建：

- 存得准
- 找得回
- 排得对
- 边界清
- 可解释
- 可评估

本文档默认原则：

- 必须支撑 Phase 1
- 必须保留向后扩展性
- 不为 Phase 2 提前引入过多复杂字段和治理逻辑
- 必须先收缩 Phase 2 对主链路的运行时影响

## 2. Phase 1 的边界

### 2.1 要做什么

Phase 1 只做以下六件事：

1. 统一 memory 的基础数据模型
2. 保证写入链路正确
3. 提高召回准确率
4. 提高排序准确率
5. 保证 scope 隔离与生命周期可靠
6. 建立 explain 与 benchmark

### 2.2 不做什么

Phase 1 不把以下能力作为主交付：

- 自动用户画像提炼
- 自动 Skill 提炼
- 自动知识治理闭环
- 复杂的冲突检测
- 全量版本化知识图谱

这些能力需要 Phase 2 的高层知识结构，不能在 Phase 1 里反客为主。

### 2.3 Phase 1 的前置动作：收缩 Phase 2

当前系统里的 Phase 2 并不只是“未来能力”，其中一部分已经进入默认运行路径。

这会带来三个问题：

- 主链路目标不纯，Phase 1 的召回准确率会被知识候选与自动抽取链路干扰
- 系统复杂度提前上升，问题定位会混入 knowledge / archivist / trace pipeline
- 接口契约变得不稳定，很多“看起来是 recall 问题”的现象，实际是 Phase 2 默认开启造成的

因此，Phase 1 优化计划应把“收缩 Phase 2”作为正式工作项，而不是旁路建议。

这里的收缩，不等于永久删除代码，而是：

- 关闭默认启用的 Phase 2 runtime
- 把 Phase 2 改成显式 feature gate
- 让主系统先只围绕 memory ingest / recall / rank / explain 运转

### 2.4 Phase 1 期间的 Phase 2 处理原则

Phase 1 期间，Phase 2 相关代码建议遵循以下原则：

1. 不删除数据结构与模块目录
2. 不让 knowledge / archivist / auto-skill 流程默认参与主请求
3. 不让 session end 自动触发知识抽取
4. 不让 recall 默认混入 knowledge 结果
5. 对外接口保留兼容壳，但默认关闭或返回 feature disabled

建议采用“配置门控”而不是“直接注释代码”：

- 注释代码只能暂时止血
- feature gate 才能保留后续恢复能力
- benchmark 也更容易在 on/off 两种模式下对比

## 3. Phase 1 的目标架构

### 3.1 总体设计

Phase 1 建议坚持“主链路双层架构 + Phase 2 旁路门控”：

- 主链路索引层：负责召回、过滤、排序
- 主链路内容层：负责摘要、原文、审计
- Phase 2 扩展层：保留模块，但默认不参与主链路

对应到当前系统，可以继续使用：

- Qdrant 作为索引层
- CortexFS 作为内容层
- Alpha / Knowledge / Archivist 作为受控扩展层

### 3.2 Phase 2 收缩后的运行原则

在 Phase 1 默认模式下：

- `prepare()` 只准备 memory context，不默认混入 knowledge
- `commit()` 只保证主记忆链路写入与反馈闭环
- `end()` 不自动触发 trace split / archivist / knowledge candidate 生成
- HTTP / MCP 中的 knowledge 能力默认关闭
- Phase 2 模块只在显式配置打开时参与运行

### 3.3 三层内容视图

Phase 1 仍建议保留三层内容：

| 层级 | 目标 | 默认用途 |
|---|---|---|
| L0 | 一句话摘要 | 快速检索、低成本候选判断 |
| L1 | 结构化概要 | 默认返回层 |
| L2 | 原始内容 | 审计、深度展开 |

Phase 1 不要求所有场景都返回 L2。
Phase 1 的默认返回层建议是 L1。

## 4. Phase 1 最小字段集

### 4.1 设计原则

字段不追求完整，只追求三点：

- 足够支撑 Phase 1
- 不影响未来升级
- 不让当前 schema 过重

### 4.2 最小字段集 v1

建议正式 schema 先收敛到以下字段。字段名以当前代码实现（`collection_schemas.py`）为准，新增字段标注"新增"：

| 字段 | 说明 | 当前代码对应 | 是否必须 |
|---|---|---|---|
| `id` | 主键 | 已有 | 必须 |
| `memory_type` | `profile / preference / constraint / entity / event` | 新增（替代 `category`，见 §4.3 说明） | 必须 |
| `scope` | `private / shared` | 已有（可见性维度，隔离由独立字段实现，见下方说明） | 必须 |
| `abstract` | L0 摘要 | 已有 | 必须 |
| `overview` | L1 结构化概要 | 已有（默认返回层，见 §3.3） | 必须 |
| `content` | 原始内容或 L2 来源 | 已有（通过 CortexFS `content.md`） | 必须 |
| `keywords` | 稀疏召回与硬关键词增强 | 已有 | 必须 |
| `source_type` | `user_explicit / doc / event / inferred` | 新增 | 必须 |
| `status` | `active / deprecated` | 新增（当前仅 alpha 集合有 status） | 必须 |
| `created_at` | 创建时间 | 已有 | 必须 |
| `updated_at` | 更新时间 | 已有 | 必须 |
| `accessed_at` | 最近访问时间 | 已有（代码命名为 `accessed_at`） | 必须 |
| `active_count` | 访问次数 | 已有（代码命名为 `active_count`） | 必须 |
| `vector` | 稠密向量 | 已有（代码命名为 `vector`，dense named vector） | 必须 |
| `sparse_vector` | 稀疏向量 | 已有（BM25SparseEmbedder 已全面集成） | 必须 |
| `reward_score` | RL 反馈累计分 | 已有 | 必须 |
| `meta` | 扩展字段容器 | 已有（通过 payload 扩展字段） | 必须 |

> **命名对齐说明**：文档统一使用代码中的实际字段名。`accessed_at`（非 `last_accessed_at`）、`active_count`（非 `access_count`）、`sparse_vector`（非 `embedding_sparse`）、`vector`（非 `embedding_dense`）。

> **scope 与隔离的关系**：当前系统的 tenant/user/project 隔离不是通过 `scope` 字段实现的。隔离由三个独立字段 `source_tenant_id`、`source_user_id`、`project_id` 配合 `RequestContextMiddleware`（`http/request_context.py`）实现——每个请求通过 HTTP headers 携带身份信息，中间件注入 contextvars，存储层按这些字段过滤。`scope` 字段（`private/shared`）只控制可见性范围。Phase 1 保持这一设计不变，不将隔离逻辑迁移到 `scope` 字段。

### 4.3 与现有 schema 的关系

当前 `collection_schemas.py` 定义的 context collection 有 36 个字段。Phase 1 最小字段集的策略是：

- **保留并继续使用**：`id`, `uri`, `parent_uri`, `abstract`, `overview`, `keywords`, `scope`, `created_at`, `updated_at`, `accessed_at`, `active_count`, `vector`, `sparse_vector`, `reward_score`, `session_id`, `source_user_id`, `source_tenant_id`, `project_id`
- **新增**：`memory_type`（替代 `category`）、`source_type`、`status`
- **Phase 1 保留**：`context_type`（与 `memory_type` 正交，见下方说明）
- **Phase 1 后逐步废弃**：`category`（被 `memory_type` 替代）、`type`（冗余）
- **保留但降级**：`mergeable`（被 `memory_type` 的写入策略隐含）

#### `context_type` 与 `memory_type` 的关系

这两个字段是**正交维度**，不是替代关系：

- `context_type`（`memory/resource/skill/case/pattern/staging`）回答的是"内容形态是什么"——是一条记忆、一份文档资源、还是一个技能模板
- `memory_type`（`profile/preference/constraint/entity/event`）回答的是"语义分类是什么"——是用户偏好、约束规则、还是临时事件
- `category` 与 `memory_type` 功能重叠，是被替代的对象

举例：一份文档导入的实体记忆，`context_type=resource, memory_type=entity, source_type=doc`——三个字段各自独立。

Phase 1 中 `context_type` 继续保留并参与检索过滤（`HierarchicalRetriever` 中已有 `context_type` filter）。`memory_type` 作为新增的语义分类维度，与 `context_type` 并存，不替代它。迁移映射见 §4.6。

### 4.4 为什么这组字段够用

这组字段已经足够支撑：

- 语义分类：`memory_type`
- 内容形态：`context_type`（保留）
- 可见性：`scope`（private/shared）
- 身份隔离：`source_tenant_id + source_user_id + project_id`（保留）
- 语义检索：`vector`（dense）
- 专有名词检索：`sparse_vector + keywords`
- 三层内容：`abstract`（L0）+ `overview`（L1）+ `content`（L2）
- 热度排序：`accessed_at + active_count`
- 来源优先级：`source_type`
- RL 反馈排序：`reward_score`
- 生命周期控制：`status`
- 向后扩展：`meta`

### 4.5 明确后置的字段

以下字段先不进正式 Phase 1 核心 schema，统一留给 `meta` 或 Phase 2：

- `trust_level`
- `confidence`
- `evidence_refs`
- `relations`
- `supersedes`
- `valid_from`
- `valid_to`
- `candidate_status`
- `version`

这些字段有价值，但不是 Phase 1 的必要条件。

### 4.6 Schema 迁移策略

Phase 1 的数据模型收敛会引入新字段（`memory_type`、`source_type`、`status`），现有数据没有这些字段。必须制定迁移策略，避免大爆炸式升级。

#### 现有分类体系

当前系统使用两个字段联合分类：

- `context_type`：`memory / resource / skill / case / pattern / staging`（粗粒度类型）
- `category`：`profile / preferences / entities / events / cases / patterns / documents / ...`（细粒度分类）
- `MERGEABLE_CATEGORIES = frozenset({"profile", "preferences", "entities", "patterns"})`（定义在 `retrieve/types.py:28`）

目标 `memory_type` 是对 `category` 的语义升级，映射关系如下：

| 现有 category | 目标 memory_type | 说明 |
|---|---|---|
| `profile` | `profile` | 直接映射 |
| `preferences` | `preference` | 去复数 |
| `entities` | `entity` | 去复数 |
| `events` | `event` | 去复数 |
| `documents` | `entity` | 文档导入归为长期对象 |
| `cases` / `patterns` | `event` | 保守降级 |
| 其他 / 缺失 | `event` | 最保守默认值 |

> 注意：现有 category 中没有 `constraint`。Phase 1 新增此类型，仅通过新写入产生，不从旧数据迁移。

#### 迁移原则

- 新字段必须有默认值，旧数据不迁移也能被检索到
- `memory_type` 默认值：`event`（最保守，不会错误提升优先级）
- `source_type` 默认值：`inferred`（最保守）
- `status` 默认值：`active`
- `sparse_vector`：当前已全面集成（`BM25SparseEmbedder` 通过 `CompositeHybridEmbedder` 包装所有 embedding provider），新写入已自动生成；旧数据若缺少，迁移脚本补算
- 迁移脚本幂等，可重复执行

#### 渐进式迁移路径

1. 新增字段到 schema，带默认值，旧数据自动降级为最保守分类
2. 运行迁移脚本，根据 `category` 映射表批量更新 `memory_type`
3. 过渡期 `category` 与 `memory_type` 并存，检索同时支持两种过滤
4. 人工抽检迁移结果，修正误分类
5. 过渡期结束后，检索链路切换到 `memory_type`，`category` 降级为只读兼容字段

## 5. Memory 类型定义

Phase 1 建议将 memory 明确拆成 5 类：

| 类型 | 含义 | 默认写入策略 | 默认检索优先级 |
|---|---|---|---|
| `profile` | 稳定用户事实 | merge/update | 高 |
| `preference` | 用户偏好与工作习惯 | merge/update | 高 |
| `constraint` | 不可违反的边界与规则 | conflict-check + merge | 最高 |
| `entity` | 长期对象、项目、模块、主题 | merge/update | 中高 |
| `event` | 会话事件与一次性经历 | append | 中低 |

关键原则：

- `event` 不等于长期知识
- `constraint` 必须单独建类，不能混在 preference 里
- `profile/preference/constraint/entity` 默认走稳定记忆路径

### 5.1 Merge 策略与 `MERGEABLE_CATEGORIES` 的对应

当前 dedup 逻辑依赖 `MERGEABLE_CATEGORIES = frozenset({"profile", "preferences", "entities", "patterns"})`（`retrieve/types.py:28`）。Phase 1 引入 `memory_type` 后，merge 策略重新定义如下：

| memory_type | merge 策略 | 替代原 category |
|---|---|---|
| `profile` | merge/update（同 scope 下同主题去重，保留最新） | `profile` |
| `preference` | merge/update（同 scope 下同主题去重，保留最新） | `preferences` |
| `entity` | merge/update（同 scope 下同对象去重，保留最新） | `entities` |
| `constraint` | **conflict-check + merge**（见下方说明） | 新增 |
| `event` | append only（不去重，不合并） | `events` |

#### Constraint 的特殊 merge 行为

`constraint` 不能像 `preference` 一样静默 merge。两条矛盾的约束同时存在会导致系统行为不可预测。

Phase 1 对 `constraint` 的 merge 策略——**标记冲突候选，不自动废弃旧规则**：

1. 同 scope 下写入新 constraint 时，先做语义相似度检查（复用 dedup 的 embedding 相似度阈值）
2. 如果命中相似的已有 constraint：
   - 语义一致（补充/细化）→ merge，保留更完整的版本
   - 语义冲突（矛盾）→ 新 constraint 正常写入（`status=active`），旧 constraint **保持 `status=active` 不变**，在新 constraint 的 `meta` 中标记 `potential_conflict_with: [old_id]`
3. 冲突检测仅做 embedding 相似度匹配，不引入 LLM 判定（Phase 1 不做"复杂的冲突检测"，与 §2.2 保持一致）

> **设计决策**：Phase 1 选择"标记冲突候选"而非"自动废弃旧规则"，原因是写路径上的 LLM 误判风险不对称——recall 链路误判只是返回不相关记忆（用户可忽略），write 链路误判会静默废弃仍然有效的约束（难以发现、难以恢复）。冲突解决（LLM 判定 + 用户确认）留给 Phase 2。

#### 新的 MERGEABLE 常量

```python
MERGEABLE_TYPES = frozenset({"profile", "preference", "entity", "constraint"})
# event 不在此集合中，始终 append
```

Phase 1 过渡期 `MERGEABLE_CATEGORIES` 与 `MERGEABLE_TYPES` 并存。当 `memory_type` 字段存在时优先使用 `MERGEABLE_TYPES`，否则 fallback 到 `MERGEABLE_CATEGORIES`。

## 6. 写入链路优化

### 6.1 目标

写入链路的目标不是“尽量多记”，而是“尽量少错记”。

### 6.2 建议写入分流

建议将 Phase 1 写入路径拆为三类：

1. `explicit memory write`
   用户明确要求记住，或用户明确表达稳定事实
2. `document ingest`
   文档、知识文件、代码扫描导入
3. `event capture`
   会话事件或即时记录

### 6.3 写入策略

#### 对稳定类 memory

适用：

- `profile`
- `preference`
- `constraint`
- `entity`

策略：

- 先分类
- 在当前身份隔离范围内（`RequestContextMiddleware` 注入的 `source_tenant_id` + `source_user_id` + `project_id`），做同 scope、同 memory_type 下的去重
- 命中重复后 merge/update
- 保留最近内容与摘要一致性

#### 对 event

策略：

- 只 append
- 不默认晋升为长期事实
- 可以压缩，但不应自动变成 `profile/preference/constraint`

### 6.4 必须补强的现有点

结合当前实现，写入链路应优先改造以下内容：

- 增加 `constraint` 类型
- 明确 `source_type`
- 重新约束 `event` 的长期记忆地位
- 让 dedup 策略以 `memory_type + scope` 为核心，而不是仅依赖 category

## 7. 检索链路优化

### 7.1 检索核心原则

Phase 1 的检索不能只做“全库向量 top-k”。

正确顺序应是：

1. 解析 query intent
2. 收缩检索 scope
3. 收缩 memory type
4. 做 hybrid retrieval
5. 做 rerank 与加权排序

### 7.2 检索规划建议

#### 按 memory type 规划

| 问题类型 | 优先 memory_type |
|---|---|
| 用户偏好 | `preference` |
| 长期稳定事实 | `profile` |
| 不可违反规则 | `constraint` |
| 项目、模块、对象 | `entity` |
| 上次、最近、这次 | `event` |

#### 按时间规划

建议正式支持：

- `session`：当前会话
- `recent`：近期记忆，按 memory_type 差异化定义（见下表）
- `all`：全部历史

`recent` 的时间窗口必须有明确定义，否则无法转化为 Qdrant 过滤条件，也无法在 benchmark 中判定 pass/fail。

不同 memory_type 的"近期"含义不同，参照艾宾浩斯遗忘曲线的直觉——事件衰减快，约束几乎不过期：

| memory_type | recent 默认窗口 | 设计依据 |
|---|---|---|
| `event` | 3 天 | 事件衰减最快，3 天前的事件已不算"近期" |
| `entity` | 14 天 | 项目/模块的关注周期中等 |
| `preference` | 30 天 | 偏好相对稳定，一个月内都算近期 |
| `profile` | 90 天 | 用户背景很少变化 |
| `constraint` | 180 天 | 约束几乎不过期 |

默认值写入配置文件，允许覆盖。

#### 当前实现状态

`IntentRouter` 已实现 `time_scope` 的提取（`intent_router.py:194-201`，通过 `_TEMPORAL_KEYWORDS` 关键词匹配 + LLM 分析），提取结果存入 `SearchIntent.time_scope`（`types.py:297`）。但 **当前 time_scope 仅作为标签传递，未转化为实际的 Qdrant 过滤条件**——在 `HierarchicalRetriever.retrieve()` 中完全被忽略。

Schema 中 `created_at`、`updated_at`、`accessed_at` 三个时间字段均已建立 `ScalarIndex`（`collection_schemas.py:74-76`），具备过滤能力。

#### 落地方案

IntentRouter 解析出 `time_scope=recent` 后，需在 `HierarchicalRetriever` 或 `orchestrator.search()` 中构建时间过滤条件。过滤条件需通过 `filter_translator.py` 的 VikingDB DSL 转换为 Qdrant Filter：

```python
# 在构建 metadata_filter 时注入时间条件
if time_scope == "recent":
    window = RECENT_WINDOW.get(memory_type, 180)
    metadata_filter.append({
        "op": "gte",
        "field": "created_at",
        "conds": [(now() - timedelta(days=window)).isoformat()]
    })
elif time_scope == "session":
    metadata_filter.append({
        "op": "must",
        "field": "session_id",
        "conds": [current_session_id]
    })
# time_scope == "all" 不加时间过滤
```

如果查询未指定 memory_type，使用所有类型中最长的窗口（180 天）作为兜底。

> **已知限制**：180 天兜底会让 `recent` 对 event 类型近乎等于 `all`。更精确的实现是按 memory_type 构造 per-type OR 条件（`(type=event AND created_at > 3d) OR (type=entity AND created_at > 14d) OR ...`），但需要 `filter_translator.py` 支持嵌套 OR+AND 复合条件。Phase 1 先用 180 天保守兜底，per-type OR 过滤作为增强项评估。

这些时间窗口必须真正参与检索过滤，而不是只停留在 intent 标签中。

### 7.3 检索算法建议

Phase 1 建议保留并强化以下算法：

- dense retrieval
- sparse retrieval
- lexical retrieval
- RRF 融合
- 条件 rerank
- freshness 排序

建议新增两个显式排序因子：

- `type_prior`
- `source_prior`

这样排序从：

- “内容像不像”

变成：

- “内容像不像 + 类型该不该优先 + 来源该不该优先”

### 7.4 现有实现应优先修复的点

当前实现里，检索链路应优先处理：

- `time_scope` 真正落地
- `memory_context` 配置透传完整
- `category/context_type/include_knowledge` 过滤真正生效
- 默认关闭 `include_knowledge`
- Phase 1 检索统一围绕 `memory_type` 规划，而不是继续堆 category

## 8. 排序策略优化

### 8.1 当前排序实现

当前代码（`hierarchical_retriever.py`）的排序公式：

```
fused = beta * rerank_score + (1 - beta) * retrieval_score + reward_weight * reward_score + hot_weight * hotness_score
```

参数值（硬编码）：

| 参数 | 值 | 位置 |
|---|---|---|
| `beta`（fusion_beta） | 0.7 | `rerank_config.py:24` |
| `reward_weight` | 0.05 | `hierarchical_retriever.py:69` |
| `hot_weight` | 0.03 | `hierarchical_retriever.py:70` |

其中 `hotness_score` 的计算公式（`hierarchical_retriever.py:126-159`）：

```python
hotness = sigmoid(log1p(active_count)) * exp(-λ * age_days)
# λ = ln(2)/7 ≈ 0.099，7 天半衰期
# age_days 基于 accessed_at，默认 30 天
```

当前排序有 4 个因子，但缺少两个关键维度：

- **无 type_prior**：所有 memory type 同等对待，constraint 不会比 event 优先
- **无 source_prior**：用户明确表达与系统推断同等对待

### 8.2 Phase 1 目标排序公式：3 因子

Phase 1 的排序不追求因子数量多，而追求可解释、可调、可验证。

将当前 4 因子 + 新增 2 因子，重组为 3 个语义组：

```
final_score = α × relevance + β × prior + γ × freshness
```

初始权重：`α=0.6, β=0.25, γ=0.15`

| 因子 | 含义 | 组成 | 与当前代码的关系 |
|---|---|---|---|
| `relevance` | 这条记忆和 query 有多相关 | RRF 融合分经 rerank 校准后的输出 | 合并当前的 `beta * rerank + (1-beta) * retrieval` |
| `prior` | 这类记忆该不该优先 | `type_weight + source_weight`，静态查表 | **新增**，当前无此因子 |
| `freshness` | 这条记忆当前是否活跃 | 时间衰减 + 访问频率 + 反馈分 | 合并当前的 `hotness_score` + `reward_score` |

合并理由：

- 当前 `beta * rerank + (1-beta) * retrieval` 已经是一个融合后的相关性分数，直接作为 `relevance`
- `type_prior + source_prior` 是新增的静态查表值，合成一个 `prior`
- 当前 `hotness_score`（访问频率 + 时间衰减）和 `reward_score`（RL 反馈累计）描述的都是"这条记忆当前的活跃价值"，合成一个 `freshness`

3 个权重的搜索空间远小于分散加权，手工试几组就能找到合理范围。

### 8.3 Prior 权重表

`prior` 的内部计算不需要参数搜索，直接用静态查表：

```python
TYPE_WEIGHT = {
    "constraint":  0.30,
    "preference":  0.20,
    "profile":     0.15,
    "entity":      0.10,
    "event":       0.00,
}

SOURCE_WEIGHT = {
    "user_explicit": 0.20,
    "doc":           0.10,
    "event":         0.05,
    "inferred":      0.00,
}

prior = TYPE_WEIGHT[memory_type] + SOURCE_WEIGHT[source_type]
```

这些值可以从业务语义直接推导——constraint 就是应该比 event 优先，用户明确表达就是应该比系统推断优先。这是设计决策，不是统计结论。

### 8.4 Freshness 衰减公式

当前系统已有两套衰减机制：

1. **排序时 hotness**（`hierarchical_retriever.py:126-159`）：`sigmoid(log1p(active_count)) * exp(-λ * age_days)`，固定 7 天半衰期
2. **存储层 RL decay**（`qdrant/adapter.py:906-976`）：`reward *= (rate + access_bonus)`，rate=0.95 / protected=0.99

Phase 1 将两者统一为单一 `freshness` 函数，替代当前的 hotness + reward 分离计算。采用艾宾浩斯遗忘曲线模型，让不同 memory_type 有不同的衰减速率：

```python
TYPE_STABILITY = {
    "event":       1.0,    # 衰减最快
    "entity":      3.0,
    "preference":  5.0,
    "profile":     8.0,
    "constraint":  15.0,   # 衰减最慢
}

# 基础稳定性：保证 active_count=0 时 S 不为零
BASE_STABILITY = 1.0

def freshness_score(memory) -> float:
    t = (now() - memory.accessed_at).total_seconds() / 86400  # 天数
    type_s = TYPE_STABILITY[memory.memory_type]
    S = (BASE_STABILITY + math.log1p(memory.active_count)) * type_s
    base = math.exp(-t / S)
    feedback = memory.reward_score * 0.05  # RL 反馈微调
    return base + feedback
```

> **零访问保护**：当 `active_count=0`（刚写入、从未被检索）时，`S = BASE_STABILITY * type_s`，不会归零。例如一条刚创建的 constraint，`S = 1.0 * 15.0 = 15.0`，1 天后 freshness = `e^(-1/15) ≈ 0.94`——合理地保持高鲜度。当前 hotness 公式中 `sigmoid(log1p(0)) = 0.5` 也提供了非零基础分，新公式通过 `BASE_STABILITY` 保持一致。

与当前实现的差异：

| 维度 | 当前 hotness | 目标 freshness |
|---|---|---|
| 半衰期 | 固定 7 天 | 按 memory_type：event ~1天 → constraint ~15天 |
| 零访问行为 | `sigmoid(0) = 0.5`，有基础分 | `BASE_STABILITY` 保底，`S ≥ type_s` |
| 访问频率 | `sigmoid(log1p(active_count))` 独立乘因子 | `log1p(active_count)` 放大稳定性 S |
| RL 反馈 | 独立因子 `reward_weight * reward_score` | 合并进 freshness 作为微调 |
| 受保护记忆 | `protected` 标记 → 慢衰减 | memory_type=constraint 天然慢衰减 |

核心特性：

- 刚写入的记忆 freshness 接近 1.0，不会因为从未被访问而归零
- 同一条 `preference`，被访问过 10 次的比只被访问 1 次的衰减更慢——复习增强记忆
- `constraint` 的基础稳定性是 `event` 的 15 倍——约束不应因时间流逝而快速失效
- `reward_score` 作为微调项叠加，不主导排序
- 统一替代当前分散在两个模块的两套衰减逻辑

#### 废弃 `apply_decay()`，统一为查询时计算

当前存储层的 `apply_decay()`（`qdrant/adapter.py:906-976`）是一个批量后台任务，定期修改 Qdrant 中的 `reward_score`。它存在以下问题：

- 与查询时的 freshness 计算职责重叠，容易导致双重衰减
- 批量执行的时机不确定，衰减行为不可预测（同一条记忆在 decay 执行前后排序结果不同）
- 依赖后台定时任务，增加运维复杂度
- `protected` 标记的慢衰减逻辑，在 `memory_type` 引入后被 `TYPE_STABILITY` 天然替代

Phase 1 的处理方式——**用 Ebbinghaus freshness 完全替代旧的双层衰减**：

| 旧机制 | 替代方案 |
|---|---|
| `_compute_hotness()`（查询时，7 天固定半衰期） | `freshness_score()`（查询时，按 memory_type 差异化半衰期） |
| `apply_decay()`（存储层，定期修改 reward_score） | 废弃。reward_score 保留为原始反馈累计值，不再被后台衰减 |
| `hot_weight = 0.03`（独立加权） | 合并进 `γ × freshness`（统一权重） |
| `reward_weight = 0.05`（独立加权） | reward_score 纳入 freshness 公式，不再独立加权 |
| `protected` 标记 → 慢衰减 | `TYPE_STABILITY` 按 memory_type 天然差异化。`protected` 字段保留但仅用于标记，不再影响计算 |
| `access_bonus = 0.04 * exp(-days/30)` | `log1p(active_count)` 放大 S 值，自然实现访问增强 |

**reward_score 不再被后台衰减的合理性**：

`reward_score` 保留为用户反馈的原始累计值（正反馈 +1，负反馈 -1）。时间衰减完全由 `freshness_score()` 中的 `e^(-t/S)` 主项负责。`reward_score * 0.05` 只是一个小幅微调——即使一条 2 年前的记忆 reward_score=1.0，微调只有 0.05，而主项 `e^(-730/S)` 早已趋近于零，不会影响排序。

这样做的好处：

- 衰减行为完全确定：同样的输入永远产生同样的排序
- 零运维开销：不需要定时后台任务
- 无双重衰减风险
- `reward_score` 保持可解释性：它就是"用户给了多少正/负反馈"，不会被后台静默修改

### 8.5 演进路径

Phase 1 先用 3 因子 + hardcoded 权重，用 benchmark 验证。如果 benchmark 表明某个子因子需要独立调权（例如 reward_score 的影响需要单独放大），再从 3 因子中拆出，演进到 4 因子。

实施时需要：

1. 新增 `freshness_score()` 函数（Ebbinghaus 模型 + `BASE_STABILITY` + `TYPE_STABILITY`）
2. 新增 `prior_score()` 函数（`TYPE_WEIGHT` + `SOURCE_WEIGHT` 查表，零开销）
3. 删除 `_compute_hotness()` 函数及 `_HOTNESS_LAMBDA` 常量（`hierarchical_retriever.py:123-159`）
4. 删除 `hot_weight` 和 `reward_weight` 两个独立常量（`hierarchical_retriever.py:70,69`）
5. 将分散在 9 个位置的 score 加算（行 282、477、487、608、788、831、871、937、969）统一为 3 因子公式
6. 废弃 `apply_decay()` 方法（`qdrant/adapter.py:906-976`）——保留代码但不再调用
7. 修改 `orchestrator.py` 中 `decay()` 入口（行 1680-1712）：返回 `{"status": "deprecated", "message": "replaced by query-time freshness"}`
8. `protected` 字段保留在 schema 中但不再参与计算，`set_protected()` 方法标记为 deprecated
9. 更新测试：`test_recall_optimization.py` 中 `TestHotnessScoring`（行 519-585）改为测试 `freshness_score()`；`test_text_scoring.py` 中 `TestAccessDrivenDecay`（行 134-206）改为测试 Ebbinghaus 衰减曲线

Phase 1 的要求是：

- 每一项都可解释
- 每一项都可调
- 每一项都能被 benchmark 验证

## 9. 可解释性与可观测性

Phase 1 的 explain 不是调试附属品，而是正式能力。

### 9.1 Explain 必须回答的问题

一次 recall 至少要能解释：

- query 被解析成了什么 intent
- 搜了哪些 scope
- 搜了哪些 memory type
- 哪些候选被召回
- dense / sparse / lexical / rerank 各自贡献了什么
- 为什么 A 在 B 前面
- 为什么某条被过滤掉

### 9.2 建议补充的能力

- explain API
- search_debug API 增强
- 检索 trace 输出结构化
- query plan 可视化

## 10. 可靠性与边界

### 10.1 必须保证的边界

Phase 1 必须保证：

- tenant 隔离
- user 隔离
- project 隔离
- session 与长期 memory 的边界
- `event` 与稳定 memory 的边界

### 10.2 必须优先解决的可靠性问题

结合当前系统，Phase 1 优先级最高的可靠性问题是：

- transcript 不能只停留在内存缓冲
- prepare / commit / end 参数契约必须统一
- Alpha 相关链路不能污染主记忆链路

### 10.3 Phase 2 收缩清单

当前系统已有 `CortexAlphaConfig`（`config.py:39-65`）提供 per-component 配置开关，但所有开关默认为 `True`。Phase 1 的收缩不需要新建 feature gate 机制，而是**修改现有开关的默认值**。TraceStore / KnowledgeStore 的 `init()` 虽然不受 config 开关门控（始终在 `storage + embedder` 可用时初始化），但其启动成本仅为一次性集合创建（毫秒级），且运行时数据流已被 `trace_splitter_enabled` / `archivist_enabled` 有效截断——TraceSplitter 关闭后无 trace 写入 TraceStore，Archivist 关闭后无 knowledge 写入 KnowledgeStore。

建议把以下内容列为 Phase 1 的正式治理动作：

| 模块/入口 | 当前状态 | 代码位置 | Phase 1 处理方式 |
|---|---|---|---|
| `ContextManager.prepare()` 的 `include_knowledge` | 默认 `True` | `manager.py:172` | 改默认值为 `False` |
| MCP `recall` 工具的 `include_knowledge` | 默认 `true` | `mcp-server.mjs:85` | 改默认值为 `false` |
| `CortexAlphaConfig.trace_splitter_enabled` | 默认 `True` | `config.py:48` | 改默认值为 `False` |
| `CortexAlphaConfig.archivist_enabled` | 默认 `True` | `config.py:50` | 改默认值为 `False` |
| `_init_alpha()` 无条件调用 | 始终执行 | `orchestrator.py:221` | 仅 Observer 始终初始化，其余组件受 config 门控 |
| `session_end()` 自动 trace split + archivist | TraceSplitter 有数据即拆分，Archivist 达阈值即触发 | `orchestrator.py:1934-1952` | 受 `trace_splitter_enabled` / `archivist_enabled` 控制 |
| knowledge/archivist HTTP endpoints | 始终暴露，无门控 | `server.py:323-365` | 增加 config 检查，未启用时返回 `{“error”: “feature disabled”}` |

> **已发现的实现断点**：`orchestrator.session_end()` 返回 `alpha_traces`（行 1959），但 `ContextManager._end()` 尝试提取 `knowledge_candidates`（`manager.py:504`），该 key 始终缺失（因 Archivist 异步执行），导致 `knowledge_candidates` 始终为 0。此断点应在 P1 链路正确性阶段修复。

收缩目标不是把这些模块删掉，而是让它们从”默认主链路”退回”受控实验能力”。

## 11. Phase 1 的 benchmark 体系

Phase 1 不建议再以“主观感觉变聪明”作为优化依据。

必须建立回归型 benchmark。

### 11.1 建议测试集结构

至少包括以下五类：

- 偏好召回
- 约束召回
- 实体召回
- 事件召回
- 硬关键词召回

### 11.2 建议指标

#### 正确性指标

| 指标 | 说明 |
|---|---|
| `Recall@5` | 前五条能否覆盖正解 |
| `MRR` | 正解排位是否足够靠前 |
| `Precision@5` | 前排结果是否噪声过多 |
| `Type Accuracy` | 是否优先搜到正确 memory type |
| `Scope Accuracy` | 是否未发生越权召回 |
| `Hard Keyword Hit Rate` | 专有名词、路径、错误码是否稳定命中 |
| `False Recall Rate` | 错召回率 |

#### 性能指标

| 指标 | 说明 |
|---|---|
| `Recall P95 Latency` | prepare() 从收到请求到返回结果的端到端耗时 |
| `Write P95 Latency` | add() 从收到请求到写入完成的端到端耗时 |

性能指标与正确性指标具有对等约束地位。后续任何优化不能为了正确性指标的微小提升而导致延迟大幅恶化。具体目标值在 P0 跑 baseline 后设定，建议红线为 baseline 的 1.5 倍。

### 11.3 指标门槛

Phase 1 不预设绝对目标值。指标门槛的确定流程：

1. P0 收缩 Phase 2 后，在干净状态下跑一轮 baseline
2. 基于 baseline 为每个指标设定 Phase 1 目标（`baseline + Δ`）
3. 设定回归红线：任何 PR 不能让任何指标低于 baseline
4. Phase 1 完成时，所有指标必须达到目标值

### 11.4 Benchmark 构建方法

Benchmark 必须作为一等公民工程交付物管理，不能停留在一次性脚本。

#### 测试集来源

- 从现有 E2E 测试中提取 query-answer 对作为种子集
- 从真实会话日志中采样，人工标注 ground truth
- 针对 5 种 memory_type 各构造专项 case

#### 测试集规模

- Phase 1 最小可用集：50 个 query，每个 query 标注 1–3 个正确记忆 ID
- 每种 memory_type 至少覆盖 10 个 query
- 硬关键词专项：至少 10 个包含路径、错误码、配置项的 query

#### 执行方式

- 离线脚本：先写入测试数据，再执行查询，比对召回结果与标注 ground truth
- 纳入 CI：每次检索链路改动自动跑回归
- 输出结构化报告：每个指标的当前值、与 baseline 的 delta、是否触发红线

#### 版本管理

- 测试集与代码同仓库管理，放在 `tests/benchmark/` 目录
- 测试集变更必须有 PR review
- 禁止为了让指标好看而修改测试集（benchmark hacking）

## 12. 实施优先级

### P0：收缩 Phase 2 + 建诊断基线

- 修改 `CortexAlphaConfig` 默认值：`trace_splitter_enabled=False`, `archivist_enabled=False`
- 修改 `ContextManager.prepare()` 默认 `include_knowledge=False`
- 修改 MCP recall 工具默认 `include_knowledge=false`
- knowledge/archivist HTTP endpoints 增加 config 门控
- Observer 保持始终启用（transcript 记录是 Phase 1 持久化可靠性的基础）
- 构建最小 benchmark 测试集（50 query + ground truth）
- 在收缩后的状态下跑**诊断基线**，记录所有指标初始值（此基线度量的是含已知链路缺陷的系统，用于衡量 P1 修复的改善幅度，不作为回归红线）

### P1：链路正确性

- 修复协议透传
- 修复 filter DSL / storage 契约不一致
- 修复 Alpha 子系统与主系统的接口断点
- P1 完成后重跑 benchmark，建立**验收基线**——此基线作为后续 P2-P5 所有改动的回归红线

### P2：数据模型收敛

- 新增 `memory_type` 字段（`profile/preference/constraint/entity/event`），替代 `category`（`context_type` 正交保留，见 §4.3）
- 新增 `source_type` 字段（`user_explicit/doc/event/inferred`）
- 新增 `status` 字段（`active/deprecated`）
- 编写 schema 迁移脚本（`category` → `memory_type` 映射表，见 §4.6）
- 过渡期 `category` 与 `memory_type` 并存

### P3：持久化可靠性

- transcript durable buffer（Observer 从纯内存缓冲升级为准持久化）
- prepare / commit / end 参数契约统一
- event 到长期 memory 的受控晋升

### P4：检索准确率提升（有 benchmark 量化每一步效果）

- 落地 `time_scope`：在 `HierarchicalRetriever` 中将 `SearchIntent.time_scope` 转化为 Qdrant 时间过滤（通过 `filter_translator.py` DSL）
- 实现 recent 按 memory_type 差异化窗口（event 3天 → constraint 180天）
- **Ebbinghaus freshness 替代旧衰减**：
  - 新增 `freshness_score()` + `prior_score()` 函数
  - 删除 `_compute_hotness()` + `_HOTNESS_LAMBDA` + `hot_weight` + `reward_weight`
  - 统一 9 处分散的 score 加算为 3 因子公式 `α×relevance + β×prior + γ×freshness`
  - 废弃 `apply_decay()`，orchestrator 的 `decay()` 入口返回 deprecated
  - `protected` 字段标记 deprecated，不再参与计算
  - 更新 `TestHotnessScoring` + `TestAccessDrivenDecay` 测试用例
- 优化 hard keyword 召回
- 每项改动前后跑 benchmark 回归

### P5：可解释性增强

- 增强 explain API
- 检索 trace 输出结构化
- query plan 可视化

## 13. Phase 1 完成定义

Phase 1 完成，不代表系统已经“会学习”。

Phase 1 完成只代表下面这些条件成立：

- 用户明确表达的稳定偏好可跨会话召回
- 约束类记忆稳定优先于偏好与事件
- 专有名词、路径、配置项、错误码可稳定命中
- session / recent / all 三类时间意图能真正影响检索
- tenant/user/project 边界稳定生效
- 错召回可解释、可归因、可回归
- Phase 2 能力已退出默认运行面，不再干扰 Phase 1 指标
- Phase 2 只能通过显式开关进入实验路径

只要这些没成立，就不应该进入 Phase 2 主线。

## 14. 最终判断

Phase 1 的正确方向不是继续堆功能，而是做减法：

- 收敛字段
- 收敛 memory 类型
- 收敛目标
- 收敛评价标准

把记忆系统先做成可靠基础设施，再谈自动画像和 Skill 学习。
