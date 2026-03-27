import React, { useState } from 'react';
import { PageLayout } from '../components/layout/PageLayout';
import { Card } from '../components/common/Card';
import { Button } from '../components/common/Button';
import { Badge } from '../components/common/Badge';
import { Modal } from '../components/common/Modal';
import { LoadingSpinner } from '../components/common/LoadingSpinner';
import { EmptyState } from '../components/common/EmptyState';
import { useApi } from '../api/Context';
import { useFetch } from '../hooks/useFetch';
import { TokenRecord } from '../api/types';
import { Key, Plus, Trash2, Copy, Check, AlertTriangle } from 'lucide-react';

export const Tokens: React.FC = () => {
  const { client, role } = useApi();
  const { data, loading, refetch } = useFetch(() => client!.listTokens());

  const [showCreate, setShowCreate] = useState(false);
  const [tenantId, setTenantId] = useState('');
  const [userId, setUserId] = useState('');
  const [creating, setCreating] = useState(false);
  const [createdToken, setCreatedToken] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const [revokeTarget, setRevokeTarget] = useState<TokenRecord | null>(null);
  const [revoking, setRevoking] = useState(false);

  if (role !== 'admin') {
    return (
      <PageLayout title="Tokens">
        <EmptyState
          icon={<AlertTriangle size={48} className="text-red-300" />}
          title="Access Denied"
          message="Admin access is required to manage tokens."
        />
      </PageLayout>
    );
  }

  const handleCreate = async () => {
    if (!client || !tenantId.trim() || !userId.trim()) return;
    setCreating(true);
    try {
      const res = await client.createToken(tenantId.trim(), userId.trim());
      setCreatedToken(res.token);
      setTenantId('');
      setUserId('');
      refetch();
    } catch (e) {
      console.error('Failed to create token', e);
    } finally {
      setCreating(false);
    }
  };

  const handleRevoke = async () => {
    if (!client || !revokeTarget) return;
    setRevoking(true);
    try {
      await client.revokeToken(revokeTarget.token_prefix.replace('...', ''));
      setRevokeTarget(null);
      refetch();
    } catch (e) {
      console.error('Failed to revoke token', e);
    } finally {
      setRevoking(false);
    }
  };

  const copyToken = (text: string) => {
    navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const tokens = data?.tokens || [];

  return (
    <PageLayout title="Token Management" onRefresh={refetch} isLoading={loading}>
      <div className="space-y-6">
        {/* Token List */}
        <Card>
          <div className="flex items-center justify-between mb-6">
            <h2 className="text-lg font-bold text-gray-900">Issued Tokens</h2>
            <Button onClick={() => setShowCreate(!showCreate)}>
              <Plus size={16} className="mr-2" /> New Token
            </Button>
          </div>

          {loading ? (
            <LoadingSpinner />
          ) : tokens.length === 0 ? (
            <EmptyState
              icon={<Key size={48} className="text-gray-200" />}
              title="No tokens"
              message="No tokens have been issued yet."
            />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left">
                <thead>
                  <tr className="border-b border-gray-100">
                    <th className="pb-3 text-sm font-semibold text-gray-600">Tenant</th>
                    <th className="pb-3 text-sm font-semibold text-gray-600">User</th>
                    <th className="pb-3 text-sm font-semibold text-gray-600">Role</th>
                    <th className="pb-3 text-sm font-semibold text-gray-600">Created</th>
                    <th className="pb-3 text-sm font-semibold text-gray-600">Token Prefix</th>
                    <th className="pb-3 text-sm font-semibold text-gray-600">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-50">
                  {tokens.map((t, i) => (
                    <tr key={i} className={`hover:bg-gray-50 ${t.role === 'admin' ? 'bg-indigo-50/50' : ''}`}>
                      <td className="py-3 text-sm text-gray-900">{t.tenant_id}</td>
                      <td className="py-3 text-sm text-gray-900">{t.user_id}</td>
                      <td className="py-3">
                        <Badge color={t.role === 'admin' ? 'indigo' : 'gray'}>{t.role}</Badge>
                      </td>
                      <td className="py-3 text-sm text-gray-500">
                        {t.created_at ? new Date(t.created_at).toLocaleDateString() : '-'}
                      </td>
                      <td className="py-3 text-xs font-mono text-gray-400">{t.token_prefix}</td>
                      <td className="py-3">
                        <div className="flex gap-1">
                          <Button variant="ghost" size="sm" className="text-gray-400 hover:text-indigo-600 hover:bg-indigo-50" onClick={() => copyToken(t.token)}>
                            <Copy size={14} />
                          </Button>
                          {t.role !== 'admin' && (
                            <Button variant="ghost" size="sm" className="text-red-500 hover:bg-red-50" onClick={() => setRevokeTarget(t)}>
                              <Trash2 size={14} />
                            </Button>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        {/* Create Token Form */}
        {showCreate && (
          <Card>
            <h3 className="text-md font-bold text-gray-900 mb-4">Generate New Token</h3>
            <div className="flex gap-4 items-end">
              <div className="flex-1">
                <label className="block text-sm font-medium text-gray-700 mb-1">Tenant ID</label>
                <input
                  className="w-full px-3 py-2 border border-gray-200 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
                  value={tenantId}
                  onChange={(e) => setTenantId(e.target.value)}
                  placeholder="e.g. netops"
                />
              </div>
              <div className="flex-1">
                <label className="block text-sm font-medium text-gray-700 mb-1">User ID</label>
                <input
                  className="w-full px-3 py-2 border border-gray-200 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
                  value={userId}
                  onChange={(e) => setUserId(e.target.value)}
                  placeholder="e.g. john"
                />
              </div>
              <Button onClick={handleCreate} loading={creating} disabled={!tenantId.trim() || !userId.trim()}>
                Generate
              </Button>
            </div>
          </Card>
        )}

        {/* Created Token Display */}
        {createdToken && (
          <Card className="border-green-200 bg-green-50">
            <div className="flex items-start gap-4">
              <Check size={20} className="text-green-600 mt-1 shrink-0" />
              <div className="flex-1 min-w-0">
                <p className="text-sm font-bold text-green-800 mb-2">Token created — copy it now, it will not be shown again.</p>
                <div className="flex items-center gap-2 bg-white border border-green-200 rounded p-2">
                  <code className="text-xs text-gray-700 truncate flex-1">{createdToken}</code>
                  <button
                    onClick={() => copyToken(createdToken)}
                    className="p-1 hover:bg-green-100 rounded shrink-0"
                  >
                    {copied ? <Check size={16} className="text-green-600" /> : <Copy size={16} className="text-gray-400" />}
                  </button>
                </div>
              </div>
              <button onClick={() => setCreatedToken(null)} className="text-gray-400 hover:text-gray-600 text-sm">Dismiss</button>
            </div>
          </Card>
        )}
      </div>

      {/* Revoke Confirmation Modal */}
      <Modal
        isOpen={!!revokeTarget}
        onClose={() => setRevokeTarget(null)}
        title="Revoke Token"
        footer={
          <>
            <Button variant="ghost" onClick={() => setRevokeTarget(null)}>Cancel</Button>
            <Button variant="danger" onClick={handleRevoke} loading={revoking}>Revoke</Button>
          </>
        }
      >
        <p className="text-gray-600">
          Revoke token for <strong>{revokeTarget?.tenant_id}/{revokeTarget?.user_id}</strong>? This cannot be undone.
        </p>
      </Modal>
    </PageLayout>
  );
};
