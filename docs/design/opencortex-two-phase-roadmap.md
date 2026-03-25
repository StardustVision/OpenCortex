# OpenCortex 两阶段演进路线与架构判断

> 状态：Draft  
> 日期：2026-03-14  
> 范围：基于当前仓库代码、文档与测试的架构评审结果

## 1. 文档目的

本文档给出 OpenCortex 的两阶段演进路线，并明确当前系统应优先投入的方向。

核心判断：

- Phase 1 的任务不是“让系统看起来更聪明”，而是把记忆基建做扎实。
- Phase 2 的任务不是继续堆检索技巧，而是利用稳定基建做结构化学习。
- 如果在 Phase 1 未稳定前提前推进自动画像、自动 Skill 学习，系统会把噪声结构化，越学越乱。

## 2. 结论摘要

OpenCortex 当前最强的部分是：

- 可解释的记忆存储结构：`URI + CortexFS + Qdrant`
- 工程化检索链路：`IntentRouter + HierarchicalRetriever + hybrid retrieval + rerank + RL/hotness`
- 生命周期协议：`prepare / commit / end`
- 多租户、多用户、项目级别的边界隔离

OpenCortex 当前最不成熟的部分是：

- 自动知识闭环的工程落地完整度
- 结构化知识治理
- 自动用户画像
- 自动 Skill 提炼与演化

因此，系统演进必须分为两个阶段：

| Phase | 核心目标 | 成功标准 |
|---|---|---|
| Phase 1 | 做好基建，做好存储与召回，准确率高 | 记得准、找得回、排得对、边界清 |
| Phase 2 | 利用稳定基建做结构化学习 | 能可靠提炼用户画像与 Skill，且不引入系统性污染 |

## 3. 为什么必须拆成两个 Phase

记忆系统的上层智能，本质上依赖下层检索质量。

如果 Phase 1 不稳定，会直接导致以下问题：

- 写入不准：错误内容、重复内容、噪声内容进入长期记忆
- 检索不准：真正需要的记忆召不回，次优或错误记忆排在前面
- 解释不清：无法定位命中来源，无法做归因和调优
- 边界不清：不同 tenant、user、project、memory type 的内容互相污染

在这种情况下做 Phase 2，会把低质量样本“提炼”为更高层的画像或 Skill，后果比错召回更严重，因为错误会被升级为规则。

所以正确顺序不是：

1. 先做自动学习
2. 再修召回

而是：

1. 先把记忆底座做成可靠基础设施
2. 再把高质量样本变成结构化知识

## 4. 当前系统的阶段定位

从现有代码看，OpenCortex 更接近一个“Phase 1.5”的系统：

- Phase 1 方向基本正确，且已有明显优势
- Phase 2 的模块雏形已经存在
- 但 Phase 2 的关键闭环还没有工程化跑通

### 4.1 已具备的 Phase 1 能力

- 统一编排层：`src/opencortex/orchestrator.py`
- 可解释三层存储：`src/opencortex/storage/cortex_fs.py`
- 向量与过滤存储：`src/opencortex/storage/qdrant/adapter.py`
- 意图路由：`src/opencortex/retrieve/intent_router.py`
- 层级检索：`src/opencortex/retrieve/hierarchical_retriever.py`
- 上下文协议：`src/opencortex/context/manager.py`
- 多租户与项目隔离：`src/opencortex/http/request_context.py`

### 4.2 已出现但尚未成熟的 Phase 2 能力

- Observer：`src/opencortex/alpha/observer.py`
- TraceSplitter：`src/opencortex/alpha/trace_splitter.py`
- Archivist：`src/opencortex/alpha/archivist.py`
- Sandbox：`src/opencortex/alpha/sandbox.py`
- TraceStore：`src/opencortex/alpha/trace_store.py`
- KnowledgeStore：`src/opencortex/alpha/knowledge_store.py`

### 4.3 关键判断

当前系统最应该做的不是“扩写 Alpha”，而是“收缩 Alpha 预期，优先夯实 Phase 1”。

## 5. Phase 1：记忆基建期

### 5.1 目标定义

Phase 1 的唯一目标是：

`存得准，找得回，排得对，边界清`

这里的“准”不是泛泛而谈，而是指以下四件事同时成立：

- 写入语义正确
- 检索召回正确
- 排序优先级正确
- 访问边界正确

### 5.2 Phase 1 不做什么

Phase 1 不应该以“自动提炼用户画像”作为主目标。

Phase 1 不应该以“自动学习 Skill”作为主目标。

Phase 1 不应该以“知识演化闭环”作为主目标。

这些能力可以有代码雏形，但不能成为当前阶段的交付承诺。

### 5.3 Phase 1 的核心工作流

#### 5.3.1 写入链路

目标：

- 进入系统的内容是可控的
- 同一事实不会被无序重复写入
- 不同类型内容会进入正确的记忆槽位

