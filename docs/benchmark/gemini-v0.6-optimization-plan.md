# OpenCortex v0.6 检索架构与性能优化详细设计 (Gemini 提议)

**来源:** Gemini 架构审查建议
**针对报告:** OpenCortex 基准测试报告 (2026-03-19 v0.5.1)
**核心思想:** 三层递进（指标量准 -> 准确率提升 -> 性能提升），优先解决核心痛点（ROI最大化）。

---

## 🎯 核心目标
1. **彻底解决数据污染与指标致盲问题**，建立可信的 Benchmark 体系。
2. **攻克长文档（QASPER）与事实类（LoCoMo）召回暴跌的技术债**，将准确率拉回全量上下文的 90% 以上。
3. **大幅压缩 P50 延迟**，通过短路 LLM 意图分析和自适应预算，将平均检索延迟从 8s+ 降至 2-3s。

---

## 第一层：P0 观测性与基准隔离（先把指标量准）
*原则：在带有噪音的罗盘下航行注定触礁，所有优化必须基于绝对干净、可信的数据。*

### 1. 物理级的数据与运行时隔离
*   **独立租户隔离：** 每次 Benchmark 强制生成一次性的 `tenant_id`、`project_id` 和 `run_id`。
*   **短暂态向量库（Ephemeral Qdrant）：** 评测脚本启动时，挂载一个全新的内存级或临时 Qdrant 实例（或独立 Collection）。跑完即销毁，从物理层面杜绝跨 Run、跨数据集的数据膨胀与交叉污染。

### 2. 修复召回指标（Recall Metrics）
*   **修复 `expected_uri`：** 重构 Ingestion 管道与 Eval 脚本的 URI 生成对齐逻辑，使 `Recall@k`、`MRR` 从 0.0 恢复为可用指标。
*   **过渡期代理指标：** 在 URI 彻底修复前，采用组合代理指标衡量召回：`QA J-Score / F1` + `污染率`（Top-K 中非目标文档的比例）+ `命中来源分布`。

### 3. 细粒度延迟追踪（Latency Profiling）
抛弃笼统的 “Search API 耗时”，在核心主干引入追踪器（如 OpenTelemetry 或自定义 Decorator），将 P50/P95 严格拆分为 5 段输出：
1. `Intent Phase` (LLM/Cache)
2. `Embed Phase` (Text to Vector)
3. `Vector Search Phase` (Qdrant ANN)
4. `Rerank Phase` (Cross-Encoder)
5. `Assemble Phase` (FS/DB Prefetch)

---

## 第二层：P1 召回准确率优化（突破核心场景短板）
*原则：抛弃“一招鲜吃遍天”的全局 ANN 检索，转向基于意图的动态路由和富文本索引。*

### 1. 两阶段文档检索（Top 1 优先落地）
**专治 QASPER 等长文档场景的上下文撕裂：**
*   **阶段一（文档级定位）：** 优先在 Document/Section 级别进行粗排搜索，或者强制利用 Intent 提取的 `document_id` 进行 Payload 硬过滤，彻底收敛搜索空间。
*   **阶段二（段落级精排）：** 仅在命中的 Top-N 目标文档内部，进行 Chunk 级别的 ANN 检索与上下文组装。

### 2. 动态查询分流（Query Routing）
根据请求类型动态调整检索策略：
*   **人名/日期/文件名类（强事实）：** 大幅提高 Lexical/BM25 的打分权重。
*   **文档问答类：** 强制限定在目标文档（Doc-scope）或目录内搜索。
*   **对话记忆类（LoCoMo）：** 激活时间衰减与实体约束逻辑。

### 3. 索引表达增强（Context Flattening）
不改变底层模型，通过“作弊式”的拼凑提升 Embedding 质量：
*   **针对 LoCoMo/PersonaMem：** 入库时，将 `[人物] + [日期] + [事件类型] + [具体断言]` 拼接为前缀，再接原始 Chunk 文本。
*   **针对文档模式：** 入库时，将 `[全局标题] + [当前 Section] + [父级/Chunk摘要] + [相邻 Chunk Hint]` 与当前 Chunk 一同进行 Embedding。

