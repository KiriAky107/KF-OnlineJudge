<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { submissions, findProblem } from '../data/preview'
import PageHeading from '../components/PageHeading.vue'
import PanelCard from '../components/PanelCard.vue'
import VerdictTag from '../components/VerdictTag.vue'
import EmptyState from '../components/EmptyState.vue'
const route = useRoute()
const submission = computed(() => submissions.find((s) => s.id === route.params.id))
</script>
<template>
  <template v-if="submission"
    ><div class="breadcrumbs">
      <RouterLink to="/submissions">评测记录</RouterLink><span>/</span
      ><span>#{{ submission.id }}</span>
    </div>
    <PageHeading
      :title="'提交 #' + submission.id"
      :description="findProblem(submission.problemId)?.title"
      ><VerdictTag :value="submission.verdict" full
    /></PageHeading>
    <div class="summary-grid">
      <div class="summary-card">
        <p>运行时间</p>
        <strong
          >{{ submission.time ?? '—' }} <small v-if="submission.time !== null">ms</small></strong
        ><small>示例数据</small>
      </div>
      <div class="summary-card">
        <p>内存使用</p>
        <strong
          >{{ submission.memory === null ? '—' : submission.memory / 1024 }}
          <small v-if="submission.memory !== null">MiB</small></strong
        ><small>示例数据</small>
      </div>
      <div class="summary-card">
        <p>语言</p>
        <strong>{{ submission.language }}</strong
        ><small>工具链版本待接入</small>
      </div>
      <div class="summary-card">
        <p>提交用户</p>
        <strong style="font-size: 21px"
          ><RouterLink :to="'/users/' + submission.username">{{
            submission.username
          }}</RouterLink></strong
        ><small>{{ submission.created }}</small>
      </div>
    </div>
    <PanelCard title="评测详情" icon="submissions"
      ><p class="notice-strip">当前展示评测界面的示例记录，实际用例结果、日志和源码尚未接入。</p>
      <EmptyState
        title="用例详情待接入"
        description="评测完成后展示各测试点状态、资源用量及可见的编译信息。"
      /><RouterLink :to="'/problems/' + submission.problemId"
        ><el-button>返回题目</el-button></RouterLink
      ></PanelCard
    ></template
  ><EmptyState v-else title="提交记录不存在"
    ><RouterLink to="/submissions"><el-button>返回评测列表</el-button></RouterLink></EmptyState
  >
</template>
