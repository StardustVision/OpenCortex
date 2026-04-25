# Changelog

All notable changes to OpenCortex are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) loosely, and dates
are ISO-8601 in UTC.

Versions before this file existed are reconstructed from project memory and
git history; treat them as best-effort summaries rather than authoritative
records.

## Unreleased

### Changed

- **Benchmark offline conversation ingest endpoint moved under the admin
  namespace.** The endpoint `POST /api/v1/benchmark/conversation_ingest`
  is now `POST /api/v1/admin/benchmark/conversation_ingest` and requires
  an admin role JWT (`_require_admin()`). The legacy URL returns
  **HTTP 410 Gone** with a JSON body pointing at the new path; the
  shim is scheduled for removal in v0.8.0. Callers using the bundled
  `benchmarks/oc_client.py` were updated automatically — only out-of-tree
  consumers need to update their URL and ensure their token carries the
  admin role.

- **Admin route error envelopes standardized on `{"detail": ...}`.** The
  pre-existing `bench-collections` admin endpoints previously returned
  `{"error": ...}`; they now return `{"detail": ...}` to match every
  other admin route (403 from `_require_admin`, 409 from transcript hash
  conflict, 504 from server-side timeout). FastAPI's default exception
  envelope is the canonical shape across the admin surface.

- **`_hash_transcript` canonicalizes list ordering for benchmark
  transcript hashing.** Previously, two transcripts that differed only
  in the ordering of list values inside `meta` (e.g., reordered
  `time_refs`) hashed to different SHA-256 digests, causing benign
  benchmark replays to receive a false **HTTP 409 Conflict**. The hash
  function now sorts lists of primitives recursively in the meta values
  before serialization. Lists of dicts (e.g. `tool_calls`) keep their
  original order — sequence is treated as semantic for those.

  **Migration note:** Source records ingested before this release used
  the old hash algorithm. The first benchmark replay of any pre-existing
  session will see a hash mismatch and receive 409. To resolve, either
  delete the source record (`opencortex://{tenant}/{user}/session/conversations/{session_id}/source`)
  or rotate the `session_id`. Subsequent replays follow the new
  canonical-form hash and remain idempotent.
