<script setup lang="ts">
defineProps<{ title: string; description?: string }>()
</script>
<template>
  <div class="empty-state">
    <el-empty :description="title" :image-size="80" />
    <p v-if="description" class="muted">{{ description }}</p>
    <slot />
  </div>
</template>
