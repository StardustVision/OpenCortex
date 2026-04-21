---
date: 2026-04-17
topic: document-ingestion-parallel-derive
---

# Document Ingestion: 并发 Derive + Bottom-Up 汇总

## Problem Frame

Document mode ingestion 对每个 chunk 串行调用 `_derive_layers()`（远程 LLM），导致中等以上文档超时。QASPER 基准测试 281 篇论文 0 篇成功入库。

超时链路实测：
- MCP client（Node.js `tools.ts:178`）：`AbortSignal.timeout(30000)` — **30s**
- Python HTTP client（`http/client.py:25`）：`_DEFAULT_TIMEOUT = 30.0` — **30s**
- OCClient（benchmark 客户端）：`timeout=120.0` — **120s**

生产环境瓶颈是 30s MCP timeout，不是之前假设的 120s。

根因链：`POST /api/v1/memory/store` → `orchestrator.add()` → `_add_document()` → `for chunk in chunks: self.add()` → `_derive_layers()`（串行，每次 5-30s）。中位数文档 10 chunks × 10s = 100s，远超 30s。

附带设计缺陷：当前 `_add_document` 的 parent record（`is_leaf=False`）跳过 `_derive_layers`，导致父节点没有 LLM 生成的 L0/L1 摘要。OpenViking 的 document 策略是 bottom-up：叶子先 summary → 父节点从孩子摘要汇总 L1 → 从 L1 导出 L0。这套策略支撑了 OpenViking 的 object-first 检索（先搜父节点 L0 定位范围，再向下钻取叶子）。

## Requirements

**并发 Derive**
- R1. `_add_document` 中的叶子 chunk 处理从串行改为并发，使用 semaphore 限流
- R2. 并发度通过 `CortexConfig` 可配置，默认值 3
- R3. `parent_index` 依赖关系在并发化后仍然正确。扁平文档（所有 `parent_index=-1`）可直接 `asyncio.gather`；嵌套文档需按层级拓扑序处理（同层并发，跨层串行）
- R4. 已有 `batch_add` 的 semaphore + gather 模式（`orchestrator.py:4822`）作为实现参考

**Bottom-Up 父节点汇总**
- R5. 所有叶子 chunk derive 完成后，父节点（`is_leaf=False`）从孩子的 L0 abstract 汇总生成自己的 L1 overview
- R6. 父节点的 L0 abstract 从 L1 overview 压缩导出（不直接吃原文）
- R7. 如果文档有多层嵌套（section → subsection），汇总自底向上逐层执行
- R8. 父节点汇总是额外 1-2 次 LLM 调用（远少于现在的 0 次），不影响总耗时上界（与叶子并发交错）

**Client Timeout 适配**
- R9. MCP client 的 `store` 操作 timeout 从 30s 提升到 300s（仅 store，不影响其他工具的 timeout）
- R10. Python HTTP client 的 store 操作 timeout 同步提升

## Success Criteria

- QASPER 基准测试 ingestion 成功率 > 95%（当前 0%）
- 中位数文档（10 chunks，并发度 3，10s/LLM）总耗时 < 45s
- 父节点 record 有 LLM 生成的 L0/L1（当前为空）
- 现有 memory/conversation mode 不受影响

## Scope Boundaries

- 不改 MarkdownParser chunking 逻辑
- 不改 `_derive_layers` 本身的 LLM prompt（叶子 derive 不变）
- 不引入异步 job 系统（无 job_id / status endpoint / 轮询 API）— 并发化 + 提升 timeout 即可解决，无需新抽象
- 不做微小 chunk 合并 — 并发化后微小 chunk 不贡献额外 wall-clock time
- 不做两阶段 ingestion（先写后 derive）
- QASPER adapter 的 `expected_uris` 膨胀问题不在此范围

## Key Decisions

- **砍掉异步 job 系统（原 R4-R7）**：三个 review 一致指出这是过度工程。Scope guardian 确认 R1-R3 + 提升 timeout 即可满足所有 success criteria。异步 job 还会破坏 MCP 工具的响应契约（缺少 `uri` 字段），且需要 contextvars 传播、TTL 清理等新子系统
- **砍掉微小 chunk 合并（原 R8-R9）**：并发化后微小 chunk 不增加 wall-clock time，且合并会破坏 `parent_index` 位置索引（需要重写所有后续 chunk 的引用）
- **纳入 bottom-up 父节点汇总**：借鉴 OpenViking document 策略。当前 parent record 的 L0/L1 为空是一个结构性缺陷，会导致 object-first 检索（§3.4 of openviking alignment doc）无法在 document 模式下生效
- **提升 client timeout 而非改 HTTP 契约**：最小改动解决超时，不引入新 API 或改变响应格式

## Dependencies / Assumptions

- 扁平文档（如 QASPER，所有 `parent_index=-1`）：叶子 chunk derive 完全独立，可直接 gather
- 嵌套文档：需按拓扑序（同层并发、跨层串行）。planning 阶段需验证当前 `chunk_results` 列表的索引结构
- 父节点汇总 prompt 需要新建（当前 `build_layer_derivation_prompt` 面向叶子原文，不适用于从孩子摘要汇总）
- 远程 LLM API 能承受 3 并发调用（默认值保守）

## Outstanding Questions

### Deferred to Planning
- [Affects R3][Needs research] 嵌套文档的拓扑序实现：当前 `chunk_results` 按序 append，并发后需要 pre-sized 索引结构。检查 `batch_add` 的现有模式是否可复用
- [Affects R5-R6][Technical] 父节点汇总 prompt 设计：输入是 N 个孩子的 L0 abstract，输出是父 L1 overview + L0 abstract。参考 OpenViking `overview_generation.yaml`
- [Affects R9][Technical] MCP client timeout 是否需要 per-tool 粒度控制，还是全局提升到 300s

## Next Steps

-> `/ce:plan` for structured implementation planning
