# OpenCortex Benchmark v2 — 完整基准测试方案设计

> 目标：建立覆盖记忆、对话、文档、知识库、技能、认知生命周期的全维度基准测试体系，对齐 LoCoMo / LongMemEval / PersonaMem 等行业标准，并定义竞品不具备的差异化评测维度。

## 一、现状评估

### 已有（可复用）

| 组件 | 文件 | 状态 |
|------|------|------|
| 统一评估框架 | `benchmarks/unified_eval.py` (695行) | ✅ 完整 |
| 评分系统 | `benchmarks/scoring.py` (F1, EM, J-Score) | ✅ 完整 |
| 指标聚合 | `benchmarks/metrics.py` (Recall@k, MRR, latency) | ✅ 完整 |
| OC/LLM 客户端 | `benchmarks/oc_client.py`, `llm_client.py` | ✅ 完整 |
| 报告生成 | `benchmarks/report.py`, `analyze_eval.py` | ⚠️ 有 bug |
| Memory adapter | `benchmarks/adapters/memory.py` (PersonaMem v2) | ✅ 完整 |
| Conversation adapter | `benchmarks/adapters/conversation.py` (LoCoMo + LongMemEval) | ✅ 完整 |
| Document adapter | `benchmarks/adapters/document.py` (QASPER/LongBench/CMRC) | ✅ 完整 |
| HotPotQA adapter | `benchmarks/adapters/hotpotqa.py` | ✅ 完整 |
| 本地数据集 | LoCoMo, PersonaMem v2, HotPotQA, QASPER | ✅ 本地已有 |

### 缺失（需新建）

| 维度 | 现状 |
|------|------|
| LongMemEval 数据集 | adapter 代码有，数据集未下载 |
| ConvoMem 数据集 | 无 adapter，无数据 |
| Knowledge 质量评测 | 完全空白 |
| Skill 质量评测 | 完全空白 |
| 认知生命周期评测 | 完全空白 |
| MCP 生命周期评测 | 仅 LongMemEval fallback 使用 |
| 系统性能评测 | 无吞吐量/写入延迟/冷启动 |
| 高级指标 | 无 NDCG/MAP/BLEU/ROUGE |

### 已知 Bug

1. `analyze_eval.py:53` — `cat_names` 引用先于定义，J-Score 分析会 NameError
2. QASPER `data.json` 缺失 — `--mode all` 会失败
3. deprecated `locomo_eval.py` (951行) 增加维护负担

---

## 二、评测维度设计（8 大维度）

```
                    ┌─────────────────────────────────────┐
                    │      OpenCortex Benchmark v2        │
                    └──────────────┬──────────────────────┘
                                   │
        ┌──────────┬───────────┬───┴───┬──────────┬──────────┐
        │          │           │       │          │          │
    ┌───┴───┐ ┌───┴───┐ ┌─────┴──┐ ┌─┴──┐ ┌─────┴──┐ ┌─────┴──┐
    │Memory │ │Conv   │ │Document│ │Know│ │Skill   │ │Cogni- │
    │Recall │ │Memory │ │RAG    │ │Qlty│ │Qlty    │ │tive   │
    └───────┘ └───────┘ └────────┘ └────┘ └────────┘ │Lifecycle│
                                                  └────────┘
        ┌──────────┬──────────────────────────────────────┐
        │MCP       │System                                │
        │Lifecycle │Performance                           │
        └──────────┴──────────────────────────────────────┘
```

### 维度 1：Memory Recall（记忆召回）

**对标**：PersonaMem v2 (COLM 2025)、Mem0 论文评测

**数据集**：PersonaMem v2（已有本地）

**评测流程**：
1. Ingest 用户画像历史（7 类别：basic_info, preference, routine, plan, relationship, ask_to_forget, sensitive_info）
2. 对每个 QA item 执行 search()
3. LLM 基于检索结果生成答案
4. 评分：F1 (token overlap) + J-Score (LLM-as-Judge binary)

