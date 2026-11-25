<script setup lang="ts">
import { computed } from 'vue';

import type {
  FactoryDepartmentLane,
  FactoryRunView,
  RunDetail,
  RunEvent,
  RunMode,
  SpecView,
} from '@/api/types';
import { formatDateTime, formatMicroYuan } from '@/utils/format';

const props = defineProps<{
  run?: RunDetail | null;
  factoryData?: FactoryRunView | null;
  events?: RunEvent[];
  spec?: SpecView | null;
  runMode?: RunMode;
  selectedNode?: string | null;
  previewOnly?: boolean;
}>();

const emit = defineEmits<{
  (e: 'select-node', nodeId: string, moduleHint?: string): void;
}>();

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

const STANDARD_STAGES = [
  { key: 'load_confirmed_spec', idx: 1, title: '载入已确认规格', short: '载入规格', desc: '锁定冻结版本与 SHA-256 哈希' },
  { key: 'resolve_assets', idx: 2, title: '解析能力包与资产', short: '解析资产', desc: '匹配已认证模板与集成契约' },
  { key: 'assemble', idx: 3, title: '装配前后端源码', short: '源码装配', desc: '按选定技术栈确定性生成代码树' },
  { key: 'verify', idx: 4, title: 'Docker Runner 构建验收', short: '构建验收', desc: '编译、测试与验收用例全量校验' },
  { key: 'package', idx: 5, title: '交付产物打包', short: '产物打包', desc: '产出 ZIP 源码包与验证报告' },
] as const;

const isFactoryView = computed(() => {
  if (props.factoryData?.factory_active) return true;
  if (!props.run && props.previewOnly && props.runMode === 'factory') return true;
  return false;
});

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

