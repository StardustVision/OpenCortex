import { loadState, saveState } from '../../lib/common.mjs';
import { httpPost } from '../../lib/http-client.mjs';

export default async function sessionEnd(ctx) {
  const state = loadState();
  if (!state) return {};

  let extractionMsg = '';

  // Trigger full session extraction via SessionManager
  if (state.active && state.session_id) {
    try {
      const result = await httpPost(`${state.http_url}/api/v1/session/end`, {
        session_id: state.session_id,
        quality_score: 0.5,
      }, 30000);  // LLM analysis needs time

      if (result && (result.stored_count > 0 || result.merged_count > 0)) {
        extractionMsg = ` extraction: stored=${result.stored_count} merged=${result.merged_count} skipped=${result.skipped_count}`;
      }
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
    systemMessage: `[opencortex-memory] session ended — turns=${turns}${extractionMsg}`,
  };
}
