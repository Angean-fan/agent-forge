<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';

import { ElMessage } from 'element-plus';

import { projectsApi } from '@/api/client';
import type { ProjectSummary } from '@/api/types';
import { clearSession } from '@/stores/auth';
import {
  refreshProjects,
  setActiveHeaderContext,
  setDefaultRunMode,
  useWorkspaceStore,
} from '@/stores/workspace';
import { describeError } from '@/utils/errors';
import { formatDateTime } from '@/utils/format';

const router = useRouter();
const workspace = useWorkspaceStore();

const REQUIREMENT_MIN = 20;
const REQUIREMENT_MAX = 5000;

const loading = ref(true);
const errorMessage = ref('');
const projects = ref<ProjectSummary[]>([]);

// 底部快速对话输入框：直接输入需求发起新项目对话
const quickRequirement = ref('');
const creating = ref(false);

const trimmedQuick = computed(() => quickRequirement.value.trim());
const canQuickCreate = computed(
  () =>
    !creating.value &&
    trimmedQuick.value.length >= REQUIREMENT_MIN &&
    trimmedQuick.value.length <= REQUIREMENT_MAX,
);

function inferProjectName(text: string): string {
  const cleaned = text
    .replace(/^(我想做一个|我想做|请帮我做一个|请帮我开发一个|需要一个|建设一个|做一个|开发一个)/, '')
    .trim();
  const firstClause = cleaned.split(/[。！？!?\n，,；;]/)[0]?.trim() || cleaned;
  return firstClause.slice(0, 24).trim() || '未命名项目';
}

async function load(): Promise<void> {
  loading.value = true;
  errorMessage.value = '';
  setActiveHeaderContext({
    projectId: null,
    projectName: '工作台总览',
    stageLabel: '',
    cost: null,
  });
  try {
    const { items } = await projectsApi.list();
    projects.value = items;
    workspace.projects = items;
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

function toggleRunMode(): void {
  setDefaultRunMode(workspace.defaultRunMode === 'factory' ? 'standard' : 'factory');
}

async function onQuickCreate(): Promise<void> {
  if (!canQuickCreate.value) return;
  creating.value = true;
  errorMessage.value = '';
  try {
    const text = trimmedQuick.value;
    const project = await projectsApi.create(inferProjectName(text), text);
    quickRequirement.value = '';
    ElMessage.success('项目对话已创建');
    void refreshProjects();
    await router.push({ name: 'project-detail', params: { id: String(project.id) } });
  } catch (error) {
    errorMessage.value = describeError(error).message;
  } finally {
    creating.value = false;
  }
}

function onQuickKeydown(event: KeyboardEvent): void {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    void onQuickCreate();
  }
}

function specStatus(project: ProjectSummary): string {
  return project.active_spec_id === null ? '需求草稿' : '已冻结规格';
}

onMounted(load);
</script>

<template>
  <section class="home-workspace">
    <div class="home-scroll af-scroll">
      <div class="home-stage">
        <div class="hero-card">
          <div class="hero-head">
            <h1 class="page-title">我的项目</h1>
            <el-button type="primary" size="small" @click="goCreate">新建项目</el-button>
          </div>
        </div>

        <el-alert
          v-if="errorMessage"
          :title="errorMessage"
          type="error"
          :closable="false"
          show-icon
          class="list-error"
        />

        <div class="table-card">
          <el-table
            v-loading="loading"
            :data="projects"
            row-key="id"
            class="list-table"
            empty-text="还没有项目，直接在下方对话框输入需求，或点击“新建项目”开始"
            @row-click="openProject"
          >
            <el-table-column prop="name" label="项目名称" min-width="180" />
            <el-table-column label="规格状态" width="126">
              <template #default="{ row }">
                <el-tag :type="row.active_spec_id === null ? 'info' : 'success'" size="small" disable-transitions>
                  {{ specStatus(row) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="创建时间" width="170">
              <template #default="{ row }">
                <span class="af-mono">{{ formatDateTime(row.created_at) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="84">
              <template #default="{ row }">
                <el-button text size="small" type="primary" @click.stop="openProject(row)">打开</el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </div>
    </div>

    <!-- Sticky Bottom Chat Composer -->
    <div class="composer-dock" data-testid="home-chat-composer">
      <div class="composer-inner">
        <div class="composer-box">
          <textarea
            v-model="quickRequirement"
            rows="2"
            :maxlength="REQUIREMENT_MAX"
            class="composer-textarea"
            placeholder="直接描述你想做的管理系统需求（至少 20 字），按 Enter 发起新项目对话..."
            @keydown="onQuickKeydown"
          />
          <div class="composer-toolbar">
            <div class="toolbar-left">
              <button type="button" class="mode-chip af-mono" @click="toggleRunMode">
                模式: {{ workspace.defaultRunMode === 'factory' ? '异步工厂' : '标准装配' }}
              </button>
              <span class="char-hint af-mono">
                {{
                  trimmedQuick.length < REQUIREMENT_MIN
                    ? `还需 ${REQUIREMENT_MIN - trimmedQuick.length} 字`
                    : `${trimmedQuick.length} / ${REQUIREMENT_MAX}`
                }}
              </span>
            </div>
            <button
              type="button"
              class="send-icon-btn"
              :disabled="!canQuickCreate"
              :title="creating ? '创建中...' : '发送 (Enter)'"
              aria-label="发送需求"
              @click="onQuickCreate"
            >
              <svg class="send-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                <line x1="12" y1="19" x2="12" y2="5" />
                <polyline points="5 12 12 5 19 12" />
              </svg>
            </button>
          </div>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.home-workspace {
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--af-bg);
}

.home-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 20px 22px;
}

.home-stage {
  max-width: 820px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.hero-card,
.table-card {
  border-radius: 12px;
  border: 1px solid var(--af-border-strong);
  background: var(--af-surface);
  padding: 16px;
}

.hero-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.hero-head .page-subtitle {
  margin-bottom: 0;
}

.list-error {
  margin-bottom: 0;
}

.list-table {
  width: 100%;
}

.composer-dock {
  flex-shrink: 0;
  border-top: 1px solid var(--af-border);
  background: var(--af-surface);
  padding: 10px 20px 12px;
}

.composer-inner {
  max-width: 820px;
  margin: 0 auto;
}

.composer-box {
  border-radius: 11px;
  border: 1px solid var(--af-border-strong);
  background: var(--af-bg);
  padding: 8px 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.composer-box:focus-within {
  border-color: var(--af-accent-border);
}

.composer-textarea {
  width: 100%;
  border: none;
  background: transparent;
  color: var(--af-fg);
  font: inherit;
  font-size: 12.5px;
  line-height: 1.5;
  resize: none;
  outline: none;
}

.composer-textarea::placeholder {
  color: var(--af-muted);
}

.composer-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.toolbar-left {
  display: flex;
  align-items: center;
  gap: 8px;
}

.mode-chip {
  font-size: 11px;
  padding: 3px 8px;
  border-radius: 6px;
  border: 1px solid var(--af-border-strong);
  background: var(--af-surface);
  color: var(--af-fg-secondary);
  cursor: pointer;
}

.char-hint {
  font-size: 11px;
  color: var(--af-muted);
}

.send-icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  padding: 0;
  border-radius: 8px;
  border: 1px solid var(--af-btn-primary-bg);
  background: var(--af-btn-primary-bg);
  color: var(--af-btn-primary-fg);
  cursor: pointer;
}

.send-icon-btn:hover:not(:disabled) {
  background: var(--af-btn-primary-hover);
}

.send-icon-btn:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}

.send-icon {
  width: 15px;
  height: 15px;
}
</style>
