<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import PageHeading from '../components/PageHeading.vue'
import PanelCard from '../components/PanelCard.vue'
import { adminSections } from './navigation'
const route = useRoute()
const section = computed(() => String(route.params.section || 'overview'))
const current = computed(() => adminSections.find((item) => item.id === section.value))
</script>
<template>
  <PageHeading
    :title="current?.title ?? '页面不存在'"
    eyebrow="ADMINISTRATION"
    description="题目维护与审计已接通真实服务。"
  />
  <PanelCard v-if="section === 'overview'" title="管理工作台" icon="connection"
    ><p>当前可维护不可变题目版本、上传并校验题包、下架或恢复题目，以及查询维护审计。</p>
    <RouterLink to="/problems">打开题目维护</RouterLink>
    <p>比赛、用户管理、评测节点和站点设置仍在开发中。</p></PanelCard
  >
  <PanelCard v-else-if="current" :title="`${current.title} · 预览`" :icon="current.icon"
    ><p>本区尚未接入业务接口，暂无真实统计或写入操作。</p>
    <button disabled>尚未开放</button></PanelCard
  >
  <PanelCard v-else title="无此管理页面" icon="connection"
    ><RouterLink to="/">返回管理工作台</RouterLink></PanelCard
  >
</template>
