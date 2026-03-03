#!/usr/bin/env node
import { parseArgs } from 'node:util';
import { spawn } from 'node:child_process';
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { getHttpUrl, loadState } from '../lib/common.mjs';
import { httpPost, httpGet, healthCheck } from '../lib/http-client.mjs';

const USAGE = `Usage: oc-cli.mjs <command> [options]

Commands:
  health              Check server health
  status              Show session state
  recall <query>      Search memories
  store <text>        Store a memory
  stats               Show memory statistics
  feedback <uri> <reward>
                     Apply reward feedback to a memory
  decay               Apply reward decay to all memories
  hook <name>         Trigger one hook (session-start|user-prompt-submit|stop|session-end)
  simulate            Trigger full hook lifecycle in order

Options:
  --top-k, -k <n>    Number of results for recall (default: 5)
  --category, -c <s>  Category for store
  --prompt, -p <s>    Prompt used by user-prompt-submit (default: "Help me debug memory recall behavior.")
  --transcript <path> Transcript JSONL for stop hook (default: auto-generated mock transcript)
  --project-dir <p>   Project dir used as hook working directory (default: cwd)
  --help, -h          Show this help
`;

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const PLUGIN_ROOT = join(__dirname, '..');
const RUNNER = join(PLUGIN_ROOT, 'hooks', 'run.mjs');
const HOOKS = new Set(['session-start', 'user-prompt-submit', 'stop', 'session-end']);

async function main() {
  const { values, positionals } = parseArgs({
    allowPositionals: true,
    options: {
      'top-k':    { type: 'string', short: 'k', default: '5' },
      'category': { type: 'string', short: 'c', default: '' },
      'prompt':   { type: 'string', short: 'p', default: 'Help me debug memory recall behavior.' },
      'transcript': { type: 'string', default: '' },
      'project-dir': { type: 'string', default: process.cwd() },
      'help':     { type: 'boolean', short: 'h', default: false },
    },
  });

  if (values.help || positionals.length === 0) {
    process.stdout.write(USAGE);
    process.exit(0);
  }

  const cmd = positionals[0];
  const httpUrl = getHttpUrl();

  switch (cmd) {
    case 'health': {
      const ok = await healthCheck(httpUrl);
      console.log(ok ? 'OK' : 'UNREACHABLE');
      process.exit(ok ? 0 : 1);
    }

    case 'status': {
      const state = loadState();
      if (!state) {
        console.log('No active session.');
        process.exit(0);
      }
      console.log(JSON.stringify(state, null, 2));
      break;
    }

    case 'recall': {
      const query = positionals.slice(1).join(' ');
      if (!query) { console.error('Usage: oc-cli.mjs recall <query>'); process.exit(1); }
      const topK = parseInt(values['top-k'], 10) || 5;
      const data = await httpPost(`${httpUrl}/api/v1/memory/search`, { query, limit: topK });
      if (data.results && data.results.length) {
        for (const r of data.results) {
          console.log(`[${(r.score ?? 0).toFixed(3)}] ${r.abstract || r.uri || '(no title)'}`);
          if (r.content) console.log(`  ${r.content.slice(0, 200)}`);
          console.log();
        }
      } else {
        console.log('No results.');
      }
      break;
    }

    case 'store': {
      const text = positionals.slice(1).join(' ');
      if (!text) { console.error('Usage: oc-cli.mjs store <text>'); process.exit(1); }
      const payload = {
        abstract: text.slice(0, 200),
        content: text,
        context_type: 'memory',
      };
      if (values.category) payload.category = values.category;
      const data = await httpPost(`${httpUrl}/api/v1/memory/store`, payload);
      console.log('Stored:', data.uri || 'ok');
      break;
    }

    case 'stats': {
      const data = await httpGet(`${httpUrl}/api/v1/memory/stats`);
      console.log(JSON.stringify(data, null, 2));
      break;
    }

    case 'feedback': {
      const uri = positionals[1];
      const rewardRaw = positionals[2];
      const reward = Number.parseFloat(rewardRaw);
      if (!uri || Number.isNaN(reward)) {
        console.error('Usage: oc-cli.mjs feedback <uri> <reward>');
        process.exit(1);
      }
      const data = await httpPost(`${httpUrl}/api/v1/memory/feedback`, { uri, reward });
      console.log(JSON.stringify(data, null, 2));
      break;
    }

    case 'decay': {
      const data = await httpPost(`${httpUrl}/api/v1/memory/decay`, {});
      console.log(JSON.stringify(data, null, 2));
      break;
    }

    case 'hook': {
      const hook = positionals[1];
      if (!HOOKS.has(hook)) {
        console.error('Usage: oc-cli.mjs hook <session-start|user-prompt-submit|stop|session-end>');
        process.exit(1);
      }
      const projectDir = resolve(values['project-dir']);
      const payload = await payloadForHook(hook, values.prompt, values.transcript, projectDir);
      const result = await runHook(hook, payload, projectDir);
      process.stdout.write(result.stdout);
      process.stderr.write(result.stderr);
      process.exit(result.code);
      break;
    }

    case 'simulate': {
      const projectDir = resolve(values['project-dir']);
      const order = ['session-start', 'user-prompt-submit', 'stop', 'session-end'];
      let code = 0;
      for (const hook of order) {
        const payload = await payloadForHook(hook, values.prompt, values.transcript, projectDir);
        const result = await runHook(hook, payload, projectDir);
        console.log(`== ${hook} ==`);
        process.stdout.write(result.stdout);
        process.stderr.write(result.stderr);
        if (result.code !== 0 && code === 0) code = result.code;
        if (result.stdout && !result.stdout.endsWith('\n')) console.log();
      }
      process.exit(code);
      break;
    }

    default:
      console.error(`Unknown command: ${cmd}\n`);
      process.stdout.write(USAGE);
      process.exit(1);
  }
}

async function payloadForHook(hook, prompt, transcriptOpt, projectDir) {
  if (hook === 'user-prompt-submit') return { prompt };
  if (hook === 'stop') {
    const transcriptPath = transcriptOpt || makeMockTranscript(projectDir, prompt);
    return { transcript_path: transcriptPath };
  }
  return {};
}

function makeMockTranscript(projectDir, prompt) {
  const dir = join(projectDir, '.opencortex', 'memory');
  mkdirSync(dir, { recursive: true });
  const path = join(dir, 'mock_transcript.jsonl');
  const user = JSON.stringify({
    role: 'user',
    uuid: `user-${randomUUID()}`,
    content: [{ type: 'text', text: prompt }],
  });
  const assistant = JSON.stringify({
    role: 'assistant',
    uuid: `assistant-${randomUUID()}`,
    content: [{ type: 'text', text: 'This is a simulated assistant response.' }],
  });
  writeFileSync(path, `${user}\n${assistant}\n`, 'utf-8');
  return path;
}

function runHook(hook, payload, projectDir) {
  return new Promise((resolvePromise) => {
    const child = spawn('node', [RUNNER, hook], {
      cwd: projectDir,
      env: process.env,
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (d) => { stdout += d.toString(); });
    child.stderr.on('data', (d) => { stderr += d.toString(); });
    child.on('close', (code) => {
      resolvePromise({ code: code ?? 1, stdout, stderr });
    });
    child.stdin.write(JSON.stringify(payload));
    child.stdin.end();
  });
}

main().catch(err => {
  console.error(`Error: ${err.message}`);
  process.exit(1);
});
