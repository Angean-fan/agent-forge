const BASE = '/api';

// MVP：令牌仅保存在内存中，绝不写入 localStorage/sessionStorage；刷新页面即需重新登录。
let authToken = '';

export function getToken(): string {
  return authToken;
}

export function setToken(token: string): void {
  authToken = token;
}

export function clearToken(): void {
  authToken = '';
}

export async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...((options.headers as Record<string, string> | undefined) ?? {}),
  };
  if (authToken) {
    headers.Authorization = `Bearer ${authToken}`;
  }

  const response = await fetch(`${BASE}${path}`, { ...options, headers });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ code: 'ERROR', message: response.statusText }));
    throw new Error((body as { message?: string }).message ?? 'request failed');
  }
  if (response.status === 204) {
    return undefined as T;
  }
  const text = await response.text();
  return (text ? (JSON.parse(text) as T) : (undefined as T));
}
