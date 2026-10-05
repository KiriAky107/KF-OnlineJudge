<script setup lang="ts">
import { computed, ref } from 'vue'
import { discussions, rankings } from '../data/preview'
import PageHeading from '../components/PageHeading.vue'
import PanelCard from '../components/PanelCard.vue'
const category = ref('全部')
const query = ref('')
const items = computed(() =>
  discussions.filter(
    (d) =>
      (category.value === '全部' || d.category === category.value) && d.title.includes(query.value),
  ),
)
</script>
<template>
  <PageHeading
    title="讨论社区"
    eyebrow="COMMUNITY"
    description="分享思路，提出问题，一起把代码写得更好。"
    ><el-tooltip content="讨论发布将在账号服务接入后开放"
      ><el-button type="primary" disabled>发起讨论</el-button></el-tooltip
    ></PageHeading
  >
  <div class="content-with-aside">
    <PanelCard title="最新讨论" icon="discussions" subtitle="示例文章"
      ><div class="filters">
        <el-radio-group v-model="category"
          ><el-radio-button
            v-for="c in ['全部', '交流', '问答', '题解', '反馈']"
            :key="c"
            :value="c"
            >{{ c }}</el-radio-button
          ></el-radio-group
        ><el-input v-model="query" placeholder="搜索讨论" aria-label="搜索讨论" clearable />
      </div>
      <article v-for="discussion in items" :key="discussion.id" class="discussion-item">
        <RouterLink
          :to="'/users/' + discussion.author"
          class="avatar"
          :style="{ background: rankings.find((r) => r.username === discussion.author)?.color }"
          >{{ discussion.author[0] }}</RouterLink
        >
        <div>
          <h2>
            <el-tag v-if="discussion.pinned" size="small" effect="plain">置顶</el-tag
            ><RouterLink :to="'/discussions/' + discussion.id">{{ discussion.title }}</RouterLink>
          </h2>
          <p>
            <span class="tag">{{ discussion.category }}</span
            ><RouterLink :to="'/users/' + discussion.author">{{ discussion.author }}</RouterLink
            ><span>{{ discussion.date }}</span>
          </p>
        </div>
        <div class="discussion-numbers">
          <span
            ><strong>{{ discussion.replies }}</strong> 回复</span
          ><span>{{ discussion.views }} 浏览</span>
        </div>
      </article>
      <el-empty v-if="!items.length" description="没有符合条件的讨论"
    /></PanelCard>
    <div class="stack">
      <PanelCard title="欢迎交流" icon="reading"
        ><p class="small muted">
          提问时写清楚题号、语言、报错和已经尝试过的思路。分享题解时，也可以记录从哪里开始卡住，以及后来怎样找到解法。
        </p></PanelCard
      ><PanelCard title="社区分区" icon="folder"
        ><dl class="details-list">
          <div v-for="c in ['交流', '问答', '题解', '反馈']" :key="c">
            <dt>{{ c }}</dt>
            <dd>{{ discussions.filter((d) => d.category === c).length }} 篇示例文章</dd>
          </div>
        </dl></PanelCard
      >
    </div>
  </div>
</template>
