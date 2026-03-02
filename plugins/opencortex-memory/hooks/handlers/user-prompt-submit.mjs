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
  const source = ctx.mode === 'remote' ? `remote(${httpUrl})` : 'local';
  const headers = {};
  if (state.tenant_id) headers['X-Tenant-ID'] = state.tenant_id;
  if (state.user_id) headers['X-User-ID'] = state.user_id;

  // Proactive memory recall — 3s timeout
  let result;
  try {
    result = await httpPost(
      `${httpUrl}/api/v1/memory/search`,
      { query: prompt, limit: 3, detail_level: 'l1' },
      3000,
      headers,
    );
  } catch (err) {
    const reason = err.name === 'AbortError' ? 'timeout (3s)' : err.message;
    return {
      systemMessage: `[opencortex-memory:${source}] Recall failed: ${reason}`,
    };
  }

  const allItems = result?.results || [];
  if (allItems.length === 0) {
    return { systemMessage: `[opencortex-memory:${source}] No memories matched.` };
  }

  const items = allItems.filter(r => r.score > 0.5);
  if (items.length === 0) {
    const best = Math.max(...allItems.map(r => r.score ?? 0)).toFixed(2);
    return {
      systemMessage: `[opencortex-memory:${source}] ${allItems.length} found, all scored ≤ 0.5 (best: ${best}).`,
    };
  }

  const lines = items.map(r => {
    const tag = r.context_type ? `[${r.context_type}]` : '';
    const score = r.score != null ? `(${r.score.toFixed(2)})` : '';
    const text = r.abstract || r.overview || '';
    return `- ${tag}${score} ${text}`;
  });
  return {
    systemMessage: `[opencortex-memory:${source}] Recalled:\n${lines.join('\n')}`,
  };
}
