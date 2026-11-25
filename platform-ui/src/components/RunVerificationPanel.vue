<script setup lang="ts">
import { computed } from 'vue';

import { runsApi } from '@/api/client';
import type { FactoryRunView, RunDetail } from '@/api/types';
import SafeText from '@/components/SafeText.vue';

const props = defineProps<{
  run: RunDetail | null;
  factoryData?: FactoryRunView | null;
}>();

const CODE_DELIVERY_LABELS: Record<string, string> = {
  PASS: '代码交付通过',
  FAIL: '代码交付失败',
  NOT_RUN: '代码交付未运行',
};

const READINESS_LABELS: Record<string, string> = {
  NOT_REQUIRED: '无外部服务',
  MOCK_ONLY: '仅 Mock 已验证',
  AWAITING_USER_CONFIG: '待配置 Live 凭据',
  LIVE_VERIFIED: 'Live 已验证',
  LIVE_FAILED: 'Live 验证失败',
};

function checkTagType(status: string): 'success' | 'danger' | 'info' {
  if (status === 'PASS') return 'success';
  if (status === 'FAIL') return 'danger';
  return 'info';
}

function readinessTagType(readiness: string): 'success' | 'danger' | 'warning' | 'info' {
  if (readiness === 'LIVE_VERIFIED') return 'success';
  if (readiness === 'LIVE_FAILED') return 'danger';
  if (readiness === 'AWAITING_USER_CONFIG') return 'warning';
  return 'info';
}

const zipArtifact = computed(() => props.run?.artifacts.find((item) => item.kind === 'zip') ?? null);
const notRunChecks = computed(
  () => props.run?.verification?.checks?.filter((check) => check.status === 'NOT_RUN') ?? [],
);
const externalServices = computed(() => props.run?.external_services ?? []);
const codeDeliveryLabel = computed(() =>
  props.run
    ? (CODE_DELIVERY_LABELS[props.run.code_delivery_status] ?? props.run.code_delivery_status)
    : '',
);
const readinessLabel = computed(() =>
  props.run
    ? (READINESS_LABELS[props.run.external_readiness] ?? props.run.external_readiness)
    : '',
);
</script>

