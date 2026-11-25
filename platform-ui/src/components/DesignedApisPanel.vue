<script setup lang="ts">
import { computed, reactive, watch } from 'vue';

import type { FactoryBlueprintSummary, SoftwareSpecification } from '@/api/types';
import {
  synthesizeDesignedApis,
  type DesignedApiEndpoint,
  type HttpMethod,
} from '@/utils/apiDesign';

const props = defineProps<{
  spec: SoftwareSpecification | null;
  blueprints?: FactoryBlueprintSummary[];
  selectedModule?: string | null;
}>();

// 受控折叠状态：默认全部关闭（不默认展开任何功能大类或具体接口）
const expandedGroups = reactive<Record<string, boolean>>({});
const expandedEndpoints = reactive<Record<string, boolean>>({});

const endpoints = computed(() => synthesizeDesignedApis(props.spec, props.blueprints ?? []));

interface FeatureApiGroup {
  moduleId: string;
  title: string;
  endpoints: DesignedApiEndpoint[];
}

function groupDisplayTitle(moduleId: string, items: DesignedApiEndpoint[]): string {
  const entities = Array.from(new Set(items.map((ep) => ep.entityName).filter(Boolean)));
  if (entities.length === 1 && entities[0] === 'Auth') {
    return '身份认证与鉴权';
  }
  if (entities.length > 0) {
    return entities.join(' / ');
  }
  return moduleId;
}

const featureGroups = computed<FeatureApiGroup[]>(() => {
  const map = new Map<string, DesignedApiEndpoint[]>();
  for (const ep of endpoints.value) {
    const list = map.get(ep.moduleId);
    if (list) {
      list.push(ep);
    } else {
      map.set(ep.moduleId, [ep]);
    }
  }
  return Array.from(map.entries()).map(([moduleId, items]) => ({
    moduleId,
    title: groupDisplayTitle(moduleId, items),
    endpoints: items,
  }));
});

function isGroupOpen(moduleId: string): boolean {
  return expandedGroups[moduleId] === true;
}

function toggleGroup(moduleId: string): void {
  expandedGroups[moduleId] = !isGroupOpen(moduleId);
}

function isEndpointOpen(endpointId: string): boolean {
  return expandedEndpoints[endpointId] === true;
}

function toggleEndpoint(endpointId: string): void {
  expandedEndpoints[endpointId] = !isEndpointOpen(endpointId);
}

function methodClass(method: HttpMethod): string {
  switch (method) {
    case 'GET':
      return 'm-get';
    case 'POST':
      return 'm-post';
    case 'PUT':
      return 'm-put';
    case 'DELETE':
      return 'm-del';
  }
}

watch(
  () => props.selectedModule,
  (mod) => {
    if (!mod) return;
    const found = featureGroups.value.find(
      (g) =>
        g.moduleId.toLowerCase() === mod.toLowerCase() ||
        mod.toLowerCase().includes(g.moduleId.toLowerCase()),
    );
    if (found) {
      expandedGroups[found.moduleId] = true;
    }
  },
);
</script>

