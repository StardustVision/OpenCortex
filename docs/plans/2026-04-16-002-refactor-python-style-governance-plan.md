---
title: "refactor: python style and simplicity governance"
type: refactor
status: active
date: 2026-04-16
origin: docs/brainstorms/2026-04-16-python-simplicity-style-enforcement-requirements.md
deepened: 2026-04-16
---

# Refactor: Python Style And Simplicity Governance

## Overview

把“Google Python 规范强制要求”落成仓库内可执行的最小治理闭环：先建立可运行、可收敛的 Python style gate，再针对当前最高复杂度热点做外科式减负，最后从高频 HTTP/search 边界开始收口 typed contract。目标是持续降低维护成本，而不是做一次性大扫除。

## Problem Frame

当前仓库的 Python 代码主要有三类问题叠在一起：

1. 风格规范没有自动化门禁，导致 review 在替 lint 做工作。
2. `src/opencortex/orchestrator.py` 与 `src/opencortex/context/manager.py` 已经大到影响后续任何改动的认知成本。
3. 高频公共边界仍大量依赖 `Dict[str, Any]`，尤其是 HTTP server/client 和部分 storage/context 返回值，导致 contract 漂移风险高。

requirements 文档已经明确这轮工作的边界：不做全仓大爆炸重写，不引入重型流程体系，按“gate -> 热点 -> typed boundary”推进，并继续保持最小改动（见 origin: `docs/brainstorms/2026-04-16-python-simplicity-style-enforcement-requirements.md`）。

## Requirements Trace

- R1-R5. 建立可执行的 Python 风格门禁，并把 Google Python 规范里的关键子集自动化。
- R6-R9. 优先治理 `src/opencortex/orchestrator.py` 和 `src/opencortex/context/manager.py`，删除重复 helper 和无价值 plumbing。
- R10-R12. 从高频公共边界开始收口 typed contract，同时保持现有对外 JSON contract 兼容。
- R13-R15. 分阶段推进，每阶段有完成信号，不引入复杂设计。

## Scope Boundaries

- 不做全仓 Python 文件的同步风格重写。
- 不在本轮引入完整静态类型体系或一次性上 `mypy --strict`。
- 不修改 retrieval/memory pipeline 的产品行为或外部 JSON 字段语义。
- 不把 benchmark 脚本、`.context/`、本地产物纳入治理范围。

### Deferred to Separate Tasks

- 更广泛的 storage payload typed contract 收口：在 HTTP/search 试点稳定后另起一轮推进。
- 更高强度的类型检查器接入（例如项目级 mypy/pyright）：等待公共边界收紧后单独评估。

## Context & Research

### Relevant Code and Patterns

- `pyproject.toml` 目前 dev 依赖只有 `pytest`，没有任何 Python style gate。
- `src/opencortex/context/manager.py` 的 `_prepare()` 已经天然分成“配置归一化 -> probe/planner/runtime -> 并发 retrieval -> envelope 组装”四段，但仍挤在一个函数里。
- `src/opencortex/orchestrator.py` 当前把 Phase 1/2/3 编排、scope/filter 构造、anchor rerank、object query 执行都放在一个类里；纯函数辅助逻辑已经形成可抽离簇。
- `src/opencortex/http/models.py` 已经是 request model 集中点，适合作为 response model 和 typed contract 的第一落点。
- `src/opencortex/http/server.py` 的 `/api/v1/memory/search` 已经稳定暴露 `memory_pipeline`，且 `tests/test_http_server.py`、`tests/test_recall_planner.py` 已经锁住关键 contract。
- `src/opencortex/http/client.py` 与 `src/opencortex/http/server.py` 存在天然一一对应关系，是最低风险的 server/client contract 试点。
- `src/opencortex/context/manager.py` 与 `src/opencortex/orchestrator.py` 各自保留了重复 `_merge_unique_strings()`，属于本轮应清理的重复 helper。

### Institutional Learnings

- `docs/solutions/best-practices/memory-intent-hot-path-refactor-2026-04-12.md`：memory hot path 必须保持 `probe -> planner -> runtime` 分层，不要把 phase 责任重新揉回一个入口。
- `docs/solutions/best-practices/single-bucket-scoped-probe-2026-04-16.md`：scope 选择必须单桶且权威，probe/planner/runtime contract 要和 HTTP/benchmark 暴露保持一致，不能为了兼容字段把旧语义偷偷带回来。

