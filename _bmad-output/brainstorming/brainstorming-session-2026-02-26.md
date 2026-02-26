---
stepsCompleted: [1, 2, 3, 4]
inputDocuments:
  - HRCM融合概览（对话中提供）
  - _bmad-output/memcortex-architecture.md
session_topic: 设计可实施的 HRCM POC——节省 Agent context window Token + 检索排序自学习优化，兼容 OpenViking/RuVector 后续演进
session_goals: 产出可实施 HRCM POC 设计，解决评审 8 个 findings，保持对 OpenViking/RuVector 的兼容性
selected_approach: ai-recommended
techniques_used:
  - Constraint Mapping
  - First Principles Thinking
  - Solution Matrix
ideas_generated: 28
context_file: ''
project_name: OpenCortex
repo: https://github.com/StardustVision/OpenCortex.git
session_active: false
workflow_completed: true
facilitation_notes: 用户决策风格果断务实，偏好"先跑通再优化"。关键转折点是发现 OpenViking 和 RuVector 生态隔离，用户果断选择 adapter 桥接方案。
---

# Brainstorming Session Results

**Facilitator:** Hugo
**Date:** 2026-02-26

## Session Overview

**Topic:** 设计可实施的 HRCM POC——节省 Agent context window Token + 检索排序自学习优化，兼容 OpenViking/RuVector 后续演进
**Goals:** 产出可实施 HRCM POC 设计，解决评审 8 个 findings，保持对 OpenViking/RuVector 的兼容性
**Project:** OpenCortex (https://github.com/StardustVision/OpenCortex.git)

### Session Setup

用户明确两个核心需求：
1. **节省 Token** = Agent 调用 memory 时 context window 消耗最小化
2. **自学习 Memory** = 检索排序基于使用反馈自动优化
3. **兼容性约束** = 参考 OpenViking 三层上下文 + RuVector 强化排序，保持对两者后续变更的兼容

## Technique Selection

**Approach:** AI-Recommended Techniques
**Analysis Context:** HRCM POC 设计——从概念设计收敛到可实施方案

**Recommended Techniques:**

- **Constraint Mapping:** 先画边界——识别兼容性约束、管道约束、token 预算约束
- **First Principles Thinking:** 拆到骨架——找到不可再分解最小核心
- **Solution Matrix:** 收敛决策——对 8 个 findings 逐一打分产出方案

**AI Rationale:** "先画边界 → 再拆本质 → 再做决策"的收敛路径。

## Technique Execution Results

### 1) Constraint Mapping

**约束地图总览：**

| # | 约束 | 类型 | 核心决策 |
|---|------|------|---------|
| 1 | OpenViking L0/L1/L2 层语义不可重定义 | 硬 | 强化排序叠加在三层之上，不替换任何层 |
| 2 | OpenViking Node 结构不侵入修改 | 硬 | 强化数据住在 RuVector 侧，通过 node_id 关联 |
| 3 | RuVector 排序公式完全原样使用 | 硬 | 不做外围修补，接受已知问题 |
| 4 | HRCM 替换 Phase 1 管道 | 硬 | 旧 spool/engine/sync 代码废弃 |
| 5 | 向量层用 RuVector | 硬 | 被约束 #6 进一步明确 |
| 6 | 为 OpenViking 实现 RuVector VectorDB adapter | 硬 | 两个生态通过 adapter 桥接 |
| 7 | 反馈 = 隐式推断为主 + 显式标记为辅 | 软 | 独立 Feedback Analyzer 模块，语义相似度法优先 |
| 7b | 隐式反馈由独立 Feedback Analyzer 模块处理 | 硬 | 职责与 Orchestrator 分离 |
| 8 | Token 展开 = 自动推荐 + 用户确认 | 硬 | 不完全自动也不完全手动 |
| 9 | POC 全部跑通 | 硬 | adapter + 三层 + 反馈 + token 控制 |
| 10 | 项目更名 OpenCortex | 硬 | 远程仓库 StardustVision/OpenCortex |

**关键发现：** OpenViking（字节/火山引擎）和 RuVector（独立开发者 ruvnet）是完全不同生态的项目，不能直接互联，需通过 adapter 桥接。OpenViking 自带 VikingFS（AGFS + VectorDB），支持 4 种后端（local/http/volcengine/vikingdb），RuVector 不在其中。

### 2) First Principles Thinking

**不可再分解核心架构（5 个模块，0 个可砍）：**

```
┌─────────────────────────────────────────────────┐
│                   Agent Layer                    │
│            retrieve(query) / feedback            │
└──────────────────────┬──────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────┐
│            Memory Orchestrator (8职责)            │
│  检索编排 / 写入 / 反馈转发 / decay调度           │
└───┬──────────┬──────────┬──────────┬────────────┘
    │          │          │          │
┌───▼───┐ ┌───▼────┐ ┌───▼───┐ ┌───▼──────────┐
│ Token │ │Feedback│ │Viking │ │  RuVector     │
│ Depth │ │Analyzer│ │  FS   │ │  Adapter      │
│Control│ │(语义)  │ │L0/L1  │ │(双面:标准+强化)│
│       │ │        │ │/L2    │ │              │
└───────┘ └────────┘ └───┬───┘ └──────┬───────┘
                         │            │
                    AGFS内容      RuVector向量+强化
```

**模块职责：**

1. **RuVector Adapter（双面）** — OpenViking 标准 VectorDB 接口（upsert/search/delete/update_uri）+ RuVector 强化能力全暴露（update_reward/get_profile/decay）
2. **Memory Orchestrator（8 职责不可删）** — ①接收 retrieve ②L0 过滤 ③RuVector search ④Token Depth 推荐 ⑤返回结果 ⑥反馈转发 ⑦decay 调度 ⑧写入新记忆
3. **Feedback Analyzer** — 语义相似度法（cosine similarity）推断 reward，预留 LLM 判定升级接口。独立模块，与 Orchestrator 职责分离
4. **Token Depth Controller** — 基于 query 复杂度 + L0 候选数推荐展开深度，不依赖 Agent 侧 token 信息
5. **OpenViking VikingFS** — L0(.abstract ~100tokens)/L1(.overview ~2ktokens)/L2(原文) 三层内容管理，fork 源码到 OpenCortex

### 3) Solution Matrix

**8 个 Findings 解决方案：**

| Finding | 解决方案 | 兼容性 | 复杂度 | 风险 |
|---------|---------|--------|--------|------|
| F1: Node 数据模型 | 双系统独立，node_id 关联，不需要 AdaptiveVikingNode 混合体 | 10/10 | 低 | 低 |
| F2: 三层边界 | 用 OpenViking 原生定义 + 延迟 SLO（L0<50ms L1<200ms L2<100ms 总<500ms） | 10/10 | 极低 | 中 |
| F3: 排序公式 | RuVector 原样使用 + Orchestrator 层 exploration slot（Top-K 留 1-2 个新记忆槽位） | 10/10 | 低 | 中 |
| F4: 反馈闭环 | Feedback Analyzer 语义相似度法 + 用户显式标记低频辅助 | 10/10 | 中 | 中 |
| F5: 衰减剪枝 | 双速 decay（普通 0.95 / protected 0.99）+ archive 不删除 + baseline 0.01 | 9/10 | 中 | 低 |
| F6: 管道集成 | 替换 Phase 1，新模块映射清晰（废弃 spool/engine/sync/vector_store/models） | N/A | 高 | 低 |
| F7: 检索 API | MCP Server + Claude Code Plugin(marketplace+skills) + CLI | 10/10 | 中 | 低 |
| F8: 参考实现 | 三项目完整分析：OpenViking(主框架fork) + RuVector(adapter桥接) + cortex-mem(架构参考) | 10/10 | — | 已识别 |

**三大参考项目定位：**

| 项目 | 在 OpenCortex 中的角色 | 集成方式 | 许可证 |
|------|----------------------|---------|--------|
| OpenViking | 主框架（VikingFS + L0/L1/L2 + 检索） | fork 源码到 OpenCortex，按需裁剪 | Apache 2.0 |
| RuVector | 向量后端 + 自学习强化排序 | adapter 桥接（POC: subprocess → 后续: PyO3） | Apache 2.0 |
| cortex-mem | 架构参考（trait 组合、记忆优化管道） | 设计参考，不直接依赖 | Apache 2.0 |

**检索 API 定义：**

```python
# MCP Tools
retrieve(query, namespace?, top_k?) -> RetrieveResult
expand(node_ids, depth?) -> list[NodeDetail]
write(content, namespace?, metadata?) -> node_id
feedback(node_ids, signal) -> None

# RetrieveResult
{
    nodes: list[NodeSummary],       # 排序后结果（L0/L1 级别）
    expandable_count: int,          # 可展开到 L2 的数量
    has_new_unverified: bool,       # 是否有 exploration slot
    recommended_depth: str,         # "L0" | "L1" | "L2"
}

# Claude Code Skills
/memory-search <query>
/memory-save <content>
/memory-feedback <node_id>
```

**反馈闭环完整通路：**

```
隐式反馈：
Agent retrieve → [node_1..k] → Agent response →
Feedback Analyzer(cosine_similarity) → reward → Orchestrator → RuVector.update_reward()

显式反馈：
用户标记 "有用/无用" → feedback(node_id, ±strong_reward) → RuVector.update_reward()
```

## Idea Organization and Prioritization

### Thematic Organization

**Theme A：架构桥接层（OpenViking ↔ RuVector）**
- 双面 RuVector Adapter（标准接口 + 强化能力全暴露）
- node_id 关联两个独立数据模型，零耦合
- POC 用 subprocess 调 rvf-cli，后续升级 PyO3
- OpenViking 源码 fork 到 OpenCortex，按需裁剪

**Theme B：智能 Token 控制**
- OpenViking L0/L1/L2 三层渐进展开（原样使用）
- Token Depth Controller 自动推荐 + 用户确认
- 延迟 SLO: L0<50ms, L1<200ms, L2<100ms, 总<500ms
- cortex-mem "Retrieve-then-Generate" 作为后续优化方向

**Theme C：自学习反馈闭环**
- 独立 Feedback Analyzer 模块（语义相似度法）
- 用户显式标记（低频辅助）
- LLM 判定法预留升级接口
- Exploration slot 解决冷启动（Top-K 留 1-2 个新记忆）

**Theme D：记忆生命周期管理**
- 双速 decay（普通 0.95 / protected 0.99 慢衰减）
- Protected 标记（用户手动 + 自动升级）
- Archive 不物理删除 + restore 接口
- 最小存活 baseline 0.01

**Theme E：暴露方式与生态集成**
- MCP Server（retrieve/expand/write/feedback tools）
- Claude Code Plugin（marketplace + skills）
- CLI（开发调试）
- 六边形架构：单一 core 多表面暴露

### Prioritization Results

**Top Priority — 地基层：**
1. fork OpenViking 源码，移植核心模块
2. 实现 RuVector Adapter
3. 实现 Memory Orchestrator

**Second Priority — 核心价值层：**
4. Feedback Analyzer（自学习核心）
5. Token Depth Controller（token 控制核心）
6. Decay + Protected + Archive 机制

**Third Priority — 生态层：**
7. MCP Server
8. Claude Code Plugin
9. CLI

**Breakthrough Concepts:**
- OpenViking 和 RuVector 是不同生态，HRCM 概览的融合前提不成立 → adapter 桥接
- HRCM 替换 Phase 1 管道（不是叠加）
- cortex-mem 的 trait 组合模式指导 OpenCortex 模块化
- RuVector 公式不动 + exploration slot 最小干预

### Action Planning

**Phase 1: 地基（周 1-2）**

| 步骤 | 任务 | 产出 |
|------|------|------|
| 1.1 | 研读 OpenViking 源码，产出移植清单 | 移植清单文档 |
| 1.2 | fork 核心源码：VikingFS + AGFS + L0/L1/L2 管道 | 内容存储层 |
| 1.3 | fork VectorDB 后端接口 + 检索模块 | 接口层 + 检索层 |
| 1.4 | 裁剪不需要的部分（volcengine/vikingdb 专有后端、C++ 扩展等） | 精简代码 |
| 1.5 | 验证移植：write → 自动 L0/L1/L2 → local 后端检索 | 移植验证通过 |
| 1.6 | 安装 RuVector，实现 RuVectorAdapter，注册为 VectorDB 后端 | 桥接跑通 |
| 1.7 | 实现 Memory Orchestrator 骨架 + 端到端验证 | 基本链路跑通 |

成功指标：write → L0/L1/L2 → RuVector 存储 → retrieve → 强化排序结果 → 三层展开

**Phase 2: 核心价值（周 3-4）**

| 步骤 | 任务 | 产出 |
|------|------|------|
| 2.1 | 实现 Feedback Analyzer（语义相似度法） | feedback_analyzer.py |
| 2.2 | 跑通隐式反馈通路 | 反馈闭环 |
| 2.3 | 实现显式反馈接口 | 用户标记通路 |
| 2.4 | 实现 Token Depth Controller | token_depth.py |
| 2.5 | 实现展开推荐 + 用户确认流程 | 展开机制 |
| 2.6 | 实现 Decay 调度 + protected + archive | 生命周期管理 |
| 2.7 | 实现 exploration slot | 冷启动缓解 |

成功指标：记忆排序随反馈动态变化 + 展开深度可控 + 生命周期自动管理

**Phase 3: 生态（周 5-6）**

| 步骤 | 任务 | 产出 |
|------|------|------|
| 3.1 | 实现 MCP Server | MCP 可用 |
| 3.2 | 实现 Claude Code Plugin（hooks + skills + marketplace） | Plugin 可用 |
| 3.3 | 实现 CLI | CLI 可用 |
| 3.4 | 端到端集成测试 | 完整 POC 验证 |
| 3.5 | 性能验证 <500ms SLO | 延迟达标 |
| 3.6 | 文档 + 发布准备 | 可发布状态 |

成功指标：Agent 通过 MCP/Plugin 完整体验节省 Token + 自学习排序

**关键风险：**

| 风险 | 缓解 |
|------|------|
| RuVector subprocess 延迟超 SLO | POC 先验证，不行提前投 PyO3 |
| OpenViking VectorDB 后端接口不可扩展 | 研究源码，必要时 fork 修改 |
| OpenViking C++ 扩展编译依赖 | 裁剪 C++ 部分，POC 用 pure Python |

## Session Summary and Insights

### Key Achievements

- 从概念性 HRCM 概览收敛到可实施的 OpenCortex POC 蓝图
- 识别并解决了全部 8 个评审 findings
- 发现 OpenViking/RuVector 生态隔离这一关键约束（原 HRCM 概览未识别）
- 引入 cortex-mem 作为第三参考项目，丰富了架构设计（trait 组合、记忆优化管道）
- 产出 10 个硬约束 + 5 个核心模块 + 3 阶段 action plan

### Session Reflections

本轮 brainstorming 的关键转折点是 Constraint Mapping 阶段发现 OpenViking 和 RuVector 是完全不同的生态——这一发现直接否定了 HRCM 概览中"融合"的前提假设，推动了 adapter 桥接方案的产生。用户决策风格果断务实（"先原样使用"、"替换旧管道"、"全部跑通"），使得 session 高效收敛。cortex-mem 的加入为架构设计提供了工程级参考，特别是 trait 组合模式和多表面暴露策略。

## Session Completion

本次 brainstorming 工作流已完成。
文档状态：`stepsCompleted: [1,2,3,4]`，`workflow_completed: true`。
