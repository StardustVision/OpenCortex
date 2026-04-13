# Insights Frontend + Docker Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the Insights console experience end-to-end by wiring the existing insights backend/types into the web app, adding missing API coverage, and shipping the frontend through an Nginx container in `docker-compose`.

**Architecture:** The current branch already contains the `GET /api/v1/insights/report` handler and shared TypeScript report types, so the remaining work is incremental rather than greenfield. First harden the backend metadata layer by testing `/latest`, `/history`, and `/report` together and fixing the `report_period` parsing mismatch (`"to"` vs `" - "`), then add the React client/page/navigation wiring, and finally add a dedicated frontend container that serves Vite output behind Nginx and proxies `/api/` to the existing backend service.

**Tech Stack:** Python 3 + FastAPI + pytest/httpx, React 18 + TypeScript + Tailwind + Vite, Docker Compose + Nginx

**Spec:** `docs/superpowers/specs/2026-03-31-insights-frontend-design.md`

---

## File Map

- Modify: `src/opencortex/insights/api.py`
  Reason: normalize `report_period` parsing for `/latest` and `/history`, and keep `/report` aligned with the tested contract.
- Create: `tests/insights/test_api.py`
  Reason: add focused API coverage for history/latest parsing plus `/report` access control and success cases.
- Modify: `web/src/api/client.ts`
  Reason: add insights client methods and surface HTTP status codes so the page can gate on backend `404`.
- Create: `web/src/pages/Insights.tsx`
  Reason: implement the full split-panel Insights page, loading flow, generate flow, feature gate, and expandable session details.
- Modify: `web/src/App.tsx`
  Reason: register the `/insights` route.
- Modify: `web/src/components/layout/Sidebar.tsx`
  Reason: add the always-visible Insights navigation item after Knowledge.
- Existing baseline, no planned edit: `web/src/api/types.ts`
  Reason: the insights interfaces requested by the spec are already present on this branch; leave them unchanged unless build verification proves a mismatch.
- Create: `web/.dockerignore`
  Reason: prevent host `node_modules` and build artifacts from polluting Docker build context.
- Create: `web/Dockerfile`
  Reason: build the Vite app in Node, then serve static assets from Nginx.
- Create: `web/nginx.conf`
  Reason: support SPA fallback and reverse-proxy `/api/` requests to the backend container.
- Modify: `docker-compose.yml`
  Reason: add the `opencortex-web` service and inline guidance for the insights prerequisites.

---

### Task 1: Harden Insights API Metadata Parsing And Cover `/report`

**Files:**
- Modify: `src/opencortex/insights/api.py`
- Create: `tests/insights/test_api.py`
- Test: `tests/insights/test_api.py`

- [ ] **Step 1: Write the failing API tests first**

Create `tests/insights/test_api.py` with focused coverage for the current regression: `ReportManager` writes `report_period` as `"YYYY-MM-DD to YYYY-MM-DD"`, but `src/opencortex/insights/api.py` currently only parses `"YYYY-MM-DD - YYYY-MM-DD"` in `/latest` and `/history`.