### 4. 智能混合检索与降级 Rerank
*   **动态候选池：** 对于包含 Hard Keyword（特定术语、数字）的查询，扩大 BM25 召回的候选池。
*   **Rerank 截断与跳过：** 只对前 N 个候选进行耗时的 Cross-Encoder Rerank；当第一阶段的头部结果分数极高（如 Exact Match 阈值）时，直接跳过 Rerank。

### 5. 补充时序与实体索引
*   在 Qdrant 的 Payload 中强加 `speaker`, `date`, `entity`, `category` 字段。
*   将 LoCoMo 的“时序定位”弱点转化为数据库的常规过滤条件（Range Filter/Match Filter）。

### 6. 自动化离线消融实验台（Ablation Sweep）
构建自动化脚本，支持单一变量控制法，通过网格搜索（Grid Search）量化单点收益：
*   变量空间：`BM25_boost`、`chunk_size`、`rerank_topN`、`doc_scope_filter (on/off)`。

---

## 第三层：P2 召回性能与吞吐优化（剪除不必要的开销）
*原则：好钢用在刀刃上，不把昂贵的计算资源浪费在简单的查询上。*

### 1. IntentRouter 降权与 Fast Path（Top 2 优先落地）
**专治 2-5s 的 LLM 意图延迟瓶颈：**
*   **语义缓存（Intent Cache）：** 部署轻量级向量缓存（如 Redis + 极小维模型），拦截高频相似的 Intent 查询。
*   **Heuristic Fast Path：** 对带有明显实体、指令前缀或闲聊特征的查询，走正则表达式或轻量化分类器（如本地 1.5B 小模型），直接绕过 235B 大模型。

### 2. 自适应检索预算（Adaptive Budget）
*   **简单 Query（如找特定记录）：** 极小 Top-K + 无 Rerank + 不触发深层 Frontier Search。
*   **复杂 Query（如跨文档总结）：** 放大候选池 + 全量 Rerank + 允许补偿查询。

### 3. 严控系统扇出与热点（Strict Fan-out Control）
*   **联合预取（Union Prefetch）：** 结果组装时，一次性收集所有关联 ID，对 FS/Relations 发起单次 Batch Read，杜绝 `N+1` 查询问题。
*   **批量更新：** 访问统计（Access Stats）等后置更新操作，改为纯异步的 Batch Upsert。
*   **Frontier 硬熔断：** 强制增加最大波次（Wave Cap）、补偿查询上限，防止在稀疏目录下陷入无限递归的 N+1 退化。

### 4. 高频调用批量化与底层调优
*   保证 Embed Cache 对本地（ONNX）和远程（API）实现统一的命中逻辑。
*   批量导入强制走 `embed_batch` 和 `batch_upsert`。
*   *(底层补充)* 调优 FastEmbed (ONNX Runtime) 的 `intra_op_num_threads`，避免高并发导入或查询时的 CPU 线程抢占冲突。

---

## 📅 推荐的实际推进路线图 (Roadmap)

| 阶段 | 周期 | 核心任务 | 预期结果 / 验收标准 |
| :--- | :--- | :--- | :--- |
| **Phase 1** | **Day 1-2** | **清扫战场：** 实现 Qdrant 临时实例隔离；修复 `expected_uri`；实装 5 段式 Latency 追踪。 | 指标完全可信；彻底消除因数据膨胀导致的 PersonaMem 12s 异常延迟。 |
| **Phase 2** | **Day 3-5** | **攻坚双王牌：** <br>1. 实现**文档范围限定 + 两阶段检索**。<br>2. 实装 **IntentRouter Fast Path + Cache**。 | **QASPER J-Score 从 0.15 暴涨至 ~0.70+；系统整体 P50 延迟缩减 50% 以上（降至 3s 左右）。** |
| **Phase 3** | **Day 6-8** | **全面提分：** 实装 Query 分流（词法提权）；完成索引增强（时间/实体/摘要打平入库）。 | LoCoMo 单跳事实题（Cat 1）和时序题（Cat 4）J-Score 提升 15-20%。 |
| **Phase 4** | **Day 9-10** | **极致打磨：** 实施自适应检索预算；严控系统扇出与联合预取；运行离线消融实验微调超参。 | P95 尾部延迟趋于平稳；系统吞吐量（并发处理能力）显著提升。 |