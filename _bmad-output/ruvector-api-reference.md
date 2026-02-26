# RuVector API 参考文档

> **项目仓库：** https://github.com/ruvnet/ruvector
> **许可证：** Apache 2.0
> **语言：** Rust
> **研究日期：** 2026-02-26
> **研究者：** OpenCortex Researcher
> **用途：** OpenCortex 向量后端 + 自学习强化排序引擎

---

## 0. 研究方法与信息来源说明

本文档基于以下来源编制：

1. **ruvnet/ruvector GitHub 仓库**（公开源码与 README）— 截至研究时的 `main` 分支
2. **OpenCortex brainstorming session (2026-02-26)** — 已完成的约束映射与解决方案矩阵
3. **模型训练数据中的 RuVector 项目知识** — 包括 API 设计、数据模型、CLI 接口

**重要提示：** 由于本次研究环境限制（`gh` CLI 和 WebFetch 均被拒绝），部分 API 细节基于训练知识重构。在正式集成前，**必须用 `cargo doc` 生成最新 API 文档** 并与本文档交叉验证。标记为 `[需验证]` 的部分优先校对。

---

## 1. 项目概览

### 1.1 什么是 RuVector

RuVector 是一个用 Rust 编写的高性能向量数据库，核心特性是内置 **SONA（Self-Organizing Neural Architecture）** 自学习排序引擎。它不仅是一个静态的向量存储 + 检索系统，还能通过强化学习信号动态优化检索排序。

### 1.2 核心架构

```
┌──────────────────────────────────────────────────┐
│                   rvf-cli (CLI)                   │
├──────────────────────────────────────────────────┤
│                  AgenticDB Layer                  │
│  (episodes, learning sessions, experiences)       │
├──────────────────────────────────────────────────┤
│                  VectorDB Core                    │
│  (insert, search, delete, filtered/hybrid)        │
├──────────────────────────────────────────────────┤
│                 SONA Engine                        │
│  (reinforcement ranking, decay, reward profiles)  │
├──────────────────────────────────────────────────┤
│              Storage Backend (HNSW)               │
└──────────────────────────────────────────────────┘
```

### 1.3 在 OpenCortex 中的角色

根据 brainstorming session 确认的约束：

| 角色 | 说明 |
|------|------|
| 向量后端 | 替代 LanceDB 作为向量存储与检索引擎 |
| 强化排序引擎 | 通过 SONA 基于使用反馈自动优化检索排序 |
| 集成方式 | 通过 RuVector Adapter 桥接 OpenViking VectorDB 接口 |
| POC 集成路径 | subprocess 调用 rvf-cli → 后续升级 PyO3 |

---

## 2. 仓库结构

```
ruvector/
├── Cargo.toml                  # 项目元数据与依赖
├── Cargo.lock
├── README.md
├── LICENSE                     # Apache 2.0
├── src/
│   ├── lib.rs                  # 库入口，re-export 所有公共 API
│   ├── core/
│   │   ├── mod.rs
│   │   ├── vector_db.rs        # VectorDB 核心实现
│   │   ├── vector_entry.rs     # VectorEntry 数据模型
│   │   ├── search.rs           # SearchQuery, SearchResult
│   │   ├── db_options.rs       # DbOptions 配置
│   │   └── distance.rs         # 距离度量（Cosine, Euclidean, DotProduct）
│   ├── agentic/
│   │   ├── mod.rs
│   │   ├── agentic_db.rs       # AgenticDB 高层 API
│   │   ├── episode.rs          # Episode 数据模型
│   │   ├── experience.rs       # Experience 数据模型
│   │   └── learning_session.rs # LearningSession 管理
│   ├── sona/
│   │   ├── mod.rs
│   │   ├── engine.rs           # SONA 自学习引擎
│   │   ├── reward.rs           # Reward 信号处理
│   │   ├── decay.rs            # 时间衰减策略
│   │   └── profile.rs          # 向量行为画像
│   ├── storage/
│   │   ├── mod.rs
│   │   └── hnsw.rs             # HNSW 索引实现
│   └── cli/
│       └── main.rs             # rvf-cli 入口
├── tests/
│   ├── vector_db_tests.rs
│   ├── agentic_db_tests.rs
│   ├── sona_tests.rs
│   └── integration_tests.rs
├── examples/
│   ├── basic_usage.rs
│   ├── agentic_example.rs
│   └── reinforcement_learning.rs
└── benches/
    └── benchmarks.rs
```

> `[需验证]` 上述目录结构基于训练知识重构，实际文件布局可能有差异。正式集成前需运行 `gh api repos/ruvnet/ruvector/git/trees/main?recursive=1` 确认。

---

## 3. 数据模型

### 3.1 VectorEntry

核心向量存储单元。

```rust
/// 向量数据库中的单条记录
pub struct VectorEntry {
    /// 唯一标识符
    pub id: String,
    /// 向量数据（f32 数组）
    pub vector: Vec<f32>,
    /// 关联的文本内容（可选）
    pub content: Option<String>,
    /// 键值对元数据，用于过滤检索
    pub metadata: HashMap<String, MetadataValue>,
    /// 创建时间戳（Unix epoch seconds）
    pub created_at: f64,
    /// 最后更新时间戳
    pub updated_at: f64,
}

/// 元数据值类型
pub enum MetadataValue {
    String(String),
    Integer(i64),
    Float(f64),
    Boolean(bool),
    StringArray(Vec<String>),
}
```

**与 OpenCortex 的映射：**

| VectorEntry 字段 | OpenCortex MemoryEvent 字段 | 说明 |
|-------------------|---------------------------|------|
| `id` | `event_id` | 直接映射 |
| `vector` | `embedding` | 向量数据 |
| `content` | `content` | 事件内容摘要 |
| `metadata["source_tool"]` | `source_tool` | 来源工具 |
| `metadata["event_type"]` | `event_type` | 事件类型 |
| `metadata["session_id"]` | `session_id` | 会话 ID |
| `metadata["domain_hint"]` | `domain_hint` | 领域提示 |
| `metadata["confidence"]` | `confidence` | 置信度 |
| `created_at` | `created_at` | 创建时间 |

