import { mount, type VueWrapper } from '@vue/test-utils';
import ElementPlus from 'element-plus';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { runsApi } from '@/api/client';
import type { RunDetail, RunEvent } from '@/api/types';
import RunDetailView from '@/views/RunDetailView.vue';

import { makeTestRouter } from './helpers';

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>();
  return {
    ...actual,
    runsApi: {
      start: vi.fn(),
      get: vi.fn(),
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

function detail(overrides: Partial<RunDetail> = {}): RunDetail {
  return {
    id: 5,
    project_id: 9,
    spec_id: 3,
    status: 'QUEUED',
    phase: null,
    error_code: null,
    created_at: '2026-10-11T05:00:00',
    updated_at: '2026-10-11T05:00:05',
    tasks: { total: 4, done: 1 },
    cost: { currency_unit: 'micro_yuan', settled_micro: 0, reserved_micro: 0, unknown_calls: 0 },
    artifacts: [],
    verification: null,
    cancellation: null,
    ...overrides,
  };
}

function event(sequence: number, type: string, payload: Record<string, unknown> = {}): RunEvent {
  return { sequence, type, payload, created_at: '2026-10-11T05:00:00' };
}

/** 反复让出微任务，排空 poll() 的 await 链（不依赖 setTimeout）。 */
async function settle(): Promise<void> {
  for (let i = 0; i < 12; i += 1) {
    await Promise.resolve();
  }
}

async function mountView(): Promise<VueWrapper> {
  const wrapper = mount(RunDetailView, {
    props: { id: '5' },
    global: { plugins: [ElementPlus, makeTestRouter()] },
  });
  await settle();
  return wrapper;
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.mocked(runsApi.get).mockReset();
  vi.mocked(runsApi.events).mockReset();
  vi.mocked(runsApi.cancel).mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('RunDetailView', () => {
  it('renders status, phase, task counts and stops polling once terminal', async () => {
    vi.mocked(runsApi.get).mockResolvedValue(
      detail({
        status: 'SUCCEEDED',
        phase: 'verify',
        tasks: { total: 4, done: 4 },
        cost: { currency_unit: 'micro_yuan', settled_micro: 2_000_000, reserved_micro: 0, unknown_calls: 1 },
        artifacts: [
          { id: 1, kind: 'zip', path: 'source.zip', sha256: 'f'.repeat(64), verified: true, created_at: '2026-10-11T05:00:09' },
        ],
        verification: { label: '已验证', verified: true, checks: [{ name: 'backend_build', status: 'PASS' }] },
      }),
    );
    vi.mocked(runsApi.events).mockResolvedValue({
      run_id: 5,
      items: [event(1, 'queued'), event(2, 'succeeded')],
      latest_sequence: 2,
    });

    const wrapper = await mountView();

    expect(wrapper.text()).toContain('已完成');
    expect(wrapper.text()).toContain('构建与验收');
    expect(wrapper.get('[data-testid="task-count"]').text()).toBe('4 / 4');
    expect(wrapper.text()).toContain('¥2.0000');
    expect(wrapper.text()).toContain('有 1 次调用缺少核定价目');
    expect(wrapper.get('[data-testid="download-link"]').attributes('href')).toBe('/api/runs/5/artifact');
    expect(wrapper.text()).toContain('已验证');

    expect(runsApi.get).toHaveBeenCalledTimes(1);
    // 终态后不再轮询
    await vi.advanceTimersByTimeAsync(10000);
    await settle();
    expect(runsApi.get).toHaveBeenCalledTimes(1);
  });

  it('polls every 2s and reads events incrementally', async () => {
    vi.mocked(runsApi.get)
      .mockResolvedValueOnce(detail({ status: 'QUEUED' }))
      .mockResolvedValueOnce(detail({ status: 'GENERATING', phase: 'assemble' }));
    vi.mocked(runsApi.events)
      .mockResolvedValueOnce({ run_id: 5, items: [event(1, 'queued'), event(2, 'claimed')], latest_sequence: 2 })
      .mockResolvedValueOnce({ run_id: 5, items: [event(3, 'stage', { stage: 'assemble' })], latest_sequence: 3 });

    const wrapper = await mountView();

    expect(runsApi.get).toHaveBeenCalledTimes(1);
    expect(runsApi.events).toHaveBeenLastCalledWith(5, 0);
    expect(wrapper.findAll('[data-testid="event-list"] li')).toHaveLength(2);

    await vi.advanceTimersByTimeAsync(2000);
    await settle();

    expect(runsApi.get).toHaveBeenCalledTimes(2);
    expect(runsApi.events).toHaveBeenLastCalledWith(5, 2); // 只取新事件
    expect(wrapper.findAll('[data-testid="event-list"] li')).toHaveLength(3);
    expect(wrapper.text()).toContain('装配源码');
    expect(wrapper.text()).toContain('stage');
  });

  it('backs off on poll failure without stopping', async () => {
    vi.mocked(runsApi.get)
      .mockRejectedValueOnce(new (class extends Error {})('boom'))
      .mockResolvedValueOnce(detail({ status: 'GENERATING' }));
    vi.mocked(runsApi.events).mockResolvedValue({ run_id: 5, items: [], latest_sequence: 0 });

    const wrapper = await mountView();
    expect(wrapper.text()).toContain('发生未知错误');

    // 失败后下一次重试间隔变长（并非 2s）
    await vi.advanceTimersByTimeAsync(2000);
    await settle();
    expect(runsApi.get).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(2000);
    await settle();
    expect(runsApi.get).toHaveBeenCalledTimes(2);
  });

  it('shows failure code and NOT_RUN items, with no download link', async () => {
    vi.mocked(runsApi.get).mockResolvedValue(
      detail({
        status: 'FAILED',
        error_code: 'VERIFICATION_FAILED',
        verification: {
          label: '未验证',
          verified: false,
          checks: [
            { name: 'backend_test', status: 'FAIL' },
            { name: 'frontend_build', status: 'NOT_RUN' },
          ],
        },
      }),
    );
    vi.mocked(runsApi.events).mockResolvedValue({ run_id: 5, items: [event(1, 'failed', { code: 'VERIFICATION_FAILED' })], latest_sequence: 1 });

    const wrapper = await mountView();

    expect(wrapper.get('[data-testid="failure-alert"]').text()).toContain('VERIFICATION_FAILED');
    expect(wrapper.text()).toContain('backend_test（FAIL）');
    expect(wrapper.text()).toContain('frontend_build（NOT_RUN）');
    expect(wrapper.find('[data-testid="download-link"]').exists()).toBe(false);
  });

  it('labels an unverified failure package', async () => {
    vi.mocked(runsApi.get).mockResolvedValue(
      detail({
        status: 'FAILED',
        error_code: 'VERIFICATION_FAILED',
        artifacts: [
          { id: 2, kind: 'zip', path: 'source.zip', sha256: 'e'.repeat(64), verified: false, created_at: '2026-10-11T05:01:00' },
        ],
        verification: { label: '未验证', verified: false, checks: [{ name: 'backend_build', status: 'FAIL' }] },
      }),
    );
    vi.mocked(runsApi.events).mockResolvedValue({ run_id: 5, items: [], latest_sequence: 0 });

    const wrapper = await mountView();

    expect(wrapper.find('[data-testid="download-link"]').exists()).toBe(true);
    expect(wrapper.text()).toContain('未验证');
    expect(wrapper.text()).toContain('该交付包未通过全部必需检查');
  });

  it('shows a cancel button for an in-flight run and calls the cancel API', async () => {
    vi.mocked(runsApi.get).mockResolvedValue(detail({ status: 'GENERATING' }));
    vi.mocked(runsApi.events).mockResolvedValue({ run_id: 5, items: [], latest_sequence: 0 });
    vi.mocked(runsApi.cancel).mockResolvedValue({
      id: 5,
      project_id: 9,
      spec_id: 3,
      status: 'CANCELLED',
      error_code: null,
      cancel_requested: true,
      lease_owner: null,
      created_at: '2026-10-11T05:00:00',
      updated_at: '2026-10-11T05:00:06',
    });

    const wrapper = await mountView();
    const button = wrapper.get('[data-testid="cancel-run"]');
    await button.trigger('click');
    await settle();

    expect(runsApi.cancel).toHaveBeenCalledWith(5);
    expect(wrapper.text()).toContain('已取消');
  });

  it('shows the cancellation cost notice and hides the button once cancelled', async () => {
    vi.mocked(runsApi.get).mockResolvedValue(
      detail({
        status: 'CANCELLED',
        cancellation: { requested: true, notice: '取消不会撤回模型 Provider 已处理的调用' },
      }),
    );
    vi.mocked(runsApi.events).mockResolvedValue({ run_id: 5, items: [], latest_sequence: 0 });

    const wrapper = await mountView();

    expect(wrapper.find('[data-testid="cancel-run"]').exists()).toBe(false);
    expect(wrapper.get('[data-testid="cancel-notice"]').text()).toContain('运行已取消');
    expect(wrapper.text()).toContain('取消不会撤回模型 Provider 已处理的调用');
  });
});
