# Prompt: Build OpenCortex Memory Console

You are building a developer management panel called **OpenCortex Memory Console** — a React + Tailwind SPA for managing an AI memory system. The backend already exists (FastAPI on `http://localhost:8921`). You need to build only the frontend.

## Project Setup

Create a Vite + React + TypeScript project in a `web/` directory with these dependencies:

```json
{
  "dependencies": {
    "react": "^18",
    "react-dom": "^18",
    "react-router-dom": "^6",
    "recharts": "^2",
    "lucide-react": "latest"
  },
  "devDependencies": {
    "@types/react": "^18",
    "@types/react-dom": "^18",
    "@vitejs/plugin-react": "^4",
    "autoprefixer": "^10",
    "postcss": "^8",
    "tailwindcss": "^3",
    "typescript": "^5",
    "vite": "^5"
  }
}
```

Vite config should proxy `/api` to `http://localhost:8921` in dev mode.

## Design Language

- **Style**: Modern minimalist, similar to Linear/Notion. Light theme, clean typography, generous whitespace, subtle borders (`border-gray-200`), no heavy shadows.
- **Accent color**: Indigo (`indigo-500` / `indigo-600`)
- **Font**: System font stack (Inter if available)
- **Cards**: `bg-white rounded-lg border border-gray-200 p-6`
- **Badges**: Small rounded pills with soft background colors per category
- **Buttons**: Primary (indigo), Danger (red-500), Ghost (transparent hover:bg-gray-100)
- **Tables**: Clean, no heavy borders, alternating row hover
- **Empty states**: Centered icon + message + optional action button
- **Responsive**: Desktop-first, min-width 1024px

## File Structure

```
web/src/
  main.tsx                    # ReactDOM.createRoot, BrowserRouter
  App.tsx                     # Routes definition
  api/
    client.ts                 # OpenCortexClient class (fetch wrapper)
    types.ts                  # All TypeScript interfaces
  components/
    layout/
      Sidebar.tsx             # Left nav sidebar
      PageLayout.tsx          # Page wrapper with header
    common/
      Badge.tsx               # Category/type badge
      Button.tsx              # Primary/Danger/Ghost variants
      Card.tsx                # White card container
      Modal.tsx               # Confirmation modal
      EmptyState.tsx          # Empty state placeholder
      SearchInput.tsx         # Debounced search input
      ScoreBar.tsx            # Horizontal score bar (0-1 range)
      JsonViewer.tsx          # Collapsible JSON tree
      LoadingSpinner.tsx
  pages/
    Dashboard.tsx
    Memories.tsx
    Knowledge.tsx
    SearchDebug.tsx
    System.tsx
    Skills.tsx                # Coming Soon placeholder
  hooks/
    useApi.ts                 # Generic async hook { data, loading, error, refetch }
    useDebounce.ts            # Debounce hook (300ms)
```

## Authentication

All API calls need `Authorization: Bearer <JWT>` header.

Token resolution order:
1. URL query param `?token=xxx` → save to localStorage
2. `localStorage.getItem('opencortex_token')`
3. If no token found → show a simple token input page (text input + "Connect" button)

In `client.ts`:

