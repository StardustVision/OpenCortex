import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Brain, ChevronDown, ChevronUp, Copy, Trash2 } from 'lucide-react';
import { Badge } from '../components/common/Badge';
import { Button } from '../components/common/Button';
import { Card } from '../components/common/Card';
import { EmptyState } from '../components/common/EmptyState';
import { LoadingSpinner } from '../components/common/LoadingSpinner';
import { Modal } from '../components/common/Modal';
import { SearchInput } from '../components/common/SearchInput';
import { PageLayout } from '../components/layout/PageLayout';
import { useApi } from '../api/Context';
import { MemoryItem } from '../api/types';

const PAGE_SIZE = 20;

function selectionKey(memory: MemoryItem | null): string {
  if (!memory) return '';
  return [
    memory.uri,
    memory.source_tenant_id || '',
    memory.source_user_id || '',
  ].join('::');
}

function matchesUri(memory: MemoryItem, uri: string): boolean {
  return memory.uri === uri;
}

export const Memories: React.FC = () => {
  const { client, role } = useApi();
  const [searchParams, setSearchParams] = useSearchParams();
  const selectedUri = searchParams.get('uri');
  const [items, setItems] = useState<MemoryItem[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [filters, setFilters] = useState({ context_type: '', category: '' });
  const [adminFilters, setAdminFilters] = useState({
    tenant_id: '',
    user_id: '',
    project_id: '',
  });
  const [users, setUsers] = useState<
    { tenant_id: string; user_id: string }[]
  >([]);
  const [selected, setSelected] = useState<MemoryItem | null>(null);
  const [content, setContent] = useState({ abstract: '', overview: '', content: '' });
  const [contentLoading, setContentLoading] = useState(false);
  const [activeTab, setActiveTab] = useState<'l0' | 'l1' | 'l2'>('l0');
  const [showMetadata, setShowMetadata] = useState(false);
  const [isDeleteModalOpen, setIsDeleteModalOpen] = useState(false);
  const selectedKey = selectionKey(selected);
  const scope = useMemo(
    () => (role === 'admin' ? adminFilters : {}),
    [adminFilters, role],
  );

  useEffect(() => {
    if (role !== 'admin' || !client) return;
    client.listTokens()
      .then((res) => {
        setUsers(res.tokens
          .filter((token) => token.role !== 'admin')
          .map((token) => ({
            tenant_id: token.tenant_id,
            user_id: token.user_id,
          })));
      })
      .catch(() => {});
  }, [client, role]);

  const fetchMemories = useCallback(async (nextOffset: number, append = false) => {
    if (!client) return;
    setLoading(true);
    try {
      const response = searchQuery
        ? await client.searchMemories(
            { query: searchQuery, limit: PAGE_SIZE },
            scope,
          )
        : await client.listMemories({
            ...scope,
            limit: PAGE_SIZE,
            offset: nextOffset,
            context_type: filters.context_type || undefined,
            category: filters.category || undefined,
          });
      setItems((previous) => append ? [...previous, ...response.results] : response.results);
      setTotal(response.total);
    } catch (error) {
      console.error('Failed to fetch memories', error);
    } finally {
      setLoading(false);
    }
  }, [client, filters, scope, searchQuery]);

  useEffect(() => {
    setOffset(0);
    fetchMemories(0);
  }, [fetchMemories]);

  useEffect(() => {
    if (!selectedUri) {
      setSelected(null);
      return;
    }
    const next = items.find((item) => matchesUri(item, selectedUri)) || null;
    setSelected((previous) => {
      if (previous && next && selectionKey(previous) === selectionKey(next)) {
        return previous;
      }
      return next;
    });
  }, [items, selectedUri]);

  useEffect(() => {
    if (!client || !selected) return;
    let cancelled = false;
    setContentLoading(true);
    client.getMemoryContent(selected.uri)
      .then((response) => {
        if (!cancelled) {
          setContent(response);
        }
      })
      .catch((error) => {
        console.error('Failed to load memory content', error);
      })
      .finally(() => {
        if (!cancelled) setContentLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [client, selected]);

  const loadMore = () => {
    const nextOffset = offset + PAGE_SIZE;
    setOffset(nextOffset);
    fetchMemories(nextOffset, true);
  };

  const handleSelect = (memory: MemoryItem) => {
    setSelected(memory);
    setSearchParams({ uri: memory.uri });
  };

  const handleDelete = async () => {
    if (!client || !selected) return;
    try {
      await client.forgetMemory(selected.uri, scope);
      setIsDeleteModalOpen(false);
      setSearchParams({});
      await fetchMemories(0);
    } catch (error) {
      console.error('Delete failed', error);
    }
  };

  return (
    <PageLayout title="Memories" onRefresh={() => fetchMemories(0)} isLoading={loading}>
      <div className="flex h-[calc(100vh-160px)] gap-6 overflow-hidden">
        <section className="flex w-[40%] flex-col gap-4 overflow-hidden">
          <div className="space-y-3">
            <SearchInput onSearch={setSearchQuery} placeholder="Search memories..." />

            {role === 'admin' && (
              <div className="grid grid-cols-2 gap-2">
                <select
                  className="min-w-0 rounded-md border border-indigo-200 bg-indigo-50 p-2 text-sm outline-none focus:ring-2 focus:ring-indigo-500"
                  value={adminFilters.tenant_id}
                  onChange={(event) => setAdminFilters((current) => ({
                    ...current,
                    tenant_id: event.target.value,
                    user_id: '',
                  }))}
                >
                  <option value="">All tenants</option>
                  {[...new Set(users.map((user) => user.tenant_id))].map((tenantId) => (
                    <option key={tenantId} value={tenantId}>{tenantId}</option>
                  ))}
                </select>
                <select
                  className="min-w-0 rounded-md border border-indigo-200 bg-indigo-50 p-2 text-sm outline-none focus:ring-2 focus:ring-indigo-500"
                  value={adminFilters.user_id}
                  onChange={(event) => setAdminFilters((current) => ({
                    ...current,
                    user_id: event.target.value,
                  }))}
                >
                  <option value="">All users</option>
                  {users
                    .filter((user) => !adminFilters.tenant_id || user.tenant_id === adminFilters.tenant_id)
                    .map((user) => (
                      <option key={`${user.tenant_id}:${user.user_id}`} value={user.user_id}>
                        {user.user_id}
                      </option>
                    ))}
                </select>
              </div>
            )}

            <div className="flex gap-2">
              <select
                className="flex-1 rounded-md border border-gray-200 bg-white p-2 text-sm outline-none focus:ring-2 focus:ring-indigo-500"
                value={filters.context_type}
                onChange={(event) => setFilters((current) => ({
                  ...current,
                  context_type: event.target.value,
                }))}
              >
                <option value="">All types</option>
                <option value="memory">Memory</option>
                <option value="resource">Resource</option>
              </select>
              <select
                className="flex-1 rounded-md border border-gray-200 bg-white p-2 text-sm outline-none focus:ring-2 focus:ring-indigo-500"
                value={filters.category}
                onChange={(event) => setFilters((current) => ({
                  ...current,
                  category: event.target.value,
                }))}
              >
                <option value="">All categories</option>
                {['semantic', 'episodic', 'procedural', 'events'].map((category) => (
                  <option key={category} value={category}>{category}</option>
                ))}
              </select>
            </div>
          </div>

          <div className="flex-1 space-y-3 overflow-y-auto pr-2">
            {items.map((memory) => (
              <button
                key={selectionKey(memory)}
                type="button"
                onClick={() => handleSelect(memory)}
                className={`block w-full rounded-lg border p-4 text-left transition-all ${
                  selectedKey === selectionKey(memory)
                    ? 'border-indigo-500 bg-indigo-50'
                    : 'border-gray-200 bg-white hover:border-gray-300'
                }`}
              >
                <p className="mb-2 line-clamp-2 text-sm font-medium text-gray-900">
                  {memory.abstract || memory.uri}
                </p>
                <div className="flex items-center justify-between gap-3">
                  <div className="flex min-w-0 flex-wrap gap-2">
                    {memory.category && <Badge color="indigo">{memory.category}</Badge>}
                    <Badge color="gray">{memory.context_type}</Badge>
                    {role === 'admin' && memory.source_tenant_id && (
                      <Badge color="green">
                        {memory.source_tenant_id}/{memory.source_user_id}
                      </Badge>
                    )}
                  </div>
                  {memory.score != null && (
                    <span className="shrink-0 text-xs text-gray-400">
                      {Number(memory.score).toFixed(2)}
                    </span>
                  )}
                </div>
              </button>
            ))}

            {loading && <LoadingSpinner />}
            {!loading && !searchQuery && items.length < total && (
              <Button variant="ghost" className="w-full py-4 font-medium text-indigo-600" onClick={loadMore}>
                Load More
              </Button>
            )}
            {!loading && items.length === 0 && (
              <div className="rounded-lg border border-dashed border-gray-300 bg-white py-12 text-center text-gray-500">
                No memories found.
              </div>
            )}
          </div>
        </section>

        <section className="w-[60%] overflow-y-auto pr-2">
          {selected ? (
            <div key={selectedKey} className="space-y-6">
              <Card>
                <div className="mb-4 flex items-start justify-between gap-4">
                  <h2 className="text-xl font-bold text-gray-900">
                    {selected.abstract || selected.uri}
                  </h2>
                  <div className="flex shrink-0 gap-2">
                    {selected.category && <Badge color="indigo">{selected.category}</Badge>}
                    <Badge color="gray">{selected.context_type}</Badge>
                  </div>
                </div>

                <div className="mb-6 flex items-center gap-2 rounded bg-gray-50 p-2 font-mono text-xs text-gray-500">
                  <span className="truncate">{selected.uri}</span>
                  <button
                    type="button"
                    onClick={() => navigator.clipboard.writeText(selected.uri)}
                    className="shrink-0 p-1 hover:text-indigo-600"
                  >
                    <Copy size={14} />
                  </button>
                </div>

                <div className="mb-6 border-b border-gray-200">
                  <nav className="flex gap-8">
                    <TabButton active={activeTab === 'l0'} onClick={() => setActiveTab('l0')}>Abstract</TabButton>
                    <TabButton active={activeTab === 'l1'} onClick={() => setActiveTab('l1')}>Overview</TabButton>
                    <TabButton active={activeTab === 'l2'} onClick={() => setActiveTab('l2')}>Content</TabButton>
                  </nav>
                </div>

                <div className="min-h-[220px]">
                  {contentLoading ? (
                    <LoadingSpinner />
                  ) : (
                    <div className="prose prose-sm max-w-none whitespace-pre-wrap text-gray-700">
                      {activeTab === 'l0' && (content.abstract || selected.abstract)}
                      {activeTab === 'l1' && (content.overview || selected.overview || 'No overview available.')}
                      {activeTab === 'l2' && (content.content || selected.content || 'No content available.')}
                    </div>
                  )}
                </div>
              </Card>

              <Card className="overflow-hidden p-0">
                <button
                  type="button"
                  className="flex w-full items-center justify-between p-4 transition-colors hover:bg-gray-50"
                  onClick={() => setShowMetadata(!showMetadata)}
                >
                  <span className="text-sm font-semibold uppercase tracking-wider text-gray-700">
                    Technical Metadata
                  </span>
                  {showMetadata ? <ChevronUp size={18} /> : <ChevronDown size={18} />}
                </button>
                {showMetadata && (
                  <div className="overflow-x-auto border-t border-gray-100 bg-gray-50 p-4">
                    <pre className="rounded border border-gray-200 bg-white p-4 text-xs text-gray-600">
                      {JSON.stringify(selected, null, 2)}
                    </pre>
                  </div>
                )}
              </Card>

              <div className="flex justify-end pb-8 pt-4">
                <Button variant="danger" onClick={() => setIsDeleteModalOpen(true)}>
                  <Trash2 size={18} className="mr-2" /> Delete Memory
                </Button>
              </div>
            </div>
          ) : (
            <div className="flex h-full items-center justify-center">
              <EmptyState
                icon={<Brain size={48} className="text-gray-200" />}
                title="Select a memory"
                message="Choose a memory from the list to view its details and content."
              />
            </div>
          )}
        </section>
      </div>

      <Modal
        isOpen={isDeleteModalOpen}
        onClose={() => setIsDeleteModalOpen(false)}
        title="Confirm Deletion"
        footer={(
          <>
            <Button variant="ghost" onClick={() => setIsDeleteModalOpen(false)}>Cancel</Button>
            <Button variant="danger" onClick={handleDelete}>Delete Permanently</Button>
          </>
        )}
      >
        <p className="text-gray-600">
          Are you sure you want to delete this memory? This removes the stored tree and its vector projections.
        </p>
      </Modal>
    </PageLayout>
  );
};

const TabButton: React.FC<{
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}> = ({ active, onClick, children }) => (
  <button
    type="button"
    onClick={onClick}
    className={`border-b-2 pb-4 text-sm font-medium transition-colors ${
      active
        ? 'border-indigo-600 text-indigo-600'
        : 'border-transparent text-gray-500 hover:text-gray-700'
    }`}
  >
    {children}
  </button>
);
