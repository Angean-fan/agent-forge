import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError, CSRF_HEADER, apiRequest, authApi, projectsApi, runsApi } from '@/api/client';
import type { SoftwareSpecification } from '@/api/types';

const fetchMock = vi.fn();

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function setCsrfCookie(value: string): void {
  document.cookie = `af_csrf=${value}; path=/`;
}

function clearCookies(): void {
  document.cookie = 'af_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/';
}

beforeEach(() => {
  clearCookies();
  vi.stubGlobal('fetch', fetchMock);
});

describe('apiRequest', () => {
  it('returns the data payload from a success envelope', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ request_id: 'req-1', data: { items: [] } }));

    const result = await apiRequest<{ items: unknown[] }>('/projects');

    expect(result).toEqual({ items: [] });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/projects');
    expect(init).toMatchObject({ method: 'GET', credentials: 'include' });
  });

  it('attaches the CSRF token and JSON body on unsafe methods', async () => {
    setCsrfCookie('csrf-token-abc');
    fetchMock.mockResolvedValue(jsonResponse({ request_id: 'req-2', data: { ok: true } }));

    await apiRequest('/projects', { method: 'POST', body: { name: 'demo' } });

    const init = fetchMock.mock.calls[0][1];
    expect(init.headers[CSRF_HEADER]).toBe('csrf-token-abc');
    expect(init.headers['Content-Type']).toBe('application/json');
    expect(init.body).toBe(JSON.stringify({ name: 'demo' }));
  });

  it('does not attach the CSRF token on safe methods', async () => {
    setCsrfCookie('csrf-token-abc');
    fetchMock.mockResolvedValue(jsonResponse({ request_id: 'req-3', data: {} }));

    await apiRequest('/projects');

    expect(fetchMock.mock.calls[0][1].headers[CSRF_HEADER]).toBeUndefined();
  });

  it('surfaces the backend error code/message/retryable', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        {
          request_id: 'req-4',
          error: { code: 'SPEC_CONFLICT', message: '规格版本冲突。', retryable: false },
        },
        409,
      ),
    );

    await expect(apiRequest('/projects/1/spec')).rejects.toMatchObject({
      code: 'SPEC_CONFLICT',
      message: '规格版本冲突。',
      status: 409,
      retryable: false,
    });
  });

  it('classifies 401/403/422 failures', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ request_id: 'a', error: { code: 'UNAUTHORIZED', message: '未登录。' } }, 401),
    );
    const unauthorized = await apiRequest('/projects').catch((error: unknown) => error);
    expect(unauthorized).toBeInstanceOf(ApiError);
    expect((unauthorized as ApiError).isUnauthorized).toBe(true);

    fetchMock.mockResolvedValueOnce(
      jsonResponse({ request_id: 'b', error: { code: 'FORBIDDEN', message: '无权访问。' } }, 403),
    );
    const forbidden = await apiRequest('/projects/2').catch((error: unknown) => error);
    expect((forbidden as ApiError).isForbidden).toBe(true);

    fetchMock.mockResolvedValueOnce(
      jsonResponse({ request_id: 'c', error: { code: 'VALIDATION_ERROR', message: '校验失败。' } }, 422),
    );
    const invalid = await apiRequest('/projects', { method: 'POST', body: {} }).catch(
      (error: unknown) => error,
    );
    expect((invalid as ApiError).isValidation).toBe(true);
  });

  it('turns a network failure into a retryable NETWORK_ERROR', async () => {
    fetchMock.mockRejectedValue(new TypeError('offline'));

    await expect(apiRequest('/projects')).rejects.toMatchObject({
      code: 'NETWORK_ERROR',
      status: 0,
      retryable: true,
    });
  });

  it('rejects responses without a data envelope', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ unexpected: true }));

    await expect(apiRequest('/projects')).rejects.toMatchObject({ code: 'INVALID_RESPONSE' });
  });
});

describe('authApi', () => {
  it('posts credentials to /api/auth/login', async () => {
    setCsrfCookie('csrf');
    fetchMock.mockResolvedValue(jsonResponse({ request_id: 'r', data: { account: { id: 1, username: 'tester' } } }));

    const result = await authApi.login('tester', 'secret');

    expect(result.account).toEqual({ id: 1, username: 'tester' });
    expect(fetchMock.mock.calls[0][0]).toBe('/api/auth/login');
    expect(fetchMock.mock.calls[0][1].method).toBe('POST');
  });
});

const minimalSpec = {
  schema_version: '0.1',
  project_type: 'crud_web',
  goal: 'g',
  actors: [],
  entities: [],
  relations: [],
  permissions: [],
  business_rules: [],
  acceptance_cases: [],
  technical_profile: { profile_id: 'demo-ts-mp', profile_version: '0.3', template_version: 'demo-0.2' },
  constraints: [],
  exclusions: [],
  capability_requests: [],
  recipe_requests: [],
  integrations: [],
  external_requirements: [],
} as SoftwareSpecification;

describe('projectsApi spec editing', () => {
  it('PUTs the draft with optimistic-lock version and revision', async () => {
    setCsrfCookie('csrf-token');
    fetchMock.mockResolvedValue(jsonResponse({ request_id: 'r', data: { spec_id: 3, version: 1 } }));

    await projectsApi.saveSpec(9, 1, 4, minimalSpec);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/projects/9/spec');
    expect(init.method).toBe('PUT');
    expect(init.headers[CSRF_HEADER]).toBe('csrf-token');
    expect(JSON.parse(init.body)).toEqual({ version: 1, draft_revision: 4, spec: minimalSpec });
  });

  it('confirms with the frozen version and revision', async () => {
    setCsrfCookie('csrf-token');
    fetchMock.mockResolvedValue(jsonResponse({ request_id: 'r', data: { spec_id: 3, version: 1 } }));

    await projectsApi.confirm(9, 1, 4);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/projects/9/confirm');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body)).toEqual({ version: 1, draft_revision: 4 });
  });
});

describe('runsApi', () => {
  it('submits a run with the Idempotency-Key header and CSRF token', async () => {
    setCsrfCookie('csrf-token');
    fetchMock.mockResolvedValue(
      jsonResponse({ request_id: 'r', data: { id: 42, project_id: 9, status: 'QUEUED' } }, 202),
    );

    const run = await runsApi.start(9, 3, 'idem-123');

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/projects/9/runs');
    expect(init.method).toBe('POST');
    expect(init.headers['Idempotency-Key']).toBe('idem-123');
    expect(init.headers[CSRF_HEADER]).toBe('csrf-token');
    expect(JSON.parse(init.body)).toEqual({ spec_id: 3 });
    expect(run.id).toBe(42);
  });
});
