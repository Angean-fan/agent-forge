<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';

import { ElMessage, ElMessageBox } from 'element-plus';

import { profilesApi, runsApi } from '@/api/client';
import type {
  CustomModelPreset,
  CustomModelPresetCreatePayload,
  FactoryRoutingRole,
  ModelRoleConfig,
  ModelRoleUpdateItem,
  ProfileOption,
  ProjectSummary,
  RunMode,
} from '@/api/types';
import SafeText from '@/components/SafeText.vue';
import {
  addCustomModelPreset,
  deleteCustomModelPreset,
  deleteProject,
  refreshModelConfig,
  refreshProfiles,
  refreshProjectRuns,
  refreshProjects,
  saveModelConfig,
  setDefaultRunMode,
  setProjectRoutingSnapshot,
  useWorkspaceStore,
} from '@/stores/workspace';
import { describeError } from '@/utils/errors';

const route = useRoute();
const router = useRouter();
const workspace = useWorkspaceStore();

const activeTab = ref<'history' | 'models'>('history');
const searchQuery = ref('');
// 每个项目下拉框内当前查看的子面板：历史运行 vs 模型路由
const projectDropdownView = reactive<Record<number, 'runs' | 'routing'>>({});

// 自定义模型库管理状态
const TOOL_REQUIRED_ROLES = new Set(['coding', 'complex_repair', 'dept_coder', 'dept_tester']);
const showAddPreset = ref(false);
const savingPreset = ref(false);
const deletingPresetId = ref<string | null>(null);
const updatingRole = ref<string | null>(null);
const presetForm = reactive<CustomModelPresetCreatePayload>({
  name: '',
  provider: 'openai_compatible',
  model: '',
  base_url: 'https://api.deepseek.com/v1',
  api_key_env: 'DEEPSEEK_API_KEY',
  supports_tools: true,
  max_output_tokens: 8192,
  timeout_seconds: 180,
});

// 模型配置编辑状态（点击角色标题可展开微调高级参数）
const editingRole = ref<string | null>(null);
const savingRole = ref(false);
const modelError = ref('');
const roleForm = reactive<ModelRoleUpdateItem>({
  role: '',
  provider: '',
  model: '',
  base_url: '',
  api_key_env: '',
  enabled: true,
  supports_tools: true,
  max_output_tokens: 8192,
  timeout_seconds: 180,
});

// 技术选型 AI 解释
const explainingProfileId = ref<string | null>(null);
const profileExplanation = ref('');

const filteredProjects = computed(() => {
  const q = searchQuery.value.trim().toLowerCase();
  if (!q) return workspace.projects;
  return workspace.projects.filter(
    (p) => p.name.toLowerCase().includes(q) || String(p.id).includes(q),
  );
});

const readyRolesCount = computed(() => {
  const roles = workspace.modelConfig?.roles ?? [];
  return roles.filter((r) => r.enabled && r.api_key_present).length;
});

const customModels = computed<CustomModelPreset[]>(() => {
  const fromApi = workspace.modelConfig?.custom_models;
  if (fromApi && fromApi.length > 0) {
    return fromApi;
  }
  // 兜底：当后端旧响应未返回 custom_models 时，从现有角色去重推导可选列表
  const seen = new Set<string>();
  const derived: CustomModelPreset[] = [];
  for (const r of workspace.modelConfig?.roles ?? []) {
    const key = `${r.provider}|${r.model}|${r.base_url}|${r.api_key_env}`;
    if (seen.has(key) || !r.model) continue;
    seen.add(key);
    derived.push({
      id: `${r.provider}-${r.model}`.toLowerCase().replace(/[^a-z0-9._-]+/g, '-'),
      name: `${r.model} (${r.provider})`,
      provider: r.provider,
      model: r.model,
      base_url: r.base_url,
      base_url_host: r.base_url_host,
      api_key_env: r.api_key_env,
      api_key_present: r.api_key_present,
      supports_tools: r.supports_tools,
      max_output_tokens: r.max_output_tokens,
      timeout_seconds: r.timeout_seconds,
      removable: false,
    });
  }
  return derived;
});

function matchedPresetIdForRole(role: ModelRoleConfig): string {
  const found = customModels.value.find(
    (p) =>
      p.provider === role.provider &&
      p.model === role.model &&
      p.base_url === role.base_url &&
      p.api_key_env === role.api_key_env,
  );
  return found?.id ?? '';
}

function isPresetIncompatibleForRole(role: ModelRoleConfig, preset: CustomModelPreset): boolean {
  return TOOL_REQUIRED_ROLES.has(role.role) && !preset.supports_tools;
}

function resetPresetForm(): void {
  presetForm.name = '';
  presetForm.provider = 'openai_compatible';
  presetForm.model = '';
  presetForm.base_url = 'https://api.deepseek.com/v1';
  presetForm.api_key_env = 'DEEPSEEK_API_KEY';
  presetForm.supports_tools = true;
  presetForm.max_output_tokens = 8192;
  presetForm.timeout_seconds = 180;
}

async function onAddCustomPreset(): Promise<void> {
  const model = presetForm.model.trim();
  const provider = presetForm.provider.trim();
  const baseUrl = presetForm.base_url.trim();
  const apiKeyEnv = presetForm.api_key_env.trim();
  if (!model || !provider || !baseUrl || !apiKeyEnv) {
    modelError.value = '请完整填写模型名、提供方、接口地址与密钥环境变量名。';
    return;
  }
  savingPreset.value = true;
  modelError.value = '';
  try {
    await addCustomModelPreset({
      name: presetForm.name.trim() || model,
      provider,
      model,
      base_url: baseUrl,
      api_key_env: apiKeyEnv,
      supports_tools: presetForm.supports_tools,
      max_output_tokens: presetForm.max_output_tokens || null,
      timeout_seconds: presetForm.timeout_seconds || null,
    });
    ElMessage.success(`已添加自定义模型「${presetForm.name.trim() || model}」`);
    resetPresetForm();
    showAddPreset.value = false;
  } catch (error) {
    modelError.value = describeError(error).message;
  } finally {
    savingPreset.value = false;
  }
}

