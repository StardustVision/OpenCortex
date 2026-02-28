# Memory System Test Plan

## 1. Goal

Validate the memory system from three angles:

1. Correctness: store/search/feedback/decay/session flows are stable.
2. Quality: retrieval returns the right memory for user queries.
3. Value: warmed memory improves task success and reduces interaction cost.

## 2. Scope

In scope:

- Core memory APIs (store/search/feedback/decay/health/stats)
- Session extraction pipeline (begin/message/end)
- Retrieval quality with labeled query set
- Online A/B comparison: cold-start vs warmed memory
- Performance baseline (latency and throughput)

Out of scope:

- UI experience and frontend styling
- Multi-region deployment behavior
- Non-memory business modules

## 3. Test Environment

- Local runtime from this repository.
- Python test command:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -p "test_*.py" -v
```

- Optional live tests when services are running:
- HTTP server: `http://127.0.0.1:8921`
- MCP server: `http://127.0.0.1:8920/mcp`

## 4. Success Criteria

Functional pass criteria:

1. No critical test failures in memory-related suites.
2. No data corruption in CRUD + session lifecycle.

Quality pass criteria:

1. Recall@5 >= 0.80
2. MRR >= 0.60
3. Precision@5 >= 0.50

Online value criteria (warmed vs cold):

1. Task success rate +10% or more
2. Average turns -15% or more
3. P95 response latency does not regress by >10%

## 5. Test Data Design

Build a labeled dataset with 50-200 records:

- `query`: natural language user request
- `expected_uris`: one or more ground-truth memory URIs
- `category`: preferences/patterns/entities/errors/skills
- `difficulty`: easy/medium/hard
- `tokens_with_memory` (optional): actual tokens consumed with memory enabled
- `tokens_without_memory` (optional): actual tokens consumed without memory

Recommended split:

1. 60% straightforward fact recall
2. 30% paraphrased or implicit intent queries
3. 10% distractor queries (should return none or low confidence)

## 6. Test Phases

### Phase A: Functional Regression

Run and gate:

1. `tests/test_qdrant_adapter.py`
2. `tests/test_e2e_phase1.py`
3. `tests/test_http_server.py`
4. `tests/test_mcp_server.py`
5. `tests/test_rl_integration.py` (if env config exists)

Output:

- Pass/fail summary
- Failed case root cause list

### Phase B: Retrieval Quality Evaluation

Procedure:

1. Import/seed labeled memories.
2. Run each query through `memory_search`.
3. Compute Recall@k, MRR, Precision@k.
4. Break down by category and difficulty.

Command example:

```bash
PYTHONPATH=src python3 scripts/eval_memory.py \
  --dataset data/memory_eval_dataset.json \
  --base-url http://127.0.0.1:8921 \
  --k 1,3,5 \
  --output _bmad-output/memory-eval-report.json
```

Output:

- Metric table by overall/category/difficulty
- Top false-positive and false-negative examples

### Phase C: Online Effect Evaluation (A/B)

Groups:

1. Control: cold-start memory store (empty or minimal)
2. Experiment: warmed memory store (historical memory loaded)

Run for at least 7 days with the same task mix.

Track:

1. Task success rate
2. Average turns per task
3. P50/P95 latency
4. Error rate and retry rate

Output:

- Daily trend charts
- Final A/B significance summary

### Phase D: Performance Baseline

Load profile:

1. Search QPS sweep (low/medium/high)
2. Mixed traffic (store:search:feedback = 1:8:1)

Metrics:

1. P50/P95/P99 latency
2. Error rate
3. CPU/memory usage

Output:

- Capacity baseline and bottleneck notes

## 7. Test Case Matrix (Minimum)

1. Store then search exact match.
2. Store then search paraphrase query.
3. Feedback positive increases ranking stability.
4. Feedback negative suppresses irrelevant item.
5. Decay lowers stale memory score over time.
6. Session end triggers extraction and persists useful memory.
7. Duplicate memory merge behavior is correct.
8. Category filter returns scoped results only.
9. Context type filter (memory/resource/skill) works.
10. Empty/no-recall query does not return noisy history.

## 8. Risks and Controls

Risk:

1. Ground-truth labels are noisy.
2. Traffic mix differs between control and experiment.
3. Environment instability skews latency numbers.

Control:

1. Label review by two people for hard cases.
2. Keep identical query pool for both groups.
3. Run fixed-time windows and record infra status.

## 9. Reporting Template

Weekly report should include:

1. Functional status: pass rate and blockers
2. Quality metrics: Recall@k/MRR/Precision@k with delta vs last run
3. Online metrics: success/turns/latency/error with A/B delta
4. Top 5 failure cases and fix plan

## 10. One-Week Execution Plan

1. Day 1: Prepare dataset and run Phase A baseline.
2. Day 2: Run Phase B and publish first quality report.
3. Day 3-6: Run Phase C online A/B and collect daily metrics.
4. Day 7: Run Phase D, summarize results, decide go/no-go.
