# OpenCortex 产品文档

> 版本：draft v1
> 更新：2026-04-06

## 1. 产品定位

OpenCortex 是一套面向 AI Agent 的记忆原生知识系统。

它不是简单的向量数据库封装，不是传统 RAG 外挂，也不是静态文档知识库。  
OpenCortex 的目标，是为 Agent 提供一个可长期运行的认知底座，使其具备：

- 跨会话持续存在的长期记忆
- 不仅依赖语义相似度、还具备结构联想能力的召回
- 对低价值、过时、冲突内容进行压缩、归档、遗忘的代谢能力
- 从重复经验中沉淀知识的巩固能力
- 将高阶经验外化为技能的进化能力

一句话：

`OpenCortex 是 Agent 的长期记忆、知识与技能底座。`

## 2. 产品主张

传统知识系统默认“知识已经作为文档存在”，系统要做的主要是存储和检索。

OpenCortex 的基本假设不同：

- Agent 首先产生的是经历，而不是稳定知识
- 记忆天然带时间性、噪声、重复和冲突
- 真正有价值的知识需要经过 recall、反馈和巩固才会浮现
- 长期运行的系统不能只有存储，还必须具备代谢能力

因此，OpenCortex 从一开始就被设计成三层结构：

- 底层是 `Memory`
- 中层是 `Knowledge`
- 上层是 `Skill`

这意味着 OpenCortex 的核心产品定义不是：

`把知识存进去再查出来`

而是：

`让知识从记忆中生长出来。`

## 3. 核心产品定义

OpenCortex 是一套符合 `Continuum Memory Architectures` 标准的长程 Agent 记忆系统。

这里的 `CMA` 指的是 `Continuum Memory Architectures`，也就是长期记忆系统的上位行为标准。技术细节见技术设计文档；在产品层面，只需把它理解为：OpenCortex 不是静态知识库，而是一套会持续保留、联想召回、选择性代谢并不断抽象经验的长期记忆系统。

OpenCortex 通过以下机制来落实这一标准：

- `CortexFS` 三层记忆载体（历史上受 OpenViking 启发）
- `m_flow` 风格锥形检索
- 受细胞自噬启发的 `Autophagy` 生命周期层
- 对齐 `OpenSpace` 思路的 `SkillEngine`

## 3.1 当前实现状态

截至当前代码基线，OpenCortex 的能力可分为两类：

### 已实现

- `CortexFS + Qdrant` 三层持久化
- dense / sparse / rerank 平面召回
- `Cone Retrieval` 锥形检索
- `Observer -> TraceSplitter -> TraceStore`
- `Archivist -> Sandbox -> KnowledgeStore`
- `SkillEngine`
- reward / decay / protect / accessed_at 等既有排序与反馈机制
- `IntentRouter`
- 三模式写入 `memory / document / conversation`
- MCP 协议接入
- JWT 多租户身份隔离

### 设计中

- `Autophagy` 生命周期总线
- 生命周期事件模型
- 通用状态机
- `Chaperone-Mediated Autophagy` 精细代谢管线
- 统一的代谢决策信号体系

## 4. 产品分层

### 4.1 记忆层

记忆层存放原始、带上下文、带时间性的材料：

- 用户偏好
- 项目上下文
- 事实与实体信息
- 事件记录
- 对话片段
- trace 轨迹
- 文档解析后的片段

这一层回答：

`发生过什么？系统见过什么？在某个时刻知道什么？`

### 4.2 知识层

知识层存放从记忆中沉淀出来的稳定结果：

- belief
- rule
- SOP
- negative rule
- entity profile
- 项目 canonical knowledge

这一层回答：

`哪些内容已经稳定到足以被视为知识？`

### 4.3 技能层

技能层存放面向执行的高阶经验：

- workflow
- tool guide
- debugging pattern
- deployment procedure
- reusable skill

这一层回答：

`遇到类似任务时，Agent 应该怎么做？`

## 5. 为什么 OpenCortex 不只是知识库

OpenCortex 可以作为知识库，但它的原生形态不是静态文档库。

传统知识库通常只关注：

- 文档导入
- 分块
- embedding
- 检索

OpenCortex 额外具备传统知识库通常缺少的系统行为：

- 时间敏感记忆
- trace 连续性
- recall 之后的强化与抑制
- 选择性保留与遗忘
- 经验巩固
- 技能提炼

因此，OpenCortex 更准确的定义应当是：

`记忆原生知识引擎`

而不是：