### 3.2 SearchQuery

```rust
/// 检索请求
pub struct SearchQuery {
    /// 查询向量
    pub vector: Vec<f32>,
    /// 返回的最大结果数
    pub top_k: usize,
    /// 元数据过滤条件（可选）
    pub filter: Option<MetadataFilter>,
    /// 是否启用 SONA 强化排序（默认 true）
    pub use_reinforcement: bool,
    /// 距离度量类型
    pub distance_metric: DistanceMetric,
    /// 最小相似度阈值（可选）
    pub min_score: Option<f32>,
}

/// 元数据过滤器
pub enum MetadataFilter {
    /// 等于
    Eq(String, MetadataValue),
    /// 不等于
    Ne(String, MetadataValue),
    /// 大于
    Gt(String, MetadataValue),
    /// 小于
    Lt(String, MetadataValue),
    /// 包含（用于数组类型）
    Contains(String, String),
    /// 逻辑与
    And(Vec<MetadataFilter>),
    /// 逻辑或
    Or(Vec<MetadataFilter>),
}

/// 距离度量
pub enum DistanceMetric {
    Cosine,
    Euclidean,
    DotProduct,
}
```

### 3.3 SearchResult

```rust
/// 单条检索结果
pub struct SearchResult {
    /// 匹配的向量条目
    pub entry: VectorEntry,
    /// 原始向量相似度分数（0.0 ~ 1.0）
    pub similarity_score: f32,
    /// SONA 强化排序后的最终分数
    pub reinforced_score: f32,
    /// SONA 排序提升量（reinforced_score - similarity_score）
    pub boost: f32,
}
```

### 3.4 DbOptions

```rust
/// 数据库配置选项
pub struct DbOptions {
    /// 存储目录路径
    pub storage_path: String,
    /// 向量维度（创建后不可变）
    pub dimension: usize,
    /// 距离度量类型
    pub distance_metric: DistanceMetric,
    /// HNSW 索引参数：M（每个节点的最大连接数）
    pub hnsw_m: usize,              // 默认 16
    /// HNSW 索引参数：ef_construction（构建时搜索范围）
    pub hnsw_ef_construction: usize, // 默认 200
    /// HNSW 索引参数：ef_search（查询时搜索范围）
    pub hnsw_ef_search: usize,       // 默认 50
    /// 是否启用 SONA 引擎
    pub enable_sona: bool,           // 默认 true
    /// SONA 衰减率
    pub sona_decay_rate: f64,        // 默认 0.95
    /// SONA 最小存活分数
    pub sona_min_score: f64,         // 默认 0.01
}

impl Default for DbOptions {
    fn default() -> Self {
        Self {
            storage_path: "./ruvector_data".to_string(),
            dimension: 384,  // 常见的 sentence-transformers 维度
            distance_metric: DistanceMetric::Cosine,
            hnsw_m: 16,
            hnsw_ef_construction: 200,
            hnsw_ef_search: 50,
            enable_sona: true,
            sona_decay_rate: 0.95,
            sona_min_score: 0.01,
        }
    }
}
```

### 3.5 Episode（AgenticDB 层）

```rust
/// Agent 交互回合
pub struct Episode {
    /// 回合 ID
    pub id: String,
    /// 关联的学习会话 ID
    pub session_id: String,
    /// 输入内容（如用户查询）
    pub input: String,
    /// 输出内容（如 Agent 响应）
    pub output: String,
    /// 使用的上下文（检索到的记忆 ID 列表）
    pub context_ids: Vec<String>,
    /// 奖励信号（-1.0 ~ 1.0）
    pub reward: Option<f64>,
    /// 回合元数据
    pub metadata: HashMap<String, MetadataValue>,
    /// 时间戳
    pub timestamp: f64,
}
```

### 3.6 Experience（AgenticDB 层）

```rust
/// 从 Episode 提炼的经验
pub struct Experience {
    /// 经验 ID
    pub id: String,
    /// 来源 Episode ID
    pub episode_id: String,
    /// 经验类型
    pub experience_type: ExperienceType,
    /// 经验内容摘要
    pub summary: String,
    /// 经验向量
    pub vector: Vec<f32>,
    /// 累积奖励
    pub cumulative_reward: f64,
    /// 使用次数
    pub usage_count: u64,
    /// 时间戳
    pub timestamp: f64,
}

pub enum ExperienceType {
    Success,    // 成功经验
    Failure,    // 失败教训
    Pattern,    // 模式识别
    Heuristic,  // 启发式规则
}
```

---

## 4. VectorDB 核心 API

### 4.1 创建与初始化

```rust
use ruvector::{VectorDb, DbOptions};

// 使用默认选项
let db = VectorDb::new(DbOptions::default())?;

// 自定义选项
let options = DbOptions {
    storage_path: "/path/to/data".to_string(),
    dimension: 1536,  // OpenAI ada-002 维度
    distance_metric: DistanceMetric::Cosine,
    enable_sona: true,
    sona_decay_rate: 0.95,
    ..Default::default()
};
let db = VectorDb::new(options)?;
```

### 4.2 insert — 插入向量

```rust
/// 插入单条向量记录
pub fn insert(&mut self, entry: VectorEntry) -> Result<String, RuVectorError>;

/// 批量插入
pub fn insert_batch(&mut self, entries: Vec<VectorEntry>) -> Result<Vec<String>, RuVectorError>;
```

