---
date: 2026-04-13
topic: memory-router-autotraining
---

# Memory Router Auto-Training

## Problem Frame

当前 router 的离线训练能力只能从已有 benchmark 和人工样本构建训练集，再手动训练并导出 artifact。

这解决了“如何训练一个轻量分类器”的问题，但没有解决“系统如何在真实使用中持续积累可训练数据”的问题。

如果未来要让 router 真正具备泛化能力，仅靠一次性 benchmark 训练不够。系统需要一条稳定的数据闭环：

- 从真实 conversation 中收集 router 相关样本
- 将原始行为转化为可训练候选样本
- 对候选样本做质量控制
- 周期性离线重训
- 用离线评估决定是否发布新 artifact

本期不实现这条闭环，但需要先把职责、边界和约束定义清楚，避免后续把“自动训练”误做成在线自学习或把噪声直接写入模型。

## Requirements

**Scope**
- R1. 自动训练闭环是 router 的后续演进能力，不是本期交付范围。
- R2. 本期代码主路径不得因为预留自动训练而引入额外请求参数、远程依赖或在线训练逻辑。
- R3. 自动训练闭环只服务于 Phase 1 router，不得反向耦合 planner 或 runtime 的策略实现。

**Data Collection**
- R4. 系统未来必须能够从真实 conversation/query 中自动积累 router 训练候选样本。
- R5. 采集范围至少应包括：
  - 原始 query
  - router 输出的 `should_recall/task_class/confidence`
  - planner 结果摘要
  - runtime 执行摘要
  - 用户后续行为信号
- R6. 用户后续行为信号至少应允许表示：
  - 是否发生追问
  - 是否发生改写重试
  - 是否发生手动纠偏
  - 是否出现明显失败信号
- R7. 原始 conversation 不得直接等价为训练标签；必须先经过样本构建与质量过滤。

**Label Construction**
- R8. 自动训练闭环必须区分三层数据状态：
  - 原始行为日志
  - 候选训练样本
  - 已批准训练样本
- R9. 候选训练样本必须显式记录标签来源，例如：
  - benchmark seed
  - heuristic weak label
  - human review
  - model-assisted labeling
- R10. 未经质量门控的弱标签样本不得直接进入正式训练集。
- R11. `no_recall` 标签必须格外谨慎，不能仅凭一次短句或一次检索 miss 自动认定。
- R12. 训练样本必须保留时间戳与来源元数据，以支持未来回放、审计和回滚。
- R12a. 人工确认必须由后台控制面能力承接，而不是由在线 router 或训练脚本隐式完成。

**Training Policy**
- R13. 自动训练闭环必须采用周期性离线重训，不得做逐请求在线训练。
- R14. 训练目标应保持轻量：
  - 不训练主 embedding 模型
  - 只训练轻量分类头或等价轻量分类器
- R15. 训练过程必须产生独立 artifact，不得把训练状态写回在线进程内存。
- R16. 新 artifact 只有在离线评估通过门槛后才允许进入候选发布状态。
- R16a. 训练任务必须由后台控制面显式触发，不得因为在线样本收集或候选标注完成而自动启动。

**Evaluation and Promotion**
- R17. 自动训练闭环必须内置离线评估，至少覆盖：
  - holdout router classification
  - benchmark-derived regression set
  - 关键类别混淆矩阵
- R18. artifact 晋升规则必须同时关注：
  - 均值准确率
  - 方差/稳定性
  - 热路径性能预算
- R19. 新 artifact 不得仅凭单次随机 seed 提升就自动上线。
- R20. 发布前必须保留与上一版本 artifact 的可对照评估结果。

**Runtime Integration**
- R21. 在线 router 未来只能加载已经发布的静态 artifact。
- R22. 在线 router 不得在请求路径上执行训练、增量拟合或权重更新。
- R23. artifact 缺失、损坏或版本不兼容时，router 必须自动退回到当前默认可用模式。
- R24. router trace 未来应可标记当前使用的是：
  - prototype/lightweight baseline
  - trained classifier artifact
  - fallback mode

**Safety and Operations**
- R25. 自动训练闭环必须允许人工审核与人工回滚。
- R25a. 后台控制面必须拥有训练触发、审批、发布和回滚的明确权限边界。
- R26. 任何自动生成的训练样本都必须可追溯回原始来源，而不是只保留聚合结果。
- R27. 不同来源样本必须可以按时间窗口、租户范围、标签来源进行过滤。
- R28. 训练数据收集与 artifact 发布必须解耦，避免“收集一批脏数据立刻上生产”。

## Success Criteria

- 系统未来能够从真实 conversation/query 自动积累 router 训练候选样本。
- 候选样本、批准样本、训练 artifact 三者边界清晰，不混用。
- 训练仍保持离线、轻量、本地 artifact 发布，不侵入在线热路径。
- artifact 晋升基于多 seed 与回归评估，而不是单次跑分。
- 线上 router 可以无感加载新 artifact，也可以安全回退到旧模式。
- 人工确认与训练触发都由后台控制面负责，而不是在线路径自动推进。

## Scope Boundaries

- 本文档不实现自动训练闭环。
- 本文档不定义具体存储表结构、目录布局或调度器实现。
- 本文档不规定最终使用 `scikit-learn`、`onnx`、`joblib` 还是其他 artifact 形式。
- 本文档不定义具体人工审核 UI，但要求人工确认属于后台控制面能力。
- 本文档不改变本期 router/planner/runtime 的线上执行逻辑。

## Key Decisions

- 自动训练采用“收集 -> 标注 -> 审核 -> 离线重训 -> 评估 -> 发布”的闭环，而不是在线自学习。
- 真实 conversation 只提供原始信号，不直接等价为训练标签。
- 模型更新以 artifact 发布为边界，而不是请求时动态更新。
- 稳定性优先于单次高分，必须关注多 seed 波动。
- 人工确认与训练触发都属于后台控制动作，不属于在线服务自动行为。

## Dependencies / Assumptions

- 当前代码库已经具备离线训练轻量 classifier 的基础能力。
- 当前 router 已经明确输出 `should_recall/task_class/confidence`，可以作为行为日志的一部分。
- 后续系统可以在不破坏热路径的前提下增加训练样本采集与离线调度能力。

## Outstanding Questions

### Deferred to Planning
- [Technical] 候选训练样本应落在什么存储介质中，才能同时满足追溯、过滤和批量训练读取？
- [Technical] 用户后续行为如何映射为弱标签，哪些信号只能作为置信度修正而不能直接变成标签？
- [Product] 人工审核是一次性批处理，还是按高价值/高争议样本队列化处理？
- [Operations] artifact 的版本命名、灰度策略和回滚策略如何定义？
- [Evaluation] 多 seed 离线评估的最小样本规模和晋升门槛如何设定？
- [Product] 后台控制面第一版只支持批量审批，还是需要细粒度样本级操作？

## Next Steps

- 后续需要单独补一份 implementation plan
- 本期不实现
