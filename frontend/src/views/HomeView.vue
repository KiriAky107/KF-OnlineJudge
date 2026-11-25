<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { notices, rankings, problems, problemTags } from '../data/preview'
import { api } from '../api/client'
import type { components } from '../api/generated/schema'
import { displayTime } from '../lib/presentation'
import PanelCard from '../components/PanelCard.vue'
import ProblemTable from '../components/ProblemTable.vue'
import SubmissionChart from '../components/SubmissionChart.vue'
import TagSphere from '../components/TagSphere.vue'
import UiIcon from '../components/UiIcon.vue'
const router = useRouter()
const upcoming = ref<components['schemas']['ContestSchedule'][]>([])
const contestRequest = new AbortController()
onMounted(async () => {
  try {
    const { data } = await api.GET('/api/v1/contests', { signal: contestRequest.signal })
    if (contestRequest.signal.aborted) return
    upcoming.value = (data ?? [])
      .filter((contest) => Date.parse(contest.ends_at) > Date.now())
      .sort((left, right) => Date.parse(left.starts_at) - Date.parse(right.starts_at))
      .slice(0, 3)
  } catch {
    // The contest list provides its own loading and error states.
  }
})
onBeforeUnmount(() => contestRequest.abort())
const noticeOpen = ref(false)
const selectedNotice = ref(notices[0]!)
function openNotice(index: number) {
  selectedNotice.value = notices[index]!
  noticeOpen.value = true
}
</script>
<template>
  <div class="practice-home">
    <section class="practice-hero">
      <div class="practice-hero-copy">
        <p class="eyebrow">KF ONLINEJUDGE / PRACTICE SPACE</p>
        <h1>把想法写成代码，<br />让思路一步步清晰。</h1>
        <p class="practice-intro">
          从一道题开始，沿着自己的节奏练习。<br />在这里写代码、做训练，也和同伴一起迎接新的挑战。
        </p>
        <div class="practice-actions">
          <RouterLink to="/problems"
            ><el-button type="primary" size="large"
              >开始解题 <UiIcon name="arrow" /></el-button></RouterLink
          ><RouterLink to="/trainings" class="practice-secondary"
            >浏览训练题单 <UiIcon name="arrow"
          /></RouterLink>
        </div>
        <div class="practice-hero-meta">
          <span
            ><strong>{{ problems.length }}</strong> 道示例题目</span
          ><span
            ><strong>{{ problemTags.length }}</strong> 个算法标签</span
          ><span>ACM / OI</span>
        </div>
      </div>
      <div class="practice-hero-art">
        <img
          src="/images/practice-space.svg"
          alt="蓝色路径连接代码、算法节点与完成标记的编程空间插画"
        />
      </div>
    </section>
    <section class="practice-announcements" aria-label="站内公告">
      <div class="announcements-label">
        <UiIcon name="bell" /><strong>站内公告</strong><span>示例内容</span>
      </div>
      <button v-for="(notice, index) in notices" :key="notice.id" @click="openNotice(index)">
        <time>{{ notice.date.slice(5) }}</time
        ><span>{{ notice.title }}</span
        ><UiIcon name="arrow" />
      </button>
    </section>
    <div class="practice-activity">
      <PanelCard class="activity-panel" title="练习的每一步" subtitle="最近一周 · 示例统计">
        <template #actions
          ><RouterLink to="/submissions">查看评测 <UiIcon name="arrow" /></RouterLink
        ></template>
        <div class="activity-totals">
          <div>
            <span>累计提交</span><strong>544<small>次</small></strong>
          </div>
          <div>
            <span>通过提交</span><strong>342<small>次</small></strong>
          </div>
          <div>
            <span>统计周期</span
            ><strong class="activity-period">09.28 <small>—</small> 10.04</strong>
          </div>
        </div>
        <SubmissionChart />
      </PanelCard>
      <PanelCard
        id="tags"
        class="explore-panel"
        title="算法探索"
        :subtitle="problemTags.length + ' 个标签'"
      >
        <TagSphere
          :tags="problemTags"
          @select="(tag) => router.push({ path: '/problems', query: { tag } })"
        />
      </PanelCard>
    </div>
    <div class="practice-library">
      <PanelCard title="从这里开始解题" subtitle="最新题目">
        <template #actions
          ><RouterLink to="/problems">完整题库 <UiIcon name="arrow" /></RouterLink
        ></template>
        <ProblemTable :items="problems.slice(0, 6)" compact />
        <div class="practice-library-note">
          <span>换一个角度，试试新的解法。</span
          ><RouterLink to="/trainings">按专题练习 <UiIcon name="arrow" /></RouterLink>
        </div>
      </PanelCard>
      <PanelCard class="practice-rank" title="一起向前" subtitle="过题榜 · 示例排名">
        <template #actions
          ><RouterLink to="/rankings">全部 <UiIcon name="arrow" /></RouterLink
        ></template>
        <RouterLink
          v-for="(user, index) in rankings.slice(0, 5)"
          :key="user.username"
          :to="'/users/' + user.username"
          class="practice-rank-row"
          ><span class="rank-index">{{ String(index + 1).padStart(2, '0') }}</span
          ><span class="avatar" :style="{ background: user.color }">{{ user.username[0] }}</span>
          <div>
            <strong>{{ user.username }}</strong>
            <p>{{ user.motto }}</p>
          </div>
          <span class="rank-solved"
            ><strong>{{ user.solved }}</strong
            ><small>过题</small></span
          ></RouterLink
        >
      </PanelCard>
    </div>
    <section class="practice-contests">
      <div class="section-title">
        <div>
          <p class="eyebrow">NEXT CHALLENGE</p>
          <h2>给下一场挑战留点时间。</h2>
        </div>
        <RouterLink to="/contests">比赛日历 <UiIcon name="arrow" /></RouterLink>
      </div>
      <div class="practice-contest-grid">
        <RouterLink
          v-for="contest in upcoming"
          :key="contest.key"
          :to="'/contests/' + contest.key"
          class="practice-contest-card"
          ><span class="tag">{{
            Date.parse(contest.starts_at) <= Date.now() ? '进行中' : '即将开始'
          }}</span>
          <h3>{{ contest.title }}</h3>
          <p><UiIcon name="clock" />{{ displayTime(contest.starts_at) }}</p>
          <span class="contest-enter">查看比赛 <UiIcon name="arrow" /></span
        ></RouterLink>
      </div>
    </section>
    <div class="practice-links">
      <span>练习之外，也可以一起聊聊。</span
      ><RouterLink to="/discussions">讨论交流 <UiIcon name="arrow" /></RouterLink
      ><RouterLink to="/teams/create">创建团队 <UiIcon name="arrow" /></RouterLink
      ><RouterLink to="/about">关于 KF OJ <UiIcon name="arrow" /></RouterLink>
    </div>
  </div>
  <el-dialog v-model="noticeOpen" :title="selectedNotice.title" width="min(600px,92vw)"
    ><p class="small muted">{{ selectedNotice.date }} · {{ selectedNotice.category }} · 示例公告</p>
    <p style="margin-top: 20px; line-height: 1.9">{{ selectedNotice.text }}</p></el-dialog
  >