### External References

- Google Python Style Guide: type annotations、exceptions、imports、TODO comments、naming、docstrings、function length 等规则基线。
- Ruff 官方文档：支持 `pydocstyle` 的 Google convention，以及 `ANN`、`TD`、`I`、`N`、`RET`、`SIM`、`RSE` 等规则族，可作为最小统一 gate。

## Key Technical Decisions

- 只引入一个主门禁工具：首阶段采用 `ruff format` + `ruff check`，不同时引入多套 lint/type 工具。原因是仓库当前债务面已经很大，再叠 `pylint + flake8 + black + isort + mypy` 只会放大迁移成本。
- 用单一 gate + 临时 `per-file-ignores` 收敛旧债，而不是做“双轨 lint 体系”。原因是用户明确偏好简单方案；一个命令、一个配置文件、少量临时豁免，比拆成多套 pipeline 更稳。
- Google 强制子集优先映射到这些 Ruff 规则族：`I`（导入）、`N`（命名）、`D`（docstring，Google convention）、`ANN`（公开 API 类型注解）、`TD`（TODO 格式）、`RSE`/`RET`/`SIM`/`B`/`UP`（异常、明显坏味道和简化问题），并保留 `F`/`E`/`W` 作为基础质量门槛。
- 首个 orchestrator 拆分点放在“probe/retrieval support 纯辅助簇”，而不是一上来拆 storage 或完整 query executor。原因是 `_build_scope_filter`、`_build_probe_scope_input`、`_merge_filter_clauses`、anchor grouping/rerank、start-point filter 这簇逻辑边界清晰、与近期 retrieval 设计一致、提取风险最低。
- `ContextManager` 第一刀只拆 `_prepare()`，并引入少量内部 dataclass/typed container 承接阶段间数据；不碰 `commit/end` 语义。原因是 `_prepare()` 已经是最热路径，且其职责混杂最明显。
- typed boundary 首批试点选择 `/api/v1/memory/search` 的 server/client 对偶 contract，而不是直接从 storage interface 开始。原因是它高频、已有稳定 tests、`memory_pipeline` 已结构化、兼容性风险最可控。
- typed boundary 的兼容策略是“线上的 JSON 不变，仓内边界先类型化”。也就是说先引入 response models / parse layer，再决定是否在后续迭代调整 client 返回类型。

## Open Questions

### Resolved During Planning

- 哪组工具代表“Google Python 强制子集”？结论：先用 Ruff 统一承接格式、导入、命名、docstring、注解存在性、TODO、明显异常/简化规则，不在本轮引入第二套主 lint 工具。
- `src/opencortex/orchestrator.py` 第一刀拆哪里最安全？结论：先拆 probe/retrieval support 纯函数簇，再评估是否需要把 `_execute_object_query()` 进一步下沉为 collaborator。
- `src/opencortex/context/manager.py` 的 `prepare` 热路径怎么切？结论：按“输入归一化 / recall planning / retrieval fan-out / response assembly”四段拆，并用内部 typed container 接住中间态。
- 哪条公共边界最适合作 typed contract 试点？结论：`/api/v1/memory/search`，因为 server/client 成对、已有契约测试、`memory_pipeline` 是高频核心 surface。

### Deferred to Implementation

- 具体选择哪些文件加入首批 `per-file-ignores`：需要在第一次 `ruff check` 实跑后基于真实噪音面收敛。
- `ContextManager` 内部 dataclass 是 2 个还是 3 个：执行时以最小够用为准，不提前扩大模型数量。
- `/api/v1/memory/search` 在 client 侧是直接返回 typed model 还是保留 dict 并增加 typed helper：实现时结合仓内调用点做兼容判断。

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

### Governance Ratchet

| Stage | Primary Output | Why this order |
|------|----------------|----------------|
| Stage 1 | `ruff` gate + baseline豁免清单 | 先阻止新债流入 |
| Stage 2 | `_prepare()` 拆段 + orchestrator support 瘦身 | 先清热路径，再谈更广泛收口 |
| Stage 3 | `memory/search` typed contract + follow-up boundary cleanup | 在已有 gate 和更小热点代码上做 contract 收紧 |

### Google Rule Mapping

