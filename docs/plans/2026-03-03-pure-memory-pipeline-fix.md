# Pure Memory Pipeline Fix — 移除 ACE 干扰，修复召回管道

## 根因诊断

通过代码走读，确认了两份测试报告的根因：

### 根因 1：context_type 过滤器漏网（P0 — 导致 case/pattern 类型完全不可召回）

**存储路径：**
- `memory_store(context_type="case")` → 记录存入 `"context"` 集合，`context_type="case"`
- `memory_store(context_type="pattern")` → 同理，`context_type="pattern"`

**搜索路径：**
- `IntentRouter._build_queries()` (intent_router.py:307) 生成 TypedQuery 只有 **3 种类型**：
  ```python
  types = [ContextType.MEMORY, ContextType.RESOURCE, ContextType.SKILL]
  ```
- HierarchicalRetriever (line 224) 对每个 TypedQuery 施加过滤：
  ```python
  type_filter = {"op": "must", "field": "context_type", "conds": [query.context_type.value]}
  ```
- **结果：** `context_type="case"` / `"pattern"` 的记录被过滤器永久排除

### 根因 2：Skillbook 搜索注入噪声（P1 — 导致无关结果占位）

- `search()` (orchestrator.py:869-870) 每次都并行搜索 `"skillbooks"` 集合
- 返回 ACE 自动提取的 skills + 系统内置 skill（如搜索流程描述 bba59987）
- 这些结果得分 0.37-0.48，占据返回名额，挤掉真正相关的结果

### 根因 3：hooks_remember/recall 走 Skillbook，不走 Context 集合

- `hooks_remember` → `ACEngine.remember()` → `Skillbook.add_skill()` → 存入 `"skillbooks"` 集合
- `hooks_recall` → `ACEngine.recall()` → `Skillbook.search()` → 只搜 `"skillbooks"` 集合
- 与 `memory_store/search` 的 `"context"` 集合完全隔离

### 根因 4：memory_store 的 fire-and-forget skill 提取（P2 — 部分复制，制造混淆）

- `add()` (orchestrator.py:612-613) 每次存储后触发 `_try_extract_skills()`
- RuleExtractor 从内容中提取 skill → 写入 `"skillbooks"` 集合
- 只有少量内容（报告 A 中的 P6/P7）被成功提取，其余丢失
- 制造了"部分可搜索"的假象

## 修改方案

### 原则：先让纯记忆存取 100% 可靠，ACE 作为独立可选能力

---

### Step 1: 移除搜索中的 context_type 过滤器

**文件：** `src/opencortex/retrieve/hierarchical_retriever.py`

**变更：** 将 retrieve() 中的 type_filter 改为可选。当搜索不限定具体类型时，不施加 context_type 过滤。

```python
# Before (line 224):
type_filter = {"op": "must", "field": "context_type", "conds": [query.context_type.value]}
filters_to_merge = [type_filter]

# After:
filters_to_merge = []
# Only apply type filter if an explicit type restriction was requested
if query.context_type not in (None, ContextType.MEMORY):
    type_filter = {"op": "must", "field": "context_type", "conds": [query.context_type.value]}
    filters_to_merge.append(type_filter)
```

但这样改 TypedQuery 需要支持 None context_type。**更简洁的方案**：在 orchestrator 层面只生成一个不带 type_filter 的查询。

**实际方案：** 在 `orchestrator.search()` 中，当无显式 context_type 限制时，生成**单个**不带类型过滤的 TypedQuery，而非 3 个分类型的。

**文件：** `src/opencortex/orchestrator.py` (search 方法, ~line 792-815)

```python
# Before:
types_to_search = [ContextType.MEMORY, ContextType.RESOURCE, ContextType.SKILL]
typed_queries = [TypedQuery(query=query, context_type=ct, ...) for ct in types_to_search]

# After: single query, no type restriction
typed_queries = [TypedQuery(query=query, context_type=ContextType.MEMORY, intent="", priority=1,
                            target_directories=[target_uri] if target_uri else [],
                            detail_level=dl)]
```

**同步修改 retriever：** 当 `context_type == ContextType.MEMORY` 时不加 type_filter（因为 MEMORY 是默认值/全局搜索含义）。或者引入一个 `ContextType.ANY` 值。

**最终方案：** 添加 `ContextType.ANY` 枚举值，retriever 见到 ANY 时跳过 type_filter。

---

### Step 2: IntentRouter 生成单一全局查询

**文件：** `src/opencortex/retrieve/intent_router.py`

