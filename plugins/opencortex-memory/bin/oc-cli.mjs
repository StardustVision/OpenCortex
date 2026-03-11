#!/usr/bin/env node
import { parseArgs } from 'node:util';
import { getHttpUrl } from '../lib/common.mjs';
import { httpPost, httpGet, healthCheck } from '../lib/http-client.mjs';

const USAGE = `Usage: oc-cli.mjs <command> [options]

Commands:
  health              Check server health
  status              Show server status (via HTTP API)
  recall <query>      Search memories
  store <text>        Store a memory
  stats               Show memory statistics
  feedback <uri> <reward>
                     Apply reward feedback to a memory
  decay               Apply reward decay to all memories
  context-recall <query>
                     Test recall via /api/v1/context (prepare phase)
  context-commit <user_msg> <assistant_msg>
                     Test commit via /api/v1/context (commit phase)
  context-end         Test end via /api/v1/context (end phase)

Options:
  --top-k, -k <n>    Number of results for recall (default: 5)
  --category, -c <s>  Category for store
  --session-id, -s <s> Session ID for context commands (default: auto-generated)
  --help, -h          Show this help
`;

async function main() {
  const { values, positionals } = parseArgs({
    allowPositionals: true,
    options: {
      'top-k':      { type: 'string', short: 'k', default: '5' },
      'category':   { type: 'string', short: 'c', default: '' },
      'session-id': { type: 'string', short: 's', default: '' },
      'help':       { type: 'boolean', short: 'h', default: false },
    },
  });

  if (values.help || positionals.length === 0) {
    process.stdout.write(USAGE);
    process.exit(0);
  }

  const cmd = positionals[0];
  const httpUrl = getHttpUrl();
  const sessionId = values['session-id'] || `cli_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;

  switch (cmd) {
    case 'health': {
      const ok = await healthCheck(httpUrl);
      console.log(ok ? 'OK' : 'UNREACHABLE');
      process.exit(ok ? 0 : 1);
    }

    case 'status': {
      const data = await httpGet(`${httpUrl}/api/v1/system/status?type=health`);
      console.log(JSON.stringify(data, null, 2));
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

    case 'context-recall': {
      const query = positionals.slice(1).join(' ');
      if (!query) { console.error('Usage: oc-cli.mjs context-recall <query>'); process.exit(1); }
      const data = await httpPost(`${httpUrl}/api/v1/context`, {
        session_id: sessionId,
        phase: 'prepare',
        turn_id: 't1',
        messages: [{ role: 'user', content: query }],
        config: { max_items: parseInt(values['top-k'], 10) || 5 },
      });
      console.log(JSON.stringify(data, null, 2));
      break;
    }

    case 'context-commit': {
      const userMsg = positionals[1];
      const assistantMsg = positionals[2];
      if (!userMsg || !assistantMsg) {
        console.error('Usage: oc-cli.mjs context-commit <user_msg> <assistant_msg>');
        process.exit(1);
      }
      const data = await httpPost(`${httpUrl}/api/v1/context`, {
        session_id: sessionId,
        phase: 'commit',
        turn_id: 't1',
        messages: [
          { role: 'user', content: userMsg },
          { role: 'assistant', content: assistantMsg },
        ],
      });
      console.log(JSON.stringify(data, null, 2));
      break;
    }

    case 'context-end': {
      const data = await httpPost(`${httpUrl}/api/v1/context`, {
        session_id: sessionId,
        phase: 'end',
      });
      console.log(JSON.stringify(data, null, 2));
      break;
    }

    default:
      console.error(`Unknown command: ${cmd}\n`);
      process.stdout.write(USAGE);
      process.exit(1);
  }
}

main().catch(err => {
  console.error(`Error: ${err.message}`);
  process.exit(1);
});
