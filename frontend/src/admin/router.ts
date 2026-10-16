import { createRouter, createWebHashHistory } from 'vue-router'
import { adminSections } from './navigation'
import { useSession } from '../composables/useSession'
import { hasMaintenance } from './intents'
export const adminRouter = createRouter({
  history: createWebHashHistory('/admin/index.html'),
  scrollBehavior: () => ({ top: 0 }),
  routes: [
    { path: '/login', component: () => import('./AdminLogin.vue'), meta: { public: true } },
    {
      path: '/forbidden',
      component: () => import('./AdminUnavailable.vue'),
      meta: { public: true },
    },
    {
      path: '/unavailable',
      component: () => import('./AdminUnavailable.vue'),
      meta: { public: true },
    },
    { path: '/problems', component: () => import('./AdminProblems.vue') },
    { path: '/:section?', component: () => import('./AdminView.vue') },
  ],
})
adminRouter.beforeEach(async (to) => {
  if (to.meta.public) return true
  const session = useSession()
  try {
    if (!(await session.refresh())) return '/login'
    return hasMaintenance(session.user.value) ? true : '/forbidden'
  } catch {
    return '/unavailable'
  }
})
adminRouter.afterEach((to) => {
  const section = adminSections.find((item) => item.id === (to.params.section || 'overview'))
  document.title = `${to.path === '/problems' ? '题目维护' : (section?.title ?? '管理员身份')} · KF 管理控制台`
})
