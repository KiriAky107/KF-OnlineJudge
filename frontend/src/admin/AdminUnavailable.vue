<script setup lang="ts">
import { useRoute } from 'vue-router'
const route = useRoute()
</script>
<template>
  <section class="admin-real-panel admin-access-panel">
    <p class="eyebrow">ADMINISTRATION</p>
    <h1>{{ route.path === '/forbidden' ? '无题目维护权限' : '身份服务暂时不可用' }}</h1>
    <p>请核对当前账号或稍后重新验证；此页面没有管理操作权限。</p>
    <div class="admin-actions">
      <RouterLink to="/login" class="admin-link-button">更换账号</RouterLink>
      <RouterLink to="/problems" class="admin-link-button">重新验证</RouterLink>
    </div>
  </section>
</template>
