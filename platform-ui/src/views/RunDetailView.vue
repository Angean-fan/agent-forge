<script setup lang="ts">
import { computed, inject, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRouter } from 'vue-router';

import { projectsApi, runsApi } from '@/api/client';
import type { FactoryRunView, RunDetail, RunEvent, SoftwareSpecification } from '@/api/types';
import DesignedApisPanel from '@/components/DesignedApisPanel.vue';
import FactoryFlowchart from '@/components/FactoryFlowchart.vue';
import RunEventsPanel from '@/components/RunEventsPanel.vue';
import RunVerificationPanel from '@/components/RunVerificationPanel.vue';
import StandardAssemblyTodo from '@/components/StandardAssemblyTodo.vue';
import { clearSession } from '@/stores/auth';
import {
  INSPECTOR_HOST_KEY,
  setActiveHeaderContext,
  setProjectRoutingSnapshot,
  setRightPanelOpen,
} from '@/stores/workspace';
import { describeError } from '@/utils/errors';
import { formatDateTime, formatMicroYuan } from '@/utils/format';

const props = defineProps<{ id: string }>();
const router = useRouter();
const inspectorHostReady = inject(INSPECTOR_HOST_KEY, ref(false));

const POLL_MS = 2000;
const HIDDEN_POLL_MS = 10000;
const MAX_BACKOFF_MS = 30000;
const TERMINAL = new Set(['SUCCEEDED', 'FAILED', 'CANCELLED']);

const STATUS_LABELS: Record<string, string> = {
  QUEUED: '排队中',
  PLANNING: '规划与资产解析',
  GENERATING: '装配与生成',
  VERIFYING: '构建与验收',
  SUCCEEDED: '已完成',
  FAILED: '失败',
  CANCELLED: '已取消',
  PAUSED_BUDGET: '预算不足已暂停',
};

const STAGE_LABELS: Record<string, string> = {
  load_confirmed_spec: '载入已确认规格',
  resolve_assets: '解析能力包与资产',
  assemble: '装配源码',
  verify: '构建与验收',
};

const runId = computed(() => Number(props.id));

const run = ref<RunDetail | null>(null);
const factoryData = ref<FactoryRunView | null>(null);
const projectSpec = ref<SoftwareSpecification | null>(null);
const events = ref<RunEvent[]>([]);
const lastSequence = ref(0);
const loading = ref(true);
const errorMessage = ref('');
const errorsInARow = ref(0);

// 与 ProjectDetailView 100% 统一：右侧边栏始终固定为「设计接口」「需求规格」「验收交付」「事件流」
const rightTab = ref<'apis' | 'spec' | 'verify' | 'events'>('verify');
const selectedNode = ref<string | null>(null);
const selectedModule = ref<string | null>(null);

let timer: ReturnType<typeof setTimeout> | null = null;
let stopped = false;
let specFetchedForProject: number | null = null;

const isTerminal = computed(() => run.value !== null && TERMINAL.has(run.value.status));
const statusLabel = computed(() =>
  run.value ? (STATUS_LABELS[run.value.status] ?? run.value.status) : '',
);
const statusTagType = computed(() => {
  switch (run.value?.status) {
    case 'SUCCEEDED':
      return 'success';
    case 'FAILED':
      return 'danger';
    case 'CANCELLED':
      return 'info';
    default:
      return 'primary';
  }
});
const phaseLabel = computed(() => {
  const phase = run.value?.phase;
  if (!phase) return statusLabel.value;
  return STAGE_LABELS[phase] ?? phase;
});
const failedChecks = computed(
  () => run.value?.verification?.checks?.filter((check) => check.status !== 'PASS') ?? [],
);
const canCancel = computed(
  () => run.value !== null && !isTerminal.value && !run.value.cancellation?.requested,
);
const cancelNotice = computed(() => run.value?.cancellation?.notice ?? null);

const canceling = ref(false);
const resuming = ref(false);
const resumeTopupYuan = ref(5);
const canResumeBudget = computed(
  () => run.value?.status === 'PAUSED_BUDGET' && typeof runsApi.resume === 'function',
);

function syncHeader(): void {
  if (!run.value) return;
  const tone =
    run.value.status === 'SUCCEEDED'
      ? 'ok'
      : run.value.status === 'FAILED'
        ? 'err'
        : run.value.status === 'PAUSED_BUDGET'
          ? 'warn'
          : 'accent';
  setActiveHeaderContext({
    projectId: run.value.project_id,
    projectName: `运行 #${run.value.id}`,
    stageLabel: statusLabel.value,
    stageTone: tone,
    cost: run.value.cost,
  });
}

