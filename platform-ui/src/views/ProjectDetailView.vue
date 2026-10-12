<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue';
import { useRouter } from 'vue-router';

import { ElMessage, ElMessageBox } from 'element-plus';

import { ApiError, newIdempotencyKey, projectsApi, runsApi } from '@/api/client';
import type { ProjectDetail, SoftwareSpecification, SpecView } from '@/api/types';
import ConfirmToggle from '@/components/ConfirmToggle.vue';
import SafeText from '@/components/SafeText.vue';
import StringListEditor from '@/components/StringListEditor.vue';
import { clearSession } from '@/stores/auth';
import { describeError } from '@/utils/errors';
import { formatDateTime } from '@/utils/format';

const props = defineProps<{ id: string }>();
const router = useRouter();

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

const project = ref<ProjectDetail | null>(null);
const spec = ref<SpecView | null>(null);
const draft = reactive<SoftwareSpecification>(blankSpec());
const loadedJson = ref('');

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

function applySpec(view: SpecView): void {
  spec.value = view;
  Object.assign(draft, structuredClone(view.spec));
  loadedJson.value = JSON.stringify(view.spec);
  reopenEditing.value = false;
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
    // 尚无规格草稿时后端返回 SPEC_NOT_FOUND，属正常状态而非错误。
    spec.value = null;
    if (!(error instanceof ApiError && error.code === 'SPEC_NOT_FOUND')) {
      specMessage.value = describeError(error).message;
    }
  } finally {
    specLoaded.value = true;
  }
}

async function loadAll(): Promise<void> {
  loading.value = true;
  specMessage.value = '';
  await Promise.all([loadProject(), loadSpec()]);
  loading.value = false;
}

async function onExtract(): Promise<void> {
  if (!project.value) {
    return;
  }
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
    ElMessage.success('已生成需求草稿');
  } catch (error) {
    if (await handleAuthError(error)) {
      return;
    }
    specMessage.value = describeError(error).message;
    if (error instanceof ApiError && error.code === 'SPEC_CONFLICT') {
      await loadSpec();
    }
  } finally {
    extracting.value = false;
  }
}

async function onSave(): Promise<void> {
  if (!spec.value) {
    return;
  }
  saving.value = true;
  specMessage.value = '';
  try {
    applySpec(
      await projectsApi.saveSpec(projectId.value, spec.value.version, spec.value.draft_revision, draft),
    );
    ElMessage.success('草稿已保存');
  } catch (error) {
    if (await handleAuthError(error)) {
      return;
    }
    const info = describeError(error);
    specMessage.value = info.retryable ? `${info.message} 请重新读取后再试。` : info.message;
    if (error instanceof ApiError && error.code === 'SPEC_CONFLICT') {
      await loadSpec();
    }
  } finally {
    saving.value = false;
  }
}

async function onConfirm(): Promise<void> {
  if (!spec.value) {
    return;
  }
  confirming.value = true;
  specMessage.value = '';
  try {
    await projectsApi.confirm(projectId.value, spec.value.version, spec.value.draft_revision);
    ElMessage.success('规格已确认并冻结');
    await Promise.all([loadSpec(), loadProject()]);
  } catch (error) {
    if (await handleAuthError(error)) {
      return;
    }
    specMessage.value = describeError(error).message;
    if (error instanceof ApiError && error.code === 'SPEC_CONFLICT') {
      await loadSpec();
    }
  } finally {
    confirming.value = false;
  }
}

