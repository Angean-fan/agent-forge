<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';

import { ElMessage } from 'element-plus';

import { projectsApi } from '@/api/client';
import SafeText from '@/components/SafeText.vue';
import {
  refreshProjects,
  setActiveHeaderContext,
  setDefaultRunMode,
  useWorkspaceStore,
} from '@/stores/workspace';
import { describeError } from '@/utils/errors';

const router = useRouter();
const workspace = useWorkspaceStore();

const REQUIREMENT_MIN = 20;
const REQUIREMENT_MAX = 5000;

interface QuickPrompt {
  title: string;
  subtitle: string;
  name: string;
  requirement: string;
}

const QUICK_PROMPTS: QuickPrompt[] = [
  {
    title: '社区活动报名系统',
    subtitle: '角色权限 · 名额上限 · 状态流转',
    name: '社区活动报名系统',
    requirement:
      '我想做一个社区活动报名系统。社区管理员可以发布、编辑和下架活动，设置活动时间、地点和人数上限；社区居民可以浏览活动列表、在线报名和取消报名，人数满额后禁止继续报名，报名后能查看自己的报名状态。',
  },
  {
    title: '内部报销审批流',
    subtitle: '多级审批 · 报销单据 · 状态校验',
    name: '内部报销审批流',
    requirement:
      '需要一个企业内部费用报销审批系统。员工可以提交报销单（包含费用类型、金额、事由与发生日期），草稿支持修改或撤回；部门主管审批通过后流转财务复核，驳回需填写驳回原因，所有角色均可按状态筛选历史单据。',
  },
  {
    title: '多部门商城订单系统',
    subtitle: '商品库存 · 购物车 · 沙箱模拟支付',
    name: '多部门商城订单系统',
    requirement:
      '建设一个轻量商城订单管理系统。管理员维护商品分类、价格和库存并管理订单发货状态；普通用户可浏览商品、加入购物车、提交订单并在本地沙箱模拟支付，下单时校验库存充足并自动扣减库存。',
  },
];

const projectName = ref('');
const requirement = ref('');
const submittedUserText = ref('');
const submitting = ref(false);
const errorMessage = ref('');

const trimmedRequirement = computed(() => requirement.value.trim());
const remainingChars = computed(() => Math.max(0, REQUIREMENT_MIN - trimmedRequirement.value.length));
const canSubmit = computed(
  () =>
    !submitting.value &&
    trimmedRequirement.value.length >= REQUIREMENT_MIN &&
    trimmedRequirement.value.length <= REQUIREMENT_MAX,
);

function inferProjectName(text: string): string {
  const cleaned = text
    .replace(/^(我想做一个|我想做|请帮我做一个|请帮我开发一个|需要一个|建设一个|做一个|开发一个)/, '')
    .trim();
  const firstClause = cleaned.split(/[。！？!?\n，,；;]/)[0]?.trim() || cleaned;
  const candidate = firstClause.slice(0, 24).trim();
  return candidate || '未命名项目';
}

function applyQuickPrompt(item: QuickPrompt): void {
  projectName.value = item.name;
  requirement.value = item.requirement;
  errorMessage.value = '';
}

function toggleRunMode(): void {
  setDefaultRunMode(workspace.defaultRunMode === 'factory' ? 'standard' : 'factory');
}

async function onSubmit(): Promise<void> {
  if (submitting.value) return;
  const reqText = trimmedRequirement.value;
  if (reqText.length < REQUIREMENT_MIN) {
    errorMessage.value = `请再补充至少 ${REQUIREMENT_MIN - reqText.length} 个字符的需求描述（至少 ${REQUIREMENT_MIN} 字），以便准确拆解业务规格。`;
    return;
  }
  if (reqText.length > REQUIREMENT_MAX) {
    errorMessage.value = `需求描述不能超过 ${REQUIREMENT_MAX} 个字符。`;
    return;
  }

  const finalName = projectName.value.trim().slice(0, 60) || inferProjectName(reqText);
  submittedUserText.value = reqText;
  submitting.value = true;
  errorMessage.value = '';

  try {
    const project = await projectsApi.create(finalName, reqText);
    ElMessage.success('项目对话已创建');
    void refreshProjects();
    await router.push({ name: 'project-detail', params: { id: String(project.id) } });
  } catch (error) {
    errorMessage.value = describeError(error).message;
    submittedUserText.value = '';
  } finally {
    submitting.value = false;
  }
}

function onComposerKeydown(event: KeyboardEvent): void {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    void onSubmit();
  }
}

onMounted(() => {
  setActiveHeaderContext({
    projectId: null,
    projectName: '新建项目对话',
    stageLabel: '需求输入',
    stageTone: 'accent',
    cost: null,
  });
});
</script>

