import React, { useState, useEffect } from 'react';
import { PageLayout } from '../components/layout/PageLayout';
import { Card } from '../components/common/Card';
import { Badge } from '../components/common/Badge';
import { Button } from '../components/common/Button';
import { LoadingSpinner } from '../components/common/LoadingSpinner';
import { EmptyState } from '../components/common/EmptyState';
import { SearchInput } from '../components/common/SearchInput';
import { useApi } from '../api/Context';
import { KnowledgeCandidate, ArchivistStatus } from '../api/types';
import { 
  BookOpen, 
  Check, 
  X as XIcon, 
  ChevronDown, 
  ChevronUp, 
  Play,
  AlertCircle
} from 'lucide-react';

export const Knowledge: React.FC = () => {
  const { client } = useApi();
  const [activeTab, setActiveTab] = useState<'candidates' | 'approved'>('candidates');
  const [candidates, setCandidates] = useState<KnowledgeCandidate[]>([]);
  const [archivistStatus, setArchivistStatus] = useState<ArchivistStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [expandedRow, setExpandedRow] = useState<string | null>(null);
  const [isFeatureDisabled, setIsFeatureDisabled] = useState(false);

  // Approved knowledge state
  const [approvedResults, setApprovedResults] = useState<any[]>([]);
  const [approvedLoading, setApprovedLoading] = useState(false);

  const fetchData = async () => {
    if (!client) return;
    setLoading(true);
    try {
      const [candRes, statusRes] = await Promise.all([
        client.getKnowledgeCandidates(),
        client.getArchivistStatus()
      ]);

      if ('error' in candRes || 'error' in statusRes) {
        setIsFeatureDisabled(true);
        return;
      }

      setCandidates(candRes.candidates);
      setArchivistStatus(statusRes);
    } catch (error) {
      console.error('Failed to fetch knowledge data', error);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchData();
  }, [client]);

  const handleApprove = async (id: string) => {
    if (!client) return;
    try {
      await client.approveKnowledge(id);
      fetchData();
    } catch (error) {
      console.error('Approve failed', error);
    }
  };

  const handleReject = async (id: string) => {
    if (!client) return;
    try {
      await client.rejectKnowledge(id);
      fetchData();
    } catch (error) {
      console.error('Reject failed', error);
    }
  };

  const handleTriggerArchivist = async () => {
    if (!client) return;
    try {
      await client.triggerArchivist();
      fetchData();
    } catch (error) {
      console.error('Trigger failed', error);
    }
  };

  const handleSearchApproved = async (query: string) => {
    if (!client || !query) {
      setApprovedResults([]);
      return;
    }
    setApprovedLoading(true);
    try {
      const res = await client.searchKnowledge({ query });
      if ('results' in res) {
        setApprovedResults(res.results);
      }
    } catch (error) {
      console.error('Search approved failed', error);
    } finally {
      setApprovedLoading(false);
    }
  };

  if (isFeatureDisabled) {
    return (
      <PageLayout title="Knowledge">
        <div className="flex flex-col items-center justify-center py-24">
          <Card className="max-w-md text-center">
            <AlertCircle size={48} className="text-yellow-500 mx-auto mb-4" />
            <h2 className="text-xl font-bold text-gray-900 mb-2">Feature Disabled</h2>
            <p className="text-gray-600">
              The Knowledge and Archivist features are currently disabled in this environment.
            </p>
          </Card>
        </div>
      </PageLayout>
    );
  }

  return (
    <PageLayout title="Knowledge" onRefresh={fetchData} isLoading={loading}>
      <div className="flex gap-8 border-b border-gray-200 mb-8">
        <button 
          onClick={() => setActiveTab('candidates')}
          className={`pb-4 text-sm font-medium transition-colors border-b-2 ${
            activeTab === 'candidates' ? 'text-indigo-600 border-indigo-600' : 'text-gray-500 border-transparent hover:text-gray-700'
          }`}
        >
          Candidates
        </button>
        <button 
          onClick={() => setActiveTab('approved')}
          className={`pb-4 text-sm font-medium transition-colors border-b-2 ${
            activeTab === 'approved' ? 'text-indigo-600 border-indigo-600' : 'text-gray-500 border-transparent hover:text-gray-700'
          }`}
        >
          Approved Knowledge
        </button>
      </div>

      {activeTab === 'candidates' ? (
        <div className="space-y-6">
          {/* Archivist Status Bar */}
          <Card className="flex items-center justify-between">
            <div className="flex items-center gap-8">
              <div className="space-y-1">
                <p className="text-xs font-medium text-gray-500 uppercase">Archivist Status</p>
                <div className="flex items-center gap-2">
                  <div className={`w-2 h-2 rounded-full ${archivistStatus?.enabled ? 'bg-green-500' : 'bg-gray-300'}`} />
                  <span className="text-sm font-semibold text-gray-900">
                    {archivistStatus?.enabled ? (archivistStatus.running ? 'Running' : 'Enabled') : 'Disabled'}
                  </span>
                </div>
              </div>
              <div className="space-y-1 border-l border-gray-100 pl-8">
                <p className="text-xs font-medium text-gray-500 uppercase">Trigger Mode</p>
                <p className="text-sm font-semibold text-gray-900">{archivistStatus?.trigger_mode || 'Manual'}</p>
              </div>
              <div className="space-y-1 border-l border-gray-100 pl-8">
                <p className="text-xs font-medium text-gray-500 uppercase">Last Run</p>
                <p className="text-sm font-semibold text-gray-900">
                  {archivistStatus?.last_run_at ? new Date(archivistStatus.last_run_at).toLocaleString() : 'Never'}
                </p>
              </div>
            </div>
            <Button onClick={handleTriggerArchivist} disabled={archivistStatus?.running}>
              <Play size={16} className="mr-2 fill-current" /> Trigger Extraction
            </Button>
          </Card>

          {/* Candidates Table */}
          <Card className="p-0 overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full text-left">
                <thead className="bg-gray-50 border-b border-gray-200">
                  <tr>
                    <th className="px-6 py-4 text-xs font-semibold text-gray-500 uppercase tracking-wider">Abstract</th>
                    <th className="px-6 py-4 text-xs font-semibold text-gray-500 uppercase tracking-wider">Type</th>
                    <th className="px-6 py-4 text-xs font-semibold text-gray-500 uppercase tracking-wider">Scope</th>
                    <th className="px-6 py-4 text-xs font-semibold text-gray-500 uppercase tracking-wider">Created</th>
                    <th className="px-6 py-4 text-xs font-semibold text-gray-500 uppercase tracking-wider text-right">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {candidates.map((cand) => (
                    <React.Fragment key={cand.knowledge_id}>
                      <tr 
                        className="hover:bg-gray-50 cursor-pointer transition-colors"
                        onClick={() => setExpandedRow(expandedRow === cand.knowledge_id ? null : cand.knowledge_id)}
                      >
                        <td className="px-6 py-4">
                          <div className="flex items-center gap-3">
                            {expandedRow === cand.knowledge_id ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
                            <span className="text-sm font-medium text-gray-900 truncate max-w-md">{cand.abstract}</span>
                          </div>
                        </td>
                        <td className="px-6 py-4">
                          <Badge color="indigo">{cand.knowledge_type}</Badge>
                        </td>
                        <td className="px-6 py-4">
                          <Badge color="gray">{cand.scope}</Badge>
                        </td>
                        <td className="px-6 py-4 text-sm text-gray-500 whitespace-nowrap">
                          {new Date(cand.created_at).toLocaleDateString()}
                        </td>
                        <td className="px-6 py-4 text-right">
                          <div className="flex justify-end gap-2" onClick={e => e.stopPropagation()}>
                            <button 
                              onClick={() => handleApprove(cand.knowledge_id)}
                              className="p-1.5 text-green-600 hover:bg-green-50 rounded-md transition-colors"
                              title="Approve"
                            >
                              <Check size={18} />
                            </button>
                            <button 
                              onClick={() => handleReject(cand.knowledge_id)}
                              className="p-1.5 text-red-600 hover:bg-red-50 rounded-md transition-colors"
                              title="Reject"
                            >
                              <XIcon size={18} />
                            </button>
                          </div>
                        </td>
                      </tr>
                      {expandedRow === cand.knowledge_id && (
                        <tr className="bg-gray-50">
                          <td colSpan={5} className="px-12 py-6">
                            <div className="space-y-4">
                              <div>
                                <h4 className="text-xs font-bold text-gray-400 uppercase mb-2">Overview</h4>
                                <p className="text-sm text-gray-700 whitespace-pre-wrap leading-relaxed">
                                  {cand.overview || 'No overview available.'}
                                </p>
                              </div>
                              {cand.source_trace_ids && cand.source_trace_ids.length > 0 && (
                                <div>
                                  <h4 className="text-xs font-bold text-gray-400 uppercase mb-2">Sources</h4>
                                  <div className="flex flex-wrap gap-2">
                                    {cand.source_trace_ids.map(id => (
                                      <Badge key={id} color="blue">{id}</Badge>
                                    ))}
                                  </div>
                                </div>
                              )}
                            </div>
                          </td>
                        </tr>
                      )}
                    </React.Fragment>
                  ))}
                  {candidates.length === 0 && (
                    <tr>
                      <td colSpan={5} className="py-12">
                        <EmptyState 
                          icon={<BookOpen size={48} className="text-gray-200" />}
                          title="No pending candidates"
                          message="There are no knowledge candidates waiting for approval."
                        />
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </Card>
        </div>
      ) : (
        <div className="space-y-6">
          <Card>
            <SearchInput onSearch={handleSearchApproved} placeholder="Search approved knowledge..." />
          </Card>
          
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            {approvedResults.map((result, idx) => (
              <Card key={idx} className="flex flex-col h-full">
                <div className="flex items-start justify-between gap-4 mb-4">
                  <h3 className="text-md font-bold text-gray-900">{result.abstract}</h3>
                  <div className="flex gap-2 shrink-0">
                    <Badge color="indigo">{result.knowledge_type}</Badge>
                    <Badge color="gray">{result.scope}</Badge>
                  </div>
                </div>
                <p className="text-sm text-gray-600 line-clamp-4 flex-1 mb-4">
                  {result.overview}
                </p>
                {result.source_trace_ids && (
                  <div className="flex flex-wrap gap-1 mt-auto">
                    {result.source_trace_ids.slice(0, 3).map((id: string) => (
                      <Badge key={id} color="blue" className="text-[10px] px-1.5">{id}</Badge>
                    ))}
                    {result.source_trace_ids.length > 3 && (
                      <span className="text-[10px] text-gray-400">+{result.source_trace_ids.length - 3} more</span>
                    )}
                  </div>
                )}
              </Card>
            ))}
            {approvedLoading && <LoadingSpinner />}
            {!approvedLoading && approvedResults.length === 0 && (
              <div className="col-span-full">
                <EmptyState 
                  icon={<BookOpen size={48} className="text-gray-200" />}
                  title="Search Approved Knowledge"
                  message="Enter a query to find existing knowledge records."
                />
              </div>
            )}
          </div>
        </div>
      )}
    </PageLayout>
  );
};
