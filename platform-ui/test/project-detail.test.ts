import { flushPromises, mount, type DOMWrapper, type VueWrapper } from '@vue/test-utils';
import ElementPlus from 'element-plus';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { nextTick } from 'vue';

import { ApiError, projectsApi, runsApi } from '@/api/client';
import type { ProjectDetail, SoftwareSpecification, SpecView } from '@/api/types';
import ProjectDetailView from '@/views/ProjectDetailView.vue';

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
    runsApi: { start: vi.fn(), get: vi.fn() },
  };
});

vi.mock('@/stores/auth', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/stores/auth')>();
  return { ...actual, clearSession: vi.fn() };
});

function spec(overrides: Partial<SoftwareSpecification> = {}): SoftwareSpecification {
  return {
    schema_version: '0.1',
    project_type: 'crud_web',
    goal: '管理社区活动',
    actors: ['ADMIN', 'MEMBER'],
    entities: [
      {
        name: 'Task',
        source: 'pending',
        fields: [{ name: 'title', type: 'string', required: true }],
      },
    ],
    relations: [],
    pages: [],
    permissions: [{ role: 'ADMIN', actions: ['create', 'read'], source: 'pending' }],
    business_rules: [{ id: 'BR01', text: '同一成员不能重复报名', source: 'pending' }],
    acceptance_cases: [{ id: 'AC01', scenario: '重复报名', expected: '被拒绝', source: 'pending' }],
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
    ...overrides,
  };
}

function specView(overrides: Partial<SpecView> = {}): SpecView {
  return {
    spec_id: 3,
    version: 1,
    draft_revision: 1,
    confirmed: false,
    confirmed_at: null,
    hash: 'a'.repeat(64),
    spec: spec(),
    pending: ['entity:Task', 'permission:ADMIN', 'business_rule:BR01', 'acceptance_case:AC01'],
    issues: [],
    summary: '模型摘要',
    missing_fields: [],
    questions: [],
    ...overrides,
  };
}

const project: ProjectDetail = {
  id: 9,
  name: '社区活动报名',
  active_spec_id: null,
  created_at: '2026-10-11T03:00:00',
  requirement: '管理员可以发布活动，普通成员可以报名。',
};

function buttonByText(wrapper: VueWrapper, text: string): DOMWrapper<HTMLButtonElement> {
  const button = wrapper.findAll('button').find((node) => node.text().includes(text));
  if (!button) {
    throw new Error(`button not found: ${text}`);
  }
  return button;
}

function isDisabled(wrapper: VueWrapper, text: string): boolean {
  return (buttonByText(wrapper, text).element as HTMLButtonElement).disabled;
}

async function mountView(router = makeTestRouter()): Promise<VueWrapper> {
  const wrapper = mount(ProjectDetailView, {
    props: { id: '9' },
    global: { plugins: [ElementPlus, router] },
  });
  await flushPromises();
  await nextTick();
  return wrapper;
}

beforeEach(() => {
  vi.mocked(projectsApi.get).mockReset();
  vi.mocked(projectsApi.getSpec).mockReset();
  vi.mocked(projectsApi.saveSpec).mockReset();
  vi.mocked(projectsApi.confirm).mockReset();
  vi.mocked(runsApi.start).mockReset();
});

