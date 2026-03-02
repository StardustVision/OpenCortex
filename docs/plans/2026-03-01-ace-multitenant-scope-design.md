# OpenCortex ACE 多租户私有/共享技能设计方案

## 1. 背景与问题

当前 ACE Skillbook 在多租户场景存在以下风险：

1. 隔离边界主要依赖 URI 约定，缺少字段级强约束。
2. ACE 初始化时固化 tenant/user，无法保证请求级身份生效。
3. Skill ID 使用 `section+counter`，在共享 collection 下存在跨用户覆盖风险。
4. 查询过滤以 `context_type` 为主，tenant/user/scope 维度约束不足。
5. HTTP Server 全生命周期共享单一 `MemoryOrchestrator` → `ACEngine` → `Skillbook` 实例，Skillbook 内部 `_counters`/`_skills` 等内存状态无租户维度隔离。

本方案目标是在不重构存储后端的前提下，建立可灰度、可回滚、可审计的多租户 ACE 共享机制。

## 2. 设计目标

1. 支持"可共享判定"能力：配置为 `true` 仅代表"允许共享候选"，不是"全部直写共享"。
2. 默认读取策略为"双读"：当前用户私有 + 当前租户共享。
3. 隔离边界基于结构化字段（`tenant_id/owner_user_id/scope`），URI 仅作组织路径。
4. 保持现有 API 兼容，支持平滑迁移。

## 3. 非目标

1. 不替换 Qdrant 或重写向量检索后端。
2. 不重构 Memory/Resource 的 URI 体系。
3. 不在本阶段实现复杂 ACL（角色/组织树）。

## 4. 配置设计

在 `CortexConfig` 新增配置项：

```python
share_skills_to_team: bool = False
skill_share_mode: str = "manual"  # "manual" | "auto_safe" | "auto_aggressive"
skill_share_score_threshold: float = 0.85
ace_scope_enforcement_enabled: bool = False
```

说明：

1. `share_skills_to_team`：仅表示"允许进入共享判定流程"，不代表直接共享写入。
2. `skill_share_mode`：
   - `manual`：只打候选标签，需通过审批 API 提升为 shared。
   - `auto_safe`：满足严格规则才自动提升。
   - `auto_aggressive`：满足基础规则即可提升（仅建议内测）。
3. `skill_share_score_threshold`：自动提升的最低分阈值。
4. `ace_scope_enforcement_enabled`：控制**写入防越权校验**（Phase E）是否启用。查询隔离（Phase D）不受此开关控制，一旦上线即生效。此开关仅用于在 Phase E 上线后提供回滚能力。

## 5. 数据模型与存储字段

### 5.1 Skill payload 新增字段

1. `tenant_id: str`（必填）
2. `owner_user_id: str`（必填，shared 技能保留原作者 ID 以便追溯）
3. `scope: str`（`private|shared|legacy`）
4. `id: str` 使用全局唯一 ID（`uuid4`）
5. `share_status: str`（`private_only|candidate|promoted|demoted|blocked`）
6. `share_score: float`（共享判定分）
7. `share_reason: str`（判定原因，如 `contains_secret`）

说明：

1. `id` 不再使用 `section+counter`，避免跨用户 upsert 覆盖。
2. `uri` 保留，用于层级组织与调试，不作为权限判定依据。
3. `owner_user_id` 在 shared 技能中保留原始作者 ID（不清空），用于审计追溯和降级操作。
4. `share_status` 新增 `demoted` 状态，支持从 `promoted` 回退（见 6.4）。

### 5.2 Qdrant payload 索引

Phase A 必须在 `skillbooks` collection 上创建以下 payload index，否则过滤查询退化为全量扫描：

```python
# 在 QdrantStorageAdapter 或 Skillbook.init 中执行
await client.create_payload_index(
    collection_name="skillbooks",
    field_name="tenant_id",
    field_schema=models.PayloadSchemaType.KEYWORD,
)
await client.create_payload_index(
    collection_name="skillbooks",
    field_name="owner_user_id",
    field_schema=models.PayloadSchemaType.KEYWORD,
)
await client.create_payload_index(
    collection_name="skillbooks",
    field_name="scope",
    field_schema=models.PayloadSchemaType.KEYWORD,
)
await client.create_payload_index(
    collection_name="skillbooks",
    field_name="share_status",
    field_schema=models.PayloadSchemaType.KEYWORD,
)
```

