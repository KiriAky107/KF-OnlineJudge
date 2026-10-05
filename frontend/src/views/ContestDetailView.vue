<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'
import { contests, problems } from '../data/preview'
import PageHeading from '../components/PageHeading.vue'
import PanelCard from '../components/PanelCard.vue'
import ProblemTable from '../components/ProblemTable.vue'
import EmptyState from '../components/EmptyState.vue'
const route = useRoute()
const tab = ref('overview')
const contest = computed(() => contests.find((c) => c.id === route.params.id))
const items = computed(() => problems.filter((p) => contest.value?.ids.includes(p.id)))
</script>
<template>
  <template v-if="contest"
    ><div class="breadcrumbs">
      <RouterLink to="/contests">比赛</RouterLink><span>/</span><span>{{ contest.title }}</span>
    </div>
    <PageHeading :title="contest.title" :description="contest.description"
      ><el-tag effect="light">{{ contest.status }} · 示例</el-tag></PageHeading
    >
    <div class="content-with-aside">
      <PanelCard title="比赛信息" icon="contests"
        ><el-tabs v-model="tab"
          ><el-tab-pane label="比赛说明" name="overview"
            ><div class="detail-prose">
              <h2>比赛介绍</h2>
              <p>{{ contest.description }}</p>
              <h2>比赛规则</h2>
              <p>
                {{
                  contest.type === 'ACM'
                    ? 'ACM 赛制按照通过题目数排序，通过数相同时比较罚时。'
                    : 'OI 赛制按照题目得分总和排序。具体规则以实际比赛公告为准。'
                }}
              </p>
              <h2>参赛须知</h2>
              <p>
                认真阅读每道题的数据范围和限制。赛程、题目和状态当前为界面示例，报名功能将在比赛服务接入后开放。
              </p>
            </div></el-tab-pane
          ><el-tab-pane label="比赛题目" name="problems"
            ><ProblemTable :items="items" compact /></el-tab-pane
          ><el-tab-pane label="比赛榜单" name="rank"
            ><EmptyState
              title="暂无实际比赛成绩"
              description="比赛服务接入后展示实时榜单。" /></el-tab-pane
          ><el-tab-pane label="提交记录" name="submissions"
            ><EmptyState title="暂无实际提交记录" /></el-tab-pane></el-tabs></PanelCard
      ><PanelCard title="比赛概况" icon="clock"
        ><dl class="details-list">
          <div>
            <dt>赛制</dt>
            <dd>{{ contest.type }}</dd>
          </div>
          <div>
            <dt>开始时间</dt>
            <dd>{{ contest.start }}</dd>
          </div>
          <div>
            <dt>比赛时长</dt>
            <dd>{{ contest.duration }}</dd>
          </div>
          <div>
            <dt>题目数量</dt>
            <dd>{{ items.length }} 道</dd>
          </div>
        </dl>
        <el-button type="primary" disabled style="width: 100%; margin-top: 25px"
          >报名功能待接入</el-button
        ></PanelCard
      >
    </div></template
  ><EmptyState v-else title="比赛不存在"
    ><RouterLink to="/contests"><el-button>返回比赛列表</el-button></RouterLink></EmptyState
  >
</template>