```python
import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import httpx
from fastapi import FastAPI
from httpx import ASGITransport

from opencortex.http.request_context import (
    reset_request_identity,
    set_request_identity,
)
from opencortex.insights.api import create_insights_router


class _FakeCortexFS:
    def __init__(self) -> None:
        self.files: dict[str, str] = {}

    async def read(self, uri: str, layer: str = "L2") -> str | None:
        return self.files.get(uri)


@asynccontextmanager
async def _test_client(report_manager: MagicMock):
    app = FastAPI()
    app.include_router(
        create_insights_router(
            agent=MagicMock(),
            report_manager=report_manager,
            orchestrator=MagicMock(),
        )
    )

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        yield client


class TestInsightsAPI(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    def test_latest_and_history_accept_report_manager_period_format(self) -> None:
        async def check():
            report = {
                "json_uri": "opencortex://tenant1/user1/insights/reports/2026-03-31/weekly.json",
                "generated_at": "2026-03-31T10:00:00",
                "report_period": "2026-03-24 to 2026-03-31",
                "total_sessions": 12,
                "total_messages": 347,
            }

            report_manager = MagicMock()
            report_manager._cortex_fs = _FakeCortexFS()
            report_manager.get_latest_report = AsyncMock(return_value=report)
            report_manager.get_report_history = AsyncMock(return_value=[report])

            async with _test_client(report_manager) as client:
                tokens = set_request_identity("tenant1", "user1")
                try:
                    latest_response = await client.get("/api/v1/insights/latest")
                    history_response = await client.get("/api/v1/insights/history")
                finally:
                    reset_request_identity(tokens)

            self.assertEqual(latest_response.status_code, 200)
            self.assertEqual(history_response.status_code, 200)

            latest_payload = latest_response.json()
            history_payload = history_response.json()

            self.assertEqual(latest_payload["report"]["period_start"], "2026-03-24")
            self.assertEqual(latest_payload["report"]["period_end"], "2026-03-31")
            self.assertEqual(history_payload["reports"][0]["period_start"], "2026-03-24")
            self.assertEqual(history_payload["reports"][0]["period_end"], "2026-03-31")

        self._run(check())

    def test_report_endpoint_rejects_cross_tenant_uri(self) -> None:
        async def check():
            report_manager = MagicMock()
            report_manager._cortex_fs = _FakeCortexFS()

            async with _test_client(report_manager) as client:
                tokens = set_request_identity("tenant1", "user1")
                try:
                    response = await client.get(
                        "/api/v1/insights/report",
                        params={
                            "report_uri": "opencortex://tenant2/user2/insights/reports/2026-03-31/weekly.json"
                        },
                    )
                finally:
                    reset_request_identity(tokens)

            self.assertEqual(response.status_code, 403)
            self.assertEqual(
                response.json()["detail"],
                "Access denied: report does not belong to requesting user",
            )

        self._run(check())

    def test_report_endpoint_returns_full_json_for_owner(self) -> None:
        async def check():
            report_uri = "opencortex://tenant1/user1/insights/reports/2026-03-31/weekly.json"
            report_json = {
                "tenant_id": "tenant1",
                "user_id": "user1",
                "report_period": "2026-03-24 to 2026-03-31",
                "generated_at": "2026-03-31T10:00:00",
                "total_sessions": 12,
                "total_messages": 347,
                "total_duration_hours": 8.5,
                "at_a_glance": "A productive week with heavy frontend work.",
                "cache_hits": 3,
                "llm_calls": 7,
                "project_areas": {"OpenCortex": 5},
                "what_works": ["Fast iteration on the console UI"],
                "friction_analysis": {"Docker rebuilds": 2},
                "suggestions": ["Add a frontend container"],
                "on_the_horizon": ["Session-level detail drilldown"],
                "session_facets": [],
            }

            cortex_fs = _FakeCortexFS()
            cortex_fs.files[report_uri] = json.dumps(report_json)

            report_manager = MagicMock()
            report_manager._cortex_fs = cortex_fs

            async with _test_client(report_manager) as client:
                tokens = set_request_identity("tenant1", "user1")
                try:
                    response = await client.get(
                        "/api/v1/insights/report",
                        params={"report_uri": report_uri},
                    )
                finally:
                    reset_request_identity(tokens)

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["tenant_id"], "tenant1")
            self.assertEqual(response.json()["what_works"][0], "Fast iteration on the console UI")

        self._run(check())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the targeted API test to verify the current failure**

Run: `python3 -m pytest tests/insights/test_api.py -v`

Expected: `test_latest_and_history_accept_report_manager_period_format` fails because `/latest` and `/history` currently fall back to `date.today()` when `report_period` uses `" to "`.

- [ ] **Step 3: Add a shared period parser and reuse it in both metadata endpoints**

Update `src/opencortex/insights/api.py` so `/latest` and `/history` accept both the existing `"to"` format from `ReportManager` and the `" - "` format written in the spec. Move `json` to the top-level imports while you are touching the module.

```python
import json
import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from opencortex.http.request_context import get_effective_identity

logger = logging.getLogger(__name__)


def _parse_report_period(period_str: str) -> tuple[date, date]:
    """Parse report period strings from both spec and stored report formats."""
    for separator in (" - ", " to "):
        if separator not in period_str:
            continue

        start_str, end_str = period_str.split(separator, 1)
        try:
            return date.fromisoformat(start_str), date.fromisoformat(end_str)
        except ValueError:
            break

    today = date.today()
    return today, today
```

Replace the duplicated parsing logic inside `/latest`:

```python
            period_start, period_end = _parse_report_period(
                report.get("report_period", "")
            )

            return {
                "report": {
                    "report_uri": report.get("json_uri", ""),
                    "generated_at": datetime.fromisoformat(
                        report.get("generated_at", "")
                    ),
                    "period_start": period_start,
                    "period_end": period_end,
                    "total_sessions": report.get("total_sessions", 0),
                    "total_messages": report.get("total_messages", 0),
                },
                "message": "Latest report retrieved",
            }
