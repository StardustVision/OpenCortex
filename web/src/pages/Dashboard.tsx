import React from 'react';
import { Link } from 'react-router-dom';
import { Activity, Database, Layers, Users } from 'lucide-react';
import { Badge } from '../components/common/Badge';
import { Card } from '../components/common/Card';
import { LoadingSpinner } from '../components/common/LoadingSpinner';
import { PageLayout } from '../components/layout/PageLayout';
import { useApi } from '../api/Context';
import { useFetch } from '../hooks/useFetch';

export const Dashboard: React.FC = () => {
  const { client } = useApi();
  const {
    data: stats,
    loading: statsLoading,
    refetch: refetchStats,
  } = useFetch(() => client!.getConsoleStats());
  const {
    data: memories,
    loading: memoriesLoading,
    refetch: refetchMemories,
  } = useFetch(() => client!.listMemories({ limit: 10 }));

  const isLoading = statsLoading || memoriesLoading;

  return (
    <PageLayout
      title="Dashboard"
      onRefresh={() => {
        refetchStats();
        refetchMemories();
      }}
      isLoading={isLoading}
    >
      <Card className="mb-8 py-4">
        <div className="flex items-center gap-8 overflow-x-auto">
          <div className="flex shrink-0 items-center gap-2">
            <Activity size={18} className="text-gray-400" />
            <span className="text-sm font-medium text-gray-700">Console Scope</span>
          </div>
          <div className="flex items-center gap-3 text-sm text-gray-600">
            <Badge color="gray">{stats?.tenant_id || '-'}</Badge>
            <Badge color="gray">{stats?.user_id || '-'}</Badge>
            <Badge color="gray">{stats?.project_id || '-'}</Badge>
            <Badge color={stats?.role === 'admin' ? 'indigo' : 'gray'}>
              {stats?.role || 'user'}
            </Badge>
          </div>
        </div>
      </Card>

      <div className="mb-8 grid grid-cols-1 gap-6 md:grid-cols-2 lg:grid-cols-4">
        <StatCard
          icon={<Database size={24} />}
          label="Vector Records"
          value={stats?.total_records.toLocaleString() || '0'}
          loading={statsLoading}
        />
        <StatCard
          icon={<Layers size={24} />}
          label="Primary Records"
          value={stats?.primary_records.toLocaleString() || '0'}
          loading={statsLoading}
        />
        <StatCard
          icon={<Users size={24} />}
          label="Memory"
          value={(stats?.by_context_type.memory || 0).toLocaleString()}
          loading={statsLoading}
        />
        <StatCard
          icon={<Database size={24} />}
          label="Resource"
          value={(stats?.by_context_type.resource || 0).toLocaleString()}
          loading={statsLoading}
        />
      </div>

      <Card>
        <div className="mb-6 flex items-center justify-between">
          <h2 className="text-lg font-bold text-gray-900">Recent Memories</h2>
          <Link to="/memories" className="text-sm font-medium text-indigo-600 hover:text-indigo-700">
            View all
          </Link>
        </div>

        {memoriesLoading ? (
          <LoadingSpinner />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left">
              <thead>
                <tr className="border-b border-gray-100">
                  <th className="pb-3 text-sm font-semibold text-gray-600">Abstract</th>
                  <th className="pb-3 text-sm font-semibold text-gray-600">Category</th>
                  <th className="pb-3 text-sm font-semibold text-gray-600">Type</th>
                  <th className="pb-3 text-sm font-semibold text-gray-600">Scope</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-50">
                {memories?.results.map((memory) => (
                  <tr key={memory.uri} className="group cursor-pointer transition-colors hover:bg-gray-50">
                    <td className="py-4 pr-4">
                      <Link
                        to={`/memories?uri=${encodeURIComponent(memory.uri)}`}
                        className="block max-w-[520px] truncate text-sm font-medium text-gray-900"
                      >
                        {memory.abstract || memory.uri}
                      </Link>
                    </td>
                    <td className="py-4">
                      {memory.category && <Badge color="indigo">{memory.category}</Badge>}
                    </td>
                    <td className="py-4">
                      <Badge color="gray">{memory.context_type}</Badge>
                    </td>
                    <td className="py-4">
                      <Badge color="gray">{memory.scope || '-'}</Badge>
                    </td>
                  </tr>
                ))}
                {memories?.results.length === 0 && (
                  <tr>
                    <td colSpan={4} className="py-8 text-center text-gray-500">
                      No memories found.
                    </td>
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

const StatCard: React.FC<{
  icon: React.ReactNode;
  label: string;
  value: string;
  loading?: boolean;
}> = ({ icon, label, value, loading }) => (
  <Card>
    <div className="mb-4 flex items-center justify-between">
      <div className="rounded-lg bg-indigo-50 p-2 text-indigo-600">{icon}</div>
    </div>
    <div>
      <p className="mb-1 text-sm font-medium text-gray-500">{label}</p>
      {loading ? (
        <div className="h-8 w-20 animate-pulse rounded bg-gray-100" />
      ) : (
        <p className="text-2xl font-bold text-gray-900">{value}</p>
      )}
    </div>
  </Card>
);

