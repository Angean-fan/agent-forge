import { createRouter, createWebHistory } from 'vue-router';

import { useAuthStore } from '../stores/auth';
import HomeView from '../views/HomeView.vue';
import LoginView from '../views/LoginView.vue';

// 能力包通过 src/router/modules/*.js 追加业务路由，无需修改本文件。
const modules = import.meta.glob('./modules/*.js', { eager: true });

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: LoginView },
    { path: '/', name: 'home', component: HomeView, meta: { requiresAuth: true } },
    ...Object.values(modules).map((module) => module.default),
  ],
});

router.beforeEach((to) => {
  const auth = useAuthStore();
  if (to.meta.requiresAuth && !auth.token) {
    return { name: 'login' };
  }
  return true;
});

export default router;
