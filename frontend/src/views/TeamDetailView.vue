<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../api/client'
import type { components } from '../api/generated/schema'
import { csrfHeaders, errorText, unwrap } from '../api/transport'
import PageHeading from '../components/PageHeading.vue'
import PanelCard from '../components/PanelCard.vue'
import { useSession } from '../composables/useSession'

type Role = components['schemas']['TeamMembershipView']['role']
type Members = components['schemas']['TeamMembersPage']
type Decision = components['schemas']['UpdateTeamMember']['decision']

const route = useRoute()
const { user } = useSession()
const teamId = computed(() => (typeof route.params.id === 'string' ? route.params.id : ''))
const team = ref<components['schemas']['TeamView']>()
const role = ref<Role>()
const pending = ref<Members>()
const members = ref<Members>()
const trainingPage = ref<components['schemas']['TrainingPublicationPage']>()
const offsets = ref({ pending: 0, member: 0 })
const loading = ref(false)
const changing = ref(false)
const error = ref('')
const path = computed(() => ({ team_id: teamId.value }))
let request = new AbortController()

async function read(signal: AbortSignal) {
  const params = { path: path.value }
  const [profile, membership] = await Promise.all([
    api.GET('/api/v1/teams/{team_id}', { params, signal }).then(unwrap),
    user.value
      ? api.GET('/api/v1/teams/{team_id}/membership', { params, signal }).then(unwrap)
      : undefined,
  ])
  if (signal.aborted) return
  team.value = profile
  role.value = membership?.role
  trainingPage.value = undefined
  if (membership?.role === 'member' || membership?.role === 'captain') {
    const page = unwrap(await api.GET('/api/v1/teams/{team_id}/trainings', { params, signal }))
    if (!signal.aborted) trainingPage.value = page
  }
  if (signal.aborted) return
  if (membership?.role !== 'captain') {
    pending.value = undefined
    members.value = undefined
    return
  }
  const [applications, roster] = await Promise.all(
    (['pending', 'member'] as const).map((value) =>
      api
        .GET('/api/v1/teams/{team_id}/members', {
          params: { ...params, query: { role: value, offset: offsets.value[value] } },
          signal,
        })
        .then(unwrap),
    ),
  )
  if (signal.aborted) return
  pending.value = applications
  members.value = roster
}

async function load() {
  request.abort()
  request = new AbortController()
  const { signal } = request
  team.value = undefined
  role.value = undefined
  pending.value = undefined
  members.value = undefined
  trainingPage.value = undefined
  offsets.value = { pending: 0, member: 0 }
  changing.value = false
  loading.value = true
  error.value = ''
  try {
    if (teamId.value) await read(signal)
  } catch (cause) {
    if (!signal.aborted) error.value = errorText(cause)
  } finally {
    if (!signal.aborted) loading.value = false
  }
}

async function update(write: (signal: AbortSignal) => Promise<void>) {
  if (changing.value) return
  const { signal } = request
  changing.value = true
  error.value = ''
  try {
    await write(signal)
    if (!signal.aborted) await read(signal)
  } catch (cause) {
    if (!signal.aborted) error.value = errorText(cause)
  } finally {
    if (!signal.aborted) changing.value = false
  }
}

function apply() {
  return update(async (signal) => {
    unwrap(
      await api.PUT('/api/v1/teams/{team_id}/membership', {
        params: { path: path.value },
        headers: csrfHeaders(),
        signal,
      }),
    )
  })
}

function leave() {
  return update(async (signal) => {
    const { response } = await api.DELETE('/api/v1/teams/{team_id}/membership', {
      params: { path: path.value },
      headers: csrfHeaders(),
      signal,
    })
    if (!response.ok) throw new Error('无法更新团队成员状态，请刷新后重试。')
  })
}

function moderate(accountId: string, decision: Decision) {
  return update(async (signal) => {
    unwrap(
      await api.PUT('/api/v1/teams/{team_id}/members/{account_id}', {
        params: { path: { ...path.value, account_id: accountId } },
        body: { decision },
        headers: csrfHeaders(),
        signal,
      }),
    )
  })
}

function showPage(list: 'pending' | 'member', offset: number) {
  return update(async () => {
    offsets.value[list] = Math.max(0, offset)
  })
}

watch([teamId, () => user.value?.id], load, { immediate: true, flush: 'sync' })
onBeforeUnmount(() => request.abort())
</script>