**指标**：
| 指标 | 含义 | 竞品对标 |
|------|------|---------|
| J-Score | LLM judge 准确率 | Mem0: 91% (LoCoMo), 对标主指标 |
| F1 | Token overlap | LoCoMo 官方指标 |
| Recall@1/3/5 | 检索命中率 | 通用检索指标 |
| MRR | 平均倒数排名 | 通用检索指标 |
| Token Reduction | 相对全上下文的压缩率 | Mem0: 90%+ |
| Category Breakdown | 7 类别分别的得分 | PersonaMem 特色 |

**vs 现有改进**：
- 增加 NDCG@5（分级相关性）
- 增加 per-category 置信区间（bootstrap 1000 次）
- 增加 ask_to_forget 验证（确认删除后确实不可检索）

---

### 维度 2：Conversation Memory（对话记忆）

**对标**：LoCoMo (ACL 2024)、LongMemEval (ICLR 2025)、ConvoMem (Salesforce)

**数据集**：
| 数据集 | 规模 | 重点 | 状态 |
|--------|------|------|------|
| LoCoMo | 10 convos, 1986 QA | 5 类推理（单跳/多跳/时序/常识/对抗） | ✅ 已有 |
| LongMemEval | 500 QA, up to 500 sessions | 5 能力维度 + abstention | ⬇️ 需下载 |
| ConvoMem | 75,336 QA | 大规模 + 6 类证据 | ⬇️ 新增 |

**评测流程**（以 LongMemEval 为例）：
1. 按时间顺序 ingest sessions（通过 MCP context_commit 或 store）
2. 对每个问题：context_recall → LLM 生成答案 → 评分
3. Session 结束时 context_end → 触发 Alpha pipeline
4. 评估跨 session 的知识累积效果

**指标**：
| 指标 | 含义 | LoCoMo 官方 | LongMemEval |
|------|------|------------|-------------|
| F1 | Token overlap | ✅ 主指标 | ✅ |
| J-Score | LLM-as-Judge | ✅ (Mem0 对标) | ✅ |
| BLEU-1 | 生成质量 | ✅ (LoCoMo 官方) | — |
| Per-Category | 5 类推理分别 | ✅ | ✅ 5 能力 |
| Abstention | 正确拒绝率 | — | ✅ 独立维度 |
| Cross-Session | 跨 session 召回 | — | ✅ |

**vs 现有改进**：
- LoCoMo 适配器当前用 store()+search()，需增加 MCP 生命周期路径
- 新增长 Bleu-1 评分
- LongMemEval 的 abstention 维度测试"知道何时不该回答"

---

### 维度 3：Document RAG（文档检索增强）

**对标**：HotPotQA (EMNLP 2018)、QASPER (NAACL 2021)

**数据集**：
| 数据集 | 规模 | 重点 | 状态 |
|--------|------|------|------|
| HotPotQA | 7405 QA | 多跳推理 | ✅ 已有 |
| QASPER | 5049 QA | 学术论文全文理解 | ⚠️ 需修复 data.json |
| LongBench | — | 长文档 | adapter 已有 |

**评测流程**：
1. 通过 document mode ingest 全文（heading-based chunking）
2. 对每个问题：search() → LLM 生成答案 → 评分
3. HotPotQA 额外评分：EM, SP F1, Joint F1

**指标**：
| 指标 | 含义 | HotPotQA 官方 | QASPER |
|------|------|--------------|--------|
| EM | 精确匹配 | ✅ | — |
| F1 | Token overlap | ✅ | ✅ 主指标 |
| SP F1 | 支持文档命中率 | ✅ | — |
| Joint F1 | EM × SP | ✅ | — |
| Recall@5 | 检索质量 | ✅ | ✅ |

**vs 现有改进**：
- 修复 QASPER data.json 生成脚本
- 增加 chunk 质量评估（chunk 边界是否合理）

---

### 维度 4：Knowledge Quality（知识质量）⭐ 新增

**对标**：无行业标准，OpenCortex 独创维度

**设计思路**：评估 Archivist 从 trace 中提取知识的质量——这是 OpenCortex 最核心的差异化能力，但当前完全没有评测。

