#!/usr/bin/env node
/**
 * OpenCortex MCP Server — pure Node.js, stdio transport.
 * Thin proxy: MCP JSON-RPC ↔ HTTP REST API.
 * Zero external dependencies.
 */
import { getHttpUrl, ensureDefaultConfig } from './common.mjs';
import { buildClientHeaders } from './http-client.mjs';

// ── Tool definitions ───────────────────────────────────────────────────
// Each entry: [httpMethod, httpPath, description, parameters]
// parameters: { name: { type, description, required?, default? } }

const TOOLS = {
  // ── Core Memory ──
  memory_store: ['POST', '/api/v1/memory/store',
    'Persist a piece of knowledge the user wants remembered across sessions. '
    + 'Use when the user explicitly shares a preference, fact, decision, or correction — '
    + 'NOT for recording conversation turns (use memory_context commit for that). '
    + 'Semantic dedup is on by default: if a similar memory exists, it will be merged instead of duplicated. '
    + 'Returns {uri, context_type, category, abstract, dedup_action?}.', {
      abstract:     { type: 'string',  description: 'One-sentence summary capturing the key point (used for retrieval ranking)', required: true },
      content:      { type: 'string',  description: 'Full detailed content. If >500 chars, the system auto-generates a structured overview from it', default: '' },
      category:     { type: 'string',  description: 'Semantic category. Choose the most specific: profile | preferences | entities | events | cases | patterns | error_fixes | workflows | strategies | documents | plans', default: '' },
      context_type: { type: 'string',  description: 'Storage type: memory (default, for knowledge/facts) | resource (reference docs) | skill (reusable procedures)', default: 'memory' },
      meta:         { type: 'object',  description: 'Arbitrary key-value metadata (e.g. {source: "user", language: "zh"})' },
      dedup:        { type: 'boolean', description: 'Enable semantic dedup — merges into existing similar memory if found. Set false only for intentional duplicates', default: true },
    }],
  memory_batch_store: ['POST', '/api/v1/memory/batch_store',
    'Import multiple documents in one call. Use for bulk ingestion of files, notes, or scan results. '
    + 'Each item is stored independently with its own URI. '
    + 'Returns {stored, skipped, errors}.', {
      items:       { type: 'array',  description: 'Array of objects, each with: {abstract (required), content, category, context_type, meta}', required: true },
      source_path: { type: 'string', description: 'Source directory path for provenance tracking', default: '' },
      scan_meta:   { type: 'object', description: 'Import metadata: {total_files, has_git, project_id}' },
    }],
  memory_search: ['POST', '/api/v1/memory/search',
    'Search stored memories by natural language query. Uses intent-aware retrieval: '
    + 'the system analyzes your query to determine search strategy (top_k, detail level, reranking). '
    + 'Returns {results: [{uri, abstract, overview?, content?, context_type, score}], total}. '
    + 'Use when you need to recall facts, preferences, past decisions, or any previously stored knowledge.', {
      query:        { type: 'string',  description: 'Natural language query describing what you need to recall', required: true },
      limit:        { type: 'integer', description: 'Max results (system may return fewer based on relevance)', default: 5 },
      context_type: { type: 'string',  description: 'Restrict to type: memory | resource | skill. Omit to search all types' },
      category:     { type: 'string',  description: 'Restrict to category (e.g. "preferences", "error_fixes"). Omit to search all categories' },
    }],
  memory_feedback: ['POST', '/api/v1/memory/feedback',
    'Reinforce or penalize a memory via reward signal. Call with positive reward (+0.1 to +1.0) '
    + 'when a retrieved memory was useful. Call with negative reward (-0.1 to -1.0) when it was '
    + 'irrelevant or wrong. This adjusts future retrieval ranking through reinforcement learning.', {
      uri:    { type: 'string', description: 'The opencortex:// URI of the memory to reward (from search results)', required: true },
      reward: { type: 'number', description: 'Reward signal: positive reinforces retrieval, negative penalizes. Typical range: -1.0 to +1.0', required: true },
    }],
  memory_decay: ['POST', '/api/v1/memory/decay',
    'Maintenance: apply time-decay to all memories, reducing scores of inactive ones. '
    + 'Call periodically (e.g. daily) to let unused memories naturally fade. '
    + 'Frequently accessed memories resist decay.', {}],
  system_status: ['GET', '/api/v1/system/status',
    'Check system health and diagnostics. Returns memory count, storage stats, and component status.', {
      type: { type: 'string', description: 'Report depth: health (quick liveness) | stats (counts and sizes) | doctor (full diagnostic)', default: 'doctor' },
    }],

  // ── Session (low-level) ──
  // Prefer memory_context for most use cases — it handles session lifecycle automatically.
  session_begin: ['POST', '/api/v1/session/begin',
    'Low-level: start session recording. Prefer memory_context which auto-creates sessions. '
    + 'Only use directly if you need explicit session control without the context lifecycle.', {
      session_id: { type: 'string', description: 'Unique session identifier', required: true },
    }],
  session_message: ['POST', '/api/v1/session/message',
    'Low-level: record a single message to an active session. '
    + 'Prefer memory_context commit which records the full turn and handles idempotency.', {
      session_id: { type: 'string', description: 'Session identifier (must call session_begin first)', required: true },
      role:       { type: 'string', description: 'Message role: user | assistant', required: true },
      content:    { type: 'string', description: 'Message text content', required: true },
    }],
  session_end: ['POST', '/api/v1/session/end',
    'Low-level: end session and trigger knowledge extraction pipeline. '
    + 'Prefer memory_context with phase="end". '
    + 'The system splits the conversation into task traces and extracts reusable knowledge.', {
      session_id:    { type: 'string', description: 'Session identifier to close', required: true },
      quality_score: { type: 'number', description: 'Overall session quality (0.0-1.0). Higher scores prioritize knowledge extraction', default: 0.5 },
    }],

  // ── Context Protocol (recommended) ──
  memory_context: ['POST', '/api/v1/context',
    'Primary tool for memory-augmented conversations. Manages the full lifecycle in three phases:\n'
    + '\n'
    + 'PHASE 1 — prepare: Call BEFORE generating your response. Retrieves relevant memories and knowledge '
    + 'based on the user\'s message. Returns {memory: [...], knowledge: [...], instructions, intent}. '
    + 'Use the returned context to inform your response. Session is auto-created if needed.\n'
    + '\n'
    + 'PHASE 2 — commit: Call AFTER generating your response. Records the full conversation turn '
    + '(user message + your response). Pass cited_uris to reward memories you actually used. '
    + 'Returns {accepted, write_status, session_turns}. Idempotent — safe to retry.\n'
    + '\n'
    + 'PHASE 3 — end: Call when the conversation is over. Triggers knowledge extraction from the '
    + 'session transcript. Returns {status: "closed", total_turns}.\n'
    + '\n'
    + 'Typical flow per turn: prepare → [generate response] → commit. Call end once at session close.', {
      session_id: { type: 'string', description: 'Stable session identifier — reuse across all turns in one conversation. Alphanumeric, hyphens, underscores, 1-128 chars', required: true },
      phase:      { type: 'string', description: 'Lifecycle phase: "prepare" (before response) | "commit" (after response) | "end" (session close)', required: true },
      turn_id:    { type: 'string', description: 'Unique per-turn ID for idempotency. Required for prepare and commit. Use a counter (t1, t2...) or UUID' },
      messages:   { type: 'array',  description: 'Array of {role, content}. prepare: pass [user message]. commit: pass [user message, assistant response]. Not needed for end' },
      cited_uris: { type: 'array',  description: 'commit only: array of opencortex:// URIs from prepare results that you referenced in your response. Triggers +0.1 RL reward per URI' },
      config:     { type: 'object', description: 'prepare only: {max_items: 1-20 (default 5), detail_level: "l0"|"l1"|"l2" (default "l1"), recall_mode: "auto"|"always"|"never" (default "auto")}' },
    }],
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

// ── HTTP proxy ─────────────────────────────────────────────────────────
const HTTP_URL = getHttpUrl();

async function callTool(name, args) {
  const def = TOOLS[name];
  if (!def) throw new Error(`Unknown tool: ${name}`);
  const [method, path] = def;
  let url = `${HTTP_URL}${path}`;

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

  // Build headers with identity from MCP config
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
  if (!res.ok) throw new Error(`${method} ${path} → ${res.status}: ${text}`);
  try { return JSON.parse(text); } catch { return text; }
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
        serverInfo: { name: 'opencortex', version: '0.4.2' },
      });

    case 'notifications/initialized':
      return; // no response needed

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
  let buffer = '';
  for await (const chunk of process.stdin) {
    buffer += chunk.toString();
    // Process complete lines
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
}

main();
