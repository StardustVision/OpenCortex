# CE Analysis Report: PersonaMem 2061 QA (context_recall)

**Date**: 2026-04-12  
**Model**: GPT-4o-mini  
**Retrieve Method**: `context_recall` (production path)  
**Report JSON**: `memory-eval_memory_3fff043d.json`

---

## 1. Executive Summary

This run is **close to its own full-context baseline on J-Score**, even though retrieval quality is weak.

| Metric | OpenCortex | In-Run Full-Context Baseline | Delta |
|--------|-----------|------------------------------|-------|
| **J-Score (Overall)** | **0.604** | 0.638 | -0.034 |
| **F1 (Overall)** | **0.050** | 0.049 | +0.001 |
| Token Reduction | 99.2% (254 vs 32,073 avg) | — | — |
| Recall Latency p50 | 13.2s | — | — |

Headline result:

- **As a judged answer benchmark, this run is competitive.**
- **As a retrieval benchmark, this run is weak.**

Those two statements are both true because PersonaMem is unusually tolerant of generic answers:

| Slice | J-Score |
|-------|---------|
| Zero retrieval | 0.615 |
| Non-zero retrieval | 0.589 |

So the current run's strong overall J-Score does **not** mean retrieval is healthy. It means GPT-4o-mini plus the PersonaMem judging setup can often answer acceptably without stored memory.

---

## 2. Method and Scope Notes

### 2.1 Source of truth

This report is derived from:

- `docs/benchmark/memory-eval_memory_3fff043d.json`
- `benchmarks/datasets/personamem/data.json`
- benchmark adapter behavior in `benchmarks/adapters/memory.py`
- current production recall path in `src/opencortex/context/manager.py`, `src/opencortex/orchestrator.py`, and `src/opencortex/retrieve/intent_router.py`

### 2.2 Run provenance

This report is anchored to the latest full recall run recorded in `benchmarks/full_eval.log`:

- `benchmarks/full_eval.log:157` -> `Mode: memory | Run ID: eval_memory_3fff043d`
- `benchmarks/full_eval.log:301` -> `Report saved to docs/benchmark/memory-eval_memory_3fff043d.json`

Older PersonaMem runs exist in the repo but are **not** the basis of this report, including:

- `docs/benchmark/full-memory-20260324.log:1` -> `eval_memory_6bd9b4cc`
- `docs/benchmark/benchmark-report-2026-03-25.md` -> older full-memory summary built on earlier runs

### 2.3 Primary interpretation rule

For PersonaMem, **J-Score is the primary metric**. F1 remains near zero for both OpenCortex and baseline because the answers are long-form, open-ended, and often lexically diverse.

### 2.4 `ask_to_forget` is special by design

The benchmark adapter explicitly simulates forgetting:

- it stores `ask_to_forget` attributes
- immediately deletes them
- clears `expected_uris` for that category

Relevant code:

- `benchmarks/adapters/memory.py:45-47`
- `benchmarks/adapters/memory.py:76-79`
- `benchmarks/adapters/memory.py:120-123`

This means:

- zero retrieval for `ask_to_forget` is **often expected**
- retrieval metrics skip those 420 items entirely
- `retrieval.skipped_no_ground_truth = 420` in the JSON exactly matches the size of the `ask_to_forget` category

So this category should be read as a **privacy / forgetting behavior** test, not a normal retrieval-recall test.

---

## 3. Comparison to Baseline

### 3.1 Overall

| Metric | OpenCortex | Baseline | Delta |
|--------|-----------|----------|-------|
| J-Score | 0.6036 | 0.6380 | -0.0344 |
| F1 | 0.0499 | 0.0486 | +0.0013 |
| BLEU-1 | 0.0008 | 0.0009 | -0.0001 |

### 3.2 Per-category

| Category | N | OC J | BL J | J Delta | OC F1 | BL F1 | Zero-Retrieval |
|----------|---|------|------|---------|-------|-------|----------------|
| anti_stereotypical_pref | 375 | 0.491 | 0.512 | -0.021 | 0.049 | 0.049 | 61.3% |
| ask_to_forget | 420 | 0.636 | 0.679 | -0.043 | 0.050 | 0.045 | 54.8% |
| health_and_medical_conditions | 249 | 0.711 | 0.791 | -0.080 | 0.063 | 0.064 | 74.3% |
| neutral_preferences | 355 | 0.583 | 0.620 | -0.037 | 0.042 | 0.044 | 71.3% |
| sensitive_info | 184 | 0.603 | 0.609 | -0.006 | 0.077 | 0.073 | 25.5% |
| stereotypical_pref | 224 | 0.487 | 0.487 | 0.000 | 0.050 | 0.043 | 69.2% |
| therapy_background | 254 | 0.744 | 0.787 | -0.043 | 0.029 | 0.032 | 28.7% |

