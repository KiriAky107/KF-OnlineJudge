<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'
import UiIcon from '../components/UiIcon.vue'
const route = useRoute()
const mobileOpen = ref(false)
const links = [
  { to: '/', label: '首页', icon: 'home' },
  { to: '/problems', label: '题库', icon: 'problems' },
  { to: '/trainings', label: '训练', icon: 'trainings' },
  { to: '/contests', label: '比赛', icon: 'contests' },
  { to: '/submissions', label: '评测', icon: 'submissions' },
  { to: '/rankings', label: '排名', icon: 'rankings' },
  { to: '/discussions', label: '讨论', icon: 'discussions' },
  { to: '/teams', label: '团队', icon: 'teams' },
  { to: '/about', label: '关于', icon: 'about' },
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
        </nav>
        <div class="header-account">
          <el-tooltip content="页面使用示例内容，业务服务逐步接入" placement="bottom"
            ><span class="preview-label">界面预览</span></el-tooltip
          ><RouterLink to="/login"
            ><el-button type="primary" size="small" round>登录</el-button></RouterLink
          ><RouterLink to="/register"><el-button size="small" round>注册</el-button></RouterLink>
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
          ><RouterLink to="/about#system">系统状态</RouterLink
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
