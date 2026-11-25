import { type InjectionKey, reactive, type Ref } from 'vue';

import { modelsApi, profilesApi, projectsApi, runsApi } from '@/api/client';
import type {
  CostSummary,
  CustomModelPresetCreatePayload,
  FactoryRoutingRole,
  ModelConfigView,
  ModelRoleUpdateItem,
  ProfileOption,
  ProjectSummary,
  RunMode,
  RunView,
} from '@/api/types';

export const INSPECTOR_HOST_KEY: InjectionKey<Ref<boolean>> = Symbol('af-inspector-host');

export const LEFT_SIDEBAR_MIN = 200;
export const LEFT_SIDEBAR_MAX = 480;
export const LEFT_SIDEBAR_DEFAULT = 264;

export const RIGHT_SIDEBAR_MIN = 280;
export const RIGHT_SIDEBAR_MAX = 640;
export const RIGHT_SIDEBAR_DEFAULT = 380;

const STORAGE_LEFT_WIDTH = 'af-left-sidebar-width';
const STORAGE_RIGHT_WIDTH = 'af-right-sidebar-width';

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, Math.round(value)));
}

function readStoredWidth(key: string, fallback: number, min: number, max: number): number {
  if (typeof window === 'undefined' || !window.localStorage) {
    return fallback;
  }
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return fallback;
    const parsed = Number(raw);
    if (!Number.isFinite(parsed)) return fallback;
    return clamp(parsed, min, max);
  } catch {
    return fallback;
  }
}

function persistWidth(key: string, value: number): void {
  if (typeof window === 'undefined' || !window.localStorage) {
    return;
  }
  try {
    window.localStorage.setItem(key, String(value));
  } catch {
    // ignore storage errors in private mode
  }
}

export interface WorkspaceState {
  projects: ProjectSummary[];
  projectsLoading: boolean;
  projectRuns: Record<number, RunView[]>;
  projectRoutingSnapshots: Record<number, { runId: number; roles: FactoryRoutingRole[] }>;
  expandedProjectIds: Record<number, boolean>;
  defaultRunMode: RunMode;
  modelConfig: ModelConfigView | null;
  modelConfigLoading: boolean;
  profiles: ProfileOption[];
  activeProjectId: number | null;
  activeProjectName: string;
  activeStageLabel: string;
  activeStageTone: 'muted' | 'accent' | 'ok' | 'warn' | 'err';
  activeCost: CostSummary | null;
  selectedFlowNode: string | null;
  leftSidebarWidth: number;
  rightSidebarWidth: number;
  rightPanelOpen: boolean;
}

const state = reactive<WorkspaceState>({
  projects: [],
  projectsLoading: false,
  projectRuns: {},
  projectRoutingSnapshots: {},
  expandedProjectIds: {},
  defaultRunMode: 'standard',
  modelConfig: null,
  modelConfigLoading: false,
  profiles: [],
  activeProjectId: null,
  activeProjectName: '',
  activeStageLabel: '',
  activeStageTone: 'muted',
  activeCost: null,
  selectedFlowNode: null,
  leftSidebarWidth: readStoredWidth(
    STORAGE_LEFT_WIDTH,
    LEFT_SIDEBAR_DEFAULT,
    LEFT_SIDEBAR_MIN,
    LEFT_SIDEBAR_MAX,
  ),
  rightSidebarWidth: readStoredWidth(
    STORAGE_RIGHT_WIDTH,
    RIGHT_SIDEBAR_DEFAULT,
    RIGHT_SIDEBAR_MIN,
    RIGHT_SIDEBAR_MAX,
  ),
  // 右侧边栏默认不显示，由用户点击顶栏开关或点击规格/节点时按需展开
  rightPanelOpen: false,
});

export function useWorkspaceStore(): WorkspaceState {
  return state;
}

export function setLeftSidebarWidth(width: number): void {
  const next = clamp(width, LEFT_SIDEBAR_MIN, LEFT_SIDEBAR_MAX);
  state.leftSidebarWidth = next;
  persistWidth(STORAGE_LEFT_WIDTH, next);
}

export function setRightSidebarWidth(width: number): void {
  const next = clamp(width, RIGHT_SIDEBAR_MIN, RIGHT_SIDEBAR_MAX);
  state.rightSidebarWidth = next;
  persistWidth(STORAGE_RIGHT_WIDTH, next);
}

