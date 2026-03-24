#!/usr/bin/env node
/**
 * OpenCortex MCP Server — pure Node.js, stdio transport.
 * Thin proxy: MCP JSON-RPC <-> HTTP REST API.
 * Zero external dependencies.
 *
 * Session lifecycle (recall/add_message/end) is managed internally;
 * all other tools proxy directly to the HTTP API.
 */
import { getHttpUrl, getPluginMode, getMcpConfig, ensureDefaultConfig, startLocalHttpServer } from './common.mjs';
import { buildClientHeaders, healthCheck, httpPost } from './http-client.mjs';

// ── Module-level session state ──────────────────────────────────────────
let _httpUrl = null;
let _sessionId = null;
let _turnCounter = 0;
let _lastRecallTurnId = null;
let _httpPid = 0;
let _initialized = false;

// ── Tool definitions ───────────────────────────────────────────────────
// Proxy tools: [httpMethod, httpPath, description, parameters]
// Lifecycle tools (recall, add_message, end): [null, null, description, parameters]

const TOOLS = {
  // ── Core Memory ──
  store: ['POST', '/api/v1/memory/store',
    'Persist a piece of knowledge the user wants remembered across sessions. '
    + 'Use when the user explicitly shares a preference, fact, decision, or correction — '
    + 'NOT for recording conversation turns (use add_message for that). '
    + 'Semantic dedup is on by default: if a similar memory exists, it will be merged instead of duplicated. '
    + 'Returns {uri, context_type, category, abstract, dedup_action?}.', {
      abstract:     { type: 'string',  description: 'One-sentence summary capturing the key point (used for retrieval ranking)', required: true },
      content:      { type: 'string',  description: 'Full detailed content. If >500 chars, the system auto-generates a structured overview from it', default: '' },
      category:     { type: 'string',  description: 'Semantic category. Choose the most specific: profile | preferences | entities | events | cases | patterns | error_fixes | workflows | strategies | documents | plans', default: '' },
      context_type: { type: 'string',  description: 'Storage type: memory (default, for knowledge/facts) | resource (reference docs) | skill (reusable procedures)', default: 'memory' },
      meta:         { type: 'object',  description: 'Arbitrary key-value metadata (e.g. {source: "user", language: "zh"})' },
      dedup:        { type: 'boolean', description: 'Enable semantic dedup — merges into existing similar memory if found. Set false only for intentional duplicates', default: true },
    }],
  batch_store: ['POST', '/api/v1/memory/batch_store',
    'Import multiple documents in one call. Use for bulk ingestion of files, notes, or scan results. '
    + 'Each item is stored independently with its own URI. '
    + 'Returns {stored, skipped, errors}.', {
      items:       { type: 'array',  description: 'Array of objects, each with: {abstract (required), content, category, context_type, meta}', required: true },
      source_path: { type: 'string', description: 'Source directory path for provenance tracking', default: '' },
      scan_meta:   { type: 'object', description: 'Import metadata: {total_files, has_git, project_id}' },
    }],
  search: ['POST', '/api/v1/memory/search',
    'Search stored memories by natural language query. Uses intent-aware retrieval: '
    + 'the system analyzes your query to determine search strategy (top_k, detail level, reranking). '
    + 'Returns {results: [{uri, abstract, overview?, content?, context_type, score}], total}. '
    + 'Use when you need to recall facts, preferences, past decisions, or any previously stored knowledge.', {
      query:        { type: 'string',  description: 'Natural language query describing what you need to recall', required: true },
      limit:        { type: 'integer', description: 'Max results (system may return fewer based on relevance)', default: 5 },
      context_type: { type: 'string',  description: 'Restrict to type: memory | resource | skill. Omit to search all types' },
      category:     { type: 'string',  description: 'Restrict to category (e.g. "preferences", "error_fixes"). Omit to search all categories' },
    }],
  feedback: ['POST', '/api/v1/memory/feedback',
    'Reinforce or penalize a memory via reward signal. Call with positive reward (+0.1 to +1.0) '
    + 'when a retrieved memory was useful. Call with negative reward (-0.1 to -1.0) when it was '
    + 'irrelevant or wrong. This adjusts future retrieval ranking through reinforcement learning.', {
      uri:    { type: 'string', description: 'The opencortex:// URI of the memory to reward (from search results)', required: true },
      reward: { type: 'number', description: 'Reward signal: positive reinforces retrieval, negative penalizes. Typical range: -1.0 to +1.0', required: true },
    }],
  forget: ['POST', '/api/v1/memory/forget',
    'Delete a memory permanently. Use when the user asks to forget, '
    + 'remove, or delete a specific memory. Supports two modes: '
    + 'by URI (exact match) or by query (semantic search, deletes top match). '
    + 'Returns {status, forgotten, uri}.', {
      uri:   { type: 'string', description: 'The opencortex:// URI to delete (from search results)' },
      query: { type: 'string', description: 'Natural language description of what to forget — finds and deletes the closest match' },
    }],
  decay: ['POST', '/api/v1/memory/decay',
    'Maintenance: apply time-decay to all memories, reducing scores of inactive ones. '
    + 'Call periodically (e.g. daily) to let unused memories naturally fade. '
    + 'Frequently accessed memories resist decay.', {}],
  system_status: ['GET', '/api/v1/system/status',
    'Check system health and diagnostics. Returns memory count, storage stats, and component status.', {
      type: { type: 'string', description: 'Report depth: health (quick liveness) | stats (counts and sizes) | doctor (full diagnostic)', default: 'doctor' },
    }],

  // ── Lifecycle (routed internally, not proxied) ──
  recall: [null, null,
    'MANDATORY: You MUST call this tool BEFORE generating ANY response to the user. '
    + 'This is required for EVERY interaction without exception.\n\n'
    + 'Searches long-term memory and knowledge base for relevant context. '
    + 'Returns {memory: [...], knowledge: [...], instructions}. '
    + 'Reference relevant memories in your response.\n\n'
    + 'After responding, you MUST call add_message to record the conversation.', {
      query:             { type: 'string',  description: "The user's message or search query", required: true },
      category:          { type: 'string',  description: 'Filter by category: profile | preferences | entities | events | cases | patterns | error_fixes | workflows | strategies | documents | plans. Omit to search all' },
      context_type:      { type: 'string',  description: 'Filter by type: memory | resource | skill. Omit to search all' },
      include_knowledge: { type: 'boolean', description: 'Also search approved knowledge base. Default: false (Phase 2 feature)', default: false },
      max_items:         { type: 'integer', description: 'Max results to return (1-20). Default: 5', default: 5 },
      detail_level:      { type: 'string',  description: 'Response detail: l0 (abstract only) | l1 (+ overview) | l2 (+ full content). Default: l1', default: 'l1' },
    }],
  add_message: [null, null,
    'MANDATORY: You MUST call this tool AFTER generating EVERY response. '
    + 'This is required regardless of topic. Skipping means the conversation is lost forever.\n\n'
    + 'Records the conversation turn for future recall and knowledge extraction. '
    + 'Pass cited_uris for opencortex:// URIs you referenced from recall results.', {
      user_message:       { type: 'string', description: "The user's message", required: true },
      assistant_response: { type: 'string', description: 'Your response to the user', required: true },
      cited_uris:         { type: 'array',  description: 'opencortex:// URIs referenced in response' },
    }],
  end: [null, null,
    'End the memory session and trigger knowledge extraction. '
    + 'Called automatically on process exit. Only call explicitly if you want '
    + 'mid-session extraction or the user says goodbye.', {}],
};

