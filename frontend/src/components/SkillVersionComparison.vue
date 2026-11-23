<script setup lang="ts">
import { computed, onUnmounted, ref } from 'vue'
import { api } from '../api/client'
import type { components } from '../api/generated/schema'
import { skillPurposes } from '../api/skillPurposes'
import { errorText, unwrap } from '../api/transport'

type Version = Pick<components['schemas']['SkillVersionView'], 'version_id' | 'release'>
type Declaration = components['schemas']['SkillDraftDocument']
const props = defineProps<{ saved: Version; selected: Version }>()
const declarations = ref<[Declaration, Declaration] | null>(null)
const loading = ref(false)
const error = ref('')
let controller: AbortController | undefined

const fields: ReadonlyArray<[string, (input: Declaration) => string]> = [
  ['名称', (doc) => doc.title],
  ['用途', (doc) => skillPurposes[doc.purpose]],
  ['简介', (doc) => doc.description],
  ['教学方法', (doc) => doc.method],
  ['许可', (doc) => doc.license ?? ''],
  ['署名', (doc) => doc.attribution ?? ''],
  ['默认参数', (doc) => JSON.stringify(doc.parameters ?? {}, null, 2)],
  [
    '申请的工具',
    (doc) => (doc.requested_tools ?? []).map((tool) => tool.name + ' v' + tool.version).join('\n'),
  ],
]
const changes = computed(() => {
  if (!declarations.value) return []
  const [saved, selected] = declarations.value
  return fields
    .map(([name, value]) => ({ name, before: value(saved), after: value(selected) }))
    .filter(({ before, after }) => before !== after)
})

async function compare() {
  if (loading.value || declarations.value) return
  controller = new AbortController()
  const { signal } = controller
  loading.value = true
  error.value = ''
  try {
    const documents = await Promise.all(
      [props.saved, props.selected].map((version) =>
        api
          .GET('/api/v1/learning/agent/skill-library/{version_id}', {
            params: { path: { version_id: version.version_id } },
            signal,
          })
          .then(unwrap),
      ),
    )
    if (!signal.aborted) declarations.value = [documents[0].document, documents[1].document]
  } catch (cause) {
    if (!signal.aborted) error.value = errorText(cause)
  } finally {
    if (!signal.aborted) loading.value = false
  }
}
onUnmounted(() => controller?.abort())
</script>

<template>
  <section class="comparison">
    <p>已保存 v{{ saved.release }} · 当前选择 v{{ selected.release }}</p>
    <el-button v-if="!declarations" :loading="loading" @click="compare">查看版本差异</el-button>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="declarations && !changes.length">两版的声明内容相同。</p>
    <dl v-if="changes.length">
      <template v-for="change in changes" :key="change.name">
        <dt>{{ change.name }}</dt>
        <dd>
          <div>
            <strong>v{{ saved.release }}</strong>
            <pre>{{ change.before || '未填写' }}</pre>
          </div>
          <div>
            <strong>v{{ selected.release }}</strong>
            <pre>{{ change.after || '未填写' }}</pre>
          </div>
        </dd>
      </template>
    </dl>
  </section>
</template>

<style scoped>
.comparison {
  border-top: 1px solid var(--line);
  margin-top: 0.7rem;
  padding: 0.75rem 0;
}
.comparison p {
  margin: 0 0 0.6rem;
}
.comparison dt {
  font-weight: 650;
  margin-top: 0.75rem;
}
.comparison dd {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(12rem, 100%), 1fr));
  gap: 0.5rem;
  margin: 0.35rem 0 0;
}
.comparison dd > div {
  min-width: 0;
  padding: 0.5rem;
  border: 1px solid var(--line);
  border-radius: 4px;
}
.comparison pre {
  margin: 0.25rem 0 0;
  font: inherit;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
</style>
