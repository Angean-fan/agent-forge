<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';

import { projectsApi } from '@/api/client';
import type { ProjectSummary } from '@/api/types';
import { clearSession } from '@/stores/auth';
import { describeError } from '@/utils/errors';
import { formatDateTime } from '@/utils/format';

const router = useRouter();

const loading = ref(true);
const errorMessage = ref('');
const projects = ref<ProjectSummary[]>([]);

async function load(): Promise<void> {
  loading.value = true;
  errorMessage.value = '';
  try {
    const { items } = await projectsApi.list();
    projects.value = items;
  } catch (error) {
    const info = describeError(error);
    if (info.unauthorized) {
      clearSession();
      await router.push({ name: 'login' });
      return;
    }
    errorMessage.value = info.message;
  } finally {
    loading.value = false;
  }
}

function openProject(project: ProjectSummary): void {
  void router.push({ name: 'project-detail', params: { id: String(project.id) } });
}

function goCreate(): void {
  void router.push({ name: 'project-new' });
}

function specStatus(project: ProjectSummary): string {
  return project.active_spec_id === null ? '需求草稿' : '已冻结规格';
}

onMounted(load);
</script>

<template>
  <section>
    <div class="list-head">
      <div>
        <h1 class="page-title">我的项目</h1>
        <p class="page-subtitle">每个项目从一段自然语言需求开始，确认后才会生成代码。</p>
      </div>
      <el-button type="primary" @click="goCreate">新建项目</el-button>
    </div>

    <el-alert
      v-if="errorMessage"
      :title="errorMessage"
      type="error"
      :closable="false"
      show-icon
      class="list-error"
    />

    <el-table
      v-loading="loading"
      :data="projects"
      row-key="id"
      class="list-table"
      empty-text="还没有项目，点击“新建项目”开始"
      @row-click="openProject"
    >
      <el-table-column prop="name" label="项目名称" min-width="200" />
      <el-table-column label="规格状态" width="140">
        <template #default="{ row }">
          <el-tag :type="row.active_spec_id === null ? 'info' : 'success'" disable-transitions>
            {{ specStatus(row) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="创建时间" width="180">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="100">
        <template #default="{ row }">
          <el-button text type="primary" @click.stop="openProject(row)">查看</el-button>
        </template>
      </el-table-column>
    </el-table>
  </section>
</template>

<style scoped>
.list-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}
.list-error {
  margin-bottom: 16px;
}
.list-table {
  margin-top: 16px;
}
</style>
