import React, { useState } from 'react';
import { PageLayout } from '../components/layout/PageLayout';
import { Card } from '../components/common/Card';
import { Button } from '../components/common/Button';
import { LoadingSpinner } from '../components/common/LoadingSpinner';
import { EmptyState } from '../components/common/EmptyState';
import { useApi } from '../api/Context';
import { SearchDebugResponse } from '../api/types';
import { 
  BarChart, 
  Bar, 
  XAxis, 
  YAxis, 
  CartesianGrid, 
  Tooltip, 
  ResponsiveContainer,
  Cell
} from 'recharts';
import { SearchCode, Info } from 'lucide-react';

export const SearchDebug: React.FC = () => {
  const { client } = useApi();
  const [query, setQuery] = useState('');
  const [limit, setLimit] = useState(5);
  const [loading, setLoading] = useState(false);
  const [debugData, setDebugData] = useState<SearchDebugResponse | null>(null);

  const handleSearch = async () => {
    if (!client || !query) return;
    setLoading(true);
    try {
      const res = await client.searchDebug(query, limit);
      setDebugData(res);
    } catch (error) {
      console.error('Debug search failed', error);
    } finally {
      setLoading(false);
    }
  };

  return (
    <PageLayout title="Search Debug">
      <div className="space-y-6">
        {/* Query Section */}
        <Card>
          <div className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Search Query</label>
              <textarea
                className="w-full px-4 py-2 border border-gray-200 rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-500 min-h-[100px] text-sm"
                placeholder="Enter a complex query to test search quality..."
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
            </div>
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-4">
                <div className="flex items-center gap-2">
                  <span className="text-sm text-gray-600">Limit:</span>
                  <input
                    type="number"
                    min="1"
                    max="20"
                    className="w-16 px-2 py-1 border border-gray-200 rounded text-sm"
                    value={limit}
                    onChange={(e) => setLimit(parseInt(e.target.value))}
                  />
                </div>
                {debugData && (
                  <div className="flex gap-4 text-xs text-gray-400">
                    <span>Fusion Beta: {debugData.fusion_beta}</span>
                    <span>Rerank Mode: {debugData.rerank_mode}</span>
                  </div>
                )}
              </div>
              <Button onClick={handleSearch} loading={loading} size="lg">
                Run Search Debug
              </Button>
            </div>
          </div>
        </Card>

        {loading ? (
          <LoadingSpinner className="h-12 w-12" />
        ) : debugData ? (
          <div className="space-y-6">
            {/* Overall Score Distribution */}
            <Card>
              <h3 className="text-sm font-bold text-gray-900 uppercase tracking-wider mb-6">Fused Score Distribution</h3>
              <div className="h-[300px] w-full">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart 
                    data={debugData.results} 
                    layout="vertical" 
                    margin={{ top: 5, right: 30, left: 100, bottom: 5 }}
                  >
                    <CartesianGrid strokeDasharray="3 3" horizontal={true} vertical={false} />
                    <XAxis type="number" domain={[0, 1]} />
                    <YAxis 
                      dataKey="abstract" 
                      type="category" 
                      width={100} 
                      tick={{ fontSize: 10 }}
                      tickFormatter={(val) => val.length > 20 ? val.substring(0, 20) + '...' : val}
                    />
                    <Tooltip 
                      formatter={(value: number) => value.toFixed(4)}
                      labelStyle={{ fontWeight: 'bold' }}
                    />
                    <Bar dataKey="fused_score" fill="#8b5cf6" radius={[0, 4, 4, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </Card>

            {/* Individual Result Cards */}
            <div className="space-y-4">
              {debugData.results.map((result) => (
                <Card key={result.rank} className="border-l-4 border-l-indigo-500">
                  <div className="flex flex-col gap-6">
                    <div className="flex items-start justify-between">
                      <div className="flex gap-4">
                        <span className="flex items-center justify-center w-8 h-8 rounded-full bg-indigo-50 text-indigo-600 font-bold text-sm">
                          #{result.rank}
                        </span>
                        <div>
                          <h4 className="text-md font-bold text-gray-900">{result.abstract}</h4>
                          <p className="text-xs font-mono text-gray-400 mt-1">{result.uri}</p>
                        </div>
                      </div>
                    </div>

                    <div className="grid grid-cols-1 md:grid-cols-2 gap-8 items-center">
                      <div className="h-[120px]">
                        <ResponsiveContainer width="100%" height="100%">
                          <BarChart 
                            data={[
                              { name: 'Vector', score: result.raw_vector_score },
                              { name: 'Rerank', score: result.rerank_score },
                              { name: 'Fused', score: result.fused_score },
                            ]}
                            layout="vertical"
                          >
                            <XAxis type="number" domain={[0, 1]} hide />
                            <YAxis dataKey="name" type="category" width={60} axisLine={false} tickLine={false} />
                            <Tooltip formatter={(v: number) => v.toFixed(4)} />
                            <Bar dataKey="score" radius={[0, 4, 4, 0]}>
                              <Cell fill="#6366f1" />
                              <Cell fill="#10b981" />
                              <Cell fill="#8b5cf6" />
                            </Bar>
                          </BarChart>
                        </ResponsiveContainer>
                      </div>

                      <div className="bg-gray-50 p-4 rounded-lg">
                        <div className="flex items-center gap-2 mb-2">
                          <Info size={14} className="text-gray-400" />
                          <span className="text-xs font-bold text-gray-500 uppercase">Score Breakdown</span>
                        </div>
                        <div className="space-y-2 text-sm">
                          <div className="flex justify-between">
                            <span className="text-gray-500">Vector Score:</span>
                            <span className="font-mono">{result.raw_vector_score.toFixed(4)}</span>
                          </div>
                          <div className="flex justify-between">
                            <span className="text-gray-500">Rerank Score:</span>
                            <span className="font-mono">{result.rerank_score.toFixed(4)}</span>
                          </div>
                          <div className="border-t border-gray-200 my-1 pt-1 flex justify-between font-bold text-indigo-600">
                            <span>Fused Score:</span>
                            <span className="font-mono">{result.fused_score.toFixed(4)}</span>
                          </div>
                          <p className="text-[10px] text-gray-400 mt-2 leading-tight">
                            {debugData.rerank_mode !== 'disabled' 
                              ? `Formula: ${debugData.fusion_beta} × rerank + ${(1 - debugData.fusion_beta).toFixed(2)} × vector`
                              : `Formula: raw_vector`}
                          </p>
                        </div>
                      </div>
                    </div>
                  </div>
                </Card>
              ))}
            </div>
          </div>
        ) : (
          <EmptyState 
            icon={<SearchCode size={48} className="text-gray-200" />}
            title="Search Quality Debugger"
            message="Enter a search query above to see how scores are calculated and how results are ranked."
          />
        )}
      </div>
    </PageLayout>
  );
};