**评测流程**：
1. 准备一组"种子对话"（人工标注的对话 trace + 期望知识输出）
2. 通过 MCP lifecycle 完整跑一轮（prepare → commit → end → Alpha pipeline）
3. 等待 Archivist 产出知识候选
4. 对比产出 vs 期望：

**评测子维度**：

| 子维度 | 指标 | 含义 |
|--------|------|------|
| 提取完整性 | Knowledge Recall | 期望知识点中被提取出的比例 |
| 提取准确性 | Knowledge Precision | 提取的知识中正确的比例 |
| 知识类型分类 | Type Accuracy | belief/SOP/negative_rule/root_cause 分类准确率 |
| 去重质量 | Dedup Precision | 相似知识是否被正确合并 |
| Scope 隔离 | Scope Isolation | User/Tenant/Global 知识是否正确隔离 |
| 幻觉率 | Hallucination Rate | 提取的知识中有多少是原文不存在的 |

**数据集构建**：
- 基于 LoCoMo 对话，人工标注 50-100 条期望知识
- 分类覆盖 4 种知识类型（belief, SOP, negative_rule, root_cause）
- 包含边界 case：冲突信息、部分正确、需要推理才能得出的知识

---

### 维度 5：Skill Quality（技能质量）⭐ 新增

**对标**：无行业标准，OpenCortex 独创维度

**评测流程**：
1. 准备一组"技能种子场景"（对话 trace + 期望技能描述）
2. 通过 Skill Engine 提取技能候选
3. QualityGate + SandboxTDD 评分
4. 对比产出 vs 期望：

**指标**：
| 指标 | 含义 |
|------|------|
| Skill Extraction Recall | 期望技能中被提取出的比例 |
| Skill Extraction Precision | 提取的技能中正确/有用的比例 |
| Skill Ranking NDCG | 技能排序质量 |
| Quality Gate Precision | 通过 QG 的技能中真正可用的比例 |
| Sandbox TDD Pass Rate | TDD 验证的通过率 |

---

### 维度 6：Cognitive Lifecycle（认知生命周期）⭐ 新增

**对标**：无行业标准，OpenCortex 独创维度

**设计思路**：评估 Autophagy Kernel 的记忆生命周期管理是否正确。

**评测流程**：
1. 创建一组有明确生命周期的记忆（高频/中频/低频/过时/冲突）
2. 模拟一系列 recall 事件（reinforce/penalize/contest）
3. 触发 metabolism sweep
4. 验证认知状态转换是否符合预期

**测试场景**：

| 场景 | 预期行为 | 验证点 |
|------|---------|--------|
| 高频被引用的记忆 | activation_score 持续上升 | reinforce 增益饱和递减 |
| 冷门但高价值记忆 | 不被压缩（stability 高） | value_score 守门 |
| 过时低价值记忆 | ACTIVE → COMPRESSED → ARCHIVED | 阈值触发正确 |
| 冲突标记的记忆 | ExposureState → CONTESTED | contest 信号正确 |
| 受保护记忆 | 衰减率 0.99 vs 0.95 | protected 标志有效 |
| 知识整合候选 | ConsolidationState 正确流转 | fingerprint 去重有效 |
| Sweep 分页 | 大量记忆分页处理无遗漏 | cursor 正确推进 |

**指标**：
| 指标 | 含义 |
|------|------|
| Transition Accuracy | 状态转换正确率 |
| Score Evolution MAE | activation/stability 演化与预期均方误差 |
| Sweep Completeness | sweep 处理的覆盖率 |
| False Positive Rate | 被错误压缩/归档的比例 |
| False Negative Rate | 应被压缩但未压缩的比例 |

---

### 维度 7：MCP Lifecycle（MCP 生命周期）

**评测流程**：
1. 通过 MCP 完整跑 prepare → commit → end
2. 验证每个阶段的副作用：
   - prepare：缓存命中、session 自动创建、意图路由正确性
   - commit：Observer 记录、即时写入、奖励评分
   - end：缓冲区刷盘、即时记录清理、Alpha pipeline 触发、autophagy metabolism tick

