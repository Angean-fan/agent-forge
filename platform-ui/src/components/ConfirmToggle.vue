<script setup lang="ts">
import type { SpecSource } from '@/api/types';

// 显式确认开关：模型推断为 pending，只有用户点击才变为 user_confirmed。
const source = defineModel<SpecSource>({ required: true });
defineProps<{ readonly?: boolean }>();

function toggle(): void {
  source.value = source.value === 'pending' ? 'user_confirmed' : 'pending';
}
</script>

<template>
  <span class="confirm-toggle">
    <el-tag :type="source === 'pending' ? 'warning' : 'success'" size="small" disable-transitions>
      {{ source === 'pending' ? '待确认' : '已确认' }}
    </el-tag>
    <el-button v-if="!readonly" size="small" link type="primary" @click="toggle">
      {{ source === 'pending' ? '确认此项' : '取消确认' }}
    </el-button>
  </span>
</template>

<style scoped>
.confirm-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
</style>
