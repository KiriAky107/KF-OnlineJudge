<script setup lang="ts">
import { computed } from 'vue'
import { ElButton, ElCard, ElTag } from 'element-plus'
import { usePlatformHealth } from '../composables/usePlatformHealth'

const { state, version, refresh } = usePlatformHealth()
const statusText = computed(
  () =>
    ({
      loading: '正在连接平台',
      connected: '平台已连接',
      unavailable: '平台暂未连接',
    })[state.value],
)
</script>

<template>
  <section class="welcome">
    <el-tag effect="plain"> 开发预览 </el-tag>
    <h1>从一道题开始，<br />积累每一次进步。</h1>
    <p class="welcome-description">
      KF-OnlineJudge 正在建设中。题目练习、提交评测和个人学习记录将在后续版本开放。
    </p>
    <el-card shadow="never" class="connection-card">
      <div class="connection-row">
        <div>
          <h2>平台连接</h2>
          <p aria-live="polite">
            {{ statusText }}<span v-if="version && state === 'connected'"> · {{ version }}</span>
          </p>
        </div>
        <el-button :loading="state === 'loading'" @click="refresh"> 重新检查 </el-button>
      </div>
    </el-card>
  </section>
</template>