## 6. 作用域路由与读写策略

### 6.1 写入与共享提级策略

请求身份来源：`get_effective_identity()`。

写入规则（单记录提级模型）：

1. 所有新技能写入 `private`：
   - `tenant_id=t`
   - `owner_user_id=u`
   - `scope=private`
   - `share_status=private_only`
   - URI 前缀：`opencortex://{t}/user/{u}/skillbooks/...`
2. 仅当 `share_skills_to_team=True` 时，执行 `_should_promote_to_shared(skill, context)`。
3. 若判定通过，**原地更新**该记录的 `scope` 和 `share_status`（不创建副本）：
   - `scope=shared`
   - `share_status=promoted`（或 `candidate`，取决于 `skill_share_mode`）
   - URI 更新为：`opencortex://{t}/agent/skillbooks/...`
4. 判定失败则保留 `scope=private`，`share_status=blocked/private_only`，不进入团队池。

**为什么不做双写副本**：双写会引入 private/shared 两份数据的一致性问题（编辑同步、反馈归属、reward 分裂）。单记录提级通过更改 `scope` 字段实现，回滚只需改回 `scope=private`。

共享判定采用两段式：

1. 硬拦截（任一命中即禁止共享）：
   - 命中密钥/令牌/密码模式（正则：`(?i)(api[_-]?key|secret|token|password|credential)\s*[:=]`）。
   - 命中 PII（正则：邮箱 `\S+@\S+\.\S+`、手机号 `1[3-9]\d{9}`、身份证 `\d{17}[\dXx]`）。
   - 强环境绑定（正则：绝对路径 `/Users/|/home/|C:\\`、内网主机名 `\.\w+\.internal`）。
2. 软评分（`share_score`，范围 0.0-1.0）：

   ```python
   def _compute_share_score(skill: dict) -> float:
       score = 0.0
       content = skill.get("content", "")
       # 可泛化性 (0.4)：不含用户/环境特定引用
       env_refs = len(re.findall(r'(?i)(localhost|127\.0\.0\.1|/Users/|~\/)', content))
       score += 0.4 * max(0, 1 - env_refs * 0.2)
       # 可复用性 (0.3)：有正向反馈信号
       helpful = skill.get("helpful", 0)
       score += 0.3 * min(1.0, helpful / 3.0)
       # 可执行性 (0.3)：步骤完整（有动作动词、有条件判断）
       has_actions = bool(re.search(r'(?i)(run|execute|create|update|delete|check|verify)', content))
       has_conditions = bool(re.search(r'(?i)(if|when|before|after|unless)', content))
       score += 0.3 * (0.5 * has_actions + 0.5 * has_conditions)
       return round(score, 3)
   ```

提级规则：

1. `manual`：`share_status=candidate`，不自动提级。等待审批 API 确认（见 6.5）。
2. `auto_safe`：必须"无硬拦截 + score>=threshold + helpful>=2"。
3. `auto_aggressive`：必须"无硬拦截 + score>=threshold"。

### 6.2 读取策略（默认双读）

统一过滤条件：

```json
{
  "op": "and",
  "conds": [
    {"op": "must", "field": "context_type", "conds": ["ace_skill"]},
    {"op": "must", "field": "tenant_id", "conds": ["{t}"]},
    {
      "op": "or",
      "conds": [
        {"op": "must", "field": "scope", "conds": ["shared"]},
        {
          "op": "and",
          "conds": [
            {"op": "must", "field": "scope", "conds": ["private"]},
            {"op": "must", "field": "owner_user_id", "conds": ["{u}"]}
          ]
        }
      ]
    }
  ]
}
```

**Filter 兼容性**：已验证 `filter_translator.py` 的 `and` handler（L35-49）会将子 `or` 结果包装为 `models.Filter(should=child.should)` 嵌入 `must` 列表，`or` handler（L51-61）会将多 `must` 子条件包装为 `models.Filter(must=[...])`。上述三层嵌套结构可正确翻译为 Qdrant 原生 Filter。

