import { defineStore } from 'pinia';

import { login as loginApi, me as meApi } from '../api/auth';
import { clearToken, setToken } from '../api/client';

// 令牌只驻留内存：store 初始为空，刷新页面后需重新登录。
export const useAuthStore = defineStore('auth', {
  state: () => ({
    token: '',
    user: null,
  }),
  actions: {
    async login(username, password) {
      const result = await loginApi(username, password);
      this.token = result.token;
      this.user = result.user;
      setToken(result.token);
    },
    async loadMe() {
      this.user = await meApi();
    },
    logout() {
      this.token = '';
      this.user = null;
      clearToken();
    },
  },
});
