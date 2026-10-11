<script setup lang="ts">
import { reactive, ref } from 'vue';
import { useRouter } from 'vue-router';

import { ElMessage, type FormInstance, type FormRules } from 'element-plus';

import { projectsApi } from '@/api/client';
import { describeError } from '@/utils/errors';

const router = useRouter();

const REQUIREMENT_MIN = 20;
const REQUIREMENT_MAX = 5000;

const formRef = ref<FormInstance>();
const form = reactive({ name: '', requirement: '' });

const rules: FormRules = {
  name: [
    { required: true, message: '请填写项目名称', trigger: 'blur' },
    { min: 1, max: 60, message: '项目名称为 1 至 60 个字符', trigger: 'blur' },
  ],
  requirement: [
    { required: true, message: '请描述你的需求', trigger: 'blur' },
    {
      min: REQUIREMENT_MIN,
      max: REQUIREMENT_MAX,
      message: `需求描述为 ${REQUIREMENT_MIN} 至 ${REQUIREMENT_MAX} 个字符`,
      trigger: 'blur',
    },
  ],
};

const submitting = ref(false);
const errorMessage = ref('');

async function onSubmit(): Promise<void> {
  if (!formRef.value) {
    return;
  }
  const valid = await formRef.value.validate().catch(() => false);
  if (!valid) {
    return;
  }
  submitting.value = true;
  errorMessage.value = '';
  try {
    const project = await projectsApi.create(form.name, form.requirement);
    ElMessage.success('项目已创建');
    await router.push({ name: 'project-detail', params: { id: String(project.id) } });
  } catch (error) {
    errorMessage.value = describeError(error).message;
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <section>
    <h1 class="page-title">新建项目</h1>
    <p class="page-subtitle">
      用业务语言描述你想做的管理系统即可，无需选择框架或数据库；系统会先给出理解草稿供你确认。
    </p>

    <el-alert
      v-if="errorMessage"
      :title="errorMessage"
      type="error"
      :closable="false"
      show-icon
      class="create-error"
    />

    <el-card>
      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-position="top"
        @submit.prevent="onSubmit"
      >
        <el-form-item label="项目名称" prop="name">
          <el-input
            v-model="form.name"
            maxlength="60"
            show-word-limit
            placeholder="例如：社区活动报名"
          />
        </el-form-item>
        <el-form-item label="需求描述" prop="requirement">
          <el-input
            v-model="form.requirement"
            type="textarea"
            :rows="8"
            :maxlength="REQUIREMENT_MAX"
            show-word-limit
            placeholder="例如：谁可以发布活动、谁可以报名，活动需要记录哪些信息，有什么限制……"
          />
        </el-form-item>
        <div class="create-actions">
          <el-button @click="router.back()">返回</el-button>
          <el-button type="primary" :loading="submitting" @click="onSubmit">创建项目</el-button>
        </div>
      </el-form>
    </el-card>
  </section>
</template>

<style scoped>
.create-error {
  margin-bottom: 16px;
}
.create-actions {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
}
</style>