export function setRightPanelOpen(open: boolean): void {
  state.rightPanelOpen = open;
}

export function toggleRightPanel(): void {
  state.rightPanelOpen = !state.rightPanelOpen;
}

export function resetSidebarLayout(): void {
  state.leftSidebarWidth = LEFT_SIDEBAR_DEFAULT;
  state.rightSidebarWidth = RIGHT_SIDEBAR_DEFAULT;
  state.rightPanelOpen = false;
  persistWidth(STORAGE_LEFT_WIDTH, LEFT_SIDEBAR_DEFAULT);
  persistWidth(STORAGE_RIGHT_WIDTH, RIGHT_SIDEBAR_DEFAULT);
}

export async function refreshProjects(): Promise<void> {
  if (typeof projectsApi.list !== 'function') {
    return;
  }
  state.projectsLoading = true;
  try {
    const res = await projectsApi.list();
    state.projects = res.items ?? [];
  } catch {
    // 侧边栏加载失败不阻断主视图
  } finally {
    state.projectsLoading = false;
  }
}

export async function deleteProject(projectId: number): Promise<void> {
  await projectsApi.remove(projectId);
  state.projects = state.projects.filter((p) => p.id !== projectId);
  delete state.projectRuns[projectId];
  delete state.expandedProjectIds[projectId];
  delete state.projectRoutingSnapshots[projectId];
  if (state.activeProjectId === projectId) {
    state.activeProjectId = null;
    state.activeProjectName = '';
    state.activeStageLabel = '';
    state.activeCost = null;
  }
}

export async function refreshProjectRuns(projectId: number): Promise<void> {
  if (typeof runsApi.listByProject !== 'function') {
    return;
  }
  try {
    const res = await runsApi.listByProject(projectId);
    state.projectRuns[projectId] = res.items ?? [];
  } catch {
    // ignore sidebar sub-query errors
  }
}

export async function refreshModelConfig(): Promise<void> {
  if (typeof modelsApi.getConfig !== 'function') {
    return;
  }
  state.modelConfigLoading = true;
  try {
    state.modelConfig = await modelsApi.getConfig();
  } catch {
    // ignore if unauthenticated or endpoint unavailable in legacy mock
  } finally {
    state.modelConfigLoading = false;
  }
}

export async function saveModelConfig(roles: ModelRoleUpdateItem[]): Promise<ModelConfigView> {
  const updated = await modelsApi.updateConfig(roles);
  state.modelConfig = updated;
  return updated;
}

export async function addCustomModelPreset(
  preset: CustomModelPresetCreatePayload,
): Promise<ModelConfigView> {
  const updated = await modelsApi.addPreset(preset);
  state.modelConfig = updated;
  return updated;
}

export async function deleteCustomModelPreset(presetId: string): Promise<ModelConfigView> {
  const updated = await modelsApi.deletePreset(presetId);
  state.modelConfig = updated;
  return updated;
}

export async function refreshProfiles(): Promise<void> {
  if (typeof profilesApi.list !== 'function') {
    return;
  }
  try {
    state.profiles = await profilesApi.list();
  } catch {
    // ignore
  }
}

export function setActiveHeaderContext(ctx: {
  projectId?: number | null;
  projectName?: string;
  stageLabel?: string;
  stageTone?: WorkspaceState['activeStageTone'];
  cost?: CostSummary | null;
}): void {
  if (ctx.projectId !== undefined) state.activeProjectId = ctx.projectId;
  if (ctx.projectName !== undefined) state.activeProjectName = ctx.projectName;
  if (ctx.stageLabel !== undefined) state.activeStageLabel = ctx.stageLabel;
  if (ctx.stageTone !== undefined) state.activeStageTone = ctx.stageTone;
  if (ctx.cost !== undefined) state.activeCost = ctx.cost;
}

export function setSelectedFlowNode(nodeId: string | null): void {
  state.selectedFlowNode = nodeId;
}

export function setDefaultRunMode(mode: RunMode): void {
  state.defaultRunMode = mode;
}

export function setProjectRoutingSnapshot(
  projectId: number,
  runId: number,
  roles: FactoryRoutingRole[],
): void {
  if (!roles.length) return;
  state.projectRoutingSnapshots[projectId] = { runId, roles };
}