async function onDeleteCustomPreset(preset: CustomModelPreset): Promise<void> {
  if (!preset.removable || deletingPresetId.value) return;
  deletingPresetId.value = preset.id;
  modelError.value = '';
  try {
    await deleteCustomModelPreset(preset.id);
    ElMessage.success(`已移除自定义模型「${preset.name}」`);
  } catch (error) {
    modelError.value = describeError(error).message;
  } finally {
    deletingPresetId.value = null;
  }
}

async function onSelectPresetForRole(role: ModelRoleConfig, presetId: string): Promise<void> {
  const preset = customModels.value.find((p) => p.id === presetId);
  if (!preset) return;
  if (isPresetIncompatibleForRole(role, preset)) {
    modelError.value = `角色「${role.label}」必须使用支持工具调用（supports_tools=true）的模型。`;
    return;
  }
  updatingRole.value = role.role;
  modelError.value = '';
  try {
    await saveModelConfig([
      {
        role: role.role,
        provider: preset.provider,
        model: preset.model,
        base_url: preset.base_url,
        api_key_env: preset.api_key_env,
        enabled: role.enabled,
        supports_tools: preset.supports_tools,
        max_output_tokens: preset.max_output_tokens ?? role.max_output_tokens ?? 8192,
        timeout_seconds: preset.timeout_seconds ?? role.timeout_seconds ?? 180,
      },
    ]);
    ElMessage.success(`已为「${role.label}」切换为 ${preset.name}`);
  } catch (error) {
    modelError.value = describeError(error).message;
  } finally {
    updatingRole.value = null;
  }
}

function isActiveProject(projectId: number): boolean {
  if (workspace.activeProjectId === projectId) return true;
  if (
    (route.name === 'project-detail' || route.name === 'project-interview') &&
    Number(route.params.id) === projectId
  ) {
    return true;
  }
  return false;
}

function isActiveRun(runId: number): boolean {
  if (route.name === 'run-detail' && Number(route.params.id) === runId) return true;
  if (route.name === 'project-detail' && Number(route.query.run) === runId) return true;
  return false;
}

function runStatusColor(status: string): string {
  if (status === 'SUCCEEDED') return 'var(--af-ok)';
  if (status === 'FAILED') return 'var(--af-err)';
  if (status === 'PAUSED_BUDGET') return 'var(--af-warn)';
  if (status === 'CANCELLED') return 'var(--af-muted)';
  return 'var(--af-accent)';
}

function runStatusShort(status: string): string {
  const map: Record<string, string> = {
    QUEUED: '排队',
    PLANNING: '规划中',
    GENERATING: '组装中',
    VERIFYING: '验收中',
    SUCCEEDED: '已完成',
    FAILED: '失败',
    CANCELLED: '已取消',
    PAUSED_BUDGET: '预算暂停',
  };
  return map[status] ?? status;
}

function getDropdownView(projectId: number): 'runs' | 'routing' {
  return projectDropdownView[projectId] ?? 'runs';
}

async function ensureProjectRoutingSnapshot(projectId: number): Promise<void> {
  if (workspace.projectRoutingSnapshots[projectId]?.roles.length) return;
  if (!workspace.projectRuns[projectId]) {
    await refreshProjectRuns(projectId);
  }
  const latestRun = workspace.projectRuns[projectId]?.[0];
  if (!latestRun || typeof runsApi.factory !== 'function') return;
  try {
    const fd = await runsApi.factory(latestRun.id);
    if (fd.model_routing_snapshot?.length) {
      setProjectRoutingSnapshot(projectId, latestRun.id, fd.model_routing_snapshot);
    }
  } catch {
    // 无工厂路由快照时回退展示全局 A04 模型路由
  }
}

function getProjectRoutingRoles(projectId: number): {
  sourceLabel: string;
  roles: FactoryRoutingRole[];
} {
  const snap = workspace.projectRoutingSnapshots[projectId];
  if (snap && snap.roles.length > 0) {
    return {
      sourceLabel: `运行 #${snap.runId} 快照`,
      roles: snap.roles,
    };
  }
  const globalRoles = (workspace.modelConfig?.roles ?? []).map((r) => ({
    role: r.role,
    provider: r.provider,
    model: r.model,
    base_url_host: r.base_url_host,
    api_key_env: r.api_key_env,
    api_key_present: r.api_key_present,
    enabled: r.enabled,
    supports_tools: r.supports_tools,
  }));
  return {
    sourceLabel: '当前全局 A04 路由',
    roles: globalRoles,
  };
}

function selectDropdownView(projectId: number, view: 'runs' | 'routing'): void {
  projectDropdownView[projectId] = view;
  if (view === 'routing') {
    void ensureProjectRoutingSnapshot(projectId);
  }
}

async function toggleProjectRuns(project: ProjectSummary): Promise<void> {
  const next = !workspace.expandedProjectIds[project.id];
  workspace.expandedProjectIds[project.id] = next;
  if (next && !workspace.projectRuns[project.id]) {
    await refreshProjectRuns(project.id);
  }
}

function openProjectChat(project: ProjectSummary): void {
  void router.push({
    name: 'project-detail',
    params: { id: String(project.id) },
    query: { view: 'chat' },
  });
}

function isActiveProjectChat(projectId: number): boolean {
  return (
    route.name === 'project-detail' &&
    Number(route.params.id) === projectId &&
    !route.query.run &&
    route.query.view !== 'flowchart'
  );
}

const deletingProjectId = ref<number | null>(null);