```

Replace the loop body inside `/history` the same way:

```python
            report_list = []
            for report in reports:
                period_start, period_end = _parse_report_period(
                    report.get("report_period", "")
                )

                report_list.append(
                    {
                        "report_uri": report.get("json_uri", ""),
                        "generated_at": datetime.fromisoformat(
                            report.get("generated_at", "")
                        ),
                        "period_start": period_start,
                        "period_end": period_end,
                        "total_sessions": report.get("total_sessions", 0),
                        "total_messages": report.get("total_messages", 0),
                    }
                )
```

Keep the existing `/report` handler, but remove the inline import now that `json` is imported at module scope:

```python
            content = await report_manager._cortex_fs.read(report_uri)
            if not content:
                raise HTTPException(status_code=404, detail="Report not found")

            return json.loads(content)
```

- [ ] **Step 4: Run the focused and full insights test suites**

Run: `python3 -m pytest tests/insights/test_api.py -v`

Expected: all tests in `tests/insights/test_api.py` pass.

Run: `python3 -m pytest tests/insights -v`

Expected: existing insights tests still pass, confirming the parser change did not regress report generation/storage logic.

- [ ] **Step 5: Commit the backend/API slice**

```bash
git add src/opencortex/insights/api.py tests/insights/test_api.py
git commit -m "test(insights): cover report API and normalize period parsing"
```

---

### Task 2: Add Insights API Client Methods And Propagate HTTP Status

**Files:**
- Modify: `web/src/api/client.ts`

- [ ] **Step 1: Add the missing imports, enrich request errors, and expose the insights methods**

Update `web/src/api/client.ts` so the page can call the insights endpoints and reliably detect backend `404` responses without parsing a string message.

```typescript
import {
  SystemHealth, MemoryStats, SearchResponse, ListResponse, ContentResponse,
  KnowledgeCandidate, ArchivistStatus, SearchDebugResponse,
  TokenRecord, AuthMe, AdminListResponse,
  GenerateInsightsResponse, LatestReportResponse, ReportHistoryResponse, InsightsReport
} from './types';

type OpenCortexApiError = Error & {
  status: number;
  payload: unknown;
};

export class OpenCortexClient {
  private baseUrl: string;
  private token: string;

  constructor(baseUrl: string, token: string) {
    this.baseUrl = baseUrl.endsWith('/') ? baseUrl.slice(0, -1) : baseUrl;
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

    if (!res.ok) {
      const errorData = await res.json().catch(() => ({}));
      if ((errorData as { error?: string }).error === 'feature disabled') {
        return { error: 'feature disabled' } as T;
      }

      const error = new Error(`API error: ${res.status}`) as OpenCortexApiError;
      error.status = res.status;
      error.payload = errorData;
      throw error;
    }

    return res.json();
  }

  // ...existing methods...

  // Insights
  generateInsights(days: number = 7): Promise<GenerateInsightsResponse> {
    return this.request('POST', `/api/v1/insights/generate?days=${days}`);
  }

  getLatestInsights(): Promise<LatestReportResponse> {
    return this.request('GET', '/api/v1/insights/latest');
  }

  getInsightsHistory(limit: number = 10): Promise<ReportHistoryResponse> {
    return this.request('GET', `/api/v1/insights/history?limit=${limit}`);
  }

  getInsightsReport(reportUri: string): Promise<InsightsReport> {
    return this.request(
      'GET',
      `/api/v1/insights/report?report_uri=${encodeURIComponent(reportUri)}`
    );
  }
}
```

- [ ] **Step 2: Verify the shared client still compiles cleanly**

Run: `npm --prefix web run build`

Expected: TypeScript and Vite complete successfully with no new client-level errors.

- [ ] **Step 3: Commit the client slice**

```bash
git add web/src/api/client.ts
git commit -m "feat(web): add insights API client methods"
```

---

### Task 3: Build The Insights Page

**Files:**
- Create: `web/src/pages/Insights.tsx`

- [ ] **Step 1: Create the full split-panel Insights page**

Create `web/src/pages/Insights.tsx` with the same layout vocabulary already used by `Memories.tsx` and `Knowledge.tsx`: a 40/60 split, a centered disabled-state card, shared `Card`/`Badge`/`Button`/`LoadingSpinner`/`EmptyState` usage, and no new shared component abstractions.

```tsx
import React, { useEffect, useState } from 'react';
import { PageLayout } from '../components/layout/PageLayout';
import { Card } from '../components/common/Card';
import { Badge } from '../components/common/Badge';
import { Button } from '../components/common/Button';
import { LoadingSpinner } from '../components/common/LoadingSpinner';
import { EmptyState } from '../components/common/EmptyState';
import { useApi } from '../api/Context';
import { InsightsReport, ReportHistoryResponse, ReportMetadata } from '../api/types';
import {
  AlertCircle,
  BarChart3,
  ChevronDown,
  ChevronUp,
  Lightbulb,
  Sparkles,
  TrendingUp,
  TriangleAlert,
} from 'lucide-react';

