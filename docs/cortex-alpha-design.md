# Cortex Alpha 设计文档

> **文档状态**: Draft v3（经头脑风暴 + Codex/Gemini Review 修订）
> **基线版本**: OpenCortex 0.3.9
> **更新日期**: 2026-03-10

---

## 目录

1. [设计目标与非目标](#1-设计目标与非目标)
2. [目标平台与部署模型](#2-目标平台与部署模型)
3. [总体架构](#3-总体架构)
4. [决策记录](#4-决策记录)
5. [新增模块设计](#5-新增模块设计)
6. [存储设计](#6-存储设计)
7. [数据模型](#7-数据模型)
8. [知识作用域与治理](#8-知识作用域与治理)
9. [核心流程](#9-核心流程)
10. [算法设计](#10-算法设计)
11. [配置设计](#11-配置设计)
12. [模块废弃与迁移](#12-模块废弃与迁移)
13. [推荐 API 扩展](#13-推荐-api-扩展)
14. [测试计划](#14-测试计划)
15. [风险与控制](#15-风险与控制)
16. [未来方向](#16-未来方向)

---

## 1. 设计目标与非目标

### 1.1 目标

把 OpenCortex 从"会记住"升级为"会进化"——精确存储、精确检索、自动采集、持续优化。

- 全自动数据采集：用户正常使用即可，不需要主动"记东西"
- 精确存储：schema 完整，该存的字段一个不漏
- 精确检索：搜出来的东西准确、相关、token 消耗极低
- 记忆生命周期管理：从采集 → 整理 → 验证 → 生效的完整闭环
- 多租户隔离：User / Tenant / Global 三层作用域

### 1.2 非目标

- 不做多变体竞争（Skill Arena）——用户自己会判断
- 不做模型训练固化（Coach / Adapter Registry）——等未来 AReaL
- 不做 LLM API Proxy——LiteLLM 已经做得很好
- 不重写 CortexFS / HierarchicalRetriever / IntentRouter 核心检索链路
- MemoryOrchestrator 保留为顶层协调器，渐进接入新模块；被替代的子模块（Skillbook、SessionManager）通过双写过渡期安全切换（见第 12 节）

---

## 2. 目标平台与部署模型

### 2.1 目标平台

| 平台 | 类型 | 接入方式 |
|------|------|----------|
| **Claude Code** | 封闭运行时 | Hooks + MCP 工具 |
| **Codex** | 封闭运行时 | 插件 / MCP 接口 |
| **Chat 工具** | 大部分封闭 | 插件（视工具支持情况） |
| **Agno 等 Agent 框架** | 开放框架 | SDK 自动埋点 |

### 2.2 部署模型

- **服务形态**：Python 云端服务（FastAPI）
- **用户规模**：团队起步，可扩展至 C 端规模
- **向量存储**：Qdrant（嵌入式或独立部署）
- **横向扩容**：无状态 HTTP 服务 + 独立 worker 集群

---

## 3. 总体架构

### 3.1 优先级分层调度模型

**不采用白昼/黑夜双节律**——云端服务 + C 端规模下无统一时间窗口。

改为 **B+C 模型：优先级分层 + Tenant 可配节律**。

```text
平台层（优先级队列）：
  P0 实时    ← 搜索、知识查找（同步 HTTP 响应）
  P1 近实时  ← prompt/response 记录（异步写入，客户端防抖缓冲）
  P2 异步    ← Archivist 整理、Sandbox 验证（后台 worker）
  P3 低优    ← 预留给未来训练任务

Tenant 层（可配策略）：
  每个 Tenant 配置 P2 的触发频率：
    ├── 立即（高活跃）
    ├── 每小时（默认）
    └── 每天（低频 / 省成本）
```

**P2/P3 异步任务技术选型**：
- 初期：FastAPI BackgroundTasks（零额外依赖，适合单实例）
- 扩容期：迁移至 Celery + Redis（支持多 worker 分布式调度）
- P2 任务需要幂等设计，支持失败重试

### 3.2 模块总览

**新增模块（4 个）：**

| 模块 | 职责 |
|------|------|
| **Observer** | 每次 prompt/response 实时记录到服务端 |
| **Trace Splitter** | session_end 时 LLM 拆分 transcript 为多条 trace |
| **Archivist** | 定期聚类 trace + LLM 提炼 Belief/SOP/Negative Rule |
| **Sandbox Evaluator** | 统计门 + LLM 模拟验证 + 人工确认 |

**新增存储（2 个）：**

| 存储 | 说明 |
|------|------|
| **Trace Store** | trace 数据，三层 FS，可检索 |
| **Knowledge Store** | Belief/SOP/Negative Rule/Root Cause，单集合 + type 过滤 |

**保留模块：**

| 模块 | 变化 |
|------|------|
| MemoryOrchestrator | 接入新模块，调整 API |
| CortexFS | 不变，Trace 和 Knowledge 都复用三层架构 |
| HierarchicalRetriever | 不变 |
| IntentRouter | 不变 |
| QdrantStorageAdapter | 新增 trace / knowledge 集合 |

### 3.3 完整生命周期

```text
Agent 执行任务
  → Observer 每次 prompt/response 实时记录到服务端（P1）
  → Session End 时 Trace Splitter 按任务拆分为多条 trace（三层 FS）
  → Archivist 定期聚类 trace + LLM 提炼知识（P2）
  → Sandbox 统计门 + LLM 模拟验证
  → 人工确认 → 写入 Knowledge Store
  → Agent 下次搜索时 L0 精准命中（P0，极低 token）
  → 用户反馈好/不好 → 知识质量持续优化
```

---

## 4. 决策记录

以下为头脑风暴评审中确认的所有设计决策：

| # | 决策 | 理由 |
|---|------|------|
| D1 | User SOP/Belief 严格隔离，永不自动影响其他用户 | 避免个人偏好污染他人体验 |
| D2 | Tenant → Global 晋升必须人工审批 | 防止低质量知识扩散 |
| D3 | Observer：Hooks + Session API 为主，SDK 为辅，不做 Proxy | 封闭工具（Claude Code/Codex）无法注入 SDK |
| D4 | 失败 trace 三级筛选：丢弃噪声 / 降级存摘要 / 完整存高价值 | 控制存储成本 |
| D5 | 统一 Trace Schema，细粒度字段全部选填 | Archivist 按可用字段自适应处理 |
| D6 | Archivist 默认每 N 条 trace 自动触发，Tenant 可配，支持手动触发 | 灵活适配不同用户节律 |
| D7 | Archivist 全新模块，废弃 RuleExtractor | 输入数据格式和产出物类型根本不同 |
| D8 | 新建 Knowledge Store，废弃 Skillbook | Archivist 产出 4 种知识类型，Skillbook 只管 1 种 |
| D9 | Knowledge Store 单集合 + knowledge_type 过滤 | 物理一个集合，逻辑四个视图；检索时按场景指定 type |
| D10 | Archivist 全量 LLM，先聚类再提炼控制成本 | 编程 trace 是自然语言混合体，NLP 规则写不全 |
| D11 | Sandbox 两阶段：统计门 + LLM 模拟验证 | 先零成本淘汰，再精准验证 |
| D12 | 验证阈值全部 Tenant 级可配 | 不同团队对质量要求不同 |
| D13 | 砍掉 Skill Arena | 用户自己判断，不需要系统做多变体竞争 |
| D14 | Coach / Adapter Registry 延后至 AReaL 阶段 | 当前目标平台大部分不支持微调 |
| D15 | Knowledge Store 预留 training_ready 字段 | 未来 AReaL 可直接消费 |
| D16 | Phase 1 包含 Observer + Archivist + Knowledge Store + Sandbox | 都是记忆生命周期的核心环节 |
| D17 | Knowledge Store 产出物接入 CortexFS 三层架构 | L0 极低 token 预览，L1/L2 按需展开 |
| D18 | LLM 成本：用户可配自己的 Key，平台 Key 加预算上限 | 暂不深入设计 |
| D19 | Sandbox 验证结果需人工确认才能生效 | 防止 LLM 幻觉产生错误知识 |
| D20 | Skillbook → Knowledge Store 一次性迁移 | 不做兼容期 |
| D21 | Trace 走 CortexFS 三层架构，支持检索 | trace 本身也是有检索价值的记忆 |
| D22 | Session End 按任务相关性 LLM 拆分；超长时滑动窗口增量归并 | 一个 session 可能包含多个独立任务 |
| D23 | 每次 prompt/response 通过 hooks 实时记录到服务端 | 防止 session 中途崩溃丢数据 |
| D24 | Trace 拆分替代 MemoryExtractor，废弃 SessionManager | 不需要两套 session_end 处理逻辑 |

---

## 5. 新增模块设计

### 5.1 Observer

**职责**：每次 prompt/response 发生时实时记录到服务端。

**接入模式**：

| 平台类型 | 接入方式 | 数据粒度 |
|----------|----------|----------|
| 封闭工具（Claude Code / Codex） | Hooks 被动捕获 + session API | prompt + final_text + MCP 工具调用 |
| 开放框架（Agno 等） | SDK 自动埋点 | 完整 prompt/thought/tool_call/observation |

**实时记录链路**：

```text
用户发 prompt
  → user-prompt-submit hook 触发
  → 调 session_message(role="user", content=...)

Agent 回复
  → stop hook 触发
  → 调 session_message(role="assistant", content=...)

服务端累积完整 transcript，不依赖 session_end 一次性推送
```

**客户端防抖缓冲**：
高频对话场景下，每次消息都实时调用 API 会造成碎片化 I/O。客户端应实现缓冲机制：
- 每 3-5 秒或每 3 条消息合并上报一次
- session_end 时强制 flush 剩余缓冲
- 服务端接口支持批量 `session_messages()` 写入

### 5.2 Trace Splitter

**职责**：session_end 时将完整 transcript 按任务相关性拆分为多条 trace。

**流程**：

```text
Session End 触发
  ├── 加载服务端已有的完整 transcript
  │
  ├── 检查 token 量 vs 配置的模型上下文大小
  │   ├── 未超出 → 整体交给 LLM 一次性分析
  │   └── 即将超出 → 滑动窗口增量抽取归并
  │
  ├── LLM 分析："这个 session 里做了几件事？"
  │   → 输出 N 组，每组是一个独立任务
  │
  └── 每组生成一条 trace：
      ├── L0：一句话摘要（如"修复了 Python import error"）
      ├── L1：关键步骤摘要
      └── L2：对应的原始对话片段
```

**超长 session 处理**：

```text
假设模型上下文 128K，session 有 200K：
  前 120K 先做一轮抽取 → 产出 N 条 trace 的 L0/L1
  剩余 80K + 前面 N 条 L0（作为上下文衔接）
  第二轮抽取 → 产出 M 条 trace
  最后一轮去重合并（相同任务的 trace 合并）
```

### 5.3 Archivist

**职责**：定期把 trace 整理成高质量结构化知识。

**触发策略**：
- 默认：每积累 N 条新 trace 自动触发（N 可配，默认 20）
- Tenant 可配：改为每小时 / 每天 / 每 N 条
- 支持管理员或用户手动触发
- 执行优先级：P2

**处理流程**：

```text
新增 trace
  → embedding 聚类（零 LLM 成本，相似度 > 0.8 归为一簇）
      第一级：按 source + task_type 粗分
      第二级：按 prompt 语义相似度聚类
  → 每个簇调一次 LLM 提炼（控制 LLM 调用次数）
  → 产出：
      ├── Belief（判断规则）
      ├── SOP（标准操作流程）
      ├── Negative Rule（禁止做什么）
      └── Root Cause（失败归因）
  → 全部进入 Sandbox 验证队列
```

**Root Cause Analysis（全量 LLM）**：
- 编程 trace 是自然语言 + 代码 + 工具调用的混合体，NLP 规则无法覆盖
- 通过先聚类再分析控制成本（100 条 trace → 8 个簇 → 8 次 LLM 调用）

**名词解释**：

| 术语 | 含义 | 例子 |
|------|------|------|
| **Belief** | Agent 积累的判断规则 | "处理 import error 时，先查拼写再装包，成功率更高" |
| **SOP** | 有步骤的标准操作流程 | "1. 查报错 2. 判断拼写还是缺包 3. 对应修复 4. 验证" |
| **Negative Rule** | 已验证的禁止规则 | "不要无脑 pip install，先确认是不是拼写问题" |
| **Root Cause** | 失败归因 | "这类 import error 通常因为虚拟环境未激活" |

### 5.4 Sandbox Evaluator

**职责**：验证 Archivist 产出的候选知识是否真的管用。

**两阶段验证**：

```text
阶段 1 统计门（零成本，快速淘汰）：
  ├── 来源 trace 数量 ≥ min_traces？
  ├── 成功 trace 占比 ≥ min_success_rate？
  ├── 来源用户多样性？
  └── 不通过 → 打回，等积累更多 trace

阶段 2 LLM 模拟验证（只有统计通过的才走）：
  ├── 取 K 条历史 trace
  ├── LLM 推理："如果用了这条 SOP，结果会更好吗？"
  ├── pass_rate ≥ 阈值 → 通过
  └── 不通过 → 标记为待改进，附上失败原因

阶段 3 人工确认或自动生效：
  ├── scope = tenant/global → 人工确认（必须）→ status = active
  └── scope = user 且 confidence ≥ 0.95 → 自动 status = active（免人工）
```

**状态语义明确定义**：
- `candidate`：Archivist 产出，等待验证
- `verified`：Sandbox 统计门 + LLM 验证通过，等待人工确认（**不可被检索**）
- `active`：人工确认（或 User 层自动生效）后，**可被检索**
- `deprecated`：失效 / 被替代（**不可被检索**）

**默认阈值（Tenant 级可配）**：

```yaml
sandbox:
  stat_gate:
    min_traces: 3
    min_success_rate: 0.7
    min_source_users: 2           # Tenant/Global 层
    min_source_users_private: 1   # User 层
  llm_verify:
    sample_size: 5
    min_pass_rate: 0.6
```

---

## 6. 存储设计

### 6.1 Trace Store

Trace 走 CortexFS 三层架构，存入 Qdrant，支持检索。

| 层级 | 内容 | 用途 |
|------|------|------|
| **L0** | 一句话描述（如"修复了 Python import error"） | 搜索结果预览，极低 token |
| **L1** | 关键步骤摘要（查了什么、调了什么工具、结果如何） | 展开查看 |
| **L2** | 完整对话原文 | 审计、Archivist 分析 |

L0/L1 由 Trace Splitter 在 session_end 时 LLM 自动生成。

### 6.2 Knowledge Store

单集合 + `knowledge_type` 字段过滤，替代原 Skillbook。

四种知识类型共用一个 Qdrant 集合，搜索时按场景指定 type：

| 场景 | 查询 types |
|------|-----------|
| Agent 执行任务时查知识 | `["sop", "negative_rule", "root_cause"]` |
| Archivist 整理时查历史 | `["root_cause", "belief"]` |
| 用户手动查 | 全部 |

**注意**：Agent 执行时也查 `root_cause`——debug 场景下"这类错误通常因为..."是高价值命中。

所有知识类型均接入 CortexFS 三层架构：
- L0 存入 Qdrant payload → 搜索时零文件 I/O
- L1/L2 按需加载

### 6.3 失败 Trace 存储策略

三级筛选，控制存储成本：

| 级别 | 条件 | 处理方式 |
|------|------|----------|
| **元数据** | 网络超时、API 限流、用户取消 | 仅存元数据（trace_id/session_id/outcome/error_code/created_at），不存内容 |
| **降级** | 原因不明、低频任务类型、重复错误 ≥10 条 | 只存 L0 摘要 |
| **完整** | 有明确 root cause、高频任务、用户负反馈 | 三层完整存储 |

**证据链保护规则**：凡是被候选知识的 `source_trace_ids` / `evidence_trace_ids` 引用的 trace，**最低保留到 L1 层级**，即使该 trace 原本按策略只保留元数据或 L0。Sandbox 验证和人工审批需要足够上下文。

**注意**：网络超时不完全丢弃（改为存元数据），以便分析由 OpenCortex 服务自身响应慢引发的 Agent 故障。

---

## 7. 数据模型

### 7.1 Trace Schema（统一最大集）

所有平台共用一套 schema，细粒度字段全部选填。Archivist 按"有什么用什么"自适应处理。

```text
Trace
├── trace_id            ← 必填
├── session_id          ← 必填
├── tenant_id           ← 必填
├── user_id             ← 必填
├── source              ← 必填（"claude_code" / "codex" / "agno" / ...）
├── source_version      ← 选填（平台版本号，如 "claude_code@1.2.3"）
├── task_type           ← 选填（"coding" / "chat" / "debug" / ...）
│
├── turns[]             ← 至少 1 条
│   ├── turn_id         ← 必填
│   ├── prompt_text     ← 选填（用户输入；timeout/cancelled 时可能为空）
│   ├── thought_text    ← 选填（封闭工具拿不到）
│   ├── tool_calls[]    ← 选填
│   │   ├── tool_name
│   │   ├── tool_args
│   │   └── tool_result
│   ├── final_text      ← 选填（最终输出；中断/超时时可能为空）
│   ├── turn_status     ← 选填（"complete" / "interrupted" / "timeout"）
│   ├── latency_ms      ← 选填
│   └── token_count     ← 选填
│
├── outcome             ← 选填（success / failure / timeout / cancelled）
├── error_code          ← 选填
├── cost_meta           ← 选填
├── training_ready      ← 选填（预留给未来 AReaL）
└── created_at          ← 必填
```

**Schema 设计说明**：
- `prompt_text` 和 `final_text` 均为**选填**：超时、用户中断、仅工具调用未产出最终答复的 turn 不强制要求这两个字段。Archivist 遇到缺失字段时跳过该 turn。
- `turn_status` 标记每个 turn 的完成状态，避免后续处理需要猜测 turn 是否完整。
- `source_version` 用于 Archivist 选择正确的 transcript 解析器（不同版本的 Claude Code 输出格式可能不同）。

### 7.2 Knowledge Types

#### Belief（判断规则）

| 字段 | 必填 | 说明 |
|------|:---:|------|
| `knowledge_type` | ✅ | 固定值 `"belief"` |
| `statement` | ✅ | 规则表达 |
| `objective` | ✅ | 适用目标 |
| `scope` | ✅ | user / tenant / global |
| `preconditions` | | 生效前置条件 |
| `evidence_trace_ids` | | 证据来源 trace |
| `counter_examples` | | 反例 |
| `confidence` | | 置信度 |
| `status` | ✅ | draft / candidate / verified / active / deprecated |

#### SOP（标准操作流程）

| 字段 | 必填 | 说明 |
|------|:---:|------|
| `knowledge_type` | ✅ | 固定值 `"sop"` |
| `objective` | ✅ | 任务目标 |
| `preconditions` | | 前置条件 |
| `action_steps` | ✅ | 标准步骤 |
| `trigger_keywords` | | 触发关键词列表（检索时可做 O(1) 初步过滤） |
| `anti_patterns` | | 禁止路径 |
| `success_criteria` | | 成功判据 |
| `failure_signals` | | 失败信号 |
| `source_trace_ids` | | 来源 trace |
| `confidence` | | 稳定度 |
| `status` | ✅ | 生命周期状态 |

#### Negative Rule（禁止规则）

| 字段 | 必填 | 说明 |
|------|:---:|------|
| `knowledge_type` | ✅ | 固定值 `"negative_rule"` |
| `statement` | ✅ | "不要做 X，因为 Y" |
| `context` | | 适用场景 |
| `source_trace_ids` | | 来自哪些失败 trace |
| `severity` | | 严重程度 |
| `status` | ✅ | 生命周期状态 |

#### Root Cause（失败归因）

| 字段 | 必填 | 说明 |
|------|:---:|------|
| `knowledge_type` | ✅ | 固定值 `"root_cause"` |
| `error_pattern` | ✅ | 错误模式 |
| `cause` | ✅ | 根因分析 |
| `fix_suggestion` | | 修复建议 |
| `source_trace_ids` | | 来源 trace |
| `frequency` | | 出现频次 |
| `status` | ✅ | 生命周期状态 |

---

## 8. 知识作用域与治理

### 8.1 三层作用域

```text
Layer 1 Global（全局，所有 tenant 可见）：
  → 只有高置信度 + 多 tenant 验证才能修改
  → 改动需要人工审批

Layer 2 Tenant（团队级，覆盖全局）：
  → 团队可以 fork 全局知识，做团队定制
  → 不影响其他 tenant

Layer 3 User（个人级，优先级最高）：
  → 严格隔离，永不自动影响其他用户
  → 只影响自己
```

**查找优先级**：User → Tenant → Global

### 8.2 晋升流程

```text
User 层知识 → 不可自动晋升
  → 可作为"参考证据"提交审批
  → 人工审批通过后 → 写入 Tenant/Global 层
```

### 8.3 入库审批流程

```text
Archivist 产出候选知识
  → Sandbox 统计门（自动）
  → Sandbox LLM 验证（自动）
  → 人工确认（必须）
  → 写入 Knowledge Store
```

### 8.4 知识状态机

```text
candidate（Archivist 产出）
  → verified（Sandbox 统计门 + LLM 验证通过）
  → active（人工确认，或 User 层自动生效）
  → deprecated（失效 / 被替代）
```

**检索可见性规则**：
| 状态 | 能否被 Agent 检索到 | 说明 |
|------|:---:|------|
| `candidate` | ❌ | 等待 Sandbox 验证 |
| `verified` | ❌ | 等待人工确认 |
| `active` | ✅ | 唯一可检索状态 |
| `deprecated` | ❌ | 已失效 |

---

## 9. 核心流程

### 9.1 实时记录流程（P1）

```text
用户发 prompt
  → user-prompt-submit hook
  → session_message(role="user", content=...)
  → 服务端存储

Agent 回复
  → stop hook
  → session_message(role="assistant", content=...)
  → 服务端存储

结果：服务端累积完整 transcript，不依赖 session_end 推送
```

### 9.2 Session End Trace 拆分流程

```text
session_end 触发
  → 加载服务端完整 transcript
  → 检查 token 量 vs 模型上下文
  → LLM 按任务相关性拆分为 N 条 trace
  → 每条 trace 生成 L0/L1/L2
  → 写入 Trace Store（Qdrant + CortexFS）
```

### 9.3 Archivist 整理流程（P2）

```text
触发条件：N 条新 trace / Tenant 配置 / 手动触发
  → embedding 聚类（source + task_type 粗分 → 语义相似度精分）
  → 每簇调一次 LLM 提炼
  → 产出 Belief / SOP / Negative Rule / Root Cause
  → 进入 Sandbox 验证队列
```

### 9.4 Sandbox 验证流程

```text
候选知识进入队列
  → 阶段 1：统计门（零成本淘汰）
  → 阶段 2：LLM 模拟验证（抽样历史 trace 做反事实推理）
  → 阶段 3：人工确认
  → Tenant/Global 层通过 → 写入 Knowledge Store（status=active）
  → User 层且 confidence ≥ 0.95 → 自动 status=active
  → 不通过 → 标记待改进 / 打回
```

### 9.5 知识检索流程（P0）

```text
Agent 搜索 "怎么处理 import error"
  → IntentRouter 路由
  → HierarchicalRetriever 检索 Knowledge Store
      type_filter: ["sop", "negative_rule"]
  → 返回 L0（极少 token）：
      SOP: "4 步处理 import error"
      Negative Rule: "不要无脑 pip install"
  → Agent 需要细节 → 展开 L1
  → 极少情况 → 加载 L2
```

---

## 10. 算法设计

### 10.1 Trace 聚类

```text
第一级：按 source + task_type 粗分
  → "claude_code + debug" 一组
  → "agno + coding" 一组

第二级：按 prompt 语义相似度聚类
  → 复用 Qdrant embedding 能力
  → 相似度 > 0.8 归为一簇
```

### 10.2 超长 Session 增量归并

```text
模型上下文 = C，session 长度 = L

if L ≤ C:
  整体一次性 LLM 分析

if L > C:
  窗口 1：前 0.9C 做第一轮抽取 → N 条 trace 的 L0/L1
  窗口 2：剩余内容 + 前 N 条 L0（上下文衔接）→ M 条 trace
  ...
  最后：跨窗口去重合并（相同任务的 trace 合并）
```

### 10.3 检索评分融合（保留现有）

```text
final = 0.7 × rerank_score + 0.3 × retrieval_score + 0.05 × reward_score
```

---

## 10.5 性能优化设计

### 瓶颈分析

当前 store/search 链路 90% 时间花在外部 API 调用（embedding、LLM、rerank），不在 Python 计算。
Rust 重写无法解决 I/O 瓶颈。

```text
memory_store 耗时拆解：
  HTTP 接收          ~1ms       ← 不是瓶颈
  embedding 生成     ~200-800ms ← 主要瓶颈（远程 API）
  Qdrant upsert     ~5-20ms    ← 很快
  CortexFS 写文件   ~2-5ms     ← 很快

memory_search 耗时拆解：
  IntentRouter       ~1-500ms   ← LLM 层 500ms，关键词层 <1ms
  embedding 生成     ~200-800ms ← 主要瓶颈
  Qdrant search     ~5-30ms    ← 很快
  Rerank（可选）    ~100-500ms  ← 远程 API
```

### 优化方案

#### Phase 1（立竿见影）

**P1-1 本地 Embedding 模型**

用本地 ONNX 模型替代远程 API 调用，延迟从 200-800ms 降至 10-30ms。

| 推荐模型 | 维度 | 量化大小 | CPU 延迟 | 理由 |
|----------|:---:|:---:|:---:|------|
| **BGE-M3** | 1024 | ~200MB | 10-30ms | 中英混合最佳；支持 dense+sparse；1024 维兼容现有集合 |

技术实现：
- 使用 FastEmbed（Qdrant 官方 ONNX 推理库，零 GPU 依赖）
- 新增 `embedding_provider: "local"` 配置项
- 回退：配置远程 API 作为 fallback（本地模型加载失败时）

**P1-2 Embedding 缓存**

```text
LRU 缓存策略：
  key = hash(query_text)
  value = embedding vector
  size = 10000 条（约 40MB 内存，1024 维 float32）
  TTL = 1 小时

预期效果：
  同一用户短期内相似查询命中率 > 60%
  命中时延迟 = 0ms
```

**P1-3 Qdrant 独立部署**

嵌入式 Qdrant 在高并发下有 GIL + 锁争用问题。C 端规模必须切到独立部署：
- Docker 独立进程
- gRPC 连接（比 HTTP 快 2-3x）
- 支持并发读写

#### Phase 2（进一步优化）

**P2-1 连接池优化**

httpx 连接复用，避免每次请求都建 TCP 连接。

**P2-2 批量写入合并**

Observer 的 prompt/response 记录攒一批（3-5 条或 3 秒间隔）再批量写入 Qdrant。

### 优化前后对比

| 链路 | 优化前 | 优化后（Phase 1） |
|------|:---:|:---:|
| memory_store | ~200-850ms | **~20-60ms** |
| memory_search（无 rerank） | ~200-850ms | **~20-60ms** |
| memory_search（有 rerank） | ~500-1800ms | ~120-560ms |

**不需要 Rust 重写**——本地 embedding + 缓存已经能把核心链路压到 50ms 以内。

---

## 11. 配置设计

### 11.1 服务端新增配置

```yaml
# Cortex Alpha 配置（server.json）
cortex_alpha:
  observer:
    enabled: true

  trace_splitter:
    enabled: true
    max_context_tokens: 128000       # 模型上下文大小

  archivist:
    enabled: true
    trigger_mode: "auto"             # auto / manual
    trigger_threshold: 20            # 每 N 条 trace 触发
    max_delay_hours: 24              # 最大延迟（低频用户攒不够 N 条也触发）
    llm_model: ""                    # 用于整理的 LLM

  sandbox:
    stat_gate:
      min_traces: 3
      min_success_rate: 0.7
      min_source_users: 2
      min_source_users_private: 1
    llm_verify:
      sample_size: 5
      min_pass_rate: 0.6
    require_human_approval: true

  knowledge_store:
    collection_name: "knowledge"
```

### 11.2 Tenant 级可覆盖配置

```yaml
# Tenant 可覆盖以下字段（在 cortex_alpha.* 命名空间下）
cortex_alpha:
  archivist:
    trigger_mode: "auto"
    trigger_threshold: 10          # 更频繁触发
    max_delay_hours: 12

  sandbox:
    stat_gate:
      min_traces: 5                # 更严格
      min_success_rate: 0.8
```

---

## 12. 模块废弃与迁移

### 12.1 废弃清单

| 废弃模块 | 替代为 | 迁移方式 |
|----------|--------|----------|
| **RuleExtractor** | Archivist | 废弃，不迁移数据 |
| **Skillbook** | Knowledge Store | 数据迁移 + 双写过渡期 |
| **skill_evolve()** | Sandbox Evaluator | 废弃 API |
| **SessionManager + MemoryExtractor** | Observer + Trace Splitter | 双写过渡期 |
| **Skill Arena** | 不做 | — |
| **Coach / Adapter Registry** | 延后至 AReaL | — |

### 12.2 双写过渡方案

**问题背景**：当前 `MemoryOrchestrator.__init__` 强依赖 Skillbook 和 SessionManager 初始化；插件链路直接调用 `/api/v1/session/end` 做会话提取。如果一次性砍掉，现有测试和客户端全部崩溃。

**过渡策略（三步切流）**：

```text
Step 1 — 双写期（新旧并存）：
  ├── 新增 Observer + Trace Splitter + Knowledge Store
  ├── 旧 SessionManager / Skillbook 保持运行
  ├── session_end 同时走旧链路（MemoryExtractor）和新链路（Trace Splitter）
  ├── Skillbook 查询同时查 Knowledge Store，结果合并
  └── 持续时间：直到新链路通过回归测试

Step 2 — 切流期（新链路为主）：
  ├── 默认走新链路
  ├── 旧链路降级为 fallback（配置开关控制）
  ├── 旧 API（/session/end、/skill/lookup）映射到新实现
  └── 插件侧不需要改动

Step 3 — 清理期（移除旧代码）：
  ├── 删除 SessionManager、MemoryExtractor、Skillbook、RuleExtractor
  ├── 删除 skillbooks Qdrant 集合
  └── 插件侧切换到新 API endpoint
```

### 12.3 Skillbook 数据迁移

```text
读取 skillbooks 集合全量数据
  → 每条 skill 映射为 Knowledge Store 的 Belief 或 SOP
      判断规则型 skill → Belief
      步骤型 skill → SOP
  → 迁移 scope、owner、confidence 等字段
  → 写入 knowledge 集合，status = active（已验证的历史数据）
  → 迁移前自动备份 skillbooks 集合
```

---

## 13. 推荐 API 扩展

### 13.1 Trace 相关

| 接口 | 说明 |
|------|------|
| `POST /api/v1/trace/split` | 手动触发 trace 拆分 |
| `GET /api/v1/trace/list` | 列出用户的 trace |
| `GET /api/v1/trace/detail` | 获取 trace L1/L2 |

### 13.2 Knowledge 相关

| 接口 | 说明 |
|------|------|
| `POST /api/v1/knowledge/search` | 搜索知识（带 type 过滤） |
| `GET /api/v1/knowledge/list` | 列出知识条目 |
| `POST /api/v1/knowledge/approve` | 人工审批候选知识 |
| `POST /api/v1/knowledge/reject` | 拒绝候选知识 |
| `POST /api/v1/knowledge/promote` | 晋升知识作用域 |

### 13.3 Archivist 相关

| 接口 | 说明 |
|------|------|
| `POST /api/v1/archivist/trigger` | 手动触发整理 |
| `GET /api/v1/archivist/status` | 整理任务状态 |
| `GET /api/v1/archivist/candidates` | 查看待审批候选知识 |

---

## 14. 测试计划

### 14.1 新增测试面

- Observer 实时记录完整性测试
- Trace Splitter 任务拆分准确性测试
- 超长 session 滑动窗口归并测试
- Archivist 聚类质量测试
- Knowledge Store CRUD + type 过滤测试
- Sandbox 统计门阈值测试
- Sandbox LLM 验证稳定性测试
- 知识作用域隔离测试
- Skillbook → Knowledge Store 迁移测试

### 14.2 分层测试

1. **单元测试**：Trace Schema 验证、聚类逻辑、统计门计算
2. **集成测试**：Observer → Trace Splitter → Archivist → Knowledge Store 全链路
3. **回归测试**：现有 search / feedback / memory_store 不退化
4. **并发压力测试**：Observer 高频写入、多 session 并发、P2 worker 负载
5. **长连接稳定性测试**：Observer 引入的持久 session 状态，验证内存泄漏和连接超时
6. **双写过渡期测试**：新旧链路并行时数据一致性验证

---

## 15. 风险与控制

| 风险 | 控制策略 |
|------|----------|
| Archivist 全量 LLM 成本爆炸 | 先聚类再提炼（100 条→8 次 LLM）；用户可配自己的 Key；平台 Key 加预算上限 |
| Sandbox LLM 验证不可靠 | 统计门先过滤；LLM 验证后仍需人工确认 |
| Skillbook 一次性迁移丢数据 | 迁移前备份；迁移后验证 |
| 封闭工具 trace 粒度粗 | 统一 schema 选填字段；Archivist 自适应处理 |
| 超长 session 拆分质量 | 滑动窗口 + 跨窗口去重合并 |

---

## 16. 未来方向

以下能力不在 Phase 1 范围内，预留接口但不实现：

| 方向 | 说明 | 预留 |
|------|------|------|
| **AReaL 训练固化** | 把高质量知识训练成 LoRA 权重 | `training_ready` 字段 |
| **Skill Arena** | 多变体竞争选优 | 不预留 |
| **Adapter Registry** | LoRA 版本管理和运行时路由 | 不预留 |
| **Graph Query** | 利用 `.relations.json` 做多跳查询 | CortexFS 关系已有 |
| **Event JSONL 镜像** | 可观测性增强 | Observer 可扩展 |

---

*Cortex Alpha 设计文档 — 经头脑风暴评审后更新*