`一个 RAG 知识库`

## 6. 核心产品能力

### 6.1 持久记忆

OpenCortex 能让记忆跨 session 保留，并在未来任务中再次提供给 Agent。

### 6.2 三模式写入

OpenCortex 当前已实现三种写入模式：

- `memory`
面向偏好、约束、事实、事件等原生记忆写入

- `document`
面向文档解析与知识导入

- `conversation`
面向对话流写入，支持即时可检索与后续聚合

其中，`conversation` 模式采用 `immediate + merged` 双层写入，先保证即时 recall，再在后续阶段进行合并与替换。

### 6.3 分层召回

系统采用三层表示：

- `L0`：极短摘要
- `L1`：结构化概述
- `L2`：完整内容

这样既能降低 token 成本，也能保留完整可追溯性。

### 6.4 联想召回

OpenCortex 不只依赖平面相似度，还可以通过锥形检索沿实体共现邻域扩展召回。

### 6.5 Recall 路由

在 recall 之前，系统会先经过 `IntentRouter`：

- 关键词快速判断
- LLM 语义分类
- memory trigger 扩展查询

这使得 recall 不是固定模板，而是按意图动态选择召回策略。

### 6.6 记忆代谢

OpenCortex 把记忆系统视为代谢系统，而不是堆积系统。它能：

- 强化高价值内容
- 隔离冲突内容
- 压缩冗余细节
- 归档陈旧材料
- 遗忘低价值噪声
- 回收出更高阶的知识产物

其中，统一的 `Autophagy` 生命周期层目前仍属于设计中目标；当前系统已具备部分相关能力，但还没有收敛成一个单独的总线模块。

### 6.7 知识巩固

重复出现、被反复验证的经验，可以沉淀为：

- 稳定事实
- belief
- rule
- procedure

### 6.8 技能外化

高价值操作经验可以被提炼成显式 skill，用于指导未来执行。

### 6.9 多租户与 Agent 接入

OpenCortex 已具备两项关键基础设施能力：

- 基于 JWT claims `tid / uid` 的多租户身份隔离
- 基于 MCP 的 Agent 接入协议

这使得它既能作为后端记忆服务存在，也能作为标准 Agent 工具链接入 Claude Code、Cursor 等 MCP 客户端。

## 7. 产品闭环

OpenCortex 的产品闭环如下：

1. Agent 产生内容、行为和 trace。
2. OpenCortex 将其写入 memory 与 trace 资产。
3. 写入阶段根据 `memory / document / conversation` 三模式走不同处理路径；其中 conversation 模式支持 `immediate + merged` 双层写入。
4. 后续查询通过 `IntentRouter`、分层召回和联想召回获得相关上下文。
5. Recall 会改变未来记忆状态，强化命中内容、压制竞争噪声。
6. `Observer -> TraceSplitter -> TraceStore -> Archivist -> Sandbox -> KnowledgeStore` 将过程材料持续巩固为知识。
7. `Autophagy` 目标上将统一管理 memory 与 trace 的生命周期；当前仍属于设计中能力。
8. 高价值材料进一步巩固为知识，并最终外化为技能。

这条闭环使 OpenCortex 从“存储系统”升级为“认知基础设施”。

## 8. 适用场景

OpenCortex 适合以下场景：

- 需要长期项目记忆的 coding agent
- 需要知识与执行上下文并存的 enterprise copilot
- 通过积累 trace 持续提升的 workflow agent
- 维护个人偏好、习惯和约束的 personal AI
- 需要跨会话稳定保持上下文的通用助手

## 9. 产品原则

OpenCortex 遵循以下原则：

- 记忆优先，知识其次，技能在上
- 召回必须具备结构性，而不仅是语义相似
- 遗忘是能力，不是故障
- 抽象必须保留来源与证据
- 长期系统必须有稳态控制
- trace 很重要，因为过程知识很重要

## 10. 产品边界

OpenCortex 不是以下任何一个单一系统：

- 仅仅是向量数据库
- 仅仅是文档存储
- 仅仅是 workflow 引擎
- 仅仅是 skill 注册表
- 仅仅是 recall 排序优化栈

它是这些能力之下的长期认知底座。

## 11. 最终产品定义

OpenCortex 是一套面向 AI Agent 的长程记忆原生知识系统。它将持久记忆、分层载体、联想式锥形召回、记忆代谢、知识巩固与技能外化整合进同一架构中，使 Agent 能够在长期运行中持续记住、检索、压缩、抽象并运用经验。