async function onDeleteProject(project: ProjectSummary): Promise<void> {
  if (deletingProjectId.value !== null) return;
  try {
    await ElMessageBox.confirm(
      `确定要删除项目「${project.name}」吗？删除后将永久移除该项目的规格草稿与历史运行记录，且无法恢复。`,
      '删除项目警告',
      {
        confirmButtonText: '确认删除',
        cancelButtonText: '取消',
        type: 'warning',
        confirmButtonClass: 'el-button--danger',
      },
    );
  } catch {
    return;
  }

  deletingProjectId.value = project.id;
  try {
    const viewingDeleted =
      workspace.activeProjectId === project.id ||
      ((route.name === 'project-detail' || route.name === 'project-interview') &&
        Number(route.params.id) === project.id);
    await deleteProject(project.id);
    ElMessage.success(`已删除项目「${project.name}」`);
    if (viewingDeleted) {
      await router.push({ name: 'projects' });
    }
  } catch (error) {
    ElMessage.error(describeError(error).message);
  } finally {
    deletingProjectId.value = null;
  }
}

function openRun(projectId: number, runId: number): void {
  void router.push({
    name: 'project-detail',
    params: { id: String(projectId) },
    query: { run: String(runId), view: 'flowchart' },
  });
}

function goNewProject(): void {
  void router.push({ name: 'project-new' });
}

function onModeChange(mode: RunMode): void {
  setDefaultRunMode(mode);
}

function startEditRole(role: ModelRoleConfig): void {
  modelError.value = '';
  if (editingRole.value === role.role) {
    editingRole.value = null;
    return;
  }
  editingRole.value = role.role;
  roleForm.role = role.role;
  roleForm.provider = role.provider;
  roleForm.model = role.model;
  roleForm.base_url = role.base_url;
  roleForm.api_key_env = role.api_key_env;
  roleForm.enabled = role.enabled;
  roleForm.supports_tools = role.supports_tools;
  roleForm.max_output_tokens = role.max_output_tokens ?? 8192;
  roleForm.timeout_seconds = role.timeout_seconds ?? 180;
}

async function onSaveRole(): Promise<void> {
  if (!roleForm.role) return;
  savingRole.value = true;
  modelError.value = '';
  try {
    await saveModelConfig([
      {
        role: roleForm.role,
        provider: roleForm.provider.trim(),
        model: roleForm.model.trim(),
        base_url: roleForm.base_url.trim(),
        api_key_env: roleForm.api_key_env.trim(),
        enabled: roleForm.enabled,
        supports_tools: roleForm.supports_tools,
        max_output_tokens: roleForm.max_output_tokens || null,
        timeout_seconds: roleForm.timeout_seconds || null,
      },
    ]);
    ElMessage.success(`已保存角色 ${roleForm.role} 的模型路由配置`);
    editingRole.value = null;
  } catch (error) {
    modelError.value = describeError(error).message;
  } finally {
    savingRole.value = false;
  }
}

async function onExplainProfile(option: ProfileOption): Promise<void> {
  const targetProjectId = workspace.activeProjectId ?? workspace.projects[0]?.id;
  if (!targetProjectId) {
    ElMessage.warning('请先创建项目后再获取 AI 选型解释');
    return;
  }
  explainingProfileId.value = option.profile_id;
  try {
    const res = await profilesApi.explain(targetProjectId, option.profile_id);
    profileExplanation.value = `${option.profile_id}: ${res.explanation}`;
  } catch (error) {
    ElMessage.error(describeError(error).message);
  } finally {
    explainingProfileId.value = null;
  }
}

watch(
  () => workspace.activeProjectId,
  (pid) => {
    if (pid) {
      workspace.expandedProjectIds[pid] = true;
      void refreshProjectRuns(pid);
    }
  },
);

onMounted(() => {
  void Promise.all([refreshProjects(), refreshModelConfig(), refreshProfiles()]);
});
</script>

