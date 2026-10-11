<script setup lang="ts">
import { useRoute, useRouter } from 'vue-router'
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { useSession } from '../composables/useSession'
import { clearIntents, hasMaintenance } from './intents'
import { maintenanceError } from '../api/adminProblems'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import UiIcon from '../components/UiIcon.vue'
import { adminSections } from './navigation'
const route = useRoute(),
  router = useRouter(),
  session = useSession()
const identityError = ref('')
watch(session.user, (user) => {
  if (!user) {
    clearIntents()
    if (!route.meta.public) void router.replace('/login')
  } else if (!hasMaintenance(user) && !route.meta.public) void router.replace('/forbidden')
})
async function revalidate() {
  if (document.hidden || route.meta.public) return
  try {
    await session.refresh()
    identityError.value = ''
  } catch (e) {
    identityError.value = maintenanceError(e)
    await router.replace('/unavailable')
  }
}
async function logout() {
  try {
    await session.logout()
    clearIntents()
    await router.replace('/login')
  } catch (e) {
    identityError.value = maintenanceError(e)
  }
}
onMounted(() => {
  window.addEventListener('focus', revalidate)
  document.addEventListener('visibilitychange', revalidate)
})
onUnmounted(() => {
  window.removeEventListener('focus', revalidate)
  document.removeEventListener('visibilitychange', revalidate)
})
</script>
<template>
  <el-config-provider :locale="zhCn">
    <div class="admin-application">
      <header class="admin-app-header">
        <a href="/admin/index.html" class="brand"
          ><span class="brand-mark">KF.</span
          ><span class="brand-name">管理控制台<small>ADMIN CONSOLE</small></span></a
        >
        <span class="admin-app-label">题目维护 · 服务端权限验证</span>
        <button v-if="session.user.value" class="admin-return" @click="logout">
          退出 {{ session.user.value.username }}
        </button>
        <a href="/" class="admin-return">返回前台 <UiIcon name="arrow" /></a>
      </header>
      <nav class="admin-app-nav" aria-label="管理导航">
        <RouterLink
          v-for="section in adminSections"
          :key="section.id"
          :to="section.id === 'overview' ? '/' : '/' + section.id"
          :aria-label="section.title"
          :class="{
            active:
              (route.path === '/problems' ? 'problems' : route.params.section || 'overview') ===
              section.id,
          }"
          ><UiIcon :name="section.icon" /><span>{{ section.title }}</span></RouterLink
        >
      </nav>
      <main class="admin-app-main">
        <p v-if="identityError" role="alert">{{ identityError }}</p>
        <RouterView v-slot="{ Component, route: currentRoute }"
          ><component :is="Component" :key="currentRoute.path"
        /></RouterView>
      </main>
      <footer class="admin-app-footer">
        KF OnlineJudge / 管理控制台<span>题目维护已接通 · 其他业务区为明确预览</span>
      </footer>
    </div>
  </el-config-provider>
</template>