<template>
  <div class="breadcrumbs">
    <RouterLink to="/teams">团队</RouterLink><span>/</span><span>团队详情</span>
  </div>
  <p v-if="loading && !team" role="status">正在读取团队…</p>
  <p v-if="error" role="alert">{{ error }}</p>
  <template v-if="team">
    <PageHeading :title="team.name" :description="team.member_count + ' 名成员'">
      <RouterLink
        v-if="!user"
        class="el-button el-button--primary"
        :to="{ path: '/login', query: { next: route.fullPath } }"
      >
        登录后申请加入
      </RouterLink>
      <el-button v-else-if="role === null" type="primary" :loading="changing" @click="apply">
        申请加入
      </el-button>
      <el-button v-else-if="role === 'pending'" :loading="changing" @click="leave">
        撤回申请
      </el-button>
      <el-button v-else-if="role === 'member'" :loading="changing" @click="leave">
        退出团队
      </el-button>
    </PageHeading>

    <PanelCard title="团队介绍">
      <p class="team-description">{{ team.description }}</p>
      <p v-if="role === 'pending'" role="status">申请已发送，等待队长审核。</p>
      <p v-else-if="role === 'member'">你已加入团队。</p>
      <p v-else-if="role === 'captain'">你是这支团队的队长。</p>
    </PanelCard>

    <PanelCard v-if="trainingPage" title="团队训练">
      <p v-if="!trainingPage.items.length" class="muted">暂无已发布的团队训练</p>
      <ul v-else class="roster">
        <li v-for="training in trainingPage.items" :key="training.id">
          <RouterLink :to="{ name: 'team-training-detail', params: { teamId, id: training.id } }">
            {{ training.title }}
          </RouterLink>
          <span class="muted">{{ training.item_count }} 道题</span>
        </li>
      </ul>
      <p v-if="trainingPage.truncated" class="muted">当前显示最近发布的 50 份训练。</p>
    </PanelCard>

    <template v-if="role === 'captain'">
      <PanelCard v-if="pending" title="加入申请">
        <p v-if="!pending.items.length" class="muted">暂无待审核申请</p>
        <ul v-else class="roster">
          <li v-for="applicant in pending.items" :key="applicant.account_id">
            <span>{{ applicant.username }}</span>
            <div class="roster-actions">
              <el-button
                size="small"
                :disabled="changing"
                @click="moderate(applicant.account_id, 'remove')"
              >
                拒绝
              </el-button>
              <el-button
                size="small"
                type="primary"
                :disabled="changing"
                @click="moderate(applicant.account_id, 'approve')"
              >
                通过
              </el-button>
            </div>
          </li>
        </ul>
        <div v-if="offsets.pending || pending.next_offset !== null" class="roster-pages">
          <el-button
            size="small"
            :disabled="!offsets.pending || changing"
            @click="showPage('pending', offsets.pending - 50)"
          >
            上一页
          </el-button>
          <el-button
            v-if="pending.next_offset !== null"
            size="small"
            :disabled="changing"
            @click="showPage('pending', pending.next_offset)"
          >
            下一页
          </el-button>
        </div>
      </PanelCard>
      <PanelCard v-if="members" title="成员管理">
        <p v-if="!members.items.length" class="muted">暂无其他成员</p>
        <ul v-else class="roster">
          <li v-for="member in members.items" :key="member.account_id">
            <span>{{ member.username }}</span>
            <el-button
              size="small"
              :disabled="changing"
              @click="moderate(member.account_id, 'remove')"
            >
              移除成员
            </el-button>
          </li>
        </ul>
        <div v-if="offsets.member || members.next_offset !== null" class="roster-pages">
          <el-button
            size="small"
            :disabled="!offsets.member || changing"
            @click="showPage('member', offsets.member - 50)"
          >
            上一页
          </el-button>
          <el-button
            v-if="members.next_offset !== null"
            size="small"
            :disabled="changing"
            @click="showPage('member', members.next_offset)"
          >
            下一页
          </el-button>
        </div>
      </PanelCard>
    </template>
  </template>
</template>

<style scoped>
.team-description {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.roster {
  padding: 0;
  margin: 0;
  list-style: none;
}
.roster li {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  padding-block: 0.65rem;
}
.roster li + li {
  border-top: 1px solid var(--line);
}
.roster-actions {
  display: flex;
  align-items: center;
}
.roster-pages {
  margin-block-start: 0.65rem;
}
</style>