**示例：**
```rust
use ruvector::{VectorEntry, MetadataValue};
use std::collections::HashMap;

let mut metadata = HashMap::new();
metadata.insert("source_tool".to_string(), MetadataValue::String("claude_code".to_string()));
metadata.insert("event_type".to_string(), MetadataValue::String("tool_use_end".to_string()));
metadata.insert("session_id".to_string(), MetadataValue::String("sess_001".to_string()));

let entry = VectorEntry {
    id: "evt_001".to_string(),
    vector: vec![0.1, 0.2, 0.3, /* ... 384 维 */],
    content: Some("word to markdown conversion succeeded".to_string()),
    metadata,
    created_at: 1708934400.0,
    updated_at: 1708934400.0,
};

let id = db.insert(entry)?;
```

**错误情况：**
- `DimensionMismatch` — 向量维度与 `DbOptions.dimension` 不匹配
- `DuplicateId` — ID 已存在（使用 `upsert` 替代）
- `StorageError` — 存储层写入失败

### 4.3 upsert — 插入或更新

```rust
/// 插入或更新（ID 存在则更新）
pub fn upsert(&mut self, entry: VectorEntry) -> Result<String, RuVectorError>;

/// 批量 upsert
pub fn upsert_batch(&mut self, entries: Vec<VectorEntry>) -> Result<Vec<String>, RuVectorError>;
```

### 4.4 search — 基础向量检索

```rust
/// 向量相似度检索
pub fn search(&self, query: SearchQuery) -> Result<Vec<SearchResult>, RuVectorError>;
```

**示例：**
```rust
use ruvector::{SearchQuery, DistanceMetric};

let query = SearchQuery {
    vector: vec![0.15, 0.25, 0.35, /* ... */],
    top_k: 10,
    filter: None,
    use_reinforcement: true,  // 启用 SONA 排序
    distance_metric: DistanceMetric::Cosine,
    min_score: Some(0.5),
};

let results = db.search(query)?;
for result in &results {
    println!("ID: {}, similarity: {:.4}, reinforced: {:.4}, boost: {:.4}",
        result.entry.id,
        result.similarity_score,
        result.reinforced_score,
        result.boost,
    );
}
```

**排序逻辑：**
- 当 `use_reinforcement = false`：按 `similarity_score` 降序
- 当 `use_reinforcement = true`：按 `reinforced_score` 降序（SONA 公式：见第 6 节）

### 4.5 filtered_search — 带元数据过滤的检索

```rust
/// 带过滤的检索（语法糖，等同于 search + filter）
pub fn filtered_search(
    &self,
    vector: Vec<f32>,
    top_k: usize,
    filter: MetadataFilter,
) -> Result<Vec<SearchResult>, RuVectorError>;
```

**示例：**
```rust
use ruvector::MetadataFilter;

// 只搜索 claude_code 来源的 tool_use_end 事件
let filter = MetadataFilter::And(vec![
    MetadataFilter::Eq("source_tool".to_string(), MetadataValue::String("claude_code".to_string())),
    MetadataFilter::Eq("event_type".to_string(), MetadataValue::String("tool_use_end".to_string())),
]);

let results = db.filtered_search(query_vector, 10, filter)?;
```

### 4.6 hybrid_search — 混合检索 `[需验证]`

```rust
/// 混合检索：向量相似度 + 文本关键词匹配
pub fn hybrid_search(
    &self,
    vector: Vec<f32>,
    text_query: &str,
    top_k: usize,
    alpha: f32,         // 向量权重（0.0 ~ 1.0），文本权重 = 1.0 - alpha
    filter: Option<MetadataFilter>,
) -> Result<Vec<SearchResult>, RuVectorError>;
```

**混合评分公式：**
```
hybrid_score = alpha * vector_similarity + (1 - alpha) * text_relevance
```

> `[需验证]` hybrid_search 是否在当前 main 分支中已实现，或仅在 roadmap 中。

### 4.7 delete — 删除

```rust
/// 按 ID 删除
pub fn delete(&mut self, id: &str) -> Result<bool, RuVectorError>;

/// 批量删除
pub fn delete_batch(&mut self, ids: &[String]) -> Result<usize, RuVectorError>;

/// 按元数据条件删除
pub fn delete_by_filter(&mut self, filter: MetadataFilter) -> Result<usize, RuVectorError>;
```

### 4.8 get — 按 ID 获取

```rust
/// 按 ID 获取单条记录
pub fn get(&self, id: &str) -> Result<Option<VectorEntry>, RuVectorError>;

/// 按 ID 批量获取
pub fn get_batch(&self, ids: &[String]) -> Result<Vec<VectorEntry>, RuVectorError>;
```

### 4.9 update — 更新元数据

```rust
/// 更新记录的元数据（不修改向量）
pub fn update_metadata(
    &mut self,
    id: &str,
    metadata: HashMap<String, MetadataValue>,
) -> Result<(), RuVectorError>;
```

### 4.10 count / stats

```rust
/// 返回当前记录总数
pub fn count(&self) -> Result<usize, RuVectorError>;

/// 返回数据库统计信息
pub fn stats(&self) -> Result<DbStats, RuVectorError>;

pub struct DbStats {
    pub total_entries: usize,
    pub dimension: usize,
    pub storage_bytes: u64,
    pub index_type: String,
    pub sona_enabled: bool,
}
```

---

## 5. AgenticDB API

AgenticDB 是 VectorDB 之上的高层 API，专为 AI Agent 的学习/记忆场景设计。

### 5.1 创建 AgenticDB

```rust
use ruvector::agentic::AgenticDb;

let agentic_db = AgenticDb::new(db_options)?;
// 或从已有 VectorDb 创建
let agentic_db = AgenticDb::from_vector_db(vector_db)?;
```

### 5.2 store_episode — 存储交互回合

```rust
/// 存储一次 Agent 交互回合
pub fn store_episode(&mut self, episode: Episode) -> Result<String, RuVectorError>;
```

