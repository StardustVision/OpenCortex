import React, { useState, useEffect, useCallback } from 'react';
import { useSearchParams } from 'react-router-dom';
import { PageLayout } from '../components/layout/PageLayout';
import { Card } from '../components/common/Card';
import { Badge } from '../components/common/Badge';
import { Button } from '../components/common/Button';
import { SearchInput } from '../components/common/SearchInput';
import { LoadingSpinner } from '../components/common/LoadingSpinner';
import { Modal } from '../components/common/Modal';
import { EmptyState } from '../components/common/EmptyState';
import { useApi } from '../api/Context';
import { SearchResult, MemoryItem } from '../api/types';
import { 
  Brain, 
  ThumbsUp, 
  ThumbsDown, 
  Trash2, 
  Copy, 
  ChevronDown,
  ChevronUp
} from 'lucide-react';

export const Memories: React.FC = () => {
  const { client, role } = useApi();
  const [searchParams, setSearchParams] = useSearchParams();
  const selectedUri = searchParams.get('uri');

  const [memories, setMemories] = useState<MemoryItem[]>([]);
  const [, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [isSearchMode, setIsSearchMode] = useState(false);
  const [filters, setFilters] = useState({
    context_type: '',
    category: '',
    detail_level: 'l0'
  });
  const [offset, setOffset] = useState(0);
  const [selectedMemory, setSelectedMemory] = useState<MemoryItem | null>(null);
  const [content, setContent] = useState({ abstract: '', overview: '', full: '' });
  const [contentLoading, setContentLoading] = useState(false);
  const [activeTab, setActiveTab] = useState<'l0' | 'l1' | 'l2'>('l0');
  const [isDeleteModalOpen, setIsDeleteModalOpen] = useState(false);
  const [showMetadata, setShowMetadata] = useState(false);
  const [adminFilters, setAdminFilters] = useState({ tenant_id: '', user_id: '' });
  const [users, setUsers] = useState<{ tenant_id: string; user_id: string }[]>([]);

  useEffect(() => {
    if (role === 'admin' && client) {
      client.listTokens().then(res => {
        setUsers(res.tokens
          .filter(t => t.role !== 'admin')
          .map(t => ({ tenant_id: t.tenant_id, user_id: t.user_id }))
        );
      }).catch(() => {});
    }
  }, [role, client]);

  const fetchMemories = useCallback(async (query: string, currentOffset: number, append = false) => {
    if (!client) return;
    setLoading(true);
    try {
      let results: MemoryItem[] = [];
      let totalCount = 0;

      if (query) {
        const res = await client.searchMemories({
          query,
          limit: 20,
          context_type: filters.context_type || undefined,
          category: filters.category || undefined,
          detail_level: filters.detail_level
        });
        results = res.results;
        totalCount = res.total;
        setIsSearchMode(true);
      } else if (role === 'admin') {
        const res = await client.listAllMemories({
          tenant_id: adminFilters.tenant_id || undefined,
          user_id: adminFilters.user_id || undefined,
          limit: 20,
          offset: currentOffset,
          context_type: filters.context_type || undefined,
          category: filters.category || undefined,
        });
        results = res.results;
        totalCount = res.total;
        setIsSearchMode(false);
      } else {
        const res = await client.listMemories({
          limit: 20,
          offset: currentOffset,
          context_type: filters.context_type || undefined,
          category: filters.category || undefined
        });
        results = res.results;
        totalCount = res.total;
        setIsSearchMode(false);
      }

      setMemories(prev => append ? [...prev, ...results] : results);
      setTotal(totalCount);
    } catch (error) {
      console.error('Failed to fetch memories', error);
    } finally {
      setLoading(false);
    }
  }, [client, filters, adminFilters, role]);

  useEffect(() => {
    fetchMemories(searchQuery, 0);
    setOffset(0);
  }, [searchQuery, filters, adminFilters, fetchMemories]);

  const loadMore = () => {
    const nextOffset = offset + 20;
    setOffset(nextOffset);
    fetchMemories(searchQuery, nextOffset, true);
  };

  useEffect(() => {
    if (selectedUri) {
      const found = memories.find(m => m.uri === selectedUri);
      if (found) {
        setSelectedMemory(found);
      } else {
        // If not in current list, we should probably fetch it specifically, 
        // but for now we'll just wait for it to appear or assume it's there.
      }
    } else {
      setSelectedMemory(null);
    }
  }, [selectedUri, memories]);

  const fetchContent = useCallback(async (uri: string, memory: MemoryItem) => {
    if (!client) return;
    setContentLoading(true);
    try {
      // Search results may already have overview/content inline
      const hasInlineOverview = 'overview' in memory && !!memory.overview;
      const hasInlineContent = 'content' in memory && !!memory.content;

      const [abs, over, full] = await Promise.all([
        client.getContentAbstract(uri),
        hasInlineOverview ? Promise.resolve({ status: 'ok', result: (memory as SearchResult).overview! }) : client.getContentOverview(uri),
        hasInlineContent ? Promise.resolve({ status: 'ok', result: (memory as SearchResult).content! }) : client.getContentRead(uri),
      ]);
      setContent({
        abstract: abs.result,
        overview: over.result,
        full: full.result
      });
    } catch (error) {
      console.error('Failed to fetch content', error);
    } finally {
      setContentLoading(false);
    }
  }, [client]);

  useEffect(() => {
    if (selectedMemory) {
      fetchContent(selectedMemory.uri, selectedMemory);
    }
  }, [selectedMemory, fetchContent]);

  const handleSelect = (memory: MemoryItem) => {
    setSearchParams({ uri: memory.uri });
  };

  const handleFeedback = async (reward: number) => {
    if (!selectedMemory || !client) return;
    try {
      await client.feedbackMemory(selectedMemory.uri, reward);
      // Show toast?
    } catch (error) {
      console.error('Feedback failed', error);
    }
  };

  const handleDelete = async () => {
    if (!selectedMemory || !client) return;
    try {
      await client.forgetMemory(selectedMemory.uri);
      setIsDeleteModalOpen(false);
      setSearchParams({});
      fetchMemories(searchQuery, 0);
    } catch (error) {
      console.error('Delete failed', error);
    }
  };

  const copyToClipboard = (text: string) => {
    navigator.clipboard.writeText(text);
    // Show toast?
  };

  return (
    <PageLayout title="Memories" onRefresh={() => fetchMemories(searchQuery, 0)}>
      <div className="flex h-[calc(100vh-160px)] gap-6 overflow-hidden">
        {/* Left Panel: List */}
        <div className="w-[40%] flex flex-col gap-4 overflow-hidden">
          <div className="space-y-3">
            <SearchInput onSearch={setSearchQuery} placeholder="Search memories..." />
            
            {role === 'admin' && (
              <div className="flex gap-2">
                <select
                  className="flex-1 text-sm border border-indigo-200 rounded-md p-2 bg-indigo-50 outline-none focus:ring-2 focus:ring-indigo-500"
                  value={adminFilters.tenant_id}
                  onChange={(e) => setAdminFilters(f => ({ ...f, tenant_id: e.target.value }))}
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
            <div className="flex gap-2">
              <select
                className="flex-1 text-sm border border-gray-200 rounded-md p-2 bg-white outline-none focus:ring-2 focus:ring-indigo-500"
                value={filters.context_type}
                onChange={(e) => setFilters(f => ({ ...f, context_type: e.target.value }))}
              >
                <option value="">All Types</option>
                <option value="memory">Memory</option>
                <option value="resource">Resource</option>
                <option value="skill">Skill</option>
                <option value="case">Case</option>
                <option value="pattern">Pattern</option>
              </select>
              
              <select 
                className="flex-1 text-sm border border-gray-200 rounded-md p-2 bg-white outline-none focus:ring-2 focus:ring-indigo-500"
                value={filters.category}
                onChange={(e) => setFilters(f => ({ ...f, category: e.target.value }))}
              >
                <option value="">All Categories</option>
                {['profile', 'preferences', 'entities', 'events', 'cases', 'patterns', 'error_fixes', 'workflows', 'strategies', 'documents', 'plans'].map(cat => (
                  <option key={cat} value={cat}>{cat}</option>
                ))}
              </select>
            </div>
          </div>

          <div className="flex-1 overflow-y-auto space-y-3 pr-2">
            {memories.map((memory) => (
              <div 
                key={memory.uri}
                onClick={() => handleSelect(memory)}
                className={`p-4 rounded-lg border cursor-pointer transition-all ${
                  selectedUri === memory.uri 
                    ? 'border-indigo-500 bg-indigo-50' 
                    : 'border-gray-200 bg-white hover:border-gray-300'
                }`}
              >
                <p className="text-sm font-medium text-gray-900 line-clamp-2 mb-2">
                  {memory.abstract}
                </p>
                <div className="flex items-center justify-between">
                  <div className="flex gap-2">
                    {'category' in memory && memory.category && (
                      <Badge color="indigo">{memory.category}</Badge>
                    )}
                    <Badge color="gray">{memory.context_type}</Badge>
                    {'source_tenant_id' in memory && role === 'admin' && (
                      <Badge color="green">{(memory as any).source_tenant_id}/{(memory as any).source_user_id}</Badge>
                    )}
                  </div>
                  {'score' in memory && memory.score != null && (
                    <span className="text-xs text-gray-400">score: {Number(memory.score).toFixed(2)}</span>
                  )}
                </div>
              </div>
            ))}
            
            {loading && <LoadingSpinner />}
            
            {!loading && !isSearchMode && memories.length > 0 && (
              <Button 
                variant="ghost" 
                className="w-full text-indigo-600 font-medium py-4" 
                onClick={loadMore}
              >
                Load More
              </Button>
            )}

            {!loading && memories.length === 0 && (
              <div className="text-center py-12 text-gray-500 bg-white rounded-lg border border-dashed border-gray-300">
                No memories found.
              </div>
            )}
          </div>
        </div>

        {/* Right Panel: Detail */}
        <div className="w-[60%] overflow-y-auto pr-2">
          {selectedMemory ? (
            <div className="space-y-6">
              <Card>
                <div className="flex items-start justify-between gap-4 mb-4">
                  <h2 className="text-xl font-bold text-gray-900">{selectedMemory.abstract}</h2>
                  <div className="flex gap-2 shrink-0">
                    {'category' in selectedMemory && selectedMemory.category && (
                      <Badge color="indigo">{selectedMemory.category}</Badge>
                    )}
                    <Badge color="gray">{selectedMemory.context_type}</Badge>
                  </div>
                </div>
                
                <div className="flex items-center gap-2 bg-gray-50 p-2 rounded text-xs font-mono text-gray-500 mb-6">
                  <span className="truncate">{selectedMemory.uri}</span>
                  <button onClick={() => copyToClipboard(selectedMemory.uri)} className="p-1 hover:text-indigo-600 shrink-0">
                    <Copy size={14} />
                  </button>
                </div>

                {/* Tabs */}
                <div className="border-b border-gray-200 mb-6">
                  <nav className="flex gap-8">
                    <TabButton active={activeTab === 'l0'} onClick={() => setActiveTab('l0')}>Abstract</TabButton>
                    <TabButton active={activeTab === 'l1'} onClick={() => setActiveTab('l1')}>Overview</TabButton>
                    <TabButton active={activeTab === 'l2'} onClick={() => setActiveTab('l2')}>Content</TabButton>
                  </nav>
                </div>

                <div className="min-h-[200px]">
                  {contentLoading ? (
                    <LoadingSpinner />
                  ) : (
                    <div className="prose prose-sm max-w-none text-gray-700 whitespace-pre-wrap">
                      {activeTab === 'l0' && (content.abstract || selectedMemory.abstract)}
                      {activeTab === 'l1' && (content.overview || ('overview' in selectedMemory && selectedMemory.overview) || 'No overview available.')}
                      {activeTab === 'l2' && (content.full || 'No full content available.')}
                    </div>
                  )}
                </div>
              </Card>

              {/* Metadata */}
              <Card className="p-0 overflow-hidden">
                <button 
                  className="w-full flex items-center justify-between p-4 hover:bg-gray-50 transition-colors"
                  onClick={() => setShowMetadata(!showMetadata)}
                >
                  <span className="text-sm font-semibold text-gray-700 uppercase tracking-wider">Technical Metadata</span>
                  {showMetadata ? <ChevronUp size={18} /> : <ChevronDown size={18} />}
                </button>
                {showMetadata && (
                  <div className="p-4 pt-0 border-t border-gray-100 bg-gray-50 overflow-x-auto">
                    <pre className="text-xs text-gray-600 p-4 bg-white rounded border border-gray-200 mt-4">
                      {JSON.stringify(selectedMemory, null, 2)}
                    </pre>
                  </div>
                )}
              </Card>

              {/* Actions */}
              <div className="flex items-center justify-between pt-4 pb-8">
                <div className="flex gap-4">
                  <Button variant="ghost" className="text-green-600 hover:bg-green-50" onClick={() => handleFeedback(1)}>
                    <ThumbsUp size={18} className="mr-2" /> +1
                  </Button>
                  <Button variant="ghost" className="text-red-600 hover:bg-red-50" onClick={() => handleFeedback(-1)}>
                    <ThumbsDown size={18} className="mr-2" /> -1
                  </Button>
                </div>
                <Button variant="danger" onClick={() => setIsDeleteModalOpen(true)}>
                  <Trash2 size={18} className="mr-2" /> Delete Memory
                </Button>
              </div>
            </div>
          ) : (
            <div className="h-full flex items-center justify-center">
              <EmptyState 
                icon={<Brain size={48} className="text-gray-200" />}
                title="Select a memory"
                message="Choose a memory from the list to view its details and content."
              />
            </div>
          )}
        </div>
      </div>

      <Modal
        isOpen={isDeleteModalOpen}
        onClose={() => setIsDeleteModalOpen(false)}
        title="Confirm Deletion"
        footer={
          <>
            <Button variant="ghost" onClick={() => setIsDeleteModalOpen(false)}>Cancel</Button>
            <Button variant="danger" onClick={handleDelete}>Delete Permanently</Button>
          </>
        }
      >
        <p className="text-gray-600">
          Are you sure you want to delete this memory? This action cannot be undone.
        </p>
      </Modal>
    </PageLayout>
  );
};

const TabButton: React.FC<{ active: boolean; onClick: () => void; children: React.ReactNode }> = ({ active, onClick, children }) => (
  <button 
    onClick={onClick}
    className={`pb-4 text-sm font-medium transition-colors border-b-2 ${
      active ? 'text-indigo-600 border-indigo-600' : 'text-gray-500 border-transparent hover:text-gray-700'
    }`}
  >
    {children}
  </button>
);
