<script setup lang="ts">
import { computed, inject, nextTick, onUnmounted, reactive, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';

import { ElMessage, ElMessageBox } from 'element-plus';

import { ApiError, interviewApi, newIdempotencyKey, projectsApi, runsApi } from '@/api/client';
import type {
  FactoryRunView,
  InterviewAnswer,
  InterviewView as InterviewViewData,
  ProjectDetail,
  RunDetail,
  RunEvent,
  RunMode,
  RunView,
  SoftwareSpecification,
  SpecView,
} from '@/api/types';
import ConfirmToggle from '@/components/ConfirmToggle.vue';
import DesignedApisPanel from '@/components/DesignedApisPanel.vue';
import FactoryFlowchart from '@/components/FactoryFlowchart.vue';
import RunEventsPanel from '@/components/RunEventsPanel.vue';
import RunVerificationPanel from '@/components/RunVerificationPanel.vue';
import SafeText from '@/components/SafeText.vue';
import StandardAssemblyTodo from '@/components/StandardAssemblyTodo.vue';
import StringListEditor from '@/components/StringListEditor.vue';
import { clearSession } from '@/stores/auth';
import {
  INSPECTOR_HOST_KEY,
  refreshProjectRuns,
  refreshProjects,
  setActiveHeaderContext,
  setProjectRoutingSnapshot,
  setRightPanelOpen,
  useWorkspaceStore,
} from '@/stores/workspace';
import { describeError } from '@/utils/errors';
import { formatDateTime } from '@/utils/format';

const props = defineProps<{ id: string }>();
const route = useRoute();
const router = useRouter();
const workspace = useWorkspaceStore();
const inspectorHostReady = inject(INSPECTOR_HOST_KEY, ref(false));

const projectId = computed(() => Number(props.id));

const FIELD_TYPES = [
  'long', 'int', 'integer', 'string', 'text', 'bool', 'boolean', 'datetime', 'date', 'decimal',
];
const ROLE_OPTIONS = ['ADMIN', 'MEMBER'];
const ACTION_OPTIONS = ['create', 'read', 'update', 'delete'];

function blankSpec(): SoftwareSpecification {
  return {
    schema_version: '0.1',
    project_type: 'crud_web',
    goal: '',
    actors: [],
    entities: [],
    relations: [],
    pages: [],
    permissions: [],
    business_rules: [],
    acceptance_cases: [],
    technical_profile: {
      profile_id: 'demo-ts-mp',
      profile_version: '0.3',
      template_version: 'demo-0.2',
    },
    constraints: [],
    exclusions: [],
    capability_requests: [],
    recipe_requests: [],
    integrations: [],
    external_requirements: [],
  };
}

interface DraftAnswer {
  selected: string[];
  custom: string;
  unsure: boolean;
}

interface LocalChatTurn {
  id: number;
  role: 'user' | 'assistant';
  text: string;
  time: string;
  actionTab?: 'spec' | 'apis';
}

const project = ref<ProjectDetail | null>(null);
const spec = ref<SpecView | null>(null);
const runs = ref<RunView[]>([]);
const draft = reactive<SoftwareSpecification>(blankSpec());
const loadedJson = ref('');

// 内嵌 R02/R05 对话反问状态
const interview = ref<InterviewViewData | null>(null);
const interviewDrafts = reactive<Record<string, DraftAnswer>>({});
const interviewBusy = ref(false);
const interviewError = ref('');

// 底部对话框输入与本地对话补充流
const composerInput = ref('');
const composerBusy = ref(false);
const chatTurns = ref<LocalChatTurn[]>([]);
const chatScrollRef = ref<HTMLElement | null>(null);
let nextTurnId = 1;

// 视图切换：冻结前默认对话反问，冻结后自动切换为工厂组装流程图
const centerView = ref<'chat' | 'flowchart'>('chat');
const rightTab = ref<'apis' | 'spec' | 'verify' | 'events'>('apis');
const selectedNode = ref<string | null>(null);
const selectedModule = ref<string | null>(null);

// 最近一次或用户选中运行的实时轮询数据（直接在「工厂组装流程图」与右侧边栏展示）
const TERMINAL_RUN_STATUS = new Set(['SUCCEEDED', 'FAILED', 'CANCELLED']);
interface CachedRunSnapshot {
  detail: RunDetail;
  factory: FactoryRunView | null;
  events: RunEvent[];
  seq: number;
}
const terminalRunCache = new Map<number, CachedRunSnapshot>();

const liveRunDetail = ref<RunDetail | null>(null);
const liveFactoryData = ref<FactoryRunView | null>(null);
const liveRunEvents = ref<RunEvent[]>([]);
const selectedRunId = computed<number | null>(() => {
  const q = route.query.run;
  const parsed = typeof q === 'string' ? Number(q) : NaN;
  if (Number.isFinite(parsed) && parsed > 0) return parsed;
  return runs.value[0]?.id ?? null;
});
let liveEventSeq = 0;
let livePollTimer: ReturnType<typeof setTimeout> | null = null;
let livePollRunId: number | null = null;

// 按需懒挂载右侧「需求规格」重表单：避免在看流程图/对话时同步挂载数百个 Element Plus 表单组件造成卡顿
const specTabEverOpened = ref(false);
const specTabMounted = computed(
  () =>
    !inspectorHostReady.value ||
    specTabEverOpened.value ||
    (workspace.rightPanelOpen && rightTab.value === 'spec'),
);
watch(
  () => workspace.rightPanelOpen && rightTab.value === 'spec',
  (visible) => {
    if (visible) specTabEverOpened.value = true;
  },
  { immediate: true },
);

const loading = ref(true);
const specLoaded = ref(false);
const extracting = ref(false);
const saving = ref(false);
const confirming = ref(false);
const generating = ref(false);
const reopenEditing = ref(false);

const errorMessage = ref('');
const specMessage = ref('');
const idempotencyKey = ref<string | null>(null);
const runMode = ref<RunMode>(workspace.defaultRunMode ?? 'standard');

const hasSpec = computed(() => spec.value !== null);
const isDirty = computed(() => hasSpec.value && JSON.stringify(draft) !== loadedJson.value);
const readOnly = computed(() => spec.value?.confirmed === true && !reopenEditing.value);
const canConfirm = computed(
  () =>
    spec.value !== null &&
    !spec.value.confirmed &&
    !readOnly.value &&
    !isDirty.value &&
    spec.value.pending.length === 0 &&
    spec.value.issues.length === 0,
);
const canGenerate = computed(
  () =>
    spec.value !== null &&
    spec.value.confirmed &&
    project.value !== null &&
    project.value.active_spec_id === spec.value.spec_id,
);

const composerPlaceholder = computed(() => {
  if (interview.value?.waiting) {
    return '继续补充需求细节、直接输入文本回答，或点击上方问答卡片推进澄清...（Enter 发送，Shift+Enter 换行）';
  }
  return '继续补充业务规则、边界条件或需求细节，发送后将自动同步至规格草稿...（Enter 发送，Shift+Enter 换行）';
});

watch(
  () => workspace.defaultRunMode,
  (mode) => {
    runMode.value = mode;
  },
);

function nowClock(): string {
  const now = new Date();
  return `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;
}

function appendChatTurn(role: 'user' | 'assistant', text: string, actionTab?: 'spec' | 'apis'): void {
  chatTurns.value.push({ id: nextTurnId++, role, text, time: nowClock(), actionTab });
  void nextTick(() => {
    if (chatScrollRef.value) {
      chatScrollRef.value.scrollTop = chatScrollRef.value.scrollHeight;
    }
  });
}

function openInspectorTab(tab: 'apis' | 'spec' | 'verify' | 'events'): void {
  rightTab.value = tab;
  setRightPanelOpen(true);
}

watch(
  () => [route.query.run, route.query.view] as const,
  ([runQuery, viewQuery]) => {
    if (viewQuery === 'chat') {
      centerView.value = 'chat';
    } else if (viewQuery === 'flowchart' || (typeof runQuery === 'string' && Number(runQuery) > 0)) {
      centerView.value = 'flowchart';
    }
    if (selectedRunId.value !== livePollRunId) {
      startLiveRunPolling(selectedRunId.value);
    }
  },
);

function stopLiveRunPolling(): void {
  if (livePollTimer !== null) {
    clearTimeout(livePollTimer);
    livePollTimer = null;
  }
  livePollRunId = null;
}

async function pollLatestRun(runId: number): Promise<void> {
  if (livePollRunId !== runId) return;
  if (typeof runsApi.get !== 'function') return;
  try {
    // 并发请求 get / events / factory，消除 3 次串行网络 RTT 叠加卡顿
    const [detailRes, eventsRes, factoryRes] = await Promise.allSettled([
      runsApi.get(runId),
      typeof runsApi.events === 'function' ? runsApi.events(runId, liveEventSeq) : Promise.resolve(null),
      typeof runsApi.factory === 'function' ? runsApi.factory(runId) : Promise.resolve(null),
    ]);

    if (livePollRunId !== runId) return;

    const detail = detailRes.status === 'fulfilled' ? detailRes.value : null;
    if (detail) {
      liveRunDetail.value = detail;
    }

    if (eventsRes.status === 'fulfilled' && eventsRes.value?.items?.length) {
      liveRunEvents.value = [...liveRunEvents.value, ...eventsRes.value.items];
      liveEventSeq = eventsRes.value.items[eventsRes.value.items.length - 1].sequence;
    }

    if (factoryRes.status === 'fulfilled' && factoryRes.value) {
      liveFactoryData.value = factoryRes.value;
      if (detail && factoryRes.value.model_routing_snapshot?.length) {
        setProjectRoutingSnapshot(detail.project_id, detail.id, factoryRes.value.model_routing_snapshot);
      }
    }

    if (detail && TERMINAL_RUN_STATUS.has(detail.status)) {
      if (terminalRunCache.size > 30) {
        const oldestKey = terminalRunCache.keys().next().value;
        if (oldestKey !== undefined) terminalRunCache.delete(oldestKey);
      }
      terminalRunCache.set(runId, {
        detail,
        factory: liveFactoryData.value,
        events: [...liveRunEvents.value],
        seq: liveEventSeq,
      });
      stopLiveRunPolling();
      return;
    }
  } catch {
    // non-fatal for project detail view
  }
  if (livePollRunId === runId) {
    livePollTimer = setTimeout(() => {
      void pollLatestRun(runId);
    }, 2500);
  }
}

function startLiveRunPolling(runId: number | null): void {
  stopLiveRunPolling();
  if (runId === null) {
    liveRunDetail.value = null;
    liveFactoryData.value = null;
    liveRunEvents.value = [];
    liveEventSeq = 0;
    return;
  }

  // 命中已完成的终态缓存：0ms 瞬开，无需重复打数据库
  const cached = terminalRunCache.get(runId);
  if (cached) {
    liveRunDetail.value = cached.detail;
    liveFactoryData.value = cached.factory;
    liveRunEvents.value = [...cached.events];
    liveEventSeq = cached.seq;
    if (cached.factory?.model_routing_snapshot?.length) {
      setProjectRoutingSnapshot(
        cached.detail.project_id,
        cached.detail.id,
        cached.factory.model_routing_snapshot,
      );
    }
    return;
  }

  liveRunEvents.value = [];
  liveEventSeq = 0;
  // 切换不同运行且无缓存时，重置工厂数据但不闪烁整页
  if (liveRunDetail.value?.id !== runId) {
    liveRunDetail.value = null;
    liveFactoryData.value = null;
  }
  livePollRunId = runId;
  void pollLatestRun(runId);
}

onUnmounted(stopLiveRunPolling);

function syncHeader(): void {
  if (!project.value) return;
  const frozen = spec.value?.confirmed === true;
  setActiveHeaderContext({
    projectId: project.value.id,
    projectName: project.value.name,
    stageLabel: frozen ? `已冻结规格 v${spec.value?.version ?? 1}` : hasSpec.value ? '需求对话澄清' : '待提取需求',
    stageTone: frozen ? 'ok' : hasSpec.value ? 'warn' : 'muted',
    cost: null,
  });
}

function applySpec(view: SpecView): void {
  spec.value = view;
  Object.assign(draft, structuredClone(view.spec));
  loadedJson.value = JSON.stringify(view.spec);
  reopenEditing.value = false;
  // 若显式带 ?view=chat 则保留对话流；否则冻结后自动切至组装流程图
  if (route.query.view === 'chat') {
    centerView.value = 'chat';
  } else {
    centerView.value = view.confirmed ? 'flowchart' : 'chat';
  }
  syncHeader();
}

function ensureInterviewDrafts(data: InterviewViewData): void {
  for (const question of data.questions) {
    if (!interviewDrafts[question.question_id]) {
      interviewDrafts[question.question_id] = { selected: [], custom: '', unsure: false };
    }
  }
}

async function handleAuthError(error: unknown): Promise<boolean> {
  const info = describeError(error);
  if (info.unauthorized) {
    clearSession();
    await router.push({ name: 'login' });
    return true;
  }
  return false;
}

async function loadProject(): Promise<void> {
  errorMessage.value = '';
  try {
    project.value = await projectsApi.get(projectId.value);
    syncHeader();
  } catch (error) {
    errorMessage.value = describeError(error).message;
    await handleAuthError(error);
  }
}

async function loadSpec(): Promise<void> {
  try {
    applySpec(await projectsApi.getSpec(projectId.value));
  } catch (error) {
    if (await handleAuthError(error)) {
      return;
    }
    spec.value = null;
    centerView.value = 'chat';
    if (!(error instanceof ApiError && error.code === 'SPEC_NOT_FOUND')) {
      specMessage.value = describeError(error).message;
    }
  } finally {
    specLoaded.value = true;
    syncHeader();
  }
}

async function loadRuns(): Promise<void> {
  if (typeof runsApi.listByProject !== 'function') {
    runs.value = [];
    startLiveRunPolling(null);
    return;
  }
  try {
    const res = await runsApi.listByProject(projectId.value);
    runs.value = res.items ?? [];
    if (route.query.view === 'flowchart' || (typeof route.query.run === 'string' && Number(route.query.run) > 0)) {
      centerView.value = 'flowchart';
    }
    const targetRunId = selectedRunId.value;
    if (targetRunId !== livePollRunId && liveRunDetail.value?.id !== targetRunId) {
      startLiveRunPolling(targetRunId);
    }
  } catch (error) {
    await handleAuthError(error);
  }
}

async function loadInterview(): Promise<void> {
  if (typeof interviewApi?.get !== 'function') {
    interview.value = null;
    return;
  }
  try {
    const data = await interviewApi.get(projectId.value);
    interview.value = data;
    ensureInterviewDrafts(data);
  } catch {
    interview.value = null;
  }
}

async function loadAll(): Promise<void> {
  loading.value = !project.value;
  specMessage.value = '';
  interviewError.value = '';
  chatTurns.value = [];
  composerInput.value = '';
  // 若 URL 已指定具体 runId，立即并行开始拉取该运行数据，不必等 loadRuns 返回后再串行启动
  const explicitRun = typeof route.query.run === 'string' ? Number(route.query.run) : NaN;
  if (Number.isFinite(explicitRun) && explicitRun > 0) {
    startLiveRunPolling(explicitRun);
  }
  await Promise.all([loadProject(), loadSpec(), loadRuns(), loadInterview()]);
  loading.value = false;
}

async function onExtract(): Promise<void> {
  if (!project.value) return;
  try {
    await ElMessageBox.confirm(
      hasSpec.value
        ? '将再次调用需求模型重新生成草稿，可能产生新的调用费用。是否继续？'
        : '将调用需求模型生成结构化草稿，可能产生调用费用。是否继续？',
      '生成需求草稿',
      { confirmButtonText: '继续', cancelButtonText: '取消', type: 'warning' },
    );
  } catch {
    return;
  }

  extracting.value = true;
  specMessage.value = '';
  try {
    const version = spec.value?.version ?? 0;
    applySpec(await projectsApi.extract(projectId.value, version));
    specLoaded.value = true;
    await loadInterview();
    ElMessage.success('已生成需求草稿');
  } catch (error) {
    if (await handleAuthError(error)) return;
    specMessage.value = describeError(error).message;
    if (error instanceof ApiError && error.code === 'SPEC_CONFLICT') {
      await loadSpec();
    }
  } finally {
    extracting.value = false;
  }
}

async function onStartInterview(): Promise<void> {
  if (typeof interviewApi?.submit !== 'function') return;
  interviewBusy.value = true;
  interviewError.value = '';
  try {
    const data = await interviewApi.submit(projectId.value, []);
    interview.value = data;
    ensureInterviewDrafts(data);
    ElMessage.success('已生成澄清反问问题');
  } catch (error) {
    interviewError.value = describeError(error).message;
  } finally {
    interviewBusy.value = false;
  }
}

function chooseSingleAnswer(questionId: string, option: string): void {
  interviewDrafts[questionId].selected = [option];
}

function buildInterviewAnswers(): InterviewAnswer[] {
  return (interview.value?.questions ?? []).map((q) => {
    const d = interviewDrafts[q.question_id];
    const answer: InterviewAnswer = { question_id: q.question_id, question_version: q.version };
    if (d.unsure) {
      answer.unsure = true;
      return answer;
    }
    if (q.type === 'text') {
      if (d.custom.trim()) answer.custom = d.custom.trim();
    } else {
      if (d.selected.length) answer.selected = [...d.selected];
      if (q.allow_custom && d.custom.trim()) answer.custom = d.custom.trim();
    }
    return answer;
  });
}

async function onSubmitInterview(): Promise<void> {
  if (typeof interviewApi?.submit !== 'function') return;
  interviewBusy.value = true;
  interviewError.value = '';
  try {
    const data = await interviewApi.submit(projectId.value, buildInterviewAnswers());
    interview.value = data;
    ensureInterviewDrafts(data);
    await loadSpec();
    ElMessage.success('已提交本轮回答并同步规格草稿');
  } catch (error) {
    interviewError.value = describeError(error).message;
  } finally {
    interviewBusy.value = false;
  }
}

async function onSave(): Promise<void> {
  if (!spec.value) return;
  saving.value = true;
  specMessage.value = '';
  try {
    applySpec(
      await projectsApi.saveSpec(projectId.value, spec.value.version, spec.value.draft_revision, draft),
    );
    ElMessage.success('草稿已保存');
  } catch (error) {
    if (await handleAuthError(error)) return;
    const info = describeError(error);
    specMessage.value = info.retryable ? `${info.message} 请重新读取后再试。` : info.message;
    if (error instanceof ApiError && error.code === 'SPEC_CONFLICT') {
      await loadSpec();
    }
  } finally {
    saving.value = false;
  }
}

async function onSendComposerMessage(): Promise<void> {
  const text = composerInput.value.trim();
  if (!text || composerBusy.value) return;

  if (readOnly.value) return;

  composerInput.value = '';
  appendChatTurn('user', text);

  // 场景 1：存在等待作答的文本题或支持补充说明的反问题目，直接填入并提交本轮回答
  if (interview.value?.waiting && interview.value.questions.length > 0) {
    const targetQ =
      interview.value.questions.find(
        (q) => q.type === 'text' && !interviewDrafts[q.question_id]?.custom.trim(),
      ) ??
      interview.value.questions.find(
        (q) => q.allow_custom && !interviewDrafts[q.question_id]?.custom.trim(),
      );
    if (targetQ) {
      interviewDrafts[targetQ.question_id].custom = text;
      interviewDrafts[targetQ.question_id].unsure = false;
      composerBusy.value = true;
      try {
        await onSubmitInterview();
        appendChatTurn('assistant', '已将你的补充回答提交到本轮需求澄清，并同步更新规格草稿。', 'spec');
      } finally {
        composerBusy.value = false;
      }
      return;
    }
  }

  // 场景 2：已有可编辑草稿，将对话补充记录为明确业务规则并持久化保存
  if (spec.value && !readOnly.value) {
    const nextRuleId = `BR${String(draft.business_rules.length + 1).padStart(2, '0')}`;
    draft.business_rules.push({
      id: nextRuleId,
      text,
      source: 'user_explicit',
    });
    composerBusy.value = true;
    try {
      await onSave();
      if (!specMessage.value) {
        appendChatTurn(
          'assistant',
          `已记录补充规则 ${nextRuleId} 并保存至规格草稿，可随时在右侧面板核对规格与设计接口。`,
          'spec',
        );
      }
    } finally {
      composerBusy.value = false;
    }
    return;
  }

  // 场景 3：尚未生成草稿
  appendChatTurn(
    'assistant',
    '已记下你的补充说明。点击下方「生成需求草稿」即可开始 AI 结构化拆解与多轮反问。',
  );
}

function onComposerKeydown(event: KeyboardEvent): void {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    void onSendComposerMessage();
  }
}

async function onConfirm(): Promise<void> {
  if (!spec.value) return;
  confirming.value = true;
  specMessage.value = '';
  try {
    await projectsApi.confirm(projectId.value, spec.value.version, spec.value.draft_revision);
    ElMessage.success('规格已确认并冻结');
    await Promise.all([loadSpec(), loadProject(), refreshProjects()]);
    centerView.value = 'flowchart';
  } catch (error) {
    if (await handleAuthError(error)) return;
    specMessage.value = describeError(error).message;
    if (error instanceof ApiError && error.code === 'SPEC_CONFLICT') {
      await loadSpec();
    }
  } finally {
    confirming.value = false;
  }
}

async function onGenerate(): Promise<void> {
  if (!spec.value || !project.value) return;
  generating.value = true;
  specMessage.value = '';
  if (idempotencyKey.value === null) {
    idempotencyKey.value = newIdempotencyKey();
  }
  try {
    const run = await runsApi.start(
      projectId.value,
      spec.value.spec_id,
      idempotencyKey.value,
      runMode.value,
    );
    idempotencyKey.value = null;
    ElMessage.success('已提交生成运行');
    void refreshProjectRuns(projectId.value);
    await router.push({ name: 'run-detail', params: { id: String(run.id) } });
  } catch (error) {
    if (await handleAuthError(error)) return;
    specMessage.value = describeError(error).message;
  } finally {
    generating.value = false;
  }
}

function addEntity(): void {
  draft.entities.push({
    name: `Entity${draft.entities.length + 1}`,
    source: 'user_explicit',
    fields: [{ name: 'id', type: 'long', required: true }],
  });
}
function removeEntity(entityIndex: number): void {
  draft.entities.splice(entityIndex, 1);
}
function addPage(): void {
  const firstEntity = draft.entities[0]?.name ?? 'Task';
  draft.pages.push({
    name: `Page${draft.pages.length + 1}`,
    entity: firstEntity,
    path: `/page-${draft.pages.length + 1}`,
  });
}
function removePage(pageIndex: number): void {
  draft.pages.splice(pageIndex, 1);
}
function runStatusTagType(status: string): 'success' | 'danger' | 'warning' | 'info' | 'primary' {
  if (status === 'SUCCEEDED') return 'success';
  if (status === 'FAILED') return 'danger';
  if (status === 'PAUSED_BUDGET') return 'warning';
  if (status === 'CANCELLED') return 'info';
  return 'primary';
}
function addField(entityIndex: number): void {
  draft.entities[entityIndex].fields.push({ name: '', type: 'string', required: false });
}
function removeField(entityIndex: number, fieldIndex: number): void {
  draft.entities[entityIndex].fields.splice(fieldIndex, 1);
}
function addPermission(): void {
  draft.permissions.push({ role: 'MEMBER', actions: ['read'], source: 'pending' });
}
function removePermission(index: number): void {
  draft.permissions.splice(index, 1);
}
function addRule(): void {
  draft.business_rules.push({
    id: `BR${String(draft.business_rules.length + 1).padStart(2, '0')}`,
    text: '',
    source: 'user_explicit',
  });
}
function removeRule(index: number): void {
  draft.business_rules.splice(index, 1);
}
function addCase(): void {
  draft.acceptance_cases.push({
    id: `AC${String(draft.acceptance_cases.length + 1).padStart(2, '0')}`,
    scenario: '',
    expected: '',
    source: 'user_explicit',
  });
}
function removeCase(index: number): void {
  draft.acceptance_cases.splice(index, 1);
}
function onFlowNodeSelect(nodeId: string, moduleHint?: string): void {
  selectedNode.value = nodeId;
  selectedModule.value = moduleHint ?? null;
  if (moduleHint) {
    const hasOpenDefect = (liveFactoryData.value?.open_defects ?? []).some(
      (d) => d.dept_id === moduleHint,
    );
    openInspectorTab(hasOpenDefect ? 'verify' : 'apis');
  } else if (nodeId === 'verify' || nodeId === 'package' || nodeId === 'e2e' || nodeId === 'delivery' || nodeId === 'be-verify') {
    openInspectorTab('verify');
  } else {
    openInspectorTab('apis');
  }
}

watch(() => props.id, loadAll, { immediate: true });
</script>

<template>
  <section v-loading="loading" class="pd-stage">
    <!-- Center Stage Sticky Sub-header -->
    <div class="stage-bar">
      <div class="stage-bar-left">
        <div class="stage-switch">
          <button
            type="button"
            class="seg-pill"
            :class="{ active: centerView === 'chat' }"
            data-testid="center-tab-chat"
            @click="centerView = 'chat'"
          >
            对话反问流
          </button>
          <button
            type="button"
            class="seg-pill"
            :class="{ active: centerView === 'flowchart' }"
            data-testid="center-tab-flowchart"
            @click="centerView = 'flowchart'"
          >
            工厂组装流程图
          </button>
        </div>

        <div class="stage-title-wrap">
          <h1 class="stage-title">{{ project?.name ?? '项目详情' }}</h1>
          <el-tag
            v-if="project"
            :type="project.active_spec_id === null ? 'info' : 'success'"
            size="small"
            disable-transitions
          >
            {{ project.active_spec_id === null ? '需求草稿' : '已冻结规格' }}
          </el-tag>
        </div>
      </div>

      <!-- Always visible on the right of center top bar (both chat & flowchart) -->
      <div class="stage-bar-right">
        <StandardAssemblyTodo
          :run="liveRunDetail"
          :factory-data="liveFactoryData"
          :spec="spec"
          :selected-node="selectedNode"
          @select-node="onFlowNodeSelect"
        />
      </div>
    </div>

    <!-- Scrollable Center Body -->
    <div ref="chatScrollRef" class="stage-scroll af-scroll">
      <div class="stage-inner">
        <el-alert
          v-if="errorMessage"
          :title="errorMessage"
          type="error"
          :closable="false"
          show-icon
          class="block-gap"
        />
        <el-alert
          v-if="specMessage"
          :title="specMessage"
          type="error"
          :closable="false"
          show-icon
          class="block-gap"
        />

        <!-- ================= CENTER MODE A: CONVERSATIONAL FLOW ================= -->
        <div v-show="centerView === 'chat'" class="chat-stream" data-testid="conversation-stage">
          <!-- User Initial Requirement Bubble -->
          <div v-if="project" class="user-row">
            <div class="bubble-user">
              <div class="bubble-meta">
                <span class="bubble-author">需求原文</span>
                <span class="af-mono">{{ formatDateTime(project.created_at) }}</span>
              </div>
              <SafeText :text="project.requirement" />
            </div>
          </div>

          <!-- Assistant Extraction Summary Bubble -->
          <div class="assistant-row">
            <div class="assistant-avatar" aria-hidden="true">AF</div>
            <div class="assistant-col">
              <div class="assistant-head">
                <span class="assistant-name">AgentForge 需求分析助手</span>
                <span v-if="spec" class="meta-chip af-mono">
                  v{{ spec.version }} · r{{ spec.draft_revision }}
                </span>
                <div v-if="!readOnly" class="assistant-actions">
                  <el-button
                    v-if="hasSpec && !spec?.confirmed"
                    size="small"
                    data-testid="open-interview"
                    @click="router.push({ name: 'project-interview', params: { id: String(projectId) } })"
                  >
                    独立访谈页
                  </el-button>
                  <el-button
                    size="small"
                    type="primary"
                    :loading="extracting"
                    data-testid="extract-spec-btn"
                    @click="onExtract"
                  >
                    {{ hasSpec ? '重新生成草稿' : '生成需求草稿' }}
                  </el-button>
                </div>
              </div>

              <div class="bubble-assistant">
                <div v-if="specLoaded && !hasSpec" class="assistant-empty">
                  点击右上角「生成需求草稿」开始拆解需求。
                </div>

                <template v-if="spec">
                  <div class="draft-meta">
                    <el-tag size="small" :type="spec.confirmed ? 'success' : 'warning'" disable-transitions>
                      {{ spec.confirmed ? '已确认并冻结' : '未确认' }}
                    </el-tag>
                    <span class="af-mono draft-hash">hash {{ spec.hash.slice(0, 12) }}</span>
                    <span v-if="spec.confirmed_at" class="af-mono draft-hash">
                      · 确认于 {{ formatDateTime(spec.confirmed_at) }}
                    </span>
                    <button
                      type="button"
                      class="inline-link"
                      @click="openInspectorTab('spec')"
                    >
                      在右侧核对规格
                    </button>
                    <button
                      type="button"
                      class="inline-link"
                      @click="openInspectorTab('apis')"
                    >
                      查看设计接口
                    </button>
                  </div>

                  <div class="kpi-row">
                    <div class="kpi-box">
                      <span class="kpi-k">业务实体</span>
                      <span class="kpi-v af-mono">{{ draft.entities.length }} 个</span>
                    </div>
                    <div class="kpi-box">
                      <span class="kpi-k">业务规则</span>
                      <span class="kpi-v af-mono">{{ draft.business_rules.length }} 条</span>
                    </div>
                    <div class="kpi-box">
                      <span class="kpi-k">待确认项</span>
                      <span class="kpi-v af-mono" :class="{ warn: spec.pending.length > 0 }">
                        {{ spec.pending.length }} 项
                      </span>
                    </div>
                  </div>

                  <SafeText v-if="spec.summary" :text="`模型摘要：${spec.summary}`" class="summary-text" />

                  <el-alert
                    v-if="spec.pending.length"
                    type="warning"
                    :closable="false"
                    show-icon
                    class="block-gap-sm"
                  >
                    <template #title>以下为模型推断，仍未确认（确认前需逐项处理）</template>
                    <div class="tag-row">
                      <el-tag v-for="item in spec.pending" :key="item" type="warning" size="small" disable-transitions>
                        {{ item }}
                      </el-tag>
                    </div>
                  </el-alert>

                  <el-alert v-if="spec.issues.length" type="info" :closable="false" show-icon class="block-gap-sm">
                    <template #title>确认前需解决的校验项</template>
                    <ul class="bullet-list">
                      <li v-for="(item, index) in spec.issues" :key="`issue-${index}`">{{ item }}</li>
                    </ul>
                  </el-alert>

                  <el-alert
                    v-if="spec.missing_fields.length"
                    type="warning"
                    :closable="false"
                    show-icon
                    class="block-gap-sm"
                  >
                    <template #title>模型认为仍需补充的信息</template>
                    <ul class="bullet-list">
                      <li v-for="(item, index) in spec.missing_fields" :key="`missing-${index}`">{{ item }}</li>
                    </ul>
                  </el-alert>

                  <el-alert
                    v-if="spec.questions.length"
                    type="warning"
                    :closable="false"
                    show-icon
                    class="block-gap-sm"
                  >
                    <template #title>模型建议确认的问题</template>
                    <ul class="bullet-list">
                      <li v-for="(item, index) in spec.questions" :key="`question-${index}`">{{ item }}</li>
                    </ul>
                  </el-alert>
                </template>
              </div>

              <!-- Embedded R02/R05 Conversational Clarification Card -->
              <div
                v-if="hasSpec && !spec?.confirmed && interview"
                class="interview-thread"
                data-testid="inline-interview-card"
              >
                <div class="interview-head">
                  <div class="interview-head-left">
                    <span class="interview-dot" />
                    <span class="interview-title">第 {{ interview.round }} 轮需求澄清</span>
                  </div>
                  <el-tag
                    size="small"
                    :type="interview.status === 'READY_TO_CONFIRM' ? 'success' : interview.status === 'NEEDS_INPUT' ? 'warning' : 'info'"
                    disable-transitions
                  >
                    {{ interview.status }}
                  </el-tag>
                </div>

                <div class="interview-body">
                  <el-alert
                    v-if="interviewError"
                    :title="interviewError"
                    type="error"
                    :closable="false"
                    show-icon
                  />

                  <div v-if="interview.status === 'NEW'" class="interview-start-bar">
                    <span>点击开始，由 AI 反问补全关键细节。</span>
                    <el-button type="primary" size="small" :loading="interviewBusy" @click="onStartInterview">
                      开始澄清
                    </el-button>
                  </div>

                  <div v-for="(q, idx) in interview.questions" :key="q.question_id" class="q-card">
                    <div class="q-prompt-row">
                      <span class="q-index af-mono">Q{{ idx + 1 }}</span>
                      <SafeText :text="q.prompt" class="q-prompt" />
                    </div>
                    <div class="q-options">
                      <template v-if="q.type === 'single_choice'">
                        <label v-for="opt in q.options" :key="opt" class="q-opt">
                          <input
                            type="radio"
                            :name="`inline-${q.question_id}`"
                            :value="opt"
                            :checked="interviewDrafts[q.question_id]?.selected[0] === opt"
                            @change="chooseSingleAnswer(q.question_id, opt)"
                          />
                          <span>{{ opt }}</span>
                        </label>
                      </template>
                      <template v-else-if="q.type === 'multi_choice'">
                        <label v-for="opt in q.options" :key="opt" class="q-opt">
                          <input v-model="interviewDrafts[q.question_id].selected" type="checkbox" :value="opt" />
                          <span>{{ opt }}</span>
                        </label>
                      </template>
                      <input
                        v-else
                        v-model="interviewDrafts[q.question_id].custom"
                        type="text"
                        class="q-input"
                        placeholder="请输入你的回答..."
                      />
                    </div>
                    <div class="q-footer">
                      <input
                        v-if="q.allow_custom && q.type !== 'text'"
                        v-model="interviewDrafts[q.question_id].custom"
                        type="text"
                        class="q-input"
                        placeholder="其他补充说明（可选）"
                      />
                      <label class="q-unsure">
                        <input v-model="interviewDrafts[q.question_id].unsure" type="checkbox" />
                        <span>不确定</span>
                      </label>
                    </div>
                  </div>

                  <div v-if="interview.waiting" class="interview-submit-row">
                    <el-button type="primary" size="small" :loading="interviewBusy" @click="onSubmitInterview">
                      提交回答
                    </el-button>
                  </div>

                  <el-alert
                    v-if="interview.status === 'READY_TO_CONFIRM'"
                    type="success"
                    :closable="false"
                    show-icon
                  >
                    <template #title>澄清完成，可以确认并生成代码。</template>
                  </el-alert>
                </div>
              </div>

              <!-- Ready-to-Freeze Banner -->
              <div v-if="canConfirm" class="ready-freeze-card" data-testid="ready-freeze-card">
                <div class="ready-head">
                  <span class="ready-title">需求已就绪，可确认冻结</span>
                  <button
                    type="button"
                    class="ready-btn-primary"
                    :disabled="confirming"
                    @click="onConfirm"
                  >
                    {{ confirming ? '冻结中...' : '确认冻结' }}
                  </button>
                </div>
              </div>
            </div>
          </div>

          <!-- Incremental User & Assistant Turns from Bottom Composer -->
          <template v-for="turn in chatTurns" :key="turn.id">
            <div v-if="turn.role === 'user'" class="user-row">
              <div class="bubble-user">
                <div class="bubble-meta">
                  <span class="bubble-author">需求补充</span>
                  <span class="af-mono">{{ turn.time }}</span>
                </div>
                <SafeText :text="turn.text" />
              </div>
            </div>
            <div v-else class="assistant-row">
              <div class="assistant-avatar" aria-hidden="true">AF</div>
              <div class="assistant-col">
                <div class="bubble-assistant compact-reply">
                  <SafeText :text="turn.text" />
                  <button
                    v-if="turn.actionTab"
                    type="button"
                    class="inline-link"
                    @click="openInspectorTab(turn.actionTab)"
                  >
                    打开右侧面板核对 →
                  </button>
                </div>
              </div>
            </div>
          </template>
        </div>

        <!-- ================= CENTER MODE B: LIVE & STANDARD ASSEMBLY FLOWCHART ================= -->
        <div v-show="centerView === 'flowchart'" class="flowchart-stage" data-testid="frozen-assembly-stage">
          <FactoryFlowchart
            :run="liveRunDetail"
            :factory-data="liveFactoryData"
            :events="liveRunEvents"
            :spec="spec"
            :run-mode="runMode"
            :selected-node="selectedNode"
            :preview-only="!liveRunDetail"
            @select-node="onFlowNodeSelect"
          />
        </div>
      </div>
    </div>

    <!-- ================= STICKY BOTTOM CHAT COMPOSER & ACTION DOCK ================= -->
    <div v-if="(centerView === 'chat' && !readOnly) || spec" class="composer-dock" data-testid="chat-composer">
      <div class="composer-inner">
        <!-- Bottom Conversational Input Box (hidden once spec is frozen / dialogue finished) -->
        <div v-if="centerView === 'chat' && !readOnly" class="composer-box" data-testid="chat-composer-box">
          <textarea
            v-model="composerInput"
            rows="2"
            class="composer-textarea"
            :placeholder="composerPlaceholder"
            :disabled="composerBusy"
            data-testid="chat-composer-input"
            @keydown="onComposerKeydown"
          />
          <div class="composer-bar">
            <div class="composer-bar-left">
              <button
                v-if="!hasSpec"
                type="button"
                class="chip-btn"
                :disabled="extracting"
                title="生成需求草稿"
                @click="onExtract"
              >
                生成草稿
              </button>
            </div>
            <button
              type="button"
              class="send-icon-btn"
              :disabled="!composerInput.trim() || composerBusy"
              :title="composerBusy ? '同步中...' : '发送 (Enter)'"
              aria-label="发送补充"
              data-testid="chat-composer-send"
              @click="onSendComposerMessage"
            >
              <svg class="send-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                <line x1="12" y1="19" x2="12" y2="5" />
                <polyline points="5 12 12 5 19 12" />
              </svg>
            </button>
          </div>
        </div>

        <!-- Run Mode & Freeze / Generate Action Controls -->
        <div v-if="spec" class="dock-controls">
          <div class="run-mode-box" data-testid="run-mode-selector">
            <div class="run-mode-header">
              <span class="run-mode-label">运行模式</span>
              <el-radio-group v-model="runMode" size="small">
                <el-radio-button value="standard" data-testid="mode-standard">标准装配模式</el-radio-button>
                <el-radio-button value="factory" data-testid="mode-factory">异步工厂模式</el-radio-button>
              </el-radio-group>
            </div>
            <p class="run-mode-hint" data-testid="run-mode-hint">
              <template v-if="runMode === 'factory'">
                异步工厂模式：由总管拆分并调度多部门并发开发与自动化返工修复；建议模块数 ≤ 8、业务实体 ≤ 25（超过软上限将提示拆分；支付等第三方能力运行于本地沙箱模拟模式）。
              </template>
              <template v-else>
                标准装配模式：适用于 1–3 个业务实体的快速确定性装配与构建验收（默认模式）。
              </template>
            </p>
          </div>

          <div class="action-bar">
            <div class="action-hint">
              <template v-if="spec.confirmed">
                <template v-if="!reopenEditing">
                  规格已确认并冻结（hash {{ spec.hash.slice(0, 12) }}）。确认后不可原地修改，如需调整请创建新版本草稿。
                </template>
                <template v-else>正在基于已确认版本编辑，保存后会产生新的未确认草稿版本。</template>
              </template>
              <template v-else-if="isDirty">有未保存的修改，请先保存后再确认。</template>
              <template v-else-if="spec.pending.length || spec.issues.length">
                仍有待确认项或校验未通过，处理并保存后才可确认。
              </template>
              <template v-else>内容已就绪，可以确认并生成代码。</template>
            </div>
            <div class="action-buttons">
              <el-button
                v-if="spec.confirmed && !reopenEditing"
                size="small"
                :loading="saving"
                @click="reopenEditing = true; centerView = 'chat'; openInspectorTab('spec')"
              >
                创建新版本草稿以修改
              </el-button>
              <el-button
                v-if="!spec.confirmed"
                size="small"
                :disabled="readOnly || !isDirty"
                :loading="saving"
                @click="onSave"
              >
                保存修改
              </el-button>
              <el-button
                v-else-if="reopenEditing"
                size="small"
                :loading="saving"
                @click="onSave"
              >
                保存为新草稿版本
              </el-button>
              <el-button
                v-if="!spec.confirmed"
                size="small"
                type="primary"
                :disabled="!canConfirm"
                :loading="confirming"
                @click="onConfirm"
              >
                确认规格
              </el-button>
              <el-button
                size="small"
                type="success"
                :disabled="!canGenerate"
                :loading="generating"
                @click="onGenerate"
              >
                开始生成
              </el-button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- ================= RIGHT SIDEBAR INSPECTOR (Fixed Top Tab Bar + Scrollable Body) ================= -->
    <Teleport to="#workbench-inspector-host" :disabled="!inspectorHostReady">
      <div class="inspector-root" data-testid="project-inspector">
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
              事件流 ({{ liveRunEvents.length }})
            </button>
          </div>
        </div>

        <div class="inspector-body af-scroll" data-testid="inspector-body">
        <!-- TAB 1: DESIGNED APIs -->
        <div v-show="rightTab === 'apis'" class="ins-pane">
          <DesignedApisPanel
            :spec="hasSpec ? draft : null"
            :blueprints="liveFactoryData?.blueprints ?? []"
            :selected-module="selectedModule"
          />
        </div>

        <!-- TAB 2: EDITABLE STRUCTURED SPEC (Lazy-mounted when opened to avoid heavy form jank) -->
        <div v-if="specTabMounted" v-show="rightTab === 'spec'" class="ins-pane">
          <div v-if="!spec" class="ins-empty">
            暂无需求规格，请在中间对话区点击「生成需求草稿」。
          </div>
          <template v-else>
            <div class="spec-block">
              <h3 class="section-title">摘要 / 目标</h3>
              <el-input
                v-model="draft.goal"
                type="textarea"
                :rows="3"
                :disabled="readOnly"
                placeholder="用一句话说明这个系统要达到的目标"
                data-testid="goal-input"
              />
            </div>

            <div class="spec-block">
              <h3 class="section-title">角色</h3>
              <StringListEditor v-model="draft.actors" :readonly="readOnly" placeholder="例如 MEMBER / ADMIN" />
            </div>

            <div class="spec-block">
              <div class="spec-block-head">
                <h3 class="section-title">业务实体 ({{ draft.entities.length }})</h3>
                <el-button
                  v-if="!readOnly"
                  size="small"
                  type="primary"
                  plain
                  data-testid="add-entity-btn"
                  @click="addEntity"
                >
                  添加业务实体
                </el-button>
              </div>
              <el-empty v-if="!draft.entities.length" description="至少添加一个业务实体" :image-size="48" />
              <div v-for="(entity, entityIndex) in draft.entities" :key="entityIndex" class="entity-block">
                <div class="entity-head">
                  <el-input v-model="entity.name" :disabled="readOnly" size="small" class="entity-name" placeholder="实体名（PascalCase）" />
                  <ConfirmToggle v-model="entity.source" :readonly="readOnly" />
                  <el-button
                    v-if="!readOnly"
                    text
                    size="small"
                    type="danger"
                    :data-testid="`remove-entity-${entityIndex}`"
                    @click="removeEntity(entityIndex)"
                  >
                    删除
                  </el-button>
                </div>
                <el-table :data="entity.fields" size="small" border>
                  <el-table-column label="字段">
                    <template #default="{ row }">
                      <el-input v-model="row.name" :disabled="readOnly" size="small" />
                    </template>
                  </el-table-column>
                  <el-table-column label="类型" width="110">
                    <template #default="{ row }">
                      <select
                        v-model="row.type"
                        :disabled="readOnly"
                        class="field-type-select af-mono"
                      >
                        <option v-for="type in FIELD_TYPES" :key="type" :value="type">
                          {{ type }}
                        </option>
                      </select>
                    </template>
                  </el-table-column>
                  <el-table-column label="必填" width="52">
                    <template #default="{ row }">
                      <el-checkbox v-model="row.required" :disabled="readOnly" />
                    </template>
                  </el-table-column>
                  <el-table-column v-if="!readOnly" label="" width="56">
                    <template #default="{ $index }">
                      <el-button text size="small" type="danger" @click="removeField(entityIndex, $index)">删</el-button>
                    </template>
                  </el-table-column>
                </el-table>
                <el-button v-if="!readOnly" size="small" class="block-gap-sm" @click="addField(entityIndex)">
                  添加字段
                </el-button>
              </div>
            </div>

            <div class="spec-block">
              <div class="spec-block-head">
                <h3 class="section-title">业务页面</h3>
                <el-button v-if="!readOnly" size="small" data-testid="add-page-btn" @click="addPage">添加业务页面</el-button>
              </div>
              <div v-for="(page, pageIndex) in draft.pages" :key="pageIndex" class="perm-row">
                <el-input v-model="page.name" :disabled="readOnly" size="small" placeholder="页面名" />
                <el-input v-model="page.entity" :disabled="readOnly" size="small" placeholder="关联实体" />
                <el-input v-model="page.path" :disabled="readOnly" size="small" placeholder="/path" />
                <el-button v-if="!readOnly" text size="small" type="danger" @click="removePage(pageIndex)">删除</el-button>
              </div>
            </div>

            <div class="spec-block">
              <div class="spec-block-head">
                <h3 class="section-title">角色权限</h3>
                <el-button v-if="!readOnly" size="small" @click="addPermission">添加权限</el-button>
              </div>
              <div v-for="(permission, index) in draft.permissions" :key="index" class="perm-card">
                <div class="perm-row">
                  <el-select
                    v-model="permission.role"
                    :disabled="readOnly"
                    size="small"
                    filterable
                    allow-create
                    default-first-option
                    class="perm-role"
                  >
                    <el-option v-for="role in ROLE_OPTIONS" :key="role" :label="role" :value="role" />
                  </el-select>
                  <ConfirmToggle v-model="permission.source" :readonly="readOnly" />
                  <el-button v-if="!readOnly" text size="small" type="danger" @click="removePermission(index)">删除</el-button>
                </div>
                <el-checkbox-group v-model="permission.actions" :disabled="readOnly" size="small">
                  <el-checkbox v-for="action in ACTION_OPTIONS" :key="action" :value="action">
                    {{ action }}
                  </el-checkbox>
                </el-checkbox-group>
              </div>
            </div>

            <div class="spec-block">
              <div class="spec-block-head">
                <h3 class="section-title">业务规则</h3>
                <el-button v-if="!readOnly" size="small" @click="addRule">添加规则</el-button>
              </div>
              <div v-for="(rule, index) in draft.business_rules" :key="index" class="rule-edit">
                <div class="rule-top">
                  <span class="rule-id af-mono">{{ rule.id }}</span>
                  <ConfirmToggle v-model="rule.source" :readonly="readOnly" />
                  <el-button v-if="!readOnly" text size="small" type="danger" @click="removeRule(index)">删除</el-button>
                </div>
                <el-input v-model="rule.text" type="textarea" :rows="2" size="small" :disabled="readOnly" />
              </div>
            </div>

            <div class="spec-block">
              <div class="spec-block-head">
                <h3 class="section-title">验收场景</h3>
                <el-button v-if="!readOnly" size="small" @click="addCase">添加场景</el-button>
              </div>
              <div v-for="(item, index) in draft.acceptance_cases" :key="index" class="rule-edit">
                <div class="rule-top">
                  <span class="rule-id af-mono">{{ item.id }}</span>
                  <ConfirmToggle v-model="item.source" :readonly="readOnly" />
                  <el-button v-if="!readOnly" text size="small" type="danger" @click="removeCase(index)">删除</el-button>
                </div>
                <el-input v-model="item.scenario" size="small" :disabled="readOnly" placeholder="场景" />
                <el-input v-model="item.expected" size="small" :disabled="readOnly" placeholder="预期结果" />
              </div>
            </div>

            <div class="spec-block">
              <h3 class="section-title">约束</h3>
              <StringListEditor v-model="draft.constraints" :readonly="readOnly" />
            </div>

            <div class="spec-block">
              <h3 class="section-title">明确不做</h3>
              <StringListEditor v-model="draft.exclusions" :readonly="readOnly" />
            </div>

            <div class="spec-block">
              <h3 class="section-title">技术方案</h3>
              <p class="fixed-profile af-mono">
                固定技术方案（MVP 暂不可切换）：{{ spec.spec.technical_profile.profile_id }}
                @{{ spec.spec.technical_profile.profile_version }} ·
                模板 {{ spec.spec.technical_profile.template_version }}
              </p>
            </div>
          </template>
        </div>

        <!-- TAB 3: VERIFICATION & DELIVERY (Always available regardless of center tab) -->
        <div v-show="rightTab === 'verify'" class="ins-pane">
          <RunVerificationPanel :run="liveRunDetail" :factory-data="liveFactoryData" />
        </div>

        <!-- TAB 4: INCREMENTAL RUN EVENTS (Always available regardless of center tab) -->
        <div v-show="rightTab === 'events'" class="ins-pane">
          <RunEventsPanel :events="liveRunEvents" :run-id="liveRunDetail?.id ?? selectedRunId" />
        </div>

        <!-- Standalone mount fallback for legacy tests when left sidebar is absent -->
        <div v-if="!inspectorHostReady && runs.length" class="runs-card" data-testid="project-runs-history">
          <h3 class="section-title">历史运行记录</h3>
          <el-table :data="runs" size="small" border>
            <el-table-column label="ID" width="64">
              <template #default="{ row }">
                <span class="af-mono">#{{ row.id }}</span>
              </template>
            </el-table-column>
            <el-table-column label="状态" min-width="96">
              <template #default="{ row }">
                <el-tag :type="runStatusTagType(row.status)" size="small" disable-transitions>
                  {{ row.status }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="74">
              <template #default="{ row }">
                <el-button
                  text
                  size="small"
                  type="primary"
                  :data-testid="`open-run-${row.id}`"
                  @click="router.push({ name: 'run-detail', params: { id: String(row.id) } })"
                >
                  查看
                </el-button>
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
.pd-stage {
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--af-bg);
}

.stage-bar {
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

.stage-bar-left {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}

.stage-title-wrap {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.stage-title {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 240px;
}

.stage-switch {
  display: inline-flex;
  gap: 2px;
  padding: 2px;
  border-radius: 7px;
  background: var(--af-bg);
  border: 1px solid var(--af-border-strong);
  flex-shrink: 0;
}

.seg-pill {
  border: none;
  background: transparent;
  color: var(--af-muted);
  font-size: 11.5px;
  font-weight: 500;
  padding: 4px 10px;
  border-radius: 5px;
  cursor: pointer;
}

.seg-pill.active {
  background: var(--af-btn-primary-bg);
  color: var(--af-btn-primary-fg);
}

.stage-bar-right {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.stage-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 18px 20px;
}

.stage-inner {
  max-width: 820px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.block-gap {
  margin-bottom: 4px;
}

.block-gap-sm {
  margin-top: 8px;
}

.chat-stream {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.user-row {
  display: flex;
  justify-content: flex-end;
}

.bubble-user {
  max-width: 85%;
  padding: 12px 15px;
  border-radius: 16px 4px 16px 16px;
  background: var(--af-accent-soft);
  border: 1px solid var(--af-accent-border);
  font-size: 13px;
  line-height: 1.6;
}

.bubble-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  font-size: 11px;
  color: var(--af-muted);
  margin-bottom: 5px;
}

.bubble-author {
  font-weight: 600;
  color: var(--af-fg);
}

.assistant-row {
  display: flex;
  align-items: flex-start;
  gap: 10px;
}

.assistant-col {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.assistant-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.assistant-avatar {
  width: 26px;
  height: 26px;
  border-radius: 7px;
  flex-shrink: 0;
  margin-top: 2px;
  background: var(--af-surface);
  border: 1px solid var(--af-border-strong);
  color: var(--af-accent);
  font-size: 11px;
  font-weight: 700;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.assistant-name {
  font-size: 12.5px;
  font-weight: 600;
}

.meta-chip {
  font-size: 11px;
  padding: 1px 7px;
  border-radius: 5px;
  background: var(--af-surface);
  border: 1px solid var(--af-border);
  color: var(--af-muted);
}

.assistant-actions {
  margin-left: auto;
  display: flex;
  gap: 8px;
}

.bubble-assistant {
  border-radius: 12px;
  border: 1px solid var(--af-border-strong);
  background: var(--af-surface);
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.compact-reply {
  padding: 11px 14px;
  font-size: 12.5px;
  line-height: 1.55;
  flex-direction: row;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
}

.assistant-empty {
  font-size: 12.5px;
  color: var(--af-muted);
  line-height: 1.55;
  padding: 4px 2px;
}

.draft-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.draft-hash {
  color: var(--af-muted);
  font-size: 11.5px;
}

.inline-link {
  border: none;
  background: transparent;
  color: var(--af-accent);
  font-size: 11.5px;
  cursor: pointer;
  padding: 0;
  text-decoration: underline;
}

.kpi-row {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
}

.kpi-box {
  border-radius: 8px;
  border: 1px solid var(--af-border);
  background: var(--af-surface-2);
  padding: 8px 10px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.kpi-k {
  font-size: 11px;
  color: var(--af-muted);
}

.kpi-v {
  font-size: 15px;
  font-weight: 600;
}

.kpi-v.warn {
  color: var(--af-warn);
}

.summary-text {
  font-size: 12.5px;
  color: var(--af-fg-secondary);
}

.tag-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
}

.bullet-list {
  margin: 6px 0 0;
  padding-left: 18px;
}

/* Interview Clarification Card (aligned with design prototype) */
.interview-thread {
  border-radius: 12px;
  border: 1px solid var(--af-accent-border);
  background: var(--af-surface);
  overflow: hidden;
}

.interview-head {
  padding: 9px 14px;
  border-bottom: 1px solid var(--af-border);
  background: var(--af-accent-soft);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.interview-head-left {
  display: flex;
  align-items: center;
  gap: 7px;
  flex-wrap: wrap;
}

.interview-dot {
  width: 6px;
  height: 6px;
  border-radius: 99px;
  background: var(--af-accent);
}

.interview-title {
  font-size: 12.5px;
  font-weight: 600;
}

.interview-sub {
  font-size: 11px;
  color: var(--af-muted);
}

.interview-body {
  padding: 13px 14px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.interview-start-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 12px;
  border-radius: 8px;
  background: var(--af-surface-2);
  font-size: 12px;
  color: var(--af-fg-secondary);
}

.q-card {
  border-radius: 9px;
  border: 1px solid var(--af-border);
  background: var(--af-surface-2);
  padding: 11px 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.q-prompt-row {
  display: flex;
  align-items: baseline;
  gap: 6px;
}

.q-index {
  font-size: 11.5px;
  font-weight: 600;
  color: var(--af-accent);
  flex-shrink: 0;
}

.q-prompt {
  font-size: 12.5px;
  font-weight: 500;
}

.q-options {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.q-opt {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
  padding: 6px 9px;
  border-radius: 7px;
  border: 1px solid var(--af-border);
  background: var(--af-surface);
  cursor: pointer;
}

.q-footer {
  display: flex;
  align-items: center;
  gap: 10px;
}

.q-input {
  flex: 1;
  height: 30px;
  padding: 0 9px;
  border-radius: 6px;
  border: 1px solid var(--af-border-strong);
  background: var(--af-bg);
  color: var(--af-fg);
  font-size: 12px;
  outline: none;
}

.q-unsure {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 11.5px;
  color: var(--af-muted);
  cursor: pointer;
  flex-shrink: 0;
}

.interview-submit-row {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 10px;
  padding-top: 8px;
  border-top: 1px solid var(--af-border);
}

/* Ready-to-Freeze Card */
.ready-freeze-card {
  border-radius: 10px;
  border: 1px solid var(--af-border-strong);
  background: var(--af-surface);
  padding: 11px 14px;
}

.ready-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.ready-title {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--af-fg);
}

.ready-btn-primary {
  height: 28px;
  padding: 0 12px;
  border-radius: 7px;
  border: 1px solid var(--af-btn-primary-bg);
  background: var(--af-btn-primary-bg);
  color: var(--af-btn-primary-fg);
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
}

.ready-btn-primary:hover:not(:disabled) {
  background: var(--af-btn-primary-hover);
}

.flowchart-stage {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

/* Sticky Bottom Composer & Action Dock */
.composer-dock {
  flex-shrink: 0;
  border-top: 1px solid var(--af-border);
  background: var(--af-surface);
  padding: 10px 20px 12px;
}

.composer-inner {
  max-width: 820px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 9px;
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

.composer-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.composer-bar-left {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.chip-btn {
  border: 1px solid var(--af-border-strong);
  background: var(--af-surface);
  color: var(--af-muted);
  font-size: 11px;
  padding: 3px 8px;
  border-radius: 6px;
  cursor: pointer;
}

.chip-btn:hover:not(:disabled) {
  border-color: var(--af-accent-border);
  color: var(--af-fg);
}

.send-icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  padding: 0;
  border-radius: 7px;
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
  width: 14px;
  height: 14px;
}

.dock-controls {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.run-mode-box {
  padding: 7px 10px;
  border-radius: 8px;
  background: var(--af-surface-2);
  border: 1px solid var(--af-border);
}

.run-mode-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
}

.run-mode-label {
  font-size: 12px;
  font-weight: 600;
}

.run-mode-hint {
  margin: 5px 0 0;
  font-size: 11px;
  color: var(--af-muted);
  line-height: 1.4;
}

.action-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
}

.action-hint {
  font-size: 11.5px;
  color: var(--af-muted);
  flex: 1;
  min-width: 180px;
}

.action-buttons {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

/* Right Inspector Styles: Sticky Fixed Top Bar + Independent Scroll Body */
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
  gap: 12px;
}

.ins-pane {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.ins-empty {
  padding: 20px 12px;
  border-radius: 9px;
  border: 1px dashed var(--af-border-strong);
  font-size: 12px;
  color: var(--af-muted);
  text-align: center;
}

.spec-block,
.runs-card {
  border-radius: 10px;
  border: 1px solid var(--af-border);
  background: var(--af-surface);
  padding: 11px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.spec-block-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.spec-block-head .section-title {
  margin: 0;
}

.entity-block {
  padding: 8px;
  border-radius: 8px;
  background: var(--af-surface-2);
  border: 1px solid var(--af-border);
}

.entity-head {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}

.entity-name {
  flex: 1;
  min-width: 110px;
}

.field-type-select {
  width: 100%;
  height: 24px;
  padding: 0 5px;
  border-radius: 4px;
  border: 1px solid var(--af-border-strong);
  background: var(--af-surface-2);
  color: var(--af-fg);
  font-size: 11px;
  outline: none;
}

.field-type-select:disabled {
  opacity: 0.6;
}

.perm-card {
  padding: 8px;
  border-radius: 8px;
  background: var(--af-surface-2);
  border: 1px solid var(--af-border);
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.perm-row {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.perm-role {
  width: 110px;
}

.rule-edit {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px;
  border-radius: 8px;
  background: var(--af-surface-2);
  border: 1px solid var(--af-border);
}

.rule-top {
  display: flex;
  align-items: center;
  gap: 6px;
}

.rule-id {
  font-size: 11.5px;
  font-weight: 600;
  color: var(--af-accent);
  margin-right: auto;
}

.fixed-profile {
  margin: 0;
  font-size: 11.5px;
  color: var(--af-muted);
  line-height: 1.5;
}
</style>
