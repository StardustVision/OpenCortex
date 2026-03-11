// HTTP client using native fetch (Node.js >= 18)
import { getMcpConfig, getProjectId } from './common.mjs';

/**
 * Build per-request HTTP headers from MCP config.
 * Uses JWT Bearer token for authentication (identity extracted from claims).
 * Retains X-Project-ID (project dimension is not in the JWT).
 */
export function buildClientHeaders() {
  const hdrs = {};
  const token = getMcpConfig('token', '');
  if (token) {
    hdrs['Authorization'] = `Bearer ${token}`;
  }
  hdrs['X-Project-ID'] = getProjectId();
  return hdrs;
}

export async function httpPost(url, data, timeoutMs = 10000, extraHeaders = {}) {
  const headers = { 'Content-Type': 'application/json', ...buildClientHeaders(), ...extraHeaders };
  const res = await fetch(url, {
    method: 'POST',
    headers,
    body: JSON.stringify(data),
    signal: AbortSignal.timeout(timeoutMs),
  });
  if (!res.ok) throw new Error(`POST ${url} → ${res.status}`);
  return res.json();
}

export async function httpGet(url, timeoutMs = 5000, extraHeaders = {}) {
  const headers = { ...buildClientHeaders(), ...extraHeaders };
  const res = await fetch(url, {
    headers,
    signal: AbortSignal.timeout(timeoutMs),
  });
  if (!res.ok) throw new Error(`GET ${url} → ${res.status}`);
  return res.json();
}

/**
 * Batch message recording for Observer debounce buffer.
 * @param {string} httpUrl - Server base URL
 * @param {string} sessionId - Session identifier
 * @param {Array<{role: string, content: string}>} messages - Messages to record
 */
export async function sessionMessagesBatch(httpUrl, sessionId, messages, timeoutMs = 5000) {
  return httpPost(`${httpUrl}/api/v1/session/messages`, {
    session_id: sessionId,
    messages,
  }, timeoutMs);
}

export async function healthCheck(httpUrl, timeoutMs = 3000) {
  try {
    await fetch(`${httpUrl}/api/v1/memory/health`, {
      signal: AbortSignal.timeout(timeoutMs),
    });
    return true;
  } catch {
    return false;
  }
}
