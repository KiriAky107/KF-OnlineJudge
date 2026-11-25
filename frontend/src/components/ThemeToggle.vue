<script setup lang="ts">
import { computed } from 'vue'
import { useSiteTheme } from '../composables/useSiteTheme'
import UiIcon from './UiIcon.vue'

const { theme, toggle } = useSiteTheme()
const label = computed(() => (theme.value === 'dark' ? '切换浅色主题' : '切换深色主题'))
</script>

<template>
  <button type="button" class="theme-toggle" :aria-label="label" :title="label" @click="toggle">
    <UiIcon :name="theme === 'dark' ? 'sun' : 'moon'" />
  </button>
</template>

<style scoped>
.theme-toggle {
  display: grid;
  place-items: center;
  width: 2rem;
  height: 2rem;
  padding: 0;
  border: 1px solid transparent;
  border-radius: 8px;
  color: var(--el-text-color-regular);
  background: transparent;
}
.theme-toggle:hover,
.theme-toggle:focus-visible {
  color: var(--blue);
  background: var(--accent-bg);
  border-color: var(--el-border-color);
}
</style>
