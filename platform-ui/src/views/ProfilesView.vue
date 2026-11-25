<template>
  <div class="profiles-scroll af-scroll">
    <div class="profiles">
    <h1 class="page-title">技术选型组合</h1>
    <p class="page-subtitle">已认证的前后端技术栈底座，点击「AI 通俗解释」可基于你的项目上下文生成选型说明。</p>
    <el-alert v-if="error" :title="error" type="error" :closable="false" data-testid="profiles-error" class="err-box" />

    <div class="profiles-card">
      <el-table v-loading="loading" :data="options" class="options">
        <el-table-column label="组合" min-width="160">
          <template #default="{ row }">
            <span class="af-mono">{{ row.profile_id }}@{{ row.profile_version }}</span>
            <el-tag v-if="row.recommended" type="success" size="small" class="tag">推荐</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="frontend_language" label="前端" width="110" />
        <el-table-column prop="orm" label="ORM" width="130" />
        <el-table-column label="特点" min-width="200">
          <template #default="{ row }">
            <div v-for="note in row.notes" :key="note" class="note">
              <SafeText :text="note" />
            </div>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="140">
          <template #default="{ row }">
            <el-button size="small" @click="onExplain(row)">AI 通俗解释</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <div v-if="explanation" class="explain" data-testid="profile-explanation">
      <h2 class="section-title">选型说明</h2>
      <SafeText :text="explanation" />
    </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ElMessage } from 'element-plus';
import { onMounted, ref } from 'vue';

import { ApiError, profilesApi, projectsApi } from '@/api/client';
import type { ProfileOption } from '@/api/types';
import SafeText from '@/components/SafeText.vue';
import { setActiveHeaderContext } from '@/stores/workspace';

const options = ref<ProfileOption[]>([]);
const loading = ref(false);
const error = ref('');
const explanation = ref('');

async function load(): Promise<void> {
  loading.value = true;
  setActiveHeaderContext({
    projectName: '技术选型组合',
    stageLabel: '',
    cost: null,
  });
  try {
    options.value = await profilesApi.list();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '加载失败';
  } finally {
    loading.value = false;
  }
}

async function onExplain(option: ProfileOption): Promise<void> {
  error.value = '';
  try {
    const projects = await projectsApi.list();
    if (projects.items.length === 0) {
      ElMessage.warning('请先创建项目后再获取 AI 解释。');
      return;
    }
    const result = await profilesApi.explain(projects.items[0].id, option.profile_id);
    explanation.value = result.explanation;
  } catch (cause) {
    error.value = cause instanceof ApiError ? cause.message : '解释获取失败';
  }
}

onMounted(load);
</script>

<style scoped>
.profiles-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 18px 22px;
}
.profiles {
  max-width: 840px;
  margin: 0 auto;
}
.err-box {
  margin-bottom: 14px;
}
.profiles-card {
  border-radius: 12px;
  border: 1px solid var(--af-border-strong);
  background: var(--af-surface);
  padding: 12px;
}
.tag {
  margin-left: 8px;
}
.note {
  font-size: 12px;
  color: var(--af-muted);
}
.explain {
  margin-top: 14px;
  border-radius: 10px;
  border: 1px solid var(--af-accent-border);
  background: var(--af-surface);
  padding: 14px;
}
</style>
