import { defineComponent, h } from 'vue';
import { createMemoryHistory, createRouter, type Router } from 'vue-router';

const Stub = defineComponent({ name: 'RouteStub', render: () => h('div') });

/** 组件测试用的最小路由（无登录守卫，避免影响被测组件的导航断言）。 */
export function makeTestRouter(): Router {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', name: 'projects', component: Stub },
      { path: '/login', name: 'login', component: Stub },
      { path: '/projects/new', name: 'project-new', component: Stub },
      { path: '/projects/:id', name: 'project-detail', component: Stub },
      { path: '/projects/:id/interview', name: 'project-interview', component: Stub },
      { path: '/runs/:id', name: 'run-detail', component: Stub },
      { path: '/profiles', name: 'profiles', component: Stub },
    ],
  });
}