</template>
<style scoped>
.practice-home {
  display: grid;
  gap: 28px;
}
.practice-hero {
  display: grid;
  grid-template-columns: 1.2fr 1fr;
  min-height: 360px;
  border-radius: 20px;
  background: linear-gradient(110deg, var(--surface) 36%, var(--accent-bg));
  overflow: hidden;
  border: 1px solid var(--line);
}
.practice-hero-copy {
  padding: 42px 44px;
}
.practice-hero h1 {
  font-size: clamp(28px, 3vw, 42px);
  line-height: 1.45;
  letter-spacing: -1.5px;
  color: var(--strong-ink);
  margin: 12px 0 18px;
}
.practice-intro {
  font-size: 14px;
  color: #7d8fa6;
}
.practice-actions {
  display: flex;
  align-items: center;
  gap: 24px;
  flex-wrap: wrap;
  margin: 26px 0;
}
.practice-secondary {
  display: flex;
  gap: 10px;
  align-items: center;
  font-size: 13px;
  color: var(--secondary-ink);
}
.practice-hero-meta {
  display: flex;
  gap: 22px;
  flex-wrap: wrap;
  font-size: 11px;
  color: #8b9db3;
}
.practice-hero-meta strong {
  font:
    600 16px ui-monospace,
    monospace;
  color: #566f92;
}
.practice-hero-art {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px 28px 20px 0;
}
.practice-hero-art img {
  display: block;
  width: 100%;
  max-width: 460px;
}
.practice-announcements {
  display: grid;
  grid-template-columns: 135px repeat(3, minmax(0, 1fr));
  gap: 18px;
  padding: 8px 0;
  align-items: center;
}
.announcements-label {
  display: grid;
  grid-template-columns: 18px 1fr;
  gap: 5px 8px;
  font-size: 13px;
  color: var(--secondary-ink);
}
.announcements-label > span {
  grid-column: 2;
  font-size: 10px;
  color: var(--muted);
}
.practice-announcements button {
  border: 0;
  background: none;
  text-align: left;
  display: grid;
  grid-template-columns: 1fr 18px;
  gap: 5px;
  padding: 8px 18px;
  border-left: 1px solid var(--line);
  min-width: 0;
  color: var(--secondary-ink);
}
.practice-announcements time {
  grid-column: 1/-1;
  font:
    11px ui-monospace,
    monospace;
  color: #96a5b8;
}
.practice-announcements button > span {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  font-size: 12px;
}
.practice-announcements button:hover {
  color: var(--blue);
}
.practice-activity,
.practice-library {
  display: grid;
  grid-template-columns: minmax(0, 1.65fr) minmax(320px, 1fr);
  gap: 24px;
}
.activity-totals {
  display: flex;
  justify-content: space-between;
  gap: 18px;
  margin: 8px 0 22px;
}
.activity-totals > div {
  display: grid;
  gap: 8px;
}
.activity-totals span {
  color: var(--muted);
  font-size: 11px;
}
.activity-totals strong {
  color: var(--secondary-ink);
  font:
    600 30px ui-monospace,
    monospace;
}
.activity-totals small {
  font:
    11px 'Microsoft YaHei',
    sans-serif;
  color: #9aacbf;
  margin-left: 7px;
}
.activity-totals strong.activity-period {
  font-size: 17px;
  margin-top: 10px;
}
.activity-panel :deep(.submission-chart) {
  padding-top: 6px;
}
.practice-library-note {
  display: flex;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 10px;
  padding-top: 18px;
  font-size: 11px;
  color: var(--muted);
}
.practice-library-note a {
  color: var(--blue);
}
.practice-rank-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 16px 0;
  border-bottom: 1px solid var(--line);
}
.practice-rank-row:last-child {
  border-bottom: 0;
}
.rank-index {
  font:
    11px ui-monospace,
    monospace;
  color: #a6b4c6;
}
.practice-rank-row .avatar {
  width: 34px;
  height: 34px;
  border-radius: 10px;
}
.practice-rank-row > div {
  min-width: 0;
}
.practice-rank-row strong {
  font-size: 13px;
  font-weight: 600;
}
.practice-rank-row p {
  font-size: 10px;
  color: var(--muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.rank-solved {
  margin-left: auto;
  display: grid;
  text-align: right;
  gap: 2px;
}
.rank-solved strong {
  font:
    600 18px ui-monospace,
    monospace;
  color: #4995e5;
}
.rank-solved small {
  font-size: 9px;
  color: var(--muted);
}
.section-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  margin: 8px 0 20px;
}
.section-title h2 {
  font-size: 23px;
  font-weight: 600;
  color: #354c6e;
}
.section-title > a {
  font-size: 12px;
  color: var(--blue);
}
.practice-contest-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 20px;
}
.practice-contest-card {
  position: relative;
  display: block;
  padding: 26px;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 16px;
}
.practice-contest-card > div {
  display: flex;
  justify-content: space-between;
  font-size: 11px;
  color: #8e9eb3;
}
.practice-contest-card h3 {
  font-size: 16px;
  font-weight: 600;
  line-height: 1.6;
  margin: 22px 0 12px;
  color: var(--secondary-ink);
}
.practice-contest-card p {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: var(--muted);
}
.contest-enter {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 25px;
  font-size: 12px;
  color: var(--blue);
}
.practice-demo-note {
  margin-top: 12px;
  font-size: 10px;
  color: var(--muted);
}
.practice-links {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 24px;
  padding: 22px 0 4px;
  border-top: 1px solid var(--line);
  font-size: 12px;
}
.practice-links > span {
  margin-right: auto;
  color: var(--muted);
}
.practice-links > a {
  color: var(--secondary-ink);
}
@media (max-width: 960px) {
  .practice-hero-copy {
    padding: 32px;
  }
  .practice-hero-art {
    padding-right: 10px;
  }
  .practice-activity,
  .practice-library {
    gap: 18px;
    grid-template-columns: minmax(0, 1.4fr) minmax(280px, 1fr);
  }
  .practice-announcements {
    grid-template-columns: 110px repeat(3, minmax(0, 1fr));
    gap: 6px;
  }
  .practice-announcements button {
    padding: 8px 12px;
  }
  .activity-totals strong {
    font-size: 25px;
  }
  .activity-period {
    display: none;
  }
  .practice-contest-card {
    padding: 20px;
  }
}
@media (max-width: 760px) {
  .practice-home {
    gap: 22px;
  }
  .practice-hero {
    grid-template-columns: 1fr;
  }
  .practice-hero-copy {
    padding: 30px 26px 18px;
  }
  .practice-hero-art {
    padding: 0 28px 10px;
    height: 190px;
    overflow: hidden;
  }
  .practice-hero-art img {
    max-width: 330px;
  }
  .practice-announcements {
    grid-template-columns: 1fr;
    gap: 2px;
  }
  .announcements-label {
    display: flex;
    gap: 8px;
    margin: 0 0 5px;
  }
  .practice-announcements button {
    grid-template-columns: 46px 1fr 18px;
    border: 0;
    padding: 10px 0;
  }
  .practice-announcements time {
    grid-column: auto;
  }
  .practice-activity,
  .practice-library,
  .practice-contest-grid {
    grid-template-columns: 1fr;
  }
  .section-title h2 {
    font-size: 20px;
  }
  .practice-links {
    gap: 18px;
  }
  .practice-links > span {
    width: 100%;
  }
  .practice-contest-card h3 {
    margin-top: 16px;
  }
  .practice-hero h1 {
    font-size: 28px;
  }
  .activity-totals > div:last-child {
    display: none;
  }
  .activity-totals {
    justify-content: flex-start;
    gap: 50px;
  }
  .practice-actions {
    gap: 18px;
  }
}
</style>
