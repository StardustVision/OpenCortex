# ACE (Agentic Context Engine) — 设计提案 v2

> 状态: RFC (Request for Comments)
> 日期: 2026-02-27
> 参考: [kayba-ai/agentic-context-engine](https://github.com/kayba-ai/agentic-context-engine)

---

## 1. 设计原则

v1 设计过于复杂（Q-learning + Trajectory + Error KB + Experience Pool），实际上主流 agent 自学习框架（Voyager、ExpeL、kayba-ai ACE）都不需要经典 RL。

**v2 核心简化:**

- **Skillbook 模式** — 中央可变技能库，替代 Q-table + Experience Pool + Error KB
- **三角色分离** — Reflector（分析）、SkillManager（决策）、Skillbook（存储）
- **增量操作** — ADD / UPDATE / TAG / REMOVE，避免全量重写导致的"简短偏差"
- **零权重更新** — 纯 in-context learning，所有知识存为可检索文本
- **LLM 驱动** — 反思和技能管理由 LLM prompt 完成，不需要自定义算法

---

## 2. 架构

```
Agent 交互结果 (reasoning + answer + feedback)
    │
    ▼
┌──────────────────────────────────────────────────────┐
│                    ACE Engine                        │
│                                                      │
│  ┌──────────┐  ┌──────────────────────┐              │
│  │ Reflector│  │    SkillManager      │              │
│  │          │──│                      │              │
│  │ 分析结果 │  │ 生成 ADD/UPDATE/     │              │
│  │ 提取洞察 │  │ TAG/REMOVE 操作      │              │
│  └──────────┘  └──────────┬───────────┘              │
│                           │                          │
│  ┌────────────────────────▼───────────────────────┐  │
│  │              Skillbook (双写)                   │  │
│  │                                                 │  │
│  │  Qdrant ("ace" collection)    VikingFS          │  │
│  │  ┌────────────────────┐   ┌──────────────────┐  │  │
│  │  │ L0 content 向量化  │   │ .abstract.md (L0)│  │  │
│  │  │ abstract 标量字段  │   │ .overview.md (L1)│  │  │
│  │  │ tags / metadata    │   │ content.md   (L2)│  │  │
│  │  │ → 快速搜索         │   │ .relations.json  │  │  │
│  │  └────────────────────┘   │ → 持久化+层级检索│  │  │
│  │                           └──────────────────┘  │  │
│  └─────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────┘
         │
         ▼
   MemoryOrchestrator._hooks
```

**对比 v1:**

| 组件 | v1 (太复杂) | v2 (Skillbook) |
|------|------------|----------------|
| Q-Learner | 单独子系统 | 删除 — TAG 计数替代 |
| Experience Pool | 单独子系统 + LLM 洞察提取 | 删除 — Reflector 直接提取 |
| Trajectory Tracker | 单独子系统 | 删除 — 简化为上下文缓冲 |
| Error-Fix KB | 单独子系统 | 合并到 Skillbook (section="error_fixes") |
| Semantic Memory | 单独子系统 | 合并到 Skillbook (section="general") |
| 三因子评分 | 复杂公式 | TAG 计数 (helpful/harmful) + recency |

---

## 3. 数据模型

### 3.1 Skill — 三层上下文结构

借鉴 OpenViking 的 L0/L1/L2 设计，每个 Skill 天然具备三层精度:

```python
@dataclass
class Skill:
    id: str                          # 自动生成: "{section_prefix}-{N:05d}"
    section: str                     # 分类: "strategies", "error_fixes", "patterns", "general"
    content: str                     # L0: 技能内容 (祈使句, <20 字)
    justification: Optional[str]     # L1 组成: 为什么添加
    evidence: Optional[str]          # L1 组成: 具体证据
    helpful: int = 0                 # L1 组成: 被标记为有帮助的次数
    harmful: int = 0                 # L1 组成: 被标记为有害的次数
    neutral: int = 0                 # L1 组成: 被标记为中性的次数
    status: str = "active"           # "active" | "invalid"
    created_at: str = ""             # ISO 时间戳
    updated_at: str = ""             # ISO 时间戳
    # L2 (完整 trace) 不存储在 Skill 对象中，仅存于 VikingFS
```

**三层映射:**

| 层 | 内容 | 存储位置 | Token 成本 | 用途 |
|---|---|---|---|---|
| **L0** | `content` — 祈使句 <20 字 | Qdrant `abstract` 字段 + VikingFS `.abstract.md` | 极低 (~20-50 tokens) | 向量检索 + 注入 agent prompt |
| **L1** | `justification` + `evidence` + tag 计数 | VikingFS `.overview.md` | 低 (~100-200 tokens) | Agent 需要理解 WHY 时按需拉取 |
| **L2** | 完整 Reflector 分析 + 原始执行 trace | VikingFS `content.md` | 高 (不限) | 审计/调试/深度学习 |

**双写存储**: Qdrant (L0 向量 + 标量字段，用于快速搜索) + VikingFS (L0/L1/L2 完整持久化，用于层级检索)。这与现有记忆节点的存储模式完全一致。

### 3.2 Skillbook

```python
class Skillbook:
    """技能库 — Skill 的集合，支持增量更新操作。"""

    def add_skill(self, section, content, **kwargs) -> Skill
    def update_skill(self, skill_id, content=None, **kwargs) -> Skill
    def tag_skill(self, skill_id, tag: Literal["helpful","harmful","neutral"], increment=1)
    def remove_skill(self, skill_id)
    def get_by_section(self, section) -> List[Skill]
    def search(self, query, limit=5) -> List[Skill]      # 语义检索
    def as_prompt(self) -> str                             # LLM-友好格式输出
    def stats(self) -> Dict                                # 统计信息
```

### 3.3 VikingFS 存储 — Skill 即上下文节点

每个 Skill 以标准上下文节点的形式存入 VikingFS，复用现有的文件系统层级和检索管线。

**URI 结构:**

```
opencortex://tenant/{t}/user/{u}/skillbooks/
  strategies/
    .abstract.md             ← Section L0: "12 条策略技能，覆盖 API、错误处理、验证"
    .overview.md             ← Section L1: "## 快速导航\n- API (5 条)\n- 错误处理 (4 条)..."
    strat-00001/
      .abstract.md           ← L0: "对事实性问题直接给出答案，不做多余解释"
      .overview.md           ← L1: "## 依据\n从3次成功执行中提取\n## 证据\nAPI响应准确率100%\n## 标签\nhelpful: 5 | harmful: 0"
      content.md             ← L2: 完整 Reflector 分析 + 原始执行 trace
      .relations.json        ← 关联到衍生此 Skill 的记忆节点
    strat-00002/
      ...
  error_fixes/
    error-00001/
      .abstract.md           ← L0: "JSON 解析失败时先校验 UTF-8 编码"
      .overview.md           ← L1: "## 错误\nJSONDecodeError at line 42\n## 修复\n..."
      content.md             ← L2: 完整错误堆栈 + 修复过程
  patterns/
    ...
  general/
    ...
```

**关键收益:**

1. **参与层级检索** — Skills 和普通记忆节点一样被 `HierarchicalRetriever` 发现，全局预搜索匹配 L0+L1，按需下探 L2
2. **Section 级导航** — `strategies/` 目录本身有 `.abstract.md` 和 `.overview.md`，检索器可以先匹配 section 再下探到具体 skill
3. **跨引用** — `.relations.json` 链接 Skill 到产生它的记忆/会话，形成学习溯源图
4. **标准 API** — 用 `viking_fs.write_context(uri, content=L2, abstract=L0, overview=L1)` 一次写入三层
5. **read_batch 兼容** — `viking_fs.read_batch(skill_uris, level="l0")` 批量读取 Skill 摘要，注入 prompt 时极省 token

**写入流程 (ADD 操作):**

```python
async def _persist_skill(self, skill: Skill, trace: str = ""):
    """三层持久化: Qdrant + VikingFS"""
    uri = f"{self._skillbook_prefix}/{skill.section}/{skill.id}"

    # 1. VikingFS 三层写入
    overview = self._build_overview(skill)  # justification + evidence + tags
    await self._fs.write_context(
        uri=uri,
        abstract=skill.content,              # L0
        overview=overview,                    # L1
        content=trace,                        # L2 (完整执行 trace)
        is_leaf=True,
    )

    # 2. Qdrant 向量写入 (L0 做 embedding)
    vector = await self._embedder.embed(skill.content)
    await self._storage.upsert(self._collection, {
        "id": skill.id,
        "uri": uri,
        "abstract": skill.content,
        "context_type": "ace_skill",
        "type": skill.section,
        "vector": vector,
        "active_count": skill.helpful + skill.harmful + skill.neutral,
        "is_leaf": True,
    })
```

**Section 摘要自动生成:**

当 section 内的 Skills 变化时，自动更新 section 目录的 L0/L1:

```python
async def _update_section_summary(self, section: str):
    """更新 section 目录级摘要 (L0 + L1)。"""
    skills = await self.get_by_section(section)
    section_uri = f"{self._skillbook_prefix}/{section}"

    # L0: 一句话统计
    abstract = f"{len(skills)} 条{section}技能"

    # L1: 技能列表概览
    overview = f"# {section}\n\n"
    for s in skills:
        tag_info = f"helpful:{s.helpful} harmful:{s.harmful}"
        overview += f"- **{s.id}**: {s.content} ({tag_info})\n"

    await self._fs.write_context(
        uri=section_uri, abstract=abstract, overview=overview,
    )
```

### 3.4 增量更新操作

```python
@dataclass
class UpdateOperation:
    type: Literal["ADD", "UPDATE", "TAG", "REMOVE"]
    section: str                     # 目标分类
    content: Optional[str]           # ADD/UPDATE 的新内容
    skill_id: Optional[str]          # UPDATE/TAG/REMOVE 的目标 ID
    metadata: Dict[str, int]         # TAG: {"helpful": 1} 等
    justification: Optional[str]
    evidence: Optional[str]
```

**防止简短偏差的关键**:
- ADD 前必须检查是否有语义重复的已有 Skill，重复则用 UPDATE
- content 必须是原子性的（不含 "and/also"），否则拆分为多个 ADD
- evidence 字段必填 — 无证据 = 无技能

---

## 4. 三角色管线

### 4.1 Reflector

**输入**: question + agent reasoning + answer + feedback (success/fail) + 相关 Skills

**输出**: `ReflectorOutput`

```python
@dataclass
class ReflectorOutput:
    reasoning: str                           # 完整分析
    error_identification: str                # "none" 或具体错误
    root_cause_analysis: str                 # 根因
    key_insight: str                         # 最重要的洞察
    extracted_learnings: List[Learning]      # 提取的可复用学习
    skill_tags: List[SkillTag]              # 对现有 Skill 的标记
```

```python
@dataclass
class Learning:
    learning: str            # 祈使句, <20 字
    evidence: str            # 必填: 具体证据
    justification: str       # 为什么这是通用模式

@dataclass
class SkillTag:
    skill_id: str
    tag: Literal["helpful", "harmful", "neutral"]
```

**Prompt 要点** (借鉴 kayba-ai):
1. 优先诊断协议: SUCCESS → CALCULATION_ERROR → STRATEGY_MISAPPLICATION → WRONG_STRATEGY → MISSING_STRATEGY
2. 每条 learning 必须有具体证据，禁止空泛建议（"注意边界情况"）
3. 祈使句格式（"直接回答事实性问题"），禁止第三人称观察

### 4.2 SkillManager

**输入**: ReflectorOutput + 当前 Skillbook 状态 + 上下文

**输出**: `List[UpdateOperation]`

**决策逻辑** (LLM prompt):
1. 遍历 `extracted_learnings`
2. 对每条 learning，检查 Skillbook 中是否有语义相似的 Skill
3. 如有 → UPDATE 现有 Skill（合并内容/更新证据）
4. 如无 → ADD 新 Skill
5. 应用所有 `skill_tags`（TAG 操作）
6. harmful 计数远超 helpful 的 Skill → 考虑 REMOVE

**UPDATE 优先于 ADD** — 这是防止 Skillbook 膨胀的关键机制。

### 4.3 管线执行

```python
async def learn_from_feedback(self, question, reasoning, answer, feedback):
    """完整学习管线: Reflect → Manage → Apply → Persist"""

    # 1. 检索相关 Skills 作为上下文 (L0 级别，省 token)
    relevant_skills = await self._skillbook.search(question, limit=10)

    # 2. Reflector 分析
    reflection = await self._reflector.reflect(
        question=question,
        reasoning=reasoning,
        answer=answer,
        feedback=feedback,
        skills=relevant_skills,
    )

    # 3. SkillManager 生成操作
    operations = await self._skill_manager.decide(
        reflection=reflection,
        skillbook=self._skillbook,
        context=question,
    )

    # 4. 应用操作到 Skillbook (Qdrant + VikingFS 双写)
    #    - ADD/UPDATE: L0 写 Qdrant + VikingFS，L1 写 VikingFS，
    #                  L2 (完整 trace) 写 VikingFS content.md
    #    - TAG: 更新 Qdrant 标量字段 + VikingFS L1 (.overview.md)
    #    - REMOVE: 两边同步删除
    trace = self._build_trace(question, reasoning, answer, feedback, reflection)
    for op in operations:
        await self._skillbook.apply(op, trace=trace)

    # 5. 更新受影响 section 的目录级摘要
    affected_sections = {op.section for op in operations}
    for section in affected_sections:
        await self._skillbook.update_section_summary(section)
```

**检索时的层级利用:**

```python
# 注入 agent prompt 时 — 只读 L0，极省 token
skills_for_prompt = await viking_fs.read_batch(skill_uris, level="l0")
# → ["直接回答事实性问题", "JSON解析失败先校验UTF-8", ...]

# Agent 需要理解某条 Skill 的原因 — 读 L1
overview = await viking_fs.overview(skill_uri)
# → "## 依据\n从3次成功执行中提取\n## 证据\n..."

# 调试/审计 — 读 L2
full_trace = await viking_fs.read_file(f"{skill_uri}/content.md")
# → 完整 Reflector 分析 + 原始执行日志
```

---

## 5. 集成到 OpenCortex

### 5.1 文件结构

```
src/opencortex/ace/
  __init__.py                # 导出 ACEngine
  engine.py                  # ACEngine (实现 HooksProtocol)
  skillbook.py               # Skillbook + Skill 数据模型
  reflector.py               # Reflector (LLM prompt)
  skill_manager.py           # SkillManager (LLM prompt)
  types.py                   # UpdateOperation, ReflectorOutput 等
  prompts.py                 # Prompt 模板
```

**6 个文件，对比 v1 的 8 个文件。**

### 5.2 ACEngine — HooksProtocol 映射

ACEngine 实现现有的 `_hooks` 协议，映射到 Skillbook 操作:

```python
class ACEngine:
    def __init__(self, storage, embedder, viking_fs, llm_fn=None,
                 tenant_id="default", user_id="default"):
        # opencortex://tenant/{t}/user/{u}/skillbooks/
        prefix = f"opencortex://tenant/{tenant_id}/user/{user_id}/skillbooks"
        self._skillbook = Skillbook(storage, embedder, viking_fs, prefix)
        self._reflector = Reflector(llm_fn) if llm_fn else None
        self._skill_manager = SkillManager(llm_fn) if llm_fn else None
```

| HooksProtocol 方法 | Skillbook 映射 |
|---|---|
| `learn(state, action, reward)` | 触发完整管线: Reflect → Manage → Apply。无 LLM 时退化为 TAG 现有 Skills |
| `remember(content, type)` | `skillbook.add_skill(section=type, content=content)` |
| `recall(query, limit)` | `skillbook.search(query, limit)` |
| `trajectory_begin/step/end` | 累积到上下文缓冲，`end` 时触发 `learn()` |
| `error_record(error, fix)` | `skillbook.add_skill(section="error_fixes", content=fix, evidence=error)` |
| `error_suggest(error)` | `skillbook.search(error, limit=5)` 过滤 section="error_fixes" |
| `stats()` | `skillbook.stats()` |

### 5.3 Storage SONA Mixin

现有 orchestrator 通过 `hasattr` 检测的 4 个方法，映射到 Skillbook:

| 方法 | 映射 |
|---|---|
| `update_reward(col, id, reward)` | 对记忆对应的 Skill 执行 TAG (reward > 0 → helpful, < 0 → harmful) |
| `apply_decay()` | 清理长期未更新且 harmful > helpful 的 Skills |
| `set_protected(col, id, protected)` | Skill 标记为 `status="protected"` (不被 decay 清理) |
| `get_profile(col, id)` | 返回 Skill 的 helpful/harmful/neutral 计数 |

这些方法可以作为 mixin 添加到 QdrantStorageAdapter，或者直接在 ACEngine 中实现一个 wrapper。

### 5.4 Orchestrator 集成

```python
# orchestrator.py init()
if self._hooks is None:
    from opencortex.ace import ACEngine
    self._hooks = ACEngine(
        storage=self._storage,
        embedder=self._embedder,
        viking_fs=self._fs,              # VikingFS 实例
        llm_fn=self._llm_completion,     # 可选
        tenant_id=self._config.tenant_id,
    )
    await self._hooks.init()
```

**渐进降级**: 无 LLM 时，Reflector 和 SkillManager 不可用，但 `remember/recall/error_record/error_suggest` 仍然工作（直接 Skillbook CRUD）。

---

## 6. Prompt 设计摘要

### 6.1 Reflector Prompt (核心段)

```
你是一个任务分析器。分析以下执行结果，提取可复用的学习。

## 执行上下文
- 问题: {question}
- Agent 推理: {reasoning}
- 最终答案: {answer}
- 反馈: {feedback}

## 当前相关技能
{skills_excerpt}

## 诊断协议 (按优先级)
1. SUCCESS — 提取可复用模式，标记相关 Skill 为 helpful
2. CALCULATION_ERROR — 定位具体步骤和根因
3. STRATEGY_MISAPPLICATION — 策略正确但执行有误
4. WRONG_STRATEGY — 标记相关 Skill 为 harmful
5. MISSING_STRATEGY — 提取新学习

## 输出要求
- 每条 learning 必须 <20 字，祈使句格式
- evidence 必填，引用具体的推理步骤/数值/错误
- 禁止空泛建议: "注意边界情况"、"仔细验证"、"考虑各种情况"
```

### 6.2 SkillManager Prompt (核心段)

```
你是技能库管理器。根据 Reflector 的分析，决定对技能库的操作。

## 当前技能库
{skillbook_state}

## Reflector 分析
{reflection}

## 操作类型
- ADD: 添加新技能 (必须先检查是否与已有技能语义重复)
- UPDATE: 更新已有技能内容 (优先于 ADD)
- TAG: 标记已有技能 (helpful/harmful/neutral)
- REMOVE: 删除失效技能 (harmful >> helpful 且多次确认)

## 关键规则
1. UPDATE 优先于 ADD — 如果已有相似技能，合并而非新增
2. 每次 ADD 前引用最相似的已有技能，证明新技能确实不同
3. 原子性: 每条技能只表达一个概念，不含 "and/also"
```

---

## 7. 实现路线

### Phase 1: Skillbook 核心 (1 天)

- [ ] `types.py` — Skill, UpdateOperation, ReflectorOutput 数据类
- [ ] `skillbook.py` — Skillbook CRUD + 语义检索 + as_prompt()
- [ ] `engine.py` — ACEngine 壳 (无 LLM 模式: remember/recall/error_* 直接走 Skillbook)
- [ ] 单元测试: Skillbook CRUD + 检索

### Phase 2: Reflector + SkillManager (1-2 天)

- [ ] `prompts.py` — Reflector 和 SkillManager prompt 模板
- [ ] `reflector.py` — Reflector (LLM 调用 + 输出解析)
- [ ] `skill_manager.py` — SkillManager (LLM 调用 + 操作生成)
- [ ] `engine.py` — 完整 learn() 管线
- [ ] 集成测试: learn() 端到端

### Phase 3: Orchestrator 集成 (0.5 天)

- [ ] Orchestrator 自动创建 ACEngine
- [ ] SONA mixin (update_reward/apply_decay/set_protected/get_profile → Skillbook)
- [ ] MCP/HTTP 层验证
- [ ] E2E 测试

---

## 8. 对比

| 维度 | v1 | v2 |
|------|----|----|
| 子系统数 | 5 (Q-Learner, ExperiencePool, TrajectoryTracker, ErrorKB, SemanticMemory) | 3 (Reflector, SkillManager, Skillbook) |
| 文件数 | 8 | 6 |
| 需要 LLM | 部分子系统需要 | Reflector + SkillManager 需要，其余不需要 |
| 数据模型 | 5 种 (QEntry, Experience, Trajectory, ErrorFix, SemanticMemory) | 1 种 (Skill，含 L0/L1/L2 三层) |
| 存储 | 仅 Qdrant | Qdrant (搜索) + VikingFS (持久化 + 层级检索) |
| 学习算法 | Q-learning + ExpeL 洞察提取 + 三因子评分 | LLM 反思 → 增量操作 (ADD/UPDATE/TAG/REMOVE) |
| Token 效率 | 未设计 | L0 注入 prompt (~20 token/skill)，按需拉取 L1/L2 |
| 与现有管线复用 | 独立 collection | 标准上下文节点，复用 HierarchicalRetriever + VikingFS |
| 实现周期 | ~6 天 | ~3 天 |
| kayba-ai 验证 | 未验证 | 核心 Skillbook 模式已在生产中验证 |
