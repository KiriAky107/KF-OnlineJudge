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
      path: '/discussions',
      meta: { title: '讨论' },
      component: () => import('../views/DiscussionsView.vue'),
    },
    {
      path: '/discussions/:id',
      meta: { title: '讨论详情' },
      component: () => import('../views/DiscussionDetailView.vue'),
    },
    { path: '/teams', meta: { title: '团队' }, component: () => import('../views/TeamsView.vue') },
    {
      path: '/teams/create',
      meta: { title: '创建团队' },
      component: () => import('../views/CreateTeamView.vue'),
    },
    {
      path: '/users/:username',
      meta: { title: '个人主页' },
      component: () => import('../views/ProfileView.vue'),
    },
    { path: '/login', meta: { title: '登录' }, component: () => import('../views/AuthView.vue') },
    {
      path: '/register',
      meta: { title: '注册' },
      component: () => import('../views/AuthView.vue'),
    },
    {
      path: '/logout',
      meta: { title: '退出登录' },
      component: () => import('../views/LogoutView.vue'),
    },
    { path: '/about', meta: { title: '关于' }, component: () => import('../views/AboutView.vue') },
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