### 6.3 去重与冲突策略

1. 提级前先做语义去重查询：以待提级 skill 的向量在 `scope=shared, tenant_id=t` 范围内搜索，**相似度 >= 0.92** 视为已有同义技能。
2. 命中同义技能时执行 TAG/UPDATE（合并 evidence、取较高 share_score），不创建新 shared 记录。
3. 双读返回结果按 `id` 去重（同一 skill 不会同时出现 private 和 shared，因为是原地提级）。
4. 并发提级竞态：由 Qdrant 的 point upsert 幂等性保证——先完成的写入生效，后到的 upsert 基于相同 id 覆盖为最终状态。

### 6.4 降级与召回机制

`share_status` 状态流转：

```
private_only ──→ candidate ──→ promoted ──→ demoted
                    │              │            │
                    ↓              ↓            ↓
                 blocked        blocked      private_only
```

降级触发条件：

1. **负反馈累积**：`harmful >= 3` 且 `harmful > helpful * 2` 时自动降级。
2. **管理员手动降级**：通过审批 API 将 `promoted` 改为 `demoted`。
3. **事后发现敏感内容**：硬拦截规则更新后，重新扫描 shared 技能，命中者标记 `blocked`。

降级操作：将 `scope` 改回 `private`，`share_status` 设为 `demoted`，恢复 `owner_user_id` 对应的私有 URI。

### 6.5 审批 API（`manual` 模式）

新增 HTTP 端点：

```
POST /api/v1/skills/review
```

请求体：

```json
{
  "skill_id": "uuid",
  "action": "approve" | "reject",
  "reason": "optional review note"
}
```

行为：

1. `approve`：将 `share_status` 从 `candidate` 更新为 `promoted`，`scope` 更新为 `shared`。
2. `reject`：将 `share_status` 更新为 `blocked`，保持 `scope=private`。
3. 权限：仅允许同 `tenant_id` 下的请求操作。

辅助查询端点：

```
GET /api/v1/skills/candidates?tenant_id={t}
```

返回当前租户下所有 `share_status=candidate` 的技能列表，供审批者浏览。

## 7. 安全边界与校验

### 7.1 请求身份校验

1. 入口层校验 `X-Tenant-ID/X-User-ID` 格式（与 `UserIdentifier` 一致）。
2. 非法身份直接拒绝请求。

### 7.2 写入 URI 属主校验

对显式传入 URI（如 `memory_store`）做强校验：

1. URI 的 tenant 必须等于当前请求 tenant。
2. private 写入仅允许当前用户私有路径。
3. shared 写入仅允许当前租户共享路径。
4. 任一不满足返回 4xx（建议 403/422）。

## 8. 代码改造计划

### Phase A: 配置、字段与索引（低风险）

涉及文件：

1. `src/opencortex/config.py`
2. `src/opencortex/ace/skillbook.py`
3. `src/opencortex/storage/qdrant/adapter.py`

改动：

1. 新增配置项（`share_skills_to_team` 等 4 项）。
2. Skill 持久化新增 `tenant_id/owner_user_id/scope/share_*` 字段。
3. Skill ID 切换为 `uuid4`，移除 `_counters` 内存计数器。
4. 在 `skillbooks` collection 创建 `tenant_id`、`owner_user_id`、`scope`、`share_status` 四个 keyword payload index（见 5.2）。

### Phase B: 请求级身份生效与实例策略（中风险）

涉及文件：

1. `src/opencortex/ace/engine.py`
2. `src/opencortex/ace/skillbook.py`
3. `src/opencortex/orchestrator.py`

改动：

1. **Skillbook 去状态化**：移除 `_skills: Dict`、`_counters: Dict` 等内存状态，所有读写直接走 Qdrant。Skillbook 变为无状态服务，可安全被多租户并发使用。
2. Skillbook 方法（`add/search/get_by_section/tag`）新增 `tenant_id`、`user_id` 参数，不再依赖初始化时固化的 prefix。
3. ACEngine hook 方法（`remember/recall/learn` 等）新增 `tenant_id`、`user_id` 参数，传递给 Skillbook。
4. **Orchestrator hook 委派层**（`hooks_learn/hooks_remember/hooks_recall` 等 9 个方法）：每个方法调用 `get_effective_identity()` 获取当前请求身份，传递给 ACEngine。

