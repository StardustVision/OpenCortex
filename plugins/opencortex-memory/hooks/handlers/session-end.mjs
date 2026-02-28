import { loadState, saveState } from '../../lib/common.mjs';
import { httpPost } from '../../lib/http-client.mjs';

export default async function sessionEnd(ctx) {
  const state = loadState();
  if (!state) return {};

  // Post session summary (best-effort)
  if (state.active && (state.ingested_turns || 0) > 0) {
    try {
      await httpPost(`${state.http_url}/api/v1/memory/store`, {
        abstract: `Session summary: ${state.ingested_turns} turns`,
        content: `Session with ${state.ingested_turns} turns from ${state.started_at || 'unknown'} to ${Math.floor(Date.now() / 1000)}.`,
        category: 'session_summary',
        context_type: 'memory',
        meta: {
          source: 'hook:session-end',
          ingested_turns: state.ingested_turns,
          started_at: state.started_at,
          ended_at: Math.floor(Date.now() / 1000),
        },
      }, 10000);
    } catch {
      // best-effort
    }
  }

  // Kill local HTTP server (MCP is managed by Claude Code)
  if (state.mode === 'local' && state.http_pid > 0) {
    try { process.kill(state.http_pid, 'SIGTERM'); } catch { /* already dead */ }
  }

  // Mark inactive
  state.active = false;
  state.ended_at = Math.floor(Date.now() / 1000);
  saveState(state);

  const turns = state.ingested_turns || 0;
  return {
    systemMessage: `[opencortex-memory] session ended — turns=${turns}`,
  };
}
