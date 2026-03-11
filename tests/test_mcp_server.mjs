/**
 * MCP Server tests for the Node.js stdio MCP proxy.
 *
 * Requires a running HTTP server on port 8921.
 * Run: node --test tests/test_mcp_server.mjs
 */
import { spawn } from 'node:child_process';
import { describe, it, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = join(__dirname, '..');
const MCP_SERVER = join(PROJECT_ROOT, 'plugins', 'opencortex-memory', 'lib', 'mcp-server.mjs');

function loadMcpConfig() {
  try {
    const raw = readFileSync(join(PROJECT_ROOT, 'mcp.json'), 'utf8');
    return JSON.parse(raw);
  } catch {
    return {};
  }
}

const MCP_CONFIG = loadMcpConfig();
const TEST_MODE = MCP_CONFIG.mode || 'local';
const HTTP_URL = TEST_MODE === 'remote'
  ? (MCP_CONFIG.remote?.http_url || 'http://127.0.0.1:8921')
  : 'http://127.0.0.1:8921';

// ── helpers ────────────────────────────────────────────────────────────

async function healthCheck() {
  try {
    const res = await fetch(`${HTTP_URL}/api/v1/memory/health`, { signal: AbortSignal.timeout(2000) });
    return res.ok;
  } catch { return false; }
}

async function waitForServer(maxWaitMs = 15000) {
  const start = Date.now();
  while (Date.now() - start < maxWaitMs) {
    if (await healthCheck()) return true;
    await new Promise(r => setTimeout(r, 500));
  }
  return false;
}

/** Create MCP client that spawns the MCP server process. */
function createMcpClient() {
  const child = spawn('node', [MCP_SERVER], {
    cwd: PROJECT_ROOT,
    env: { ...process.env, CLAUDE_PROJECT_DIR: PROJECT_ROOT },
    stdio: ['pipe', 'pipe', 'pipe'],
  });

  let buffer = '';
  const pending = new Map(); // id -> { resolve, reject }
  let nextId = 1;

  child.stdout.on('data', (chunk) => {
    buffer += chunk.toString();
    let nl;
    while ((nl = buffer.indexOf('\n')) !== -1) {
      const line = buffer.slice(0, nl).trim();
      buffer = buffer.slice(nl + 1);
      if (!line) continue;
      try {
        const msg = JSON.parse(line);
        if (msg.id != null && pending.has(msg.id)) {
          const { resolve } = pending.get(msg.id);
          pending.delete(msg.id);
          resolve(msg);
        }
      } catch { /* skip */ }
    }
  });

  return {
    async request(method, params = {}) {
      const id = nextId++;
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          pending.delete(id);
          reject(new Error(`Timeout waiting for response to ${method}`));
        }, 30000);
        pending.set(id, {
          resolve: (msg) => { clearTimeout(timer); resolve(msg); },
          reject,
        });
        const msg = JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n';
        child.stdin.write(msg);
      });
    },
    async callTool(name, args = {}) {
      const res = await this.request('tools/call', { name, arguments: args });
      if (res.error) throw new Error(res.error.message);
      const text = res.result?.content?.[0]?.text;
      if (!text) return res.result;
      // Check for tool-level error
      if (res.result?.isError) throw new Error(text);
      try { return JSON.parse(text); } catch { return text; }
    },
    async init() {
      // Initialize + wait for notifications/initialized
      await this.request('initialize', {
        protocolVersion: '2024-11-05',
        capabilities: {},
        clientInfo: { name: 'test', version: '1.0' },
      });
      // Send notifications/initialized (no response expected)
      const msg = JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized', params: {} }) + '\n';
      child.stdin.write(msg);
      // Give the server time to run initSession()
      await new Promise(r => setTimeout(r, 2000));
    },
    close() {
      child.stdin.end();
      child.kill();
    },
  };
}

// ── tests ──────────────────────────────────────────────────────────────

let httpServer = null;

