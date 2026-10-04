<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { discussions } from '../data/preview'
import PageHeading from '../components/PageHeading.vue'
import PanelCard from '../components/PanelCard.vue'
import EmptyState from '../components/EmptyState.vue'
const route = useRoute()
const discussion = computed(() => discussions.find((d) => d.id === route.params.id))
</script>
<template>
  <template v-if="discussion"
    ><div class="breadcrumbs">
      <RouterLink to="/discussions">讨论</RouterLink><span>/</span
      ><span>{{ discussion.category }}</span>
    </div>
    <PageHeading :title="discussion.title"
      ><el-tag effect="plain">{{ discussion.category }}</el-tag></PageHeading
    ><PanelCard title="正文" icon="reading" subtitle="示例文章"
      ><div class="detail-meta">
        <RouterLink :to="'/users/' + discussion.author">{{ discussion.author }}</RouterLink
        ><time>{{ discussion.date }}</time
        ><span>{{ discussion.views }} 次浏览</span>
      </div>
      <div class="detail-prose">
        <p>{{ discussion.content }}</p>
      </div>
      <RouterLink
        v-if="discussion.problemId"
        class="text-link"
        :to="'/problems/' + discussion.problemId"
        style="display: inline-block; margin-top: 24px"
        >查看相关题目 →</RouterLink
      >
      <div class="comment-placeholder">评论功能将在社区服务接入后开放。</div></PanelCard
    ></template
  ><EmptyState v-else title="讨论不存在"
    ><RouterLink to="/discussions"><el-button>返回讨论列表</el-button></RouterLink></EmptyState
  >
</template>
