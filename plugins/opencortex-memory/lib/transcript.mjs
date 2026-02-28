import { readFileSync } from 'node:fs';

// ── JSONL reader ───────────────────────────────────────────────────────
export function readJsonl(path) {
  const lines = readFileSync(path, 'utf-8').split('\n');
  const rows = [];
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    try { rows.push(JSON.parse(trimmed)); } catch { /* skip malformed */ }
  }
  return rows;
}

// ── text extraction helpers ────────────────────────────────────────────
function extractTextParts(message) {
  if (!message) return '';
  if (typeof message.content === 'string') return message.content;
  if (!Array.isArray(message.content)) return '';
  return message.content
    .filter(b => b.type === 'text')
    .map(b => b.text || '')
    .join('\n');
}

function extractToolUses(message) {
  if (!message || !Array.isArray(message.content)) return [];
  return message.content
    .filter(b => b.type === 'tool_use')
    .map(b => {
      const name = b.name || 'unknown';
      const input = b.input ? short(JSON.stringify(b.input), 120) : '';
      return `[tool-use] ${name}(${input})`;
    });
}

function isToolResult(message) {
  if (!message || !Array.isArray(message.content)) return false;
  return message.content.some(b => b.type === 'tool_result');
}

function short(s, maxLen) {
  if (!s) return '';
  return s.length <= maxLen ? s : s.slice(0, maxLen) + '...';
}

// ── extract last turn ──────────────────────────────────────────────────
export function extractLastTurn(transcriptPath) {
  const rows = readJsonl(transcriptPath);
  if (!rows.length) return null;

  // Find the last user message that is NOT a tool_result
  let lastUserIdx = -1;
  for (let i = rows.length - 1; i >= 0; i--) {
    const msg = rows[i];
    if (msg.role === 'user' && !isToolResult(msg)) {
      const text = extractTextParts(msg);
      if (text.trim()) {
        lastUserIdx = i;
        break;
      }
    }
  }
  if (lastUserIdx < 0) return null;

  const userMsg = rows[lastUserIdx];
  const userText = extractTextParts(userMsg);
  const turnUuid = userMsg.uuid || userMsg.id || `turn-${lastUserIdx}`;

  // Collect assistant chunks after the user message
  const assistantParts = [];
  const toolUses = [];
  for (let i = lastUserIdx + 1; i < rows.length; i++) {
    const msg = rows[i];
    if (msg.role === 'user') break; // next user turn
    if (msg.role === 'assistant') {
      const text = extractTextParts(msg);
      if (text) assistantParts.push(text);
      toolUses.push(...extractToolUses(msg));
    }
  }

  const assistantText = assistantParts.join('\n');
  return { turnUuid, userText, assistantText, toolUses };
}

// ── summarize turn (no LLM) ───────────────────────────────────────────
export function summarizeTurn(turn) {
  if (!turn) return '';
  const lines = [];

  // User prompt (truncated)
  if (turn.userText) {
    lines.push(`User: ${short(turn.userText, 200)}`);
  }

  // Tool uses as bullet points
  if (turn.toolUses && turn.toolUses.length) {
    lines.push('Actions:');
    for (const tu of turn.toolUses.slice(0, 8)) {
      lines.push(`  - ${tu}`);
    }
    if (turn.toolUses.length > 8) {
      lines.push(`  - ... and ${turn.toolUses.length - 8} more`);
    }
  }

  // Assistant excerpt
  if (turn.assistantText) {
    lines.push(`Assistant: ${short(turn.assistantText, 300)}`);
  }

  return lines.join('\n');
}