<template>
  <aside class="wb-sidebar" data-testid="workbench-sidebar">
    <!-- Top Segmented Switcher + New Project CTA -->
    <div class="sb-top">
      <div class="sb-segment" role="tablist" aria-label="侧边栏导航">
        <button
          type="button"
          class="sb-seg-btn"
          :class="{ active: activeTab === 'history' }"
          data-testid="sidebar-tab-history"
          @click="activeTab = 'history'"
        >
          历史记录
        </button>
        <button
          type="button"
          class="sb-seg-btn"
          :class="{ active: activeTab === 'models' }"
          data-testid="sidebar-tab-models"
          @click="activeTab = 'models'"
        >
          模型配置
        </button>
      </div>

      <button type="button" class="sb-new-btn" data-testid="sidebar-new-project" @click="goNewProject">
        <svg class="sb-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <line x1="12" y1="5" x2="12" y2="19" />
          <line x1="5" y1="12" x2="19" y2="12" />
        </svg>
        <span>新建项目</span>
      </button>
    </div>

    <!-- TAB 1: HISTORY (Projects + Expandable Runs) -->
    <div v-show="activeTab === 'history'" class="sb-body af-scroll" data-testid="sidebar-history-panel">
      <div class="sb-search-wrap">
        <svg class="sb-search-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="11" cy="11" r="8" />
          <line x1="21" y1="21" x2="16.65" y2="16.65" />
        </svg>
        <input
          v-model="searchQuery"
          type="text"
          class="sb-search-input"
          placeholder="搜索项目..."
          aria-label="搜索项目"
        />
      </div>

      <div class="sb-section-head">
        <span>历史项目</span>
        <span class="af-mono">{{ filteredProjects.length }}</span>
      </div>

      <div v-if="workspace.projectsLoading && !workspace.projects.length" class="sb-empty">
        加载项目中...
      </div>
      <div v-else-if="!filteredProjects.length" class="sb-empty">
        暂无匹配项目，点击上方新建开始
      </div>

      <div v-else class="sb-project-list">
        <div
          v-for="project in filteredProjects"
          :key="project.id"
          class="sb-project-card"
          :class="{ active: isActiveProject(project.id) }"
        >
          <div class="sb-project-row" :data-testid="`project-row-${project.id}`">
            <button
              type="button"
              class="sb-expand-btn"
              :aria-label="workspace.expandedProjectIds[project.id] ? '收起项目历史' : '展开项目历史'"
              @click.stop="toggleProjectRuns(project)"
            >
              <svg
                class="sb-chevron"
                :class="{ rotated: workspace.expandedProjectIds[project.id] }"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
              >
                <polyline points="9 18 15 12 9 6" />
              </svg>
            </button>
            <button
              type="button"
              class="sb-project-main"
              :data-testid="`project-toggle-${project.id}`"
              @click="toggleProjectRuns(project)"
            >
              <span class="sb-project-name">{{ project.name }}</span>
            </button>
            <div class="sb-row-trailing">
              <span
                class="sb-status-pill"
                :class="project.active_spec_id === null ? 'pill-draft' : 'pill-frozen'"
              >
                {{ project.active_spec_id === null ? '草稿' : '已冻结' }}
              </span>
              <button
                type="button"
                class="sb-delete-btn"
                :disabled="deletingProjectId === project.id"
                :aria-label="`删除项目 ${project.name}`"
                title="删除项目"
                :data-testid="`delete-project-${project.id}`"
                @click.stop="onDeleteProject(project)"
              >
                <svg class="sb-trash-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <polyline points="3 6 5 6 21 6" />
                  <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
                  <path d="M10 11v6" />
                  <path d="M14 11v6" />
                  <path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2" />
                </svg>
              </button>
            </div>
          </div>

          <!-- Nested Dropdown: Historical Runs & Model Routing Snapshot -->
          <div
            v-if="workspace.expandedProjectIds[project.id]"
            class="sb-runs-list"
            :data-testid="`project-dropdown-${project.id}`"
          >
            <div class="sb-sub-tabs">
              <button
                type="button"
                class="sb-sub-tab"
                :class="{ active: getDropdownView(project.id) === 'runs' }"
                :data-testid="`project-subtab-runs-${project.id}`"
                @click.stop="selectDropdownView(project.id, 'runs')"
              >
                历史进程 ({{ workspace.projectRuns[project.id]?.length ?? 0 }})
              </button>
              <button
                type="button"
                class="sb-sub-tab"
                :class="{ active: getDropdownView(project.id) === 'routing' }"
                :data-testid="`project-subtab-routing-${project.id}`"
                @click.stop="selectDropdownView(project.id, 'routing')"
              >
                模型路由
              </button>
            </div>

            <!-- Sub-tab 1: Historical Chat & Runs (Click inside dropdown to navigate) -->
            <template v-if="getDropdownView(project.id) === 'runs'">
              <button
                type="button"
                class="sb-run-item sb-chat-entry"
                :class="{ active: isActiveProjectChat(project.id) }"
                :data-testid="`project-chat-entry-${project.id}`"
                @click="openProjectChat(project)"
              >
                <span class="sb-run-dot" style="background: var(--af-accent);" />
                <span class="sb-run-id">需求对话与规格</span>
                <span class="sb-run-status">{{ project.active_spec_id === null ? '草稿' : '已冻结' }}</span>
              </button>

              <div
                v-if="!(workspace.projectRuns[project.id]?.length)"
                class="sb-run-empty"
              >
                尚无构建运行记录
              </div>
              <button
                v-for="run in workspace.projectRuns[project.id] ?? []"
                :key="run.id"
                type="button"
                class="sb-run-item"
                :class="{ active: isActiveRun(run.id) }"
                :data-testid="`project-run-entry-${run.id}`"
                @click="openRun(project.id, run.id)"
              >
                <span
                  class="sb-run-dot"
                  :style="{ background: runStatusColor(run.status) }"
                />
                <span class="af-mono sb-run-id">#{{ run.id }}</span>
                <span class="sb-run-status">{{ runStatusShort(run.status) }}</span>
              </button>
            </template>

            <!-- Sub-tab 2: Per-Project Model Routing Snapshot -->
            <div
              v-else
              class="sb-routing-box"
              :data-testid="`project-routing-panel-${project.id}`"
            >
              <div class="sb-routing-head">
                <span class="af-mono sb-routing-src">{{ getProjectRoutingRoles(project.id).sourceLabel }}</span>
                <button
                  type="button"
                  class="sb-link-btn"
                  @click.stop="activeTab = 'models'"
                >
                  去编辑
                </button>
              </div>
              <div
                v-if="!getProjectRoutingRoles(project.id).roles.length"
                class="sb-run-empty"
              >
                暂无路由信息
              </div>
              <div
                v-for="role in getProjectRoutingRoles(project.id).roles"
                :key="`${project.id}-${role.role}`"
                class="sb-routing-row"
              >
                <span
                  class="sb-key-dot"
                  :class="role.api_key_present ? 'dot-ok' : 'dot-warn'"
                  :title="`${role.api_key_env}: ${role.api_key_present ? '已注入' : '未注入'}`"
                />
                <span class="af-mono sb-routing-role">{{ role.role }}</span>
                <span class="af-mono sb-routing-model" :title="`${role.model} @ ${role.base_url_host}`">
                  {{ role.model }}
                </span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- TAB 2: MODEL CONFIGURATION & ROUTING -->
    <div v-show="activeTab === 'models'" class="sb-body af-scroll" data-testid="sidebar-model-config">
      <!-- Default Run Mode -->
      <div class="sb-card">
        <div class="sb-card-head">
          <span class="sb-card-title">默认模式</span>
        </div>
        <div class="sb-mode-toggle">
          <button
            type="button"
            class="sb-mode-btn"
            :class="{ active: workspace.defaultRunMode === 'standard' }"
            @click="onModeChange('standard')"
          >
            标准装配
          </button>
          <button
            type="button"
            class="sb-mode-btn"
            :class="{ active: workspace.defaultRunMode === 'factory' }"
            @click="onModeChange('factory')"
          >
            异步工厂
          </button>
        </div>
      </div>

      <!-- Technical Profile Summary -->
      <div class="sb-card">
        <div class="sb-card-head">
          <span class="sb-card-title">技术选型组合</span>
          <button
            type="button"
            class="sb-link-btn"
            @click="router.push({ name: 'profiles' })"
          >
            全部组合 →
          </button>
        </div>
        <div v-if="workspace.profiles.length" class="sb-profile-list">
          <div
            v-for="opt in workspace.profiles.slice(0, 2)"
            :key="opt.profile_id"
            class="sb-profile-item"
          >
            <div class="sb-profile-top">
              <span class="af-mono sb-profile-id">{{ opt.profile_id }}@{{ opt.profile_version }}</span>
              <span v-if="opt.recommended" class="sb-ok-badge">推荐</span>
            </div>
            <div class="sb-profile-meta">
              {{ opt.frontend_language ?? 'TS' }} · {{ opt.orm ?? 'MyBatis-Plus' }}
            </div>
            <button
              type="button"
              class="sb-link-btn"
              :disabled="explainingProfileId === opt.profile_id"
              @click="onExplainProfile(opt)"
            >
              {{ explainingProfileId === opt.profile_id ? '生成解释中...' : 'AI 通俗解释' }}
            </button>
          </div>
        </div>
        <div v-else class="sb-hint af-mono">demo-ts-mp@0.3 · Vue3+TS / SpringBoot</div>
        <div v-if="profileExplanation" class="sb-explain-box">
          <SafeText :text="profileExplanation" />
        </div>
      </div>

      <!-- Custom Model Library (Unified Add Custom Model) -->
      <div class="sb-card" data-testid="custom-models-section">
        <div class="sb-card-head">
          <span class="sb-card-title">自定义模型库 ({{ customModels.length }})</span>
          <button
            type="button"
            class="sb-add-model-btn"
            data-testid="add-custom-model-btn"
            :aria-label="showAddPreset ? '收起添加模型表单' : '添加自定义模型'"
            :title="showAddPreset ? '收起' : '添加自定义模型'"
            @click="showAddPreset = !showAddPreset; modelError = ''"
          >
            <svg class="sb-icon-xs" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <line v-if="!showAddPreset" x1="12" y1="5" x2="12" y2="19" />
              <line x1="5" y1="12" x2="19" y2="12" />
            </svg>
          </button>
        </div>

        <!-- Add Custom Model Form -->
        <div v-if="showAddPreset" class="sb-preset-form" data-testid="custom-model-form">
          <label class="sb-field">
            <span>显示名称（可选）</span>
            <input
              v-model="presetForm.name"
              type="text"
              class="sb-input"
              placeholder="例如 DeepSeek Chat / Qwen3 Coder"
              data-testid="preset-name-input"
            />
          </label>
          <label class="sb-field">
            <span>模型名 (model)</span>
            <input
              v-model="presetForm.model"
              type="text"
              class="sb-input af-mono"
              placeholder="例如 deepseek-chat"
              data-testid="preset-model-input"
            />
          </label>
          <label class="sb-field">
            <span>提供方 (provider)</span>
            <input
              v-model="presetForm.provider"
              type="text"
              class="sb-input af-mono"
              placeholder="例如 openai_compatible"
              data-testid="preset-provider-input"
            />
          </label>
          <label class="sb-field">
            <span>接口地址 (base_url)</span>
            <input
              v-model="presetForm.base_url"
              type="text"
              class="sb-input af-mono"
              placeholder="https://api.deepseek.com/v1"
              data-testid="preset-baseurl-input"
            />
          </label>
          <label class="sb-field">
            <span>密钥环境变量名 (api_key_env)</span>
            <input
              v-model="presetForm.api_key_env"
              type="text"
              class="sb-input af-mono"
              placeholder="例如 DEEPSEEK_API_KEY"
              data-testid="preset-env-input"
            />
          </label>
          <div class="sb-field-grid">
            <label class="sb-field">
              <span>最大输出 Token</span>
              <input v-model.number="presetForm.max_output_tokens" type="number" min="256" class="sb-input af-mono" />
            </label>
            <label class="sb-field">
              <span>超时 (秒)</span>
              <input v-model.number="presetForm.timeout_seconds" type="number" min="10" class="sb-input af-mono" />
            </label>
          </div>
          <label class="sb-check">
            <input v-model="presetForm.supports_tools" type="checkbox" />
            <span>支持工具调用 (supports_tools)</span>
          </label>
          <div class="sb-editor-actions">
            <button type="button" class="sb-btn-ghost" @click="showAddPreset = false">取消</button>
            <button
              type="button"
              class="sb-btn-primary"
              :disabled="savingPreset"
              data-testid="submit-custom-model-btn"
              @click="onAddCustomPreset"
            >
              {{ savingPreset ? '添加中...' : '添加到模型库' }}
            </button>
          </div>
        </div>

        <!-- Saved Custom Models List -->
        <div v-if="customModels.length" class="sb-preset-list">
          <div
            v-for="preset in customModels"
            :key="preset.id"
            class="sb-preset-item"
            :data-testid="`custom-model-item-${preset.id}`"
          >
            <span
              class="sb-key-dot"
              :class="preset.api_key_present ? 'dot-ok' : 'dot-warn'"
              :title="`${preset.api_key_env}: ${preset.api_key_present ? '已注入' : '未注入'}`"
            />
            <div class="sb-preset-info">
              <div class="sb-preset-name">{{ preset.name }}</div>
              <div class="af-mono sb-preset-meta">
                {{ preset.model }} · {{ preset.base_url_host || preset.provider }}
              </div>
            </div>
            <button
              v-if="preset.removable"
              type="button"
              class="sb-del-preset-btn"
              :disabled="deletingPresetId === preset.id"
              :data-testid="`delete-preset-${preset.id}`"
              title="从模型库移除"
              @click="onDeleteCustomPreset(preset)"
            >
              ×
            </button>
          </div>
        </div>
        <div v-else class="sb-hint">暂无自定义模型，点击右上角「+ 添加自定义模型」开始。</div>
      </div>

      <!-- A04 Role Model Allocation via Dropdown -->
      <div class="sb-section-head">
        <span>角色模型分配</span>
        <span class="af-mono" style="color: var(--af-ok);">
          {{ readyRolesCount }}/{{ workspace.modelConfig?.roles.length ?? 0 }} 就绪
        </span>
      </div>

      <el-alert
        v-if="modelError"
        :title="modelError"
        type="error"
        :closable="false"
        show-icon
        class="sb-error"
      />

      <div v-if="workspace.modelConfigLoading && !workspace.modelConfig" class="sb-empty">
        读取模型配置中...
      </div>

      <div v-else class="sb-role-list">
        <div
          v-for="role in workspace.modelConfig?.roles ?? []"
          :key="role.role"
          class="sb-role-card"
          :class="{ editing: editingRole === role.role }"
          :data-testid="`model-role-${role.role}`"
        >
          <button
            type="button"
            class="sb-role-summary"
            title="点击展开单角色高级参数微调"
            @click="startEditRole(role)"
          >
            <div class="sb-role-info">
              <div class="sb-role-title-row">
                <span class="sb-role-label">{{ role.label }}</span>
                <span
                  class="sb-role-badge"
                  :class="role.enabled ? 'badge-on' : 'badge-off'"
                >
                  {{ role.enabled ? '启用' : '停用' }}
                </span>
              </div>
              <div class="af-mono sb-role-sub">
                {{ role.role }}
              </div>
            </div>
            <span
              class="sb-key-dot"
              :class="role.api_key_present ? 'dot-ok' : 'dot-warn'"
              :title="`${role.api_key_env}: ${role.api_key_present ? '已注入' : '未注入'}`"
            />
          </button>

          <!-- Direct Dropdown Selector from Custom Model Library -->
          <div class="sb-role-select-wrap">
            <select
              class="sb-role-select af-mono"
              :value="matchedPresetIdForRole(role)"
              :disabled="updatingRole === role.role || !customModels.length"
              :aria-label="`为 ${role.label} 选择模型`"
              :data-testid="`role-model-select-${role.role}`"
              @change="onSelectPresetForRole(role, ($event.target as HTMLSelectElement).value)"
            >
              <option v-if="!matchedPresetIdForRole(role)" value="" disabled>
                {{ role.model }}（当前自定义）
              </option>
              <option
                v-for="preset in customModels"
                :key="preset.id"
                :value="preset.id"
                :disabled="isPresetIncompatibleForRole(role, preset)"
              >
                {{ preset.name }} ({{ preset.model }}){{ isPresetIncompatibleForRole(role, preset) ? ' [需工具调用]' : '' }}
              </option>
            </select>
          </div>

          <!-- Inline Editor for this Role -->
          <div v-if="editingRole === role.role" class="sb-role-editor">
            <label class="sb-field">
              <span>提供方 (provider)</span>
              <input v-model="roleForm.provider" type="text" class="sb-input af-mono" />
            </label>
            <label class="sb-field">
              <span>模型名 (model)</span>
              <input
                v-model="roleForm.model"
                type="text"
                class="sb-input af-mono"
                data-testid="edit-role-model"
              />
            </label>
            <label class="sb-field">
              <span>接口地址 (base_url)</span>
              <input v-model="roleForm.base_url" type="text" class="sb-input af-mono" />
            </label>
            <label class="sb-field">
              <span>密钥环境变量名 (api_key_env)</span>
              <input
                v-model="roleForm.api_key_env"
                type="text"
                class="sb-input af-mono"
                placeholder="例如 DEEPSEEK_API_KEY"
              />
            </label>
            <div class="sb-field-grid">
              <label class="sb-field">
                <span>最大输出 Token</span>
                <input v-model.number="roleForm.max_output_tokens" type="number" min="256" class="sb-input af-mono" />
              </label>
              <label class="sb-field">
                <span>超时 (秒)</span>
                <input v-model.number="roleForm.timeout_seconds" type="number" min="10" class="sb-input af-mono" />
              </label>
            </div>
            <div class="sb-check-row">
              <label class="sb-check">
                <input v-model="roleForm.enabled" type="checkbox" />
                <span>启用该角色</span>
              </label>
              <label class="sb-check">
                <input v-model="roleForm.supports_tools" type="checkbox" />
                <span>支持工具调用</span>
              </label>
            </div>
            <div class="sb-editor-actions">
              <button type="button" class="sb-btn-ghost" @click="editingRole = null">取消</button>
              <button
                type="button"
                class="sb-btn-primary"
                :disabled="savingRole"
                data-testid="save-model-role"
                @click="onSaveRole"
              >
                {{ savingRole ? '保存中...' : '保存配置' }}
              </button>
            </div>
          </div>
        </div>
      </div>

      <!-- Bottom Minimal Status -->
    </div>
  </aside>
