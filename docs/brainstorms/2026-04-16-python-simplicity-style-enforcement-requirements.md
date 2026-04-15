---
date: 2026-04-16
topic: python-simplicity-style-enforcement
---

# Python Simplicity And Style Enforcement

## Problem Frame
当前仓库已经完成一轮 retrieval/probe/runtime 的结构收口，但全仓代码面仍然存在两类持续性问题：

1. Python 风格约束没有被工具强制执行，`pyproject.toml` 的 dev 依赖里只有 `pytest`，团队无法把 Google Python 风格要求转成稳定门禁。
2. 核心热路径仍然有明显的结构复杂度，尤其是 `src/opencortex/orchestrator.py`、`src/opencortex/context/manager.py`、以及一批 HTTP/public API 边界上的 `Dict[str, Any]` 返回值，这些问题会持续放大维护成本。

这次工作的目标不是“全仓一次性重写成教科书风格”，而是建立一个可持续的收口路径：先让规范变成强制门禁，再清掉最影响维护效率的热点复杂度。

## Normative Baseline
这项工作的 Python 规范基线采用 Google Python 风格指南中的两部分：

- 语言规范：异常、导入、全局变量、生成器、lambda、默认参数、properties、线程、类型注释等使用规则
- 风格规范：行宽、缩进、导入格式、文档字符串、TODO 注释、命名、函数长度、类型注解等书写规则

本次治理不要求逐条机械复刻整份文档，但规划和执行必须以这两部分为权威来源，且不能只停留在“参考一下”的层面。

## Requirements

**Style Enforcement**
- R1. 仓库必须新增可执行的 Python 风格门禁，使“Google Python 规范强制要求”不再依赖人工 review 才能执行。
- R2. 新门禁必须至少覆盖基础格式/导入/未使用符号/明显可简化问题，并能在本地与 CI 中稳定运行。
- R3. 新门禁引入后，新增或修改的 Python 代码必须以通过门禁为默认要求。
- R4. 新门禁必须明确覆盖 Google 规范里当前最影响仓库维护质量的规则子集，而不是只做纯格式化。
- R5. 这组强制子集至少应覆盖以下方向：导入规则、命名规则、公开 API 文档字符串、类型注解、异常使用约束、TODO 注释格式、以及禁止明显不推荐写法。

**Simplicity Hotspots**
- R6. 本轮收口必须优先处理最高复杂度热点，而不是无差别全仓扫尾。
- R7. `src/opencortex/orchestrator.py` 必须开始去 god object 化，至少把最重的一段职责从主类中切出去或收紧边界。
- R8. `src/opencortex/context/manager.py` 必须继续降低单函数和单模块负担，优先拆分 `prepare` 热路径中职责混杂的部分。
- R9. 重复 helper、只为兼容而存在的内部 plumbing、以及无明确当前价值的预留分支，应优先删除而不是继续保留。

**Typed Boundaries**
- R10. 公开边界上的未建模 `Dict[str, Any]` 返回值必须开始系统收口，优先覆盖最热的 HTTP/server/client 和 memory pipeline 边界。
- R11. 新的类型化边界应先覆盖高频接口，而不是试图一次性替换全部内部 dict。
- R12. 对外行为和已有 contract 必须保持兼容，除非有明确的破坏性调整决策。

**Execution Strategy**
- R13. 这项工作必须按阶段推进：先立 gate，再收热点，再扩展到更广的类型边界和风格清理。
- R14. 每个阶段都必须有清晰的完成信号，避免“永远在做代码洁癖治理”。
- R15. 这项工作必须继续遵守现有仓库偏好：最小改动、避免复杂设计、避免为未来假设做大抽象。

## Success Criteria
- 团队可以在本地一条命令运行 Python 风格门禁，并且结果可复现。
- 后续 Python 变更不再轻易引入未使用符号、显著风格漂移、或显而易见的简化问题。
- 至少一组与 Google 规范直接对应的规则已经被自动化执行，而不是仅写在文档里。
- `src/opencortex/orchestrator.py` 和 `src/opencortex/context/manager.py` 的后续修改不再需要在超长函数或超大文件中进行。
- 至少一条高频公共边界不再依赖松散的 `Dict[str, Any]` 契约。

## Scope Boundaries
- 不要求一次性把全仓所有 Python 文件都改成完全统一风格。
- 不要求这轮直接完成 `src/opencortex/orchestrator.py` 的全面拆分。
- 不要求这轮清理 benchmark 脚本、一次性脚本、或明显偏 CLI 输出型文件中的所有 `print()`。
- 不要求为“Google Python 规范”引入比当前团队负担更重的大型流程体系。
- 不要求打破现有对外 API 契约来换取代码更“干净”。

## Key Decisions
- 分阶段推进而不是全仓一次性清扫：这样才能在不打断当前主线的前提下持续收口。
- 先立门禁再做大面积代码清理：否则风格债会边修边重新流入。
- 优先治理热点复杂度而不是平均用力：`src/opencortex/orchestrator.py` 和 `src/opencortex/context/manager.py` 是当前最值得花力气的地方。
- 类型边界优先于内部纯格式清理：公共 contract 的收紧会比纯样式修补带来更高的长期收益。

## Dependencies / Assumptions
- 假设本次使用的 Google Python 风格指南链接可作为团队认可的外部规范来源。
- 假设团队接受“Google Python 规范”在本仓库里以自动化规则子集落地，而不是逐条全文人工比对。
- 假设现有测试覆盖足以支撑分阶段的小步重构。
- 假设 `.context/` 这类本地产物不属于这轮治理范围。

## Alternatives Considered
- 方案 A：先全仓扫一遍风格，再说架构问题。
  - 不采纳。成本高，容易制造巨大 diff，而且不会阻止新债继续流入。
- 方案 B：只拆热点模块，不立门禁。
  - 不采纳。能局部变好，但无法形成持续约束。
- 方案 C：分阶段混合推进，先立 gate，再做热点和边界收口。
  - 采纳。收益和 carrying cost 最平衡。

## Outstanding Questions

### Deferred to Planning
- [Affects R1, R2, R4, R5][Technical] 应该采用哪组最小但足够有效的 lint/type/style 规则来代表仓库内的“Google Python 强制子集”？
- [Affects R7][Technical] `src/opencortex/orchestrator.py` 第一刀应按哪条职责边界切分，才能在最小风险下获得最大减负？
- [Affects R8][Technical] `src/opencortex/context/manager.py` 的 `prepare` 热路径应先拆成几个步骤函数，哪些数据结构需要一起建模？
- [Affects R10, R11][Needs research] 哪一条公共边界最适合作为 typed contract 的第一批试点，同时具备高频价值和低迁移风险？

## Next Steps
-> /ce:plan for structured implementation planning
