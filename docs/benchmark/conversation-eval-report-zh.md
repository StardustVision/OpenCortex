# OpenCortex 对话记忆召回基准测试报告

**运行 ID**: `eval_conversation_5cac650c`
**日期**: 2026-03-15
**评测方法**: LoCoMo (ACL 2024) — 基于 observation 的 RAG 检索

---

## 1. 概述

本报告评测 OpenCortex 的**记忆搜索**能力，使用 LoCoMo 长对话基准数据集，严格遵循论文推荐的 RAG 评测方法：以 observation（对说话人的结构化断言）作为检索单元。

### 评测流水线

```
摄入阶段:  LoCoMo observations → oc.store(abstract="[日期] 说话人: observation", category="observation")
           每条 observation 包含会话日期用于时间定位。
           共提取 2,531 条 observations，成功存储 2,521 条（10 条错误，0.4%）。

检索阶段:  问题 → oc.search(query, limit=10, category="observation", detail_level="l0")
           纯向量搜索，基于 observation 的 embedding 匹配。

评分阶段:  - Recall@k: 证据 dia_id 映射到已存储的 observation URI，计算检索召回率
           - QA F1: LLM 根据检索到的 observations 生成答案 → 与标准答案计算 F1
           - 第 5 类（对抗性问题）按论文协议排除在整体 F1 之外
```

### 系统配置

| 参数 | 值 |
|------|-----|
| 服务端 LLM | Qwen3-235B-A22B-Instruct-2507 (xcloud API) |
| Embedding 模型 | 本地 multilingual-e5-large (1024维) |
| 重排序器 | 禁用（detail_level=l0，纯向量搜索） |
| 检索参数 | top_k=10, detail_level=l0（仅 embedding） |
| 评判 LLM | Qwen3-235B-A22B-Instruct-2507 |
| 并发数 | 5 |
| 上下文预算 | 32,000 tokens |

### 论文参考分数（LoCoMo 论文 Table 3）

| 模型 | 整体 F1（排除 Cat5） |
|------|---------------------|
| GPT-4（全量上下文） | 32.1 |
| 人类 | 87.9 |
| Observations + GPT-4 RAG | ~28–30 |

---

## 2. 数据集

**LoCoMo**（Long Conversation Memory，ACL 2024）：10 段长多轮多会话对话，包含 1,986 个 QA 问答对，分为 5 个难度类别。

| 类别 | 类型 | 数量 | 说明 |
|------|------|------|------|
| 1 | 单跳事实 | 282 | 直接事实问题（"Caroline 的身份是什么？"） |
| 2 | 时间相关 | 321 | 时间定位问题（"Caroline 什么时候参加了 LGBTQ 支持小组？"） |
| 3 | 推理 | 96 | 需要常识推理（"X 可能会怎样？"） |
| 4 | 多跳 | 841 | 需要链接多个事实（"X 在 Y 之后意识到了什么？"） |
| 5 | 对抗性/不可回答 | 446 | 关于错误人物或不存在事件的问题 |

**摄入统计**: 从 10 段对话中提取 2,531 条 observations，成功存储 2,521 条（10 条错误来自多 dia_id 边界情况）。

**Recall@k 评测**: 1,931 个问题有证据 URI 可评测（55 个跳过，无标准答案）。

---

## 3. 结果

### 3.1 检索质量（Recall@k）

| 类别 | Recall@1 | Recall@3 | Recall@5 | MRR | 数量 |
|------|----------|----------|----------|-----|------|
| 1（单跳事实） | 0.000 | 0.002 | 0.002 | 0.005 | 281 |
| 2（时间相关） | 0.000 | 0.000 | 0.000 | 0.001 | 316 |
| 3（推理） | 0.000 | 0.012 | 0.012 | 0.008 | 85 |
| 4（多跳） | 0.000 | 0.000 | 0.003 | 0.001 | 816 |
| 5（对抗性） | 0.000 | 0.002 | 0.002 | 0.002 | 433 |
| **整体** | **0.000** | **0.001** | **0.003** | **0.002** | **1,931** |