| Google concern | Repo enforcement shape |
|------|-------------------------|
| Imports / import formatting | Ruff `I`, `TID` |
| Naming | Ruff `N` |
| Public docstrings / Google style | Ruff `D` + `pydocstyle.convention = "google"` |
| Type annotations on public APIs | Ruff `ANN` |
| TODO format | Ruff `TD` |
| Exceptions / obvious bad patterns | Ruff `RSE`, `B`, `RET`, `SIM` |
| Simplicity / remove needless branches | Ruff `SIM`, `RET`, selective `PLR` only if low-noise after baseline |

## Implementation Units

- [x] **Unit 1: Establish the minimal Python style gate**

**Goal:** 把 Google Python 规范的核心子集落成一个仓库内可执行、可复现的 gate。

**Requirements:** R1, R2, R3, R4, R5, R13, R15

**Dependencies:** None

**Files:**
- Modify: `pyproject.toml`
- Modify: `README.md`
- Modify: `AGENTS.md`
- Create: `.github/workflows/python-style.yml`

**Approach:**
- 在 `pyproject.toml` 增加 `ruff` dev 依赖、统一 lint/format 配置、Google docstring convention、以及最小必要的 `per-file-ignores`。
- gate 只承接“当前能稳定执行的 Google 子集”，不引入第二套 lint 主工具。
- 明确对 `tests/`、超大 legacy 热点文件、以及明显非产品路径文件做临时豁免，豁免必须带收敛意图，不能当永久垃圾桶。
- 在文档中把“本仓库的 Google 强制子集”写清楚，避免团队把 `ruff` 误解成纯格式化工具。

**Patterns to follow:**
- `src/opencortex/http/models.py` 中集中定义 transport 模型的做法，说明仓库接受“规则集中配置 + transport 集中建模”。
- 现有 conventional docs/plan 习惯：让规则可检索、可定位，而不是散落在口头约定里。

**Test scenarios:**
- Test expectation: none -- 本 unit 是配置、文档与 CI 接线，验证依赖 gate 本身可执行而不是新增 pytest 文件。
- Happy path: 干净文件通过 `ruff format --check` 与 `ruff check`，输出稳定可复现。
- Edge case: `tests/` 中允许的历史写法仅通过显式 `per-file-ignores` 放行，而不是全局关闭同类规则。
- Error path: 新增不合规 TODO、缺失公开函数类型注解、缺失公开 docstring、错误导入顺序时，gate 会失败。
- Integration: 本地开发与 CI 使用同一套 `pyproject.toml` 配置，不出现“本地过、CI 不过”的分叉。

**Verification:**
- 仓库出现单一入口的 Python style gate。
- 文档中能明确看到哪些 Google 规则已经被自动化、哪些仍属后续阶段。

- [x] **Unit 2: Ratchet the baseline without a repo-wide rewrite**

**Goal:** 让新增 gate 在当前仓库内可落地运行，同时把豁免面控制在少数 legacy 热点，不制造全仓大 diff。

**Requirements:** R2, R3, R6, R9, R13, R14, R15

