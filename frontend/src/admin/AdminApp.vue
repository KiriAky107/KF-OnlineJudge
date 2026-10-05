<script setup lang="ts">
import { useRoute } from 'vue-router'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import UiIcon from '../components/UiIcon.vue'
import { adminSections } from './navigation'
const route = useRoute()
</script>
<template>
  <el-config-provider :locale="zhCn">
    <div class="admin-application">
      <header class="admin-app-header">
        <a href="/admin/index.html" class="brand"
          ><span class="brand-mark">KF.</span
          ><span class="brand-name">管理控制台<small>ADMIN CONSOLE</small></span></a
        >
        <span class="admin-app-label">独立管理应用 · 界面预览</span>
        <a href="/" class="admin-return">返回前台 <UiIcon name="arrow" /></a>
      </header>
      <nav class="admin-app-nav" aria-label="管理导航">
        <RouterLink
          v-for="section in adminSections"
          :key="section.id"
          :to="section.id === 'overview' ? '/' : '/' + section.id"
          :aria-label="section.title"
          :class="{ active: (route.params.section || 'overview') === section.id }"
          ><UiIcon :name="section.icon" /><span>{{ section.title }}</span></RouterLink
        >
      </nav>
      <main class="admin-app-main">
        <RouterView v-slot="{ Component, route: currentRoute }"
          ><component :is="Component" :key="currentRoute.path"
        /></RouterView>
      </main>
      <footer class="admin-app-footer">
        KF OnlineJudge / 管理控制台<span>示例数据 · 管理员授权与业务接口待接入</span>
      </footer>
    </div>
  </el-config-provider>
</template>
