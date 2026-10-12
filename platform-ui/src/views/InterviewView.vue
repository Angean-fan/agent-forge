<script setup lang="ts">
// R05：动态问答卡片。题型完全由后端 R02 协议给出，前端不猜测题型；
// 题面按普通文本渲染（SafeText，永不 v-html）。答案本地保留，提交失败不清空。
import { computed, reactive, ref, watch } from 'vue';
import { useRouter } from 'vue-router';

import { ElMessage } from 'element-plus';

import { interviewApi } from '@/api/client';
import type { InterviewAnswer, InterviewView as InterviewViewData } from '@/api/types';
import SafeText from '@/components/SafeText.vue';
import { describeError } from '@/utils/errors';

const props = defineProps<{ id: string }>();
const router = useRouter();
const projectId = computed(() => Number(props.id));

interface DraftAnswer {
  selected: string[];
  custom: string;
  unsure: boolean;
}

const view = ref<InterviewViewData | null>(null);
const drafts = reactive<Record<string, DraftAnswer>>({});
const loading = ref(true);
const submitting = ref(false);
const errorMessage = ref('');

const statusLabel = computed(() => {
  switch (view.value?.status) {
    case 'NEW':
      return '未开始';
    case 'CLARIFYING':
      return '进行中';
    case 'READY_TO_CONFIRM':
      return '信息已收敛';
    case 'NEEDS_INPUT':
      return '待补充';
    default:
      return view.value?.status ?? '未开始';
  }
});
const statusType = computed<'info' | 'success' | 'warning'>(() => {
  if (view.value?.status === 'READY_TO_CONFIRM') return 'success';
  if (view.value?.status === 'NEEDS_INPUT') return 'warning';
  return 'info';
});

function ensureDrafts(data: InterviewViewData): void {
  // 只补新题目的空答案，保留用户已填内容（网络中断不丢已确认答案）。
  for (const question of data.questions) {
    if (!drafts[question.question_id]) {
      drafts[question.question_id] = { selected: [], custom: '', unsure: false };
    }
  }
}

function applyView(data: InterviewViewData): void {
  view.value = data;
  ensureDrafts(data);
}

async function load(): Promise<void> {
  loading.value = true;
  errorMessage.value = '';
  try {
    applyView(await interviewApi.get(projectId.value));
  } catch (error) {
    const info = describeError(error);
    if (info.unauthorized) {
      await router.push({ name: 'login' });
      return;
    }
    errorMessage.value = info.message;
  } finally {
    loading.value = false;
  }
}

async function start(): Promise<void> {
  submitting.value = true;
  errorMessage.value = '';
  try {
    applyView(await interviewApi.submit(projectId.value, []));
    ElMessage.success('已生成澄清问题');
  } catch (error) {
    errorMessage.value = describeError(error).message;
  } finally {
    submitting.value = false;
  }
}

function buildAnswers(): InterviewAnswer[] {
  return (view.value?.questions ?? []).map((question) => {
    const draft = drafts[question.question_id];
    const answer: InterviewAnswer = {
      question_id: question.question_id,
      question_version: question.version,
    };
    if (draft.unsure) {
      answer.unsure = true;
      return answer;
    }
    if (question.type === 'text') {
      if (draft.custom.trim()) answer.custom = draft.custom.trim();
    } else {
      if (draft.selected.length) answer.selected = [...draft.selected];
      if (question.allow_custom && draft.custom.trim()) answer.custom = draft.custom.trim();
    }
    return answer;
  });
}

async function submit(): Promise<void> {
  submitting.value = true;
  errorMessage.value = '';
  try {
    // 提交成功后草稿不清空：过期提示由后端 issues 给出，用户据此重答。
    applyView(await interviewApi.submit(projectId.value, buildAnswers()));
    ElMessage.success('已提交本轮回答');
  } catch (error) {
    errorMessage.value = describeError(error).message;
  } finally {
    submitting.value = false;
  }
}

function chooseSingle(questionId: string, option: string): void {
  drafts[questionId].selected = [option];
}

function gotoProject(): void {
  void router.push({ name: 'project-detail', params: { id: String(projectId.value) } });
}

watch(() => props.id, load, { immediate: true });
</script>