**变更：** `_build_queries()` (line 296-332) 在无 context_type 限制时，生成单个 `ContextType.ANY` 查询。

```python
# Before:
types = [ContextType.MEMORY, ContextType.RESOURCE, ContextType.SKILL]

# After:
types = [ContextType.ANY]
```

trigger_categories 的额外查询保持不变（仍使用 MEMORY 类型）。

---

### Step 3: 移除 search() 中的 Skillbook 并行搜索

**文件：** `src/opencortex/orchestrator.py`

**变更：** 删除 `_search_skillbook()` 调用及 skill_contexts 合并逻辑（line 868-889）。

```python
# Delete these lines:
should_search_skills = self._hooks and (...)
skill_search_coro = self._search_skillbook(query, limit=3) if should_search_skills else None
if skill_search_coro:
    all_results = await asyncio.gather(*retrieval_coros, skill_search_coro)
    ...

# Replace with:
query_results = list(await asyncio.gather(*retrieval_coros))
```

---

### Step 4: 移除 add() 中的自动 Skill 提取

**文件：** `src/opencortex/orchestrator.py`

**变更：** 删除 `add()` 中的 `_try_extract_skills()` 调用（line 612-613）和对应方法。

```python
# Delete:
if self._rule_extractor and self._hooks and content:
    asyncio.create_task(self._try_extract_skills(abstract, content, tid, uid))
```

---

### Step 5: hooks_remember/recall 重定向到 Context 集合

**文件：** `src/opencortex/orchestrator.py`

**变更 hooks_remember：** 不再调用 `ACEngine.remember()`，改为直接调用 `self.add()`。

```python
async def hooks_remember(self, content: str, memory_type: str = "general") -> Dict[str, Any]:
    self._ensure_init()
    result = await self.add(
        abstract=content,
        content=content,
        category=memory_type,
        context_type="memory",
    )
    return {"success": True, "uri": result.uri, "section": memory_type}
```

**变更 hooks_recall：** 不再调用 `ACEngine.recall()`，改为调用 `self.search()`。

```python
async def hooks_recall(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
    self._ensure_init()
    result = await self.search(query=query, limit=limit)
    return [
        {"content": m.abstract, "uri": m.uri, "section": m.category,
         "score": m.score}
        for m in result
    ]
```

---

### Step 6: _aggregate_results 适配无类型查询

**文件：** `src/opencortex/orchestrator.py`

**变更：** `_aggregate_results()` 需要处理 `ContextType.ANY` — 按记录实际 context_type 分类。

`_convert_to_matched_contexts()` 在 retriever 中需要改用记录的实际 context_type 而非 query 的。

---

### Step 7: 更新 ContextType 枚举

**文件：** `src/opencortex/retrieve/types.py`

```python
class ContextType(str, Enum):
    MEMORY = "memory"
    RESOURCE = "resource"
    SKILL = "skill"
    CASE = "case"
    PATTERN = "pattern"
    STAGING = "staging"
    ANY = "any"  # 新增：全局搜索，不施加 type_filter
```

---

## 不动的部分

- ACEngine 本身不删除，保留 hooks_learn, trajectory, error_record/suggest 等功能
- Skillbook 集合保留（已有数据不丢失），但不再被 memory 管道引用
- RuleExtractor 保留代码，只是不再在 add() 中自动触发
- IntentRouter 的 should_recall 逻辑保留（Layer 1 keyword + Layer 2 LLM）
- Reranker 保留现有逻辑

## 涉及文件清单

| 文件 | 变更类型 |
|------|----------|
| `src/opencortex/retrieve/types.py` | 添加 `ContextType.ANY` |
| `src/opencortex/retrieve/intent_router.py` | `_build_queries()` 改用 ANY |
| `src/opencortex/retrieve/hierarchical_retriever.py` | retrieve() 跳过 ANY 的 type_filter |
| `src/opencortex/orchestrator.py` | search() 移除 skillbook；add() 移除 skill 提取；hooks_remember/recall 重定向 |
| tests (affected) | test_e2e_phase1, test_ace_phase1/2, test_skill_search_fusion |

## 预期效果

- memory_store 存入的任何 context_type (memory/case/pattern/resource/skill) 都能被 memory_search 找到
- hooks_remember 存入的内容能被 hooks_recall 和 memory_search 找到
- 搜索结果不再被 skillbook 噪声干扰
- 召回准确率从 0-25% → 应接近嵌入模型的理论上限
