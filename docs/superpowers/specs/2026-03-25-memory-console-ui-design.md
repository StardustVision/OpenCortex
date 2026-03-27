# OpenCortex Memory Console — UI Design Spec

## Overview

A developer management panel for the OpenCortex memory system. Provides full visibility and control over memories, knowledge pipeline, search quality, and system health. Embedded in the OpenCortex project as a React + Tailwind SPA served by the existing FastAPI backend.

## Tech Stack

- React 18+ (Vite)
- Tailwind CSS
- React Router (client-side routing)
- Recharts or lightweight charting for score visualizations
- Lucide React for icons
- No state management library (React state + context sufficient)

## Design Language

Modern minimalist, similar to Linear/Notion. Light theme, clean typography, generous whitespace, subtle borders, no heavy shadows. Accent color: indigo/blue.

## Project Structure

```
web/
  index.html
  vite.config.ts
  package.json
  tsconfig.json
  tailwind.config.js
  src/
    main.tsx
    App.tsx
    api/
      client.ts          # HTTP client wrapper (fetch + Bearer token)
      types.ts           # TypeScript types matching API models
    components/
      layout/
        Sidebar.tsx       # Left icon sidebar with nav
        PageLayout.tsx    # Common page wrapper
      common/
        Badge.tsx
        Button.tsx
        Card.tsx
        Modal.tsx
        EmptyState.tsx
        SearchInput.tsx
        ScoreBar.tsx      # Horizontal score visualization
        JsonViewer.tsx    # Collapsible JSON display
        LoadingSpinner.tsx
    pages/
      Dashboard.tsx
      Memories.tsx
      Knowledge.tsx
      SearchDebug.tsx
      System.tsx
      Skills.tsx          # Reserved — Coming Soon placeholder
    hooks/
      useApi.ts           # Generic fetch hook with loading/error
      useDebounce.ts
```

## Authentication

API requires `Authorization: Bearer <JWT>` header. The console reads the token from:
1. URL query param `?token=xxx` (for quick access)
2. localStorage `opencortex_token`
3. If neither exists, show a token input page

The client attaches the token to every request via `client.ts`.

## Navigation

Left sidebar (collapsed icon-only by default, expandable):

| Icon | Label | Route | Status |
|------|-------|-------|--------|
| LayoutDashboard | Dashboard | `/` | Active |
| Brain | Memories | `/memories` | Active |
| BookOpen | Knowledge | `/knowledge` | Active |
| SearchCode | Search Debug | `/search-debug` | Active |
| Settings | System | `/system` | Active |
| Sparkles | Skills | `/skills` | Reserved (Coming Soon) |

## Pages

### 1. Dashboard (`/`)

**Purpose**: System overview at a glance.

**Layout**: Grid of stat cards + recent activity list.

**Components**:

- **Health Status Bar** (top): Horizontal strip showing component health (storage / embedder / LLM). Each shows green/yellow/red dot + label. Data from `GET /api/v1/system/status?type=health`, which currently returns `{ initialized, storage, embedder, llm }`.

- **Stat Cards** (2×2 grid):
  - Total Records (`storage.total_records`)
  - Tenant / User (`tenant_id`, `user_id`)
  - Embedder (`embedder`, `has_llm`)
  - Rerank (`rerank.enabled`, `rerank.mode`, `rerank.model`, `rerank.fusion_beta`)

  Data from `GET /api/v1/memory/stats`.

- **Recent Memories** (table, 10 rows):
  - Columns: abstract (truncated), category, type, created_at
  - Click → navigate to `/memories?uri=xxx`
  - Data from `GET /api/v1/memory/list?limit=10`.

### 2. Memories (`/memories`)

**Purpose**: Browse, search, inspect, and manage all memories.

**Layout**: Master-detail. Left panel (list) + right panel (detail).

**Left Panel**:

- **Search Bar**: Text input, triggers `POST /api/v1/memory/search` with debounce (300ms). When empty, falls back to `GET /api/v1/memory/list`.

- **Filters** (below search):
  - `context_type` dropdown: all / memory / resource / skill / case / pattern
  - `category` dropdown: seed with common values, but allow for categories outside the preset list
  - `detail_level` toggle: L0 / L1 / L2 for search mode only

- **Results List**: Scrollable list of memory cards:
  - Each card shows: abstract (2 lines), category badge, context_type badge, score (if from search)
  - Selected card highlighted
  - Pagination: "Load more" button in list mode (offset-based via `GET /api/v1/memory/list`)

**Right Panel** (when a memory is selected):

- **Header**: Full abstract, URI (monospace, copyable)
- **Three-Layer Content**:
  - Tabs: Abstract (L0) | Overview (L1) | Full Content (L2)
  - Use L0/L1 from search results when present
  - When selected from list mode, fetch L0/L1 via `GET /api/v1/content/abstract` and `GET /api/v1/content/overview`
  - L2 lazy-loaded via `GET /api/v1/content/read?uri=xxx`
  - All content endpoints return `{ status, result }`
- **Metadata Section**: Collapsible JSON viewer showing the selected record payload (`uri`, `category`, `context_type`, `scope`, `project_id`, `created_at`, `updated_at`, and other returned fields)
- **Actions**:
  - **Feedback**: +1 / -1 buttons → `POST /api/v1/memory/feedback`
  - **Delete**: red button with confirmation modal → `POST /api/v1/memory/forget { uri }`

### 3. Knowledge (`/knowledge`)

**Purpose**: Manage the Cortex Alpha knowledge pipeline.

