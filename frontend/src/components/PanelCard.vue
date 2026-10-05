<script setup lang="ts">
import UiIcon from './UiIcon.vue'
defineProps<{ title: string; icon?: string; subtitle?: string }>()
</script>
<template>
  <section class="panel">
    <header class="panel-heading">
      <h2>
        <UiIcon v-if="icon" :name="icon" />{{ title }}<small v-if="subtitle">{{ subtitle }}</small>
      </h2>
      <div class="panel-actions"><slot name="actions" /></div>
    </header>
    <div class="panel-body"><slot /></div>
  </section>
</template>
