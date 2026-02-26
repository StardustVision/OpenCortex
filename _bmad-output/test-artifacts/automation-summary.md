# MemCortex 自动化开发与验证总结（BMAD/TEA 节奏）

日期：2026-02-11

## 1) 执行范围

本轮按 BMAD 的“继续开发 + 验证”目标，完成了 Phase-1 最小闭环的可执行骨架增强与自动化验证：

- 开发：补齐 CLI/队列/flush/sync 的可测试实现
- 验证：新增 unittest 流程测试并执行
- 产物：测试框架说明、Hook 示例、验证报告

## 2) 本轮新增/更新内容

### 代码与结构

- `src/memcortex/`：CLI、engine、spool、vector_store、sync_client、config 等骨架
- `tests/test_phase1_cli_flow.py`：3 条关键流程测试
- `tests/README.md`：测试运行说明
- `docs/phase1-scaffold.md`：阶段骨架说明
- `examples/hooks/claude-code/observe.sh`
- `examples/hooks/cursor/observe.sh`

### 关键能力

- 事件入队：`capture`
- 阈值判定：`maybe-flush`
- 本地处理：`flush`（默认本地处理 + 低水位触发同步）
- 同步通道：`sync`（当前为 MCP mock outbox）
- 状态观测：`status`

## 3) 覆盖计划与落地状态

| 测试级别 | 目标 | 状态 |
|---|---|---|
| CLI 流程级 | capture/status/maybe-flush/flush/sync 端到端链路 | 已完成 |
| 模块级 | queue 状态机、重试、死信分流 | 已具备基础（后续补专项测试） |
| 集成级 | 真实 MCP transport、真实 LanceDB 后端 | 待下一轮 |

## 4) 风险与假设

### 当前假设

- `sync` 目前使用 Mock MCP 客户端，仅验证状态推进与幂等路径
- 向量存储默认 SQLite fallback；LanceDB 需额外安装并切换环境变量

### 主要风险

1. 真实 MCP 接入后需补充失败分类与重试策略差异验证
2. LanceDB 后端在真实数据量下需补充性能与一致性验证
3. Hook 在真实工具环境中的事件字段一致性需要回归

## 5) 下一步推荐（BMAD）

1. 将 Mock MCP 替换为真实 MCP transport（保持接口不变）
2. 增加 `dead_letter` 与 `retry` 场景测试
3. 扩展 Hook 映射验证（Claude Code/Cursor 真实事件）
4. 执行一次 `test-review` 深度质量评估（针对新增 tests）

