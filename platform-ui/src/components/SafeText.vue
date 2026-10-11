<script setup lang="ts">
// 模型/用户文本一律按普通文本渲染（Vue 文本插值转义），永不使用 v-html，
// 因此不会执行模型返回的任何 HTML/脚本。
defineProps<{ text: string | null | undefined }>();
</script>

<template>
  <p class="text-block" data-testid="safe-text">{{ text ?? '' }}</p>
</template>
