# 2026-04-14 LoCoMo 检索报告（Ground Truth 修正后）

## 1. 本次结果

- 数据集: `benchmarks/locomo10.json`
- QA 数: `1986`
- 运行方式: `ingest + retrieve only`
- 检索方法: `context_recall`
- 新报告: `docs/benchmark/conversation-eval_conversation_f82bef07.json`
- 对照旧报告: `docs/benchmark/conversation-eval_conversation_e84524a7.json`

## 2. Ground Truth 修正内容

本次修正不再把一个 conversation 下的全部新 URI 都塞给该 conversation 的所有 QA。

新逻辑改为：

1. LoCoMo `evidence_sessions` 先定位到内部 `session_N`。
2. ingest 后读取最终 merged records 的 `meta.msg_range`。
3. 对每个 `session_N`，优先选择“覆盖该 session 的最窄 merged record”作为 ground truth。
4. 只有 `msg_range` 不可用时，才退回到 `time_refs` 对齐。

这会把 ground truth 从“整段会话全量命中”修正为“该问题真正应命中的 merged 证据”。

## 3. 关键指标

| 指标 | 修正后 |
| --- | ---: |
| Recall@1 | 0.0446 |
| Recall@3 | 0.1107 |
| Recall@5 | 0.1682 |
| Precision@1 | 0.0720 |
| Precision@3 | 0.0619 |
| Precision@5 | 0.0580 |
| HitRate@1 | 0.0720 |
| HitRate@3 | 0.1717 |
| HitRate@5 | 0.2467 |
| MRR | 0.1686 |
| NDCG@5 | 0.1331 |
| 延迟 p50 | 12234.3 ms |
| 延迟 p95 | 31510.5 ms |
| 延迟均值 | 14349.3 ms |

分类型 Recall@5:

- Cat 1: `0.1583`
- Cat 2: `0.1822`
- Cat 3: `0.1993`
- Cat 4: `0.1639`
- Cat 5: `0.1659`

## 4. 与错误 Ground Truth 版本对照

旧报告 `conversation-eval_conversation_e84524a7.json` 的问题是：

- 平均 `expected_uris` 数量: `17.996`
- 中位数 `expected_uris`: `18`

修正后变为：

- 平均 `expected_uris` 数量: `1.7321`
- 中位数 `expected_uris`: `1`

对应指标变化：

| 指标 | 旧错误版本 | 修正后 | Delta |
| --- | ---: | ---: | ---: |
| Recall@1 | 0.0105 | 0.0446 | +0.0341 |
| Recall@3 | 0.0321 | 0.1107 | +0.0786 |
| Recall@5 | 0.0599 | 0.1682 | +0.1083 |
| Precision@1 | 0.1757 | 0.0720 | -0.1037 |
| Precision@3 | 0.1809 | 0.0619 | -0.1190 |
| Precision@5 | 0.2023 | 0.0580 | -0.1443 |
| HitRate@1 | 0.1757 | 0.0720 | -0.1037 |
| HitRate@3 | 0.2598 | 0.1717 | -0.0881 |
| HitRate@5 | 0.3635 | 0.2467 | -0.1168 |
| MRR | 0.2612 | 0.1686 | -0.0926 |

## 5. 如何解读这些变化

`Recall@k` 上升是预期结果：

- 旧版分母被错误放大到了一个问题对应十几条 URI。
- 修正后，一个问题通常只对应 1 条主证据，少数对应 2~5 条。
- 因此旧版 `Recall@k` 明显低估了真实召回。

`Precision/HitRate/MRR` 下降也不是异常，而是暴露了真实问题：

- 旧版 ground truth 很宽，只要命中会话里任意一个 URI 就算对。
- 修正后必须命中“该问题对应的那条主 merged 证据”。
- 所以现在看到的是更严格、更真实的排序质量。

换句话说：

- 旧版主要低估了“有没有召回到相关会话”。
- 修正版开始真实衡量“有没有把最该命中的那条证据排到前面”。

## 6. 当前结论

- LoCoMo 的 ground truth 失真已经修正。
- 修正后，OpenCortex 的真实检索水平大致是：
  - `Recall@5 = 0.1682`
  - `HitRate@5 = 0.2467`
  - `MRR = 0.1686`
- 这说明系统已经能在一部分问题上找到正确证据，但前排排序仍然偏弱。
- 当前 retrieval 路径的另一个明确问题是时延仍高：
  - `p50 = 12.2s`
  - `p95 = 31.5s`

## 7. 可直接引用的文件

- 修正后检索结果: `docs/benchmark/conversation-eval_conversation_f82bef07.json`
- 旧错误版本: `docs/benchmark/conversation-eval_conversation_e84524a7.json`
