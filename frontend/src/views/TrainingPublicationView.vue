<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api/client'
import type { components } from '../api/generated/schema'
import { ApiError, csrfHeaders, errorText, unwrap } from '../api/transport'
import DifficultyTag from '../components/DifficultyTag.vue'
import PageHeading from '../components/PageHeading.vue'
import PanelCard from '../components/PanelCard.vue'
import { useSession } from '../composables/useSession'
import { trainingDifficultyLabels } from '../lib/trainingSetChat'

const route = useRoute()
const router = useRouter()
const { user, refresh } = useSession()
const id = computed(() => (typeof route.params.id === 'string' ? route.params.id : ''))
const teamId = computed(() => (typeof route.params.teamId === 'string' ? route.params.teamId : ''))
const scopeLabel = computed(() => (teamId.value ? '团队训练' : '官方训练'))
const training = ref<components['schemas']['TrainingPublicationDetail']>()
const review = ref<components['schemas']['TeamTrainingReview']>()
const canReview = ref(false)
const reviewing = ref(false)
const reviewError = ref('')
const teachingTopics = {
  correctness: {
    title: '答案正确性',
    outline: '用公开样例写出预期输出。再选一个小规模边界输入，逐步核对状态变化和输出。',
  },
  resource_use: {
    title: '资源开销',
    outline: '从题目给定规模推导时间和空间需求。比较常见做法的复杂度，再检查重复操作和输出量。',
  },
  runtime: {
    title: '运行时错误',
    outline: '先复核下标、整数范围和空输入。用一个能手算的输入走查出错的操作。',
  },
  compilation: {
    title: '编译错误',
    outline: '核对语言版本和入口。一起读第一条编译报错，再用最小代码验证对应的语法和类型。',
  },
  mixed: {
    title: '解题流程',
    outline: '从题意和样例开始走一遍。按编译、运行和答案核验三个步骤交流复现方法。',
  },
} satisfies Record<
  components['schemas']['TeamTrainingReviewItem']['topic'],
  { title: string; outline: string }
>
const loading = ref(false)
const starting = ref(false)
const message = ref('')
const allAvailable = computed(() =>
  Boolean(
    training.value?.items.length && training.value.items.every((item) => item.problem_version_id),
  ),
)
let request = new AbortController()

async function load() {
  request.abort()
  request = new AbortController()
  const { signal } = request
  training.value = undefined
  review.value = undefined
  canReview.value = false
  reviewing.value = false
  reviewError.value = ''
  message.value = ''
  starting.value = false
  loading.value = true
  try {
    if (!id.value || (teamId.value && !user.value)) return
    if (teamId.value) {
      const [detail, membership] = await Promise.all([
        api
          .GET('/api/v1/teams/{team_id}/trainings/{training_id}', {
            params: { path: { team_id: teamId.value, training_id: id.value } },
            signal,
          })
          .then(unwrap),
        api
          .GET('/api/v1/teams/{team_id}/membership', {
            params: { path: { team_id: teamId.value } },
            signal,
          })
          .then(unwrap),
      ])
      if (!signal.aborted) {
        training.value = detail
        canReview.value = membership.role === 'captain'
      }
    } else {
      const detail = unwrap(
        await api.GET('/api/v1/official-trainings/{training_id}', {
          params: { path: { training_id: id.value } },
          signal,
        }),
      )
      if (!signal.aborted) training.value = detail
    }
  } catch (cause) {
    if (!signal.aborted) message.value = publicationError(cause)
  } finally {
    if (!signal.aborted) loading.value = false
  }
}

async function loadReview() {
  if (!training.value || !teamId.value || !canReview.value || reviewing.value) return
  const { signal } = request
  reviewing.value = true
  review.value = undefined
  reviewError.value = ''
  try {
    const result = unwrap(
      await api.GET('/api/v1/teams/{team_id}/trainings/{training_id}/review', {
        params: { path: { team_id: teamId.value, training_id: training.value.id } },
        signal,
      }),
    )
    if (!signal.aborted) review.value = result
  } catch (cause) {
    if (!signal.aborted) reviewError.value = publicationError(cause)
  } finally {
    if (!signal.aborted) reviewing.value = false
  }
}

function publicationError(cause: unknown) {
  if (cause instanceof ApiError && teamId.value && cause.status === 403)
    return '这份题单仅向该团队的正式成员开放。'
  if (cause instanceof ApiError && cause.status === 409) return '训练题目已变化，请刷新后重试。'
  return errorText(cause)
}

