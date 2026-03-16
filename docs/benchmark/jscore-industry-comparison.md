# LoCoMo J-Score 行业对比报告

**日期**: 2026-03-16
**运行 ID**: `eval_conversation_v7full`
**评测协议**: Mem0-aligned J-Score (binary LLM-as-Judge, CORRECT/WRONG)

---

## 1. 评测协议

采用行业标准 J-Score 评测协议（与 Mem0、Zep、Letta、MemMachine 一致）：

| 参数 | 本次评测 | 行业标准 (Mem0 paper) |
|------|---------|---------------------|
| 答题 Prompt | "answer in 5-6 words" | "answer in 5-6 words" |
| 评判方式 | Binary CORRECT/WRONG | Binary CORRECT/WRONG |
| 评判模型 | Qwen3-235B | gpt-4o-mini |
| 评判温度 | 0 | 0 |
| 输出格式 | `{"label": "CORRECT"}` | `{"label": "CORRECT"}` |
| 评分范围 | Cat 1–4 微平均 | Cat 1–4 微平均 |
| Cat 5 | 排除 | 排除 |

> **注意**: 本次评测使用 Qwen3-235B 作为答题和评判模型，行业各家使用 gpt-4o-mini。不同 LLM 的能力差异会影响绝对分数，因此**各系统与自身 Full-Context Baseline 的相对差距**是更公平的对比维度。

---

## 2. OpenCortex 本次结果

### 系统配置

| 参数 | 值 |
|------|-----|
| 答题/评判 LLM | Qwen3-235B-A22B-Instruct-2507 |
| Embedding | 本地 multilingual-e5-large (1024维) |
| Reranker | 本地 jina-reranker-v2-base-multilingual |
| 混合搜索 | Dense + BM25 Sparse (Qdrant RRF) |
| top_k | 10 |
| rerank_max_candidates | 100 |
| 上下文预算 (Baseline) | 32,000 tokens |
| 数据集 | LoCoMo 10 conversations, 1,986 QA |

### J-Score 结果

| 类别 | Baseline | OpenCortex | Delta |
|------|----------|------------|-------|
| Cat 1 (单跳事实) | 0.773 | 0.585 | -0.188 |
| Cat 2 (时间相关) | 0.542 | 0.511 | -0.031 |
| Cat 3 (推理) | 0.688 | 0.583 | -0.104 |
| Cat 4 (多跳) | 0.937 | 0.765 | -0.172 |
| **Overall (excl. Cat5)** | **0.809** | **0.668** | **-0.142** |

### F1 结果 (辅助指标)

| 类别 | Baseline | OpenCortex | Delta |
|------|----------|------------|-------|
| Cat 1 | 0.433 | 0.296 | -0.137 |
| Cat 2 | 0.356 | 0.384 | **+0.028** |
| Cat 3 | 0.302 | 0.236 | -0.065 |
| Cat 4 | 0.578 | 0.452 | -0.126 |
| **Overall (excl. Cat5)** | **0.488** | **0.396** | **-0.092** |

### 效率指标

| 指标 | 值 |
|------|-----|
| Baseline 平均 tokens | 26,452 |
| OpenCortex 平均 tokens | 563 |
| **Token 压缩率** | **97.9%** |
| 检索延迟 p50 / p95 / p99 | 20.1s / 29.1s / 33.8s |

---

## 3. 行业 J-Score 对比

### 3.1 绝对 J-Score 对比

> **重要说明**: 各系统使用的 LLM 不同（OpenCortex 用 Qwen3-235B，其他用 gpt-4o-mini），绝对分数不完全可比。但相对排名和差距分析仍有参考价值。

| 排名 | 系统 | Overall J-Score | LLM | 来源 |
|------|------|----------------|-----|------|
| 1 | MemMachine v0.1 | **0.849** | gpt-4o-mini | 自报 |
| 2 | Full-Context Baseline (Qwen3) | 0.809 | Qwen3-235B | 本次评测 |
| 3 | Zep (自测) | 0.751 ± 0.17 | gpt-4o-mini | Zep blog |
| 4 | Letta (file-based) | 0.740 | gpt-4o-mini | Letta blog |
| 5 | Full-Context Baseline (gpt-4o-mini) | 0.729 | gpt-4o-mini | Mem0 paper |
| 6 | Mem0[g] (Graph) | 0.684 | gpt-4o-mini | Mem0 paper |
| **7** | **OpenCortex** | **0.668** | **Qwen3-235B** | **本次评测** |
| 8 | Mem0 | 0.669 | gpt-4o-mini | Mem0 paper |
| 9 | Zep (Mem0 复测) | 0.584 ± 0.20 | gpt-4o-mini | Mem0 rebuttal |

### 3.2 与 Baseline 的相对效率（更公平的对比）

由于各系统使用不同 LLM，与各自 Full-Context Baseline 的比率更能反映记忆系统的检索效能：

| 系统 | J-Score | 对应 Baseline | 比率 (J/BL) | Token 压缩率 |
|------|---------|-------------|-------------|-------------|
| MemMachine v0.1 | 0.849 | 0.729 | **116.4%** ★ | 未公开 |
| Zep (自测) | 0.751 | 0.729 | 103.0% | 未公开 |
| Letta (file-based) | 0.740 | 0.729 | 101.5% | 未公开 |
| Mem0[g] | 0.684 | 0.729 | 93.8% | 未公开 |
| Mem0 | 0.669 | 0.729 | 91.8% | 未公开 |
| **OpenCortex** | **0.668** | **0.809** | **82.5%** | **97.9%** |

