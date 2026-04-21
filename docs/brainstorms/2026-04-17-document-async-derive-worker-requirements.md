---
date: 2026-04-17
topic: document-async-derive-worker
---

# Document Mode: Async Derive Worker

## Problem Frame

`store()` 目前同步等待所有 LLM derive + embed 完成才返回。一篇 3500-token QASPER 论文需要 120-300s，超出 benchmark client 和 MCP client 的 timeout，导致 QASPER ingestion 大量失败。

根因：derive worker 逻辑（`_add_document`）和 HTTP 请求在同一调用链里串行执行。

OpenViking 解法：`store()` 立即返回，LLM derive 在后台 worker 执行，Qdrant 记录在 derive 完成后才写入（最终一致）。

## Requirements

**R1. store() 立即返回**
- 写入 CortexFS（L2 原文）后立即返回 URI，不等 LLM
- store() 端到端延迟 < 1s（不含网络），与文档大小无关

**R2. 后台 DeriveWorker**
- 进程内 `asyncio.Queue` + worker coroutine
- 消费 derive 任务：读 CortexFS L2 → LLM derive chunks → bottom-up → embed → upsert Qdrant
- 并发度复用现有 `document_derive_concurrency`（默认 3）

**R3. 最终一致可搜索**
- derive 期间 Qdrant 无占位记录（与 OpenViking 对齐）
- derive 完成后记录进入搜索面
- 客户端无需感知 derive 状态，最终一致即可

**R4. 启动恢复扫描**
- Server 启动时扫描 CortexFS：找出有 content.md 但 Qdrant 无对应记录的 URI
- 自动重入队 derive，补偿重启前未完成的任务
- 标记方式：直接查 Qdrant（filter by uri）；若记录不存在则视为待处理

**R5. derive 失败处理**
- 单个 chunk derive 失败不阻塞其他 chunk（`return_exceptions=True`，现有逻辑）
- 整个文档 derive 失败：记录 error 日志，Qdrant 无记录（下次重入库可重试）
- 不重试（避免队列积压）

**R6. 不引入新 API**
- 无 task_id / status endpoint / 轮询 API
- 现有 MCP / HTTP API 接口不变（store 仍返回 `uri`）

## Success Criteria

- QASPER ingestion 成功率 > 95%（当前 <1%）
- store() p99 < 2s（当前 60-300s）
- derive 完成后文档可被 recall 搜索
- 重启后未完成文档自动恢复

## Scope Boundaries

- 仅改 document mode（`_add_document`）；memory mode 和 conversation mode 不变
- 不引入进程外队列（无 Redis / AGFS 队列）
- 不引入 status API
- conversation 的 `_spawn_merge_task` 模式已是 worker 架构，不在此范围

## Key Decisions

- **无占位记录**：OpenViking 对齐，derive 完成前 Qdrant 无该文档记录
- **进程内 asyncio.Queue**：无需持久化，CortexFS 原文就是持久层，重启扫描补偿即可
- **最终一致**：客户端不需要知道 derive 何时完成，与 OpenViking 行为一致

## Dependencies / Assumptions

- `document_derive_concurrency`（CortexConfig）：Unit 1 已有，worker 直接用
- `_derive_parent_summary`（orchestrator）：Unit 4 已有，bottom-up 逻辑不变
- CortexFS `read_file` 支持读 L2 content：已验证
- Qdrant filter by uri：现有能力

## Outstanding Questions

### Deferred to Planning
- DeriveWorker 放在 orchestrator 还是独立模块？
- 启动恢复扫描的触发点（`init()` 内，还是独立 `startup_scan()`）？
- Queue 满时的背压策略（drop? block? 丢弃最旧?）

## Next Steps

→ `/ce:plan` 规划实现细节