async function maybeLoadProjectSpec(projectId: number): Promise<void> {
  if (specFetchedForProject === projectId || typeof projectsApi?.getSpec !== 'function') {
    return;
  }
  specFetchedForProject = projectId;
  try {
    const view = await projectsApi.getSpec(projectId);
    projectSpec.value = view.spec;
  } catch {
    // 缺少规格接口 mock 或无权访问时不阻塞主流程
  }
}

async function cancelRun(): Promise<void> {
  if (!run.value || canceling.value) return;
  canceling.value = true;
  try {
    const updated = await runsApi.cancel(runId.value);
    run.value = { ...run.value, status: updated.status };
    syncHeader();
  } catch (error) {
    errorMessage.value = describeError(error).message;
  } finally {
    canceling.value = false;
  }
}

async function resumeBudgetRun(): Promise<void> {
  if (!run.value || resuming.value || typeof runsApi.resume !== 'function') return;
  const addMicro = Math.max(1, Math.round(Number(resumeTopupYuan.value || 0) * 1_000_000));
  resuming.value = true;
  errorMessage.value = '';
  try {
    const updated = await runsApi.resume(runId.value, addMicro, 'ui_budget_topup');
    run.value = { ...run.value, status: updated.status };
    syncHeader();
    if (stopped) {
      stopped = false;
      void poll();
    }
  } catch (error) {
    errorMessage.value = describeError(error).message;
  } finally {
    resuming.value = false;
  }
}

function stopPolling(): void {
  stopped = true;
  if (timer !== null) {
    clearTimeout(timer);
    timer = null;
  }
}

function nextDelay(): number {
  if (document.hidden) return HIDDEN_POLL_MS;
  if (errorsInARow.value === 0) return POLL_MS;
  return Math.min(POLL_MS * 2 ** errorsInARow.value, MAX_BACKOFF_MS);
}

function schedule(): void {
  if (stopped) return;
  timer = setTimeout(() => {
    void poll();
  }, nextDelay());
}

async function poll(): Promise<void> {
  if (stopped) return;
  try {
    // 并发请求 get / events / factory，消除 3 次串行网络等待
    const [detail, eventsRes, factoryRes] = await Promise.all([
      runsApi.get(runId.value),
      runsApi.events(runId.value, lastSequence.value).catch(() => null),
      typeof runsApi.factory === 'function' ? runsApi.factory(runId.value).catch(() => null) : Promise.resolve(null),
    ]);
    run.value = detail;
    syncHeader();
    void maybeLoadProjectSpec(detail.project_id);
    if (eventsRes && eventsRes.items.length > 0) {
      events.value.push(...eventsRes.items);
      lastSequence.value = eventsRes.items[eventsRes.items.length - 1].sequence;
    }
    if (factoryRes) {
      factoryData.value = factoryRes;
      if (factoryRes.model_routing_snapshot?.length) {
        setProjectRoutingSnapshot(detail.project_id, detail.id, factoryRes.model_routing_snapshot);
      }
    }
    errorMessage.value = '';
    errorsInARow.value = 0;
    loading.value = false;
    if (isTerminal.value) {
      stopPolling();
      return;
    }
  } catch (error) {
    const info = describeError(error);
    if (info.unauthorized) {
      clearSession();
      stopPolling();
      await router.push({ name: 'login' });
      return;
    }
    errorsInARow.value += 1;
    errorMessage.value = info.message;
    loading.value = false;
  }
  schedule();
}

function onVisibilityChange(): void {
  if (!document.hidden && !stopped) {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
    void poll();
  }
}

function restart(): void {
  stopPolling();
  stopped = false;
  events.value = [];
  lastSequence.value = 0;
  run.value = null;
  factoryData.value = null;
  projectSpec.value = null;
  specFetchedForProject = null;
  loading.value = true;
  errorMessage.value = '';
  errorsInARow.value = 0;
  void poll();
}

