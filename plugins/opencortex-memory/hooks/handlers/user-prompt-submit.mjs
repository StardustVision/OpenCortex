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

  // Record prompt in Observer (best-effort, non-blocking)
  if (state.session_id) {
    httpPost(`${httpUrl}/api/v1/session/message`, {
      session_id: state.session_id,
      role: 'user',
      content: prompt.slice(0, 2000),
    }, 3000).catch(() => {});
  }

  // Ask server IntentRouter: keyword (< 1ms) + LLM classification (200ms-1s)
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
    // Server unreachable or timeout — fall through, let model decide
  }

  // Inject concise instruction for model to call MCP recall first
  return {
    systemMessage: '[opencortex-memory] Call `memory_search` with the user\'s query before other actions.',
  };
}
