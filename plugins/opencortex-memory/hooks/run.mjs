#!/usr/bin/env node
import { readStdin, buildContext, output } from '../lib/common.mjs';

const HANDLERS = {
  'session-start':       () => import('./handlers/session-start.mjs'),
  'user-prompt-submit':  () => import('./handlers/user-prompt-submit.mjs'),
  'stop':                () => import('./handlers/stop.mjs'),
  'session-end':         () => import('./handlers/session-end.mjs'),
};

async function main() {
  const hookName = process.argv[2];
  if (!hookName || !HANDLERS[hookName]) {
    output({ error: `Unknown hook: ${hookName}` });
    process.exit(1);
  }

  try {
    const input = await readStdin();
    const ctx = buildContext(input);
    const mod = await HANDLERS[hookName]();
    const result = await mod.default(ctx);
    output(result || {});
  } catch (err) {
    process.stderr.write(`[opencortex-memory] ${hookName} error: ${err.message}\n`);
    output({});
  }
}

main();
