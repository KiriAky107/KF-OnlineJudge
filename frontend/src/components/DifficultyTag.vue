<script setup lang="ts">
import type { Difficulty } from '../data/preview'
import type { components } from '../api/generated/schema'
defineProps<{
  value: Difficulty | string
  level?: components['schemas']['DifficultyLevel'] | null
}>()
</script>
<template>
  <span
    class="difficulty"
    :style="level ? { '--difficulty-color': level.color } : undefined"
    :data-custom="level ? true : undefined"
    :class="{
      'difficulty-easy': value === '入门' || value === '简单',
      'difficulty-medium': value === '中等',
      'difficulty-hard': value === '困难',
    }"
    >{{ level?.label ?? value }}</span
  >
</template>

<style scoped>
.difficulty[data-custom] {
  display: inline-flex;
  align-items: center;
  border: 1px solid color-mix(in srgb, var(--difficulty-color) 55%, var(--surface));
  border-inline-start: 3px solid var(--difficulty-color);
  background: color-mix(in srgb, var(--difficulty-color) 15%, var(--surface));
  color: var(--strong-ink);
}
</style>