### Phase C: 共享判定引擎（中风险）

涉及文件：

1. `src/opencortex/ace/engine.py`
2. `src/opencortex/ace/skillbook.py`

改动：

1. 新增 `_hard_block_check(content: str) -> tuple[bool, str]`：正则硬拦截（见 6.1 正则列表）。
2. 新增 `_compute_share_score(skill: dict) -> float`：确定性评分（见 6.1 算法）。
3. 新增 `_should_promote_to_shared(skill, context) -> tuple[bool, str]`：组合硬拦截 + 软评分 + 模式分流。
4. 在 skill 写入流程中集成判定，写入 `share_status/share_score/share_reason`。

### Phase D: 查询隔离（中风险）

涉及文件：

1. `src/opencortex/ace/skillbook.py`

改动：

1. `search/get_by_section/stats` 使用字段过滤实现 tenant/user/scope 隔离。
2. 默认启用双读策略（private + shared）。
3. 此阶段上线后查询隔离**立即生效**，不受 `ace_scope_enforcement_enabled` 控制。

### Phase E: 写入防越权（中高风险）

涉及文件：

1. `src/opencortex/orchestrator.py`
2. `src/opencortex/http/server.py`

改动：

1. 显式 URI 属主校验。
2. 非法路径写入拒绝。
3. 受 `ace_scope_enforcement_enabled` 开关控制，关闭时仅 log warning 不拒绝。

### Phase F: 审批 API 与降级机制（中风险）

涉及文件：

1. `src/opencortex/http/server.py`
2. `src/opencortex/orchestrator.py`
3. `src/opencortex/ace/skillbook.py`

改动：

1. 新增 `POST /api/v1/skills/review` 和 `GET /api/v1/skills/candidates` 端点。
2. Skillbook 新增 `promote/demote/list_candidates` 方法。
3. 降级逻辑集成到 feedback 流程（负反馈累积触发自动降级）。

### Phase G: 迁移与灰度（中风险）

涉及文件：

1. `scripts/*`（新增迁移脚本）
2. `docs/*`（操作手册）

改动：

1. 历史数据回填策略：
   - 从现有 URI 解析 tenant_id 和 user_id（格式 `opencortex://{tenant}/user/{uid}/...`）。
   - 解析成功：填充 `tenant_id`、`owner_user_id`、`scope=private`。
   - 解析失败或 URI 为空：填充 `tenant_id=__legacy__`、`owner_user_id=__unknown__`、`scope=legacy`。
2. `scope=legacy` 的数据在双读 filter 中不被匹配（filter 仅查 `private`/`shared`）。
3. 提供 CLI 命令 `oc-cli migrate assign-owner --uri-pattern ... --owner ...` 供管理员手动归属遗留数据。归属后将 `scope` 从 `legacy` 改为 `private`。

## 9. 测试计划

### 9.1 新增测试

新增测试文件：`tests/test_ace_multitenant_scope.py`

核心用例：

1. private 写入仅本人可见。
2. shared 写入租户内可见、跨租户不可见。
3. 双读命中并去重正确。
4. `share_skills_to_team=True` 但命中硬拦截时，不写 shared。
5. `manual` 模式只产出 `candidate`，不自动共享。
6. `auto_safe` 需满足阈值与 helpful>=2，才共享。
7. 非法 URI 写入被拒绝（`ace_scope_enforcement_enabled=True`）。
8. skill_id 全局唯一（uuid4），不发生覆盖。
9. 审批 API：approve 将 candidate 提级为 promoted+shared。
10. 审批 API：reject 将 candidate 标记 blocked。
11. 降级：negative feedback 累积触发自动降级。
12. 降级：promoted → demoted 恢复 private scope。
13. 语义去重：相似度 >= 0.92 的 skill 提级时合并而非新建。
14. share_score 计算：硬拦截正则覆盖率测试。
15. share_score 计算：各维度权重正确性测试。

