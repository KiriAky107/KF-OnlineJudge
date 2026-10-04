<script setup lang="ts">
import { computed, ref } from 'vue'
import { trainings } from '../data/preview'
import PageHeading from '../components/PageHeading.vue'
import DifficultyTag from '../components/DifficultyTag.vue'
const category = ref('全部')
const query = ref('')
const items = computed(() =>
  trainings.filter(
    (t) =>
      (category.value === '全部' || t.category === category.value) && t.title.includes(query.value),
  ),
)
</script>
<template>
  <PageHeading title="训练计划" eyebrow="TRAINING" description="从基础到专题，让练习更有顺序。" />
  <div class="intro-banner">
    <div>
      <p class="eyebrow">ONE STEP AT A TIME</p>
      <h2>循序渐进，把知识点练扎实。</h2>
      <p>选择适合自己的题单，先理解问题，再尝试独立完成。训练记录将在账号与评测服务接入后开启。</p>
    </div>
    <span class="intro-number">{ }</span>
  </div>
  <div class="filters">
    <el-radio-group v-model="category"
      ><el-radio-button
        v-for="c in ['全部', '基础入门', '算法专题', '进阶练习']"
        :key="c"
        :value="c"
        >{{ c }}</el-radio-button
      ></el-radio-group
    ><el-input v-model="query" placeholder="搜索训练题单" aria-label="搜索训练" clearable />
  </div>
  <div class="card-grid">
    <article v-for="training in items" :key="training.id" class="training-card">
      <div class="training-illustration" :class="training.color">{{ training.icon }}</div>
      <div style="flex: 1">
        <div class="tag-list">
          <span class="tag">{{ training.category }}</span
          ><DifficultyTag :value="training.level" />
        </div>
        <h2>
          <RouterLink :to="'/trainings/' + training.id">{{ training.title }}</RouterLink>
        </h2>
        <p>{{ training.description }}</p>
        <div class="card-meta">
          <span>{{ training.ids.length }} 道题目</span><span>示例题单</span
          ><RouterLink class="text-link" :to="'/trainings/' + training.id">开始训练 →</RouterLink>
        </div>
      </div>
    </article>
  </div>
  <el-empty v-if="!items.length" description="没有符合条件的训练" />
</template>