### 3.3 Retrieval quality

The retrieval score is much weaker than the headline J-Score:

| Retrieval Metric | Overall |
|------------------|---------|
| Recall@1 | 0.101 |
| Recall@3 | 0.150 |
| Recall@5 | 0.177 |
| Precision@1 | 0.101 |
| Hit rate@5 | 0.177 |
| MRR | 0.138 |
| nDCG@5 | 0.140 |
| Evaluated count | 1641 |
| Skipped (no ground truth) | 420 |

The high-level read is simple:

- judged answers are decent
- retrieval recall is not

---

## 4. Comparison to Competitors

### 4.1 Repo-tracked external scores

The repo currently tracks the following PersonaMem external scores in internal benchmark notes. Metric naming varies by source, so treat this table as **directional positioning**, not a perfectly apples-to-apples league table.

| System | Repo-tracked score | Gap vs OpenCortex |
|--------|--------------------|-------------------|
| DeltaMem-8B-RL | 63.61% | +3.25 pts |
| **OpenCortex (this run)** | **60.36% J-Score** | — |
| DeltaMem-4o-mini | 59.71% | -0.65 pts |
| Memobase | 58.89% | -1.47 pts |
| Zep | 56.71% | -3.65 pts |
| Supermemory | 53.88% | -6.48 pts |
| Mem0 | 43.12% | -17.24 pts |

### 4.2 Interpretation

- OpenCortex is **close to the top tier** on this dataset.
- It trails the strongest repo-tracked system (`DeltaMem-8B-RL`) by only **3.25 points**.
- It slightly exceeds the repo-tracked `DeltaMem-4o-mini` score.
- It is clearly ahead of Zep, Supermemory, and Mem0 on the currently tracked numbers.

Important caveat:

- this does **not** mean OpenCortex retrieval is stronger than those systems
- it means the overall answer pipeline scores well on this benchmark's judge

---

## 5. Strengths

### S1. Near-baseline judged quality at extreme compression

- J-Score retention: **94.6%** of full-context baseline (`0.6036 / 0.6380`)
- token reduction: **99.2%**
- 254 tokens/query vs 32,073 baseline

This is the strongest single number in the report.

### S2. Strong external positioning on the repo-tracked leaderboard

Directional comparison only, but still useful:

- above DeltaMem-4o-mini, Memobase, Zep, Supermemory, and Mem0 in the repo-tracked table
- only clearly behind DeltaMem-8B-RL

### S3. Therapy and sensitive information are the best categories

| Category | J-Score | Retrieval evidence overlap |
|----------|---------|----------------------------|
| therapy_background | 0.744 | 43.3% |
| sensitive_info | 0.603 | 63.6% |

These are the categories where the stored memory is most concrete and easiest to align with the query.

### S4. `ask_to_forget` behavior is implemented, not missing

The benchmark harness actually deletes `ask_to_forget` memories during ingest. That means:

- this run does test forgetting behavior
- the current codebase already supports the forget path used by the benchmark

### S5. Zero retrieval does not cause immediate answer collapse

Zero retrieval is bad for recall, but it does not tank J-Score on this benchmark:

- zero retrieval J: **0.615**
- non-zero retrieval J: **0.589**

This shows the answer model can often produce privacy-safe or preference-generic answers without memory.

---

## 6. Weaknesses

### W1. Retrieval quality is weak in absolute terms

- Recall@5 is only **0.177**
- evidence overlap is only **22.3%** overall
- zero-retrieval is **56.9%**

So the memory system is not being used effectively, even though the judged answers still look decent.

### W2. Preference categories are the main retrieval problem

| Category | Zero-Retrieval | Evidence overlap |
|----------|----------------|------------------|
| health_and_medical_conditions | 74.3% | 5.6% |
| neutral_preferences | 71.3% | 21.7% |
| stereotypical_pref | 69.2% | 19.6% |
| anti_stereotypical_pref | 61.3% | 25.9% |