```typescript
class OpenCortexClient {
  private baseUrl: string;
  private token: string;

  constructor(baseUrl: string, token: string) {
    this.baseUrl = baseUrl;
    this.token = token;
  }

  private async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const res = await fetch(`${this.baseUrl}${path}`, {
      method,
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${this.token}`,
      },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) throw new Error(`API error: ${res.status}`);
    return res.json();
  }

  // Methods listed in API Reference section below
}
```

Provide the client via React Context so all pages can access it.

## Navigation — Left Sidebar

Collapsed by default (icon-only, 64px wide). Click to expand (220px with labels). Persists state in localStorage.

Items (use Lucide icons):

| Icon | Label | Route | Status |
|------|-------|-------|--------|
| `LayoutDashboard` | Dashboard | `/` | Active |
| `Brain` | Memories | `/memories` | Active |
| `BookOpen` | Knowledge | `/knowledge` | Active |
| `SearchCode` | Search Debug | `/search-debug` | Active |
| `Settings` | System | `/system` | Active |
| `Sparkles` | Skills | `/skills` | Coming Soon (dimmed, tooltip "Coming Soon") |

Active route should have `bg-indigo-50 text-indigo-600` highlight.

## Page Specifications

### Page 1: Dashboard (`/`)

A system overview page.

**Health Status Bar** (top, full width):
- Horizontal strip with 3 status indicators: Storage, Embedder, LLM
- Each: colored dot (green=healthy / red=error / yellow=degraded) + component name
- API: `GET /api/v1/system/status?type=health`
- Response shape: `{ initialized: boolean, storage: boolean, embedder: boolean, llm: boolean }`

**Stat Cards** (2×2 grid below health bar):

Card 1 — **Total Records**: `storage.total_records` large number display.
Card 2 — **Tenant / User**: `tenant_id` and `user_id`.
Card 3 — **Embedder**: `embedder` string and `has_llm` status.
Card 4 — **Rerank**: `rerank.enabled`, `rerank.mode`, `rerank.model`, `rerank.fusion_beta`.

- API: `GET /api/v1/memory/stats`

**Recent Memories** (below cards, table):
- Columns: Abstract (truncated 80 chars), Category (badge), Type (badge), Created
- 10 rows, click row → navigate to `/memories?uri=<uri>`
- API: `GET /api/v1/memory/list?limit=10`

### Page 2: Memories (`/memories`)

Master-detail layout. Left panel (40% width, scrollable) + Right panel (60% width).

**Left Panel**:

- **Search Input** (top): Placeholder "Search memories...". Debounce 300ms.
  - When has text: `POST /api/v1/memory/search { query, limit: 20, context_type?, category?, detail_level }`
  - When empty: `GET /api/v1/memory/list?limit=20&offset=0&context_type=&category=`

- **Filter Row** (below search):
  - `context_type` select: All / memory / resource / skill / case / pattern
  - `category` select: free-form options based on common values (`profile`, `preferences`, `entities`, `events`, `cases`, `patterns`, `error_fixes`, `workflows`, `strategies`, `documents`, `plans`) but do not assume the list is exhaustive
  - `detail_level` toggle for search requests only: L0 / L1 / L2

- **Memory List**: Vertical scrollable list of cards:
  - Each card: abstract (2 lines truncated), category badge (colored), type badge, score if from search (gray text `score: 0.85`)
  - Selected card: `border-indigo-500 bg-indigo-50`
  - Load more button at bottom (increment offset by 20 for list mode only)

- URL param support: if `?uri=xxx` is present, auto-select that memory

**Right Panel** (when memory selected):

- **Header**: Full abstract text (large font), URI in monospace with copy button

- **Content Tabs**: Three tabs — Abstract (L0) | Overview (L1) | Full Content (L2)
  - If selected from search response, use `abstract` and `overview` from the response when available
  - If selected from list response, lazy-load L0/L1 via `GET /api/v1/content/abstract?uri=<uri>` and `GET /api/v1/content/overview?uri=<uri>`
  - L2 is lazy-loaded via `GET /api/v1/content/read?uri=<uri>`
  - All content endpoints return `{ status: "ok", result: string }`
  - Content rendered as markdown-like text (preserve newlines, code blocks)

- **Metadata** (collapsible section):
  - Show the selected record JSON from `/api/v1/memory/list` or `/api/v1/memory/search`
  - Prefer showing: `category`, `context_type`, `scope`, `project_id`, `created_at`, `updated_at`, `uri`

- **Actions** (bottom of right panel):
  - Row of buttons:
    - 👍 `+1` (green ghost button) → `POST /api/v1/memory/feedback { uri, reward: 1.0 }`
    - 👎 `-1` (red ghost button) → `POST /api/v1/memory/feedback { uri, reward: -1.0 }`
    - 🗑️ Delete (red) → opens confirmation Modal → `POST /api/v1/memory/forget { uri }`
  - After any action, refetch the current list and refresh the selected item state if it still exists

**Empty State** (right panel, no selection): Centered Brain icon + "Select a memory to view details"

### Page 3: Knowledge (`/knowledge`)

Two tabs at top: **Candidates** | **Approved Knowledge**

**Tab: Candidates**:

- **Archivist Status Bar** (top card):
  - Left: `enabled`, `running`, `trigger_mode`, `trigger_threshold`, `last_run_at`
  - Right: "Trigger Extraction" button (indigo) → `POST /api/v1/archivist/trigger`
  - API: `GET /api/v1/archivist/status`
  - If API returns `{ error: "feature disabled" }`, show a non-error empty/admin-disabled state instead of a broken screen

- **Candidates Table**:
  - Columns: Abstract, Type, Scope, Status, Created, Actions
  - API: `GET /api/v1/knowledge/candidates`
  - Actions per row:
    - ✅ Approve (green button) → `POST /api/v1/knowledge/approve { knowledge_id }`
    - ❌ Reject (red button) → `POST /api/v1/knowledge/reject { knowledge_id }`
  - Expandable row: click to expand and show `overview` plus `source_trace_ids` when present
  - Empty state: "No pending candidates" with BookOpen icon

**Tab: Approved Knowledge**:

- Search input at top → `POST /api/v1/knowledge/search { query, limit: 20, types?: string[] }`
- Results displayed as cards similar to Memories page
- Each card: `abstract`, `knowledge_type` badge, `scope` badge, optional `source_trace_ids`
- If API returns `{ error: "feature disabled" }`, show a disabled placeholder state

### Page 4: Search Debug (`/search-debug`)

A tool for visualizing search quality.

**Query Section** (top card):
- Large textarea for search query (3 rows)
- Options row:
  - Limit: number input (1-20, default 5)
- "Run Search" button (indigo, large)
- API: `POST /api/v1/admin/search_debug { query, limit }`
- Show top-level debug metadata from the response: `query`, `fusion_beta`, `rerank_mode`

**Results Section** (below, one card per result):

Each result card contains:

- **Header**: Rank number (#1, #2...), abstract text, URI in monospace
- **Score Breakdown** (horizontal grouped bar chart using Recharts BarChart):
  - 3 bars per result:
    - `raw_vector_score` — blue (#6366f1)
    - `rerank_score` — green (#10b981)
    - `fused_score` — purple (#8b5cf6)
  - Each bar: render using the numeric range returned by the API
  - Show exact values as labels on bars

- **Score Formula** (small gray text): if rerank is enabled, `fused = fusion_beta × rerank + (1 - fusion_beta) × raw_vector`; otherwise `fused = raw_vector`

**Score Comparison Chart** (at the very top of results, before individual cards):
- Single BarChart showing all results' `fused_score` as horizontal bars, sorted descending
- Each bar labeled with the abstract (truncated 40 chars)
- This gives an at-a-glance view of score distribution

**Empty State**: "Enter a query to debug search results" with SearchCode icon

### Page 5: System (`/system`)

**Doctor Report** (top section):
- Card with structured diagnostic output
- API: `GET /api/v1/system/status?type=doctor`
- Display each component as a sub-card: name, status (green/red dot), details

**Storage Stats** (middle section):
- Card showing collection info, record counts, storage sizes
- API: `GET /api/v1/system/status?type=stats`
- Display as a clean key-value table

**Admin Operations** (bottom section, with red left border to indicate danger zone):
- Title: "Danger Zone"
- **Re-embed All Records**:
  - Description: "Re-embed all records with the current embedding model. This is a long-running operation."
  - Button: "Re-embed All" (red outline)
  - Click → Modal confirmation → `POST /api/v1/admin/reembed`
  - Show loading state during operation

- **Apply Decay**:
  - Description: "Apply time-decay to all memory scores. Protected memories decay slower."
  - Button: "Run Decay" (red outline)
  - Click → Modal confirmation → `POST /api/v1/memory/decay`

### Page 6: Skills (`/skills`) — Coming Soon

- Centered layout
- `Sparkles` icon (48px, indigo-400)
- Title: "Skills Management"
- Subtitle: "Automatic skill discovery, extraction, and management will be available in a future release."
- Subtle animated gradient border or pulse on the icon (optional, tasteful)
- Consistent with the rest of the console's visual language

## API Reference

All endpoints are relative to the backend base URL. All POST bodies are JSON. All responses are JSON.

### Memory

```
POST /api/v1/memory/store
  Body: { abstract: string, content?: string, category?: string, context_type?: "memory"|"resource"|"skill"|"case"|"pattern", meta?: object, dedup?: boolean }
  Response: { uri: string, context_type: string, category: string, abstract: string, dedup_action?: string }

