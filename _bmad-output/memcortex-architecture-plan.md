# MemCortex 架构设计文档 — 实施计划

## Context

Hugo 提出了 MemCortex（跨平台 Agent 长期记忆与技能分发系统）的初始设计概念，经过审阅后接受了 6 项关键改进建议。本计划将这些改进融合到一份完整的、可用于实施的中文架构设计文档中。

**6 项改进：**
1. Memory 侧完善安全机制（输入验证、内容消毒、访问控制、投毒防御）
2. 本地技能缓存（离线可用、版本管理、LRU 淘汰）
3. 智能传感器设计（按错误类型自适应采集策略）
4. 前置过滤分诊层（去重、严重性分级、冷却窗口）
5. 多标签命名空间 + 软路由（替代单一命名空间强制分类）
6. 服务端 Agent 每日记忆优化（过期、合并、冲突消解、质量评分）

---

## 输出文件

```
/Users/hugo/CodeSpace/Work/memcortex/_bmad-output/memcortex-architecture.md
```

文档语言：中文（遵循 `_bmad/core/config.yaml` 中的 `communication_language: Chinese`）

---

## 文档结构

### 第 1 节：系统概述
- 项目愿景与目标
- 核心设计原则（云端大脑 + 本地双手、安全优先、离线可用）
- 术语表
- 系统边界与约束

### 第 2 节：系统架构总览
- 高层架构图（Mermaid C4 图）
- 组件清单表
- 技术栈选型（Python, FastMCP, LanceDB, SQLite, Agno）
- 部署架构图

### 第 3 节：本地边缘层
- **3.1 智能传感器设计 [改进 #3]**
  - 传感器类型矩阵（Shell Hook / Git Hook / IDE Plugin / 手动触发）
  - 按错误类型的自适应上下文采集策略表
  - 结构化提取引擎（语言感知的堆栈解析器）
  - 优先级截断算法（P1 错误消息 → P2 内层栈帧 → P3 外层栈帧 → P4 环境信息 → P5 完整文件）
- **3.2 预过滤分诊层 [改进 #4]**
  - 三阶段管道：去重 → 严重性分级 → 冷却检查
  - 内容哈希 + 模糊 SimHash 去重
  - P0-P3 严重性分级规则
  - 滑动窗口冷却机制（P0 不冷却 / P1 10分钟 / P2-P3 1小时）
  - 分诊决策流程图
- **3.3 本地技能缓存 [改进 #2]**
  - 缓存目录：`~/.memcortex/cache/skills/`
  - ETag/hash 版本验证 + stale-while-revalidate
  - LRU 淘汰（默认 500MB）+ 可固定 Skill
  - 离线检测与降级策略
- **3.4 本地执行器接口**

### 第 4 节：云端中枢层
- 数据摄入 API（端点规范、输入验证、速率限制）
- Agno AI 反思引擎（Agent 设计、多标签路由、嵌入策略）
- 存储引擎（LanceDB 向量表设计、SQLite 元数据 Schema）
- FastMCP 服务器（MCP Tools 定义、传输层配置）
- 技能蓝图管理（生命周期、版本、分发协议）

### 第 5 节：安全模型 [改进 #1]
- 威胁模型（记忆投毒、Prompt 注入、未授权访问、数据外泄）
- 认证与授权（Agent 身份注册、RBAC、命名空间级 ACL）
- 记忆侧安全机制
  - 三层验证管道：Schema 验证 → 内容消毒 → 语义验证
  - Provenance 追踪 + confidence_score
  - 审计日志
- 传输安全（TLS）
- 数据安全（静态加密、敏感数据处理）

### 第 6 节：多标签命名空间系统 [改进 #5]
- 层次化标签体系（`lang:python`、`domain:auth:oauth2`）
- 软路由检索算法：`final_score = α × vector_similarity + (1-α) × label_match_score`
- 自动标签推断（模式匹配 + LLM 分类）
- 标签治理（系统标签受控词表 + `custom:` 前缀用户标签）

### 第 7 节：服务端 Agent 优化系统 [改进 #6]
- 调度架构（APScheduler / cron，默认每日 03:00 UTC）
- 四轮优化：
  1. 过期清理（TTL + 闲置天数 + 低置信度）
  2. 记忆合并（余弦相似度 > 0.85 + 标签重叠 → Agno Agent 合并）
  3. 冲突消解（矛盾记忆检测 → 自动/人工审核）
  4. 质量评分（使用次数 × 时效性 × 来源质量 × 内容结构）
- 安全机制：不硬删除，归档 30 天后永久清理
- 优化报告 Schema

### 第 8 节：核心数据流
- 记忆摄入流程（Mermaid 时序图）
- 记忆检索流程（Mermaid 时序图）
- 技能获取流程（缓存命中 vs 未命中路径）
- 每日优化流程

### 第 9 节：数据模型详细设计
- MemoryRecord、SkillBlueprint、Label、MemoryLabel
- AccessPolicy、AgentIdentity、AuditLog
- TriageRule、TriageEvent、CacheEntry
- SensorPayload、OptimizationReport

### 第 10 节：API 规范
- REST API（CRUD + 搜索 + Admin）
- MCP Tools（memory_store / memory_search / skill_fetch 等）
- 错误码规范

### 第 11 节：错误处理与可观测性
- 重试 / 熔断 / 优雅降级
- 结构化日志 + 监控指标 + 告警规则

### 第 12 节：项目结构与实施路线
- 推荐代码仓库目录结构
- 10 阶段实施计划（附依赖关系图）

---

## 关键架构决策摘要

| 改进 | 核心决策 |
|---|---|
| #1 安全 | 三层验证管道 + Agent RBAC + Provenance 追踪 + 记忆置信度评分 |
| #2 缓存 | ETag 版本验证 + LRU 淘汰 + stale-while-revalidate 离线降级 |
| #3 传感器 | 错误类型驱动的自适应采集 + 优先级截断 + 结构化输出 |
| #4 分诊 | 本地三阶段管道（去重→分级→冷却）减少 90%+ 噪声 |
| #5 多标签 | 层次化标签 + 加权软路由 + 自动推断 + 治理词表 |
| #6 优化 Agent | 四轮每日优化 + 归档不删除 + 优化报告 |

---

## Mermaid 图表清单

1. 系统架构总览（C4 Level 1）
2. 组件图（C4 Level 2）
3. 记忆摄入时序图
4. 记忆检索时序图
5. 技能获取时序图（缓存路径）
6. 分诊决策流程图
7. 每日优化时序图
8. 安全验证管道图
9. 多标签路由图
10. 部署架构图

---

## 涉及的关键文件

| 文件 | 用途 |
|---|---|
| `_bmad-output/memcortex-architecture.md` | **目标输出** — 完整架构设计文档 |
| `_bmad/core/config.yaml` | 读取语言配置和输出目录 |
| `_bmad/tea/config.yaml` | 参考 TEA 模块配置模式 |

---

## 验证方式

1. **文档完整性**：所有 12 个章节均已编写，6 项改进全部体现
2. **Mermaid 图表**：所有图表语法正确，可在 Markdown 预览器中渲染
3. **数据模型一致性**：第 9 节的模型定义与第 10 节 API 的请求/响应 Schema 一致
4. **中文质量**：全文为中文，术语首次出现时附英文原文
5. **可实施性**：第 12 节的实施路线包含具体的阶段划分和依赖关系
