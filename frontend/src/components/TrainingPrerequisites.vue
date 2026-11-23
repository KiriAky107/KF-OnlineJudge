<script setup lang="ts">
import type { components } from '../api/generated/schema'

defineProps<{ proofs: components['schemas']['TrainingPrerequisite'][] }>()
</script>

<template>
  <div class="small muted">
    <span>先修依据</span>
    <ul>
      <li v-for="proof in proofs" :key="proof.tag">
        <RouterLink :to="'/submissions/' + proof.evidence.submission_id">
          {{ proof.tag }} · 通过记录
        </RouterLink>
      </li>
    </ul>
  </div>
</template>