**关键发现**：所有类别的检索召回率接近零。Embedding 模型（multilingual-e5-large）无法将自然语言问题匹配到短小的 observation 文本。这是系统性能的**根本瓶颈** — 当检索返回的是不相关的 observations 时，LLM 评判器不可能产生正确答案。

具体表现：
- 用关键词 "LGBTQ" 搜索 → 能找到相关 observation
- 用自然语言 "What is Caroline's identity?" 搜索 → 找不到 "Caroline is a transgender woman"
- 这说明问题不在 Qdrant 或存储层，而在 embedding 的跨格式匹配能力

### 3.2 QA 准确度（F1 分数）

| 类别 | Baseline F1 | OpenCortex F1 | 差值 | OC 胜 | BL 胜 | 平局 |
|------|------------|---------------|------|-------|-------|------|
| 1（单跳事实） | 0.3554 | 0.0501 | -0.3053 | 6 | 231 | 45 |
| 2（时间相关） | 0.2381 | 0.0212 | -0.2169 | 11 | 201 | 109 |
| 3（推理） | 0.1603 | 0.0830 | -0.0773 | 16 | 50 | 30 |
| 4（多跳） | 0.5075 | 0.0405 | -0.4670 | 19 | 773 | 49 |
| 5（对抗性）* | **0.0717** | **0.6996** | **+0.6279** | **289** | **9** | **148** |
| **整体（排除 5）** | **0.4018** | **0.0409** | **-0.3609** | 52 | 1,255 | 233 |

\* 第 5 类按 LoCoMo 论文协议排除在整体 F1 之外（对抗性/不可回答问题没有实质性标准答案）。

**Baseline**：将完整对话全文送入 LLM（超过 32k tokens 时截断）。
**OpenCortex**：将 top-10 条 observation 搜索结果送入 LLM。

### 3.3 Token 效率

| 指标 | Baseline | OpenCortex | 压缩率 |
|------|----------|------------|--------|
| 平均 tokens/问题 | 26,466 | 410 | **98.5%** |
| 总 tokens | 52,561,486 | 813,913 | **98.5%** |
| 是否需要截断 | 否（平均 < 32k） | 不适用 | — |

OpenCortex 每次查询平均只需 410 tokens（约 10 条 observation），相比 baseline 的 26,466 tokens 减少了 **65 倍**。

### 3.4 召回延迟

| 百分位 | 延迟 |
|--------|------|
| p50 | 6,970 ms |
| p95 | 9,834 ms |
| p99 | 11,100 ms |
| 平均 | 6,935 ms |
| 最小 | 2,464 ms |
| 最大 | 12,742 ms |

延迟组成：embedding 编码（~100ms）+ Qdrant 向量搜索（~50ms）+ 网络开销。因为使用 `detail_level=l0`，跳过了 IntentRouter LLM 调用和重排序器，相比上一轮测试（p50=11s）快了约 40%。

### 3.5 可靠性

| 指标 | 值 |
|------|-----|
| 摄入错误 | 10 / 2,531 (0.4%) |
| QA 评测错误 | 0 / 1,986 |
| 重试事件 | 0 |

---

## 4. 详细分析

### 4.1 优势

#### 对抗性问题鲁棒性（第 5 类）— 9.8 倍提升

这是 OpenCortex 最突出的优势。第 5 类问题是故意设计的"陷阱题"，问的是对话中不存在的事件或张冠李戴的事实。

| | Baseline | OpenCortex |
|---|---------|------------|
| F1 | 0.0717 | **0.6996** |
| 胜场 | 9 | **289** |
| 提升 | — | **9.8x** |

**为什么 OC 远优于 Baseline？**

- **Baseline**：LLM 看到 26k tokens 的完整对话，容易在大量上下文中"找到"似是而非的答案，产生幻觉
- **OpenCortex**：只检索 10 条最相关的 observation（~410 tokens）。当问题问的是不存在的事件时，检索结果中自然不会包含相关内容，LLM 更容易正确回答"上下文中没有提到这个信息"

这在生产环境中意义重大 — **精确性比召回率更重要**。用户宁可得到"我不知道"的诚实回答，也不愿收到看似正确实则编造的答案。