describe('ProjectDetailView spec confirmation', () => {
  it('disables confirm and generate while model inferences remain pending', async () => {
    vi.mocked(projectsApi.get).mockResolvedValue(project);
    vi.mocked(projectsApi.getSpec).mockResolvedValue(specView());

    const wrapper = await mountView();

    expect(wrapper.text()).toContain('待确认');
    expect(wrapper.text()).toContain('固定技术方案（MVP 暂不可切换）');
    expect(isDisabled(wrapper, '确认规格')).toBe(true);
    expect(isDisabled(wrapper, '开始生成')).toBe(true);
  });

  it('saves edits with version/revision, then enables confirm when nothing is pending', async () => {
    vi.mocked(projectsApi.get).mockResolvedValue(project);
    vi.mocked(projectsApi.getSpec).mockResolvedValue(specView());
    // 保存后服务端返回：草稿修订 +1、无 pending/issues。
    const savedView = specView({
      draft_revision: 2,
      spec: spec({
        entities: [{ name: 'Task', source: 'user_confirmed', fields: [{ name: 'title', type: 'string', required: true }] }],
        permissions: [{ role: 'ADMIN', actions: ['create', 'read'], source: 'user_confirmed' }],
        business_rules: [{ id: 'BR01', text: '同一成员不能重复报名', source: 'user_confirmed' }],
        acceptance_cases: [{ id: 'AC01', scenario: '重复报名', expected: '被拒绝', source: 'user_confirmed' }],
      }),
      pending: [],
    });
    // 组件发送的是响应式工作副本，会随保存成功被重写；在调用时快照入参。
    const captured: { body?: SoftwareSpecification } = {};
    vi.mocked(projectsApi.saveSpec).mockImplementation(async (_pid, _version, _revision, body) => {
      captured.body = JSON.parse(JSON.stringify(body)) as SoftwareSpecification;
      return savedView;
    });

    const wrapper = await mountView();
    expect(isDisabled(wrapper, '保存修改')).toBe(true); // 未修改时不可保存

    await wrapper.get('[data-testid="goal-input"]').setValue('管理社区活动与报名');
    await nextTick();
    expect(isDisabled(wrapper, '保存修改')).toBe(false);

    await buttonByText(wrapper, '保存修改').trigger('click');
    await flushPromises();

    expect(projectsApi.saveSpec).toHaveBeenCalledTimes(1);
    const [projectIdArg, versionArg, revisionArg] = vi.mocked(projectsApi.saveSpec).mock.calls[0];
    expect(projectIdArg).toBe(9);
    expect(versionArg).toBe(1);
    expect(revisionArg).toBe(1);
    expect(captured.body?.goal).toBe('管理社区活动与报名');

    expect(isDisabled(wrapper, '确认规格')).toBe(false);
    expect(isDisabled(wrapper, '开始生成')).toBe(true); // 仍未确认
  });

  it('confirms then generates a run with an idempotency key', async () => {
    vi.mocked(projectsApi.get)
      .mockResolvedValueOnce(project)
      .mockResolvedValue({ ...project, active_spec_id: 3 });
    vi.mocked(projectsApi.getSpec)
      .mockResolvedValueOnce(
        specView({
          pending: [],
          spec: spec({
            entities: [{ name: 'Task', source: 'user_confirmed', fields: [{ name: 'title', type: 'string', required: true }] }],
            permissions: [{ role: 'ADMIN', actions: ['create', 'read'], source: 'user_confirmed' }],
            business_rules: [{ id: 'BR01', text: 'r', source: 'user_confirmed' }],
            acceptance_cases: [{ id: 'AC01', scenario: 's', expected: 'e', source: 'user_confirmed' }],
          }),
        }),
      )
      .mockResolvedValue(
        specView({
          confirmed: true,
          confirmed_at: '2026-10-11T05:00:00',
          pending: [],
          spec: spec({
            entities: [{ name: 'Task', source: 'user_confirmed', fields: [{ name: 'title', type: 'string', required: true }] }],
            permissions: [{ role: 'ADMIN', actions: ['create', 'read'], source: 'user_confirmed' }],
            business_rules: [{ id: 'BR01', text: 'r', source: 'user_confirmed' }],
            acceptance_cases: [{ id: 'AC01', scenario: 's', expected: 'e', source: 'user_confirmed' }],
          }),
        }),
      );
    vi.mocked(projectsApi.confirm).mockResolvedValue({
      spec_id: 3,
      version: 1,
      draft_revision: 1,
      hash: 'b'.repeat(64),
      confirmed_at: '2026-10-11T05:00:00',
    });
    vi.mocked(runsApi.start).mockResolvedValue({
      id: 42,
      project_id: 9,
      spec_id: 3,
      status: 'QUEUED',
      error_code: null,
      lease_owner: null,
      created_at: '2026-10-11T05:01:00',
      updated_at: '2026-10-11T05:01:00',
    });

    const router = makeTestRouter();
    await router.push('/projects/9');
    await router.isReady();
    const wrapper = await mountView(router);
    expect(isDisabled(wrapper, '确认规格')).toBe(false);
    expect(isDisabled(wrapper, '开始生成')).toBe(true);

    await buttonByText(wrapper, '确认规格').trigger('click');
    await flushPromises();
    await nextTick();

    expect(projectsApi.confirm).toHaveBeenCalledWith(9, 1, 1);
    expect(wrapper.text()).toContain('已确认并冻结');
    expect(isDisabled(wrapper, '开始生成')).toBe(false);

    await buttonByText(wrapper, '开始生成').trigger('click');
    await flushPromises();
    await nextTick();

    expect(runsApi.start).toHaveBeenCalledTimes(1);
    const [projectIdArg, specIdArg, keyArg] = vi.mocked(runsApi.start).mock.calls[0];
    expect(projectIdArg).toBe(9);
    expect(specIdArg).toBe(3);
    expect(typeof keyArg).toBe('string');
    expect(keyArg.length).toBeGreaterThan(0);
    // M27：生成后跳转到运行详情页
    expect(router.currentRoute.value.name).toBe('run-detail');
    expect(router.currentRoute.value.params.id).toBe('42');
  });

  it('shows a conflict message and reloads the latest draft on 409', async () => {
    vi.mocked(projectsApi.get).mockResolvedValue(project);
    vi.mocked(projectsApi.getSpec).mockResolvedValue(specView());
    vi.mocked(projectsApi.saveSpec).mockRejectedValue(
      new ApiError('SPEC_CONFLICT', '规格版本冲突：期望 1，当前 2。', 409, false),
    );

    const wrapper = await mountView();
    await wrapper.get('[data-testid="goal-input"]').setValue('改了目标');
    await nextTick();
    await buttonByText(wrapper, '保存修改').trigger('click');
    await flushPromises();

    expect(wrapper.text()).toContain('规格版本冲突');
    expect(vi.mocked(projectsApi.getSpec).mock.calls.length).toBeGreaterThanOrEqual(2);
  });
});
