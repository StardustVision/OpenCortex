export interface SystemHealth {
  initialized: boolean;
  storage: boolean;
  embedder: boolean;
  llm: boolean;
}

export interface MemoryStats {
  tenant_id: string;
  user_id: string;
  storage: {
    total_records: number;
    [key: string]: any;
  };
  embedder: string | null;
  has_llm: boolean;
  rerank: {
    enabled: boolean;
    mode: string;
    model: string;
    fusion_beta: number;
  };
}

/** Returned by /api/v1/memory/list — has full metadata */
export interface MemoryRecord {
  uri: string;
  abstract: string;
  category: string;
  context_type: string;
  scope: string;
  project_id: string;
  updated_at: string;
  created_at: string;
}

/** Returned by /api/v1/memory/search — sparse fields */
export interface SearchResult {
  uri: string;
  abstract: string;
  context_type: string;
  score: number | null;
  overview?: string;
  content?: string;
  keywords?: string;
}

export interface SearchResponse {
  results: SearchResult[];
  total: number;
  search_intent?: Record<string, unknown>;
}

export interface ListResponse {
  results: MemoryRecord[];
  total: number;
}

export interface ContentResponse {
  status: string;
  result: string;
}

export interface KnowledgeCandidate {
  knowledge_id: string;
  knowledge_type: string;
  scope: string;
  status: string;
  abstract?: string;
  overview?: string;
  created_at: string;
  updated_at: string;
  source_trace_ids?: string[];
}

export interface ArchivistStatus {
  enabled: boolean;
  running?: boolean;
  last_run_at?: string | null;
  trigger_mode?: string;
  trigger_threshold?: number;
}

export interface SearchDebugResult {
  rank: number;
  abstract: string;
  raw_vector_score: number;
  rerank_score: number;
  fused_score: number;
  uri: string;
}

export interface SearchDebugResponse {
  query: string;
  fusion_beta: number;
  rerank_mode: string;
  results: SearchDebugResult[];
}

export interface TokenRecord {
  tenant_id: string;
  user_id: string;
  role: string;
  created_at: string;
  token_prefix: string;
  token: string;
}

export interface AuthMe {
  tenant_id: string;
  user_id: string;
  role: string;
}

export interface AdminMemoryRecord {
  uri: string;
  abstract: string;
  category: string;
  context_type: string;
  scope: string;
  project_id: string;
  source_tenant_id: string;
  source_user_id: string;
  updated_at: string;
  created_at: string;
}

export interface AdminListResponse {
  results: AdminMemoryRecord[];
  total: number;
}

/** Union type for items displayed in the memory list panel */
export type MemoryItem = MemoryRecord | SearchResult | AdminMemoryRecord;
