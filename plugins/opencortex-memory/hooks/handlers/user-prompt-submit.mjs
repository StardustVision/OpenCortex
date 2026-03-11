import { loadState, getMcpConfig } from '../../lib/common.mjs';
import { httpPost } from '../../lib/http-client.mjs';

export default async function userPromptSubmit(ctx) {
  const { input } = ctx;
  const prompt = input?.prompt;
  if (!prompt) return {};

  if (!ctx.configPath) return {};

  const state = loadState();
  if (!state || state.active !== true) return {};

  const httpUrl = state.http_url || ctx.httpUrl;

  // Record prompt in Observer (best-effort, non-blocking)
  if (state.session_id) {
    httpPost(`${httpUrl}/api/v1/session/message`, {
      session_id: state.session_id,
      role: 'user',
      content: prompt.slice(0, 2000),
    }, 3000).catch(() => {});
  }

  // Ask server IntentRouter: should we recall?
  try {
    const intent = await httpPost(
      `${httpUrl}/api/v1/intent/should_recall`,
      { query: prompt },
      2000,
    );
    if (intent && intent.should_recall === false) {
      return {};  // no recall needed
    }
  } catch {
    // Server unreachable or timeout — fall through to search
  }

  // Perform search directly in the hook
  const recallTimeout = getMcpConfig('recall_timeout_ms', 3000);
  try {
    const searchRes = await httpPost(
      `${httpUrl}/api/v1/memory/search`,
      { query: prompt, limit: 5 },
      recallTimeout,
    );
    const results = (searchRes && searchRes.results) || [];
    if (results.length === 0) {
      return {};
    }

    // Format results for injection
    const lines = results.map((r, i) => {
      let line = `${i + 1}. [${r.context_type || 'memory'}] ${r.abstract}`;
      if (r.keywords) line += ` | keywords: ${r.keywords}`;
      if (r.score != null) line += ` (score: ${r.score.toFixed(3)})`;
      return line;
    });

    const msg = `[opencortex-memory] Recalled ${results.length} memories:\n${lines.join('\n')}\n\nUse these memories as context. If any are relevant, reference them in your response.`;

    return { systemMessage: msg };
  } catch {
    // Search failed — don't block the prompt
    return {};
  }
}
