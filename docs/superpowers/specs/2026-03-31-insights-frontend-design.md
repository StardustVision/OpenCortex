# Insights Frontend + Docker Deployment

## Summary

Add an Insights page to the OpenCortex web console for viewing and triggering LLM-powered usage analysis reports. Containerize the frontend with Nginx and add it to docker-compose for unified deployment.

## Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Display depth | Full report content | Maximize value of 7-stage LLM analysis |
| Docker strategy | Nginx container + static files | Production-grade, decoupled, performant |
| Page layout | Left-right split (like Memories) | Consistent UX across console |
| Generate UX | Blocking wait + loading spinner | Simple, no async task infra needed |

## 1. Backend — New Report Detail API

### Endpoint

```
GET /api/v1/insights/report?report_uri=<uri>
```

- **Auth**: JWT required (same as all insights endpoints)
- **Query param**: `report_uri` — the `opencortex://` URI returned by `/history` or `/latest`
- **Response**: Full `InsightsReport` JSON from CortexFS

### Response Schema

```json
{
  "tenant_id": "string",
  "user_id": "string",
  "report_period": "2026-03-24 - 2026-03-31",
  "generated_at": "2026-03-31T10:00:00",
  "total_sessions": 12,
  "total_messages": 347,
  "total_duration_hours": 8.5,
  "at_a_glance": "Your coding sessions this week...",
  "cache_hits": 3,
  "llm_calls": 7,
  "project_areas": {"OpenCortex": 5, "Frontend": 3},
  "what_works": ["Memory retrieval accuracy..."],
  "friction_analysis": {"Embedding timeout": 3},
  "suggestions": ["Consider increasing batch size..."],
  "on_the_horizon": ["Graph-based retrieval..."],
  "session_facets": [
    {
      "session_id": "abc123",
      "underlying_goal": "Add insights page",
      "brief_summary": "Brainstormed and designed insights frontend",
      "goal_categories": ["frontend", "feature"],
      "outcome": "fully_achieved",
      "user_satisfaction_counts": {"positive": 3, "negative": 0},
      "claude_helpfulness": 0.9,
      "session_type": "coding",
      "friction_counts": {},
      "primary_success": "Design completed"
    }
  ]
}
```

### Implementation

Add a new route handler in `src/opencortex/insights/api.py` within `create_insights_router()`:

1. Validate JWT identity via `get_effective_identity()`
2. Verify the `report_uri` belongs to the requesting tenant/user (security check)
3. Read full JSON from CortexFS via `report_manager._cortex_fs.read(report_uri)`
4. Parse and return as JSON response

### Security

The endpoint must verify that the `tid/uid` extracted from JWT matches the `tid/uid` in the requested `report_uri`. This prevents cross-tenant report access.

## 2. Frontend — Insights Page

### Route & Navigation

- **Route**: `/insights` in `App.tsx`
- **Sidebar**: Add "Insights" nav item with `BarChart3` icon (from lucide-react), positioned after "Knowledge" and before "Search Debug". The nav item is always visible (consistent with Knowledge page pattern).

### Feature Gate

Insights is an optional backend feature — it requires TraceStore + LLM to be configured. When the backend hasn't registered insights routes, all `/api/v1/insights/*` calls return 404.

The page handles this using the same pattern as `Knowledge.tsx`:
- On mount, call `getInsightsHistory()`. If the response is a 404 or `{error: 'feature disabled'}`, set `isFeatureDisabled = true`.
- When disabled, render a centered Card with `AlertCircle` icon and message: "The Insights feature requires Cortex Alpha (trace collection) and LLM to be configured."
- When enabled, render the normal split-panel layout.

### API Client Methods

Add to `web/src/api/client.ts`:

```typescript
// Trigger report generation (POST /api/v1/insights/generate?days=N)
async generateInsights(days: number = 7): Promise<GenerateInsightsResponse>

// Get latest report metadata
async getLatestInsights(): Promise<LatestReportResponse>

// Get report history
async getInsightsHistory(limit: number = 10): Promise<ReportHistoryResponse>

// Get full report content
async getInsightsReport(reportUri: string): Promise<InsightsReport>
```

### TypeScript Types

Add to `web/src/api/types.ts`:

```typescript
interface ReportMetadata {
  report_uri: string
  generated_at: string
  period_start: string
  period_end: string
  total_sessions: number
  total_messages: number
}

interface GenerateInsightsResponse {
  report_uri: string
  summary: string
  generated_at: string
}

interface LatestReportResponse {
  report: ReportMetadata | null
  message: string
}

interface ReportHistoryResponse {
  reports: ReportMetadata[]
  total: number
}

interface SessionFacet {
  session_id: string
  underlying_goal: string
  brief_summary: string
  goal_categories: string[]
  outcome: string
  user_satisfaction_counts: Record<string, number>
  claude_helpfulness: number
  session_type: string
  friction_counts: Record<string, number>
  primary_success: string | null
}

interface InsightsReport {
  tenant_id: string
  user_id: string
  report_period: string
  generated_at: string
  total_sessions: number
  total_messages: number
  total_duration_hours: number
  at_a_glance: string
  cache_hits: number
  llm_calls: number
  project_areas: Record<string, number>
  what_works: string[]
  friction_analysis: Record<string, number>
  suggestions: string[]
  on_the_horizon: string[]
  session_facets: SessionFacet[]
}
```