**Dependencies:** Unit 1

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/opencortex/http/server.py`
- Modify: `src/opencortex/http/client.py`
- Modify: `src/opencortex/http/models.py`
- Modify: `src/opencortex/skill_engine/http_routes.py`
- Modify: `tests/test_http_server.py`
- Modify: `tests/test_context_manager.py`
- Modify: `tests/test_recall_planner.py`

**Approach:**
- 先清理低风险、高收益的触发点：导入顺序、未使用符号、明显简化机会、docstring/annotation 漏洞集中区。
- 对 `src/opencortex/orchestrator.py`、`src/opencortex/context/manager.py` 这类即将进入 Stage 2 的热点文件，优先用临时豁免保住 gate，而不是在 Stage 1 做重写式修补。
- 基线策略是“少数文件豁免 + 规则默认开启”，而不是“多数规则先关掉”。

**Execution note:** 先做 characterization 式基线清点，执行时以第一次 `ruff check` 的真实失败分布决定豁免粒度。

**Patterns to follow:**
- `tests/test_http_server.py`、`tests/test_recall_planner.py` 已经用 contract assertions 锁核心 surface，适合作为 gate 引入后的回归样本。

**Test scenarios:**
- Happy path: 选定的低风险模块在不改变行为的前提下通过新增 gate。
- Edge case: 超大 legacy 文件被精确豁免，不影响同目录其他文件受 gate 约束。
- Error path: 若某条规则在现有仓库噪音过高，则以更小范围 ignore 或延后启用处理，而不是全局关闭整个规则族。
- Integration: HTTP/context/intent 相关关键测试继续通过，证明 gate 收敛没有顺手破坏 contract。

**Verification:**
- `ruff` gate 可以在当前主仓执行。
- legacy 例外清单数量有限，且主要集中在 Stage 2/3 目标文件。

- [x] **Unit 3: Split `ContextManager._prepare()` into typed internal phases**

**Goal:** 在不改变 prepare 对外行为的前提下，降低 `src/opencortex/context/manager.py` 的热路径复杂度。

**Requirements:** R8, R9, R10, R13, R14, R15

**Dependencies:** Unit 2

**Files:**
- Modify: `src/opencortex/context/manager.py`
- Modify: `tests/test_context_manager.py`
- Modify: `tests/test_recall_planner.py`

**Approach:**
- 把 `_prepare()` 拆成四段 helper：请求归一化、recall planning、并发 retrieval、response/envelope assembly。
- 引入少量内部 dataclass 或 typed container，承接 `config`、planning result、retrieval result，减少跨阶段裸 `Dict[str, Any]` 传递。
- 顺手删除与 orchestrator 重复的 `_merge_unique_strings()`，把这类纯 helper 收口到单一位置。
- 只重构 prepare，不动 `commit` 和 `end` 的行为边界。

**Execution note:** 先补/稳住 `_prepare()` 契约测试，再做拆分，避免在 300+ 行热路径里盲改。

**Patterns to follow:**
- `docs/solutions/best-practices/memory-intent-hot-path-refactor-2026-04-12.md`
- `tests/test_context_manager.py` 里对 `memory_pipeline`、cache、session scope 的现有断言

**Test scenarios:**
- Happy path: 正常 `prepare` 仍返回同样的 `intent.memory_pipeline` envelope，并能召回 memory/knowledge。
- Happy path: `recall_mode=never` 仍跳过 probe/planner。
- Edge case: 空 query 仍走 `_empty_prepare()` 分支并缓存结果。
- Edge case: `session_scope` 开启时，传入 `session_id` 仍只影响 scoped retrieval，不改变无 scope 查询行为。
- Error path: probe/planner timeout 或异常时，仍退化到 fallback runtime plan，不抛出新的外部异常。
- Integration: `_prepare()` 拆分后，`tests/test_context_manager.py` 现有关于 `memory_pipeline`、skill tracking、merge buffer 的回归断言继续成立。

**Verification:**
- `_prepare()` 主函数体明显变短，阶段责任清晰可读。
- prepare 阶段不再在一个函数里同时持有过多中间 dict 状态。

- [x] **Unit 4: Extract orchestrator retrieval support helpers out of the god object**

**Goal:** 从 `src/opencortex/orchestrator.py` 中切走最自洽的一簇 retrieval support 逻辑，降低 `MemoryOrchestrator` 的职责密度。

**Requirements:** R7, R9, R13, R14, R15

**Dependencies:** Unit 2

**Files:**
- Modify: `src/opencortex/orchestrator.py`
- Create: `src/opencortex/intent/retrieval_support.py`
- Modify: `tests/test_recall_planner.py`
- Modify: `tests/test_memory_probe.py`
- Modify: `tests/test_http_server.py`

**Approach:**
- 首批抽离纯辅助簇：scope/filter build、filter merge、query/record anchor grouping、start-point filter、anchor rerank bonus。
- 保留 `MemoryOrchestrator` 作为编排入口，不在这一刀引入新的服务层或复杂依赖注入。
- 若抽离后 `_execute_object_query()` 仍过重，只允许继续做局部 helper 下沉，不在本计划内扩成完整新子系统。

**Patterns to follow:**
- `docs/solutions/best-practices/single-bucket-scoped-probe-2026-04-16.md`
- 当前 `src/opencortex/intent/` 下 phase-native 模块化方向

**Test scenarios:**
- Happy path: 普通 search 仍返回相同 `memory_pipeline` 关键字段。
- Happy path: scope-aware search 仍保留 `scope_level`、`scope_source`、`selected_root_uris`。
- Edge case: authoritative scoped miss 仍不偷偷 widening。
- Edge case: starting points、query entities、starting point anchors 仍按原 contract 参与 planner/runtime。
- Error path: 抽离 helper 后，缺失 start-point 或缺失 anchors 时仍走既有 global/scoped 路径，不新增异常。
- Integration: `/api/v1/memory/search`、`ContextManager.prepare`、planner 集成测试对 retrieval contract 的断言不变。

**Verification:**
- `MemoryOrchestrator` 行数和纯辅助方法数量下降。
- retrieval support 逻辑能在更小模块中被独立阅读和测试。

- [x] **Unit 5: Type the `memory/search` HTTP contract end-to-end**

**Goal:** 用最小兼容改动把 `/api/v1/memory/search` 变成 typed boundary 试点。

**Requirements:** R10, R11, R12, R13, R14, R15

**Dependencies:** Unit 2; Unit 4

**Files:**
- Modify: `src/opencortex/http/models.py`
- Modify: `src/opencortex/http/server.py`
- Modify: `src/opencortex/http/client.py`
- Modify: `src/opencortex/retrieve/types.py`
- Modify: `tests/test_http_server.py`
- Create: `tests/test_http_client.py`

**Approach:**
- 在 `src/opencortex/http/models.py` 为 `memory/search` 增加 response model：顶层 response、result item、必要的 `memory_pipeline` wrapper。
- `src/opencortex/http/server.py` 使用 response model 组装返回值，避免 route handler 继续裸拼 dict。
- `src/opencortex/http/client.py` 在收到 JSON 后做 model validation；兼容策略以“不改变线上 JSON”为前提，优先内部 parse，再决定公开返回形态。
- 只覆盖 `memory/search` 这条高频边界，不在本轮顺手把所有 endpoint 都类型化。

**Patterns to follow:**
- `src/opencortex/http/models.py` 现有 request model 布局
- `tests/test_http_server.py` 中已有 `memory_pipeline` contract assertions
- `src/opencortex/retrieve/types.py` 现有 `to_dict()` / `memory_pipeline_dict()` 结构

**Test scenarios:**
- Happy path: `/api/v1/memory/search` 仍返回现有 JSON shape，且 response model 能成功验证。
- Happy path: client 调 `memory_search()` 时，返回结果经过 typed parse 后仍能访问 `results`, `total`, `memory_pipeline`。
- Edge case: 搜索结果无 `overview` / `content` / `source_doc_id` 时，response model 允许缺省字段。
- Edge case: `memory_pipeline` 缺失 planner/runtime 的 scoped miss 场景仍能通过模型验证。
- Error path: server 返回非预期 payload 时，client 侧能明确暴露 contract validation failure，而不是静默吞掉。
- Integration: `tests/test_http_server.py` 现有 probe/planner/runtime、scoped_miss、fallback 断言继续成立。

**Verification:**
- `memory/search` 不再在 server/client 边界上以裸 `Dict[str, Any]` 作为唯一 contract 形式。
- 对外 JSON 字段保持兼容，typed contract 已开始约束内部边界。

- [x] **Unit 6: Expand the ratchet to the next typed boundary and retire temporary ignores**

**Goal:** 用 Stage 2/3 的结果回收一部分临时豁免，并把 typed boundary 从 `memory/search` 扩展到 `/api/v1/context` 的 prepare response。

**Requirements:** R9, R10, R11, R13, R14

**Dependencies:** Unit 3; Unit 4; Unit 5

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/opencortex/http/models.py`
- Modify: `src/opencortex/http/server.py`
- Modify: `src/opencortex/context/manager.py`
- Modify: `src/opencortex/http/client.py`
- Modify: `tests/test_context_manager.py`
- Modify: `tests/test_http_server.py`

