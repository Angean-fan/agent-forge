<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRouter } from 'vue-router';

import { runsApi } from '@/api/client';
import type { FactoryDepartmentLane, FactoryRunView, RunDetail, RunEvent } from '@/api/types';
import SafeText from '@/components/SafeText.vue';
import { clearSession } from '@/stores/auth';
import { describeError } from '@/utils/errors';
import { formatDateTime, formatMicroYuan } from '@/utils/format';

const props = defineProps<{ id: string }>();
const router = useRouter();

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

// E04：代码交付与外部服务就绪分开呈现。
const CODE_DELIVERY_LABELS: Record<string, string> = {
  PASS: '代码交付通过',
  FAIL: '代码交付失败',
  NOT_RUN: '代码交付未运行',
};

const READINESS_LABELS: Record<string, string> = {
  NOT_REQUIRED: '无外部服务',
  MOCK_ONLY: '仅 Mock 已验证',
  AWAITING_USER_CONFIG: '待配置 Live 凭据',
  LIVE_VERIFIED: 'Live 已验证',
  LIVE_FAILED: 'Live 验证失败',
};

function checkTagType(status: string): 'success' | 'danger' | 'info' {
  if (status === 'PASS') {
    return 'success';
  }
  if (status === 'FAIL') {
    return 'danger';
  }
  return 'info';
}

function readinessTagType(readiness: string): 'success' | 'danger' | 'warning' | 'info' {
  if (readiness === 'LIVE_VERIFIED') {
    return 'success';
  }
  if (readiness === 'LIVE_FAILED') {
    return 'danger';
  }
  if (readiness === 'AWAITING_USER_CONFIG') {
    return 'warning';
  }
  return 'info';
}

const DEPT_STATUS_LABELS: Record<string, string> = {
  QUEUED: '排队中',
  PLANNING: '规划中',
  CODING: '编码中',
  TESTING: '测试中',
  REWORK: '返工中',
  PASSED: '已通过',
  FAILED: '失败',
  CANCELLED: '已取消',
};

const FACTORY_PHASE_LABELS: Record<string, string> = {
  backend: '后端并行阶段',
  frontend: '前端并行阶段',
  idle: '等待调度',
};

const DEPT_PIPELINE_STEPS = [
  { key: 'PLANNING', label: '规划' },
  { key: 'CODING', label: '编码' },
  { key: 'TESTING', label: '测试' },
  { key: 'RESULT', label: '通过/返工' },
] as const;

function deptStatusTagType(status: string): 'success' | 'danger' | 'warning' | 'info' | 'primary' {
  switch (status) {
    case 'PASSED':
      return 'success';
    case 'FAILED':
      return 'danger';
    case 'REWORK':
      return 'warning';
    case 'QUEUED':
    case 'CANCELLED':
      return 'info';
    default:
      return 'primary';
  }
}

function deptStepState(lane: FactoryDepartmentLane, stepKey: string): 'done' | 'active' | 'warn' | 'idle' {
  const order = ['PLANNING', 'CODING', 'TESTING'];
  if (stepKey === 'RESULT') {
    if (lane.status === 'PASSED') return 'done';
    if (lane.status === 'REWORK' || lane.status === 'FAILED') return 'warn';
    return 'idle';
  }
  const currentIdx = order.indexOf(lane.status);
  const stepIdx = order.indexOf(stepKey);
  if (lane.status === 'PASSED') return 'done';
  if (lane.status === 'REWORK') return stepIdx === 0 ? 'warn' : 'idle';
  if (currentIdx === -1) return 'idle';
  if (stepIdx < currentIdx) return 'done';
  if (stepIdx === currentIdx) return 'active';
  return 'idle';
}

function formatDurationMs(ms: number | null): string {
  if (ms === null || ms < 0) {
    return '—';
  }
  if (ms < 1000) {
    return `${ms} ms`;
  }
  return `${(ms / 1000).toFixed(1)} s`;
}

const runId = computed(() => Number(props.id));

const run = ref<RunDetail | null>(null);
const factoryData = ref<FactoryRunView | null>(null);
const events = ref<RunEvent[]>([]);
const lastSequence = ref(0);
const loading = ref(true);
const errorMessage = ref('');
const errorsInARow = ref(0);