**指标**：
| 指标 | 含义 |
|------|------|
| Prepare Idempotency | 重复 prepare 是否返回缓存 |
| Commit Durability | commit 后数据是否确实写入 |
| End Cleanup | end 后 session 状态是否完全清理 |
| End-to-End Latency | prepare → end 总耗时 |
| Context Quality | prepare 返回的上下文是否包含相关信息 |

---

### 维度 8：System Performance（系统性能）

**指标**：
| 指标 | 含义 | 目标 |
|------|------|------|
| Search p50/p95/p99 | 检索延迟 | p95 < 500ms |
| Ingest Throughput | 写入 QPS | > 100 items/s |
| Time-to-Available | store() 后到 search() 可查到 | < 100ms |
| Concurrent QPS | 并发检索吞吐量 | > 50 QPS |
| Cold Start | 服务器启动到首次查询 | < 10s |
| Memory Footprint | 服务器内存占用 | 监控 |

---

## 三、竞品对齐对照表

| 评测维度 | LoCoMo | LongMemEval | Mem0 | Zep | Hindsight | **OpenCortex** |
|----------|--------|-------------|------|-----|-----------|----------------|
| Memory Recall | — | — | ✅ | ✅ | ✅ | **✅ PersonaMem** |
| Conversation QA | ✅ 主场 | ✅ 主场 | ✅ 91% | ✅ 58% | ✅ 90% | **✅ LoCoMo + LongMemEval** |
| Document RAG | — | — | — | — | — | **✅ HotPotQA + QASPER** |
| Knowledge Quality | — | — | — | — | — | **⭐ 独有** |
| Skill Quality | — | — | — | — | — | **⭐ 独有** |
| Cognitive Lifecycle | — | — | — | — | — | **⭐ 独有** |
| Temporal Reasoning | ✅ | ✅ | — | ✅ | ✅ | **✅ (via LoCoMo/LME)** |
| Conflict Resolution | — | ✅ (abstention) | — | — | — | **✅ (via contest)** |
| Multi-hop | ✅ | ✅ | — | — | — | **✅ HotPotQA** |

---

## 四、实施计划

### Phase 0：修复 & 准备（预计 1-2 天）

- [ ] 修复 `analyze_eval.py` cat_names bug
- [ ] 生成 QASPER `data.json`（写转换脚本）
- [ ] 下载 LongMemEval 数据集
- [ ] 清理 deprecated `locomo_eval.py`
- [ ] 在 scoring.py 中增加 BLEU-1 计算
- [ ] 在 metrics.py 中增加 NDCG@k 计算
- [ ] 增加 bootstrap 置信区间计算

### Phase 1：行业标准对齐（预计 3-5 天）

- [ ] **LongMemEval 适配器完善**：确保 ConversationAdapter 支持 LongMemEval 全部 5 个能力维度，特别是 abstention 测试
- [ ] **LoCoMo MCP 路径**：在 ConversationAdapter 中增加 MCP lifecycle 路径选项（prepare → commit → end），与现有 store+search 路径对比
- [ ] **完整 QASPER 运行**：修复 data.json 后跑完整 QASPER dev set（不只是 10 QA）
- [ ] **统一评测运行**：`python unified_eval.py --mode all --enable-llm-judge`，产出完整报告

### Phase 2：知识 & 技能质量评测（预计 3-5 天）

- [ ] **构建知识评测数据集**：
  - 基于 LoCoMo 对话，标注 50-100 条期望知识
  - 覆盖 4 种知识类型 + 边界 case
  - 存为 `benchmarks/datasets/knowledge/gold_standard.json`

- [ ] **新建 KnowledgeAdapter** (`benchmarks/adapters/knowledge.py`)：
  - ingest 种子对话 → 触发 Alpha pipeline → 等待 Archivist 产出
  - 对比 Archivist 输出 vs gold standard
  - 计算 Knowledge Recall/Precision/Type Accuracy/Hallucination Rate

- [ ] **构建技能评测数据集**：
  - 设计 20-30 个技能提取场景
  - 存为 `benchmarks/datasets/skills/gold_standard.json`