function deptStepState(
  lane: FactoryDepartmentLane,
  stepKey: string,
): 'done' | 'active' | 'warn' | 'idle' {
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
  if (ms === null || ms < 0) return '—';
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

type StageState = 'done' | 'active' | 'warn' | 'idle';

const factoryStageStates = computed<Record<string, StageState>>(() => {
  if (!props.run && props.previewOnly) {
    return {
      spec: 'done',
      beBlueprint: 'idle',
      beWorkshop: 'idle',
      beVerify: 'idle',
      feWorkshop: 'idle',
      e2e: 'idle',
      delivery: 'idle',
    };
  }
  const runStatus = props.run?.status ?? '';
  const fd = props.factoryData;
  const beDone =
    (fd?.backend_departments.length ?? 0) > 0 &&
    (fd?.backend_departments.every((l) => l.status === 'PASSED') ?? false);
  const feHas = (fd?.frontend_departments.length ?? 0) > 0;
  const feDone =
    feHas && (fd?.frontend_departments.every((l) => l.status === 'PASSED') ?? false);

  if (runStatus === 'SUCCEEDED') {
    return {
      spec: 'done',
      beBlueprint: 'done',
      beWorkshop: 'done',
      beVerify: 'done',
      feWorkshop: 'done',
      e2e: 'done',
      delivery: 'done',
    };
  }
  if (fd?.current_phase === 'frontend' || feHas) {
    const feWarn = fd?.frontend_departments.some(
      (l) => l.status === 'REWORK' || l.status === 'FAILED',
    );
    return {
      spec: 'done',
      beBlueprint: 'done',
      beWorkshop: 'done',
      beVerify: 'done',
      feWorkshop: feDone ? 'done' : feWarn ? 'warn' : 'active',
      e2e: feDone ? 'active' : 'idle',
      delivery: 'idle',
    };
  }
  if (fd?.current_phase === 'backend' || (fd?.backend_departments.length ?? 0) > 0) {
    const beWarn = fd?.backend_departments.some(
      (l) => l.status === 'REWORK' || l.status === 'FAILED',
    );
    return {
      spec: 'done',
      beBlueprint: 'done',
      beWorkshop: beDone ? 'done' : beWarn ? 'warn' : 'active',
      beVerify: beDone ? 'active' : 'idle',
      feWorkshop: 'idle',
      e2e: 'idle',
      delivery: 'idle',
    };
  }
  if (fd?.blueprints.length) {
    return {
      spec: 'done',
      beBlueprint: 'done',
      beWorkshop: 'active',
      beVerify: 'idle',
      feWorkshop: 'idle',
      e2e: 'idle',
      delivery: 'idle',
    };
  }
  return {
    spec: 'done',
    beBlueprint: runStatus === 'QUEUED' ? 'idle' : 'active',
    beWorkshop: 'idle',
    beVerify: 'idle',
    feWorkshop: 'idle',
    e2e: 'idle',
    delivery: 'idle',
  };
});

function standardStageState(stageKey: string): StageState {
  if (!props.run) {
    return stageKey === 'load_confirmed_spec' && props.spec?.confirmed ? 'done' : 'idle';
  }
  const status = props.run.status;
  if (status === 'SUCCEEDED') return 'done';

  // 若是工厂运行，映射到 5 步标准总进度
  if (props.factoryData?.factory_active) {
    const fs = factoryStageStates.value;
    if (stageKey === 'load_confirmed_spec') return fs.spec;
    if (stageKey === 'resolve_assets') return fs.beBlueprint;
    if (stageKey === 'assemble') {
      if (fs.feWorkshop === 'done') return 'done';
      if (fs.beWorkshop === 'warn' || fs.feWorkshop === 'warn') return 'warn';
      if (fs.beWorkshop !== 'idle' || fs.feWorkshop !== 'idle') return 'active';
      return 'idle';
    }
    if (stageKey === 'verify') {
      if (fs.e2e !== 'idle') return fs.e2e;
      return fs.beVerify;
    }
    if (stageKey === 'package') return fs.delivery;
  }

  const order = ['load_confirmed_spec', 'resolve_assets', 'assemble', 'verify', 'package'];
  const currentIdx = order.indexOf(props.run.phase ?? '');
  const idx = order.indexOf(stageKey);
  if (currentIdx === -1) {
    return idx === 0 && status !== 'QUEUED' ? 'active' : 'idle';
  }
  if (idx < currentIdx) return 'done';
  if (idx === currentIdx) return status === 'FAILED' ? 'warn' : 'active';
  return 'idle';
}

const currentStepSummary = computed(() => {
  if (!props.run) {
    return props.runMode === 'factory'
      ? '规格已冻结 · 异步工厂实时流水线已就绪，点击「开始生成」启动多部门并行组装'
      : '规格已冻结 · 点击「开始生成」启动实时装配与 Docker Runner 验收';
  }
  const status = props.run.status;
  if (status === 'SUCCEEDED') {
    return `运行 #${props.run.id} 已完成 · 全部阶段组装与验收通过，交付产物已就绪`;
  }
  if (status === 'FAILED') {
    return `运行 #${props.run.id} 中断（${props.run.error_code ?? 'FAILED'}）· 请在右侧「验收交付」查看失败项与缺陷单`;
  }
  if (status === 'PAUSED_BUDGET') {
    return `运行 #${props.run.id} 预算不足已暂停 · 已完成检查点已持久化，追加预算后可断点续跑`;
  }
  if (status === 'CANCELLED') {
    return `运行 #${props.run.id} 已取消 · 历史调用与成本记录如实保留`;
  }
  if (props.factoryData?.factory_active) {
    const fd = props.factoryData;
    const reworkLane = [...fd.backend_departments, ...fd.frontend_departments].find(
      (l) => l.status === 'REWORK',
    );
    if (reworkLane) {
      return `运行 #${props.run.id} · ${reworkLane.phase === 'frontend' ? 'Stage 5 前端车间' : 'Stage 3 后端车间'} · 部门 ${reworkLane.dept_id} 第 ${reworkLane.current_round} 轮自动返工中`;
    }
    if (fd.current_phase === 'frontend') {
      const done = fd.frontend_departments.filter((l) => l.status === 'PASSED').length;
      return `运行 #${props.run.id} · Stage 5 前端并行车间（${done}/${fd.frontend_departments.length} 部门已通过）`;
    }
    if (fd.current_phase === 'backend') {
      const done = fd.backend_departments.filter((l) => l.status === 'PASSED').length;
      return `运行 #${props.run.id} · Stage 3 后端并行车间（${done}/${fd.backend_departments.length} 部门已通过）`;
    }
    return `运行 #${props.run.id} · Stage 2 总管架构拆解与蓝图冻结中`;
  }
  const phaseMap: Record<string, string> = {
    load_confirmed_spec: `运行 #${props.run.id} · Stage 1 载入已确认规格`,
    resolve_assets: `运行 #${props.run.id} · Stage 2 解析能力包与资产`,
    assemble: `运行 #${props.run.id} · Stage 3 实时装配前后端源码`,
    verify: `运行 #${props.run.id} · Stage 4 Docker Runner 构建与验收`,
  };
  return phaseMap[props.run.phase ?? ''] ?? `运行 #${props.run.id} · 排队等待调度中`;
});

const backendBlueprint = computed(
  () => props.factoryData?.blueprints.find((b) => b.phase === 'backend') ?? null,
);
const frontendBlueprint = computed(
  () => props.factoryData?.blueprints.find((b) => b.phase === 'frontend') ?? null,
);

const recentEvents = computed(() => {
  const list = props.events ?? [];
  return [...list].slice(-12).reverse();
});

function formatEventPayload(payload: Record<string, unknown>): string {
  const keys = Object.keys(payload);
  if (keys.length === 0) return '—';
  const raw = JSON.stringify(payload);
  return raw.length > 140 ? `${raw.slice(0, 140)}…` : raw;
}
</script>

<template>
  <div class="flow-shell" data-testid="assembly-flowchart">
    <!-- Top Live Progress Banner -->
    <div
      class="flow-banner"
      :class="{
        'banner-ok': run?.status === 'SUCCEEDED',
        'banner-warn': run?.status === 'FAILED' || run?.status === 'PAUSED_BUDGET',
      }"
    >
      <span
        v-if="run && run.status !== 'SUCCEEDED' && run.status !== 'FAILED' && run.status !== 'CANCELLED'"
        class="af-pulse-dot banner-pulse"
      />
      <span class="banner-text">{{ currentStepSummary }}</span>
      <span v-if="run" class="af-mono banner-cost">
        {{ run.tasks.done }}/{{ run.tasks.total }} 任务 · {{ formatMicroYuan(run.cost.settled_micro) }}
      </span>
    </div>

    <!-- Main Canvas (Standard TodoList is floated at non-scrolling top-right of center viewport) -->
    <div class="flow-canvas-wrap">
      <!-- ============ MAIN CONTENT A: FACTORY MULTI-STAGE LIVE FLOWCHART ============ -->
      <div
        v-if="isFactoryView"
        class="flow-canvas"
        :data-testid="factoryData?.factory_active ? 'factory-swimlanes' : 'factory-preview-flow'"
      >
        <div class="flow-meta-strip">
          <span class="flow-title">异步工厂多部门泳道视图</span>
          <div class="flow-meta-pills">
            <el-tag
              v-if="factoryData"
              size="small"
              type="primary"
              disable-transitions
              data-testid="factory-phase"
            >
              {{ FACTORY_PHASE_LABELS[factoryData.current_phase] ?? factoryData.current_phase }}
            </el-tag>
            <span v-if="factoryData" class="af-mono meta-chip">
              后端总装大循环：第 {{ factoryData.integration_rounds.backend ?? 0 }} 轮
            </span>
            <span v-if="factoryData" class="af-mono meta-chip">
              前端总装大循环：第 {{ factoryData.integration_rounds.frontend ?? 0 }} 轮
            </span>
          </div>
        </div>

        <div class="dag-column">
          <!-- Stage 1: Spec Loaded -->
          <div
            role="button"
            tabindex="0"
            class="dag-node"
            :class="[`state-${factoryStageStates.spec}`, { selected: selectedNode === 'spec' }]"
            @click="emit('select-node', 'spec')"
            @keydown.enter.prevent="emit('select-node', 'spec')"
          >
            <div class="dag-node-head">
              <span class="dag-step-badge">1</span>
              <span class="dag-node-title">冻结规格载入</span>
              <span class="af-mono dag-node-state">
                {{ factoryStageStates.spec === 'done' ? 'DONE' : 'WAITING' }}
              </span>
            </div>
            <div class="dag-node-sub af-mono">
              <template v-if="spec">
                Spec v{{ spec.version }} · hash {{ spec.hash.slice(0, 10) }} · {{ spec.spec.entities.length }} 实体
              </template>
              <template v-else-if="run">
                Spec #{{ run.spec_id }} · Run #{{ run.id }}
              </template>
              <template v-else>已锁定规格快照</template>
            </div>
          </div>

          <svg class="dag-arrow" viewBox="0 0 24 22" aria-hidden="true">
            <line x1="12" y1="0" x2="12" y2="18" :stroke="factoryStageStates.spec === 'done' ? 'var(--af-ok)' : 'var(--af-border-strong)'" stroke-width="1.5" />
            <polygon points="8,15 16,15 12,21" :fill="factoryStageStates.spec === 'done' ? 'var(--af-ok)' : 'var(--af-border-strong)'" />
          </svg>

          <!-- Stage 2: Chief Architect & Blueprint Freeze -->
          <div
            role="button"
            tabindex="0"
            class="dag-node"
            :class="[`state-${factoryStageStates.beBlueprint}`, { selected: selectedNode === 'blueprint' }]"
            @click="emit('select-node', 'blueprint')"
            @keydown.enter.prevent="emit('select-node', 'blueprint')"
          >
            <div class="dag-node-head">
              <span class="dag-step-badge">2</span>
              <span class="dag-node-title">总管架构拆解 &amp; 后端蓝图冻结</span>
              <span class="af-mono dag-node-state">
                {{ backendBlueprint ? backendBlueprint.status : factoryStageStates.beBlueprint.toUpperCase() }}
              </span>
            </div>
            <div class="dag-node-sub af-mono">
              <template v-if="backendBlueprint">
                后端蓝图 v{{ backendBlueprint.version }} · {{ backendBlueprint.module_count }} 部门 · {{ backendBlueprint.blueprint_hash.slice(0, 8) }}
              </template>
              <template v-else>
                Chief Architect 拆解模块归属路径与共享接口契约
              </template>
            </div>
          </div>

          <svg class="dag-arrow" viewBox="0 0 24 22" aria-hidden="true">
            <line
              x1="12"
              y1="0"
              x2="12"
              y2="18"
              :stroke="factoryStageStates.beWorkshop === 'active' ? 'var(--af-accent)' : factoryStageStates.beWorkshop === 'done' ? 'var(--af-ok)' : 'var(--af-border-strong)'"
              stroke-width="1.5"
              :class="{ 'af-flow-dash': factoryStageStates.beWorkshop === 'active' }"
            />
            <polygon points="8,15 16,15 12,21" :fill="factoryStageStates.beWorkshop === 'done' ? 'var(--af-ok)' : 'var(--af-accent)'" />
          </svg>

          <!-- Stage 3: Backend Parallel Department Workshop -->
          <div
            class="dag-workshop"
            :class="`state-${factoryStageStates.beWorkshop}`"
            data-testid="backend-swimlanes"
          >
            <div class="workshop-head">
              <div class="workshop-title-group">
                <span class="dag-step-badge">3</span>
                <span class="dag-node-title">
                  后端并行部门泳道（{{ factoryData?.backend_departments.length ?? (spec?.spec.entities.length || 1) }}）
                </span>
                <span v-if="factoryStageStates.beWorkshop === 'active' || factoryStageStates.beWorkshop === 'warn'" class="active-step-pill">
                  当前所在步骤
                </span>
              </div>
              <span class="af-mono dag-node-state">staging/backend/&lt;dept&gt;/</span>
            </div>

            <div v-if="factoryData?.backend_departments.length" class="lane-grid">
              <div
                v-for="lane in factoryData.backend_departments"
                :key="`be-${lane.dept_id}`"
                class="dept-card"
                :class="{ selected: selectedNode === `be-${lane.dept_id}` }"
                :data-testid="`dept-lane-backend-${lane.dept_id}`"
                @click="emit('select-node', `be-${lane.dept_id}`, lane.dept_id)"
              >
                <div class="dept-card-head">
                  <span class="dept-name af-mono">{{ lane.dept_id }}</span>
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
                <div class="dept-metrics af-mono">
                  <span>轮次：{{ lane.current_round }} / {{ lane.max_rounds }}</span>
                  <span>耗时：{{ formatDurationMs(lane.duration_ms) }}</span>
                  <span>
                    测试：{{ lane.test_status ?? '未运行' }}
                    <template v-if="lane.test_exit_code !== null">（exit {{ lane.test_exit_code }}）</template>
                  </span>
                  <span>费用：{{ formatMicroYuan(lane.cost_micro) }}（{{ lane.input_tokens }}/{{ lane.output_tokens }} tok）</span>
                </div>
                <p v-if="lane.error_code" class="dept-error af-mono">{{ lane.error_code }}</p>
              </div>
            </div>

            <div v-else class="workshop-placeholder">
              启动工厂后，各后端业务模块将在此并行执行「规划 → 编码 → 测试 → 返工修复」闭环。
            </div>
          </div>

          <svg class="dag-arrow" viewBox="0 0 24 22" aria-hidden="true">
            <line x1="12" y1="0" x2="12" y2="18" :stroke="factoryStageStates.beVerify === 'done' ? 'var(--af-ok)' : 'var(--af-border-strong)'" stroke-width="1.5" />
            <polygon points="8,15 16,15 12,21" :fill="factoryStageStates.beVerify === 'done' ? 'var(--af-ok)' : 'var(--af-border-strong)'" />
          </svg>

          <!-- Stage 4: Backend Integration & Docker Runner Verify -->
          <div
            role="button"
            tabindex="0"
            class="dag-node"
            :class="[`state-${factoryStageStates.beVerify}`, { selected: selectedNode === 'be-verify' }]"
            @click="emit('select-node', 'be-verify')"
            @keydown.enter.prevent="emit('select-node', 'be-verify')"
          >
            <div class="dag-node-head">
              <span class="dag-step-badge">4</span>
              <span class="dag-node-title">后端合流 &amp; Docker Runner 总验收</span>
              <span class="af-mono dag-node-state">
                第 {{ factoryData?.integration_rounds.backend ?? 0 }} 轮
              </span>
            </div>
            <div class="dag-node-sub">合流各部门 staging 补丁，驱动独立 Docker Runner 执行全量编译与 API 联调</div>
          </div>

          <svg class="dag-arrow" viewBox="0 0 24 22" aria-hidden="true">
            <line x1="12" y1="0" x2="12" y2="18" :stroke="factoryStageStates.feWorkshop === 'done' ? 'var(--af-ok)' : 'var(--af-border-strong)'" stroke-width="1.5" />
            <polygon points="8,15 16,15 12,21" :fill="factoryStageStates.feWorkshop === 'done' ? 'var(--af-ok)' : 'var(--af-border-strong)'" />
          </svg>

          <!-- Stage 5: Frontend Parallel Department Workshop -->
          <div
            class="dag-workshop"
            :class="`state-${factoryStageStates.feWorkshop}`"
            data-testid="frontend-swimlanes"
          >
            <div class="workshop-head">
              <div class="workshop-title-group">
                <span class="dag-step-badge">5</span>
                <span class="dag-node-title">
                  前端并行部门泳道（{{ factoryData?.frontend_departments.length ?? 0 }}）
                </span>
                <span v-if="factoryStageStates.feWorkshop === 'active' || factoryStageStates.feWorkshop === 'warn'" class="active-step-pill">
                  当前所在步骤
                </span>
              </div>
              <span v-if="frontendBlueprint" class="af-mono dag-node-state">
                前端蓝图 v{{ frontendBlueprint.version }} · {{ frontendBlueprint.blueprint_hash.slice(0, 8) }}
              </span>
              <span v-else class="af-mono dag-node-state">staging/frontend/&lt;dept&gt;/</span>
            </div>

            <div v-if="factoryData?.frontend_departments.length" class="lane-grid">
              <div
                v-for="lane in factoryData.frontend_departments"
                :key="`fe-${lane.dept_id}`"
                class="dept-card"
                :class="{ selected: selectedNode === `fe-${lane.dept_id}` }"
                :data-testid="`dept-lane-frontend-${lane.dept_id}`"
                @click="emit('select-node', `fe-${lane.dept_id}`, lane.dept_id)"
              >
                <div class="dept-card-head">
                  <span class="dept-name af-mono">{{ lane.dept_id }}</span>
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
                <div class="dept-metrics af-mono">
                  <span>轮次：{{ lane.current_round }} / {{ lane.max_rounds }}</span>
                  <span>耗时：{{ formatDurationMs(lane.duration_ms) }}</span>
                  <span>
                    测试：{{ lane.test_status ?? '未运行' }}
                    <template v-if="lane.test_exit_code !== null">（exit {{ lane.test_exit_code }}）</template>
                  </span>
                  <span>费用：{{ formatMicroYuan(lane.cost_micro) }}（{{ lane.input_tokens }}/{{ lane.output_tokens }} tok）</span>
                </div>
                <p v-if="lane.error_code" class="dept-error af-mono">{{ lane.error_code }}</p>
              </div>
            </div>

            <div v-else class="workshop-placeholder">
              后端总验收通过后，自动生成前端蓝图并派发前端页面模块并行构建。
            </div>
          </div>

          <svg class="dag-arrow" viewBox="0 0 24 22" aria-hidden="true">
            <line x1="12" y1="0" x2="12" y2="18" :stroke="factoryStageStates.e2e === 'done' ? 'var(--af-ok)' : 'var(--af-border-strong)'" stroke-width="1.5" />
            <polygon points="8,15 16,15 12,21" :fill="factoryStageStates.e2e === 'done' ? 'var(--af-ok)' : 'var(--af-border-strong)'" />
          </svg>

          <!-- Stage 6 & 7: Fullstack E2E + Delivery -->
          <div class="dag-end-grid">
            <div
              role="button"
              tabindex="0"
              class="dag-node"
              :class="[`state-${factoryStageStates.e2e}`, { selected: selectedNode === 'e2e' }]"
              @click="emit('select-node', 'e2e')"
              @keydown.enter.prevent="emit('select-node', 'e2e')"
            >
              <div class="dag-node-head">
                <span class="dag-step-badge">6</span>
                <span class="dag-node-title">全栈总装 &amp; E2E</span>
                <span class="af-mono dag-node-state">{{ factoryStageStates.e2e.toUpperCase() }}</span>
              </div>
              <div class="dag-node-sub">Docker 全栈联调与外部服务就绪校验</div>
            </div>

            <div
              role="button"
              tabindex="0"
              class="dag-node"
              :class="[`state-${factoryStageStates.delivery}`, { selected: selectedNode === 'delivery' }]"
              @click="emit('select-node', 'delivery')"
              @keydown.enter.prevent="emit('select-node', 'delivery')"
            >
              <div class="dag-node-head">
                <span class="dag-step-badge">7</span>
                <span class="dag-node-title">交付产物打包</span>
                <span class="af-mono dag-node-state">{{ factoryStageStates.delivery.toUpperCase() }}</span>
              </div>
              <div class="dag-node-sub">ZIP 源码包 + SHA-256 验证报告</div>
            </div>
          </div>
        </div>
      </div>

      <!-- ============ MAIN CONTENT B: LIVE RUNTIME FLOWCHART (STANDARD MODE) ============ -->
      <div v-else class="flow-canvas" data-testid="live-run-flowchart">
        <div class="flow-meta-strip">
          <div class="live-title-group">
            <span class="flow-title">实时运行流程图</span>
            <span v-if="run" class="af-mono meta-chip">Run #{{ run.id }} · {{ run.status }}</span>
            <span v-else class="af-mono meta-chip">待启动</span>
          </div>
        </div>

        <!-- Horizontal Compact Live Pipeline Flow -->
        <div class="live-track">
          <template v-for="(stage, idx) in STANDARD_STAGES" :key="stage.key">
            <div
              role="button"
              tabindex="0"
              class="live-stage-card"
              :class="[`state-${standardStageState(stage.key)}`, { selected: selectedNode === stage.key }]"
              @click="emit('select-node', stage.key)"
              @keydown.enter.prevent="emit('select-node', stage.key)"
            >
              <div class="live-stage-top">
                <span class="dag-step-badge">{{ stage.idx }}</span>
                <span class="live-stage-name">{{ stage.short }}</span>
              </div>
              <div class="live-stage-foot af-mono">
                <span v-if="standardStageState(stage.key) === 'active'" class="active-step-pill">
                  执行中
                </span>
                <span v-else>{{ standardStageState(stage.key).toUpperCase() }}</span>
              </div>
            </div>
            <svg
              v-if="idx < STANDARD_STAGES.length - 1"
              class="live-arrow"
              viewBox="0 0 22 16"
              aria-hidden="true"
            >
              <line
                x1="0"
                y1="8"
                x2="16"
                y2="8"
                :stroke="standardStageState(stage.key) === 'done' ? 'var(--af-ok)' : standardStageState(stage.key) === 'active' ? 'var(--af-accent)' : 'var(--af-border-strong)'"
                stroke-width="1.5"
                :class="{ 'af-flow-dash': standardStageState(stage.key) === 'active' }"
              />
              <polygon
                points="14,4 21,8 14,12"
                :fill="standardStageState(stage.key) === 'done' ? 'var(--af-ok)' : standardStageState(stage.key) === 'active' ? 'var(--af-accent)' : 'var(--af-border-strong)'"
              />
            </svg>
          </template>
        </div>

        <!-- Live Execution Timeline -->
        <div class="live-timeline-card">
          <div class="live-timeline-head">
            <span class="live-timeline-title">实时推进轨迹</span>
            <span v-if="run" class="af-mono meta-chip">
              更新于 {{ formatDateTime(run.updated_at) }}
            </span>
          </div>

          <div v-if="!run" class="workshop-placeholder">
            点击下方「开始生成」启动构建。
          </div>
          <div v-else-if="recentEvents.length === 0" class="workshop-placeholder">
            等待执行事件...
          </div>
          <ul v-else class="live-event-list">
            <li v-for="ev in recentEvents" :key="ev.sequence" class="live-event-row">
              <span class="af-mono ev-seq">#{{ ev.sequence }}</span>
              <span class="ev-type af-mono">{{ ev.type }}</span>
              <span class="ev-payload af-mono">{{ formatEventPayload(ev.payload) }}</span>
              <span class="ev-time af-mono">{{ formatDateTime(ev.created_at) }}</span>
            </li>
          </ul>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.flow-shell {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.flow-banner {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 13px;
  border-radius: 10px;
  border: 1px solid var(--af-accent-border);
  background: var(--af-accent-soft);
  font-size: 12px;
  font-weight: 500;
  color: var(--af-fg);
}

.flow-banner.banner-ok {
  border-color: var(--af-ok-border);
  background: var(--af-ok-soft);
}

.flow-banner.banner-warn {
  border-color: var(--af-warn-border);
  background: var(--af-warn-soft);
}

.banner-pulse {
  background: var(--af-accent);
  color: var(--af-accent);
  flex-shrink: 0;
}

.banner-text {
  flex: 1;
  line-height: 1.45;
}

.banner-cost {
  font-size: 11px;
  color: var(--af-fg-secondary);
  flex-shrink: 0;
}

.flow-canvas-wrap {
  position: relative;
}

.flow-canvas {
  border-radius: 12px;
  border: 1px solid var(--af-border-strong);
  background-color: var(--af-surface);
  background-image: radial-gradient(var(--af-grid-dot) 1px, transparent 1px);
  background-size: 18px 18px;
  padding: 16px;
  min-height: 220px;
}

.flow-meta-strip {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}

.live-title-group {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.flow-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--af-fg);
}