let timer: ReturnType<typeof setTimeout> | null = null;
let stopped = false;

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
  if (!phase) {
    return statusLabel.value;
  }
  return STAGE_LABELS[phase] ?? phase;
});
const zipArtifact = computed(() => run.value?.artifacts.find((item) => item.kind === 'zip') ?? null);
const failedChecks = computed(
  () => run.value?.verification?.checks?.filter((check) => check.status !== 'PASS') ?? [],
);
const notRunChecks = computed(
  () => run.value?.verification?.checks?.filter((check) => check.status === 'NOT_RUN') ?? [],
);
// R22：仅在非终态且尚未请求取消时可点；取消不撤回已处理调用，成本如实保留。
const canCancel = computed(
  () => run.value !== null && !isTerminal.value && !run.value.cancellation?.requested,
);
const cancelNotice = computed(() => run.value?.cancellation?.notice ?? null);
const externalServices = computed(() => run.value?.external_services ?? []);
const codeDeliveryLabel = computed(() =>
  run.value ? (CODE_DELIVERY_LABELS[run.value.code_delivery_status] ?? run.value.code_delivery_status) : '',
);
const readinessLabel = computed(() =>
  run.value ? (READINESS_LABELS[run.value.external_readiness] ?? run.value.external_readiness) : '',
);
const canceling = ref(false);
const resuming = ref(false);
const resumeTopupYuan = ref(5);
const canResumeBudget = computed(
  () => run.value?.status === 'PAUSED_BUDGET' && typeof runsApi.resume === 'function',
);

async function cancelRun(): Promise<void> {
  if (!run.value || canceling.value) {
    return;
  }
  canceling.value = true;
  try {
    const updated = await runsApi.cancel(runId.value);
    run.value = { ...run.value, status: updated.status };
  } catch (error) {
    errorMessage.value = describeError(error).message;
  } finally {
    canceling.value = false;
  }
}

