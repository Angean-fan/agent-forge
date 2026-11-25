<script setup lang="ts">
import { computed, onMounted, onUnmounted, provide, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';

import WorkbenchSidebar from '@/components/WorkbenchSidebar.vue';
import { clearSession, logout, useAuthState } from '@/stores/auth';
import { useTheme } from '@/stores/theme';
import {
  INSPECTOR_HOST_KEY,
  LEFT_SIDEBAR_DEFAULT,
  RIGHT_SIDEBAR_DEFAULT,
  setLeftSidebarWidth,
  setRightSidebarWidth,
  toggleRightPanel,
  useWorkspaceStore,
} from '@/stores/workspace';
import { formatMicroYuan } from '@/utils/format';

const route = useRoute();
const router = useRouter();
const auth = useAuthState();
const workspace = useWorkspaceStore();
const { theme, toggleTheme } = useTheme();

const loggingOut = ref(false);
const inspectorHostReady = ref(false);
provide(INSPECTOR_HOST_KEY, inspectorHostReady);

const showWorkbench = computed(() => Boolean(route.name) && route.name !== 'login');

// 可拖拽调整左右侧边栏宽度
const resizingSide = ref<'left' | 'right' | null>(null);
let dragStartX = 0;
let dragStartWidth = 0;

function onResizeMove(event: MouseEvent): void {
  if (!resizingSide.value) return;
  const delta = event.clientX - dragStartX;
  if (resizingSide.value === 'left') {
    setLeftSidebarWidth(dragStartWidth + delta);
  } else {
    setRightSidebarWidth(dragStartWidth - delta);
  }
}

function stopResize(): void {
  if (!resizingSide.value) return;
  resizingSide.value = null;
  window.removeEventListener('mousemove', onResizeMove);
  window.removeEventListener('mouseup', stopResize);
}

function startResize(side: 'left' | 'right', event: MouseEvent): void {
  event.preventDefault();
  resizingSide.value = side;
  dragStartX = event.clientX;
  dragStartWidth = side === 'left' ? workspace.leftSidebarWidth : workspace.rightSidebarWidth;
  window.addEventListener('mousemove', onResizeMove);
  window.addEventListener('mouseup', stopResize);
}

function onResizerKeydown(side: 'left' | 'right', event: KeyboardEvent): void {
  const step = event.shiftKey ? 32 : 16;
  if (side === 'left') {
    if (event.key === 'ArrowRight') {
      event.preventDefault();
      setLeftSidebarWidth(workspace.leftSidebarWidth + step);
    } else if (event.key === 'ArrowLeft') {
      event.preventDefault();
      setLeftSidebarWidth(workspace.leftSidebarWidth - step);
    }
  } else {
    if (event.key === 'ArrowLeft') {
      event.preventDefault();
      setRightSidebarWidth(workspace.rightSidebarWidth + step);
    } else if (event.key === 'ArrowRight') {
      event.preventDefault();
      setRightSidebarWidth(workspace.rightSidebarWidth - step);
    }
  }
}

function resetSideWidth(side: 'left' | 'right'): void {
  if (side === 'left') {
    setLeftSidebarWidth(LEFT_SIDEBAR_DEFAULT);
  } else {
    setRightSidebarWidth(RIGHT_SIDEBAR_DEFAULT);
  }
}

function syncInspectorHost(): void {
  inspectorHostReady.value =
    showWorkbench.value &&
    typeof document !== 'undefined' &&
    document.getElementById('workbench-inspector-host') !== null;
}

onMounted(syncInspectorHost);
onUnmounted(stopResize);
watch(showWorkbench, () => {
  setTimeout(syncInspectorHost, 0);
});

async function onLogout(): Promise<void> {
  loggingOut.value = true;
  try {
    await logout();
  } catch {
    clearSession();
  } finally {
    loggingOut.value = false;
    await router.push({ name: 'login' });
  }
}
</script>

<template>
  <div class="app-shell" :class="{ 'is-resizing': resizingSide !== null }">
    <!-- Login Route: Full-canvas centered view -->
    <template v-if="!showWorkbench">
      <div class="login-theme-bar">
        <button
          type="button"
          class="chrome-icon-btn"
          data-testid="theme-toggle"
          :aria-label="theme === 'dark' ? '切换浅色主题' : '切换深色主题'"
          :title="theme === 'dark' ? '切换浅色' : '切换深色'"
          @click="toggleTheme"
        >
          <svg v-if="theme === 'dark'" class="chrome-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <circle cx="12" cy="12" r="5" />
            <line x1="12" y1="1" x2="12" y2="3" />
            <line x1="12" y1="21" x2="12" y2="23" />
            <line x1="4.22" y1="4.22" x2="5.64" y2="5.64" />
            <line x1="18.36" y1="18.36" x2="19.78" y2="19.78" />
            <line x1="1" y1="12" x2="3" y2="12" />
            <line x1="21" y1="12" x2="23" y2="12" />
            <line x1="4.22" y1="19.78" x2="5.64" y2="18.36" />
            <line x1="18.36" y1="5.64" x2="19.78" y2="4.22" />
          </svg>
          <svg v-else class="chrome-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
          </svg>
        </button>
      </div>
      <main class="login-main">
        <router-view />
      </main>
    </template>

    <!-- Authenticated Workbench: 44px Top Chrome + Resizable Split Layout -->
    <template v-else>
      <header class="wb-header">
        <div class="wb-header-left">
          <router-link class="wb-brand" :to="{ name: 'projects' }">
            <span class="wb-logo">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <polygon points="12 2 2 7 12 12 22 7 12 2" />
                <polyline points="2 17 12 22 22 17" />
                <polyline points="2 12 12 17 22 12" />
              </svg>
            </span>
            <span>AgentForge</span>
          </router-link>

          <template v-if="workspace.activeProjectName">
            <span class="wb-slash">/</span>
            <span class="wb-crumb">{{ workspace.activeProjectName }}</span>
          </template>

          <span
            v-if="workspace.activeStageLabel"
            class="wb-stage-pill"
            :class="`tone-${workspace.activeStageTone}`"
          >
            {{ workspace.activeStageLabel }}
          </span>
        </div>

        <div class="wb-header-right">
          <div v-if="workspace.activeCost" class="wb-cost-pill af-mono" title="A07 事务账本成本">
            <span>已结算 {{ formatMicroYuan(workspace.activeCost.settled_micro) }}</span>
            <span class="wb-cost-sep">|</span>
            <span>预留 {{ formatMicroYuan(workspace.activeCost.reserved_micro) }}</span>
          </div>

          <button
            type="button"
            class="chrome-icon-btn"
            :class="{ active: workspace.rightPanelOpen }"
            data-testid="toggle-right-panel"
            :aria-pressed="workspace.rightPanelOpen"
            :aria-label="workspace.rightPanelOpen ? '收起右侧面板' : '展开右侧面板'"
            :title="workspace.rightPanelOpen ? '收起右侧面板' : '展开右侧面板'"
            @click="toggleRightPanel"
          >
            <svg class="chrome-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <rect x="3" y="3" width="18" height="18" rx="2" />
              <line x1="15" y1="3" x2="15" y2="21" />
            </svg>
          </button>

          <button
            type="button"
            class="chrome-icon-btn"
            data-testid="theme-toggle"
            :aria-label="theme === 'dark' ? '切换浅色主题' : '切换深色主题'"
            :title="theme === 'dark' ? '切换浅色' : '切换深色'"
            @click="toggleTheme"
          >
            <svg v-if="theme === 'dark'" class="chrome-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <circle cx="12" cy="12" r="5" />
              <line x1="12" y1="1" x2="12" y2="3" />
              <line x1="12" y1="21" x2="12" y2="23" />
              <line x1="4.22" y1="4.22" x2="5.64" y2="5.64" />
              <line x1="18.36" y1="18.36" x2="19.78" y2="19.78" />
              <line x1="1" y1="12" x2="3" y2="12" />
              <line x1="21" y1="12" x2="23" y2="12" />
              <line x1="4.22" y1="19.78" x2="5.64" y2="18.36" />
              <line x1="18.36" y1="5.64" x2="19.78" y2="4.22" />
            </svg>
            <svg v-else class="chrome-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
            </svg>
          </button>

          <div v-if="auth.account" class="wb-user-group">
            <span class="wb-avatar" :title="auth.account.username">{{ auth.account.username.slice(0, 1).toUpperCase() }}</span>
            <button
              type="button"
              class="chrome-icon-btn"
              :disabled="loggingOut"
              aria-label="退出登录"
              title="退出登录"
              @click="onLogout"
            >
              <svg class="chrome-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
                <polyline points="16 17 21 12 16 7" />
                <line x1="21" y1="12" x2="9" y2="12" />
              </svg>
            </button>
          </div>
        </div>
      </header>

      <!-- Resizable Split Workbench (Left Sidebar | Resizer | Center Stage | Resizer | Right Inspector) -->
      <div class="wb-grid" data-testid="workbench-grid">
        <div
          class="wb-left-pane"
          :style="{ width: `${workspace.leftSidebarWidth}px` }"
          data-testid="workbench-left-pane"
        >
          <WorkbenchSidebar />
        </div>

        <div
          class="wb-resizer"
          :class="{ active: resizingSide === 'left' }"
          role="separator"
          aria-orientation="vertical"
          aria-label="调整左侧边栏宽度"
          tabindex="0"
          title="拖拽调整左侧边栏宽度（双击恢复默认）"
          data-testid="resize-left"
          @mousedown="startResize('left', $event)"
          @dblclick="resetSideWidth('left')"
          @keydown="onResizerKeydown('left', $event)"
        />

        <main class="wb-center">
          <router-view />
        </main>

        <div
          v-show="workspace.rightPanelOpen"
          class="wb-resizer"
          :class="{ active: resizingSide === 'right' }"
          role="separator"
          aria-orientation="vertical"
          aria-label="调整右侧边栏宽度"
          tabindex="0"
          title="拖拽调整右侧边栏宽度（双击恢复默认）"
          data-testid="resize-right"
          @mousedown="startResize('right', $event)"
          @dblclick="resetSideWidth('right')"
          @keydown="onResizerKeydown('right', $event)"
        />

        <aside
          v-show="workspace.rightPanelOpen"
          id="workbench-inspector-host"
          class="wb-right"
          :style="{ width: `${workspace.rightSidebarWidth}px` }"
          data-testid="workbench-inspector"
        >
          <!-- Teleported right-sidebar panels from ProjectDetailView / RunDetailView render here -->
        </aside>
      </div>
    </template>
  </div>
</template>

<style scoped>
.app-shell {
  height: 100vh;
  width: 100vw;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--af-bg);
  color: var(--af-fg);
}

