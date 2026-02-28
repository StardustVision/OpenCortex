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

// ── Default MCP config ──────────────────────────────────────────────────
const DEFAULT_MCP_CONFIG = {
  mode: 'local',
  tenant_id: 'default',
  user_id: 'default',
  local: { http_port: 8921 },
  remote: { http_url: 'http://127.0.0.1:8921' },
};

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

// ── MCP config discovery ──────────────────────────────────────────────
/**
 * Search order:
 *   CWD/mcp.json → CWD/opencortex.json → CWD/.opencortex.json
 *   → $HOME/.opencortex/mcp.json → $HOME/.opencortex/opencortex.json
 */
function findMcpConfig() {
  const candidates = [
    join(PROJECT_DIR, 'mcp.json'),
    join(PROJECT_DIR, 'opencortex.json'),
    join(PROJECT_DIR, '.opencortex.json'),
    join(homedir(), '.opencortex', 'mcp.json'),
    join(homedir(), '.opencortex', 'opencortex.json'),
  ];
  for (const p of candidates) {
    if (existsSync(p)) return p;
  }
  return null;
}

/**
 * Migrate legacy $HOME/.opencortex/opencortex.json → mcp.json
 * Extracts MCP-related fields from the legacy config.
 */
function _migrateLegacyConfig(legacyData) {
  const mcp = { ...DEFAULT_MCP_CONFIG };
  // Direct fields
  if (legacyData.tenant_id) mcp.tenant_id = legacyData.tenant_id;
  if (legacyData.user_id) mcp.user_id = legacyData.user_id;
  if (legacyData.mcp_mode) mcp.mode = legacyData.mcp_mode;
  // Port from various sources
  const port = legacyData.mcp_port || legacyData.http_server_port;
  if (port) mcp.local.http_port = port;
  // Remote URL
  if (legacyData.http_server_host || legacyData.http_server_port) {
    const host = legacyData.http_server_host || '127.0.0.1';
    const p = legacyData.http_server_port || 8921;
    mcp.remote.http_url = `http://${host}:${p}`;
  }
  return mcp;
}

/**
 * Ensure $HOME/.opencortex/mcp.json exists.
 * If not, attempt migration from legacy opencortex.json, otherwise create defaults.
 */
export function ensureDefaultConfig() {
  const configDir = join(homedir(), '.opencortex');
  const mcpPath = join(configDir, 'mcp.json');

  if (existsSync(mcpPath)) return mcpPath;

  mkdirSync(configDir, { recursive: true });

  // Check for legacy opencortex.json to migrate from
  const legacyPath = join(configDir, 'opencortex.json');
  let mcpData = DEFAULT_MCP_CONFIG;

  if (existsSync(legacyPath)) {
    try {
      const legacy = JSON.parse(readFileSync(legacyPath, 'utf-8'));
      mcpData = _migrateLegacyConfig(legacy);
    } catch {
      // Fall through to defaults
    }
  }

  writeFileSync(mcpPath, JSON.stringify(mcpData, null, 2) + '\n');
  _mcpConfig = undefined; // Invalidate cache so next getMcpConfig() reads the new file
  return mcpPath;
}

// ── Cached MCP config ─────────────────────────────────────────────────
let _mcpConfig = undefined;

function _loadMcpConfig() {
  if (_mcpConfig !== undefined) return _mcpConfig;
  const p = findMcpConfig();
  if (!p) { _mcpConfig = { ...DEFAULT_MCP_CONFIG }; return _mcpConfig; }
  try {
    const raw = JSON.parse(readFileSync(p, 'utf-8'));
    _mcpConfig = { ...DEFAULT_MCP_CONFIG, ...raw };
    // Merge nested objects
    if (raw.local) _mcpConfig.local = { ...DEFAULT_MCP_CONFIG.local, ...raw.local };
    if (raw.remote) _mcpConfig.remote = { ...DEFAULT_MCP_CONFIG.remote, ...raw.remote };
  } catch {
    _mcpConfig = { ...DEFAULT_MCP_CONFIG };
  }
  // Apply environment variable overrides
  _applyEnvOverrides(_mcpConfig);
  return _mcpConfig;
}

/**
 * Apply OPENCORTEX_* environment variable overrides to MCP config.
 */
function _applyEnvOverrides(cfg) {
  const env = process.env;
  if (env.OPENCORTEX_TENANT_ID) cfg.tenant_id = env.OPENCORTEX_TENANT_ID;
  if (env.OPENCORTEX_USER_ID) cfg.user_id = env.OPENCORTEX_USER_ID;
  if (env.OPENCORTEX_MODE) cfg.mode = env.OPENCORTEX_MODE;
  if (env.OPENCORTEX_HTTP_PORT) cfg.local.http_port = parseInt(env.OPENCORTEX_HTTP_PORT, 10);
  if (env.OPENCORTEX_HTTP_URL) cfg.remote.http_url = env.OPENCORTEX_HTTP_URL;
}

/**
 * Get a value from the MCP config using dot-notation key.
 * Falls back to defaultVal if not found.
 */
export function getMcpConfig(dotKey, defaultVal = undefined) {
  const cfg = _loadMcpConfig();
  const keys = dotKey.split('.');
  let cur = cfg;
  for (const k of keys) {
    if (cur == null || typeof cur !== 'object') return defaultVal;
    cur = cur[k];
  }
  return cur ?? defaultVal;
}

// ── Backward-compatible aliases ─────────────────────────────────────────
// These delegate to the MCP config so existing callers keep working.
export function getPluginConfig(dotKey, defaultVal = undefined) {
  return getMcpConfig(dotKey, defaultVal);
}

export function getPluginMode() {
  return getMcpConfig('mode', 'local');
}

export function getConfigPath() {
  return findMcpConfig();
}

// Legacy getProjectConfig — now returns MCP config (tenant_id/user_id used for headers)
let _projectConfig = undefined;
export function getProjectConfig() {
  if (_projectConfig !== undefined) return _projectConfig;
  const p = findMcpConfig();
  if (!p) { _projectConfig = null; return null; }
  try { _projectConfig = JSON.parse(readFileSync(p, 'utf-8')); } catch { _projectConfig = null; }
  return _projectConfig;
}

// ── HTTP URL ────────────────────────────────────────────────────────────
export function getHttpUrl() {
  const mode = getMcpConfig('mode', 'local');
  if (mode === 'remote') return getMcpConfig('remote.http_url', 'http://127.0.0.1:8921');
  const port = getMcpConfig('local.http_port', 8921);
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