This is the exact pattern expected when the stored memory is short preference statements and the query is a more natural-language paraphrase.

### W3. The run's good J-Score can hide weak memory use

On 2061 questions:

- zero retrieval + judge success: **721**
- non-overlap retrieval + judge success: **240**
- exact evidence retrieval + judge success: **283**

In other words, many "wins" are not strong retrieval wins. They are often:

- generic advice accepted by the judge
- privacy-safe fallback answers
- answers generated from model priors rather than recalled memory

### W4. Latency is still far above what the current timeout policy can tolerate

| Percentile | Latency |
|------------|---------|
| p50 | 13,233 ms |
| p95 | 28,232 ms |
| p99 | 36,244 ms |
| max | 63,036 ms |

The production recall path is still slow enough to collide with the 10s planning timeout.

### W5. `l0` context limits answer richness

The benchmark forces `context_recall(..., detail_level="l0")`, so the answer model only receives abstract-level snippets. This is especially limiting for personalized, open-ended PersonaMem answers.

---

## 7. Failure Mode Breakdown

### 7.1 Phase-1 router follow-up

Subsequent code analysis against the current Phase-1 router found that the main routing problem is **not** false `should_recall=false` decisions. The larger issue is **`task_class` over-classification** on long, polite, or technical prompts:

- polite phrases containing `like` were being over-read as `profile`
- technical prompts containing `review` were being over-read as `summarize`
- incidental `all`, `across`, or `compare` wording could over-trigger `aggregate`

This matters because router `task_class` feeds planner depth and breadth. A wrong class does not only label the query incorrectly; it also pushes retrieval into the wrong search posture.

The Phase-1 rewrite therefore targets:

- keeping `should_recall=false` extremely narrow
- replacing bare substring precedence with a conservative scoring classifier
- falling back to `task_class=fact` on ambiguity rather than over-classifying

This follow-up does **not** change the historical results in this report. It explains one of the code-level causes behind the weak retrieval behavior observed in this run.

The most useful breakdown here is `retrieval outcome x judge outcome`:

| Bucket | Count | Share | Meaning |
|--------|-------|-------|---------|
| Zero retrieval + judge right | 721 | 35.0% | generic answer still accepted |
| Zero retrieval + judge wrong | 452 | 21.9% | blind-answer failure |
| Exact evidence retrieved + judge right | 283 | 13.7% | ideal memory-assisted path |
| Exact evidence retrieved + judge wrong | 176 | 8.5% | memory found, answer still weak |
| Non-evidence retrieved + judge right | 240 | 11.6% | model priors / partial clues |
| Non-evidence retrieved + judge wrong | 189 | 9.2% | wrong recall |

What this means:

- the report's headline J-Score is real, but
- the memory subsystem is contributing less than the headline suggests

For a memory product, this matters. A high judged answer score is useful, but it is not the same thing as a strong memory retrieval result.

---

## 8. Code-Level Attribution

### A1. The same timeout-to-no-recall failure mode exists here too

Flow:

```text
context_recall
  -> ContextManager._prepare()
  -> plan_recall() wrapped in 10s timeout
  -> timeout/error keeps fallback plan
  -> fallback plan in auto mode sets should_recall = False
  -> memory search skipped
```

Relevant code:

- `src/opencortex/context/manager.py:202-233`
- `src/opencortex/context/manager.py:297-335`
- `src/opencortex/cognition/recall_planner.py:29-54`

Why it matters here:

- p50 latency is **13.2s**
- planning timeout is **10.0s**
- zero-retrieval is **56.9%**

This is an inference from code + metrics, but again a strong one: timeout-induced skip is likely a major reason retrieval is so weak on this run.

### A2. PersonaMem stores raw attribute text with no retrieval-oriented expansion

Attributes are stored as:

- `abstract = attr_text`
- `content = attr_text`
- no category-specific `embed_text`
- no paraphrase expansion
- no explicit facet tagging

Relevant code:

- `benchmarks/adapters/memory.py:67-71`
- `benchmarks/adapters/memory.py:137-141`

This is the concrete reason preference-style query mismatch is so severe. The index sees short raw statements like "I enjoy jazz", while the question is often a longer paraphrase.

### A3. `ask_to_forget` zero-retrieval is partly by design

The benchmark adapter:

- deletes `ask_to_forget` memories after storing them
- clears `expected_uris` for those questions

Relevant code:

- `benchmarks/adapters/memory.py:76-79`
- `benchmarks/adapters/memory.py:120-123`

This is why:

- the category has high zero retrieval
- retrieval metrics skip exactly 420 items

That is expected behavior, not evidence that forgetting support is absent.

### A4. The benchmark forces `detail_level="l0"` on the production recall path

Relevant code:

- `benchmarks/adapters/memory.py:147-154`
- `src/opencortex/retrieve/types.py:31-36`

`l0` is abstract-only. That keeps token cost tiny, but it also caps how much personalization the answer model can express from recalled memory.

### A5. LLM planning is still on the critical path

`IntentRouter.route()` runs LLM classification whenever `session_context` is provided:

- `src/opencortex/retrieve/intent_router.py:136-153`

And the benchmark's `context_recall` path always supplies `session_context` through `ContextManager._prepare()`:

- `src/opencortex/context/manager.py:307-323`

This means a benchmark question like "What kind of music would I enjoy during long train rides?" still pays planning cost before any recall happens.

### A6. Hybrid retrieval exists, but the current path still under-serves lexical preference queries

OpenCortex already has dense+sparse hybrid retrieval and dynamic lexical weighting:

- `src/opencortex/orchestrator.py:2049-2063`
- `src/opencortex/orchestrator.py:724-736`

So the right conclusion is **not** "add BM25 from scratch". The right conclusion is:

- the current lexical weighting and query classification are still insufficient for preference paraphrases
- especially in categories where the attribute text is short and semantically sparse

### A7. Search errors can silently look like misses

Like the LoCoMo path, memory search exceptions are swallowed into empty results:

- `src/opencortex/context/manager.py:351-380`

This again is probably secondary, but it weakens observability and can inflate apparent retrieval misses.

---

## 9. Optimization Roadmap

| Priority | Action | Why it matters | Expected effect |
|----------|--------|----------------|-----------------|
| **P0** | Make planner timeout fail open for recall | Current timeout path can convert slow planning into zero retrieval | Directly reduces false zero-recall |
| **P0** | Add no-LLM fast path for straightforward preference / profile questions | Many PersonaMem prompts do not need expensive intent planning | Cuts latency and timeout pressure |
| **P1** | Add attribute expansion during ingest (`likes`, `prefers`, `enjoys`, category tags, synonyms) | Raw attribute text is too sparse for paraphrase-heavy queries | Better preference-category recall |
| **P1** | Increase lexical weight for preference and health queries | Hybrid search already exists; preference matching needs stronger lexical help | Higher Recall@5 in weak categories |
| **P1** | Promote answer context from `l0` to adaptive `l1` on personalized questions | Current memory snippets are too thin for nuanced answers | Better memory-conditioned personalization |
| **P2** | Split privacy benchmark reporting from retrieval benchmark reporting | `ask_to_forget` is behaviorally special and distorts aggregate interpretation | Cleaner metric reading |
| **P2** | Emit explicit recall-skip reasons and timeout counters | Today skip causes must be inferred | Faster diagnosis and tuning |
| **P2** | Stop swallowing retrieval exceptions into empty lists | Miss vs infra failure is currently blurred | Better ops and cleaner benchmark analysis |

### Recommended implementation order

1. **Planner timeout policy**
2. **No-LLM fast path**
3. **Preference-oriented attribute expansion**
4. **Lexical weight tuning**
5. **Adaptive `l1` answer context**

---

## 10. Bottom Line

This PersonaMem run is strong if the question is:

> "Can OpenCortex produce benchmark answers that the judge often accepts while using almost no tokens?"

Answer: **yes**.

It is weaker if the question is:

> "Is the memory retrieval layer itself healthy and doing most of the work?"

Answer: **not yet**.

The key read is:

- **overall benchmark quality is good**
- **retrieval quality is not**
- **the benchmark harness already supports forgetting**
- **the main technical debt is still in planning latency, timeout policy, and preference-query matching**

So the next goal is not to chase another small J-Score gain. It is to make the memory subsystem earn more of the score instead of letting the answer model carry it.

---

*Report regenerated from benchmark JSON + current code paths. Primary sources: `memory-eval_memory_3fff043d.json`, `benchmarks/adapters/memory.py`, and current retrieval implementation.*
