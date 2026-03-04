# Skill Evolution Protocol

## Before Task Execution

Use `skill_lookup` to find relevant learned skills:

```
skill_lookup({ objective: "<what you are trying to accomplish>" })
```

If relevant skills are returned, follow their `action_template` steps and respect their `trigger_conditions`.

## After Task Completion

Report outcome via `skill_feedback`:

```
skill_feedback({
  uri: "<skill URI>",
  success: true/false,
  score: 0.0-1.0
})
```

This updates the skill's confidence score through reinforcement learning. Skills with consistently positive feedback rise in ranking; poor-performing skills get evolved or deprecated automatically.

## Lifecycle

1. **RuleExtractor** automatically extracts skills from stored memories
2. **skill_lookup** retrieves relevant skills before task execution
3. **skill_feedback** records success/failure after task execution
4. **skill_mine** clusters successful cases into new skill templates (LLM-assisted)
5. **skill_evolve** replaces underperforming skills via dual-track observation