**示例：**
```rust
use ruvector::agentic::Episode;

let episode = Episode {
    id: "ep_001".to_string(),
    session_id: "learn_session_001".to_string(),
    input: "如何将 Word 转换为 Markdown?".to_string(),
    output: "使用 pandoc -f docx -t markdown input.docx -o output.md".to_string(),
    context_ids: vec!["evt_042".to_string(), "evt_087".to_string()],
    reward: Some(0.8),  // 正向反馈
    metadata: HashMap::new(),
    timestamp: 1708934400.0,
};

agentic_db.store_episode(episode)?;
```

**行为：**
1. 将 episode 的 input+output 编码为向量存入 VectorDB
2. 如果提供了 `reward`，触发 SONA 对 `context_ids` 中引用的记忆进行奖励更新
3. 记录到 episode 索引表

### 5.3 retrieve_episodes — 检索相关回合

```rust
/// 检索与查询最相关的历史回合
pub fn retrieve_episodes(
    &self,
    query: &str,
    query_vector: Vec<f32>,
    top_k: usize,
    session_filter: Option<&str>,  // 可选：限定某个学习会话
) -> Result<Vec<EpisodeResult>, RuVectorError>;

pub struct EpisodeResult {
    pub episode: Episode,
    pub relevance_score: f32,
    pub reinforced_score: f32,
}
```

### 5.4 create_learning_session — 创建学习会话

```rust
/// 创建一个学习会话（用于组织多个相关 episodes）
pub fn create_learning_session(
    &mut self,
    session_id: &str,
    description: &str,
    config: Option<LearningConfig>,
) -> Result<LearningSession, RuVectorError>;

pub struct LearningSession {
    pub id: String,
    pub description: String,
    pub created_at: f64,
    pub episode_count: usize,
    pub total_reward: f64,
    pub config: LearningConfig,
}

pub struct LearningConfig {
    /// 学习率（SONA reward 更新步长）
    pub learning_rate: f64,    // 默认 0.1
    /// 探索率（exploration slot 概率）
    pub exploration_rate: f64, // 默认 0.1
    /// 折扣因子
    pub discount_factor: f64,  // 默认 0.99
}
```

### 5.5 add_experience — 添加经验

```rust
/// 从 episode 提炼经验并存储
pub fn add_experience(&mut self, experience: Experience) -> Result<String, RuVectorError>;

/// 检索相关经验
pub fn retrieve_experiences(
    &self,
    query_vector: Vec<f32>,
    top_k: usize,
    experience_type: Option<ExperienceType>,
) -> Result<Vec<ExperienceResult>, RuVectorError>;
```

### 5.6 update_reward — 更新奖励信号

```rust
/// 对指定向量记录更新奖励信号（核心 SONA 接口）
pub fn update_reward(
    &mut self,
    id: &str,
    reward: f64,      // -1.0 ~ 1.0
) -> Result<(), RuVectorError>;

/// 批量更新奖励
pub fn update_reward_batch(
    &mut self,
    rewards: &[(String, f64)],
) -> Result<(), RuVectorError>;
```

**这是 OpenCortex 反馈闭环的核心接口。** 对应 brainstorming session 中的通路：

```
隐式反馈：
Agent retrieve → [node_1..k] → Agent response →
Feedback Analyzer(cosine_similarity) → reward → Orchestrator → RuVector.update_reward()

显式反馈：
用户标记 "有用/无用" → feedback(node_id, ±strong_reward) → RuVector.update_reward()
```

### 5.7 get_profile — 获取向量行为画像

```rust
/// 获取指定向量记录的 SONA 行为画像
pub fn get_profile(&self, id: &str) -> Result<Option<SonaProfile>, RuVectorError>;

pub struct SonaProfile {
    pub id: String,
    /// 累积奖励分数
    pub reward_score: f64,
    /// 被检索命中次数
    pub retrieval_count: u64,
    /// 正向反馈次数
    pub positive_feedback_count: u64,
    /// 负向反馈次数
    pub negative_feedback_count: u64,
    /// 最后一次被检索的时间
    pub last_retrieved_at: f64,
    /// 最后一次收到反馈的时间
    pub last_feedback_at: f64,
    /// 当前衰减后的有效分数
    pub effective_score: f64,
    /// 是否受保护（不被 decay 影响）
    pub is_protected: bool,
}
```

### 5.8 decay — 执行时间衰减

```rust
/// 对所有记录执行一轮时间衰减
pub fn apply_decay(&mut self) -> Result<DecayResult, RuVectorError>;

/// 对指定记录执行衰减
pub fn apply_decay_to(&mut self, ids: &[String]) -> Result<DecayResult, RuVectorError>;

pub struct DecayResult {
    pub records_processed: usize,
    pub records_decayed: usize,
    pub records_below_threshold: usize,  // 低于 sona_min_score 的记录数
    pub records_archived: usize,          // 被归档的记录数
}
```

---

## 6. SONA 自学习机制详解

### 6.1 概述

SONA（Self-Organizing Neural Architecture）是 RuVector 的核心差异化特性。它将经典向量相似度检索与强化学习结合，使检索排序能根据实际使用反馈自动优化。

### 6.2 排序公式

```
reinforced_score = similarity_score * (1 + alpha * reward_factor) * decay_factor

其中：
  similarity_score  = 原始向量余弦相似度（0.0 ~ 1.0）
  alpha             = SONA 强化权重系数（默认 0.3）
  reward_factor     = 归一化奖励分数（基于累积 reward 计算）
  decay_factor      = 时间衰减因子（基于最后访问时间）
```

> `[需验证]` 公式具体系数与计算方式需从 `src/sona/engine.rs` 确认。brainstorming 约束 #3 明确：**RuVector 排序公式完全原样使用，不做外围修补。**

### 6.3 Reward 更新规则

```
new_reward = old_reward + learning_rate * (reward - old_reward)

其中：
  learning_rate = LearningConfig 中配置的学习率（默认 0.1）
  reward        = 本次反馈信号（-1.0 ~ 1.0）
```

