import { flushPromises, mount } from '@vue/test-utils';
import ElementPlus from 'element-plus';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { nextTick } from 'vue';

import { ApiError, projectsApi } from '@/api/client';
import SafeText from '@/components/SafeText.vue';
import { clearSession, login } from '@/stores/auth';
import LoginView from '@/views/LoginView.vue';
import ProjectListView from '@/views/ProjectListView.vue';

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
    },
  };
});

vi.mock('@/stores/auth', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/stores/auth')>();
  return {
    ...actual,
    login: vi.fn(),
    logout: vi.fn(),
    clearSession: vi.fn(),
  };
});

describe('SafeText', () => {
  it('renders model text verbatim without executing HTML', () => {
    const payload = '<img src=x onerror="alert(1)"> 活动报名';
    const wrapper = mount(SafeText, { props: { text: payload } });

    expect(wrapper.text()).toBe(payload);
    expect(wrapper.html()).not.toContain('<img');
    expect(wrapper.find('img').exists()).toBe(false);
  });
});

describe('ProjectListView', () => {
  beforeEach(() => {
    vi.mocked(projectsApi.list).mockReset();
  });

  it('lists the signed-in user projects with spec status', async () => {
    vi.mocked(projectsApi.list).mockResolvedValue({
      items: [
        { id: 1, name: '社区活动报名', active_spec_id: null, created_at: '2026-10-11T03:00:00' },
        { id: 2, name: '任务看板', active_spec_id: 7, created_at: '2026-10-11T04:30:00' },
      ],
    });

    const wrapper = mount(ProjectListView, {
      global: { plugins: [ElementPlus, makeTestRouter()] },
    });
    await flushPromises();
    await nextTick();

    expect(projectsApi.list).toHaveBeenCalledTimes(1);
    expect(wrapper.text()).toContain('社区活动报名');
    expect(wrapper.text()).toContain('任务看板');
    expect(wrapper.text()).toContain('需求草稿');
    expect(wrapper.text()).toContain('已冻结规格');
  });

  it('shows a load failure alert instead of an empty table', async () => {
    vi.mocked(projectsApi.list).mockRejectedValue(
      new ApiError('INTERNAL_ERROR', '加载项目失败，请稍后重试。', 500, true),
    );

    const wrapper = mount(ProjectListView, {
      global: { plugins: [ElementPlus, makeTestRouter()] },
    });
    await flushPromises();

    expect(wrapper.text()).toContain('加载项目失败，请稍后重试。');
  });

  it('redirects to login when the session has expired', async () => {
    vi.mocked(projectsApi.list).mockRejectedValue(
      new ApiError('UNAUTHORIZED', '未登录。', 401, false),
    );
    const router = makeTestRouter();
    await router.push('/');
    await router.isReady();

    mount(ProjectListView, { global: { plugins: [ElementPlus, router] } });
    await flushPromises();

    expect(clearSession).toHaveBeenCalled();
    expect(router.currentRoute.value.name).toBe('login');
  });
});

describe('LoginView', () => {
  beforeEach(() => {
    vi.mocked(login).mockReset();
  });

  it('submits credentials and routes to projects on success', async () => {
    vi.mocked(login).mockResolvedValue({ id: 1, username: 'tester' });
    const router = makeTestRouter();
    await router.push('/login');
    await router.isReady();

    const wrapper = mount(LoginView, { global: { plugins: [ElementPlus, router] } });

    const inputs = wrapper.findAll('input');
    await inputs[0].setValue('tester');
    await inputs[1].setValue('secret');
    await wrapper.get('button').trigger('click');
    await flushPromises();
    await nextTick();

    expect(login).toHaveBeenCalledWith('tester', 'secret');
    expect(router.currentRoute.value.name).toBe('projects');
  });

  it('keeps the user on the login page and shows the error when sign-in fails', async () => {
    vi.mocked(login).mockRejectedValue(
      new ApiError('UNAUTHORIZED', '用户名或密码错误。', 401, false),
    );
    const router = makeTestRouter();
    await router.push('/login');
    await router.isReady();

    const wrapper = mount(LoginView, { global: { plugins: [ElementPlus, router] } });

    const inputs = wrapper.findAll('input');
    await inputs[0].setValue('tester');
    await inputs[1].setValue('wrong');
    await wrapper.get('button').trigger('click');
    await flushPromises();
    await nextTick();

    expect(wrapper.text()).toContain('用户名或密码错误。');
    expect(router.currentRoute.value.name).toBe('login');
  });
});