</template>

<style scoped>
.wb-sidebar {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-width: 0;
  background: var(--af-sidebar);
  overflow: hidden;
}

.sb-top {
  padding: 10px;
  border-bottom: 1px solid var(--af-border);
  display: flex;
  flex-direction: column;
  gap: 8px;
  flex-shrink: 0;
}

.sb-segment {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 2px;
  padding: 2px;
  border-radius: 8px;
  background: var(--af-surface);
  border: 1px solid var(--af-border);
}

.sb-seg-btn {
  border: none;
  background: transparent;
  color: var(--af-muted);
  font-size: 12px;
  font-weight: 500;
  padding: 5px 8px;
  border-radius: 6px;
  cursor: pointer;
  transition: background 140ms ease, color 140ms ease;
}

.sb-seg-btn.active {
  background: var(--af-surface-2);
  color: var(--af-fg);
}

.sb-new-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  height: 32px;
  border-radius: 8px;
  border: 1px solid var(--af-btn-primary-bg);
  background: var(--af-btn-primary-bg);
  color: var(--af-btn-primary-fg);
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition: background 140ms ease;
}

.sb-new-btn:hover {
  background: var(--af-btn-primary-hover);
}

.sb-icon {
  width: 14px;
  height: 14px;
}

.sb-icon-xs {
  width: 13px;
  height: 13px;
}