.flow-meta-pills {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.meta-chip {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 6px;
  border: 1px solid var(--af-border-strong);
  background: var(--af-surface-2);
  color: var(--af-muted);
}

/* Live Standard Flowchart Track */
.live-track {
  display: flex;
  align-items: stretch;
  gap: 6px;
  overflow-x: auto;
  padding-bottom: 4px;
}

.live-stage-card {
  flex: 1;
  min-width: 118px;
  border-radius: 10px;
  border: 1px solid var(--af-border-strong);
  background: var(--af-surface);
  padding: 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  cursor: pointer;
  transition: border-color 140ms ease, box-shadow 140ms ease;
}

.live-stage-card:hover {
  border-color: var(--af-accent-border);
}

.live-stage-card.selected {
  border-color: var(--af-accent);
  box-shadow: 0 0 0 1px var(--af-accent);
}

.live-stage-card.state-done {
  border-color: var(--af-ok-border);
}

.live-stage-card.state-active {
  border-color: var(--af-accent);
  background: var(--af-surface-2);
}

.live-stage-card.state-warn {
  border-color: var(--af-warn-border);
}

.live-stage-top {
  display: flex;
  align-items: center;
  gap: 6px;
}

.live-stage-name {
  font-size: 12px;
  font-weight: 600;
  color: var(--af-fg);
}

.live-stage-desc {
  font-size: 11px;
  color: var(--af-muted);
  line-height: 1.4;
  flex: 1;
}

.live-stage-foot {
  font-size: 10px;
  color: var(--af-muted);
}

.live-arrow {
  width: 20px;
  flex-shrink: 0;
  align-self: center;
}

.live-timeline-card {
  margin-top: 14px;
  border-radius: 10px;
  border: 1px solid var(--af-border);
  background: var(--af-bg);
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.live-timeline-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.live-timeline-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--af-fg);
}

