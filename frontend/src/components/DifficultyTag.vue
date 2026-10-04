<script setup lang="ts">
import type { Difficulty } from '../data/preview'
defineProps<{ value: Difficulty | string }>()
</script>
<template>
  <span
    class="difficulty"
    :class="{
      'difficulty-easy': value === '入门' || value === '简单',
      'difficulty-medium': value === '中等',
      'difficulty-hard': value === '困难',
    }"
    >{{ value }}</span
  >
</template>
