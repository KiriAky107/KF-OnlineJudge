<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from '../api/client'
import type { components } from '../api/generated/schema'
import { errorText, unwrap } from '../api/transport'
import PageHeading from '../components/PageHeading.vue'

const query = ref('')
const teams = ref<components['schemas']['TeamPage']>()
const loading = ref(false)
const error = ref('')
let request = new AbortController()

async function search() {
  request.abort()
  request = new AbortController()
  const { signal } = request
  loading.value = true
  error.value = ''
  try {
    const page = unwrap(
      await api.GET('/api/v1/teams', {
        params: { query: { search: query.value.trim() } },
        signal,
      }),
    )
    if (!signal.aborted) teams.value = page
  } catch (cause) {
    if (!signal.aborted) error.value = errorText(cause)
  } finally {
    if (!signal.aborted) loading.value = false
  }
}

onMounted(search)
onBeforeUnmount(() => request.abort())
</script>

<template>
  <PageHeading title="团队" eyebrow="TEAMS" description="找到一起练习和交流的伙伴。">
    <RouterLink to="/teams/create" class="el-button el-button--primary">创建团队</RouterLink>
  </PageHeading>
  <form class="filters" @submit.prevent="search">
    <el-input
      v-model="query"
      placeholder="搜索团队名称"
      aria-label="搜索团队名称"
      maxlength="30"
      clearable
      @clear="search"
    />
    <el-button native-type="submit" :loading="loading">搜索</el-button>
  </form>
  <p v-if="error" role="alert">{{ error }}</p>
  <p v-else-if="loading" role="status">正在读取团队…</p>
  <template v-else-if="teams">
    <div class="card-grid">
      <article v-for="team in teams.items" :key="team.id" class="panel team-card">
        <span class="team-logo" aria-hidden="true">{{
          Array.from(team.name).slice(0, 2).join('')
        }}</span>
        <div class="team-card-body">
          <h2>
            <RouterLink :to="{ name: 'team-detail', params: { id: team.id } }">
              {{ team.name }}
            </RouterLink>
          </h2>
          <p>{{ team.description }}</p>
          <span class="muted">{{ team.member_count }} 名成员</span>
        </div>
      </article>
    </div>
    <el-empty
      v-if="!teams.items.length"
      :description="query.trim() ? '没有匹配的团队' : '暂无团队'"
    />
    <p v-if="teams.truncated" class="muted">还有其他团队，可以按名称搜索。</p>
  </template>
</template>

<style scoped>
.filters > .el-input {
  width: min(100%, 23rem);
}
.team-card-body {
  min-width: 0;
  flex: 1;
  overflow-wrap: anywhere;
}
.team-card-body p {
  white-space: pre-wrap;
}
</style>
