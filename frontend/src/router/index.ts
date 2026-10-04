import { createRouter, createWebHistory } from 'vue-router'
export const router = createRouter({
  history: createWebHistory(),
  scrollBehavior: (to, _from, saved) => saved ?? (to.hash ? { el: to.hash } : { top: 0 }),
  routes: [
    {
      path: '/',
      name: 'home',
      meta: { title: '首页' },
      component: () => import('../views/HomeView.vue'),
    },
    {
      path: '/problems',
      meta: { title: '题库' },
      component: () => import('../views/ProblemsView.vue'),
    },
    {
      path: '/problems/:id',
      meta: { title: '题目详情' },
      component: () => import('../views/ProblemDetailView.vue'),
    },
    {
      path: '/trainings',
      meta: { title: '训练' },
      component: () => import('../views/TrainingsView.vue'),
    },
    {
      path: '/trainings/:id',
      meta: { title: '训练详情' },
      component: () => import('../views/TrainingDetailView.vue'),
    },
    {
      path: '/contests',
      meta: { title: '比赛' },
      component: () => import('../views/ContestsView.vue'),
    },
    {
      path: '/contests/:id',
      meta: { title: '比赛详情' },
      component: () => import('../views/ContestDetailView.vue'),
    },
    {
      path: '/submissions',
      meta: { title: '评测' },
      component: () => import('../views/SubmissionsView.vue'),
    },
    {
      path: '/submissions/:id',
      meta: { title: '评测详情' },
      component: () => import('../views/SubmissionDetailView.vue'),
    },
    {
      path: '/rankings',
      meta: { title: '排名' },
      component: () => import('../views/RankingsView.vue'),
    },
    {
      path: '/:pathMatch(.*)*',
      name: 'not-found',
      meta: { title: '页面不存在' },
      component: () => import('../views/NotFoundView.vue'),
    },
  ],
})
router.afterEach((to) => {
  document.title = `${String(to.meta.title ?? '首页')} · KF OnlineJudge`
})
