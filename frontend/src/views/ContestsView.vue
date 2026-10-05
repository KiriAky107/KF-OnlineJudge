<script setup lang="ts">
import { computed, ref } from 'vue'
import { contests } from '../data/preview'
import PanelCard from '../components/PanelCard.vue'
import PageHeading from '../components/PageHeading.vue'
const status = ref('全部')
const query = ref('')
const type = ref('')
const items = computed(() =>
  contests.filter(
    (c) =>
      (status.value === '全部' || c.status === status.value) &&
      (!type.value || c.type === type.value) &&
      c.title.includes(query.value),
  ),
)
</script>
<template>
  <PageHeading title="比赛" eyebrow="CONTESTS" description="在有限的时间里，挑战新的问题。" />
  <PanelCard title="比赛列表" icon="contests" subtitle="示例赛程"
    ><div class="filters">
      <el-radio-group v-model="status"
        ><el-radio-button
          v-for="s in ['全部', '即将开始', '进行中', '已结束']"
          :key="s"
          :value="s"
          >{{ s }}</el-radio-button
        ></el-radio-group
      ><el-input v-model="query" placeholder="搜索比赛" aria-label="搜索比赛" clearable /><el-select
        v-model="type"
        placeholder="全部赛制"
        aria-label="赛制"
        clearable
        ><el-option label="ACM" value="ACM" /><el-option label="OI" value="OI"
      /></el-select>
    </div>
    <el-table :data="items" stripe empty-text="没有符合条件的比赛"
      ><el-table-column label="比赛名称" min-width="270"
        ><template #default="{ row }"
          ><RouterLink class="table-link" :to="'/contests/' + row.id">{{
            row.title
          }}</RouterLink></template
        ></el-table-column
      ><el-table-column label="赛制" width="85" prop="type" /><el-table-column
        label="状态"
        width="120"
        ><template #default="{ row }"
          ><el-tag
            size="small"
            :type="
              row.status === '进行中' ? 'success' : row.status === '已结束' ? 'info' : 'primary'
            "
            effect="light"
            >{{ row.status }}</el-tag
          ></template
        ></el-table-column
      ><el-table-column label="开始时间" prop="start" min-width="160" /><el-table-column
        label="时长"
        prop="duration"
        width="100"
      /><el-table-column label="题目" width="90"
        ><template #default="{ row }">{{ row.ids.length }} 道</template></el-table-column
      ><el-table-column label="" width="100"
        ><template #default="{ row }"
          ><RouterLink class="text-link" :to="'/contests/' + row.id"
            >查看比赛 →</RouterLink
          ></template
        ></el-table-column
      ></el-table
    >
    <p class="tab-summary">共 {{ items.length }} 场比赛</p></PanelCard
  >
</template>
