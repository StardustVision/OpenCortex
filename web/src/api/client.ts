import {
  AuthMe,
  ConsoleContentResponse,
  ConsoleListResponse,
  ConsoleStats,
  SearchResponse,
  TokenRecord,
} from './types';

export class APIRequestError extends Error {
  status: number;
  payload: unknown;
  method: string;
  path: string;

  constructor(
    status: number,
    payload: unknown,
    method: string,
    path: string,
    message?: string
  ) {
    super(message ?? `API error: ${status} ${method} ${path}`);
    Object.setPrototypeOf(this, APIRequestError.prototype);
    this.status = status;
    this.payload = payload;
    this.method = method;
    this.path = path;
  }
}

export interface MemoryListParams {
  tenant_id?: string;
  user_id?: string;
  project_id?: string;
  category?: string;
  context_type?: string;
  limit?: number;
  offset?: number;
}

export class OpenCortexClient {
  private baseUrl: string;
  private token: string;

  constructor(baseUrl: string, token: string) {
    this.baseUrl = baseUrl.endsWith('/') ? baseUrl.slice(0, -1) : baseUrl;
    this.token = token;
  }

  private withQuery(
    path: string,
    params?: Record<string, string | number | boolean | undefined>
  ): string {
    if (!params) {
      return path;
    }

    const query = new URLSearchParams();
    Object.entries(params).forEach(([key, value]) => {
      if (value !== undefined && value !== '') {
        query.append(key, String(value));
      }
    });

    const queryString = query.toString();
    return queryString ? `${path}?${queryString}` : path;
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
    let parsedBody: unknown = undefined;
    try {
      parsedBody = await res.json();
    } catch (parseError) {
      if (res.ok) {
        throw parseError;
      }
    }

    if (!res.ok) {
      throw new APIRequestError(res.status, parsedBody, method, path);
    }

    return parsedBody as T;
  }

  getMe(): Promise<AuthMe> {
    return this.request('GET', '/api/v1/auth/me');
  }

  getConsoleStats(params: MemoryListParams = {}): Promise<ConsoleStats> {
    return this.request('GET', this.withQuery('/console/v1/stats', { ...params }));
  }

  listMemories(params: MemoryListParams = {}): Promise<ConsoleListResponse> {
    return this.request('GET', this.withQuery('/console/v1/memories', { ...params }));
  }

  searchMemories(
    params: { query: string; limit?: number },
    scope: MemoryListParams = {},
  ): Promise<SearchResponse> {
    return this.request(
      'POST',
      this.withQuery('/console/v1/memories/search', { ...scope }),
      params,
    );
  }

  getMemoryContent(uri: string): Promise<ConsoleContentResponse> {
    return this.request(
      'GET',
      this.withQuery('/console/v1/memories/content', { uri }),
    );
  }

  forgetMemory(
    uri: string,
    scope: Pick<MemoryListParams, 'tenant_id' | 'user_id' | 'project_id'> = {},
  ): Promise<{ forgotten: number; uri: string; matched_by: string }> {
    return this.request('DELETE', '/console/v1/memories', { uri, ...scope });
  }

  listTokens(): Promise<{ tokens: TokenRecord[] }> {
    return this.request('GET', '/admin/v1/tokens');
  }

  createToken(
    tenant_id: string,
    user_id: string,
  ): Promise<{ token: string; tenant_id: string; user_id: string; role: string }> {
    return this.request('POST', '/admin/v1/tokens', { tenant_id, user_id });
  }

  revokeToken(token_prefix: string): Promise<{ status: string }> {
    return this.request('DELETE', '/admin/v1/tokens', { token_prefix });
  }
}
