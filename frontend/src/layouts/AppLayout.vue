<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import UiIcon from '../components/UiIcon.vue'
import { useSession } from '../composables/useSession'
const { user, refresh } = useSession()
onMounted(() => {
  void refresh().catch(() => {})
})
const route = useRoute()
const mobileOpen = ref(false)
const aboutMenu = ref<HTMLDetailsElement>()
const accountMenu = ref<HTMLDetailsElement>()
function closeAbout() {
  if (aboutMenu.value) aboutMenu.value.open = false
}
function closeAccount() {
  if (accountMenu.value) accountMenu.value.open = false
}
watch(
  () => route.fullPath,
  () => {
    closeAbout()
    closeAccount()
    mobileOpen.value = false
  },
)
const links = [
  { to: '/', label: '首页', icon: 'home' },
  { to: '/problems', label: '题库', icon: 'problems' },
  { to: '/trainings', label: '训练', icon: 'trainings' },
  { to: '/contests', label: '比赛', icon: 'contests' },
  { to: '/submissions', label: '评测', icon: 'submissions' },
  { to: '/rankings', label: '排名', icon: 'rankings' },
  { to: '/discussions', label: '讨论', icon: 'discussions' },
  { to: '/teams', label: '团队', icon: 'teams' },
]
const activePath = computed(() => '/' + route.path.split('/')[1])
</script>
<template>
  <div class="app-shell">
    <header class="app-header">
      <div class="header-inner">
        <RouterLink to="/" class="brand" aria-label="KF OnlineJudge 首页"
          ><span class="brand-mark">KF<span class="brand-dot">.</span></span
          ><span class="brand-name"
            >OnlineJudge<small>CODE · PRACTICE · GROW</small></span
          ></RouterLink
        >
        <button
          class="mobile-toggle"
          :aria-expanded="mobileOpen"
          aria-controls="main-navigation"
          @click="mobileOpen = !mobileOpen"
        >
          <UiIcon name="problems" /> 导航
        </button>
        <nav
          id="main-navigation"
          class="main-nav"
          :class="{ 'mobile-open': mobileOpen }"
          aria-label="主导航"
        >
          <RouterLink
            v-for="link in links"
            :key="link.to"
            :to="link.to"
            :class="{ active: activePath === link.to }"
            @click="mobileOpen = false"
            ><UiIcon :name="link.icon" />{{ link.label }}</RouterLink
          >
          <details ref="aboutMenu" class="about-menu" @keydown.esc.prevent="closeAbout">
            <summary :class="{ active: activePath === '/about' }">
              关于 <span aria-hidden="true">⌄</span>
            </summary>
            <div class="about-menu-links">
              <RouterLink
                to="/about"
                :aria-current="route.path === '/about' ? 'page' : undefined"
                @click="closeAbout"
                >简介</RouterLink
              >
              <RouterLink
                to="/about/developers"
                :aria-current="route.path === '/about/developers' ? 'page' : undefined"
                @click="closeAbout"
                >开发者</RouterLink
              >
            </div>
          </details>
        </nav>
        <div class="header-account">
          <details
            v-if="user"
            ref="accountMenu"
            class="about-menu"
            @keydown.esc.prevent="closeAccount"
          >
            <summary aria-label="账号菜单">
              {{ user.username }} <span aria-hidden="true">⌄</span>
            </summary>
            <div class="about-menu-links account-links">
              <RouterLink
                :to="{ name: 'profile', params: { username: user.username } }"
                @click="closeAccount"
                >个人主页</RouterLink
              >
              <a v-if="user.capabilities?.problem_maintenance" href="/admin/index.html"
                >管理控制台</a
              >
              <RouterLink to="/logout" @click="closeAccount">退出登录</RouterLink>
            </div>
          </details>
          <RouterLink v-else to="/login"
            ><el-button type="primary" size="small" round>登录</el-button></RouterLink
          >
        </div>
      </div>
    </header>
    <main id="main-content" class="app-main"><slot /></main>
    <footer class="app-footer">
      <div class="footer-inner">
        <div class="footer-brand">
          <span class="brand-mark">KF.</span><strong>每一次提交，都有新的收获。</strong>
          <p>一个面向练习、竞赛与交流的在线评测平台。</p>
        </div>
        <div>
          <h3>开始练习</h3>
          <RouterLink to="/problems">浏览题库</RouterLink
          ><RouterLink to="/trainings">专题训练</RouterLink
          ><RouterLink to="/contests">参加比赛</RouterLink>
        </div>
        <div>
          <h3>交流与成长</h3>
          <RouterLink to="/discussions">讨论社区</RouterLink
          ><RouterLink to="/rankings">过题排行榜</RouterLink
          ><RouterLink to="/teams/create">创建团队</RouterLink>
        </div>
        <div>
          <h3>平台</h3>
          <RouterLink to="/about">关于 KF OnlineJudge</RouterLink
          ><RouterLink to="/about/developers">开发者</RouterLink
          ><RouterLink to="/about/developers#system">系统状态</RouterLink
          ><a href="/admin/index.html">管理入口</a>
        </div>
      </div>
      <div class="footer-bottom">
        <span>© 2026 KF OnlineJudge</span><span>示例数据 · 备案信息待填写</span
        ><span>编程 · 练习 · 成长</span>
      </div>
    </footer>
  </div>
</template>

<style scoped>
.about-menu {
  position: relative;
  color: #64748b;
}
.about-menu summary {
  cursor: pointer;
  list-style: none;
  padding: 10px 13px;
  border-radius: 8px;
  font-size: 13px;
  display: flex;
  gap: 6px;
  align-items: center;
}
.about-menu summary::-webkit-details-marker {
  display: none;
}
.about-menu summary.active,
.about-menu[open] summary {
  background: #edf6ff;
  color: var(--blue);
}
.about-menu summary:focus-visible {
  outline: 2px solid var(--blue);
  outline-offset: 2px;
}
.about-menu-links {
  position: absolute;
  right: 0;
  top: calc(100% + 8px);
  min-width: 140px;
  padding: 6px;
  background: white;
  border: 1px solid #e4ebf4;
  border-radius: 10px;
  box-shadow: 0 8px 24px #263d5d18;
  z-index: 101;
}
.main-nav .about-menu-links a,
.account-links a {
  display: block;
  padding: 10px 14px;
}
.account-links a {
  border-radius: 6px;
  color: var(--el-text-color-primary);
  white-space: nowrap;
}
.about-menu-links a:hover,
.about-menu-links a[aria-current='page'] {
  background: #edf6ff;
  color: var(--blue);
}
</style>