function onFlowNodeSelect(nodeId: string, moduleHint?: string): void {
  selectedNode.value = nodeId;
  selectedModule.value = moduleHint ?? null;
  if (moduleHint) {
    const hasOpenDefect = (factoryData.value?.open_defects ?? []).some(
      (d) => d.dept_id === moduleHint,
    );
    rightTab.value = hasOpenDefect ? 'verify' : 'apis';
  } else if (
    nodeId === 'blueprint' ||
    nodeId === 'spec' ||
    nodeId === 'load_confirmed_spec' ||
    nodeId === 'resolve_assets'
  ) {
    rightTab.value = 'apis';
  } else {
    rightTab.value = 'verify';
  }
  setRightPanelOpen(true);
}

onMounted(() => {
  document.addEventListener('visibilitychange', onVisibilityChange);
  void poll();
});

onUnmounted(() => {
  document.removeEventListener('visibilitychange', onVisibilityChange);
  stopPolling();
});

watch(() => props.id, restart);
</script>

<template>
  <section v-loading="loading" class="rd-shell">
    <!-- Fixed Top Sub-header (above floating TodoList so buttons are never covered) -->
    <div class="run-head">
      <div class="run-head-left">
        <div class="run-title-row">
          <h1 class="stage-title af-mono">运行 #{{ runId }}</h1>
          <el-tag
            :type="statusTagType"
            size="small"
            disable-transitions
            data-testid="run-status"
          >
            {{ statusLabel || '加载中' }}
          </el-tag>
          <span class="run-sub">
            <span>{{ phaseLabel || '—' }}</span>
            <span v-if="run" class="af-mono">· {{ formatDateTime(run.updated_at) }}</span>
          </span>
        </div>
      </div>

      <div class="run-actions">
        <StandardAssemblyTodo
          v-if="run"
          :run="run"
          :factory-data="factoryData"
          :selected-node="selectedNode"
          @select-node="onFlowNodeSelect"
        />
        <el-button
          v-if="canCancel"
          size="small"
          type="danger"
          plain
          :loading="canceling"
          data-testid="cancel-run"
          @click="cancelRun"
        >
          取消运行
        </el-button>
        <el-button
          v-if="run"
          size="small"
          @click="router.push({ name: 'project-detail', params: { id: String(run.project_id) } })"
        >
          返回项目
        </el-button>
      </div>
    </div>

    <!-- Scrollable Live Flowchart Body -->
    <div class="rd-scroll af-scroll">
        <div class="rd-stage">
          <el-alert
            v-if="errorMessage"
            :title="errorMessage"
            type="warning"
            :closable="false"
            show-icon
          />

          <template v-if="run">
            <el-alert
              v-if="run.status === 'CANCELLED' || run.cancellation?.requested"
              type="info"
              :closable="false"
              show-icon
              data-testid="cancel-notice"
            >
              <template #title>
                {{ run.status === 'CANCELLED' ? '运行已取消' : '已请求取消（在途工作将尽快停止）' }}
              </template>
              <div v-if="cancelNotice">{{ cancelNotice }}</div>
            </el-alert>

            <el-alert
              v-if="run.status === 'PAUSED_BUDGET'"
              type="warning"
              :closable="false"
              show-icon
              data-testid="budget-paused-alert"
            >
              <template #title>预算不足已暂停（已完成的部门/阶段检查点已保留，加预算后可断点续跑）</template>
              <div v-if="canResumeBudget" class="resume-bar">
                <span>追加预算（元）：</span>
                <el-input-number
                  v-model="resumeTopupYuan"
                  :min="1"
                  :max="500"
                  :step="1"
                  size="small"
                  data-testid="resume-topup-input"
                />
                <el-button
                  type="primary"
                  size="small"
                  :loading="resuming"
                  data-testid="resume-run"
                  @click="resumeBudgetRun"
                >
                  增加预算并恢复运行
                </el-button>
              </div>
            </el-alert>

            <el-alert
              v-if="run.status === 'FAILED'"
              type="error"
              :closable="false"
              show-icon
              data-testid="failure-alert"
            >
              <template #title>运行失败：{{ run.error_code ?? '未知错误' }}</template>
              <div v-if="failedChecks.length" class="fail-list">
                未通过项：
                <el-tag
                  v-for="check in failedChecks"
                  :key="check.name"
                  :type="check.status === 'NOT_RUN' ? 'info' : 'danger'"
                  size="small"
                  disable-transitions
                >
                  {{ check.name }}（{{ check.status }}）
                </el-tag>
              </div>
            </el-alert>

            <!-- Compact Progress Metrics Strip -->
            <div class="metrics-strip">
              <div class="metric-tile">
                <span class="metric-label">已完成任务</span>
                <span class="metric-value af-mono" data-testid="task-count">{{ run.tasks.done }} / {{ run.tasks.total }}</span>
              </div>
              <div class="metric-tile">
                <span class="metric-label">已结算成本</span>
                <span class="metric-value af-mono">{{ formatMicroYuan(run.cost.settled_micro) }}</span>
              </div>
              <div class="metric-tile">
                <span class="metric-label">预留成本</span>
                <span class="metric-value af-mono">{{ formatMicroYuan(run.cost.reserved_micro) }}</span>
              </div>
            </div>
            <p v-if="run.cost.unknown_calls > 0" class="unknown-note">
              有 {{ run.cost.unknown_calls }} 次调用缺少核定价目，成本记为 UNKNOWN（保留预留）。
            </p>

            <!-- MAIN CONTENT: LIVE RUNTIME FLOWCHART -->
            <FactoryFlowchart
              :run="run"
              :factory-data="factoryData"
              :events="events"
              :selected-node="selectedNode"
              @select-node="onFlowNodeSelect"
            />
          </template>
        </div>
      </div>

    <!-- ================= RIGHT SIDEBAR INSPECTOR (Fixed Top Tab Bar + Scrollable Body) ================= -->
    <Teleport v-if="run" to="#workbench-inspector-host" :disabled="!inspectorHostReady">
      <div class="inspector-root" data-testid="run-inspector">
        <div class="inspector-header" data-testid="inspector-header">
          <div class="inspector-tabs" data-testid="inspector-tabs">
            <button
              type="button"
              class="ins-tab"
              :class="{ active: rightTab === 'apis' }"
              data-testid="inspector-tab-apis"
              @click="rightTab = 'apis'"
            >
              设计接口
            </button>
            <button
              type="button"
              class="ins-tab"
              :class="{ active: rightTab === 'spec' }"
              data-testid="inspector-tab-spec"
              @click="rightTab = 'spec'"
            >
              需求规格
            </button>
            <button
              type="button"
              class="ins-tab"
              :class="{ active: rightTab === 'verify' }"
              data-testid="inspector-tab-verify"
              @click="rightTab = 'verify'"
            >
              验收交付
            </button>
            <button
              type="button"
              class="ins-tab"
              :class="{ active: rightTab === 'events' }"
              data-testid="inspector-tab-events"
              @click="rightTab = 'events'"
            >
              事件流 ({{ events.length }})
            </button>
          </div>
        </div>

        <div class="inspector-body af-scroll" data-testid="inspector-body">
        <!-- RIGHT TAB 1: DESIGNED APIs & BLUEPRINT CONTRACTS -->
        <div v-show="rightTab === 'apis'" class="ins-pane">
          <DesignedApisPanel
            :spec="projectSpec"
            :blueprints="factoryData?.blueprints ?? []"
            :selected-module="selectedModule"
          />
        </div>

        <!-- RIGHT TAB 2: READ-ONLY SPEC SNAPSHOT -->
        <div v-show="rightTab === 'spec'" class="ins-pane">
          <div v-if="!projectSpec" class="ins-card ins-muted">
            暂无规格快照，返回项目可查看或编辑完整需求规格。
          </div>
          <template v-else>
            <div class="ins-card">
              <h3 class="section-title">摘要 / 目标</h3>
              <p class="spec-summary-text">{{ projectSpec.goal || '—' }}</p>
            </div>
            <div class="ins-card">
              <h3 class="section-title">业务实体 ({{ projectSpec.entities.length }})</h3>
              <div class="spec-chips">
                <span v-for="ent in projectSpec.entities" :key="ent.name" class="spec-chip af-mono">
                  {{ ent.name }} ({{ ent.fields.length }})
                </span>
              </div>
            </div>
            <div v-if="projectSpec.business_rules.length" class="ins-card">
              <h3 class="section-title">业务规则 ({{ projectSpec.business_rules.length }})</h3>
              <ul class="spec-rule-list">
                <li v-for="r in projectSpec.business_rules" :key="r.id">
                  <span class="af-mono">{{ r.id }}:</span> {{ r.text }}
                </li>
              </ul>
            </div>
          </template>
        </div>

        <!-- RIGHT TAB 3: VERIFICATION & DELIVERY (Unified component) -->
        <div v-show="rightTab === 'verify'" class="ins-pane">
          <RunVerificationPanel :run="run" :factory-data="factoryData" />
        </div>

        <!-- RIGHT TAB 4: INCREMENTAL RUN EVENTS -->
        <div v-show="rightTab === 'events'" class="ins-pane">
          <RunEventsPanel :events="events" :run-id="run.id" />
        </div>

        <!-- Standalone mount fallback when left sidebar is absent (unit test compatibility) -->
        <div
          v-if="!inspectorHostReady && factoryData && factoryData.model_routing_snapshot.length"
          class="ins-card"
          data-testid="factory-routing-snapshot"
        >
          <h3 class="section-title">自定义模型路由脱敏快照</h3>
          <el-table :data="factoryData.model_routing_snapshot" size="small" border>
            <el-table-column prop="role" label="角色" width="110" />
            <el-table-column prop="model" label="模型" />
            <el-table-column prop="base_url_host" label="域名" />
            <el-table-column prop="api_key_env" label="环境变量" />
            <el-table-column label="凭据" width="68">
              <template #default="{ row }">
                <el-tag :type="row.api_key_present ? 'success' : 'info'" size="small" disable-transitions>
                  {{ row.api_key_present ? '已注入' : '未注入' }}
                </el-tag>
              </template>
            </el-table-column>
          </el-table>
        </div>
        </div>
      </div>
    </Teleport>
  </section>