.app-shell.is-resizing {
  user-select: none;
  cursor: col-resize;
}

.login-theme-bar {
  position: fixed;
  top: 16px;
  right: 20px;
  z-index: 20;
}

.login-main {
  flex: 1;
  overflow-y: auto;
}

.wb-header {
  height: 44px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 14px;
  border-bottom: 1px solid var(--af-border);
  background: var(--af-sidebar);
  gap: 12px;
}

.wb-header-left {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.wb-brand {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  font-weight: 600;
  font-size: 13px;
  color: var(--af-fg);
  text-decoration: none;
  flex-shrink: 0;
}

.wb-logo {
  width: 24px;
  height: 24px;
  border-radius: 6px;
  background: var(--af-accent-soft);
  border: 1px solid var(--af-accent-border);
  color: var(--af-accent);
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.wb-logo svg {
  width: 14px;
  height: 14px;
}

.wb-slash {
  color: var(--af-muted);
  font-size: 12px;
}

.wb-crumb {
  font-size: 12.5px;
  font-weight: 500;
  color: var(--af-fg);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 240px;
}

.wb-stage-pill {
  font-size: 11px;
  font-weight: 500;
  padding: 2px 8px;
  border-radius: 99px;
  border: 1px solid var(--af-border-strong);
  flex-shrink: 0;
}

.tone-accent {
  background: var(--af-accent-soft);
  border-color: var(--af-accent-border);
  color: var(--af-accent);
}
.tone-ok {
  background: var(--af-ok-soft);
  border-color: var(--af-ok-border);
  color: var(--af-ok);
}
.tone-warn {
  background: var(--af-warn-soft);
  border-color: var(--af-warn-border);
  color: var(--af-warn);
}
.tone-err {
  background: var(--af-err-soft);
  border-color: var(--af-err-border);
  color: var(--af-err);
}
.tone-muted {
  background: var(--af-surface);
  color: var(--af-muted);
}

.wb-header-right {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.wb-cost-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 9px;
  border-radius: 7px;
  border: 1px solid var(--af-border-strong);
  background: var(--af-surface);
  font-size: 11px;
  color: var(--af-muted);
}

.wb-cost-sep {
  opacity: 0.4;
}

.chrome-icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  padding: 0;
  border-radius: 7px;
  border: 1px solid transparent;
  background: transparent;
  color: var(--af-muted);
  cursor: pointer;
  transition: background 140ms ease, color 140ms ease, border-color 140ms ease;
}

.chrome-icon-btn:hover:not(:disabled) {
  background: var(--af-btn-ghost-hover);
  color: var(--af-fg);
}

.chrome-icon-btn.active {
  background: var(--af-surface-2);
  border-color: var(--af-border-strong);
  color: var(--af-fg);
}

.chrome-icon {
  width: 15px;
  height: 15px;
}

.wb-user-group {
  display: flex;
  align-items: center;
  gap: 6px;
  padding-left: 8px;
  border-left: 1px solid var(--af-border);
}

.wb-avatar {
  width: 22px;
  height: 22px;
  border-radius: 99px;
  background: var(--af-surface-2);
  border: 1px solid var(--af-border-strong);
  color: var(--af-fg);
  font-size: 11px;
  font-weight: 600;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

/* Resizable Flex Workbench */
.wb-grid {
  flex: 1;
  min-height: 0;
  display: flex;
  overflow: hidden;
}

.wb-left-pane {
  flex-shrink: 0;
  min-width: 200px;
  max-width: 480px;
  height: 100%;
  overflow: hidden;
}

.wb-resizer {
  width: 5px;
  flex-shrink: 0;
  cursor: col-resize;
  position: relative;
  background: transparent;
  z-index: 5;
}

.wb-resizer::before {
  content: '';
  position: absolute;
  top: 0;
  bottom: 0;
  left: 2px;
  width: 1px;
  background: var(--af-border);
  transition: background 140ms ease, width 140ms ease, left 140ms ease;
}

.wb-resizer:hover::before,
.wb-resizer:focus-visible::before,
.wb-resizer.active::before {
  left: 1px;
  width: 3px;
  background: var(--af-accent);
}

.wb-center {
  flex: 1;
  min-width: 0;
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--af-bg);
}

.wb-right {
  flex-shrink: 0;
  min-width: 280px;
  max-width: 640px;
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  padding: 0;
  background: var(--af-sidebar);
}

.wb-right:empty::before {
  content: '暂无内容';
  display: block;
  margin: 48px 14px 0;
  font-size: 12px;
  color: var(--af-muted);
  text-align: center;
}

@media (max-width: 820px) {
  .wb-grid {
    flex-direction: column;
    overflow-y: auto;
  }
  .wb-left-pane,
  .wb-right {
    width: 100% !important;
    max-width: none;
    height: auto;
  }
  .wb-resizer {
    display: none;
  }
  .wb-right {
    border-top: 1px solid var(--af-border);
  }
}
</style>
