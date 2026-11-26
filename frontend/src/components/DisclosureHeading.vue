<script setup lang="ts">
import UiIcon from './UiIcon.vue'

defineProps<{ icon?: string; tone?: 'muted' | 'accent' | 'warning' }>()
</script>

<template>
  <summary class="disclosure-heading" :class="tone && 'disclosure-heading--' + tone">
    <slot name="indicator">
      <UiIcon v-if="icon" class="disclosure-icon" :name="icon" />
    </slot>
    <span class="disclosure-label"><slot /></span>
    <UiIcon class="disclosure-chevron" name="down" />
  </summary>
</template>

<style scoped>
.disclosure-heading {
  display: inline-flex;
  max-width: 100%;
  align-items: center;
  gap: 0.45rem;
  padding: 0.25rem 0.35rem;
  border-radius: 5px;
  cursor: pointer;
  list-style: none;
  color: var(--strong-ink);
  line-height: 1.4;
}
.disclosure-heading::-webkit-details-marker {
  display: none;
}
.disclosure-heading::marker {
  content: '';
}
.disclosure-heading:hover {
  background: var(--surface-soft);
}
.disclosure-heading:focus-visible {
  outline: 2px solid var(--accent-ink);
  outline-offset: 2px;
}
.disclosure-label {
  display: inline-flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 0 0.35rem;
  min-width: 0;
  overflow-wrap: anywhere;
}
.disclosure-icon,
.disclosure-chevron {
  flex: none;
  color: var(--secondary-ink);
}
.disclosure-heading--muted {
  color: var(--secondary-ink);
  font-size: 0.875rem;
}
.disclosure-heading--accent .disclosure-icon {
  color: var(--accent-ink);
}
.disclosure-heading--warning .disclosure-icon {
  color: var(--warn-ink);
}
:global(details[open] > .disclosure-heading > .disclosure-chevron) {
  transform: rotate(180deg);
}
</style>