.sb-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 10px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.sb-search-wrap {
  position: relative;
}

.sb-search-icon {
  position: absolute;
  left: 9px;
  top: 50%;
  transform: translateY(-50%);
  width: 13px;
  height: 13px;
  color: var(--af-muted);
  pointer-events: none;
}

.sb-search-input {
  width: 100%;
  height: 30px;
  padding: 0 8px 0 28px;
  border-radius: 7px;
  border: 1px solid var(--af-border);
  background: var(--af-surface);
  color: var(--af-fg);
  font-size: 12px;
  outline: none;
}

.sb-search-input:focus {
  border-color: var(--af-accent-border);
}

.sb-section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 11px;
  font-weight: 600;
  color: var(--af-muted);
  padding: 0 2px;
  letter-spacing: 0.02em;
}

.sb-empty {
  font-size: 12px;
  color: var(--af-muted);
  padding: 14px 8px;
  text-align: center;
}

.sb-project-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.sb-project-card {
  border-radius: 9px;
  border: 1px solid var(--af-border);
  background: transparent;
  padding: 6px;
  transition: border-color 140ms ease, background 140ms ease;
}

.sb-project-card:hover {
  background: var(--af-surface);
}

.sb-project-card.active {
  background: var(--af-surface);
  border-color: var(--af-accent-border);
}

