import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import ElementPlus from 'element-plus';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { nextTick } from 'vue';

import { projectsApi, runsApi } from '@/api/client';
import type {
  FactoryRunView,
  ProjectDetail,
  RunDetail,
  SoftwareSpecification,
  SpecView,
} from '@/api/types';
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
      getSpec: vi.fn(),
      extract: vi.fn(),
      saveSpec: vi.fn(),
      confirm: vi.fn(),
    },
    runsApi: {
      start: vi.fn(),
      get: vi.fn(),
      factory: vi.fn(),
      events: vi.fn(),
      cancel: vi.fn(),
      artifactUrl: actual.runsApi.artifactUrl,
    },
  };
});

vi.mock('@/stores/auth', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/stores/auth')>();
  return { ...actual, clearSession: vi.fn() };
});

function makeRunDetail(overrides: Partial<RunDetail> = {}): RunDetail {
  return {
    id: 10,
    project_id: 9,
    spec_id: 3,
    status: 'GENERATING',
    phase: 'frontend',
    error_code: null,
    created_at: '2026-11-23T14:00:00',
    updated_at: '2026-11-23T14:00:10',
    tasks: { total: 10, done: 8 },
    cost: {
      currency_unit: 'micro_yuan',
      settled_micro: 3_500_000,
      reserved_micro: 0,
      unknown_calls: 0,
    },
    artifacts: [],
    verification: null,
    cancellation: null,
    code_delivery_status: 'NOT_RUN',
    external_readiness: 'MOCK_ONLY',
    external_notice: '本地沙箱支付已隔离为 Mock 模式。',
    external_services: [],
    ...overrides,
  };
}

function makeFactoryView(overrides: Partial<FactoryRunView> = {}): FactoryRunView {
  return {
    run_id: 10,
    project_id: 9,
    run_status: 'GENERATING',
    factory_active: true,
    current_phase: 'frontend',
    integration_rounds: { backend: 2, frontend: 1 },
    blueprints: [
      {
        phase: 'backend',
        version: 1,
        status: 'APPROVED_FOR_BUILD',
        blueprint_hash: 'a'.repeat(64),
        module_count: 5,
        module_ids: ['auth', 'product', 'cart', 'order', 'payment'],
        sandbox_integrations: [{ id: 'payment-sandbox', kind: 'payment', mode: 'sandbox_mock' }],
      },
      {
        phase: 'frontend',
        version: 1,
        status: 'APPROVED_FOR_BUILD',
        blueprint_hash: 'b'.repeat(64),
        module_count: 5,
        module_ids: ['auth-ui', 'catalog-ui', 'cart-ui', 'order-ui', 'payment-ui'],
        sandbox_integrations: [{ id: 'payment-sandbox', kind: 'payment', mode: 'sandbox_mock' }],
      },
    ],
    backend_departments: [
      {
        phase: 'backend',
        dept_id: 'auth',
        status: 'PASSED',
        current_round: 1,
        max_rounds: 3,
        duration_ms: 1200,
        test_status: 'PASS',
        test_executed: true,
        test_exit_code: 0,
        verifier_kind: 'docker_runner',
        error_code: null,
        cost_micro: 500_000,
        calls: 3,
        input_tokens: 1200,
        output_tokens: 600,
      },
      {
        phase: 'backend',
        dept_id: 'cart',
        status: 'PASSED',
        current_round: 2,
        max_rounds: 3,
        duration_ms: 2400,
        test_status: 'PASS',
        test_executed: true,
        test_exit_code: 0,
        verifier_kind: 'docker_runner',
        error_code: null,
        cost_micro: 900_000,
        calls: 6,
        input_tokens: 2400,
        output_tokens: 1100,
      },
    ],
    frontend_departments: [
      {
        phase: 'frontend',
        dept_id: 'catalog-ui',
        status: 'REWORK',
        current_round: 2,
        max_rounds: 3,
        duration_ms: 1500,
        test_status: 'FAIL',
        test_executed: true,
        test_exit_code: 2,
        verifier_kind: 'local_process',
        error_code: 'BUILD_FAILED',
        cost_micro: 700_000,
        calls: 4,
        input_tokens: 1800,
        output_tokens: 900,
      },
      {
        phase: 'frontend',
        dept_id: 'payment-ui',
        status: 'CODING',
        current_round: 1,
        max_rounds: 3,
        duration_ms: null,
        test_status: null,
        test_executed: false,
        test_exit_code: null,
        verifier_kind: null,
        error_code: null,
        cost_micro: 300_000,
        calls: 2,
        input_tokens: 900,
        output_tokens: 450,
      },
    ],
    open_defects: [
      {
        id: 101,
        phase: 'frontend',
        dept_id: 'catalog-ui',
        scope: 'department',
        round: 1,
        error_class: 'TS_TYPE_ERROR',
        summary: 'ProductDto 缺少字段 <img src=x onerror=alert(1)>',
        log_excerpt: 'error TS2339: Property nonExistentGhostField <script>evil()</script>',
        affected_paths: ['frontend/src/modules/catalog-ui/service.ts'],
        resolved: false,
        created_at: '2026-11-23T14:00:08',
        resolved_at: null,
      },
    ],
    resolved_defects: [
      {
        id: 99,
        phase: 'backend',
        dept_id: 'cart',
        scope: 'integration',
        round: 1,
        error_class: 'FLYWAY_MIGRATION_FK_MISMATCH',
        summary: 'V300__cart.sql 外键字段类型已修正为 BIGINT',
        log_excerpt: 'resolved after round 2',
        affected_paths: ['backend/src/main/resources/db/migration/V300__cart.sql'],
        resolved: true,
        created_at: '2026-11-23T14:00:03',
        resolved_at: '2026-11-23T14:00:06',
      },
    ],
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
      {
        role: 'dept_coder',
        provider: 'openai_compatible',
        model: 'qwen3-coder-plus',
        base_url_host: 'dashscope.aliyuncs.com',
        api_key_env: 'DASHSCOPE_API_KEY',
        api_key_present: true,
        enabled: true,
        supports_tools: true,
      },
    ],
    ...overrides,
  };
}

