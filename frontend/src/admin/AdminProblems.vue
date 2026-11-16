<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useSession } from '../composables/useSession'
import {
  adminProblems as api,
  maintenanceError,
  type ImportDetail,
  type ManagedProblem,
  type Version,
  type AuditEvent,
} from '../api/adminProblems'
import { ApiError } from '../api/transport'
import { boundedPoll } from '../api/polling'
import { auditActionLabels, importStateLabels } from './presentation'
import { displayRuntimeVersion, displayTime } from '../lib/presentation'
import {
  importIntent,
  mutationIntent,
  loadIntent,
  saveIntent,
  hasMaintenance,
  type ImportIntent,
  type MutationIntent,
} from './intents'
const session = useSession()
const problems = ref<ManagedProblem[]>([]),
  problemAfter = ref<string | null>(null)
const selected = ref<ManagedProblem | null>(null),
  versions = ref<Version[]>([]),
  versionBefore = ref<number | null>(null)
const imports = ref<Awaited<ReturnType<typeof api.imports>>['items']>([]),
  importAfter = ref<string | null>(null),
  task = ref<ImportDetail | null>(null)
const audits = ref<AuditEvent[]>([]),
  auditBefore = ref<string | null>(null)
const file = ref<File | null>(null),
  fileName = ref(''),
  busy = ref(false),
  loading = ref(true),
  error = ref(''),
  info = ref('')
const targetReady = ref(false)
const uploadAvailable = ref(false),
  enabledFilter = ref('all'),
  publicationTarget = ref<ManagedProblem | null>(null)
let intent = loadIntent<ImportIntent>('import'),
  mutation = loadIntent<MutationIntent>('mutation')
let generation = 0,
  poll: ReturnType<typeof boundedPoll<ImportDetail>> | undefined
