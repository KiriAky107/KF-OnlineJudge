<script setup lang="ts">
import DisclosureHeading from '../components/DisclosureHeading.vue'
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api/client'
import type { components } from '../api/generated/schema'
import { createSkillDraft } from '../api/skillDrafts'
import { skillPurposes } from '../api/skillPurposes'
import { csrfHeaders, errorText, unwrap } from '../api/transport'
import PageHeading from '../components/PageHeading.vue'
import PanelCard from '../components/PanelCard.vue'
import SkillDeclaration from '../components/SkillDeclaration.vue'
import SkillRating from '../components/SkillRating.vue'
import SkillReporting from '../components/SkillReporting.vue'
import { useSession } from '../composables/useSession'
import { displayTime } from '../lib/presentation'

type Purpose = components['schemas']['AgentPurpose']
type Published = components['schemas']['PublishedSkill']
const { user, refresh } = useSession()
const router = useRouter()
const search = ref('')
const purpose = ref<Purpose | ''>('')
const favoritesOnly = ref(false)
const expanded = ref(new Set<string>())
const page = ref<components['schemas']['PublishedSkillPage'] | null>(null)
const error = ref('')
const loading = ref(false)
const copying = ref('')
const favoritePending = ref('')
const actionError = ref('')
let controller: AbortController | undefined
let copyController: AbortController | undefined

async function load() {
  controller?.abort()
  page.value = null
  expanded.value.clear()
  error.value = ''
  actionError.value = ''
  favoritePending.value = ''
  loading.value = false
  if (!user.value) return
  controller = new AbortController()
  const { signal } = controller
  loading.value = true
  try {
    const result = unwrap(
      await api.GET('/api/v1/learning/agent/skill-library', {
        params: {
          query: {
            search: search.value.trim(),
            purpose: purpose.value || undefined,
            favorites_only: favoritesOnly.value || undefined,
          },
        },
        signal,
      }),
    )
    if (!signal.aborted) page.value = result
  } catch (cause) {
    if (!signal.aborted) error.value = errorText(cause)
  } finally {
    if (!signal.aborted) loading.value = false
  }
}
async function copy(versionId: string) {
  const owner = user.value?.id
  if (!owner || copying.value) return
  copyController = new AbortController()
  const { signal } = copyController
  copying.value = versionId
  actionError.value = ''
  try {
    const draft = await createSkillDraft({ source_version_id: versionId }, owner, signal)
    if (!signal.aborted) {
      await router.push({ path: '/trainings/skill-drafts', query: { draft: draft.draft_id } })
    }
  } catch (cause) {
    if (!signal.aborted) actionError.value = errorText(cause)
  } finally {
    if (!signal.aborted) copying.value = ''
  }
}
function setExpanded(event: Event, version: string) {
  if ((event.currentTarget as HTMLDetailsElement).open) expanded.value.add(version)
  else expanded.value.delete(version)
}
async function toggleFavorite(item: Published) {
  const signal = controller?.signal
  if (!user.value || !signal || favoritePending.value) return
  favoritePending.value = item.skill_id
  actionError.value = ''
  try {
    const result = unwrap(
      await api.PUT('/api/v1/learning/agent/skill-library/{skill_id}/favorite', {
        params: { path: { skill_id: item.skill_id } },
        body: { favorited: !item.favorited },
        headers: csrfHeaders(),
        signal,
      }),
    )
    if (signal.aborted || !page.value) return
    if (favoritesOnly.value && !result.favorited) {
      void load()
    } else {
      page.value.items = page.value.items.map((entry) =>
        entry.skill_id === item.skill_id ? { ...entry, favorited: result.favorited } : entry,
      )
    }
  } catch (cause) {
    if (!signal.aborted) actionError.value = errorText(cause)
  } finally {
    if (!signal.aborted) favoritePending.value = ''
  }
}
watch(
  () => user.value?.id,
  () => {
    copyController?.abort()
    copying.value = ''
    actionError.value = ''
    void load()
  },
  { immediate: true },
)
onMounted(async () => {
  if (user.value) return
  try {
    await refresh()
  } catch (cause) {
    error.value = errorText(cause)
  }
})
onUnmounted(() => {
  controller?.abort()
  copyController?.abort()
})
</script>

<template>
  <PageHeading title="技能目录">
    <RouterLink class="el-button" to="/trainings/assistant">学习助手</RouterLink>
    <RouterLink class="el-button" to="/trainings/skill-drafts">我的草稿</RouterLink>
  </PageHeading>
  <p v-if="error" role="alert">{{ error }}</p>
  <p v-if="actionError" role="alert">{{ actionError }}</p>
  <p v-if="!user">
    <RouterLink :to="{ path: '/login', query: { next: '/trainings/skill-library' } }"
      >登录</RouterLink
    >后查看已发布版本。
  </p>
  <PanelCard v-else title="已发布技能" icon="trainings">
    <form class="filters native-form" @submit.prevent="load">
      <label>名称 <input v-model="search" type="search" maxlength="80" /></label>
      <label
        >用途
        <select v-model="purpose">
          <option value="">全部用途</option>
          <option v-for="(label, value) in skillPurposes" :key="value" :value="value">
            {{ label }}
          </option>
        </select>
      </label>
      <label class="favorite-filter"
        ><input v-model="favoritesOnly" type="checkbox" @change="load" /> 仅收藏</label
      >
      <el-button native-type="submit" :loading="loading">搜索</el-button>
    </form>
    <p v-if="loading" role="status">正在读取目录…</p>
    <p v-else-if="page && !page.items.length">没有符合条件的版本。</p>
    <details
      v-for="item in page?.items ?? []"
      :key="item.version_id"
      class="result"
      @toggle="setExpanded($event, item.version_id)"
    >
      <DisclosureHeading icon="trainings"
        >{{ item.document.title }} · v{{ item.version }}</DisclosureHeading
      >
      <p class="small muted">
        {{ skillPurposes[item.document.purpose] }} · {{ item.author }} ·
        {{ displayTime(item.published_at) }}
      </p>
      <SkillDeclaration :declaration="item.document" />
      <RouterLink
        class="el-button"
        :to="{ path: '/trainings/assistant', query: { skill: item.version_id } }"
      >
        配置此版本
      </RouterLink>
      <el-button
        :disabled="!!copying"
        :loading="copying === item.version_id"
        @click="copy(item.version_id)"
      >
        复制为私有草稿
      </el-button>
      <el-button
        :disabled="!!favoritePending"
        :aria-pressed="item.favorited"
        @click="toggleFavorite(item)"
      >
        {{ item.favorited ? '取消收藏' : '收藏' }}
      </el-button>
      <template v-if="expanded.has(item.version_id)">
        <SkillRating :version-id="item.version_id" />
        <SkillReporting :version-id="item.version_id" />
      </template>
    </details>
    <p v-if="page?.truncated" class="small muted">显示最近发布的 50 个版本。可使用搜索缩小范围。</p>
  </PanelCard>
</template>

<style scoped>
.filters {
  display: flex;
  flex-wrap: wrap;
  align-items: end;
  gap: 0.6rem;
}
.filters label {
  display: grid;
  gap: 0.3rem;
}
.filters .favorite-filter {
  display: inline-flex;
  align-items: center;
  align-self: center;
}
.result {
  border-top: 1px solid var(--line);
  margin-top: 0.8rem;
  padding-top: 0.8rem;
  overflow-wrap: anywhere;
}
.result summary {
  cursor: pointer;
  font-weight: 650;
}
</style>