async function settle(): Promise<void> {
  for (let i = 0; i < 14; i += 1) {
    await Promise.resolve();
  }
}

describe('FP10 RunDetailView factory swimlanes & XSS safety', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.mocked(runsApi.get).mockReset();
    vi.mocked(runsApi.factory).mockReset();
    vi.mocked(runsApi.events).mockReset();
    vi.mocked(runsApi.cancel).mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('renders backend & frontend swimlane cards, defects, per-dept cost and routing snapshot with zero v-html XSS', async () => {
    vi.mocked(runsApi.get).mockResolvedValue(makeRunDetail());
    vi.mocked(runsApi.factory).mockResolvedValue(makeFactoryView());
    vi.mocked(runsApi.events).mockResolvedValue({ run_id: 10, items: [], latest_sequence: 0 });

    const wrapper = mount(RunDetailView, {
      props: { id: '10' },
      global: { plugins: [ElementPlus, makeTestRouter()] },
    });
    await settle();

    // 确认源码中严禁出现 v-html
    const src = readFileSync(resolve(__dirname, '../src/views/RunDetailView.vue'), 'utf-8');
    expect(src).not.toContain('v-html');

    const panel = wrapper.get('[data-testid="factory-swimlanes"]');
    expect(panel.text()).toContain('异步工厂多部门泳道视图');
    expect(panel.text()).toContain('前端并行阶段');
    expect(panel.text()).toContain('后端总装大循环：第 2 轮');
    expect(panel.text()).toContain('前端总装大循环：第 1 轮');

    // 后端与前端部门泳道卡片
    const cartLane = wrapper.get('[data-testid="dept-lane-backend-cart"]');
    expect(cartLane.text()).toContain('已通过');
    expect(cartLane.text()).toContain('轮次：2 / 3');
    expect(cartLane.text()).toContain('¥0.9000');

    const catalogLane = wrapper.get('[data-testid="dept-lane-frontend-catalog-ui"]');
    expect(catalogLane.text()).toContain('返工中');
    expect(catalogLane.text()).toContain('测试：FAIL');
    expect(catalogLane.text()).toContain('exit 2');

    // 未解决与已解决缺陷单，且恶意 HTML 标签必须纯文本转义，绝不注入真实 DOM 节点
    const openDefects = wrapper.get('[data-testid="open-defects"]');
    expect(openDefects.text()).toContain('<img src=x onerror=alert(1)>');
    expect(openDefects.findAll('img')).toHaveLength(0);
    expect(openDefects.findAll('script')).toHaveLength(0);

    const resolvedDefects = wrapper.get('[data-testid="resolved-defects"]');
    expect(resolvedDefects.text()).toContain('FLYWAY_MIGRATION_FK_MISMATCH');

    // 脱敏模型路由快照
    const routing = wrapper.get('[data-testid="factory-routing-snapshot"]');
    expect(routing.text()).toContain('deepseek-reasoner');
    expect(routing.text()).toContain('api.deepseek.com');
    expect(routing.text()).toContain('DEEPSEEK_API_KEY');

    // 2 秒轮询继续刷新 /factory
    await vi.advanceTimersByTimeAsync(2000);
    await settle();
    expect(runsApi.factory).toHaveBeenCalledTimes(2);
  });

  it('hides factory swimlanes cleanly for standard assembly runs', async () => {
    vi.mocked(runsApi.get).mockResolvedValue(makeRunDetail({ status: 'SUCCEEDED' }));
    vi.mocked(runsApi.factory).mockResolvedValue(
      makeFactoryView({
        factory_active: false,
        current_phase: 'idle',
        backend_departments: [],
        frontend_departments: [],
        open_defects: [],
        resolved_defects: [],
      }),
    );
    vi.mocked(runsApi.events).mockResolvedValue({ run_id: 10, items: [], latest_sequence: 0 });

    const wrapper = mount(RunDetailView, {
      props: { id: '10' },
      global: { plugins: [ElementPlus, makeTestRouter()] },
    });
    await settle();

    expect(wrapper.find('[data-testid="factory-swimlanes"]').exists()).toBe(false);
  });
});