describe('MCP Server (Node.js stdio proxy)', async () => {
  before(async () => {
    if (TEST_MODE === 'remote') {
      const ok = await healthCheck();
      if (!ok) throw new Error(`Remote HTTP server unreachable: ${HTTP_URL}`);
      return;
    }

    // Local mode: start HTTP server if not already running
    if (!(await healthCheck())) {
      httpServer = spawn('uv', ['run', 'python3', '-m', 'opencortex.http',
        '--host', '127.0.0.1', '--port', '8921', '--log-level', 'WARNING',
      ], {
        cwd: PROJECT_ROOT,
        stdio: ['ignore', 'pipe', 'pipe'],
        detached: true,
      });
      httpServer.unref();
      const ready = await waitForServer();
      if (!ready) throw new Error('HTTP server failed to start');
    }
  });

  after(() => {
    if (httpServer) {
      try { process.kill(httpServer.pid, 'SIGTERM'); } catch { /* ok */ }
    }
  });

  it('01 initialize + list tools', async () => {
    const client = createMcpClient();
    try {
      const initRes = await client.request('initialize', {
        protocolVersion: '2024-11-05',
        capabilities: {},
        clientInfo: { name: 'test', version: '1.0' },
      });
      assert.equal(initRes.result.serverInfo.name, 'opencortex');
      assert.equal(initRes.result.serverInfo.version, '0.5.0');

      const toolsRes = await client.request('tools/list');
      const names = toolsRes.result.tools.map(t => t.name);
      for (const expected of [
        'store', 'batch_store', 'search', 'feedback', 'decay',
        'system_status',
        'recall', 'add_message', 'end',
      ]) {
        assert.ok(names.includes(expected), `Missing tool: ${expected}`);
      }
      assert.equal(names.length, 9, `Expected 9 tools, got ${names.length}: ${names.join(', ')}`);

      // Verify old tools are NOT present
      for (const removed of [
        'memory_store', 'memory_batch_store', 'memory_search', 'memory_feedback', 'memory_decay',
        'memory_context', 'session_begin', 'session_message', 'session_end',
      ]) {
        assert.ok(!names.includes(removed), `Tool should be removed: ${removed}`);
      }
    } finally {
      client.close();
    }
  });

  it('02 store', async () => {
    const client = createMcpClient();
    try {
      await client.init();
      const data = await client.callTool('store', {
        abstract: 'User prefers dark theme',
        category: 'preferences',
      });
      assert.ok(data.uri, 'Should return URI');
      assert.equal(data.context_type, 'memory');
      assert.equal(data.category, 'preferences');
    } finally {
      client.close();
    }
  });

  it('03 search', async () => {
    const client = createMcpClient();
    try {
      await client.init();
      // Store then search
      await client.callTool('store', {
        abstract: 'Project uses TypeScript and React',
        category: 'tech',
      });
      const data = await client.callTool('search', {
        query: 'What tech stack does the project use?',
        limit: 5,
      });
      assert.ok('results' in data, 'Should have results');
      assert.ok('total' in data, 'Should have total');
    } finally {
      client.close();
    }
  });

  it('04 feedback', async () => {
    const client = createMcpClient();
    try {
      await client.init();
      const stored = await client.callTool('store', {
        abstract: 'Important design decision',
      });
      const fb = await client.callTool('feedback', {
        uri: stored.uri,
        reward: 1.0,
      });
      assert.equal(fb.status, 'ok');
      assert.equal(fb.uri, stored.uri);
    } finally {
      client.close();
    }
  });

  it('05 system_status(stats)', async () => {
    const client = createMcpClient();
    try {
      await client.init();
      const data = await client.callTool('system_status', { type: 'stats' });
      assert.ok('tenant_id' in data);
      assert.ok('storage' in data);
    } finally {
      client.close();
    }
  });

  it('06 decay', async () => {
    const client = createMcpClient();
    try {
      await client.init();
      const data = await client.callTool('decay');
      assert.ok('records_processed' in data);
    } finally {
      client.close();
    }
  });

  it('07 system_status(health)', async () => {
    const client = createMcpClient();
    try {
      await client.init();
      const data = await client.callTool('system_status', { type: 'health' });
      assert.ok(data.initialized);
      assert.ok(data.storage);
      assert.ok(data.embedder);
    } finally {
      client.close();
    }
  });

  it('08 full pipeline: store -> search -> feedback -> decay', async () => {
    const client = createMcpClient();
    try {
      await client.init();
      // Store
      const uris = [];
      for (const text of [
        'User prefers dark theme in VS Code',
        'Team uses PostgreSQL for production',
        'Deploy via GitHub Actions CI/CD',
      ]) {
        const r = await client.callTool('store', {
          abstract: text, category: 'general',
        });
        uris.push(r.uri);
      }

      // Search
      const searchResult = await client.callTool('search', {
        query: 'database', limit: 3,
      });
      assert.ok(searchResult.total > 0, 'Should find results');

      // Feedback
      const fb = await client.callTool('feedback', {
        uri: uris[0], reward: 1.0,
      });
      assert.equal(fb.status, 'ok');

      // Decay
      const decayResult = await client.callTool('decay');
      assert.ok(decayResult.records_processed >= 0);

      // Health
      const health = await client.callTool('system_status', { type: 'health' });
      assert.ok(health.initialized);
    } finally {
      client.close();
    }
  });

  it('09 lifecycle: recall -> add_message -> end', async () => {
    const client = createMcpClient();
    try {
      await client.init();

      // Store a memory first for recall to find
      await client.callTool('store', {
        abstract: 'The deploy pipeline uses Docker containers',
        category: 'workflows',
      });

      // Recall
      const recallResult = await client.callTool('recall', {
        query: 'How do we deploy?',
        max_items: 3,
      });
      assert.ok('memory' in recallResult, 'recall should return memory array');
      assert.ok('knowledge' in recallResult, 'recall should return knowledge array');
      assert.ok('instructions' in recallResult, 'recall should return instructions');
      assert.ok('turn_id' in recallResult, 'recall should return turn_id');

      // Add message
      const commitResult = await client.callTool('add_message', {
        user_message: 'How do we deploy?',
        assistant_response: 'We use Docker containers for deployment.',
      });
      assert.ok(commitResult.accepted, 'commit should be accepted');
      assert.ok(commitResult.turn_id, 'commit should return turn_id');

      // End
      const endResult = await client.callTool('end', {});
      assert.ok(endResult.status, 'end should return status');
      assert.ok('total_turns' in endResult, 'end should return total_turns');
    } finally {
      client.close();
    }
  });
});
