import { loadState, saveState } from '../../lib/common.mjs';
import { httpPost } from '../../lib/http-client.mjs';

export default async function sessionEnd(ctx) {
  const state = loadState();
  if (!state) return {};

  let extractionMsg = '';

  // End session and trigger trace splitting + knowledge extraction
  if (state.active && state.session_id) {
    try {
      const result = await httpPost(`${state.http_url}/api/v1/session/end`, {
        session_id: state.session_id,
        quality_score: 0.5,
      }, 30000);  // Trace splitting + archivist needs time

      if (result && result.alpha_traces > 0) {
        extractionMsg = ` traces=${result.alpha_traces}`;
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
