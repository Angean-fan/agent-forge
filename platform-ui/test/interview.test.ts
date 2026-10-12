import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import ElementPlus from 'element-plus';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { interviewApi } from '@/api/client';
import type { InterviewQuestion, InterviewView } from '@/api/types';
import InterviewViewComp from '@/views/InterviewView.vue';

import { makeTestRouter } from './helpers';

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>();
  return { ...actual, interviewApi: { get: vi.fn(), submit: vi.fn() } };
});

vi.mock('@/stores/auth', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/stores/auth')>();
  return { ...actual, clearSession: vi.fn() };
});

function question(overrides: Partial<InterviewQuestion> = {}): InterviewQuestion {
  return {
    question_id: 'q1',
    version: 1,
    type: 'single_choice',
    prompt: '成员能否重复报名同一个活动？',
    options: ['可以', '不可以'],
    allow_custom: false,
    required: true,
    affects: ['business_rules'],
    ...overrides,
  };
}

function view(overrides: Partial<InterviewView> = {}): InterviewView {
  return {
    project_id: 1,
    draft_id: 3,
    round: 1,
    status: 'CLARIFYING',
    waiting: true,
    questions: [question()],
    pending: [],
    issues: [],
    error_code: null,
    ...overrides,
  };
}

async function mountView(): Promise<VueWrapper> {
  const router = makeTestRouter();
  await router.push('/projects/1/interview');
  await router.isReady();
  return mount(InterviewViewComp, {
    props: { id: '1' },
    global: { plugins: [router, ElementPlus] },
  }) as unknown as VueWrapper;
}

function buttonByText(wrapper: VueWrapper, text: string) {
  return wrapper.findAll('button').find((node) => node.text().includes(text));
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('InterviewView', () => {
  it('shows a start action when the interview has not begun', async () => {
    vi.mocked(interviewApi.get).mockResolvedValue(view({ status: 'NEW', waiting: false, questions: [] }));
    vi.mocked(interviewApi.submit).mockResolvedValue(view());

    const wrapper = await mountView();
    await flushPromises();

    await buttonByText(wrapper, '开始访谈')!.trigger('click');
    await flushPromises();

    expect(interviewApi.submit).toHaveBeenCalledWith(1, []);
    expect(wrapper.find('[data-testid="question-q1"]').exists()).toBe(true);
  });

  it('builds answers for choice, custom and unsure questions', async () => {
    vi.mocked(interviewApi.get).mockResolvedValue(
      view({
        questions: [
          question({ question_id: 'q1', type: 'single_choice', options: ['可以', '不可以'] }),
          question({ question_id: 'q2', type: 'multi_choice', options: ['发布', '报名'] }),
          question({ question_id: 'q3', type: 'text', options: [], allow_custom: true }),
        ],
      }),
    );
    vi.mocked(interviewApi.submit).mockResolvedValue(view({ status: 'READY_TO_CONFIRM', waiting: false }));

    const wrapper = await mountView();
    await flushPromises();

    await wrapper.get('[data-testid="question-q1"] input[value="可以"]').setValue(true);
    await wrapper.get('[data-testid="option-q2-报名"]').setValue(true);
    await wrapper.get('[data-testid="text-q3"]').setValue('大约一百人');
    await buttonByText(wrapper, '提交回答')!.trigger('click');
    await flushPromises();

    expect(interviewApi.submit).toHaveBeenCalledWith(1, [
      { question_id: 'q1', question_version: 1, selected: ['可以'] },
      { question_id: 'q2', question_version: 1, selected: ['报名'] },
      { question_id: 'q3', question_version: 1, custom: '大约一百人' },
    ]);
    expect(wrapper.find('[data-testid="ready-alert"]').exists()).toBe(true);
  });

  it('marks an unsure answer and keeps it out of selections', async () => {
    vi.mocked(interviewApi.get).mockResolvedValue(view());
    vi.mocked(interviewApi.submit).mockResolvedValue(view({ round: 2 }));

    const wrapper = await mountView();
    await flushPromises();

    await wrapper.get('[data-testid="unsure-q1"]').setValue(true);
    await buttonByText(wrapper, '提交回答')!.trigger('click');
    await flushPromises();

    expect(interviewApi.submit).toHaveBeenCalledWith(1, [
      { question_id: 'q1', question_version: 1, unsure: true },
    ]);
  });

  it('surfaces stale-answer issues without losing progress', async () => {
    vi.mocked(interviewApi.get).mockResolvedValue(view());
    vi.mocked(interviewApi.submit).mockResolvedValue(
      view({ issues: ['q1: 答案绑定题目版本 1，当前为 2；过期题目的答案不予接受'] }),
    );

    const wrapper = await mountView();
    await flushPromises();

    await buttonByText(wrapper, '提交回答')!.trigger('click');
    await flushPromises();

    expect(wrapper.get('[data-testid="stale-alert"]').text()).toContain('过期');
    // 仍停留在当前题目，可重新作答
    expect(wrapper.find('[data-testid="question-q1"]').exists()).toBe(true);
  });

  it('shows a distinct message when generation failed', async () => {
    vi.mocked(interviewApi.get).mockResolvedValue(
      view({ status: 'NEEDS_INPUT', waiting: false, questions: [], error_code: 'MODEL_UNAVAILABLE' }),
    );

    const wrapper = await mountView();
    await flushPromises();

    expect(wrapper.get('[data-testid="needs-input-alert"]').text()).toContain('MODEL_UNAVAILABLE');
    expect(wrapper.find('[data-testid="submit-answers"]').exists()).toBe(false);
  });
});
