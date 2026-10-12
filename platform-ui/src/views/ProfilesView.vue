<template>
  <div class="profiles">
    <h1>技术选型组合</h1>
    <el-alert v-if="error" :title="error" type="error" :closable="false" data-testid="profiles-error" />

    <el-table v-loading="loading" :data="options" class="options">
      <el-table-column label="组合">
        <template #default="{ row }">
          <span>{{ row.profile_id }}@{{ row.profile_version }}</span>
          <el-tag v-if="row.recommended" type="success" size="small" class="tag">推荐</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="frontend_language" label="前端" />
      <el-table-column prop="orm" label="ORM" />
      <el-table-column label="特点">
        <template #default="{ row }">
          <div v-for="note in row.notes" :key="note" class="note">
            <SafeText :text="note" />
          </div>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="160">
        <template #default="{ row }">
          <el-button size="small" @click="onExplain(row)">AI 通俗解释</el-button>
        </template>
      </el-table-column>
    </el-table>

    <div v-if="explanation" class="explain" data-testid="profile-explanation">
      <h2>选型说明</h2>
      <SafeText :text="explanation" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { ElMessage } from 'element-plus';
import { onMounted, ref } from 'vue';

import { ApiError, profilesApi, projectsApi } from '@/api/client';
import type { ProfileOption } from '@/api/types';
import SafeText from '@/components/SafeText.vue';

const options = ref<ProfileOption[]>([]);
const loading = ref(false);
const error = ref('');
const explanation = ref('');

async function load(): Promise<void> {
  loading.value = true;
  try {
    options.value = await profilesApi.list();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '加载失败';
  } finally {
    loading.value = false;
  }
}

// 通俗解释为项目上下文计费；需先有项目。缺失时给出明确提示，不静默失败。
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
.profiles {
  padding: 24px;
}
.tag {
  margin-left: 8px;
}
.note {
  font-size: 13px;
  color: #555;
}
.explain {
  margin-top: 16px;
}
</style>
