import React from 'react';
import { PageLayout } from '../components/layout/PageLayout';
import { Card } from '../components/common/Card';
import { Badge } from '../components/common/Badge';
import { LoadingSpinner } from '../components/common/LoadingSpinner';
import { useApi } from '../api/Context';
import { useFetch } from '../hooks/useFetch';
import { Link } from 'react-router-dom';
import { 
  Database, 
  Users, 
  Cpu, 
  Layers,
  Activity
} from 'lucide-react';

export const Dashboard: React.FC = () => {
  const { client, role } = useApi();

  const { data: health, loading: healthLoading, refetch: refetchHealth } = useFetch(
    () => client!.getHealth()
  );

  const { data: stats, loading: statsLoading, refetch: refetchStats } = useFetch(
    () => client!.getStats()
  );

  const { data: memories, loading: memoriesLoading, refetch: refetchMemories } = useFetch(
    () => role === 'admin'
      ? client!.listAllMemories({ limit: 10 })
      : client!.listMemories({ limit: 10 })
  );

  const onRefresh = () => {
    refetchHealth();
    refetchStats();
    refetchMemories();
  };

  const isLoading = healthLoading || statsLoading || memoriesLoading;

  return (
    <PageLayout title="Dashboard" onRefresh={onRefresh} isLoading={isLoading}>
      {/* Health Status Bar */}
      <Card className="mb-8 py-4">
        <div className="flex items-center gap-8 overflow-x-auto">
          <div className="flex items-center gap-2 shrink-0">
            <Activity size={18} className="text-gray-400" />
            <span className="text-sm font-medium text-gray-700">System Health:</span>
          </div>
          
          <div className="flex items-center gap-6">
            <HealthIndicator label="Storage" status={health?.storage} />
            <HealthIndicator label="Embedder" status={health?.embedder} />
            <HealthIndicator label="LLM" status={health?.llm} />
          </div>
        </div>
      </Card>

      {/* Stat Cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6 mb-8">
        <StatCard 
          icon={<Database size={24} />} 
          label="Total Records" 
          value={stats?.storage.total_records.toLocaleString() || '0'} 
          loading={statsLoading}
        />
        <StatCard 
          icon={<Users size={24} />} 
          label="Tenant / User" 
          value={stats ? `${stats.tenant_id} / ${stats.user_id}` : '-'} 
          loading={statsLoading}
          subtext="Active workspace"
        />
        <StatCard 
          icon={<Cpu size={24} />} 
          label="Embedder" 
          value={stats?.embedder || 'None'} 
          loading={statsLoading}
          subtext={stats?.has_llm ? 'LLM Enabled' : 'LLM Disabled'}
        />
        <StatCard 
          icon={<Layers size={24} />} 
          label="Rerank" 
          value={stats?.rerank.enabled ? 'Enabled' : 'Disabled'} 
          loading={statsLoading}
          subtext={stats ? `${stats.rerank.mode} (${stats.rerank.fusion_beta})` : ''}
        />
      </div>

      {/* Recent Memories */}
      <Card>
        <div className="flex items-center justify-between mb-6">
          <h2 className="text-lg font-bold text-gray-900">Recent Memories</h2>
          <Link to="/memories" className="text-sm font-medium text-indigo-600 hover:text-indigo-700">View all</Link>
        </div>

        {memoriesLoading ? (
          <LoadingSpinner />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left">
              <thead>
                <tr className="border-b border-gray-100">
                  <th className="pb-3 font-semibold text-gray-600 text-sm">Abstract</th>
                  <th className="pb-3 font-semibold text-gray-600 text-sm">Category</th>
                  <th className="pb-3 font-semibold text-gray-600 text-sm">Type</th>
                  <th className="pb-3 font-semibold text-gray-600 text-sm">Created</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-50">
                {memories?.results.map((memory) => (
                  <tr key={memory.uri} className="group hover:bg-gray-50 transition-colors cursor-pointer">
                    <td className="py-4 pr-4">
                      <Link to={`/memories?uri=${encodeURIComponent(memory.uri)}`} className="block truncate max-w-[400px] text-sm text-gray-900 font-medium">
                        {memory.abstract}
                      </Link>
                    </td>
                    <td className="py-4">
                      <Badge color="indigo">{memory.category}</Badge>
                    </td>
                    <td className="py-4">
                      <Badge color="gray">{memory.context_type}</Badge>
                    </td>
                    <td className="py-4 text-sm text-gray-500 whitespace-nowrap">
                      {new Date(memory.created_at).toLocaleDateString()}
                    </td>
                  </tr>
                ))}
                {memories?.results.length === 0 && (
                  <tr>
                    <td colSpan={4} className="py-8 text-center text-gray-500">No memories found.</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </PageLayout>
  );
};

const HealthIndicator: React.FC<{ label: string; status: boolean | undefined }> = ({ label, status }) => (
  <div className="flex items-center gap-2">
    <div className={`w-2 h-2 rounded-full ${status === true ? 'bg-green-500' : status === false ? 'bg-red-500' : 'bg-gray-300'}`} />
    <span className="text-sm text-gray-600">{label}</span>
  </div>
);

const StatCard: React.FC<{ icon: React.ReactNode; label: string; value: string; subtext?: string; loading?: boolean }> = ({ icon, label, value, subtext, loading }) => (
  <Card className="flex items-start gap-4">
    <div className="p-3 bg-indigo-50 text-indigo-600 rounded-lg shrink-0">
      {icon}
    </div>
    <div className="min-w-0">
      <p className="text-sm font-medium text-gray-500">{label}</p>
      {loading ? (
        <div className="h-7 w-20 bg-gray-100 animate-pulse rounded mt-1" />
      ) : (
        <p className="text-xl font-bold text-gray-900 truncate mt-1">{value}</p>
      )}
      {subtext && !loading && <p className="text-xs text-gray-400 mt-1 truncate">{subtext}</p>}
    </div>
  </Card>
);
