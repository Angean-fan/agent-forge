import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { flushPromises, mount } from '@vue/test-utils';
import ElementPlus, { ElMessageBox } from 'element-plus';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { nextTick } from 'vue';

import { modelsApi, profilesApi, projectsApi, runsApi } from '@/api/client';
import type { SoftwareSpecification } from '@/api/types';
import App from '@/App.vue';
import DesignedApisPanel from '@/components/DesignedApisPanel.vue';
import WorkbenchSidebar from '@/components/WorkbenchSidebar.vue';
import { setTheme, toggleTheme, useTheme } from '@/stores/theme';
import {
  LEFT_SIDEBAR_DEFAULT,
  LEFT_SIDEBAR_MAX,
  LEFT_SIDEBAR_MIN,
  resetSidebarLayout,
  RIGHT_SIDEBAR_DEFAULT,
  RIGHT_SIDEBAR_MAX,
  RIGHT_SIDEBAR_MIN,
  useWorkspaceStore,
} from '@/stores/workspace';
import { synthesizeDesignedApis } from '@/utils/apiDesign';
import ProjectCreateView from '@/views/ProjectCreateView.vue';
import ProjectDetailView from '@/views/ProjectDetailView.vue';
import RunDetailView from '@/views/RunDetailView.vue';

import { makeTestRouter } from './helpers';

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>();
  return {
    ...actual,
    projectsApi: {
      list: vi.fn(),
      create: vi.fn(),
      get: vi.fn(),
      remove: vi.fn(),
      getSpec: vi.fn(),
      extract: vi.fn(),
      saveSpec: vi.fn(),
      confirm: vi.fn(),
    },
    runsApi: {
      start: vi.fn(),
      get: vi.fn(),
      listByProject: vi.fn(),
      factory: vi.fn(),
      events: vi.fn(),
      cancel: vi.fn(),
      resume: vi.fn(),
      artifactUrl: (id: number) => `/api/runs/${id}/artifact`,
    },
    profilesApi: {
      list: vi.fn(),
      explain: vi.fn(),
    },
    modelsApi: {
      getConfig: vi.fn(),
      updateConfig: vi.fn(),
      addPreset: vi.fn(),
      deletePreset: vi.fn(),
    },
  };
});

function sampleSpec(): SoftwareSpecification {
  return {
    schema_version: '0.1',
    project_type: 'crud_web',
    goal: '管理社区活动与报名',
    actors: ['ADMIN', 'MEMBER'],
    entities: [
      {
        name: 'Activity',
        source: 'user_confirmed',
        fields: [
          { name: 'id', type: 'long', required: true },
          { name: 'title', type: 'string', required: true },
          { name: 'capacity', type: 'int', required: true },
        ],
      },
      {
        name: 'Registration',
        source: 'user_confirmed',
        fields: [
          { name: 'id', type: 'long', required: true },
          { name: 'activityId', type: 'long', required: true },
        ],
      },
    ],
    relations: [],
    pages: [{ name: 'ActivitiesPage', entity: 'Activity', path: '/activities' }],
    permissions: [
      { role: 'ADMIN', actions: ['create', 'read', 'update', 'delete'], source: 'user_confirmed' },
      { role: 'MEMBER', actions: ['read', 'create'], source: 'user_confirmed' },
    ],
    business_rules: [
      { id: 'BR01', text: 'Activity 容量达到上限后禁止新 Registration', source: 'user_confirmed' },
    ],
    acceptance_cases: [],
    technical_profile: {
      profile_id: 'demo-ts-mp',
      profile_version: '0.3',
      template_version: 'demo-0.2',
    },
    constraints: [],
    exclusions: [],
    capability_requests: [],
    recipe_requests: [],
    integrations: [],
    external_requirements: [],
  };
}