POST /api/v1/memory/batch_store
  Body: { items: [{ content: string, category?: string, context_type?: string, meta?: object }], source_path?: string, scan_meta?: object }
  Response: { stored: number, skipped: number, errors: string[] }

POST /api/v1/memory/search
  Body: { query: string, limit?: number, context_type?: string, category?: string, detail_level?: "l0"|"l1"|"l2" }
  Response: { results: [{ uri, abstract, overview?, content?, context_type, score, keywords? }], total: number, search_intent? }

GET /api/v1/memory/list?category=&context_type=&limit=20&offset=0
  Response: { results: [{ uri, abstract, category, context_type, scope, project_id, updated_at, created_at }], total: number }
  Note: `total` is the number of items returned in the current page, not a global total count.

POST /api/v1/memory/forget
  Body: { uri?: string, query?: string }
  Response: { status: string, forgotten: number, uri?: string }

POST /api/v1/memory/feedback
  Body: { uri: string, reward: number }
  Response: { status: string, uri: string, reward: string }

POST /api/v1/memory/decay
  Response: { records_processed, records_decayed, records_below_threshold, records_archived, staging_cleaned? }

GET /api/v1/memory/stats
  Response: { tenant_id, user_id, storage: {...}, embedder: string|null, has_llm: boolean, rerank: { enabled, mode, model, fusion_beta } }