现有代码基础：

- `add()` 支持 ingest mode 判定、层级摘要派生、embedding、dedup、upsert、CortexFS 落盘
- `MERGEABLE_CATEGORIES` 已经表达了“哪些类目应该合并，哪些类目应该保留为事件”

Phase 1 应补强的点：

- memory type 进一步明确：`profile / preferences / entities / events / resources`
- write-time dedup 策略进一步稳定
- 写入源可信度建模，至少区分：
  - 用户明确表达
  - 系统推断
  - 文档导入
  - 对话即时事件

#### 5.3.2 检索链路

目标：

- 真正相关的记忆能被召回
- 专有名词、配置名、路径名、错误码不会被语义检索吃掉
- 排序能反映“相关性 + 使用价值”，不是只看 embedding 分数

现有代码基础：

- 硬关键词检测
- dense+sparse 混检
- lexical fallback
- 双路 RRF 融合
- 条件 rerank
- RL reward
- hotness
- frontier batching

Phase 1 应补强的点：

- 真正打通 `time_scope`，不能只停留在 intent 字段
- 统一 recall 配置透传，保证 `category/context_type/include_knowledge` 真正生效
- 针对专有名词加强 sparse 路径，后续可考虑引入 BM25/SPLADE

#### 5.3.3 可解释性链路

目标：

- 开发者能看清为什么命中
- 开发者能看清命中了哪类 memory、哪个 URI、哪层内容
- 能定位 recall 错误发生在路由、召回、融合还是 rerank

现有代码基础：

- URI 目录结构
- CortexFS 三层内容
- 检索路径有明确模块分层

Phase 1 应补强的点：

- 补全 retrieval trace/debug 输出
- 将 `search_intent`、typed queries、起点目录、融合分数暴露为可诊断结果

#### 5.3.4 生命周期可靠性

目标：

- prepare / commit / end 行为一致
- 崩溃、重启、超时不会造成关键 turn 丢失
- transcript 和 memory 之间的数据边界清楚

现有问题：

- `Observer` 目前是内存缓冲，不是 crash-safe
- Context 协议参数透传不完整
- Alpha 部分调用链仍有实现断点

Phase 1 应补强的点：

- 将 transcript 批量写入改为更可靠的持久化或准持久化方案
- 统一协议字段与服务端模型
- 明确“即时事件写入”和“长期记忆写入”的边界

### 5.4 Phase 1 交付清单

建议将 Phase 1 切成三段。

#### Phase 1A：可靠性修正

- 修复 Context 协议参数透传
- 修复 Alpha 相关的 filter DSL / storage API 调用不一致
- 修复 KnowledgeStore 和 TraceStore 的文件写入/筛选断点
- 建立最基本的 recall benchmark

#### Phase 1B：召回准确率提升

- 稳定专有名词检索
- 稳定历史偏好召回
- 稳定事件型记忆与事实型记忆的边界
- 强化 lexical / sparse 路径

#### Phase 1C：可解释与可观测

- 建 recall debug 面板或 debug API
- 输出 search intent、typed query、candidate、rerank 前后顺序
- 建立错误归因机制

### 5.5 Phase 1 指标

建议以检索指标为主，不要以“会不会自动总结”作为主指标。

| 指标 | 说明 |
|---|---|
| Recall@5 | 前五条是否能覆盖真正相关记忆 |
| MRR | 正确记忆排位是否足够靠前 |
| Precision@k | 是否存在明显噪声污染 |
| Hard-keyword hit rate | 专有名词、路径、错误码是否稳定命中 |
| Cross-session recall success | 跨会话偏好/决策是否稳定召回 |
| Isolation correctness | tenant/user/project 边界是否严格生效 |
| Commit durability | 会话记录在异常情况下是否仍可恢复 |

Phase 1 的验收，不建议使用“主观感觉更聪明了”。
Phase 1 的验收必须依赖 benchmark 和回归测试。

## 6. Phase 2：知识结构化学习期

### 6.1 目标定义

Phase 2 的目标是：

`利用稳定的记忆基建，把高质量经验提炼成结构化用户画像和可复用 Skill`

这里的前提是“稳定基建”。没有稳定基建，结构化学习只是在放大误差。

### 6.2 Phase 2 的两条主线

#### 6.2.1 用户画像

用户画像不是把对话摘要堆成一坨，而是要提炼成稳定结构。

建议最少拆为：

| 类型 | 含义 |
|---|---|
| Profile | 相对稳定的用户背景与长期属性 |
| Preference | 明确偏好、风格偏好、工作习惯 |
| Constraint | 不能违反的限制、边界条件 |
| Entity | 用户长期关注的项目、系统、对象 |

用户画像应满足：

- 可回溯到原始证据
- 可更新而不是无限追加
- 可冲突检测
- 可区分“用户明确说过”和“系统推断出来”

