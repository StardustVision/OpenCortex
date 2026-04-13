# OpenCortex 术语解释文档

> 版本：draft v1
> 更新：2026-04-06

## 1. 文档目的

本术语文档用于统一 OpenCortex 的产品语言与架构语言，避免概念混用、缩写冲突和边界模糊。

它的目标是：

- 统一产品文档与技术文档口径
- 防止同一个缩写承载多个含义
- 把外来概念解释清楚，而不是停留在比喻
- 明确 memory、knowledge、skill、retrieval、lifecycle 之间的边界

## 2. 关键命名规则

缩写 `CMA` 在 OpenCortex 中专指：

`Continuum Memory Architectures`

它不能再用作：

`Chaperone-Mediated Autophagy`

当需要引用生物学里的 CMA 时，必须写全称：

- `Chaperone-Mediated Autophagy`

如果只在内部草稿中需要缩写，可写：

- `bio-CMA`

这样做是为了避免最核心的术语冲突。

## 3. 核心架构术语

### 术语状态说明

本节术语按三种状态理解：

- `已实现`：当前代码中已有明确对应实现
- `设计中`：当前主要用于架构定义
- `概念性`：作为上位标准或解释框架存在

### OpenCortex

OpenCortex 是一套面向 Agent 的长程记忆原生知识系统。

它把持久记忆、联想召回、生命周期代谢、知识巩固与技能外化组合在同一架构中。

状态：`已实现 + 持续演进`

### Continuum Memory Architectures（CMA）

CMA 是 OpenCortex 采用的上位行为标准，用来定义长期记忆系统必须具备什么能力。

在 OpenCortex 中，CMA 包括：

- persistence
- selective retention
- retrieval-driven mutation
- associative routing
- temporal continuity
- consolidation and abstraction

状态：`概念性`

### Autophagy

Autophagy 是从细胞自噬机制中拆解后引入 OpenCortex 的生命周期层。

在 OpenCortex 中，Autophagy 指的是：

- 受压力触发的记忆代谢
- 对 memory 与 trace 的选择性处理
- 包括 quarantine、compression、merge、archive、forget、recycle 等分阶段动作
- 以稳态维持为目标，而不是无差别删除

Autophagy 不是系统的上位标准，CMA 才是。

状态：`设计中`

### Chaperone-Mediated Autophagy

这是一个生物学机制，在 OpenCortex 中被作为局部工程启发引入。

它在 OpenCortex 中对应：

- 高选择性
- 单条级别
- 先识别、再 escort、再处理
- 通过 gate 控制准入

它是 Autophagy 内部的子机制，不是整个系统的总框架。

状态：`设计中`

## 4. 数据层术语

### Memory

Memory 指系统保留的原始或半结构化上下文资产。

例如：

- user preference
- profile fact
- project constraint
- entity fact
- event
- document-derived fragment

Memory 通常具有时间性，并可能与新证据发生竞争。

状态：`已实现`

### Trace

Trace 指按时间组织的过程性记忆，描述“某件事是如何发生的”。

例如：

- session transcript 切片
- task episode
- tool-use chain
- failure/fix sequence

Trace 的主要价值是保留时间连续性，并支撑后续巩固。

状态：`已实现`

### Knowledge

Knowledge 指从 memory 与 trace 中沉淀出来的稳定抽象。

例如：

- belief
- rule
- SOP
- canonical profile
- negative rule

Knowledge 比 memory 更稳定，也更适合作为知识库的主表面。

状态：`已实现`

### Skill

Skill 指面向未来执行的可复用操作知识。

例如：

- workflow
- tool guide
- debugging pattern
- deployment procedure

Skill 回答的是“应该怎么做”，而不仅是“什么是真的”。

状态：`已实现`

## 5. 存储与召回术语

### CortexFS

指用于持久化记忆资产的三层表示：

- `L0`：abstract
- `L1`：overview
- `L2`：full content

它是存储基底，不是生命周期层。

历史说明：该设计在思路上受 OpenViking 启发，但在 OpenCortex 当前代码与文档中，规范名称是 `CortexFS`。

状态：`已实现`

### L0 / L1 / L2

三层细节等级分别表示：

- `L0`
极短摘要，用于低成本索引和快速预览。

- `L1`
结构化上下文概述，用于标准 recall。

- `L2`
完整内容，用于深度检查、审计和再抽象。

### Recall

Recall 指把相关的 memory、knowledge 或 skill 重新带回当前 Agent 上下文窗口的过程。

Recall 不等于 search。  
在 OpenCortex 中，Recall 还可能改变未来记忆状态。