GET /api/v1/memory/health
  Response: { initialized: boolean, storage: boolean, embedder: boolean, llm: boolean }
```

### Content (Three-Layer)

```
GET /api/v1/content/abstract?uri=<uri>
  Response: { status: "ok", result: string }

GET /api/v1/content/overview?uri=<uri>
  Response: { status: "ok", result: string }

GET /api/v1/content/read?uri=<uri>&offset=0&limit=2000
  Response: { status: "ok", result: string }
```

### Knowledge

```
POST /api/v1/knowledge/search
  Body: { query: string, types?: string[], limit?: number }
  Response: { results: [...], count: number } or { error: "feature disabled" }

GET /api/v1/knowledge/candidates
  Response: { candidates: [{ knowledge_id, knowledge_type, scope, status, abstract?, overview?, created_at, updated_at, source_trace_ids? }], count: number } or { error: "feature disabled" }

POST /api/v1/knowledge/approve
  Body: { knowledge_id: string }

POST /api/v1/knowledge/reject
  Body: { knowledge_id: string }

POST /api/v1/archivist/trigger
  Response: { ok: boolean, status: string } or { error: "feature disabled" }

GET /api/v1/archivist/status
  Response: { enabled: boolean, running?: boolean, last_run_at?: string|null, trigger_mode?: string, trigger_threshold?: number } or { error: "feature disabled" }
```

### System & Admin

```
GET /api/v1/system/status?type=health|stats|doctor
  Response varies by type

POST /api/v1/admin/reembed
  Response: { status, updated }

POST /api/v1/admin/search_debug
  Body: { query: string, limit?: number }
  Response: { query, fusion_beta, rerank_mode, results: [{ rank, abstract, raw_vector_score, rerank_score, fused_score, uri }] }
```

## Important Implementation Notes

1. **No mock data**: All data comes from the real API. If the API is unreachable, show an error state with retry button.

2. **Error handling**: Every API call should handle errors gracefully. Show a toast or inline error message, never crash.

3. **Loading states**: Every page should show a loading spinner or skeleton while fetching. Never show a blank page.

4. **URL state**: The Memories page should sync selected URI to URL params (`?uri=xxx`) so links are shareable.

5. **Refresh**: Each page should have a small refresh icon button in the top right to manually refetch data.

6. **Responsive sidebar**: Sidebar collapse state saved to localStorage. On narrow screens, sidebar auto-collapses.

7. **Feature-disabled handling**: The Knowledge and Archivist endpoints may return `{ error: "feature disabled" }` with HTTP 200. Treat this as a disabled state, not a transport error.

8. **Confirmation modals**: Delete, Re-embed, and Decay operations must show a confirmation modal before executing. Modal has cancel + confirm buttons.

9. **Toast notifications**: After successful operations (feedback, delete, approve, reject), show a brief success toast (auto-dismiss 3s). Use a simple custom toast, no external library needed.

10. **The Skills page** is a placeholder only. Do not build any API integration for it. Just render the Coming Soon UI.

## Build & Serve

Dev mode:
```bash
cd web && npm install && npm run dev
# Vite dev server on port 5173, proxy /api → localhost:8921
```

Production build:
```bash
cd web && npm run build
# Output: web/dist/
# FastAPI serves via: app.mount("/", StaticFiles(directory="web/dist", html=True))
```
