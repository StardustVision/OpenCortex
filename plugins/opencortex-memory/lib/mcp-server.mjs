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
  memory_stats: ['GET', '/api/v1/memory/stats',
    'Get system statistics including storage info, tenant, and component status.', {}],
  memory_decay: ['POST', '/api/v1/memory/decay',
    'Trigger time-decay across all stored memories. Reduces effective scores of inactive memories over time.', {}],
  memory_health: ['GET', '/api/v1/memory/health',
    'Check health status of all OpenCortex components.', {}],

  // ── Hooks Learn ──
  memory_hooks_learn: ['POST', '/api/v1/hooks/learn',
    'Record a learning outcome using native Q-learning. Maps OpenCortex concepts to hooks: state=URI, action=context_type, reward=feedback. Returns best action recommendation based on learned patterns.', {
      state:             { type: 'string', description: 'Current state identifier', required: true },
      action:            { type: 'string', description: 'Action taken', required: true },
      reward:            { type: 'number', description: 'Reward value', required: true },
      available_actions: { type: 'string', description: 'Comma-separated available actions', default: '' },
    }],
  memory_hooks_remember: ['POST', '/api/v1/hooks/remember',
    'Store content in semantic memory. Useful for remembering important context that should persist beyond session.', {
      content:     { type: 'string', description: 'Content to remember', required: true },
      memory_type: { type: 'string', description: 'Memory type category', default: 'general' },
    }],
  memory_hooks_recall: ['POST', '/api/v1/hooks/recall',
    'Search semantic memory for relevant content. Different from vector search - searches learned patterns and memories.', {
      query: { type: 'string',  description: 'Recall query', required: true },
      limit: { type: 'integer', description: 'Max results', default: 5 },
    }],
  memory_hooks_stats: ['GET', '/api/v1/hooks/stats',
    'Get hooks intelligence statistics (Q-learning patterns, memories, trajectories, errors).', {}],

  // ── Trajectory ──
  memory_hooks_trajectory_begin: ['POST', '/api/v1/hooks/trajectory/begin',
    'Begin tracking a learning trajectory for multi-step tasks.', {
      trajectory_id: { type: 'string', description: 'Unique trajectory identifier', required: true },
      initial_state: { type: 'string', description: 'Starting state', required: true },
    }],
  memory_hooks_trajectory_step: ['POST', '/api/v1/hooks/trajectory/step',
    'Add a step to an existing learning trajectory.', {
      trajectory_id: { type: 'string', description: 'Trajectory identifier', required: true },
      action:        { type: 'string', description: 'Action taken', required: true },
      reward:        { type: 'number', description: 'Step reward', required: true },
      next_state:    { type: 'string', description: 'Resulting state', default: '' },
    }],
  memory_hooks_trajectory_end: ['POST', '/api/v1/hooks/trajectory/end',
    'End a learning trajectory with a quality score.', {
      trajectory_id: { type: 'string', description: 'Trajectory identifier', required: true },
      quality_score: { type: 'number', description: 'Overall quality score', required: true },
    }],

  // ── Error Learning ──
  memory_hooks_error_record: ['POST', '/api/v1/hooks/error/record',
    'Record an error and its fix for the system to learn from. Helps the system remember how to fix common errors.', {
      error:   { type: 'string', description: 'Error description', required: true },
      fix:     { type: 'string', description: 'How the error was fixed', required: true },
      context: { type: 'string', description: 'Additional context', default: '' },
    }],
  memory_hooks_error_suggest: ['POST', '/api/v1/hooks/error/suggest',
    'Get suggested fixes for an error based on learned patterns. The system will recommend fixes based on previously recorded errors.', {
      error: { type: 'string', description: 'Error to get suggestions for', required: true },
    }],

  // ── Session ──
  session_begin: ['POST', '/api/v1/session/begin',
    'Begin a new session for context self-iteration. The session will buffer messages and extract persistent memories on end.', {
      session_id: { type: 'string', description: 'Unique session identifier', required: true },
    }],
  session_message: ['POST', '/api/v1/session/message',
    'Add a message to an active session. Messages are buffered for memory extraction when the session ends.', {
      session_id: { type: 'string', description: 'Session identifier', required: true },
      role:       { type: 'string', description: 'Message role (user/assistant)', required: true },
      content:    { type: 'string', description: 'Message content', required: true },
    }],
  session_end: ['POST', '/api/v1/session/end',
    'End a session and trigger memory extraction. The system will analyze the conversation and automatically extract persistent memories (preferences, patterns, skills, errors).', {
      session_id:    { type: 'string', description: 'Session identifier', required: true },
      quality_score: { type: 'number', description: 'Session quality score', default: 0.5 },
    }],

  // ── Integration ──
  hooks_route: ['POST', '/api/v1/integration/route',
    'Route a task to the best agent based on learned patterns. Returns the recommended agent and reasoning.', {
      task:   { type: 'string', description: 'Task description to route', required: true },
      agents: { type: 'string', description: 'Comma-separated available agents', default: '' },
    }],
  hooks_init: ['POST', '/api/v1/integration/init',
    'Initialize OpenCortex hooks configuration for a project.', {
      project_path: { type: 'string', description: 'Path to project', default: '.' },
    }],
  hooks_pretrain: ['POST', '/api/v1/integration/pretrain',
    'Pre-train OpenCortex from repository content (files, patterns, structure).', {
      repo_path: { type: 'string', description: 'Path to repository', default: '.' },
    }],
  hooks_verify: ['GET', '/api/v1/integration/verify',
    'Verify OpenCortex hooks configuration is correct and functional.', {}],
  hooks_doctor: ['GET', '/api/v1/integration/doctor',
    'Diagnose OpenCortex system health, configuration issues, and connectivity.', {}],
  hooks_export: ['POST', '/api/v1/integration/export',
    'Export OpenCortex intelligence data (learned patterns, memories, trajectories).', {
      format: { type: 'string', description: 'Export format', default: 'json' },
    }],
  hooks_build_agents: ['GET', '/api/v1/integration/build-agents',
    'Generate agent configuration based on learned patterns and project structure.', {}],
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
  const url = `${HTTP_URL}${path}`;

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