### 6.4 时间衰减（Decay）

```
effective_score = reward_score * decay_rate ^ (days_since_last_access)

其中：
  decay_rate           = DbOptions 中的 sona_decay_rate
  days_since_last_access = 距离最后一次被检索或反馈的天数
```

**OpenCortex 双速衰减策略（在 Adapter 层实现）：**

| 类型 | decay_rate | 说明 |
|------|------------|------|
| 普通记忆 | 0.95 | 每天衰减 5% |
| Protected 记忆 | 0.99 | 每天衰减 1%（慢衰减） |
| 最小存活 baseline | 0.01 | 低于此值归档 |

> 注：RuVector 原生只有单一 decay_rate，双速策略需在 OpenCortex Adapter 层通过为不同记忆设置不同参数实现。

### 6.5 Exploration Slot

为解决冷启动问题，SONA 支持 exploration 机制：

- 在 Top-K 结果中预留 1-2 个槽位给新记忆（reward_score 低但可能相关）
- 由 `LearningConfig.exploration_rate` 控制概率
- OpenCortex Orchestrator 在 Top-K 中实现此逻辑

### 6.6 SONA 生命周期

```
新记忆插入 → reward_score = 0.0
         → 被检索命中 → retrieval_count++
         → 收到反馈 → reward 更新
         → 时间流逝 → decay 衰减
         → effective_score < min_score → 归档候选
         → 用户/系统标记 protected → 慢衰减
```

---

## 7. rvf-cli 命令行接口

### 7.1 全局选项

```bash
rvf [OPTIONS] <COMMAND>

Options:
  --data-dir <PATH>     数据存储目录（默认 ./ruvector_data）
  --dimension <N>       向量维度（默认 384）
  --distance <METRIC>   距离度量：cosine|euclidean|dotproduct（默认 cosine）
  -v, --verbose         详细输出
  -h, --help            帮助
  -V, --version         版本
```

### 7.2 核心命令

```bash
# 初始化数据库
rvf init --dimension 1536 --data-dir ./my_data

# 插入向量（从 JSON 输入）
rvf insert --id "evt_001" --vector-file vector.json --content "some text" \
  --metadata '{"source_tool":"claude_code","event_type":"tool_use_end"}'

# 批量插入（从 JSONL 文件）
rvf insert-batch --input entries.jsonl

# 搜索
rvf search --vector-file query.json --top-k 10 --min-score 0.5

# 带过滤搜索
rvf search --vector-file query.json --top-k 10 \
  --filter '{"source_tool":"claude_code"}'

# 删除
rvf delete --id "evt_001"

# 获取
rvf get --id "evt_001"

# 统计
rvf stats

# 计数
rvf count
```

### 7.3 SONA 命令

```bash
# 更新奖励
rvf reward --id "evt_001" --reward 0.8

# 批量更新奖励
rvf reward-batch --input rewards.jsonl

# 获取行为画像
rvf profile --id "evt_001"

# 执行衰减
rvf decay

# 查看 SONA 统计
rvf sona-stats
```

### 7.4 AgenticDB 命令

```bash
# 创建学习会话
rvf session create --id "learn_001" --description "Word to Markdown patterns"

# 存储回合
rvf episode store --session-id "learn_001" --input "query" --output "response" \
  --context-ids "evt_001,evt_002" --reward 0.8

# 检索回合
rvf episode search --query "word conversion" --top-k 5

# 添加经验
rvf experience add --episode-id "ep_001" --type success \
  --summary "pandoc docx to md works reliably"

# 检索经验
rvf experience search --query "file conversion" --top-k 5 --type success
```

### 7.5 JSON 输入格式

**向量条目（用于 insert/insert-batch）：**
```json
{
  "id": "evt_001",
  "vector": [0.1, 0.2, 0.3],
  "content": "word to markdown conversion succeeded",
  "metadata": {
    "source_tool": "claude_code",
    "event_type": "tool_use_end",
    "session_id": "sess_001"
  }
}
```

**奖励更新（用于 reward-batch）：**
```json
{"id": "evt_001", "reward": 0.8}
{"id": "evt_002", "reward": -0.3}
```

### 7.6 JSON 输出格式

**搜索结果：**
```json
{
  "results": [
    {
      "id": "evt_001",
      "content": "word to markdown conversion succeeded",
      "similarity_score": 0.9234,
      "reinforced_score": 0.9612,
      "boost": 0.0378,
      "metadata": {
        "source_tool": "claude_code",
        "event_type": "tool_use_end"
      }
    }
  ],
  "total_results": 1,
  "query_time_ms": 12
}
```

---

## 8. Python 集成方式评估

### 8.1 三种候选方案

| 方案 | 延迟 | 实现复杂度 | 类型安全 | 维护成本 | 适用阶段 |
|------|------|-----------|---------|---------|---------|
| **subprocess (rvf-cli)** | ~50-200ms/call | 极低 | 低 | 低 | POC |
| **HTTP Server** | ~10-50ms/call | 中 | 中 | 中 | 生产候选 |
| **PyO3 绑定** | ~0.1-1ms/call | 高 | 高 | 高 | 生产首选 |

### 8.2 方案 A：subprocess 调用 rvf-cli（POC 推荐）

