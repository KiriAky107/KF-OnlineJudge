import { createRouter, createWebHistory } from 'vue-router'
import { useSession } from '../composables/useSession'
import { assistantRouteName } from '../lib/assistantNavigation'

const assistantQueryKeys = new Set(['helpRun', 'trainingRun', 'assistantTab'])
export const router = createRouter({
  history: createWebHistory(),
  scrollBehavior: (to, from, saved) => {
    if (saved) return saved
    if (to.path === from.path && to.hash === from.hash) {
      const keys = new Set([...Object.keys(to.query), ...Object.keys(from.query)])
      const changed = [...keys].filter(
        (key) => JSON.stringify(to.query[key]) !== JSON.stringify(from.query[key]),
      )
      if (changed.length && changed.every((key) => assistantQueryKeys.has(key))) return false
    }
    return to.hash ? { el: to.hash } : { top: 0 }
  },
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
      name: 'problem-workspace',
      meta: { title: '题目详情' },
      component: () => import('../views/ProblemDetailView.vue'),
    },
    {
      path: '/trainings',
      meta: { title: '训练' },
      component: () => import('../views/TrainingsView.vue'),
    },
    {
      path: '/trainings/assistant',
      name: assistantRouteName,
      meta: { title: '学习助手' },
      component: () => import('../views/LearningAgentView.vue'),
    },
    {
      path: '/trainings/create',
      redirect: (to) => ({
        name: assistantRouteName,
        query: { trainingRun: to.query.run ?? to.query.trainingRun, assistantTab: 'training' },
      }),
    },
    {
      path: '/trainings/skill-drafts',
      meta: { title: '技能草稿' },
      component: () => import('../views/SkillDraftsView.vue'),
    },
    {
      path: '/trainings/skill-library',
      meta: { title: '技能目录' },
      component: () => import('../views/SkillLibraryView.vue'),
    },
    {
      path: '/trainings/official/:id',
      name: 'official-training-detail',
      meta: { title: '官方训练' },
      component: () => import('../views/TrainingPublicationView.vue'),
    },
    {
      path: '/trainings/:id',
      name: 'training-detail',
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
      path: '/teams/:id',
      name: 'team-detail',
      meta: { title: '团队详情' },
      component: () => import('../views/TeamDetailView.vue'),
    },
    {
      path: '/teams/:teamId/trainings/:id',
      name: 'team-training-detail',
      meta: { title: '团队训练' },
      component: () => import('../views/TrainingPublicationView.vue'),
    },
    {
      path: '/users/:username',
      name: 'profile',
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
    {
      path: '/about',
      meta: { title: '简介' },
      component: () => import('../views/AboutView.vue'),
    },
    {
      path: '/about/developers',
      meta: { title: '开发者' },
      component: () => import('../views/DevelopersView.vue'),
    },
    {
      path: '/:pathMatch(.*)*',
      name: 'not-found',
      meta: { title: '页面不存在' },
      component: () => import('../views/NotFoundView.vue'),
    },
  ],
})
router.beforeEach(async (to) => {
  if (to.path === '/about' && to.hash === '#system') return '/about/developers#system'
  if (
    to.name === 'profile' ||
    to.path === '/submissions' ||
    to.path.startsWith('/submissions/') ||
    to.path === '/logout'
  ) {
    try {
      if (await useSession().refresh()) return true
    } catch {
      return { path: '/login', query: { next: to.fullPath, unavailable: '1' } }
    }
    return { path: '/login', query: { next: to.fullPath } }
  }
})
router.afterEach((to) => {
  document.title = `${String(to.meta.title ?? '首页')} · KF OnlineJudge`
})
