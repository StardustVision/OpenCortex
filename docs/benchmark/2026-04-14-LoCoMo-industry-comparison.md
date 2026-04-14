# 2026-04-14 LoCoMo 行业对照（OpenCortex 全量）

## 1. 本次评测信息

- 日期: 2026-04-14
- Run ID: `eval_conversation_2c8112d7`
- 原始报告: `docs/benchmark/conversation-eval_conversation_2c8112d7.json`
- 数据集: `benchmarks/locomo10.json`（1,986 QA）
- LLM: `gpt-4o-mini`
- top_k: `10`
- 并发: `3`

## 2. OpenCortex 本次结果（全量）

| 指标 | 数值 |
| --- | ---: |
| Recall@1 | 0.1445 |
| Recall@3 | 0.2734 |
| Recall@5 | 0.3536 |
| MRR | 0.2714 |
| J-Score（Cat1-4） | 0.5156 |
| F1（overall） | 0.2453 |
| Token 压缩率 | 94.7% |
| 召回延迟 p50 | 5761.4 ms |
| 召回延迟 p95 | 16265.4 ms |

## 3. 与本次 Full-Context Baseline 对照

| 指标 | Baseline | OpenCortex | Delta | 比率（OC/BL） |
| --- | ---: | ---: | ---: | ---: |
| J-Score（Cat1-4） | 0.7818 | 0.5156 | -0.2662 | 66.0% |
| F1（overall） | 0.4199 | 0.2453 | -0.1746 | 58.4% |
| 平均 tokens | 26445 | 1415 | -25030 | 5.3%（压缩 94.7%） |

## 4. 与业界公开结果对照（J-Score）

> 业界数值来自既有对照文档 `docs/benchmark/jscore-industry-comparison.md`（2026-03-16 版本）。  
> 该表用于定位差距，不代表同机同脚本严格复现。

| 系统 | J-Score | 与 OpenCortex 差值（OC - 对方） |
| --- | ---: | ---: |
| MemMachine v0.1 | 0.849 | -0.3334 |
| Zep（自测） | 0.751 | -0.2354 |
| Letta（file-based） | 0.740 | -0.2244 |
| Mem0[g] | 0.684 | -0.1684 |
| Mem0 | 0.669 | -0.1534 |
| Zep（Mem0 rebuttal） | 0.584 | -0.0684 |
| **OpenCortex（本次）** | **0.5156** | **0.0000** |

## 5. 分类别对照（J-Score）

| 类别 | OpenCortex（本次） | Baseline（本次） | Mem0（历史） | Mem0[g]（历史） |
| --- | ---: | ---: | ---: | ---: |
| Cat 1（单跳） | 0.4291 | 0.6667 | 0.671 | 0.652 |
| Cat 2（时间） | 0.2087 | 0.5670 | 0.538 | 0.581 |
| Cat 3（推理） | 0.4896 | 0.6146 | 0.490 | 0.490 |
| Cat 4（多跳） | 0.6647 | 0.9215 | 0.702 | 0.757 |

结论：

- Cat 2（时间）是当前最大短板。
- Cat 3（推理）与 Mem0 基本持平，但明显低于本次 baseline。
- Cat 4（多跳）有一定能力，但尚未达到历史竞品区间。

## 6. 可比性边界（必须注意）

- 本文档中的业界数据来自公开资料与既有内部整理，存在实现细节差异。
- 不同系统的 ingest 规则、时间戳处理、prompt 模板、评测脚本版本可能不同。
- 因此，最稳妥的决策依据仍是:
  1. OpenCortex 自身版本间对比（同脚本同数据同模型）。
  2. OpenCortex 与本次 full-context baseline 的相对比率。

## 7. 当前结论（面向决策）

- 现在的核心矛盾不是 token 成本，token 已经足够低（94.7% 压缩）。
- 当前瓶颈是有效召回到可答证据的比例，尤其是时间类问题（Cat 2）。
- 下一步优先级应聚焦：
  1. 时间锚点强约束（ingest 与 query 两侧统一）。
  2. 会话内证据去噪（降低 cone 引入的跨会话噪声）。
  3. rerank 前置过滤（按会话/对象候选分桶后再重排）。

