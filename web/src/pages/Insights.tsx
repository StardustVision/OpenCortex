import React, { useCallback, useEffect, useState } from 'react';
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
  const { client, role } = useApi();
  const [reports, setReports] = useState<ReportMetadata[]>([]);
  const [selectedReportUri, setSelectedReportUri] = useState<string | null>(null);
  const [selectedReport, setSelectedReport] = useState<InsightsReport | null>(null);
  const [expandedSessions, setExpandedSessions] = useState<Record<string, boolean>>({});
  const [isHistoryLoading, setIsHistoryLoading] = useState(false);
  const [isReportLoading, setIsReportLoading] = useState(false);
  const [isGenerating, setIsGenerating] = useState(false);
  const [isFeatureDisabled, setIsFeatureDisabled] = useState(false);

  // Admin: user selection
  const [adminFilters, setAdminFilters] = useState({ tenant_id: '', user_id: '' });
  const [users, setUsers] = useState<{ tenant_id: string; user_id: string }[]>([]);

  const adminTid = adminFilters.tenant_id || undefined;
  const adminUid = adminFilters.user_id || undefined;

  useEffect(() => {
    if (role === 'admin' && client) {
      client.listTokens().then(res => {
        setUsers(res.tokens
          .filter((t: { role?: string }) => t.role !== 'admin')
          .map((t: { tenant_id: string; user_id: string }) => ({
            tenant_id: t.tenant_id, user_id: t.user_id,
          }))
        );
      }).catch(() => {});
    }
  }, [role, client]);

  // Reset selection when admin switches user
  useEffect(() => {
    setSelectedReportUri(null);
    setSelectedReport(null);
    setReports([]);
  }, [adminFilters.tenant_id, adminFilters.user_id]);

  const handleApiFailure = useCallback((error: unknown) => {
    if ((error as ApiError).status === 404) {
      setIsFeatureDisabled(true);
      return;
    }

    console.error('Insights request failed', error);
  }, []);

  const loadHistory = useCallback(async (nextSelection?: string | null) => {
    if (!client) return;

    setIsHistoryLoading(true);
    try {
      const response = await client.getInsightsHistory(10, adminTid, adminUid) as HistoryResult;
      if ('error' in response && response.error === 'feature disabled') {
        setIsFeatureDisabled(true);
        return;
      }

      const historyResponse = response as ReportHistoryResponse;

      setIsFeatureDisabled(false);
      setReports(historyResponse.reports);

      if (nextSelection !== undefined) {
        setSelectedReportUri(nextSelection);
        return;
      }

      setSelectedReportUri((currentSelection) => {
        if (!currentSelection) {
          return currentSelection;
        }

        const stillExists = historyResponse.reports.some(
          (report) => report.report_uri === currentSelection
        );
        return stillExists ? currentSelection : null;
      });
    } catch (error) {
      handleApiFailure(error);
    } finally {
      setIsHistoryLoading(false);
    }
  }, [client, handleApiFailure, adminTid, adminUid]);

  const loadReport = useCallback(async (reportUri: string) => {
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
  }, [client, handleApiFailure]);

  useEffect(() => {
    void loadHistory();
  }, [loadHistory]);

  useEffect(() => {
    if (!selectedReportUri) {
      setSelectedReport(null);
      return;
    }

    void loadReport(selectedReportUri);
  }, [loadReport, selectedReportUri]);

  const handleGenerate = async () => {
    if (!client) return;

    setIsGenerating(true);
    try {
      const response = await client.generateInsights(7, adminTid, adminUid);
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
          <Card className="space-y-3">
            <div className="flex items-center justify-between">
              <div>
                <h2 className="text-lg font-bold text-gray-900">Reports</h2>
                <p className="text-sm text-gray-500">
                  Weekly LLM-generated activity summaries.
                </p>
              </div>
              <Button onClick={() => { void handleGenerate(); }} loading={isGenerating}>
                <Sparkles size={16} className="mr-2" />
                Generate
              </Button>
            </div>
            {role === 'admin' && (
              <div className="flex gap-2">
                <select
                  className="flex-1 text-sm border border-indigo-200 rounded-md p-2 bg-indigo-50 outline-none focus:ring-2 focus:ring-indigo-500"
                  value={adminFilters.tenant_id}
                  onChange={(e) => setAdminFilters(f => ({ ...f, tenant_id: e.target.value, user_id: '' }))}
                >
                  <option value="">All Tenants</option>
                  {[...new Set(users.map(u => u.tenant_id))].map(tid => (
                    <option key={tid} value={tid}>{tid}</option>
                  ))}
                </select>
                <select
                  className="flex-1 text-sm border border-indigo-200 rounded-md p-2 bg-indigo-50 outline-none focus:ring-2 focus:ring-indigo-500"
                  value={adminFilters.user_id}
                  onChange={(e) => setAdminFilters(f => ({ ...f, user_id: e.target.value }))}
                >
                  <option value="">All Users</option>
                  {users
                    .filter(u => !adminFilters.tenant_id || u.tenant_id === adminFilters.tenant_id)
                    .map(u => <option key={u.user_id} value={u.user_id}>{u.user_id}</option>)
                  }
                </select>
              </div>
            )}
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
                  action={(
                    <Button onClick={() => { void handleGenerate(); }} loading={isGenerating}>
                      Generate Report
                    </Button>
                  )}
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
                <div className="mt-3 space-y-2 text-sm leading-7 text-indigo-950">
                  {Object.entries(selectedReport.at_a_glance).map(([key, value]) => (
                    <p key={key}>{value}</p>
                  ))}
                </div>
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
                    {selectedReport.what_works.map((item, i) => (
                      <li key={i}>{item}</li>
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
                    {selectedReport.suggestions.map((item, i) => (
                      <li key={i}>{item}</li>
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
                    {selectedReport.on_the_horizon.map((item, i) => (
                      <li key={i}>{item}</li>
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
                                  {facet.claude_helpfulness.replace(/_/g, ' ')}
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
                                  {Object.entries(facet.goal_categories).map(([category, count]) => (
                                    <Badge key={category} color="indigo">{category} ({count})</Badge>
                                  ))}
                                </div>
                              </div>

                              {facet.primary_success && facet.primary_success !== 'none' && (
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
