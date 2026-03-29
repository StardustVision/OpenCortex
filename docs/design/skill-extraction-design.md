# Skill 提炼与进化系统设计文档

> **文档状态**: Draft v2
> **基线版本**: OpenCortex 0.6.4
> **更新日期**: 2026-03-29
> **变更记录**: v2 — 重构为分层架构，Skill 从已验证 Knowledge 提炼而非原始 trace

---

## 目录

1. [背景与动机](#1-背景与动机)
2. [设计原则](#2-设计原则)
3. [分层架构](#3-分层架构)
4. [数据模型](#4-数据模型)
5. [模块设计](#5-模块设计)
6. [核心流程](#6-核心流程)
7. [Prompt 设计](#7-prompt-设计)
8. [API 设计](#8-api-设计)
9. [配置设计](#9-配置设计)
10. [实施计划](#10-实施计划)
11. [风险与控制](#11-风险与控制)

---

## 1. 背景与动机

### 1.1 历史演进

```
ACE (Feb 2026)
  ├─ 自动从会话提取 skill
  ├─ 问题: Phase 1 不稳定时做自动提取 → "把噪声结构化"
  └─ 结局: 完全删除 (ac080aa, Mar 10)

两阶段路线图 (Mar 14)
  ├─ Phase 1: 记忆基建 → 存得准、找得回、排得对、边界清
  ├─ Phase 2: 结构化学习 → 在稳定基建上做知识提炼
  └─ 核心判断: "Phase 1 未稳定前推进自动学习, 系统会把噪声结构化"

Cortex Alpha (Mar 10 - 现在)
  ├─ Observer + TraceSplitter + Archivist + Sandbox + KnowledgeStore
  ├─ 提取 4 类知识: belief / sop / negative_rule / root_cause
  └─ 当前状态: Observer 启用, 其余默认关闭 (Phase 1 收缩)
```

### 1.2 ACE 失败的核心教训

1. **从原始会话直接提取 skill → 噪声被结构化为规则**
2. **没有质量门控 → 一次偶然成功被永久化**
3. **没有进化/淘汰 → 低质量 skill 堆积**

### 1.3 本次设计的核心改变

**Skill 不从原始 trace 提取，而是从已验证的 Knowledge 中提炼。**

这解决了 ACE 的根本问题：Knowledge 经过 Archivist 聚类提取 + Sandbox 质量门控 + 人工/自动审批 → ACTIVE 状态，噪声已被过滤。Skill 只消费这些已验证的结构化知识。

---

## 2. 设计原则

### 2.1 分层隔离

```
记忆系统: 独立优化、独立精炼    (Phase 1 基建)
Skill 系统: 独立优化、独立生命周期 (Phase 2 进化)
```

- 记忆系统负责：存储、检索、反馈排序、知识提取
- Skill 系统负责：从已验证知识中提炼可复用技能、进化优化
- **单向数据流**：Knowledge → Skill（提炼方向），Skill 变更 → 参考记忆（反馈方向）
- **不互相污染**：Skill 进化不修改 Knowledge，Knowledge 更新不自动改 Skill

### 2.2 不重复建设

| v1 设计 (冗余) | v2 设计 (精简) |
|----------------|----------------|
| SkillExtractor 从 trace 聚类提取 | **复用 Archivist**，Skill 只消费 ACTIVE Knowledge |
| SkillStore 独立实现 CRUD | **复用 KnowledgeStore 模式**，但独立 collection |
| 三类型 (workflow/strategy/code) | **不预设类型**，由 LLM 自由分类 |
| Sandbox 重写 skill 版 | **直接复用 Sandbox** |
| SkillExtractor.extract_session_quick() | **删除**，不从原始 trace 快提 |

### 2.3 只新增真正缺失的能力

现有系统缺的不是"另一套提取管道"，而是：

1. **从知识到技能的提炼** — Knowledge 是碎片化的事实/规则，Skill 是可操作的组合能力
2. **失败驱动的进化** — Knowledge 是静态的（提取后不变），Skill 需要持续优化
3. **版本与回滚** — Knowledge 无版本概念，Skill 进化需要血统追踪和退化保护

---

## 3. 分层架构

### 3.1 系统分层

```
┌─────────────────────────────────────────────────┐
│                Agent 会话层                       │
│  prepare: 检索 memory + knowledge + skill        │
│  commit:  记录引用, 反馈计分                       │
│  end:     触发记忆采集                            │
└───────────────────┬─────────────────────────────┘
                    │
┌───────────────────┴─────────────────────────────┐
│            记忆层 (Phase 1, 独立优化)             │
│                                                   │
│  写入: add() → dedup → L0/L1/L2 → Qdrant         │
│  检索: IntentRouter → HierarchicalRetriever       │
│  排序: dense+sparse + rerank + reward scoring + hotness │
│  采集: Observer → TraceSplitter → TraceStore      │
│  提炼: Archivist → Sandbox → KnowledgeStore      │
│                                                   │
│  输出: ACTIVE Knowledge (已验证的结构化知识)      │
└───────────────────┬─────────────────────────────┘
                    │ 数据源: ACTIVE Knowledge
                    ▼
┌─────────────────────────────────────────────────┐
│            Skill 层 (Phase 2, 独立优化)           │
│                                                   │
│  提炼: SkillRefiner (从 Knowledge 合成 Skill)    │
│  存储: SkillStore (独立 collection)               │
│  进化: SkillDesigner (失败分析 → 改进)            │
│  保护: EvolutionSnapshotManager (回滚)            │
│                                                   │
│  反馈: Skill 变更 → 记忆层参考 (不修改)          │
└─────────────────────────────────────────────────┘
```

### 3.2 数据流

```
记忆层内部流程 (已有, 不修改):
  Session → Observer → TraceSplitter → Archivist → Sandbox → KnowledgeStore
                                                                    │
                                                          ACTIVE Knowledge
                                                                    │
Skill 层流程 (新增):                                                │
                                                                    ▼
  SkillRefiner ◄──── 读取 ACTIVE Knowledge ────────────────────────┘
       │               (belief + sop + negative_rule + root_cause)
       │
       ▼
  合成 Skill 候选 ──── SkillStore.save(CANDIDATE)
       │
       ▼
  Sandbox.evaluate() ──── 验证 → VERIFIED → approve → ACTIVE
       │
       ▼
  Agent 使用 Skill ──── ContextManager.prepare() 返回 skills
       │
       ├─ 成功引用 → SkillStore.update_reward(+)
       └─ 失败场景 → CaseCollector.add_case()
                          │
                          ▼
                    SkillDesigner.run_evolution()
                      ├─ 分析失败模式
                      ├─ 提议改进
                      ├─ 创建进化版本
                      └─ 快照保护
```

### 3.3 与 v1 设计的关键区别

| | v1 | v2 |
|---|---|---|
| **Skill 数据源** | 原始 trace (L0 摘要) | ACTIVE Knowledge (已验证) |
| **提取触发** | 每次 session end | 定期批量 (Knowledge 累积后) |
| **噪声风险** | 高 (原始 trace 含噪声) | 低 (Knowledge 经过 Sandbox) |
| **新建组件数** | 3 (Extractor + Store + Designer) | 2 (Refiner + Designer) |
| **复用程度** | 低 (几乎复制整套管道) | 高 (复用 Archivist + Sandbox + KnowledgeStore 模式) |

---

## 4. 数据模型

### 4.1 Skill 数据类

Skill 不预设类型分类，由 LLM 在提炼时自由描述。

```python
@dataclass
class Skill:
    # ── 身份 ──
    skill_id: str                              # s-{uuid.hex}
    tenant_id: str
    user_id: str
    scope: KnowledgeScope                      # USER | TENANT | GLOBAL
    status: SkillStatus
    version: int = 1
    parent_skill_id: Optional[str] = None      # 进化血统
    created_at: str
    updated_at: str

    # ── 内容 ──
    name: str                                  # 简短标识名
    abstract: Optional[str] = None             # L0: 一句话描述
    overview: Optional[str] = None             # L1: 完整 skill 卡片

    # ── 结构化字段 ──
    description: Optional[str] = None          # 详细描述
    trigger_conditions: List[str]              # 何时使用
    action_steps: Optional[List[str]] = None   # 操作步骤 (如有)
    anti_patterns: Optional[List[str]] = None  # 常见错误
    preconditions: Optional[str] = None        # 前置条件
    success_criteria: Optional[str] = None     # 成功标准

    # ── 来源 ──
    source_knowledge_ids: List[str]            # 来源 Knowledge ID 列表
    source_trace_ids: List[str]                # 间接来源 trace ID (经由 Knowledge)

    # ── 反馈计分 ──
    reward_score: float = 0.0
    usage_count: int = 0
    success_count: int = 0
    failure_count: int = 0

    # ── 进化 ──
    evolution_history: List[Dict[str, Any]] = field(default_factory=list)
```

**相比 v1 的简化**:
- 去掉 `SkillType` 枚举 — 不强制分类
- 去掉 `code_template` / `language` / `dependencies` — 代码模板是特殊场景，不应作为通用字段
- 去掉 `principle` / `when_to_apply` / `when_to_avoid` — 这些可以自然地放在 `overview` 中
- 新增 `source_knowledge_ids` — 追踪 Skill 的知识来源，实现分层可追溯

### 4.2 SkillStatus 枚举

```python
class SkillStatus(str, Enum):
    CANDIDATE = "candidate"    # 新提炼
    VERIFIED = "verified"      # 通过 Sandbox
    ACTIVE = "active"          # 生效中
    DEPRECATED = "deprecated"  # 已废弃
    EVOLVED = "evolved"        # 被新版本取代
```

### 4.3 FailureCase 数据类

```python
@dataclass
class FailureCase:
    case_id: str                               # fc-{uuid.hex}
    trace_id: str
    session_id: str
    tenant_id: str
    user_id: str
    trace_abstract: Optional[str] = None
    outcome: str = "failure"
    skills_applied: List[str] = field(default_factory=list)
    created_at: str
```

### 4.4 Qdrant Collection Schema

`skills` collection — 轻量化，只保留必要字段：

| 字段 | 类型 | 索引 | 说明 |
|------|------|------|------|
| `id` | string | PK | == skill_id |
| `skill_id` | string | scalar | 唯一标识 |
| `tenant_id` | string | scalar | 租户隔离 |
| `user_id` | string | scalar | 用户隔离 |
| `scope` | string | scalar | user / tenant / global |
| `status` | string | scalar | 状态 |
| `version` | int64 | scalar | 版本号 |
| `parent_skill_id` | string | scalar | 父版本 ID |
| `reward_score` | float | scalar | 反馈累积分数 |
| `usage_count` | int64 | scalar | 使用次数 |
| `name` | string | scalar | skill 标识名 |
| `abstract` | string | full-text | L0 摘要 |
| `overview` | string | — | L1 详情 |
| `vector` | dense_vector | HNSW | 语义向量 |
| `created_at` | datetime | scalar | 创建时间 |
| `updated_at` | datetime | scalar | 更新时间 |

---

## 5. 模块设计

### 5.1 SkillRefiner (新建)

**职责**: 从 ACTIVE Knowledge 合成 Skill

**文件**: `src/opencortex/alpha/skill_refiner.py`

```python
class SkillRefiner:
    def __init__(
        self,
        llm_fn: Callable[..., Coroutine],
        knowledge_store: KnowledgeStore,
        skill_store: SkillStore,
        embedder=None,
        min_knowledge_count: int = 3,  # 最少多少条 Knowledge 才触发合成
    ):

    async def run(self, tenant_id: str, user_id: str) -> List[Skill]:
        """从 ACTIVE Knowledge 合成 Skill 候选。

        流程:
          1. 读取所有 ACTIVE Knowledge
          2. 按 embedding 相似度聚类 (复用 Archivist 的聚类逻辑)
          3. 对每个聚类, 判断是否已有对应 ACTIVE Skill (去重)
          4. 对无覆盖的聚类, 调用 LLM 合成 Skill
          5. 返回 Skill 候选 (status=CANDIDATE)
        """

    async def _synthesize_from_cluster(
        self,
        knowledge_cluster: List[Dict],
        existing_skills: List[Dict],
    ) -> Optional[Skill]:
        """从一组相关 Knowledge 合成一个 Skill。

        输入 Knowledge 可能包含:
          - 1 个 SOP (操作步骤)
          - 2 个 BELIEF (策略原则)
          - 1 个 NEGATIVE_RULE (反模式)
          - 1 个 ROOT_CAUSE (根因分析)

        LLM 将这些碎片知识合成为一个可操作的 Skill 卡片。
        """
```

**为什么叫 Refiner 而不是 Extractor**:
- Extractor 暗示从原始数据中提取 (ACE 的做法)
- Refiner 强调从已精炼的 Knowledge 中进一步提炼 (分层架构)

### 5.2 SkillStore (新建)

**职责**: Skill 持久化、检索、反馈计分更新、版本管理

**文件**: `src/opencortex/alpha/skill_store.py`

复用 `KnowledgeStore` 的代码模式，增加版本和进化相关方法：

```python
class SkillStore:
    async def init(self) -> "SkillStore"
    async def save(self, skill: Skill) -> str
    async def search(self, query, tenant_id, user_id, limit=10) -> List[Dict]
    async def get(self, skill_id) -> Optional[Dict]
    async def approve(self, skill_id) -> bool
    async def reject(self, skill_id) -> bool
    async def list_candidates(self, tenant_id) -> List[Dict]
    async def list_active(self, tenant_id, user_id) -> List[Dict]

    # 反馈计分
    async def update_reward(self, skill_id, reward_delta, success) -> bool

    # 进化
    async def create_evolved_version(self, parent_id, new_skill) -> str
    async def rollback(self, skill_id) -> Optional[str]
```

### 5.3 SkillDesigner (新建)

**职责**: 失败驱动的 Skill 进化

**文件**: `src/opencortex/alpha/skill_designer.py`

包含三个组件，与 v1 相同但简化了 prompt：

```python
class CaseCollector:
    """滚动窗口失败案例收集 + Qdrant 持久化"""
    # v1 缺陷修复: 持久化到 Qdrant, 不再是纯内存

class EvolutionSnapshotManager:
    """快照回滚保护"""

class SkillDesigner:
    async def collect_failure(self, trace, skills_applied) -> None
    async def run_evolution(self, tenant_id, user_id) -> Dict:
        """两阶段进化:
        Stage 1: LLM 分析失败模式
        Stage 2: LLM 提议改进 (add_new / refine_existing / no_change)
        + 快照保护
        """
```

**v2 的改进**: CaseCollector 持久化到 Qdrant（使用独立 collection），解决 v1 中"重启丢失失败案例"的缺陷。

### 5.4 不新建的组件

| 组件 | 不新建原因 | 替代方案 |
|------|-----------|---------|
| SkillExtractor | Archivist 已做 trace → Knowledge | SkillRefiner 从 Knowledge 合成 |
| Skill 专用 Sandbox | 通用 Sandbox 够用 | 直接复用，调整参数 |
| Skill 专用 IntentRouter | 语义搜索足够 | SkillStore.search() |

---

## 6. 核心流程

### 6.1 Skill 提炼 (定期批量)

```
触发条件 (满足任一):
  - 定时: 每 interval_hours (默认 24h)
  - 阈值: 新增 ACTIVE Knowledge >= threshold (默认 5)
  - 手动: POST /api/v1/skill/refine/trigger

流程:
  1. SkillRefiner 读取所有 ACTIVE Knowledge
  2. 按 embedding 相似度聚类
  3. 对每个聚类, 检查是否已有对应 ACTIVE Skill
  4. 对无覆盖的聚类, LLM 合成 Skill 候选
     - 输入: 聚类中所有 Knowledge 的 abstract + overview
     - 输出: Skill 候选 (name, description, trigger_conditions, ...)
  5. Sandbox.evaluate() 质量门控
  6. 保存到 SkillStore (CANDIDATE → VERIFIED → ACTIVE)
```

### 6.2 Skill 检索与反馈

```
检索 (ContextManager._prepare):
  1. 并行: _memory_search() + _knowledge_search() + _skill_search()
  2. _skill_search() 调用 SkillStore.search()
  3. 合并返回

反馈计分:
  - Session 成功 + agent 引用了 skill → update_reward(+)
  - Session 失败 + skill 被引用 → update_reward(-)
  - 以 session outcome 为准, 而非 "引用即正反馈"
```

### 6.3 Skill 进化 (失败驱动)

```
触发: 定期 (默认 24h, 与提炼可合并)

流程:
  1. CaseCollector 提供近期失败案例
  2. SkillDesigner.run_evolution():
     a. 聚类失败案例
     b. LLM 分析: skill_gap / wrong_retrieval / skill_quality
     c. LLM 提议: add_new / refine_existing / no_change
     d. 应用变更
     e. 快照保护
  3. 如果连续 N 次无提升 → 回滚到最佳快照
```

### 6.4 反向参考: Skill → 记忆层

当 Skill 进化时，Designer 可以**读取**相关的 Knowledge 和 trace 作为参考：

```python
# Designer 分析时, 通过 source_knowledge_ids 追溯
for kid in skill.source_knowledge_ids:
    knowledge = await knowledge_store.get(kid)
    # 参考 knowledge 的 evidence_trace_ids 获取原始 trace
```

**只读不写**: Designer 不修改 Knowledge 或 trace，只作为分析输入。

---

## 7. Prompt 设计

### 7.1 SKILL_SYNTHESIZE_PROMPT (提炼)

```
You are synthesizing a reusable skill from verified knowledge items.

Knowledge items ({count} items):
{knowledge_items}

Each knowledge item has been verified through quality gates.
Your task: combine them into ONE actionable skill card.

A good skill card:
- Has a clear trigger condition (when to use)
- Has concrete action steps or principles
- Notes common pitfalls (anti-patterns)
- Is general enough to reuse, specific enough to be useful

Existing skills (avoid duplicates):
{existing_skills_summary}

Return JSON:
{{
  "name": "snake_case_name",
  "description": "What this skill does",
  "trigger_conditions": ["when to use this"],
  "action_steps": ["step 1", "step 2"],
  "anti_patterns": ["common mistake"],
  "preconditions": "what must be true before using",
  "success_criteria": "how to know it worked"
}}

Return null if the knowledge items don't form a coherent skill.
```

### 7.2 SKILL_EVOLUTION_ANALYSIS_PROMPT (进化分析)

```
Analyze failure cases for the skill system.

Current active skills:
{skill_bank}

Failure cases ({count}):
{failure_cases}

For each case, classify:
- skill_gap: No skill covers this scenario
- wrong_retrieval: Skill exists but wasn't retrieved
- skill_quality: Skill was retrieved but inadequate
- non_skill: Unrelated to skills

Return JSON:
{{
  "patterns": [
    {{
      "root_cause": "skill_gap|wrong_retrieval|skill_quality|non_skill",
      "description": "pattern description",
      "recommendation": "what to change"
    }}
  ]
}}
```

### 7.3 SKILL_EVOLUTION_REFINEMENT_PROMPT (进化提议)

```
Based on the analysis, propose skill changes.

Analysis: {analysis}
Current skills: {skill_bank}

For each recommendation, choose:
A. add_new — provide full skill definition
B. refine_existing — provide skill_id + changes
C. no_change

Return JSON:
{{
  "changes": [
    {{
      "action": "add_new|refine_existing|no_change",
      "skill_id": "...",
      "skill": {{...}},
      "reasoning": "..."
    }}
  ]
}}
```

---

## 8. API 设计

### 8.1 HTTP Endpoints

| Method | Path | 说明 |
|--------|------|------|
| POST | `/api/v1/skill/search` | 语义搜索 ACTIVE skills |
| GET | `/api/v1/skill/list` | 列出 ACTIVE skills |
| GET | `/api/v1/skill/candidates` | 列出待审批候选 |
| POST | `/api/v1/skill/approve` | 审批通过 |
| POST | `/api/v1/skill/reject` | 审批拒绝 |
| POST | `/api/v1/skill/feedback` | 反馈计分 |
| POST | `/api/v1/skill/refine/trigger` | 手动触发提炼 |
| POST | `/api/v1/skill/evolution/trigger` | 手动触发进化 |
| GET | `/api/v1/skill/evolution/status` | 进化状态 |

### 8.2 ContextManager 返回扩展

```json
{
  "memory": [...],
  "knowledge": [...],
  "skills": [
    {
      "skill_id": "s-abc123",
      "name": "python_dependency_debug",
      "abstract": "Python 依赖冲突排查流程",
      "score": 0.87,
      "reward_score": 0.35,
      "source_knowledge_ids": ["k-def456", "k-ghi789"]
    }
  ]
}
```

---

## 9. 配置设计

```python
# CortexAlphaConfig 新增字段

# ── Skill 提炼 ──
skill_refine_enabled: bool = False         # 总开关
skill_collection_name: str = "skills"
skill_refine_min_knowledge: int = 3        # 最少 Knowledge 数才触发
skill_refine_interval_hours: int = 24      # 定期提炼间隔

# ── Skill 进化 ──
skill_evolution_enabled: bool = False
skill_evolution_patience: int = 3          # 早停耐心值
skill_evolution_max_changes: int = 2       # 每次最大变更数
skill_evolution_failure_pool_size: int = 200
```

**注意**: 两个开关都默认 `False`，需要 Phase 1 benchmark 验收后才启用。

---

## 10. 实施计划

### 10.1 前置条件 (Phase 1 验收)

在开始 Skill 层之前，必须完成：

- [ ] 建立 recall benchmark baseline (Recall@5 目标 >= 80%)
- [ ] 启用 TraceSplitter + Archivist (当前默认关闭)
- [ ] 积累足够的 ACTIVE Knowledge (至少 20+ 条)

### 10.2 实施阶段

| 阶段 | 内容 | 新建/修改 |
|------|------|-----------|
| **Phase A** | 数据模型 + 存储 | types.py + collection_schemas.py + skill_store.py + config.py |
| **Phase B** | 提炼管道 | skill_refiner.py + prompts.py + orchestrator 集成 |
| **Phase C** | API + 检索集成 | models.py + server.py + context/manager.py |
| **Phase D** | 进化 Designer | skill_designer.py + 定期任务 |
| **Phase E** | 测试 | 单元 + 集成测试 |

### 10.3 文件变更清单

**新建 (3 核心)**:

| 文件 | 说明 |
|------|------|
| `src/opencortex/alpha/skill_refiner.py` | 从 Knowledge 合成 Skill |
| `src/opencortex/alpha/skill_store.py` | Skill 持久化 (复用 KnowledgeStore 模式) |
| `src/opencortex/alpha/skill_designer.py` | 进化 Designer + CaseCollector + SnapshotManager |

**修改 (6)**:

| 文件 | 变更 |
|------|------|
| `src/opencortex/alpha/types.py` | +SkillStatus, Skill, FailureCase |
| `src/opencortex/config.py` | +skill 配置字段 |
| `src/opencortex/prompts.py` | +3 个 prompt (synthesize, analysis, refinement) |
| `src/opencortex/storage/collection_schemas.py` | +skill_collection schema |
| `src/opencortex/orchestrator.py` | +skill 组件初始化 + 新方法 |
| `src/opencortex/http/server.py` | +9 个端点 |
| `src/opencortex/context/manager.py` | +_skill_search() |

---

## 11. 风险与控制

| 风险 | 控制 |
|------|------|
| Knowledge 层本身不稳定 | 前置条件: Phase 1 benchmark 验收后才启用 |
| 进化退化 | EvolutionSnapshotManager 快照回滚 |
| 反馈信号不准 | session outcome 作为延迟信号 (非 cited = +reward) |
| CaseCollector 重启丢失 | 持久化到 Qdrant (v1 缺陷修复) |
| Knowledge 与 Skill 重叠 | 清晰边界: Knowledge = 碎片事实/规则, Skill = 可操作组合能力 |
| LLM 成本 | 定期批量 (非每次 session), 仅在 Knowledge 累积后触发 |