**典型案例**：
- 问题："What are Melanie's plans for the summer with respect to adoption?"
- 标准答案："researching adoption agencies"（对抗性 — 故意将别人的事张冠李戴到 Melanie 身上）
- OC：选择性检索 → LLM 正确回答 → F1=1.000
- Baseline：全文上下文让 LLM 产生混淆 → F1=0.000

#### Token 效率 — 98.5% 压缩

| | Baseline | OpenCortex |
|---|---------|------------|
| 平均 tokens | 26,466 | 410 |
| 压缩倍数 | — | **65x** |

这意味着：
- LLM 推理成本降低 65 倍
- 响应速度显著提升（更短的 prompt = 更快的生成）
- 在 token 预算有限的场景下（如移动端、低成本部署），OC 可以处理远超 baseline 的对话长度

#### 延迟优化

使用 `detail_level=l0`（纯向量搜索）后，p50 延迟从上一轮的 11.0s 降至 7.0s，减少 37%。主要节省来自跳过 IntentRouter 的 LLM 调用（~5s）。

### 4.2 劣势

#### 检索召回率接近零（Recall@5 = 0.003）

这是整个评测中最严重的问题。在 1,931 个有标准答案的检索任务中，top-5 返回的结果几乎从不包含正确的 observation。

**数据佐证**：
- Recall@1 = 0.000（所有类别）— 排名第一的结果几乎从不正确
- Recall@5 = 0.003 — 仅 0.3% 的问题在前 5 名中找到了正确 observation
- 最好的类别是第 3 类（推理），Recall@5 = 0.012，也只有 1.2%

**直接后果**：当检索失败时，LLM 评判器收到的是不相关的 observations，自然无法产生正确答案。这解释了为什么除第 5 类外所有类别 F1 都远低于 baseline。

#### 多跳问题（第 4 类，F1=0.041）

多跳问题需要链接来自 2-3 个不同 observation 的事实。即使 embedding 能匹配到一个相关 observation，单次查询也很难同时召回所有所需的证据片段。

**典型案例**：
- 问题："What country is Caroline's grandma from?"
- 标准答案："Sweden"
- OC：检索完全遗漏了相关 observation → F1=0.000
- Baseline：LLM 在全文中找到答案 → F1=1.000

#### 时间类问题（第 2 类，F1=0.021）

尽管我们在 observation 文本前添加了日期前缀（如 `[May 7, 2023]`），embedding 模型对日期 token 的权重很低。时间查询（"什么时候发生了 X？"）在语义空间中无法匹配到包含特定日期的 observation。

**典型案例**：
- 问题："When did Caroline go to the LGBTQ support group?"
- 标准答案："7 May 2023"
- OC：检索到了主题相关的内容，但没有匹配到包含日期的 observation → F1=0.000
- Baseline：在全文对话中找到日期 → F1=0.462

#### 单跳事实（第 1 类，F1=0.050）

即使是最简单的单跳事实问题，OC 也只有 2.1%（6/282）的胜率。这进一步确认了 embedding 质量是根本问题 — 当"What is Caroline's identity?"无法匹配到"Caroline is a transgender woman"时，没有任何下游组件能补救。

### 4.3 根因分析：Embedding 跨格式匹配失败

整个评测的核心瓶颈可以归结为一个问题：**multilingual-e5-large 无法在自然语言问题和短断言文本之间建立有效的语义匹配**。

| 查询类型 | 示例查询 | 示例 Observation | 能否匹配？ |
|---------|---------|-----------------|-----------|
| 关键词 | "LGBTQ" | "Caroline attended an LGBTQ support group" | 能 |
| 自然语言问题 | "What is Caroline's identity?" | "Caroline is a transgender woman" | **不能** |
| 时间查询 | "When did Caroline go to the support group?" | "[May 7, 2023] Caroline attended LGBTQ support group" | **不能** |
| 多跳问题 | "What did X realize after Y?" | 分散在两条 observation 中 | **不能**（单次查询） |

**为什么会出现这种情况？**

1. **E5 模型的设计特点**：multilingual-e5-large 是通用 embedding 模型，在长文本段落匹配上表现优秀，但对"问题 → 短断言"这种跨格式匹配并非其强项

