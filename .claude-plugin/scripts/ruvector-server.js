#!/usr/bin/env node
// SPDX-License-Identifier: Apache-2.0
/**
 * RuVector HTTP Server for OpenCortex.
 *
 * Wraps @ruvector/core VectorDB + SonaEngine as a standalone HTTP service.
 * Python adapter connects via HTTP — no subprocess management needed.
 *
 * Usage:
 *   npx ruvector-server              # or
 *   node data/ruvector-server.js      # default: port 6921, dim 1024
 *   node data/ruvector-server.js --port 8080 --dim 512 --data-dir ./mydata
 */

const http = require("http");
const fs = require("fs");
const path = require("path");

// ---------------------------------------------------------------------------
// Parse CLI args
// ---------------------------------------------------------------------------
const args = process.argv.slice(2);
function getArg(name, defaultVal) {
  const idx = args.indexOf(name);
  return idx >= 0 && idx + 1 < args.length ? args[idx + 1] : defaultVal;
}

const PORT = parseInt(getArg("--port", "6921"));
const DIMENSIONS = parseInt(getArg("--dim", "1024"));
const METRIC = getArg("--metric", "cosine");
const DATA_DIR = getArg("--data-dir", path.join(process.cwd(), "data", "ruvector"));
const SONA_DIM = parseInt(getArg("--sona-dim", "256"));

// ---------------------------------------------------------------------------
// Locate ruvector module (npx cache or local node_modules)
// ---------------------------------------------------------------------------
let VectorDB, SonaEngine;

function loadRuvector() {
  // Try local node_modules first
  const candidates = [
    "ruvector",
    path.join(process.env.HOME, ".npm/_npx/237f288c6e2d38aa/node_modules/ruvector"),
  ];

  for (const mod of candidates) {
    try {
      const dist = require(`${mod}/dist/index.js`);
      VectorDB = dist.VectorDB;
      const core = require(`${mod}/dist/core/index.js`);
      SonaEngine = core.SonaEngine;
      return mod;
    } catch (_) {}
  }
  throw new Error(
    "Cannot find ruvector module. Install with: npm install ruvector"
  );
}

const modPath = loadRuvector();
console.log(`[ruvector-server] Module loaded from: ${modPath}`);

// ---------------------------------------------------------------------------
// Initialise VectorDB + SONA
// ---------------------------------------------------------------------------
fs.mkdirSync(DATA_DIR, { recursive: true });

const db = new VectorDB({ dimensions: DIMENSIONS, metric: METRIC });
const dbFile = path.join(DATA_DIR, "vectors.json");

// Load existing data if present
if (fs.existsSync(dbFile)) {
  try {
    db.load(dbFile);
    console.log(`[ruvector-server] Loaded existing DB from ${dbFile}`);
  } catch (e) {
    console.warn(`[ruvector-server] Could not load ${dbFile}: ${e.message}`);
  }
}

let sona = null;
try {
  sona = new SonaEngine(SONA_DIM);
  console.log(`[ruvector-server] SONA engine ready (hiddenDim=${SONA_DIM})`);
} catch (e) {
  console.warn(`[ruvector-server] SONA init failed: ${e.message}`);
}

// In-memory SONA profiles (simple reward/decay model)
const sonaProfiles = {};

function getProfile(id) {
  if (!sonaProfiles[id]) {
    sonaProfiles[id] = {
      id,
      reward_score: 0.0,
      retrieval_count: 0,
      positive_feedback_count: 0,
      negative_feedback_count: 0,
      last_retrieved_at: 0,
      last_feedback_at: 0,
      effective_score: 1.0,
      is_protected: false,
    };
  }
  return sonaProfiles[id];
}

// Auto-save interval
let dirty = false;
setInterval(() => {
  if (dirty) {
    try {
      db.save(dbFile);
      dirty = false;
    } catch (_) {}
  }
}, 5000);

// ---------------------------------------------------------------------------
// HTTP request handler
// ---------------------------------------------------------------------------
async function readBody(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  const raw = Buffer.concat(chunks).toString();
  return raw ? JSON.parse(raw) : {};
}

