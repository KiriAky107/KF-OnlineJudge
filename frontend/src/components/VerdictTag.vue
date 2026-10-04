<script setup lang="ts">
import { verdictLabels, type Verdict } from '../data/preview'
defineProps<{ value: Verdict; full?: boolean }>()
</script>
<template>
  <span class="verdict" :class="`verdict-${value.toLowerCase()}`">{{
    full ? verdictLabels[value] : value === 'Pending' ? '等待评测' : value
  }}</span>
</template>
