export interface ConsoleStats {
  tenant_id: string;
  user_id: string;
  project_id: string;
  role: string;
  total_records: number;
  primary_records: number;
  by_context_type: Record<string, number>;
  by_surface: Record<string, number>;
}

export interface MemoryRecord {
  uri: string;
  type?: string;
  abstract: string;
  overview?: string;
  content?: string;
  category: string;
  context_type: string;
  scope: string;
  project_id: string;
  session_id?: string;
  source_tenant_id?: string;
  source_user_id?: string;
  updated_at?: string;
  created_at?: string;
  score?: number | null;
  evidence?: RecallEvidence[];
  source?: RecallSource;
  keywords?: string;
  entities?: string[];
  meta?: Record<string, unknown>;
}

export interface RecallSource {
  uri: string;
  primary_uri: string;
  session_id?: string;
  document_id?: string | null;
  title?: string;
  section?: string;
}

export interface RecallEvidence {
  uri: string;
  kind: string;
  score: number;
  snippet: string;
}

export type SearchResult = MemoryRecord;

export interface ConsoleListResponse {
  results: MemoryRecord[];
  total: number;
}

export interface SearchResponse {
  results: SearchResult[];
  total: number;
}

export interface ConsoleContentResponse {
  uri: string;
  abstract: string;
  overview: string;
  content: string;
}

export interface TokenRecord {
  tenant_id: string;
  user_id: string;
  role: string;
  created_at: string;
  token_prefix: string;
  token?: string;
}

export interface AuthMe {
  tenant_id: string;
  user_id: string;
  project_id: string;
  role: string;
}

export type MemoryItem = MemoryRecord | SearchResult;
