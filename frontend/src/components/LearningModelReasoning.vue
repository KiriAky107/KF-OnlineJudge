<script setup lang="ts">
import type { components } from '../api/generated/schema'
import DisclosureHeading from './DisclosureHeading.vue'

defineProps<{
  text: string
  thinking?: boolean
  kind?: components['schemas']['ModelHint']['reasoning_kind']
}>()
</script>

<template>
  <details v-if="kind === 'text' && text.trim()" class="model-reasoning">
    <DisclosureHeading icon="assistant" tone="muted">
      <span>已思考<span v-if="thinking"> · 接收中</span></span>
    </DisclosureHeading>
    <pre class="reasoning-content">{{ text }}</pre>
  </details>
</template>

<style scoped>
.model-reasoning {
  color: var(--secondary-ink);
  font-size: 0.875rem;
}
.reasoning-content {
  max-block-size: 15rem;
  margin: 0.4rem 0 0;
  padding-inline: 0.75rem 0.15rem;
  border-inline-start: 2px solid var(--line);
  overflow: auto;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
  font: inherit;
}
</style>
