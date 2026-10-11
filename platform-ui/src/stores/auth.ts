import { reactive } from 'vue';

import { ApiError, authApi } from '@/api/client';
import type { AccountInfo } from '@/api/types';

interface AuthState {
  account: AccountInfo | null;
  loaded: boolean;
}

const state = reactive<AuthState>({ account: null, loaded: false });

export function useAuthState(): AuthState {
  return state;
}

/** 首次导航时确认会话；已确认过则直接返回缓存结果。 */
export async function ensureSession(): Promise<boolean> {
  if (state.loaded) {
    return state.account !== null;
  }
  try {
    const { account } = await authApi.me();
    state.account = account;
  } catch (error) {
    state.account = null;
    if (!(error instanceof ApiError)) {
      throw error;
    }
  }
  state.loaded = true;
  return state.account !== null;
}

export async function login(username: string, password: string): Promise<AccountInfo> {
  const { account } = await authApi.login(username, password);
  state.account = account;
  state.loaded = true;
  return account;
}

export async function logout(): Promise<void> {
  try {
    await authApi.logout();
  } finally {
    state.account = null;
    state.loaded = true;
  }
}

export function clearSession(): void {
  state.account = null;
  state.loaded = true;
}