**Approach:**
- 回收 Unit 3/4/5 直接覆盖到的临时 `per-file-ignores`。
- 为 `/api/v1/context` 的 prepare 响应补齐 typed model，让 `intent.memory_pipeline`、`memory`、`knowledge`、`instructions` 这些返回结构不再只靠裸 dict 维持。
- `src/opencortex/http/client.py` 复用 Unit 5 的 parse 模式，把 context prepare 返回结果也纳入 typed validation。
- 只做增量收口，不在本 unit 里追求“typed everything”。

**Patterns to follow:**
- Unit 5 已建立的 typed response/model 试点形态
- `tests/test_context_manager.py` 和 `tests/test_http_server.py` 的 contract-style assertions

**Test scenarios:**
- Happy path: 已覆盖文件从临时 ignore 中移出后仍通过 gate。
- Happy path: `/api/v1/context` prepare 响应在 typed validation 下仍保留现有 `memory_pipeline` envelope。
- Edge case: prepare 返回空 `memory`/`knowledge`、空 query、`recall_mode=never` 时仍保持兼容序列化。
- Error path: 新模型在遇到不合法 payload 时给出可诊断失败，而不是把错误埋在业务层。
- Integration: context manager、HTTP route、HTTP client 和测试一起收敛，不留下“模型存在但没人真正使用”的半成品。