状态：`已实现 + 持续演进`

### Cone Retrieval

Cone Retrieval 指受 m_flow 启发的联想式召回增强机制。

其原理是：

- 先拿一个较窄的粗召回集合
- 提取 anchor entities
- 沿实体共现邻域向外扩展
- 再按关系路径成本重新排序

它不是 dense retrieval 的替代物，也不是通用图谱推理引擎。当前实现主要依赖实体倒排和实体共现传播，而不是显式关系边。

状态：`已实现`

### m_flow

`m_flow` 在 OpenCortex 语境里指一种以路径传播和联想式扩展为核心思想的检索启发来源。

OpenCortex 没有直接照搬其完整结构，而是吸收了其中的关键原则：

- recall 不应只依赖平面相似度
- 检索可以从少量锚点出发向外扩展
- 路径成本可以作为重排辅助信号

在当前实现中，`m_flow` 的主要落点是 `Cone Retrieval`。

状态：`概念性启发`

### Associative Routing

Associative Routing 指 recall 能沿结构化线索传播，而不仅依赖平面语义相似度。

Cone Retrieval 是 Associative Routing 的一种实现方式。

状态：`已实现`

## 6. 生命周期术语

### Selective Retention

指并非所有被存储的内容都应永久保持同等可达性。

高价值内容可以被强化，低价值、陈旧、重复、被替代内容可以被压缩、归档或遗忘。

状态：`概念性 / 部分实现`

### Retrieval-Driven Mutation

指 recall 成功或失败都应影响未来记忆状态。

例如：

- 强化被引用的 memory
- 压低重复 near-miss 竞争项
- 将过时答案标记为 superseded

状态：`概念性 / 部分实现`

### Homeostasis

指长期记忆系统在运行中维持稳定、可用、低噪声状态的目标。

具体表现为在以下指标之间取得平衡：

- recall precision
- token cost
- noise density
- archive ratio
- abstraction yield

状态：`设计中`

### Quarantine

指一种临时隔离状态，用于处理不确定、冲突或风险较高的对象，使其先退出主 recall 面，再决定后续动作。

### Compression

指把对象降级为更低成本表示的过程，例如：

- `L2 -> L1`
- `L1 -> L0`
- trace -> gist

Compression 不等同于删除。

### Archive

指对象仍被保留，但默认不再进入主 recall 表面。

Archive 的主要价值是保留追溯能力、审计能力和恢复空间。

状态：`已实现 + 设计扩展中`

### Forgetting

指低价值或过时对象的逻辑删除或物理删除。

在 OpenCortex 中，Forgetting 是生命周期能力，而不是系统故障。

状态：`部分实现`

### Recycling

指把被降级或被替代的细节转换成更高价值抽象的过程。

例如：

- multiple episodes -> one belief
- repeated traces -> workflow candidate
- noisy details -> compact gist

状态：`概念性 / 部分实现`

## 7. 抽象层术语

### Consolidation

Consolidation 指把重复出现、具备证据支持的经历转化为更稳定抽象的过程。

它连接了 memory 与 knowledge。

状态：`已实现 + 持续演进`

### Abstraction

Abstraction 指从具体细节提升到可复用高阶表示的过程。

例如：

- event -> rule
- trace cluster -> SOP
- procedure pattern -> skill

状态：`概念性 / 部分实现`

### SkillEngine

SkillEngine 是下游的技能抽象层，概念上对齐 OpenSpace 的 skill engine 思路。

它消费巩固结果，将其转化为可执行 skill；但它不接管原始 memory 的生命周期。

状态：`已实现`

## 8. 对外产品语言建议

对外优先使用这些表述：

- `长程记忆系统`
- `记忆原生知识系统`
- `三层持久记忆`
- `联想式锥形召回`
- `受自噬机制启发的记忆代谢层`
- `知识与技能巩固`

对外不建议直接使用这些词，除非已经解释清楚：

- `bio-CMA`
- `lysosome-like cleanup`
- `认知操作系统`
- `全图推理`

## 9. 最终术语总结

OpenCortex 的规范术语层级如下：

- `CMA`
定义系统整体应成为什么

- `Autophagy`
定义 memory 与 trace 如何被代谢式管理

- `Chaperone-Mediated Autophagy`
定义 Autophagy 内部的一条高选择性精细处理子机制

- `CortexFS`
定义记忆如何被物理表示与持久化

- `Cone Retrieval`
定义联想式 recall 如何被增强

- `SkillEngine`
定义可复用操作知识如何被外化