function jsonResponse(res, status, data) {
  const body = JSON.stringify(data);
  res.writeHead(status, {
    "Content-Type": "application/json",
    "Content-Length": Buffer.byteLength(body),
  });
  res.end(body);
}

async function handleRequest(req, res) {
  const url = new URL(req.url, `http://localhost:${PORT}`);
  const route = url.pathname;
  const method = req.method;

  try {
    // Health
    if (route === "/health" && method === "GET") {
      return jsonResponse(res, 200, {
        status: "ok",
        backend: "ruvector",
        dimensions: DIMENSIONS,
        sona: sona !== null,
      });
    }

    // Insert
    if (route === "/insert" && method === "POST") {
      const body = await readBody(req);
      await db.insert({
        id: body.id,
        vector: body.vector,
        metadata: body.metadata || {},
      });
      dirty = true;
      return jsonResponse(res, 200, { id: body.id });
    }

    // Insert batch
    if (route === "/insert-batch" && method === "POST") {
      const body = await readBody(req);
      const entries = body.entries || [];
      for (const e of entries) {
        await db.insert({
          id: e.id,
          vector: e.vector,
          metadata: e.metadata || {},
        });
      }
      dirty = true;
      return jsonResponse(res, 200, {
        count: entries.length,
        ids: entries.map((e) => e.id),
      });
    }

    // Upsert
    if (route === "/upsert" && method === "POST") {
      const body = await readBody(req);
      // Delete then re-insert (upsert)
      try {
        await db.delete(body.id);
      } catch (_) {}
      await db.insert({
        id: body.id,
        vector: body.vector,
        metadata: body.metadata || {},
      });
      dirty = true;
      return jsonResponse(res, 200, { id: body.id });
    }

    // Get by ID
    if (route === "/get" && method === "POST") {
      const body = await readBody(req);
      const entry = await db.get(body.id);
      if (!entry) {
        return jsonResponse(res, 404, { error: "not found" });
      }
      return jsonResponse(res, 200, entry);
    }

    // Delete
    if (route === "/delete" && method === "POST") {
      const body = await readBody(req);
      try {
        await db.delete(body.id);
        dirty = true;
        return jsonResponse(res, 200, { deleted: true });
      } catch (_) {
        return jsonResponse(res, 200, { deleted: false });
      }
    }

    // Search
    if (route === "/search" && method === "POST") {
      const body = await readBody(req);
      const results = await db.search({
        vector: body.vector,
        k: body.top_k || 10,
      });

      // Apply metadata filter if provided
      let filtered = results;
      if (body.filter) {
        filtered = results.filter((r) => {
          const meta = r.metadata || {};
          for (const [key, val] of Object.entries(body.filter)) {
            if (meta[key] !== val) return false;
          }
          return true;
        });
      }

      // Convert distance to similarity: sim = 1.0 - distance
      // RuVector cosine distance is in [0, 2], similarity in [-1, 1]
      filtered = filtered.map((r) => ({
        ...r,
        similarity_score: 1.0 - r.score,
      }));

      // Apply SONA reinforcement scoring
      if (body.use_reinforcement) {
        filtered = filtered.map((r) => {
          const profile = getProfile(r.id);
          profile.retrieval_count++;
          profile.last_retrieved_at = Date.now() / 1000;
          return {
            ...r,
            reinforced_score:
              r.similarity_score * 0.7 + profile.effective_score * 0.3,
          };
        });
        filtered.sort((a, b) => b.reinforced_score - a.reinforced_score);
      } else {
        filtered.sort((a, b) => b.similarity_score - a.similarity_score);
      }

      return jsonResponse(res, 200, { results: filtered });
    }

    // Count
    if (route === "/count" && method === "GET") {
      const count = await db.len();
      return jsonResponse(res, 200, { count });
    }

    // Stats
    if (route === "/stats" && method === "GET") {
      const count = await db.len();
      return jsonResponse(res, 200, {
        total_entries: count,
        dimensions: DIMENSIONS,
        metric: METRIC,
        sona_enabled: sona !== null,
        profiles_count: Object.keys(sonaProfiles).length,
      });
    }

    // SONA: reward
    if (route === "/sona/reward" && method === "POST") {
      const body = await readBody(req);
      const profile = getProfile(body.id);
      profile.reward_score += body.reward;
      profile.last_feedback_at = Date.now() / 1000;
      if (body.reward > 0) {
        profile.positive_feedback_count++;
        profile.effective_score = Math.min(
          profile.effective_score + body.reward * 0.1,
          2.0
        );
      } else {
        profile.negative_feedback_count++;
        profile.effective_score = Math.max(
          profile.effective_score + body.reward * 0.1,
          0.01
        );
      }
      return jsonResponse(res, 200, { profile });
    }

    // SONA: reward batch
    if (route === "/sona/reward-batch" && method === "POST") {
      const body = await readBody(req);
      const rewards = body.rewards || [];
      for (const { id, reward } of rewards) {
        const profile = getProfile(id);
        profile.reward_score += reward;
        profile.last_feedback_at = Date.now() / 1000;
        if (reward > 0) {
          profile.positive_feedback_count++;
          profile.effective_score = Math.min(
            profile.effective_score + reward * 0.1,
            2.0
          );
        } else {
          profile.negative_feedback_count++;
          profile.effective_score = Math.max(
            profile.effective_score + reward * 0.1,
            0.01
          );
        }
      }
      return jsonResponse(res, 200, { updated: rewards.length });
    }

    // SONA: get profile
    if (route === "/sona/profile" && method === "POST") {
      const body = await readBody(req);
      const profile = getProfile(body.id);
      return jsonResponse(res, 200, profile);
    }

    // SONA: decay
    if (route === "/sona/decay" && method === "POST") {
      const body = await readBody(req);
      const decayRate = body.decay_rate || 0.95;
      const protectedDecayRate = body.protected_decay_rate || 0.99;
      const minScore = body.min_score || 0.01;

      let processed = 0;
      let decayed = 0;
      let belowThreshold = 0;

      for (const [id, profile] of Object.entries(sonaProfiles)) {
        processed++;
        const rate = profile.is_protected ? protectedDecayRate : decayRate;
        const oldScore = profile.effective_score;
        profile.effective_score *= rate;
        if (profile.effective_score < oldScore) decayed++;
        if (profile.effective_score < minScore) belowThreshold++;
      }

      return jsonResponse(res, 200, {
        records_processed: processed,
        records_decayed: decayed,
        records_below_threshold: belowThreshold,
        records_archived: 0,
      });
    }

    // SONA: set protected
    if (route === "/sona/protect" && method === "POST") {
      const body = await readBody(req);
      const profile = getProfile(body.id);
      profile.is_protected = body.protected !== false;
      return jsonResponse(res, 200, { profile });
    }

    // 404
    jsonResponse(res, 404, { error: `Unknown route: ${method} ${route}` });
  } catch (err) {
    console.error(`[ruvector-server] Error: ${err.message}`);
    jsonResponse(res, 500, { error: err.message });
  }
}

// ---------------------------------------------------------------------------
// Start server
// ---------------------------------------------------------------------------
const server = http.createServer(handleRequest);
server.listen(PORT, "127.0.0.1", () => {
  console.log(`[ruvector-server] Listening on http://127.0.0.1:${PORT}`);
  console.log(`[ruvector-server] Dimensions: ${DIMENSIONS}, Metric: ${METRIC}`);
  console.log(`[ruvector-server] Data dir: ${DATA_DIR}`);
});

// Graceful shutdown
process.on("SIGINT", () => {
  console.log("\n[ruvector-server] Shutting down...");
  try {
    db.save(dbFile);
    console.log("[ruvector-server] Data saved.");
  } catch (_) {}
  process.exit(0);
});

process.on("SIGTERM", () => {
  try {
    db.save(dbFile);
  } catch (_) {}
  process.exit(0);
});
