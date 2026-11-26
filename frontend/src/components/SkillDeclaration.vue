<script setup lang="ts">
import DisclosureHeading from './DisclosureHeading.vue'
import type { components } from '../api/generated/schema'

defineProps<{ declaration: components['schemas']['SkillDraftDocument'] }>()
</script>

<template>
  <dl class="declaration">
    <dt>简介</dt>
    <dd>{{ declaration.description }}</dd>
    <dt>教学方法</dt>
    <dd>{{ declaration.method }}</dd>
    <template v-if="declaration.references?.length">
      <dt>参考资料</dt>
      <dd>
        <details
          v-for="reference in declaration.references"
          :key="reference.path"
          class="reference"
        >
          <DisclosureHeading icon="document" tone="muted">{{ reference.path }}</DisclosureHeading>
          <pre>{{ reference.content }}</pre>
        </details>
      </dd>
    </template>
    <dt>许可</dt>
    <dd>{{ declaration.license }}</dd>
    <template v-if="declaration.attribution">
      <dt>来源与署名</dt>
      <dd>{{ declaration.attribution }}</dd>
    </template>
    <template v-if="declaration.requested_tools?.length">
      <dt>申请的工具</dt>
      <dd>
        <ul>
          <li v-for="tool in declaration.requested_tools" :key="tool.name + ':' + tool.version">
            {{ tool.name }} · v{{ tool.version }}
          </li>
        </ul>
      </dd>
    </template>
    <template v-if="declaration.parameters && Object.keys(declaration.parameters).length">
      <dt>默认参数</dt>
      <dd>
        <pre>{{ JSON.stringify(declaration.parameters, null, 2) }}</pre>
      </dd>
    </template>
  </dl>
</template>

<style scoped>
.declaration {
  display: grid;
  gap: 0.4rem;
  margin: 0.9rem 0;
}
.declaration dt {
  font-weight: 650;
}
.declaration dd {
  margin: 0 0 0.35rem;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.declaration ul,
.declaration pre {
  margin: 0;
}
.declaration pre {
  white-space: pre-wrap;
}
.reference {
  padding: 0.55rem 0.75rem;
  border: 1px solid var(--el-border-color-light);
  border-radius: var(--el-border-radius-base);
  background: var(--el-fill-color-lighter);
}
.reference + .reference {
  margin-top: 0.45rem;
}
.reference summary {
  color: var(--el-text-color-primary);
  font-weight: 600;
  cursor: pointer;
}
.reference summary:focus-visible {
  outline: 2px solid var(--el-color-primary);
  outline-offset: 2px;
}
.reference pre {
  margin-top: 0.45rem;
  color: var(--el-text-color-regular);
}
</style>