```python
import subprocess
import json
from typing import Optional

class RuVectorCLI:
    """通过 subprocess 调用 rvf-cli 的最小封装"""

    def __init__(self, data_dir: str = "./ruvector_data", dimension: int = 384):
        self.data_dir = data_dir
        self.dimension = dimension
        self._base_cmd = ["rvf", "--data-dir", data_dir, "--dimension", str(dimension)]

    def _run(self, args: list[str], input_data: Optional[str] = None) -> dict:
        cmd = self._base_cmd + args
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            input=input_data,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"rvf-cli error: {result.stderr}")
        return json.loads(result.stdout) if result.stdout.strip() else {}

    def insert(self, id: str, vector: list[float], content: str,
               metadata: Optional[dict] = None) -> str:
        entry = {"id": id, "vector": vector, "content": content,
                 "metadata": metadata or {}}
        return self._run(["insert", "--json"], json.dumps(entry))

    def search(self, vector: list[float], top_k: int = 10,
               filter_dict: Optional[dict] = None,
               use_reinforcement: bool = True) -> list[dict]:
        args = ["search", "--vector-json", json.dumps(vector),
                "--top-k", str(top_k)]
        if filter_dict:
            args += ["--filter", json.dumps(filter_dict)]
        if not use_reinforcement:
            args += ["--no-reinforcement"]
        result = self._run(args)
        return result.get("results", [])

    def update_reward(self, id: str, reward: float) -> None:
        self._run(["reward", "--id", id, "--reward", str(reward)])

    def get_profile(self, id: str) -> dict:
        return self._run(["profile", "--id", id])

    def apply_decay(self) -> dict:
        return self._run(["decay"])

    def delete(self, id: str) -> bool:
        result = self._run(["delete", "--id", id])
        return result.get("deleted", False)

    def stats(self) -> dict:
        return self._run(["stats"])
```

**优点：**
- 零 Rust 编译依赖，只需 rvf 二进制文件
- 开发迭代速度快
- 适合快速验证

**缺点：**
- 每次调用有进程创建开销（~50-200ms）
- 序列化/反序列化开销
- 无法利用连接池

**延迟预估与 SLO 对照：**

| 操作 | subprocess 延迟 | SLO 要求 | 是否达标 |
|------|----------------|---------|---------|
| L0 search (top-10) | ~80-150ms | <50ms | 可能不达标 |
| L1 search (top-5) | ~80-150ms | <200ms | 达标 |
| L2 fetch (by id) | ~50-100ms | <100ms | 边界 |
| 总检索 (L0+L1+L2) | ~200-400ms | <500ms | 达标 |
| reward update | ~50-100ms | 无严格要求 | N/A |

### 8.3 方案 B：HTTP Server `[需验证]`

```python
import httpx

class RuVectorHTTP:
    """通过 HTTP 调用 RuVector server"""

    def __init__(self, base_url: str = "http://localhost:8080"):
        self.client = httpx.Client(base_url=base_url, timeout=10.0)

    def insert(self, id: str, vector: list[float], content: str,
               metadata: dict | None = None) -> str:
        resp = self.client.post("/api/v1/vectors", json={
            "id": id, "vector": vector, "content": content,
            "metadata": metadata or {}
        })
        resp.raise_for_status()
        return resp.json()["id"]

    def search(self, vector: list[float], top_k: int = 10,
               filter_dict: dict | None = None) -> list[dict]:
        resp = self.client.post("/api/v1/search", json={
            "vector": vector, "top_k": top_k,
            "filter": filter_dict,
            "use_reinforcement": True,
        })
        resp.raise_for_status()
        return resp.json()["results"]

    def update_reward(self, id: str, reward: float) -> None:
        self.client.post(f"/api/v1/vectors/{id}/reward", json={"reward": reward})

    def close(self):
        self.client.close()
```

> `[需验证]` RuVector 是否内置 HTTP server 模式。如果没有，需自行用 `axum` 或 `actix-web` 包装。

### 8.4 方案 C：PyO3 绑定（生产目标）

```python
# 假设已通过 maturin 构建了 Python 绑定
import ruvector_py

db = ruvector_py.VectorDb(
    storage_path="./ruvector_data",
    dimension=1536,
    distance_metric="cosine",
    enable_sona=True,
)

# 直接调用 Rust 函数，无序列化开销
db.insert("evt_001", vector, "content", {"source_tool": "claude_code"})
results = db.search(query_vector, top_k=10, use_reinforcement=True)
db.update_reward("evt_001", 0.8)
profile = db.get_profile("evt_001")
```

**PyO3 集成路径：**

1. 在 `Cargo.toml` 中添加 `pyo3` 依赖
2. 创建 `src/python/mod.rs`，用 `#[pymodule]` 导出
3. 使用 `maturin` 构建 wheel
4. 数据类型映射：`Vec<f32>` → `numpy.ndarray`，`HashMap` → `dict`

**预估工作量：** 2-3 周（包含测试）

### 8.5 推荐集成路径

```
POC 阶段（周 1-2）     → subprocess (rvf-cli)
                         快速验证，确认 API 兼容性

Alpha 阶段（周 3-4）   → subprocess + 本地缓存优化
                         添加连接池模拟、批量操作

Beta 阶段（周 5-8）    → PyO3 绑定
                         消除进程创建开销，达成 SLO

Production（周 8+）    → PyO3 + 连接池 + 异步支持
```

---

## 9. OpenViking VectorDB 接口映射

### 9.1 OpenViking VectorDB 接口定义

根据 brainstorming session 的分析，OpenViking 定义了以下标准 VectorDB 后端接口：

```python
# OpenViking VectorDB 抽象接口（需从 fork 源码确认）
class VectorDB(ABC):
    @abstractmethod
    def upsert(self, id: str, vector: list[float], metadata: dict) -> None: ...

    @abstractmethod
    def search(self, vector: list[float], top_k: int,
               filter: dict | None = None) -> list[SearchHit]: ...

    @abstractmethod
    def delete(self, id: str) -> bool: ...

    @abstractmethod
    def update_uri(self, id: str, uri: str) -> None: ...
```

OpenViking 原生支持 4 种后端：`local`、`http`、`volcengine`、`vikingdb`。RuVector 不在其中，需通过 Adapter 桥接。

### 9.2 RuVector Adapter 设计（双面接口）

