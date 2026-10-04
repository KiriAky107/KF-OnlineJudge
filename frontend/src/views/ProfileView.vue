<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'
import { rankings, submissions, problems } from '../data/preview'
import PanelCard from '../components/PanelCard.vue'
import SubmissionTable from '../components/SubmissionTable.vue'
import ProblemTable from '../components/ProblemTable.vue'
import EmptyState from '../components/EmptyState.vue'
const route = useRoute()
const user = computed(() => rankings.find((u) => u.username === route.params.username))
const tab = ref('activity')
const records = computed(() => submissions.filter((s) => s.username === user.value?.username))
const solved = computed(() =>
  problems.filter((p) => records.value.some((s) => s.problemId === p.id && s.verdict === 'AC')),
)
const activity = Array.from({ length: 140 }, (_, index) =>
  index % 11 === 0 ? 4 : index % 7 === 0 ? 3 : index % 3 === 0 ? 2 : index % 2 === 0 ? 1 : 0,
)
</script>
<template>
  <template v-if="user"
    ><div class="profile-banner">
      <span class="avatar" :style="{ background: user.color }">{{ user.username[0] }}</span>
      <div>
        <p class="eyebrow">PERSONAL PROFILE · 示例用户</p>
        <h1>{{ user.username }}</h1>
        <p>{{ user.motto }}</p>
        <div class="profile-tags">
          <span class="tag">算法爱好者</span><span class="tag">持续练习</span>
        </div>
      </div>
      <div class="profile-actions">
        <RouterLink :to="{ path: '/submissions', query: { user: user.username } }"
          ><el-button>查看提交</el-button></RouterLink
        ><el-button disabled>关注</el-button>
      </div>
    </div>
    <div class="summary-grid">
      <div class="summary-card">
        <p>已通过题目</p>
        <strong>{{ user.solved }}</strong
        ><small>示例统计</small>
      </div>
      <div class="summary-card">
        <p>总提交数</p>
        <strong>{{ user.submissions }}</strong
        ><small>示例统计</small>
      </div>
      <div class="summary-card">
        <p>通过率</p>
        <strong>{{ Math.round((user.solved / user.submissions) * 100) }}%</strong
        ><small>示例统计</small>
      </div>
      <div class="summary-card">
        <p>竞赛积分</p>
        <strong>{{ user.rating }}</strong
        ><small>示例积分</small>
      </div>
    </div>
    <div class="content-with-aside">
      <PanelCard title="练习记录" icon="rankings"
        ><el-tabs v-model="tab"
          ><el-tab-pane label="提交活动" name="activity"
            ><p class="small muted">提交活动热力图 · 布局示例</p>
            <div class="heatmap" role="img" aria-label="示例提交活动热力图">
              <span
                v-for="(level, index) in activity"
                :key="index"
                class="heat-cell"
                :data-level="level"
              />
            </div>
            <div class="detail-meta"><span>少</span><span>← 提交活跃度 →</span><span>多</span></div>
            <SubmissionTable :items="records" compact /></el-tab-pane
          ><el-tab-pane label="已通过题目" name="problems"
            ><p class="tab-summary">以下题目来自当前示例提交记录。</p>
            <ProblemTable :items="solved" compact /></el-tab-pane></el-tabs
      ></PanelCard>
      <div class="stack">
        <PanelCard title="个人介绍" icon="about"
          ><p class="small muted">{{ user.motto }}</p>
          <dl class="details-list" style="margin-top: 20px">
            <div>
              <dt>用户名</dt>
              <dd>{{ user.username }}</dd>
            </div>
            <div>
              <dt>榜单位置</dt>
              <dd>#{{ rankings.indexOf(user) + 1 }}</dd>
            </div>
          </dl></PanelCard
        ><PanelCard title="学习分析" icon="reading"
          ><p class="small muted">
            后续结合历史代码、提交结果和题目标签，分析练习情况并生成个性化训练建议。
          </p>
          <el-button disabled style="margin-top: 18px; width: 100%"
            >分析服务待接入</el-button
          ></PanelCard
        >
      </div>
    </div></template
  ><EmptyState v-else title="用户不存在"
    ><RouterLink to="/rankings"><el-button>查看排行榜</el-button></RouterLink></EmptyState
  >
</template>
