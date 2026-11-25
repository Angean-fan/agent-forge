<script setup lang="ts">
import { reactive, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';

import { ElMessage, type FormInstance, type FormRules } from 'element-plus';

import { login } from '@/stores/auth';
import { describeError } from '@/utils/errors';

const router = useRouter();
const route = useRoute();

const formRef = ref<FormInstance>();
const form = reactive({ username: '', password: '' });
const rules: FormRules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
};

const submitting = ref(false);
const errorMessage = ref('');

async function onSubmit(): Promise<void> {
  if (!formRef.value) return;
  const valid = await formRef.value.validate().catch(() => false);
  if (!valid) return;
  submitting.value = true;
  errorMessage.value = '';
  try {
    await login(form.username, form.password);
    ElMessage.success('登录成功');
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : null;
    await router.push(redirect ?? { name: 'projects' });
  } catch (error) {
    errorMessage.value = describeError(error).message;
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <div class="login-wrap">
    <div class="login-card">
      <div class="login-brand">
        <span class="login-logo">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <polygon points="12 2 2 7 12 12 22 7 12 2" />
            <polyline points="2 17 12 22 22 17" />
            <polyline points="2 12 12 17 22 12" />
          </svg>
        </span>
        <div>
          <h1 class="page-title">登录 AgentForge 平台</h1>
          <p class="page-subtitle login-sub">使用维护者预置的内测账号登录，本平台不提供注册与找回密码。</p>
        </div>
      </div>

      <el-alert
        v-if="errorMessage"
        :title="errorMessage"
        type="error"
        :closable="false"
        show-icon
        class="login-error"
      />

      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-position="top"
        @submit.prevent="onSubmit"
      >
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" autocomplete="username" placeholder="请输入用户名" />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input
            v-model="form.password"
            type="password"
            show-password
            autocomplete="current-password"
            placeholder="请输入密码"
            @keyup.enter="onSubmit"
          />
        </el-form-item>
        <el-button type="primary" :loading="submitting" class="login-submit" @click="onSubmit">
          登录
        </el-button>
      </el-form>
    </div>
  </div>
</template>

<style scoped>
.login-wrap {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background-image: radial-gradient(var(--af-grid-dot) 1px, transparent 1px);
  background-size: 22px 22px;
}

.login-card {
  width: 100%;
  max-width: 400px;
  border-radius: 14px;
  border: 1px solid var(--af-border-strong);
  background: var(--af-surface);
  padding: 24px;
}

.login-brand {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 18px;
}

.login-logo {
  width: 34px;
  height: 34px;
  border-radius: 9px;
  background: var(--af-accent-soft);
  border: 1px solid var(--af-accent-border);
  color: var(--af-accent);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.login-logo svg {
  width: 18px;
  height: 18px;
}

.login-sub {
  margin-bottom: 0;
}

.login-error {
  margin-bottom: 14px;
}

.login-submit {
  width: 100%;
  margin-top: 6px;
}
</style>
