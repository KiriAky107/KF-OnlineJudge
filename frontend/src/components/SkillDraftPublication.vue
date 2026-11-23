<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api } from '../api/client'
import type { components } from '../api/generated/schema'
import { ApiError, csrfHeaders, errorText, unwrap } from '../api/transport'
import { displayTime } from '../lib/presentation'

type Review = components['schemas']['SkillVersionReview']
type Page = components['schemas']['SkillVersionReviewPage']
const props = defineProps<{ draftId: string; revision: number; license?: string | null }>()
const versions = ref<Page | null>(null)
const confirmed = ref(false)
const archiveVersion = ref('')
const error = ref('')
const busy = ref(false)
const labels: Record<Review['state'], string> = {
  pending: '待审核',
  published: '已发布',
  archived: '已归档',
  rejected: '已驳回',
  withdrawn: '已撤回',
  disabled: '已停用',
}
const submitted = computed(() =>
  versions.value?.items.some((item) => item.draft_revision === props.revision),
)
const controller = new AbortController()
const { signal } = controller

async function perform(operation: () => Promise<void>) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    await operation()
  } catch (cause) {
    if (signal.aborted) return
    if (cause instanceof ApiError && cause.status === 409) {
      error.value = '版本状态或草稿修订已变化，请重新打开草稿。'
    } else if (cause instanceof ApiError && cause.status === 422) {
      error.value = '公开版本须填写许可，默认参数不得包含私有题目范围。'
    } else {
      error.value = errorText(cause)
    }
  } finally {
    if (!signal.aborted) busy.value = false
  }
}
async function load() {
  const page = unwrap(
    await api.GET('/api/v1/learning/agent/skill-drafts/{draft_id}/releases', {
      params: { path: { draft_id: props.draftId } },
      signal,
    }),
  )
  if (!signal.aborted) versions.value = page
}
function submit() {
  if (!confirmed.value) return
  void perform(async () => {
    const release = unwrap(
      await api.POST('/api/v1/learning/agent/skill-drafts/{draft_id}/releases', {
        params: { path: { draft_id: props.draftId } },
        body: { revision: props.revision, confirmed_public: true },
        headers: csrfHeaders(),
        signal,
      }),
    )
    if (!signal.aborted && versions.value) {
      const updated = [
        release,
        ...versions.value.items.filter((item) => item.version_id !== release.version_id),
      ]
      versions.value = {
        items: updated.slice(0, 50),
        truncated: versions.value.truncated || updated.length > 50,
      }
      confirmed.value = false
    }
  })
}
function changeVersion(item: Review, action: 'withdraw' | 'archive') {
  void perform(async () => {
    const route =
      action === 'archive'
        ? '/api/v1/learning/agent/skill-versions/{version_id}/archive'
        : '/api/v1/learning/agent/skill-versions/{version_id}/withdraw'
    const result = unwrap(
      await api.POST(route, {
        params: { path: { version_id: item.version_id } },
        headers: csrfHeaders(),
        signal,
      }),
    )
    if (!signal.aborted && versions.value) {
      versions.value.items = versions.value.items.map((version) =>
        version.version_id === result.version_id ? result : version,
      )
      archiveVersion.value = ''
    }
  })
}
onMounted(() => void perform(load))
onUnmounted(() => controller.abort())
</script>

<template>
  <section class="publication stack" aria-label="版本送审">
    <h3>版本送审</h3>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="!versions && busy" role="status">正在读取版本…</p>
    <el-button v-else-if="!versions" @click="perform(load)">重新读取</el-button>
    <template v-else>
      <p v-if="submitted" class="small muted">已提交此修订。保存新的草稿修订后可重新送审。</p>
      <p v-else-if="!license?.trim()" class="small muted">请先填写许可并保存草稿。</p>
      <div v-else class="stack">
        <label
          ><input v-model="confirmed" type="checkbox" :disabled="busy" />
          我确认已保存的声明和参考资料可向平台用户公开</label
        >
        <div>
          <el-button :disabled="!confirmed || busy" @click="submit">提交已保存修订</el-button>
        </div>
      </div>
      <ul v-if="versions.items.length" class="versions">
        <li v-for="version in versions.items" :key="version.version_id">
          <div>
            v{{ version.version }} · {{ labels[version.state] }} · {{ version.document.title }}
          </div>
          <p class="small muted">
            修订 {{ version.draft_revision }} · {{ displayTime(version.submitted_at) }}
          </p>
          <p v-if="version.review_note">{{ version.review_note }}</p>
          <p v-if="version.archived_at" class="small muted">
            归档于 {{ displayTime(version.archived_at) }}
          </p>
          <p v-if="version.disable_reason">停用原因 · {{ version.disable_reason }}</p>
          <el-button
            v-if="version.state === 'pending'"
            :disabled="busy"
            @click="changeVersion(version, 'withdraw')"
            >撤回送审</el-button
          >
          <template v-if="version.state === 'published'">
            <el-button
              v-if="archiveVersion !== version.version_id"
              :disabled="busy"
              @click="archiveVersion = version.version_id"
            >
              归档此版本
            </el-button>
            <div v-else class="archive-confirmation stack">
              <p class="small">目录会移除此版本。已有绑定的用户仍可运行。</p>
              <div>
                <el-button
                  type="warning"
                  :disabled="busy"
                  @click="changeVersion(version, 'archive')"
                >
                  确认归档
                </el-button>
                <el-button :disabled="busy" @click="archiveVersion = ''">取消</el-button>
              </div>
            </div>
          </template>
        </li>
      </ul>
      <p v-if="versions.truncated" class="small muted">显示最近 50 个版本。</p>
    </template>
  </section>
</template>

<style scoped>
.publication {
  margin-top: 1.3rem;
  padding-top: 1rem;
  border-top: 1px solid var(--line);
}
.versions {
  list-style: none;
  margin: 0;
  padding: 0;
}
.versions li {
  padding: 0.7rem 0;
  border-bottom: 1px solid var(--line);
  overflow-wrap: anywhere;
}
.versions p {
  margin: 0.35rem 0;
}
.archive-confirmation {
  padding: 0.55rem 0.75rem;
  border-radius: var(--el-border-radius-base);
  background: var(--el-color-warning-light-9);
}
</style>
