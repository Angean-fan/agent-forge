<script setup lang="ts">
const items = defineModel<string[]>({ required: true });
defineProps<{ readonly?: boolean; placeholder?: string; addLabel?: string }>();

function setItem(index: number, value: string): void {
  items.value[index] = value;
}

function add(): void {
  items.value.push('');
}

function remove(index: number): void {
  items.value.splice(index, 1);
}
</script>

<template>
  <div class="list-editor">
    <div v-for="(item, index) in items" :key="index" class="list-row">
      <el-input
        :model-value="item"
        :disabled="readonly"
        :placeholder="placeholder"
        @update:model-value="setItem(index, $event)"
      />
      <el-button v-if="!readonly" text type="danger" @click="remove(index)">删除</el-button>
    </div>
    <el-button v-if="!readonly" size="small" @click="add">
      {{ addLabel ?? '添加一项' }}
    </el-button>
  </div>
</template>

<style scoped>
.list-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
</style>