type ApiError = Error & { status?: number };
type HistoryResult = ReportHistoryResponse | { error: string };

const formatDate = (value: string) => new Date(value).toLocaleDateString();
const formatDateTime = (value: string) => new Date(value).toLocaleString();
const formatRange = (report: ReportMetadata) =>
  `${formatDate(report.period_start)} - ${formatDate(report.period_end)}`;

const getOutcomeColor = (outcome: string) => {
  switch (outcome) {
    case 'fully_achieved':
    case 'mostly_achieved':
      return 'green';
    case 'partially_achieved':
      return 'yellow';
    case 'not_achieved':
      return 'red';
    default:
      return 'gray';
  }
};

const getOutcomeLabel = (outcome: string) => outcome.replace(/_/g, ' ');

export const Insights: React.FC = () => {
  const { client } = useApi();
  const [reports, setReports] = useState<ReportMetadata[]>([]);
  const [selectedReportUri, setSelectedReportUri] = useState<string | null>(null);
  const [selectedReport, setSelectedReport] = useState<InsightsReport | null>(null);
  const [expandedSessions, setExpandedSessions] = useState<Record<string, boolean>>({});
  const [isHistoryLoading, setIsHistoryLoading] = useState(false);
  const [isReportLoading, setIsReportLoading] = useState(false);
  const [isGenerating, setIsGenerating] = useState(false);
  const [isFeatureDisabled, setIsFeatureDisabled] = useState(false);

  const handleApiFailure = (error: unknown) => {
    if ((error as ApiError).status === 404) {
      setIsFeatureDisabled(true);
      return;
    }

    console.error('Insights request failed', error);
  };

  const loadHistory = async (nextSelection?: string | null) => {
    if (!client) return;

    setIsHistoryLoading(true);
    try {
      const response = await client.getInsightsHistory(10) as HistoryResult;
      if ('error' in response && response.error === 'feature disabled') {
        setIsFeatureDisabled(true);
        return;
      }

      setIsFeatureDisabled(false);
      setReports(response.reports);

      if (nextSelection !== undefined) {
        setSelectedReportUri(nextSelection);
      } else if (selectedReportUri && !response.reports.some((report) => report.report_uri === selectedReportUri)) {
        setSelectedReportUri(null);
        setSelectedReport(null);
      }
    } catch (error) {
      handleApiFailure(error);
    } finally {
      setIsHistoryLoading(false);
    }
  };

  const loadReport = async (reportUri: string) => {
    if (!client) return;

    setIsReportLoading(true);
    try {
      const report = await client.getInsightsReport(reportUri);
      setSelectedReport(report);
      setExpandedSessions({});
    } catch (error) {
      handleApiFailure(error);
    } finally {
      setIsReportLoading(false);
    }
  };

  useEffect(() => {
    void loadHistory();
  }, [client]);

  useEffect(() => {
    if (!selectedReportUri) {
      setSelectedReport(null);
      return;
    }

    void loadReport(selectedReportUri);
  }, [client, selectedReportUri]);

  const handleGenerate = async () => {
    if (!client) return;

    setIsGenerating(true);
    try {
      const response = await client.generateInsights(7);
      await loadHistory(response.report_uri);
    } catch (error) {
      handleApiFailure(error);
    } finally {
      setIsGenerating(false);
    }
  };

  const handleRefresh = async () => {
    await loadHistory(selectedReportUri);
    if (selectedReportUri) {
      await loadReport(selectedReportUri);
    }
  };

  const toggleSession = (sessionId: string) => {
    setExpandedSessions((current) => ({
      ...current,
      [sessionId]: !current[sessionId],
    }));
  };

  if (isFeatureDisabled) {
    return (
      <PageLayout title="Insights">
        <div className="flex flex-col items-center justify-center py-24">
          <Card className="max-w-lg text-center">
            <AlertCircle size={48} className="text-yellow-500 mx-auto mb-4" />
            <h2 className="text-xl font-bold text-gray-900 mb-2">Feature Disabled</h2>
            <p className="text-gray-600">
              The Insights feature requires Cortex Alpha (trace collection) and LLM to be configured.
            </p>
          </Card>
        </div>
      </PageLayout>
    );
  }

  return (
    <PageLayout
      title="Insights"
      onRefresh={() => { void handleRefresh(); }}
      isLoading={isHistoryLoading || isReportLoading || isGenerating}
    >
      <div className="flex h-[calc(100vh-160px)] gap-6 overflow-hidden">
        <div className="w-[40%] flex flex-col gap-4 overflow-hidden">
          <Card className="flex items-center justify-between">
            <div>
              <h2 className="text-lg font-bold text-gray-900">Reports</h2>
              <p className="text-sm text-gray-500">
                Weekly LLM-generated activity summaries for this workspace.
              </p>
            </div>
            <Button onClick={() => { void handleGenerate(); }} loading={isGenerating}>
              <Sparkles size={16} className="mr-2" />
              Generate
            </Button>
          </Card>

          <div className="flex-1 overflow-y-auto pr-2">
            {isHistoryLoading ? (
              <LoadingSpinner />
            ) : reports.length === 0 ? (
              <Card>
                <EmptyState
                  icon={<BarChart3 size={48} className="text-gray-200" />}
                  title="No reports yet"
                  message="Click Generate to create your first insights report."
                  action={
                    <Button onClick={() => { void handleGenerate(); }} loading={isGenerating}>
                      Generate Report
                    </Button>
                  }
                />
              </Card>
            ) : (
              <div className="space-y-3">
                {reports.map((report) => {
                  const isActive = selectedReportUri === report.report_uri;

                  return (
                    <button
                      key={report.report_uri}
                      type="button"
                      onClick={() => setSelectedReportUri(report.report_uri)}
                      className={`w-full rounded-lg border bg-white p-4 text-left transition-all ${
                        isActive
                          ? 'border-indigo-200 border-l-4 border-l-indigo-600'
                          : 'border-gray-200 hover:border-gray-300'
                      }`}
                    >
                      <div className="flex items-start justify-between gap-3">
                        <div>
                          <p className="text-sm font-semibold text-gray-900">
                            {formatRange(report)}
                          </p>
                          <p className="mt-1 text-xs text-gray-500">
                            Generated {formatDateTime(report.generated_at)}
                          </p>
                        </div>
                        <Badge color="indigo">{report.total_sessions} sessions</Badge>
                      </div>
                      <p className="mt-3 text-sm text-gray-600">
                        {report.total_messages} messages analyzed
                      </p>
                    </button>
                  );
                })}
              </div>
            )}
          </div>
        </div>

        <div className="w-[60%] overflow-y-auto pr-2">
          {!selectedReportUri ? (
            <Card>
              <EmptyState
                icon={<BarChart3 size={48} className="text-gray-200" />}
                title="Select a report"
                message="Select a report to view details."
              />
            </Card>
          ) : isReportLoading ? (
            <LoadingSpinner />
          ) : !selectedReport ? (
            <Card>
              <EmptyState
                icon={<AlertCircle size={48} className="text-gray-200" />}
                title="Report unavailable"
                message="Reload the page or choose another report."
              />
            </Card>
          ) : (
            <div className="space-y-4">
              <Card>
                <div className="flex items-start justify-between gap-4">
                  <div>
                    <p className="text-sm font-medium text-indigo-600">Insights Report</p>
                    <h2 className="mt-1 text-2xl font-bold text-gray-900">
                      {selectedReport.report_period}
                    </h2>
                  </div>
                  <Badge color="gray">{formatDateTime(selectedReport.generated_at)}</Badge>
                </div>
              </Card>

              <Card className="border-indigo-100 bg-indigo-50">
                <p className="text-xs font-semibold uppercase tracking-wide text-indigo-600">
                  At a Glance
                </p>
                <p className="mt-3 text-sm leading-7 text-indigo-950">
                  {selectedReport.at_a_glance}
                </p>
              </Card>

              <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                <StatCard label="Sessions" value={selectedReport.total_sessions.toString()} />
                <StatCard label="Messages" value={selectedReport.total_messages.toString()} />
                <StatCard label="Hours" value={selectedReport.total_duration_hours.toFixed(1)} />
              </div>

              <Card className="border-l-4 border-l-green-500">
                <div className="flex items-center gap-2">
                  <TrendingUp size={18} className="text-green-600" />
                  <h3 className="text-lg font-bold text-gray-900">What Works</h3>
                </div>
                {selectedReport.what_works.length === 0 ? (
                  <p className="mt-4 text-sm text-gray-500">No strengths captured in this report.</p>
                ) : (
                  <ul className="mt-4 space-y-2 text-sm text-gray-700 list-disc list-inside">
                    {selectedReport.what_works.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                )}
              </Card>

              <Card className="border-l-4 border-l-amber-500">
                <div className="flex items-center gap-2">
                  <TriangleAlert size={18} className="text-amber-600" />
                  <h3 className="text-lg font-bold text-gray-900">Friction Areas</h3>
                </div>
                {Object.keys(selectedReport.friction_analysis).length === 0 ? (
                  <p className="mt-4 text-sm text-gray-500">No friction hotspots recorded.</p>
                ) : (
                  <div className="mt-4 space-y-3">
                    {Object.entries(selectedReport.friction_analysis).map(([label, count]) => (
                      <div
                        key={label}
                        className="flex items-center justify-between rounded-md bg-amber-50 px-4 py-3"
                      >
                        <span className="text-sm font-medium text-gray-800">{label}</span>
                        <Badge color="yellow">{count}x</Badge>
                      </div>
                    ))}
                  </div>
                )}
              </Card>

              <Card className="border-l-4 border-l-blue-500">
                <div className="flex items-center gap-2">
                  <Lightbulb size={18} className="text-blue-600" />
                  <h3 className="text-lg font-bold text-gray-900">Suggestions</h3>
                </div>
                {selectedReport.suggestions.length === 0 ? (
                  <p className="mt-4 text-sm text-gray-500">No suggestions were generated.</p>
                ) : (
                  <ul className="mt-4 space-y-2 text-sm text-gray-700 list-disc list-inside">
                    {selectedReport.suggestions.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                )}
              </Card>

              <Card className="border-l-4 border-l-purple-500">
                <div className="flex items-center gap-2">
                  <Sparkles size={18} className="text-purple-600" />
                  <h3 className="text-lg font-bold text-gray-900">On the Horizon</h3>
                </div>
                {selectedReport.on_the_horizon.length === 0 ? (
                  <p className="mt-4 text-sm text-gray-500">No forward-looking ideas were generated.</p>
                ) : (
                  <ul className="mt-4 space-y-2 text-sm text-gray-700 list-disc list-inside">
                    {selectedReport.on_the_horizon.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                )}
              </Card>

              <Card>
                <div className="flex items-center justify-between">
                  <h3 className="text-lg font-bold text-gray-900">Session Details</h3>
                  <Badge color="gray">{selectedReport.session_facets.length} sessions</Badge>
                </div>

                {selectedReport.session_facets.length === 0 ? (
                  <p className="mt-4 text-sm text-gray-500">No session facets were included in this report.</p>
                ) : (
                  <div className="mt-4 space-y-3">
                    {selectedReport.session_facets.map((facet) => {
                      const isExpanded = !!expandedSessions[facet.session_id];

                      return (
                        <div key={facet.session_id} className="rounded-lg border border-gray-200">
                          <button
                            type="button"
                            onClick={() => toggleSession(facet.session_id)}
                            className="flex w-full items-center justify-between gap-4 px-4 py-4 text-left"
                          >
                            <div>
                              <p className="text-sm font-semibold text-gray-900">
                                {facet.brief_summary}
                              </p>
                              <p className="mt-1 text-xs text-gray-500">{facet.session_id}</p>
                            </div>
                            {isExpanded ? <ChevronUp size={18} /> : <ChevronDown size={18} />}
                          </button>

                          {isExpanded && (
                            <div className="border-t border-gray-100 px-4 py-4 space-y-4">
                              <div className="flex flex-wrap gap-2">
                                <Badge color={getOutcomeColor(facet.outcome)}>
                                  {getOutcomeLabel(facet.outcome)}
                                </Badge>
                                <Badge color="blue">{facet.session_type}</Badge>
                                <Badge color="gray">
                                  helpfulness {Math.round(facet.claude_helpfulness * 100)}%
                                </Badge>
                              </div>

                              <div>
                                <p className="text-xs font-semibold uppercase tracking-wide text-gray-500">
                                  Underlying Goal
                                </p>
                                <p className="mt-1 text-sm text-gray-700">
                                  {facet.underlying_goal}
                                </p>
                              </div>

                              <div>
                                <p className="text-xs font-semibold uppercase tracking-wide text-gray-500">
                                  Goal Categories
                                </p>
                                <div className="mt-2 flex flex-wrap gap-2">
                                  {facet.goal_categories.map((category) => (
                                    <Badge key={category} color="indigo">{category}</Badge>
                                  ))}
                                </div>
                              </div>

                              {facet.primary_success && (
                                <div>
                                  <p className="text-xs font-semibold uppercase tracking-wide text-gray-500">
                                    Primary Success
                                  </p>
                                  <p className="mt-1 text-sm text-gray-700">
                                    {facet.primary_success}
                                  </p>
                                </div>
                              )}
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                )}
              </Card>
            </div>
          )}
        </div>
      </div>
    </PageLayout>
  );
};

const StatCard: React.FC<{ label: string; value: string }> = ({ label, value }) => (
  <Card>
    <p className="text-sm font-medium text-gray-500">{label}</p>
    <p className="mt-2 text-2xl font-bold text-gray-900">{value}</p>
  </Card>
);
```

- [ ] **Step 2: Verify the page compiles before routing it**

Run: `npm --prefix web run build`

Expected: the new page compiles cleanly, including the new async flows and local `StatCard`.

- [ ] **Step 3: Lint the page-level code**

Run: `npm --prefix web run lint`

Expected: ESLint exits `0`, confirming there are no unused imports, missing dependencies, or hook warnings.

- [ ] **Step 4: Commit the page slice**

```bash
git add web/src/pages/Insights.tsx
git commit -m "feat(web): add insights page"
```

---

### Task 4: Wire Route And Sidebar Navigation

**Files:**
- Modify: `web/src/App.tsx`
- Modify: `web/src/components/layout/Sidebar.tsx`

- [ ] **Step 1: Register the `/insights` route**

Update `web/src/App.tsx` so the page is mounted alongside the existing console pages.

```tsx
import React from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import { useApi } from './api/Context';
import { Dashboard } from './pages/Dashboard';
import { Memories } from './pages/Memories';
import { Knowledge } from './pages/Knowledge';
import { Insights } from './pages/Insights';
import { SearchDebug } from './pages/SearchDebug';
import { System } from './pages/System';
import { Skills } from './pages/Skills';
import { Tokens } from './pages/Tokens';
import { Connect } from './pages/Connect';

export const App: React.FC = () => {
  const { token } = useApi();

  if (!token) {
    return <Connect />;
  }

  return (
    <Routes>
      <Route path="/" element={<Dashboard />} />
      <Route path="/memories" element={<Memories />} />
      <Route path="/knowledge" element={<Knowledge />} />
      <Route path="/insights" element={<Insights />} />
      <Route path="/search-debug" element={<SearchDebug />} />
      <Route path="/system" element={<System />} />
      <Route path="/skills" element={<Skills />} />
      <Route path="/tokens" element={<Tokens />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
};
```

- [ ] **Step 2: Add the Insights nav item immediately after Knowledge**

Update `web/src/components/layout/Sidebar.tsx` to import `BarChart3` and insert the new item in the existing `navItems` array.

```tsx
import React, { useState, useEffect } from 'react';
import { NavLink } from 'react-router-dom';
import {
  LayoutDashboard,
  Brain,
  BookOpen,
  BarChart3,
  SearchCode,
  Settings,
  Sparkles,
  Key,
  ChevronLeft,
  ChevronRight,
  LogOut
} from 'lucide-react';
import { useApi } from '../../api/Context';

interface NavItem {
  icon: React.FC<any>;
  label: string;
  path: string;
  status: string;
  adminOnly?: boolean;
}

const navItems: NavItem[] = [
  { icon: LayoutDashboard, label: 'Dashboard', path: '/', status: 'active' },
  { icon: Brain, label: 'Memories', path: '/memories', status: 'active' },
  { icon: BookOpen, label: 'Knowledge', path: '/knowledge', status: 'active' },
  { icon: BarChart3, label: 'Insights', path: '/insights', status: 'active' },
  { icon: SearchCode, label: 'Search Debug', path: '/search-debug', status: 'active' },
  { icon: Settings, label: 'System', path: '/system', status: 'active' },
  { icon: Key, label: 'Tokens', path: '/tokens', status: 'active', adminOnly: true },
  { icon: Sparkles, label: 'Skills', path: '/skills', status: 'coming-soon' },
];
```

- [ ] **Step 3: Rebuild the web app with routing + navigation in place**

Run: `npm --prefix web run build`

Expected: build succeeds and includes the new route/page import graph.

- [ ] **Step 4: Commit the routing slice**

```bash
git add web/src/App.tsx web/src/components/layout/Sidebar.tsx
git commit -m "feat(web): add insights navigation"
```

---

### Task 5: Containerize The Frontend And Add Compose Wiring

**Files:**
- Create: `web/.dockerignore`
- Create: `web/Dockerfile`
- Create: `web/nginx.conf`
- Modify: `docker-compose.yml`

- [ ] **Step 1: Add the frontend Docker context exclusions**

Create `web/.dockerignore`:

```dockerignore
node_modules
dist
.env*
```

- [ ] **Step 2: Add the multi-stage frontend Dockerfile**

Create `web/Dockerfile`:

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

- [ ] **Step 3: Add the Nginx config for SPA fallback and API proxying**

Create `web/nginx.conf`:

```nginx
server {
    listen 80;
    root /usr/share/nginx/html;
    index index.html;

    location /api/ {
        proxy_pass http://opencortex:8921;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }

    location / {
        try_files $uri $uri/ /index.html;
    }
}
```

- [ ] **Step 4: Add the `opencortex-web` service and insights guidance to Compose**

Update `docker-compose.yml`:

```yaml
services:
  opencortex:
    build: .
    container_name: opencortex-server
    ports:
      - "8921:8921"
    volumes:
      - ./.cortex:/data
    environment:
      # ---- Server ----
      - OPENCORTEX_HTTP_SERVER_HOST=0.0.0.0
      - OPENCORTEX_HTTP_SERVER_PORT=8921
      # ---- Storage ----
      - OPENCORTEX_DATA_ROOT=/data
      # ---- Embedding ----
      # - OPENCORTEX_EMBEDDING_PROVIDER=openai
      # - OPENCORTEX_EMBEDDING_MODEL=text-embedding-3-large
      # - OPENCORTEX_EMBEDDING_API_KEY=your-api-key
      # - OPENCORTEX_EMBEDDING_API_BASE=https://api.openai.com/v1
      # ---- LLM (required for insights generation) ----
      # - OPENCORTEX_LLM_MODEL=<your-model>
      # - OPENCORTEX_LLM_API_KEY=<your-key>
      # - OPENCORTEX_LLM_API_BASE=
      # ---- Rerank ----
      # - OPENCORTEX_RERANK_PROVIDER=
      # - OPENCORTEX_RERANK_MODEL=
      # - OPENCORTEX_RERANK_API_KEY=
      # - OPENCORTEX_RERANK_API_BASE=
      # - OPENCORTEX_RERANK_THRESHOLD=0.0
      # - OPENCORTEX_RERANK_FUSION_BETA=0.7
      # ---- Cortex Alpha (required for insights routes) ----
      # - OPENCORTEX_CORTEX_ALPHA={"trace_splitter_enabled":true,"archivist_enabled":true,"archivist_trigger_threshold":5}
    restart: unless-stopped

  opencortex-web:
    build: ./web
    container_name: opencortex-web
    ports:
      - "8080:80"
    depends_on:
      - opencortex
    restart: unless-stopped
```

- [ ] **Step 5: Validate the Compose definition and build the frontend image**

Run: `docker compose config`

Expected: merged Compose config renders without YAML or interpolation errors.

Run: `docker compose build opencortex-web`

Expected: the image builds successfully, including `npm ci`, `npm run build`, and the final Nginx stage.

- [ ] **Step 6: Smoke-test the two-container deployment**

Run: `docker compose up -d opencortex opencortex-web`

Expected: both containers enter `running` state.

Run: `curl -I http://localhost:8080`

Expected: Nginx responds with `HTTP/1.1 200 OK`, confirming the static frontend is reachable on the documented port.

- [ ] **Step 7: Commit the deployment slice**

```bash
git add web/.dockerignore web/Dockerfile web/nginx.conf docker-compose.yml
git commit -m "feat(web): containerize insights console frontend"
```

---

## Self-Review

- Spec coverage:
  `GET /report` is covered in Task 1 with added tests and security assertions.
  Frontend route, sidebar, feature gate, split layout, generate flow, report detail rendering, and expandable session details are covered in Tasks 2 through 4.
  Dockerfile, `.dockerignore`, Nginx proxying, and `docker-compose.yml` service wiring are covered in Task 5.
- Placeholder scan:
  no placeholder markers or vague catch-all phrases remain.
- Type and naming consistency:
  uses the existing branch names `GenerateInsightsResponse`, `ReportHistoryResponse`, `InsightsReport`, `getInsightsHistory`, `getInsightsReport`, and `report_uri` consistently across backend, client, page, and compose plan steps.
