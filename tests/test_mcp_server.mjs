/**
 * MCP Server tests for the Node.js stdio MCP proxy.
 *
 * Requires a running HTTP server on port 8921.
 * Run: node tests/test_mcp_server.mjs
 */
import { spawn } from 'node:child_process';
import { describe, it, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = join(__dirname, '..');
const MCP_SERVER = join(PROJECT_ROOT, 'plugins', 'opencortex-memory', 'lib', 'mcp-server.mjs');
const HTTP_URL = 'http://127.0.0.1:8921';

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

/** Send a JSON-RPC request to the MCP server and return the response. */
function createMcpClient() {
  const child = spawn('node', [MCP_SERVER], {
    cwd: PROJECT_ROOT,
    env: { ...process.env, CLAUDE_PROJECT_DIR: PROJECT_ROOT },
    stdio: ['pipe', 'pipe', 'pipe'],
  });

  let buffer = '';
  const pending = new Map(); // id → { resolve, reject }
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
      return text ? JSON.parse(text) : res.result;
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
    // Start HTTP server if not already running
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

      const toolsRes = await client.request('tools/list');
      const names = toolsRes.result.tools.map(t => t.name);
      for (const expected of [
        'memory_store', 'memory_search', 'memory_feedback',
        'memory_stats', 'memory_decay', 'memory_health',
        'session_begin', 'session_message', 'session_end',
        'hooks_route', 'hooks_init', 'hooks_verify',
        'hooks_doctor', 'hooks_export', 'hooks_build_agents',
      ]) {
        assert.ok(names.includes(expected), `Missing tool: ${expected}`);
      }
      assert.equal(names.length, 25, 'Expected 25 tools');
    } finally {
      client.close();
    }
  });

  it('02 memory_store', async () => {
    const client = createMcpClient();
    try {
      const data = await client.callTool('memory_store', {
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

  it('03 memory_search', async () => {
    const client = createMcpClient();
    try {
      // Store then search
      await client.callTool('memory_store', {
        abstract: 'Project uses TypeScript and React',
        category: 'tech',
      });
      const data = await client.callTool('memory_search', {
        query: 'What tech stack does the project use?',
        limit: 5,
      });
      assert.ok('results' in data, 'Should have results');
      assert.ok('total' in data, 'Should have total');
    } finally {
      client.close();
    }
  });

  it('04 memory_feedback', async () => {
    const client = createMcpClient();
    try {
      const stored = await client.callTool('memory_store', {
        abstract: 'Important design decision',
      });
      const fb = await client.callTool('memory_feedback', {
        uri: stored.uri,
        reward: 1.0,
      });
      assert.equal(fb.status, 'ok');
      assert.equal(fb.uri, stored.uri);
    } finally {
      client.close();
    }
  });

  it('05 memory_stats', async () => {
    const client = createMcpClient();
    try {
      const data = await client.callTool('memory_stats');
      assert.ok('tenant_id' in data);
      assert.ok('storage' in data);
    } finally {
      client.close();
    }
  });

  it('06 memory_decay', async () => {
    const client = createMcpClient();
    try {
      const data = await client.callTool('memory_decay');
      assert.ok('records_processed' in data);
    } finally {
      client.close();
    }
  });

  it('07 memory_health', async () => {
    const client = createMcpClient();
    try {
      const data = await client.callTool('memory_health');
      assert.ok(data.initialized);
      assert.ok(data.storage);
      assert.ok(data.embedder);
    } finally {
      client.close();
    }
  });

  it('08 full pipeline: store → search → feedback → decay', async () => {
    const client = createMcpClient();
    try {
      // Store
      const uris = [];
      for (const text of [
        'User prefers dark theme in VS Code',
        'Team uses PostgreSQL for production',
        'Deploy via GitHub Actions CI/CD',
      ]) {
        const r = await client.callTool('memory_store', {
          abstract: text, category: 'general',
        });
        uris.push(r.uri);
      }

      // Search
      const search = await client.callTool('memory_search', {
        query: 'database', limit: 3,
      });
      assert.ok(search.total > 0, 'Should find results');

      // Feedback
      const fb = await client.callTool('memory_feedback', {
        uri: uris[0], reward: 1.0,
      });
      assert.equal(fb.status, 'ok');

      // Decay
      const decay = await client.callTool('memory_decay');
      assert.ok(decay.records_processed >= 0);

      // Health
      const health = await client.callTool('memory_health');
      assert.ok(health.initialized);
    } finally {
      client.close();
    }
  });
});