const controllers = new Set<AbortController>()
const candidate = computed(() => task.value?.candidate ?? null)
const permitted = computed(() => hasMaintenance(session.user.value))
function fence() {
  generation++
  for (const c of controllers) c.abort()
  controllers.clear()
  poll?.dispose()
  poll = undefined
  busy.value = false
}
async function request<T>(call: (signal: AbortSignal) => Promise<T>): Promise<T> {
  const c = new AbortController()
  controllers.add(c)
  try {
    return await call(c.signal)
  } finally {
    controllers.delete(c)
  }
}
async function run(action: () => Promise<void>) {
  if (busy.value || !permitted.value) return
  const current = generation
  busy.value = true
  error.value = ''
  info.value = ''
  try {
    await action()
  } catch (e) {
    if (current === generation) {
      error.value = maintenanceError(e)
      if (e instanceof ApiError && [401, 403].includes(e.status)) {
        fence()
        task.value = null
        selected.value = null
        await session.refresh().catch(() => {})
      }
    }
  } finally {
    if (current === generation) busy.value = false
  }
}
async function loadProblems(append = false) {
  const current = generation
  const page = await request((s) =>
    api.page(
      s,
      append ? (problemAfter.value ?? undefined) : undefined,
      enabledFilter.value === 'all' ? undefined : enabledFilter.value === 'enabled',
    ),
  )
  if (current !== generation) return
  problems.value = page.items
  problemAfter.value = page.next_after
}
async function loadImports(append = false) {
  const current = generation
  const page = await request((s) =>
    api.imports(s, append ? (importAfter.value ?? undefined) : undefined),
  )
  if (current !== generation) return
  imports.value = page.items
  importAfter.value = page.next_after
}
async function loadAudit(append = false) {
  const current = generation
  const page = await request((s) =>
    api.audit(s, append ? (auditBefore.value ?? undefined) : undefined),
  )
  if (current !== generation) return
  audits.value = page.items
  auditBefore.value = page.next_before
}
async function chooseProblem(problem: ManagedProblem) {
  fence()
  selected.value = problem
  versions.value = []
  versionBefore.value = null
  await run(async () => {
    const current = generation
    const page = await request((s) => api.versions(problem.id, s))
    if (current === generation) {
      versions.value = page.items
      versionBefore.value = page.next_before
    }
  })
}
async function moreVersions() {
  await run(async () => {
    const p = selected.value
    if (!p || !versionBefore.value) return
    const current = generation
    const page = await request((s) => api.versions(p.id, s, versionBefore.value!))
    if (current === generation) {
      versions.value = page.items
      versionBefore.value = page.next_before
    }
  })
}
async function targetFor(value: ImportDetail, current: number) {
  publicationTarget.value = null
  targetReady.value = false
  if (!value.candidate) return
  try {
    const target = await request((s) => api.bySlug(value.candidate!.slug, s))
    if (current === generation) publicationTarget.value = target
  } catch (e) {
    if (!(e instanceof ApiError && e.status === 404)) throw e
  }
  if (current === generation) targetReady.value = true
}
function startPoll(key: string) {
  poll?.dispose()
  const current = generation
  poll = boundedPoll({
    load: (signal) => api.import(key, signal),
    maxRequests: 90,
    maxTime: 180000,
    interval: 2000,
    terminal: (value) =>
      ['ready', 'published', 'failed', 'expired', 'discarded'].includes(value.state),
    result: (value) => {
      if (current !== generation) return
      task.value = value
      if (value.candidate)
        void targetFor(value, current).catch((e) => {
          if (current === generation) error.value = maintenanceError(e)
        })
    },
    error: (e) => {
      if (current === generation) error.value = maintenanceError(e)
    },
    exhausted: () => {
      if (current === generation)
        info.value = '自动查询已停止。可手动核对原操作；不会重复创建导入。'
    },
  })
  poll.visible(!document.hidden)
  poll.start()
}
async function chooseTask(key: string) {
  fence()
  task.value = null
  publicationTarget.value = null
  await run(async () => {
    const current = generation
    const value = await request((s) => api.import(key, s))
    if (current !== generation) return
    task.value = value
    await targetFor(value, current)
    if (!['ready', 'published', 'failed', 'expired', 'discarded'].includes(value.state))
      startPoll(key)
  })
}
async function chooseFile(event: Event) {
  const picked = (event.target as HTMLInputElement).files?.[0] ?? null
  fence()
  file.value = null
  fileName.value = ''
  task.value = null
  publicationTarget.value = null
  if (!picked) return
  await run(async () => {
    const current = generation
    if (picked.size < 1 || picked.size > 67108864) throw new Error('ZIP 题包限 1 字节至 64 MiB。')
    const digest = Array.from(
      new Uint8Array(await crypto.subtle.digest('SHA-256', await picked.arrayBuffer())),
      (b) => b.toString(16).padStart(2, '0'),
    ).join('')
    if (current !== generation) return
    intent = importIntent(intent, session.user.value!.id, digest, picked.size)
    saveIntent('import', intent)
    file.value = picked
    fileName.value = picked.name
    info.value = '文件已核对。重试同一文件会沿用原导入记录。'
  })
}
async function upload() {
  await run(async () => {
    if (!file.value || !intent) return
    const current = generation,
      picked = file.value,
      receipt = intent
    const created = await request((s) =>
      api.create(
        {
          schema_version: 1,
          operation_id: receipt.operation_id,
          archive_digest: receipt.archive_digest,
          archive_bytes: receipt.archive_bytes,
        },
        s,
      ),
    )
    if (current !== generation) return
    if (created.state === 'uploading')
      await request((s) => api.upload(receipt.operation_id, picked, s))
    if (current !== generation) return
    const value = await request((s) => api.import(receipt.operation_id, s))
    if (current !== generation) return
    task.value = value
    await targetFor(value, current)
    await loadImports()
    startPoll(receipt.operation_id)
  })
}
async function discard() {
  await run(async () => {
    const value = task.value
    if (!value) return
    const current = generation
    await request((s) => api.discard(value.operation_id, s))
    if (current !== generation) return
    task.value = await request((s) => api.import(value.operation_id, s))
    publicationTarget.value = null
    await loadAudit()
    info.value = '候选已按服务端结果丢弃。'
  })
}
async function publish() {
  await run(async () => {
    const value = task.value
    if (!value?.candidate || value.state !== 'ready' || !targetReady.value) return
    const current = generation
    const body = {
      schema_version: 1 as const,
      import_operation_id: value.operation_id,
      candidate_id: value.candidate_id!,
      expected_active_version_id: publicationTarget.value?.active_version_id ?? null,
    }
    mutation = mutationIntent(
      mutation,
      session.user.value!.id,
      JSON.stringify({ action: 'publish', ...body }),
    )
    saveIntent('mutation', mutation)
    try {
      const result = await request((s) =>
        api.publish({ ...body, operation_id: mutation!.operation_id }, s),
      )
      if (current !== generation) return
      info.value = `服务端已发布 revision ${result.problem.revision}。`
      publicationTarget.value = result.problem
      task.value = await request((s) => api.import(value.operation_id, s))
      await loadProblems()
      await loadAudit()
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        await targetFor(value, current)
        await loadProblems()
      }
      throw e
    }
  })
}
async function availability() {
  await run(async () => {
    const value = selected.value
    if (!value?.active_version_id) return
    const current = generation
    const enabled = !value.enabled
    const body = {
      schema_version: 1 as const,
      expected_active_version_id: value.active_version_id,
      expected_enabled: value.enabled,
    }
    mutation = mutationIntent(
      mutation,
      session.user.value!.id,
      JSON.stringify({ action: enabled ? 'restore' : 'disable', problem: value.id, ...body }),
    )
    saveIntent('mutation', mutation)
    try {
      const result = await request((s) =>
        api.availability(value.id, enabled, { ...body, operation_id: mutation!.operation_id }, s),
      )
      if (current !== generation) return
      selected.value = result.problem
      info.value = enabled ? '服务端已恢复新提交。' : '服务端已下架；已受理任务与本人历史保留。'
      await loadProblems()
      await loadAudit()
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        const latest = await request((s) => api.detail(value.id, s))
        if (current === generation) selected.value = latest
        await loadProblems()
      }
      throw e
    }
  })
}
async function verifyMutation() {
  await run(async () => {
    if (!mutation || mutation.owner !== session.user.value?.id) return
    const current = generation
    const receipt = await request((s) => api.receipt(mutation!.operation_id, s))
    if (current !== generation) return
    info.value = `服务器已确认维护操作：${receipt.problem.slug} · 版本 ${receipt.problem.revision}。`
    await loadProblems()
    await loadAudit()
  })
}
function restartImport() {
  if (
    busy.value ||
    !file.value ||
    !intent ||
    !task.value ||
    !['failed', 'discarded', 'expired'].includes(task.value.state)
  )
    return
  intent = importIntent(null, session.user.value!.id, intent.archive_digest, intent.archive_bytes)
  saveIntent('import', intent)
  task.value = null
  publicationTarget.value = null
  targetReady.value = false
  info.value = '上次操作已终止；已创建新的本地导入凭据，请重新上传。'
}
function visibility() {
  poll?.visible(!document.hidden)
}
watch(
  () => session.user.value?.capabilities?.authorization_version,
  () => {
    fence()
    task.value = null
    selected.value = null
    file.value = null
    audits.value = []
    problems.value = []
  },
)
onMounted(() => {
  document.addEventListener('visibilitychange', visibility)
  void run(async () => {
    const current = generation
    const caps = await request((s) => api.capabilities(s))
    if (current !== generation) return
    uploadAvailable.value = caps.uploads_available && caps.validation_available
    await loadProblems()
    await loadImports()
    await loadAudit()
    if (intent && intent.owner === session.user.value?.id)
      info.value = '已保留上次导入记录；可重新选择原文件或从任务列表核对。'
  }).finally(() => (loading.value = false))
})
onUnmounted(() => {
  fence()
  document.removeEventListener('visibilitychange', visibility)
})
function sampleText(encoded: string) {
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(
      Uint8Array.from(atob(encoded), (x) => x.charCodeAt(0)),
    )
  } catch {
    return '公开样例为二进制数据。'
  }
}
</script>
<template>
  <div class="admin-management" data-testid="admin-problems">
    <header class="admin-page-heading">
      <p class="eyebrow">PROBLEM MANAGEMENT</p>
      <h1>题目维护</h1>
      <p>发布生成不可变新版本；下架停止公开读取和新提交，已受理任务继续使用原版本。</p>
    </header>
    <p v-if="loading" class="admin-feedback" role="status">正在读取维护服务…</p>
    <p v-if="error" class="admin-feedback" role="alert">{{ error }}</p>
    <p v-if="info" class="admin-feedback" role="status">{{ info }}</p>
    <section class="admin-real-panel" aria-labelledby="managed-problems-title">
      <h2 id="managed-problems-title">已发布题目</h2>
      <div class="admin-real-toolbar">
        <label
          >状态
          <select v-model="enabledFilter" :disabled="busy" @change="run(() => loadProblems())">
            <option value="all">全部</option>
            <option value="enabled">已上架</option>
            <option value="disabled">已下架</option>
          </select></label
        ><button :disabled="busy" @click="run(() => loadProblems())">刷新题目</button
        ><button :disabled="busy" @click="verifyMutation">核对最近维护回执</button>
      </div>
      <div class="admin-table-scroll" role="region" aria-label="已发布题目列表" tabindex="0">
        <table>
          <thead>
            <tr>
              <th>题目</th>
              <th>当前版本</th>
              <th>状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="p in problems" :key="p.id" :data-problem-slug="p.slug">
              <td>
                <strong>{{ p.title ?? p.slug }}</strong
                ><small>{{ p.slug }}</small>
              </td>
              <td>版本 {{ p.revision }}</td>
              <td>
                <span class="admin-status" :class="{ 'is-positive': p.enabled }">{{
                  p.enabled ? '已上架' : '已下架'
                }}</span>
              </td>
              <td><button :disabled="busy" @click="chooseProblem(p)">版本与状态</button></td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-if="!loading && !problems.length" class="admin-empty">没有符合条件的题目。</p>
      <button v-if="problemAfter" :disabled="busy" @click="run(() => loadProblems(true))">
        下一页题目
      </button>
    </section>
    <section v-if="selected" class="admin-real-panel">
      <h2>{{ selected.slug }} 的版本历史</h2>
      <p class="admin-muted">当前版本 {{ selected.revision }}</p>
      <button
        :class="selected.enabled ? 'admin-danger' : 'admin-primary'"
        :disabled="busy"
        @click="availability"
      >
        {{ selected.enabled ? '下架题目' : '恢复题目' }}
      </button>
      <ul class="admin-version-list">
        <li v-for="v in versions" :key="v.version_id" :data-version-id="v.version_id">
          <div class="admin-version-heading">
            <span class="admin-status">版本 {{ v.revision }}</span
            ><strong>{{ v.title }}</strong>
          </div>
          <p>{{ v.languages.join(' / ') }} · {{ v.case_count }} 个测试</p>
          <time :datetime="v.published_at">{{ displayTime(v.published_at) }}</time>
        </li>
      </ul>
      <button v-if="versionBefore" :disabled="busy" @click="moreVersions">下一页历史版本</button>
    </section>
    <section class="admin-real-panel">
      <h2>导入新候选</h2>
      <p>单层 ZIP，problem.json 清单；最大 64 MiB；不运行上传内容。修改题目请上传新包。</p>
      <p v-if="!uploadAvailable">上传或校验服务尚未就绪。可查询已有任务。</p>
      <input
        class="admin-file-input"
        aria-label="选择 ZIP 题包"
        type="file"
        accept=".zip,application/zip"
        :disabled="busy || !uploadAvailable"
        @change="chooseFile"
      />
      <p v-if="fileName" class="admin-muted">已选择：{{ fileName }}</p>
      <div class="admin-actions">
        <button class="admin-primary" :disabled="busy || !file || !uploadAvailable" @click="upload">
          上传并校验</button
        ><button :disabled="busy" @click="run(() => loadImports())">刷新我的导入</button>
      </div>
      <ul class="admin-import-list">
        <li v-for="item in imports" :key="item.operation_id">
          <button
            :data-operation-id="item.operation_id"
            :aria-pressed="task?.operation_id === item.operation_id"
            :disabled="busy"
            @click="chooseTask(item.operation_id)"
          >
            <span class="admin-status">{{ importStateLabels[item.state] }}</span>
            <time :datetime="item.created_at">{{ displayTime(item.created_at) }}</time>
            <span>{{ (item.archive_bytes / 1048576).toFixed(2) }} MiB</span>
          </button>
        </li>
      </ul>
      <p v-if="!loading && !imports.length" class="admin-empty">暂无导入记录。</p>
      <button v-if="importAfter" :disabled="busy" @click="run(() => loadImports(true))">
        下一页导入
      </button>
    </section>
    <section v-if="task" class="admin-real-panel">
      <h2>导入状态：{{ importStateLabels[task.state] }}</h2>
      <p class="admin-muted">
        创建于 {{ displayTime(task.created_at) }} · 到期 {{ displayTime(task.expires_at) }}
      </p>
      <p v-if="task.failure_code" role="alert">校验失败：{{ task.failure_code }}</p>
      <div class="admin-actions">
        <button :disabled="busy" @click="chooseTask(task.operation_id)">核对导入状态</button
        ><button
          v-if="['ready', 'failed', 'uploading', 'uploaded'].includes(task.state)"
          class="admin-danger"
          :disabled="busy"
          @click="discard"
        >
          丢弃候选
        </button>
        <button
          v-if="file && ['failed', 'discarded', 'expired'].includes(task.state)"
          :disabled="busy"
          @click="restartImport"
        >
          用新凭据重新导入
        </button>
      </div>
      <article v-if="candidate" class="admin-candidate">
        <h3>{{ candidate.title }} / {{ candidate.slug }}</h3>
        <pre>{{ candidate.statement }}</pre>
        <p>{{ candidate.languages.join(' / ') }} · {{ candidate.case_count }} 个隐藏测试</p>
        <p>
          当前版本：{{
            publicationTarget
              ? `revision ${publicationTarget.revision} · ${publicationTarget.title}`
              : '尚未发布'
          }}
        </p>
        <ul>
          <li v-for="c in candidate.language_configurations" :key="c.language">
            {{ c.language }} / {{ displayRuntimeVersion(c.runtime_version) }} / CPU
            {{ c.run_limits.cpu_time_us }} μs / 总内存 {{ c.run_limits.memory_bytes }} B
          </li>
        </ul>
        <p v-if="candidate.legacy_budget">
          C/C++ CPU {{ candidate.legacy_budget.run_limits.cpu_time_us }} μs / 总内存
          {{ candidate.legacy_budget.run_limits.memory_bytes }} B
        </p>
        <div v-for="(s, i) in candidate.samples" :key="i">
          <h4>公开样例 {{ i + 1 }}</h4>
          <pre>{{ sampleText(s.input) }}</pre>
          <pre>{{ sampleText(s.output) }}</pre>
        </div>
        <button
          v-if="task.state === 'ready'"
          class="admin-primary"
          :disabled="
            busy || !targetReady || (publicationTarget !== null && !publicationTarget.enabled)
          "
          @click="publish"
        >
          发布新版本
        </button>
        <p v-if="task.state === 'published'" class="admin-feedback" role="status">
          此候选已发布，可在题目的版本历史中查看。
        </p>
      </article>
    </section>
    <section class="admin-real-panel" aria-labelledby="maintenance-audit-title">
      <h2 id="maintenance-audit-title">维护审计</h2>
      <button :disabled="busy" @click="run(() => loadAudit())">刷新审计</button>
      <div class="admin-table-scroll" role="region" aria-label="维护审计列表" tabindex="0">
        <table>
          <thead>
            <tr>
              <th>时间</th>
              <th>主体</th>
              <th>动作与结果</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="a in audits" :key="a.id">
              <td>
                <time :datetime="a.created_at">{{ displayTime(a.created_at) }}</time>
              </td>
              <td>
                {{ a.operator_name ?? (a.actor_kind === 'account' ? '维护账号' : '运维操作员') }}
              </td>
              <td>
                {{ auditActionLabels[a.action] }}
                <span class="admin-status is-positive">已完成</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-if="!loading && !audits.length" class="admin-empty">暂无维护审计记录。</p>
      <button v-if="auditBefore" :disabled="busy" @click="run(() => loadAudit(true))">
        下一页审计
      </button>
    </section>
  </div>
</template>
