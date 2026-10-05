<script setup lang="ts">
import AppLayout from './layouts/AppLayout.vue'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
</script>

<template>
  <el-config-provider :locale="zhCn">
    <AppLayout>
      <RouterView v-slot="{ Component, route }">
        <component :is="Component" :key="route.path" />
      </RouterView>
    </AppLayout>
  </el-config-provider>
</template>
