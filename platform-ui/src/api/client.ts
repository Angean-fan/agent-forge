import type {
  AccountInfo,
  InterviewAnswer,
  InterviewView,
  ProjectDetail,
  ProjectSummary,
  RunDetail,
  RunEventsData,
  RunView,
  SoftwareSpecification,
  SpecConfirmView,
  SpecView,
} from './types';

export const CSRF_COOKIE = 'af_csrf';
export const CSRF_HEADER = 'X-CSRF-Token';

const UNSAFE_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);

/** 统一的 API 失败：保留后端错误码/消息/可重试标记与 HTTP 状态。 */
export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly retryable: boolean;

  constructor(code: string, message: string, status: number, retryable: boolean) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    this.status = status;
    this.retryable = retryable;
  }

  get isUnauthorized(): boolean {
    return this.status === 401;
  }

  get isForbidden(): boolean {
    return this.status === 403;
  }

  get isValidation(): boolean {
    return this.status === 422;
  }
}

export interface RequestOptions {
  method?: string;
  body?: unknown;
  headers?: Record<string, string>;
}

function readCookie(name: string): string | null {
  const target = `${name}=`;
  for (const part of document.cookie.split(';')) {
    const trimmed = part.trim();
    if (trimmed.startsWith(target)) {
      return decodeURIComponent(trimmed.slice(target.length));
    }
  }
  return null;
}

interface ErrorEnvelope {
  error?: { code?: string; message?: string; retryable?: boolean };
}

/**
 * 调用平台 API（相对路径 /api/...，由 Vite 代理到后端，浏览器视角同源）。
 * 状态变更方法附带双提交 CSRF 令牌；失败抛出 ApiError。
 */
export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = (options.method ?? 'GET').toUpperCase();
  const headers: Record<string, string> = { Accept: 'application/json', ...options.headers };
  if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json';
  }
  if (UNSAFE_METHODS.has(method)) {
    const token = readCookie(CSRF_COOKIE);
    if (token) {
      headers[CSRF_HEADER] = token;
    }
  }

  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      method,
      headers,
      credentials: 'include',
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    });
  } catch {
    throw new ApiError('NETWORK_ERROR', '网络请求失败，请检查连接后重试。', 0, true);
  }

  const text = await response.text();
  let payload: unknown = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null;
    }
  }

  if (!response.ok) {
    const envelope = (payload ?? {}) as ErrorEnvelope;
    const error = envelope.error;
    throw new ApiError(
      error?.code ?? `HTTP_${response.status}`,
      error?.message ?? '请求失败，请稍后重试。',
      response.status,
      error?.retryable ?? false,
    );
  }

  if (payload !== null && typeof payload === 'object' && 'data' in payload) {
    return (payload as { data: T }).data;
  }
  throw new ApiError('INVALID_RESPONSE', '服务返回了无法识别的响应。', response.status, false);
}

export const authApi = {
  login(username: string, password: string): Promise<{ account: AccountInfo }> {
    return apiRequest('/auth/login', { method: 'POST', body: { username, password } });
  },
  me(): Promise<{ account: AccountInfo }> {
    return apiRequest('/auth/me');
  },
  logout(): Promise<{ ok: boolean }> {
    return apiRequest('/auth/logout', { method: 'POST' });
  },
};

export const projectsApi = {
  list(): Promise<{ items: ProjectSummary[] }> {
    return apiRequest('/projects');
  },
  create(name: string, requirement: string): Promise<ProjectDetail> {
    return apiRequest('/projects', { method: 'POST', body: { name, requirement } });
  },
  get(projectId: number): Promise<ProjectDetail> {
    return apiRequest(`/projects/${projectId}`);
  },
  getSpec(projectId: number): Promise<SpecView> {
    return apiRequest(`/projects/${projectId}/spec`);
  },
  extract(projectId: number, version: number): Promise<SpecView> {
    return apiRequest(`/projects/${projectId}/extract`, {
      method: 'POST',
      body: { version },
    });
  },
  saveSpec(
    projectId: number,
    version: number,
    draftRevision: number,
    spec: SoftwareSpecification,
  ): Promise<SpecView> {
    return apiRequest(`/projects/${projectId}/spec`, {
      method: 'PUT',
      body: { version, draft_revision: draftRevision, spec },
    });
  },
  confirm(projectId: number, version: number, draftRevision: number): Promise<SpecConfirmView> {
    return apiRequest(`/projects/${projectId}/confirm`, {
      method: 'POST',
      body: { version, draft_revision: draftRevision },
    });
  },
};

export const interviewApi = {
  get(projectId: number): Promise<InterviewView> {
    return apiRequest(`/projects/${projectId}/interview`);
  },
  /** 无 answers 时开始访谈（已开始则原样返回）；带 answers 时续答。 */
  submit(projectId: number, answers: InterviewAnswer[] = []): Promise<InterviewView> {
    return apiRequest(`/projects/${projectId}/interview`, { method: 'POST', body: { answers } });
  },
};

/** 生成幂等键：优先用 crypto.randomUUID，缺失时退化为随机串。 */
export function newIdempotencyKey(): string {
  const globalCrypto = globalThis.crypto as Crypto | undefined;
  if (globalCrypto?.randomUUID) {
    return globalCrypto.randomUUID();
  }
  return `run-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

export const runsApi = {
  start(projectId: number, specId: number, idempotencyKey: string): Promise<RunView> {
    return apiRequest(`/projects/${projectId}/runs`, {
      method: 'POST',
      body: { spec_id: specId },
      headers: { 'Idempotency-Key': idempotencyKey },
    });
  },
  get(runId: number): Promise<RunDetail> {
    return apiRequest(`/runs/${runId}`);
  },
  events(runId: number, afterSequence: number): Promise<RunEventsData> {
    const query = new URLSearchParams({ after_sequence: String(afterSequence) });
    return apiRequest(`/runs/${runId}/events?${query.toString()}`);
  },
  /** 授权下载链接：只含 run_id，路径由后端按归属与已记录产物解析。 */
  artifactUrl(runId: number): string {
    return `/api/runs/${runId}/artifact`;
  },
};
