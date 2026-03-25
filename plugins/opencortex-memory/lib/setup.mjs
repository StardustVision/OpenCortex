/**
 * Interactive setup wizard for OpenCortex MCP plugin.
 *
 * Usage:  opencortex-cli setup
 *
 * Guides the user through local/remote mode selection, writes
 * ~/.opencortex/mcp.json, tests connectivity, and optionally
 * registers the MCP server at Claude Code user level.
 */

import { createInterface } from 'node:readline';
import { execSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { DEFAULT_MCP_CONFIG, writeMcpConfig, getHttpUrl } from './common.mjs';
import { healthCheck } from './http-client.mjs';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const MCP_SERVER_PATH = join(__dirname, 'mcp-server.mjs');

// ── helpers ──────────────────────────────────────────────────────────────

function createRL() {
  return createInterface({ input: process.stdin, output: process.stdout });
}

function ask(rl, question) {
  return new Promise((resolve, reject) => {
    rl.question(question, answer => resolve(answer.trim()));
    rl.once('close', () => resolve(''));
  });
}

async function askChoice(rl, question, choices) {
  const labels = choices.map((c, i) => `  ${i + 1}) ${c}`).join('\n');
  while (true) {
    const answer = await ask(rl, `${question}\n${labels}\n> `);
    const idx = parseInt(answer, 10) - 1;
    if (idx >= 0 && idx < choices.length) return choices[idx];
    console.log(`Please enter 1-${choices.length}.`);
  }
}

// ── main ─────────────────────────────────────────────────────────────────

export async function runSetup() {
  const rl = createRL();

  try {
    console.log('\n=== OpenCortex Setup ===\n');

    // 1. Mode selection
    const mode = await askChoice(rl, 'Select mode:', ['local', 'remote']);

    const config = { ...DEFAULT_MCP_CONFIG, mode };

    if (mode === 'remote') {
      // 2a. Remote: URL + token
      const url = await ask(rl, `Server URL [${DEFAULT_MCP_CONFIG.remote.http_url}]: `);
      if (url) config.remote = { http_url: url };

      const token = await ask(rl, 'JWT token: ');
      if (token) config.token = token;
    } else {
      // 2b. Local: port + optional token
      const port = await ask(rl, `HTTP port [${DEFAULT_MCP_CONFIG.local.http_port}]: `);
      if (port) config.local = { http_port: parseInt(port, 10) || 8921 };

      const token = await ask(rl, 'JWT token (optional, press Enter to skip): ');
      if (token) config.token = token;
    }

    // 3. Write config
    const configPath = writeMcpConfig(config);
    console.log(`\nConfig saved to ${configPath}`);

    // 4. Health check
    const httpUrl = mode === 'remote'
      ? (config.remote?.http_url || DEFAULT_MCP_CONFIG.remote.http_url)
      : `http://127.0.0.1:${config.local?.http_port || DEFAULT_MCP_CONFIG.local.http_port}`;

    process.stdout.write(`Testing connection to ${httpUrl} ... `);
    const ok = await healthCheck(httpUrl);
    console.log(ok ? 'OK' : 'UNREACHABLE (you can configure the server later)');

    // 5. Offer Claude Code user-level MCP registration
    let registerClaude = 'no';
    try {
      registerClaude = await askChoice(
        rl,
        '\nRegister as Claude Code user-level MCP server? (all projects will have access)',
        ['yes', 'no'],
      );
    } catch { /* readline closed (e.g., piped input) — skip */ }

    if (registerClaude === 'yes') {
      try {
        const envArgs = [
          `-e OPENCORTEX_MODE=${mode}`,
          config.token ? `-e OPENCORTEX_TOKEN=${config.token}` : '',
          mode === 'remote' && config.remote?.http_url
            ? `-e OPENCORTEX_HTTP_URL=${config.remote.http_url}`
            : '',
          mode === 'local' && config.local?.http_port
            ? `-e OPENCORTEX_HTTP_PORT=${config.local.http_port}`
            : '',
        ].filter(Boolean).join(' ');

        const cmd = `claude mcp add -s user opencortex ${envArgs} -- node ${MCP_SERVER_PATH}`;
        console.log(`\nRunning: ${cmd}`);
        execSync(cmd, { stdio: 'inherit' });
        console.log('Registered successfully.');
      } catch {
        console.log('Failed to register. You can run the command manually later.');
      }
    }

    // 6. Summary
    console.log('\n=== Setup Complete ===');
    console.log(`  Mode:   ${mode}`);
    console.log(`  Server: ${httpUrl}`);
    console.log(`  Config: ${configPath}`);
    if (ok) {
      console.log('\nReady to use. Start a new Claude Code session to activate.\n');
    } else {
      console.log('\nServer not reachable yet. Start the server, then verify with:');
      console.log('  opencortex-cli health\n');
    }
  } finally {
    rl.close();
  }
}
