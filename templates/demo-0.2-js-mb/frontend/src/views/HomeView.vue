<template>
  <div class="home">
    <el-card>
      <template #header>AgentForge 生成项目 · 基础模板</template>
      <p v-if="auth.user">当前用户：{{ auth.user.username }}（{{ auth.user.role }}）</p>
      <p v-else>未加载用户信息</p>
      <el-button @click="onLoadMe">刷新当前用户</el-button>
      <el-button type="danger" @click="onLogout">退出登录</el-button>
    </el-card>
  </div>
</template>

<script setup>
import { ElMessage } from 'element-plus';
import { useRouter } from 'vue-router';

import { useAuthStore } from '../stores/auth';

const router = useRouter();
const auth = useAuthStore();

async function onLoadMe() {
  try {
    await auth.loadMe();
  } catch (cause) {
    ElMessage.error(cause instanceof Error ? cause.message : '加载失败');
  }
}

async function onLogout() {
  auth.logout();
  await router.push({ name: 'login' });
}
</script>

<style scoped>
.home {
  padding: 24px;
}
</style>