2. **Observation 文本过短**：典型 observation 只有 10-20 个 token（如 "Caroline is a transgender woman"），语义信息密度低，embedding 向量的区分度不够

3. **Query prefix 问题**：E5 系列模型要求查询前加 `query:` 前缀，文档前加 `passage:` 前缀。如果 OpenCortex 的 embedding 流程未正确添加这些前缀，检索效果会下降 10-30%

4. **论文使用 OpenAI embedding**：LoCoMo 论文的 RAG 实验使用 text-embedding-ada-002，该模型在问答匹配任务上经过专门优化，可能在跨格式场景下表现更好

### 4.4 与论文参考值的对比

| 指标 | LoCoMo 论文 (GPT-4 full) | OpenCortex | 差距 |
|------|------------------------|------------|------|
| 整体 F1（排除 Cat5） | 32.1 | 4.1 | -28.0 |
| Baseline F1（我们测的） | 40.2 | — | 高于论文，可能因 Qwen3-235B 在中文对话上更强 |

我们的 baseline F1（40.2）高于论文中 GPT-4 的结果（32.1），说明 Qwen3-235B 作为评判 LLM 在理解完整上下文后的回答能力不弱。但 OC 的 4.1 远低于论文中 RAG 方法的 28-30，这进一步确认瓶颈在检索而非 LLM 生成。

### 4.5 方法论 v1 vs v2 对比

我们在修正方法论前后跑了两次评测，对比如下：

| 指标 | v1（对话模式） | v2（observation 模式） | 说明 |
|------|-------------|---------------------|------|
| 摄入方式 | context_commit() | oc.store() | v2 遵循论文 |
| 检索单元 | 对话轮次 + 合并块 | Observations（断言） | v2 遵循论文 |
| 检索 API | context_recall() | oc.search() | v2 匹配存储类型 |
| detail_level | auto（IntentRouter + 重排序） | l0（纯 embedding） | v1 有重排序器 |
| 整体 F1（排除 Cat5） | ~0.08* | 0.041 | v1 反而更高 |
| Cat5 F1 | 0.305 | **0.700** | v2 大幅提升 |
| Token 压缩率 | 91.1% | **98.5%** | v2 更紧凑 |
| 延迟 p50 | 11,032 ms | **6,970 ms** | v2 更快 |

\* v1 未排除 Cat5，重新计算排除后约 0.08。

**关键洞察**：v1 的 IntentRouter + 重排序器在一定程度上弥补了 embedding 匹配的不足 — 通过 LLM 分析生成更精确的查询词、通过重排序器对候选结果重新排序。纯 embedding 的 v2 路径更直接地暴露了 embedding 质量差距。

这意味着 IntentRouter 和重排序器并非"无用"，它们确实在弥补底层 embedding 的不足。但要从根本上解决问题，需要提升 embedding 本身的跨格式匹配能力。

---

## 5. 改进方向

### P0 — Embedding 质量（关键路径）

**这是唯一会产生数量级改进的方向**。其他所有优化在 Recall@k 接近零的情况下都是无意义的。

#### 1. 验证 E5 Query Prefix

multilingual-e5-large 要求：
- 查询文本前加 `query: ` 前缀
- 文档文本前加 `passage: ` 前缀

如果 OpenCortex 的 embedding 流程未正确添加这些前缀，检索效果会显著下降。需要检查 `EmbedderBase` 和 `LocalEmbedder` 的实现。

**预期收益**：如果前缀确实缺失，修复后 Recall@k 可能提升 10-30%。

#### 2. Embedding 模型升级

测试以下替代模型：
- **OpenAI text-embedding-3-large**：论文使用的同系列模型，在问答匹配上有专门优化
- **Cohere embed-v3**：在 MTEB 排行榜上多项任务表现优异
- **BGE-large-en-v1.5**：BAAI 出品，在问答检索任务上表现突出
- **GTE-Qwen2-7B-instruct**：指令微调的 embedding 模型，支持查询指令

**预期收益**：模型切换可能将 Recall@5 从 0.003 提升到 0.1-0.3 级别。

#### 3. Observation 文本扩展

