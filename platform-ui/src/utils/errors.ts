import { ApiError } from '@/api/client';

export interface ErrorInfo {
  message: string;
  retryable: boolean;
  unauthorized: boolean;
}

/** 把 API 失败翻译成面向非技术用户的中文提示。 */
export function describeError(error: unknown): ErrorInfo {
  if (error instanceof ApiError) {
    return {
      message: error.message || '请求失败，请稍后重试。',
      retryable: error.retryable,
      unauthorized: error.isUnauthorized,
    };
  }
  return { message: '发生未知错误，请稍后重试。', retryable: true, unauthorized: false };
}
