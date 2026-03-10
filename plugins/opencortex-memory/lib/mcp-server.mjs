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
    'Store a new memory, resource, or skill. Returns the URI and metadata of the stored context.', {
      abstract:     { type: 'string',  description: 'Short summary of the memory', required: true },
      content:      { type: 'string',  description: 'Full content to store', default: '' },
      category:     { type: 'string',  description: 'Category: profile, preferences, entities, events, cases, patterns, error_fixes, workflows, strategies, documents, plans', default: '' },
      context_type: { type: 'string',  description: 'Type: memory, resource, skill, case, pattern', default: 'memory' },
      meta:         { type: 'object',  description: 'Optional metadata key-value pairs' },
      dedup:        { type: 'boolean', description: 'Check for semantic duplicates before storing (default true)', default: true },
    }],
  memory_batch_store: ['POST', '/api/v1/memory/batch_store',
    'Batch store multiple documents. Use with oc-scan for deterministic import.', {
      items:       { type: 'array',  description: 'Array of {content, category, context_type, meta}', required: true },
      source_path: { type: 'string', description: 'Source directory path', default: '' },
      scan_meta:   { type: 'object', description: 'Scan metadata {total_files, has_git, project_id}' },
    }],
  memory_search: ['POST', '/api/v1/memory/search',
    'Semantic search across stored memories, resources, and skills. Returns ranked results with relevance scores.', {
      query:        { type: 'string',  description: 'Search query', required: true },
      limit:        { type: 'integer', description: 'Max results to return', default: 5 },
      context_type: { type: 'string',  description: 'Filter by type (memory, resource, skill, case, pattern)' },
      category:     { type: 'string',  description: 'Filter by category (profile, preferences, entities, events, cases, patterns, etc.)' },
    }],
  memory_feedback: ['POST', '/api/v1/memory/feedback',
    'Submit reward feedback for a memory (reinforcement learning). Positive rewards reinforce retrieval; negative rewards penalize it.', {
      uri:    { type: 'string', description: 'URI of the memory to reward', required: true },
      reward: { type: 'number', description: 'Reward value (positive or negative)', required: true },
    }],
  memory_decay: ['POST', '/api/v1/memory/decay',
    'Trigger time-decay across all stored memories. Reduces effective scores of inactive memories over time.', {}],
  system_status: ['GET', '/api/v1/system/status',
    'Get system status (health, stats, or full doctor report).', {
      type: { type: 'string', description: 'Status type: health | stats | doctor', default: 'doctor' },
    }],

  // ── Session ──
  session_begin: ['POST', '/api/v1/session/begin',
    'Begin a new session. Starts Observer recording for trace splitting on session end.', {
      session_id: { type: 'string', description: 'Unique session identifier', required: true },
    }],
  session_message: ['POST', '/api/v1/session/message',
    'Add a message to an active session. Messages are recorded by the Observer for later trace splitting.', {
      session_id: { type: 'string', description: 'Session identifier', required: true },
      role:       { type: 'string', description: 'Message role (user/assistant)', required: true },
      content:    { type: 'string', description: 'Message content', required: true },
    }],
  session_end: ['POST', '/api/v1/session/end',
    'End a session and trigger trace splitting. The system splits the conversation into task traces and extracts reusable knowledge via the Archivist pipeline.', {
      session_id:    { type: 'string', description: 'Session identifier', required: true },
      quality_score: { type: 'number', description: 'Session quality score', default: 0.5 },
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

  // Build headers with identity + ACE config from MCP config
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
        serverInfo: { name: 'opencortex', version: '1.0.0' },
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
