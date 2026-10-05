<script setup lang="ts">
import { computed, ref } from 'vue'
import PageHeading from '../components/PageHeading.vue'
const query = ref('')
const teams = [
  {
    name: 'KF 算法研习组',
    initials: 'KF',
    description: '一起练习基础算法，分享解题笔记。',
    members: 12,
    tags: ['基础训练', '算法交流'],
  },
  {
    name: 'Rust 解题小队',
    initials: 'RS',
    description: '用 Rust 完成题目，交流语言与算法的使用经验。',
    members: 8,
    tags: ['Rust', '专题练习'],
  },
  {
    name: '周末练习伙伴',
    initials: 'AC',
    description: '每周留一点时间，认真做几道题。',
    members: 16,
    tags: ['练习赛', '学习打卡'],
  },
]
const items = computed(() =>
  teams.filter((t) => t.name.toLowerCase().includes(query.value.toLowerCase())),
)
</script>
<template>
  <PageHeading title="团队" eyebrow="TEAMS" description="找到一起练习和交流的伙伴。"
    ><RouterLink to="/teams/create"
      ><el-button type="primary">创建团队</el-button></RouterLink
    ></PageHeading
  >
  <div class="filters">
    <el-input v-model="query" placeholder="搜索团队" aria-label="搜索团队" clearable /><span
      class="filter-label"
      >示例团队 · 加入功能待接入</span
    >
  </div>
  <div class="card-grid">
    <article v-for="team in items" :key="team.name" class="panel team-card">
      <span class="team-logo">{{ team.initials }}</span>
      <div style="flex: 1">
        <h2>{{ team.name }}</h2>
        <p>{{ team.description }}</p>
        <div class="tag-list">
          <span v-for="tag in team.tags" :key="tag" class="tag">{{ tag }}</span>
        </div>
        <div class="card-meta" style="margin-top: 20px">
          <span>{{ team.members }} 名成员 · 示例</span
          ><el-button disabled size="small" style="margin-left: auto">申请加入</el-button>
        </div>
      </div>
    </article>
  </div>
  <el-empty v-if="!items.length" description="没有找到团队" />
</template>