- [ ] **新建 SkillAdapter** (`benchmarks/adapters/skill.py`)：
  - 通过 Skill Engine 提取 → QualityGate 评分 → 对比期望
  - 计算 Skill Recall/Precision/Ranking NDCG

### Phase 3：认知生命周期评测（预计 2-3 天）

- [ ] **构建生命周期测试场景** (`benchmarks/adapters/lifecycle.py`)：
  - 7 个测试场景（高频/冷门/过时/冲突/受保护/整合/sweep）
  - 直接调用 AutophagyKernel API
  - 验证状态转换和分数演化

- [ ] **认知生命周期指标**：
  - Transition Accuracy, Score MAE, Sweep Completeness
  - False Positive/Negative Rate

### Phase 4：系统性能评测（预计 1-2 天）

- [ ] **性能测试脚本** (`benchmarks/perf_bench.py`)：
  - 检索延迟分布（不同 collection 大小：1K/10K/100K）
  - 写入吞吐量（单线程 vs 并发）
  - Time-to-Available 测量
  - 冷启动时间
  - 并发 QPS 压力测试

### Phase 5：报告 & 发布（预计 1-2 天）

- [ ] **统一报告格式**：所有维度整合为单一 JSON + Markdown 报告
- [ ] **竞品对比表**：与 Mem0/Zep/Hindsight 在相同数据集上的公开数据对比
- [ ] **README benchmark section**：在 README 中添加 benchmark 结果章节

---

## 五、文件结构（新增/修改）

```
benchmarks/
  unified_eval.py              # 修改：增加 knowledge/skill/lifecycle/perf 模式
  scoring.py                   # 修改：增加 BLEU-1, NDCG 计算
  metrics.py                   # 修改：增加 bootstrap CI
  report.py                    # 修改：支持多维度报告
  analyze_eval.py              # 修复：cat_names bug
  perf_bench.py                # 新增：性能基准测试
  adapters/
    knowledge.py               # 新增：知识质量评测适配器
    skill.py                   # 新增：技能质量评测适配器
    lifecycle.py               # 新增：认知生命周期评测适配器
    conversation.py            # 修改：增加 MCP lifecycle 路径
  datasets/
    longmemeval/               # 新增：LongMemEval 数据集
      data.json
    knowledge/                 # 新增：知识评测 gold standard
      gold_standard.json
    skills/                    # 新增：技能评测 gold standard
      gold_standard.json
    qasper/
      data.json                # 新增：转换后的 QASPER 数据
      convert.py               # 新增：QASPER 原始格式转换脚本
  results/                     # 新增：统一结果存放目录
    benchmark-v2-report.md     # 最终报告
```

---

## 六、验证方案

1. **Phase 0 验证**：`python -m pytest tests/test_eval_*.py -v` 全部通过
2. **Phase 1 验证**：
   - `python unified_eval.py --mode memory --dataset personamem` → 产出 J-Score + F1 + Recall
   - `python unified_eval.py --mode conversation --dataset longmemeval_s` → 产出 5 能力维度分数
   - `python unified_eval.py --mode document --dataset hotpotqa --limit 100` → 产出 EM/F1/SP
3. **Phase 2 验证**：KnowledgeAdapter 产出 Knowledge Recall > 0 且有 per-type breakdown
4. **Phase 3 验证**：LifecycleAdapter 所有 7 场景的 Transition Accuracy 可计算
5. **Phase 4 验证**：perf_bench.py 产出延迟分布和吞吐量数据
6. **Phase 5 验证**：最终报告包含所有 8 个维度的数据 + 竞品对比表

---

## 七、优先级建议

**最高优先级（必须做，对齐竞品）**：
1. Phase 0 修复
2. Phase 1 LongMemEval + LoCoMo MCP 路径 + 完整 QASPER

**高优先级（差异化竞争力）**：
3. Phase 2 知识质量评测（独有维度）
4. Phase 3 认知生命周期评测（独有维度）

**中优先级**：
5. Phase 4 系统性能
6. Phase 5 报告 & 发布

**低优先级**：
7. 技能质量评测（Skill Engine 尚未成熟）
8. ConvoMem 大规模评测（数据集获取和适配工作量大）