### 9.2 现有测试回归

以下现有测试文件会因字段变更、ID 格式变更、filter 变更而受影响，需同步修改：

1. `tests/test_ace_phase1.py`（21 个）：skill ID 格式从 `section-counter` 改为 uuid4，`add/search/tag` 接口新增 tenant_id/user_id 参数。
2. `tests/test_ace_phase2.py`（17 个）：同上，涉及 Skillbook 方法签名变更。
3. `tests/test_integration_skill_pipeline.py`（10 个）：Qdrant 集成测试需适配新 payload 字段和索引。
4. `tests/test_skill_search_fusion.py`（11 个）：搜索 filter 变更，需注入 tenant_id/user_id。
5. `tests/test_multi_tenant.py`：补强"跨租户 skill 查询不可见性"断言。

### 9.3 测试策略

- Phase A/B 完成后先跑回归，确保现有测试在新接口下全部通过。
- Phase C/D 完成后运行新增多租户测试。
- Phase E/F 完成后运行审批与降级测试。

## 10. 验收标准

1. 安全：跨租户查询结果为 0；越权写入拒绝率 100%。
2. 正确：双读召回符合预期，且无重复覆盖。
3. 共享质量：敏感技能共享漏出率为 0，候选提级精度达到预设目标。
4. 兼容：默认行为与当前版本一致（private 写入）。
5. 可观测：日志可按 `tenant_id/user_id/scope` 追踪。

## 11. 灰度与回滚

灰度顺序：

1. Phase A/B：字段写入 + Skillbook 去状态化上线。回滚方式：代码回滚，新字段被忽略不影响旧逻辑。
2. Phase D：查询隔离上线。**此阶段不可通过配置回滚**，需代码回滚。因此上线前必须通过全量回归测试。
3. Phase E：写入强校验上线（打开 `ace_scope_enforcement_enabled`）。回滚方式：关闭开关回退宽松模式。

回滚策略：

1. 关闭 `ace_scope_enforcement_enabled` 回退写入宽松模式（仅 Phase E）。
2. Phase D 的查询隔离无配置开关——如需回滚则代码回滚。设计上查询隔离是安全增强（缩小返回范围），不会破坏已有写入数据。
3. 数据层只增不删，避免回滚损坏。

## 12. 工期评估

1. Phase A（配置/字段/索引）+ Phase B（去状态化/身份传递）：3-4 天
2. Phase C（共享判定引擎）+ Phase D（查询隔离）：3-4 天
3. Phase E（写入防越权）+ Phase F（审批 API/降级）：2-3 天
4. Phase G（迁移脚本/灰度）：2 天
5. 现有测试回归修复：2-3 天
6. 新增测试编写：2-3 天
7. 灰度验证与修复：1-2 天
8. 合计：15-19 天

## 13. 风险与缓解

1. 风险：历史数据缺少 owner 信息。
   - 缓解：从 URI 解析归属；解析失败标记 `legacy`（双读不可见）；提供 CLI 命令手动归属。
2. 风险：prefix 过滤与字段过滤并存导致行为不一致。
   - 缓解：权限判定仅认字段，prefix 仅做展示。
3. 风险：并发提级下语义重复。
   - 缓解：提级前以向量相似度 >= 0.92 去重查询；Qdrant upsert 幂等性兜底。
4. 风险：Skillbook 去状态化后性能退化（每次操作都走 Qdrant 而非内存）。
   - 缓解：通过 payload index 保证 filter 查询性能；热点操作（如 section 计数）改为 Qdrant count API。
5. 风险：share_score 评分算法精度不足，初期可能产生大量误判。
   - 缓解：初期仅使用 `manual` 模式，积累 candidate 数据后调参再开启 `auto_safe`。
6. 风险：Phase D 查询隔离无配置开关，上线后无法快速回滚。
   - 缓解：Phase D 上线前必须通过全量回归测试 + staging 环境验证。查询隔离本质是缩小返回范围（安全方向），不会破坏数据。
