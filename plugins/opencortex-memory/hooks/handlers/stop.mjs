import { existsSync } from 'node:fs';
import { loadState, saveState } from '../../lib/common.mjs';
import { sessionMessagesBatch } from '../../lib/http-client.mjs';
import { extractLastTurn } from '../../lib/transcript.mjs';

export default async function stop(ctx) {
  const { input } = ctx;

  // Guard: prevent re-entrant calls
  if (input?.stop_hook_active === 'true' || input?.stop_hook_active === true) return {};

  if (!ctx.configPath) return {};

  const state = loadState();
  if (!state || state.active !== true) return {};

  const transcriptPath = input?.transcript_path;
  if (!transcriptPath || !existsSync(transcriptPath)) return {};

  try {
    const turn = extractLastTurn(transcriptPath);
    if (!turn) return {};

    // Deduplicate
    if (turn.turnUuid && turn.turnUuid === state.last_turn_uuid) return {};

    // Record turn messages via Observer batch endpoint
    // Knowledge extraction happens later via TraceSplitter → Archivist pipeline
    if (state.session_id) {
      const messages = [];
      if (turn.userText) {
        messages.push({ role: 'user', content: turn.userText.slice(0, 2000) });
      }
      if (turn.assistantText) {
        messages.push({ role: 'assistant', content: turn.assistantText.slice(0, 2000) });
      }
      if (messages.length > 0) {
        await sessionMessagesBatch(state.http_url, state.session_id, messages, 5000);
      }
    }

    // Update state
    state.last_turn_uuid = turn.turnUuid;
    state.ingested_turns = (state.ingested_turns || 0) + 1;
    state.last_ingested_at = Math.floor(Date.now() / 1000);
    saveState(state);
  } catch {
    // Best-effort — don't fail the hook
  }

  return {};
}