describe('FP10 ProjectDetailView run mode selector', () => {
  beforeEach(() => {
    vi.mocked(projectsApi.get).mockReset();
    vi.mocked(projectsApi.getSpec).mockReset();
    vi.mocked(runsApi.start).mockReset();
  });

  it('switches between standard and factory modes with soft-limit hint and submits factory mode', async () => {
    const project: ProjectDetail = {
      id: 9,
      name: '电商沙箱项目',
      active_spec_id: 3,
      created_at: '2026-11-23T14:00:00',
      requirement: '登录注册、商品展示、购物车、订单与本地模拟支付',
    };
    const confirmedSpec: SoftwareSpecification = {
      schema_version: '0.1',
      project_type: 'crud_web',
      goal: '电商沙箱',
      actors: ['ADMIN', 'USER'],
      entities: [
        { name: 'Product', source: 'user_confirmed', fields: [{ name: 'title', type: 'string', required: true }] },
      ],
      relations: [],
      pages: [],
      permissions: [{ role: 'ADMIN', actions: ['create', 'read'], source: 'user_confirmed' }],
      business_rules: [{ id: 'BR01', text: '库存不足拒下单', source: 'user_confirmed' }],
      acceptance_cases: [{ id: 'AC01', scenario: '沙箱支付', expected: '返回回执', source: 'user_confirmed' }],
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
    const view: SpecView = {
      spec_id: 3,
      version: 1,
      draft_revision: 1,
      confirmed: true,
      confirmed_at: '2026-11-23T14:00:00',
      hash: 'c'.repeat(64),
      spec: confirmedSpec,
      pending: [],
      issues: [],
      summary: '电商沙箱规格',
      missing_fields: [],
      questions: [],
    };

    vi.mocked(projectsApi.get).mockResolvedValue(project);
    vi.mocked(projectsApi.getSpec).mockResolvedValue(view);
    vi.mocked(runsApi.start).mockResolvedValue({
      id: 88,
      project_id: 9,
      spec_id: 3,
      status: 'QUEUED',
      error_code: null,
      cancel_requested: false,
      lease_owner: null,
      created_at: '2026-11-23T14:01:00',
      updated_at: '2026-11-23T14:01:00',
    });

    const router = makeTestRouter();
    await router.push('/projects/9');
    await router.isReady();

    const wrapper: VueWrapper = mount(ProjectDetailView, {
      props: { id: '9' },
      global: { plugins: [ElementPlus, router] },
    });
    await flushPromises();
    await nextTick();

    // 默认标准装配模式
    expect(wrapper.get('[data-testid="run-mode-hint"]').text()).toContain('标准装配模式');

    // 切换至异步工厂模式，展示模块软上限提示
    const factoryRadio = wrapper.get('[data-testid="mode-factory"] input');
    await factoryRadio.setValue(true);
    await nextTick();
    expect(wrapper.get('[data-testid="run-mode-hint"]').text()).toContain('建议模块数 ≤ 8');
    expect(wrapper.get('[data-testid="run-mode-hint"]').text()).toContain('本地沙箱模拟模式');

    // 点击“开始生成”携带 'factory' 模式
    const generateBtn = wrapper.findAll('button').find((b) => b.text().includes('开始生成'));
    expect(generateBtn).toBeDefined();
    await generateBtn!.trigger('click');
    await flushPromises();

    expect(runsApi.start).toHaveBeenCalledTimes(1);
    const [pidArg, specIdArg, keyArg, modeArg] = vi.mocked(runsApi.start).mock.calls[0];
    expect(pidArg).toBe(9);
    expect(specIdArg).toBe(3);
    expect(typeof keyArg).toBe('string');
    expect(modeArg).toBe('factory');
  });
});