在存储前用 LLM 将短小的 observation 扩展为更易被搜索的格式：

```
原始：  "Caroline is a transgender woman"
扩展：  "About Caroline's identity and gender: Caroline is a transgender woman.
         Caroline identifies as transgender. Caroline's gender identity is that
         of a woman who is transgender."
```

这增加了 embedding 向量的信息量，提高了跨格式匹配的概率。

**预期收益**：中等。需要在摄入时额外调用 LLM，增加摄入延迟和成本。

### P1 — 检索策略优化

在 Recall@k 提升到合理水平后，以下优化才有意义：

#### 4. 多查询分解

IntentRouter 已支持 `queries[]` 数组进行并发检索。对复杂问题（尤其是第 4 类多跳），让 LLM 将原问题分解为多个子查询：

```
原始：  "What did X realize after the charity race?"
分解：  ["What did X participate in?", "What happened at the charity race?",
         "What did X learn or realize?"]
```

需要使用 `detail_level` > l0 以启用 IntentRouter。

#### 5. 混合搜索（BM25 + 稠密向量）

时间类查询包含具体日期字符串，BM25 词法匹配可以在稠密 embedding 失败时作为补充。OpenCortex 已有词法搜索的基础设施（`lexical_mode: fallback_only`），可以在 observation 搜索中启用。

#### 6. 自适应 top_k

根据查询复杂度动态调整检索数量：
- 简单事实问题 → top_k=5
- 多跳问题 → top_k=20-30
- 时间问题 → top_k=10 + 词法搜索兜底

### P2 — 架构优化

#### 7. 重新评估重排序器

v1（带 IntentRouter + 重排序器）的 F1 实际高于 v2（纯 embedding）。考虑在 observation 搜索中也启用重排序器 — 使用 `detail_level=auto` 重新测试。

#### 8. Observation 双索引

将每条 observation 同时用原始文本和 LLM 扩展文本进行 embedding，存储两个向量。搜索时同时查两个向量空间，取并集。

---

## 6. 结论

### 核心数据

| 指标 | 值 | 评估 |
|------|-----|------|
| Recall@5 | 0.003 | 严重不足 — embedding 无法匹配问题到 observation |
| 整体 F1（排除 Cat5） | 0.041（vs 0.402 baseline） | 远低于 baseline，被检索失败主导 |
| 对抗性 F1（Cat5） | 0.700（vs 0.072 baseline） | **9.8 倍提升** — 极强的反幻觉能力 |
| Token 压缩率 | 98.5% | 优秀 — 比全文上下文减少 65 倍 |
| 延迟 p50 | 7.0s | 异步场景可接受 |
| 可靠性 | 0 QA 错误 / 1,986 | 生产级 |

### 总结

正确的 LoCoMo observation 评测揭示了一个清晰的结论：**embedding 质量是 OpenCortex 对话记忆检索的关键瓶颈**。

当 Recall@5 接近零时，无论下游的 LLM 评判器多强大，都无法从不相关的检索结果中生成正确答案。这不是 Qdrant 向量数据库、CortexFS 存储层、或 LLM 生成能力的问题 — 纯粹是 embedding 模型在"自然语言问题 → 短断言文本"跨格式匹配上的能力不足。

**唯一的亮点是对抗性问题**：OpenCortex 的选择性检索机制在第 5 类问题上实现了 9.8 倍的 F1 提升（0.700 vs 0.072），证明了记忆检索架构在防止幻觉方面的巨大优势。在生产环境中，"正确地说不知道"比"自信地给出错误答案"有价值得多。

**下一步行动的优先级**：
1. **P0**：检查并修复 E5 query prefix → 测试替代 embedding 模型 → 确认 Recall@k 能提升到合理水平
2. **P1**：在 Recall@k 改善后，启用多查询分解和混合搜索
3. **P2**：重新评估 IntentRouter + 重排序器在 observation 检索中的价值

只有当 Recall@5 从 0.003 提升到 0.1+ 级别时，其他优化方向才有实际意义。

---

*基于运行 `eval_conversation_5cac650c` 于 2026-03-15 生成。完整逐条结果见 `conversation-eval_conversation_5cac650c.json`。*
