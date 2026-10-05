<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { trainings, problems } from '../data/preview'
import PanelCard from '../components/PanelCard.vue'
import PageHeading from '../components/PageHeading.vue'
import ProblemTable from '../components/ProblemTable.vue'
import EmptyState from '../components/EmptyState.vue'
const route = useRoute()
const training = computed(() => trainings.find((t) => t.id === route.params.id))
const items = computed(() => problems.filter((p) => training.value?.ids.includes(p.id)))
</script>
<template>
  <template v-if="training"
    ><div class="breadcrumbs">
      <RouterLink to="/trainings">训练</RouterLink><span>/</span
      ><span>{{ training.category }}</span>
    </div>
    <PageHeading :title="training.title" :description="training.description"
      ><RouterLink v-if="items[0]" :to="'/problems/' + items[0].id"
        ><el-button type="primary">从第一题开始</el-button></RouterLink
      ></PageHeading
    >
    <div class="content-with-aside">
      <PanelCard title="训练题目" icon="problems" subtitle="按顺序练习"
        ><ProblemTable :items="items" compact
      /></PanelCard>
      <div class="stack">
        <PanelCard title="题单信息" icon="trainings"
          ><dl class="details-list">
            <div>
              <dt>分类</dt>
              <dd>{{ training.category }}</dd>
            </div>
            <div>
              <dt>难度</dt>
              <dd>{{ training.level }}</dd>
            </div>
            <div>
              <dt>题目数量</dt>
              <dd>{{ items.length }} 道</dd>
            </div>
            <div>
              <dt>训练记录</dt>
              <dd>账号服务接入后开启</dd>
            </div>
          </dl></PanelCard
        ><PanelCard title="练习建议" icon="reading"
          ><p class="small muted">
            先写出自己的解题思路，检查数据范围和边界情况。完成后回顾代码，记录遇到的问题。
          </p>
          <RouterLink to="/discussions" class="text-link">去讨论区交流 →</RouterLink></PanelCard
        >
      </div>
    </div></template
  ><EmptyState v-else title="题单不存在"
    ><RouterLink to="/trainings"><el-button>返回训练列表</el-button></RouterLink></EmptyState
  >
</template>
