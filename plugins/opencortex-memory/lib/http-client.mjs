// HTTP client using native fetch (Node.js >= 18)
import { getMcpConfig, getProjectId } from './common.mjs';

/**
 * Build per-request HTTP headers from MCP config.
 * Includes identity (X-Tenant-ID, X-User-ID) and ACE skill sharing headers.
 */
export function buildClientHeaders() {
  const hdrs = {};
  // Identity
  const tenantId = getMcpConfig('tenant_id', 'default');
  const userId = getMcpConfig('user_id', 'default');
  if (tenantId) hdrs['X-Tenant-ID'] = tenantId;
  if (userId) hdrs['X-User-ID'] = userId;
  // Project ID
  hdrs['X-Project-ID'] = getProjectId();
  // ACE skill sharing
  const shareSkills = getMcpConfig('share_skills_to_team');
  if (shareSkills) hdrs['X-Share-Skills-To-Team'] = String(shareSkills);
  const shareMode = getMcpConfig('skill_share_mode');
  if (shareMode) hdrs['X-Skill-Share-Mode'] = shareMode;
  const shareThreshold = getMcpConfig('skill_share_score_threshold');
  if (shareThreshold != null) hdrs['X-Skill-Share-Score-Threshold'] = String(shareThreshold);
  const enforcement = getMcpConfig('ace_scope_enforcement');
  if (enforcement) hdrs['X-ACE-Scope-Enforcement'] = String(enforcement);
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
    const headers = buildClientHeaders();
    await fetch(`${httpUrl}/api/v1/memory/health`, {
      headers,
      signal: AbortSignal.timeout(timeoutMs),
    });
    return true;
  } catch {
    return false;
  }
}