<template>
  <section v-loading="loading">
    <div class="detail-head">
      <div>
        <h1 class="page-title">需求访谈</h1>
        <p class="page-subtitle">用业务语言回答几个问题，帮助平台理解你的需求。每轮最多 3 题。</p>
      </div>
      <el-button @click="gotoProject">返回项目</el-button>
    </div>

    <el-alert
      v-if="errorMessage"
      :title="errorMessage"
      type="error"
      :closable="false"
      show-icon
      class="block-gap"
    />

    <el-card v-if="view" class="block-gap">
      <div class="meta">
        <el-tag size="small" type="info" disable-transitions>第 {{ view.round }} 轮</el-tag>
        <el-tag size="small" :type="statusType" disable-transitions>{{ statusLabel }}</el-tag>
        <span v-if="view.pending.length" class="pending-note">
          待确认：{{ view.pending.join('、') }}
        </span>
      </div>

      <el-alert
        v-if="view.status === 'NEW'"
        type="info"
        :closable="false"
        show-icon
        class="block-gap"
      >
        <template #title>还没有开始访谈</template>
        <div class="round-note">点击“开始访谈”，平台会基于当前需求草稿生成澄清问题。</div>
      </el-alert>

      <el-alert
        v-if="view.issues.length"
        type="warning"
        :closable="false"
        show-icon
        class="block-gap"
        data-testid="stale-alert"
      >
        <template #title>部分回答已过期或与当前题目不符，请按最新题目重新作答</template>
        <ul class="bullet-list">
          <li v-for="(item, index) in view.issues" :key="`issue-${index}`">{{ item }}</li>
        </ul>
      </el-alert>

      <div
        v-for="question in view.questions"
        :key="question.question_id"
        class="question-card"
        :data-testid="`question-${question.question_id}`"
      >
        <SafeText :text="question.prompt" />
        <div class="options">
          <template v-if="question.type === 'single_choice'">
            <label v-for="option in question.options" :key="option" class="option">
              <input
                type="radio"
                :name="question.question_id"
                :value="option"
                :checked="drafts[question.question_id]?.selected[0] === option"
                @change="chooseSingle(question.question_id, option)"
              />
              <span>{{ option }}</span>
            </label>
          </template>
          <template v-else-if="question.type === 'multi_choice'">
            <label v-for="option in question.options" :key="option" class="option">
              <input
                type="checkbox"
                :value="option"
                :data-testid="`option-${question.question_id}-${option}`"
                v-model="drafts[question.question_id].selected"
              />
              <span>{{ option }}</span>
            </label>
          </template>
          <input
            v-else
            type="text"
            class="text-answer"
            v-model="drafts[question.question_id].custom"
            :data-testid="`text-${question.question_id}`"
            placeholder="请输入你的回答"
          />
        </div>
        <input
          v-if="question.allow_custom && question.type !== 'text'"
          type="text"
          class="custom-answer"
          v-model="drafts[question.question_id].custom"
          :data-testid="`custom-${question.question_id}`"
          placeholder="其他（可选）"
        />
        <label class="option unsure">
          <input
            type="checkbox"
            v-model="drafts[question.question_id].unsure"
            :data-testid="`unsure-${question.question_id}`"
          />
          <span>不确定</span>
        </label>
      </div>

      <div class="action-bar">
        <el-button
          v-if="view.status === 'NEW'"
          type="primary"
          :loading="submitting"
          @click="start"
        >
          开始访谈
        </el-button>
        <el-button
          v-else-if="view.waiting"
          type="primary"
          :loading="submitting"
          data-testid="submit-answers"
          @click="submit"
        >
          提交回答
        </el-button>
        <el-alert
          v-if="view.status === 'READY_TO_CONFIRM'"
          type="success"
          :closable="false"
          show-icon
          class="block-gap"
          data-testid="ready-alert"
        >
          <template #title>信息已收敛，可以前往项目页确认规格</template>
        </el-alert>
        <el-alert
          v-else-if="view.status === 'NEEDS_INPUT'"
          type="warning"
          :closable="false"
          show-icon
          class="block-gap"
          data-testid="needs-input-alert"
        >
          <template #title>
            {{
              view.error_code
                ? `问题生成失败（${view.error_code}），请稍后重试`
                : '仍有信息未确认，请编辑需求或缩小范围后再生成'
            }}
          </template>
        </el-alert>
        <el-button
          v-if="view.status === 'READY_TO_CONFIRM'"
          type="primary"
          @click="gotoProject"
        >
          前往规格确认
        </el-button>
      </div>
    </el-card>
  </section>
</template>

<style scoped>
.detail-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}
.block-gap {
  margin-top: 16px;
}
.meta {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.pending-note {
  color: #909399;
  font-size: 13px;
}
.round-note {
  margin-top: 4px;
  color: #606266;
  font-size: 13px;
}
.bullet-list {
  margin: 8px 0 0;
  padding-left: 20px;
}
.question-card {
  margin-top: 16px;
  padding: 12px 16px;
  border: 1px solid var(--el-border-color-light);
  border-radius: 6px;
}
.options {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 8px;
}
.option {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
}
.unsure {
  margin-top: 8px;
  color: #909399;
}
.text-answer,
.custom-answer {
  margin-top: 8px;
  padding: 6px 10px;
  border: 1px solid var(--el-border-color);
  border-radius: 4px;
  font: inherit;
}
.action-bar {
  margin-top: 20px;
  padding-top: 16px;
  border-top: 1px solid var(--el-border-color-light);
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}
</style>
