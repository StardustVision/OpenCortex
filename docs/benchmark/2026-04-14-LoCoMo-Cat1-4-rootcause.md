# 2026-04-14 LoCoMo Cat1-4 深度排查（证据版）

## 1. 结论先行

Cat1-4 同时偏弱不是单点问题，而是三段叠加：

1. **写入阶段丢失关键时间信号**（Cat2 主因）。
2. **评测检索固定 `l0`，证据粒度过粗**（Cat1/4 主因，Cat3 次因）。
3. **隔离 collection 下 immediate 清理打到错误集合名**，导致检索混入大量 immediate 片段（影响 Cat1-4 稳定性）。

---

## 2. 本次样本与结果基线

- 报告：`docs/benchmark/conversation-eval_conversation_2c8112d7.json`
- 数据：LoCoMo 10 对话 / 1986 QA（Cat1-4 共 1540）
- 本次 J-Score（Cat1-4）：
  - Cat1: `0.4291`（BL `0.6667`，`-0.2376`）
  - Cat2: `0.2087`（BL `0.5670`，`-0.3583`）
  - Cat3: `0.4896`（BL `0.6146`，`-0.1250`）
  - Cat4: `0.6647`（BL `0.9215`，`-0.2568`）

---

## 3. 关键诊断指标（按类别）

说明：
- `Exact Hit@5`：top5 是否命中 `expected_uris`（严格 URI）。
- `Session Hit@5`：top5 是否命中同一 `session_id`（放宽到同会话）。
- `Recovered`：`Session Hit` 但 `Exact Hit` 失败的比例（多数是命中 immediate 而非 merged URI）。

| Cat | N | Exact Hit@5 | Session Hit@5 | Recovered | Top1 Immediate | Any Immediate in Top5 |
|---|---:|---:|---:|---:|---:|---:|
| 1 | 282 | 0.507 | 0.681 | 0.174 | 0.330 | 0.496 |
| 2 | 321 | 0.489 | 0.679 | 0.190 | 0.374 | 0.660 |
| 3 | 96 | 0.323 | 0.469 | 0.146 | 0.500 | 0.688 |
| 4 | 841 | 0.372 | 0.671 | 0.298 | 0.452 | 0.671 |

核心观察：
- 严格 URI 命中偏低，但**同会话命中显著更高**，说明很多时候“找到了会话但没命中 expected merged URI”。
- top5 中 immediate 占比很高（尤其 Cat2/3/4），检索上下文质量不稳定。

---

## 4. Cat2 为什么最差（最强证据）

### 4.1 数据源有绝对时间，但写入链路没保住

LoCoMo 源数据 272/272 session 都有 `session_N_date_time`：

- 例：`benchmarks/locomo10.json:1651` -> `"session_1_date_time": "1:56 pm on 8 May, 2023"`

但 ingest 时 commit 消息只写 speaker + text，没有 session datetime：

- `benchmarks/adapters/locomo.py:194-201`

`ContextMessage` 也只有 `role/content`，无 timestamp 字段：

- `src/opencortex/http/models.py:176-179`

### 4.2 存储后时间结构化几乎缺失

在本次 run 对应落盘对象（`data_cone_bench/eval_conversation_2c8112d7/...`）统计：

- `.abstract.json` 总数：6238
- `slots.event_date`：`0`（0%）
- `slots.time_refs`：`912`（14.6%）
- merged（有 `.overview.md`）记录中，`content.md` 出现显式时间 token 比例仅 `2.8%`，时间点 token 基本为 0。

这与源数据“100% session 有绝对时间”明显不一致。

### 4.3 结果层面直接体现为“相对时间回答”

Cat2 中：

- gold 含绝对时间 token 比例：`80.4%`
- OC 预测含绝对时间 token 比例：`2.2%`
- OC 预测含相对时间词（yesterday/last/next/recent）比例：`55.5%`

即使命中同会话，Cat2 仍低：

- `Session Hit` 子集：OC J `0.243` vs BL J `0.587`

结论：Cat2 不是“只要召回就能解决”，而是**写入阶段没有保留绝对时间锚点**。

---

## 5. Cat1/3/4 为什么也弱

### 5.1 评测强制 `detail_level=\"l0\"`，证据深度被钉死

LoCoMo adapter 检索固定传 `l0`：

- `benchmarks/adapters/locomo.py:285-291`

这会把回答上下文限制在 abstract 级别。对于 Cat1 精确槽位、Cat4 多事实组合、Cat3推理补证都不够。

### 5.2 merged 摘要对细节有损（对精确问答不友好）

常见失败形态：

- 命中会话但答案“语义接近、细节不对”（人名、数量、事件名、具体措辞）。
- 典型例子：问演出者，摘要里同时提到 Matt Patterson 和乐队语境，模型答成乐队名。

### 5.3 检索混入 immediate 片段，路径不稳定

`Any Immediate in Top5` 高达 49.6%~68.8%。  
这导致：

- 有时 immediate 片段能提供局部事实（短期看似有利）。
- 但大量片段化证据会挤占 merged 上下文预算，导致跨条目整合能力下降。

---

## 6. 代码级根因定位

### 根因 A：benchmark 下 immediate 清理命中错误 collection

评测使用隔离 collection（`bench_xxx`）：

- `benchmarks/unified_eval.py:326,334`

但 ContextManager 清理 immediate 时写死 `"context"`：

- `src/opencortex/context/manager.py:805-808`
- `src/opencortex/context/manager.py:846-852`

结果：在隔离 collection 中清理失效，immediate 长期参与检索。

### 根因 B：时间提取规则弱，且依赖文本碰运气

时间 regex 仅覆盖少量模式：

- `src/opencortex/memory/mappers.py:25-28`
- `src/opencortex/intent/planner.py:22-25`

且 `mappers` 在无显式 `meta.time_refs` 时只从 abstract/overview/content 正则抽取：

- `src/opencortex/memory/mappers.py:318-332`

当 ingest 未写入绝对时间时，后续很难补救。

### 根因 C：LoCoMo ingest 未透传 session datetime

- `benchmarks/adapters/locomo.py:194-201`（消息文本仅 speaker + text）
- `src/opencortex/orchestrator.py:912-937`（immediate 写入 meta 无 event_date/time_refs）

---

## 7. 优先级建议（非 benchmark hack，通用收益）

## P0（先做）

1. **修复 collection 硬编码**
   - 把 ContextManager 中 `batch_delete("context", ...)` 改成当前 request collection（与 `_get_collection()` 对齐）。
2. **打通时间字段写入**
   - 给 `ContextMessage` 增加可选时间字段（如 `timestamp`/`event_date`）。
   - LoCoMo ingest 透传 `session_date_time`。
   - commit/merge 时写入 `meta.time_refs` + `event_date`，不要只靠 regex。
3. **LoCoMo 检索不要强钉 `l0`**
   - 让 planner 决定深度，或至少允许 Cat3/4 进入 `l1/l2`。

## P1（随后）

1. **时间归一化器**
   - 统一把“yesterday/last Friday/下周二”等相对时间归一到绝对日期（基于 session 基准时间）。
2. **会话约束 rerank**
   - 对 conversation 基准优先同 `lineage.session_id` / 同 conv 的候选，降低跨会话干扰。

---

## 8. 一句话总结

现在 Cat1-4 弱，不是单纯“embedding 不够好”，而是**时间锚点在写入链路丢失 + l0 证据粒度过粗 + immediate/merged 混检不稳定**三者叠加导致。