<template>
  <section class="chat-workspace" data-testid="create-chat-workspace">
    <!-- Top Sub-header -->
    <div class="stage-bar">
      <div class="stage-bar-left">
        <span class="stage-title">新建项目</span>
      </div>
      <div class="stage-bar-right">
        <button
          type="button"
          class="bar-icon-btn"
          title="返回项目列表"
          aria-label="返回项目列表"
          @click="router.push({ name: 'projects' })"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <line x1="19" y1="12" x2="5" y2="12" />
            <polyline points="12 19 5 12 12 5" />
          </svg>
        </button>
      </div>
    </div>

    <!-- Scrollable Conversation Feed -->
    <div class="chat-scroll af-scroll">
      <div class="chat-container">
        <!-- Assistant Welcome Message -->
        <div class="assistant-row">
          <div class="assistant-avatar" aria-hidden="true">AF</div>
          <div class="assistant-content">
            <div class="assistant-bubble">
              <p class="welcome-lead">
                用日常语言描述你想做的应用，或先选一个示例开始：
              </p>

              <div class="quick-prompts">
                <button
                  v-for="item in QUICK_PROMPTS"
                  :key="item.name"
                  type="button"
                  class="prompt-card"
                  :data-testid="`quick-prompt-${item.name}`"
                  @click="applyQuickPrompt(item)"
                >
                  <span class="prompt-title">{{ item.title }}</span>
                </button>
              </div>
            </div>
          </div>
        </div>

        <!-- User Sent Bubble (shown while submitting) -->
        <div v-if="submittedUserText" class="user-row">
          <div class="user-bubble">
            <div class="user-bubble-head">
              <span class="user-label">需求原文</span>
              <span class="af-mono user-sub">{{ projectName.trim() || inferProjectName(submittedUserText) }}</span>
            </div>
            <SafeText :text="submittedUserText" />
          </div>
        </div>

        <!-- Assistant Creating Feedback Bubble -->
        <div v-if="submitting" class="assistant-row">
          <div class="assistant-avatar" aria-hidden="true">AF</div>
          <div class="assistant-content">
            <div class="assistant-bubble creating-bubble">
              <span class="af-pulse-dot creating-dot" />
              <span>正在创建项目对话工作台，即将进入需求拆解与反问澄清...</span>
            </div>
          </div>
        </div>

        <el-alert
          v-if="errorMessage"
          :title="errorMessage"
          type="error"
          :closable="false"
          show-icon
          class="chat-error"
        />
      </div>
    </div>

    <!-- Sticky Bottom Chat Composer (aligned with agentforge-workspace-design.html) -->
    <div class="composer-dock" data-testid="chat-composer">
      <div class="composer-inner">
        <div class="composer-box">
          <div class="composer-name-row">
            <span class="name-prefix">项目名称</span>
            <input
              v-model="projectName"
              type="text"
              maxlength="60"
              class="name-input"
              placeholder="可选，留空将根据你的描述自动提炼（如：社区活动报名系统）"
              data-testid="create-project-name"
            />
          </div>

          <textarea
            v-model="requirement"
            rows="3"
            :maxlength="REQUIREMENT_MAX"
            class="composer-textarea"
            placeholder="描述你想做的系统：有哪些角色、各自能做什么操作、需要记录哪些信息、有哪些业务限制……（Enter 发送，Shift+Enter 换行）"
            data-testid="create-requirement-input"
            @keydown="onComposerKeydown"
          />

          <div class="composer-toolbar">
            <div class="toolbar-left">
              <button
                type="button"
                class="mode-chip af-mono"
                title="点击切换默认执行模式"
                @click="toggleRunMode"
              >
                模式: {{ workspace.defaultRunMode === 'factory' ? '异步工厂' : '标准装配' }}
              </button>
              <span class="char-hint af-mono" :class="{ muted: remainingChars === 0 }">
                <template v-if="remainingChars > 0">
                  还需 {{ remainingChars }} 字
                </template>
                <template v-else>
                  {{ trimmedRequirement.length }} / {{ REQUIREMENT_MAX }}
                </template>
              </span>
            </div>

            <div class="toolbar-right">
              <button
                type="button"
                class="send-icon-btn"
                :disabled="!canSubmit"
                :title="submitting ? '创建中...' : '发送 (Enter)'"
                aria-label="发送需求"
                data-testid="create-send-btn"
                @click="onSubmit"
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
    </div>
  </section>
</template>

<style scoped>
.chat-workspace {
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--af-bg);
}

.stage-bar {
  height: 40px;
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
  gap: 8px;
}

.stage-dot {
  width: 7px;
  height: 7px;
  border-radius: 99px;
  background: var(--af-accent);
}