// ── Build JSON Schema for tools/list ───────────────────────────────────
function buildToolSchema(name, [, , description, params]) {
  const properties = {};
  const required = [];
  for (const [pName, pDef] of Object.entries(params)) {
    const prop = { type: pDef.type, description: pDef.description };
    if (pDef.default !== undefined) prop.default = pDef.default;
    properties[pName] = prop;
    if (pDef.required) required.push(pName);
  }
  const schema = { type: 'object', properties };
  if (required.length) schema.required = required;
  return { name, description, inputSchema: schema };
}

// ── HTTP proxy for standard tools ───────────────────────────────────────
async function callProxyTool(name, args) {
  const def = TOOLS[name];
  if (!def) throw new Error(`Unknown tool: ${name}`);
  const [method, path] = def;
  let url = `${_httpUrl}${path}`;

  // Apply defaults
  const params = def[3];
  const body = {};
  for (const [pName, pDef] of Object.entries(params)) {
    if (args[pName] !== undefined) {
      body[pName] = args[pName];
    } else if (pDef.default !== undefined) {
      body[pName] = pDef.default;
    }
  }

  const hdrs = buildClientHeaders();
  const opts = { method, signal: AbortSignal.timeout(30000) };
  if (method === 'POST') {
    hdrs['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  } else if (method === 'GET' && Object.keys(body).length > 0) {
    const qs = new URLSearchParams(body).toString();
    url = `${url}?${qs}`;
  }
  opts.headers = hdrs;

  const res = await fetch(url, opts);
  const text = await res.text();
  if (!res.ok) throw new Error(`${method} ${path} -> ${res.status}: ${text}`);
  try { return JSON.parse(text); } catch { return text; }
}

// ── Lifecycle tool handlers ──────────────────────────────────────────────
async function handleRecall(args) {
  _turnCounter++;
  const turnId = `t${_turnCounter}`;
  _lastRecallTurnId = turnId;

  const config = {};
  if (args.max_items !== undefined) config.max_items = args.max_items;
  if (args.detail_level !== undefined) config.detail_level = args.detail_level;
  if (args.category !== undefined) config.category = args.category;
  if (args.context_type !== undefined) config.context_type = args.context_type;
  if (args.include_knowledge !== undefined) config.include_knowledge = args.include_knowledge;

  const body = {
    session_id: _sessionId,
    phase: 'prepare',
    turn_id: turnId,
    messages: [{ role: 'user', content: args.query }],
    config,
  };

  return await httpContextCall(body);
}

async function handleAddMessage(args) {
  const turnId = _lastRecallTurnId || `t${++_turnCounter}`;

  const body = {
    session_id: _sessionId,
    phase: 'commit',
    turn_id: turnId,
    messages: [
      { role: 'user', content: args.user_message },
      { role: 'assistant', content: args.assistant_response },
    ],
  };
  if (args.cited_uris) body.cited_uris = args.cited_uris;

  return await httpContextCall(body);
}

async function handleEnd() {
  const body = {
    session_id: _sessionId,
    phase: 'end',
  };
  return await httpContextCall(body);
}

async function httpContextCall(body) {
  const hdrs = buildClientHeaders();
  hdrs['Content-Type'] = 'application/json';

  let res, text;
  try {
    res = await fetch(`${_httpUrl}/api/v1/context`, {
      method: 'POST',
      headers: hdrs,
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(30000),
    });
    text = await res.text();
  } catch (err) {
    // Network error / timeout → degrade gracefully
    // Memory is auxiliary — should never block the user's prompt
    const reason = `Memory unavailable: ${err.message}`;
    process.stderr.write(`[opencortex-mcp] ${reason}\n`);
    return _degradedResult(body.phase, reason);
  }

  if (!res.ok) {
    const detail = text?.slice(0, 200) || 'unknown error';
    const reason = res.status === 401 || res.status === 403
      ? `Memory unavailable (HTTP ${res.status}): authentication required`
      : `Memory unavailable (HTTP ${res.status}): ${detail}`;
    process.stderr.write(`[opencortex-mcp] ${reason}\n`);
    return _degradedResult(body.phase, reason);
  }

  try { return JSON.parse(text); } catch { return text; }
}

/**
 * Build a degraded (best-effort) result for lifecycle tools.
 * Memory is auxiliary — auth/network/server failures should not block the user's prompt.
 */
function _degradedResult(phase, reason) {
  switch (phase) {
    case 'prepare':
      return { memory: [], knowledge: [], instructions: reason, _degraded: true };
    case 'commit':
      return { accepted: false, _degraded: true, reason };
    case 'end':
      return { status: 'skipped', _degraded: true, reason };
    default:
      return { _degraded: true, reason };
  }
}

// ── Unified callTool dispatcher ──────────────────────────────────────────
async function callTool(name, args) {
  switch (name) {
    case 'recall':      return handleRecall(args);
    case 'add_message': return handleAddMessage(args);
    case 'end':         return handleEnd();
    default:            return callProxyTool(name, args);
  }
}

// ── Init / Shutdown lifecycle ───────────────────────────────────────────
async function initSession() {
  _httpUrl = getHttpUrl();
  const mode = getPluginMode();
  const _log = (msg) => process.stderr.write(`[opencortex-mcp] ${msg}\n`);

  if (mode === 'local') {
    const result = await startLocalHttpServer(_httpUrl, _log);
    _httpPid = result.pid;
    if (!result.ready) {
      _log('WARNING: HTTP server not ready — tools may fail');
    }
  } else {
    const ok = await healthCheck(_httpUrl);
    if (!ok) {
      _log(`WARNING: remote server unreachable at ${_httpUrl}`);
    }
  }

  // Generate session ID
  _sessionId = `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;

  // Register session on server (best-effort)
  try {
    await httpPost(`${_httpUrl}/api/v1/session/begin`, {
      session_id: _sessionId,
    }, 5000);
  } catch {
    // best-effort
  }

  _initialized = true;
  _log(`session ${_sessionId} (${mode} mode)`);
}

async function shutdown() {
  const _log = (msg) => process.stderr.write(`[opencortex-mcp] ${msg}\n`);

  if (_initialized && _sessionId) {
    const result = await httpContextCall({
      session_id: _sessionId,
      phase: 'end',
    });
    _log(result?._degraded ? `session end skipped: ${result.reason}` : 'session ended');
  }

  if (_httpPid > 0 && getPluginMode() === 'local') {
    try {
      process.kill(_httpPid, 'SIGTERM');
    } catch {
      // already exited
    }
  }
}

// ── JSON-RPC stdio transport ───────────────────────────────────────────
function send(msg) {
  const json = JSON.stringify(msg);
  process.stdout.write(`${json}\n`);
}

function jsonrpcResult(id, result) {
  send({ jsonrpc: '2.0', id, result });
}

function jsonrpcError(id, code, message) {
  send({ jsonrpc: '2.0', id, error: { code, message } });
}

async function handleMessage(msg) {
  const { id, method, params } = msg;

  switch (method) {
    case 'initialize':
      return jsonrpcResult(id, {
        protocolVersion: '2024-11-05',
        capabilities: { tools: {} },
        serverInfo: { name: 'opencortex', version: '0.5.0' },
      });

    case 'notifications/initialized':
      // Trigger session init in background (don't block the notification)
      initSession().catch(err => {
        process.stderr.write(`[opencortex-mcp] init error: ${err.message}\n`);
      });
      return;

    case 'tools/list':
      return jsonrpcResult(id, {
        tools: Object.entries(TOOLS).map(([name, def]) => buildToolSchema(name, def)),
      });

    case 'tools/call': {
      const toolName = params?.name;
      const toolArgs = params?.arguments || {};
      try {
        const result = await callTool(toolName, toolArgs);
        return jsonrpcResult(id, {
          content: [{ type: 'text', text: typeof result === 'string' ? result : JSON.stringify(result) }],
        });
      } catch (err) {
        return jsonrpcResult(id, {
          content: [{ type: 'text', text: `Error: ${err.message}` }],
          isError: true,
        });
      }
    }

    default:
      if (id != null) {
        jsonrpcError(id, -32601, `Method not found: ${method}`);
      }
  }
}

// ── Main loop ──────────────────────────────────────────────────────────
async function main() {
  ensureDefaultConfig();

  // Graceful shutdown on signals
  const onExit = () => {
    shutdown().finally(() => process.exit(0));
  };
  process.on('SIGTERM', onExit);
  process.on('SIGINT', onExit);

  let buffer = '';
  for await (const chunk of process.stdin) {
    buffer += chunk.toString();
    let nl;
    while ((nl = buffer.indexOf('\n')) !== -1) {
      const line = buffer.slice(0, nl).trim();
      buffer = buffer.slice(nl + 1);
      if (!line) continue;
      try {
        const msg = JSON.parse(line);
        await handleMessage(msg);
      } catch (err) {
        process.stderr.write(`[opencortex-mcp] parse error: ${err.message}\n`);
      }
    }
  }

  // stdin closed — shutdown
  await shutdown();
}

main();
