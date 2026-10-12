import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router';

import { ensureSession } from '@/stores/auth';

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue'),
    meta: { public: true },
  },
  {
    path: '/',
    name: 'projects',
    component: () => import('@/views/ProjectListView.vue'),
  },
  {
    path: '/projects/new',
    name: 'project-new',
    component: () => import('@/views/ProjectCreateView.vue'),
  },
  {
    path: '/projects/:id',
    name: 'project-detail',
    component: () => import('@/views/ProjectDetailView.vue'),
    props: true,
  },
  {
    path: '/projects/:id/interview',
    name: 'project-interview',
    component: () => import('@/views/InterviewView.vue'),
    props: true,
  },
  {
    path: '/runs/:id',
    name: 'run-detail',
    component: () => import('@/views/RunDetailView.vue'),
    props: true,
  },
  { path: '/:pathMatch(.*)*', redirect: '/' },
];

export const router = createRouter({
  history: createWebHistory(),
  routes,
});

router.beforeEach(async (to) => {
  if (to.meta.public) {
    return true;
  }
  const authenticated = await ensureSession();
  if (!authenticated) {
    return { name: 'login', query: to.fullPath === '/' ? {} : { redirect: to.fullPath } };
  }
  return true;
});

export default router;
