import { spawn } from 'node:child_process';
import { openSync } from 'node:fs';
import { join } from 'node:path';
import {
  PROJECT_DIR, ensureStateDir, saveState,
  getMcpConfig, getHttpUrl, findUv, findPython, ensureDefaultConfig,
} from '../../lib/common.mjs';
import { healthCheck } from '../../lib/http-client.mjs';

export default async function sessionStart(ctx) {
  // Ensure mcp.json exists (auto-create or migrate from legacy opencortex.json)
  ensureDefaultConfig();

  const configPath = ctx.configPath;
  if (!configPath) {
    return { systemMessage: '[opencortex-memory] WARNING: no config found — memory disabled' };
  }

  const mode = ctx.mode;
  const httpUrl = ctx.httpUrl;
  const tenantId = getMcpConfig('tenant_id', 'default');
  const userId = getMcpConfig('user_id', 'default');

  let httpPid = 0;

  if (mode === 'local') {
    // Start HTTP server if not running (MCP is managed by Claude Code via .mcp.json)
    let ready = await healthCheck(httpUrl);
    if (!ready) {
      const httpPort = getMcpConfig('local.http_port', 8921);
      ensureStateDir();
      const logPath = join(PROJECT_DIR, '.opencortex', 'memory', 'http_server.log');
      const logFd = openSync(logPath, 'a');

      // Prefer uv (handles venv + editable install automatically), fallback to python
      const uv = findUv();
      const spawnCmd = uv
        ? [uv, ['run', 'opencortex-server', '--host', '127.0.0.1', '--port', String(httpPort), '--log-level', 'WARNING']]
        : [findPython(), ['-m', 'opencortex.http', '--host', '127.0.0.1', '--port', String(httpPort), '--log-level', 'WARNING']];

      const child = spawn(spawnCmd[0], spawnCmd[1], {
        cwd: PROJECT_DIR,
        detached: true,
        stdio: ['ignore', logFd, logFd],
      });
      httpPid = child.pid || 0;
      child.unref();

      // Wait up to 10s
      for (let i = 0; i < 10; i++) {
        await sleep(1000);
        ready = await healthCheck(httpUrl);
        if (ready) break;
      }
      if (!ready) {
        return { systemMessage: `[opencortex-memory] WARNING: HTTP server failed to start on port ${httpPort}` };
      }
    }
  } else {
    // Remote mode — verify connectivity
    const ok = await healthCheck(httpUrl);
    if (!ok) {
      return { systemMessage: `[opencortex-memory] WARNING: remote server unreachable at ${httpUrl}` };
    }
  }

  // Write state
  const state = {
    active: true,
    mode,
    project_dir: PROJECT_DIR,
    config_path: configPath,
    http_url: httpUrl,
    tenant_id: tenantId,
    user_id: userId,
    http_pid: httpPid,
    last_turn_uuid: '',
    ingested_turns: 0,
    started_at: Math.floor(Date.now() / 1000),
  };
  saveState(state);

  const portInfo = mode === 'local'
    ? `HTTP :${getMcpConfig('local.http_port', 8921)}`
    : httpUrl;

  return {
    systemMessage: `[opencortex-memory] ${mode} mode — ${portInfo} tenant=${tenantId} user=${userId}`,
  };
}

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}
