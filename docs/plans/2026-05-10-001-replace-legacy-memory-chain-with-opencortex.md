---
title: opencortex 新链路替换旧记忆链路
status: completed
created: 2026-05-10
module: opencortex
tags:
  - migration
  - memory
  - retrieval
  - storage
problem_type: refactor
---

# opencortex 新链路替换旧记忆链路

## 当前状态

旧记忆链路已经从产品源码中移除。当前 `src/opencortex` 只保留新链路：

- `core`：身份上下文与中间件。
- `storage`：CFS、CortexStorage、SQLite 持久队列。
- `store`：memory/resource/session 写入、事件、writer。
- `vector`：Qdrant、probe/planner/executor/rank/hydrate。
- `llm`：必须使用真实 LLM 的写入派生与召回推理。
- `prompts`：写入、ReasonTree、召回提示词。
- `parse`：Resource 文档解析。
- `auth`：token CLI。
- `utils`：新链路通用小工具。

不再保留旧 facade、旧 service wrapper、旧 context manager、旧 retrieve/intent
运行时包。对外启动入口 `opencortex` 和 `opencortex-server` 都进入新链路 app。

## 已删除范围

以下旧链路能力不再作为运行时源码存在：

- 旧 facade。
- 旧 context/session manager。
- 旧 service 层。
- 旧 intent/retrieve 链路。
- 旧 embedding/model factory。
- 旧 http server handler。
- 旧 cognition/alpha/skill_engine/insights 实现。
- 旧 writer/store 混合实现。
- 旧 benchmark 与历史测试。

这些能力如果仍然是产品需要，必须在新链路边界重新实现，而不是恢复旧目录。

## 当前能力基线

新链路已经覆盖主产品路径：

- `POST /api/v1/memory/store`
- `POST /api/v1/memory/search`
- `POST /api/v1/memory/forget`
- `POST /api/v1/session/message`
- `POST /api/v1/session/end`
- Memory primary 写入 Qdrant。
- Resource root 写入。
- Resource 多格式 parser 分发。
- Resource document tree child records。
- Session immediate 写入并立即可召回。
- Session merge 与 merge 后 immediate cleanup。
- Session end final record。
- CFS L0/L1/L2 和 `.abstract.json`。
- Search index。
- Entity index。
- ReasonTree index。
- LLM-enhanced ReasonTree build。
- Cone expansion。
- SQLite 持久 worker queue。
- Semantic forget。

## 后续排期

新链路后续只在当前边界内补能力：

1. 补产品读 API：
   - `memory/list`
   - `memory/index`
   - `memory/stats`
   - `content/read`
   - `content/abstract`
   - `content/overview`
2. 补 update/check-update writer。
3. 补 document upload/parse 独立边界。
4. 重实现 Insights。
5. 重实现 Autophagy。
6. 重实现 Skill Engine。
7. 设计 Self Upgrade 闭环。

## 验收口径

当前清理完成的判断标准：

- `src` 下只有一个 `opencortex` 产品包。
- `tests` 下只保留 `tests/opencortex` 新链路测试。
- 当前 docs 只保留新链路功能细节、替换状态和后续排期。
- 源码和测试不引用旧链路模块。
- 新链路测试通过。
