import { readFileSync, writeFileSync, mkdirSync, existsSync, accessSync, constants } from 'node:fs';
import { join, dirname } from 'node:path';
import { homedir } from 'node:os';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

export const PLUGIN_ROOT = join(__dirname, '..');
export const PROJECT_DIR = process.env.CLAUDE_PROJECT_DIR || process.cwd();

const STATE_DIR = join(PROJECT_DIR, '.opencortex', 'memory');
export const STATE_FILE = join(STATE_DIR, 'session_state.json');

// ── stdin ──────────────────────────────────────────────────────────────
export async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  const raw = Buffer.concat(chunks).toString().trim();
  if (!raw) return {};
  try { return JSON.parse(raw); } catch { return {}; }
}

// ── stdout ─────────────────────────────────────────────────────────────
export function output(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n');
}

// ── config discovery ───────────────────────────────────────────────────
function findConfigFile() {
  const candidates = [
    join(PROJECT_DIR, 'opencortex.json'),
    join(PROJECT_DIR, '.opencortex.json'),
    join(homedir(), '.opencortex', 'opencortex.json'),
  ];
  for (const p of candidates) {
    if (existsSync(p)) return p;
  }
  return null;
}

let _projectConfig = undefined;
export function getProjectConfig() {
  if (_projectConfig !== undefined) return _projectConfig;
  const p = findConfigFile();
  if (!p) { _projectConfig = null; return null; }
  try { _projectConfig = JSON.parse(readFileSync(p, 'utf-8')); } catch { _projectConfig = null; }
  return _projectConfig;
}

export function getConfigPath() {
  return findConfigFile();
}

// ── plugin config (config.json) ────────────────────────────────────────
let _pluginConfig = undefined;
function loadPluginConfig() {
  if (_pluginConfig !== undefined) return _pluginConfig;
  const p = join(PLUGIN_ROOT, 'config.json');
  try { _pluginConfig = JSON.parse(readFileSync(p, 'utf-8')); } catch { _pluginConfig = {}; }
  return _pluginConfig;
}

export function getPluginConfig(dotKey, defaultVal = undefined) {
  const cfg = loadPluginConfig();
  const keys = dotKey.split('.');
  let cur = cfg;
  for (const k of keys) {
    if (cur == null || typeof cur !== 'object') return defaultVal;
    cur = cur[k];
  }
  return cur ?? defaultVal;
}

export function getPluginMode() {
  return getPluginConfig('mode', 'local');
}

export function getHttpUrl() {
  const mode = getPluginMode();
  if (mode === 'remote') return getPluginConfig('remote.http_url', 'http://127.0.0.1:8921');
  const port = getPluginConfig('local.http_port', 8921);
  return `http://127.0.0.1:${port}`;
}

// ── state file ─────────────────────────────────────────────────────────
export function ensureStateDir() {
  mkdirSync(STATE_DIR, { recursive: true });
}

export function loadState() {
  try { return JSON.parse(readFileSync(STATE_FILE, 'utf-8')); } catch { return null; }
}

export function saveState(state) {
  ensureStateDir();
  writeFileSync(STATE_FILE, JSON.stringify(state, null, 2) + '\n');
}

// ── python discovery (local mode server start) ─────────────────────────
export function findPython() {
  const candidates = process.platform === 'win32'
    ? [join(PROJECT_DIR, '.venv', 'Scripts', 'python.exe'), 'python3', 'python']
    : [join(PROJECT_DIR, '.venv', 'bin', 'python3'), 'python3', 'python'];
  for (const c of candidates) {
    try {
      if (c.includes('/') || c.includes('\\')) {
        accessSync(c, constants.X_OK);
        return c;
      }
      return c; // bare name — assume on PATH
    } catch { /* next */ }
  }
  return 'python3';
}

// ── build context ──────────────────────────────────────────────────────
export function buildContext(input) {
  return {
    input,
    pluginRoot: PLUGIN_ROOT,
    projectDir: PROJECT_DIR,
    stateDir: STATE_DIR,
    stateFile: STATE_FILE,
    configPath: getConfigPath(),
    mode: getPluginMode(),
    httpUrl: getHttpUrl(),
  };
}