async function start() {
  if (!training.value || !user.value || !allAvailable.value || starting.value) return
  const { signal } = request
  const owner = user.value.id
  starting.value = true
  message.value = ''
  try {
    const options = { headers: csrfHeaders(), signal }
    const mine = teamId.value
      ? unwrap(
          await api.POST('/api/v1/teams/{team_id}/trainings/{training_id}/start', {
            ...options,
            params: { path: { team_id: teamId.value, training_id: training.value.id } },
          }),
        )
      : unwrap(
          await api.POST('/api/v1/official-trainings/{training_id}/start', {
            ...options,
            params: { path: { training_id: training.value.id } },
          }),
        )
    if (!signal.aborted && user.value?.id === owner)
      await router.push({
        name: 'training-detail',
        params: { id: mine.id },
        query: { tab: 'problems' },
      })
  } catch (cause) {
    if (!signal.aborted) {
      message.value = publicationError(cause)
      if (teamId.value && cause instanceof ApiError && cause.status === 403)
        training.value = undefined
    }
  } finally {
    if (!signal.aborted) starting.value = false
  }
}

function recheckRole() {
  if (teamId.value && user.value && !starting.value && !loading.value && !reviewing.value)
    void load()
}

watch([id, teamId, () => (teamId.value ? user.value?.id : undefined)], load, {
  immediate: true,
  flush: 'sync',
})
onMounted(() => {
  if (!user.value) void refresh().catch(() => undefined)
  window.addEventListener('focus', recheckRole)
})
onBeforeUnmount(() => {
  request.abort()
  window.removeEventListener('focus', recheckRole)
})
</script>

<template>
  <div class="breadcrumbs">
    <RouterLink v-if="teamId" :to="{ name: 'team-detail', params: { id: teamId } }"
      >团队</RouterLink
    >
    <RouterLink v-else to="/trainings">训练集</RouterLink>
    <span>/</span><span>{{ scopeLabel }}</span>
  </div>
  <p v-if="teamId && !user">
    <RouterLink :to="{ path: '/login', query: { next: route.fullPath } }">登录</RouterLink>
    后查看你所在团队的训练。
  </p>
  <p v-if="loading" role="status">正在读取{{ scopeLabel }}…</p>
  <p v-if="message" role="alert">{{ message }}</p>
  <template v-if="training">
    <PageHeading :title="training.title" :eyebrow="scopeLabel">
      <el-button
        v-if="user"
        type="primary"
        :loading="starting"
        :disabled="!allAvailable"
        @click="start"
      >
        开始训练
      </el-button>
      <RouterLink
        v-else-if="allAvailable"
        class="el-button el-button--primary"
        :to="{ path: '/login', query: { next: route.fullPath } }"
      >
        登录后开始
      </RouterLink>
    </PageHeading>
    <p v-if="teamId" class="muted">队长可查看本队成员按题汇总的做题卡点及讲评提纲。</p>
    <section class="panel publication-exercises" :aria-label="teamId ? '团队题单' : '官方题单'">
      <h2>题目列表</h2>
      <p class="muted">题单提示上限为 {{ training.max_hint_level ?? 'L3' }}。</p>
      <p v-if="!allAvailable" class="muted">题单中有题目暂不可用。</p>
      <ol>
        <li v-for="item in training.items" :key="item.position">
          <RouterLink
            v-if="item.problem_version_id"
            :to="{ name: 'problem-workspace', params: { id: item.problem_version_id } }"
          >
            {{ item.title }}
          </RouterLink>
          <span v-else class="muted">题目暂不可用</span>
          <DifficultyTag
            v-if="item.difficulty"
            :value="trainingDifficultyLabels[item.difficulty]"
          />
        </li>
      </ol>
    </section>
    <PanelCard v-if="canReview" title="队内讲评" aria-label="队内讲评">
      <p>只汇总本队正式成员开始训练后的完成评测。每道题至少有 5 名成员参与才纳入讲评。</p>
      <el-button :loading="reviewing" @click="loadReview">
        {{ review ? '刷新讲评提纲' : '查看讲评提纲' }}
      </el-button>
      <p v-if="reviewError" role="alert">{{ reviewError }}</p>
      <p v-else-if="review?.state === 'insufficient_data'" role="status">
        参与人数还不足，暂时没有团队讲评数据。
      </p>
      <p v-else-if="review && !review.items.length" role="status">暂未发现共同卡点。</p>
      <ul v-else-if="review" class="teaching-outline">
        <li v-for="item in review.items" :key="item.position">
          <strong>{{ item.title }}</strong>
          <span class="tag">{{ teachingTopics[item.topic].title }}</span>
          <p>{{ teachingTopics[item.topic].outline }}</p>
        </li>
      </ul>
    </PanelCard>
  </template>
</template>

<style scoped>
.publication-exercises {
  padding: 1.1rem 1.35rem;
}
.publication-exercises li {
  padding: 0.7rem 0.3rem;
}
.publication-exercises li + li {
  border-top: 1px solid var(--line);
}
.publication-exercises li .difficulty {
  margin-inline-start: 0.65rem;
}
.teaching-outline {
  padding-inline-start: 1.1rem;
}
.teaching-outline .tag {
  margin-inline-start: 0.6rem;
}
</style>
