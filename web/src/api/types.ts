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

// ---- Insights ----

export interface ReportMetadata {
  report_uri: string;
  generated_at: string;
  period_start: string;
  period_end: string;
  total_sessions: number;
  total_messages: number;
}

export interface GenerateInsightsResponse {
  report_uri: string;
  summary: string;
  generated_at: string;
}

export interface LatestReportResponse {
  report: ReportMetadata | null;
  message: string;
}

export interface ReportHistoryResponse {
  reports: ReportMetadata[];
  total: number;
}

export interface SessionFacet {
  session_id: string;
  underlying_goal: string;
  brief_summary: string;
  goal_categories: Record<string, number>;
  outcome: string;
  user_satisfaction_counts: Record<string, number>;
  claude_helpfulness: string;
  session_type: string;
  friction_counts: Record<string, number>;
  friction_detail: string;
  primary_success: string | null;
}

export interface InsightsReport {
  tenant_id: string;
  user_id: string;
  report_period: string;
  generated_at: string;
  total_sessions: number;
  total_messages: number;
  total_duration_hours: number;
  at_a_glance: Record<string, string>;
  cache_hits: number;
  llm_calls: number;
  project_areas: Record<string, number>;
  what_works: string[];
  friction_analysis: Record<string, number>;
  suggestions: string[];
  on_the_horizon: string[];
  session_facets: SessionFacet[];
  // Enriched fields from CC-equivalent pipeline
  interaction_style?: Record<string, string> | null;
  what_works_detail?: Record<string, unknown> | null;
  friction_detail?: Record<string, unknown> | null;
  suggestions_detail?: Record<string, unknown> | null;
  on_the_horizon_detail?: Record<string, unknown> | null;
  fun_ending?: Record<string, string> | null;
  aggregated?: Record<string, unknown> | null;
}