<template>
  <div class="verify-panel" data-testid="verification-delivery-panel">
    <div v-if="!run" class="ins-empty">
      暂无验收记录
    </div>

    <template v-else>
      <!-- Defects -->
      <div
        v-if="factoryData && (factoryData.open_defects.length || factoryData.resolved_defects.length)"
        class="ins-card"
        data-testid="factory-defects"
      >
        <h3 class="section-title">
          缺陷单追踪（未解决 {{ factoryData.open_defects.length }} / 已解决 {{ factoryData.resolved_defects.length }}）
        </h3>
        <div v-if="factoryData.open_defects.length" class="defect-list" data-testid="open-defects">
          <div
            v-for="defect in factoryData.open_defects"
            :key="`open-${defect.id}`"
            class="defect-item defect-open"
          >
            <div class="defect-head">
              <el-tag type="danger" size="small" disable-transitions>未解决</el-tag>
              <span class="defect-meta af-mono">
                [{{ defect.phase }} / {{ defect.dept_id }} / 第 {{ defect.round }} 轮 / {{ defect.scope }}]
                {{ defect.error_class }}
              </span>
            </div>
            <SafeText :text="defect.summary" />
            <SafeText v-if="defect.log_excerpt" :text="defect.log_excerpt" class="defect-log af-mono" />
          </div>
        </div>
        <div v-if="factoryData.resolved_defects.length" class="defect-list" data-testid="resolved-defects">
          <div
            v-for="defect in factoryData.resolved_defects"
            :key="`res-${defect.id}`"
            class="defect-item defect-resolved"
          >
            <div class="defect-head">
              <el-tag type="success" size="small" disable-transitions>已解决</el-tag>
              <span class="defect-meta af-mono">
                [{{ defect.phase }} / {{ defect.dept_id }} / 第 {{ defect.round }} 轮 / {{ defect.scope }}]
                {{ defect.error_class }}
              </span>
            </div>
            <SafeText :text="defect.summary" />
            <SafeText v-if="defect.log_excerpt" :text="defect.log_excerpt" class="defect-log af-mono" />
          </div>
        </div>
      </div>

      <!-- Code Delivery & External Service Readiness -->
      <div class="ins-card" data-testid="external-readiness">
        <h3 class="section-title">代码交付与外部服务就绪</h3>
        <div class="readiness-grid">
          <div class="readiness-box">
            <span class="metric-label">代码交付</span>
            <el-tag
              :type="checkTagType(run.code_delivery_status)"
              size="small"
              disable-transitions
              data-testid="code-delivery-status"
            >
              {{ codeDeliveryLabel }}
            </el-tag>
          </div>
          <div class="readiness-box">
            <span class="metric-label">外部服务就绪</span>
            <el-tag
              :type="readinessTagType(run.external_readiness)"
              size="small"
              disable-transitions
              data-testid="external-readiness-status"
            >
              {{ readinessLabel }}
            </el-tag>
          </div>
        </div>
        <p v-if="run.external_notice" class="unknown-note" data-testid="external-notice">
          {{ run.external_notice }}
        </p>
        <el-table
          v-if="externalServices.length"
          :data="externalServices"
          size="small"
          border
          data-testid="external-services"
        >
          <el-table-column prop="name" label="服务" />
          <el-table-column label="Mock" width="76">
            <template #default="{ row }">
              <el-tag :type="checkTagType(row.mock_status)" size="small" disable-transitions>
                {{ row.mock_status }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="Live" width="76">
            <template #default="{ row }">
              <el-tag :type="checkTagType(row.live_status)" size="small" disable-transitions>
                {{ row.live_status }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="配置" width="76">
            <template #default="{ row }">
              <el-tag :type="row.config_present ? 'success' : 'info'" size="small" disable-transitions>
                {{ row.config_present ? '已配置' : '待配置' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="reason" label="说明" />
        </el-table>
        <p v-else class="ins-muted">无第三方外部服务，无须配置。</p>
      </div>

      <!-- ZIP Artifact Download -->
      <div class="ins-card">
        <h3 class="section-title">交付产物</h3>
        <template v-if="zipArtifact">
          <div class="download-row">
            <a class="download-btn" :href="runsApi.artifactUrl(run.id)" data-testid="download-link">
              下载源码包（ZIP）
            </a>
            <el-tag :type="zipArtifact.verified ? 'success' : 'warning'" size="small" disable-transitions>
              {{ zipArtifact.verified ? '已验证' : '未验证' }}
            </el-tag>
          </div>
          <span class="artifact-hash af-mono">sha256 {{ zipArtifact.sha256.slice(0, 12) }}</span>
          <p v-if="!zipArtifact.verified" class="unknown-note">
            该交付包未通过全部必需检查，标注为“未验证”，请以 verification 报告为准。
          </p>
        </template>
        <p v-else class="ins-muted">尚无可下载的产物（运行完成或失败后生成）。</p>
      </div>

      <!-- Verification Report -->
      <div v-if="run.verification" class="ins-card">
        <h3 class="section-title">验证报告（{{ run.verification.label ?? '—' }}）</h3>
        <el-table :data="run.verification.checks ?? []" size="small" border>
          <el-table-column prop="name" label="检查项" />
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag
                :type="row.status === 'PASS' ? 'success' : row.status === 'NOT_RUN' ? 'info' : 'danger'"
                size="small"
                disable-transitions
              >
                {{ row.status }}
              </el-tag>
            </template>
          </el-table-column>
        </el-table>
        <p v-if="notRunChecks.length" class="unknown-note">
          {{ notRunChecks.length }} 项检查未运行（NOT_RUN）：{{ notRunChecks.map((c) => c.name).join('、') }}
        </p>
      </div>
    </template>
  </div>
</template>

<style scoped>
.verify-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.ins-empty {
  padding: 20px 12px;
  border-radius: 9px;
  border: 1px dashed var(--af-border-strong);
  font-size: 12px;
  color: var(--af-muted);
  text-align: center;
  line-height: 1.55;
}

.ins-card {
  border-radius: 10px;
  border: 1px solid var(--af-border);
  background: var(--af-surface);
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.ins-card .section-title {
  margin: 0;
  font-size: 12.5px;
}

.ins-muted {
  margin: 0;
  font-size: 12px;
  color: var(--af-muted);
}

.unknown-note {
  margin: 0;
  font-size: 11.5px;
  color: var(--af-warn);
  line-height: 1.5;
}

.readiness-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 8px;
}

.readiness-box {
  border-radius: 8px;
  border: 1px solid var(--af-border);
  background: var(--af-surface-2);
  padding: 8px;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 5px;
}

.metric-label {
  font-size: 11px;
  color: var(--af-muted);
}

.download-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.download-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 6px 12px;
  border-radius: 7px;
  border: 1px solid var(--af-btn-primary-bg);
  background: var(--af-btn-primary-bg);
  color: var(--af-btn-primary-fg);
  font-size: 12px;
  font-weight: 500;
  text-decoration: none;
}

.download-btn:hover {
  background: var(--af-btn-primary-hover);
}

.artifact-hash {
  font-size: 11px;
  color: var(--af-muted);
}

.defect-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.defect-item {
  border-radius: 8px;
  padding: 8px 10px;
  border: 1px solid var(--af-border);
  font-size: 12px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.defect-open {
  background: var(--af-err-soft);
  border-color: var(--af-err-border);
}

.defect-resolved {
  background: var(--af-ok-soft);
  border-color: var(--af-ok-border);
}

.defect-head {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.defect-meta {
  font-size: 11px;
  color: var(--af-fg-secondary);
}

.defect-log {
  margin-top: 2px;
  padding: 6px 8px;
  border-radius: 6px;
  background: var(--af-bg);
  color: var(--af-muted);
  font-size: 11px;
  overflow-x: auto;
}
</style>
