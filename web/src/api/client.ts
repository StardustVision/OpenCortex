import {
  SystemHealth, MemoryStats, SearchResponse, ListResponse, ContentResponse,
  KnowledgeCandidate, ArchivistStatus, SearchDebugResponse,
  TokenRecord, AuthMe, AdminListResponse
} from './types';

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
      if (errorData.error === 'feature disabled') {
        return { error: 'feature disabled' } as any;
      }
      throw new Error(`API error: ${res.status}`);
    }
    
    return res.json();
  }

  // System
  getHealth(): Promise<SystemHealth> {
    return this.request('GET', '/api/v1/system/status?type=health');
  }

  getStats(): Promise<MemoryStats> {
    return this.request('GET', '/api/v1/memory/stats');
  }

  getDoctor(): Promise<any> {
    return this.request('GET', '/api/v1/system/status?type=doctor');
  }

  getSystemStats(): Promise<any> {
    return this.request('GET', '/api/v1/system/status?type=stats');
  }

  // Memory
  listMemories(params: { category?: string; context_type?: string; limit?: number; offset?: number }): Promise<ListResponse> {
    const query = new URLSearchParams();
    if (params.category) query.append('category', params.category);
    if (params.context_type) query.append('context_type', params.context_type);
    if (params.limit) query.append('limit', params.limit.toString());
    if (params.offset) query.append('offset', params.offset.toString());
    return this.request('GET', `/api/v1/memory/list?${query.toString()}`);
  }

  searchMemories(params: { query: string; limit?: number; context_type?: string; category?: string; detail_level?: string }): Promise<SearchResponse> {
    return this.request('POST', '/api/v1/memory/search', params);
  }

  forgetMemory(uri: string): Promise<{ status: string; forgotten: number }> {
    return this.request('POST', '/api/v1/memory/forget', { uri });
  }

  feedbackMemory(uri: string, reward: number): Promise<{ status: string; uri: string; reward: string }> {
    return this.request('POST', '/api/v1/memory/feedback', { uri, reward });
  }

  decayMemories(): Promise<any> {
    return this.request('POST', '/api/v1/memory/decay');
  }

  // Content
  getContentAbstract(uri: string): Promise<ContentResponse> {
    return this.request('GET', `/api/v1/content/abstract?uri=${encodeURIComponent(uri)}`);
  }

  getContentOverview(uri: string): Promise<ContentResponse> {
    return this.request('GET', `/api/v1/content/overview?uri=${encodeURIComponent(uri)}`);
  }

  getContentRead(uri: string, offset = 0, limit = 2000): Promise<ContentResponse> {
    return this.request('GET', `/api/v1/content/read?uri=${encodeURIComponent(uri)}&offset=${offset}&limit=${limit}`);
  }

  // Knowledge
  getKnowledgeCandidates(): Promise<{ candidates: KnowledgeCandidate[]; count: number } | { error: string }> {
    return this.request('GET', '/api/v1/knowledge/candidates');
  }

  searchKnowledge(params: { query: string; types?: string[]; limit?: number }): Promise<{ results: any[]; count: number } | { error: string }> {
    return this.request('POST', '/api/v1/knowledge/search', params);
  }

  approveKnowledge(knowledge_id: string): Promise<any> {
    return this.request('POST', '/api/v1/knowledge/approve', { knowledge_id });
  }

  rejectKnowledge(knowledge_id: string): Promise<any> {
    return this.request('POST', '/api/v1/knowledge/reject', { knowledge_id });
  }

  // Archivist
  triggerArchivist(): Promise<{ ok: boolean; status: string } | { error: string }> {
    return this.request('POST', '/api/v1/archivist/trigger');
  }

  getArchivistStatus(): Promise<ArchivistStatus | { error: string }> {
    return this.request('GET', '/api/v1/archivist/status');
  }

  // Admin
  reembedAll(): Promise<{ status: string; updated: number }> {
    return this.request('POST', '/api/v1/admin/reembed');
  }

  searchDebug(query: string, limit = 5): Promise<SearchDebugResponse> {
    return this.request('POST', '/api/v1/admin/search_debug', { query, limit });
  }

  // Auth
  getMe(): Promise<AuthMe> {
    return this.request('GET', '/api/v1/auth/me');
  }

  // Admin — Tokens
  listTokens(): Promise<{ tokens: TokenRecord[] }> {
    return this.request('GET', '/api/v1/admin/tokens');
  }

  createToken(tenant_id: string, user_id: string): Promise<{ token: string; tenant_id: string; user_id: string; role: string }> {
    return this.request('POST', '/api/v1/admin/tokens', { tenant_id, user_id });
  }

  revokeToken(token_prefix: string): Promise<{ status: string }> {
    return this.request('DELETE', '/api/v1/admin/tokens', { token_prefix });
  }

  // Admin — Memories
  listAllMemories(params: { tenant_id?: string; user_id?: string; category?: string; context_type?: string; limit?: number; offset?: number }): Promise<AdminListResponse> {
    const query = new URLSearchParams();
    if (params.tenant_id) query.append('tenant_id', params.tenant_id);
    if (params.user_id) query.append('user_id', params.user_id);
    if (params.category) query.append('category', params.category);
    if (params.context_type) query.append('context_type', params.context_type);
    if (params.limit) query.append('limit', params.limit.toString());
    if (params.offset) query.append('offset', params.offset.toString());
    return this.request('GET', `/api/v1/admin/memories?${query.toString()}`);
  }
}
