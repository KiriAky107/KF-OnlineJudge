<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { problems, contests, rankings } from '../data/preview'
import PageHeading from '../components/PageHeading.vue'
import PanelCard from '../components/PanelCard.vue'
import SubmissionChart from '../components/SubmissionChart.vue'
import EmptyState from '../components/EmptyState.vue'
import { adminSections as sections } from './navigation'
const route = useRoute()
const section = computed(() => String(route.params.section || 'overview'))
const current = computed(() => sections.find((s) => s.id === section.value))
const query = ref('')
const problemItems = computed(() => problems.filter((p) => (p.id + p.title).includes(query.value)))
const contestItems = computed(() => contests.filter((c) => c.title.includes(query.value)))
const userItems = computed(() =>
  rankings.filter((u) => u.username.toLowerCase().includes(query.value.toLowerCase())),
)
const editOpen = ref(false)
const draft = reactive({ id: '', title: '', difficulty: '入门', status: '公开' })
const settings = reactive({
  name: 'KF OnlineJudge',
  slogan: '每一次提交，都有新的收获。',
  registration: true,
  announcement: '欢迎来到 KF OnlineJudge',
  filing: '',
})
const nodes = [
  {
    name: 'home-worker-01',
    host: 'Debian',
    capacity: '4 核 / 8 GB',
    status: '在线',
    load: 32,
    tasks: 2,
  },
  {
    name: 'cloud-worker-01',
    host: 'Ubuntu',
    capacity: '8 核 / 8 GB',
    status: '在线',
    load: 46,
    tasks: 4,
  },
  {
    name: 'cloud-worker-02',
    host: 'Ubuntu',
    capacity: '2 核 / 2 GB',
    status: '离线',
    load: 0,
    tasks: 0,
  },
]
function editProblem(id?: string) {
  const row = problems.find((problem) => problem.id === id)
  Object.assign(draft, {
    id: row?.id ?? '',
    title: row?.title ?? '',
    difficulty: row?.difficulty ?? '入门',
    status: '公开',
  })
  editOpen.value = true
}
</script>
<template>
  <PageHeading
    title="管理中心"
    eyebrow="ADMINISTRATION"
    description="管理界面预览 · 管理员身份与写入接口待接入。"
    ><a href="/"><el-button>返回站点</el-button></a></PageHeading
  >
  <div class="admin-shell">
    <div v-if="current" class="stack">
      <template v-if="section === 'overview'"
        ><div class="summary-grid">
          <div class="summary-card">
            <p>公开题目</p>
            <strong>248</strong><small>示例统计</small>
          </div>
          <div class="summary-card">
            <p>累计提交</p>
            <strong>18,240</strong><small>示例统计</small>
          </div>
          <div class="summary-card">
            <p>注册用户</p>
            <strong>328</strong><small>示例统计</small>
          </div>
          <div class="summary-card">
            <p>在线节点</p>
            <strong>2 / 3</strong><small>示例统计</small>
          </div>
        </div>
        <PanelCard title="提交趋势" icon="rankings"><SubmissionChart /></PanelCard
        ><PanelCard title="开发接入状态" icon="connection"
          ><el-descriptions :column="1" border
            ><el-descriptions-item label="当前界面">前端预览</el-descriptions-item
            ><el-descriptions-item label="账号与权限">待接入</el-descriptions-item
            ><el-descriptions-item label="评测与节点状态">待接入</el-descriptions-item
            ><el-descriptions-item label="管理写入与操作审计"
              >待接入</el-descriptions-item
            ></el-descriptions
          ></PanelCard
        ></template
      >
      <PanelCard
        v-else-if="section === 'problems'"
        title="题目管理"
        icon="problems"
        subtitle="示例题库"
        ><template #actions
          ><el-button size="small" type="primary" @click="editProblem()"
            >新建题目草稿</el-button
          ></template
        >
        <div class="filters">
          <el-input
            v-model="query"
            placeholder="搜索题号或名称"
            aria-label="搜索管理题目"
            clearable
          />
        </div>
        <el-table :data="problemItems" stripe
          ><el-table-column label="题号" prop="id" width="80" /><el-table-column
            label="名称"
            prop="title"
            min-width="190"
          /><el-table-column label="难度" prop="difficulty" width="90" /><el-table-column
            label="更新日期"
            prop="updated"
            width="130"
          /><el-table-column label="操作" width="150"
            ><template #default="{ row }"
              ><el-button link type="primary" @click="editProblem(row.id)">编辑预览</el-button
              ><a :href="'/problems/' + row.id" class="text-link" style="margin-left: 10px"
                >查看</a
              ></template
            ></el-table-column
          ></el-table
        ></PanelCard
      >
      <PanelCard
        v-else-if="section === 'contests'"
        title="比赛管理"
        icon="contests"
        subtitle="示例赛程"
        ><template #actions
          ><el-button size="small" type="primary" disabled>创建比赛</el-button></template
        >
        <div class="filters">
          <el-input v-model="query" placeholder="搜索比赛" aria-label="搜索管理比赛" clearable />
        </div>
        <el-table :data="contestItems" stripe
          ><el-table-column label="比赛名称" prop="title" min-width="220" /><el-table-column
            label="赛制"
            prop="type"
            width="75"
          /><el-table-column label="状态" prop="status" width="120" /><el-table-column
            label="开始时间"
            prop="start"
            min-width="165"
          /><el-table-column label="操作" width="90"
            ><template #default="{ row }"
              ><a class="text-link" :href="'/contests/' + row.id">查看</a></template
            ></el-table-column
          ></el-table
        ></PanelCard
      >
      <PanelCard v-else-if="section === 'users'" title="用户管理" icon="teams" subtitle="示例用户"
        ><div class="filters">
          <el-input v-model="query" placeholder="搜索用户名" aria-label="搜索管理用户" clearable />
        </div>
        <el-table :data="userItems" stripe
          ><el-table-column label="用户名" prop="username" min-width="170" /><el-table-column
            label="通过题目"
            prop="solved"
            width="100"
          /><el-table-column label="竞赛积分" prop="rating" width="110" /><el-table-column
            label="角色"
            width="90"
            ><template #default>用户</template></el-table-column
          ><el-table-column label="操作" width="160"
            ><template #default="{ row }"
              ><a class="text-link" :href="'/users/' + row.username">查看主页</a
              ><el-button link disabled style="margin-left: 12px">修改角色</el-button></template
            ></el-table-column
          ></el-table
        ></PanelCard
      >
      <PanelCard
        v-else-if="section === 'judges'"
        title="判题节点"
        icon="connection"
        subtitle="示例监控数据"
        ><p class="notice-strip">实际节点、负载、心跳和任务状态将在协调器接口接入后展示。</p>
        <el-table :data="nodes" stripe
          ><el-table-column label="节点" prop="name" min-width="170" /><el-table-column
            label="系统"
            prop="host"
            width="100"
          /><el-table-column label="资源" prop="capacity" min-width="135" /><el-table-column
            label="状态"
            width="90"
            ><template #default="{ row }"
              ><span class="status-dot" :class="{ offline: row.status === '离线' }" />{{
                row.status
              }}</template
            ></el-table-column
          ><el-table-column label="负载" min-width="140"
            ><template #default="{ row }"
              ><el-progress :percentage="row.load" :stroke-width="5" /></template></el-table-column
          ><el-table-column label="任务" prop="tasks" width="70" /><el-table-column
            label="操作"
            width="100"
            ><template #default
              ><el-button link disabled>暂停接单</el-button></template
            ></el-table-column
          ></el-table
        >
        <div class="comment-placeholder">
          队列积压、租约过期、重试记录与故障恢复操作待接入。
        </div></PanelCard
      >
      <PanelCard v-else-if="section === 'settings'" title="站点设置" icon="settings"
        ><el-form label-position="top" style="max-width: 700px" @submit.prevent
          ><el-form-item label="站点名称"><el-input v-model="settings.name" /></el-form-item
          ><el-form-item label="站点标语"><el-input v-model="settings.slogan" /></el-form-item
          ><el-form-item label="首页公告"
            ><el-input v-model="settings.announcement" type="textarea" :rows="3" /></el-form-item
          ><el-form-item label="备案信息"
            ><el-input
              v-model="settings.filing"
              placeholder="上线前填写真实备案信息" /></el-form-item
          ><el-form-item label="允许注册"
            ><el-switch v-model="settings.registration"
          /></el-form-item>
          <div class="form-actions">
            <el-button type="primary" disabled>保存设置</el-button
            ><span class="muted">当前编辑只在本页面预览</span>
          </div></el-form
        ></PanelCard
      >
    </div>
    <EmptyState v-else title="管理页面不存在" />
  </div>
  <el-dialog
    v-model="editOpen"
    :title="draft.id ? '编辑题目 · ' + draft.id : '新建题目草稿'"
    width="min(600px, 92vw)"
    ><el-form label-position="top"
      ><el-form-item label="题目名称"
        ><el-input v-model="draft.title" placeholder="填写题目名称" /></el-form-item
      ><el-form-item label="难度"
        ><el-select v-model="draft.difficulty"
          ><el-option
            v-for="d in ['入门', '简单', '中等', '困难']"
            :key="d"
            :label="d"
            :value="d" /></el-select></el-form-item
      ><el-form-item label="发布状态"
        ><el-radio-group v-model="draft.status"
          ><el-radio value="公开">公开</el-radio
          ><el-radio value="草稿">草稿</el-radio></el-radio-group
        ></el-form-item
      >
      <p class="small muted">题面编辑、测试数据和版本发布将在题库接口接入后启用。</p></el-form
    ><template #footer
      ><el-button @click="editOpen = false">关闭预览</el-button
      ><el-button type="primary" disabled>保存题目</el-button></template
    ></el-dialog
  >
</template>
