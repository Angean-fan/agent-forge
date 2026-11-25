<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue';

import type { FactoryRunView, RunDetail, SpecView } from '@/api/types';

const props = defineProps<{
  run?: RunDetail | null;
  factoryData?: FactoryRunView | null;
  spec?: SpecView | null;
  selectedNode?: string | null;
}>();

const emit = defineEmits<{
  (e: 'select-node', nodeId: string): void;
}>();

// 默认在顶栏以紧凑胶囊显示，点击下拉展开清单，绝不遮挡正文内容
const open = ref(false);
const rootRef = ref<HTMLElement | null>(null);

const STANDARD_STAGES = [
  { key: 'load_confirmed_spec', idx: 1, short: '载入规格' },
  { key: 'resolve_assets', idx: 2, short: '解析资产' },
  { key: 'assemble', idx: 3, short: '源码装配' },
  { key: 'verify', idx: 4, short: '构建验收' },
  { key: 'package', idx: 5, short: '产物打包' },
] as const;

type StageState = 'done' | 'active' | 'warn' | 'idle';

function stageState(stageKey: string): StageState {
  if (!props.run) {
    return stageKey === 'load_confirmed_spec' && props.spec?.confirmed ? 'done' : 'idle';
  }
  const status = props.run.status;
  if (status === 'SUCCEEDED') return 'done';

  if (props.factoryData?.factory_active) {
    const fd = props.factoryData;
    const beDone =
      fd.backend_departments.length > 0 &&
      fd.backend_departments.every((l) => l.status === 'PASSED');
    const feHas = fd.frontend_departments.length > 0;
    const feDone = feHas && fd.frontend_departments.every((l) => l.status === 'PASSED');
    const anyWarn = [...fd.backend_departments, ...fd.frontend_departments].some(
      (l) => l.status === 'REWORK' || l.status === 'FAILED',
    );

    if (stageKey === 'load_confirmed_spec') return 'done';
    if (stageKey === 'resolve_assets') return fd.blueprints.length > 0 ? 'done' : 'active';
    if (stageKey === 'assemble') {
      if (feDone) return 'done';
      if (anyWarn) return 'warn';
      if (fd.backend_departments.length > 0 || feHas) return 'active';
      return 'idle';
    }
    if (stageKey === 'verify') {
      if (feDone) return 'active';
      if (beDone && !feHas) return 'active';
      return 'idle';
    }
    if (stageKey === 'package') return 'idle';
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

const doneCount = computed(
  () => STANDARD_STAGES.filter((s) => stageState(s.key) === 'done').length,
);

const hasActive = computed(() =>
  STANDARD_STAGES.some((s) => stageState(s.key) === 'active'),
);

const hasWarn = computed(() =>
  STANDARD_STAGES.some((s) => stageState(s.key) === 'warn'),
);

function onSelectStage(stageKey: string): void {
  emit('select-node', stageKey);
}

function onDocClick(event: MouseEvent): void {
  if (!open.value || !rootRef.value) return;
  if (!rootRef.value.contains(event.target as Node)) {
    open.value = false;
  }
}

onMounted(() => {
  document.addEventListener('click', onDocClick);
});

onUnmounted(() => {
  document.removeEventListener('click', onDocClick);
});
</script>

<template>
  <div
    ref="rootRef"
    class="topbar-todo-wrap"
    aria-label="标准确定性装配清单"
    data-testid="standard-todo-widget"
  >
    <button
      type="button"
      class="todo-trigger"
      :class="{ open, active: hasActive, warn: hasWarn, done: doneCount === STANDARD_STAGES.length }"
      :aria-expanded="open"
      data-testid="standard-todo-toggle"
      @click.stop="open = !open"
    >
      <span
        class="todo-dot"
        :class="{
          'dot-ok': doneCount === STANDARD_STAGES.length,
          'dot-warn': hasWarn,
          'dot-active': hasActive,
        }"
      />
      <span class="todo-title">装配清单</span>
      <span class="af-mono todo-progress">{{ doneCount }}/{{ STANDARD_STAGES.length }}</span>
      <span class="todo-caret">{{ open ? '▾' : '▸' }}</span>
    </button>

    <div v-if="open" class="todo-popover" data-testid="standard-todo-popover">
      <div class="popover-head">
        <span>标准确定性装配</span>
        <span class="af-mono">{{ doneCount }} / {{ STANDARD_STAGES.length }}</span>
      </div>
      <ul class="todo-list">
        <li
          v-for="stage in STANDARD_STAGES"
          :key="stage.key"
          class="todo-item"
          :class="[`todo-${stageState(stage.key)}`, { selected: selectedNode === stage.key }]"
          @click="onSelectStage(stage.key)"
        >
          <span class="todo-check af-mono">
            <template v-if="stageState(stage.key) === 'done'">✓</template>
            <template v-else-if="stageState(stage.key) === 'active'">●</template>
            <template v-else-if="stageState(stage.key) === 'warn'">!</template>
            <template v-else>○</template>
          </span>
          <span class="todo-label">{{ stage.idx }}. {{ stage.short }}</span>
          <span class="todo-state af-mono">
            {{
              stageState(stage.key) === 'done'
                ? '完成'
                : stageState(stage.key) === 'active'
                  ? '运行中'
                  : stageState(stage.key) === 'warn'
                    ? '异常'
                    : '待执行'
            }}
          </span>
        </li>
      </ul>
    </div>
  </div>
</template>

<style scoped>
.topbar-todo-wrap {
  position: relative;
  flex-shrink: 0;
}

.todo-trigger {
  height: 28px;
  padding: 0 10px;
  border-radius: 7px;
  border: 1px solid var(--af-border-strong);
  background: var(--af-surface-2);
  color: var(--af-fg);
  display: inline-flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  font-size: 11.5px;
  font-weight: 500;
  transition: border-color 140ms ease, background 140ms ease;
}

.todo-trigger:hover,
.todo-trigger.open {
  border-color: var(--af-accent-border);
  background: var(--af-surface-hover);
}

.todo-dot {
  width: 6px;
  height: 6px;
  border-radius: 99px;
  background: var(--af-muted);
  flex-shrink: 0;
}

.todo-dot.dot-active {
  background: var(--af-accent);
}

.todo-dot.dot-ok {
  background: var(--af-ok);
}

.todo-dot.dot-warn {
  background: var(--af-warn);
}

.todo-title {
  color: var(--af-fg);
}

.todo-progress {
  font-size: 11px;
  color: var(--af-muted);
}

.todo-caret {
  font-size: 10px;
  color: var(--af-muted);
}

.todo-popover {
  position: absolute;
  top: calc(100% + 6px);
  right: 0;
  width: 236px;
  z-index: 40;
  border-radius: 10px;
  border: 1px solid var(--af-border-strong);
  background: var(--af-sidebar);
  box-shadow: 0 10px 28px rgba(0, 0, 0, 0.28);
  overflow: hidden;
}

.popover-head {
  padding: 8px 10px;
  background: var(--af-surface-2);
  border-bottom: 1px solid var(--af-border);
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 11.5px;
  font-weight: 600;
  color: var(--af-fg);
}

.todo-list {
  list-style: none;
  margin: 0;
  padding: 5px 6px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.todo-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 7px;
  border-radius: 6px;
  font-size: 11.5px;
  color: var(--af-fg-secondary);
  cursor: pointer;
  transition: background 120ms ease;
}

.todo-item:hover {
  background: var(--af-surface-hover);
}

.todo-item.selected {
  background: var(--af-accent-soft);
}

.todo-check {
  width: 14px;
  text-align: center;
  font-size: 11px;
  flex-shrink: 0;
  color: var(--af-muted);
}

.todo-done .todo-check {
  color: var(--af-ok);
}

.todo-done .todo-label {
  color: var(--af-muted);
  text-decoration: line-through;
}

.todo-active .todo-check {
  color: var(--af-accent);
}

.todo-active .todo-label {
  color: var(--af-fg);
  font-weight: 600;
}

.todo-warn .todo-check {
  color: var(--af-warn);
}

.todo-label {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.todo-state {
  font-size: 10px;
  color: var(--af-muted);
  flex-shrink: 0;
}

.todo-active .todo-state {
  color: var(--af-accent);
}

.todo-done .todo-state {
  color: var(--af-ok);
}
</style>