describe('Workbench redesign: theme, API synthesis, sidebar model config & XSS safety', () => {
  beforeEach(() => {
    setTheme('dark');
    vi.mocked(projectsApi.list).mockReset();
    vi.mocked(runsApi.listByProject).mockReset();
    vi.mocked(profilesApi.list).mockReset();
    vi.mocked(modelsApi.getConfig).mockReset();
    vi.mocked(modelsApi.updateConfig).mockReset();
    vi.mocked(modelsApi.addPreset).mockReset();
    vi.mocked(modelsApi.deletePreset).mockReset();
  });

  it('toggles theme between dark and light and updates documentElement data-theme', () => {
    const { theme } = useTheme();
    expect(theme.value).toBe('dark');
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark');

    toggleTheme();
    expect(theme.value).toBe('light');
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');

    toggleTheme();
    expect(theme.value).toBe('dark');
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark');
  });

  it('synthesizes REST API endpoints from SoftwareSpecification and overlays blueprint modules', () => {
    const apis = synthesizeDesignedApis(sampleSpec(), [
      {
        phase: 'backend',
        version: 1,
        status: 'APPROVED_FOR_BUILD',
        blueprint_hash: 'abc123456789',
        module_count: 2,
        module_ids: ['activity-core', 'registration-svc'],
        sandbox_integrations: [],
      },
    ]);

    expect(apis.length).toBeGreaterThanOrEqual(12);
    const listActivity = apis.find((e) => e.method === 'GET' && e.path === '/api/v1/activities');
    expect(listActivity).toBeDefined();
    expect(listActivity?.moduleId).toBe('activity-core');
    expect(listActivity?.roles).toEqual(['ADMIN', 'MEMBER']);
    expect(listActivity?.ruleIds).toContain('BR01');

    const createReg = apis.find((e) => e.method === 'POST' && e.path === '/api/v1/registrations');
    expect(createReg).toBeDefined();
    expect(createReg?.moduleId).toBe('registration-svc');
    expect(createReg?.source).toBe('blueprint');
  });

  it('loads and edits A04 model configuration inside left sidebar via modelsApi', async () => {
    vi.mocked(projectsApi.list).mockResolvedValue({
      items: [{ id: 1, name: '社区活动报名', active_spec_id: 3, created_at: '2026-11-25T10:00:00' }],
    });
    vi.mocked(profilesApi.list).mockResolvedValue([]);
    vi.mocked(modelsApi.getConfig).mockResolvedValue({
      config_source: 'custom',
      writable: true,
      roles: [
        {
          role: 'dept_coder',
          label: '部门编码引擎',
          provider: 'deepseek_official',
          model: 'deepseek-flash',
          base_url: 'https://api.deepseek.com/v1',
          base_url_host: 'api.deepseek.com',
          api_key_env: 'DEEPSEEK_API_KEY',
          api_key_present: true,
          enabled: true,
          supports_tools: true,
          max_output_tokens: 8192,
          timeout_seconds: 180,
          source: 'custom',
        },
      ],
    });
    vi.mocked(modelsApi.updateConfig).mockResolvedValue({
      config_source: 'custom',
      writable: true,
      roles: [
        {
          role: 'dept_coder',
          label: '部门编码引擎',
          provider: 'deepseek_official',
          model: 'deepseek-chat',
          base_url: 'https://api.deepseek.com/v1',
          base_url_host: 'api.deepseek.com',
          api_key_env: 'DEEPSEEK_API_KEY',
          api_key_present: true,
          enabled: true,
          supports_tools: true,
          max_output_tokens: 8192,
          timeout_seconds: 180,
          source: 'custom',
        },
      ],
    });

    const router = makeTestRouter();
    await router.push('/');
    await router.isReady();

    const wrapper = mount(WorkbenchSidebar, {
      global: { plugins: [ElementPlus, router] },
    });
    await flushPromises();
    await nextTick();

    expect(wrapper.text()).toContain('社区活动报名');
    await wrapper.get('[data-testid="sidebar-tab-models"]').trigger('click');
    await nextTick();

    const roleCard = wrapper.get('[data-testid="model-role-dept_coder"]');
    await roleCard.get('.sb-role-summary').trigger('click');
    await nextTick();

    await wrapper.get('[data-testid="edit-role-model"]').setValue('deepseek-chat');
    await wrapper.get('[data-testid="save-model-role"]').trigger('click');
    await flushPromises();

    expect(modelsApi.updateConfig).toHaveBeenCalledTimes(1);
    expect(vi.mocked(modelsApi.updateConfig).mock.calls[0][0][0].model).toBe('deepseek-chat');
  });

  it('renders 2:5:3 workbench layout with one-click theme toggle on authenticated routes', async () => {
    vi.mocked(projectsApi.list).mockResolvedValue({ items: [] });
    vi.mocked(profilesApi.list).mockResolvedValue([]);
    vi.mocked(modelsApi.getConfig).mockResolvedValue({
      config_source: 'default',
      writable: true,
      roles: [],
    });

    const router = makeTestRouter();
    await router.push('/');
    await router.isReady();

    const wrapper = mount(App, {
      global: { plugins: [ElementPlus, router] },
    });
    await flushPromises();
    await nextTick();

    expect(wrapper.find('[data-testid="workbench-grid"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="workbench-sidebar"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="workbench-inspector"]').exists()).toBe(true);

    await wrapper.get('[data-testid="theme-toggle"]').trigger('click');
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');
  });

  it('contains zero v-html across all redesigned Vue files', () => {
    const files = [
      '../src/App.vue',
      '../src/components/WorkbenchSidebar.vue',
      '../src/components/DesignedApisPanel.vue',
      '../src/components/FactoryFlowchart.vue',
      '../src/components/RunEventsPanel.vue',
      '../src/components/RunVerificationPanel.vue',
      '../src/components/StandardAssemblyTodo.vue',
      '../src/views/ProjectCreateView.vue',
      '../src/views/ProjectDetailView.vue',
      '../src/views/ProjectListView.vue',
      '../src/views/RunDetailView.vue',
    ];
    for (const rel of files) {
      const src = readFileSync(resolve(__dirname, rel), 'utf-8');
      expect(src).not.toContain('v-html');
    }
  });

  it('hides right inspector by default, toggles it, and resizes both sidebars via drag handles', async () => {
    resetSidebarLayout();
    vi.mocked(projectsApi.list).mockResolvedValue({ items: [] });
    vi.mocked(profilesApi.list).mockResolvedValue([]);
    vi.mocked(modelsApi.getConfig).mockResolvedValue({
      config_source: 'default',
      writable: true,
      roles: [],
    });

    const router = makeTestRouter();
    await router.push('/projects/new');
    await router.isReady();

    const wrapper = mount(App, {
      global: { plugins: [ElementPlus, router] },
    });
    await flushPromises();
    await nextTick();

    const workspace = useWorkspaceStore();
    const inspector = wrapper.get('[data-testid="workbench-inspector"]');
    // 右侧边栏默认不显示
    expect(workspace.rightPanelOpen).toBe(false);
    expect((inspector.element as HTMLElement).style.display).toBe('none');

    // 点击顶栏按钮展开右侧边栏
    await wrapper.get('[data-testid="toggle-right-panel"]').trigger('click');
    await nextTick();
    expect(workspace.rightPanelOpen).toBe(true);
    expect((inspector.element as HTMLElement).style.display).not.toBe('none');

    // 拖拽左侧分割线调整左侧栏宽度（并验证上限 clamp）
    const leftResizer = wrapper.get('[data-testid="resize-left"]');
    await leftResizer.trigger('mousedown', { clientX: 264 });
    window.dispatchEvent(new MouseEvent('mousemove', { clientX: 344 }));
    window.dispatchEvent(new MouseEvent('mouseup'));
    await nextTick();
    expect(workspace.leftSidebarWidth).toBe(LEFT_SIDEBAR_DEFAULT + 80);

    await leftResizer.trigger('mousedown', { clientX: 344 });
    window.dispatchEvent(new MouseEvent('mousemove', { clientX: 1200 }));
    window.dispatchEvent(new MouseEvent('mouseup'));
    await nextTick();
    expect(workspace.leftSidebarWidth).toBe(LEFT_SIDEBAR_MAX);

    await leftResizer.trigger('dblclick');
    await nextTick();
    expect(workspace.leftSidebarWidth).toBe(LEFT_SIDEBAR_DEFAULT);

    // 拖拽右侧分割线调整右侧栏宽度（向左拖增宽，向右拖变窄并 clamp）
    const rightResizer = wrapper.get('[data-testid="resize-right"]');
    await rightResizer.trigger('mousedown', { clientX: 900 });
    window.dispatchEvent(new MouseEvent('mousemove', { clientX: 820 }));
    window.dispatchEvent(new MouseEvent('mouseup'));
    await nextTick();
    expect(workspace.rightSidebarWidth).toBe(RIGHT_SIDEBAR_DEFAULT + 80);

    await rightResizer.trigger('mousedown', { clientX: 820 });
    window.dispatchEvent(new MouseEvent('mousemove', { clientX: 1600 }));
    window.dispatchEvent(new MouseEvent('mouseup'));
    await nextTick();
    expect(workspace.rightSidebarWidth).toBe(RIGHT_SIDEBAR_MIN);

    // 验证左右边界常量合理
    expect(LEFT_SIDEBAR_MIN).toBeLessThan(LEFT_SIDEBAR_DEFAULT);
    expect(RIGHT_SIDEBAR_MAX).toBeGreaterThan(RIGHT_SIDEBAR_DEFAULT);

    // 再次点击顶栏按钮收起右侧边栏
    await wrapper.get('[data-testid="toggle-right-panel"]').trigger('click');
    await nextTick();
    expect(workspace.rightPanelOpen).toBe(false);
  });

  it('creates a project conversationally from the bottom composer in ProjectCreateView', async () => {
    vi.mocked(projectsApi.create).mockResolvedValue({
      id: 21,
      name: '社区活动报名系统',
      active_spec_id: null,
      created_at: '2026-11-25T14:00:00',
      requirement: '我想做一个社区活动报名系统，支持管理员发布活动和居民在线报名。',
    });
    vi.mocked(projectsApi.list).mockResolvedValue({ items: [] });

    const router = makeTestRouter();
    await router.push('/projects/new');
    await router.isReady();

    const wrapper = mount(ProjectCreateView, {
      global: { plugins: [ElementPlus, router] },
    });
    await flushPromises();
    await nextTick();

    expect(wrapper.find('[data-testid="chat-composer"]').exists()).toBe(true);
    const input = wrapper.get('[data-testid="create-requirement-input"]');
    await input.setValue('我想做一个社区活动报名系统，支持管理员发布活动、设置人数上限，居民可以在线报名。');
    await wrapper.get('[data-testid="create-send-btn"]').trigger('click');
    await flushPromises();

    expect(projectsApi.create).toHaveBeenCalledTimes(1);
    const [nameArg, reqArg] = vi.mocked(projectsApi.create).mock.calls[0];
    expect(nameArg).toBe('社区活动报名系统');
    expect(reqArg).toContain('管理员发布活动');
    expect(router.currentRoute.value.name).toBe('project-detail');
    expect(router.currentRoute.value.params.id).toBe('21');
  });

  it('sends follow-up requirements from ProjectDetailView bottom composer and opens right panel on demand', async () => {
    resetSidebarLayout();
    const baseSpec = sampleSpec();
    vi.mocked(projectsApi.get).mockResolvedValue({
      id: 9,
      name: '社区活动报名系统',
      active_spec_id: null,
      created_at: '2026-11-25T14:00:00',
      requirement: '社区活动报名系统',
    });
    vi.mocked(projectsApi.getSpec).mockResolvedValue({
      spec_id: 3,
      version: 1,
      draft_revision: 1,
      confirmed: false,
      confirmed_at: null,
      hash: 'a'.repeat(64),
      spec: baseSpec,
      pending: [],
      issues: [],
      summary: '社区活动摘要',
      missing_fields: [],
      questions: [],
    });
    vi.mocked(projectsApi.saveSpec).mockImplementation(async (_pid, _v, _r, body) => ({
      spec_id: 3,
      version: 1,
      draft_revision: 2,
      confirmed: false,
      confirmed_at: null,
      hash: 'b'.repeat(64),
      spec: JSON.parse(JSON.stringify(body)) as SoftwareSpecification,
      pending: [],
      issues: [],
      summary: '社区活动摘要',
      missing_fields: [],
      questions: [],
    }));
    vi.mocked(runsApi.listByProject).mockResolvedValue({ items: [] });

    const router = makeTestRouter();
    await router.push('/projects/9');
    await router.isReady();

    const wrapper = mount(ProjectDetailView, {
      props: { id: '9' },
      global: { plugins: [ElementPlus, router] },
    });
    await flushPromises();
    await nextTick();

    const workspace = useWorkspaceStore();
    expect(workspace.rightPanelOpen).toBe(false);

    // 通过底部对话框追加业务规则并自动保存
    await wrapper.get('[data-testid="chat-composer-input"]').setValue('活动开始前 2 小时禁止取消报名');
    await wrapper.get('[data-testid="chat-composer-send"]').trigger('click');
    await flushPromises();
    await nextTick();

    expect(projectsApi.saveSpec).toHaveBeenCalledTimes(1);
    expect(wrapper.text()).toContain('活动开始前 2 小时禁止取消报名');

    // 中间栏右上角已不再显示「定位右侧规格 / 定位接口与验收」按钮
    expect(wrapper.find('[data-testid="open-right-inspector"]').exists()).toBe(false);
    // 点击对话流中的内联链接可按需展开右侧面板
    const inlineLink = wrapper.findAll('button.inline-link')[0];
    await inlineLink.trigger('click');
    expect(workspace.rightPanelOpen).toBe(true);
  });

  it('unifies right inspector tabs to [设计接口, 验收交付] across flowchart views and puts routing in left project dropdown', async () => {
    resetSidebarLayout();
    const baseSpec = sampleSpec();
    vi.mocked(projectsApi.list).mockResolvedValue({
      items: [{ id: 8, name: '民宿预订系统', active_spec_id: 3, created_at: '2026-11-25T10:00:00' }],
    });
    vi.mocked(projectsApi.get).mockResolvedValue({
      id: 8,
      name: '民宿预订系统',
      active_spec_id: 3,
      created_at: '2026-11-25T10:00:00',
      requirement: '民宿预订系统',
    });
    vi.mocked(projectsApi.getSpec).mockResolvedValue({
      spec_id: 3,
      version: 1,
      draft_revision: 1,
      confirmed: true,
      confirmed_at: '2026-11-25T10:05:00',
      hash: 'a'.repeat(64),
      spec: baseSpec,
      pending: [],
      issues: [],
      summary: '民宿摘要',
      missing_fields: [],
      questions: [],
    });
    vi.mocked(runsApi.listByProject).mockResolvedValue({
      items: [
        {
          id: 10,
          project_id: 8,
          spec_id: 3,
          status: 'SUCCEEDED',
          error_code: null,
          cancel_requested: false,
          lease_owner: null,
          created_at: '2026-11-25T10:10:00',
          updated_at: '2026-11-25T10:12:00',
        },
      ],
    });
    vi.mocked(runsApi.get).mockResolvedValue({
      id: 10,
      project_id: 8,
      spec_id: 3,
      status: 'SUCCEEDED',
      phase: 'verify',
      error_code: null,
      created_at: '2026-11-25T10:10:00',
      updated_at: '2026-11-25T10:12:00',
      tasks: { total: 4, done: 4 },
      cost: { currency_unit: 'micro_yuan', settled_micro: 1_000_000, reserved_micro: 0, unknown_calls: 0 },
      artifacts: [],
      verification: null,
      cancellation: null,
      code_delivery_status: 'PASS',
      external_readiness: 'NOT_REQUIRED',
      external_notice: null,
      external_services: [],
    });
    vi.mocked(runsApi.events).mockResolvedValue({ run_id: 10, items: [], latest_sequence: 0 });
    vi.mocked(runsApi.factory).mockResolvedValue({
      run_id: 10,
      project_id: 8,
      run_status: 'SUCCEEDED',
      factory_active: false,
      current_phase: 'idle',
      integration_rounds: {},
      blueprints: [],
      backend_departments: [],
      frontend_departments: [],
      open_defects: [],
      resolved_defects: [],
      cost_by_department: {},
      model_routing_snapshot: [
        {
          role: 'chief_architect',
          provider: 'deepseek_official',
          model: 'deepseek-reasoner',
          base_url_host: 'api.deepseek.com',
          api_key_env: 'DEEPSEEK_API_KEY',
          api_key_present: true,
          enabled: true,
          supports_tools: false,
        },
      ],
    });
    vi.mocked(profilesApi.list).mockResolvedValue([]);
    vi.mocked(modelsApi.getConfig).mockResolvedValue({
      config_source: 'custom',
      writable: true,
      roles: [],
    });

    // 1) ProjectDetailView 冻结态默认进入工厂组装流程图：右上角紧凑 TodoList + 主体实时运行流程图 + 右栏仅 2 Tab（设计接口 / 验收交付）
    const router = makeTestRouter();
    await router.push('/projects/8');
    await router.isReady();
    const projectWrapper = mount(ProjectDetailView, {
      props: { id: '8' },
      global: { plugins: [ElementPlus, router] },
    });
    await flushPromises();
    await nextTick();

    // TodoList 放在中间栏顶栏右侧（.stage-bar 内），不在页面正文 .stage-scroll 内
    expect(projectWrapper.find('.stage-bar [data-testid="standard-todo-widget"]').exists()).toBe(true);
    expect(projectWrapper.find('.stage-scroll [data-testid="standard-todo-widget"]').exists()).toBe(false);
    // 右侧边栏 4 个选项放在固定顶部边栏（.inspector-header），不在滚动内容区 .inspector-body 内
    expect(projectWrapper.find('.inspector-header [data-testid="inspector-tabs"]').exists()).toBe(true);
    expect(projectWrapper.find('.inspector-body [data-testid="inspector-tabs"]').exists()).toBe(false);
    expect(projectWrapper.find('[data-testid="live-run-flowchart"]').exists()).toBe(true);
    // 已移除底部「当前实时运行 / 独立全屏运行视图」栏与中间右上角定位按钮
    expect(projectWrapper.find('.latest-run-banner').exists()).toBe(false);
    expect(projectWrapper.find('[data-testid="open-right-inspector"]').exists()).toBe(false);
    expect(projectWrapper.text()).not.toContain('独立全屏运行视图');
    const flowchartTabs = projectWrapper
      .findAll('[data-testid="inspector-tabs"] button')
      .map((b) => b.text().trim());
    expect(flowchartTabs).toEqual(['设计接口', '需求规格', '验收交付', '事件流 (0)']);

    // 切换中间到「对话反问流」，TodoList 依然在顶栏右侧可见，右侧边栏 Tab 列表完全不变（不增不减）
    await projectWrapper.get('[data-testid="center-tab-chat"]').trigger('click');
    await nextTick();
    expect(projectWrapper.find('.stage-bar [data-testid="standard-todo-widget"]').exists()).toBe(true);
    // 已冻结历史会话不再展示禁用对话框与冗余的「重新提取 / 重新生成草稿」按钮
    expect(projectWrapper.find('[data-testid="chat-composer-box"]').exists()).toBe(false);
    expect(projectWrapper.find('[data-testid="chat-composer-input"]').exists()).toBe(false);
    expect(projectWrapper.find('[data-testid="extract-spec-btn"]').exists()).toBe(false);
    expect(projectWrapper.text()).not.toContain('重新提取');
    expect(projectWrapper.text()).toContain('创建新版本草稿以修改');
    const chatTabs = projectWrapper
      .findAll('[data-testid="inspector-tabs"] button')
      .map((b) => b.text().trim());
    expect(chatTabs).toEqual(flowchartTabs);

    // 点击「创建新版本草稿以修改」后，重新展示可用对话框以继续补充需求
    const reopenBtn = projectWrapper.findAll('button').find((b) => b.text().includes('创建新版本草稿以修改'));
    expect(reopenBtn).toBeDefined();
    await reopenBtn!.trigger('click');
    await nextTick();
    expect(projectWrapper.find('[data-testid="chat-composer-box"]').exists()).toBe(true);
    expect((projectWrapper.get('[data-testid="chat-composer-input"]').element as HTMLTextAreaElement).disabled).toBe(false);

    // 2) RunDetailView 右栏同样严格统一为 4 Tab（顶部边栏固定不随滚动消失），TodoList 在顶栏右侧
    const runWrapper = mount(RunDetailView, {
      props: { id: '10' },
      global: { plugins: [ElementPlus, router] },
    });
    await flushPromises();
    await nextTick();

    const rdTabs = runWrapper.findAll('[data-testid="inspector-tabs"] button').map((b) => b.text().trim());
    expect(rdTabs).toEqual(['设计接口', '需求规格', '验收交付', '事件流 (0)']);
    expect(runWrapper.find('.run-head [data-testid="standard-todo-widget"]').exists()).toBe(true);
    expect(runWrapper.find('.rd-scroll [data-testid="standard-todo-widget"]').exists()).toBe(false);
    expect(runWrapper.find('.inspector-header [data-testid="inspector-tabs"]').exists()).toBe(true);
    expect(runWrapper.find('.inspector-body [data-testid="inspector-tabs"]').exists()).toBe(false);

    // 3) 左侧边栏对应项目的下拉框支持切换查看「历史进程」与「模型路由」
    const sidebarWrapper = mount(WorkbenchSidebar, {
      global: { plugins: [ElementPlus, router] },
    });
    await flushPromises();
    await nextTick();

    // 点击左侧历史运行 #10 直接在项目页工厂流程图内展示，不跳独立全屏运行页
    await sidebarWrapper.get('[data-testid="project-run-entry-10"]').trigger('click');
    await flushPromises();
    expect(router.currentRoute.value.name).toBe('project-detail');
    expect(router.currentRoute.value.query.run).toBe('10');
    expect(router.currentRoute.value.query.view).toBe('flowchart');

    await sidebarWrapper.get('[data-testid="project-subtab-routing-8"]').trigger('click');
    await flushPromises();
    await nextTick();

    const routingPanel = sidebarWrapper.get('[data-testid="project-routing-panel-8"]');
    expect(routingPanel.text()).toContain('deepseek-reasoner');
  });

  it('groups designed APIs by feature dropdowns and keeps all outer & inner dropdowns collapsed by default', async () => {
    const wrapper = mount(DesignedApisPanel, {
      props: {
        spec: sampleSpec(),
      },
    });

    // 初始仅显示功能大下拉框（activity / registration / auth），内部 GET/POST/PUT/DELETE 均未渲染展开
    const groups = wrapper.findAll('.api-group');
    expect(groups.length).toBe(3);
    expect(wrapper.findAll('.api-item').length).toBe(0);

    // 点击 activity 大下拉框后，才显示其内部 5 个接口，且单条接口详情默认仍关闭
    await wrapper.get('[data-testid="api-group-toggle-activity"]').trigger('click');
    await nextTick();
    expect(wrapper.findAll('.api-item').length).toBe(5);
    expect(wrapper.findAll('.api-detail').length).toBe(0);

    // 再点击其中一条接口后，展开该条接口的入参/出参详情
    await wrapper.get('[data-testid="designed-api-toggle-activity-create"]').trigger('click');
    await nextTick();
    expect(wrapper.find('[data-testid="designed-api-detail-activity-create"]').exists()).toBe(true);
  });

  it('adds a custom model to the library and assigns it to a role via dropdown in WorkbenchSidebar', async () => {
    vi.mocked(projectsApi.list).mockResolvedValue({ items: [] });
    vi.mocked(profilesApi.list).mockResolvedValue([]);
    vi.mocked(modelsApi.getConfig).mockResolvedValue({
      config_source: 'custom',
      writable: true,
      roles: [
        {
          role: 'dept_coder',
          label: '部门编码引擎',
          provider: 'deepseek_official',
          model: 'deepseek-flash',
          base_url: 'https://api.deepseek.com/v1',
          base_url_host: 'api.deepseek.com',
          api_key_env: 'DEEPSEEK_API_KEY',
          api_key_present: true,
          enabled: true,
          supports_tools: true,
          max_output_tokens: 8192,
          timeout_seconds: 180,
          source: 'custom',
        },
      ],
      custom_models: [
        {
          id: 'deepseek-official-deepseek-flash',
          name: 'DeepSeek Flash',
          provider: 'deepseek_official',
          model: 'deepseek-flash',
          base_url: 'https://api.deepseek.com/v1',
          base_url_host: 'api.deepseek.com',
          api_key_env: 'DEEPSEEK_API_KEY',
          api_key_present: true,
          supports_tools: true,
          max_output_tokens: 8192,
          timeout_seconds: 180,
          removable: false,
        },
      ],
    });
    vi.mocked(modelsApi.addPreset).mockResolvedValue({
      config_source: 'custom',
      writable: true,
      roles: [
        {
          role: 'dept_coder',
          label: '部门编码引擎',
          provider: 'deepseek_official',
          model: 'deepseek-flash',
          base_url: 'https://api.deepseek.com/v1',
          base_url_host: 'api.deepseek.com',
          api_key_env: 'DEEPSEEK_API_KEY',
          api_key_present: true,
          enabled: true,
          supports_tools: true,
          max_output_tokens: 8192,
          timeout_seconds: 180,
          source: 'custom',
        },
      ],
      custom_models: [
        {
          id: 'deepseek-official-deepseek-flash',
          name: 'DeepSeek Flash',
          provider: 'deepseek_official',
          model: 'deepseek-flash',
          base_url: 'https://api.deepseek.com/v1',
          base_url_host: 'api.deepseek.com',
          api_key_env: 'DEEPSEEK_API_KEY',
          api_key_present: true,
          supports_tools: true,
          max_output_tokens: 8192,
          timeout_seconds: 180,
          removable: false,
        },
        {
          id: 'openai-compatible-qwen3-coder-plus',
          name: 'Qwen3 Coder',
          provider: 'openai_compatible',
          model: 'qwen3-coder-plus',
          base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
          base_url_host: 'dashscope.aliyuncs.com',
          api_key_env: 'DASHSCOPE_API_KEY',
          api_key_present: true,
          supports_tools: true,
          max_output_tokens: 16384,
          timeout_seconds: 240,
          removable: true,
        },
      ],
    });
    vi.mocked(modelsApi.updateConfig).mockResolvedValue({
      config_source: 'custom',
      writable: true,
      roles: [
        {
          role: 'dept_coder',
          label: '部门编码引擎',
          provider: 'openai_compatible',
          model: 'qwen3-coder-plus',
          base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
          base_url_host: 'dashscope.aliyuncs.com',
          api_key_env: 'DASHSCOPE_API_KEY',
          api_key_present: true,
          enabled: true,
          supports_tools: true,
          max_output_tokens: 16384,
          timeout_seconds: 240,
          source: 'custom',
        },
      ],
      custom_models: [
        {
          id: 'openai-compatible-qwen3-coder-plus',
          name: 'Qwen3 Coder',
          provider: 'openai_compatible',
          model: 'qwen3-coder-plus',
          base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
          base_url_host: 'dashscope.aliyuncs.com',
          api_key_env: 'DASHSCOPE_API_KEY',
          api_key_present: true,
          supports_tools: true,
          max_output_tokens: 16384,
          timeout_seconds: 240,
          removable: true,
        },
      ],
    });

    const router = makeTestRouter();
    await router.push('/');
    await router.isReady();

    const wrapper = mount(WorkbenchSidebar, {
      global: { plugins: [ElementPlus, router] },
    });
    await flushPromises();
    await nextTick();

    await wrapper.get('[data-testid="sidebar-tab-models"]').trigger('click');
    await nextTick();

    // 1) 添加一个自定义模型
    await wrapper.get('[data-testid="add-custom-model-btn"]').trigger('click');
    await nextTick();
    await wrapper.get('[data-testid="preset-name-input"]').setValue('Qwen3 Coder');
    await wrapper.get('[data-testid="preset-model-input"]').setValue('qwen3-coder-plus');
    await wrapper.get('[data-testid="preset-baseurl-input"]').setValue('https://dashscope.aliyuncs.com/compatible-mode/v1');
    await wrapper.get('[data-testid="preset-env-input"]').setValue('DASHSCOPE_API_KEY');
    await wrapper.get('[data-testid="submit-custom-model-btn"]').trigger('click');
    await flushPromises();
    await nextTick();

    expect(modelsApi.addPreset).toHaveBeenCalledTimes(1);
    expect(wrapper.find('[data-testid="custom-model-item-openai-compatible-qwen3-coder-plus"]').exists()).toBe(true);

    // 2) 在 dept_coder 角色的下拉框中直接选择刚添加的自定义模型
    const select = wrapper.get('[data-testid="role-model-select-dept_coder"]');
    await select.setValue('openai-compatible-qwen3-coder-plus');
    await flushPromises();

    expect(modelsApi.updateConfig).toHaveBeenCalledTimes(1);
    expect(vi.mocked(modelsApi.updateConfig).mock.calls[0][0][0].model).toBe('qwen3-coder-plus');
    expect(vi.mocked(modelsApi.updateConfig).mock.calls[0][0][0].api_key_env).toBe('DASHSCOPE_API_KEY');
  });

  it('toggles project dropdown without navigating, navigates only on child history click, and deletes project after confirm', async () => {
    resetSidebarLayout();
    const workspace = useWorkspaceStore();
    workspace.activeProjectId = null;
    workspace.expandedProjectIds = {};
    workspace.projectRuns = {};

    vi.mocked(projectsApi.list).mockResolvedValue({
      items: [
        { id: 31, name: '活动报名项目', active_spec_id: 5, created_at: '2026-11-25T10:00:00' },
      ],
    });
    vi.mocked(runsApi.listByProject).mockResolvedValue({
      items: [
        {
          id: 99,
          project_id: 31,
          spec_id: 5,
          status: 'SUCCEEDED',
          error_code: null,
          cancel_requested: false,
          lease_owner: null,
          created_at: '2026-11-25T10:05:00',
          updated_at: '2026-11-25T10:08:00',
        },
      ],
    });
    vi.mocked(projectsApi.remove).mockResolvedValue({ deleted: true, project_id: 31 });
    vi.mocked(profilesApi.list).mockResolvedValue([]);
    vi.mocked(modelsApi.getConfig).mockResolvedValue({
      config_source: 'default',
      writable: true,
      roles: [],
    });

    const router = makeTestRouter();
    await router.push('/');
    await router.isReady();

    const wrapper = mount(WorkbenchSidebar, {
      global: { plugins: [ElementPlus, router] },
    });
    await flushPromises();
    await nextTick();

    // 1) 点击项目名称只展开下拉框，中间路由保持在 '/' 不跳转
    expect(wrapper.find('[data-testid="project-dropdown-31"]').exists()).toBe(false);
    await wrapper.get('[data-testid="project-toggle-31"]').trigger('click');
    await flushPromises();
    await nextTick();
    expect(wrapper.find('[data-testid="project-dropdown-31"]').exists()).toBe(true);
    expect(router.currentRoute.value.name).toBe('projects');

    // 2) 点击下拉框内的「需求对话与规格」才跳转
    await wrapper.get('[data-testid="project-chat-entry-31"]').trigger('click');
    await flushPromises();
    expect(router.currentRoute.value.name).toBe('project-detail');
    expect(router.currentRoute.value.params.id).toBe('31');
    expect(router.currentRoute.value.query.view).toBe('chat');

    // 3) 点击垃圾桶按钮：先测试取消删除（不调用 remove），再测试确认删除
    const confirmSpy = vi.spyOn(ElMessageBox, 'confirm');
    confirmSpy.mockRejectedValueOnce('cancel');
    await wrapper.get('[data-testid="delete-project-31"]').trigger('click');
    await flushPromises();
    expect(projectsApi.remove).not.toHaveBeenCalled();

    confirmSpy.mockResolvedValueOnce('confirm' as never);
    await wrapper.get('[data-testid="delete-project-31"]').trigger('click');
    await flushPromises();
    await nextTick();

    expect(projectsApi.remove).toHaveBeenCalledWith(31);
    expect(wrapper.find('[data-testid="project-row-31"]').exists()).toBe(false);
    expect(router.currentRoute.value.name).toBe('projects');
    confirmSpy.mockRestore();
  });
});