.live-event-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.live-event-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-radius: 7px;
  background: var(--af-surface);
  border: 1px solid var(--af-border);
  font-size: 11px;
}

.ev-seq {
  color: var(--af-muted);
  min-width: 28px;
}

.ev-type {
  padding: 1px 6px;
  border-radius: 5px;
  background: var(--af-accent-soft);
  color: var(--af-accent);
  font-weight: 500;
  flex-shrink: 0;
}

.ev-payload {
  flex: 1;
  color: var(--af-fg-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ev-time {
  color: var(--af-muted);
  flex-shrink: 0;
}

/* Factory DAG Styles */
.dag-column {
  display: flex;
  flex-direction: column;
  align-items: center;
  margin-top: 10px;
}

.dag-node {
  width: 100%;
  max-width: 560px;
  border-radius: 10px;
  border: 1px solid var(--af-border-strong);
  background: var(--af-surface);
  padding: 10px 13px;
  text-align: left;
  color: var(--af-fg);
  cursor: pointer;
  transition: border-color 140ms ease, box-shadow 140ms ease;
}

.dag-node:hover {
  border-color: var(--af-accent-border);
}

.dag-node.selected {
  border-color: var(--af-accent);
  box-shadow: 0 0 0 1px var(--af-accent);
}

.dag-node.state-done {
  border-color: var(--af-ok-border);
}

.dag-node.state-active {
  border-color: var(--af-accent);
  background: var(--af-surface-2);
}

.dag-node.state-warn {
  border-color: var(--af-warn-border);
}

.dag-node-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.dag-step-badge {
  width: 20px;
  height: 20px;
  border-radius: 99px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  font-weight: 600;
  background: var(--af-surface-2);
  border: 1px solid var(--af-border-strong);
  color: var(--af-muted);
  flex-shrink: 0;
}

.state-done .dag-step-badge {
  background: var(--af-ok-soft);
  border-color: var(--af-ok-border);
  color: var(--af-ok);
}

.state-active .dag-step-badge {
  background: var(--af-accent-soft);
  border-color: var(--af-accent-border);
  color: var(--af-accent);
}

.state-warn .dag-step-badge {
  background: var(--af-warn-soft);
  border-color: var(--af-warn-border);
  color: var(--af-warn);
}

.dag-node-title {
  font-size: 12.5px;
  font-weight: 600;
  flex: 1;
}

.dag-node-state {
  font-size: 10.5px;
  color: var(--af-muted);
}

.dag-node-sub {
  margin-top: 5px;
  padding-left: 28px;
  font-size: 11.5px;
  color: var(--af-muted);
  line-height: 1.4;
}

.dag-arrow {
  width: 24px;
  height: 22px;
  flex-shrink: 0;
}

.dag-workshop {
  width: 100%;
  border-radius: 12px;
  border: 1px solid var(--af-border-strong);
  background: var(--af-bg);
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.dag-workshop.state-active {
  border-color: var(--af-accent-border);
  background: var(--af-accent-soft);
}

.dag-workshop.state-warn {
  border-color: var(--af-warn-border);
}

.dag-workshop.state-done {
  border-color: var(--af-ok-border);
}

.workshop-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
}

.workshop-title-group {
  display: flex;
  align-items: center;
  gap: 8px;
}

.active-step-pill {
  font-size: 10.5px;
  font-weight: 600;
  padding: 2px 7px;
  border-radius: 99px;
  background: var(--af-accent-soft);
  border: 1px solid var(--af-accent-border);
  color: var(--af-accent);
}

.lane-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(210px, 1fr));
  gap: 9px;
}