**Verification:**
- 至少一部分临时 ignore 被回收。
- 仓库不再只有一个 typed boundary 试点，且第二条边界直接覆盖 prepare 热路径。

## System-Wide Impact

- **Interaction graph:** `pyproject.toml` gate 会影响所有 Python 代码；Stage 2 主要影响 `context -> orchestrator -> intent -> retrieve -> http` 这条 memory hot path；Stage 3 影响 HTTP server/client contract。
- **Error propagation:** style gate 的失败应停在开发/CI 边界；typed contract 校验失败应在 transport/client parse 层被明确暴露，不向 retrieval 业务逻辑下沉成隐式错误。
- **State lifecycle risks:** Stage 2 必须保持 `ContextManager.prepare` 的缓存、session activity、skill tracking、merge buffer、副作用时序不变。
- **API surface parity:** `/api/v1/memory/search` 的 server 和 client 必须同时更新；若 response model 落地，相关 benchmark/contract consumers 需要确认兼容。
- **Integration coverage:** `tests/test_context_manager.py`、`tests/test_http_server.py`、`tests/test_recall_planner.py`、`tests/test_memory_probe.py` 是本计划的核心跨层回归带。
- **Unchanged invariants:** `memory_pipeline.probe/planner/runtime` 的对外字段语义保持不变；scoped miss 不 widening；prepare/commit/end 生命周期不改产品行为。

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| 一次性启用过多 Ruff 规则导致全仓噪音爆炸 | 采用单一 gate + 临时 `per-file-ignores`，并把热点文件留到 Stage 2/3 回收 |
| `_prepare()` 拆分时误伤缓存、副作用、并发检索时序 | 先依赖现有 characterization tests，再按阶段 helper 拆，不改外部 contract |
| orchestrator 抽离过头，演化出新抽象层 | 第一刀只抽 pure support cluster，不新建“万能 service” |
| typed contract 试点意外打破 client 调用方 | 保持 JSON wire contract 不变，先内部 parse/validate，再决定公开返回类型 |
| style gate 只停留在配置，没有形成习惯 | 在 `README.md` / `AGENTS.md` 写明默认要求，并让 CI 使用同一入口 |

## Phased Delivery

### Phase 1

- Unit 1
- Unit 2

完成信号：仓库有可运行的 `ruff` gate，且 legacy 豁免面被压在少量热点文件。

### Phase 2

- Unit 3
- Unit 4

完成信号：`ContextManager._prepare()` 和 orchestrator retrieval support 都完成首轮减负，并回收一部分临时 ignore。

### Phase 3

- Unit 5
- Unit 6

完成信号：`memory/search` contract 已类型化，且至少一条后续高频边界继续收口。

## Documentation / Operational Notes

- `README.md` 需要补充 Python style gate 的本地使用入口和规则边界。
- `AGENTS.md` 需要把“Google Python 规范强制要求”的自动化落地点写成仓库约定，避免未来 plan/work 漂移。
- 若仓库已有 CI workflow，本计划应把 Python gate 接进去；若当前没有统一 workflow，则至少在文档里留出同一入口。

## Sources & References

- **Origin document:** `docs/brainstorms/2026-04-16-python-simplicity-style-enforcement-requirements.md`
- Related code: `pyproject.toml`
- Related code: `src/opencortex/context/manager.py`
- Related code: `src/opencortex/orchestrator.py`
- Related code: `src/opencortex/http/server.py`
- Related code: `src/opencortex/http/client.py`
- Related code: `src/opencortex/http/models.py`
- Related tests: `tests/test_context_manager.py`
- Related tests: `tests/test_http_server.py`
- Related tests: `tests/test_recall_planner.py`
- Institutional learning: `docs/solutions/best-practices/memory-intent-hot-path-refactor-2026-04-12.md`
- Institutional learning: `docs/solutions/best-practices/single-bucket-scoped-probe-2026-04-16.md`
- External docs: https://google.github.io/styleguide/pyguide.html
- External docs: https://docs.astral.sh/ruff/linter/
- External docs: https://docs.astral.sh/ruff/settings/
- External docs: https://docs.astral.sh/ruff/rules/