.sb-project-row {
  display: flex;
  align-items: center;
  gap: 4px;
}

.sb-expand-btn {
  width: 20px;
  height: 20px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  color: var(--af-muted);
  cursor: pointer;
  flex-shrink: 0;
  border-radius: 4px;
}

.sb-expand-btn:hover {
  background: var(--af-surface-2);
  color: var(--af-fg);
}

.sb-chevron {
  width: 12px;
  height: 12px;
  transition: transform 140ms ease;
}

.sb-chevron.rotated {
  transform: rotate(90deg);
}

.sb-project-main {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 6px;
  border: none;
  background: transparent;
  color: var(--af-fg);
  padding: 2px 4px;
  cursor: pointer;
  text-align: left;
}

.sb-project-name {
  font-size: 12.5px;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sb-row-trailing {
  display: inline-flex;
  align-items: center;
  justify-content: flex-end;
  min-width: 44px;
  height: 22px;
  flex-shrink: 0;
}

.sb-status-pill {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 99px;
  flex-shrink: 0;
  font-weight: 500;
}

.sb-delete-btn {
  display: none;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border: none;
  border-radius: 5px;
  background: transparent;
  color: var(--af-muted);
  cursor: pointer;
  transition: background 120ms ease, color 120ms ease;
}

.sb-delete-btn:hover {
  background: var(--af-err-soft);
  color: var(--af-err);
}

.sb-delete-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.sb-trash-icon {
  width: 13px;
  height: 13px;
}

/* 鼠标移到对应项目行上时，右侧状态徽标切换为垃圾桶图标 */
.sb-project-row:hover .sb-status-pill,
.sb-project-row:focus-within .sb-status-pill {
  display: none;
}

.sb-project-row:hover .sb-delete-btn,
.sb-project-row:focus-within .sb-delete-btn {
  display: inline-flex;
}

.pill-draft {
  background: var(--af-surface-2);
  color: var(--af-muted);
}

.pill-frozen {
  background: var(--af-ok-soft);
  color: var(--af-ok);
}

.sb-runs-list {
  margin-top: 6px;
  padding-top: 6px;
  padding-left: 8px;
  border-top: 1px solid var(--af-border);
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.sb-sub-tabs {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 2px;
  padding: 2px;
  border-radius: 6px;
  background: var(--af-bg);
  border: 1px solid var(--af-border);
}

.sb-sub-tab {
  border: none;
  background: transparent;
  color: var(--af-muted);
  font-size: 10.5px;
  font-weight: 500;
  padding: 3px 4px;
  border-radius: 4px;
  cursor: pointer;
}

.sb-sub-tab.active {
  background: var(--af-surface-2);
  color: var(--af-fg);
}

.sb-routing-box {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 4px 2px;
}

.sb-routing-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  padding: 0 2px 2px;
}

.sb-routing-src {
  font-size: 10px;
  color: var(--af-muted);
}

.sb-routing-row {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 3px 5px;
  border-radius: 5px;
  background: var(--af-surface-2);
  font-size: 10.5px;
}

.sb-routing-role {
  color: var(--af-muted);
  flex-shrink: 0;
}

.sb-routing-model {
  color: var(--af-fg);
  margin-left: auto;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sb-run-empty {
  font-size: 11px;
  color: var(--af-muted);
  padding: 2px 6px;
}

.sb-run-item {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  border: none;
  background: transparent;
  color: var(--af-fg-secondary);
  font-size: 11.5px;
  padding: 4px 6px;
  border-radius: 6px;
  cursor: pointer;
  text-align: left;
}

.sb-run-item:hover {
  background: var(--af-surface-2);
}

.sb-run-item.active {
  background: var(--af-accent-soft);
  color: var(--af-fg);
}

.sb-run-dot {
  width: 6px;
  height: 6px;
  border-radius: 99px;
  flex-shrink: 0;
}

.sb-run-id {
  font-weight: 600;
}

.sb-run-status {
  color: var(--af-muted);
  margin-left: auto;
  font-size: 11px;
}

.sb-card {
  border-radius: 9px;
  border: 1px solid var(--af-border);
  background: var(--af-surface);
  padding: 10px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.sb-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.sb-card-title {
  font-size: 12px;
  font-weight: 600;
}

.sb-muted-tag {
  font-size: 10px;
  color: var(--af-muted);
}

.sb-mode-toggle {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 3px;
  padding: 2px;
  border-radius: 7px;
  background: var(--af-surface-2);
  border: 1px solid var(--af-border);
}

.sb-mode-btn {
  border: none;
  background: transparent;
  color: var(--af-muted);
  font-size: 11.5px;
  font-weight: 500;
  padding: 4px 6px;
  border-radius: 5px;
  cursor: pointer;
}

.sb-mode-btn.active {
  background: var(--af-surface);
  color: var(--af-fg);
}

.sb-hint {
  margin: 0;
  font-size: 11px;
  line-height: 1.45;
  color: var(--af-muted);
}

.sb-link-btn {
  border: none;
  background: transparent;
  color: var(--af-accent);
  font-size: 11px;
  cursor: pointer;
  padding: 0;
  text-align: left;
}

.sb-link-btn:hover {
  text-decoration: underline;
}

.sb-profile-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.sb-profile-item {
  padding: 6px 8px;
  border-radius: 7px;
  background: var(--af-surface-2);
  border: 1px solid var(--af-border);
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.sb-profile-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 4px;
}

.sb-profile-id {
  font-size: 11.5px;
  font-weight: 600;
}

.sb-ok-badge {
  font-size: 10px;
  padding: 1px 5px;
  border-radius: 4px;
  background: var(--af-ok-soft);
  color: var(--af-ok);
}

.sb-profile-meta {
  font-size: 11px;
  color: var(--af-muted);
}

.sb-explain-box {
  font-size: 11.5px;
  padding: 8px;
  border-radius: 7px;
  background: var(--af-surface-2);
  border: 1px solid var(--af-accent-border);
  color: var(--af-fg-secondary);
}

.sb-error {
  margin-bottom: 4px;
}

.sb-add-model-btn {
  width: 24px;
  height: 24px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--af-border-strong);
  background: var(--af-btn-ghost-bg);
  color: var(--af-fg);
  padding: 0;
  border-radius: 6px;
  cursor: pointer;
}

.sb-add-model-btn:hover {
  background: var(--af-btn-ghost-hover);
}

.sb-preset-form {
  display: flex;
  flex-direction: column;
  gap: 7px;
  padding: 8px;
  border-radius: 8px;
  border: 1px solid var(--af-accent-border);
  background: var(--af-surface-2);
}

.sb-preset-list {
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.sb-preset-item {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 6px 8px;
  border-radius: 7px;
  border: 1px solid var(--af-border);
  background: var(--af-surface-2);
}

.sb-preset-info {
  flex: 1;
  min-width: 0;
}

.sb-preset-name {
  font-size: 11.5px;
  font-weight: 500;
  color: var(--af-fg);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sb-preset-meta {
  font-size: 10px;
  color: var(--af-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sb-del-preset-btn {
  width: 20px;
  height: 20px;
  border: none;
  border-radius: 4px;
  background: transparent;
  color: var(--af-muted);
  font-size: 14px;
  line-height: 1;
  cursor: pointer;
  flex-shrink: 0;
}

.sb-del-preset-btn:hover {
  background: var(--af-err-soft);
  color: var(--af-err);
}

.sb-role-select-wrap {
  padding: 0 8px 8px;
}

.sb-role-select {
  width: 100%;
  height: 28px;
  padding: 0 7px;
  border-radius: 6px;
  border: 1px solid var(--af-border-strong);
  background: var(--af-surface-2);
  color: var(--af-fg);
  font-size: 11px;
  outline: none;
  cursor: pointer;
}

.sb-role-select:focus {
  border-color: var(--af-accent);
}

.sb-role-select:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.sb-role-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.sb-role-card {
  border-radius: 8px;
  border: 1px solid var(--af-border);
  background: var(--af-surface);
  overflow: hidden;
}

.sb-role-card.editing {
  border-color: var(--af-accent-border);
}

.sb-role-summary {
  width: 100%;
  border: none;
  background: transparent;
  padding: 7px 9px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  cursor: pointer;
  text-align: left;
  color: var(--af-fg);
}

.sb-role-summary:hover {
  background: var(--af-surface-hover);
}

.sb-role-info {
  min-width: 0;
  flex: 1;
}

.sb-role-title-row {
  display: flex;
  align-items: center;
  gap: 6px;
}

.sb-role-label {
  font-size: 12px;
  font-weight: 500;
}

.sb-role-badge {
  font-size: 10px;
  padding: 0 5px;
  border-radius: 4px;
}

.badge-on {
  background: var(--af-ok-soft);
  color: var(--af-ok);
}

.badge-off {
  background: var(--af-surface-2);
  color: var(--af-muted);
}

.sb-role-sub {
  font-size: 10.5px;
  color: var(--af-muted);
  margin-top: 2px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sb-key-dot {
  width: 7px;
  height: 7px;
  border-radius: 99px;
  flex-shrink: 0;
}

.dot-ok {
  background: var(--af-ok);
}

.dot-warn {
  background: var(--af-warn);
}

.sb-role-editor {
  padding: 8px 9px 10px;
  border-top: 1px solid var(--af-border);
  background: var(--af-surface-2);
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.sb-field {
  display: flex;
  flex-direction: column;
  gap: 3px;
  font-size: 11px;
  color: var(--af-muted);
}

.sb-input {
  height: 28px;
  padding: 0 8px;
  border-radius: 6px;
  border: 1px solid var(--af-border-strong);
  background: var(--af-surface);
  color: var(--af-fg);
  font-size: 11.5px;
  outline: none;
  width: 100%;
}

.sb-input:focus {
  border-color: var(--af-accent);
}

.sb-field-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px;
}

.sb-check-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.sb-check {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 11.5px;
  color: var(--af-fg-secondary);
  cursor: pointer;
}

.sb-editor-actions {
  display: flex;
  justify-content: flex-end;
  gap: 6px;
  padding-top: 4px;
}

.sb-btn-ghost,
.sb-btn-primary {
  height: 26px;
  padding: 0 10px;
  border-radius: 6px;
  font-size: 11.5px;
  font-weight: 500;
  cursor: pointer;
}

.sb-btn-ghost {
  border: 1px solid var(--af-border-strong);
  background: transparent;
  color: var(--af-fg-secondary);
}

.sb-btn-primary {
  border: 1px solid var(--af-btn-primary-bg);
  background: var(--af-btn-primary-bg);
  color: var(--af-btn-primary-fg);
}

.sb-btn-primary:hover:not(:disabled) {
  background: var(--af-btn-primary-hover);
}

.sb-btn-primary:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.sb-security-note {
  padding-top: 2px;
}

.sb-footer {
  height: 34px;
  flex-shrink: 0;
  border-top: 1px solid var(--af-border);
  padding: 0 12px;
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: var(--af-muted);
}

.sb-footer-dot {
  width: 6px;
  height: 6px;
  border-radius: 99px;
  background: var(--af-ok);
}

.sb-footer-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
