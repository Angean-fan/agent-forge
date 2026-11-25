<script setup lang="ts">
import type { RunEvent } from '@/api/types';
import { formatDateTime } from '@/utils/format';

defineProps<{
  events: RunEvent[];
  runId?: number | null;
}>();

function eventSummary(event: RunEvent): string {
  const keys = Object.keys(event.payload);
  if (keys.length === 0) return '—';
  return JSON.stringify(event.payload);
}
</script>

<template>
  <div class="events-panel" data-testid="run-events-panel">
    <div class="events-head">
      <h3 class="section-title">运行事件流</h3>
      <span v-if="runId" class="af-mono events-run-tag">Run #{{ runId }}</span>
    </div>

    <div v-if="events.length === 0" class="ins-empty">
      暂无事件
    </div>

    <ul v-else class="event-list" data-testid="event-list">
      <li v-for="event in events" :key="event.sequence" class="event-item">
        <div class="event-top">
          <span class="event-seq af-mono">#{{ event.sequence }}</span>
          <el-tag size="small" disable-transitions>{{ event.type }}</el-tag>
          <span class="event-time af-mono">{{ formatDateTime(event.created_at) }}</span>
        </div>
        <div class="event-payload af-mono">{{ eventSummary(event) }}</div>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.events-panel {
  border-radius: 10px;
  border: 1px solid var(--af-border);
  background: var(--af-surface);
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.events-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.events-head .section-title {
  margin: 0;
  font-size: 12.5px;
}

.events-run-tag {
  font-size: 10.5px;
  padding: 2px 7px;
  border-radius: 6px;
  border: 1px solid var(--af-border-strong);
  background: var(--af-surface-2);
  color: var(--af-muted);
}

.ins-empty {
  padding: 18px 12px;
  border-radius: 8px;
  border: 1px dashed var(--af-border-strong);
  font-size: 12px;
  color: var(--af-muted);
  text-align: center;
  line-height: 1.5;
}

.event-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.event-item {
  padding: 8px 9px;
  border-radius: 8px;
  background: var(--af-surface-2);
  border: 1px solid var(--af-border);
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.event-top {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.event-seq {
  font-size: 11px;
  font-weight: 600;
  color: var(--af-muted);
}

.event-time {
  margin-left: auto;
  font-size: 10.5px;
  color: var(--af-muted);
}

.event-payload {
  font-size: 11px;
  color: var(--af-fg-secondary);
  word-break: break-all;
  line-height: 1.45;
}
</style>
