import { flushPromises, mount } from '@vue/test-utils';
import ElementPlus from 'element-plus';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { profilesApi, projectsApi } from '@/api/client';
import type { ProfileOption } from '@/api/types';
import ProfilesView from '@/views/ProfilesView.vue';

import { makeTestRouter } from './helpers';

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>();
  return {
    ...actual,
    profilesApi: { list: vi.fn(), explain: vi.fn() },
    projectsApi: { list: vi.fn() },
  };
});

function option(overrides: Partial<ProfileOption> = {}): ProfileOption {
  return {
    profile_id: 'demo-js-mb',
    profile_version: '0.1',
    template_version: 'demo-0.2-js-mb',
    frontend_language: 'javascript',
    orm: 'mybatis',
    recommended: false,
    notes: ['前端 JavaScript：更轻量，无需类型检查步骤。'],
    ...overrides,
  };
}

async function mountView() {
  const wrapper = mount(ProfilesView, {
    global: { plugins: [ElementPlus, makeTestRouter()] },
  });
  await flushPromises();
  return wrapper;
}

describe('ProfilesView', () => {
  beforeEach(() => {
    vi.mocked(profilesApi.list).mockReset();
    vi.mocked(profilesApi.explain).mockReset();
    vi.mocked(projectsApi.list).mockReset();
  });

  it('lists the four verified combinations with the recommended badge', async () => {
    vi.mocked(profilesApi.list).mockResolvedValue([
      option({ profile_id: 'demo-ts-mp', orm: 'mybatis-plus', recommended: true }),
      option(),
    ]);

    const wrapper = await mountView();

    expect(wrapper.text()).toContain('demo-ts-mp');
    expect(wrapper.text()).toContain('推荐');
    expect(wrapper.text()).toContain('demo-js-mb');
  });

  it('fetches a plain-language explanation for a project', async () => {
    vi.mocked(profilesApi.list).mockResolvedValue([option()]);
    vi.mocked(projectsApi.list).mockResolvedValue({
      items: [{ id: 7, name: 'p', active_spec_id: null, created_at: '2026-01-01' }],
    });
    vi.mocked(profilesApi.explain).mockResolvedValue({
      profile_id: 'demo-js-mb',
      explanation: '该组合前端为 JavaScript，后端为 MyBatis。',
      thinking_mode: 'disabled',
      reasoning_effort: null,
    });

    const wrapper = await mountView();
    await wrapper.find('button').trigger('click');
    await flushPromises();

    expect(vi.mocked(profilesApi.explain)).toHaveBeenCalledWith(7, 'demo-js-mb');
    expect(wrapper.find('[data-testid="profile-explanation"]').text()).toContain('MyBatis');
  });

  it('surfaces a load failure without crashing', async () => {
    vi.mocked(profilesApi.list).mockRejectedValue(new Error('boom'));

    const wrapper = await mountView();

    expect(wrapper.find('[data-testid="profiles-error"]').exists()).toBe(true);
  });
});