#### 6.2.2 Skill 学习

Skill 不是任何成功回答都能提炼。

建议 Skill 的输入样本至少满足：

- 多次重复出现
- 结果质量稳定
- 在相似问题上具备复用价值
- 可以表达为 procedure / strategy / negative rule

Skill 学习建议至少拆为：

| 类型 | 含义 |
|---|---|
| SOP | 可重复执行的标准步骤 |
| Strategy | 在特定情境下的高层策略 |
| Negative Rule | 明确不能做的错误模式 |
| Root Cause Pattern | 某类问题的成因与修复规律 |

### 6.3 Phase 2 的核心工作流

建议形成下面的受控流程：

1. 收集高质量 trace
2. 聚类相似任务
3. 提炼候选画像或 Skill
4. 做来源校验与冲突检测
5. 进入 candidate 状态
6. 经验证后再激活

也就是说，Phase 2 的关键不是“抽取得快”，而是“提炼得稳”。

### 6.4 Phase 2 的必要治理机制

Phase 2 必须补齐以下能力：

- 来源分级
- 冲突检测
- 版本化
- 置信度评分
- 人工确认或半自动确认
- 自动失效与淘汰

否则系统会出现两个典型问题：

- 老画像与新画像互相冲突
- 一次偶然成功被错误提炼为永久 Skill

### 6.5 Phase 2 的验收标准

Phase 2 的指标不能只看“抽取了多少条知识”，而要看知识有没有实际增益。

建议指标：

| 指标 | 说明 |
|---|---|
| Profile precision | 用户画像是否真实、稳定、低冲突 |
| Skill adoption gain | 激活 Skill 后是否提升后续任务成功率 |
| Skill replay success | 相似任务是否能因 Skill 提升结果质量 |
| Conflict rate | 新知识与旧知识的冲突比例 |
| Human approval rate | 候选知识中真正值得保留的比例 |
| Regression rate | 新提炼知识是否导致下游 recall/decision 退化 |

## 7. 当前仓库的模块归属建议

### 7.1 应优先归入 Phase 1 的模块

- `src/opencortex/orchestrator.py`
- `src/opencortex/storage/cortex_fs.py`
- `src/opencortex/storage/qdrant/adapter.py`
- `src/opencortex/retrieve/intent_router.py`
- `src/opencortex/retrieve/hierarchical_retriever.py`
- `src/opencortex/context/manager.py`
- `src/opencortex/http/*`

这些模块决定“记忆底座是否可靠”。

### 7.2 应降级预期、归入 Phase 2 的模块

- `src/opencortex/alpha/observer.py`
- `src/opencortex/alpha/trace_splitter.py`
- `src/opencortex/alpha/archivist.py`
- `src/opencortex/alpha/sandbox.py`
- `src/opencortex/alpha/trace_store.py`
- `src/opencortex/alpha/knowledge_store.py`

这些模块可以继续保留，但当前不应作为主承诺能力对外描述为“已完成自动知识进化闭环”。

## 8. 实施优先级建议

建议优先级严格按下面顺序推进。

### P0：必须先修

- Context 协议参数透传与模型对齐
- 检索过滤条件实际生效
- transcript 持久化可靠性
- Alpha 存储 DSL 与主存储接口对齐

### P1：Phase 1 主线增强

- recall benchmark 与回归集
- hard keyword/sparse path 增强
- debug trace 与可观测性增强
- memory type 进一步清晰化

### P2：Phase 2 启动条件建设

- trace 质量分级
- candidate knowledge 状态机
- 来源可信度与冲突检测

### P3：Phase 2 正式推进

- 用户画像抽取
- Skill candidate 提炼
- 验证与激活闭环

## 9. 风险判断

### 9.1 最大风险

最大的风险不是“功能不够多”，而是“阶段边界不清”。

如果在 Phase 1 未完成时提前推进 Phase 2，会导致：

- 噪声被提炼为规则
- 错误画像长期污染系统
- 错误 Skill 反过来影响召回和回答
- 调试复杂度指数级上升

### 9.2 正确风险控制方式

- 对外明确阶段定位
- 对内用 benchmark 驱动 Phase 1
- 对 Phase 2 采用 candidate-first 策略
- 所有高层知识必须可追溯到低层样本

## 10. 最终判断

OpenCortex 的正确路线不是“尽快做成全自动学习系统”，而是：

先把它做成行业里少见的、可靠的、可解释的记忆基础设施；
然后再利用这套基础设施，谨慎地提炼用户画像与 Skill。

换句话说：

- Phase 1 决定这套系统是否能成为基础设施
- Phase 2 决定这套系统是否能成为会进化的知识系统

这两个阶段都重要，但顺序不能错。

当前阶段，OpenCortex 最值得押注的是 Phase 1。
