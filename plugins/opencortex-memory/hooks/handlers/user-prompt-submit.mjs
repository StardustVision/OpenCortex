import { loadState, getConfigPath } from '../../lib/common.mjs';

export default async function userPromptSubmit(ctx) {
  const { input } = ctx;
  const prompt = input?.prompt;
  if (!prompt) return {};

  if (!ctx.configPath) return {};

  const state = loadState();
  if (!state || state.active !== true) return {};

  return {
    systemMessage: '[opencortex-memory] Memory system active. If this query could benefit from past context, preferences, or learned patterns, use the memory_search MCP tool.',
  };
}