.dept-card {
  border-radius: 9px;
  border: 1px solid var(--af-border-strong);
  background: var(--af-surface);
  padding: 10px;
  display: flex;
  flex-direction: column;
  gap: 7px;
  cursor: pointer;
  transition: border-color 140ms ease;
}

.dept-card:hover,
.dept-card.selected {
  border-color: var(--af-accent);
}

.dept-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
}

.dept-name {
  font-size: 12px;
  font-weight: 600;
  color: var(--af-fg);
}

.step-track {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 4px;
}

.step-pill {
  text-align: center;
  font-size: 10px;
  padding: 2px 3px;
  border-radius: 4px;
  background: var(--af-surface-2);
  color: var(--af-muted);
}

.step-done {
  background: var(--af-ok-soft);
  color: var(--af-ok);
}

.step-active {
  background: var(--af-accent-soft);
  color: var(--af-accent);
  font-weight: 600;
}

.step-warn {
  background: var(--af-warn-soft);
  color: var(--af-warn);
  font-weight: 600;
}

.dept-metrics {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 10.5px;
  color: var(--af-muted);
}

.dept-error {
  margin: 0;
  font-size: 10.5px;
  color: var(--af-err);
}

.workshop-placeholder {
  font-size: 12px;
  color: var(--af-muted);
  padding: 10px;
  border-radius: 8px;
  border: 1px dashed var(--af-border-strong);
  text-align: center;
}

.dag-end-grid {
  width: 100%;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

@media (max-width: 780px) {
  .dag-end-grid {
    grid-template-columns: 1fr;
  }
}
</style>