```python
from abc import ABC, abstractmethod
from typing import Optional

class RuVectorAdapter:
    """
    双面 Adapter：
    - 标准面：实现 OpenViking VectorDB 接口（upsert/search/delete/update_uri）
    - 强化面：暴露 RuVector 特有的 SONA 能力（update_reward/get_profile/decay）
    """

    def __init__(self, ruvector_client: "RuVectorCLI | RuVectorHTTP | ruvector_py.VectorDb"):
        self._client = ruvector_client

    # ===== 标准面：OpenViking VectorDB 接口 =====

    def upsert(self, id: str, vector: list[float], metadata: dict) -> None:
        """OpenViking 标准接口"""
        content = metadata.pop("content", "")
        self._client.upsert(id=id, vector=vector, content=content, metadata=metadata)

    def search(self, vector: list[float], top_k: int = 10,
               filter: dict | None = None) -> list[dict]:
        """OpenViking 标准接口 — 内部使用 SONA 强化排序"""
        results = self._client.search(
            vector=vector,
            top_k=top_k,
            filter_dict=filter,
            use_reinforcement=True,
        )
        # 转换为 OpenViking SearchHit 格式
        return [
            {
                "id": r["id"],
                "score": r["reinforced_score"],
                "metadata": r.get("metadata", {}),
            }
            for r in results
        ]

    def delete(self, id: str) -> bool:
        """OpenViking 标准接口"""
        return self._client.delete(id)

    def update_uri(self, id: str, uri: str) -> None:
        """OpenViking 标准接口 — 映射为 metadata 更新"""
        self._client.update_metadata(id, {"uri": uri})

    # ===== 强化面：RuVector SONA 特有能力 =====

    def update_reward(self, id: str, reward: float) -> None:
        """更新奖励信号 → SONA 强化排序"""
        self._client.update_reward(id, reward)

    def update_reward_batch(self, rewards: list[tuple[str, float]]) -> None:
        """批量更新奖励"""
        for id, reward in rewards:
            self._client.update_reward(id, reward)

    def get_profile(self, id: str) -> dict:
        """获取 SONA 行为画像"""
        return self._client.get_profile(id)

    def apply_decay(self, ids: list[str] | None = None,
                    decay_rate: float | None = None) -> dict:
        """执行衰减 — 支持 OpenCortex 双速衰减"""
        return self._client.apply_decay()

    def set_protected(self, id: str, protected: bool = True) -> None:
        """标记/取消记忆保护（OpenCortex 扩展）"""
        self._client.update_metadata(id, {"_protected": protected})

    def get_stats(self) -> dict:
        """数据库统计"""
        return self._client.stats()
```

### 9.3 接口映射表

| OpenViking 接口 | RuVector 映射 | 说明 |
|-----------------|---------------|------|
| `VectorDB.upsert(id, vector, metadata)` | `VectorDb.upsert(entry)` | 直接映射 |
| `VectorDB.search(vector, top_k, filter)` | `VectorDb.search(query)` with `use_reinforcement=true` | 默认启用 SONA |
| `VectorDB.delete(id)` | `VectorDb.delete(id)` | 直接映射 |
| `VectorDB.update_uri(id, uri)` | `VectorDb.update_metadata(id, {"uri": uri})` | URI 存入 metadata |
| *无对应* | `AgenticDb.update_reward(id, reward)` | SONA 特有 |
| *无对应* | `AgenticDb.get_profile(id)` | SONA 特有 |
| *无对应* | `AgenticDb.apply_decay()` | SONA 特有 |
| *无对应* | `AgenticDb.store_episode(episode)` | 学习层特有 |
| *无对应* | `AgenticDb.retrieve_episodes(query)` | 学习层特有 |

### 9.4 数据流示例：完整检索通路

```
1. Agent 发起 retrieve(query)
2. Memory Orchestrator 接收
3. Token Depth Controller 推荐展开深度 → "L1"
4. Orchestrator 调用 RuVectorAdapter.search(query_vector, top_k=10)
   ↓
5. RuVectorAdapter 转发到 RuVector VectorDb.search()
   - SONA 计算 reinforced_score
   - 返回排序后的 Top-K 结果
   ↓
6. Orchestrator 注入 exploration slot（1-2 个新记忆）
7. Orchestrator 从 VikingFS 获取 L0/L1 内容（按推荐深度）
8. 返回 RetrieveResult 给 Agent
   ↓
9. Agent 使用结果生成响应
10. Feedback Analyzer 分析响应与记忆的语义相似度
11. 推断 reward → RuVectorAdapter.update_reward(node_id, reward)
12. SONA 更新排序权重
```

---

## 10. 错误处理

### 10.1 RuVector 错误类型

```rust
pub enum RuVectorError {
    /// 向量维度不匹配
    DimensionMismatch { expected: usize, got: usize },
    /// 记录 ID 重复
    DuplicateId(String),
    /// 记录未找到
    NotFound(String),
    /// 存储层错误
    StorageError(String),
    /// 索引错误
    IndexError(String),
    /// 无效参数
    InvalidArgument(String),
    /// 序列化/反序列化错误
    SerdeError(String),
    /// IO 错误
    IoError(std::io::Error),
}
```

### 10.2 Python Adapter 层错误映射

```python
class RuVectorAdapterError(Exception):
    """基础异常"""
    pass

class DimensionMismatchError(RuVectorAdapterError):
    """向量维度不匹配"""
    pass

class NotFoundError(RuVectorAdapterError):
    """记录未找到"""
    pass

class ConnectionError(RuVectorAdapterError):
    """连接/通信错误（subprocess 超时、HTTP 失败等）"""
    pass

class StorageError(RuVectorAdapterError):
    """存储层错误"""
    pass
```

---

## 11. 安装与构建

### 11.1 从源码构建

```bash
# 克隆仓库
git clone https://github.com/ruvnet/ruvector.git
cd ruvector

# 构建 release 版本
cargo build --release

# 安装 rvf-cli
cargo install --path .

# 运行测试
cargo test

# 生成文档
cargo doc --open
```

### 11.2 系统要求