<template>
  <div class="api-panel" data-testid="designed-apis-panel">
    <div class="api-header-card">
      <div class="api-header-top">
        <span class="api-title">设计接口</span>
        <span class="af-mono api-count">{{ endpoints.length }} APIs</span>
      </div>
    </div>

    <div v-if="!featureGroups.length" class="api-empty">
      暂无接口
    </div>

    <!-- Feature-grouped outer dropdowns + nested endpoint dropdowns (all collapsed by default) -->
    <div v-else class="api-group-list">
      <div
        v-for="group in featureGroups"
        :key="group.moduleId"
        class="api-group"
        :class="{ open: isGroupOpen(group.moduleId) }"
        :data-testid="`api-group-${group.moduleId}`"
      >
        <button
          type="button"
          class="api-group-summary"
          :aria-expanded="isGroupOpen(group.moduleId)"
          :data-testid="`api-group-toggle-${group.moduleId}`"
          @click="toggleGroup(group.moduleId)"
        >
          <span class="group-caret" aria-hidden="true">▸</span>
          <span class="group-title">{{ group.title }}</span>
          <span class="group-mod af-mono">{{ group.moduleId }}</span>
          <span class="group-count af-mono">{{ group.endpoints.length }}</span>
        </button>

        <div v-if="isGroupOpen(group.moduleId)" class="api-group-body">
          <div
            v-for="ep in group.endpoints"
            :key="ep.id"
            class="api-item"
            :class="{ open: isEndpointOpen(ep.id) }"
            :data-testid="`designed-api-${ep.id}`"
          >
            <button
              type="button"
              class="api-summary"
              :aria-expanded="isEndpointOpen(ep.id)"
              :data-testid="`designed-api-toggle-${ep.id}`"
              @click="toggleEndpoint(ep.id)"
            >
              <span class="item-caret" aria-hidden="true">▸</span>
              <span class="api-method af-mono" :class="methodClass(ep.method)">{{ ep.method }}</span>
              <span class="api-path af-mono">{{ ep.path }}</span>
            </button>

            <div
              v-if="isEndpointOpen(ep.id)"
              class="api-detail"
              :data-testid="`designed-api-detail-${ep.id}`"
            >
              <div class="api-desc">{{ ep.summary }}</div>
              <div class="api-meta af-mono">
                <span>权限：{{ ep.roles.join(' / ') }}</span>
                <span v-if="ep.ruleIds.length">规则：{{ ep.ruleIds.join(', ') }}</span>
              </div>
              <div class="api-schema af-mono">
                <div><span class="schema-k">Req:</span> {{ ep.requestSummary }}</div>
                <div><span class="schema-k">Res:</span> <span class="schema-ok">{{ ep.responseSummary }}</span></div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Blueprint Modules Summary when available -->
    <div v-if="blueprints && blueprints.length" class="bp-card" data-testid="blueprint-summary-card">
      <div class="api-title">工厂蓝图与沙箱集成边界</div>
      <div v-for="bp in blueprints" :key="`${bp.phase}-v${bp.version}`" class="bp-block">
        <div class="bp-top">
          <span class="bp-name">
            {{ bp.phase === 'frontend' ? '前端蓝图' : '后端蓝图' }} v{{ bp.version }}
          </span>
          <span class="af-mono bp-hash">{{ bp.blueprint_hash.slice(0, 8) }} · {{ bp.status }}</span>
        </div>
        <div class="bp-mods">
          <span v-for="mod in bp.module_ids" :key="mod" class="bp-mod-tag af-mono">{{ mod }}</span>
        </div>
        <div v-if="bp.sandbox_integrations.length" class="bp-sandbox">
          沙箱能力：{{ bp.sandbox_integrations.length }} 项（本地模拟运行）
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.api-panel {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.api-header-card,
.bp-card {
  border-radius: 10px;
  border: 1px solid var(--af-border-strong);
  background: var(--af-surface);
  padding: 11px;
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.api-header-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.api-title {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--af-fg);
}

.api-count {
  font-size: 11px;
  padding: 1px 7px;
  border-radius: 99px;
  background: var(--af-accent-soft);
  color: var(--af-accent);
  font-weight: 600;
}

.api-sub {
  margin: 0;
  font-size: 11.5px;
  line-height: 1.45;
  color: var(--af-muted);
}

.api-empty {
  border-radius: 10px;
  border: 1px dashed var(--af-border-strong);
  padding: 20px 14px;
  font-size: 12px;
  color: var(--af-muted);
  text-align: center;
  line-height: 1.5;
}

/* Outer Feature Dropdown */
.api-group-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.api-group {
  border-radius: 10px;
  border: 1px solid var(--af-border-strong);
  background: var(--af-surface);
  overflow: hidden;
}

.api-group-summary {
  width: 100%;
  border: none;
  cursor: pointer;
  padding: 9px 11px;
  display: flex;
  align-items: center;
  gap: 7px;
  background: var(--af-surface);
  color: var(--af-fg);
  text-align: left;
  transition: background 120ms ease;
}

.api-group-summary:hover {
  background: var(--af-surface-hover);
}

.group-caret,
.item-caret {
  font-size: 11px;
  color: var(--af-muted);
  display: inline-block;
  transition: transform 140ms ease;
  flex-shrink: 0;
}

.api-group.open > .api-group-summary .group-caret,
.api-item.open > .api-summary .item-caret {
  transform: rotate(90deg);
  color: var(--af-accent);
}

.group-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--af-fg);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.group-mod {
  font-size: 10.5px;
  color: var(--af-muted);
  margin-left: auto;
  flex-shrink: 0;
}

.group-count {
  font-size: 10.5px;
  padding: 1px 6px;
  border-radius: 99px;
  background: var(--af-surface-2);
  border: 1px solid var(--af-border);
  color: var(--af-fg-secondary);
  flex-shrink: 0;
}

.api-group-body {
  padding: 8px;
  border-top: 1px solid var(--af-border);
  background: var(--af-bg);
  display: flex;
  flex-direction: column;
  gap: 6px;
}

/* Nested Endpoint Dropdown */
.api-item {
  border-radius: 8px;
  border: 1px solid var(--af-border);
  background: var(--af-surface);
  overflow: hidden;
}

.api-summary {
  width: 100%;
  border: none;
  cursor: pointer;
  padding: 7px 9px;
  display: flex;
  align-items: center;
  gap: 6px;
  background: transparent;
  color: var(--af-fg);
  text-align: left;
}

.api-summary:hover {
  background: var(--af-surface-hover);
}

.api-method {
  font-size: 10px;
  font-weight: 700;
  padding: 2px 5px;
  border-radius: 4px;
  flex-shrink: 0;
}

.m-get {
  background: var(--af-accent-soft);
  color: var(--af-accent);
}
.m-post {
  background: var(--af-ok-soft);
  color: var(--af-ok);
}
.m-put {
  background: var(--af-warn-soft);
  color: var(--af-warn);
}
.m-del {
  background: var(--af-err-soft);
  color: var(--af-err);
}

.api-path {
  font-size: 11.5px;
  font-weight: 500;
  color: var(--af-fg);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
}

.api-detail {
  padding: 8px 10px 10px;
  border-top: 1px solid var(--af-border);
  background: var(--af-surface-2);
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.api-desc {
  font-size: 11.5px;
  color: var(--af-fg);
}

.api-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  font-size: 10.5px;
  color: var(--af-muted);
}

.api-schema {
  font-size: 10.5px;
  color: var(--af-fg-secondary);
  display: flex;
  flex-direction: column;
  gap: 2px;
  word-break: break-word;
}

.schema-k {
  color: var(--af-muted);
}

.schema-ok {
  color: var(--af-ok);
}

.bp-block {
  padding: 8px;
  border-radius: 7px;
  background: var(--af-surface-2);
  border: 1px solid var(--af-border);
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.bp-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  font-size: 11.5px;
}

.bp-name {
  font-weight: 600;
}

.bp-hash {
  font-size: 10.5px;
  color: var(--af-muted);
}

.bp-mods {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}

.bp-mod-tag {
  font-size: 10.5px;
  padding: 2px 6px;
  border-radius: 5px;
  border: 1px solid var(--af-border-strong);
  background: var(--af-surface);
  color: var(--af-fg-secondary);
}

.bp-sandbox {
  font-size: 11px;
  color: var(--af-warn);
}
</style>
