import { defineStore } from 'pinia';

import { login as loginApi, me as meApi } from '../api/auth';
import { clearToken, getToken, setToken } from '../api/client';
import type { UserView } from '../types/api';

export const useAuthStore = defineStore('auth', {
  state: () => ({
    token: getToken(),
    user: null as UserView | null,
  }),
  actions: {
    async login(username: string, password: string): Promise<void> {
      const result = await loginApi(username, password);
      this.token = result.token;
      this.user = result.user;
      setToken(result.token);
    },
    async loadMe(): Promise<void> {
      this.user = await meApi();
    },
    logout(): void {
      this.token = '';
      this.user = null;
      clearToken();
    },
  },
});
