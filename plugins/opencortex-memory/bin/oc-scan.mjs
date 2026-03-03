#!/usr/bin/env node
/**
 * oc-scan.mjs — Deterministic file scanner for OpenCortex document import.
 * Pure Node.js, zero external dependencies.
 *
 * Usage: node oc-scan.mjs <directory> [--json]
 * Output: JSON to stdout with { items, source_path, scan_meta }
 */
import { execSync } from 'node:child_process';
import { readFileSync, statSync, readdirSync } from 'node:fs';
import { join, relative, extname, basename } from 'node:path';

const MAX_FILE_SIZE = 1024 * 1024; // 1 MB

const SUPPORTED_EXTS = new Set([
  '.md', '.mdx',
  '.py', '.js', '.mjs', '.ts', '.tsx', '.jsx',
  '.go', '.rs', '.java', '.c', '.cpp', '.h', '.hpp',
  '.rb', '.sh', '.yaml', '.yml', '.toml', '.json',
  '.css', '.html', '.txt', '.rst',
]);

const SKIP_DIRS = new Set([
  '.git', 'node_modules', '__pycache__', '.venv', 'venv',
  'dist', 'build', '.tox', '.mypy_cache', '.next', '.nuxt',
  'coverage', '.cache', '.turbo', '.claude',
]);

function detectGit(dir) {
  try {
    const toplevel = execSync('git rev-parse --show-toplevel', {
      cwd: dir, stdio: ['ignore', 'pipe', 'ignore'], encoding: 'utf-8',
    }).trim();
    return { hasGit: true, projectId: basename(toplevel) };
  } catch {
    return { hasGit: false, projectId: 'public' };
  }
}

function discoverFiles(dir) {
  // Try git ls-files first
  try {
    const output = execSync('git ls-files --cached --others --exclude-standard', {
      cwd: dir, stdio: ['ignore', 'pipe', 'ignore'], encoding: 'utf-8', maxBuffer: 10 * 1024 * 1024,
    });
    return output.trim().split('\n').filter(Boolean).map(f => join(dir, f));
  } catch {
    // Fallback: recursive walk
    return walkDir(dir);
  }
}

function walkDir(dir) {
  const results = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (SKIP_DIRS.has(entry.name)) continue;
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      results.push(...walkDir(full));
    } else if (entry.isFile()) {
      results.push(full);
    }
  }
  return results;
}

function fileType(ext) {
  if (['.md', '.mdx'].includes(ext)) return 'markdown';
  if (['.txt', '.rst'].includes(ext)) return 'text';
  return 'code';
}

// --- Main ---
const targetDir = process.argv[2];
if (!targetDir) {
  console.error('Usage: node oc-scan.mjs <directory>');
  process.exit(1);
}

const { hasGit, projectId } = detectGit(targetDir);
const files = discoverFiles(targetDir)
  .filter(f => {
    const ext = extname(f).toLowerCase();
    if (!SUPPORTED_EXTS.has(ext)) return false;
    try { return statSync(f).size <= MAX_FILE_SIZE; } catch { return false; }
  });

const items = files.map(f => {
  const relPath = relative(targetDir, f);
  const ext = extname(f).toLowerCase();
  const content = readFileSync(f, 'utf-8');
  return {
    content,
    category: 'documents',
    context_type: 'resource',
    meta: {
      source: 'scan',
      file_path: relPath,
      file_type: fileType(ext),
    },
  };
});

const output = {
  items,
  source_path: targetDir,
  scan_meta: {
    total_files: items.length,
    has_git: hasGit,
    project_id: projectId,
  },
};

console.log(JSON.stringify(output));
