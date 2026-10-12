<template>
  <div class="login-page">
    <el-card class="login-card">
      <template #header>登录</template>
      <el-form @submit.prevent="onSubmit">
        <el-form-item label="用户名">
          <el-input v-model="username" placeholder="请输入用户名" />
        </el-form-item>
        <el-form-item label="密码">
          <el-input v-model="password" type="password" placeholder="请输入密码" show-password />
        </el-form-item>
        <el-alert v-if="error" :title="error" type="error" :closable="false" />
        <el-button
          class="submit"
          type="primary"
          :disabled="!canSubmit"
          :loading="loading"
          @click="onSubmit"
        >
          登录
        </el-button>
      </el-form>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ElMessage } from 'element-plus';
import { computed, ref } from 'vue';
import { useRouter } from 'vue-router';

import { useAuthStore } from '../stores/auth';
import { canSubmitLogin } from '../utils/validation';

const router = useRouter();
const auth = useAuthStore();

const username = ref('');
const password = ref('');
const error = ref('');
const loading = ref(false);

const canSubmit = computed(() => canSubmitLogin(username.value, password.value));

async function onSubmit(): Promise<void> {
  if (!canSubmit.value) {
    return;
  }
  error.value = '';
  loading.value = true;
  try {
    await auth.login(username.value, password.value);
    ElMessage.success('登录成功');
    await router.push({ name: 'home' });
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '登录失败';
  } finally {
    loading.value = false;
  }
}
</script>

<style scoped>
.login-page {
  display: flex;
  justify-content: center;
  padding-top: 12vh;
}
.login-card {
  width: 360px;
}
.submit {
  width: 100%;
  margin-top: 12px;
}
</style>
