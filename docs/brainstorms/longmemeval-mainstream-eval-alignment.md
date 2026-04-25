---
date: 2026-04-25
topic: longmemeval-mainstream-eval-alignment
---

# LongMemEval Mainstream Evaluation Alignment

## Problem Frame
OpenCortex currently can run LongMemEval through the generic conversation benchmark path, but that path is not aligned with how mainstream memory frameworks report LongMemEval. The current path tends to treat each item as a very large recomposed conversation and uses low-k recall-oriented retrieval, which makes runs slow and makes results hard to compare with Mem0, Zep, Supermemory, EmergenceMem, and related reports.

The benchmark should measure OpenCortex against the same practical task those frameworks measure: ingest a question-specific haystack, retrieve enough relevant memory for the question, answer with an LLM, and judge answer correctness.

## Requirements

**Dataset and Isolation**
- R1. Each LongMemEval question must be evaluated in an isolated memory scope so cross-question leakage cannot improve or harm results.
- R2. The ingest input should follow the LongMemEval haystack shape: sessions made of user/assistant rounds, with session dates preserved as metadata.
- R3. The default LongMemEval mode should avoid whole-haystack full recomposition as the primary ingest behavior, because mainstream comparisons ingest searchable session/round evidence rather than one giant conversation summary.

**Retrieval and Answering**
- R4. The primary retrieval path should be search-style retrieval, not low-k session-scoped recall, because mainstream LongMemEval reports retrieve top-k evidence and then answer.
- R5. The default top-k for mainstream-aligned LongMemEval should support high cutoffs such as 20, 50, and 200, with 200 available for Mem0-style comparison.
- R6. The benchmark should pass retrieved evidence into the answer model and score the generated answer, rather than treating URI recall as the primary result.

**Metrics and Reporting**
- R7. The primary metric should be LongMemEval-style answer accuracy using an LLM judge with type-aware prompts.
- R8. Retrieval metrics should be diagnostic and should include high-k cutoffs where ground-truth answer sessions or evidence are available.
- R9. Reports should clearly label the run as mainstream-aligned LongMemEval and record ingest shape, retrieval method, top-k/cutoffs, judge model, answer model, and whether the run is full or sampled.

**Run Modes**
- R10. Provide a fast smoke/sample mode comparable to public reports that sample a small number of questions per type.
- R11. Provide a full mode over the complete configured LongMemEval file, but make it explicit that runtime is expected to be much higher than LoCoMo.
- R12. Existing generic conversation benchmark behavior should remain available for OpenCortex-internal recall experiments, but it must not be confused with mainstream LongMemEval comparison numbers.

## Success Criteria
- A one-conversation or small-sample LongMemEval run completes quickly enough to validate the path before full runs.
- A full LongMemEval run produces an answer-accuracy report with comparable metadata instead of spending many hours in full recomposition ingest.
- The report makes clear whether it is comparable to Mem0/Zep/Supermemory-style LongMemEval or only an internal OpenCortex recall experiment.
- Retrieval diagnostics can explain misses without being presented as the headline benchmark score.

## Scope Boundaries
- This alignment does not require copying any one vendor's private implementation.
- This alignment does not replace LoCoMo scoring or OpenCortex recall benchmarks.
- This alignment does not make URI Recall@5 the headline LongMemEval metric.
- This alignment does not require a new production memory API if the existing benchmark-only path can safely express the needed ingest shape.

## Key Decisions
- Use mainstream answer accuracy as the headline metric: public LongMemEval comparisons generally report whether the final answer is correct, with retrieval as a supporting diagnostic.
- Prefer pair/session evidence ingest over full conversation recomposition: this keeps the benchmark closer to Mem0-style haystack ingest and avoids pathological runtime.
- Keep high-k retrieval available: top-k 200 is important for Mem0-style retrieval sufficiency comparisons, while smaller cutoffs such as 10/20/50 help diagnose rank quality.
- Keep internal recall separate: OpenCortex recall may be valuable, but it answers a different evaluation question from mainstream LongMemEval reports.

## Dependencies / Assumptions
- The local dataset file contains `haystack_sessions`, `haystack_session_ids`, `haystack_dates`, `question`, `answer`, and question type metadata.
- The existing answer judge already has LongMemEval-aware type prompts and can be reused as the headline accuracy scorer if wired into a mainstream-aligned run mode.
- Search results can preserve enough content and metadata for answer generation and diagnostic reporting.

## Outstanding Questions

### Deferred to Planning
- [Affects R5][Technical] Decide the exact default cutoffs for aggregate retrieval diagnostics, likely `10,20,50,200`.
- [Affects R10][Technical] Decide whether sample mode should be `per_type=5`, `max_qa`, or both.
- [Affects R3][Technical] Decide whether benchmark-only ingest should write user/assistant pairs directly or use the current offline conversation ingest with recomposition disabled.

## Next Steps
-> /ce:plan for structured implementation planning.