### Page Layout — `Insights.tsx`

Left-right split layout (40% / 60%), consistent with `Memories.tsx`:

**Left Panel (40%)**:
- Header row: "Reports" title + "Generate" button (primary variant)
- Generate button: on click calls `POST /api/v1/insights/generate?days=7`, shows loading spinner, on success refreshes history list and auto-selects the new report
- Report history list: each item shows date range + session count + message count. Active item highlighted with left border (indigo). Click to select and load full report in right panel.
- Empty state: "No reports yet. Click Generate to create your first insights report."

**Right Panel (60%)**:
- Empty state when no report selected: "Select a report to view details"
- When report selected, loads full content via `GET /api/v1/insights/report?report_uri=...`
- Loading spinner while fetching
- Content sections (top to bottom):
  1. **Header**: Report date range
  2. **At a Glance**: Indigo background card with summary text
  3. **Stats row**: 3 cards — Sessions / Messages / Hours (grid, same style as Dashboard)
  4. **What Works**: Card with bullet list (green left border)
  5. **Friction Areas**: Card with items showing name + occurrence count (orange left border)
  6. **Suggestions**: Card with bullet list (blue left border)
  7. **On the Horizon**: Card with bullet list (purple left border)
  8. **Session Details**: Expandable list. Each session shows brief_summary, click to expand showing: underlying_goal, outcome badge, session_type badge, claude_helpfulness score, goal_categories as Badge components

### Component Reuse

Use existing common components:
- `Card` for content sections
- `Badge` for outcome/session_type labels
- `Button` for Generate action
- `LoadingSpinner` for loading states
- `EmptyState` for empty panels
- `PageLayout` for page structure

No new shared components needed.

## 3. Docker — Frontend Service

### `web/.dockerignore`

```
node_modules
dist
.env*
```

Prevents the host's `node_modules` from being copied into the build context, which would overwrite the Linux-specific dependencies installed by `npm ci`.

### `web/Dockerfile`

```dockerfile
# Stage 1: Build
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

# Stage 2: Serve
FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
```

### `web/nginx.conf`

```nginx
server {
    listen 80;
    root /usr/share/nginx/html;
    index index.html;

    # API reverse proxy
    location /api/ {
        proxy_pass http://opencortex:8921;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 120s;  # insights generation can take time
    }

    # SPA fallback
    location / {
        try_files $uri $uri/ /index.html;
    }
}
```

### `docker-compose.yml` changes

Add `opencortex-web` service and uncomment insights prerequisites:

```yaml
services:
  opencortex:
    # ... existing config ...
    environment:
      # --- existing settings ---
      - OPENCORTEX_HTTP_SERVER_HOST=0.0.0.0
      - OPENCORTEX_HTTP_SERVER_PORT=8921
      - OPENCORTEX_DATA_ROOT=/data
      # --- uncomment for insights (requires LLM + Cortex Alpha) ---
      # - OPENCORTEX_LLM_MODEL=<your-model>
      # - OPENCORTEX_LLM_API_KEY=<your-key>
      # - OPENCORTEX_CORTEX_ALPHA={"trace_splitter_enabled":true,"archivist_enabled":true}

  opencortex-web:
    build: ./web
    container_name: opencortex-web
    ports:
      - "8080:80"
    depends_on:
      - opencortex
    restart: unless-stopped
```

Users access the console at `http://localhost:8080`. API calls are proxied internally to `opencortex:8921` via Docker networking.

**Note**: Insights requires LLM and Cortex Alpha (trace collection) to be configured on the backend. Without these, the insights API routes are not registered and the frontend Insights page shows a "Feature Disabled" message. The docker-compose template includes the relevant env vars as commented examples.

## 4. File Change Summary

| File | Action | Description |
|------|--------|-------------|
| `src/opencortex/insights/api.py` | Edit | Add `GET /report` endpoint |
| `web/src/pages/Insights.tsx` | Create | New insights page (with feature gate) |
| `web/src/api/client.ts` | Edit | Add 4 insights API methods |
| `web/src/api/types.ts` | Edit | Add insights type definitions |
| `web/src/App.tsx` | Edit | Add `/insights` route |
| `web/src/components/layout/Sidebar.tsx` | Edit | Add Insights nav item |
| `web/.dockerignore` | Create | Exclude node_modules/dist from build context |
| `web/Dockerfile` | Create | Node build + Nginx serve |
| `web/nginx.conf` | Create | Static files + API proxy |
| `docker-compose.yml` | Edit | Add `opencortex-web` service + insights env var examples |
