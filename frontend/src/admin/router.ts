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
    { path: '/skill-reviews', component: () => import('./AdminSkillReviews.vue') },
    { path: '/skill-reports', component: () => import('./AdminSkillReports.vue') },
    { path: '/learning-observations', component: () => import('./AdminLearningObservations.vue') },
    { path: '/contests', component: () => import('./AdminContestSettlement.vue') },
    { path: '/problem-difficulties', component: () => import('./AdminDifficultyLevels.vue') },
    { path: '/problems', component: () => import('./AdminProblems.vue') },
    {
      path: '/problems/:id/edit',
      name: 'admin-problem-editor',
      component: () => import('./AdminProblemEditor.vue'),
    },
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
  const section = adminSections.find((item) => item.id === (to.path.slice(1) || 'overview'))
  const title =
    to.name === 'admin-problem-editor'
      ? '编辑题目'
      : to.path === '/problems'
        ? '题目维护'
        : (section?.title ?? '管理员身份')
  document.title = `${title} · KF 管理控制台`
})