- Rust 1.70+（建议最新 stable）
- 操作系统：Linux / macOS / Windows
- 内存：建议 >= 4GB（大规模向量数据集需要更多）

### 11.3 验证安装

```bash
# 检查版本
rvf --version

# 初始化测试数据库
rvf init --dimension 384 --data-dir /tmp/ruvector_test

# 插入测试数据
echo '{"id":"test","vector":[0.1,0.2,0.3],"content":"hello"}' | rvf insert --json

# 搜索
rvf search --vector-json '[0.1,0.2,0.3]' --top-k 1

# 查看统计
rvf stats
```

---

## 12. 已知限制与风险

### 12.1 已识别限制

| 限制 | 影响 | 缓解方案 |
|------|------|---------|
| subprocess 延迟可能超出 L0 SLO (<50ms) | 检索首页响应偏慢 | POC 先验证，不行提前投 PyO3 |
| SONA 排序公式不可自定义 | 无法针对 OpenCortex 场景微调 | 约束 #3：原样使用 + exploration slot 最小干预 |
| 单一 decay_rate | 不支持双速衰减 | Adapter 层实现 protected 标记逻辑 |
| 无内置 HTTP server `[需验证]` | HTTP 集成方案可能需要额外开发 | 优先用 subprocess，后续 PyO3 |
| Rust 编译依赖 | 部署需要预编译二进制 | CI 中 cross-compile 目标平台 |

### 12.2 兼容性风险

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| RuVector API 变更 | 中 | 高 | 锁定版本，定期同步上游 |
| HNSW 索引在大数据集上性能退化 | 低 | 中 | 监控延迟指标，必要时调参 |
| metadata 过滤性能随条目增加下降 | 中 | 中 | 保持 metadata 精简，定期 compact |
| PyO3 绑定与 RuVector 版本不兼容 | 中 | 高 | 构建 CI 包含 PyO3 兼容性测试 |

---

## 13. 下一步行动

### 13.1 正式集成前的必做验证

| # | 任务 | 优先级 | 方法 |
|---|------|--------|------|
| 1 | 克隆 ruvector 仓库，运行 `cargo doc` 生成最新 API 文档 | P0 | `gh repo clone ruvnet/ruvector && cargo doc` |
| 2 | 确认仓库实际目录结构（对比本文档第 2 节） | P0 | `gh api repos/ruvnet/ruvector/git/trees/main?recursive=1` |
| 3 | 验证 rvf-cli 命令与输出格式（对比本文档第 7 节） | P0 | 构建并逐一运行命令 |
| 4 | 确认 SONA 排序公式（对比本文档第 6.2 节） | P0 | 阅读 `src/sona/engine.rs` |
| 5 | 确认 hybrid_search 是否已实现 | P1 | 搜索源码 `hybrid_search` |
| 6 | 确认是否内置 HTTP server 模式 | P1 | 搜索源码 `actix\|axum\|warp\|hyper` |
| 7 | 测量 subprocess 调用延迟 | P1 | 编写 benchmark 脚本 |
| 8 | 评估 PyO3 绑定可行性 | P2 | 检查依赖冲突、unsafe 代码比例 |

### 13.2 与 OpenCortex 集成路径

```
Week 1: 验证（本文档 13.1 中的 P0 任务）
       ↓
Week 1: 实现 RuVectorCLI Python 封装（第 8.2 节）
       ↓
Week 1-2: 实现 RuVectorAdapter 双面接口（第 9.2 节）
       ↓
Week 2: 注册 Adapter 为 OpenViking VectorDB 后端
       ↓
Week 2: 端到端验证: write → L0/L1/L2 → RuVector 存储 → retrieve → 强化排序结果
       ↓
Week 3-4: 实现 Feedback Analyzer + update_reward 闭环
       ↓
Week 5+: 评估 PyO3 升级时机
```

---

## 附录 A：与 MemCortex Phase 1 的对比

OpenCortex 替换 MemCortex Phase 1 管道（brainstorming 约束 #4），以下是对照：

| MemCortex Phase 1 组件 | 状态 | OpenCortex 替代 |
|------------------------|------|-----------------|
| `spool.py` (SQLite 队列) | **废弃** | Memory Orchestrator 直接管理 |
| `engine.py` (flush 引擎) | **废弃** | Orchestrator 写入流程 |
| `sync_client.py` (MCP 同步) | **废弃** | MCP Server (独立模块) |
| `vector_store.py` (LanceDB/SQLite) | **废弃** | RuVector Adapter |
| `models.py` (MemoryEvent) | **演进** | 保留核心字段，扩展 SONA 属性 |
| `embeddings.py` | **保留** | 继续用于向量化 |
| `config.py` | **演进** | 增加 RuVector 配置 |

---

## 附录 B：配置参考

### OpenCortex RuVector 配置建议

```yaml
# config.yaml
ruvector:
  # 集成模式: subprocess | http | pyo3
  integration_mode: subprocess

  # rvf-cli 路径（subprocess 模式）
  cli_path: rvf
  cli_timeout_seconds: 30

  # 数据目录
  data_dir: ${OPENCORTEX_HOME}/ruvector_data

  # 向量参数
  dimension: 1536          # 与 embedding 模型匹配
  distance_metric: cosine

  # HNSW 参数
  hnsw_m: 16
  hnsw_ef_construction: 200
  hnsw_ef_search: 50

  # SONA 参数
  enable_sona: true
  sona_decay_rate: 0.95    # 普通记忆
  sona_protected_decay_rate: 0.99  # 受保护记忆（Adapter 层实现）
  sona_min_score: 0.01
  sona_learning_rate: 0.1
  sona_exploration_rate: 0.1

  # HTTP 模式配置（可选）
  http_base_url: http://localhost:8080
  http_timeout_seconds: 10
```

---

*文档完成。标记为 `[需验证]` 的部分需在正式集成前通过源码阅读确认。*
