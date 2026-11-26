<script setup lang="ts">
import StatementMarkdown from './StatementMarkdown.vue'
import DisclosureHeading from './DisclosureHeading.vue'

defineProps<{ text: string; thinking?: boolean }>()
</script>

<template>
  <details class="model-reasoning">
    <DisclosureHeading icon="assistant" tone="muted">
      <span>{{ thinking ? '正在思考' : '已思考' }}</span>
    </DisclosureHeading>
    <StatementMarkdown :source="text" :links="false" highlight-code />
  </details>
</template>

<style scoped>
.model-reasoning {
  color: var(--secondary-ink);
  font-size: 0.875rem;
}
.model-reasoning .statement-markdown {
  max-block-size: 15rem;
  margin-block-start: 0.4rem;
  padding-inline: 0.75rem 0.15rem;
  border-inline-start: 2px solid var(--line);
  overflow: auto;
}
</style>