**Layout**: Two tabs — Candidates | Approved Knowledge.

**Tab: Candidates**:

- **Archivist Status Bar** (top):
  - `enabled`, `running`, `trigger_mode`, `trigger_threshold`, `last_run_at`
  - "Trigger Extraction" button → `POST /api/v1/archivist/trigger`
  - Data from `GET /api/v1/archivist/status`
  - If the API returns `{ error: "feature disabled" }`, render a disabled-state placeholder instead of failing the page

- **Candidates List** (table):
  - Columns: abstract, knowledge_type, scope, status, created_at, actions
  - Data from `GET /api/v1/knowledge/candidates`
  - **Actions per row**:
    - Approve (green) → `POST /api/v1/knowledge/approve { knowledge_id }`
    - Reject (red) → `POST /api/v1/knowledge/reject { knowledge_id }`
  - Expandable row to show `overview` and `source_trace_ids` when present

**Tab: Approved Knowledge**:

- Search bar → `POST /api/v1/knowledge/search`
- Results list similar to memories page, showing approved knowledge items
- Each item shows: `abstract`, `knowledge_type`, `scope`, optional `source_trace_ids`
- If the API returns `{ error: "feature disabled" }`, render a disabled-state placeholder

### 4. Search Debug (`/search-debug`)

**Purpose**: Visualize and debug the search pipeline.

**Layout**: Query input at top, results below with score breakdown.

**Query Section**:
- Large text input for search query
- Options: limit (slider 1-20)
- "Search" button → `POST /api/v1/admin/search_debug`
- Show response metadata above the chart area: `query`, `fusion_beta`, `rerank_mode`

**Results Section** (for each result):

- **Result Card**:
  - Abstract text
  - URI (monospace)

- **Score Breakdown** (horizontal stacked bar or grouped bars):
  - `raw_vector_score` (blue) — raw vector similarity
  - `rerank_score` (green) — reranker output
  - `fused_score` (purple) — fused score
  - Formula displayed from response metadata: `fused = fusion_beta × rerank + (1 - fusion_beta) × raw_vector` when rerank is enabled; otherwise `fused = raw_vector`

- **Score Comparison View**: All results in a single bar chart, sorted by `fused_score`, so you can see the spread and how reranking changes the order.

### 5. System (`/system`)

**Purpose**: System diagnostics and admin operations.

**Layout**: Sections stacked vertically.

**Sections**:

- **Doctor Report**: Full diagnostic from `GET /api/v1/system/status?type=doctor`
  - Component-by-component health
  - Rendered as a structured card list

- **Storage Stats**: From `GET /api/v1/system/status?type=stats`
  - Collection counts, record counts, storage size

- **Admin Operations** (danger zone, visually separated):
  - **Re-embed All**: Button with confirmation modal ("This will re-embed all records with the current model. This may take a long time.") → `POST /api/v1/admin/reembed`
  - **Decay**: Button with confirmation → `POST /api/v1/memory/decay`
  - Both show progress/result after execution

### 6. Skills (`/skills`) — Reserved

**Purpose**: Future skill auto-discovery and extraction management.

**Current State**: Coming Soon placeholder page.

**Page Content**:
- Centered illustration or icon (Sparkles)
- "Skills Management — Coming Soon"
- Brief description: "Automatic skill discovery, extraction, and management will be available in a future release."
- Visually consistent with the rest of the console

**Future Provisions** (for when the backend is ready):
- Navigation entry already exists in sidebar
- Route already registered
- Page component exists as placeholder, ready to be replaced

## API Client (`web/src/api/client.ts`)

```typescript
class OpenCortexClient {
  private baseUrl: string;
  private token: string;

  // Memory
  listMemories(params: { category?, context_type?, limit?, offset? }): Promise<MemoryListResponse>
  searchMemories(params: { query, limit?, context_type?, category?, detail_level? }): Promise<SearchResponse>
  forgetMemory(params: { uri?: string, query?: string }): Promise<void>
  feedbackMemory(params: { uri: string, reward: number }): Promise<void>
  decayAll(): Promise<void>

  // Content
  getAbstract(uri: string): Promise<string>
  getOverview(uri: string): Promise<string>
  readContent(uri: string): Promise<string>

  // Knowledge
  searchKnowledge(params: { query, types?, limit? }): Promise<KnowledgeSearchResponse>
  listCandidates(): Promise<CandidatesResponse>
  approveKnowledge(id: string): Promise<void>
  rejectKnowledge(id: string): Promise<void>
  triggerArchivist(): Promise<void>
  archivistStatus(): Promise<ArchivistStatusResponse>

  // System
  systemStatus(type: 'health' | 'stats' | 'doctor'): Promise<StatusResponse>
  reembedAll(): Promise<void>

  // Search Debug
  searchDebug(params: { query, limit? }): Promise<DebugSearchResponse>
}
```

## Backend Integration

The FastAPI server needs to serve the built frontend as static files:

```python
# In server.py — mount after API routes
app.mount("/", StaticFiles(directory="web/dist", html=True), name="frontend")
```

Build command: `cd web && npm run build` produces `web/dist/`.

Development: `cd web && npm run dev` runs Vite dev server with proxy to `localhost:8921`.

## Non-Goals

- User authentication UI (token is provided externally)
- Multi-tenant management (single tenant per token)
- Real-time WebSocket updates (polling or manual refresh is sufficient)
- Mobile-optimized layout (desktop-first developer tool)
- RL profile inspection and protect/unprotect UI until dedicated HTTP endpoints exist