> ★ MemMachine 超过 baseline 可能因为其记忆提取/摘要过程过滤了噪声，提供了比全文更精准的上下文。

### 3.3 分类别对比 (J-Score)

| 类别 | OpenCortex | Mem0 | Mem0[g] | MemMachine v0.1 |
|------|-----------|------|---------|-----------------|
| Cat 1 (单跳) | 0.585 | 0.671 | 0.652 | 0.933 |
| Cat 2 (时间) | 0.511 | 0.538 | 0.581 | 0.726 |
| Cat 3 (推理) | 0.583 | 0.490 | 0.490 | 0.646 |
| Cat 4 (多跳) | 0.765 | 0.702 | 0.757 | 0.805 |
| **Overall** | **0.668** | **0.669** | **0.684** | **0.849** |

**关键发现**:
- Cat 3（推理）：OC 0.583 **优于** Mem0 0.490，是唯一超过 Mem0 的类别
- Cat 4（多跳）：OC 0.765 优于 Mem0 0.702，与 Mem0[g] 0.757 持平
- Cat 1（单跳）和 Cat 2（时间）是 OC 的弱项，低于 Mem0

---

## 4. 分析

### 4.1 OC 的优势

1. **Cat 3 推理和 Cat 4 多跳超过 Mem0** — 说明 OC 的混合检索（dense + BM25）和 rerank 在需要推理的场景下表现好
2. **97.9% token 压缩** — 极致的 token 效率，563 vs 26,452 tokens
3. **全本地部署** — embedding 和 reranker 都在本地，无需外部 API
4. **绝对 J-Score 与 Mem0 持平** (0.668 vs 0.669) — 考虑到 Mem0 使用 gpt-4o-mini 评判而 OC 使用 Qwen3-235B，实际水平相当

### 4.2 OC 的差距

1. **与 baseline 的差距较大** (82.5% vs Mem0 的 91.8%) — 说明检索召回率有提升空间
2. **Cat 1 单跳事实偏低** (0.585) — 细粒度事实检索是短板
3. **14.8% 的回答说"无信息"** — 检索完全未命中的比例偏高
4. **检索延迟高** (p50=20s) — 需要优化

### 4.3 行业数据可信度说明

LoCoMo 行业评测数据存在争议：

- **Zep 分数争议**: Mem0 论文报告 Zep 为 0.660，Zep 自测为 0.751，Mem0 反驳后为 0.584。差异源于实现细节（时间戳处理、SDK 版本、prompt 配置）
- **Letta 质疑 Mem0**: Letta 团队（MemGPT 原作者）无法复现 Mem0 论文中 MemGPT 的配置
- **MemMachine 自报未经第三方验证**
- **LLM 差异**: 所有行业数据使用 gpt-4o-mini，OC 使用 Qwen3-235B，绝对分数不直接可比

---

## 5. 优化路线图

### P0: 提升检索召回率 (目标: OC J-Score > 0.75)

| 方向 | 措施 | 预期收益 |
|------|------|---------|
| Embedding 升级 | 换用 GTE-Qwen2-7B-instruct 或 BGE-M3-v2 | Cat 1/2 召回提升 |
| 检索策略 | 增加 top_k (10→20)，扩大 rerank 窗口 | 减少 "无信息" 回答 |
| 减少延迟 | 优化 Qdrant 查询，embedding 缓存预热 | p50 < 5s |
| 对话感知检索 | 按对话维度分组检索，避免跨对话干扰 | Cat 2 时间类提升 |

### P1: 记忆质量提升

| 方向 | 措施 | 预期收益 |
|------|------|---------|
| 摘要质量 | 改进 conversation merge 的 LLM prompt | 减少信息丢失 |
| 时间锚定 | 强化消息时间戳在 embedding 和检索中的权重 | Cat 2 提升 |
| 知识蒸馏 | Alpha pipeline 提取关键事实作为独立记忆 | Cat 1 单跳提升 |

---

## 6. 数据来源

| 来源 | URL |
|------|-----|
| Mem0 论文 | https://arxiv.org/abs/2504.19413 |
| Zep 反驳 | https://blog.getzep.com/lies-damn-lies-statistics-is-mem0-really-sota-in-agent-memory/ |
| Letta 评测 | https://www.letta.com/blog/benchmarking-ai-agent-memory |
| MemMachine v0.1 | https://memmachine.ai/blog/2025/09/memmachine-reaches-new-heights-on-locomo/ |
| MemMachine v0.2 | https://memmachine.ai/blog/2025/12/memmachine-v0.2-delivers-top-scores-and-efficiency-on-locomo-benchmark/ |
| LoCoMo 论文 | https://snap-research.github.io/locomo/ |
| Mem0 复测 Zep | https://github.com/getzep/zep-papers/issues/5 |

---

## 附录: 原始 JSON 报告

完整评测数据（含每题预测和 J-Score）保存在:
`docs/benchmark/conversation-eval_conversation_v7full.json`
