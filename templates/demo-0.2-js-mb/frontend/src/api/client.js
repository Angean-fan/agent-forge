const BASE = '/api';

// MVP：令牌仅保存在内存中，绝不写入 localStorage/sessionStorage；刷新页面即需重新登录。
let authToken = '';

export function getToken() {
  return authToken;
}

export function setToken(token) {
  authToken = token;
}

export function clearToken() {
  authToken = '';
}

export async function request(path, options = {}) {
  const headers = {
    'Content-Type': 'application/json',
    ...(options.headers ?? {}),
  };
  if (authToken) {
    headers.Authorization = `Bearer ${authToken}`;
  }

  const response = await fetch(`${BASE}${path}`, { ...options, headers });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ message: response.statusText }));
    throw new Error(body.message ?? 'request failed');
  }
  if (response.status === 204) {
    return undefined;
  }
  const text = await response.text();
  return text ? JSON.parse(text) : undefined;
}