async function resumeBudgetRun(): Promise<void> {
  if (!run.value || resuming.value || typeof runsApi.resume !== 'function') {
    return;
  }
  const addMicro = Math.max(1, Math.round(Number(resumeTopupYuan.value || 0) * 1_000_000));
  resuming.value = true;
  errorMessage.value = '';
  try {
    const updated = await runsApi.resume(runId.value, addMicro, 'ui_budget_topup');
    run.value = { ...run.value, status: updated.status };
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
  if (document.hidden) {
    return HIDDEN_POLL_MS;
  }
  if (errorsInARow.value === 0) {
    return POLL_MS;
  }
  return Math.min(POLL_MS * 2 ** errorsInARow.value, MAX_BACKOFF_MS);
}

function schedule(): void {
  if (stopped) {
    return;
  }
  timer = setTimeout(() => {
    void poll();
  }, nextDelay());
}

async function poll(): Promise<void> {
  if (stopped) {
    return;
  }
  try {
    const detail = await runsApi.get(runId.value);
    run.value = detail;
    const data = await runsApi.events(runId.value, lastSequence.value);
    if (data.items.length > 0) {
      events.value.push(...data.items);
      lastSequence.value = data.items[data.items.length - 1].sequence;
    }
    if (typeof runsApi.factory === 'function') {
      try {
        factoryData.value = await runsApi.factory(runId.value);
      } catch {
        // 标准装配运行或暂未启用工厂接口时不阻塞主详情轮询
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
  loading.value = true;
  errorMessage.value = '';
  errorsInARow.value = 0;
  void poll();
}

function eventSummary(event: RunEvent): string {
  const keys = Object.keys(event.payload);
  if (keys.length === 0) {
    return '—';
  }
  return JSON.stringify(event.payload);
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
  <section v-loading="loading">
    <div class="run-head">
      <div>
        <h1 class="page-title">运行 #{{ runId }}</h1>
        <p class="page-subtitle">
          <el-tag
            :type="statusTagType"
            size="small"
            disable-transitions
            data-testid="run-status"
          >
            {{ statusLabel || '加载中' }}
          </el-tag>
          <span class="phase">当前阶段：{{ phaseLabel || '—' }}</span>
          <span v-if="run" class="updated">更新于 {{ formatDateTime(run.updated_at) }}</span>
        </p>
      </div>
      <div class="run-actions">
        <el-button
          v-if="canCancel"
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
          @click="router.push({ name: 'project-detail', params: { id: String(run.project_id) } })"
        >
          返回项目
        </el-button>
      </div>
    </div>

    <el-alert
      v-if="errorMessage"
      :title="errorMessage"
      type="warning"
      :closable="false"
      show-icon
      class="block-gap"
    />

    <template v-if="run">
      <el-alert
        v-if="run.status === 'CANCELLED' || run.cancellation?.requested"
        type="info"
        :closable="false"
        show-icon
        class="block-gap"
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
        class="block-gap"
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
        class="block-gap"
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

      <el-card class="block-gap">
        <h2 class="section-title">进度概览</h2>
        <div class="metrics">
          <div class="metric">
            <span class="metric-label">已完成任务</span>
            <span class="metric-value" data-testid="task-count">{{ run.tasks.done }} / {{ run.tasks.total }}</span>
          </div>
          <div class="metric">
            <span class="metric-label">已结算成本</span>
            <span class="metric-value">{{ formatMicroYuan(run.cost.settled_micro) }}</span>
          </div>
          <div class="metric">
            <span class="metric-label">预留成本</span>
            <span class="metric-value">{{ formatMicroYuan(run.cost.reserved_micro) }}</span>
          </div>
        </div>
        <p v-if="run.cost.unknown_calls > 0" class="unknown-note">
          有 {{ run.cost.unknown_calls }} 次调用缺少核定价目，成本记为 UNKNOWN（保留预留）。
        </p>
      </el-card>

      <el-card
        v-if="factoryData && factoryData.factory_active"
        class="block-gap"
        data-testid="factory-swimlanes"
      >
        <div class="factory-head">
          <div>
            <h2 class="section-title">异步工厂多部门泳道视图</h2>
            <p class="factory-sub">
              当前阶段：
              <el-tag size="small" type="primary" disable-transitions data-testid="factory-phase">
                {{ FACTORY_PHASE_LABELS[factoryData.current_phase] ?? factoryData.current_phase }}
              </el-tag>
              <span class="factory-meta-item">
                后端总装大循环：第 {{ factoryData.integration_rounds.backend ?? 0 }} 轮
              </span>
              <span class="factory-meta-item">
                前端总装大循环：第 {{ factoryData.integration_rounds.frontend ?? 0 }} 轮
              </span>
            </p>
          </div>
          <div v-if="factoryData.blueprints.length" class="blueprint-pills">
            <el-tag
              v-for="bp in factoryData.blueprints"
              :key="`${bp.phase}-v${bp.version}`"
              size="small"
              :type="bp.status === 'APPROVED_FOR_BUILD' ? 'success' : 'info'"
              disable-transitions
            >
              {{ bp.phase === 'frontend' ? '前端蓝图' : '后端蓝图' }} v{{ bp.version }}（{{ bp.module_count }} 部门 · {{ bp.blueprint_hash.slice(0, 8) }}）
            </el-tag>
          </div>
        </div>

        <div v-if="factoryData.backend_departments.length" class="lane-section" data-testid="backend-swimlanes">
          <h3 class="lane-title">后端并行部门泳道（{{ factoryData.backend_departments.length }}）</h3>
          <div class="lane-grid">
            <div
              v-for="lane in factoryData.backend_departments"
              :key="`be-${lane.dept_id}`"
              class="dept-card"
              :data-testid="`dept-lane-backend-${lane.dept_id}`"
            >
              <div class="dept-card-head">
                <span class="dept-name">{{ lane.dept_id }}</span>
                <el-tag :type="deptStatusTagType(lane.status)" size="small" disable-transitions>
                  {{ DEPT_STATUS_LABELS[lane.status] ?? lane.status }}
                </el-tag>
              </div>
              <div class="step-track">
                <span
                  v-for="step in DEPT_PIPELINE_STEPS"
                  :key="step.key"
                  class="step-pill"
                  :class="`step-${deptStepState(lane, step.key)}`"
                >
                  {{ step.label }}
                </span>
              </div>
              <div class="dept-metrics">
                <span>轮次：{{ lane.current_round }} / {{ lane.max_rounds }}</span>
                <span>耗时：{{ formatDurationMs(lane.duration_ms) }}</span>
                <span>
                  测试：{{ lane.test_status ?? '未运行' }}
                  <template v-if="lane.test_exit_code !== null">（exit {{ lane.test_exit_code }}）</template>
                </span>
                <span>费用：{{ formatMicroYuan(lane.cost_micro) }}（{{ lane.input_tokens }}/{{ lane.output_tokens }} tok）</span>
              </div>
              <p v-if="lane.error_code" class="dept-error">{{ lane.error_code }}</p>
            </div>
          </div>
        </div>

        <div v-if="factoryData.frontend_departments.length" class="lane-section" data-testid="frontend-swimlanes">
          <h3 class="lane-title">前端并行部门泳道（{{ factoryData.frontend_departments.length }}）</h3>
          <div class="lane-grid">
            <div
              v-for="lane in factoryData.frontend_departments"
              :key="`fe-${lane.dept_id}`"
              class="dept-card"
              :data-testid="`dept-lane-frontend-${lane.dept_id}`"
            >
              <div class="dept-card-head">
                <span class="dept-name">{{ lane.dept_id }}</span>
                <el-tag :type="deptStatusTagType(lane.status)" size="small" disable-transitions>
                  {{ DEPT_STATUS_LABELS[lane.status] ?? lane.status }}
                </el-tag>
              </div>
              <div class="step-track">
                <span
                  v-for="step in DEPT_PIPELINE_STEPS"
                  :key="step.key"
                  class="step-pill"
                  :class="`step-${deptStepState(lane, step.key)}`"
                >
                  {{ step.label }}
                </span>
              </div>
              <div class="dept-metrics">
                <span>轮次：{{ lane.current_round }} / {{ lane.max_rounds }}</span>
                <span>耗时：{{ formatDurationMs(lane.duration_ms) }}</span>
                <span>
                  测试：{{ lane.test_status ?? '未运行' }}
                  <template v-if="lane.test_exit_code !== null">（exit {{ lane.test_exit_code }}）</template>
                </span>
                <span>费用：{{ formatMicroYuan(lane.cost_micro) }}（{{ lane.input_tokens }}/{{ lane.output_tokens }} tok）</span>
              </div>
              <p v-if="lane.error_code" class="dept-error">{{ lane.error_code }}</p>
            </div>
          </div>
        </div>

        <div
          v-if="factoryData.open_defects.length || factoryData.resolved_defects.length"
          class="lane-section"
          data-testid="factory-defects"
        >
          <h3 class="lane-title">
            缺陷单追踪（未解决 {{ factoryData.open_defects.length }} / 已解决 {{ factoryData.resolved_defects.length }}）
          </h3>
          <div v-if="factoryData.open_defects.length" class="defect-list" data-testid="open-defects">
            <div
              v-for="defect in factoryData.open_defects"
              :key="`open-${defect.id}`"
              class="defect-item defect-open"
            >
              <div class="defect-head">
                <el-tag type="danger" size="small" disable-transitions>未解决</el-tag>
                <span class="defect-meta">
                  [{{ defect.phase }} / {{ defect.dept_id }} / 第 {{ defect.round }} 轮 / {{ defect.scope }}]
                  {{ defect.error_class }}
                </span>
              </div>
              <SafeText :text="defect.summary" />
              <SafeText v-if="defect.log_excerpt" :text="defect.log_excerpt" class="defect-log" />
            </div>
          </div>
          <div v-if="factoryData.resolved_defects.length" class="defect-list" data-testid="resolved-defects">
            <div
              v-for="defect in factoryData.resolved_defects"
              :key="`res-${defect.id}`"
              class="defect-item defect-resolved"
            >
              <div class="defect-head">
                <el-tag type="success" size="small" disable-transitions>已解决</el-tag>
                <span class="defect-meta">
                  [{{ defect.phase }} / {{ defect.dept_id }} / 第 {{ defect.round }} 轮 / {{ defect.scope }}]
                  {{ defect.error_class }}
                </span>
              </div>
              <SafeText :text="defect.summary" />
              <SafeText v-if="defect.log_excerpt" :text="defect.log_excerpt" class="defect-log" />
            </div>
          </div>
        </div>

        <div
          v-if="factoryData.model_routing_snapshot.length"
          class="lane-section"
          data-testid="factory-routing-snapshot"
        >
          <h3 class="lane-title">自定义模型路由脱敏快照</h3>
          <el-table :data="factoryData.model_routing_snapshot" size="small" border>
            <el-table-column prop="role" label="角色" width="140" />
            <el-table-column prop="provider" label="提供方" width="140" />
            <el-table-column prop="model" label="模型" />
            <el-table-column prop="base_url_host" label="接口域名" />
            <el-table-column prop="api_key_env" label="密钥环境变量名" width="170" />
            <el-table-column label="凭据状态" width="100">
              <template #default="{ row }">
                <el-tag :type="row.api_key_present ? 'success' : 'info'" size="small" disable-transitions>
                  {{ row.api_key_present ? '已注入' : '未注入' }}
                </el-tag>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-card>

      <el-card class="block-gap" data-testid="external-readiness">
        <h2 class="section-title">代码交付与外部服务就绪</h2>
        <div class="metrics">
          <div class="metric">
            <span class="metric-label">代码交付</span>
            <el-tag
              :type="checkTagType(run.code_delivery_status)"
              size="small"
              disable-transitions
              data-testid="code-delivery-status"
            >
              {{ codeDeliveryLabel }}
            </el-tag>
          </div>
          <div class="metric">
            <span class="metric-label">外部服务就绪</span>
            <el-tag
              :type="readinessTagType(run.external_readiness)"
              size="small"
              disable-transitions
              data-testid="external-readiness-status"
            >
              {{ readinessLabel }}
            </el-tag>
          </div>
        </div>
        <p v-if="run.external_notice" class="unknown-note" data-testid="external-notice">
          {{ run.external_notice }}
        </p>
        <el-table
          v-if="externalServices.length"
          :data="externalServices"
          size="small"
          border
          class="external-table"
          data-testid="external-services"
        >
          <el-table-column prop="name" label="外部服务" />
          <el-table-column prop="mode" label="模式" width="110" />
          <el-table-column label="Mock" width="110">
            <template #default="{ row }">
              <el-tag :type="checkTagType(row.mock_status)" size="small" disable-transitions>
                {{ row.mock_status }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="Live" width="110">
            <template #default="{ row }">
              <el-tag :type="checkTagType(row.live_status)" size="small" disable-transitions>
                {{ row.live_status }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="配置" width="90">
            <template #default="{ row }">
              <el-tag :type="row.config_present ? 'success' : 'info'" size="small" disable-transitions>
                {{ row.config_present ? '已配置' : '待配置' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="reason" label="说明" />
        </el-table>
        <el-empty v-else description="无第三方外部服务，无须配置" :image-size="60" />
      </el-card>

      <el-card class="block-gap">
        <h2 class="section-title">交付产物</h2>
        <template v-if="zipArtifact">
          <div class="download-row">
            <a class="download-btn" :href="runsApi.artifactUrl(run.id)" data-testid="download-link">
              下载源码包（ZIP）
            </a>
            <el-tag :type="zipArtifact.verified ? 'success' : 'warning'" size="small" disable-transitions>
              {{ zipArtifact.verified ? '已验证' : '未验证' }}
            </el-tag>
            <span class="artifact-hash">sha256 {{ zipArtifact.sha256.slice(0, 12) }}</span>
          </div>
          <p v-if="!zipArtifact.verified" class="unknown-note">
            该交付包未通过全部必需检查，标注为“未验证”，请以 verification 报告为准。
          </p>
        </template>
        <el-empty v-else description="尚无可下载的产物（运行完成或失败后生成）" :image-size="60" />
      </el-card>

      <el-card v-if="run.verification" class="block-gap">
        <h2 class="section-title">验证报告（{{ run.verification.label ?? '—' }}）</h2>
        <el-table :data="run.verification.checks ?? []" size="small" border>
          <el-table-column prop="name" label="检查项" />
          <el-table-column label="状态" width="120">
            <template #default="{ row }">
              <el-tag
                :type="row.status === 'PASS' ? 'success' : row.status === 'NOT_RUN' ? 'info' : 'danger'"
                size="small"
                disable-transitions
              >
                {{ row.status }}
              </el-tag>
            </template>
          </el-table-column>
        </el-table>
        <p v-if="notRunChecks.length" class="unknown-note">
          {{ notRunChecks.length }} 项检查未运行（NOT_RUN）：{{ notRunChecks.map((c) => c.name).join('、') }}
        </p>
      </el-card>

      <el-card class="block-gap">
        <h2 class="section-title">运行事件</h2>
        <el-empty v-if="events.length === 0" description="暂无事件" :image-size="60" />
        <ul v-else class="event-list" data-testid="event-list">
          <li v-for="event in events" :key="event.sequence" class="event-item">
            <span class="event-seq">#{{ event.sequence }}</span>
            <el-tag size="small" disable-transitions>{{ event.type }}</el-tag>
            <span class="event-payload">{{ eventSummary(event) }}</span>
            <span class="event-time">{{ formatDateTime(event.created_at) }}</span>
          </li>
        </ul>
      </el-card>
    </template>
  </section>
</template>

<style scoped>
.run-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}
.run-actions {
  display: flex;
  gap: 8px;
}
.phase {
  margin-left: 12px;
}
.updated {
  margin-left: 12px;
  color: #c0c4cc;
}
.block-gap {
  margin-top: 16px;
}
.metrics {
  display: flex;
  gap: 40px;
  flex-wrap: wrap;
}
.metric {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.metric-label {
  color: #909399;
  font-size: 13px;
}
.metric-value {
  font-size: 20px;
  font-weight: 600;
}
.unknown-note {
  margin: 12px 0 0;
  color: #e6a23c;
  font-size: 13px;
}
.external-table {
  margin-top: 12px;
}
.fail-list {
  margin-top: 8px;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.resume-bar {
  margin-top: 8px;
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.download-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.download-btn {
  display: inline-block;
  padding: 8px 18px;
  background: var(--el-color-primary);
  color: #fff;
  border-radius: 4px;
  text-decoration: none;
  font-size: 14px;
}
.download-btn:hover {
  background: var(--el-color-primary-light-3);
}
.artifact-hash {
  color: #909399;
  font-size: 12px;
}
.event-list {
  list-style: none;
  margin: 0;
  padding: 0;
}
.event-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 6px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
  font-size: 13px;
}
.event-seq {
  color: #909399;
  min-width: 36px;
}
.event-payload {
  flex: 1;
  color: #606266;
  word-break: break-all;
}
.event-time {
  color: #c0c4cc;
  white-space: nowrap;
}
.factory-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.factory-sub {
  margin: 4px 0 0;
  font-size: 13px;
  color: #606266;
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.factory-meta-item {
  color: #606266;
}
.blueprint-pills {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.lane-section {
  margin-top: 16px;
}
.lane-title {
  margin: 0 0 10px;
  font-size: 14px;
  font-weight: 600;
  color: #303133;
}
.lane-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 12px;
}
.dept-card {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  padding: 10px 12px;
  background: #fafafa;
}
.dept-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.dept-name {
  font-weight: 600;
  font-size: 14px;
  color: #303133;
}
.step-track {
  display: flex;
  gap: 4px;
  margin-top: 8px;
  flex-wrap: wrap;
}
.step-pill {
  font-size: 11px;
  padding: 2px 6px;
  border-radius: 4px;
  border: 1px solid #dcdfe6;
  color: #909399;
  background: #fff;
}
.step-done {
  border-color: #b3e19d;
  color: #67c23a;
  background: #f0f9eb;
}
.step-active {
  border-color: #a0cfff;
  color: #409eff;
  background: #ecf5ff;
}
.step-warn {
  border-color: #f3d19e;
  color: #e6a23c;
  background: #fdf6ec;
}
.dept-metrics {
  margin-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 3px;
  font-size: 12px;
  color: #606266;
}
.dept-error {
  margin: 6px 0 0;
  font-size: 12px;
  color: #f56c6c;
}
.defect-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 8px;
}
.defect-item {
  border-radius: 6px;
  padding: 10px 12px;
  border: 1px solid var(--el-border-color-lighter);
  font-size: 13px;
}
.defect-open {
  background: #fef0f0;
  border-color: #fde2e2;
}
.defect-resolved {
  background: #f0f9eb;
  border-color: #e1f3d8;
}
.defect-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
  flex-wrap: wrap;
}
.defect-meta {
  font-weight: 600;
  font-size: 12px;
  color: #303133;
}
.defect-log {
  margin-top: 6px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  color: #606266;
}
</style>
