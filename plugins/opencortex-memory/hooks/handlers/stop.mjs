import { existsSync } from 'node:fs';
import { loadState, saveState } from '../../lib/common.mjs';
import { httpPost, sessionMessagesBatch } from '../../lib/http-client.mjs';
import { extractLastTurn, summarizeTurn } from '../../lib/transcript.mjs';

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

    const summary = summarizeTurn(turn);
    const abstract = `Session turn: ${(turn.userText || '').slice(0, 120)}`;
    const content = [
      turn.userText ? `User: ${turn.userText.slice(0, 500)}` : '',
      summary ? `Summary:\n${summary}` : '',
      turn.assistantText ? `Assistant excerpt:\n${turn.assistantText.slice(0, 500)}` : '',
    ].filter(Boolean).join('\n\n');

    await httpPost(`${state.http_url}/api/v1/memory/store`, {
      abstract,
      content,
      category: 'session',
      context_type: 'memory',
      meta: {
        turn_uuid: turn.turnUuid,
        source: 'hook:stop',
        timestamp: Math.floor(Date.now() / 1000),
      },
    }, 15000);

    // Buffer turn messages via Observer batch endpoint
    if (state.session_id) {
      try {
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

        // Extract memories from this turn (LLM, best-effort)
        await httpPost(`${state.http_url}/api/v1/session/extract_turn`, {
          session_id: state.session_id,
        }, 15000);
      } catch {
        // Best-effort — don't fail the hook
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
