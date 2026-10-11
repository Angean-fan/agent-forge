<script setup lang="ts">
import { computed, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';

import { clearSession, logout, useAuthState } from '@/stores/auth';

const route = useRoute();
const router = useRouter();
const auth = useAuthState();
const loggingOut = ref(false);

const showHeader = computed(() => route.name !== 'login');

async function onLogout(): Promise<void> {
  loggingOut.value = true;
  try {
    await logout();
  } catch {
    clearSession();
  } finally {
    loggingOut.value = false;
    await router.push({ name: 'login' });
  }
}
</script>

<template>
  <div class="app-shell">
    <header v-if="showHeader" class="app-header">
      <router-link class="app-brand" :to="{ name: 'projects' }">AgentForge 平台</router-link>
      <div class="app-header-right">
        <span v-if="auth.account" class="app-user">{{ auth.account.username }}</span>
        <el-button v-if="auth.account" text type="primary" :loading="loggingOut" @click="onLogout">
          退出登录
        </el-button>
      </div>
    </header>
    <main class="app-main">
      <router-view />
    </main>
  </div>
</template>

<style scoped>
.app-shell {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}
.app-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 24px;
  height: 56px;
  border-bottom: 1px solid var(--el-border-color-light);
  background: #fff;
}
.app-brand {
  font-weight: 600;
  font-size: 16px;
  color: var(--el-color-primary);
  text-decoration: none;
}
.app-header-right {
  display: flex;
  align-items: center;
  gap: 12px;
}
.app-user {
  color: var(--el-text-color-regular);
  font-size: 14px;
}
.app-main {
  flex: 1;
  padding: 24px;
  max-width: 960px;
  margin: 0 auto;
  width: 100%;
  box-sizing: border-box;
}
</style>
