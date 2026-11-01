import type { ApiProblem } from './contracts';

export class ApiError extends Error {
  constructor(message: string, readonly status: number, readonly code?: string, readonly fieldErrors: Record<string, string> = {}) {
    super(message);
    this.name = 'ApiError';
  }
}

export class ApiClient {
  constructor(private readonly baseUrl = '/api/v1') {}

  async request<T>(path: string, init?: RequestInit): Promise<T> {
    return (await this.requestWithHeaders<T>(path, init)).data;
  }

  async requestWithHeaders<T>(path: string, init?: RequestInit): Promise<{ data: T; headers: Headers }> {
    const headers = new Headers(init?.headers);
    headers.set('Accept', 'application/json');
    const body = init?.body;
    const isBinaryBody = (typeof Blob !== 'undefined' && body instanceof Blob)
      || (typeof ArrayBuffer !== 'undefined' && body instanceof ArrayBuffer)
      || (typeof FormData !== 'undefined' && body instanceof FormData);
    if (body && !isBinaryBody) headers.set('Content-Type', 'application/json');
    const method = init?.method?.toUpperCase() ?? (init?.body ? 'POST' : 'GET');
    if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
      const csrf = document.cookie.split(';').map((part) => part.trim()).find((part) => part.startsWith('vh_csrf='))?.slice('vh_csrf='.length);
      if (csrf) headers.set('X-CSRF-Token', decodeURIComponent(csrf));
    }
    const response = await fetch(`${this.baseUrl}${path}`, {
      ...init,
      credentials: 'same-origin',
      headers,
    });
    if (!response.ok) {
      let problem: ApiProblem | undefined;
      const fieldErrors: Record<string, string> = {};
      try {
        const body = await response.json() as { detail?: ApiProblem | Array<{ loc?: Array<string | number>; msg?: string }> } & ApiProblem;
        if (Array.isArray(body.detail)) {
          for (const issue of body.detail) {
            const field = issue.loc?.at(-1);
            if (typeof field === 'string' && issue.msg) fieldErrors[field] = issue.msg;
          }
          problem = body;
        } else {
          problem = body.detail ?? body;
          Object.assign(fieldErrors, problem.details?.field_errors ?? {});
        }
      } catch {
        // Non-JSON responses still surface the HTTP status below.
      }
      throw new ApiError(problem?.message ?? `Falha na API (${response.status})`, response.status, problem?.code, fieldErrors);
    }
    if (response.status === 204) return { data: undefined as T, headers: response.headers };
    return { data: await response.json() as T, headers: response.headers };
  }
}

export const api = new ApiClient();
