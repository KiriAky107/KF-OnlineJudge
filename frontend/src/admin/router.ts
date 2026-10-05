import { createRouter, createWebHashHistory } from 'vue-router'
import { adminSections } from './navigation'

export const adminRouter = createRouter({
  history: createWebHashHistory('/admin/index.html'),
  scrollBehavior: () => ({ top: 0 }),
  routes: [{ path: '/:section?', component: () => import('./AdminView.vue') }],
})
adminRouter.afterEach((to) => {
  const section = adminSections.find((item) => item.id === (to.params.section || 'overview'))
  document.title = `${section?.title ?? '页面不存在'} · KF 管理控制台`
})