.stage-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--af-fg);
}

.stage-tag {
  font-size: 11px;
  padding: 2px 7px;
  border-radius: 6px;
  background: var(--af-surface-2);
  border: 1px solid var(--af-border);
  color: var(--af-muted);
}

.bar-icon-btn {
  width: 28px;
  height: 28px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid transparent;
  background: transparent;
  color: var(--af-muted);
  border-radius: 6px;
  cursor: pointer;
}

.bar-icon-btn:hover {
  background: var(--af-btn-ghost-hover);
  color: var(--af-fg);
}

.bar-icon-btn svg {
  width: 15px;
  height: 15px;
}

.chat-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 24px 20px;
}

.chat-container {
  max-width: 820px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.assistant-row {
  display: flex;
  align-items: flex-start;
  gap: 10px;
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

.assistant-content {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.assistant-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
}

.assistant-name {
  font-weight: 600;
  color: var(--af-fg);
}

.meta-badge {
  font-size: 11px;
  padding: 1px 7px;
  border-radius: 5px;
  background: var(--af-surface);
  border: 1px solid var(--af-border);
  color: var(--af-muted);
}

.assistant-bubble {
  border-radius: 12px;
  border: 1px solid var(--af-border-strong);
  background: var(--af-surface);
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.welcome-lead {
  margin: 0;
  font-size: 13px;
  line-height: 1.65;
  color: var(--af-fg-secondary);
}

.quick-prompts {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
}

.prompt-card {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
  padding: 10px 11px;
  border-radius: 9px;
  border: 1px solid var(--af-border);
  background: var(--af-surface-2);
  color: var(--af-fg);
  cursor: pointer;
  text-align: left;
  transition: border-color 140ms ease, background 140ms ease;
}

.prompt-card:hover {
  border-color: var(--af-accent-border);
  background: var(--af-surface-hover);
}

.prompt-title {
  font-size: 12.5px;
  font-weight: 600;
}

.prompt-sub {
  font-size: 11px;
  color: var(--af-muted);
}

.user-row {
  display: flex;
  justify-content: flex-end;
}

.user-bubble {
  max-width: 85%;
  border-radius: 16px 4px 16px 16px;
  padding: 12px 15px;
  background: var(--af-accent-soft);
  border: 1px solid var(--af-accent-border);
  font-size: 13px;
  line-height: 1.6;
}

.user-bubble-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  margin-bottom: 5px;
  font-size: 11px;
  color: var(--af-muted);
}

.user-label {
  font-weight: 600;
  color: var(--af-fg);
}

.creating-bubble {
  flex-direction: row;
  align-items: center;
  gap: 10px;
  font-size: 12.5px;
  color: var(--af-fg-secondary);
}

.creating-dot {
  background: var(--af-accent);
  color: var(--af-accent);
}

.chat-error {
  margin-top: 4px;
}

/* Sticky Bottom Composer */
.composer-dock {
  flex-shrink: 0;
  padding: 12px 20px 14px;
  border-top: 1px solid var(--af-border);
  background: var(--af-surface);
}

.composer-inner {
  max-width: 820px;
  margin: 0 auto;
}

.composer-box {
  border-radius: 12px;
  border: 1px solid var(--af-border-strong);
  background: var(--af-bg);
  padding: 9px 11px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  transition: border-color 140ms ease;
}

.composer-box:focus-within {
  border-color: var(--af-accent-border);
}

.composer-name-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding-bottom: 7px;
  border-bottom: 1px solid var(--af-border);
}

.name-prefix {
  font-size: 11px;
  font-weight: 500;
  color: var(--af-muted);
  flex-shrink: 0;
}

.name-input {
  flex: 1;
  min-width: 0;
  border: none;
  background: transparent;
  color: var(--af-fg);
  font-size: 12px;
  outline: none;
}

.name-input::placeholder {
  color: var(--af-muted);
}

.composer-textarea {
  width: 100%;
  border: none;
  background: transparent;
  color: var(--af-fg);
  font: inherit;
  font-size: 13px;
  line-height: 1.55;
  resize: none;
  outline: none;
  padding: 2px 2px;
}

.composer-textarea::placeholder {
  color: var(--af-muted);
}

.composer-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
}

.toolbar-left,
.toolbar-right {
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

.mode-chip:hover {
  border-color: var(--af-accent-border);
  color: var(--af-fg);
}

.char-hint {
  font-size: 11px;
  color: var(--af-warn);
}

.char-hint.muted {
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
  transition: background 140ms ease, opacity 140ms ease;
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

@media (max-width: 720px) {
  .quick-prompts {
    grid-template-columns: 1fr;
  }
}
</style>
