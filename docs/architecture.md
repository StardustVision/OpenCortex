# OpenCortex 项目说明文档

> 版本：Phase 1 v0.2.0 | 更新日期：2026-02-26

## 1. 项目概述

OpenCortex 是一个面向 AI Agent 的**记忆与上下文管理系统**，核心目标：

- **节省 Token**：三层摘要体系（L0/L1/L2），检索时只返回必要的精度层级
- **自学习 Memory**：通过 SONA 强化排序，让高价值记忆自然上浮，低价值记忆自然衰减

项目从 [OpenViking](https://github.com/volcengine/openviking) 移植并重构，新增**租户级多用户隔离**和**可插拔向量存储后端**。

---

## 2. 系统架构

```
          External AI Agent (Claude Code / Cursor / 自定义)
                    │
                    │  MCP Protocol (stdio / SSE / HTTP)
                    ▼
┌─────────────────────────────────────────────────────────┐
│              FastMCP Server (mcp_server.py)              │
│  6 tools: store / search / feedback / stats / decay /   │
│           health                                        │
├─────────────────────────────────────────────────────────┤
│                    MemoryOrchestrator                    │
│  (统一 API: add / search / feedback / decay / protect)  │
├────────────┬──────────────┬─────────────┬───────────────┤
│ CortexConfig│  EmbedderBase │IntentAnalyzer│ RerankConfig │
│ (租户/用户) │  (可插拔嵌入) │ (LLM意图分析)│  (重排序)    │
├────────────┴──────┬───────┴─────────────┴───────────────┤
│                   │                                      │
│    VikingFS       │     HierarchicalRetriever            │
│  (三层文件系统)    │   (层级递归检索 + 分数传播)            │
│  L0=摘要          │                                      │
│  L1=概要          │                                      │
│  L2=全文          │                                      │
├───────────────────┴──────────────────────────────────────┤
│              VikingDBInterface (抽象接口)                  │
│              25 个 async 方法                              │
├──────────────────────┬───────────────────────────────────┤
│  RuVectorAdapter     │     InMemoryStorage (测试用)       │
│  (SONA 强化排序面)    │                                   │
│  + CLI / HTTP client │                                   │
└──────────────────────┴───────────────────────────────────┘
```

---

## 3. 目录结构

```
src/opencortex/
├── __init__.py                    # 包入口，导出 MemoryOrchestrator
├── __main__.py                    # python -m opencortex 入口
├── config.py                      # CortexConfig 全局配置（租户/用户/嵌入/RuVector/MCP）
├── orchestrator.py                # MemoryOrchestrator 顶层编排器
├── mcp_server.py                  # FastMCP v3 Server（6 个 MCP tools）
│
├── core/                          # 核心数据模型
│   ├── context.py                 # Context 统一上下文类
│   ├── message.py                 # Message 简单消息
│   └── user_id.py                 # UserIdentifier 用户身份（租户隔离）
│
├── models/                        # 模型抽象
│   └── embedder/
│       ├── base.py                # EmbedderBase / DenseEmbedderBase / HybridEmbedderBase
│       └── volcengine_embedders.py # 火山引擎嵌入模型实现
│
├── retrieve/                      # 检索模块
│   ├── hierarchical_retriever.py  # HierarchicalRetriever 层级递归检索
│   ├── intent_analyzer.py         # IntentAnalyzer LLM 意图分析
│   ├── rerank_config.py           # RerankConfig 重排序配置
│   └── types.py                   # ContextType / TypedQuery / FindResult 等类型
│
├── storage/                       # 存储层
│   ├── vikingdb_interface.py      # VikingDBInterface 抽象接口（25 个方法）
│   ├── viking_fs.py               # VikingFS 三层文件系统抽象
│   ├── local_agfs.py              # LocalAGFS 本地文件系统适配器
│   ├── local_fs.py                # 导入/导出工具
│   ├── collection_schemas.py      # 集合 Schema 定义
│   └── ruvector/                  # RuVector 后端适配器
│       ├── adapter.py             # RuVectorAdapter (VikingDBInterface + SONA)
│       ├── cli_client.py          # RuVectorCLI subprocess 封装
│       ├── http_client.py         # RuVectorHTTP HTTP 客户端
│       ├── filter_translator.py   # VikingDB 过滤 DSL → RuVector 翻译
│       └── types.py               # RuVectorConfig / SonaProfile / DecayResult
│
└── utils/                         # 工具
    ├── uri.py                     # CortexURI 租户隔离 URI 体系
    └── time_utils.py              # 时间格式化工具
```

---

## 4. 核心概念

### 4.1 三层上下文 (L0 / L1 / L2)

| 层级 | 用途 | 存储位置 | Token 消耗 |
|------|------|----------|-----------|
| **L0 摘要** | 一句话描述，用于向量检索 | VikingFS `.abstract.md` + 向量库 | 极低 |
| **L1 概要** | 段落级概要，用于初步判断 | VikingFS `.overview.md` | 低 |
| **L2 全文** | 完整内容，按需加载 | VikingFS `content.md` | 高 |

检索时先用 L0 向量匹配，按需逐层下探，避免加载完整内容浪费 Token。

### 4.2 租户隔离 URI

```
opencortex://tenant/{team_id}/...              # 共享（团队级）
opencortex://tenant/{team_id}/user/{uid}/...   # 私有（用户级）
```

**共享子作用域**（直接在 tenant 下）：
- `resources` — 团队资源
- `agent/skills` — Agent 技能
- `agent/memories/patterns` — 共享模式

**私有子作用域**（在 `/user/{uid}/` 下）：
- `memories` — 用户记忆（preferences / entities / events / profile）
- `agent/memories/cases` — 私有案例
- `reinforcement` — SONA 强化数据
- `feedback` — 反馈数据
- `workspace` — 工作区
- `session` — 会话数据

### 4.3 SONA 自学习强化排序

**公式**：`reinforced_score = similarity × (1 + α × reward_factor) × decay_factor`

| 操作 | 说明 |
|------|------|
| `feedback(uri, reward)` | 发送奖励信号，正值增强、负值惩罚 |
| `decay()` | 时间衰减，普通节点 rate=0.95，保护节点 rate=0.99 |
| `protect(uri)` | 标记重要记忆，降低衰减速率 |
| `get_profile(uri)` | 查看 SONA 行为画像 |

### 4.4 层级递归检索

```
1. 全局向量搜索 → 定位候选目录
2. 合并起始点（根目录 + 全局命中）
3. 递归搜索：按 parent_uri 深度遍历
   - 每层评分：向量相似度 or rerank
   - 分数传播：final = α × child_score + (1-α) × parent_score
   - 收敛检测：topk 连续 3 轮不变则停止
4. 返回 MatchedContext 列表（含关联上下文）
```

---

## 5. API 参考

### 5.1 MemoryOrchestrator

```python
from opencortex import MemoryOrchestrator, CortexConfig, init_config

# 初始化
init_config(CortexConfig(tenant_id="myteam", user_id="alice"))
orch = MemoryOrchestrator(embedder=my_embedder)
await orch.init()

# 添加记忆
ctx = await orch.add(
    abstract="用户偏好暗色主题",
    content="# 主题偏好\n所有编辑器使用暗色主题...",
    category="preferences",
)

# 搜索
result = await orch.search("用户喜欢什么主题？")
for m in result.memories:
    print(f"{m.uri}: {m.abstract} (score={m.score:.3f})")

# 反馈（强化）
await orch.feedback(uri=result.memories[0].uri, reward=1.0)

# 时间衰减
await orch.decay()

# 保护重要记忆
await orch.protect(uri="opencortex://tenant/myteam/user/alice/memories/...")

# 查看 SONA 画像
profile = await orch.get_profile(uri="...")
# → {"reward_score": 3.0, "retrieval_count": 5, "is_protected": True, ...}
```

### 5.2 完整方法列表

| 类别 | 方法 | 说明 |
|------|------|------|
| 生命周期 | `init()` | 初始化所有组件 |
| | `close()` | 关闭存储、释放资源 |
| | `health_check()` | 组件健康检查 |
| | `stats()` | 统计信息 |
| CRUD | `add(abstract, content, category, ...)` | 添加上下文 |
| | `update(uri, abstract, content, meta)` | 更新上下文 |
| | `remove(uri, recursive)` | 删除上下文 |
| 检索 | `search(query, context_type, limit, ...)` | 直接检索 |
| | `session_search(query, messages, ...)` | 会话感知检索（需 LLM） |
| SONA | `feedback(uri, reward)` | 发送奖励信号 |
| | `feedback_batch(rewards)` | 批量反馈 |
| | `decay()` | 触发时间衰减 |
| | `protect(uri, protected)` | 标记/取消保护 |
| | `get_profile(uri)` | 获取 SONA 行为画像 |

---

## 6. 配置

### 6.1 CortexConfig

```python
@dataclass
class CortexConfig:
    tenant_id: str = "default"          # 团队 ID
    user_id: str = "default"            # 用户 ID
    data_root: str = "./data"           # 数据存储根目录
    embedding_dimension: int = 1024     # 嵌入向量维度
    embedding_provider: str = ""        # 嵌入提供商
    embedding_model: str = ""           # 嵌入模型名
    embedding_api_key: str = ""         # API Key
    embedding_api_base: str = ""        # API Base URL
    # RuVector 存储
    ruvector_host: str = "127.0.0.1"    # RuVector 服务地址
    ruvector_port: int = 6921           # RuVector 服务端口
    # MCP Server
    mcp_transport: str = "stdio"        # "stdio" | "sse" | "streamable-http"
    mcp_port: int = 8920                # MCP SSE/HTTP 端口
```

支持从 `opencortex.json` 或 `.opencortex.json` 文件加载。

### 6.2 嵌入模型

当前参考 `~/.openviking/ov.conf` 配置：

```json
{
  "embedding": {
    "dense": {
      "provider": "volcengine",
      "api_key": "***",
      "model": "doubao-embedding-vision-250615",
      "api_base": "https://ark.cn-beijing.volces.com/api/v3",
      "dimension": 1024,
      "input": "multimodal"
    }
  }
}
```

嵌入基类层级：

```
EmbedderBase (ABC)
  ├── DenseEmbedderBase       → 返回 dense_vector (List[float])
  ├── SparseEmbedderBase      → 返回 sparse_vector (Dict[str, float])
  ├── HybridEmbedderBase      → 返回 dense + sparse
  └── CompositeHybridEmbedder → 组合 Dense + Sparse 嵌入器
```

---

## 7. 存储后端

### 7.1 VikingDBInterface

抽象接口，定义 25 个 async 方法：

| 类别 | 方法数 | 说明 |
|------|--------|------|
| 集合管理 | 5 | create/drop/exists/list/info |
| 单条 CRUD | 6 | insert/update/upsert/delete/get/exists |
| 批量 CRUD | 4 | batch_insert/batch_upsert/batch_delete/remove_by_uri |
| 搜索 | 3 | search/filter/scroll |
| 聚合 | 1 | count |
| 索引 | 2 | create_index/drop_index |
| 生命周期 | 5 | clear/optimize/close/health_check/get_stats |

### 7.2 RuVectorAdapter

实现 VikingDBInterface + SONA 强化面：

- **Standard Face**: 实现全部 25 个抽象方法
- **Reinforcement Face**: `update_reward()` / `get_profile()` / `apply_decay()` / `set_protected()`
- **集合模拟**: RuVector 单命名空间，通过 `{collection}::{id}` 前缀模拟多集合
- **过滤翻译**: VikingDB filter DSL → RuVector equality filter + Python post-filter

> **注意**: 当前系统未安装 `rvf` 二进制文件，RuVectorAdapter 暂时无法直接使用。
> 端到端测试通过 InMemoryStorage 完成验证。

---

## 8. MCP Server

### 8.1 概述

通过 [PrefectHQ FastMCP](https://github.com/PrefectHQ/fastmcp) v3 将 MemoryOrchestrator 暴露为 MCP 工具，支持外部 AI Agent 连接。

### 8.2 MCP Tools

| Tool | 参数 | 描述 |
|------|------|------|
| `memory_store` | abstract, content?, category?, context_type?, uri?, meta? | 存储新记忆/资源/技能 |
| `memory_search` | query, limit?, context_type?, category? | 语义搜索 |
| `memory_feedback` | uri, reward | SONA 正/负反馈 |
| `memory_stats` | — | 系统统计 |
| `memory_decay` | — | 触发时间衰减 |
| `memory_health` | — | 健康状态检查 |

### 8.3 Transport 模式

```bash
# stdio (本地 Agent，如 Claude Desktop)
python -m opencortex.mcp_server --config opencortex.json

# SSE (远程 Agent 通过 HTTP 连接)
python -m opencortex.mcp_server --transport sse --port 8920

# Streamable HTTP
python -m opencortex.mcp_server --transport streamable-http --port 8920
```

### 8.4 Claude Code 集成

项目 `.mcp.json` 已配置 MCP Server，Claude Code 可直接使用。
自定义 Skill `/opencortex-mcp` 可交互式启动 Server。
Plugin manifest 位于 `.claude-plugin/plugin.json`。

---

## 9. 测试

### 9.1 端到端测试 (24 个用例，全部通过)

```bash
python3 tests/test_e2e_phase1.py -v
```

| 编号 | 测试 | 验证内容 |
|------|------|----------|
| 01 | init | 编排器初始化，组件连接 |
| 02 | add_memory | 添加记忆：自动 URI + 嵌入 + 向量库 + 文件系统 L0 |
| 03 | add_resource | 共享资源 → 团队级 URI |
| 04 | add_skill | 共享技能 → `agent/skills/` 路径 |
| 05-06 | search | 基本检索 + 按类型过滤 |
| 07-09 | feedback | SONA 正反馈 / 负反馈 / 批量反馈 |
| 10-11 | decay/protect | 时间衰减 + 保护记忆衰减更慢 |
| 12-13 | update | 更新重嵌入 + 不存在返回 False |
| 14 | remove | 从向量库和文件系统同时删除 |
| 15 | tenant_isolation | 不同租户 URI 命名空间隔离 |
| 16-18 | 基础组件 | UserIdentifier / Context 类型推导 / URI 构建解析 |
| 19 | vikingfs | 文件系统 L0/L1/L2 读写 |
| 20 | collection_schema | 集合自动创建 |
| 21 | **full_pipeline** | **完整管线**：add → search → feedback → decay → update → remove → close |
| 22-24 | 边界情况 | 幂等初始化 / 未初始化报错 / 不存在 URI 优雅处理 |

### 9.2 MCP Server 测试 (8 个用例，全部通过)

```bash
python3 -m unittest tests.test_mcp_server -v
```

| 编号 | 测试 | 验证内容 |
|------|------|----------|
| 01 | list_tools | 6 个 MCP tools 注册正确 |
| 02 | memory_store | 存储记忆并返回 URI |
| 03 | memory_search | 存储后语义搜索命中 |
| 04 | memory_feedback | SONA 反馈信号 |
| 05 | memory_stats | 返回系统统计 |
| 06 | memory_decay | 触发时间衰减 |
| 07 | memory_health | 组件健康检查 |
| 08 | **full_pipeline** | **完整管线**: store → search → feedback → decay → health |

### 9.3 运行全部测试

```bash
PYTHONPATH=src python3 -m unittest tests.test_e2e_phase1 tests.test_mcp_server -v
# 32 tests, 0 failures
```

---

## 10. 技术决策记录

| 决策 | 选择 | 理由 |
|------|------|------|
| URI Scheme | `opencortex://` | 独立命名空间，避免与 OpenViking 冲突 |
| 租户隔离 | `tenant` 作为顶级 scope | 支持团队使用，独立命名空间 |
| Agent 记忆共享策略 | patterns 共享，cases 私有 | 团队共享通用模式，个人案例隔私 |
| 配置默认值 | `tenant_id=default, user_id=default` | 单用户场景零配置可用 |
| Context 类型推导 | URI 路径段匹配 | 适配租户级长 URI，无需额外字段 |
| 向量存储抽象 | VikingDBInterface 25 方法 | 保持与 OpenViking 兼容，可插拔后端 |
| 目录节点自动创建 | `_ensure_parent_records()` | 层级检索依赖目录树遍历 |
| MCP 框架 | PrefectHQ FastMCP v3 | 比 mcp SDK 内置 FastMCP 更成熟，decorator 式注册 |
| MCP Transport | stdio + SSE + HTTP | stdio 用于本地，SSE/HTTP 用于远程 Agent |

---

## 11. 当前状态与后续计划

### 已完成 (Phase 1 + MCP)

- [x] OpenViking 源码移植（VikingFS / 检索 / 存储接口）
- [x] 租户级多用户 URI 隔离体系
- [x] RuVector Adapter（SONA 强化面）
- [x] MemoryOrchestrator 统一 API
- [x] MCP Server 实现（FastMCP v3，6 个 tools，stdio/sse/http）
- [x] Claude Code 集成（.mcp.json + skill + plugin manifest）
- [x] CortexConfig 扩展（ruvector_host/port, mcp_transport/port）
- [x] 32 个测试全部通过（24 E2E + 8 MCP）

### 待实现

- [ ] 接入真实 Embedding 模型（火山引擎 doubao-embedding-vision-250615）
- [ ] 接入 VLM/LLM 模型（doubao-seed-1-8-251228，用于 IntentAnalyzer）
- [ ] 确认并接入可用向量存储后端（RuVector 或 OpenViking 内置引擎）
- [ ] 本地事件采集 + SQLite spool 队列
- [ ] 远程同步 + Agent Swarm 编排
