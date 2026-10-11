import type { LoginResponse, UserView } from '../types/api';
import { request } from './client';

export function login(username: string, password: string): Promise<LoginResponse> {
  return request<LoginResponse>('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  });
}

export function me(): Promise<UserView> {
  return request<UserView>('/auth/me');
}