</template>

<style scoped>
.rd-shell {
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--af-bg);
}

.run-head {
  height: 42px;
  flex-shrink: 0;
  padding: 0 18px;
  border-bottom: 1px solid var(--af-border);
  background: var(--af-surface);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.run-head-left {
  display: flex;
  align-items: center;
  min-width: 0;
}

.run-title-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: nowrap;
  min-width: 0;
}

.stage-title {
  margin: 0;
  font-size: 13.5px;
  font-weight: 600;
  flex-shrink: 0;
}

.run-sub {
  font-size: 11.5px;
  color: var(--af-muted);
  display: flex;
  gap: 6px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.run-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.rd-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 18px 22px;
}

.rd-stage {
  display: flex;
  flex-direction: column;
  gap: 14px;
  max-width: 920px;
  margin: 0 auto;
}

.resume-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
  flex-wrap: wrap;
}

.fail-list {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  margin-top: 6px;
}

.metrics-strip {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px;
}

.metric-tile {
  border-radius: 10px;
  border: 1px solid var(--af-border-strong);
  background: var(--af-surface);
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.metric-label {
  font-size: 11px;
  color: var(--af-muted);
}

.metric-value {
  font-size: 16px;
  font-weight: 600;
}

.unknown-note {
  margin: 4px 0 0;
  font-size: 11.5px;
  color: var(--af-warn);
  line-height: 1.45;
}

/* Right Inspector (Fixed Top Tab Bar + Independent Scroll Body) */
.inspector-root {
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.inspector-header {
  height: 42px;
  flex-shrink: 0;
  padding: 0 10px;
  border-bottom: 1px solid var(--af-border);
  background: var(--af-surface);
  display: flex;
  align-items: center;
}

.inspector-tabs {
  width: 100%;
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 2px;
  padding: 2px;
  border-radius: 7px;
  background: var(--af-bg);
  border: 1px solid var(--af-border-strong);
}

.ins-tab {
  border: none;
  background: transparent;
  color: var(--af-muted);
  font-size: 11px;
  font-weight: 500;
  padding: 4px 4px;
  border-radius: 5px;
  cursor: pointer;
  white-space: nowrap;
}

.ins-tab.active {
  background: var(--af-surface-2);
  color: var(--af-fg);
}

.inspector-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.ins-pane {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.ins-card {
  border-radius: 10px;
  border: 1px solid var(--af-border-strong);
  background: var(--af-surface);
  padding: 11px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.ins-muted {
  font-size: 12px;
  color: var(--af-muted);
}

.spec-summary-text {
  margin: 0;
  font-size: 12px;
  line-height: 1.5;
  color: var(--af-fg-secondary);
}

.spec-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.spec-chip {
  font-size: 11px;
  padding: 2px 7px;
  border-radius: 6px;
  border: 1px solid var(--af-border);
  background: var(--af-surface-2);
  color: var(--af-fg-secondary);
}

.spec-rule-list {
  margin: 0;
  padding-left: 16px;
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--af-fg-secondary);
}
</style>
