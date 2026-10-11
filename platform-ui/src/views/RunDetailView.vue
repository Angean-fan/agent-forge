<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRouter } from 'vue-router';

import { runsApi } from '@/api/client';
import type { RunDetail, RunEvent } from '@/api/types';
import { clearSession } from '@/stores/auth';
import { describeError } from '@/utils/errors';
import { formatDateTime, formatMicroYuan } from '@/utils/format';

const props = defineProps<{ id: string }>();
const router = useRouter();

const POLL_MS = 2000;
const HIDDEN_POLL_MS = 10000;
const MAX_BACKOFF_MS = 30000;
const TERMINAL = new Set(['SUCCEEDED', 'FAILED']);

const STATUS_LABELS: Record<string, string> = {
  QUEUED: '排队中',
  PLANNING: '规划与资产解析',
  GENERATING: '装配与生成',
  VERIFYING: '构建与验收',
  SUCCEEDED: '已完成',
  FAILED: '失败',
};

const STAGE_LABELS: Record<string, string> = {
  load_confirmed_spec: '载入已确认规格',
  resolve_assets: '解析能力包与资产',
  assemble: '装配源码',
  verify: '构建与验收',
};

const runId = computed(() => Number(props.id));

const run = ref<RunDetail | null>(null);
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
            :type="run && run.status === 'SUCCEEDED' ? 'success' : run && run.status === 'FAILED' ? 'danger' : 'primary'"
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
      <el-button
        v-if="run"
        @click="router.push({ name: 'project-detail', params: { id: String(run.project_id) } })"
      >
        返回项目
      </el-button>
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
.fail-list {
  margin-top: 8px;
  display: flex;
  align-items: center;
  gap: 8px;
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
</style>