async function onGenerate(): Promise<void> {
  if (!spec.value || !project.value) {
    return;
  }
  generating.value = true;
  specMessage.value = '';
  // 复用同一幂等键，重试不会重复创建运行。
  if (idempotencyKey.value === null) {
    idempotencyKey.value = newIdempotencyKey();
  }
  try {
    const run = await runsApi.start(projectId.value, spec.value.spec_id, idempotencyKey.value);
    idempotencyKey.value = null;
    ElMessage.success('已提交生成运行');
    await router.push({ name: 'run-detail', params: { id: String(run.id) } });
  } catch (error) {
    if (await handleAuthError(error)) {
      return;
    }
    specMessage.value = describeError(error).message;
  } finally {
    generating.value = false;
  }
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

watch(() => props.id, loadAll, { immediate: true });
</script>

<template>
  <section v-loading="loading">
    <div class="detail-head">
      <div>
        <h1 class="page-title">{{ project?.name ?? '项目详情' }}</h1>
        <p class="page-subtitle">
          创建于 {{ formatDateTime(project?.created_at) }}
          <el-tag
            v-if="project"
            :type="project.active_spec_id === null ? 'info' : 'success'"
            size="small"
            disable-transitions
            class="head-tag"
          >
            {{ project.active_spec_id === null ? '需求草稿' : '已冻结规格' }}
          </el-tag>
        </p>
      </div>
      <el-button @click="router.push({ name: 'projects' })">返回列表</el-button>
    </div>

    <el-alert
      v-if="errorMessage"
      :title="errorMessage"
      type="error"
      :closable="false"
      show-icon
      class="block-gap"
    />

    <el-card v-if="project" class="block-gap">
      <h2 class="section-title">需求原文</h2>
      <SafeText :text="project.requirement" />
    </el-card>

    <el-card class="block-gap">
      <div class="draft-head">
        <div>
          <h2 class="section-title">需求规格</h2>
          <p class="page-subtitle draft-note">
            模型生成的内容一律标记为“待确认”，需你逐项确认或修改后才能确认冻结；固定技术方案暂不可切换。
          </p>
        </div>
        <div class="draft-actions">
          <el-button
            v-if="hasSpec && !spec?.confirmed"
            data-testid="open-interview"
            @click="router.push({ name: 'project-interview', params: { id: String(projectId) } })"
          >
            需求访谈
          </el-button>
          <el-button type="primary" :loading="extracting" @click="onExtract">
            {{ hasSpec ? '重新生成草稿' : '生成需求草稿' }}
          </el-button>
        </div>
      </div>

      <el-alert
        v-if="specMessage"
        :title="specMessage"
        type="error"
        :closable="false"
        show-icon
        class="block-gap"
      />

      <el-empty
        v-if="specLoaded && !hasSpec"
        description="还没有需求草稿，点击“生成需求草稿”让模型先理解你的需求"
      />

      <template v-if="spec">
        <div class="draft-meta">
          <el-tag size="small" type="info" disable-transitions>版本 v{{ spec.version }}</el-tag>
          <el-tag size="small" type="info" disable-transitions>
            草稿修订 {{ spec.draft_revision }}
          </el-tag>
          <el-tag size="small" :type="spec.confirmed ? 'success' : 'warning'" disable-transitions>
            {{ spec.confirmed ? '已确认并冻结' : '未确认' }}
          </el-tag>
          <span class="draft-hash">hash {{ spec.hash.slice(0, 12) }}</span>
          <span v-if="spec.confirmed_at" class="draft-hash">
            · 确认于 {{ formatDateTime(spec.confirmed_at) }}
          </span>
        </div>

        <el-alert
          v-if="spec.pending.length"
          type="warning"
          :closable="false"
          show-icon
          class="block-gap"
        >
          <template #title>以下为模型推断，仍未确认（确认前需逐项处理）</template>
          <div class="tag-row">
            <el-tag v-for="item in spec.pending" :key="item" type="warning" disable-transitions>
              {{ item }}
            </el-tag>
          </div>
        </el-alert>

        <el-alert v-if="spec.issues.length" type="info" :closable="false" show-icon class="block-gap">
          <template #title>确认前需解决的校验项</template>
          <ul class="bullet-list">
            <li v-for="(item, index) in spec.issues" :key="`issue-${index}`">{{ item }}</li>
          </ul>
        </el-alert>

        <template v-if="spec.missing_fields.length || spec.questions.length">
          <el-alert
            v-if="spec.missing_fields.length"
            type="warning"
            :closable="false"
            show-icon
            class="block-gap"
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
            class="block-gap"
          >
            <template #title>模型建议确认的问题</template>
            <ul class="bullet-list">
              <li v-for="(item, index) in spec.questions" :key="`question-${index}`">{{ item }}</li>
            </ul>
          </el-alert>
        </template>

        <h3 class="section-title">摘要 / 目标</h3>
        <el-input
          v-model="draft.goal"
          type="textarea"
          :rows="3"
          :disabled="readOnly"
          placeholder="用一句话说明这个系统要达到的目标"
          data-testid="goal-input"
        />
        <SafeText v-if="spec.summary" :text="`模型摘要：${spec.summary}`" />

        <h3 class="section-title">角色</h3>
        <StringListEditor v-model="draft.actors" :readonly="readOnly" placeholder="例如 MEMBER / ADMIN" />

        <h3 class="section-title">业务实体</h3>
        <el-empty v-if="!draft.entities.length" description="MVP 需要一个 Task 业务实体" :image-size="60" />
        <div v-for="(entity, entityIndex) in draft.entities" :key="entityIndex" class="entity-block">
          <div class="entity-head">
            <el-input v-model="entity.name" :disabled="readOnly" class="entity-name" />
            <ConfirmToggle v-model="entity.source" :readonly="readOnly" />
          </div>
          <el-table :data="entity.fields" size="small" border>
            <el-table-column label="字段">
              <template #default="{ row }">
                <el-input v-model="row.name" :disabled="readOnly" size="small" />
              </template>
            </el-table-column>
            <el-table-column label="类型" width="150">
              <template #default="{ row }">
                <el-select v-model="row.type" :disabled="readOnly" size="small">
                  <el-option v-for="type in FIELD_TYPES" :key="type" :label="type" :value="type" />
                </el-select>
              </template>
            </el-table-column>
            <el-table-column label="必填" width="90">
              <template #default="{ row }">
                <el-checkbox v-model="row.required" :disabled="readOnly" />
              </template>
            </el-table-column>
            <el-table-column v-if="!readOnly" label="操作" width="90">
              <template #default="{ $index }">
                <el-button text type="danger" @click="removeField(entityIndex, $index)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
          <el-button v-if="!readOnly" size="small" class="block-gap-sm" @click="addField(entityIndex)">
            添加字段
          </el-button>
        </div>

        <h3 class="section-title">角色权限</h3>
        <div v-for="(permission, index) in draft.permissions" :key="index" class="perm-row">
          <el-select v-model="permission.role" :disabled="readOnly" class="perm-role">
            <el-option v-for="role in ROLE_OPTIONS" :key="role" :label="role" :value="role" />
          </el-select>
          <el-checkbox-group v-model="permission.actions" :disabled="readOnly">
            <el-checkbox v-for="action in ACTION_OPTIONS" :key="action" :value="action">
              {{ action }}
            </el-checkbox>
          </el-checkbox-group>
          <ConfirmToggle v-model="permission.source" :readonly="readOnly" />
          <el-button v-if="!readOnly" text type="danger" @click="removePermission(index)">删除</el-button>
        </div>
        <el-button v-if="!readOnly" size="small" @click="addPermission">添加角色权限</el-button>

        <h3 class="section-title">业务规则</h3>
        <div v-for="(rule, index) in draft.business_rules" :key="index" class="rule-edit">
          <span class="rule-id">{{ rule.id }}</span>
          <el-input v-model="rule.text" type="textarea" :rows="2" :disabled="readOnly" />
          <ConfirmToggle v-model="rule.source" :readonly="readOnly" />
          <el-button v-if="!readOnly" text type="danger" @click="removeRule(index)">删除</el-button>
        </div>
        <el-button v-if="!readOnly" size="small" @click="addRule">添加业务规则</el-button>

        <h3 class="section-title">验收场景</h3>
        <div v-for="(item, index) in draft.acceptance_cases" :key="index" class="rule-edit">
          <span class="rule-id">{{ item.id }}</span>
          <div class="rule-fields">
            <el-input v-model="item.scenario" :disabled="readOnly" placeholder="场景" />
            <el-input v-model="item.expected" :disabled="readOnly" placeholder="预期结果" />
          </div>
          <ConfirmToggle v-model="item.source" :readonly="readOnly" />
          <el-button v-if="!readOnly" text type="danger" @click="removeCase(index)">删除</el-button>
        </div>
        <el-button v-if="!readOnly" size="small" @click="addCase">添加验收场景</el-button>

        <h3 class="section-title">约束</h3>
        <StringListEditor v-model="draft.constraints" :readonly="readOnly" />

        <h3 class="section-title">明确不做</h3>
        <StringListEditor v-model="draft.exclusions" :readonly="readOnly" />

        <h3 class="section-title">技术方案</h3>
        <p class="fixed-profile">
          固定技术方案（MVP 暂不可切换）：{{ spec.spec.technical_profile.profile_id }}
          @{{ spec.spec.technical_profile.profile_version }} ·
          模板 {{ spec.spec.technical_profile.template_version }}
        </p>

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
              :loading="saving"
              @click="reopenEditing = true"
            >
              创建新版本草稿以修改
            </el-button>
            <el-button
              v-if="!spec.confirmed"
              :disabled="readOnly || !isDirty"
              :loading="saving"
              @click="onSave"
            >
              保存修改
            </el-button>
            <el-button
              v-else-if="reopenEditing"
              :loading="saving"
              @click="onSave"
            >
              保存为新草稿版本
            </el-button>
            <el-button
              v-if="!spec.confirmed"
              type="primary"
              :disabled="!canConfirm"
              :loading="confirming"
              @click="onConfirm"
            >
              确认规格
            </el-button>
            <el-button
              type="success"
              :disabled="!canGenerate"
              :loading="generating"
              @click="onGenerate"
            >
              开始生成
            </el-button>
          </div>
        </div>
      </template>
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
.head-tag {
  margin-left: 8px;
}
.block-gap {
  margin-top: 16px;
}
.block-gap-sm {
  margin-top: 8px;
}
.draft-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}
.draft-note {
  margin-bottom: 0;
}
.draft-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 16px 0;
  flex-wrap: wrap;
}
.draft-hash {
  color: #909399;
  font-size: 12px;
}
.tag-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 8px;
}
.bullet-list {
  margin: 8px 0 0;
  padding-left: 20px;
}
.entity-block {
  margin-bottom: 16px;
}
.entity-head {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 8px;
}
.entity-name {
  max-width: 240px;
}
.perm-row {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}
.perm-role {
  width: 140px;
}
.rule-edit {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 12px;
}
.rule-id {
  flex: none;
  color: #909399;
  font-weight: 600;
  padding-top: 6px;
}
.rule-fields {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.action-bar {
  margin-top: 24px;
  padding-top: 16px;
  border-top: 1px solid var(--el-border-color-light);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}
.action-hint {
  color: #909399;
  font-size: 13px;
  max-width: 420px;
}
.action-buttons {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.fixed-profile {
  margin: 0;
  padding: 8px 12px;
  background: #f5f7fa;
  border-radius: 4px;
  color: #606266;
  font-size: 13px;
}
</style>
