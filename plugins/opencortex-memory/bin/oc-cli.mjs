#!/usr/bin/env node
import { parseArgs } from 'node:util';
import { getHttpUrl, loadState } from '../lib/common.mjs';
import { httpPost, httpGet, healthCheck } from '../lib/http-client.mjs';

const USAGE = `Usage: oc-cli.mjs <command> [options]

Commands:
  health              Check server health
  status              Show session state
  recall <query>      Search memories
  store <text>        Store a memory

Options:
  --top-k, -k <n>    Number of results for recall (default: 5)
  --category, -c <s>  Category for store
  --help, -h          Show this help
`;

async function main() {
  const { values, positionals } = parseArgs({
    allowPositionals: true,
    options: {
      'top-k':    { type: 'string', short: 'k', default: '5' },
      'category': { type: 'string', short: 'c', default: '' },
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
