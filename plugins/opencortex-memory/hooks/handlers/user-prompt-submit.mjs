import { loadState } from '../../lib/common.mjs';
import { httpPost } from '../../lib/http-client.mjs';

export default async function userPromptSubmit(ctx) {
  const { input } = ctx;
  const prompt = input?.prompt;
  if (!prompt) return {};

  if (!ctx.configPath) return {};

  const state = loadState();
  if (!state || state.active !== true) return {};

  const httpUrl = state.http_url || ctx.httpUrl;
  const headers = {};
  if (state.tenant_id) headers['X-Tenant-ID'] = state.tenant_id;
  if (state.user_id) headers['X-User-ID'] = state.user_id;

  // Proactive memory recall — 3s timeout, fail silently
  try {
    const result = await httpPost(
      `${httpUrl}/api/v1/memory/search`,
      { query: prompt, limit: 3, detail_level: 'l1' },
      3000,
      headers,
    );

    const items = (result?.results || []).filter(r => r.score > 0.5);
    if (items.length > 0) {
      const lines = items.map(r => {
        const tag = r.context_type ? `[${r.context_type}]` : '';
        const score = r.score != null ? `(${r.score.toFixed(2)})` : '';
        const text = r.abstract || r.overview || '';
        return `- ${tag}${score} ${text}`;
      });
      return {
        systemMessage: `[opencortex-memory] Recalled memories for this prompt:\n${lines.join('\n')}\n\nUse memory_feedback(uri, reward) to reinforce useful memories.`,
      };
    }
  } catch {
    // Search failed or timed out — fall through to default hint
  }

  return {
    systemMessage: '[opencortex-memory] Memory system active. Use memory_search MCP tool if past context would help.',
  };
}
